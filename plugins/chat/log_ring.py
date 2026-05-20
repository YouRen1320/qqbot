"""
内存 ring buffer 收 loguru 日志, 供 self_maintain 读"最近发生了什么"。
- 启动时 install_sink() 挂到 NoneBot 的 loguru
- 保留最近 5000 条; 超出自动丢老的(deque maxlen)
- 不写文件, 重启后清零 — 维修 agent 看的是当前生命周期内的事故
"""
from collections import deque
import threading
from typing import Optional

# 5000 行够覆盖最近 1-3 小时正常活动(配合 INFO level 节流)
MAX_RING_SIZE = 5000

_LOG_RING: deque[str] = deque(maxlen=MAX_RING_SIZE)
_LOG_LOCK = threading.Lock()
_INSTALLED = False


def _sink(message) -> None:
    # loguru 的 message 是 loguru Message 对象, str() 后是格式化字符串(带时间/level/name)
    with _LOG_LOCK:
        _LOG_RING.append(str(message).rstrip())


def install_sink() -> None:
    """挂到 NoneBot 的 loguru。幂等;__init__.py 只调一次"""
    global _INSTALLED
    if _INSTALLED:
        return
    from nonebot.log import logger as _l
    _l.add(
        _sink,
        level="INFO",
        format="{time:HH:mm:ss} [{level}] {name} | {message}",
    )
    _INSTALLED = True


def recent(n: int = 500, level_filter: Optional[str] = None) -> list[str]:
    """
    取最近 n 行;可选按 level 过滤(传 'ERROR' / 'WARNING')。
    level 通过 substring 匹配格式化后的字符串 — 简单粗暴, 工程上够用。
    """
    with _LOG_LOCK:
        lines = list(_LOG_RING)
    if level_filter:
        token = f"[{level_filter}]"
        lines = [l for l in lines if token in l]
    return lines[-n:]


def size() -> int:
    with _LOG_LOCK:
        return len(_LOG_RING)
