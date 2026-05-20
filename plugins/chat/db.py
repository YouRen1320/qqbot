"""
SQLite 持久化 + 长记忆检索 —— 落盘历史 + FTS5 全文召回
- 用 aiosqlite，异步、轻
- 仅保留最近 N 条/群（warm load 时拉回内存）
- 表设计：history(group_id, user_id, role, content, ts)
  - user_id=0 表示老数据没记录的（兼容首次迁移）
- FTS5 虚拟表 history_fts 跟 history 同步（trigram 分词，中文友好）
  - search_relevant() 按 query 召回 top-K 跨用户历史 → 作为"长记忆"喂给 AI
- 路径：/app/data/qqbot.db（docker volume mount）
"""
import os
import time
import aiosqlite
from nonebot.log import logger

from . import state

DB_PATH = os.getenv("QQBOT_DB_PATH", "/app/data/qqbot.db")
WARM_LOAD_LIMIT_PER_USER = 20  # 每 (群,用户) 启动时回灌多少条
TRIM_KEEP = 200                # 每群总共最多保留多少条
LONG_MEM_TOP_K = 3             # FTS 召回的"长记忆"条数
LONG_MEM_EXCLUDE_RECENT_SEC = 60.0  # 排除最近 N 秒内的命中（避免召回刚说的）
USAGE_LOG_KEEP_DAYS = 90       # token 用量日志保留天数

_db: aiosqlite.Connection | None = None


async def init() -> None:
    global _db
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _db = await aiosqlite.connect(DB_PATH)
    await _db.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL
        )
        """
    )
    # 旧库无 user_id 列时补一下
    cursor = await _db.execute("PRAGMA table_info(history)")
    cols = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    if "user_id" not in cols:
        await _db.execute("ALTER TABLE history ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
        logger.info("history 表已升级，新增 user_id 列（旧数据置 0）")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_hist_gid_ts ON history(group_id, ts)")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_hist_gid_uid_ts ON history(group_id, user_id, ts)")

    # agent 管理操作日志(谁/什么时候/调了什么工具/参数/成功失败)
    # 用于事后审计 + 排查 GPT 抽风行为(比如乱禁言)
    await _db.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            actor_qq INTEGER NOT NULL DEFAULT 0,
            tool TEXT NOT NULL,
            args TEXT NOT NULL DEFAULT '{}',
            success INTEGER NOT NULL DEFAULT 0,
            msg TEXT NOT NULL DEFAULT '',
            ts REAL NOT NULL
        )
        """
    )
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_admin_log_gid_ts ON admin_log(group_id, ts)")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_admin_log_tool_ts ON admin_log(tool, ts)")

    # AI 维修工操作日志(诊断 / 修复 / 巡逻 / 回滚)
    await _db.execute(
        """
        CREATE TABLE IF NOT EXISTS maintain_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            hint TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL DEFAULT '',
            cost_usd REAL NOT NULL DEFAULT 0,
            details TEXT NOT NULL DEFAULT '',
            ts REAL NOT NULL
        )
        """
    )
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_maintain_log_ts ON maintain_log(ts)")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_maintain_log_kind ON maintain_log(kind, ts)")

    # token 用量日志（用于 admin /cost 查每日花销）
    await _db.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL DEFAULT 0,
            profile TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            ts REAL NOT NULL
        )
        """
    )
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts)")
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_usage_gid_ts ON usage_log(group_id, ts)")

    # FTS5 全文索引（trigram 分词器对中文友好；SQLite 3.34+ 支持）
    # external content 模式：FTS 表的 rowid 跟 history.id 对齐，省一份内容副本
    await _db.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
            content,
            content='history',
            content_rowid='id',
            tokenize='trigram'
        )
        """
    )
    # 同步触发器：history 增删改 → 自动维护 FTS
    await _db.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS history_ai_fts AFTER INSERT ON history BEGIN
            INSERT INTO history_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS history_ad_fts AFTER DELETE ON history BEGIN
            INSERT INTO history_fts(history_fts, rowid, content) VALUES('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS history_au_fts AFTER UPDATE ON history BEGIN
            INSERT INTO history_fts(history_fts, rowid, content) VALUES('delete', old.id, old.content);
            INSERT INTO history_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )
    # 一次性把已有的 history 灌进 FTS（首次升级 / 万一触发器漏了）
    # 'rebuild' 命令幂等：如果 FTS 已是最新就快速返回
    try:
        await _db.execute("INSERT INTO history_fts(history_fts) VALUES('rebuild')")
    except Exception as e:
        logger.warning(f"FTS rebuild 失败（可忽略）: {e}")

    await _db.commit()
    logger.info(f"SQLite + FTS5 初始化完成 path={DB_PATH}")


async def append(group_id: int, user_id: int, role: str, content: str, ts: float) -> None:
    if _db is None:
        return
    try:
        await _db.execute(
            "INSERT INTO history(group_id, user_id, role, content, ts) VALUES (?, ?, ?, ?, ?)",
            (group_id, user_id, role, content, ts),
        )
        await _db.commit()
    except Exception as e:
        logger.warning(f"history append 失败: {e}")


async def trim(group_id: int, keep: int = TRIM_KEEP) -> None:
    """每群只保留最近 keep 条（按时间），跨用户合并算"""
    if _db is None:
        return
    try:
        await _db.execute(
            """
            DELETE FROM history
            WHERE group_id = ? AND id NOT IN (
                SELECT id FROM history WHERE group_id = ? ORDER BY ts DESC LIMIT ?
            )
            """,
            (group_id, group_id, keep),
        )
        await _db.commit()
    except Exception as e:
        logger.warning(f"history trim 失败: {e}")


async def search_relevant(
    group_id: int,
    query: str,
    limit: int = LONG_MEM_TOP_K,
    older_than_seconds: float = LONG_MEM_EXCLUDE_RECENT_SEC,
) -> list[dict]:
    """
    FTS5 跨用户召回群里历史相关消息（"长记忆"）
    - trigram 对 query 长度 < 3 没意义，跳过
    - 排除最近 older_than_seconds 秒内的（避免召回刚刚说的话）
    - 按 BM25 排序
    """
    if _db is None or not query or len(query.strip()) < 3:
        return []
    try:
        # FTS5 query escape: 用双引号包成 phrase，内部的 " 替换掉防止语法炸
        safe_q = '"' + query.replace('"', " ").strip() + '"'
        cutoff = time.time() - older_than_seconds
        cursor = await _db.execute(
            """
            SELECT h.role, h.content
            FROM history h
            JOIN history_fts f ON f.rowid = h.id
            WHERE history_fts MATCH ?
              AND h.group_id = ?
              AND h.ts < ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe_q, group_id, cutoff, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [{"role": r, "content": c} for r, c in rows]
    except Exception as e:
        logger.warning(f"FTS search 失败 group={group_id} q={query[:30]!r}: {e}")
        return []


async def warm_load_into_memory() -> None:
    """启动时按 (group_id, user_id) 灌回 state.history，老数据 user_id=0 会进各群的 (gid, 0) 桶里被自然淘汰"""
    if _db is None:
        return
    try:
        cursor = await _db.execute(
            "SELECT DISTINCT group_id, user_id FROM history WHERE user_id > 0"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for gid, uid in rows:
            c2 = await _db.execute(
                "SELECT role, content FROM history WHERE group_id = ? AND user_id = ? ORDER BY ts DESC LIMIT ?",
                (gid, uid, WARM_LOAD_LIMIT_PER_USER),
            )
            items = await c2.fetchall()
            await c2.close()
            for role, content in reversed(items):
                state.history[(gid, uid)].append({"role": role, "content": content})
        logger.info(f"warm_load 完成 (群,用户)桶数={len(rows)}")
    except Exception as e:
        logger.warning(f"warm_load 失败: {e}")


async def log_usage(
    group_id: int,
    profile: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
) -> None:
    """落盘每次 AI 调用的 token 用量（gid=0 表示非群聊场景）"""
    if _db is None:
        return
    try:
        await _db.execute(
            """
            INSERT INTO usage_log(group_id, profile, model, prompt_tokens, completion_tokens, total_tokens, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (group_id, profile, model, prompt_tokens, completion_tokens, total_tokens, time.time()),
        )
        await _db.commit()
    except Exception as e:
        logger.warning(f"usage_log 落盘失败: {e}")


async def usage_today() -> list[dict]:
    """
    返回今日按 group_id+profile 聚合的用量
    [{group_id, profile, model, calls, prompt, completion, total}, ...]
    """
    if _db is None:
        return []
    try:
        # 今天 00:00 的 unix 时间戳
        from datetime import datetime, time as dtime
        start_of_day = datetime.combine(datetime.now().date(), dtime.min).timestamp()
        cursor = await _db.execute(
            """
            SELECT group_id, profile, model,
                   COUNT(*) AS calls,
                   SUM(prompt_tokens) AS prompt,
                   SUM(completion_tokens) AS completion,
                   SUM(total_tokens) AS total
            FROM usage_log
            WHERE ts >= ?
            GROUP BY group_id, profile, model
            ORDER BY total DESC
            """,
            (start_of_day,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                "group_id": r[0],
                "profile": r[1],
                "model": r[2],
                "calls": r[3],
                "prompt": r[4] or 0,
                "completion": r[5] or 0,
                "total": r[6] or 0,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"usage_today 查询失败: {e}")
        return []


async def admin_log(
    group_id: int,
    actor_qq: int,
    tool: str,
    args: dict,
    success: bool,
    msg: str = "",
) -> None:
    """落 agent 管理操作日志(谁触发了什么工具,成功/失败)"""
    if _db is None:
        return
    import json as _json
    try:
        await _db.execute(
            """
            INSERT INTO admin_log(group_id, actor_qq, tool, args, success, msg, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                actor_qq,
                tool,
                _json.dumps(args, ensure_ascii=False),
                1 if success else 0,
                msg[:500],
                time.time(),
            ),
        )
        await _db.commit()
    except Exception as e:
        logger.warning(f"admin_log 落盘失败: {e}")


async def history_recent(group_id: int | None = None, limit: int = 50) -> list[dict]:
    """给 self_maintain 用:拉最近 N 条聊天记录(可指定群; None = 所有群)"""
    if _db is None:
        return []
    try:
        if group_id is None:
            cursor = await _db.execute(
                "SELECT ts, group_id, user_id, role, content FROM history ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
        else:
            cursor = await _db.execute(
                "SELECT ts, group_id, user_id, role, content FROM history "
                "WHERE group_id = ? ORDER BY ts DESC LIMIT ?",
                (group_id, limit),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        from datetime import datetime
        return [
            {
                "ts_str": datetime.fromtimestamp(r[0]).strftime("%m-%d %H:%M:%S"),
                "group_id": r[1],
                "user_id": r[2],
                "role": r[3],
                "content": (r[4] or "")[:300],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"history_recent 查询失败: {e}")
        return []


async def maintain_log_recent(limit: int = 10) -> list[dict]:
    """给 self_maintain 看自己历史 — 避免重复修同样问题"""
    if _db is None:
        return []
    try:
        cursor = await _db.execute(
            "SELECT ts, kind, hint, verdict, cost_usd, details FROM maintain_log "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        from datetime import datetime
        return [
            {
                "ts_str": datetime.fromtimestamp(r[0]).strftime("%m-%d %H:%M"),
                "kind": r[1],
                "hint": r[2],
                "verdict": r[3],
                "cost_usd": r[4],
                "details": (r[5] or "")[:300],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"maintain_log_recent 查询失败: {e}")
        return []


async def maintain_log(
    kind: str,
    hint: str = "",
    verdict: str = "",
    cost_usd: float = 0.0,
    details: str = "",
) -> None:
    """落 AI 维修工操作日志(kind=diagnose/propose_fix/apply_fix/rollback/patrol/post_restart_ok)"""
    if _db is None:
        return
    try:
        await _db.execute(
            """
            INSERT INTO maintain_log(kind, hint, verdict, cost_usd, details, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (kind, hint[:500], verdict[:100], float(cost_usd or 0), details[:2000], time.time()),
        )
        await _db.commit()
    except Exception as e:
        logger.warning(f"maintain_log 落盘失败: {e}")


async def maintain_cost_today() -> list[dict]:
    """按 kind 聚合今日 AI 维修工花费"""
    if _db is None:
        return []
    try:
        from datetime import datetime, time as dtime
        start_of_day = datetime.combine(datetime.now().date(), dtime.min).timestamp()
        cursor = await _db.execute(
            """
            SELECT kind, COUNT(*) AS calls, SUM(cost_usd) AS cost
            FROM maintain_log
            WHERE ts >= ?
            GROUP BY kind
            ORDER BY cost DESC
            """,
            (start_of_day,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [{"kind": r[0], "calls": r[1], "cost": r[2] or 0.0} for r in rows]
    except Exception as e:
        logger.warning(f"maintain_cost_today 查询失败: {e}")
        return []


async def admin_log_recent(limit: int = 20, tool: str | None = None) -> list[dict]:
    """查最近 N 条 agent 工具操作; tool 可选过滤(精确匹配工具名)。
    返回字段: ts_str / tool / args / success / msg / actor_qq / group_id
    """
    if _db is None:
        return []
    try:
        if tool:
            cursor = await _db.execute(
                """
                SELECT ts, tool, args, success, msg, actor_qq, group_id FROM admin_log
                WHERE tool = ? ORDER BY ts DESC LIMIT ?
                """,
                (tool, limit),
            )
        else:
            cursor = await _db.execute(
                """
                SELECT ts, tool, args, success, msg, actor_qq, group_id FROM admin_log
                ORDER BY ts DESC LIMIT ?
                """,
                (limit,),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        from datetime import datetime
        out: list[dict] = []
        for ts, t, a, s, m, q, g in rows:
            out.append({
                "ts_str": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                "tool": t,
                "args": a,
                "success": bool(s),
                "msg": m,
                "actor_qq": q,
                "group_id": g,
            })
        return out
    except Exception as e:
        logger.warning(f"admin_log_recent 查询失败: {e}")
        return []


async def trim_usage_log() -> None:
    """删掉超过 USAGE_LOG_KEEP_DAYS 天的 usage_log"""
    if _db is None:
        return
    try:
        cutoff = time.time() - USAGE_LOG_KEEP_DAYS * 86400
        await _db.execute("DELETE FROM usage_log WHERE ts < ?", (cutoff,))
        await _db.commit()
    except Exception as e:
        logger.warning(f"usage_log trim 失败: {e}")


# admin_log 保留 90 天(跟 usage_log 一致); agent 操作量不大但月级累积仍需清理
ADMIN_LOG_KEEP_DAYS = 90


async def trim_admin_log() -> None:
    if _db is None:
        return
    try:
        cutoff = time.time() - ADMIN_LOG_KEEP_DAYS * 86400
        await _db.execute("DELETE FROM admin_log WHERE ts < ?", (cutoff,))
        await _db.commit()
    except Exception as e:
        logger.warning(f"admin_log trim 失败: {e}")


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
