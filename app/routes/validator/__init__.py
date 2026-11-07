"""Validator endpoints, plus the shared bearer-token dependency.

verify_token lives here and is imported by users.py and submissions.py — this
file is not just a route module, it's also where the auth check for other
admin endpoints is defined. If a fourth consumer appears, move it to
app/core/auth.py.

Auth model today: a single shared secret in VALIDATOR_TOKEN. Anyone with that
token can verify any submission. The per-user role check below is what keeps
the audit trail meaningful — the token gets you in the door, the role lookup
decides whose UUID gets stamped on the record.
"""

import hmac
from typing import Literal

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.core.config import settings
from app.core.database import get_connection

router = APIRouter()
security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    # compare_digest, not ==. A plain == short-circuits on the first byte that
    # differs, so response time leaks how many characters of the token were
    # correct — enough to brute-force it one byte at a time.
    if not hmac.compare_digest(credentials.credentials, settings.validator_token):
        raise HTTPException(status_code=401, detail="Invalid validator token")
    return credentials.credentials


class VerificationRequest(BaseModel):
    submission_id: str

    # Must match a VALIDATOR or ADMIN row in users. Lookup is below.
    validator_username: str

    validator_notes: str

    # Bounded-claim field. Stored on the Passport so employers see what was
    # NOT verified alongside what was.
    known_limitations: str | None = None

    # Literal, not str. Pydantic rejects anything outside these two at the
    # request boundary instead of letting a typo reach the DB and fail on the
    # enum cast.
    status: Literal["VERIFIED", "REJECTED"] = "VERIFIED"


@router.post("/api/validator/verify")
async def verify_submission(
    payload: VerificationRequest,
    token: str = Depends(verify_token),
):
    async with get_connection() as conn:
        # The token only proves "someone with the shared secret is calling".
        # It says nothing about WHO. This lookup is what makes the audit trail
        # meaningful: verified_by needs a real UUID, not a string the caller
        # typed. Reject unknown users before anything else so a bad username
        # can't reach the UPDATE.
        validator = await conn.fetchrow(
            "SELECT id, role FROM users WHERE github_username = $1",
            payload.validator_username,
        )
        if not validator:
            raise HTTPException(status_code=404, detail="Validator username not found")
        if validator["role"] not in ("VALIDATOR", "ADMIN"):
            raise HTTPException(
                status_code=403,
                detail="User is not authorized to verify submissions",
            )

        # Only PASSED submissions can transition to VERIFIED or REJECTED.
        # Everything else (QUEUED, RUNNING, FAILED, already VERIFIED) is either
        # not defended yet or already closed — the defence happens once.
        submission = await conn.fetchrow(
            "SELECT id, status FROM submissions WHERE id = $1",
            payload.submission_id,
        )
        if not submission:
            raise HTTPException(status_code=404, detail="Submission not found")
        if submission["status"] != "PASSED":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot verify. Submission is currently {submission['status']}.",
            )

        # The status = 'PASSED' guard in the WHERE clause is intentional. The
        # SELECT above checked status, but between that check and this UPDATE,
        # a second validator could have submitted first. Without the guard,
        # the second write silently overwrites the first's notes and verified_by
        # and the audit trail has no record the collision happened.
        #
        # asyncpg returns "UPDATE N" — N=0 means the row no longer matched.
        result = await conn.execute(
            """
            UPDATE submissions
            SET status = $1,
                defence_notes = $2,
                verified_at = NOW(),
                verified_by = $3,
                known_limitations = $4
            WHERE id = $5 AND status = 'PASSED'
            """,
            payload.status,
            payload.validator_notes,
            validator["id"],
            payload.known_limitations,
            payload.submission_id,
        )
        if result == "UPDATE 0":
            raise HTTPException(
                status_code=409,
                detail="Submission was modified while you were reviewing it. Refresh and try again.",
            )

        return {
            "status": "success",
            "message": f"Submission {payload.submission_id} marked as {payload.status}.",
            "verified_by": payload.validator_username,
        }