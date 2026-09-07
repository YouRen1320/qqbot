import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from support import ensure_nonebot_initialized

ensure_nonebot_initialized()

from plugins.chat import maintain_commands, self_maintain


class SelfMaintainTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.backup_dir = self.root / "data" / "maintain_backups"
        self.path_patchers = [
            patch.object(self_maintain, "APP_ROOT", self.root),
            patch.object(self_maintain, "BACKUP_DIR", self.backup_dir),
            patch.object(self_maintain, "PENDING_FLAG", self.root / "data" / ".maintain_pending"),
            patch.object(self_maintain, "APPLY_LOG_FLAG", self.root / "data" / ".maintain_apply_log"),
        ]
        for patcher in self.path_patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.path_patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def test_key_is_required_for_remote_maintenance_call(self):
        with patch.object(self_maintain, "ANTHROPIC_API_KEY", ""):
            text, usage = asyncio.run(self_maintain._call_opus("只做离线验证"))

        self.assertIsNone(text)
        self.assertEqual(usage["error"], "ANTHROPIC_API_KEY 未配置")

    def test_path_allowlist_blocks_sensitive_and_outside_files(self):
        self.assertTrue(self_maintain._is_allowed_path("plugins/chat/router.py")[0])
        self.assertTrue(self_maintain._is_allowed_path("plugins/chat/data/rules.json")[0])
        self.assertFalse(self_maintain._is_allowed_path(".env")[0])
        self.assertFalse(self_maintain._is_allowed_path("Dockerfile")[0])
        self.assertFalse(self_maintain._is_allowed_path("plugins/other.py")[0])
        self.assertFalse(
            self_maintain._is_allowed_path("plugins/chat/../../outside.py")[0]
        )
        self.assertFalse(self_maintain._is_allowed_path("/plugins/chat/router.py")[0])

    def test_commands_are_disabled_without_api_key(self):
        event = type("PrivateEvent", (), {"user_id": 90001})()
        with patch.object(self_maintain, "ANTHROPIC_API_KEY", ""):
            self.assertFalse(maintain_commands._is_admin(event))
        self.assertFalse(maintain_commands._patrol_enabled)

    def test_apply_and_rollback_use_exact_paths(self):
        existing = self.root / "plugins" / "chat" / "file_with_underscore.py"
        existing.parent.mkdir(parents=True)
        existing.write_text("OLD = True\n", encoding="utf-8")
        new_file = self.root / "plugins" / "chat" / "new_module.py"
        plan = {
            "summary": "测试精确回滚",
            "risk": "low",
            "files": [
                {"path": "plugins/chat/file_with_underscore.py", "new_content": "OLD = False\n"},
                {"path": "plugins/chat/new_module.py", "new_content": "NEW = True\n"},
            ],
        }

        with patch.object(self_maintain.db, "maintain_log", new=AsyncMock()), patch.object(
            self_maintain.asyncio, "create_task", side_effect=lambda coro: coro.close()
        ):
            ok, _ = self_maintain.apply_fix(plan, hint="测试")

        self.assertTrue(ok)
        self.assertEqual(existing.read_text(encoding="utf-8"), "OLD = False\n")
        self.assertTrue(new_file.exists())
        manifest = json.loads(next(self.backup_dir.glob("apply-*.json")).read_text())
        self.assertEqual(manifest["changes"][0]["path"], "plugins/chat/file_with_underscore.py")

        with patch.object(self_maintain.db, "maintain_log", new=AsyncMock()), patch.object(
            self_maintain.asyncio, "create_task", side_effect=lambda coro: coro.close()
        ):
            rolled_back, _ = self_maintain.rollback_latest()

        self.assertTrue(rolled_back)
        self.assertEqual(existing.read_text(encoding="utf-8"), "OLD = True\n")
        self.assertFalse(new_file.exists())


if __name__ == "__main__":
    unittest.main()
