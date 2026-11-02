# AstrBot 跨平台身份统一绑定插件 (Omni Identity Binder)

为 AstrBot 提供多平台（QQ、Telegram、B站私信、Discord、微信等）用户身份统一映射与指令绑定能力。通过动态验证码机制，将用户在不同平台上的原生账号绑定合并到同一全局身份（`global_id`），实现记忆、用户画像、亲密度等上下文的跨平台无缝同步。

---

## 🌟 核心特性

- **零配置自动初始化**：新用户在任意平台首次交互时，自动初始化唯一的虚拟全局身份（`usr_{platform}_{user_id}`）。
- **极简双向验证码绑定**：聊天窗口内使用 `/link` 即可生成 6 位一次性动态验证码，在目标平台输入 `/link <code>` 即可秒级合并。
- **级联合并与防孤儿引用**：当两个已各自关联其他账号的身份合并时，自动进行全量账号级联迁移，彻底避免孤儿数据。
- **安全解绑保护**：支持 `/unlink <platform>` 安全解绑指定平台，禁止误解绑当前操作账号自身，被解绑账号独立分离为新身份。
- **向下游透明注入**：通过最高优先级拦截器，自动将 `global_id` 注入到 `event.session_context["global_user_id"]` 和 `event.get_extra("global_user_id")` 中，下游插件无需关心底层平台映射。
- **大模型动态身份认知注入**：通过 `on_llm_request` 钩子，当用户在任一已绑定的平台发言时，自动向大模型注入该用户在所有平台的绑定身份背景，模型立刻认出并保持完全一致的态度与语气，无需手动修改人设提示词。
- **原子化持久化**：采用临时文件原子替换保存（Atomic Rename），杜绝断电或重启导致 JSON 损坏。

---

## 📖 指令规范

| 指令 | 作用域 | 行为描述 |
| :--- | :--- | :--- |
| `/link` | 已认证或初始账号 | 生成 6 位纯数字绑定验证码，有效期 10 分钟（防刷冷却 15 秒）。 |
| `/link <code>` | 待绑定新平台账号 | 验证并核销验证码，将当前账号与主账号合并绑定。 |
| `/whoami` | 全平台 | 查询当前平台身份、统一标识 `global_id` 及关联的所有平台账号列表。 |
| `/unlink <platform> [uid]` | 全平台 | 解绑指定的平台账号（不可解绑当前操作账号自身）。 |

### 使用示例

1. **场景 1：在 QQ 上发起，绑定 Telegram 账号**
   - 在 QQ 聊天框发送：`/link`
     > Bot 回复：【身份绑定验证码】您的验证码为：【123456】，请在需要绑定的平台向我发送：/link 123456
   - 切换到 Telegram 向 Bot 发送：`/link 123456`
     > Bot 回复：绑定成功！当前账号已合并至统一身份【usr_aiocqhttp_1507955112】。
   - 在任一平台发送 `/whoami`：
     > Bot 回复当前 GID 以及已绑定的 QQ 和 Telegram 账号。

2. **场景 2：解除某平台绑定**
   - 在 QQ 发送：`/unlink telegram`
     > Bot 回复：成功解绑平台【telegram】账号（UID: 1697740260）

---

## 🛠️ 下游插件对接规范（Engram / Affinity 等）

下游插件若需支持跨平台统一身份，请遵循以下规范：

### 1. 解析统一用户标识
下游插件统一优先读取 `session_context` 中的 `global_user_id`：

```python
from astrbot.api.event import AstrMessageEvent

def resolve_user_id(event: AstrMessageEvent) -> str:
    """获取跨平台全局用户 ID（回退原生 sender_id）"""
    if hasattr(event, "session_context") and isinstance(event.session_context, dict):
        gid = event.session_context.get("global_user_id")
        if gid:
            return str(gid)
    if hasattr(event, "get_extra") and callable(event.get_extra):
        gid = event.get_extra("global_user_id")
        if gid:
            return str(gid)
    return str(event.get_sender_id() or "")
```

或者直接导入本插件提供的标准化解析函数：
```python
try:
    from astrbot_plugin_omni_identity import resolve_user_id
except ImportError:
    def resolve_user_id(event):
        return event.get_sender_id()
```

### 2. 存储与隔离建议
- **私聊（Direct Message）**：记忆、画像统一以 `resolve_user_id(event)` 命名与索引，跨平台私信直接共享。
- **群聊（Group Message）**：群上下文仍以 `group_id` 为边界隔离，用户画像和情感值根据 `resolve_user_id(event)` 全局累计。

---

## 📁 数据存储格式 (`data/identity_bindings.json`)

```json
{
  "bindings": {
    "aiocqhttp:1507955112": "usr_victical_001",
    "bilibili:4028591": "usr_victical_001",
    "telegram:1697740260": "usr_victical_001"
  },
  "identities": {
    "usr_victical_001": {
      "primary_name": "Victical",
      "created_at": 1727784000.0,
      "accounts": [
        {"platform": "aiocqhttp", "user_id": "1507955112", "linked_at": 1727784000.0},
        {"platform": "bilibili", "user_id": "4028591", "linked_at": 1727784500.0},
        {"platform": "telegram", "user_id": "1697740260", "linked_at": 1727784900.0}
      ]
    }
  }
}
```

---

## ⚙️ 配置项说明 (`_conf_schema.json`)

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `code_expiry_seconds` | int | 600 | 验证码有效时长（秒），默认 10 分钟 |
| `code_cooldown_seconds` | int | 15 | 验证码生成冷却时间（秒），防止频繁刷码 |
| `enable_identity_prompt_injection` | bool | true | 是否向大模型动态注入跨平台身份认知提示词 |
| `identity_prompt_target` | string | "system" | 提示词注入位置：`system`（人设系统提示词）或 `user`（当次用户发言） |

---

## 📄 License

本项目采用 **GNU Affero General Public License v3.0 (AGPL-3.0)**，与 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 主程序保持一致。

Copyright (C) 2026 Victical
