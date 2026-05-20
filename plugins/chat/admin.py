"""
私聊管理命令 —— 只接受 ADMIN_QQ 私聊
- /status: 启动时间、心情、生效群、今日发言数、上限
- /pause <gid>: 临时禁言某群
- /resume <gid>: 恢复某群
- /set daily 200: 改每日上限
- /set perminute 5: 改每分钟上限
- /say <gid> <text>: 让 bot 在指定群说话
- 通过包内相对引用 chat 的其它模块，避免跨插件 import
"""
import os
import time
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent, Message
from nonebot.params import CommandArg

from . import safety
from . import mood
from . import db
from . import ai_client
from .config import BOT_NAME

ADMIN_QQ = os.getenv("ADMIN_QQ", "").strip()


def _is_admin(event: PrivateMessageEvent) -> bool:
    return ADMIN_QQ != "" and str(event.user_id) == ADMIN_QQ


status_cmd = on_command("status", priority=5, block=True)
pause_cmd = on_command("pause", priority=5, block=True)
resume_cmd = on_command("resume", priority=5, block=True)
set_cmd = on_command("set", priority=5, block=True)
say_cmd = on_command("say", priority=5, block=True)
cost_cmd = on_command("cost", priority=5, block=True)
agent_log_cmd = on_command("agent_log", aliases={"alog", "actions"}, priority=5, block=True)


@status_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    # 延迟引 START_TIME，避免循环
    from .startup import START_TIME
    uptime = int(time.time() - START_TIME)
    h, rem = divmod(uptime, 3600)
    m = rem // 60
    counts = safety.get_daily_counts()
    daily_lines = [f"  {gid}: {n}" for gid, n in counts.items()] or ["  (无)"]
    breaker = ai_client.breaker_status()
    breaker_line = (
        f"\n软审核熔断中: " + ", ".join(f"{gid}({n})" for gid, n in sorted(breaker.items()))
        if breaker else ""
    )
    msg = (
        f"{BOT_NAME} 运行中\n"
        f"启动: {h}h{m}m 前\n"
        f"心情: {mood.today_mood()}\n"
        f"已启用群: {sorted(safety.ENABLED_GROUPS) or '(空)'}\n"
        f"暂停中: {sorted(safety.get_paused()) or '(无)'}"
        f"{breaker_line}\n"
        f"每日上限: {safety.DAILY_LIMIT_PER_GROUP}\n"
        f"每分钟上限: {safety.PER_MINUTE_LIMIT}\n"
        f"今日已发:\n" + "\n".join(daily_lines)
    )
    await status_cmd.finish(msg)


@pause_cmd.handle()
async def _(event: PrivateMessageEvent, arg: Message = CommandArg()):
    if not _is_admin(event):
        return
    raw = arg.extract_plain_text().strip()
    if not raw.isdigit():
        await pause_cmd.finish("用法: /pause 群号")
    gid = int(raw)
    safety.pause_group(gid)
    await pause_cmd.finish(f"已暂停群 {gid}")


@resume_cmd.handle()
async def _(event: PrivateMessageEvent, arg: Message = CommandArg()):
    if not _is_admin(event):
        return
    raw = arg.extract_plain_text().strip()
    if not raw.isdigit():
        await resume_cmd.finish("用法: /resume 群号")
    gid = int(raw)
    safety.resume_group(gid)
    await resume_cmd.finish(f"已恢复群 {gid}")


@set_cmd.handle()
async def _(event: PrivateMessageEvent, arg: Message = CommandArg()):
    if not _is_admin(event):
        return
    parts = arg.extract_plain_text().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await set_cmd.finish("用法: /set daily 200 | /set perminute 5")
    key, val = parts[0], int(parts[1])
    if key == "daily":
        safety.DAILY_LIMIT_PER_GROUP = val
        await set_cmd.finish(f"DAILY_LIMIT_PER_GROUP={val}")
    elif key == "perminute":
        safety.PER_MINUTE_LIMIT = val
        await set_cmd.finish(f"PER_MINUTE_LIMIT={val}")
    else:
        await set_cmd.finish("未知 key（daily / perminute）")


@say_cmd.handle()
async def _(bot: Bot, event: PrivateMessageEvent, arg: Message = CommandArg()):
    if not _is_admin(event):
        return
    raw = arg.extract_plain_text().strip()
    sp = raw.split(maxsplit=1)
    if len(sp) != 2 or not sp[0].isdigit():
        await say_cmd.finish("用法: /say 群号 内容")
    gid, text = int(sp[0]), sp[1]
    try:
        await bot.send_group_msg(group_id=gid, message=text)
        await say_cmd.finish(f"已发到 {gid}")
    except Exception as e:
        await say_cmd.finish(f"发送失败: {e}")


@agent_log_cmd.handle()
async def _(event: PrivateMessageEvent, arg: Message = CommandArg()):
    """查最近的 agent 工具操作: /agent_log [tool_name] [n=20]
    例: /agent_log         → 最近 20 条所有工具操作
        /agent_log mute_user → 只看禁言记录
        /agent_log mute_user 50 → 最近 50 条禁言记录
    """
    if not _is_admin(event):
        return
    parts = arg.extract_plain_text().split()
    tool_filter: str | None = None
    limit = 20
    for p in parts:
        if p.isdigit():
            limit = max(1, min(int(p), 200))
        else:
            tool_filter = p
    rows = await db.admin_log_recent(limit=limit, tool=tool_filter)
    if not rows:
        await agent_log_cmd.finish(
            f"暂无{tool_filter or 'agent'}操作记录"
        )
        return
    lines = [f"最近 {len(rows)} 条 agent 操作" + (f" (tool={tool_filter})" if tool_filter else "") + ":"]
    for r in rows:
        flag = "✓" if r["success"] else "✗"
        # 显示时间 + tool + 关键 args + 结果消息(失败时)
        args_brief = []
        try:
            import json as _json
            a = _json.loads(r["args"]) if r["args"] else {}
            for k in ("target", "title", "card", "message_id", "duration"):
                if k in a:
                    args_brief.append(f"{k}={a[k]}")
        except Exception:
            pass
        args_str = " ".join(args_brief) or r["args"][:60]
        line = f"  {flag} {r['ts_str']} {r['tool']} {args_str}"
        if not r["success"] and r["msg"]:
            line += f" → {r['msg'][:60]}"
        lines.append(line)
    await agent_log_cmd.finish("\n".join(lines))


@cost_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    rows = await db.usage_today()
    if not rows:
        await cost_cmd.finish("今日还没烧 token")
        return
    lines = ["今日 token 用量:"]
    total_prompt = total_completion = total_calls = 0
    for r in rows:
        lines.append(
            f"  群{r['group_id']} {r['profile']}/{r['model']}: "
            f"{r['calls']}次 prompt={r['prompt']} comp={r['completion']}"
        )
        total_prompt += r["prompt"]
        total_completion += r["completion"]
        total_calls += r["calls"]
    lines.append(
        f"合计: {total_calls}次 prompt={total_prompt} comp={total_completion} "
        f"total={total_prompt + total_completion}"
    )
    await cost_cmd.finish("\n".join(lines))
