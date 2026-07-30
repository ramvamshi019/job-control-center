#!/bin/bash
# ---- Daily JCC DB backup (cron-invoked) --------------------------------------
# Runs from the DROPLET (see /etc/cron.d/jcc-backup). Uses SQLite VACUUM INTO
# for a consistent snapshot -- doesn't block writers, safe while livewatch
# and seeders are running.
#
# Retention: 14 files. Older backups auto-delete. Total disk footprint at
# steady state: ~14 x current DB size (currently ~1.5 GB -> ~21 GB total).
# Adjust RETENTION if disk gets tight.
#
# Recovery:
#   1. Stop everything:      docker compose stop
#   2. Restore chosen file:  cp /root/backups/jcc-YYYY-MM-DD.db \
#                              /var/lib/docker/volumes/job-control-center_jcc-data/_data/jobs.db
#      (also delete jobs.db-wal and jobs.db-shm in that dir if they exist)
#   3. Restart:              docker compose up -d
set -euo pipefail

BACKUP_DIR="/root/backups"
RETENTION=14
STAMP=$(date -u +%Y-%m-%d)
LOG="${BACKUP_DIR}/backup.log"

mkdir -p "$BACKUP_DIR"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG"
}

DEST="${BACKUP_DIR}/jcc-${STAMP}.db"

# Skip if today's already-good file exists (in case cron double-fires).
if [ -f "$DEST" ] && [ "$(stat -c%s "$DEST")" -gt 1000000 ]; then
    log "SKIP: today's backup already exists (${DEST}, $(stat -c%s "$DEST") bytes)"
    exit 0
fi

log "START: backing up to ${DEST}"

# VACUUM INTO is atomic + doesn't lock writers -- perfect for a running system.
# Uses Python's sqlite3 (installed with Python; the sqlite3 CLI is NOT in the
# container image). Runs inside the backend container so it can see the volume.
if docker exec job-control-center-backend-1 python -c "
import sqlite3, sys
src = '/app/backend/data/db/jobs.db'
dst = '/app/backend/data/db/_backup_tmp.db'
c = sqlite3.connect(src)
c.execute(\"VACUUM INTO ?\", (dst,))
c.close()
print('vacuum ok')
" 2>>"$LOG"; then
    # Copy from container volume to host backups dir, then remove the tmp inside container
    docker cp job-control-center-backend-1:/app/backend/data/db/_backup_tmp.db "$DEST" 2>>"$LOG"
    docker exec job-control-center-backend-1 rm -f /app/backend/data/db/_backup_tmp.db 2>>"$LOG" || true
    SIZE=$(stat -c%s "$DEST")
    log "OK: wrote ${DEST} (${SIZE} bytes)"
else
    log "FAIL: VACUUM INTO failed"
    exit 1
fi

# Retention: keep last N backups by mtime, delete the rest.
KEEP=$(ls -1t "$BACKUP_DIR"/jcc-*.db 2>/dev/null | head -n "$RETENTION")
for f in "$BACKUP_DIR"/jcc-*.db; do
    if ! echo "$KEEP" | grep -q "^${f}\$"; then
        log "PRUNE: ${f}"
        rm -f "$f"
    fi
done

log "END"
