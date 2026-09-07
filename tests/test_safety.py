import importlib
import os
import unittest
from unittest.mock import patch

from support import ensure_nonebot_initialized

ensure_nonebot_initialized()


class SafetyTests(unittest.TestCase):
    def setUp(self):
        env = {
            "ENABLED_GROUPS": "10001,10002",
            "DAILY_LIMIT_PER_GROUP": "2",
            "PER_MINUTE_LIMIT": "1",
        }
        self.env_patch = patch.dict(os.environ, env, clear=False)
        self.env_patch.start()
        from plugins.chat import safety

        self.safety = importlib.reload(safety)

    def tearDown(self):
        self.env_patch.stop()

    def test_group_whitelist_and_runtime_pause(self):
        self.assertTrue(self.safety.is_group_enabled(10001))
        self.assertFalse(self.safety.is_group_enabled(99999))

        self.safety.pause_group(10001)
        self.assertFalse(self.safety.is_group_enabled(10001))
        self.safety.resume_group(10001)
        self.assertTrue(self.safety.is_group_enabled(10001))

    def test_rate_limit_only_counts_successful_attempts(self):
        self.assertEqual(self.safety.can_reply(10001), (True, "ok"))
        self.assertEqual(
            self.safety.can_reply(10001),
            (False, "per_minute_limit(1)"),
        )
        self.assertEqual(self.safety.get_daily_counts()[10001], 1)


if __name__ == "__main__":
    unittest.main()
