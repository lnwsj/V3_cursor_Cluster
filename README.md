# V3_cursor_Cluster

> **Cluster version of [V3_cursor](https://github.com/lnwsj/V3_cursor)** — 1
> gateway + N workers, distributed green-screen (chroma key) rendering via ffmpeg.
> Horizontal scaling for any machine with a GPU.

```
Browser ──► Gateway (FastAPI + PG) ──poll─► Worker A (RTX 3050) ──► ffmpeg
                              ├────poll──► Worker B (RTX 2050) ──► ffmpeg
                              └────poll──► Worker C (M4 MLX)   ──► ffmpeg
```

## Why?

V3_cursor's WebApp (`lnwsj/V3_cursor` repo, branch `Web_minimax`) renders one
job at a time on one machine. As you add more machines with GPUs, you have no
way to use them. **V3_cursor_Cluster** turns it into a horizontal cluster — drop
files into the gateway, any free worker picks it up.

- **Same chroma engine** — same filter chain as V3 WebApp v1.0.0.20 (B320 GPU
  variant + CPU fallback), just distributed
- **Polling-based** — workers pull jobs, no inbound port needed (NAT-friendly)
- **PostgreSQL state** — jobs, workers, files, rate limit all in PG (HA-safe)
- **Self-contained** — no V3_cursor source dependency, no Vite/React build, no
  Django, just `pip install` + `python -m`

## What's in v1

- ✅ **TC01 only** (single-product chroma over background) — the most common
  green-screen use case
- ✅ 1 gateway + N workers, scaling horizontally
- ✅ Polling job dispatcher with lease-based crash recovery
- ✅ Auth (admin / uploader / worker API keys) + rate limit (PG token bucket)
- ✅ Cancel + progress streaming
- ✅ Web UI (vanilla HTML+JS, dark theme, no build step)
- ✅ systemd units for both gateway + worker
- ✅ Schema migrations via `psql -f init_db.sql` (idempotent)

## What's NOT in v1 (but easy to add)

- ❌ TC02-TC06 (reframe / batch / audio master) — copy `tc01.py` + add a new
  filter chain in `shared/renderer/`
- ❌ True HA gateway (gateway-2) — clone the repo + point at same PG
- ❌ WebSocket progress (1Hz polling is enough for 5-30s jobs)
- ❌ Per-tenant credit system (PG column exists, not wired to payment)

## 60-second quick start (local dev)

```bash
# 1. Postgres
sudo apt install postgresql
sudo -u postgres createuser -s v3cluster
sudo -u postgres createdb -O v3cluster v3cluster
# Or use a docker container — any PG ≥ 13 works

# 2. Clone + install
git clone https://github.com/lnwsj/V3_cursor_Cluster.git
cd V3_cursor_Cluster
python3 -m venv .venv && source .venv/bin/activate
pip install -r gateway/requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env: set ADMIN_API_KEY, DATABASE_URL, STORAGE_ROOT
# Generate a random admin key:  python3 -c "import secrets;print(secrets.token_hex(32))"

# 4. Init schema
DATABASE_URL=... bash scripts/install_db.sh

# 5. Run gateway
uvicorn gateway.app:app --host 0.0.0.0 --port 8770
# Open http://localhost:8770

# 6. Bootstrap an uploader key
ADMIN_API_KEY=... GATEWAY_PUBLIC_URL=http://localhost:8770 bash scripts/bootstrap.sh my-first-key
# Save the printed plaintext key — it's shown only once

# 7. On a second machine with GPU: deploy a worker
git clone https://github.com/lnwsj/V3_cursor_Cluster.git
cd V3_cursor_Cluster
WORKER_ID=rtx3050-01 WORKER_LABEL="RTX 3050" WORKER_API_KEY=... \
  GATEWAY_URL=http://GATEWAY_IP:8770 \
  sudo bash scripts/deploy_worker.sh
```

Then open the UI, paste your uploader key, upload a green-screen product + a
background, hit Render — any free worker grabs it.

## Production deploy (3 machines, Tailscale)

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for the full playbook. TL;DR:

- **Gateway machine**: postgres + gateway, exposed via Tailscale on `:8770`
- **Worker A (RTX 3050)**: deploy_worker.sh
- **Worker B (RTX 2050 / Mac)**: deploy_worker.sh
- Workers are on Tailscale, gateway only listens on 100.x.x.x:8770

## API

| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/api/v1/health` | public | liveness + counts |
| POST | `/api/v1/files/upload` | uploader/worker | multipart, returns file_id |
| GET | `/api/v1/files/{id}` | owner/admin | download file |
| GET | `/api/v1/files/{id}/meta` | owner/admin | metadata |
| POST | `/api/v1/jobs/render` | uploader | submit job |
| GET | `/api/v1/jobs/{id}` | owner/admin | poll status |
| GET | `/api/v1/jobs` | owner (own) / admin (all) | list |
| POST | `/api/v1/jobs/claim` | worker | atomic next-job claim |
| POST | `/api/v1/jobs/{id}/progress` | worker | progress + log line |
| POST | `/api/v1/jobs/{id}/complete` | worker | succeeded/failed + output |
| DELETE | `/api/v1/jobs/{id}` | owner/admin | cancel |
| POST | `/api/v1/workers/register` | worker/admin | register/update |
| POST | `/api/v1/workers/{id}/heartbeat` | worker | liveness ping |
| GET | `/api/v1/workers` | any | list |
| POST | `/api/v1/admin/users` | admin | create user |
| POST | `/api/v1/admin/keys` | admin | issue API key (plaintext shown once) |
| GET | `/api/v1/admin/keys` | admin | list keys |
| DELETE | `/api/v1/admin/keys/{id}` | admin | revoke key |
| GET | `/api/v1/admin/stats` | admin | cluster stats |

All authenticated endpoints take `Authorization: Bearer <plaintext_key>`. Admin
role can alternatively use `X-Admin-Key: <env_admin_key>` for bootstrap.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full diagram, schema, job
lifecycle, failure modes, and the recipe for adding new TCs.

## Repo layout

```
.
├── gateway/                  # FastAPI app
│   ├── app.py                # entry
│   ├── config.py             # pydantic settings
│   ├── db.py                 # asyncpg pool
│   ├── auth.py               # API key auth + roles
│   ├── rate_limit.py         # PG token bucket
│   ├── storage.py            # file upload/download
│   └── routes/               # health, files, jobs, workers, admin
├── worker/                   # Worker process
│   ├── main.py               # polling loop, signal handling
│   ├── config.py
│   ├── client.py             # async gateway client
│   ├── runner.py             # ffmpeg subprocess + progress
│   └── tc01.py               # TC01 render flow
├── shared/                   # Used by BOTH gateway + worker
│   └── renderer/
│       ├── settings.py       # pydantic models
│       └── tc01_chroma.py    # ffmpeg arg builder
├── ui/
│   └── index.html            # single-page dark UI
├── scripts/
│   ├── init_db.sql           # schema (idempotent)
│   ├── install_db.sh         # psql wrapper
│   ├── deploy_gateway.sh     # systemd installer
│   ├── deploy_worker.sh      # systemd installer
│   ├── bootstrap.sh          # first-time key setup
│   └── cleanup.sh            # hourly cron
├── tests/
│   └── test_smoke.py         # E2E (gateway + 1 worker + 1 job)
├── docs/
│   └── RUNBOOK.md            # production playbook
├── ARCHITECTURE.md
├── README.md
└── .env.example
```

## License

Same as V3_cursor (private repo for now). Add your own license here when
publishing.

## Credits

- **V3_cursor** (the original green-screen toolkit) — `github.com/lnwsj/V3_cursor`
  (private)
- **cutdee-cluster** (the gateway + worker + rate-limit + PG pattern this is
  modeled on) — already production-tested on this user's stack
- **FFmpeg** — the actual rendering engine
