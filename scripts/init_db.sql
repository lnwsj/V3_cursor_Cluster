-- =============================================================================
-- V3_cursor_Cluster — PostgreSQL schema (v1)
-- Idempotent: safe to re-run. Use `psql -f init_db.sql` on a fresh DB.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid(), digest()

-- ---------------------------------------------------------------------------
-- Users (uploaders, not workers; workers authenticate via api_keys with role='worker')
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    plan            TEXT NOT NULL DEFAULT 'free'
                    CHECK (plan IN ('free','pro','enterprise')),
    credits         INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- API keys (admin / uploader / worker roles)
-- Stored as sha256 hash. Plaintext is shown to user once at creation.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash        TEXT UNIQUE NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin','uploader','worker')),
    owner_user_id   UUID REFERENCES users(id) ON DELETE CASCADE,
    worker_id       TEXT,         -- for role=worker, ties to workers.id
    label           TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS api_keys_worker_id_idx ON api_keys(worker_id) WHERE role = 'worker';

-- ---------------------------------------------------------------------------
-- Files (uploaded originals + rendered outputs)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS files (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id   UUID REFERENCES users(id) ON DELETE SET NULL,
    role            TEXT NOT NULL CHECK (role IN ('original','output','log')),
    original_name   TEXT NOT NULL,
    storage_path    TEXT NOT NULL,            -- relative to STORAGE_ROOT
    size_bytes      BIGINT NOT NULL CHECK (size_bytes >= 0),
    sha256          TEXT NOT NULL,
    mime            TEXT NOT NULL DEFAULT 'application/octet-stream',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS files_owner_idx ON files(owner_user_id);
CREATE INDEX IF NOT EXISTS files_sha256_idx ON files(sha256);

-- ---------------------------------------------------------------------------
-- Jobs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id           UUID REFERENCES users(id) ON DELETE SET NULL,
    tc                      TEXT NOT NULL CHECK (tc IN ('tc01')),
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','claimed','running','succeeded','failed','cancelled','expired')),
    settings_json           JSONB NOT NULL,
    input_file_ids          UUID[] NOT NULL DEFAULT '{}',
    output_file_id          UUID REFERENCES files(id) ON DELETE SET NULL,
    claimed_by_worker_id    TEXT,
    claim_expires_at        TIMESTAMPTZ,
    progress_pct            REAL NOT NULL DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
    log_text                TEXT NOT NULL DEFAULT '',
    error_text              TEXT,
    cancel_requested        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at              TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ,
    duration_ms             INTEGER
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_owner_idx ON jobs(owner_user_id);
CREATE INDEX IF NOT EXISTS jobs_worker_idx ON jobs(claimed_by_worker_id);
-- Critical: claim query uses (status='pending') AND (tc IN allowed) with claim_expires_at IS NULL OR < now()
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs(status, tc) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- Workers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workers (
    id                      TEXT PRIMARY KEY,        -- user-defined, e.g. "rtx3050-01"
    label                   TEXT NOT NULL,
    gpu_label               TEXT,
    max_parallel            INTEGER NOT NULL DEFAULT 1 CHECK (max_parallel >= 1),
    current_jobs            INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_at       TIMESTAMPTZ,
    status                  TEXT NOT NULL DEFAULT 'offline'
                            CHECK (status IN ('online','busy','offline','disabled')),
    tc_filter               TEXT[] NOT NULL DEFAULT ARRAY['tc01'],
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Heartbeats (history, capped to last 100 per worker)
CREATE TABLE IF NOT EXISTS heartbeats (
    worker_id       TEXT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    current_jobs    INTEGER NOT NULL,
    gpu_util_pct    REAL,
    mem_used_mb     INTEGER,
    status          TEXT NOT NULL,
    PRIMARY KEY (worker_id, ts)
);

CREATE INDEX IF NOT EXISTS heartbeats_ts_idx ON heartbeats(ts DESC);

-- ---------------------------------------------------------------------------
-- Rate limit (per API key, per-minute, token bucket)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rate_buckets (
    api_key_id      UUID NOT NULL,
    window_start    TIMESTAMPTZ NOT NULL,           -- truncated to minute
    count           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (api_key_id, window_start)
);

CREATE INDEX IF NOT EXISTS rate_buckets_window_idx ON rate_buckets(window_start);

-- ---------------------------------------------------------------------------
-- Default admin bootstrap (replace via /api/v1/admin/bootstrap)
-- ---------------------------------------------------------------------------
-- (No rows here — admin key is set via env var ADMIN_API_KEY at gateway boot,
--  and bootstrap endpoint creates the first user + admin key record.)
