"""Static validation on the incoming zip, before any Docker compute is spent.

Runs inside the webhook/upload request, before queueing. A submission that
fails here never touches Celery and never spins up a container — that's the
entire cost story of this file. At 5MB max and pure-Python parsing, a
rejection costs a few milliseconds of CPU.

Design constraint: never execute the candidate's code. ast.parse() builds
a syntax tree without running anything. zipfile.ZipFile reads bytes without
extracting. No subprocess, no eval, no import.

Important: this is NOT a security boundary. It only checks structure, not
intent. Valid syntax and correct filenames are all it takes to pass. Safety
against malicious execution lives in the Docker sandbox (see
app/services/sandbox.py) via network isolation, cap_drop, and pids_limit.
"""

import ast
import io
import zipfile

# Mirrors the cap in app/services/github.py so we never buffer more than we
# accept. Both numbers must stay in sync — the download path enforces the
# same limit before this function ever sees the bytes.
_MAX_ZIP_BYTES = 5 * 1024 * 1024


def validate_submission(zip_bytes: bytes, required_files: list[str]) -> tuple[bool, str]:
    # Oversized zips would blow the Upstash broker limit when base64-encoded
    # and pushed to Redis. Reject here so the failure is a clean 400, not an
    # opaque Celery enqueue error later.
    if len(zip_bytes) > _MAX_ZIP_BYTES:
        return False, "Zip file is too large (max 5MB)."

    try:
        # Everything stays in RAM. No temp files, no disk I/O, no artifacts
        # left behind if the request is cancelled mid-way.
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            names = z.namelist()

            # ---- Required files ----
            # GitHub zips wrap everything in a top-level folder ("repo-main/"),
            # so exact-name matching would fail every real submission. We
            # match by suffix instead.
            #
            # Two permissive edge cases accepted on purpose:
            #   - "not_pipeline.py" ends with "pipeline.py" and satisfies a
            #     required_files entry of "pipeline.py". Candidate would have
            #     to construct this deliberately.
            #   - "mytests/" contains "tests/" as a substring, satisfying the
            #     directory check. Same reasoning — no benign submission
            #     shapes trip this.
            #
            # The safety net is that the sandbox runs official tests regardless
            # of what the candidate named their files. Filename tricks don't
            # help them pass.
            for required in required_files:
                if required.endswith("/"):
                    present = any(required in n for n in names)
                else:
                    present = any(n.endswith(required) for n in names)
                if not present:
                    return False, f"Missing {required}"

            # ---- Python syntax ----
            # ast.parse builds a tree without executing anything. This catches
            # typos, mismatched brackets, and unclosed strings before we burn
            # a worker slot on a submission that can't possibly pass tests.
            #
            # It does NOT catch logical errors, runtime failures, or bad
            # imports — those surface in the sandbox pytest run instead.
            for name in names:
                if not name.endswith(".py"):
                    continue

                # Candidate code is UTF-8 by convention, but a non-UTF-8 file
                # raises UnicodeDecodeError here and gets caught by the
                # generic handler below — producing "Validation error: ..."
                # rather than a clean syntax message. Acceptable: UTF-8 is
                # the near-universal default for Python source.
                code = z.read(name).decode("utf-8")
                try:
                    ast.parse(code)
                except SyntaxError as e:
                    # Include filename and line — a bare "syntax error" would
                    # leave the candidate with no way to find the bug without
                    # downloading and diffing their own repo.
                    return False, f"Syntax error in {name} on line {e.lineno}: {e.msg}"

            return True, "Passed edge validation"

    except zipfile.BadZipFile:
        return False, "Invalid zip file format. Did you upload a .zip?"
    except Exception as e:
        # Broad catch on purpose: this function is the bouncer, not the
        # diagnostician. Anything unexpected (encoding errors, corrupt zip
        # entries, OOM on a huge single file) should reject cleanly rather
        # than propagate as a 500 and confuse the caller about what failed.
        return False, f"Validation error: {str(e)}"


# Deferred on purpose, not forgotten:
#
# - Uncompressed-size cap. We limit the compressed zip to 5MB but don't check
#   how much it expands to. A zip bomb (5MB → 5GB) would pass the compressed
#   check and then hang z.read() on a single huge .py file, potentially
#   exhausting the worker's memory before Celery ever picks it up. The fix
#   is to sum z.getinfo(n).file_size across entries and reject if the total
#   exceeds a threshold. Cheap to add; deferred because realistic candidate
#   submissions are nowhere near this size, and the 30s sandbox timeout
#   wouldn't help here — this runs before the sandbox.
#
# - Malicious-import scanning. The original TODO said "reject files that
#   contain import os; os.system()". That was too aggressive — legitimate
#   code imports os all the time. The correct framing: imports can't be
#   evaluated at the edge (ast.parse doesn't import anything), and anything
#   that actually runs is already contained by the sandbox. This is a
#   reminder that the edge filter is deliberately not where security lives.
#
# - Secret scanning (TruffleHog or similar). Catch hardcoded API keys before
#   they land in a public audit report. Not urgent — the sandbox has no
#   network, so leaked keys are unusable from inside it. Becomes urgent once
#   we push audit reports back to public candidate repos, because that's
#   when leaked secrets actually escape.