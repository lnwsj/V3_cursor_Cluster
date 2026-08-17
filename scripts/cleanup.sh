#!/usr/bin/env bash
# =============================================================================
# V3_cursor_Cluster — Hourly cleanup cron
# Run via /etc/cron.d/v3cluster-cleanup:
#   0 * * * * root /opt/v3cluster/app/scripts/cleanup.sh
# Cleans:
#   - rate_buckets older than 2h
#   - jobs older than 7 days (and their output files)
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$REPO_ROOT/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; source "$REPO_ROOT/.env"; set +a
fi
: "${DATABASE_URL:?DATABASE_URL required}"

PSQL="psql $DATABASE_URL -t -A"

# 1. Rate buckets
DELETED=$($PSQL -c "WITH d AS (DELETE FROM rate_buckets WHERE window_start < now() - interval '2 hours' RETURNING 1) SELECT count(*) FROM d")
echo "[cleanup] rate_buckets deleted: $DELETED"

# 2. Old jobs + their files
N_JOBS=$($PSQL -c "WITH d AS (DELETE FROM jobs WHERE completed_at < now() - interval '7 days' RETURNING output_file_id) SELECT count(*) FROM d")
echo "[cleanup] jobs older than 7d deleted: $N_JOBS"

# 3. Orphan files (no job reference) older than 1 day
N_FILES=$($PSQL -c "WITH d AS (DELETE FROM files WHERE role='output' AND created_at < now() - interval '1 day' AND id NOT IN (SELECT output_file_id FROM jobs WHERE output_file_id IS NOT NULL) RETURNING storage_path) SELECT count(*) FROM d")
echo "[cleanup] orphan outputs deleted: $N_FILES"

# 4. Best-effort disk cleanup of files whose DB rows are gone
STORAGE_ROOT="${STORAGE_ROOT:-/opt/v3cluster/storage}"
if [[ -d "$STORAGE_ROOT" ]]; then
    find "$STORAGE_ROOT/output" -type f -mmin +60 -delete 2>/dev/null || true
    echo "[cleanup] disk sweep done in $STORAGE_ROOT/output"
fi
