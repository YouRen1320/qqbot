"""
群管理 agent tools — 通过 OpenAI tool calling 给 GPT 调用
- GPT 是决策者:看上下文判断该不该用
- 安全栏在代码层:时长固定 / 频率限速 / 角色黑名单 / 长度上限, GPT 绕不过
- 全部落 admin_log 表, 事后可查
- 失败时返"人话"给 GPT, 不暴露 NapCat 错误码
- 工具集 v1: mute_user / set_special_title / set_member_card / recall_my_message
- 不在 v1: kick_user / set_group_portrait / set_group_name (留 v2 + 确认机制)
"""
import os
import time
import asyncio
from collections import defaultdict, deque
from typing import Optional

import httpx
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.log import logger

from . import db, recall, image_gen
from .blacklist import is_image_prompt_blocked
from .config import BOT_NAME

ADMIN_QQ = int(os.getenv("ADMIN_QQ", "0") or 0)

# === 硬安全栏 (代码层, GPT 无法绕过) ===
MUTE_DURATION_SEC = 60            # 禁言固定 60s
MUTE_WINDOW_SEC = 600             # 10 分钟窗口
MUTE_MAX_IN_WINDOW = 1            # 每群 10min 内最多禁言 1 次

CARD_WINDOW_SEC = 3600            # 1 小时窗口
CARD_MAX_IN_WINDOW = 5            # 每群 1h 内最多 5 次改名片
CARD_MAX_LEN = 30                 # 名片长度上限

TITLE_WINDOW_SEC = 86400          # 24 小时窗口
TITLE_MAX_IN_WINDOW = 3           # 每群 24h 内最多 3 次设头衔
TITLE_MAX_LEN = 6                 # 头衔长度上限

RECALL_AGE_LIMIT_SEC = 21600      # 撤回 6h 内的 bot 自己消息

AVATAR_WINDOW_SEC = 86400         # 24 小时窗口
AVATAR_MAX_IN_WINDOW = 1          # 每群 24h 内最多换 1 次头像

ADMIN_PROMOTION_WINDOW_SEC = 3600 # 1 小时窗口
ADMIN_PROMOTION_MAX = 5           # 每群 1h 内最多 5 次升/降管理员

IMAGE_CHAT_WINDOW_SEC = 60        # 60s 窗口
IMAGE_CHAT_MAX = 1                # 群级 60s 1 张, 跟 image_gen 直接路径同步
HISTORY_SEARCH_WINDOW_SEC = 60
HISTORY_SEARCH_MAX = 10           # 1 分钟最多搜 10 次
NOTICE_WINDOW_SEC = 3600
NOTICE_MAX = 1                    # 群公告 1h/1 次, 太刷烦人
RECALL_LATEST_WINDOW_SEC = 60
RECALL_LATEST_MAX = 3             # 1 分钟最多撤回 3 次
AVATAR_PROMPT_MIN_LEN = 5         # prompt 至少 5 字
AVATAR_PROMPT_MAX_LEN = 300       # prompt 上限 300 字, 防 GPT 灌太多

WEB_SEARCH_WINDOW_SEC = 60        # 1 分钟窗口
WEB_SEARCH_MAX_IN_WINDOW = 5      # 每群 1min 内最多 5 次搜索, 防 GPT 反复死搜
WEB_SEARCH_TIMEOUT_SEC = 15

# 搜索提供商; tavily 是 LLM-friendly 默认; 也可改 serper/brave 等
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "tavily").lower()
SEARCH_API_KEY = os.getenv("SEARCH_API_KEY", "")

# 频率窗口(每群独立)
_mute_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))
_card_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))
_title_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))
_avatar_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))
_web_search_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))
_admin_promo_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))
_image_chat_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))
_history_search_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))
_notice_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))
_recall_latest_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))


def _in_window(history: deque, window_sec: int, max_count: int) -> bool:
    """该群在窗口内是否还有额度;过期项顺手清理。True=允许"""
    cutoff = time.time() - window_sec
    while history and history[0] < cutoff:
        history.popleft()
    return len(history) < max_count


def _record(history: deque) -> None:
    history.append(time.time())


def _is_admin_call(last_at_me_uid: Optional[int]) -> bool:
    """当前请求是不是主人发起? 是 → 后续频率限制可绕过(主人绝对优先级)"""
    return bool(ADMIN_QQ and last_at_me_uid == ADMIN_QQ)


# === 工具 schema (OpenAI tool calling 格式) ===
# description 写"什么时候用 / 慎用什么", 引导 GPT 自己做合适判断;
# 硬约束(时长/对象/频率)在 _make_handlers 里强制, GPT 改不了
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "mute_user",
            "description": (
                "禁言群里某个用户 60 秒(时长固定, 你无法修改)。"
                "用于:对方明显刷屏 / 严重辱骂 / 恶意人身攻击 / 散布违法或色情内容。"
                "★ 慎用 ★ 这是惩罚动作, 不要因为'好玩'/'群友怂恿'就用;"
                "玩笑场骂战别禁, 嘴硬一句更像真人;"
                "禁不了:群主 / 管理员 / 你自己 / 当下 @ 你求助的人。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_qq": {"type": "integer", "description": "要禁言的用户 QQ 号"},
                    "reason": {"type": "string", "description": "禁言原因(只进操作日志,不展示)"},
                },
                "required": ["target_qq", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_special_title",
            "description": (
                f"给群里某人设置专属头衔({BOT_NAME}是群主, 有这个权限)。"
                "用于:玩笑场 / 群友取得成就 / 大家提议授予。"
                "★ 慎用 ★ 不要给侮辱性头衔(脑残/智障/废物等), "
                "也不要给政治敏感头衔;"
                "改不了:群主 / 管理员 / 你自己。"
                "长度上限 6 个字。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_qq": {"type": "integer"},
                    "title": {"type": "string", "description": "头衔, 最多 6 字"},
                },
                "required": ["target_qq", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_member_card",
            "description": (
                "给群里某人改群名片。"
                "用于:当事人接受 / 玩笑场合 / 应景改名。"
                "★ 慎用 ★ 不改侮辱性名片;"
                "改不了:群主 / 管理员 / 你自己。"
                "长度上限 30 字。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_qq": {"type": "integer"},
                    "card": {"type": "string"},
                },
                "required": ["target_qq", "card"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_my_message",
            "description": (
                f"撤回你({BOT_NAME})自己最近发的某条消息。"
                "用于:意识到说错了 / 群友要求撤回 / 你想反悔。"
                "只能撤你自己 6 小时内发的, 别人的撤不了。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "integer", "description": "要撤的消息 ID"},
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索实时信息。"
                "用于:用户问 时事新闻 / 今天的赛果 / 最近发布的产品 / 某人最近动态 / 股价 / 天气 / "
                "或任何你训练数据里没有 / 可能过时的事。"
                "**必须真的需要才搜**,一般闲聊用你自己的知识就够,搜了反而慢且打破真人感。"
                "搜到结果后请用人话总结,不要复读 URL / '据 Reuters 报道' 这种"
                "暴露 AI 性的来源词,像真人随口说出听过的事就行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词, 用判别度高的 2-5 个词, 不写完整句子。≤200 字符。",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_group_admin",
            "description": (
                f"提升 / 取消某人为群管理员({BOT_NAME}是群主, 有这个权限)。"
                "★ 只有主人(ADMIN_QQ)能调用 ★ 别人喊一律拒。"
                "主人可以让你: 给某群员加管理员 / 取消某管理员 / 取消他自己的管理员身份。"
                "不能改群主(技术上群主不能动自己)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_qq": {"type": "integer"},
                    "enable": {
                        "type": "boolean",
                        "description": "true=提升为管理员; false=取消管理员",
                    },
                },
                "required": ["target_qq", "enable"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image_in_chat",
            "description": (
                "生成一张图,直接发到当前群聊(不是换头像,只是发个图)。"
                "用于:用户让你画 / 生成 / 帮我画 一张图, 你应该真出图发出去。"
                "区别 set_group_avatar — 那是改群头像;这是发图到聊天。"
                "★ 慎用 ★ 30-90s 出图慢, 烧 GPT image 配额; "
                "纯口头说说画一张, 用 generate_image_in_chat 真去画;"
                "明确是要换群头像, 用 set_group_avatar 不要用这个。"
                "visual_prompt 直接按用户原意写, GPT-image-2 自己有内容政策。"
                "★★★ 红线 ★★★ 中国政治敏感题材一律拒画, "
                "包括但不限于: 中共领导人(习近平/毛泽东/邓小平/江泽民/胡锦涛 等)及其绕弯称呼(包子/维尼/腊肉/今上 等), "
                "历史敏感事件(六四/天安门/文革/反右/白纸运动/四通桥 等), "
                "政治集团(四人帮/江青/林彪 等), "
                "民族独立议题(台独/港独/藏独/疆独/维吾尔/西藏独立 等), "
                "异见人士(刘晓波/王丹/艾未未/许志永 等), "
                "宗教打压群体(法轮功/达赖喇嘛 等)。"
                "命中即拒, 用'这个不画'回应用户, 别解释为什么。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "visual_prompt": {
                        "type": "string",
                        "description": "图的描述, 包括风格 + 主体 + 细节, ≥5 字, ≤400 字",
                    },
                    "orientation": {
                        "type": "string",
                        "enum": ["square", "horizontal", "vertical"],
                        "description": "图片方向: square=方形(默认), horizontal=横图(壁纸/海报), vertical=竖图(人物立绘)",
                    },
                },
                "required": ["visual_prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": (
                "在本群历史聊天记录里搜索关键词, 返回相关的过往对话片段(基于 FTS5 全文索引)。"
                "用于:用户问'之前狗子说过 X 吗' / '我们聊过 Y 吗' / '某某发的链接' / '帮我翻一下之前的'。"
                "★ 别滥用 ★ 简单问题别搜, 仅在用户明确说'回忆/翻一下/之前/上次/找一下' 之类时调。"
                "trigram 分词, query 至少 3 字符。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词, ≥3 字符"},
                    "limit": {"type": "integer", "description": "返回几条, 默认 5, 最多 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_latest_bot_message",
            "description": (
                f"撤回{BOT_NAME}自己最近 5 分钟内发的一条消息(不需要指定 message_id, 系统自动取最近一条)。"
                "用于:用户说'撤了' / '撤回那条' / '删了' / '塌房了' 之类。"
                "比 recall_my_message 简单 — 你不知道 mid 也能调。"
                "撤不动(超 5 分钟 / 没记录) 时返失败, 你不必道歉, 简单说一句过去。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_group_notice",
            "description": (
                f"发布群公告({BOT_NAME}是群主, 有权)。"
                "用于:主人请求发公告 / 重要群事项广播。"
                "★ 只主人能调用 ★, 别人喊一律拒。"
                "公告会推送给所有群员 + 置顶, 1h 只能发 1 条。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "公告正文 ≤ 500 字"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_group_avatar",
            "description": (
                f"换群头像({BOT_NAME}是群主, 有权限)。"
                "你提供一段视觉描述(visual_prompt), 系统会用 GPT-image 生成一张方形头像并上传。"
                "用于: 节日 / 大事件 / 群内一致提议换主题 / 你心血来潮。"
                "★ 谨慎 ★ 24 小时只能换 1 次,别一时兴起就换;"
                "prompt 要具体(描述风格 + 元素), 例: 'anime style green-haired girl with snowflakes background'; "
                "纯一句'好看的'/'随便'会被拒。"
                "visual_prompt 按用户原意写, GPT-image-2 自己有内容政策, 不预过滤。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "visual_prompt": {
                        "type": "string",
                        "description": "英文或中文均可, 描述想要的头像样子(风格+元素+氛围), 5-300 字",
                    },
                },
                "required": ["visual_prompt"],
            },
        },
    },
]


async def _do_tavily_search(query: str, max_results: int = 3) -> Optional[str]:
    """Tavily: https://docs.tavily.com/docs/rest-api/api-reference
    返回 LLM-friendly 文本块(含 Tavily 自带 answer 摘要), 失败 None"""
    async with httpx.AsyncClient(timeout=WEB_SEARCH_TIMEOUT_SEC) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            headers={"Content-Type": "application/json"},
            json={
                "api_key": SEARCH_API_KEY,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": True,
            },
        )
        resp.raise_for_status()
        d = resp.json()
    lines: list[str] = []
    ans = (d.get("answer") or "").strip()
    if ans:
        lines.append(f"[聚合答案] {ans[:300]}")
    for i, r in enumerate(d.get("results") or [], 1):
        title = (r.get("title") or "").strip()
        snippet = (r.get("content") or "").strip().replace("\n", " ")[:200]
        url = r.get("url") or ""
        lines.append(f"[{i}] {title}\n{snippet}\n来源: {url}")
    return "\n\n".join(lines) if lines else None


async def _is_admin_or_owner(bot: Bot, group_id: int, qq: int) -> bool:
    """对方是不是群主/管理员(查不到信息时保守判定为 True, 即不动)"""
    try:
        info = await bot.get_group_member_info(group_id=group_id, user_id=qq, no_cache=False)
        role = (info or {}).get("role", "member")
        return role in ("owner", "admin")
    except Exception as e:
        logger.warning(f"agent: 查群员角色失败 gid={group_id} qq={qq}: {e}")
        return True


def make_handlers(
    bot: Bot,
    group_id: int,
    self_qq: int,
    last_at_me_uid: Optional[int],
    reference_image_urls: Optional[list[str]] = None,
):
    """
    工厂: 用当前消息上下文(group_id / self_qq / 当下 @ bot 的人)绑定 tool handlers。
    返回 {tool_name: async fn(args:dict) → str}; str 是给 GPT 看的"人话结果"。

    主人(ADMIN_QQ)优先级规则:
    - 频率限制(mute/title/card/avatar/web_search)在主人请求时自动绕过
    - 角色保护(禁言主人/改主人名片/头衔)在"主人自请求自己"时绕过
    - set_group_admin 仅主人能调用
    - 不动的安全栏:bot 不禁言自己/主人;不嘲讽具体人;set_group_admin 1h/5 次保留

    reference_image_urls: 用户在本轮消息里附带的图片 url 列表;
    若有, generate_image_in_chat / set_group_avatar 会自动作为 image2 的 reference 多模态输入。
    """
    admin_call = _is_admin_call(last_at_me_uid)
    ref_urls = list(reference_image_urls or [])

    async def mute_user(args: dict) -> str:
        try:
            target = int(args.get("target_qq", 0) or 0)
        except (ValueError, TypeError):
            return "拒绝: target_qq 不是合法数字"
        reason = (args.get("reason") or "")[:200]
        if target <= 0:
            return "拒绝: target_qq 缺失或非法"
        if target == self_qq:
            return "拒绝: 不能禁言自己"
        if ADMIN_QQ and target == ADMIN_QQ:
            return "拒绝: 不能禁言主人"
        if last_at_me_uid and target == last_at_me_uid:
            return "拒绝: 这人刚 @ 你求助, 别禁言提问者"
        if await _is_admin_or_owner(bot, group_id, target):
            return "拒绝: 对方是群主/管理员, 你管不动"
        if not admin_call and not _in_window(_mute_history[group_id], MUTE_WINDOW_SEC, MUTE_MAX_IN_WINDOW):
            return "拒绝: 10 分钟内已禁言过一次, 别太频繁"
        try:
            await bot.set_group_ban(
                group_id=group_id, user_id=target, duration=MUTE_DURATION_SEC
            )
            _record(_mute_history[group_id])
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "mute_user",
                    {"target": target, "duration": MUTE_DURATION_SEC, "reason": reason},
                    True, "",
                )
            )
            logger.info(f"agent mute_user 群 {group_id} target={target} reason={reason[:40]!r}")
            return f"成功: 禁言 QQ {target} 60 秒"
        except Exception as e:
            err = str(e)[:200]
            logger.warning(f"agent mute_user 失败: {e}")
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "mute_user",
                    {"target": target, "reason": reason}, False, err,
                )
            )
            return "失败: 禁不动(权限不够或对方是管理员)"

    async def set_special_title(args: dict) -> str:
        try:
            target = int(args.get("target_qq", 0) or 0)
        except (ValueError, TypeError):
            return "拒绝: target_qq 不是合法数字"
        title = (args.get("title") or "").strip()
        if target <= 0:
            return "拒绝: target_qq 缺失"
        if not title:
            return "拒绝: 头衔不能为空"
        if len(title) > TITLE_MAX_LEN:
            return f"拒绝: 头衔最多 {TITLE_MAX_LEN} 个字"
        if target == self_qq:
            return "拒绝: 不能改自己头衔"
        # 主人保护: 别人请求改主人 → 拒;主人自己请求改自己 → 放行
        is_self_request_from_admin = (
            ADMIN_QQ and last_at_me_uid == ADMIN_QQ and target == ADMIN_QQ
        )
        if ADMIN_QQ and target == ADMIN_QQ and not is_self_request_from_admin:
            return "拒绝: 不能给主人加头衔(他自己要才行)"
        # admin/owner 检查也对主人自请求豁免(主人现在是管理员, 不豁免改不了自己)
        if not is_self_request_from_admin and await _is_admin_or_owner(bot, group_id, target):
            return "拒绝: 对方是群主/管理员"
        if not admin_call and not _in_window(_title_history[group_id], TITLE_WINDOW_SEC, TITLE_MAX_IN_WINDOW):
            return f"拒绝: 24 小时内已设头衔 {TITLE_MAX_IN_WINDOW} 次"
        try:
            await bot.set_group_special_title(
                group_id=group_id, user_id=target, special_title=title, duration=-1
            )
            _record(_title_history[group_id])
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "set_special_title",
                    {"target": target, "title": title}, True, "",
                )
            )
            logger.info(f"agent set_title 群 {group_id} target={target} title={title!r}")
            return f"成功: 给 QQ {target} 设头衔 '{title}'"
        except Exception as e:
            err = str(e)[:200]
            logger.warning(f"agent set_title 失败: {e}")
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "set_special_title",
                    {"target": target, "title": title}, False, err,
                )
            )
            return "失败: 头衔设不上(可能需要群主权限)"

    async def set_member_card(args: dict) -> str:
        try:
            target = int(args.get("target_qq", 0) or 0)
        except (ValueError, TypeError):
            return "拒绝: target_qq 不是合法数字"
        card = (args.get("card") or "").strip()
        if target <= 0:
            return "拒绝: target_qq 缺失"
        if len(card) > CARD_MAX_LEN:
            return f"拒绝: 名片最多 {CARD_MAX_LEN} 字"
        if target == self_qq:
            return "拒绝: 不能改自己名片"
        # 主人保护: 别人改主人 → 拒;主人改自己 → 放行
        is_self_request_from_admin = (
            ADMIN_QQ and last_at_me_uid == ADMIN_QQ and target == ADMIN_QQ
        )
        if ADMIN_QQ and target == ADMIN_QQ and not is_self_request_from_admin:
            return "拒绝: 不能改主人的名片(他自己要才行)"
        if not is_self_request_from_admin and await _is_admin_or_owner(bot, group_id, target):
            return "拒绝: 对方是群主/管理员"
        if not admin_call and not _in_window(_card_history[group_id], CARD_WINDOW_SEC, CARD_MAX_IN_WINDOW):
            return f"拒绝: 1 小时内已改名片 {CARD_MAX_IN_WINDOW} 次"
        try:
            await bot.set_group_card(group_id=group_id, user_id=target, card=card)
            _record(_card_history[group_id])
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "set_member_card",
                    {"target": target, "card": card}, True, "",
                )
            )
            logger.info(f"agent set_card 群 {group_id} target={target} card={card!r}")
            return f"成功: 改 QQ {target} 名片为 '{card}'"
        except Exception as e:
            err = str(e)[:200]
            logger.warning(f"agent set_card 失败: {e}")
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "set_member_card",
                    {"target": target, "card": card}, False, err,
                )
            )
            return "失败: 名片改不动"

    async def recall_my_message(args: dict) -> str:
        try:
            mid = int(args.get("message_id", 0) or 0)
        except (ValueError, TypeError):
            return "拒绝: message_id 不是合法数字"
        if mid <= 0:
            return "拒绝: message_id 缺失"
        if not recall.is_my_recent_message(group_id, mid, RECALL_AGE_LIMIT_SEC):
            return "拒绝: 这条不是你发的或已超过 6 小时"
        try:
            await bot.call_api("delete_msg", message_id=mid)
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "recall_my_message",
                    {"message_id": mid}, True, "",
                )
            )
            logger.info(f"agent recall_msg 群 {group_id} mid={mid}")
            return f"成功: 撤回 mid={mid}"
        except Exception as e:
            err = str(e)[:200]
            logger.warning(f"agent recall_msg 失败: {e}")
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "recall_my_message",
                    {"message_id": mid}, False, err,
                )
            )
            return f"失败: 撤不动 ({type(e).__name__})"

    async def web_search(args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "拒绝: query 为空"
        if len(query) > 200:
            return "拒绝: query 太长(>200)"
        if not SEARCH_API_KEY:
            return "失败: 搜索功能未配置, 你只能凭已有知识答"
        if not admin_call and not _in_window(_web_search_history[group_id], WEB_SEARCH_WINDOW_SEC, WEB_SEARCH_MAX_IN_WINDOW):
            return "拒绝: 1 分钟内搜过 5 次了, 别死循环搜"
        try:
            if SEARCH_PROVIDER == "tavily":
                result = await _do_tavily_search(query)
            else:
                return f"失败: 未知搜索 provider '{SEARCH_PROVIDER}'"
        except httpx.HTTPStatusError as e:
            logger.warning(f"web_search HTTP {e.response.status_code} query={query!r}")
            return f"失败: 搜索接口返 {e.response.status_code}, 等会儿再问"
        except Exception as e:
            logger.warning(f"web_search 异常 query={query!r}: {type(e).__name__}: {e}")
            return f"失败: 搜索接口出错 ({type(e).__name__})"
        if not result:
            return "搜了但没有结果, 换个关键词或者直接凭印象答"
        _record(_web_search_history[group_id])
        asyncio.create_task(
            db.admin_log(
                group_id, self_qq, "web_search",
                {"query": query, "provider": SEARCH_PROVIDER}, True, "",
            )
        )
        logger.info(f"agent web_search 群 {group_id} q={query!r} chars={len(result)}")
        # cap 输出, 避免占满 GPT 上下文
        return result[:2000]

    async def set_group_admin(args: dict) -> str:
        # 只主人能调用(防止 prompt injection 让别人把自己提为管理员)
        if not ADMIN_QQ or last_at_me_uid != ADMIN_QQ:
            return "拒绝: 这个工具只主人能用,你不行"
        try:
            target = int(args.get("target_qq", 0) or 0)
        except (ValueError, TypeError):
            return "拒绝: target_qq 不是合法数字"
        enable = bool(args.get("enable", False))
        if target <= 0:
            return "拒绝: target_qq 缺失"
        if target == self_qq:
            return "拒绝: 不能动自己的管理员身份(群主限制)"
        if not _in_window(_admin_promo_history[group_id], ADMIN_PROMOTION_WINDOW_SEC, ADMIN_PROMOTION_MAX):
            return f"拒绝: 1 小时内已升/降管理员 {ADMIN_PROMOTION_MAX} 次"
        try:
            await bot.set_group_admin(group_id=group_id, user_id=target, enable=enable)
            _record(_admin_promo_history[group_id])
            action = "提升" if enable else "取消"
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "set_group_admin",
                    {"target": target, "enable": enable}, True, "",
                )
            )
            logger.info(f"agent set_group_admin 群 {group_id} target={target} enable={enable}")
            return f"成功: 已{action} QQ {target} 的管理员"
        except Exception as e:
            err = str(e)[:200]
            logger.warning(f"agent set_group_admin 失败: {e}")
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "set_group_admin",
                    {"target": target, "enable": enable}, False, err,
                )
            )
            return f"失败: 改不动(可能 NapCat 权限不够 / 对方是群主)"

    async def generate_image_in_chat(args: dict) -> str:
        visual_prompt = (args.get("visual_prompt") or "").strip()
        if len(visual_prompt) < 5:
            return "拒绝: prompt 太短"
        if len(visual_prompt) > 400:
            return "拒绝: prompt 太长(>400)"
        # 中国政治敏感词预过滤: image2 上游不识别中文政治敏感, 必须本地兜底
        hit, word = is_image_prompt_blocked(visual_prompt)
        if hit:
            logger.warning(
                f"agent gen_image_in_chat 群 {group_id} prompt 命中黑名单 {word!r}, 拒绝生成"
            )
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "generate_image_in_chat",
                    {"prompt": visual_prompt[:200], "blocked": word}, False,
                    f"blacklist hit: {word}",
                )
            )
            return f"拒绝: 涉及中国政治敏感内容 ({word}), 不画。换个题材。"
        orient = (args.get("orientation") or "square").lower()
        size_map = {
            "square": "1024x1024",
            "horizontal": "1536x1024",
            "vertical": "1024x1536",
        }
        size = size_map.get(orient, "1024x1024")
        if not admin_call and not _in_window(_image_chat_history[group_id], IMAGE_CHAT_WINDOW_SEC, IMAGE_CHAT_MAX):
            return "拒绝: 60 秒内已生成过一张图, 等等"

        # 自指词检测: 用户要画墨七七本人 → 强制注入人设 prompt
        # 这是功能性 — GPT 不认识墨七七, 不预置人设画出来全是路人
        vp_lower = visual_prompt.lower()
        is_self_ref = any(kw in visual_prompt or kw in vp_lower for kw in image_gen._SELF_REF_KEYWORDS_LOWER)
        if is_self_ref:
            base = image_gen._SELF_PORTRAIT_PROMPT
            if any(s in visual_prompt or s in vp_lower for s in ("自拍", "selfie", "phone", "镜头", "对镜")):
                base += (
                    " Selfie composition: upper-body framing, holding phone with one hand, "
                    "soft gaze toward camera, casual indoor lighting."
                )
            visual_prompt = base
            logger.info(f"agent gen_image 自指词 → 注入 {BOT_NAME} portrait prompt")

        b64, kind = await image_gen.generate_b64(
            visual_prompt, size=size, group_id=group_id,
            reference_urls=ref_urls if ref_urls else None,
        )
        if not b64:
            return {
                "nsfw": "失败: GPT image 安全审核拒了这张, 提示词触发了内容政策(可能是 NSFW / 暴力等)",
                "timeout": "失败: 出图超时(已重试 3 次仍卡), 让用户稍后再试",
                "busy": "失败: 本群有另一张图还在画, 让用户等画完再说",
                "server": "失败: 中转站服务器抽风(5xx), 让用户稍后再试",
                "client": "失败: 请求被拒(4xx) — prompt 可能太复杂 / 超限",
                "not_configured": "失败: 图像生成 key 未配置, 跟主人说一声",
                "no_b64": "失败: 上游返回但没 b64, 异常状态",
                "unknown": "失败: 未预期错误",
            }.get(kind, f"失败: 未知 kind={kind}")
        success_hint = ""
        try:
            img_seg = MessageSegment.image(f"base64://{b64}")
            sent = await bot.send_group_msg(group_id=group_id, message=img_seg)
            mid = sent.get("message_id") if isinstance(sent, dict) else None
            if mid:
                recall.remember_sent(group_id, mid)
            _record(_image_chat_history[group_id])
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "generate_image_in_chat",
                    {"prompt": visual_prompt[:200], "size": size}, True, "",
                )
            )
            logger.info(
                f"agent gen_image_in_chat 群 {group_id} size={size} "
                f"b64_kb={len(b64) // 1024} mid={mid}"
            )
            return "成功: 图已发到群里" + success_hint
        except Exception as e:
            err = str(e)[:200]
            logger.warning(f"agent gen_image_in_chat 发图失败: {e}")
            return f"失败: 图生出来了但发不到群里 ({type(e).__name__})"

    async def search_history(args: dict) -> str:
        query = (args.get("query") or "").strip()
        if len(query) < 3:
            return "拒绝: query 至少 3 字 (trigram 限制)"
        try:
            limit = max(1, min(int(args.get("limit", 5) or 5), 10))
        except (ValueError, TypeError):
            limit = 5
        if not admin_call and not _in_window(_history_search_history[group_id], HISTORY_SEARCH_WINDOW_SEC, HISTORY_SEARCH_MAX):
            return "拒绝: 1 分钟内已搜过 10 次了, 别死循环"
        try:
            results = await db.search_relevant(group_id, query, limit=limit, older_than_seconds=0)
        except Exception as e:
            logger.warning(f"agent search_history 失败 q={query!r}: {e}")
            return "失败: 检索出错"
        _record(_history_search_history[group_id])
        if not results:
            return f"搜了'{query}', 历史里没找到相关记录"
        lines = [f"找到 {len(results)} 条历史片段(query='{query}'):"]
        for i, r in enumerate(results, 1):
            who = BOT_NAME if r.get("role") == "assistant" else "群友"
            content = (r.get("content") or "").replace("\n", " ")[:120]
            lines.append(f"  [{i}] {who}: {content}")
        return "\n".join(lines)

    async def recall_latest_bot_message(args: dict) -> str:
        if not admin_call and not _in_window(_recall_latest_history[group_id], RECALL_LATEST_WINDOW_SEC, RECALL_LATEST_MAX):
            return "拒绝: 1 分钟内已撤回 3 次, 别太频繁"
        mid = recall.get_latest_my_message(group_id, age_sec=300)
        if mid is None:
            return "失败: 5 分钟内没找到你发过的消息, 撤不动"
        try:
            await bot.call_api("delete_msg", message_id=mid)
            recall.forget(group_id, mid)
            _record(_recall_latest_history[group_id])
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "recall_latest_bot_message",
                    {"message_id": mid}, True, "",
                )
            )
            logger.info(f"agent recall_latest 群 {group_id} mid={mid}")
            return f"成功: 撤回了最近一条 mid={mid}"
        except Exception as e:
            err = str(e)[:200]
            logger.warning(f"agent recall_latest 失败: {e}")
            return f"失败: 撤不动 ({type(e).__name__})"

    async def send_group_notice(args: dict) -> str:
        if not admin_call:
            return "拒绝: 群公告只有主人能让你发"
        content = (args.get("content") or "").strip()
        if not content:
            return "拒绝: 公告内容为空"
        if len(content) > 500:
            return "拒绝: 公告超 500 字, 太长"
        if not _in_window(_notice_history[group_id], NOTICE_WINDOW_SEC, NOTICE_MAX):
            return "拒绝: 1 小时内已发过公告, 别太频繁"
        try:
            await bot.call_api("_send_group_notice", group_id=group_id, content=content)
            _record(_notice_history[group_id])
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "send_group_notice",
                    {"content_head": content[:80]}, True, "",
                )
            )
            logger.info(f"agent send_group_notice 群 {group_id} head={content[:60]!r}")
            return "成功: 公告已发"
        except Exception as e:
            err = str(e)[:200]
            logger.warning(f"agent send_group_notice 失败: {e}")
            return f"失败: 公告发不出去 ({type(e).__name__})"

    async def set_group_avatar(args: dict) -> str:
        visual_prompt = (args.get("visual_prompt") or "").strip()
        if len(visual_prompt) < AVATAR_PROMPT_MIN_LEN:
            return f"拒绝: prompt 太短(<{AVATAR_PROMPT_MIN_LEN} 字), 你得说清楚要画什么"
        if len(visual_prompt) > AVATAR_PROMPT_MAX_LEN:
            return f"拒绝: prompt 太长(>{AVATAR_PROMPT_MAX_LEN} 字), 精简一下"
        # 屏蔽含糊关键词
        if visual_prompt in ("好看的", "随便", "随便画", "酷的", "好的"):
            return "拒绝: prompt 太含糊, 描述具体的风格 / 元素 / 氛围"
        # 中国政治敏感词预过滤
        hit, word = is_image_prompt_blocked(visual_prompt)
        if hit:
            logger.warning(
                f"agent set_group_avatar 群 {group_id} prompt 命中黑名单 {word!r}, 拒绝生成"
            )
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "set_group_avatar",
                    {"prompt": visual_prompt[:200], "blocked": word}, False,
                    f"blacklist hit: {word}",
                )
            )
            return f"拒绝: 涉及中国政治敏感内容 ({word}), 不画。"
        if not admin_call and not _in_window(_avatar_history[group_id], AVATAR_WINDOW_SEC, AVATAR_MAX_IN_WINDOW):
            return "拒绝: 24 小时内已换过一次头像, 别太频繁"
        # 生成图(走中转站 GPT-image, square 适合群头像)
        # 不预过滤 / 不加 no-text 之类约束, GPT-image-2 自己 enforce
        full_prompt = visual_prompt + ", square composition suitable as a group chat avatar"
        b64, kind = await image_gen.generate_b64(
            full_prompt, size="1024x1024", group_id=group_id,
            reference_urls=ref_urls if ref_urls else None,
        )
        if not b64:
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "set_group_avatar",
                    {"visual_prompt": visual_prompt, "kind": kind}, False, f"image_gen kind={kind}",
                )
            )
            return {
                "nsfw": "失败: GPT image 拒了 prompt(内容政策)",
                "timeout": "失败: 出图超时, 稍后再试",
                "server": "失败: 中转站抽风",
                "client": "失败: prompt 太复杂被拒",
                "not_configured": "失败: 图像 key 未配置",
                "no_b64": "失败: 上游异常无 b64",
                "unknown": "失败: 未预期错误",
            }.get(kind, f"失败: kind={kind}")
        try:
            await bot.call_api(
                "set_group_portrait",
                group_id=group_id,
                file=f"base64://{b64}",
                cache=1,
            )
            _record(_avatar_history[group_id])
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "set_group_avatar",
                    {"visual_prompt": visual_prompt, "b64_kb": len(b64) // 1024}, True, "",
                )
            )
            logger.info(
                f"agent set_avatar 群 {group_id} prompt={visual_prompt[:60]!r} "
                f"b64_kb={len(b64) // 1024}"
            )
            return "成功: 换头像了"
        except Exception as e:
            err = str(e)[:200]
            logger.warning(f"agent set_avatar 上传失败: {e}")
            asyncio.create_task(
                db.admin_log(
                    group_id, self_qq, "set_group_avatar",
                    {"visual_prompt": visual_prompt}, False, f"upload: {err}",
                )
            )
            return "失败: 图生出来了但 QQ 不让上传(权限或格式问题)"

    return {
        "mute_user": mute_user,
        "set_special_title": set_special_title,
        "set_member_card": set_member_card,
        "recall_my_message": recall_my_message,
        "recall_latest_bot_message": recall_latest_bot_message,
        "set_group_avatar": set_group_avatar,
        "set_group_admin": set_group_admin,
        "send_group_notice": send_group_notice,
        "generate_image_in_chat": generate_image_in_chat,
        "search_history": search_history,
        "web_search": web_search,
    }
