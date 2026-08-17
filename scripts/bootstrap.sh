#!/usr/bin/env bash
# =============================================================================
# V3_cursor_Cluster — Bootstrap: create first uploader key
# Run this ONCE after gateway is up. Needs ADMIN_API_KEY.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -f "$REPO_ROOT/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; source "$REPO_ROOT/.env"; set +a
fi

: "${ADMIN_API_KEY:?ADMIN_API_KEY must be set}"
: "${GATEWAY_PUBLIC_URL:?GATEWAY_PUBLIC_URL must be set (e.g. http://localhost:8770)}"

# Default uploader email
UPLOADER_EMAIL="${UPLOADER_EMAIL:-uploader@local}"
LABEL="${1:-default}"

echo "[bootstrap] gateway: $GATEWAY_PUBLIC_URL"
echo "[bootstrap] creating uploader key for: $UPLOADER_EMAIL"

# Create user first
curl -sS -X POST "$GATEWAY_PUBLIC_URL/api/v1/admin/users" \
    -H "X-Admin-Key: $ADMIN_API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$UPLOADER_EMAIL\",\"plan\":\"pro\"}" | python3 -m json.tool 2>/dev/null \
    || echo "  (user may already exist — that's fine)"

# Issue uploader key
echo
echo "[bootstrap] uploader API key (SAVE THIS — shown only once):"
echo
RESP=$(curl -sS -X POST "$GATEWAY_PUBLIC_URL/api/v1/admin/keys" \
    -H "X-Admin-Key: $ADMIN_API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"role\":\"uploader\",\"label\":\"$LABEL\",\"owner_email\":\"$UPLOADER_EMAIL\"}")
echo "$RESP" | python3 -m json.tool
echo
echo "Use this key in the UI: Settings → Uploader API Key"
echo
echo "To issue a WORKER key, run:"
echo "  curl -sS -X POST $GATEWAY_PUBLIC_URL/api/v1/admin/keys \\"
echo "    -H \"X-Admin-Key: \$ADMIN_API_KEY\" -H \"Content-Type: application/json\" \\"
echo "    -d '{\"role\":\"worker\",\"label\":\"rtx3050-01\",\"worker_id\":\"rtx3050-01\"}'"
