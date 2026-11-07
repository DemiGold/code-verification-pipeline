"""Streamlit validator dashboard.

The dashboard is a separate process from the API. It reads submissions
directly from Postgres (read-only) but routes every state change through
POST /api/validator/verify — the write-path guards live in the API, not
here. That separation is intentional: this UI could be replaced by a CLI or
a Next.js admin panel without duplicating the verification logic.

Requires the same VALIDATOR_TOKEN as the API. Note this is a client-side
gate only — it stops accidental exposure but isn't a substitute for the
API's own auth. Anyone with network access to the API and the token can
skip this UI entirely.

Run with:
    streamlit run admin/dashboard.py
"""

import asyncio
import hmac

import asyncpg
import httpx
import streamlit as st

from app.core.config import settings


# Streamlit re-executes this entire file on every interaction. Anything
# stored in st.session_state survives that; anything local is reset. So
# `authenticated` and `token` must live in session_state — a plain variable
# would be re-initialised to False/None on every button click.
st.set_page_config(page_title="Verification Admin", page_icon="🛡️", layout="wide")

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "token" not in st.session_state:
    st.session_state.token = None


# --- Auth gate ---
# If not logged in, render the login form and stop. st.stop() prevents the
# rest of the script from executing on this rerun — so no matter how the
# user interacts with the page, nothing below this point runs until they
# supply a valid token.
if not st.session_state.authenticated:
    st.title("🛡️ Verification Validator Login")
    st.markdown("Enter the validator token to access the queue.")

    token = st.text_input("Validator Token", type="password")
    if st.button("Login", type="primary"):
        # compare_digest, not == — same reasoning as the API. Streamlit runs
        # in a server process that's reachable over the network, so timing
        # attacks against this comparison are theoretically in scope.
        if hmac.compare_digest(token, settings.validator_token):
            st.session_state.authenticated = True
            st.session_state.token = token
            st.rerun()  # rebuild the page with the auth gate now passing
        else:
            st.error("Invalid token")

    st.stop()


# --- Read path ---
# Direct Postgres query, not the API. Reads are cheap and read-only, and
# the API doesn't expose an endpoint for "list all PASSED submissions" —
# that would be a lot of surface area for a single internal consumer.
#
# One connection per dashboard load. No pooling: Streamlit is single-user
# in this deployment, and each page render is one request. If this becomes
# multi-tenant, switch to asyncpg.create_pool() at module level.
async def fetch_passed_submissions():
    conn = await asyncpg.connect(settings.database_url)
    try:
        return await conn.fetch(
            """
            SELECT s.id, s.repo_url, s.commit_sha,
                   c.title AS challenge_title, c.track AS challenge_track,
                   u.github_username
            FROM submissions s
            JOIN users u ON s.user_id = u.id
            JOIN challenges c ON s.challenge_id = c.id
            WHERE s.status = 'PASSED'
            ORDER BY s.created_at ASC
            """,
        )
    finally:
        await conn.close()


# --- Write path ---
# Everything that mutates state goes through the API. The API enforces:
#   * Bearer token (401 if missing/wrong)
#   * validator_username must exist AND have role VALIDATOR or ADMIN
#   * submission must currently be in PASSED state
#   * race-condition guard: second writer gets 409
# Duplicating any of that here would mean two sources of truth that drift.
#
# Note: `verify_submission` shadows the name of the API route handler in
# app/routes/validator/__init__.py. Different modules, no conflict at
# runtime, but grep for the name returns both. Could rename to something
# like `call_verify_api` to disambiguate — cosmetic, deferred.
def verify_submission(
    submission_id: str,
    status: str,
    notes: str,
    username: str,
    limitations: str = "",
) -> tuple[bool, str]:
    try:
        # Synchronous httpx call. Streamlit handlers run on the main thread
        # of a fresh script execution, so blocking is fine — this button
        # only fires once per user click, and 30s covers even a slow DB on
        # the API side.
        resp = httpx.post(
            f"{settings.app_base_url}/api/validator/verify",
            headers={"Authorization": f"Bearer {st.session_state.token}"},
            json={
                "submission_id": submission_id,
                "validator_username": username,
                "validator_notes": notes,
                "known_limitations": limitations,
                "status": status,
            },
            timeout=30.0,
        )
    except httpx.RequestError as e:
        # Network-level failure (API down, DNS failure, timeout). Returned as
        # a plain error string — the caller renders it via st.error().
        return False, f"Network error: {e}"

    if resp.status_code == 200:
        # The API returns {"status": "success", "message": "...", "verified_by": "..."}
        # so pull `message` for the toast. Fallback to "OK" if the shape ever
        # changes, so a missing field doesn't crash the success path.
        return True, resp.json().get("message", "OK")

    # FastAPI validation errors come back as {"detail": [{"loc": [...], ...}]}
    # and HTTPException as {"detail": "message"}. Handle both — the first is
    # a list, the second is a string. .get("detail", resp.text) covers the
    # case where the response is entirely non-JSON.
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return False, f"HTTP {resp.status_code}: {detail}"


# --- Main dashboard ---
st.title("🛡️ Verification Validator Dashboard")
st.markdown("Review candidates who passed the automated Docker sandbox.")

# Default is "" on purpose. The original code defaulted to "DemiGold" — the
# developer's own handle — which meant every fresh deployment showed the
# wrong default and locked the reviewer into editing it every session.
# If you want a per-deployment default, put it in .env as VALIDATOR_USERNAME
# and read it via settings.
#
# This field is the audit-trail identifier. It must match a VALIDATOR or
# ADMIN row in the DB — the API rejects unknown usernames with 404 before
# the status transition is attempted.
validator_username = st.text_input(
    "Your GitHub Username (for audit trail)",
    value="",
    help=(
        "Must match a VALIDATOR or ADMIN user in the database. "
        "The API will reject the request with 403/404 otherwise."
    ),
)

if not validator_username:
    st.warning("Please enter your GitHub username before verifying.")
    st.stop()

with st.spinner("Fetching passed submissions from database..."):
    try:
        # asyncio.run() blocks until the coroutine completes, which is
        # correct here — Streamlit has no persistent event loop, so each
        # invocation needs its own. A raised exception means the DB is
        # unreachable; render the error but keep the page usable so the
        # user sees the failure rather than a stack trace.
        submissions = asyncio.run(fetch_passed_submissions())
    except Exception as e:
        st.error(f"Failed to connect to database: {e}")
        submissions = []

if not submissions:
    st.success("🎉 No candidates are waiting for verification right now. You're all caught up!")
else:
    st.info(f"There are **{len(submissions)}** candidates waiting for human verification.")
    st.divider()

    for sub in submissions:
        with st.container():
            col1, col2 = st.columns([2, 1])

            with col1:
                # Wider column: submission details. These are display-only —
                # the read path already fetched them, nothing here can change
                # the DB.
                st.subheader(f"@{sub['github_username']}")
                track_name = sub["challenge_track"].replace("-", " ").upper()
                st.markdown(f"**Track:** `{track_name}`")
                st.write(f"**Challenge:** {sub['challenge_title']}")
                st.write(
                    f"**Repository:** [{sub['repo_url']}]({sub['repo_url']}) "
                    f"(Commit: `{sub['commit_sha'][:7]}`)"
                )
                st.write(f"**Submission ID:** `{sub['id']}`")

            with col2:
                # Narrower column: the verification action. Both notes fields
                # use a per-submission key (the submission UUID) so that
                # typing into one candidate's notes doesn't clear another's —
                # Streamlit widgets with the same label collide by default.
                st.write("### Verification Action")

                notes = st.text_area(
                    "Defence Notes",
                    key=f"notes_{sub['id']}",
                    placeholder="e.g., Candidate explained connection pooling perfectly...",
                )

                limitations = st.text_area(
                    "Known Limitations",
                    key=f"lims_{sub['id']}",
                    placeholder="e.g., No production-scale load testing verified.",
                )

                btn_col1, btn_col2 = st.columns(2)

                with btn_col1:
                    if st.button(
                        "✅ Verify",
                        key=f"verify_{sub['id']}",
                        use_container_width=True,
                        type="primary",
                    ):
                        if not notes:
                            st.warning("Please add defence notes before verifying.")
                        else:
                            ok, msg = verify_submission(
                                str(sub["id"]),
                                "VERIFIED",
                                notes,
                                validator_username,
                                limitations,
                            )
                            if ok:
                                st.success(f"Candidate marked as VERIFIED — {msg}")
                                # Rerun the whole script. This re-fetches
                                # the submissions list, so the verified
                                # candidate disappears from the queue and
                                # the count updates. Without the rerun, the
                                # row would stay visible until the next
                                # manual refresh, which is confusing.
                                st.rerun()
                            else:
                                st.error(f"Verification failed: {msg}")

                with btn_col2:
                    if st.button(
                        "❌ Reject",
                        key=f"reject_{sub['id']}",
                        use_container_width=True,
                    ):
                        if not notes:
                            st.warning("Please add defence notes before rejecting.")
                        else:
                            ok, msg = verify_submission(
                                str(sub["id"]),
                                "REJECTED",
                                notes,
                                validator_username,
                                limitations,
                            )
                            if ok:
                                # Same rerun pattern. Message uses
                                # st.error() here even on success because
                                # the outcome is a rejection — colouring
                                # it green would read as "candidate passed".
                                st.error(f"Candidate marked as REJECTED — {msg}")
                                st.rerun()
                            else:
                                st.error(f"Reject failed: {msg}")
        st.divider()


# Deferred on purpose, not forgotten:
#
# - No logout button. Once logged in, the token lives in session_state until
#   the browser tab closes. For a single-admin tool this is acceptable; if
#   this dashboard ever shares a machine, add a "Log out" button that clears
#   session_state.authenticated and .token.
#
# - No bulk action. One candidate at a time by design — the point of the
#   dashboard is deliberate per-candidate review, not batch approve. If a
#   reviewer ever needs to clear a backlog of 100, they should question why
#   100 submissions passed automated tests without human defence in the
#   first place.
#
# - No telemetry display. The dashboard shows repo + commit but not the
#   pytest output or duration. Reviewers open the repo themselves — the
#   dashboard is a link-and-confirm tool, not a full review surface. If
#   reviewers start asking for the audit report inline, add a
#   st.expander("Show report") that calls the Passport API.
#
# - No error handling for a 409 response specifically. The updated
#   validator.py returns 409 when a submission was modified between the
#   dashboard's fetch and the reviewer's click (concurrent review). Right
#   now that falls through to the generic "HTTP 409: ..." message. Could
#   be special-cased to say "Another reviewer already acted on this one —
#   refreshing the queue." Cosmetic, deferred.
#
# - Login form has no rate limiting. A determined attacker on the network
#   could brute-force the token. At current scale (localhost or private
#   network), not worth the complexity. Would need a Redis-backed attempt
#   counter before public exposure.
#
# - Session token in memory only. Refresh the page, log in again. If this
#   becomes a pain point, store the token in a signed cookie with a short
#   TTL — but that brings its own security surface (cookie theft, CSRF).