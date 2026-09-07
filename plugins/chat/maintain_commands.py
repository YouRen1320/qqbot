"""
AI 维修工 私聊命令注册 —— 只接 ADMIN_QQ。
命令:
  /诊断 [可选 hint]            → 只读, 给报告
  /修复 <自然语言描述>          → 出修复 plan, PM 给主人看 diff
  /确认                        → 应用上次 propose 的 plan → 重启
  /取消                        → 丢弃上次 propose
  /回滚                        → 还原最近一次 apply 的 .bak
  /巡逻                        → 立刻巡逻一次
  /巡逻开 /巡逻关 /巡逻状态     → 控制定时巡逻
  /cost-ai                     → 今日 AI 维修工 token 花了多少
  /diff                        → 看 pending plan 的 file 摘要

设计:
- 每个 admin 维护一个 pending_plan(内存, 重启丢失 — 没事, 没 confirm 不丢东西)
- 重启后, post_restart_check_if_pending 主动 PM 主人 "上线了" 或 "失败"(失败逻辑 Phase 2)
"""
import os
import json
import asyncio
import time
import difflib
from pathlib import Path
from typing import Optional

from nonebot import on_command, get_driver
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.log import logger

from . import self_maintain, log_ring, db

# 启动时立刻把日志 sink 挂到 loguru, 收 ring buffer
log_ring.install_sink()

ADMIN_QQ = os.getenv("ADMIN_QQ", "").strip()

# 上次诊断/修复的 pending state(per admin; 实际上只一个 admin)
_pending_plan: Optional[dict] = None
_pending_plan_hint: str = ""

# 巡逻开关 + 状态
# 没有维护模型凭据时不注册可用入口，也不让定时巡逻产生无效调用。
_patrol_enabled: bool = self_maintain._check_api_key()
_last_patrol_ts: float = 0.0
_last_patrol_issue: Optional[str] = None


def _is_admin(event: PrivateMessageEvent) -> bool:
    return (
        self_maintain._check_api_key()
        and ADMIN_QQ != ""
        and str(event.user_id) == ADMIN_QQ
    )


# ===== 注册命令 =====

diag_cmd = on_command("诊断", aliases={"diag", "diagnose"}, priority=4, block=True)
fix_cmd = on_command("修复", aliases={"fix"}, priority=4, block=True)
confirm_cmd = on_command("确认", aliases={"confirm", "go"}, priority=4, block=True)
cancel_cmd = on_command("取消", aliases={"cancel"}, priority=4, block=True)
rollback_cmd = on_command("回滚", aliases={"rollback"}, priority=4, block=True)
patrol_cmd = on_command("巡逻", aliases={"patrol"}, priority=4, block=True)
patrol_on_cmd = on_command("巡逻开", priority=4, block=True)
patrol_off_cmd = on_command("巡逻关", priority=4, block=True)
patrol_status_cmd = on_command("巡逻状态", priority=4, block=True)
cost_ai_cmd = on_command("cost-ai", aliases={"costai", "ai花费"}, priority=4, block=True)
diff_cmd = on_command("diff", priority=4, block=True)


def _fmt_usage(usage: dict) -> str:
    """格式化一次调用的成本"""
    inp = usage.get("input", 0)
    out = usage.get("output", 0)
    cache_r = usage.get("cache_read", 0)
    cost = usage.get("cost_usd", 0)
    return f"(token: in={inp} out={out} cache_r={cache_r}; ≈ ${cost:.3f})"


@diag_cmd.handle()
async def _(event: PrivateMessageEvent, arg: Message = CommandArg()):
    if not _is_admin(event):
        return
    hint = arg.extract_plain_text().strip()
    await diag_cmd.send(f"🔍 诊断中… (Opus 4.7 + extended thinking, 通常 20-40 秒)")
    text, usage = await self_maintain.diagnose(hint)
    if not text:
        err = usage.get("error", "未知错误")
        await diag_cmd.finish(f"❌ 诊断失败: {err}")
        return
    await diag_cmd.finish(f"📋 诊断报告\n\n{text}\n\n{_fmt_usage(usage)}")


@fix_cmd.handle()
async def _(event: PrivateMessageEvent, arg: Message = CommandArg()):
    if not _is_admin(event):
        return
    global _pending_plan, _pending_plan_hint
    hint = arg.extract_plain_text().strip()
    if not hint:
        await fix_cmd.finish("用法: /修复 <自然语言描述要修啥>\n例: /修复 入群欢迎概率太低,提到 100%")
        return
    await fix_cmd.send(f"🛠 生成修复方案中… (Opus 4.7 + extended thinking, 30-60 秒)")
    plan, usage = await self_maintain.propose_fix(hint)
    if not plan:
        err = usage.get("error", "Opus 拒绝或解析失败")
        await fix_cmd.finish(f"❌ 生成方案失败: {err}\n{_fmt_usage(usage)}")
        return
    ok, reason = self_maintain.validate_plan(plan)
    if not ok:
        await fix_cmd.finish(f"❌ 方案校验未通过: {reason}\n{_fmt_usage(usage)}")
        return
    _pending_plan = plan
    _pending_plan_hint = hint
    files = plan.get("files", [])
    summary = plan.get("summary", "(无 summary)")
    risk = plan.get("risk", "?")
    lines = [
        f"📦 方案就绪(等 /确认 或 /取消)",
        f"摘要: {summary}",
        f"风险: {risk}",
        f"改动 {len(files)} 个文件:",
    ]
    # 生成简短 diff stat per file
    for f in files:
        path = f.get("path", "?")
        new_content = f.get("new_content", "")
        reason_f = f.get("reason", "")[:80]
        # 算 +/- 行
        old_text = ""
        try:
            old_path = self_maintain.APP_ROOT / path.lstrip("/")
            if old_path.exists():
                old_text = old_path.read_text(encoding="utf-8")
        except Exception:
            pass
        added = sum(1 for _ in difflib.unified_diff(
            old_text.splitlines(), new_content.splitlines(), lineterm=""
        ) if _.startswith("+") and not _.startswith("+++"))
        removed = sum(1 for _ in difflib.unified_diff(
            old_text.splitlines(), new_content.splitlines(), lineterm=""
        ) if _.startswith("-") and not _.startswith("---"))
        lines.append(f"  - {path} (+{added} -{removed}) {reason_f}")
    lines.append("")
    lines.append("回 /diff 看具体 diff;回 /确认 应用 + 重启;/取消 丢弃")
    lines.append(_fmt_usage(usage))
    await fix_cmd.finish("\n".join(lines))


@confirm_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    global _pending_plan, _pending_plan_hint
    if _pending_plan is None:
        await confirm_cmd.finish("没有 pending 的方案。先 /修复 <描述> 生成一个。")
        return
    plan = _pending_plan
    hint = _pending_plan_hint
    _pending_plan = None
    _pending_plan_hint = ""
    ok, msg = self_maintain.apply_fix(plan, hint=hint)
    if not ok:
        await confirm_cmd.finish(f"❌ 应用失败: {msg}")
        return
    await confirm_cmd.send(f"✅ {msg}\n3 秒后 bot 会重启, 30-60 秒后我会私聊你确认上线情况")
    # 触发重启
    self_maintain.trigger_restart(delay_sec=3.0)


@cancel_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    global _pending_plan, _pending_plan_hint
    if _pending_plan is None:
        await cancel_cmd.finish("没有 pending 的方案, 不用取消。")
        return
    _pending_plan = None
    _pending_plan_hint = ""
    await cancel_cmd.finish("已丢弃 pending 方案。")


@diff_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    if _pending_plan is None:
        await diff_cmd.finish("没有 pending 的方案。")
        return
    parts = [f"📊 Pending plan diff (hint={_pending_plan_hint[:80]!r})"]
    for f in _pending_plan.get("files", []):
        path = f.get("path", "?")
        new_content = f.get("new_content", "")
        old_text = ""
        try:
            old_path = self_maintain.APP_ROOT / path.lstrip("/")
            if old_path.exists():
                old_text = old_path.read_text(encoding="utf-8")
        except Exception:
            pass
        diff_lines = list(difflib.unified_diff(
            old_text.splitlines(), new_content.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}",
            lineterm="", n=2,
        ))
        # 截最多 30 行避免 QQ 消息超长
        head = "\n".join(diff_lines[:30])
        tail = f"\n...(共 {len(diff_lines)} 行 diff, 截前 30)" if len(diff_lines) > 30 else ""
        parts.append(f"\n=== {path} ===\n{head}{tail}")
    msg_text = "\n".join(parts)
    # QQ 单条 5000 字符上限, 截一下
    if len(msg_text) > 4500:
        msg_text = msg_text[:4500] + "\n...(截断)"
    await diff_cmd.finish(msg_text)


@rollback_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    ok, msg = self_maintain.rollback_latest()
    if not ok:
        await rollback_cmd.finish(f"❌ 回滚失败: {msg}")
        return
    await rollback_cmd.send(f"✅ {msg}\n3 秒后重启")
    self_maintain.trigger_restart(delay_sec=3.0)


@patrol_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    global _last_patrol_ts, _last_patrol_issue
    await patrol_cmd.send("🔍 巡逻中…")
    issue, usage = await self_maintain.patrol()
    _last_patrol_ts = time.time()
    _last_patrol_issue = issue
    if usage.get("skipped"):
        await patrol_cmd.finish("✅ 一切正常 (没 ERROR, WARNING < 5, 跳过 LLM)")
        return
    if not issue:
        await patrol_cmd.finish(f"✅ 一切正常\n{_fmt_usage(usage)}")
        return
    await patrol_cmd.finish(f"⚠️ 巡逻发现异常\n\n{issue}\n\n{_fmt_usage(usage)}")


@patrol_on_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    global _patrol_enabled
    _patrol_enabled = True
    await patrol_on_cmd.finish("巡逻已开启 (每小时整点一次)")


@patrol_off_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    global _patrol_enabled
    _patrol_enabled = False
    await patrol_off_cmd.finish("巡逻已关闭")


@patrol_status_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    state = "开" if _patrol_enabled else "关"
    last = "从未巡逻" if _last_patrol_ts == 0 else time.strftime("%m-%d %H:%M", time.localtime(_last_patrol_ts))
    issue = _last_patrol_issue or "(上次无异常)"
    await patrol_status_cmd.finish(
        f"巡逻状态: {state}\n上次巡逻: {last}\n上次结果: {issue[:200]}"
    )


@cost_ai_cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_admin(event):
        return
    rows = await db.maintain_cost_today()
    if not rows:
        await cost_ai_cmd.finish("今天还没花 AI 维修工 token")
        return
    lines = ["今日 AI 维修工花费:"]
    total = 0.0
    for r in rows:
        lines.append(f"  {r['kind']}: {r['calls']} 次, ${r['cost']:.3f}")
        total += r["cost"]
    lines.append(f"合计: ${total:.3f}")
    await cost_ai_cmd.finish("\n".join(lines))


# ===== 巡逻定时任务(scheduler 调) =====

async def patrol_tick(bot: Bot) -> None:
    """scheduler 每小时调一次"""
    global _last_patrol_ts, _last_patrol_issue
    if not _patrol_enabled or not ADMIN_QQ:
        return
    try:
        issue, usage = await self_maintain.patrol()
    except Exception as e:
        logger.warning(f"patrol_tick 异常: {e}")
        return
    _last_patrol_ts = time.time()
    _last_patrol_issue = issue
    if issue:
        try:
            await bot.send_private_msg(
                user_id=int(ADMIN_QQ),
                message=f"🚨 巡逻发现异常\n\n{issue}\n\n要诊断: /诊断 / 要修: /修复 <描述>"
            )
        except Exception as e:
            logger.warning(f"巡逻告警 PM 失败: {e}")


# ===== 启动后 post-restart 检查(__init__.py 在 on_bot_connect 调) =====

async def notify_post_restart(bot: Bot) -> None:
    """如有 maintain pending flag, 发"上线了"PM 给主人"""
    if not ADMIN_QQ:
        return
    try:
        msg = await self_maintain.post_restart_check_if_pending(bot)
    except Exception as e:
        logger.warning(f"post_restart_check 异常: {e}")
        return
    if msg:
        try:
            await bot.send_private_msg(user_id=int(ADMIN_QQ), message=msg)
        except Exception as e:
            logger.warning(f"post-restart PM 失败: {e}")


# ===== 启动钩子: bot 连上 → 检查 pending flag =====
_driver = get_driver()
_post_restart_done = {"v": False}


@_driver.on_bot_connect
async def _on_connect(bot: Bot):
    # 健康哨兵: 每次 connect 都 touch 一次, watchdog 看这个文件 mtime 判活
    # 比 grep docker logs 更可靠(cron 环境下 docker logs --since 时区/race 不稳)
    try:
        Path("/app/data/.bot_alive").touch(exist_ok=True)
    except Exception as e:
        logger.warning(f"bot_alive touch 失败: {e}")
    # 幂等, 防多次 connect 时反复发 post-restart PM
    if _post_restart_done["v"]:
        return
    _post_restart_done["v"] = True
    # 延迟几秒等 chat 插件起完
    await asyncio.sleep(3)
    await notify_post_restart(bot)
