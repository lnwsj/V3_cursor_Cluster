# V3_cursor_Cluster — Architecture

Cluster version of [V3_cursor](../V3_cursor) green-screen (chroma key) toolkit.
Replaces the single-machine WebApp with a **1 gateway + N workers** topology so
heavy ffmpeg rendering scales horizontally across any machine with a GPU.

> **v1 scope:** TC01 (single-product chroma) only. The architecture is TC-agnostic
> — adding TC02/03/04/05/06 means adding a renderer module + a small `tcXX.py` on
> the worker, no gateway schema change.

## Topology

```
┌──────────────────────┐     HTTPS (Tailscale / VPN / public)
│   Uploader Browser   │
│  (React/vanilla UI)  │
└──────────┬───────────┘
           │ upload + render
           ▼
┌──────────────────────────────────────────────────────────┐
│  Gateway (1 machine)                                      │
│  ┌────────────┐  ┌────────────┐  ┌────────────────────┐  │
│  │  FastAPI   │  │  Auth +    │  │  Storage           │  │
│  │  REST API  │  │  RateLimit │  │  /opt/v3-cluster/  │  │
│  │            │  │  (PG)      │  │    originals/      │  │
│  └─────┬──────┘  └────────────┘  │    outputs/        │  │
│        │                         └────────────────────┘  │
│        ▼                                                  │
│  ┌────────────────────────────────────────────────────┐  │
│  │  PostgreSQL: users, api_keys, files, jobs,        │  │
│  │  workers, heartbeats, rate_buckets                │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────┬────────────────────────────────┘
                          │ polling (workers → gateway, every 2-3s)
                          ▼
   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
   │  Worker A   │  │  Worker B   │  │  Worker C   │
   │  RTX 3050   │  │  RTX 2050   │  │  Mac M4 MLX │
   │  GPU:NVENC  │  │  GPU:NVENC  │  │  CPU/Metal  │
   │  par=2      │  │  par=1      │  │  par=4      │
   └─────────────┘  └─────────────┘  └─────────────┘
```

## Why polling, not push?

- **NAT-friendly** — workers don't need a public IP or open inbound port
- **Crash recovery** — if a worker dies mid-job, gateway reaps the job after
  `worker_lease_seconds` (default 120s) and another worker claims it
- **Capacity-aware** — workers only claim jobs when they have spare slots
- **Same pattern as [cutdee-cluster](../cutdee-cluster) v2.4** which is already
  production-tested on this user's stack

## Why HTTP download for files, not NFS?

- **Portable** — works on Linux, macOS, WSL, Docker with zero setup
- **No kernel modules** — no NFS server to maintain
- **Auditable** — every transfer logged with file_id
- **Trade-off** — large files (multi-GB) cost bandwidth, but typical
  green-screen product is 50-200MB, output is 5-50MB, so it's fine

## Components

### Gateway (`gateway/`)

| Module | Purpose |
|---|---|
| `app.py` | FastAPI app + middleware (auth, rate limit, request ID) |
| `db.py` | asyncpg pool + schema migrations |
| `auth.py` | API key validation, role-based (admin / uploader / worker) |
| `rate_limit.py` | PG-backed token bucket per uploader |
| `storage.py` | file upload/download with size cap + sha256 verify |
| `routes/health.py` | `/api/v1/health` |
| `routes/files.py` | `POST /files/upload`, `GET /files/{id}` |
| `routes/jobs.py` | `POST /jobs/render`, `GET /jobs/{id}`, `POST /jobs/{id}/complete`, `POST /jobs/claim`, `GET /jobs/{id}/input/{fid}` |
| `routes/workers.py` | `POST /workers/heartbeat`, `GET /workers`, `GET /workers/{id}` |
| `routes/admin.py` | `POST /admin/keys`, `GET /admin/keys`, `DELETE /admin/keys/{id}` |
| `renderer/tc01_chroma.py` | TC01 settings → ffmpeg arg list (shared with worker) |

### Worker (`worker/`)

| Module | Purpose |
|---|---|
| `main.py` | polling loop, lifecycle, signal handling |
| `client.py` | async HTTP client to gateway (httpx) |
| `runner.py` | ffmpeg subprocess wrapper with progress + cancel |
| `tc01.py` | TC01 render — download inputs, run ffmpeg, upload output |

### UI (`ui/`)

Single-page vanilla HTML/JS (dark theme matching V3 style). Tabs:
- **Render** — upload product + bg + settings → render
- **Cluster** — live worker list with GPU/load
- **My Jobs** — list of your jobs with status + download

### Database schema (`scripts/init_db.sql`)

```sql
users(id, email, plan, credits, created_at)
api_keys(id, key_hash, role, owner_user_id, label, created_at, last_used_at, revoked_at)
files(id, owner_user_id, role, original_name, storage_path, size_bytes, sha256, mime, created_at)
jobs(id, owner_user_id, tc, status, settings_json, input_file_ids[], output_file_id,
     claimed_by_worker_id, claim_expires_at, progress_pct, log_text, error_text,
     created_at, started_at, completed_at, duration_ms)
workers(id, label, gpu_label, max_parallel, current_jobs, last_heartbeat_at, status)
heartbeats(worker_id, ts, current_jobs, gpu_util_pct, mem_used_mb, status)
rate_buckets(key, window_start, count)
```

## Job lifecycle

```
pending ──► claimed ──► running ──► succeeded
                │           │           (output uploaded, files kept)
                │           └────► failed (error_text set, cleaned up after N days)
                └────► expired (heartbeat timeout → reaped by next worker)
```

## Failure modes (designed in)

1. **Worker crashes mid-render** — gateway reaps after `claim_expires_at`;
   job returns to `pending` and another worker claims
2. **Worker can't reach gateway** — keeps retrying with exponential backoff,
   no data loss (input files are still on gateway)
3. **Gateway dies** — workers mark themselves stale; clients get connection
   error and can retry. No HA in v1; deploy gateway-2 if needed (same as
   cutdee-cluster pattern)
4. **ffmpeg fails** — worker uploads error log + marks job `failed`,
   uploads only the error metadata (no garbage MP4)
5. **Two workers claim same job** — prevented by `UPDATE ... WHERE claim_expires_at IS NULL`
   atomic claim with row-level lock
6. **User cancels** — `DELETE /jobs/{id}` sets `cancel_requested_at`; worker
   sees on next progress update, kills ffmpeg, marks `cancelled`

## What's intentionally NOT here in v1

- ❌ TC02-06 renderers (easy to add — copy `renderer/tc01_chroma.py` pattern)
- ❌ True HA (gateway-2 needs only a clone + same DB)
- ❌ WebSocket progress (polling at 1Hz is enough for green-screen jobs)
- ❌ Payment / credit system (use a `users.credits` column when needed)
- ❌ Pause/resume (cancel + re-render is fine for 5-30s jobs)

## Performance budget

| Operation | Budget | Notes |
|---|---|---|
| Upload 100MB product | ~10s on LAN | bottleneck = disk write on gateway |
| Worker claim + download | <2s | small JSON, no actual file yet |
| TC01 render 24s product | 3-6s on GPU | matches V3 single-machine baseline |
| Download 5MB output | <1s | |

Total round-trip: ~15-20s for a typical job, dominated by render.
Cluster doesn't add latency to the per-job path.

## Adding a new TC (recipe)

1. Add `gateway/renderer/tc0N_<name>.py` with `build_ffmpeg_args(settings, input_paths, output_path)`
2. Add `worker/tc0N_<name>.py` calling that builder + using the same
   `download_inputs → run_ffmpeg → upload_output` flow as `tc01.py`
3. Register the TC in the `JOB_TC_ENUM` on gateway + worker
4. Add a UI tab in `ui/index.html`
5. Done — no schema change needed, job dispatcher is TC-agnostic
