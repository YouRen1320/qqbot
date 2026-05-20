"""
关键词黑名单 —— 见到敏感/辱骂/政治/色情 → 直接闭嘴
- 用户消息侧：命中 → 直接 return，不走 AI、不上下文
- 输入端拦截，避免把烫手内容喂给 AI 留痕
- 这里是粗筛，宁可错杀不可放过；可按需调宽

词源（合并两层）：
- 手写补丁：辱骂词 + 兜底政治色情词（_INSULT_HARD / _POLITICS_MANUAL / _PORN_MANUAL）
- 外置词库：data/sensitive_words.txt（来自 konsheng/Sensitive-lexicon，已过滤短词）
  - 想增删词直接编辑 .txt 重启即可，无需改代码
"""
import re
from pathlib import Path
from nonebot.log import logger

# 手写补丁：中国政治敏感词（输入侧, 严格防"恶意引导"）
# 命中 → bot 闭嘴 + punishment.py 升级处罚(禁言→踢)
# 维护原则: 命中后会真禁言, 单字/通用词放在 _EUPHEMISMS 里只用于生图/输出, 不用于输入处罚,
#         避免 "我吃个包子" 这种无辜句子触发处罚。
_POLITICS_MANUAL = (
    # === 当代核心领导人 (姓名 + 较明确的称呼) ===
    "习近平", "习大大", "近平", "习总", "习主席",
    "李克强", "李强", "胡锦涛", "江泽民", "温家宝", "朱镕基", "李鹏",
    "栗战书", "韩正", "汪洋", "赵乐际", "王沪宁", "丁薛祥", "蔡奇",
    "彭丽媛", "习明泽",
    # === 历史最高领导人 ===
    "毛泽东", "毛主席",
    "邓小平",
    "刘少奇", "周恩来", "朱德", "陈云",
    "华国锋", "胡耀邦", "赵紫阳",
    # === 政治集团 / 历史团伙 ===
    "四人帮", "江青", "张春桥", "姚文元", "王洪文",
    "林彪", "彭德怀",
    # === 政党 / 体制 (较明确的政治表述) ===
    "中共", "共军",
    "中央政治局", "政治局常委", "中南海",
    "一党专政", "一党独裁",
    # === 民族 / 独立议题 ===
    "台独", "港独", "藏独", "疆独", "蒙独",
    "新疆集中营", "再教育营", "维吾尔", "维族",
    "西藏独立", "新疆独立", "台湾独立", "香港独立", "内蒙古独立",
    "图博特", "东突", "东突厥斯坦", "世维会",
    # === 历史敏感事件 ===
    "六四", "六四事件", "天安门事件", "天安门屠杀", "八九六四",
    "六四学生", "王丹", "吾尔开希", "柴玲", "陈光诚", "刘晓波",
    "文化大革命", "文革", "红卫兵", "大跃进", "大饥荒", "三年困难",
    "反右运动",
    "白纸运动", "四通桥", "彭立发", "彭载舟",
    # === 异见 / 宗教 / 被打压群体 ===
    "法轮功", "李洪志", "大法弟子", "全能神",
    "达赖喇嘛", "达赖", "班禅", "西藏流亡",
    "热比娅",
    "709大抓捕", "709律师",
    "高智晟", "胡佳", "艾未未", "许志永", "丁家喜",
    # === 国际敏感 ===
    "反华", "辱华", "支那", "反共", "反党", "反革命",
    # === 翻墙工具 / 信息管控 ===
    "翻墙", "翻墙 教程", "vpn 推荐", "shadowsocks", "clash 订阅", "v2ray",
    "trojan 节点", "ssr 节点",
    "防火长城", "GFW",
    # === 隐晦/讽刺表达 (较明确的政治隐喻, 单字食物词放 _EUPHEMISMS) ===
    "境外势力", "境外敌对势力", "颜色革命", "和平演变",
)

# 政治隐喻 / 食物代号 / 单字 — 命中即拒画/拒说, 但不进输入侧处罚
# 因为单独说 "包子" "腊肉" 很可能是食物, 不应触发禁言
# 用在: 生图 prompt 拦截 + 输出侧屏蔽
_EUPHEMISMS = (
    # 习近平相关
    "包子", "维尼", "小熊维尼", "包子主席",
    # 毛泽东相关
    "毛太祖", "毛腊肉", "腊肉",
    # 邓小平相关
    "邓矮子",
    # 通用最高领导人称呼
    "国师", "今上", "圣上",
    # 反讽口号
    "厉害了我的国", "强国梦",
)

# 手写补丁：色情兜底
_PORN_MANUAL = (
    "约炮", "做爱", "口交", "性交", "肛交", "援交", "鸡巴", "屌", "操逼", "操b",
    "黄片", "色情片", "av 番号", "av番号", "推荐 av", "推荐av",
    "嫖娼", "妓女", "卖淫", "强奸", "迷奸",
)

# 严重辱骂（指向 bot 或群友）—— 单纯 "草" "牛逼" 不算
# 外置词库不含这部分，本层完全靠手写
_INSULT_HARD = (
    "你妈死", "你妈的死", "操你妈", "操你全家", "草你妈", "去死",
    "傻逼", "煞笔", "傻b", "弱智 全家", "脑残 全家",
    "nmsl", "rnmlgb",
)

# 外置词库加载
_EXTERNAL_PATH = Path(__file__).parent / "data" / "sensitive_words.txt"


def _load_external() -> set[str]:
    if not _EXTERNAL_PATH.exists():
        logger.warning(f"blacklist 外置词库不存在: {_EXTERNAL_PATH}，仅用手写词")
        return set()
    out: set[str] = set()
    with open(_EXTERNAL_PATH, encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if w and not w.startswith("#"):
                out.add(w.lower())  # 统一小写，匹配时也走 lower
    return out


_EXTERNAL = _load_external()
_MANUAL_ALL = set(_POLITICS_MANUAL) | set(_PORN_MANUAL) | set(_INSULT_HARD)
_ALL_WORDS = _MANUAL_ALL | _EXTERNAL

# 拆英文/中文：英文走 ASCII 字母数字边界（避免 "anal" 误伤 "analyze"、"adult" 误伤 "adulthood"），中文走纯 in
# 注意：Python 的 \b 把 CJK 也算作 \w，所以 "分析anal说" 用 \b 会判定为词内（两端都贴 \w）→ 不命中
# 这里用 (?<![a-z0-9])...(?![a-z0-9]) 自定义边界：只挡 ASCII 字母数字，CJK/标点/空白都算边界
# 效果：
#   "anal alone"  → 命中
#   "analyze"     → 不命中（l 后是 y，y 是 [a-z0-9]）
#   "分析anal说"  → 命中（析/说不属于 [a-z0-9]）
_EN_PATTERNS = [
    re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", re.IGNORECASE)
    for w in _ALL_WORDS
    if all(ord(c) < 128 for c in w)
]
_CN_WORDS = [w for w in _ALL_WORDS if any(ord(c) >= 128 for c in w)]

logger.info(
    f"blacklist 加载完成: 手写 {len(_MANUAL_ALL)} + 外置 {len(_EXTERNAL)} = "
    f"去重后 {len(_ALL_WORDS)}（英文 {len(_EN_PATTERNS)} / 中文 {len(_CN_WORDS)}）"
)

# ============ 输出侧子集 ============
# is_blacklisted 用于输入(用户消息): 严格,挡全部 2321+ 词
# is_output_blacklisted 用于输出(AI 回复): 窄,只挡真正敏感的,放过通用政治词汇
# 起因:外置库含"共产党"等历史/教育语境常用词,五一劳动节这种问题会被误过滤
# 思路:bot 的输出已经过 RLHF, 只兜底当代政治人物 + 具体事件 + 民族议题 + 翻墙工具 + 色情/严重辱骂
# 输出侧子集: 同样大幅扩, 防 GPT 自己回答时漏说敏感人物/事件
_OUTPUT_POLITICS = (
    # 当代核心领导人
    "习近平", "习大大", "近平", "习总", "习主席", "包子", "维尼", "小熊维尼",
    "李克强", "李强", "胡锦涛", "江泽民", "温家宝", "朱镕基", "李鹏",
    "栗战书", "韩正", "汪洋", "赵乐际", "王沪宁", "丁薛祥", "蔡奇",
    "彭丽媛", "习明泽",
    # 历史最高领导人
    "毛泽东", "毛主席", "毛太祖", "毛腊肉", "腊肉",
    "邓小平", "小平", "邓矮子",
    "刘少奇", "周恩来", "朱德",
    "华国锋", "胡耀邦", "赵紫阳",
    # 政治集团 / 历史团伙
    "四人帮", "江青", "张春桥", "姚文元", "王洪文",
    "林彪",
    # 民族/独立议题
    "台独", "港独", "藏独", "疆独", "蒙独",
    "新疆集中营", "再教育营", "维吾尔", "图博特", "东突", "东突厥斯坦", "世维会",
    "西藏独立", "新疆独立", "台湾独立", "香港独立", "内蒙古独立",
    # 具体敏感事件
    "六四", "六四事件", "天安门事件", "天安门屠杀", "八九六四",
    "王丹", "吾尔开希", "柴玲", "陈光诚", "刘晓波",
    "文化大革命", "文革", "红卫兵", "大跃进", "大饥荒", "三年困难", "反右运动",
    "白纸运动", "四通桥", "彭立发", "彭载舟",
    # 异见/宗教
    "法轮功", "李洪志", "大法弟子", "全能神",
    "达赖喇嘛", "达赖", "班禅",
    "709大抓捕",
    "高智晟", "胡佳", "艾未未", "许志永", "丁家喜",
    # 翻墙工具
    "翻墙", "翻墙 教程", "vpn 推荐", "shadowsocks", "clash 订阅", "v2ray",
    "trojan 节点", "ssr 节点", "防火长城", "GFW",
    # 攻击性政治标签
    "反共", "反华", "辱华", "支那", "反党", "反革命",
    # 隐晦表达
    "境外势力", "颜色革命", "和平演变", "境外敌对势力",
    "国师", "今上", "圣上",
)
# 输出侧含隐喻 (bot 不能用"包子"调侃)
_OUTPUT_WORDS = set(_OUTPUT_POLITICS) | set(_EUPHEMISMS) | set(_PORN_MANUAL) | set(_INSULT_HARD)
_OUTPUT_EN_PATTERNS = [
    re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", re.IGNORECASE)
    for w in _OUTPUT_WORDS
    if all(ord(c) < 128 for c in w)
]
_OUTPUT_CN_WORDS = [w for w in _OUTPUT_WORDS if any(ord(c) >= 128 for c in w)]
logger.info(
    f"blacklist 输出侧子集: {len(_OUTPUT_WORDS)} 词"
    f"(中文 {len(_OUTPUT_CN_WORDS)} / 英文 {len(_OUTPUT_EN_PATTERNS)}); 较输入侧宽松"
)


def is_blacklisted(text: str) -> tuple[bool, str]:
    """
    返回 (命中, 命中的词)
    True → 上游应直接闭嘴
    """
    if not text:
        return False, ""
    t = text.lower()
    for w in _CN_WORDS:
        if w in t:
            return True, w
    for p in _EN_PATTERNS:
        m = p.search(t)
        if m:
            return True, m.group(0)
    return False, ""


# ============ 生图 prompt 专用检测 ============
# 给 image_gen / agent_tools.generate_image_in_chat 用
# 含全部输入侧词 + 隐喻 (画"包子" 时上下文是绘图意图, 食物歧义低, 应拦)
_IMAGE_WORDS = _ALL_WORDS | set(_EUPHEMISMS)
_IMAGE_CN_WORDS = [w for w in _IMAGE_WORDS if any(ord(c) >= 128 for c in w)]
_IMAGE_EN_PATTERNS = [
    re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", re.IGNORECASE)
    for w in _IMAGE_WORDS
    if all(ord(c) < 128 for c in w)
]


def is_image_prompt_blocked(text: str) -> tuple[bool, str]:
    """生图 prompt 检测; 比输入侧更宽, 含隐喻(包子/腊肉/维尼 等)。
    返回 (命中, 词); image2 拿到这些 prompt 会乖乖画, 我们必须本地拦。"""
    if not text:
        return False, ""
    t = text.lower()
    for w in _IMAGE_CN_WORDS:
        if w in t:
            return True, w
    for p in _IMAGE_EN_PATTERNS:
        m = p.search(t)
        if m:
            return True, m.group(0)
    return False, ""


def is_output_blacklisted(text: str) -> tuple[bool, str]:
    """
    输出侧检测: 比 is_blacklisted 宽松, 只挡核心敏感词。
    给 output_filter 用, 避免 bot 答历史题(如五一劳动节)被外置库的"共产党"误伤。
    """
    if not text:
        return False, ""
    t = text.lower()
    for w in _OUTPUT_CN_WORDS:
        if w in t:
            return True, w
    for p in _OUTPUT_EN_PATTERNS:
        m = p.search(t)
        if m:
            return True, m.group(0)
    return False, ""
