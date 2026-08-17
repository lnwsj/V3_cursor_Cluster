"""
Jobs: submit, claim, progress, complete, cancel, list, get.
"""
from __future__ import annotations
import uuid
import json
from datetime import datetime, timezone
from typing import Annotated, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
import asyncpg

from ..auth import AuthContext, authenticate
from ..rate_limit import check_and_increment
from ..db import get_pool
from ..config import get_settings
from shared.renderer.settings import TC01Settings

router = APIRouter(prefix="/jobs", tags=["jobs"])


# ----- Request models -----

class JobSettings(BaseModel):
    """Settings for a job. Discriminated by `tc`."""
    tc: str = Field("tc01", pattern=r"^tc\d{2}$")
    # All other settings are validated as TC01Settings (we only have TC01 in v1)
    settings: dict


class SubmitJobRequest(BaseModel):
    tc: str = Field("tc01", pattern=r"^tc01$")   # v1 only
    input_file_ids: List[str] = Field(min_length=1, max_length=10)
    settings: dict
    label: Optional[str] = None                  # free-text user label


# Re-import for rate limit
from ..rate_limit import check_and_increment


class ClaimRequest(BaseModel):
    worker_id: str


class ProgressRequest(BaseModel):
    progress_pct: float = Field(0, ge=0, le=100)
    log_append:   Optional[str] = None


class CompleteRequest(BaseModel):
    output_file_id: Optional[str] = None   # None if failed
    error:          Optional[str] = None   # set if failed
    log_append:     Optional[str] = None


# ----- Endpoints -----

@router.post("/render")
async def submit(
    req: SubmitJobRequest,
    ctx: AuthContext = Depends(authenticate),
) -> dict:
    """Submit a new render job. Uploader/admin only."""
    if ctx.role not in ("uploader", "admin"):
        raise HTTPException(403, "only uploaders can submit jobs")
    if ctx.role == "uploader" and ctx.owner_user_id is None:
        raise HTTPException(401, "key not bound to a user")
    await check_and_increment(ctx)

    # Validate settings as TC01Settings
    try:
        s = TC01Settings(**req.settings)
    except Exception as e:
        raise HTTPException(422, f"invalid settings: {e}")

    # Validate input files exist + belong to caller
    pool = get_pool()
    input_uuids: List[uuid.UUID] = []
    for fid in req.input_file_ids:
        try:
            u = uuid.UUID(fid)
        except ValueError:
            raise HTTPException(400, f"invalid file_id: {fid}")
        row = await pool.fetchrow("SELECT owner_user_id, role FROM files WHERE id = $1", u)
        if row is None:
            raise HTTPException(400, f"file {fid} not found")
        if ctx.role == "uploader" and str(row["owner_user_id"]) != ctx.owner_user_id:
            raise HTTPException(403, f"file {fid} not owned by you")
        if row["role"] not in ("original",):
            raise HTTPException(400, f"file {fid} has role={row['role']!r}, expected 'original'")
        input_uuids.append(u)

    settings_json = json.dumps(s.model_dump())

    job_id = await pool.fetchval(
        """
        INSERT INTO jobs (owner_user_id, tc, settings_json, input_file_ids, status)
        VALUES ($1, $2, $3::jsonb, $4, 'pending')
        RETURNING id
        """,
        ctx.owner_user_id, req.tc, settings_json, input_uuids,
    )
    return {
        "job_id": str(job_id),
        "status": "pending",
        "tc": req.tc,
        "input_file_ids": req.input_file_ids,
        "settings": s.model_dump(),
    }


@router.get("/{job_id}")
async def get_job(job_id: str, ctx: AuthContext = Depends(authenticate)) -> dict:
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(400, "invalid job_id")
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM jobs WHERE id = $1", jid)
    if row is None:
        raise HTTPException(404, "job not found")
    if ctx.role == "uploader" and str(row["owner_user_id"]) != ctx.owner_user_id:
        raise HTTPException(403, "not your job")
    d = dict(row)
    d["id"] = str(d["id"])
    d["owner_user_id"] = str(d["owner_user_id"]) if d["owner_user_id"] else None
    d["output_file_id"] = str(d["output_file_id"]) if d["output_file_id"] else None
    d["input_file_ids"] = [str(x) for x in (d["input_file_ids"] or [])]
    d["claimed_by_worker_id"] = d["claimed_by_worker_id"]
    for k in ("created_at", "started_at", "completed_at", "claim_expires_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


@router.get("")
async def list_jobs(
    status: Optional[str] = None,
    limit: int = 50,
    ctx: AuthContext = Depends(authenticate),
) -> dict:
    """List jobs. Uploaders see their own; workers+admin see all."""
    pool = get_pool()
    limit = max(1, min(limit, 200))
    if ctx.role == "uploader":
        rows = await pool.fetch(
            """SELECT id, tc, status, progress_pct, claimed_by_worker_id, created_at, started_at, completed_at, duration_ms, error_text
               FROM jobs WHERE owner_user_id = $1
               ORDER BY created_at DESC LIMIT $2""",
            uuid.UUID(ctx.owner_user_id), limit,
        )
    else:
        if status:
            rows = await pool.fetch(
                """SELECT id, tc, status, progress_pct, claimed_by_worker_id, created_at, started_at, completed_at, duration_ms, error_text
                   FROM jobs WHERE status = $1
                   ORDER BY created_at DESC LIMIT $2""",
                status, limit,
            )
        else:
            rows = await pool.fetch(
                """SELECT id, tc, status, progress_pct, claimed_by_worker_id, created_at, started_at, completed_at, duration_ms, error_text
                   FROM jobs
                   ORDER BY created_at DESC LIMIT $1""",
                limit,
            )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        for k in ("created_at", "started_at", "completed_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        out.append(d)
    return {"jobs": out, "count": len(out)}


@router.post("/claim")
async def claim(req: ClaimRequest, ctx: AuthContext = Depends(authenticate)) -> dict:
    """
    Worker claims the next eligible pending job. Atomic UPDATE with
    WHERE status='pending' AND (claim_expires_at IS NULL OR < now()).
    Also reaps jobs whose lease expired (claimed but no progress).
    """
    if ctx.role not in ("worker", "admin"):
        raise HTTPException(403, "only workers can claim")
    if ctx.role == "worker" and ctx.worker_id and ctx.worker_id != req.worker_id:
        raise HTTPException(403, "worker_id mismatch")

    s = get_settings()
    pool = get_pool()

    # 1. Reap expired claims
    await pool.execute(
        """UPDATE jobs SET status = 'pending', claimed_by_worker_id = NULL, claim_expires_at = NULL
           WHERE status = 'claimed' AND claim_expires_at < now()"""
    )
    # Also reap running jobs whose lease expired (worker probably died)
    await pool.execute(
        """UPDATE jobs SET status = 'pending', claimed_by_worker_id = NULL, claim_expires_at = NULL
           WHERE status = 'running' AND claim_expires_at < now()"""
    )

    # 2. Find worker's tc_filter
    worker_row = await pool.fetchrow(
        "SELECT max_parallel, current_jobs, tc_filter, status FROM workers WHERE id = $1",
        req.worker_id,
    )
    if worker_row is None:
        raise HTTPException(404, f"worker {req.worker_id} not registered (call /workers/register first)")

    if worker_row["status"] == "disabled":
        return {"job": None, "reason": "worker disabled"}
    if worker_row["current_jobs"] >= worker_row["max_parallel"]:
        return {"job": None, "reason": "worker at max_parallel"}

    tc_filter = list(worker_row["tc_filter"] or ["tc01"])

    # 3. Atomic claim: find oldest pending job matching tc_filter
    row = await pool.fetchrow(
        """
        WITH next_job AS (
            SELECT id FROM jobs
            WHERE status = 'pending' AND tc = ANY($2::text[])
            ORDER BY created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE jobs j
        SET status = 'claimed',
            claimed_by_worker_id = $1,
            claim_expires_at = now() + ($3 || ' seconds')::interval,
            started_at = COALESCE(j.started_at, now())
        FROM next_job
        WHERE j.id = next_job.id
        RETURNING j.id, j.tc, j.settings_json, j.input_file_ids, j.owner_user_id
        """,
        req.worker_id, tc_filter, str(s.worker_lease_seconds),
    )
    if row is None:
        return {"job": None, "reason": "no pending jobs"}

    # 4. Bump worker current_jobs
    await pool.execute(
        "UPDATE workers SET current_jobs = current_jobs + 1, status = 'busy' WHERE id = $1",
        req.worker_id,
    )

    return {
        "job": {
            "id": str(row["id"]),
            "tc": row["tc"],
            "settings": json.loads(row["settings_json"]) if isinstance(row["settings_json"], str) else row["settings_json"],
            "input_file_ids": [str(x) for x in (row["input_file_ids"] or [])],
            "lease_seconds": s.worker_lease_seconds,
        }
    }


@router.post("/{job_id}/progress")
async def report_progress(
    job_id: str,
    req: ProgressRequest,
    ctx: AuthContext = Depends(authenticate),
) -> dict:
    """Worker reports progress + log line."""
    if ctx.role not in ("worker", "admin"):
        raise HTTPException(403, "only workers can report progress")
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(400, "invalid job_id")
    pool = get_pool()

    # If still in 'claimed', move to 'running' and set lease again
    log_append_sql = ""
    args: list = [req.progress_pct]
    if req.log_append:
        log_append_sql = ", log_text = log_text || $3"
        args.append(req.log_append + "\n")

    await pool.execute(
        f"""
        UPDATE jobs
        SET progress_pct = $2,
            status = CASE WHEN status = 'claimed' THEN 'running' ELSE status END,
            claim_expires_at = now() + interval '120 seconds'
            {log_append_sql}
        WHERE id = $1
        """,
        jid, *args,
    )
    return {"ok": True}


@router.post("/{job_id}/complete")
async def complete(
    job_id: str,
    req: CompleteRequest,
    ctx: AuthContext = Depends(authenticate),
) -> dict:
    """Worker reports completion (success or failure)."""
    if ctx.role not in ("worker", "admin"):
        raise HTTPException(403, "only workers can complete")
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(400, "invalid job_id")
    pool = get_pool()

    # Look up job to find claimed_by_worker_id + owner_user_id
    job = await pool.fetchrow(
        "SELECT claimed_by_worker_id, status, owner_user_id FROM jobs WHERE id = $1", jid,
    )
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] in ("succeeded", "failed", "cancelled"):
        return {"ok": True, "already_finalized": True, "status": job["status"]}

    if req.output_file_id:
        try:
            output_uuid = uuid.UUID(req.output_file_id)
        except ValueError:
            raise HTTPException(400, "invalid output_file_id")
        await pool.execute(
            """
            UPDATE jobs
            SET status = 'succeeded', progress_pct = 100, completed_at = now(),
                duration_ms = EXTRACT(MILLISECOND FROM (now() - created_at))::int,
                output_file_id = $2,
                claim_expires_at = NULL,
                log_text = CASE WHEN $3::text IS NOT NULL THEN log_text || $3::text ELSE log_text END
            WHERE id = $1
            """,
            jid, output_uuid, req.log_append,
        )
        # Transfer ownership of the output file to the job's owner, so
        # the uploader can download it (worker keys have no owner_user_id).
        await pool.execute(
            "UPDATE files SET owner_user_id = $1 WHERE id = $2",
            job["owner_user_id"] if job["owner_user_id"] else None,
            output_uuid,
        )
        new_status = "succeeded"
    else:
        await pool.execute(
            """
            UPDATE jobs
            SET status = 'failed', completed_at = now(),
                duration_ms = EXTRACT(MILLISECOND FROM (now() - created_at))::int,
                error_text = $2,
                claim_expires_at = NULL,
                log_text = CASE WHEN $3::text IS NOT NULL THEN log_text || $3::text ELSE log_text END
            WHERE id = $1
            """,
            jid, req.error or "(no error message)", req.log_append,
        )
        new_status = "failed"

    # Decrement worker current_jobs
    if job["claimed_by_worker_id"]:
        await pool.execute(
            """UPDATE workers
               SET current_jobs = GREATEST(current_jobs - 1, 0),
                   status = CASE WHEN current_jobs - 1 = 0 THEN 'online' ELSE 'busy' END
               WHERE id = $1""",
            job["claimed_by_worker_id"],
        )

    return {"ok": True, "status": new_status}


@router.delete("/{job_id}")
async def cancel(job_id: str, ctx: AuthContext = Depends(authenticate)) -> dict:
    """Cancel a job. Uploader can cancel own; admin can cancel any."""
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(400, "invalid job_id")
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT owner_user_id, status, claimed_by_worker_id FROM jobs WHERE id = $1", jid,
    )
    if row is None:
        raise HTTPException(404, "job not found")
    if ctx.role == "uploader" and str(row["owner_user_id"]) != ctx.owner_user_id:
        raise HTTPException(403, "not your job")
    if row["status"] in ("succeeded", "failed", "cancelled"):
        return {"ok": True, "already_finalized": True, "status": row["status"]}

    if row["status"] in ("pending",):
        await pool.execute(
            "UPDATE jobs SET status = 'cancelled', completed_at = now() WHERE id = $1",
            jid,
        )
    else:
        # Mark cancel_requested; worker sees on next progress poll, kills ffmpeg
        await pool.execute(
            "UPDATE jobs SET cancel_requested = TRUE WHERE id = $1",
            jid,
        )

    return {"ok": True, "status": "cancelling" if row["status"] != "pending" else "cancelled"}
