# V3_cursor_Cluster — Production Runbook

> Step-by-step playbook for deploying, operating, and troubleshooting the
> cluster on real machines.

## Architecture recap

```
1× Gateway (FastAPI + PostgreSQL)  ←  N× Workers (ffmpeg + GPU)
   - state, files, auth, rate limit      - poll /jobs/claim every 3s
   - one DB for whole cluster            - render + report progress
   - HTTP upload/download                - heartbeat every 10s
```

**Capacity model:**
- Gateway is stateless apart from DB. Can be HA-replicated (gateway-2 + same
  PG) without code changes.
- Workers are independent. Add as many as you have GPU machines.
- Throughput = Σ (workers × max_parallel) × per-job-time. With 2× RTX 3050
  + 1× RTX 2050 at max_parallel=2 each, you can finish ~5-6 TC01 jobs in
  parallel.

## Step-by-step: 1-gateway + 2-workers on Tailscale

### 0. Prereqs (all machines)

- Ubuntu 22.04+ (or any systemd Linux)
- Python 3.10+ (3.12 OK — the `urllib` gotcha from cutdee-cluster doesn't
  apply here, we use httpx)
- ffmpeg 5+ (system or bundled)
- Outbound HTTPS to gateway (Tailscale works great)

### 1. Postgres (on gateway machine)

```bash
sudo apt install postgresql
sudo -u postgres createuser -s v3cluster
sudo -u postgres createdb -O v3cluster v3cluster
# Optional: PgBouncer for connection pooling
sudo apt install pgbouncer
```

### 2. Gateway

```bash
# Clone
git clone https://github.com/lnwsj/V3_cursor_Cluster.git
cd V3_cursor_Cluster
cp .env.example .env
# Edit .env
nano .env
# - ADMIN_API_KEY=<secrets.token_hex(32)>
# - DATABASE_URL=postgresql://v3cluster:v3cluster@127.0.0.1:5432/v3cluster
# - STORAGE_ROOT=/opt/v3cluster/storage
# - GATEWAY_PUBLIC_URL=http://100.90.235.15:8770   (Tailscale IP)

# Install
python3 -m venv .venv && source .venv/bin/activate
pip install -r gateway/requirements.txt

# Deploy (systemd)
sudo bash scripts/deploy_gateway.sh

# Verify
curl -s http://localhost:8770/api/v1/health | jq
# → {"status":"ok", "pending":0, "running":0, ...}
```

### 3. Bootstrap an uploader key

```bash
ADMIN_API_KEY=... GATEWAY_PUBLIC_URL=http://localhost:8770 \
  bash scripts/bootstrap.sh sj88-default
```

Copy the printed plaintext `v3c_...` key. It will NOT be shown again.

### 4. Issue a worker key (per worker)

```bash
curl -sS -X POST http://localhost:8770/api/v1/admin/keys \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"role":"worker","label":"rtx3050-01","worker_id":"rtx3050-01"}' | jq .plaintext
# → "v3c_abcdef..."
```

Repeat for each worker with a unique `worker_id`.

### 5. Deploy each worker

On the GPU machine:

```bash
git clone https://github.com/lnwsj/V3_cursor_Cluster.git
cd V3_cursor_Cluster

WORKER_ID=rtx3050-01 \
WORKER_LABEL="RTX 3050 8GB" \
WORKER_API_KEY=v3c_abcdef... \
GATEWAY_URL=http://100.90.235.15:8770 \
WORKER_MAX_PARALLEL=2 \
sudo bash scripts/deploy_worker.sh
```

The worker:
1. Registers with the gateway
2. Sends heartbeats every 10s
3. Polls /jobs/claim every 3s
4. When a job arrives, downloads inputs, runs ffmpeg, uploads output

### 6. Verify cluster

Open the UI: `http://100.90.235.15:8770`
- **Settings** → paste uploader key → Save
- **Cluster** tab → should show 2 workers online
- **Render** tab → upload a green-screen product + background → Render

Check gateway log:
```bash
journalctl -u v3cluster-gateway -f
```

Check worker log:
```bash
journalctl -u v3cluster-worker -f
```

## Operations

### Add a new worker (any time)

Issue a new worker key on gateway (step 4), then run `deploy_worker.sh` on
the new machine. No gateway restart needed.

### Remove a worker

```bash
# On the worker
sudo systemctl stop v3cluster-worker

# Optional: revoke the key on gateway
curl -sS -X DELETE http://localhost:8770/api/v1/admin/keys/<key_id> \
  -H "X-Admin-Key: $ADMIN_API_KEY"
```

In-flight jobs will be reaped after `claim_expires_at` (default 120s) and
claimed by remaining workers.

### Restart gateway (zero-downtime if you have a LB)

```bash
sudo systemctl restart v3cluster-gateway
# If behind a load balancer, this is transparent — workers will reconnect
# on next poll. In-flight jobs are unaffected (DB is source of truth).
```

### Restart worker

```bash
sudo systemctl restart v3cluster-worker
# Current job (if any) will fail or be reaped by another worker
# depending on timing. Typical case: reaped + re-claimed = 0 data loss.
```

### Manual cleanup

```bash
# Force-reap stuck jobs
PGPASSWORD=... psql -U v3cluster -h localhost v3cluster -c "
  UPDATE jobs SET status='pending', claimed_by_worker_id=NULL, claim_expires_at=NULL
  WHERE status IN ('claimed','running') AND claim_expires_at < now();
"

# Drop all rate-limit buckets
PGPASSWORD=... psql -U v3cluster -h localhost v3cluster -c "TRUNCATE rate_buckets;"

# Drop old completed jobs + their outputs (see scripts/cleanup.sh)
```

### Monitoring

```bash
# Cluster health
curl -s http://localhost:8770/api/v1/health | jq

# Job counts by status
PGPASSWORD=... psql -U v3cluster -h localhost v3cluster -c "
  SELECT status, count(*) FROM jobs GROUP BY status ORDER BY status;
"

# Worker load
PGPASSWORD=... psql -U v3cluster -h localhost v3cluster -c "
  SELECT id, label, status, current_jobs, max_parallel,
         EXTRACT(EPOCH FROM (now() - last_heartbeat_at))::int AS hb_age_sec
  FROM workers ORDER BY label;
"

# Recent heartbeats (per worker)
PGPASSWORD=... psql -U v3cluster -h localhost v3cluster -c "
  SELECT worker_id, ts, current_jobs, gpu_util_pct, mem_used_mb, status
  FROM heartbeats WHERE ts > now() - interval '5 minutes'
  ORDER BY ts DESC LIMIT 50;
"
```

## Common gotchas

### Worker says "401 Unauthorized" on first poll

- The worker key may not be issued yet, or its `worker_id` in DB doesn't match
  `WORKER_ID` env. Check:
  ```sql
  SELECT * FROM api_keys WHERE role='worker' AND revoked_at IS NULL;
  ```

### Worker registers but never claims jobs

- Check `workers.tc_filter` includes `'tc01'`
- Check `jobs.status` is `'pending'`
- Check `workers.status` is `'online'` (gateway auto-marks offline if no
  heartbeat in 60s)

### Job stays in "claimed" forever

- Worker crashed mid-render. Wait 120s (default lease) — gateway will reap and
  another worker will claim.
- To force-reap now: `UPDATE jobs SET status='pending', claim_expires_at=NULL
  WHERE id='<job_id>';`

### Render fails with "ffmpeg exit code 1"

- Check the worker's `log_text` (UI: My Jobs → Detail → Log) for the actual
  ffmpeg error
- Common: encoder not available (e.g. `h264_nvenc` on CPU-only machine).
  Switch encoder in the UI.

### UI shows "Gateway unreachable"

- Check `GATEWAY_PUBLIC_URL` in the UI Settings tab matches the gateway
- From the browser's network, can you `curl` the URL?
- If behind Tailscale, is the browser also on Tailscale?

### Out of disk on gateway

- Outputs grow: a typical 24s product at 6Mbps = ~18MB per output
- Run `cleanup.sh` (cron) — drops 7-day-old jobs and their files
- Or just clear: `rm -rf /opt/v3cluster/storage/output/*`

## Capacity math

| Worker | GPU | TC01 24s render | max_parallel | Throughput |
|---|---|---|---|---|
| RTX 3050 | sm_86 / 8GB | ~5s | 2 | 9.6 jobs/min |
| RTX 2050 mobile | sm_86 / 4GB | ~8s | 1 | 7.5 jobs/min |
| M4 Mac (MLX CPU) | — | ~12s | 4 | 20 jobs/min |
| CPU only (no NVENC) | — | ~30s | 1 | 2 jobs/min |

Adjust `WORKER_MAX_PARALLEL` based on VRAM. Rule of thumb:
- 4GB VRAM → 1 job
- 8GB VRAM → 2 jobs
- 12GB+ VRAM → 3-4 jobs

## HA: deploying gateway-2

1. On a second machine, repeat steps 1-3 (Postgres + gateway)
2. **Point both gateways at the same PostgreSQL** (DATABASE_URL)
3. Both bind to `:8770` but on different IPs (or use a CF Load Balancer)
4. Workers connect to LB; LB round-robins
5. UI uses the LB URL
6. **No code changes needed.** Same `init_db.sql` is idempotent.

Caveat: in-flight jobs in the gateway process's memory don't share state
across the 2 gateways, but the DB does, so the worst case is: a job gets
`claimed` on gateway-1, then gateway-1 dies, the job stays `claimed` until
the lease expires (120s), then any gateway reaps and another worker claims.
Acceptable for green-screen workloads.
