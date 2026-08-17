"""
Admin endpoints: create users, issue API keys, view stats.
Admin role only (or env-admin via X-Admin-Key).
"""
from __future__ import annotations
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Annotated, Optional
import asyncpg

from ..auth import AuthContext, authenticate, generate_key, hash_key
from ..db import get_pool
from ..rate_limit import cleanup_old_buckets

router = APIRouter(prefix="/admin", tags=["admin"])


class CreateUserRequest(BaseModel):
    email: str
    plan: str = Field("free", pattern=r"^(free|pro|enterprise)$")


class CreateKeyRequest(BaseModel):
    role: str = Field(pattern=r"^(uploader|worker)$")
    label: str = Field(min_length=1, max_length=128)
    worker_id: Optional[str] = None   # required if role=worker
    owner_email: Optional[str] = None  # required if role=uploader; creates user if missing


class RevokeKeyRequest(BaseModel):
    key_id: str


def _require_admin(ctx: AuthContext) -> AuthContext:
    if ctx.role != "admin":
        raise HTTPException(403, "admin only")
    return ctx


@router.post("/users")
async def create_user(req: CreateUserRequest, ctx: AuthContext = Depends(authenticate)) -> dict:
    _require_admin(ctx)
    pool = get_pool()
    try:
        uid = await pool.fetchval(
            "INSERT INTO users (email, plan) VALUES ($1, $2) RETURNING id",
            req.email, req.plan,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "user already exists")
    return {"id": str(uid), "email": req.email, "plan": req.plan}


@router.get("/users")
async def list_users(ctx: AuthContext = Depends(authenticate)) -> dict:
    _require_admin(ctx)
    pool = get_pool()
    rows = await pool.fetch("SELECT id, email, plan, credits, created_at FROM users ORDER BY created_at DESC")
    return {"users": [{**dict(r), "id": str(r["id"]), "created_at": r["created_at"].isoformat()} for r in rows]}


@router.post("/keys")
async def create_key(req: CreateKeyRequest, ctx: AuthContext = Depends(authenticate)) -> dict:
    _require_admin(ctx)
    pool = get_pool()

    owner_uuid = None
    if req.role == "uploader":
        if not req.owner_email:
            raise HTTPException(400, "owner_email required for uploader keys")
        # Find or create user
        row = await pool.fetchrow("SELECT id FROM users WHERE email = $1", req.owner_email)
        if row is None:
            owner_uuid = await pool.fetchval(
                "INSERT INTO users (email) VALUES ($1) RETURNING id", req.owner_email,
            )
        else:
            owner_uuid = row["id"]
    elif req.role == "worker":
        if not req.worker_id:
            raise HTTPException(400, "worker_id required for worker keys")

    plaintext, key_hash = generate_key()
    kid = await pool.fetchval(
        """
        INSERT INTO api_keys (key_hash, role, owner_user_id, worker_id, label)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        key_hash, req.role, owner_uuid, req.worker_id, req.label,
    )
    return {
        "id": str(kid),
        "role": req.role,
        "plaintext": plaintext,   # shown ONCE — never again
        "label": req.label,
        "worker_id": req.worker_id,
        "owner_email": req.owner_email,
    }


@router.get("/keys")
async def list_keys(ctx: AuthContext = Depends(authenticate)) -> dict:
    _require_admin(ctx)
    pool = get_pool()
    rows = await pool.fetch(
        """SELECT k.id, k.role, k.label, k.worker_id, k.created_at, k.last_used_at, k.revoked_at,
                  u.email AS owner_email
           FROM api_keys k LEFT JOIN users u ON u.id = k.owner_user_id
           ORDER BY k.created_at DESC"""
    )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        for k in ("created_at", "last_used_at", "revoked_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        out.append(d)
    return {"keys": out}


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: str, ctx: AuthContext = Depends(authenticate)) -> dict:
    _require_admin(ctx)
    try:
        kid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(400, "invalid key_id")
    pool = get_pool()
    result = await pool.execute(
        "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
        kid,
    )
    if result.endswith("0"):
        raise HTTPException(404, "key not found or already revoked")
    return {"ok": True}


@router.post("/rate-buckets/cleanup")
async def rate_cleanup(older_than_minutes: int = 120, ctx: AuthContext = Depends(authenticate)) -> dict:
    _require_admin(ctx)
    n = await cleanup_old_buckets(older_than_minutes)
    return {"deleted": n}


@router.get("/stats")
async def stats(ctx: AuthContext = Depends(authenticate)) -> dict:
    _require_admin(ctx)
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM users) AS users,
            (SELECT count(*) FROM api_keys WHERE revoked_at IS NULL) AS active_keys,
            (SELECT count(*) FROM files) AS files,
            (SELECT count(*) FROM jobs) AS jobs,
            (SELECT count(*) FROM jobs WHERE status = 'succeeded') AS jobs_succeeded,
            (SELECT count(*) FROM jobs WHERE status = 'failed') AS jobs_failed,
            (SELECT count(*) FROM workers) AS workers,
            (SELECT count(*) FROM workers WHERE status IN ('online','busy')) AS workers_online
        """
    )
    return dict(row)
