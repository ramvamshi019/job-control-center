#!/bin/bash
# deploy/heartbeat.sh
# Outbound heartbeat to healthchecks.io — free external uptime monitor.
# healthchecks.io expects a GET/POST to your unique ping URL every N
# minutes; if it doesn't arrive within your configured grace period,
# they email/SMS you.
#
# Advantage over pull-based monitoring (UptimeRobot): works even though
# the droplet's ports 8000/8501 are localhost-only behind SSH tunnel.
# The droplet only needs OUTBOUND HTTP.
#
# One-time setup:
#   1. Sign up at https://healthchecks.io/ (free tier: 20 checks)
#   2. Create a new check named 'JCC droplet heartbeat'
#      Period: 10 min · Grace: 5 min
#   3. Copy the ping URL (looks like https://hc-ping.com/xxxxxxxx-...-xxxx)
#   4. Write it to /root/healthchecks_url on the droplet:
#        echo 'https://hc-ping.com/YOUR-UUID' > /root/healthchecks_url
#   5. Install cron:
#        */5 * * * * root /root/job-control-center/deploy/heartbeat.sh
#
# When any of the checks below fail, this pings /fail on the URL — which
# also triggers healthchecks.io's alert.

set -uo pipefail

PING_URL_FILE=/root/healthchecks_url
if [ ! -f "$PING_URL_FILE" ]; then
    # No URL configured yet — silent no-op so cron doesn't spam log errors.
    exit 0
fi
PING_URL=$(head -n1 "$PING_URL_FILE" | tr -d '[:space:]')
[ -z "$PING_URL" ] && exit 0

# Health probes: report failure to healthchecks.io if either service is down
CHECK_OK=1
if ! curl -f -s -m 8 http://127.0.0.1:8000/health > /dev/null; then
    CHECK_OK=0
    echo "[$(date -u +%FT%TZ)] backend unhealthy"
fi
if ! curl -f -s -m 8 http://127.0.0.1:8501/_stcore/health > /dev/null; then
    CHECK_OK=0
    echo "[$(date -u +%FT%TZ)] dashboard unhealthy"
fi

if [ "$CHECK_OK" = "1" ]; then
    curl -fsS -m 10 "$PING_URL" > /dev/null && \
        echo "[$(date -u +%FT%TZ)] heartbeat ok"
else
    # /fail suffix tells healthchecks.io to alert immediately
    curl -fsS -m 10 "${PING_URL}/fail" > /dev/null && \
        echo "[$(date -u +%FT%TZ)] heartbeat FAIL sent"
fi
