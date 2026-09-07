import unittest
from unittest.mock import AsyncMock, patch

from support import ensure_nonebot_initialized

ensure_nonebot_initialized()

from plugins.chat import agent_tools


class FakeBot:
    def __init__(self):
        self.set_group_admin = AsyncMock()


class AgentToolPermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_admin_qq = agent_tools.ADMIN_QQ
        agent_tools.ADMIN_QQ = 90001
        agent_tools._admin_promo_history.clear()

    def tearDown(self):
        agent_tools.ADMIN_QQ = self.original_admin_qq

    async def test_non_admin_cannot_change_group_admin(self):
        bot = FakeBot()
        handlers = agent_tools.make_handlers(bot, 10001, 30001, 20001)

        result = await handlers["set_group_admin"](
            {"target_qq": 20002, "enable": True}
        )

        self.assertIn("只主人能用", result)
        bot.set_group_admin.assert_not_awaited()

    async def test_admin_request_can_change_group_admin(self):
        bot = FakeBot()
        handlers = agent_tools.make_handlers(bot, 10001, 30001, 90001)
        with patch.object(agent_tools.db, "admin_log", new=AsyncMock()):
            result = await handlers["set_group_admin"](
                {"target_qq": 20002, "enable": True}
            )
            await agent_tools.db.admin_log()

        self.assertIn("成功", result)
        bot.set_group_admin.assert_awaited_once_with(
            group_id=10001,
            user_id=20002,
            enable=True,
        )


if __name__ == "__main__":
    unittest.main()
