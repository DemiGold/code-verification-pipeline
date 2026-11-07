"""Direct .zip upload — an alternative entry point that bypasses the GitHub
webhook.

The webhook path downloads the repo from GitHub; this one takes the zip
straight from the caller. Everything after ingestion — edge filter, Docker
sandbox, audit report — is identical. Primarily used to test the pipeline
without setting up a real repo and webhook, but it also gives a manual
override path for support cases.

Requires the validator token: without it, anyone could flood the Celery queue
with junk zips. Rate limiting per token is deferred (see TODO below).
"""

import base64
import json

from fastapi import APIRouter, File, Form, UploadFile, HTTPException, Depends

from app.core.database import get_connection
from app.routes.validator import verify_token
from app.services.edge_filter import validate_submission
from app.workers.tasks import process_submission

router = APIRouter()


@router.post("/api/submissions/upload")
async def upload_submission(
    github_username: str = Form(...),
    challenge_slug: str = Form(...),
    email: str = Form(None),
    file: UploadFile = File(...),
    token: str = Depends(verify_token),
):
    # Filename check before reading the body. A 5MB zip is cheap to download;
    # a 5GB one isn't. Extension check is not a security boundary — the edge
    # filter does the real validation — but it saves bandwidth on obvious junk.
    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a .zip archive")

    # read() buffers the whole file into RAM. The 5MB cap lives in the edge
    # filter below, which means we buffer up to 5MB before rejecting — fine
    # for this scale, but a streaming size check would be the right fix if
    # we ever accepted large files.
    zip_bytes = await file.read()

    async with get_connection() as conn:
        # ---- Resolve the challenge ----
        # Unlike the webhook, which infers the challenge from the repo URL,
        # this endpoint takes it explicitly. That's the whole point of the
        # direct-upload path: you pick the challenge, not the repo name.
        challenge_row = await conn.fetchrow(
            "SELECT id, required_files FROM challenges WHERE slug = $1 AND is_active = TRUE",
            challenge_slug,
        )
        if not challenge_row:
            raise HTTPException(
                status_code=404,
                detail=f"Challenge '{challenge_slug}' not found or is inactive.",
            )

        challenge_id = challenge_row["id"]
        raw_files = challenge_row["required_files"]
        # asyncpg returns JSONB as a string on some versions and as a parsed
        # list on others — same defensive branch as webhook.py.
        required_files = (
            json.loads(raw_files) if isinstance(raw_files, str) else (raw_files or [])
        )

        # ---- Edge filter ----
        # Same function the webhook uses, just called synchronously instead of
        # after downloading a repo. Rejecting here means nothing reaches the
        # queue, so a bad zip costs us bandwidth only, not CPU.
        is_valid, message = validate_submission(zip_bytes, required_files)
        if not is_valid:
            raise HTTPException(
                status_code=400, detail=f"Edge validation failed: {message}"
            )

        # ---- Find or create the user ----
        # The direct-upload path accepts an optional email so the audit report
        # can actually be sent. The webhook path has no email — it relies on
        # a later OAuth flow to populate it. This is the only ingestion path
        # where a candidate can get a report without linking GitHub.
        user_row = await conn.fetchrow(
            "SELECT id FROM users WHERE github_username = $1", github_username
        )

        if not user_row:
            user_row = await conn.fetchrow(
                "INSERT INTO users (github_username, email) VALUES ($1, $2) RETURNING id",
                github_username,
                email,
            )
        else:
            # Existing user: only overwrite email if a new one was supplied.
            # Without the `if email`, passing email=None would blank out a
            # previously-set address.
            if email:
                await conn.execute(
                    "UPDATE users SET email = $1 WHERE id = $2", email, user_row["id"]
                )

        user_id = user_row["id"]

        # ---- Create the submission ----
        # Inserted directly as QUEUED, unlike webhook.py which inserts as
        # PENDING then transitions. Safe here because the edge filter already
        # passed above — there's no failure window between insert and queue.
        # repo_url and commit_sha are placeholders so the DB schema stays
        # consistent with webhook-ingested rows.
        submission_row = await conn.fetchrow(
            """
            INSERT INTO submissions (user_id, challenge_id, repo_url, commit_sha, status)
            VALUES ($1, $2, $3, $4, 'QUEUED')
            RETURNING id
            """,
            user_id,
            challenge_id,
            "Direct Upload",
            "manual_upload",
        )
        submission_id = submission_row["id"]

    # ---- Enqueue ----
    # Called outside the `async with` so the DB connection returns to the pool
    # before we block on Redis. If .delay() throws (Redis unreachable, payload
    # too large for the broker, etc.), the submission is already committed as
    # QUEUED but no worker will ever pick it up — the row is orphaned.
    #
    # TODO: wrap this in try/except and UPDATE status to 'ERROR' on failure,
    # matching the pattern in webhook.py. Right now a Redis outage silently
    # strands every direct upload with no error record.
    zip_b64 = base64.b64encode(zip_bytes).decode("utf-8")
    process_submission.delay(str(submission_id), zip_b64)

    return {
        "status": "queued",
        "submission_id": str(submission_id),
        "message": f"Passed edge validation and queued for {challenge_slug}.",
    }


# Deferred on purpose, not forgotten:
#
# - Rate limiting per token. The shared secret stops anonymous spam, but a
#   leaked token allows unlimited uploads. A simple Redis counter (N uploads
#   per minute per token) would close this without needing per-user auth.
# - Enqueue failure handling. See the TODO inline above — the current code
#   leaves orphaned QUEUED rows if Celery rejects the task.
# - Streaming size check. file.read() buffers everything before the edge
#   filter rejects it. A Content-Length check before read() would be cheaper,
#   but UploadFile doesn't expose that reliably across ASGI servers.