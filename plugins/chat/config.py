"""
Bot 名称配置 —— 改名只需改 .env，不用翻代码
- BOT_NAME: 主名称，用于备忘录卡片、状态消息、工具描述、日志等
- BOT_ALIASES: 别名列表，用于触发关键词和 @ 检测
"""
import os

# 主名称
BOT_NAME = os.getenv("BOT_NAME", "墨七七")

# 别名（逗号分隔），群友 @ 任何一个都能触发
_raw = os.getenv("BOT_ALIASES", "七七,miku")
BOT_ALIASES: list[str] = [a.strip() for a in _raw.split(",") if a.strip()]
