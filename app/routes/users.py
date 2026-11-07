"""Admin-only user creation.

Candidates and validators get their rows via the GitHub webhook and a manual
DB seed respectively. This endpoint exists for testing and for bootstrapping
a database that hasn't seen a webhook yet.
"""

import asyncpg
from fastapi import APIRouter, HTTPException, Depends

from app.core.database import get_connection
from app.models.schemas import UserCreate
from app.routes.validator import verify_token

router = APIRouter()


@router.post("/api/users")
async def create_user(user: UserCreate, token: str = Depends(verify_token)):
    # verify_token in Depends() means FastAPI runs the auth check before this
    # function body — an invalid token never reaches the INSERT below.
    async with get_connection() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO users (github_username, email, display_name)
                VALUES ($1, $2, $3)
                RETURNING id, github_username, email, role, created_at
                """,
                user.github_username,
                user.email,
                user.display_name,
            )
            return dict(row)
        except asyncpg.UniqueViolationError:
            # Only this case is a client error — the username they chose is
            # already taken. A generic except here would mask connection
            # failures as 400s and leak the constraint name to the caller.
            raise HTTPException(
                status_code=409,
                detail=f"github_username '{user.github_username}' is already registered",
            )
        except Exception:
            # Everything else (pool timeout, connection drop, bad SQL) is
            # our problem, not theirs — 500 with a generic message so the
            # caller can't probe the schema via error text.
            raise HTTPException(status_code=500, detail="Failed to create user")