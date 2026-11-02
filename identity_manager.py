import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PendingCode:
    code: str
    source_platform: str
    source_user_id: str
    global_id: str
    expire_at: float
    created_at: float


class IdentityManager:
    """跨平台身份绑定管理器

    负责维护平台账号 (platform:user_id) 与全局统一身份 (global_id) 的双向映射关系，
    以及生成与核销动态验证码、安全解绑等生命周期管理。
    """

    def __init__(self, data_path: Path, code_expiry_seconds: int = 600, code_cooldown_seconds: int = 15):
        self.data_dir = Path(data_path)
        self.data_file = self.data_dir / "identity_bindings.json"
        self.code_expiry_seconds = code_expiry_seconds
        self.code_cooldown_seconds = code_cooldown_seconds

        self.pending_codes: Dict[str, PendingCode] = {}
        self.bindings: Dict[str, str] = {}
        self.identities: Dict[str, dict] = {}
        self._lock = threading.RLock()

        self._load()

    def _load(self) -> None:
        """从持久化文件加载绑定与身份数据"""
        with self._lock:
            if self.data_file.exists():
                try:
                    data = json.loads(self.data_file.read_text(encoding="utf-8"))
                    self.bindings = data.get("bindings", {})
                    self.identities = data.get("identities", {})
                except Exception as e:
                    # 若文件损坏，保留空结构并备份损坏文件
                    backup_path = self.data_dir / f"identity_bindings.corrupted.{int(time.time())}.json"
                    try:
                        self.data_file.rename(backup_path)
                    except Exception:
                        pass
                    self.bindings = {}
                    self.identities = {}

    def _save(self) -> None:
        """原子写入持久化数据文件"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self.data_dir / f"identity_bindings.{int(time.time() * 1000)}.tmp"
        payload = {
            "bindings": self.bindings,
            "identities": self.identities,
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(self.data_file)

    def _clean_expired_codes(self) -> None:
        """清理已过期的验证码"""
        now = time.time()
        expired = [k for k, v in self.pending_codes.items() if v.expire_at <= now]
        for k in expired:
            del self.pending_codes[k]

    def get_global_id(self, platform: str, user_id: str, display_name: Optional[str] = None) -> str:
        """获取或自动创建账号对应的全局身份标识 (global_id)"""
        platform = str(platform).strip()
        user_id = str(user_id).strip()
        key = f"{platform}:{user_id}"

        with self._lock:
            if key in self.bindings:
                gid = self.bindings[key]
                if gid in self.identities:
                    if display_name:
                        changed = False
                        ident = self.identities[gid]
                        if not ident.get("primary_name") or ident.get("primary_name") == f"{platform}_{user_id}":
                            ident["primary_name"] = display_name
                            changed = True
                        for acc in ident.get("accounts", []):
                            if acc["platform"] == platform and str(acc["user_id"]) == user_id:
                                if acc.get("nickname") != display_name:
                                    acc["nickname"] = display_name
                                    changed = True
                                break
                        if changed:
                            self._save()
                    return gid
                # 若 identity 丢失则自动补全
                self.identities[gid] = {
                    "primary_name": display_name or f"{platform}_{user_id}",
                    "created_at": time.time(),
                    "accounts": [{
                        "platform": platform,
                        "user_id": user_id,
                        "nickname": display_name or "",
                        "linked_at": time.time(),
                    }],
                }
                self._save()
                return gid

            # 首次访问，自动初始化全局身份
            new_gid = f"usr_{platform}_{user_id}"
            now = time.time()
            self.bindings[key] = new_gid
            self.identities[new_gid] = {
                "primary_name": display_name or f"{platform}_{user_id}",
                "created_at": now,
                "accounts": [{
                    "platform": platform,
                    "user_id": user_id,
                    "nickname": display_name or "",
                    "linked_at": now,
                }],
            }
            self._save()
            return new_gid

    def get_primary_user_id(self, platform: str, user_id: str) -> str:
        """获取已绑定身份体系中的主原生用户ID (如 QQ 号)，供下游插件复用既有存储数据"""
        platform = str(platform).strip()
        user_id = str(user_id).strip()
        key = f"{platform}:{user_id}"

        with self._lock:
            gid = self.bindings.get(key)
            if not gid or gid not in self.identities:
                return user_id

            accounts = self.identities[gid].get("accounts", [])
            if accounts and isinstance(accounts, list) and len(accounts) > 0:
                first_uid = accounts[0].get("user_id")
                if first_uid:
                    return str(first_uid)
            return user_id

    def create_link_code(self, platform: str, user_id: str) -> Tuple[bool, str, int]:
        """为当前平台账号生成绑定验证码。

        Returns:
            Tuple[bool, str, int]: (成功与否, 验证码或错误提示, 剩余有效秒数)
        """
        platform = str(platform).strip()
        user_id = str(user_id).strip()
        now = time.time()

        with self._lock:
            self._clean_expired_codes()

            # 检查该用户是否已有有效验证码
            for existing_code, info in list(self.pending_codes.items()):
                if info.source_platform == platform and info.source_user_id == user_id:
                    # 如果在冷却期内，直接返回已有验证码
                    remaining = int(info.expire_at - now)
                    if remaining > 0 and (now - info.created_at < self.code_cooldown_seconds):
                        return True, existing_code, remaining
                    # 超过冷却期，移除旧码生成新码
                    del self.pending_codes[existing_code]

            gid = self.get_global_id(platform, user_id)

            # 生成 6 位随机数字码，保证全局唯一
            for _ in range(10):
                code = f"{random.randint(100000, 999999)}"
                if code not in self.pending_codes:
                    break
            else:
                code = f"{int(time.time() * 1000) % 900000 + 100000}"

            self.pending_codes[code] = PendingCode(
                code=code,
                source_platform=platform,
                source_user_id=user_id,
                global_id=gid,
                expire_at=now + self.code_expiry_seconds,
                created_at=now,
            )
            return True, code, self.code_expiry_seconds

    def consume_link_code(self, code: str, target_platform: str, target_user_id: str) -> Tuple[bool, str]:
        """在新平台核销验证码，将当前账号合并绑定至发起方的全局身份。

        Returns:
            Tuple[bool, str]: (成功与否, 统一身份 global_id 或 错误提示信息)
        """
        code = str(code).strip()
        target_platform = str(target_platform).strip()
        target_user_id = str(target_user_id).strip()
        now = time.time()

        with self._lock:
            self._clean_expired_codes()

            info = self.pending_codes.get(code)
            if not info or info.expire_at <= now:
                return False, "验证码不存在或已过期"

            # 禁止绑定自身相同账号
            if info.source_platform == target_platform and info.source_user_id == target_user_id:
                return False, "当前账号为验证码生成方，无需绑定自身"

            master_gid = info.global_id
            target_key = f"{target_platform}:{target_user_id}"
            old_target_gid = self.bindings.get(target_key)

            # 如果已处于同一身份下
            if old_target_gid == master_gid:
                del self.pending_codes[code]
                return True, master_gid

            # 确保主身份存在
            if master_gid not in self.identities:
                self.identities[master_gid] = {
                    "primary_name": f"{info.source_platform}_{info.source_user_id}",
                    "created_at": now,
                    "accounts": [{
                        "platform": info.source_platform,
                        "user_id": info.source_user_id,
                        "linked_at": now,
                    }],
                }

            master_accounts = self.identities[master_gid]["accounts"]

            # 级联合并：如果目标账号此前属于另一个已有全局身份，需迁移该身份下的所有关联账号
            if old_target_gid and old_target_gid != master_gid and old_target_gid in self.identities:
                old_ident = self.identities[old_target_gid]
                old_accounts = old_ident.get("accounts", [])

                for acc in old_accounts:
                    p = acc["platform"]
                    u = acc["user_id"]
                    self.bindings[f"{p}:{u}"] = master_gid
                    if not any(a["platform"] == p and str(a["user_id"]) == str(u) for a in master_accounts):
                        master_accounts.append({
                            "platform": p,
                            "user_id": u,
                            "linked_at": acc.get("linked_at", now),
                        })

                # 删除旧的独立身份，避免孤儿记录
                del self.identities[old_target_gid]
            else:
                # 目标账号无旧身份或旧身份未注册完整
                self.bindings[target_key] = master_gid
                if not any(a["platform"] == target_platform and str(a["user_id"]) == target_user_id for a in master_accounts):
                    master_accounts.append({
                        "platform": target_platform,
                        "user_id": target_user_id,
                        "linked_at": now,
                    })

            # 核销验证码
            del self.pending_codes[code]
            self._save()
            return True, master_gid

    def unlink_account(
        self,
        operator_platform: str,
        operator_user_id: str,
        target_platform: str,
        target_user_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """解绑指定平台的关联账号。

        约束：不可解绑当前正在操作的发起方平台账号自身。

        Returns:
            Tuple[bool, str]: (成功与否, 提示信息)
        """
        operator_platform = str(operator_platform).strip()
        operator_user_id = str(operator_user_id).strip()
        target_platform = str(target_platform).strip()
        target_user_id = str(target_user_id).strip() if target_user_id else None

        with self._lock:
            op_key = f"{operator_platform}:{operator_user_id}"
            gid = self.bindings.get(op_key)
            if not gid or gid not in self.identities:
                return False, "当前账号尚未初始化身份"

            ident = self.identities[gid]
            accounts: List[dict] = ident.get("accounts", [])

            # 禁止解绑当前操作账号自身
            if target_platform.lower() == operator_platform.lower():
                if target_user_id is None or str(target_user_id) == operator_user_id:
                    return False, "不可解绑当前操作的平台账号自身"

            # 查找目标匹配账号
            matched: List[dict] = []
            for acc in accounts:
                if acc["platform"].lower() == target_platform.lower():
                    if target_user_id is None or str(acc["user_id"]) == target_user_id:
                        matched.append(acc)

            if not matched:
                return False, f"在当前身份下未找到平台【{target_platform}】的已绑定账号"

            if len(matched) > 1:
                uids = ", ".join([str(a["user_id"]) for a in matched])
                return False, f"检测到平台【{target_platform}】下有多个账号（{uids}），请指定 UID，格式：/unlink {target_platform} <user_id>"

            target_acc = matched[0]
            target_p = target_acc["platform"]
            target_u = target_acc["user_id"]

            # 从当前主身份的 accounts 列表中剔除
            ident["accounts"] = [
                a for a in accounts
                if not (a["platform"] == target_p and str(a["user_id"]) == str(target_u))
            ]

            # 为被解绑账号赋予全新的独立 global_id
            now = time.time()
            new_gid = f"usr_{target_p}_{target_u}"
            self.bindings[f"{target_p}:{target_u}"] = new_gid
            self.identities[new_gid] = {
                "primary_name": target_acc.get("nickname") or f"{target_p}_{target_u}",
                "created_at": now,
                "accounts": [{
                    "platform": target_p,
                    "user_id": target_u,
                    "nickname": target_acc.get("nickname") or "",
                    "linked_at": now,
                }],
            }

            self._save()
            return True, f"成功解绑平台【{target_p}】账号（UID: {target_u}）"

    def get_identity_info(self, platform: str, user_id: str) -> Optional[dict]:
        """获取指定账号的身份信息及关联账号列表"""
        platform = str(platform).strip()
        user_id = str(user_id).strip()
        key = f"{platform}:{user_id}"

        with self._lock:
            gid = self.bindings.get(key)
            if not gid:
                gid = self.get_global_id(platform, user_id)
            ident = self.identities.get(gid)
            if not ident:
                return None
            return {
                "global_id": gid,
                "primary_name": ident.get("primary_name", ""),
                "created_at": ident.get("created_at", 0),
                "accounts": list(ident.get("accounts", [])),
            }
