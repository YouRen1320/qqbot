"""
AI 维修工 —— 让 bot 自己看日志、改代码、安全重启。
入口:
- diagnose(hint): 只读 — 看 logs + 相关代码, Opus 出诊断报告 str
- propose_fix(hint): 看 logs + 代码 → Opus 出修复 plan dict
- apply_fix(plan): 备份原文件 → 写新内容 → git commit → 触发 docker 自动重启
- rollback(): 还原最近一次备份 + git revert
- patrol(): 巡逻一次, 返回异常 issue 列表

安全栏(代码层, GPT 改不动):
- FORBIDDEN_PATHS: .env / data/ / scripts/ / docker / Dockerfile 等永远不许改
- 所有改动必有 .bak 备份, 落 maintain_log 表
- 重启用 sys.exit(0) 触发 docker restart:always, 不调 docker socket
- 启动时检查 .maintain_pending, 60s 内 NapCat 没回 → 自动 rollback

成本:全 Opus 4.7(可走 1M context), 开 extended thinking, 用 prompt caching
"""
import os
import sys
import json
import time
import shutil
import asyncio
from pathlib import Path, PurePosixPath
from typing import Optional

from nonebot.log import logger

from . import db, log_ring
from .config import BOT_NAME

# ===== 配置 =====
# ANTHROPIC_API_KEY: Anthropic 官方 key 或兼容中转站 key
# ANTHROPIC_BASE_URL: 留空 = 走 anthropic 官方 api.anthropic.com; 填中转站地址走中转
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "").strip()
MODEL_OPUS = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")
THINKING_BUDGET = int(os.getenv("MAINTAIN_THINKING_BUDGET", "8000"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAINTAIN_MAX_TOKENS", "16000"))

# ===== 路径 =====
APP_ROOT = Path("/app")                           # 容器内根
PLUGIN_DIR = APP_ROOT / "plugins" / "chat"        # 业务代码目录(host-mount, 改了直接影响 disk)
BACKUP_DIR = APP_ROOT / "data" / "maintain_backups"
PENDING_FLAG = APP_ROOT / "data" / ".maintain_pending"        # bot 重启后 PM 主人用
APPLY_LOG_FLAG = APP_ROOT / "data" / ".maintain_apply_log"    # host-side watchdog 用 (git commit / 回滚判定)
GIT_WORKDIR = APP_ROOT  # 注: 容器内 .git 不可达; git 操作走 host 侧 watchdog

# ===== 文件白名单 / 黑名单 =====
# 白:plugin 业务代码 + JSON 配置
ALLOWED_FILE_PATTERNS = (
    "plugins/chat/*.py",
    "plugins/chat/data/*.json",
)
# 黑:绝对不能改的(secret / 数据 / 部署元信息)
FORBIDDEN_PATH_SUBSTRINGS = (
    ".env",
    "secret",
    "__pycache__",
    "scripts/backup.sh",
    "scripts/safe_restart.sh",
    "docker-compose",
    "Dockerfile",
    "requirements.txt",                  # 改 deps 要重建镜像, 别让 AI 自己改
    "plugins/chat/data/sensitive_words.txt",
    "plugins/chat/self_maintain.py",     # 别让 AI 改维修工自己
    "plugins/chat/maintain_commands.py",
)

# ===== 系统 prompt(给 Opus 看) =====
SYSTEM_PROMPT = """你是一个 senior Python 工程师, 维护一个 NoneBot QQ 群聊机器人。

【项目概况】
- 框架: NoneBot 2 + OneBot v11 + NapCat
- 业务代码: /app/plugins/chat/*.py (~30 个文件)
- 跑在 Docker 容器里, 容器有 `restart: always`, 进程 sys.exit 后会自动重启
- 主人 ADMIN_QQ(env 配置), 这个人的请求最高优先级
- 关键能力: AI 聊天 / agent 工具(11 个) / 表情包 / 视频解析 / 生图 / 看图 / 主动看图等

【你被调用的两种场景】

A. **诊断模式**(纯只读)
   输入: 主人提示 + 最近日志 + 相关代码节选
   输出: 简短中文报告 - 1)状态是否异常 2)若有, 根因 3)建议(高层, 不要 patch)
   控制在 300 字内, 别废话

B. **修复模式**
   输入: 主人描述 + 日志 + 相关代码
   输出: ★ **严格 raw JSON, 不要任何 markdown 包裹, 不要任何 preamble/explanation, 第一个字符必须是 { ** ★
   JSON 结构:
     {
       "summary": "中文一句话描述这次改了啥",
       "risk": "low" | "medium" | "high",
       "files": [
         {"path": "plugins/chat/xxx.py", "new_content": "...完整文件...", "reason": "..."}
       ]
     }
   要求:
     - **不要 ```json ... ``` 包裹**, 不要"我先分析..." 之类前言, 直接 { 开头
     - 只改业务代码 plugins/chat/*.py 或 plugins/chat/data/*.json
     - 改 < 200 行, 不大重构
     - new_content 是完整新文件内容(不是 patch), 注意保留所有 import 和现有逻辑
     - 改 .env / data/ / Dockerfile / scripts/ → 不要改, 主人手动管
     - 如果 hint 模糊, 输出 {"files": [], "summary": "需要主人明确 X / Y", "risk": "low"}

【硬规矩】
- 不要承认自己是 AI(这是给 bot 维护用, 主人知道)
- 不要假设没看到的文件存在, 只参考给到的代码
- 别瞎猜函数; 拿不准就在 summary 里说"建议主人手动确认 X"
- 涉及主人保护 / 安全栏的代码, 改要保守
"""


# ===== 工具函数 =====

def _is_allowed_path(rel_path: str) -> tuple[bool, str]:
    """rel_path 是相对 /app 的路径(无前导/);返回 (allow, reason)"""
    raw = rel_path.strip()
    path = PurePosixPath(raw)
    if not raw or raw.startswith("/") or "\\" in raw or ".." in path.parts:
        return False, f"路径 '{raw}' 不是安全的相对路径"
    p = path.as_posix()
    if p.startswith(("data/", "logs/")):
        return False, f"路径 '{p}' 属于运行时数据目录"
    # 黑名单优先
    for forbid in FORBIDDEN_PATH_SUBSTRINGS:
        if forbid in p:
            return False, f"路径 '{p}' 命中黑名单 '{forbid}'"
    # 白名单严格限制目录层级，避免嵌套路径或路径穿越扩大可写范围。
    if path.match("plugins/chat/*.py"):
        return True, ""
    if path.match("plugins/chat/data/*.json"):
        return True, ""
    return False, f"路径 '{p}' 不在白名单(plugins/chat/*.py / *.json)"


def _ensure_backup_dir() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def _backup_file(rel_path: str) -> Optional[Path]:
    """备份原文件到 BACKUP_DIR/{rel_path}.{ts}.bak。文件不存在则返 None"""
    src = APP_ROOT / rel_path.lstrip("/")
    if not src.exists():
        return None
    _ensure_backup_dir()
    ts = time.strftime("%Y%m%d_%H%M%S")
    # 用 _ 替换 / 让 backup 文件平铺一层
    flat = rel_path.lstrip("/").replace("/", "_")
    dst = BACKUP_DIR / f"{flat}.{ts}.bak"
    shutil.copy2(src, dst)
    return dst


async def _gather_runtime_context() -> str:
    """
    SQLite 拉运行时数据给 Opus看:
      - 最近 50 条聊天记录 (history)
      - 最近 20 条 agent 工具调用 (admin_log)
      - 最近 10 条 self_maintain 历史 (maintain_log)
    """
    chunks = []
    try:
        chats = await db.history_recent(limit=50)
        if chats:
            lines = []
            for c in chats:
                role = BOT_NAME if c["role"] == "assistant" else f"群友(uid={c['user_id']})"
                lines.append(f"{c['ts_str']} [群{c['group_id']}] {role}: {c['content']}")
            chunks.append("=== 最近 50 条群聊记录 ===\n" + "\n".join(lines))
    except Exception as e:
        chunks.append(f"=== 聊天记录读取失败: {e} ===")
    try:
        admins = await db.admin_log_recent(limit=20)
        if admins:
            lines = []
            for a in admins:
                flag = "✓" if a["success"] else "✗"
                lines.append(f"{a['ts_str']} {flag} {a['tool']} args={a['args'][:80]} msg={a['msg'][:40]}")
            chunks.append("=== 最近 20 次 agent 工具调用 (admin_log) ===\n" + "\n".join(lines))
    except Exception as e:
        chunks.append(f"=== admin_log 读取失败: {e} ===")
    try:
        mains = await db.maintain_log_recent(limit=10)
        if mains:
            lines = []
            for m in mains:
                lines.append(
                    f"{m['ts_str']} {m['kind']}({m['verdict']}) ${m['cost_usd']:.2f} "
                    f"hint={m['hint'][:50]} → {m['details'][:80]}"
                )
            chunks.append("=== 最近 10 次维修工历史 (maintain_log) ===\n" + "\n".join(lines))
    except Exception as e:
        chunks.append(f"=== maintain_log 读取失败: {e} ===")
    return "\n\n".join(chunks)


def _parse_time_window(hint: str) -> tuple[float | None, float | None]:
    """
    从 hint 解析时间窗, 返回 (start_ts, end_ts) Unix 秒;
    都 None = 没指定时间窗(全量)
    支持模式:
      "最近 5 分钟" / "最近 10min" / "最近 1 小时"
      "22:00-22:30" / "22:00 到 22:30"
      "今天 22:00 起"
    """
    import re
    from datetime import datetime, timedelta
    now = time.time()
    now_dt = datetime.now()
    h = hint.replace(" ", "").lower()
    # 最近 N 分钟/小时
    m = re.search(r"最近(\d+)(分钟|min|小时|hour|h)", h)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        sec = n * 60 if unit in ("分钟", "min") else n * 3600
        return (now - sec, now)
    # HH:MM-HH:MM
    m = re.search(r"(\d{1,2}):(\d{2})[-到~](\d{1,2}):(\d{2})", h)
    if m:
        h1, m1, h2, m2 = map(int, m.groups())
        start = now_dt.replace(hour=h1, minute=m1, second=0, microsecond=0)
        end = now_dt.replace(hour=h2, minute=m2, second=59, microsecond=0)
        # 如果 end 在未来(用户问昨天 22:00-22:30), 减一天
        if end.timestamp() > now:
            start = start - timedelta(days=1)
            end = end - timedelta(days=1)
        return (start.timestamp(), end.timestamp())
    return (None, None)


def _filter_logs_by_window(logs: list[str], start_ts: float | None, end_ts: float | None) -> list[str]:
    """按时间窗过滤 log_ring 输出。loguru 日志开头是 HH:MM:SS"""
    if start_ts is None and end_ts is None:
        return logs
    import re
    from datetime import datetime
    today = datetime.now().date()
    filtered = []
    for line in logs:
        m = re.match(r"^(\d{2}):(\d{2}):(\d{2})", line)
        if not m:
            filtered.append(line)  # 解析不出来时保留, 不过激删
            continue
        h, mn, s = map(int, m.groups())
        # 拼成今天的时间戳(粗;跨天问题罕见)
        dt = datetime.combine(today, datetime.min.time()).replace(hour=h, minute=mn, second=s)
        ts = dt.timestamp()
        if start_ts and ts < start_ts:
            continue
        if end_ts and ts > end_ts:
            continue
        filtered.append(line)
    return filtered


def _gather_code_context(focus_hint: str = "") -> str:
    """
    给 Opus 看的代码全 dump — plugins/chat 下所有 *.py 文件, ~150KB 字符。
    1M context 撑得起, 而且 prompt caching 让重复调用便宜(同样 system 内容 5 分钟内 1.5/M token)。
    skip:
      - 自己 (self_maintain.py / maintain_commands.py) — 防 Opus 循环改维修工
      - __pycache__ / .pyc / 太大单文件 > 60KB(只有可能的极端情况)
    """
    chunks = []
    files = sorted(PLUGIN_DIR.glob("*.py"))
    skipped_self = {"self_maintain.py", "maintain_commands.py"}
    for p in files:
        if p.name in skipped_self:
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception as e:
            chunks.append(f"=== plugins/chat/{p.name} 读取失败: {e} ===")
            continue
        if len(content) > 60000:
            content = content[:60000] + f"\n... (truncated, full size {len(content)})"
        chunks.append(f"=== plugins/chat/{p.name} ({len(content)} 字符) ===\n{content}")
    # 顺手也读 plugins/chat/data 下的 json (gun_codes 等)
    data_dir = PLUGIN_DIR / "data"
    if data_dir.exists():
        for p in sorted(data_dir.glob("*.json")):
            try:
                content = p.read_text(encoding="utf-8")
                if len(content) > 20000:
                    content = content[:20000] + f"\n... (truncated, full size {len(content)})"
                chunks.append(f"=== plugins/chat/data/{p.name} ===\n{content}")
            except Exception:
                pass
    return "\n\n".join(chunks)


def _check_api_key() -> bool:
    return bool(ANTHROPIC_API_KEY)


# ===== Anthropic 调用封装 =====

async def _call_opus(
    user_content: str,
    *,
    use_thinking: bool = True,
    purpose: str = "diagnose",
) -> tuple[Optional[str], dict]:
    """
    调 Opus 4.7, 返回 (文本内容, usage dict {input/output/cache_read/cache_creation/cost_estimate})。
    失败返 (None, {error: ...})
    """
    if not _check_api_key():
        return None, {"error": "ANTHROPIC_API_KEY 未配置"}
    try:
        # 延迟 import, 没装时不会爆
        from anthropic import AsyncAnthropic
        from anthropic.types import TextBlock
    except ImportError:
        return None, {"error": "anthropic SDK 未安装"}
    client_kwargs = {"api_key": ANTHROPIC_API_KEY}
    if ANTHROPIC_BASE_URL:
        client_kwargs["base_url"] = ANTHROPIC_BASE_URL
    client = AsyncAnthropic(**client_kwargs)
    kwargs = {
        "model": MODEL_OPUS,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user_content}],
    }
    if use_thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
    try:
        resp = await client.messages.create(**kwargs)
    except Exception as e:
        logger.warning(f"self_maintain Anthropic 调用失败 purpose={purpose}: {type(e).__name__}: {e}")
        return None, {"error": str(e)[:300]}
    # 抽 text(skip thinking block)
    text = ""
    for block in resp.content:
        if hasattr(block, "type") and block.type == "text":
            text += block.text
        elif type(block).__name__ == "TextBlock":
            text += getattr(block, "text", "")
    # usage
    u = resp.usage
    usage = {
        "input": getattr(u, "input_tokens", 0),
        "output": getattr(u, "output_tokens", 0),
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
    # 粗估成本 (Opus 4.7: input $15/M, output $75/M, cache_read $1.5/M, cache_creation $18.75/M)
    cost = (
        usage["input"] / 1e6 * 15
        + usage["output"] / 1e6 * 75
        + usage["cache_read"] / 1e6 * 1.5
        + usage["cache_creation"] / 1e6 * 18.75
    )
    usage["cost_usd"] = round(cost, 4)
    return text, usage


# ===== 公开入口:诊断 =====

async def diagnose(hint: str = "") -> tuple[Optional[str], dict]:
    """诊断模式 — 只读, 出报告 str。返回 (report, usage)"""
    logs = log_ring.recent(800)
    error_lines = log_ring.recent(200, level_filter="ERROR")
    warn_lines = log_ring.recent(200, level_filter="WARNING")
    # 时间窗过滤: hint 含 "最近 10 分钟" / "22:00-22:30" 时只送窗内日志
    start_ts, end_ts = _parse_time_window(hint)
    if start_ts or end_ts:
        logs = _filter_logs_by_window(logs, start_ts, end_ts)
        error_lines = _filter_logs_by_window(error_lines, start_ts, end_ts)
        warn_lines = _filter_logs_by_window(warn_lines, start_ts, end_ts)
        from datetime import datetime
        window_desc = (
            f"\n[时间窗] "
            + (f"{datetime.fromtimestamp(start_ts):%H:%M:%S} " if start_ts else "起始不限 ")
            + "→ "
            + (f"{datetime.fromtimestamp(end_ts):%H:%M:%S}" if end_ts else "末尾不限")
            + "\n"
        )
    else:
        window_desc = ""
    code_context = _gather_code_context(hint)
    runtime_context = await _gather_runtime_context()

    user_content = (
        ("[主人提示]\n" + hint + window_desc + "\n\n" if hint.strip() else window_desc)
        + "[最近 ERROR 级别日志]\n"
        + ("\n".join(error_lines) if error_lines else "(空, 没有 ERROR)")
        + "\n\n[最近 WARNING 级别日志]\n"
        + ("\n".join(warn_lines[-50:]) if warn_lines else "(空)")
        + "\n\n[最近 INFO 全量日志]\n"
        + "\n".join(logs)[-30000:]   # 截断防爆 token
        + "\n\n[运行时上下文 — 聊天记录 / 工具调用 / 维修历史]\n"
        + runtime_context[:50000]
        + "\n\n[相关代码]\n"
        + code_context[:200000]      # 1M context 撑得起 ~200K 字符
        + "\n\n请按 [诊断模式] 输出报告。"
    )
    text, usage = await _call_opus(user_content, use_thinking=True, purpose="diagnose")
    # 落审计
    asyncio.create_task(
        db.maintain_log(
            kind="diagnose",
            hint=hint[:200],
            verdict="ok" if text else "fail",
            cost_usd=usage.get("cost_usd", 0),
            details=text[:2000] if text else usage.get("error", "")[:200],
        )
    )
    return text, usage


def _robust_parse_plan(raw: str) -> Optional[dict]:
    """
    把 Opus 的 raw response 解析成 plan dict;允许:
    - 前后有 ``` 代码块包裹 (```json ...```)
    - 前面有 "我先分析..." 之类 preamble
    - 末尾有 trailing 文字
    - 末尾 trailing comma (用 json5-like 兜底)
    思路: 找第一个 { 和最后一个 } 之间的内容,反复尝试。
    """
    import re
    text = raw.strip()

    # 1) 直接 json.loads 试
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2) 去除 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        candidate = m.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 3) 找第一个 { 到最后一个 } 的子串
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass
        # 4) 容忍 trailing comma  —— 用正则去掉 },} ],] 之前的逗号
        cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
        try:
            return json.loads(cleaned)
        except Exception:
            pass

    return None


# ===== 公开入口:提修复方案 =====

async def propose_fix(hint: str) -> tuple[Optional[dict], dict]:
    """生成 plan dict {summary, risk, files}。返回 (plan, usage)"""
    logs = log_ring.recent(800)
    code_context = _gather_code_context(hint)
    runtime_context = await _gather_runtime_context()

    user_content = (
        f"[主人请求]\n{hint}\n\n"
        + "[最近日志]\n"
        + "\n".join(logs)[-25000:]
        + "\n\n[运行时上下文 — 看历史避免重复改 / 看聊天定位 bug]\n"
        + runtime_context[:30000]
        + "\n\n[相关代码 — 你的修改基于这些文件的当前内容]\n"
        + code_context[:200000]
        + "\n\n请按 [修复模式] 输出 JSON。"
        + "记住:JSON 是 raw response 的全部内容, 不要包 markdown 代码块, 直接以 { 开头。"
        + "new_content 是文件**完整**新内容, 不是 patch。"
    )
    text, usage = await _call_opus(user_content, use_thinking=True, purpose="propose")
    if not text:
        asyncio.create_task(
            db.maintain_log(
                kind="propose_fix",
                hint=hint[:200],
                verdict="fail",
                cost_usd=usage.get("cost_usd", 0),
                details=usage.get("error", "")[:200],
            )
        )
        return None, usage
    plan = _robust_parse_plan(text)
    if plan is None:
        logger.warning(f"propose_fix JSON 解析失败; raw head={text[:300]!r}")
        asyncio.create_task(
            db.maintain_log(
                kind="propose_fix", hint=hint[:200], verdict="parse_fail",
                cost_usd=usage.get("cost_usd", 0),
                details=f"raw head: {text[:300]}",
            )
        )
        return None, usage
    asyncio.create_task(
        db.maintain_log(
            kind="propose_fix",
            hint=hint[:200],
            verdict="ok",
            cost_usd=usage.get("cost_usd", 0),
            details=json.dumps({"summary": plan.get("summary"), "risk": plan.get("risk"),
                               "n_files": len(plan.get("files", []))}, ensure_ascii=False)[:500],
        )
    )
    return plan, usage


# ===== 公开入口:应用 plan =====

def validate_plan(plan: dict) -> tuple[bool, str]:
    """plan sanity check; 返回 (ok, reason)"""
    if not isinstance(plan, dict):
        return False, "plan 不是 dict"
    files = plan.get("files")
    if not isinstance(files, list):
        return False, "plan.files 不是 list"
    if not files:
        return False, "plan.files 空 - 没东西改"
    if len(files) > 8:
        return False, f"一次改 {len(files)} 个文件太多, 拒"
    total_chars = 0
    seen_paths: set[str] = set()
    for f in files:
        path = (f.get("path") or "").strip()
        new_content = f.get("new_content", "")
        if not path or not isinstance(new_content, str):
            return False, "file 缺少 path 或 new_content"
        ok, reason = _is_allowed_path(path)
        if not ok:
            return False, reason
        normalized_path = PurePosixPath(path).as_posix()
        if normalized_path in seen_paths:
            return False, f"{normalized_path} 在 plan 中重复出现"
        seen_paths.add(normalized_path)
        try:
            (APP_ROOT / normalized_path).resolve().relative_to(APP_ROOT.resolve())
        except ValueError:
            return False, f"{normalized_path} 解析后越出应用目录"
        total_chars += len(new_content)
        if len(new_content) > 80000:
            return False, f"{path} 内容 > 80k 字符, 拒"
        # 简单 Python 语法 check
        if path.endswith(".py"):
            try:
                import ast
                ast.parse(new_content)
            except SyntaxError as e:
                return False, f"{path} 语法错误: {e}"
    return True, ""


def _write_apply_manifest(meta: dict) -> Path:
    """保存可长期追踪的精确变更清单，供人工回滚读取。"""
    _ensure_backup_dir()
    manifest = BACKUP_DIR / f"apply-{time.time_ns()}.json"
    manifest.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return manifest


def apply_fix(plan: dict, hint: str = "") -> tuple[bool, str]:
    """
    应用 plan: 备份 → 写文件 → 落 maintain_log。
    返回 (success, message)。
    成功后由 maintain_commands 触发 trigger_restart()。
    """
    ok, reason = validate_plan(plan)
    if not ok:
        return False, f"plan 校验失败: {reason}"
    changes: list[dict] = []
    try:
        for fitem in plan["files"]:
            rel = fitem["path"].lstrip("/")
            full = APP_ROOT / rel
            existed = full.exists()
            bak = _backup_file(rel)
            change = {
                "path": rel,
                "existed": existed,
                "backup": str(bak.relative_to(BACKUP_DIR)) if bak else None,
            }
            changes.append(change)
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(fitem["new_content"], encoding="utf-8")
        # 双 flag 设计:
        # - PENDING_FLAG: bot 重启后读 → PM 主人"上线了"→ bot 自己删
        # - APPLY_LOG_FLAG: host-side watchdog 读 → git commit / 90s 无回应自动 revert
        # 两个 flag 内容相同, 但 lifecycle 不同, 避免 race condition
        meta = json.dumps({
            "ts": time.time(),
            "hint": hint,
            "files": [change["path"] for change in changes],
            # 保留旧字段供尚未更新的宿主机脚本读取；精确回滚以 changes 为准。
            "backups": [change["backup"] for change in changes if change["backup"]],
            "changes": changes,
            "summary": plan.get("summary", ""),
            "risk": plan.get("risk", ""),
        }, ensure_ascii=False)
        PENDING_FLAG.parent.mkdir(parents=True, exist_ok=True)
        manifest = _write_apply_manifest(json.loads(meta))
        meta_dict = json.loads(meta)
        meta_dict["manifest"] = str(manifest.relative_to(BACKUP_DIR))
        meta = json.dumps(meta_dict, ensure_ascii=False)
        PENDING_FLAG.write_text(meta)
        APPLY_LOG_FLAG.write_text(meta)
    except Exception as e:
        logger.exception("apply_fix 写文件失败")
        # 尝试回滚已写的
        _rollback_changes(changes)
        return False, f"写入失败: {type(e).__name__}: {e}; 已尝试回滚"
    asyncio.create_task(
        db.maintain_log(
            kind="apply_fix",
            hint=hint[:200],
            verdict="written",
            cost_usd=0,
            details=json.dumps({"files": [c["path"] for c in changes], "summary": plan.get("summary")},
                               ensure_ascii=False)[:800],
        )
    )
    return True, f"已写入 {len(changes)} 个文件, 立即重启"


def _rollback_changes(changes: list[dict]) -> list[str]:
    """按精确清单还原旧文件，并删除本次新建的文件。"""
    restored: list[str] = []
    for change in reversed(changes):
        rel = str(change.get("path") or "").lstrip("/")
        if not rel:
            continue
        target = APP_ROOT / rel
        backup = change.get("backup")
        if change.get("existed") and backup:
            bak_full = BACKUP_DIR / str(backup)
            if bak_full.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bak_full, target)
                restored.append(rel)
        elif not change.get("existed") and target.exists():
            target.unlink()
            restored.append(rel)
    return restored


def rollback_latest() -> tuple[bool, str]:
    """
    /回滚 命令调用:按最近一次 apply 的精确清单还原。
    为避免误回滚历史版本，只接受 5 分钟内创建的清单。
    """
    if not BACKUP_DIR.exists():
        return False, "没有备份目录, 没法回滚"
    manifests = sorted(
        BACKUP_DIR.glob("apply-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not manifests:
        return False, "没有可用的应用清单；旧版备份请 SSH 登录后手动恢复"
    if manifests[0].stat().st_mtime < time.time() - 300:
        return False, "5 分钟内没有可回滚的应用清单；更早版本请 SSH 登录后手动恢复"
    try:
        info = json.loads(manifests[0].read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"应用清单读取失败: {type(e).__name__}: {e}"
    changes = info.get("changes")
    if not isinstance(changes, list) or not changes:
        return False, "应用清单缺少精确变更记录"
    restored = _rollback_changes(changes)
    if not restored:
        return False, "清单中的备份不存在，未还原任何文件"
    asyncio.create_task(
        db.maintain_log(
            kind="rollback",
            hint="",
            verdict="restored" if restored else "nothing",
            cost_usd=0,
            details=f"restored={restored}",
        )
    )
    return True, f"已还原 {len(restored)} 个文件: {restored}"


# ===== 重启 trigger =====

def trigger_restart(delay_sec: float = 3.0) -> None:
    """
    延迟 delay_sec 后 sys.exit(0), 让 docker `restart: always` 自动拉起新进程。
    用 asyncio.create_task 提交, 不阻塞当前调用。
    """
    async def _exit_later():
        await asyncio.sleep(delay_sec)
        logger.warning(f"self_maintain 主动 sys.exit, docker 会自动重启")
        os._exit(0)  # 注意:用 _exit 而非 exit, 避免 atexit 阻塞
    asyncio.create_task(_exit_later())


# ===== 启动后 health-check(maintain_commands 在 on_bot_connect 调) =====

async def post_restart_check_if_pending(bot) -> Optional[str]:
    """
    如果存在 PENDING_FLAG, 说明上次 apply_fix 后重启上来了。
    检查: 1) 加载成功(本函数走到了就 OK) 2) NapCat connected(参数传入说明已连)
    成功 → 删 flag + 返回 "上线了" 消息
    失败(本函数未在 60s 内被调) → 由其他机制处理(暂留给 Phase 2)
    """
    if not PENDING_FLAG.exists():
        return None
    try:
        info = json.loads(PENDING_FLAG.read_text())
    except Exception:
        info = {}
    PENDING_FLAG.unlink()
    asyncio.create_task(
        db.maintain_log(
            kind="post_restart_ok",
            hint=info.get("hint", "")[:200],
            verdict="online",
            cost_usd=0,
            details=json.dumps({"files": info.get("files"), "summary": info.get("summary")},
                               ensure_ascii=False)[:500],
        )
    )
    files_brief = ", ".join((info.get("files") or [])[:4])
    summary = info.get("summary", "")
    return (
        f"✅ 修复已上线\n"
        f"改动: {files_brief}\n"
        f"摘要: {summary[:200]}\n"
        f"可用 /回滚 撤销, 或 /diff 看具体改了啥"
    )


# ===== 巡逻 =====

async def patrol() -> tuple[Optional[str], dict]:
    """
    巡逻一次, 看是否有异常。返回 (issue_report or None, usage)。
    None = 一切正常无需打扰。
    """
    logs = log_ring.recent(500)
    errors = log_ring.recent(200, level_filter="ERROR")
    warns = log_ring.recent(200, level_filter="WARNING")

    # 启发式快速过滤: 没 ERROR 且 WARNING 少 → 直接返 None 省钱
    if len(errors) == 0 and len(warns) < 5:
        return None, {"input": 0, "output": 0, "cost_usd": 0.0, "skipped": True}

    user_content = (
        "[巡逻请求] 检查 bot 最近 30 分钟是否有异常需要主人关注。\n"
        "无明显异常 → 输出严格字符串 'OK',不要其他文字;\n"
        "有异常 → 输出 ≤200 字简报: 异常类型 / 频率 / 建议主人是否要 /诊断 或 /修复\n\n"
        + "[最近 ERROR]\n" + ("\n".join(errors) if errors else "(空)")
        + "\n\n[最近 WARNING(节选)]\n" + ("\n".join(warns[-30:]) if warns else "(空)")
        + "\n\n[最近 INFO(节选)]\n" + "\n".join(logs)[-10000:]
    )
    text, usage = await _call_opus(user_content, use_thinking=False, purpose="patrol")
    if not text or text.strip().upper().startswith("OK"):
        asyncio.create_task(
            db.maintain_log(
                kind="patrol", hint="", verdict="ok",
                cost_usd=usage.get("cost_usd", 0), details="OK",
            )
        )
        return None, usage
    asyncio.create_task(
        db.maintain_log(
            kind="patrol", hint="", verdict="issue",
            cost_usd=usage.get("cost_usd", 0), details=text[:1000],
        )
    )
    return text, usage
