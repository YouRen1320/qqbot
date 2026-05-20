"""
群成员变动 + 邀请/好友请求处理 —— 防被恶意拉群
- group_increase: 新人入群 → 概率欢迎一句
- group_decrease: 有人退群 → 仅日志；bot 自己被踢 → 告警
- FriendRequest: 默认拒绝（除非来自 ADMIN_QQ）
- GroupRequest: 邀请进群默认拒绝（除非邀请人=ADMIN_QQ 或群在白名单）
"""
import os
import random
import asyncio

from nonebot import on_notice, on_request
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
    FriendRequestEvent,
    GroupRequestEvent,
)
from nonebot.log import logger

from .safety import is_group_enabled, can_reply, ENABLED_GROUPS

ADMIN_QQ = os.getenv("ADMIN_QQ", "").strip()

WELCOME_PROB = 1.0  # 每次入群都欢迎; 之前 0.4 太低体感差

_WELCOME_TEMPLATES = (
    "新人来了 群规第一条 别 v 我 50",
    "欢迎啊 看起来你也是来摸鱼的",
    "新人？群里都是答辩 自己保重",
    "哥们 / 姐们 来都来了 简单自我介绍一下",
    "新进来的 别紧张 我们也不咋说话",
    "欢迎欢迎 进群先看群公告",
    "新人 来了就别走了",
    "又一个 群规自己看",
    "欢迎欢迎 群里随便逛",
    "新来的 来都来了发个红包啊",
    "哎哟来新人了 介绍一下自己呗",
    "欢迎 你来对地方了 这里水群专业户",
)

increase_matcher = on_notice(priority=15, block=False)
decrease_matcher = on_notice(priority=15, block=False)
friend_req_matcher = on_request(priority=5, block=True)
group_req_matcher = on_request(priority=5, block=True)


@increase_matcher.handle()
async def _on_increase(bot: Bot, event: GroupIncreaseNoticeEvent):
    gid = event.group_id
    if not is_group_enabled(gid):
        return
    if event.user_id == event.self_id:
        logger.info(f"bot 自己加入群 {gid}")
        return
    if random.random() > WELCOME_PROB:
        return
    ok, reason = can_reply(gid)
    if not ok:
        logger.info(f"入群欢迎被风控跳过 group={gid}: {reason}")
        return
    text = random.choice(_WELCOME_TEMPLATES)
    await asyncio.sleep(random.uniform(0.8, 1.8))
    try:
        await bot.send_group_msg(group_id=gid, message=text)
        logger.info(f"入群欢迎 group={gid} user={event.user_id}")
    except Exception as e:
        logger.warning(f"入群欢迎发送失败: {e}")


@decrease_matcher.handle()
async def _on_decrease(bot: Bot, event: GroupDecreaseNoticeEvent):
    gid = event.group_id
    if event.user_id == event.self_id:
        logger.warning(f"⚠️ bot 自己离开了群 {gid}（被踢或主动退）")
        return
    if not is_group_enabled(gid):
        return
    logger.info(f"群 {gid} 有人退群 user={event.user_id} (不吐槽，仅记录)")


@friend_req_matcher.handle()
async def _on_friend_req(bot: Bot, event: FriendRequestEvent):
    """陌生好友请求一律拒绝；ADMIN_QQ 通过"""
    uid = str(event.user_id)
    if ADMIN_QQ and uid == ADMIN_QQ:
        try:
            await event.approve(bot)
            logger.info(f"好友请求自动同意 admin={uid}")
        except Exception as e:
            logger.warning(f"同意好友请求失败: {e}")
        return
    try:
        await event.reject(bot)
        logger.info(f"好友请求自动拒绝 user={uid}")
    except Exception as e:
        logger.warning(f"拒绝好友请求失败: {e}")


@group_req_matcher.handle()
async def _on_group_req(bot: Bot, event: GroupRequestEvent):
    """
    群请求两类：
    - sub_type=invite: 别人邀请 bot 进群 → 默认拒绝，邀请人=ADMIN_QQ 才同意
    - sub_type=add: 有人申请加白名单内的群 → 不处理（让群主自己审）
    """
    if event.sub_type == "invite":
        inviter = str(event.user_id)
        if ADMIN_QQ and inviter == ADMIN_QQ:
            try:
                await event.approve(bot)
                logger.info(f"群邀请自动同意 admin 邀请 gid={event.group_id}")
            except Exception as e:
                logger.warning(f"同意群邀请失败: {e}")
            return
        try:
            await event.reject(bot, reason="bot 谢绝陌生邀请")
            logger.info(f"群邀请拒绝 inviter={inviter} gid={event.group_id}")
        except Exception as e:
            logger.warning(f"拒绝群邀请失败: {e}")
        return
    # add：有人申请加 bot 所在群，留给群主处理
    logger.info(f"群加入申请未处理 gid={event.group_id} user={event.user_id}")
