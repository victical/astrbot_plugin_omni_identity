import shutil
import tempfile
import time
import unittest
from pathlib import Path

from identity_manager import IdentityManager
from main import resolve_user_id


class DummyEvent:
    """Mock for AstrMessageEvent"""

    def __init__(self, sender_id: str = "", platform: str = "", extra: dict = None, session_context: dict = None):
        self._sender_id = sender_id
        self._platform = platform
        self._extras = extra or {}
        if session_context is not None:
            self.session_context = session_context

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_platform_name(self) -> str:
        return self._platform

    def get_extra(self, key: str, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key: str, value):
        self._extras[key] = value


class TestIdentityManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_identity_"))
        self.mgr = IdentityManager(self.test_dir, code_expiry_seconds=2, code_cooldown_seconds=1)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_auto_initialize_identity(self):
        gid1 = self.mgr.get_global_id("aiocqhttp", "1507955112", display_name="Victical")
        self.assertEqual(gid1, "usr_aiocqhttp_1507955112")

        info = self.mgr.get_identity_info("aiocqhttp", "1507955112")
        self.assertIsNotNone(info)
        self.assertEqual(info["global_id"], gid1)
        self.assertEqual(info["primary_name"], "Victical")
        self.assertEqual(len(info["accounts"]), 1)
        self.assertEqual(info["accounts"][0]["platform"], "aiocqhttp")
        self.assertEqual(info["accounts"][0]["user_id"], "1507955112")

        # Second call returns same GID
        gid2 = self.mgr.get_global_id("aiocqhttp", "1507955112")
        self.assertEqual(gid1, gid2)

    def test_create_and_consume_link_code_happy_path(self):
        # 1. Create code on QQ
        ok, code, remaining = self.mgr.create_link_code("aiocqhttp", "1507955112")
        self.assertTrue(ok)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        self.assertGreater(remaining, 0)

        # 2. Consume code on Telegram
        ok, msg = self.mgr.consume_link_code(code, "telegram", "1697740260")
        self.assertTrue(ok)
        self.assertEqual(msg, "usr_aiocqhttp_1507955112")

        # Verify Telegram now maps to the same GID
        tg_gid = self.mgr.get_global_id("telegram", "1697740260")
        self.assertEqual(tg_gid, "usr_aiocqhttp_1507955112")

        # Verify identity accounts list
        info = self.mgr.get_identity_info("aiocqhttp", "1507955112")
        self.assertEqual(len(info["accounts"]), 2)
        platforms = {a["platform"] for a in info["accounts"]}
        self.assertSetEqual(platforms, {"aiocqhttp", "telegram"})

        # Verify code is consumed and cannot be used again
        ok, msg = self.mgr.consume_link_code(code, "bilibili", "4028591")
        self.assertFalse(ok)
        self.assertIn("不存在或已过期", msg)

    def test_code_cooldown_and_expiration(self):
        # Initial code
        ok, code1, _ = self.mgr.create_link_code("aiocqhttp", "1001")
        self.assertTrue(ok)

        # Immediate repeat should return existing code (cooldown)
        ok, code2, _ = self.mgr.create_link_code("aiocqhttp", "1001")
        self.assertTrue(ok)
        self.assertEqual(code1, code2)

        # Wait for expiry (configured to 2s in setUp)
        time.sleep(2.1)
        ok, msg = self.mgr.consume_link_code(code1, "telegram", "2001")
        self.assertFalse(ok)
        self.assertIn("不存在或已过期", msg)

    def test_self_binding_prevention(self):
        ok, code, _ = self.mgr.create_link_code("aiocqhttp", "1001")
        self.assertTrue(ok)

        # Attempt to consume from the same platform and user
        ok, msg = self.mgr.consume_link_code(code, "aiocqhttp", "1001")
        self.assertFalse(ok)
        self.assertIn("生成方，无需绑定自身", msg)

    def test_cascaded_identity_merging(self):
        # User A has QQ (1001) and WeChat (1002)
        gid_a = self.mgr.get_global_id("aiocqhttp", "1001")
        ok, code_a, _ = self.mgr.create_link_code("aiocqhttp", "1001")
        self.mgr.consume_link_code(code_a, "wechat", "1002")

        # User B has Bilibili (2001) and Discord (2002)
        gid_b = self.mgr.get_global_id("bilibili", "2001")
        ok, code_b, _ = self.mgr.create_link_code("bilibili", "2001")
        self.mgr.consume_link_code(code_b, "discord", "2002")

        # Now User B wants to merge with User A! User A generates code, User B consumes with Bilibili (2001)
        ok, code_merge, _ = self.mgr.create_link_code("aiocqhttp", "1001")
        ok, master_gid = self.mgr.consume_link_code(code_merge, "bilibili", "2001")
        self.assertTrue(ok)
        self.assertEqual(master_gid, gid_a)

        # Critical Check: Discord (2002) must also be migrated to gid_a!
        discord_gid = self.mgr.get_global_id("discord", "2002")
        self.assertEqual(discord_gid, gid_a)

        # Old gid_b must no longer exist in identities
        self.assertNotIn(gid_b, self.mgr.identities)

        # Master identity accounts must contain all 4 accounts
        info = self.mgr.get_identity_info("aiocqhttp", "1001")
        acc_keys = {(a["platform"], a["user_id"]) for a in info["accounts"]}
        expected = {
            ("aiocqhttp", "1001"),
            ("wechat", "1002"),
            ("bilibili", "2001"),
            ("discord", "2002"),
        }
        self.assertSetEqual(acc_keys, expected)

    def test_unlink_account(self):
        # Bind QQ, Telegram and Bilibili
        self.mgr.get_global_id("aiocqhttp", "1001")
        ok, code, _ = self.mgr.create_link_code("aiocqhttp", "1001")
        self.mgr.consume_link_code(code, "telegram", "2001")

        ok, code2, _ = self.mgr.create_link_code("aiocqhttp", "1001")
        self.mgr.consume_link_code(code2, "bilibili", "3001")

        # 1. Unlinking self must fail
        ok, msg = self.mgr.unlink_account(
            operator_platform="aiocqhttp",
            operator_user_id="1001",
            target_platform="aiocqhttp",
        )
        self.assertFalse(ok)
        self.assertIn("不可解绑当前操作的平台账号自身", msg)

        # 2. Unlinking non-existent platform must fail
        ok, msg = self.mgr.unlink_account(
            operator_platform="aiocqhttp",
            operator_user_id="1001",
            target_platform="slack",
        )
        self.assertFalse(ok)
        self.assertIn("未找到平台", msg)

        # 3. Successfully unlink Telegram
        ok, msg = self.mgr.unlink_account(
            operator_platform="aiocqhttp",
            operator_user_id="1001",
            target_platform="telegram",
        )
        self.assertTrue(ok)
        self.assertIn("成功解绑", msg)

        # Verify QQ no longer has Telegram
        info_qq = self.mgr.get_identity_info("aiocqhttp", "1001")
        platforms = {a["platform"] for a in info_qq["accounts"]}
        self.assertNotIn("telegram", platforms)
        self.assertIn("aiocqhttp", platforms)
        self.assertIn("bilibili", platforms)

        # Verify Telegram now has a separate new GID
        tg_gid = self.mgr.get_global_id("telegram", "2001")
        self.assertNotEqual(tg_gid, info_qq["global_id"])
        self.assertEqual(tg_gid, "usr_telegram_2001")

    def test_persistence_across_reloads(self):
        # Setup bindings
        gid = self.mgr.get_global_id("aiocqhttp", "1001")
        ok, code, _ = self.mgr.create_link_code("aiocqhttp", "1001")
        self.mgr.consume_link_code(code, "telegram", "2001")

        # Reload manager from same disk directory
        new_mgr = IdentityManager(self.test_dir)
        self.assertEqual(new_mgr.get_global_id("aiocqhttp", "1001"), gid)
        self.assertEqual(new_mgr.get_global_id("telegram", "2001"), gid)
        info = new_mgr.get_identity_info("telegram", "2001")
        self.assertEqual(len(info["accounts"]), 2)


class TestResolveUserId(unittest.TestCase):
    def test_resolve_from_session_context(self):
        event = DummyEvent(
            sender_id="raw_123",
            platform="telegram",
            session_context={"global_user_id": "usr_master_999"},
        )
        self.assertEqual(resolve_user_id(event), "usr_master_999")

    def test_resolve_from_extras(self):
        event = DummyEvent(
            sender_id="raw_123",
            platform="telegram",
            extra={"global_user_id": "usr_extra_888"},
        )
        self.assertEqual(resolve_user_id(event), "usr_extra_888")

    def test_resolve_fallback_to_sender_id(self):
        event = DummyEvent(
            sender_id="raw_123",
            platform="telegram",
        )
        self.assertEqual(resolve_user_id(event), "raw_123")

    def test_resolve_none_event(self):
        self.assertEqual(resolve_user_id(None), "")


if __name__ == "__main__":
    unittest.main()
