"""
AI 输出过滤 —— 发送前最后一道防线
- 去掉 "作为AI/作为语言模型" 这类自暴自弃前缀
- 剥 markdown（* # ` >）
- 去掉颜文字 / emoji（人设禁止）
- 截断超长（> MAX_LEN）
- 命中输出侧黑名单 → 返回 None（上游静默放弃）
- 偶尔尾部加口头禅
"""
import re
import random

from .blacklist import is_output_blacklisted
from .catchphrase import maybe_suffix
from .reactions import NAMED_FACES

# [表情:笑死] / [表情:doge] → 提取名字, 转 [CQ:face,id=178] 给 NoneBot 解析
# 同时容忍中文冒号
_FACE_MARKER_RE = re.compile(r"\[表情[:：]\s*([^\]]{1,12}?)\s*\]")


def _convert_face_markers(s: str) -> str:
    """把命名表情标记转成 OneBot CQ 码, 未知名直接删(免得显示成 '[表情:xxx]' 这种 raw)"""
    def repl(m):
        name = m.group(1).strip()
        face_id = NAMED_FACES.get(name) or NAMED_FACES.get(name.lower())
        return f"[CQ:face,id={face_id}]" if face_id else ""
    return _FACE_MARKER_RE.sub(repl, s)

DEFAULT_MAX_LEN = 60  # 默认硬截断；调用方可传更大值（详细模式）
SOFT_LEN = 45  # 软上限：超过偏好分段（实际分段在 __init__ 里做）

# "作为AI/语言模型" 的常见自暴自弃句式
_AI_DISCLAIMERS = re.compile(
    r"(作为(一个)?(AI|人工智能|语言模型|大语言模型|聊天机器人|机器人|程序|AI助手)[，,：:]?\s*)"
    r"|(我是(一个)?(AI|人工智能|语言模型|大语言模型|聊天机器人|机器人|AI助手)[，,：:]?\s*)"
    r"|(I am an AI[\s,]*)"
    r"|(As an AI[\s,]*)",
    re.IGNORECASE,
)

# markdown 标记残留
_MD_HEAD = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_QUOTE = re.compile(r"^>\s*", re.MULTILINE)
_MD_LIST = re.compile(r"^[-*+]\s+", re.MULTILINE)

# 颜文字（人设禁止）
# 触发字符集只保留几乎只出现在颜文字里的：ω Θ ・ ´ ` 〃 ≧ ≦ ˇ ^ \
# 不再把 > < / = 当触发字符 —— 它们在正常技术/数学输出中太常见（如 "(100 m/s)"、"(a=1)"、"(b<c)"、"(x > 0)"）
# 代价：失去对 (>_<) (=_=) (/_\) 这类纯 ASCII 颜文字的识别；人设禁止颜文字 + Unicode 颜文字更常见，权衡可接受
_KAOMOJI = re.compile(r"[（(][^）)]*[ωΘ・´`〃≧ˇ≦^\\][^）)]*[）)]")
# emoji 范围（粗略）
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F]+",
    flags=re.UNICODE,
)

# 收尾的浮夸标点 / 卖萌字
_CUTESY_TAIL = re.compile(r"[～~哒哒哒哦哦哦呢呢呢]+$")


def _strip_markdown(s: str) -> str:
    s = _MD_CODE_BLOCK.sub(lambda m: m.group(0).strip("`"), s)
    s = _MD_INLINE_CODE.sub(r"\1", s)
    s = _MD_HEAD.sub("", s)
    s = _MD_BOLD.sub(r"\1", s)
    s = _MD_ITALIC.sub(r"\1", s)
    s = _MD_QUOTE.sub("", s)
    s = _MD_LIST.sub("", s)
    return s


def _strip_emoji(s: str) -> str:
    s = _KAOMOJI.sub("", s)
    s = _EMOJI.sub("", s)
    return s


def _truncate(s: str, max_len: int) -> str:
    """按句号/逗号在 max_len 附近软切, 找不到则保留到最近词边界 + '...';
    旧版兜底直接 s[:max_len]+'。' 可能切到中文双字词中间(如 '牛逼' 只剩 '牛'),
    现在改成 '...' 至少明示截断, 视觉不会留孤字
    """
    if len(s) <= max_len:
        return s
    # 1) 优先在 [max_len-20, max_len+10] 区间找硬标点(句号问号叹号), 软切最自然
    for hard in ("。", "！", "？", ".", "!", "?"):
        idx = s.rfind(hard, max_len - 20, max_len + 10)
        if idx >= max_len - 20:
            return s[: idx + 1].rstrip()
    # 2) 退一步找软标点(逗号/空格)
    for soft in ("，", ",", " ", "、"):
        idx = s.rfind(soft, max_len - 20, max_len + 10)
        if idx >= max_len - 20:
            return s[: idx + 1].rstrip().rstrip(soft) + "..."
    # 3) 实在找不到: 直接到 max_len, 加 '...' 明示截断(不补'。' 防造单字"句")
    return s[:max_len].rstrip() + "..."


def clean(reply: str, max_len: int = DEFAULT_MAX_LEN) -> str | None:
    """
    清洗 AI 回复；返回干净文本 or None（命中黑名单要弃用）
    max_len: 硬截断长度（详细模式调用方传 100）
    """
    if not reply:
        return None
    s = reply.strip()

    # 1) 黑名单(输出侧窄子集; 通用政治词汇放过, 允许讨论历史/教育内容)
    hit, word = is_output_blacklisted(s)
    if hit:
        # 日志带匹配词, 方便事后查为啥某条被过滤
        import logging as _lg
        _lg.getLogger("chat").info(f"输出黑名单命中 '{word}', head={s[:50]!r}")
        return None

    # 2) 干掉自暴自弃
    s = _AI_DISCLAIMERS.sub("", s)

    # 3) 剥 markdown
    s = _strip_markdown(s)

    # 4) 剥颜文字 / emoji
    s = _strip_emoji(s)

    # 5) 干掉卖萌结尾
    s = _CUTESY_TAIL.sub("", s).rstrip()

    # 5.5) 表情标记 → CQ 码(在截断前做, 标记本身长度不该挤占预算)
    s = _convert_face_markers(s)

    # 6) 去多余空白
    s = re.sub(r"\n{2,}", "\n", s).strip()
    if not s:
        return None

    # 7) 截断(注: CQ 码会被算进 length, 长 prompt 时偶尔会切到一半 CQ; 罕见, 暂不补丁)
    s = _truncate(s, max_len)

    # 8) 偶尔加口头禅（短文本不加 / 已经有"。"结尾才加）
    s = maybe_suffix(s)

    return s.strip() or None


def needs_split(reply: str) -> bool:
    """判断是否值得拆 2-3 条发"""
    return len(reply) > SOFT_LEN and ("。" in reply or "..." in reply or "?" in reply or "？" in reply)


def split_segments(reply: str, max_parts: int = 3) -> list[str]:
    """
    把回复按句号 / 省略号 / 问号拆成 ≤max_parts 段
    """
    if not needs_split(reply):
        return [reply]
    # 用 splitting 保留分隔符
    parts = re.split(r"(?<=[。！？!?])|(?<=\.\.\.)", reply)
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return [reply]
    # 合并直到 ≤max_parts
    while len(parts) > max_parts:
        # 把最短的相邻两段合并
        idx = min(range(len(parts) - 1), key=lambda i: len(parts[i]) + len(parts[i + 1]))
        parts[idx : idx + 2] = [parts[idx] + parts[idx + 1]]
    return parts
