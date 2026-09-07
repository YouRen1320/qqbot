#!/usr/bin/env bash
# Self-maintain watchdog —— 每分钟 cron 跑一次。
#
# 职责:
#   - 看 data/.maintain_apply_log: 这是 bot 内部 apply_fix 写的 "我刚应用了修改, 麻烦 host 监控我"
#   - bot 在 PEND_TS 之后成功 reconnect (data/.bot_alive 被 on_bot_connect hook touch) → git add + commit + push, 清 flag
#   - bot 在 90s 内没 reconnect → 自动还原 .bak 文件 + 重启 + 标记 .auto_reverted
#
# 健康哨兵方式 (替换原 docker logs grep):
#   bot 在 on_bot_connect hook 中 touch /app/data/.bot_alive
#   watchdog 看 .bot_alive 的 mtime 是否 > PEND_TS, 是 → bot 起来了
#   原 grep "OneBot V11.*connected" 在 cron 环境下不稳 (--since 时区 + docker logs race),
#   多次把成功 apply 误判为失败强行回滚, 导致主人聊天被打断。
#
# 安装:
#   scp scripts/maintain_watchdog.sh root@your-server:/root/qqbot/maintain_watchdog.sh
#   ssh root@your-server 'chmod +x /root/qqbot/maintain_watchdog.sh && \
#     (crontab -l 2>/dev/null | grep -v maintain_watchdog; \
#      echo "* * * * * /root/qqbot/maintain_watchdog.sh") | crontab -'
set -uo pipefail

ROOT=/root/qqbot/nonebot
APPLY_LOG=$ROOT/data/.maintain_apply_log
BACKUP_DIR=$ROOT/data/maintain_backups
ALIVE_FILE=$ROOT/data/.bot_alive
LOG=/var/log/qqbot-maintain-watchdog.log

[ -f "$APPLY_LOG" ] || exit 0

NOW=$(date +%s)
PEND_TS=$(stat -c %Y "$APPLY_LOG")
AGE=$((NOW - PEND_TS))

# < 60s 给重启时间
[ $AGE -lt 60 ] && exit 0

cd $ROOT 2>/dev/null || { echo "$(date) FAIL: $ROOT 不存在" >> $LOG; exit 1; }

# 检查 bot 在 PEND_TS 之后是否 reconnect 成功:
# .bot_alive 文件 mtime > PEND_TS 即说明 on_bot_connect 跑过, bot 起来了
RECONNECTED=0
if [ -f "$ALIVE_FILE" ]; then
    ALIVE_TS=$(stat -c %Y "$ALIVE_FILE")
    [ $ALIVE_TS -gt $PEND_TS ] && RECONNECTED=1
fi

if [ $RECONNECTED -eq 1 ]; then
    # ✅ 起来了 → git commit + push + 清 flag
    HINT=$(python3 -c "import json; d=json.load(open('$APPLY_LOG')); print(d.get('hint','')[:80].replace(chr(10),' '))" 2>/dev/null || echo "ai-maintain")
    SUMMARY=$(python3 -c "import json; d=json.load(open('$APPLY_LOG')); print((d.get('summary','') or '')[:120].replace(chr(10),' '))" 2>/dev/null || echo "")

    if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
        git add -A 2>>$LOG
        if git commit -m "[ai-maintain] $SUMMARY -- $HINT" --quiet 2>>$LOG; then
            echo "$(date) committed: $SUMMARY" >> $LOG
            # push 失败不算严重(网络抖动), 仍然清 flag
            git push origin main --quiet 2>>$LOG && echo "  pushed" >> $LOG || echo "  push failed (will retry next maintain)" >> $LOG
        else
            echo "$(date) git commit no-op (no changes?)" >> $LOG
        fi
    else
        echo "$(date) reconnected but git tree clean (apply_fix 可能没真改 file?)" >> $LOG
    fi
    mv "$APPLY_LOG" "${APPLY_LOG}.done.$(date +%Y%m%d_%H%M%S)"
    exit 0
fi

# 60-90s 之间, 还在 grace period
[ $AGE -lt 90 ] && exit 0

# 90s+ 没回 → 自动 revert
echo "$(date) AUTO-REVERT triggered (age=${AGE}s, bot 未 reconnect)" >> $LOG

python3 <<EOF >>$LOG 2>&1
import json, shutil, os, sys
APPLY = "$APPLY_LOG"
ROOT = "$ROOT"
BACKUP_DIR = "$BACKUP_DIR"
try:
    info = json.load(open(APPLY))
except Exception as e:
    print(f"  pending read failed: {e}")
    sys.exit(1)
restored = []
changes = info.get("changes")
if isinstance(changes, list) and changes:
    for change in reversed(changes):
        rel = str(change.get("path") or "").lstrip("/")
        target = os.path.join(ROOT, rel)
        backup = change.get("backup")
        if change.get("existed") and backup:
            bak = os.path.join(BACKUP_DIR, backup)
            if os.path.exists(bak):
                shutil.copy2(bak, target)
                restored.append(rel)
                print(f"  restored {rel} <- {backup}")
            else:
                print(f"  BAK MISSING: {bak}")
        elif not change.get("existed") and os.path.exists(target):
            os.remove(target)
            restored.append(rel)
            print(f"  removed newly-created {rel}")
else:
    # 兼容升级前产生的旧清单。
    files = info.get("files", [])
    backups = info.get("backups", [])
    for rel, backup in zip(files, backups):
        target = os.path.join(ROOT, rel)
        bak = os.path.join(BACKUP_DIR, backup)
        if os.path.exists(bak):
            shutil.copy2(bak, target)
            restored.append(rel)
            print(f"  restored legacy {rel} <- {backup}")
print(f"  total restored: {len(restored)}")
EOF

mv "$APPLY_LOG" "${APPLY_LOG}.auto_reverted.$(date +%Y%m%d_%H%M%S)"

# 顺便清掉 bot 的 .maintain_pending(它已经 reverted, 不该再 PM "上线"了)
PEND_BOT=$ROOT/data/.maintain_pending
[ -f "$PEND_BOT" ] && mv "$PEND_BOT" "${PEND_BOT}.auto_reverted.$(date +%Y%m%d_%H%M%S)"

# 重启 nonebot
docker compose restart nonebot >>$LOG 2>&1
echo "$(date) restart after auto-revert done" >> $LOG
