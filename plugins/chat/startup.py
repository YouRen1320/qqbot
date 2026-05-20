"""
启动期健康检查 —— bot 连上后做几件事
- 拉群列表，对比 ENABLED_GROUPS：不在群里的告警；意外多出的群也告警
- 初始化 SQLite，加载历史
- 记录启动时间（用于 /status）
- 把启动信息打到日志
"""
import time
import nonebot
from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger

from .safety import ENABLED_GROUPS
from . import db

# 启动时间 —— 给 /status 用
START_TIME = time.time()


def install() -> None:
    driver = nonebot.get_driver()
    started = {"v": False}

    @driver.on_bot_connect
    async def _on_connect(bot):
        if started["v"]:
            return
        started["v"] = True
        if not isinstance(bot, Bot):
            return

        # 1) 初始化 SQLite + 回灌历史
        try:
            await db.init()
            await db.warm_load_into_memory()
            logger.info("SQLite 历史已回灌内存")
        except Exception as e:
            logger.warning(f"SQLite 初始化失败（继续运行）: {e}")

        # 2) 检查群成员关系
        # 三集合：wanted（配置白名单）∩ joined（实际在的）= effective（生效中）
        # missing = wanted - joined：配置了但 bot 不在 → 告警（白名单失效）
        # unauthorized = joined - wanted：bot 在但没配置 → 告警（可能被人乱拉群 / 安全信号）
        try:
            group_list = await bot.get_group_list()
            joined = {str(g["group_id"]) for g in group_list}
            wanted = set(ENABLED_GROUPS)
            effective = joined & wanted
            missing = wanted - joined
            unauthorized = joined - wanted
            if missing:
                logger.warning(f"⚠️ 配置在白名单但 bot 不在的群: {sorted(missing)}")
            if unauthorized:
                logger.warning(
                    f"⚠️ bot 在但未列入白名单的群（如非预期，建议手动退群或加白）: "
                    f"{sorted(unauthorized)}"
                )
            logger.info(f"白名单生效群: {sorted(effective)}（共 {len(effective)} 个）")
        except Exception as e:
            logger.warning(f"拉群列表失败: {e}")
