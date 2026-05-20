"""
GPT 图像生成 —— 通过中转站调 gpt-image 系列模型出图
- 触发: 消息以 "生图"/"画图"/"画一张" 起头, 后跟 prompt; 不需要 @ bot
- 尺寸修饰: "生图横 ..." → 1536x1024; "生图竖 ..." → 1024x1536; 默认 1024x1024
- 限速: 群级 1 张 / 60s, 命中限速回 in-character 吐槽
- 配置 env:
    IMAGE_GEN_BASE_URL  API 根 URL, 如 https://api.openai.com
    IMAGE_GEN_API_KEY   sk-...
    IMAGE_GEN_MODEL     默认 gpt-image-2
- 用量落 usage_log(profile="image_gen"), 方便 /cost 查日花费
- 失败时回滚冷却时间戳, 让用户可立刻重试
- 返回的 base64 PNG 通过 OneBot v11 base64:// 协议直发, 跟 render_memo 一致
"""
import os
import json
import random
import time
import asyncio
from typing import Optional

import httpx
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.log import logger

from . import db, recall, reactions
from .config import BOT_NAME, BOT_ALIASES

# 收到生图请求时贴的"已读"表情 ID(QQ 表情);124=OK,277=拍拍,180=惊喜,179=doge
# 用 OK/拍拍 两种百搭的,避免显得过于油腻
_ACK_EMOJI_POOL: tuple[str, ...] = ("124", "277")

# 触发后先发的"正在画"提示, 给 UX 反馈
# 风格: 模拟真人在 QQ 群里说话, 不嘴硬不技术腔
_WAIT_TIPS: tuple[str, ...] = (
    "正在画,等一下",
    "画起来了,稍等",
    "在画了,半分钟",
    "稍等哦,马上好",
    "好,等我画一下",
)

IMAGE_GEN_BASE_URL = os.getenv("IMAGE_GEN_BASE_URL", "").rstrip("/")
IMAGE_GEN_API_KEY = os.getenv("IMAGE_GEN_API_KEY", "")
IMAGE_GEN_MODEL = os.getenv("IMAGE_GEN_MODEL", "gpt-image-2")


# 触发动词 × 尺寸后缀 → 完整触发前缀表
# 加新动词只改 _VERBS 一行, 自动生成横/竖/默认三档。
# 实际命中按 key 长度倒序, 避免 "生图" 抢先于 "生图横"。
_VERBS: tuple[str, ...] = (
    "生图", "画图", "画一张", "画一下", "画下",
    "生成一张", "生成一幅", "生成图",
    "帮我画", "帮我生成",
    "给我画", "给我生成",
)

# 墨七七人设可视化(基于主人确认的角色参考图)
# 关键视觉: 长 lime-green 头发 / red 眼 / pink-magenta 翼形发饰 / 白衬衫粉丝带 / 学姐学习场景
# 注: 不强加"no text / no watermark"等限制,让 GPT-image-2 自己 enforce content policy
_SELF_PORTRAIT_PROMPT = (
    "anime style high-detail portrait of Mo Qiqi (墨七七), a 20-year-old Chinese college senior girl. "
    "Long flowing vibrant lime-green hair cascading past her shoulders, slightly windblown. "
    "Sharp crimson-red eyes with a cool aloof but cute gaze, looking back over the shoulder slightly. "
    "Distinctive pink-magenta demon-wing-shaped hair ornament on the right side of her head. "
    "Wearing a clean white school-style button-up shirt collar with a soft pastel-pink ribbon scarf wrapped around the collar. "
    "Background: a quiet desk with an open notebook and pen, soft warm natural daylight, gentle bokeh. "
    "Clean anime lineart, soft pastel shading, refined facial details."
)


# 检测"自指"关键词(用户在说画 bot 本人/自画像)
# 命中 → handler 强制注入 _SELF_PORTRAIT_PROMPT(功能性 — 图片模型不认识 bot 角色)
_SELF_REF_KEYWORDS_LOWER = (
    *[n.lower() for n in [BOT_NAME] + BOT_ALIASES],
    "自画像", "自拍", "selfie",
    "你自己", "你的照片", "你长啥样", "你的样子",
    "画你", "画一下你", "画下你",
)
# 自画像触发词(anywhere-in-message substring 匹配, 不要求开头)
# 命中即用预置 prompt, 不取用户文本(否则 GPT 缺人设上下文画不准)
_SELF_PORTRAIT_KEYWORDS: tuple[str, ...] = (
    "自画像",
    "画一下自己", "画下自己", "画一张自己", "画张自己",
    "画一下你自己", "画下你自己", "画一张你自己",
    "画个自己", "画个你自己",
    "画你自己",
)

# 用户消息含这些关键词时, 即便有"画/生图"触发, 也让给 agent 路径处理
# 因为意图是"换群头像"而不是发图,需要 set_group_avatar 工具走 image_gen + upload 两步
_DEFER_TO_AGENT_KEYWORDS: tuple[str, ...] = (
    "群头像", "本群头像", "本群的头像", "群的头像",
    "换头像", "替换头像", "改头像", "替换掉",
    "群图像", "群图标",
)
_SIZE_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("横", "1536x1024"),
    ("竖", "1024x1536"),
    ("",   "1024x1024"),
)
_TRIGGERS: dict[str, str] = {
    v + suf: size for v in _VERBS for suf, size in _SIZE_SUFFIXES
}
_TRIGGER_KEYS_SORTED = sorted(_TRIGGERS.keys(), key=len, reverse=True)

# SSE 流式 httpx 超时: read=60 是"两次 chunk 间隔上限", 不是总耗时。
# 中转站每 10s 发 keepalive 注释行, 6× buffer 兜底。
# 上游 gpt-image-2 复杂 prompt P99 ~410s, 用同步 timeout 会挂死, 必须流式。
_HTTPX_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=120.0, pool=10.0)

# 重试降配链: (quality, size_override) — size_override=None 用调用方原 size。
# 中转站建议: 原始 → quality=medium → medium+1024x1024, 同模型, 不跨号池。
_RETRY_CHAIN: tuple[tuple[str, Optional[str]], ...] = (
    ("high",   None),
    ("medium", None),
    ("medium", "1024x1024"),
)
# 指数退避: 第 N 次失败后睡 _RETRY_BACKOFF[N] 秒再起下一次
_RETRY_BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0)

# 群级"正在跑"锁: gid 在集合里 → 拒绝并发生图。
# 替代原先 60s 冷却 — 流式后单张可能 >300s, 时间窗口冷却没意义, 改用占用锁。
_inflight_groups: set[int] = set()


def is_busy(gid: int) -> bool:
    """供外部 (agent / try_handle) 在调用前快速判定该群是否已有生图任务在跑。"""
    return gid in _inflight_groups


def _parse_trigger(text: str) -> Optional[tuple[str, str]]:
    """匹配触发词, 返回 (prompt, size); 无触发 / prompt 空 → None。
    优先级:
    1) 自画像类(anywhere substring): 用预置人设 prompt, 不取用户文本
    2) 标准动词前缀(生图/画图/生成一张...): 用户文本作 prompt
    """
    t = text.lstrip()
    # 1) 自画像
    for kw in _SELF_PORTRAIT_KEYWORDS:
        if kw in t:
            return _SELF_PORTRAIT_PROMPT, "1024x1024"
    # 2) 前缀触发
    for kw in _TRIGGER_KEYS_SORTED:
        if t.startswith(kw):
            prompt = t[len(kw):].strip()
            if prompt:
                return prompt, _TRIGGERS[kw]
            return None
    return None


def _extract_b64(obj: dict) -> Optional[str]:
    """从中转站 SSE 事件 payload 里抠 b64_json。
    兼容三种形态: data[0].b64_json (OpenAI 标准) / 顶层 b64_json / image.b64_json (实验字段)。
    """
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            b = first.get("b64_json")
            if isinstance(b, str) and b:
                return b
    b = obj.get("b64_json")
    if isinstance(b, str) and b:
        return b
    img = obj.get("image")
    if isinstance(img, dict):
        b = img.get("b64_json")
        if isinstance(b, str) and b:
            return b
    return None


async def _consume_sse(resp: httpx.Response) -> tuple[Optional[str], int, dict]:
    """解析中转站 SSE: 返回 (最终 b64_json, partial 事件计数, usage dict)。
    协议:
      - `: ...` 行 = keepalive 注释, 跳过 (但能重置 httpx read timeout, 这就是关键)
      - `event: image_generation.partial_image` + `data: {...}` → 中间帧, 仅计数不展示 (避免 QQ 刷屏)
      - `event: image_generation.completed` + `data: {...}` → 最终图, 取 b64 + usage
      - 兜底: 若服务端只发 `data: {...}` 不带 event, 仍尝试抠 b64
    """
    b64_final: Optional[str] = None
    partial_count = 0
    usage_final: dict = {}
    cur_event: Optional[str] = None
    async for raw in resp.aiter_lines():
        line = raw.rstrip("\r")
        if not line:
            # 空行 = 一个 SSE 事件边界结束, 重置 event 上下文
            cur_event = None
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            cur_event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        ev = cur_event or ""
        if ev.endswith("partial_image"):
            partial_count += 1
            continue
        if ev.endswith("completed"):
            b = _extract_b64(obj)
            if b:
                b64_final = b
            u = obj.get("usage")
            if isinstance(u, dict):
                usage_final = u
            continue
        # 无 event 类型的兜底分支 (服务端协议变化时仍能拿到图)
        b = _extract_b64(obj)
        if b:
            b64_final = b
            u = obj.get("usage")
            if isinstance(u, dict):
                usage_final = u
    return b64_final, partial_count, usage_final


async def _call_api_stream(
    prompt: str, size: str, quality: str
) -> tuple[Optional[str], int, dict]:
    """SSE 流式调 /v1/images/generations。失败抛 httpx 异常给上层 generate_b64 处理。"""
    body = {
        "model": IMAGE_GEN_MODEL,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "stream": True,
        "quality": quality,
    }
    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT) as client:
        async with client.stream(
            "POST",
            f"{IMAGE_GEN_BASE_URL}/v1/images/generations",
            headers={
                "Authorization": f"Bearer {IMAGE_GEN_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json=body,
        ) as resp:
            if resp.status_code >= 400:
                # 4xx/5xx 时把 body 读出来, 让上层 HTTPStatusError 处理时能拿到 detail
                await resp.aread()
                resp.raise_for_status()
            return await _consume_sse(resp)


# 单张 reference 大小上限; QQ 图链一般 < 5MB, 给 8MB 余量
_REF_IMAGE_MAX_BYTES = 8 * 1024 * 1024
# reference 张数上限; image2 支持多图但太多会扰乱主提示
_REF_IMAGE_MAX_COUNT = 4


async def _download_image(url: str) -> Optional[tuple[bytes, str]]:
    """下载 QQ 图链, 返 (bytes, content_type); 失败 None。"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            data = resp.content
            if len(data) > _REF_IMAGE_MAX_BYTES:
                logger.warning(f"image_gen ref 图过大 {len(data)} bytes, 跳过")
                return None
            ctype = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            return data, ctype
    except Exception as e:
        logger.warning(f"image_gen 下载 ref 图失败 url={url[:80]!r}: {type(e).__name__}: {e}")
        return None


async def _call_api_edits_stream(
    prompt: str, size: str, quality: str, ref_urls: list[str]
) -> tuple[Optional[str], int, dict]:
    """SSE 流式调 /v1/images/edits multipart。
    image2 spec: 多张 reference 用同字段名 'image' 多次 append。
    multipart 场景 stream 字段写字符串 "true" (form-encoded 没有 bool)。
    """
    refs = ref_urls[:_REF_IMAGE_MAX_COUNT]
    downloaded: list[tuple[bytes, str]] = []
    for url in refs:
        item = await _download_image(url)
        if item:
            downloaded.append(item)
    if not downloaded:
        logger.warning("image_gen 所有 ref 图下载失败, 退化到 generations 路径")
        return await _call_api_stream(prompt, size, quality)

    files = []
    for i, (data, ctype) in enumerate(downloaded):
        ext = "png" if "png" in ctype else "jpg"
        files.append(("image", (f"ref_{i}.{ext}", data, ctype)))
    form = {
        "model": IMAGE_GEN_MODEL,
        "prompt": prompt,
        "size": size,
        "n": "1",
        "stream": "true",
        "quality": quality,
    }
    async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT) as client:
        async with client.stream(
            "POST",
            f"{IMAGE_GEN_BASE_URL}/v1/images/edits",
            headers={
                "Authorization": f"Bearer {IMAGE_GEN_API_KEY}",
                "Accept": "text/event-stream",
            },
            data=form,
            files=files,
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                resp.raise_for_status()
            return await _consume_sse(resp)


async def generate_b64(
    prompt: str,
    size: str = "1024x1024",
    group_id: int = 0,
    reference_urls: Optional[list[str]] = None,
) -> tuple[Optional[str], str]:
    """
    流式生图返 (b64_or_None, error_kind);成功 → (b64, "ok");失败 → (None, kind)。
    reference_urls: 若有, 走 /v1/images/edits 多模态。
    error_kind: nsfw / timeout / server / client / not_configured / no_b64 / busy / unknown

    重试策略 (仅 timeout / 网络错触发, 4xx/nsfw/no_b64 立刻返):
        attempt 0: quality=high, 原 size
        attempt 1: quality=medium, 原 size       (退避 1s)
        attempt 2: quality=medium, 1024x1024     (退避 2s)
    """
    if not (IMAGE_GEN_BASE_URL and IMAGE_GEN_API_KEY):
        logger.warning("image_gen.generate_b64 调用但 env 未配置")
        return None, "not_configured"

    # per-group 互斥: 同群有任务在跑时直接拒绝, 避免并发刷上游
    if group_id and group_id in _inflight_groups:
        logger.info(f"image_gen 群 {group_id} 已有在跑任务, busy 拒绝")
        return None, "busy"
    if group_id:
        _inflight_groups.add(group_id)

    use_edits = bool(reference_urls)
    last_kind = "unknown"
    try:
        for attempt, (quality, size_override) in enumerate(_RETRY_CHAIN):
            cur_size = size_override or size
            t0 = time.time()
            try:
                if use_edits:
                    logger.info(
                        f"image_gen edits stream attempt={attempt} q={quality} "
                        f"size={cur_size} refs={len(reference_urls)}"
                    )
                    b64, partial_n, usage = await _call_api_edits_stream(
                        prompt, cur_size, quality, reference_urls
                    )
                else:
                    logger.info(
                        f"image_gen gen stream attempt={attempt} q={quality} size={cur_size}"
                    )
                    b64, partial_n, usage = await _call_api_stream(
                        prompt, cur_size, quality
                    )
            except httpx.HTTPStatusError as e:
                body = ""
                try:
                    body = e.response.text[:500]
                except Exception:
                    pass
                status = e.response.status_code
                body_l = body.lower()
                nsfw_hints = (
                    "content_policy", "safety", "moderation", "rejected by",
                    "性敏感", "不允许", "sexual", "explicit", "inappropriate",
                )
                if 400 <= status < 500 and any(h in body_l for h in nsfw_hints):
                    logger.warning(f"image_gen 内容政策拒绝 status={status} body={body[:200]!r}")
                    return None, "nsfw"
                if 400 <= status < 500:
                    logger.warning(f"image_gen 客户端错误 status={status} body={body[:200]!r}")
                    return None, "client"
                # 5xx 也不重试 — 中转站建议同模型重试链只针对超时, 5xx 多半号池故障
                logger.warning(f"image_gen 服务端错误 status={status} body={body[:200]!r}")
                return None, "server"
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                cost = time.time() - t0
                last_kind = "timeout"
                logger.warning(
                    f"image_gen 超时/网络 attempt={attempt} type={type(e).__name__} cost={cost:.1f}s"
                )
                if attempt < len(_RETRY_CHAIN) - 1:
                    await asyncio.sleep(_RETRY_BACKOFF[attempt])
                    continue
                return None, "timeout"
            except Exception as e:
                logger.warning(f"image_gen 未预期 type={type(e).__name__}: {e!r}")
                return None, "unknown"

            cost = time.time() - t0
            if not b64:
                # 流式正常关闭但没拿到 completed b64 — 可能上游协议变更或返回空
                logger.warning(
                    f"image_gen 流式结束无 b64 attempt={attempt} partial={partial_n} cost={cost:.1f}s"
                )
                return None, "no_b64"
            logger.info(
                f"image_gen 成功 attempt={attempt} q={quality} size={cur_size} "
                f"cost={cost:.1f}s partial={partial_n} kb={len(b64)//1024}"
            )
            asyncio.create_task(db.log_usage(
                group_id=group_id,
                profile="image_gen",
                model=IMAGE_GEN_MODEL,
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
            ))
            return b64, "ok"
        return None, last_kind
    finally:
        if group_id:
            _inflight_groups.discard(group_id)


async def try_handle(bot: Bot, event: GroupMessageEvent, text: str) -> bool:
    """
    主路由入口; 匹配则吃掉本次消息并返回 True, 否则 False(交由后续 chat 流程处理)。
    限速/失败也算 "吃掉", 避免被当成普通文字再走一遍 AI。
    """
    parsed = _parse_trigger(text)
    if not parsed:
        return False
    # 含"换群头像"类意图 → 让给 agent 路径(用 set_group_avatar 工具,生图+上传两步)
    if any(kw in text for kw in _DEFER_TO_AGENT_KEYWORDS):
        logger.info(
            f"群 {event.group_id} 生图触发但含换头像意图, defer 给 agent: {text[:40]!r}"
        )
        return False
    # at_me=True 且非自画像 → 让 agent 接手, 这样能支持"画一张+改名片"这种多 tool 组合;
    # 自画像保留快速路径(prompt 预置, agent 转交无意义)
    is_self_portrait_path = any(kw in text for kw in _SELF_PORTRAIT_KEYWORDS)
    if event.is_tome() and not is_self_portrait_path:
        logger.info(
            f"群 {event.group_id} 生图 @ bot 触发, defer 给 agent: {text[:40]!r}"
        )
        return False
    if not (IMAGE_GEN_BASE_URL and IMAGE_GEN_API_KEY):
        logger.warning("image_gen 命中触发词但 env 未配置(IMAGE_GEN_BASE_URL / IMAGE_GEN_API_KEY 空), 跳过")
        return False

    prompt, size = parsed
    gid = event.group_id

    # 互斥: 同群已有任务在跑直接拒, 不再用 60s 时间窗口冷却 (流式后单张可能 >300s)。
    # 主人也走同样的锁 — 并发跑两张图实际上会拖垮号池, bypass 没价值。
    if is_busy(gid):
        tip = "上一张还在画,等画完再来"
        sent = await bot.send(event, MessageSegment.reply(event.message_id) + tip)
        mid = sent.get("message_id") if isinstance(sent, dict) else None
        if mid:
            recall.remember_sent(gid, mid)
        logger.info(f"群 {gid} 生图被 inflight 锁拒绝")
        return True

    logger.info(f"群 {gid} 生图触发 size={size} prompt={prompt[:80]!r}")

    # UX: 立刻贴 emoji + 发"在画了"提示, 避免长时间静默 (流式后复杂图可能 ~4 分钟)
    # try_react 内部已 swallow 失败; tip 发送失败不阻断主流程
    asyncio.create_task(reactions.try_react(bot, event.message_id, random.choice(_ACK_EMOJI_POOL)))
    try:
        tip = random.choice(_WAIT_TIPS)
        tip_sent = await bot.send(event, MessageSegment.reply(event.message_id) + tip)
        tip_mid = tip_sent.get("message_id") if isinstance(tip_sent, dict) else None
        if tip_mid:
            recall.remember_sent(gid, tip_mid)
    except Exception as e:
        logger.warning(f"群 {gid} 生图 wait_tip 发送失败 type={type(e).__name__} (继续生图)")

    # 走统一的 generate_b64 (含 SSE 流式 + quality 降配重试 + per-group 锁 + 用量记账)
    b64, kind = await generate_b64(prompt, size=size, group_id=gid)
    if b64:
        img_seg = MessageSegment.image(f"base64://{b64}")
        try:
            sent = await bot.send(event, MessageSegment.reply(event.message_id) + img_seg)
            mid = sent.get("message_id") if isinstance(sent, dict) else None
            if mid:
                recall.remember_sent(gid, mid)
            logger.info(f"群 {gid} 生图发送成功 b64_kb={len(b64) // 1024} mid={mid}")
        except Exception as e:
            logger.warning(f"群 {gid} 生图发送失败 type={type(e).__name__} msg={e!r}")
        return True

    # 失败兜底文案 — 用"真人嘴气"风, 不暴露中转站/key 等技术词
    tip_map = {
        "busy":           "上一张还在画,等画完再来",
        "timeout":        "刚画了一半卡住了,等下重发",
        "nsfw":           "这个画不出来,换个说法试试",
        "client":         "这个画不出来,换个说法试试",
        "server":         "卡了一下,等等再来",
        "no_b64":         "卡了一下,等等再来",
        "not_configured": "今天画不了了,改天吧",
    }
    tip = tip_map.get(kind, "画不出来,换个说法试试")
    logger.warning(f"群 {gid} 生图失败 kind={kind}")
    try:
        await bot.send(event, MessageSegment.reply(event.message_id) + tip)
    except Exception as e:
        logger.warning(f"群 {gid} 生图错误回复发送失败 type={type(e).__name__}")
    return True
