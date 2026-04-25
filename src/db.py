"""Database connection pool for asyncpg + pgvector."""

from __future__ import annotations

import os

import asyncpg
from pgvector.asyncpg import register_vector


_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.environ.get(
            "DATABASE_URL",
            "postgresql://orchestrator:orchestrator_local@postgis:5432/orchestrator",
        )
        _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10, init=_init_conn)
    return _pool


async def _init_conn(conn: asyncpg.Connection):
    """Register pgvector type on each new connection."""
    await register_vector(conn)


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
