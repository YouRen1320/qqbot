"""
看图回复 —— @ + 图片 → 简短点评
- 用 omni profile（多模态模型）
- 图片以 URL 形式传入（NapCat 给到的 cq:image url=...）
- 失败返回 None，上层走文字 fallback（不要静默）
- 走 httpx 直连 OpenAI 兼容端点（litellm 多模态 messages 格式支持各家不一，直发更稳）
"""
import asyncio
import time
import httpx
from nonebot.log import logger

from .ai_client import _PROFILES, _TIMEOUT


# omni 401 熔断：连续 3 次以上 401 就 skip 30 分钟，避免无效 API 调用
# 与 video_parse.describe_grid 的熔断对称（同一个 OMNI_API_KEY，但两个调用入口
# 各自独立计数 —— 简单，且最坏情况下浪费 2x3=6 个 401，仍远小于"完全不熔断"）
_OMNI_AUTH_FAIL_COUNT = 0
_OMNI_AUTH_FAIL_THRESHOLD = 3
_OMNI_CIRCUIT_OPEN_UNTIL = 0.0
_OMNI_CIRCUIT_COOLDOWN = 1800.0


async def describe(image_url: str, prompt_hint: str, personality: str, group_id: int = 0) -> str | None:
    global _OMNI_AUTH_FAIL_COUNT, _OMNI_CIRCUIT_OPEN_UNTIL
    cfg = _PROFILES.get("omni") or _PROFILES["default"]
    if not cfg["api_key"]:
        logger.warning("omni key 未配置，跳过看图")
        return None
    if not image_url.startswith(("http://", "https://")):
        logger.info(f"非 http 图片 URL，跳过看图: {image_url[:60]}")
        return None
    now = time.time()
    if now < _OMNI_CIRCUIT_OPEN_UNTIL:
        remaining = int(_OMNI_CIRCUIT_OPEN_UNTIL - now)
        logger.info(f"omni 401 熔断中（剩余 {remaining}s），跳过看图")
        return None

    messages = [
        {"role": "system", "content": personality},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_hint or "这图什么玩意 点评一下"},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.9,
        "max_tokens": 600,   # 之前 200 太小，omni 也可能带 reasoning 吃满
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    endpoint = f"{cfg['api_base']}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            if 400 <= resp.status_code < 500:
                logger.warning(
                    f"omni({cfg['model']}) 4xx: status={resp.status_code} body={resp.text[:300]}"
                )
                # 401 单独累计；其他 4xx（如 429 限流）不进熔断逻辑
                if resp.status_code == 401:
                    _OMNI_AUTH_FAIL_COUNT += 1
                    if _OMNI_AUTH_FAIL_COUNT >= _OMNI_AUTH_FAIL_THRESHOLD:
                        _OMNI_CIRCUIT_OPEN_UNTIL = time.time() + _OMNI_CIRCUIT_COOLDOWN
                        logger.warning(
                            f"看图 omni 连续 {_OMNI_AUTH_FAIL_COUNT} 次 401，熔断 "
                            f"{int(_OMNI_CIRCUIT_COOLDOWN)}s（检查 .env 里的 OMNI_API_KEY）"
                        )
                return None
            resp.raise_for_status()
            data = resp.json()
            # 走到这里说明成功，重置熔断计数
            _OMNI_AUTH_FAIL_COUNT = 0
            # 落 usage_log（不阻塞）—— 看图也烧 token
            usage = data.get("usage") or {}
            if usage:
                from . import db
                asyncio.create_task(
                    db.log_usage(
                        group_id,
                        "omni",
                        cfg["model"],
                        int(usage.get("prompt_tokens", 0) or 0),
                        int(usage.get("completion_tokens", 0) or 0),
                        int(usage.get("total_tokens", 0) or 0),
                    )
                )
            content = data["choices"][0]["message"]["content"]
            if not content:
                logger.warning(
                    f"omni({cfg['model']}) content 为空 usage={usage} url_head={image_url[:60]}"
                )
                return None
            return content.strip()
    except Exception as e:
        logger.warning(f"看图失败 url_head={image_url[:60]}: {e}")
        return None
