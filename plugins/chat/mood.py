"""
心情系统 —— 每天一个心情值，只影响语气和插话欲望
- 心情由日期 seed，同一天值固定（重启不变）
- 三档：低落 25% / 平常 50% / 亢奋 25%
- 字数松紧不在这里管，由 __init__.py 的"详细骰子"控制
"""
import hashlib
from datetime import date


def _mood_for(d: date) -> str:
    h = hashlib.md5(d.isoformat().encode()).digest()
    r = h[0] / 255.0
    if r < 0.25:
        return "low"
    if r < 0.75:
        return "normal"
    return "high"


def today_mood() -> str:
    return _mood_for(date.today())


def mood_multiplier() -> float:
    return {"low": 0.6, "normal": 1.0, "high": 1.4}[today_mood()]


def mood_prompt_suffix() -> str:
    return {
        "low": "\n\n[今日心情：丧丧的，话少几句，能不接就不接，回话偏冷淡]",
        "normal": "",
        "high": "\n\n[今日心情：状态拉满，嘴特别欠，整活欲望高，多接点话茬]",
    }[today_mood()]
