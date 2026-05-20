"""
定时任务 —— 三件事
- 周一摸鱼 / 周四 v50 / 周五下班（既有梗）
- 当日节日彩蛋（10:00 抛文案）
- 每分钟扫一遍冷场（>30 分钟没人说话则抛梗，每天每群上限 1 次）
- 每天 04:00 跑一次 SQLite trim，每群保留 100 条
所有发送都走 can_reply 风控
"""
import asyncio
import random
from datetime import datetime, date

import nonebot
from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger

from .safety import is_group_enabled, can_reply, ENABLED_GROUPS
from . import idle, holiday, db, recall, maintain_commands

# (weekday, hour, minute, slot_name) → 候选文案
_SLOTS: list[tuple[int, int, int, str, list[str]]] = [
    (0, 9, 30, "monday-morning", [
        "新一周开局摸鱼。。",
        "周一了 装病请假合理吗",
        "建议把周一从日历里删了",
    ]),
    (3, 11, 30, "thursday-v50", [
        "v我50 老规矩",
        "今天疯狂星期四 谁请我吃 v 我 50",
        "kfc 你打钱了吗",
    ]),
    (4, 18, 0, "friday-off", [
        "下班 别拦我",
        "周末快乐 别催我",
        "今天的我是自由的",
    ]),
]

# 节日固定 10:00 抛
_HOLIDAY_HOUR, _HOLIDAY_MINUTE = 10, 0

# 已触发过的 (group_id, date_str, slot_name)；每天清一次
_fired: set[tuple[int, str, str]] = set()
_last_clean_date: str = ""
_last_trim_date: str = ""


# 容忍迟到：到点后这么多分钟内仍可补发；过了就当今天错过
# 起因：loop 每 60s 跑一次，AI/网络抖动 + 重启时机能让某次 tick 飘过整分钟，
# 原来恰好命中 hh:mm 的写法会让该 slot 当天彻底哑火。
_LATE_TOLERANCE_MIN = 5


def _matches(now: datetime, wd: int, hh: int, mm: int) -> bool:
    if now.weekday() != wd:
        return False
    cur = now.hour * 60 + now.minute
    sched = hh * 60 + mm
    return 0 <= cur - sched <= _LATE_TOLERANCE_MIN


async def _send_to_group(bot: Bot, group_id: int, text: str) -> bool:
    try:
        sent = await bot.send_group_msg(group_id=group_id, message=text)
        # 拿到 message_id 给撤回保护用
        mid = sent.get("message_id") if isinstance(sent, dict) else None
        if mid:
            recall.remember_sent(group_id, mid)
        return True
    except Exception as e:
        logger.warning(f"定时梗发送失败 group={group_id}: {e}")
        return False


async def _tick_slots(bot: Bot, now: datetime, today: str) -> None:
    for wd, hh, mm, slot, texts in _SLOTS:
        if not _matches(now, wd, hh, mm):
            continue
        for gid_str in ENABLED_GROUPS:
            try:
                gid = int(gid_str)
            except ValueError:
                continue
            key = (gid, today, slot)
            if key in _fired:
                continue
            ok, reason = can_reply(gid)
            if not ok:
                logger.info(f"定时梗 {slot} 群 {gid} 被风控跳过: {reason}")
                _fired.add(key)
                continue
            text = random.choice(texts)
            await _send_to_group(bot, gid, text)
            _fired.add(key)
            logger.info(f"定时梗 {slot} 已发到群 {gid}: {text}")


async def _tick_holiday(bot: Bot, now: datetime, today: str) -> None:
    cur = now.hour * 60 + now.minute
    sched = _HOLIDAY_HOUR * 60 + _HOLIDAY_MINUTE
    if not (0 <= cur - sched <= _LATE_TOLERANCE_MIN):
        return
    text = holiday.text_for_today()
    if not text:
        return
    for gid_str in ENABLED_GROUPS:
        try:
            gid = int(gid_str)
        except ValueError:
            continue
        key = (gid, today, "holiday")
        if key in _fired:
            continue
        ok, reason = can_reply(gid)
        if not ok:
            logger.info(f"节日彩蛋 群 {gid} 被风控跳过: {reason}")
            _fired.add(key)
            continue
        await _send_to_group(bot, gid, text)
        _fired.add(key)
        logger.info(f"节日彩蛋 已发到群 {gid}: {text}")


async def _tick_idle(bot: Bot) -> None:
    """扫每个群，>30 分钟没人说话则抛梗"""
    for gid_str in ENABLED_GROUPS:
        try:
            gid = int(gid_str)
        except ValueError:
            continue
        text = idle.peek_break_silence(gid)
        if not text:
            continue
        ok, reason = can_reply(gid)
        if not ok:
            logger.info(f"冷场抛梗 群 {gid} 被风控跳过: {reason}（状态未消耗，下个 tick 仍可重试）")
            continue
        idle.commit_break_silence(gid)
        await _send_to_group(bot, gid, text)
        logger.info(f"冷场抛梗 群 {gid}: {text}")


async def _tick_trim(today: str) -> None:
    """每天 04:00 跑一次 DB trim"""
    global _last_trim_date
    if _last_trim_date == today:
        return
    now = datetime.now()
    if now.hour != 4:
        return
    _last_trim_date = today
    for gid_str in ENABLED_GROUPS:
        try:
            gid = int(gid_str)
        except ValueError:
            continue
        await db.trim(gid)
    await db.trim_usage_log()
    await db.trim_admin_log()
    logger.info("SQLite trim 已执行（history + usage_log + admin_log）")


_last_patrol_hour: int = -1


async def _tick_patrol(bot: Bot, now: datetime) -> None:
    """每整点跑一次 AI 维修工巡逻; 由 maintain_commands.patrol_tick 决定要不要 PM 主人"""
    global _last_patrol_hour
    cur_hour = now.hour
    if cur_hour == _last_patrol_hour:
        return
    # 启动后第一轮不立刻巡逻(等 logs 攒一会儿); 至少跑过 5 分钟
    import time as _time
    if _time.time() - _start_ts < 300:
        return
    _last_patrol_hour = cur_hour
    try:
        await maintain_commands.patrol_tick(bot)
    except Exception as e:
        logger.warning(f"巡逻 tick 异常: {e}")


_start_ts = 0.0


async def _loop() -> None:
    global _last_clean_date, _start_ts
    import time as _time
    _start_ts = _time.time()
    await asyncio.sleep(5)  # 让 NoneBot 完全就绪
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if today != _last_clean_date:
                _fired.clear()
                _last_clean_date = today

            bots = list(nonebot.get_bots().values())
            if not bots:
                await asyncio.sleep(60)
                continue
            bot = bots[0]
            if not isinstance(bot, Bot):
                await asyncio.sleep(60)
                continue

            await _tick_slots(bot, now, today)
            await _tick_holiday(bot, now, today)
            await _tick_idle(bot)
            await _tick_trim(today)
            await _tick_patrol(bot, now)
        except Exception as e:
            logger.warning(f"定时 loop 异常: {e}")
        await asyncio.sleep(60)


def install() -> None:
    """挂到 driver.on_bot_connect，bot 连上才启动 loop"""
    driver = nonebot.get_driver()
    started = {"v": False}

    @driver.on_bot_connect
    async def _start(bot):
        if started["v"]:
            return
        started["v"] = True
        asyncio.create_task(_loop())
        logger.info("定时 loop 已启动")
