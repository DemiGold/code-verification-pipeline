"""Public Talent Passport read API.

The Passport is the employer-facing artifact: a candidate's verified skills
plus the evidence behind each one. "Verified" here means a human reviewer
conducted a live defence and signed off — automated test passes do NOT
appear here. That's the whole product thesis: a candidate is only visible to
employers once a person vouched for them.

Unauthenticated on purpose. The data returned is exactly what the candidate
would publish on a CV, and gating it behind auth would mean employers can't
preview before buying credits. Sensitive fields (email, internal IDs) are
deliberately excluded from the response — if you add a field here, treat the
decision as public-facing.
"""

from fastapi import APIRouter, HTTPException

from app.core.database import get_connection

router = APIRouter()


@router.get("/api/passport/{github_username}")
async def get_talent_passport(github_username: str):
    async with get_connection() as conn:
        # ---- 1. Resolve the candidate ----
        # Two lookups in one query: by GitHub handle (used internally) or by
        # public_slug (used in shareable URLs like /passport/ada-okafor).
        #
        # Caveat: this OR has an edge case. If user A's github_username equals
        # user B's public_slug, fetchrow returns whichever Postgres finds
        # first. In practice public_slug is a kebab-case slug and github
        # handles rarely contain hyphens, so collision is improbable — but
        # it's not structurally prevented.
        # TODO: enforce slug format (lowercase + hyphens only) at user creation
        # so collisions with GitHub handles are impossible.
        user = await conn.fetchrow(
            """
            SELECT id, github_username, created_at, public_slug
            FROM users
            WHERE github_username = $1 OR public_slug = $1
            """,
            github_username,
        )
        if not user:
            raise HTTPException(status_code=404, detail="Candidate not found")

        # ---- 2. Fetch VERIFIED submissions ----
        # status = 'VERIFIED' is the only path in. A PASSED submission (which
        # means "passed automated tests, awaiting human defence") does not
        # appear — that would be handing employers unverified data with the
        # same visual weight as verified data.
        #
        # LEFT JOIN on verified_by because the FK is ON DELETE SET NULL — a
        # reviewer whose account was deleted still had their notes recorded
        # on the submission, and we don't want to drop the row just because
        # the reviewer is gone. The Python layer substitutes "Unknown Reviewer".
        verified_submissions = await conn.fetch(
            """
            SELECT
                s.id AS submission_id,
                s.repo_url,
                s.verified_at,
                s.defence_notes,
                s.known_limitations,
                c.title AS challenge_title,
                c.track AS challenge_track,
                u.github_username AS reviewer_username
            FROM submissions s
            JOIN challenges c ON s.challenge_id = c.id
            LEFT JOIN users u ON s.verified_by = u.id
            WHERE s.user_id = $1 AND s.status = 'VERIFIED'
            ORDER BY s.verified_at DESC
            """,
            user["id"],
        )

        # 400, not 404: the candidate exists, they just have nothing published
        # yet. A reviewer with API experience might argue this should be a 200
        # with an empty verified_skills list — that's arguably more correct
        # (the resource exists, its list is empty). Kept as 400 to preserve
        # the contract the frontend already implements against.
        if not verified_submissions:
            raise HTTPException(
                status_code=400, detail="This candidate has no verified skills yet."
            )

        # ---- 3. Enrich each submission ----
        # N+1 pattern: two extra queries per verified submission. Fine at this
        # scale (most candidates have 2-5 verifications, and this endpoint
        # isn't hot). If a candidate ever hits 50+ verifications, or if this
        # endpoint starts getting hammered by employer search, collapse it
        # into the parent query with array_agg or a second batched query.
        passport_data = []
        for sub in verified_submissions:
            report = await conn.fetchrow(
                "SELECT markdown_content FROM reports WHERE submission_id = $1",
                sub["submission_id"],
            )

            # Telemetry: per-test pass/fail plus duration, captured by the
            # sandbox's pytest-json-report output. This is what differentiates
            # a Verification Passport from a "skills" section — the numbers are
            # measured, not claimed.
            test_results = await conn.fetch(
                """
                SELECT test_name, passed, duration_ms, stdout, stderr
                FROM test_results
                WHERE submission_id = $1
                """,
                sub["submission_id"],
            )

            passport_data.append({
                "challenge": sub["challenge_title"],
                "track": sub["challenge_track"],
                "verified_at": sub["verified_at"],
                "reviewer": sub["reviewer_username"] or "Unknown Reviewer",
                "reviewer_notes": sub["defence_notes"],
                # Default to a string rather than null so frontend rendering
                # doesn't need to branch. "None specified" is the honest value —
                # a missing known_limitations field means the reviewer didn't
                # record any, which is not the same as "no limitations exist".
                "known_limitations": sub["known_limitations"] or "None specified",
                "repository": sub["repo_url"],
                "audit_report": report["markdown_content"] if report else None,
                "telemetry": [dict(tr) for tr in test_results],
            })

        # ---- 4. Assemble the Passport ----
        # Note: users.email is deliberately NOT selected above, even though
        # older versions of this endpoint fetched it. It's PII, and this
        # endpoint is public. If a future feature needs candidate contact,
        # that belongs behind the employer-auth unlock flow (Phase 5), not
        # in a free public response.
        return {
            "candidate": {
                "github_username": user["github_username"],
                # Fall back to the GitHub handle if no public slug was ever
                # set. Means every candidate has a stable URL from day one,
                # and slugs can be added later without breaking links.
                "public_slug": user["public_slug"] or user["github_username"],
                "joined_at": user["created_at"],
            },
            "verified_skills": passport_data,
            "total_verifications": len(passport_data),
            "status": "VERIFIED",
        }


# Deferred on purpose, not forgotten:
#
# - Pagination. Currently returns every verified submission. Fine at 2-5 per
#   candidate; will be painful at 50+. Add ?limit= and ?offset= before the
#   employer dashboard starts paginating client-side.
#
# - Cache headers. This data changes only when a reviewer verifies something
#   (rare). A short Cache-Control or ETag would cut database load massively
#   for a public endpoint. Worth adding when this endpoint starts seeing
#   real traffic.
#
# - Soft-delete support. If a candidate's account is deleted, their verified
#   submissions cascade-delete (ON DELETE CASCADE on user_id). Consider
#   preserving the Passport for the audit trail, since employers may have
#   paid to unlock it.