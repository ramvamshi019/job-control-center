#!/bin/bash
# deploy/watchdog.sh
# Runs on the droplet from cron every 5 min. Emails when disk >80% used or
# available RAM <200MB. Emails at most once per hour per alert type — flag
# files under /tmp/watchdog-*.flag mark "already alerted, don't spam".
#
# Reuses the Gmail App Password from /app/backend/data/db/gmail_settings.json
# (same creds morning_brief uses) so no new secrets to manage.
#
# Cron install:
#   */5 * * * * root /root/job-control-center/deploy/watchdog.sh >> /root/backups/watchdog.log 2>&1

set -uo pipefail

# ---- Thresholds ------------------------------------------------------------
DISK_PCT_MAX=80          # alert when / usage > this
MEM_FREE_MB_MIN=200      # alert when available memory < this
DOCKER_DOWN_ALERT=1      # 1 = alert when a compose service isn't running

# ---- SMTP creds via python + gmail_settings.json ---------------------------
SETTINGS=/var/lib/docker/volumes/job-control-center_jcc-data/_data/gmail_settings.json
if [ ! -f "$SETTINGS" ]; then
    # Alternate path — some docker installs use a different volume location
    SETTINGS=$(docker exec job-control-center-backend-1 cat /app/backend/data/db/gmail_settings.json 2>/dev/null | head -c 4096)
fi

send_email() {
    local subject="$1"
    local body="$2"
    docker exec -i job-control-center-backend-1 python -c "
import json, smtplib, sys
from email.message import EmailMessage
with open('/app/backend/data/db/gmail_settings.json') as f:
    s = json.load(f)
msg = EmailMessage()
msg['Subject'] = sys.argv[1]
msg['From'] = s['email']
msg['To'] = s['email']
msg.set_content(sys.argv[2])
with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as smtp:
    smtp.login(s['email'], s['app_password'])
    smtp.send_message(msg)
" "$subject" "$body" 2>/dev/null
}

# ---- Alert throttle: only fire once per hour per alert type ----------------
alert_if_new() {
    local key="$1"; local subject="$2"; local body="$3"
    local flag="/tmp/watchdog-${key}.flag"
    if [ -f "$flag" ] && [ $(( $(date +%s) - $(stat -c %Y "$flag") )) -lt 3600 ]; then
        # already alerted within the last hour, skip
        return
    fi
    echo "[$(date -u +%FT%TZ)] ALERT $key: $subject"
    if send_email "$subject" "$body"; then
        touch "$flag"
    fi
}

# ---- Disk check ------------------------------------------------------------
DISK_PCT=$(df -h / | tail -1 | awk '{print $5}' | tr -d '%')
if [ "$DISK_PCT" -gt "$DISK_PCT_MAX" ]; then
    DISK_INFO=$(df -h /)
    alert_if_new "disk" \
        "🚨 JCC droplet disk >${DISK_PCT_MAX}% full ($DISK_PCT%)" \
        "Disk usage on 143.198.188.116 crossed the threshold.

$DISK_INFO

Common culprits:
  du -sh /var/lib/docker/overlay2/     # container layer bloat
  du -sh /root/backups/                # DB snapshots (14-file retention)
  docker system prune -a               # nuclear cleanup"
fi

# ---- Memory check ----------------------------------------------------------
MEM_FREE=$(free -m | awk 'NR==2 {print $7}')  # 'available' column
if [ "$MEM_FREE" -lt "$MEM_FREE_MB_MIN" ]; then
    MEM_INFO=$(free -m)
    TOP=$(docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}' | head -6)
    alert_if_new "memory" \
        "🚨 JCC droplet free RAM < ${MEM_FREE_MB_MIN}MB ($MEM_FREE MB)" \
        "Free memory on 143.198.188.116 dropped below the threshold.

$MEM_INFO

Top containers by memory:
$TOP"
fi

# ---- Container health check ------------------------------------------------
if [ "$DOCKER_DOWN_ALERT" = "1" ]; then
    EXPECTED=(job-control-center-backend-1 job-control-center-livewatch-1 job-control-center-discovery-1 job-control-center-dashboard-1)
    DOWN=""
    for name in "${EXPECTED[@]}"; do
        STATUS=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)
        if [ "$STATUS" != "running" ]; then
            DOWN="$DOWN\n  $name: $STATUS"
        fi
    done
    if [ -n "$DOWN" ]; then
        alert_if_new "containers" \
            "🚨 JCC container(s) not running" \
            "One or more expected containers are not in the 'running' state on 143.198.188.116:
$(echo -e "$DOWN")

Run 'docker compose up -d' in /root/job-control-center to bring them back."
    fi
fi

# ---- Clear stale alert flags when things recover --------------------------
if [ "$DISK_PCT" -le "$DISK_PCT_MAX" ]; then rm -f /tmp/watchdog-disk.flag; fi
if [ "$MEM_FREE" -ge "$MEM_FREE_MB_MIN" ]; then rm -f /tmp/watchdog-memory.flag; fi

echo "[$(date -u +%FT%TZ)] watchdog ok — disk ${DISK_PCT}% free_mem ${MEM_FREE}MB"
