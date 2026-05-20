"""
群友画像 lite —— 每人最近几条发言留作 prompt 注脚
- 内存里存，不落盘，容器重启即清
- 当 墨七七 决定回复某人时，把"这人刚才还说过"几句附在 system prompt 上，方便针对性阴阳
- 同时记录该人累计交互次数，用于熟悉度判定 + 水群榜
"""
from collections import defaultdict, deque
from datetime import date

# (group_id, user_id) → 最近 N 条该人的发言
_PROFILE_LEN = 3
_profile: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=_PROFILE_LEN))

# (group_id, user_id) → 该人累计发言条数（用于熟悉度，跨日累计）
_interact_count: dict[tuple[int, int], int] = defaultdict(int)

# (group_id, user_id) → 今日发言条数（用于水群榜，每日 0:00 重置）
_today_count: dict[tuple[int, int], int] = defaultdict(int)
_today_day: str = date.today().isoformat()

# (group_id, user_id) → 最近一次见到的群名片/昵称
_nick: dict[tuple[int, int], str] = {}

# 熟悉度门槛
FAMILIAR_THRESHOLD = 5    # >= 这个数 → 熟人
CLOSE_THRESHOLD = 20      # >= 这个数 → 老熟人


def _maybe_rotate_day() -> None:
    """跨自然日清空今日榜计数；熟悉度计数不动"""
    global _today_day
    today = date.today().isoformat()
    if today != _today_day:
        _today_count.clear()
        _today_day = today


def record(group_id: int, user_id: int, nick: str, text: str) -> None:
    """记一条该人的发言"""
    _maybe_rotate_day()
    _profile[(group_id, user_id)].append(text)
    _interact_count[(group_id, user_id)] += 1
    _today_count[(group_id, user_id)] += 1
    if nick:
        # 防止昵称带换行 / 长尾把榜单排版打乱
        clean_nick = nick.replace("\n", " ").replace("\r", " ").strip()[:16]
        if clean_nick:
            _nick[(group_id, user_id)] = clean_nick


def top_speakers(group_id: int, n: int = 5) -> list[tuple[int, str, int]]:
    """今日发言数 top-n：返回 [(user_id, nick_or_uid_str, count), ...]
    爬日：调用前先 rotate；尚未发言过的群返回空列表。"""
    _maybe_rotate_day()
    rows = [
        (uid, _nick.get((gid, uid)) or str(uid), cnt)
        for (gid, uid), cnt in _today_count.items()
        if gid == group_id and cnt > 0
    ]
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows[:n]


def group_total_today(group_id: int) -> tuple[int, int]:
    """今日群整体水量：返回 (总条数, 发言人数)。"""
    _maybe_rotate_day()
    total = 0
    speakers = 0
    for (gid, _uid), cnt in _today_count.items():
        if gid == group_id and cnt > 0:
            total += cnt
            speakers += 1
    return total, speakers


def personal_stats(group_id: int, user_id: int) -> tuple[int, int, int, int]:
    """给"@bot 我多少条"用：返回 (today_count, total_count, today_rank, today_group_total)
    today_rank=0 表示今天没发过言；group_total 是今日有发言的人数（含 bot 自己）。"""
    _maybe_rotate_day()
    today = _today_count.get((group_id, user_id), 0)
    total = _interact_count.get((group_id, user_id), 0)
    # 排名 = 严格大于自己的人数 + 1；today=0 时返回 0（表示未上榜）
    if today == 0:
        rank = 0
    else:
        rank = 1 + sum(
            1
            for (gid, _uid), cnt in _today_count.items()
            if gid == group_id and cnt > today
        )
    group_total = sum(
        1
        for (gid, _uid), cnt in _today_count.items()
        if gid == group_id and cnt > 0
    )
    return today, total, rank, group_total


def _escape(text: str) -> str:
    """
    转义后塞到 prompt 注脚里，避免引号/换行/方括号让 LLM 误以为是新指令边界。
    """
    return (
        text.replace("\\", "\\\\")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace('"', "'")
        .replace("[", "(")
        .replace("]", ")")
    )


def get_familiarity(group_id: int, user_id: int) -> str:
    """返回 'new' / 'familiar' / 'close' —— 用于关系称呼"""
    n = _interact_count.get((group_id, user_id), 0)
    if n >= CLOSE_THRESHOLD:
        return "close"
    if n >= FAMILIAR_THRESHOLD:
        return "familiar"
    return "new"


def build_hint(group_id: int, user_id: int, nick: str) -> str:
    """生成喂给模型的 prompt 注脚；该人发言不足 2 条时返回空串"""
    msgs = list(_profile[(group_id, user_id)])
    # 当前这条已经被 record 过了，所以至少 2 条才有"之前还说过"的意义
    if len(msgs) < 2:
        return ""
    older = msgs[:-1]
    safe_nick = _escape(nick)
    lines = " / ".join(_escape(t) for t in older[-2:])
    # 熟悉度提示
    fam = get_familiarity(group_id, user_id)
    fam_tag = {
        "close": "（这人是群里老熟人，可以更损更随便点）",
        "familiar": "（这人聊过几次了，可以稍微亲近些）",
        "new": "",
    }[fam]
    return f"\n\n[当前说话的群友是 {safe_nick}{fam_tag}，他/她最近还说过：{lines}]"
