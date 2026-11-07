"""GitHub push webhook — the main ingestion path for candidate submissions.

The response body is diagnostic only. GitHub ignores it, and a 2xx just means
"we received it". The authoritative state lives in the `submissions` table —
every branch below writes a row before returning, so a rejection still leaves
an audit trail. The only way nothing gets recorded is if the DB itself is
unreachable, in which case GitHub's retry policy takes over.
"""

import base64
import hashlib
import hmac
import json
from collections import OrderedDict

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import ValidationError

from app.core.config import settings
from app.core.database import get_connection
from app.models.schemas import GitHubWebhookPayload
from app.services.github import download_repo_zip
from app.services.edge_filter import validate_submission
from app.workers.tasks import process_submission

router = APIRouter()

# Must match the commit message written by post_audit_report() in
# app/services/github.py. If that message changes, this loop-prevention check
# silently stops working — the bot's own commit re-triggers the pipeline,
# generates another audit report, which re-triggers the pipeline again.
AUDIT_COMMIT_MARKER = "Verification Audit Report"

# LRU of recent delivery IDs. In-memory means it only dedupes within a single
# process — restarting the server forgets all IDs. Acceptable because GitHub's
# retry window is short (~5 min) and duplicate deliveries beyond that are rare.
_seen_deliveries: OrderedDict[str, None] = OrderedDict()
_MAX_DELIVERIES = 500


def _is_duplicate_delivery(delivery_id: str | None) -> bool:
    if not delivery_id:
        return False
    if delivery_id in _seen_deliveries:
        return True
    _seen_deliveries[delivery_id] = None
    if len(_seen_deliveries) > _MAX_DELIVERIES:
        _seen_deliveries.popitem(last=False)  # evict oldest
    return False


@router.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    x_github_delivery: str | None = Header(None, alias="X-GitHub-Delivery"),
):
    # ---- Signature verification ----
    # Must come before any parsing. If we validated the payload first, an
    # attacker could exhaust the parser or log forged data before being
    # rejected. Verify on raw bytes, before touching them.
    if not x_hub_signature_256:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")

    # Reading the raw body here means the stream is consumed — FastAPI can't
    # re-read it later. That's why we call model_validate_json(raw_body) below
    # instead of accepting a typed payload in the signature.
    raw_body = await request.body()

    secret = settings.github_webhook_secret.encode("utf-8")
    expected_sig = "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()

    # compare_digest, not ==. See the same note in app/routes/validator.
    if not hmac.compare_digest(expected_sig, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # ---- Parse ----
    try:
        payload = GitHubWebhookPayload.model_validate_json(raw_body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    # ---- Loop prevention ----
    # Four independent checks, not one. They catch different failure modes
    # and no single one covers the rest:
    #
    #   ref check        — GitHub pushes to branches/PRs/tags, not just main
    #   audit marker     — the bot's own commit re-triggers the pipeline
    #   bot username     — catches the same loop even if commit message is
    #                      rewritten (force-push, rebase) and marker is lost
    #   delivery dedupe  — GitHub retries webhooks on timeouts (~5 min window)
    #
    # Ordering matters for observability: cheap checks first, so a bot push
    # never has to hit the LRU, and non-main branches never fill it up.
    if payload.ref != "refs/heads/main":
        return {"status": "ignored", "reason": f"ref {payload.ref} is not main"}

    if payload.head_commit and AUDIT_COMMIT_MARKER in payload.head_commit.message:
        return {"status": "ignored", "reason": "audit commit (bot's own push)"}

    if settings.bot_github_username and payload.sender.login == settings.bot_github_username:
        return {"status": "ignored", "reason": "push from bot account"}

    if _is_duplicate_delivery(x_github_delivery):
        return {"status": "ignored", "reason": "duplicate delivery"}

    github_username = payload.sender.login
    repo_url = payload.repository.clone_url
    commit_sha = payload.after

    async with get_connection() as conn:
        # ---- Find or create the user ----
        # First push from a candidate creates their row. Note: a concurrent
        # second push from the same new user could hit the unique constraint
        # here before the first transaction commits. TODO: wrap in an UPSERT
        # (INSERT ... ON CONFLICT DO NOTHING / SELECT) to make this atomic.
        user_row = await conn.fetchrow(
            "SELECT id FROM users WHERE github_username = $1", github_username
        )
        if not user_row:
            user_row = await conn.fetchrow(
                "INSERT INTO users (github_username) VALUES ($1) RETURNING id",
                github_username,
            )
        user_id = user_row["id"]

        # ---- Match the repo to a challenge ----
        # Compare only the final path segment of each URL. E.g. a push to
        # github.com/alice/challenge-etl-pool-exhaustion matches the template
        # github.com/verification-hq/challenge-etl-pool-exhaustion, because both
        # end in the same slug. Anything before the last slash is ignored.
        challenge_row = await conn.fetchrow(
            """
            SELECT id, required_files
            FROM challenges
            WHERE is_active = TRUE
              AND $1 LIKE '%' || regexp_replace(repo_template_url, '^.*/', '') || '%'
            LIMIT 1
            """,
            repo_url,
        )

        # Fallback when the candidate pushes to a fork whose name doesn't
        # contain the challenge slug. Without this, forks get rejected for
        # "no active challenge" — which would silently break most real
        # submissions, since almost everyone forks first.
        #
        # Downside: if the repo name doesn't match ANY challenge AND isn't a
        # fork of one (i.e. genuinely unrelated code), the fallback still
        # attributes it to a challenge — whichever has the lowest id. The
        # edge filter will usually reject them for missing files, but the
        # attribution recorded in the DB is wrong.
        # TODO: distinguish "fork of a challenge repo" from "unrelated repo"
        # by checking the parent repo, and reject the latter explicitly.
        if not challenge_row:
            challenge_row = await conn.fetchrow(
                "SELECT id, required_files FROM challenges WHERE is_active = TRUE LIMIT 1"
            )

        if not challenge_row:
            raise HTTPException(
                status_code=500, detail="No active challenge configured in the database."
            )

        challenge_id = challenge_row["id"]
        raw_files = challenge_row["required_files"]
        # asyncpg returns JSONB as a string on some versions and as a parsed
        # list on others depending on codec registration. This handles both.
        required_files = (
            json.loads(raw_files) if isinstance(raw_files, str) else (raw_files or [])
        )

        # ---- Create the submission row ----
        # Inserted as PENDING before anything else happens, so if the edge
        # filter or the queue fails, we still have a record that a push was
        # received. Every subsequent status transition updates this row.
        submission_row = await conn.fetchrow(
            """
            INSERT INTO submissions (user_id, challenge_id, repo_url, commit_sha, status)
            VALUES ($1, $2, $3, $4, 'PENDING')
            RETURNING id
            """,
            user_id,
            challenge_id,
            repo_url,
            commit_sha,
        )
        submission_id = submission_row["id"]

        # ---- Edge filter ----
        try:
            zip_bytes = await download_repo_zip(repo_url, commit_sha)
            is_valid, message = validate_submission(zip_bytes, required_files)

            if not is_valid:
                await conn.execute(
                    "UPDATE submissions SET status = 'REJECTED_AT_EDGE', rejection_reason = $1 WHERE id = $2",
                    message,
                    submission_id,
                )
                return {"status": "rejected", "reason": message}

            # ---- Queue for Celery ----
            # Order matters: mark QUEUED before enqueueing. If enqueue fails,
            # the outer except catches it and flips to ERROR — so the submission
            # is never stuck as PENDING with no worker assigned. The reverse
            # order would leave QUEUED rows that no worker ever picks up.
            await conn.execute(
                "UPDATE submissions SET status = 'QUEUED' WHERE id = $1",
                submission_id,
            )
            zip_b64 = base64.b64encode(zip_bytes).decode("utf-8")
            process_submission.delay(str(submission_id), zip_b64)

            return {
                "status": "queued",
                "submission_id": str(submission_id),
                "message": "Passed edge validation",
            }

        except Exception as e:
            # Marks the submission ERROR for anything that threw during
            # download, edge filter, or enqueue. The raw exception string ends
            # up in rejection_reason — useful for debugging, but it also means
            # GitHub rate-limit messages and internal URLs could be surfaced
            # to whoever replays the webhook. Only logged for now, not
            # exposed to any candidate-facing surface.
            await conn.execute(
                "UPDATE submissions SET status = 'ERROR', rejection_reason = $1 WHERE id = $2",
                str(e),
                submission_id,
            )
            return {"status": "error", "reason": str(e)}