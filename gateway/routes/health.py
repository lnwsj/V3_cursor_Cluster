"""
Health + version + stats endpoint. No auth required.
"""
from fastapi import APIRouter
from datetime import datetime, timezone
import asyncpg

from ..db import get_pool

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness + DB ping + counts."""
    pool = get_pool()
    db_ok = True
    try:
        await pool.fetchval("SELECT 1")
    except Exception:
        db_ok = False

    counts = {}
    if db_ok:
        try:
            row = await pool.fetchrow(
                """
                SELECT
                  (SELECT count(*) FROM jobs WHERE status = 'pending')   AS pending,
                  (SELECT count(*) FROM jobs WHERE status = 'running')   AS running,
                  (SELECT count(*) FROM jobs WHERE status = 'succeeded') AS succeeded,
                  (SELECT count(*) FROM jobs WHERE status = 'failed')    AS failed,
                  (SELECT count(*) FROM workers WHERE status IN ('online','busy')) AS online_workers
                """
            )
            counts = dict(row)
        except Exception:
            pass

    return {
        "status": "ok" if db_ok else "degraded",
        "service": "v3cursor-cluster-gateway",
        "version": "1.0.0",
        "ts": datetime.now(timezone.utc).isoformat(),
        **counts,
    }
