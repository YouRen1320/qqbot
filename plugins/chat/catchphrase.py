"""
嘴毒口头禅池 —— 给 AI 回复偶尔加个尾巴
- 不是每条都加（看起来太机械）
- 短文本不加（避免变成主体）
- 已经以这些口头禅结尾的不加（避免重复）
"""
import random

_POOL: tuple[str, ...] = (
    "笑死",
    "典",
    "就这",
    "急了",
    "牛",
    "6",
    "好家伙",
    "我直接",
    "自己品",
    "寻思",
    "绷不住",
    "你品",
)

# 命中概率（每次回复检查一次）
SUFFIX_PROB = 0.18

# 短于这个不加
MIN_LEN = 8


def maybe_suffix(text: str) -> str:
    if not text or len(text) < MIN_LEN:
        return text
    if random.random() > SUFFIX_PROB:
        return text
    # 已经以池里某个口头禅结尾就不加
    tail = text[-4:]
    for w in _POOL:
        if tail.endswith(w):
            return text
    # 避免加在最后是逗号/句号之后已经很完整的情况下重复
    suffix = random.choice(_POOL)
    sep = "" if text.endswith(("。", "...", "?", "？", ".", ",", "，", "!", "！")) else "。"
    return text + sep + suffix
