"""
API key auth + role check.

Keys are stored as sha256 hashes. The plaintext is shown to the user ONCE at
creation and never again. Workers + uploaders authenticate via `Authorization:
Bearer <plaintext>` header.

Roles:
- admin:    full access, can create keys
- uploader: can upload files, submit jobs, view own jobs
- worker:   can heartbeat, claim jobs, download inputs, upload outputs
"""
from __future__ import annotations
import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional
from fastapi import Header, HTTPException, status

from .db import get_pool
from .config import get_settings


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str]:
    """Returns (plaintext, hash). Plaintext is 40 hex chars (20 bytes)."""
    plaintext = "v3c_" + secrets.token_hex(20)   # 44 chars total
    return plaintext, hash_key(plaintext)


@dataclass
class AuthContext:
    key_id: str
    role: str
    owner_user_id: Optional[str]  # None for worker keys
    worker_id: Optional[str]       # set for role=worker


async def authenticate(
    authorization: Optional[str] = Header(None),
    x_admin_key:   Optional[str] = Header(None, alias="X-Admin-Key"),
) -> AuthContext:
    """
    FastAPI dependency. Accepts:
    - Authorization: Bearer <key>
    - X-Admin-Key: <key>           (only for admin role)

    For workers, also accepts ?worker_id=... for logging context only (auth
    is still by key).
    """
    s = get_settings()
    plaintext: Optional[str] = None

    if authorization and authorization.lower().startswith("bearer "):
        plaintext = authorization[7:].strip()
    elif x_admin_key:
        plaintext = x_admin_key.strip()

    # Bootstrap: env admin key grants admin role even if not in DB
    if plaintext and plaintext == s.admin_api_key:
        return AuthContext(
            key_id="env-admin", role="admin",
            owner_user_id=None, worker_id=None,
        )

    if not plaintext:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key (Authorization: Bearer ... or X-Admin-Key)",
        )

    key_hash = hash_key(plaintext)
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, role, owner_user_id, worker_id, revoked_at
        FROM api_keys
        WHERE key_hash = $1
        """,
        key_hash,
    )
    if row is None or row["revoked_at"] is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
        )

    # Best-effort last_used_at update (don't block on it)
    try:
        await pool.execute(
            "UPDATE api_keys SET last_used_at = now() WHERE id = $1",
            row["id"],
        )
    except Exception:
        pass

    return AuthContext(
        key_id=str(row["id"]),
        role=row["role"],
        owner_user_id=str(row["owner_user_id"]) if row["owner_user_id"] else None,
        worker_id=row["worker_id"],
    )


def require_role(*allowed: str):
    """Returns a dependency that checks the auth context's role."""
    async def _dep(ctx: AuthContext = None) -> AuthContext:  # type: ignore[assignment]
        if ctx is None:
            raise HTTPException(500, "auth context not injected")
        if ctx.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {list(allowed)}; you have: {ctx.role}",
            )
        return ctx
    return _dep
