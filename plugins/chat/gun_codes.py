"""
三角洲行动改枪码查询
- 数据：plugins/chat/data/gun_codes.json（手动维护）
- 触发：用户消息含"改枪码/配装/压枪/怎么改"等关键词 + 枪名/别名
- 命中后返回所有版本的码（带傲娇引子，匹配 v3.0 人设）
- 不命中返回 None，让主流程继续走 AI
"""
import json
import random
from pathlib import Path
from typing import Optional

from nonebot.log import logger

_DATA_PATH = Path(__file__).parent / "data" / "gun_codes.json"

# 触发词：含其中任意一个 + 命中枪名才算意图明确
_INTENT_WORDS = (
    "改枪码", "改枪", "配装码", "配装", "压枪",
    "怎么改", "改成", "改件", "配件码", "枪码",
)

# 傲娇引子（学结构不学原文 → 这里是直接字面）
_PREFIXES = [
    "{name} 是吧？这种程度的我顺手存了几个，记好了：",
    "哼，{name} 的码我倒是有，看你这么诚恳就给你：",
    "算你走运，{name} 的配装我顺便整理过：",
    "{name} 啊，行吧给你掏出来：",
    "真是的，{name} 这种基础的也来问。给：",
]

_SUFFIXES = [
    "码可能随版本失效，不行就去 sjz.upx8.com 自己查最新的。",
    "用之前注意看下是不是当前赛季能用。",
    "记住了，下次自己存一份。",
    "别拿来打我，谢谢。",
]


class _WeaponDB:
    def __init__(self) -> None:
        self.weapons: list[dict] = []
        self.alias_map: dict[str, dict] = {}  # lowercase alias → weapon dict
        self._load()

    def _load(self) -> None:
        try:
            raw = _DATA_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
            self.weapons = data.get("weapons", [])
            self.alias_map = {}
            for w in self.weapons:
                for a in w.get("aliases", []) + [w["name"]]:
                    self.alias_map[a.lower()] = w
            logger.info(
                f"gun_codes 加载完成: {len(self.weapons)} 把枪 / {len(self.alias_map)} 个别名"
            )
        except FileNotFoundError:
            logger.warning(f"gun_codes 数据文件不存在: {_DATA_PATH}")
        except Exception as e:
            logger.warning(f"gun_codes 加载失败: {e}")

    def find(self, text: str) -> Optional[dict]:
        """文本里扫枪名/别名，命中最长的一个（避免 AKM 被 AK 抢匹配）"""
        text_lower = text.lower()
        hits: list[tuple[int, dict]] = []
        for alias, weapon in self.alias_map.items():
            if alias in text_lower:
                hits.append((len(alias), weapon))
        if not hits:
            return None
        hits.sort(key=lambda x: -x[0])  # 长别名优先
        return hits[0][1]


_DB = _WeaponDB()


def _has_intent(text: str) -> bool:
    return any(w in text for w in _INTENT_WORDS)


def try_match(text: str) -> Optional[str]:
    """
    检查文本是否在问改枪码，命中返回拼好的回复，否则 None。
    要求：触发词 + 命中枪名同时满足，避免误触发。
    """
    if not text or not _has_intent(text):
        return None
    weapon = _DB.find(text)
    if not weapon:
        return None

    name = weapon["name"]
    codes = weapon.get("codes", [])
    if not codes:
        return None

    prefix = random.choice(_PREFIXES).format(name=name)
    lines = [prefix]
    for c in codes:
        lines.append(f"{c['mode']} {c['tag']}: {c['code']}")
    lines.append(random.choice(_SUFFIXES))
    return "\n".join(lines)


def reload() -> int:
    """运行时热重载数据文件（admin /reload_guns 可调）"""
    _DB._load()
    return len(_DB.weapons)
