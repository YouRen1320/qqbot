"""
节日彩蛋 —— 当日 10:00 抛个节日梗
- 农历节日（春节/中秋）使用预先映射好的公历日期，避免引入 lunar 库
- 公历节日（元旦/圣诞/情人节/光棍节）直接日期匹配
- 命中：scheduler 在 10:00 调一次（每群每节日一天 1 次）
"""
from datetime import date

# 公历节日 (month, day) → 文案候选
_FIXED: dict[tuple[int, int], tuple[str, ...]] = {
    (1, 1): ("新年快乐 又老一岁。", "元旦快乐 假呢？只放一天？", "新年 flag 立一立 反正最后也不会做"),
    (2, 14): ("情人节 单身的别看群消息了 自己冷静下", "情人节 我对象是工位"),
    (3, 8): ("妇女节快乐 别叫我女神 谢谢", "三八 礼物呢？"),
    (4, 1): ("愚人节 真话假话各说一句 自己猜",),
    (5, 1): ("五一假期 我们就放一天是吧",),
    (5, 4): ("青年节 你还青年？",),
    (6, 1): ("儿童节 装一天小孩 不许加班",),
    (9, 10): ("教师节 想念我的语文老师 想念语文",),
    (10, 1): ("国庆 终于能睡到自然醒",),
    (11, 1): ("万圣节 鬼？我才是鬼",),
    (11, 11): ("双十一 你买了啥 让我看看你智商税",),
    (12, 24): ("平安夜 苹果买了吗 我没",),
    (12, 25): ("圣诞快乐 没对象的别看群",),
    (12, 31): ("跨年了 这一年也没干啥",),
}

# 农历春节（公历日期，预填到 2030）
_LUNAR_NEW_YEAR: dict[int, tuple[int, int]] = {
    2026: (2, 17),
    2027: (2, 6),
    2028: (1, 26),
    2029: (2, 13),
    2030: (2, 3),
}

# 农历中秋
_MID_AUTUMN: dict[int, tuple[int, int]] = {
    2026: (9, 25),
    2027: (9, 15),
    2028: (10, 3),
    2029: (9, 22),
    2030: (9, 12),
}

_SPRING_TEXT = ("过年好 红包发了吗 V我50", "新年新气象 我还在群里阴阳")
_MID_AUTUMN_TEXT = ("中秋快乐 月饼买了吗 别买五仁的", "中秋节 谢谢老板没发月饼")


def text_for_today() -> str | None:
    """返回当天节日文案；非节日返回 None"""
    today = date.today()
    md = (today.month, today.day)
    if _LUNAR_NEW_YEAR.get(today.year) == md:
        return _SPRING_TEXT[today.day % len(_SPRING_TEXT)]
    if _MID_AUTUMN.get(today.year) == md:
        return _MID_AUTUMN_TEXT[today.day % len(_MID_AUTUMN_TEXT)]
    if md in _FIXED:
        candidates = _FIXED[md]
        return candidates[today.day % len(candidates)]
    return None
