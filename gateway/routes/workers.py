"""
Worker registration, heartbeat, listing.
"""
from __future__ import annotations
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Annotated, List, Optional
import asyncpg

from ..auth import AuthContext, authenticate
from ..db import get_pool

router = APIRouter(prefix="/workers", tags=["workers"])


class RegisterRequest(BaseModel):
    worker_id:    str = Field(min_length=1, max_length=64)
    label:        str = Field(min_length=1, max_length=128)
    gpu_label:    Optional[str] = None
    max_parallel: int = Field(1, ge=1, le=16)
    tc_filter:    List[str] = Field(default_factory=lambda: ["tc01"])


class HeartbeatRequest(BaseModel):
    current_jobs: int = Field(0, ge=0)
    gpu_util_pct: Optional[float] = Field(None, ge=0, le=100)
    mem_used_mb:  Optional[int]  = None
    status:       str = Field("online", pattern=r"^(online|busy)$")


@router.post("/register")
async def register(req: RegisterRequest, ctx: AuthContext = Depends(authenticate)) -> dict:
    """Register (or update) a worker. Caller must have a worker key."""
    if ctx.role not in ("worker", "admin"):
        raise HTTPException(403, "only workers can register")

    # Worker keys are bound to worker_id at creation; verify if worker role
    if ctx.role == "worker" and ctx.worker_id and ctx.worker_id != req.worker_id:
        raise HTTPException(403, f"this key is bound to worker_id={ctx.worker_id!r}, not {req.worker_id!r}")

    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO workers (id, label, gpu_label, max_parallel, tc_filter, last_heartbeat_at, status)
        VALUES ($1, $2, $3, $4, $5, now(), 'online')
        ON CONFLICT (id) DO UPDATE SET
            label          = EXCLUDED.label,
            gpu_label      = EXCLUDED.gpu_label,
            max_parallel   = EXCLUDED.max_parallel,
            tc_filter      = EXCLUDED.tc_filter,
            last_heartbeat_at = now(),
            status         = 'online'
        """,
        req.worker_id, req.label, req.gpu_label, req.max_parallel, req.tc_filter,
    )
    return {"ok": True, "worker_id": req.worker_id}


@router.post("/{worker_id}/heartbeat")
async def heartbeat(worker_id: str, req: HeartbeatRequest, ctx: AuthContext = Depends(authenticate)) -> dict:
    if ctx.role not in ("worker", "admin"):
        raise HTTPException(403, "only workers can heartbeat")
    if ctx.role == "worker" and ctx.worker_id and ctx.worker_id != worker_id:
        raise HTTPException(403, "worker_id mismatch")

    pool = get_pool()
    # Update worker current_jobs + heartbeat
    await pool.execute(
        """
        UPDATE workers
        SET current_jobs = $2, last_heartbeat_at = now(), status = $3
        WHERE id = $1
        """,
        worker_id, req.current_jobs, req.status,
    )
    # Append to history
    await pool.execute(
        """
        INSERT INTO heartbeats (worker_id, current_jobs, gpu_util_pct, mem_used_mb, status)
        VALUES ($1, $2, $3, $4, $5)
        """,
        worker_id, req.current_jobs, req.gpu_util_pct, req.mem_used_mb, req.status,
    )
    return {"ok": True}


@router.get("")
async def list_workers(ctx: AuthContext = Depends(authenticate)) -> dict:
    """List all known workers + their last heartbeat age + current job count."""
    pool = get_pool()
    # Mark workers offline if no heartbeat in last 60s
    await pool.execute(
        "UPDATE workers SET status = 'offline' WHERE status IN ('online','busy') AND last_heartbeat_at < now() - interval '60 seconds'"
    )
    rows = await pool.fetch(
        """
        SELECT id, label, gpu_label, max_parallel, current_jobs, last_heartbeat_at, status, tc_filter,
               EXTRACT(EPOCH FROM (now() - last_heartbeat_at))::int AS heartbeat_age_sec
        FROM workers
        ORDER BY label
        """
    )
    workers = [dict(r) for r in rows]
    for w in workers:
        for k in ("last_heartbeat_at",):
            if w.get(k) is not None:
                w[k] = w[k].isoformat()
        if w.get("tc_filter") is not None:
            w["tc_filter"] = list(w["tc_filter"])
    return {"workers": workers}


@router.get("/{worker_id}")
async def get_worker(worker_id: str, ctx: AuthContext = Depends(authenticate)) -> dict:
    pool = get_pool()
    row = await pool.fetchrow(
        """SELECT id, label, gpu_label, max_parallel, current_jobs, last_heartbeat_at, status, tc_filter,
                  EXTRACT(EPOCH FROM (now() - last_heartbeat_at))::int AS heartbeat_age_sec
           FROM workers WHERE id = $1""",
        worker_id,
    )
    if row is None:
        raise HTTPException(404, "worker not found")
    d = dict(row)
    if d.get("last_heartbeat_at"):
        d["last_heartbeat_at"] = d["last_heartbeat_at"].isoformat()
    if d.get("tc_filter") is not None:
        d["tc_filter"] = list(d["tc_filter"])
    return d
