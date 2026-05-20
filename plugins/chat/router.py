"""
模型路由 —— 决定这次回复用 flash 还是 pro
- 默认 flash（最快、最省 token）
- 命中"复杂问题"特征 → 升 pro，让回答更靠谱
"""
import re

# 技术 / 严肃问题的特征关键词（命中即升 pro）
_PRO_KEYWORDS = (
    "为什么", "怎么实现", "原理", "区别", "如何", "怎么办", "推荐", "建议",
    "对比", "优缺点", "源码", "底层", "算法", "复杂度", "设计模式",
    "react", "vue", "typescript", "webpack", "vite", "node", "v8",
    "事件循环", "闭包", "原型链", "promise", "async", "diff", "虚拟dom",
    "面试", "八股", "项目", "上线", "性能", "优化",
    # 用户显式要求"详细讲"时，上 pro：模型更愿意展开 + 输出更稳
    "详细", "展开", "多讲", "细说", "深入", "讲清楚", "好好讲", "好好说",
    # 写代码 / 语言 / 脚本 类请求：模型要给可跑的代码，flash 容易糊弄
    "python", "java", "golang", "rust", "kotlin", "swift", "php",
    "代码", "脚本", "写一段", "写一个", "给我一段", "给段", "给我写",
    "sql", "正则", "regex", "shell", "bash", "yaml", "json schema",
    # 知识 / 解释类：当用户问"含义/意思/解释"时，倾向认真答而不是嘴硬一句
    "含义", "意思", "解释", "分别是", "都有什么", "有哪些", "区分",
    # 旅游 / 攻略 / 推荐场景：内容密度高，需要 pro 给结构化答案
    "攻略", "旅游", "路线", "行程",
)

# 简单的代码片段判定（含 ; { } => function class const）
_CODE_PATTERN = re.compile(r"[{};]|=>|\bfunction\b|\bclass\b|\bconst \b|\blet \b|\bvar \b")


def pick_model(text: str, at_me: bool) -> str | None:
    """
    返回:
    - None: 用默认 flash
    - "pro": 用 pro 模型（升级版）
    判定规则:
    - 不被 @ 时永远用 flash（路过插话不值得花 pro 的钱和延迟）
    - 被 @ 且文本明显是"问问题": 升 pro
    """
    if not at_me:
        return None

    t = text.lower().strip()
    # 太短的 @（如 "你好" "在吗"）继续用 flash
    if len(t) < 8:
        return None
    # 命中技术 / 严肃问题关键词
    if any(kw in t for kw in _PRO_KEYWORDS):
        return "pro"
    # 含明显代码痕迹
    if _CODE_PATTERN.search(text):
        return "pro"
    # 末尾是问号且长度足够 —— 像在认真问问题
    if t.endswith(("?", "？")) and len(t) >= 12:
        return "pro"
    return None
