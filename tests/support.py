"""测试启动辅助：只初始化 NoneBot，不启动驱动或网络连接。"""

import nonebot


def ensure_nonebot_initialized() -> None:
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
