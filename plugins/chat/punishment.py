"""
违规升级处罚 —— 用户消息命中政治敏感词时触发
- 1 次: 禁言 10 分钟 + 撤回该消息 + 群里发警告
- 2 次: 禁言 30 分钟 + 撤回 + 警告
- 3+ 次: 踢出群 + 拒绝再加群申请 + 撤回 + 警告

设计:
- 计数滚动窗口 24h, 跨天衰减(不秋后算账)
- 主人(ADMIN_QQ) / 群主 / 群管理员 全部豁免, bot 自己也豁免
- 计数 + 操作日志落 SQLite, 重启不丢
- 失败时不抛, 仅 warning; 安全栏宁愿放过也不要 bot 自己炸群
"""
import os
import time
import asyncio
from collections import defaultdict, deque

from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger

from . import db

ADMIN_QQ = int(os.getenv("ADMIN_QQ", "0") or 0)

# 计数滚动窗口 (秒)
WINDOW_SEC = 24 * 3600

# 每次违规对应的动作
MUTE_1ST_SEC = 600    # 10 min
MUTE_2ND_SEC = 1800   # 30 min
# 3+ 次直接 kick

# 内存计数: (group_id, user_id) -> deque[timestamps]
# 重启会丢, 但 db 里有持久化记录可补; 内存是热路径快速判断
_offense_history: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))


def _count_recent(gid: int, uid: int) -> int:
    """该用户 24h 内的违规次数; 顺手清过期项"""
    dq = _offense_history[(gid, uid)]
    cutoff = time.time() - WINDOW_SEC
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq)


async def _is_exempt(bot: Bot, group_id: int, user_id: int, self_qq: int) -> bool:
    """主人 / 群主 / 群管理员 / bot 自己 — 都豁免"""
    if ADMIN_QQ and user_id == ADMIN_QQ:
        return True
    if user_id == self_qq:
        return True
    try:
        info = await bot.get_group_member_info(
            group_id=group_id, user_id=user_id, no_cache=False
        )
        role = (info or {}).get("role", "member")
        return role in ("owner", "admin")
    except Exception as e:
        logger.warning(f"punishment: 查群员角色失败 gid={group_id} uid={user_id}: {e}")
        # 查不到信息 → 保守判定为不豁免 (按普通用户处罚)
        # 因为这场景下查询失败通常意味着已退群 / 网络抖, 不应放过违规
        return False


async def punish_violation(
    bot: Bot,
    group_id: int,
    user_id: int,
    nick: str,
    self_qq: int,
    message_id: int,
    hit_word: str,
) -> None:
    """
    用户消息命中黑名单 → 执行升级处罚
    传入 message_id 用于撤回; hit_word 用于日志和警告文案
    """
    # 1) 豁免检查
    if await _is_exempt(bot, group_id, user_id, self_qq):
        logger.info(
            f"punishment: gid={group_id} uid={user_id} 豁免 (主人/群主/管理员/bot), "
            f"命中 {hit_word!r} 但不处罚"
        )
        return

    # 2) 计数 + 升级
    _offense_history[(group_id, user_id)].append(time.time())
    n = _count_recent(group_id, user_id)
    logger.warning(
        f"punishment: gid={group_id} uid={user_id} ({nick}) 第 {n} 次违规, "
        f"命中 {hit_word!r}"
    )

    # 3) 撤回该消息 (无论几次都撤; bot 是群主, 有权撤别人)
    try:
        await bot.call_api("delete_msg", message_id=message_id)
        logger.info(f"punishment: 撤回违规消息 mid={message_id}")
    except Exception as e:
        logger.warning(f"punishment: 撤回 mid={message_id} 失败: {e}")

    # 4) 按次数升级动作
    if n == 1:
        action = "禁言 10 分钟"
        try:
            await bot.set_group_ban(
                group_id=group_id, user_id=user_id, duration=MUTE_1ST_SEC
            )
        except Exception as e:
            logger.warning(f"punishment: 禁言 1st 失败: {e}")
            action = "禁言失败"
        warn = f"⚠️ 请勿发布政治敏感内容。已{action}, 24h 内再犯加重处罚, 累计 3 次踢出群。"
    elif n == 2:
        action = "禁言 30 分钟"
        try:
            await bot.set_group_ban(
                group_id=group_id, user_id=user_id, duration=MUTE_2ND_SEC
            )
        except Exception as e:
            logger.warning(f"punishment: 禁言 2nd 失败: {e}")
            action = "禁言失败"
        warn = f"⚠️ 第 2 次违规, 已{action}。再犯将被踢出群且拒绝再加入。"
    else:
        action = "踢出群 + 加群申请拉黑"
        try:
            await bot.set_group_kick(
                group_id=group_id, user_id=user_id, reject_add_request=True
            )
        except Exception as e:
            logger.warning(f"punishment: 踢人失败: {e}")
            action = "踢人失败"
        warn = f"⚠️ 第 {n} 次违规, 已{action}。"

    # 5) 群里发警告 (让其他人也看到, 起威慑作用)
    try:
        await bot.send_group_msg(group_id=group_id, message=warn)
    except Exception as e:
        logger.warning(f"punishment: 警告消息发送失败: {e}")

    # 6) 落 admin_log
    asyncio.create_task(
        db.admin_log(
            group_id,
            self_qq,
            "punish_violation",
            {
                "target": user_id,
                "nick": nick[:40],
                "hit": hit_word[:40],
                "offense_count": n,
                "action": action,
            },
            action.endswith("失败") is False,
            "" if not action.endswith("失败") else action,
        )
    )
