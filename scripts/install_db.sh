#!/usr/bin/env bash
# =============================================================================
# V3_cursor_Cluster — PostgreSQL schema installer
# Usage:  ./install_db.sh [DATABASE_URL]
# If DATABASE_URL not given, reads from .env or defaults to local postgres.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SQL_FILE="$REPO_ROOT/scripts/init_db.sql"

if [[ -f "$REPO_ROOT/.env" ]]; then
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
fi

DATABASE_URL="${1:-${DATABASE_URL:-postgresql://v3cluster:v3cluster@127.0.0.1:5432/v3cluster}}"

echo "[install_db] target: $DATABASE_URL"
echo "[install_db] schema: $SQL_FILE"

# Parse DB host + dbname for createdb if needed
DBNAME=$(echo "$DATABASE_URL" | sed -E 's|.*/([^/?]+)(\?.*)?$|\1|')

# Try to create DB if missing
if ! psql "$DATABASE_URL" -c "SELECT 1" >/dev/null 2>&1; then
    echo "[install_db] DB $DBNAME not reachable; attempting to create"
    # Connect to postgres maintenance DB
    ADMIN_URL=$(echo "$DATABASE_URL" | sed -E "s|/$DBNAME\$|/postgres|")
    psql "$ADMIN_URL" -c "CREATE DATABASE $DBNAME" 2>/dev/null || true
fi

# Run schema (idempotent)
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$SQL_FILE"

echo "[install_db] OK — schema applied to $DBNAME"
