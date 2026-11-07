"""GitHub API client for the two operations the pipeline needs:

    download_repo_zip  — fetch a candidate's repo as a zip at a specific commit
    post_audit_report  — commit VERIFICATION_AUDIT.md back after the sandbox runs

Both authenticate with the same personal access token (github_token in .env).
Both use short-lived httpx clients: no connection pooling across calls, which
is fine because neither function is called in a tight loop.

Not covered here: webhook payloads arrive via FastAPI, not this module. This
is purely outbound — what we ask GitHub for, and what we push back.
"""

import base64
from urllib.parse import urlparse

import httpx

from app.core.config import settings

# Must stay in sync with app/services/edge_filter.py. If this were larger,
# we'd download a repo, hand it to the edge filter, and have it rejected for
# being too big — wasted bandwidth for a predictable rejection.
_MAX_ZIP_BYTES = 5 * 1024 * 1024


def _repo_path(repo_url: str) -> str:
    """Strip scheme, host, and .git suffix to get "owner/repo" for the API."""
    path = urlparse(repo_url).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path


async def download_repo_zip(repo_url: str, commit_sha: str) -> bytes:
    path = _repo_path(repo_url)

    # zipball/{ref} accepts both full SHAs and branch names. Passing a SHA
    # ensures we test exactly the commit that was pushed, even if the branch
    # moved on between the webhook arriving and this function running.
    api_url = f"https://api.github.com/repos/{path}/zipball/{commit_sha}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
        }

        # stream() so headers are available before the body is buffered —
        # that's how we check Content-Length before paying for the download.
        # follow_redirects is required: GitHub returns 302 to an S3 URL for
        # zipball requests, and without it you get an empty 302 response body.
        async with client.stream(
            "GET", api_url, headers=headers, follow_redirects=True
        ) as response:
            if response.status_code != 200:
                # Any non-200 (401 bad token, 403 rate limit, 404 wrong repo,
                # 5xx GitHub outage) is unrecoverable here — caller decides
                # whether to retry. The status code is in the message so the
                # submission's rejection_reason field is actually useful.
                raise Exception(
                    f"Failed to download repo from GitHub: HTTP {response.status_code}"
                )

            declared_length = int(response.headers.get("content-length") or 0)
            if declared_length and declared_length > _MAX_ZIP_BYTES:
                raise Exception(
                    f"Repo zip exceeds 5MB limit (Content-Length: {declared_length} bytes)"
                )

            # aread() buffers the entire body. If Content-Length was absent
            # (GitHub rarely omits it, but small repos sometimes do) we can't
            # reject before this call — the belt-and-braces check below catches
            # it after the fact. A truly streaming size-limit would iterate
            # response.aiter_bytes() with a running counter. Deferred: GitHub
            # always sets Content-Length for zipball in practice, so the current
            # pattern works. See TODO block at the bottom.
            content = await response.aread()

            if len(content) > _MAX_ZIP_BYTES:
                raise Exception("Repo zip exceeds 5MB limit")

            return content


async def post_audit_report(repo_url: str, commit_sha: str, markdown_content: str):
    # NOTE: commit_sha is currently unused. The GitHub API only allows commits
    # to branches, not to arbitrary SHAs — so we resolve the default branch and
    # commit there. The parameter is kept because callers pass it and because
    # a future improvement could embed it in the commit message (e.g. "Audit
    # for {sha}") so the report indicates which version was tested.
    path = _repo_path(repo_url)

    # GitHub's contents API requires base64-encoded UTF-8. Not raw text.
    encoded_content = base64.b64encode(markdown_content.encode("utf-8")).decode("utf-8")

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
        }

        # ---- Resolve the default branch ----
        # Candidates' repos can be main, master, develop, or anything else.
        # Hardcoding "main" would 404 on every legacy repo. The API gives us
        # the real name. Fall back to "main" only if the metadata call fails
        # outright — that keeps the function usable even when we can't read
        # repo metadata (which would be strange, but not impossible for
        # private forks with restricted scopes).
        repo_res = await client.get(
            f"https://api.github.com/repos/{path}", headers=headers
        )
        branch = (
            repo_res.json().get("default_branch") or "main"
            if repo_res.status_code == 200
            else "main"
        )

        api_url = f"https://api.github.com/repos/{path}/contents/VERIFICATION_AUDIT.md"

        # ---- Check if the report already exists ----
        # The contents API uses PUT for both create and update, but update
        # requires the existing file's SHA — without it, GitHub rejects with
        # 422 "sha wasn't supplied". So we fetch the current SHA if the file
        # exists (200), and leave it None if it doesn't (404).
        #
        # Note: a 200 response with the file content also returns a SHA. This
        # means every subsequent run of the same submission overwrites the
        # previous report cleanly.
        res = await client.get(f"{api_url}?ref={branch}", headers=headers)
        file_sha = res.json().get("sha") if res.status_code == 200 else None

        data = {
            # This exact message must contain the string "Verification Audit Report"
            # to match AUDIT_COMMIT_MARKER in app/routes/webhook.py. If you
            # change it, change that constant too — otherwise the bot's own
            # commit re-triggers the pipeline and loops forever.
            "message": "Add Verification Audit Report",
            "content": encoded_content,
            "branch": branch,
        }
        if file_sha:
            data["sha"] = file_sha

        response = await client.put(api_url, headers=headers, json=data)

        # 200 = file updated, 201 = file created. Both are success.
        # Everything else is a silent failure: 403 (branch protection),
        # 409 (file deleted between GET and PUT), 422 (sha conflict from a
        # concurrent commit), 429 (rate limit). The caller in tasks.py doesn't
        # check the return value, so a failed push is invisible to the worker
        # flow — the submission is still marked PASSED and the report is still
        # emailed, but the candidate never sees the commit. TODO: either
        # raise on non-2xx so the task retries, or return a bool the caller
        # can record in rejection_reason.
        if response.status_code in (200, 201):
            print(f"Report pushed to GitHub for {path}")
        else:
            print(f"Could not push to GitHub: {response.status_code} - {response.text}")


# Deferred on purpose, not forgotten:
#
# - Streaming size enforcement. aread() buffers the full body. When
#   Content-Length is present (as it usually is for GitHub zipballs) we
#   reject before buffering. When it isn't, we pay for the download then
#   reject — memory spike before the error. Fix: iterate response.aiter_bytes()
#   with a running byte counter and abort mid-stream.
#
# - Retry with exponential backoff on 429. GitHub's authenticated rate limit
#   is 5000 requests/hour, plenty for current volume. Will matter if Verification
#   ever processes more than ~80 submissions/hour sustained, or if multiple
#   instances share the same token.
#
# - Push to a pull request instead of a direct commit. Currently we commit
#   straight to the default branch — the candidate sees the report only after
#   it's already in their history. A PR flow would let them review before
#   merge. Bigger change than the code suggests: needs a branch creation call,
#   then a PR create call, then handling PR conflicts.
#
# - Verify we're pushing to the right commit. Currently we resolve the default
#   branch and commit there, but the candidate might have pushed twice while
#   the sandbox was running. The audit would then be attached to a commit we
#   never tested. Recording commit_sha in the commit message ("Audit of
#   {sha}") would at least make the mismatch visible, without needing to
#   prevent it.