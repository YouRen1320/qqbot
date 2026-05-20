#!/usr/bin/env bash
# 远端执行的关键状态备份脚本
# 安装: scp scripts/backup.sh root@your-server:/root/qqbot/backup.sh
#       ssh root@your-server 'chmod +x /root/qqbot/backup.sh && \
#         (crontab -l 2>/dev/null; echo "0 4 * * * /root/qqbot/backup.sh") | crontab -'
# 每天 04:00 备份 qqbot.db + cookies.txt + .env 到 /root/qqbot/backups/YYYY-MM-DD/, 保留 14 天
set -euo pipefail

ROOT="/root/qqbot/nonebot"
BACKUP_ROOT="/root/qqbot/backups"
DEST="$BACKUP_ROOT/$(date +%Y-%m-%d)"
LOG="/var/log/qqbot-backup.log"

mkdir -p "$DEST"

# SQLite 用 .backup 拿一致快照(防 trans 中复制损坏); 失败兜底用 cp
if command -v sqlite3 >/dev/null 2>&1 && [ -f "$ROOT/data/qqbot.db" ]; then
    sqlite3 "$ROOT/data/qqbot.db" ".backup $DEST/qqbot.db" 2>>"$LOG" \
      || cp "$ROOT/data/qqbot.db" "$DEST/qqbot.db.raw"
fi

# 其他文件直接 cp(小, 不需要事务)
[ -f "$ROOT/data/cookies.txt" ] && cp "$ROOT/data/cookies.txt" "$DEST/cookies.txt"
[ -f "$ROOT/.env" ]              && cp "$ROOT/.env"              "$DEST/.env"

chmod -R 600 "$DEST" 2>/dev/null || true
chmod 700 "$DEST" 2>/dev/null || true

# 清 14 天前的备份目录
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} \; 2>>"$LOG" || true

# 简短记账
SIZE=$(du -sh "$DEST" 2>/dev/null | awk '{print $1}')
echo "$(date '+%F %T') backup OK $DEST size=$SIZE" >> "$LOG"
