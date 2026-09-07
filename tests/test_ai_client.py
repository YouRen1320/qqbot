import unittest
from unittest.mock import AsyncMock, patch

from support import ensure_nonebot_initialized

ensure_nonebot_initialized()

from plugins.chat import ai_client


class AiClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ai_client._fallback_failures.clear()

    async def test_primary_failure_falls_back(self):
        with patch.object(
            ai_client,
            "_call_one",
            new=AsyncMock(side_effect=[None, "备用模型回复"]),
        ) as call:
            result = await ai_client.chat_completion(
                [{"role": "user", "content": "你好"}],
                profile="default",
                group_id=10001,
            )

        self.assertEqual(result, "备用模型回复")
        self.assertEqual(call.await_count, 2)
        self.assertEqual(call.await_args_list[0].args[0], "default")
        self.assertEqual(call.await_args_list[1].args[0], "default_fallback")

    async def test_open_circuit_skips_primary(self):
        ai_client._fallback_failures["default"].extend(
            [ai_client.time.time()] * ai_client._FALLBACK_BREAKER_THRESHOLD
        )
        with patch.object(
            ai_client,
            "_call_one",
            new=AsyncMock(return_value="熔断后的备用回复"),
        ) as call:
            result = await ai_client.chat_completion([], profile="default")

        self.assertEqual(result, "熔断后的备用回复")
        call.assert_awaited_once()
        self.assertEqual(call.await_args.args[0], "default_fallback")


if __name__ == "__main__":
    unittest.main()
