"""Celery task that processes a single submission end-to-end.

Triggered by the webhook or upload endpoint. Runs in a separate worker
process, not inside the API, so a slow submission doesn't block requests.

The full pipeline:
    decode zip -> run sandbox -> generate markdown -> send email ->
    push to GitHub -> update DB -> insert telemetry

Each stage has its own failure mode, and they don't all fail the same way.
See the inline notes and the TODO block at the bottom.

NOTE: this task is not idempotent. If it retries, the sandbox runs again,
the email sends again, and the GitHub commit is attempted again. See the
"Retry semantics" TODO for why that matters.
"""

import asyncio
import base64
import json
import logging

import asyncpg

from app.core.config import settings
from app.services.email import send_report_email
from app.services.github import post_audit_report
from app.services.report import generate_markdown_report
from app.services.sandbox import run_sandbox_tests
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="verify_submission", bind=True, max_retries=3)
def process_submission(self, submission_id: str, zip_bytes_b64: str):
    # zip_bytes_b64 travels through Redis as part of the task message. That
    # means the (up to) 5MB zip is duplicated in the broker — once by the
    # API when it enqueues, once more if Celery retries. At current scale
    # that's fine; at higher volume you'd want to store the zip in S3 and
    # pass only a reference.
    logger.info("Worker picked up submission %s", submission_id)

    try:
        zip_bytes = base64.b64decode(zip_bytes_b64)
        is_passed, logs, json_report = run_sandbox_tests(zip_bytes)
        new_status = "PASSED" if is_passed else "FAILED"
        logger.info("Sandbox finished for %s: %s", submission_id, new_status)

        async def run_async_flow():
            # Fresh connection per task, not the pool from app.core.database.
            # That pool is tied to the FastAPI process — separate process,
            # separate pool. Celery with --pool=solo runs one task at a time
            # per worker, so a per-task connection is acceptable. If the
            # worker ever runs concurrently, switch to an asyncpg pool here.
            conn = await asyncpg.connect(settings.database_url)
            try:
                data = await conn.fetchrow(
                    """
                    SELECT s.repo_url, s.commit_sha, u.email, c.title AS challenge_title
                    FROM submissions s
                    JOIN users u ON s.user_id = u.id
                    JOIN challenges c ON s.challenge_id = c.id
                    WHERE s.id = $1
                    """,
                    submission_id,
                )

                if not data:
                    # Row was deleted between enqueue and execution. Nothing
                    # to update, nothing to report. Exit cleanly rather than
                    # raise — a retry wouldn't find it either.
                    logger.error("Submission %s not found in DB", submission_id)
                    return

                markdown_report = generate_markdown_report(
                    submission_id=submission_id,
                    challenge_title=data["challenge_title"],
                    is_passed=is_passed,
                    logs=logs,
                )

                # --- Email ---
                # Falls back to the bot's own address when the candidate has
                # no email on file. That means the audit still reaches
                # *someone* for audit purposes, but the candidate never sees
                # it. Preserving this fallback because removing it would
                # silently drop reports for direct-upload candidates who
                # didn't supply an email — worse than the current behaviour.
                # TODO: log a distinct warning when falling back so this is
                # visible in worker logs, and stop treating it as success.
                target_email = data["email"] or settings.resend_from_email
                send_report_email(target_email, markdown_report)
                # Return value discarded. See app/services/email.py TODO —
                # a failed send is silent. Would be fixed here once email
                # failure has somewhere to be recorded.

                # --- GitHub push ---
                # Skipped for direct-upload submissions whose repo_url is the
                # literal string "Direct Upload" — there's nowhere to push.
                # The check is against "github.com" appearing in the URL,
                # which is loose but works because direct-upload URLs are
                # clearly not GitHub URLs.
                if "github.com" in data["repo_url"]:
                    await post_audit_report(
                        data["repo_url"], data["commit_sha"], markdown_report
                    )
                else:
                    logger.info(
                        "Skipping GitHub push for %s: direct upload", submission_id
                    )

                # --- Persist results ---
                # Two writes, not wrapped in a transaction. If the UPDATE
                # succeeds and the INSERT fails (network blip, constraint
                # violation, disk full), the submission is marked PASSED but
                # has no report row — the Passport API will return the
                # submission with audit_report=None.
                # TODO: wrap both in a transaction:
                #     async with conn.transaction():
                #         await conn.execute(UPDATE ...)
                #         await conn.execute(INSERT ...)
                # This is a small fix; deferred because comment-only pass.
                await conn.execute(
                    "UPDATE submissions SET status = $1 WHERE id = $2",
                    new_status,
                    submission_id,
                )
                await conn.execute(
                    "INSERT INTO reports (submission_id, markdown_content) VALUES ($1, $2)",
                    submission_id,
                    markdown_report,
                )

                # --- Telemetry ---
                # Every test result becomes a row in test_results. Parsing
                # failures are caught here rather than propagated because a
                # broken JSON report shouldn't fail a submission that already
                # passed the sandbox — the pass/fail decision comes from the
                # exit code, not from this data.
                if json_report:
                    try:
                        report_data = json.loads(json_report)
                        for test in report_data.get("tests", []):
                            test_name = test.get("nodeid", "unknown")
                            passed = test.get("outcome") == "passed"

                            # pytest-json-report splits duration across three
                            # phases. Summing them gives total wall-clock time
                            # per test, which is what an employer actually cares
                            # about. Setup and teardown are usually tiny for
                            # this challenge set, but including them avoids
                            # under-reporting when fixtures are expensive.
                            duration_ms = 0
                            for phase in ("setup", "call", "teardown"):
                                duration_ms += int(
                                    test.get(phase, {}).get("duration", 0) * 1000
                                )

                            # Truncate to 10KB per field. Postgres TEXT has no
                            # hard limit, but storing 50MB of stdout on a
                            # runaway print statement would bloat the row.
                            # Candidate code can print arbitrarily large
                            # output; this is the only guard.
                            stdout = test.get("call", {}).get("stdout", "")
                            stderr = test.get("call", {}).get("stderr", "")
                            if len(stdout) > 10000:
                                stdout = stdout[:10000] + "... [truncated]"
                            if len(stderr) > 10000:
                                stderr = stderr[:10000] + "... [truncated]"

                            await conn.execute(
                                """
                                INSERT INTO test_results
                                (submission_id, test_name, passed, duration_ms, stdout, stderr)
                                VALUES ($1, $2, $3, $4, $5, $6)
                                """,
                                submission_id,
                                test_name,
                                passed,
                                duration_ms,
                                stdout,
                                stderr,
                            )
                        logger.info("Saved telemetry for %s", submission_id)
                    except Exception as e:
                        # Broad catch: a malformed JSON report shouldn't fail
                        # the whole task after everything else already
                        # succeeded. Logged loudly so it's visible in worker
                        # logs even though the submission is marked done.
                        logger.error("Failed to parse telemetry for %s: %s", submission_id, e)

            finally:
                await conn.close()

        # asyncio.run() inside a Celery task assumes the worker is running
        # --pool=solo or --pool=threads. With --pool=prefork (Celery's
        # default), each child process has its own event loop, so this
        # happens to work — but it's fragile. If the worker ever moves to
        # a shared event loop model, this call will raise
        # "asyncio.run() cannot be called from a running event loop".
        # TODO: migrate to async-safe Celery wrapper (e.g. async_tasks or
        # explicit loop management) before changing --pool.
        asyncio.run(run_async_flow())
        logger.info("Database updated for %s", submission_id)

    except Exception as exc:
        logger.error("Task failed for %s: %s", submission_id, exc, exc_info=True)

        # Retries exhaust at `retries >= max_retries`, meaning the task runs
        # 4 times total (initial + 3 retries). The ERROR status is only
        # written on the final failure — otherwise the UI would flash ERROR
        # for 10 seconds during the retry delay, then flip back to whatever
        # the retry produced.
        #
        # Caveat: by the time this branch fires, the sandbox has already run
        # 4 times, the email has sent up to 4 times, and the GitHub commit
        # has been attempted up to 4 times. See the "Retry semantics" TODO
        # below — this is the biggest correctness issue in the file.
        if self.request.retries >= self.max_retries:
            try:
                async def mark_error():
                    conn = await asyncpg.connect(settings.database_url)
                    try:
                        await conn.execute(
                            "UPDATE submissions SET status = 'ERROR', rejection_reason = $1 WHERE id = $2",
                            f"Worker error after {self.max_retries} retries: {exc}",
                            submission_id,
                        )
                    finally:
                        await conn.close()

                asyncio.run(mark_error())
            except Exception as db_exc:
                # Even the error handler can fail (DB down, etc.). Logging
                # is the only remaining recourse — the submission will stay
                # in QUEUED forever with no ERROR marker. Would need external
                # monitoring to catch this class of failure.
                logger.error("Failed to set ERROR status for %s: %s", submission_id, db_exc)

        raise self.retry(exc=exc, countdown=10)


# Deferred on purpose, not forgotten:
#
# - Retry semantics. THE BIG ONE. When this task retries, it re-runs
#   everything: sandbox (30s of Docker), email send, GitHub commit. None of
#   these are idempotent. A flaky DB write after a successful sandbox run
#   means:
#     * Sandbox runs again (wasted compute)
#     * Candidate receives a second identical email
#     * GitHub commit is attempted a second time (succeeds because
#       post_audit_report overwrites — but generates a second commit in
#       the audit log if there was a race between fetch-SHA and PUT)
#
#   Proper fix: split the task into two — sandbox_and_report (idempotent,
#   can retry) and notify_and_persist (needs idempotency keys). Or move the
#   notification side effects to a separate task triggered by the DB write
#   succeeding. This is an architectural change, not a small fix.
#
# - `self.retry(countdown=10)` with no backoff. Three retries at 10s each is
#   ~30s of retry window, not enough to ride out a Supabase restart (which
#   usually takes 30-60s). A backoff (10s, 30s, 90s) would be more resilient
#   for a fractionally small change.
#
# - No queue routing. Every task runs on the default `celery` queue. When
#   there's more than one kind of task (retries, audits, cleanup), they'll
#   all compete for the same worker. Defined in celery_app.py TODO.
#
# - Connection not pooled. Each task opens a fresh asyncpg connection. For
#   --pool=solo (one task at a time) that's fine. For a hypothetical
#   --pool=prefork with concurrency=4, that's 4 connections open per worker
#   instance — worth pooling at that point.
#
# - target_email fallback is opaque. When a candidate has no email, their
#   report goes to settings.resend_from_email. Useful for audit trail, but
#   the code makes no distinction between "delivered to candidate" and
#   "delivered to ourselves". Logged only at info level.