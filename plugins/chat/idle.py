"""
冷场撑场子 —— 群里太安静就主动抛个梗
- 每群记下最后一次 *群友* 说话时间（不算 bot 自己）
- > IDLE_MIN 分钟没人说话 → 抛个梗
- 每群每天最多 1 次（避免半夜还在自说自话）
- 走 can_reply 风控
"""
import time
import random
from datetime import date
from collections import defaultdict

# 每群最后一次 *非 bot* 说话时间
_last_user_msg: dict[int, float] = defaultdict(float)
# 每群最近一次冷场抛梗的日期（用于每日限 1）
_last_idle_date: dict[int, str] = defaultdict(str)

# 多久算冷场（秒）
IDLE_SECONDS = 30 * 60

_PROMPTS: tuple[str, ...] = (
    "群里这么安静 都猝死了？",
    "好家伙 集体摸鱼是吧",
    "群友呢 都被老板抓去开会了？",
    "。。。这群是不是凉了",
    "睡了？这才几点啊",
    "今天没活整",
    "无聊 谁出来唠两句",
    "群冷成这样, 是都加班去了",
    "这么安静, 学姐都快听见自己心跳了",
)


def touch(group_id: int) -> None:
    """每次群友（非 bot）发言时调一下"""
    _last_user_msg[group_id] = time.time()


def peek_break_silence(group_id: int) -> str | None:
    """到点了就返回要发的文案；不到点返回 None。**不修改状态**，方便调用方先做风控判断。
    真正决定发的时候必须紧接着调 commit_break_silence(gid)，否则同一句会被反复试发。"""
    last = _last_user_msg.get(group_id, 0)
    if last == 0:
        return None
    if time.time() - last < IDLE_SECONDS:
        return None
    today = date.today().isoformat()
    if _last_idle_date.get(group_id) == today:
        return None
    return random.choice(_PROMPTS)


def commit_break_silence(group_id: int) -> None:
    """送出冷场抛梗后调一下，标记今日已发并重置静默时钟。"""
    _last_idle_date[group_id] = date.today().isoformat()
    _last_user_msg[group_id] = time.time()
