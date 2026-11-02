from pathlib import Path
from typing import Optional

try:
    from astrbot.api import AstrBotConfig, logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, register
except ImportError:
    import logging
    logger = logging.getLogger("astrbot_plugin_omni_identity")

    class AstrBotConfig(dict):
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    def register(*args, **kwargs):
        def dec(cls):
            return cls
        return dec

    class AstrMessageEvent:
        pass

    class _Filter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def event_message_type(*args, **kwargs):
            def dec(fn):
                return fn
            return dec

        @staticmethod
        def command(*args, **kwargs):
            def dec(fn):
                return fn
            return dec

        @staticmethod
        def on_llm_request(*args, **kwargs):
            def dec(fn):
                return fn
            return dec

    filter = _Filter()

try:
    from .identity_manager import IdentityManager
except ImportError:
    from identity_manager import IdentityManager


PLATFORM_DISPLAY_NAMES = {
    "aiocqhttp": "QQ",
    "qq": "QQ",
    "qq_official": "QQ官方",
    "bilibili": "B站 (Bilibili)",
    "telegram": "Telegram",
    "tg": "Telegram",
    "wechat": "微信",
    "gewechat": "微信",
    "discord": "Discord",
    "slack": "Slack",
    "lark": "飞书",
    "feishu": "飞书",
}


def format_platform_name(platform: str) -> str:
    """转换平台内部代码为人类友好的平台名称"""
    p = str(platform or "").strip().lower()
    return PLATFORM_DISPLAY_NAMES.get(p, platform)


def build_identity_prompt(
    current_platform: str,
    current_user_id: str,
    primary_name: str,
    accounts: list,
) -> str:
    """构建注入给大模型的跨平台身份认知提示词"""
    current_plat_display = format_platform_name(current_platform)
    other_accounts = [
        a for a in accounts
        if not (a.get("platform") == current_platform and str(a.get("user_id")) == str(current_user_id))
    ]
    if not other_accounts:
        return ""

    acc_lines = []
    other_platform_names = []
    for a in other_accounts:
        plat_disp = format_platform_name(a.get("platform", ""))
        if plat_disp not in other_platform_names:
            other_platform_names.append(plat_disp)
        nick_str = f"（昵称: {a['nickname']}）" if a.get("nickname") else ""
        acc_lines.append(f"  • {plat_disp} 账号: {a.get('user_id')}{nick_str}")

    other_acc_text = "\n".join(acc_lines)
    other_plats_text = " / ".join(other_platform_names)

    prompt = (
        f"【系统跨平台身份识别提示】\n"
        f"当前发信用户正在使用【{current_plat_display}】（账号/UID: {current_user_id}）。\n"
        f"系统已验证该用户已绑定全局身份【{primary_name}】，TA 在其他平台的绑定账号包括：\n"
        f"{other_acc_text}\n"
        f"【核心指令】当前发信用户与上述绑定的各平台账号为完全相同的同一个人！"
        f"请直接将 TA 视为你在 {other_plats_text} 上的那位好友/主人，"
        f"严格保持完全相同的亲密度、称呼习惯、语气风格与人设关系，绝不可表现出陌生、生疏、客套或初次相识的态度。"
    )
    return prompt


def resolve_user_id(event: AstrMessageEvent, prefer_primary: bool = True) -> str:
    """供下游插件（Engram / Affinity 等）调用的标准化用户标识解析方法。

    解析顺序：
    1. 若 prefer_primary 为 True（默认）：优先使用 primary_user_id（无缝复用下游插件已有数据库记录）
    2. event.session_context["global_user_id"]
    3. event.get_extra("global_user_id")
    4. 原生平台 event.get_sender_id()
    """
    if event is None:
        return ""
    if hasattr(event, "session_context") and isinstance(event.session_context, dict):
        if prefer_primary:
            puid = event.session_context.get("primary_user_id")
            if puid:
                return str(puid)
        gid = event.session_context.get("global_user_id")
        if gid:
            return str(gid)
    if hasattr(event, "get_extra") and callable(event.get_extra):
        if prefer_primary:
            puid = event.get_extra("primary_user_id")
            if puid:
                return str(puid)
        gid = event.get_extra("global_user_id")
        if gid:
            return str(gid)
    if hasattr(event, "get_sender_id") and callable(event.get_sender_id):
        return str(event.get_sender_id() or "")
    return ""


def _resolve_data_dir(context: Optional[Context] = None) -> Path:
    """解析并确保插件数据持久化目录"""
    if context and hasattr(context, "get_data_dir") and callable(getattr(context, "get_data_dir")):
        try:
            p = Path(context.get_data_dir())
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass

    try:
        from astrbot.api.star import StarTools

        p = Path(StarTools.get_data_dir("astrbot_plugin_omni_identity"))
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        pass

    fallback = Path(__file__).resolve().parent / "data"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


@register("astrbot_plugin_omni_identity", "Victical", "跨平台身份统一与指令绑定插件", "1.1.0")
class IdentityBinderPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        code_expiry = int(self._get_config_val("code_expiry_seconds", 600))
        code_cooldown = int(self._get_config_val("code_cooldown_seconds", 15))
        self.enable_prompt_injection = bool(self._get_config_val("enable_identity_prompt_injection", True))
        self.prompt_target = str(self._get_config_val("identity_prompt_target", "system")).strip().lower()

        data_dir = _resolve_data_dir(context)
        self.mgr = IdentityManager(
            data_path=data_dir,
            code_expiry_seconds=code_expiry,
            code_cooldown_seconds=code_cooldown,
        )
        logger.info(f"OmniIdentity: 身份绑定插件已初始化，数据存储路径: {data_dir}，提示词动态注入: {self.enable_prompt_injection}")

    def _get_config_val(self, key: str, default):
        if hasattr(self.config, "get") and callable(self.config.get):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    # 最高优先级：在消息流转前注入 global_id
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message_early(self, event: AstrMessageEvent):
        try:
            platform_name = event.get_platform_name()
            sender_id = event.get_sender_id()
            if not platform_name or not sender_id:
                return

            sender_name = event.get_sender_name() if hasattr(event, "get_sender_name") and callable(event.get_sender_name) else None
            gid = self.mgr.get_global_id(platform_name, sender_id, display_name=sender_name)
            puid = self.mgr.get_primary_user_id(platform_name, sender_id)

            # 写入会话运行时上下文，供 downstream 插件（engram/affinity）提取
            if not hasattr(event, "session_context") or not isinstance(event.session_context, dict):
                event.session_context = {}
            event.session_context["global_user_id"] = gid
            event.session_context["primary_user_id"] = puid

            # 额外写入 event._extras，确保两种下游读取方式完全互通
            if hasattr(event, "set_extra") and callable(event.set_extra):
                event.set_extra("global_user_id", gid)
                event.set_extra("primary_user_id", puid)
        except Exception as e:
            logger.warning(f"OmniIdentity: on_message_early 注入 global_id 异常: {e}")

    # 当发起大模型请求时，向 Prompt 动态注入跨平台身份认知
    @filter.on_llm_request(priority=10)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        if not self.enable_prompt_injection:
            return

        try:
            platform_name = event.get_platform_name()
            sender_id = event.get_sender_id()
            if not platform_name or not sender_id:
                return

            info = self.mgr.get_identity_info(platform_name, sender_id)
            if not info:
                return

            accounts = info.get("accounts", [])
            # 仅当绑定了 2 个及以上账号（跨平台绑定）时进行提示词注入
            if len(accounts) <= 1:
                return

            primary_name = info.get("primary_name") or f"{platform_name}_{sender_id}"
            identity_prompt = build_identity_prompt(
                current_platform=platform_name,
                current_user_id=sender_id,
                primary_name=primary_name,
                accounts=accounts,
            )

            if not identity_prompt:
                return

            # 根据配置注入到 system_prompt 或 prompt (user)
            attr = "prompt" if self.prompt_target in {"user", "prompt", "user_prompt"} else "system_prompt"
            existing = getattr(req, attr, None) or ""
            setattr(req, attr, f"{existing}\n\n{identity_prompt}" if existing else identity_prompt)

            logger.info(
                f"OmniIdentity: 已向 {attr} 注入跨平台身份认知提示词 "
                f"(用户: {primary_name}, 当前平台: {platform_name}, 关联账号数: {len(accounts)})"
            )
        except Exception as e:
            logger.warning(f"OmniIdentity: on_llm_request 注入身份提示词异常: {e}")

    @filter.command("link")
    async def cmd_link(self, event: AstrMessageEvent, code: Optional[str] = None):
        """跨平台身份绑定指令：
        /link          - 生成 6 位身份绑定验证码（有效期 10 分钟）
        /link <code>   - 在新平台输入验证码，将当前账号合并至主身份
        """
        platform_name = event.get_platform_name()
        sender_id = event.get_sender_id()
        if not sender_id:
            yield event.plain_result("无法识别当前平台的发送者标识。")
            return

        clean_code = str(code).strip() if code else ""
        if not clean_code:
            ok, res_code, remaining = self.mgr.create_link_code(platform_name, sender_id)
            if not ok:
                yield event.plain_result(f"生成验证码失败：{res_code}")
                return
            yield event.plain_result(
                f"【身份绑定验证码】\n"
                f"您的验证码为：{res_code}\n"
                f"请在需要关联的目标平台向我发送：/link {res_code}\n"
                f"（有效时间剩余 {max(1, int(remaining // 60))} 分钟）"
            )
        else:
            ok, msg = self.mgr.consume_link_code(clean_code, platform_name, sender_id)
            if ok:
                yield event.plain_result(
                    f"绑定成功！当前账号已合并至统一身份【{msg}】。\n"
                    f"跨平台的记忆、互动记录与上下文已为您打通。"
                )
            else:
                yield event.plain_result(f"绑定失败：{msg}")

    @filter.command("whoami")
    async def cmd_whoami(self, event: AstrMessageEvent):
        """查询当前平台身份、绑定的 global_id 及关联的所有平台账号列表"""
        platform_name = event.get_platform_name()
        sender_id = event.get_sender_id()
        if not sender_id:
            yield event.plain_result("无法识别当前平台的发送者标识。")
            return

        info = self.mgr.get_identity_info(platform_name, sender_id)
        if not info:
            yield event.plain_result("未查询到您的身份信息。")
            return

        gid = info["global_id"]
        accounts = info.get("accounts", [])
        acc_lines = []
        for acc in accounts:
            is_current = (acc["platform"] == platform_name and str(acc["user_id"]) == str(sender_id))
            mark = " (当前平台)" if is_current else ""
            acc_lines.append(f"  • {acc['platform']}: {acc['user_id']}{mark}")

        acc_text = "\n".join(acc_lines) if acc_lines else "  暂无关联账号"
        yield event.plain_result(
            f"【全局统一身份信息】\n"
            f"统一标识 (GID): {gid}\n"
            f"主账号名称: {info.get('primary_name')}\n"
            f"已关联平台账号 ({len(accounts)} 个):\n{acc_text}"
        )

    @filter.command("unlink")
    async def cmd_unlink(self, event: AstrMessageEvent, platform: Optional[str] = None, user_id: Optional[str] = None):
        """解绑指定的平台账号（不可解绑当前操作账号自身）：
        /unlink <platform> [user_id]
        """
        operator_platform = event.get_platform_name()
        operator_user_id = event.get_sender_id()
        if not operator_user_id:
            yield event.plain_result("无法识别当前平台的发送者标识。")
            return

        if not platform:
            yield event.plain_result(
                "请指定需要解绑的平台名称。\n"
                "用法示例：\n"
                "  /unlink telegram\n"
                "  /unlink bilibili 4028591"
            )
            return

        ok, msg = self.mgr.unlink_account(
            operator_platform=operator_platform,
            operator_user_id=operator_user_id,
            target_platform=platform,
            target_user_id=user_id,
        )
        if ok:
            yield event.plain_result(f"解绑成功：{msg}")
        else:
            yield event.plain_result(f"解绑失败：{msg}")
