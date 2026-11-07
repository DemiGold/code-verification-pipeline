"""FastAPI entry point. Wires the DB pool into the app lifespan and mounts
every router."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.database import connect_pool, close_pool

from app.routes.health import router as health_router
from app.routes.users import router as users_router
from app.routes.webhook import router as webhook_router
from app.routes.validator import router as validator_router
from app.routes.submissions import router as submissions_router
from app.routes.passport import router as passport_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: pool is ready before the first request is accepted, so no
    # route has to defend against "pool might be None yet".
    await connect_pool()
    yield
    # Shutdown: drain in-flight queries before the process exits. Without
    # this, a SIGTERM mid-query leaves the client holding a dead socket.
    await close_pool()


app = FastAPI(
    title="Verification API",
    version="0.1.0",
    description="Evidence-anchored code verification pipeline.",
    lifespan=lifespan,
)

# Order matters for OpenAPI grouping only — routes themselves are independent.
app.include_router(health_router, tags=["health"])

# Admin/seed endpoint. Candidates never hit this; the webhook creates users
# on first push.
app.include_router(users_router, tags=["users"])

# Primary write path: GitHub push webhooks.
app.include_router(webhook_router, tags=["webhook"])

app.include_router(validator_router, tags=["validator"])
app.include_router(submissions_router, tags=["submissions"])

# Read path consumed by the (not-yet-built) employer frontend.
app.include_router(passport_router, tags=["passport"])


# Deferred on purpose, not forgotten:
#
# - CORSMiddleware. Nothing crosses origin today; add it when the Next.js
#   frontend gets a real domain. Wildcard origins will bite once cookies
#   are involved, so plan for an explicit allowlist rather than ["*"].
# - TrustedHostMiddleware + security headers. Meaningful only behind a
#   reverse proxy, which we don't have yet.
# - Sentry. Its value is proportional to user count; wire it up in the same
#   commit as the first production deploy, not before.