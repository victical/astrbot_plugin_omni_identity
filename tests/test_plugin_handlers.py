import asyncio
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from main import IdentityBinderPlugin, resolve_user_id


class MockContext:
    def __init__(self, data_dir: Path):
        self._data_dir = data_dir

    def get_data_dir(self):
        return self._data_dir


class MockEvent:
    def __init__(self, platform: str, sender_id: str, sender_name: str = ""):
        self.platform = platform
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.session_context = {}
        self._extras = {}
        self.results = []

    def get_platform_name(self) -> str:
        return self.platform

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_sender_name(self) -> str:
        return self.sender_name

    def set_extra(self, key: str, value):
        self._extras[key] = value

    def get_extra(self, key: str, default=None):
        return self._extras.get(key, default)

    def plain_result(self, text: str):
        return f"[RESULT]{text}"


class MockProviderRequest:
    def __init__(self, system_prompt: str = "你是智能助手", prompt: str = "你好"):
        self.system_prompt = system_prompt
        self.prompt = prompt


class TestPluginHandlers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_plugin_"))
        self.context = MockContext(self.test_dir)
        self.plugin = IdentityBinderPlugin(
            self.context,
            config={
                "code_expiry_seconds": 300,
                "code_cooldown_seconds": 0,
                "enable_identity_prompt_injection": True,
                "identity_prompt_target": "system",
            },
        )

    async def asyncTearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_on_message_early_injection(self):
        evt = MockEvent(platform="aiocqhttp", sender_id="1507955112", sender_name="Victical")
        await self.plugin.on_message_early(evt)

        expected_gid = "usr_aiocqhttp_1507955112"
        expected_puid = "1507955112"
        self.assertEqual(evt.session_context.get("global_user_id"), expected_gid)
        self.assertEqual(evt.session_context.get("primary_user_id"), expected_puid)
        self.assertEqual(evt.get_extra("global_user_id"), expected_gid)
        self.assertEqual(evt.get_extra("primary_user_id"), expected_puid)
        self.assertEqual(resolve_user_id(evt), expected_puid)
        self.assertEqual(resolve_user_id(evt, prefer_primary=False), expected_gid)

    async def test_cmd_link_generate_and_consume(self):
        # 1. Generate on QQ
        evt_qq = MockEvent(platform="aiocqhttp", sender_id="1507955112", sender_name="Victical")
        gen_results = [res async for res in self.plugin.cmd_link(evt_qq)]
        self.assertEqual(len(gen_results), 1)
        gen_text = gen_results[0]
        self.assertIn("验证码为：", gen_text)

        match = re.search(r"验证码为：(\d{6})", gen_text)
        self.assertIsNotNone(match)
        code = match.group(1)

        # 2. Consume on Telegram
        evt_tg = MockEvent(platform="telegram", sender_id="1697740260")
        consume_results = [res async for res in self.plugin.cmd_link(evt_tg, code=code)]
        self.assertEqual(len(consume_results), 1)
        self.assertIn("绑定成功", consume_results[0])
        self.assertIn("usr_aiocqhttp_1507955112", consume_results[0])

        # 3. Verify whoami on Telegram
        whoami_results = [res async for res in self.plugin.cmd_whoami(evt_tg)]
        self.assertEqual(len(whoami_results), 1)
        self.assertIn("usr_aiocqhttp_1507955112", whoami_results[0])
        self.assertIn("aiocqhttp: 1507955112", whoami_results[0])
        self.assertIn("telegram: 1697740260", whoami_results[0])

    async def test_cmd_unlink(self):
        # Bind Telegram to QQ
        evt_qq = MockEvent(platform="aiocqhttp", sender_id="1507955112")
        gen_results = [res async for res in self.plugin.cmd_link(evt_qq)]
        code = re.search(r"验证码为：(\d{6})", gen_results[0]).group(1)

        evt_tg = MockEvent(platform="telegram", sender_id="1697740260")
        [res async for res in self.plugin.cmd_link(evt_tg, code=code)]

        # 1. Attempt to unlink QQ while operating from QQ (must fail)
        unlink_self = [res async for res in self.plugin.cmd_unlink(evt_qq, platform="aiocqhttp")]
        self.assertIn("不可解绑当前操作的平台账号自身", unlink_self[0])

        # 2. Unlink Telegram from QQ (succeeds)
        unlink_tg = [res async for res in self.plugin.cmd_unlink(evt_qq, platform="telegram")]
        self.assertIn("解绑成功", unlink_tg[0])

        # 3. Verify whoami after unlinking
        whoami_results = [res async for res in self.plugin.cmd_whoami(evt_qq)]
        self.assertNotIn("telegram: 1697740260", whoami_results[0])

    async def test_on_llm_request_prompt_injection(self):
        # Setup: QQ (1507955112) bound with Bilibili (4028591)
        evt_qq = MockEvent(platform="aiocqhttp", sender_id="1507955112", sender_name="Victical")
        gen_results = [res async for res in self.plugin.cmd_link(evt_qq)]
        code = re.search(r"验证码为：(\d{6})", gen_results[0]).group(1)

        evt_bili = MockEvent(platform="bilibili", sender_id="4028591", sender_name="Victical")
        [res async for res in self.plugin.cmd_link(evt_bili, code=code)]

        # Trigger early message on Bilibili to update name & context
        await self.plugin.on_message_early(evt_bili)

        # Send LLM request from Bilibili
        req = MockProviderRequest(system_prompt="你的主人是Victical，QQ号是1507955112")
        await self.plugin.on_llm_request(evt_bili, req)

        # Check injected system prompt
        self.assertIn("【系统跨平台身份识别提示】", req.system_prompt)
        self.assertIn("B站 (Bilibili)", req.system_prompt)
        self.assertIn("4028591", req.system_prompt)
        self.assertIn("QQ 账号: 1507955112", req.system_prompt)
        self.assertIn("完全相同的同一个人", req.system_prompt)


if __name__ == "__main__":
    unittest.main()
