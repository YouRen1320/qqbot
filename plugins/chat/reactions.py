"""
表情回应 —— 用 QQ 表情贴消息，不发字
- NapCat 支持 set_msg_emoji_like / set_group_msg_emoji_like
- 命中关键词时偶尔用表情替代打字（比开口安全 + 省 token + 像真人）
"""
import random
from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger

# 多字关键词 → 候选 QQ 表情 ID（substring 匹配；多字符天然 FP 风险低）
_EMOJI_MAP: dict[str, tuple[str, ...]] = {
    "笑死":   ("178", "28", "182"),    # 斜眼笑 / 笑哭
    "绷不住": ("179", "174"),           # doge / 无奈
    "666":    ("76",),                   # 赞
    "v50":    ("66", "63"),              # 爱心 / 玫瑰
    "v我50":  ("66", "63"),
    "肯德基": ("204",),                  # 吃
    "kfc":    ("204",),
    "星期四": ("204", "66"),
    "下班":   ("277", "201"),            # 拍拍 / 点赞
    "周五":   ("76", "201"),
    "加班":   ("277", "174"),            # 拍拍 / 无奈
    "摸鱼":   ("179", "326"),            # doge / 摸鱼
    "工资":   ("319",),                  # 暴富
    "好家伙": ("32", "180"),             # 疑问 / 惊喜
    "服了":   ("174", "32"),
    "不会吧": ("32",),
    "鲁迅":   ("174", "179"),
    "答辩":   ("174",),
    "退退退": ("318",),
    "雪豹":   ("197",),                  # 冷漠
}

# 单字感叹关键词 —— 只在"剥掉无关边角后纯感叹"上下文匹配
# 避免 "草莓/牛肉/麻烦/急救/经典/积累/卷子/绷带" 误中
#   "草" / "草草草" / "好草啊" / "牛！" / "卧草" / "我累了" 都能匹配
#   "草地很好玩 / 牛肉好吃 / 麻烦你了 / 经典 / 积累" 都不会
_SINGLE_EMOJI_MAP: dict[str, tuple[str, ...]] = {
    "草": ("179", "178"),
    "牛": ("76", "124"),
    "麻": ("174", "197"),
    "急": ("318", "104"),
    "典": ("179",),
    "绷": ("179", "174"),
    "累": ("277", "174"),
    "卷": ("174", "318"),
}

# 剥掉这些"边角"后如果只剩一个独特字符，就当感叹处理
# 包含：常见语气词 / 强调词 / 主语小词 / 标点空白
_TRIVIA = set(
    "啊啦呢嗯哦喔呀咯嘛吧咦哈嘿哟呐欸哎诶哇"  # 语气助词
    "好真太挺超巨颇蛮老死贼"                  # 程度副词
    "了的我卧"                                # 常见小词
    " \t\n!！?？。.,，;；：:、~～"            # 标点空白
)

# 命中关键词时，使用表情代替打字回复的概率
REACT_INSTEAD_OF_REPLY_PROB = 0.25

# "已读"表情池 —— 每条文本回复前贴一个，作为读消息的视觉反馈
# 178 斜眼笑 / 179 doge / 277 拍拍 / 124 OK / 32 疑问 —— 都比较中性百搭
READ_RECEIPT_POOL: tuple[str, ...] = ("178", "179", "277", "124", "32")

# === GPT 在回复里用的"命名表情" → QQ face_id ===
# 给 output_filter 把 [表情:XX] 转 [CQ:face,id=N], 让 bot 回复内嵌 QQ 系统表情
# 名字直白让 GPT 不用记 ID; 白名单防 GPT 乱传 ID 显示成 ?
NAMED_FACES: dict[str, str] = {
    # 笑系
    "笑死": "178", "斜眼笑": "178", "哈哈": "178", "笑哭": "182",
    "微笑": "14",
    # doge / 嘲
    "doge": "179", "DOGE": "179",
    # 无奈 / 服
    "无奈": "174", "绷不住": "174", "服了": "174", "服": "174",
    # 拍拍
    "拍拍": "277",
    # OK / 赞 / 666
    "OK": "124", "ok": "124", "好的": "124",
    "赞": "76", "点赞": "76", "666": "76", "牛": "76", "牛逼": "76",
    # 心 / 玫瑰
    "爱心": "66", "心": "66", "玫瑰": "63",
    # 思 / 疑
    "思考": "32", "疑问": "32",
    # 哭 / 怒
    "哭": "5", "大哭": "5", "怒": "8",
    # 鄙视 / 冷漠 / 雪豹
    "鄙视": "197", "冷漠": "197", "雪豹": "197",
    # 主题
    "暴富": "319", "退退退": "318",
    "饭": "204", "吃": "204",
    "摸鱼": "326",
    # 抱抱
    "抱抱": "183",
    # 惊 / 好家伙
    "惊": "180", "好家伙": "180",
    # 尴尬
    "尴尬": "9",
}


def is_lone_char_interjection(text: str, ch: str) -> bool:
    """文本剥掉边角 trivia 后只剩 ch 的重复（可能 1-N 次）→ True，否则 False。
    供调用方做单字感叹判定，避免 '草莓/牛肉/经典/积累/卷子/绷带' 这种长词被误中。
    复用本模块的 _TRIVIA 池，调用方传入的 ch 应该是 .lower() 形态。"""
    t = text.lower()
    if ch not in t:
        return False
    stripped = "".join(c for c in t if c not in _TRIVIA)
    return bool(stripped) and all(c == ch for c in stripped)


def pick_emoji(text: str) -> str | None:
    """文本里命中关键词 → 返回一个候选表情 ID；未命中返回 None"""
    t = text.lower()
    # 1) 多字关键词：substring 任意位置匹配
    for kw, ids in _EMOJI_MAP.items():
        if kw in t:
            return random.choice(ids)
    # 2) 单字感叹关键词：剥掉 trivia 后只剩同一个字，且这个字在单字表里 → 命中
    stripped = "".join(c for c in t if c not in _TRIVIA)
    if stripped and len(set(stripped)) == 1:
        only_char = stripped[0]
        if only_char in _SINGLE_EMOJI_MAP:
            return random.choice(_SINGLE_EMOJI_MAP[only_char])
    return None


async def try_react(bot: Bot, message_id: int, emoji_id: str) -> bool:
    """调 NapCat 扩展接口贴表情；失败不抛，返回 False"""
    try:
        await bot.call_api("set_msg_emoji_like", message_id=message_id, emoji_id=emoji_id)
        return True
    except Exception as e:
        logger.warning(f"贴表情失败 mid={message_id} eid={emoji_id}: {e}")
        return False
