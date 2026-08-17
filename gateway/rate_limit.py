"""
PG-backed token bucket rate limiter. Per API key, per-minute window.
"""
from __future__ import annotations
from datetime import datetime, timezone
from fastapi import HTTPException, status

from .db import get_pool
from .config import get_settings
from .auth import AuthContext


async def check_and_increment(ctx: AuthContext) -> None:
    """
    Increment the rate bucket for the given key. Raises 429 if over limit.
    Window = current minute (UTC). Each key has at most
    `rate_limit_per_minute` requests per window.
    """
    s = get_settings()
    if ctx.key_id == "env-admin":
        return  # env admin is never rate-limited

    pool = get_pool()
    now = datetime.now(timezone.utc)
    window_start = now.replace(second=0, microsecond=0)

    # UPSERT + RETURNING in a single roundtrip
    row = await pool.fetchrow(
        """
        INSERT INTO rate_buckets (api_key_id, window_start, count)
        VALUES ($1, $2, 1)
        ON CONFLICT (api_key_id, window_start)
        DO UPDATE SET count = rate_buckets.count + 1
        RETURNING count
        """,
        ctx.key_id, window_start,
    )
    count = row["count"]
    if count > s.rate_limit_per_minute:
        # Revert
        await pool.execute(
            "UPDATE rate_buckets SET count = count - 1 WHERE api_key_id = $1 AND window_start = $2",
            ctx.key_id, window_start,
        )
        retry_after = 60 - now.second
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({s.rate_limit_per_minute}/min). Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(s.rate_limit_per_minute)},
        )


async def cleanup_old_buckets(older_than_minutes: int = 120) -> int:
    """For cron — drop rate buckets older than N minutes."""
    pool = get_pool()
    result = await pool.execute(
        "DELETE FROM rate_buckets WHERE window_start < now() - ($1 || ' minutes')::interval",
        older_than_minutes,
    )
    # parse "DELETE n"
    try:
        return int(result.split()[-1])
    except Exception:
        return 0
