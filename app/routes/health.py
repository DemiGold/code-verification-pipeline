"""Liveness and readiness probes.

/health answers "is this process alive?" — never fails unless the process
is truly dead. Use for container restart policies.

/ready answers "can this instance serve traffic?" — pings Postgres and Redis.
Use for load balancer routing. A failure here pulls the instance out of
rotation without triggering a restart, because the instance itself is fine.

Keeping these separate prevents the common failure mode where a Supabase
blip restarts every healthy container, turning a small outage into a big one.
"""

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, status

from app.core.config import settings
from app.core.database import get_connection

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}


@router.get("/ready")
async def ready() -> dict:
    checks: dict[str, str] = {}

    try:
        async with get_connection() as conn:
            await conn.execute("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as e:
        # Only the exception class name — the message can contain the
        # connection string, which we don't want leaking through a public probe.
        checks["postgres"] = f"fail: {e.__class__.__name__}"

    try:
        # Matches the SSL workaround in app/workers/celery_app.py — Windows
        # can't validate Upstash's cert chain. Drop ssl_cert_reqs=None on Linux.
        client = aioredis.from_url(settings.redis_url, ssl_cert_reqs=None)
        try:
            await client.ping()
        finally:
            await client.aclose()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"fail: {e.__class__.__name__}"

    if not all(v == "ok" for v in checks.values()):
        # 503 tells the load balancer to stop routing here without implying
        # the process is broken.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unavailable", "checks": checks},
        )

    return {"status": "ok", "checks": checks}