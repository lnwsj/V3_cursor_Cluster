"""
asyncpg pool + helpers.
"""
from __future__ import annotations
import asyncpg
from typing import Optional
from .config import get_settings

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    s = get_settings()
    _pool = await asyncpg.create_pool(
        dsn=s.database_url,
        min_size=2,
        max_size=20,
        command_timeout=60,
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool() first")
    return _pool
