"""
撤回保护 —— 发出去发现不对劲，群友喊一声就帮你撤
- 记下 bot 最近发的 (group_id, message_id, ts)
- 5s 窗口内有人喊 "撤回" / "sb" / "删了" / "墨七七撤回" → 撤回那条
- 仅在窗口内、且 bot 仍有权限的情况下

还兼任 agent tool `recall_my_message` 的"这条是不是 bot 发的"判定 ——
长期跟踪 6h 内所有 bot 发出消息的 (group_id, message_id),agent 撤回前查一下。
"""
import re
import time
from collections import deque

from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger

# 每群保留最近 3 条 bot 发的消息（id + 时间戳）
_recent_sent: dict[int, deque] = {}
_WINDOW = 5.0  # 5 秒内有效

# 长期跟踪 bot 发出的所有消息(给 agent recall_my_message 工具用)
# key=(group_id, message_id), value=ts; 容量 500 条,LRU 淘汰
_my_messages: dict[tuple[int, int], float] = {}
_MY_MESSAGES_MAX = 500
MY_MESSAGES_DEFAULT_TTL = 21600  # 6 小时

# 中文/多字符撤回意图：直接 substring 即可
_RECALL_KEYWORDS = ("撤回", "撤了", "删了", "删除", "傻 b 啊", "傻 b", "重发", "塌房")
# 短 ASCII 词需要边界，避免 "sb" 误中 "sbus"/"看看 sbcs" 这种
_RECALL_SHORT_RE = re.compile(r"(?<![a-z0-9])sb(?![a-z0-9])", re.IGNORECASE)


def remember_sent(group_id: int, message_id: int) -> None:
    """bot 每发一条都记一下,既给撤回保护用,也给 agent 长期判定用"""
    q = _recent_sent.setdefault(group_id, deque(maxlen=3))
    q.append((message_id, time.time()))
    # 长期登记 — 给 agent tool 用
    _my_messages[(group_id, message_id)] = time.time()
    # 容量控制(超 max → 删最老的)
    if len(_my_messages) > _MY_MESSAGES_MAX:
        oldest_key = min(_my_messages, key=_my_messages.get)
        _my_messages.pop(oldest_key, None)


def is_my_recent_message(
    group_id: int, message_id: int, age_sec: float = MY_MESSAGES_DEFAULT_TTL
) -> bool:
    """检查这条消息是不是 bot 自己 age_sec 秒内发的(给 agent recall 用)"""
    ts = _my_messages.get((group_id, message_id))
    if ts is None:
        return False
    return time.time() - ts <= age_sec


def get_latest_my_message(group_id: int, age_sec: float = 300) -> int | None:
    """返回 bot 最近 age_sec 秒内在该群发的一条 message_id, 没有返 None。
    给 agent `recall_latest_bot_message` 工具用 — 用户喊"撤回那条"时不需要 GPT 提供 mid。"""
    cutoff = time.time() - age_sec
    latest_mid: int | None = None
    latest_ts: float = 0.0
    for (gid, mid), ts in _my_messages.items():
        if gid == group_id and ts >= cutoff and ts > latest_ts:
            latest_ts = ts
            latest_mid = mid
    return latest_mid


def forget(group_id: int, message_id: int) -> None:
    """从长期跟踪里删一条(agent 撤回成功后调,避免重复撤)"""
    _my_messages.pop((group_id, message_id), None)


def _pop_recallable(group_id: int) -> int | None:
    """取一个还在窗口内的最近 message_id；超窗的清掉"""
    q = _recent_sent.get(group_id)
    if not q:
        return None
    now = time.time()
    while q and now - q[-1][1] > _WINDOW:
        q.pop()
    if not q:
        return None
    mid, _ = q.pop()  # 用掉就移除，避免重复撤
    return mid


def is_recall_request(text: str) -> bool:
    if not text:
        return False
    t = text.lower().strip()
    # 太长的不是撤回意图，是讨论
    if len(t) > 12:
        return False
    if any(k in t for k in _RECALL_KEYWORDS):
        return True
    return bool(_RECALL_SHORT_RE.search(t))


async def try_recall(bot: Bot, group_id: int) -> bool:
    """有窗口内消息就撤；返回 True 表示撤了"""
    mid = _pop_recallable(group_id)
    if mid is None:
        return False
    try:
        await bot.call_api("delete_msg", message_id=mid)
        logger.info(f"撤回保护：已撤 group={group_id} mid={mid}")
        return True
    except Exception as e:
        logger.warning(f"撤回失败 mid={mid}: {e}")
        return False
