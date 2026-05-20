"""
共享状态 —— 跨模块复用的内存结构
- 抽出来是为了让 db.py 这种"反向操作 chat 内存"的模块不用引 chat/__init__.py（避免循环 import）
- 都是进程内 deque/dict，无锁；nonebot 单事件循环，单线程读写没问题

历史隔离策略：
- 私人对话主线按 (group_id, user_id) 隔离 → history
- 群最近若干条原文（含昵称）按 group_id 共享 → group_recent，做"在场感"用
- 复读机/复读跟风 仍按 group_id（recent_texts / last_echoed）
"""
from collections import defaultdict, deque

# 每个 (群, 用户) 私人对话窗口长度
HISTORY_LEN = 20

# (group_id, user_id) → 该用户在该群跟 bot 的私人对话上下文
history: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=HISTORY_LEN))

# group_id → 群最近若干条原文（仅作"环境感知"，不喂入用户主线）
# 每条是 {"nick": str, "content": str, "uid": int}
# uid 给 agent tools 用 — 让 GPT 看到"最近发言者的 QQ 号"才能引用执行管理操作
# 12 条窗口能覆盖一轮闲聊的所有参与者
GROUP_RECENT_LEN = 12
group_recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=GROUP_RECENT_LEN))

# group_id → 最近 5 条用户原文（用于复读机检测，群级）
recent_texts: dict[int, deque] = defaultdict(lambda: deque(maxlen=5))

# group_id → 最近一次跟过的复读文本（避免连跟两次）
last_echoed: dict[int, str] = defaultdict(str)

# group_id → 最近一条 QQ 原生视频的 (url, ts)；用于"上条视频 + 下条@bot 解析"跨消息引用
# 视频 URL 有时效，5 分钟内有效
last_video: dict[int, tuple[str, float]] = {}
LAST_VIDEO_TTL = 300.0
