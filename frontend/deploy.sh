#!/usr/bin/env bash
# Deploy the built React SPA to the droplet.
#
#   ./frontend/deploy.sh
#
# Assumes:
#   * You've run `npm install` at least once in frontend/.
#   * Your SSH key is authorized on root@143.198.188.116.
#   * Caddy on the droplet already has a site block pointing at
#     /var/www/jcc-frontend/ and proxying /api/* to localhost:8000
#     (see the Caddyfile stanza for app.143.198.188.116.sslip.io).
#
# Idempotent: builds, rsyncs, prints the URL. Does NOT touch Caddy config.
set -euo pipefail

FRONTEND_DIR="$(cd "$(dirname "$0")" && pwd)"
DROPLET="root@143.198.188.116"
REMOTE_DIR="/var/www/jcc-frontend/"
URL="https://app.143.198.188.116.sslip.io/"

echo "[deploy] npm build in $FRONTEND_DIR"
cd "$FRONTEND_DIR"
npm run build

echo "[deploy] rsync dist/ -> $DROPLET:$REMOTE_DIR"
ssh "$DROPLET" "mkdir -p $REMOTE_DIR"
rsync -az --delete dist/ "$DROPLET:$REMOTE_DIR"

echo "[deploy] done -> $URL"
