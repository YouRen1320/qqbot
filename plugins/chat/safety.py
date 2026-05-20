"""
风控保护层 —— 所有发送行为前都要过这里
- 群白名单（含运行时 /pause 临时禁言）
- 每群每分钟硬限（PER_MINUTE_LIMIT）：sliding window 60s
- 每群每天硬限（DAILY_LIMIT_PER_GROUP）：自然日重置
- admin /set 可热改两个上限值
"""
import os
import time
from collections import defaultdict, deque
from datetime import datetime

ENABLED_GROUPS = {g.strip() for g in os.getenv("ENABLED_GROUPS", "").split(",") if g.strip()}

# 运行时临时禁言（admin /pause）；覆盖白名单
_runtime_paused: set[int] = set()

# 默认硬上限 —— admin /set 可热改
DAILY_LIMIT_PER_GROUP = int(os.getenv("DAILY_LIMIT_PER_GROUP", "300"))
PER_MINUTE_LIMIT = int(os.getenv("PER_MINUTE_LIMIT", "20"))


def pause_group(group_id: int) -> None:
    _runtime_paused.add(group_id)


def resume_group(group_id: int) -> None:
    _runtime_paused.discard(group_id)


def get_paused() -> set[int]:
    return set(_runtime_paused)


def get_daily_counts() -> dict[int, int]:
    """暴露给 admin /status 用"""
    return dict(_daily_count)


# 每群发言计数
_daily_count: dict[int, int] = defaultdict(int)
_daily_reset_day: str = datetime.now().strftime("%Y-%m-%d")

# 每群最近 PER_MINUTE_LIMIT 次发言时间戳（用于 sliding window）
_recent_send_ts: dict[int, deque] = defaultdict(lambda: deque(maxlen=200))


def is_group_enabled(group_id: int) -> bool:
    """检查群是否在白名单（且未被运行时 /pause）"""
    if group_id in _runtime_paused:
        return False
    return str(group_id) in ENABLED_GROUPS


def hour_weight(now: datetime | None = None) -> float:
    """保留签名供调用方使用；当前恒为 1.0（不再按时段降权）"""
    return 1.0


def record_speaker(group_id: int, is_self: bool) -> None:
    """保留签名，便于将来加回限频逻辑；当前不做任何事"""
    return


def _maybe_reset_daily() -> None:
    """跨自然日清零"""
    global _daily_reset_day
    today = datetime.now().strftime("%Y-%m-%d")
    if today != _daily_reset_day:
        _daily_count.clear()
        _daily_reset_day = today


def _count_per_minute(group_id: int) -> int:
    """sliding window 60s 内的发送条数"""
    dq = _recent_send_ts[group_id]
    cutoff = time.time() - 60.0
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq)


def can_reply(group_id: int) -> tuple[bool, str]:
    """
    返回 (能否回复, 原因说明)
    通过即认为"准备发送"，累加日计数 + 记 timestamp
    不通过：当前分钟超限 / 当日超限
    """
    _maybe_reset_daily()

    if _daily_count[group_id] >= DAILY_LIMIT_PER_GROUP:
        return False, f"daily_limit({DAILY_LIMIT_PER_GROUP})"

    if _count_per_minute(group_id) >= PER_MINUTE_LIMIT:
        return False, f"per_minute_limit({PER_MINUTE_LIMIT})"

    _daily_count[group_id] += 1
    _recent_send_ts[group_id].append(time.time())
    return True, "ok"
