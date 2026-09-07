import tempfile
import time
import unittest
from pathlib import Path

from support import ensure_nonebot_initialized

ensure_nonebot_initialized()

from plugins.chat import db


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.temp_dir.name) / "qqbot.db")
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.temp_dir.cleanup()

    async def test_history_persists_and_can_be_recalled_by_fts(self):
        await db.append(10001, 20001, "user", "周末一起学习 Python", time.time() - 120)
        await db.close()
        await db.init()
        rows = await db.search_relevant(
            10001,
            "Python",
            limit=3,
            older_than_seconds=0,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "user")
        self.assertIn("Python", rows[0]["content"])

    async def test_admin_and_usage_logs_are_queryable(self):
        await db.admin_log(10001, 90001, "set_group_admin", {"target": 20001}, True)
        await db.log_usage(10001, "default", "model-a", 10, 5, 15)

        admin_rows = await db.admin_log_recent()
        usage_rows = await db.usage_today()
        self.assertEqual(admin_rows[0]["actor_qq"], 90001)
        self.assertEqual(usage_rows[0]["total"], 15)


if __name__ == "__main__":
    unittest.main()
