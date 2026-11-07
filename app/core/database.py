"""Postgres connection pool shared across the FastAPI process.

Lifecycle is tied to the app lifespan in app/main.py: connect_pool() runs on
startup, close_pool() on shutdown. Every request borrows from the same pool
rather than opening its own connection.
"""

import asyncpg
from contextlib import asynccontextmanager

from app.core.config import settings


# Module-level singleton. None before lifespan startup and again after
# shutdown — get_pool() is the only thing that should touch it.
_pool: asyncpg.Pool | None = None


async def connect_pool() -> None:
    global _pool

    # 2/10 is tuned for two constraints that will change: Supabase free-tier
    # connection cap (~15 concurrent) and the single uvicorn worker we run
    # today. Both numbers have to move together with worker count before this
    # sees real traffic.
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=10,
    )

    # TODO: replace print() with logging — it disappears under uvicorn/systemd
    # and we currently have no visibility into startup in prod logs.
    print("Database pool connected!")


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        print("Database pool closed.")


def get_pool() -> asyncpg.Pool:
    # Fails loudly instead of returning None. If this raises, main.py either
    # reordered imports or something bypassed the lifespan context — both are
    # bugs we want surfaced here, not as a NoneType error on the first query.
    if _pool is None:
        raise RuntimeError(
            "Database pool not initialized. Check the lifespan context in main.py."
        )
    return _pool


@asynccontextmanager
async def get_connection():
    """Borrow a connection, guaranteed returned even if the query crashes."""
    async with get_pool().acquire() as conn:
        yield conn


# Deferred on purpose, not forgotten:
#
# - Pool observability (in-use vs idle counts). Worth adding before the first
#   real deploy — connection leaks in asyncpg are silent, and this is how you
#   catch them. Not worth the code yet.
# - Retry-with-backoff on connect_pool(). Supabase free tier restarts on their
#   schedule, not ours. A 3-attempt retry would save a manual restart at 3am.
#   Deferred until we have actual uptime worth protecting.
# - Async SQLAlchemy migration. Raw asyncpg is faster and every query fits on
#   one screen. Revisit if a query grows past ~30 lines, or if we start
#   needing migrations generated from models.