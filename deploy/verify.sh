#!/bin/bash
# deploy/verify.sh
# Run AFTER a deploy on the droplet. Checks backend + dashboard health
# endpoints; exits non-zero + sends alert email if either is unresponsive.
#
# Usage:
#   ./deploy/verify.sh                    # exits 0 on success, 1 on any fail
#   ./deploy/verify.sh --rollback         # additionally suggests rollback commit
#
# Wire into any deploy script:
#   docker compose up -d --build backend
#   ./deploy/verify.sh || echo "❌ deploy failed, rollback with: git reset --hard HEAD~1"

set -uo pipefail

WAIT_S=15                                # give services time to boot
BACKEND_HEALTH=http://127.0.0.1:8000/health
DASHBOARD_HEALTH=http://127.0.0.1:8501/_stcore/health

# Optional: alert to Gmail via the backend container's SMTP creds
send_alert() {
    local subject="$1"
    local body="$2"
    docker exec -i job-control-center-backend-1 python -c "
import json, smtplib, sys
from email.message import EmailMessage
try:
    with open('/app/backend/data/db/gmail_settings.json') as f: s = json.load(f)
except Exception as e:
    sys.exit(0)  # no gmail configured, silent skip
msg = EmailMessage()
msg['Subject'] = sys.argv[1]; msg['From'] = s['email']; msg['To'] = s['email']
msg.set_content(sys.argv[2])
with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as smtp:
    smtp.login(s['email'], s['app_password']); smtp.send_message(msg)
" "$subject" "$body" 2>/dev/null || true
}

echo "▶️  Waiting ${WAIT_S}s for services to boot..."
sleep "$WAIT_S"

FAILED=""

check() {
    local name="$1"; local url="$2"
    if curl -f -s -m 8 "$url" > /dev/null; then
        echo "✅ $name  ($url)"
    else
        echo "❌ $name  ($url)  — NOT RESPONDING"
        FAILED="$FAILED $name"
    fi
}

check backend  "$BACKEND_HEALTH"
check dashboard "$DASHBOARD_HEALTH"

if [ -n "$FAILED" ]; then
    LAST_COMMIT=$(git -C /root/job-control-center log -1 --format='%h  %s' 2>/dev/null || echo "?")
    PREV_COMMIT=$(git -C /root/job-control-center log -1 --format='%h' HEAD~1 2>/dev/null || echo "HEAD~1")

    ALERT_BODY="Post-deploy health check failed on 143.198.188.116

Failed services:$FAILED

Current commit:  $LAST_COMMIT

Options:
  1. Quick rollback (safest):
       cd /root/job-control-center
       git reset --hard $PREV_COMMIT
       docker compose up -d --build

  2. Inspect logs:
       docker logs --tail 100 job-control-center-backend-1
       docker logs --tail 100 job-control-center-dashboard-1

  3. If just the health endpoint is misbehaving but the service is fine:
       docker restart job-control-center-backend-1"

    send_alert "🚨 JCC deploy verification FAILED" "$ALERT_BODY"

    echo ""
    echo "🔥 Deploy failed. Rollback with:"
    echo "   cd /root/job-control-center && git reset --hard $PREV_COMMIT && docker compose up -d --build"
    exit 1
fi

echo ""
echo "✅ deploy verified — all services healthy"
exit 0
