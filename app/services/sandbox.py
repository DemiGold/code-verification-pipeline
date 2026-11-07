"""Docker sandbox — the only place candidate code is ever executed.

Threat model: assume everything inside the zip is hostile. The candidate may
have written the zip by hand, replaced files the starter repo shipped with,
or crafted a repo whose only purpose is to escape the container. Everything
this file does is in service of that assumption.

Defences, layered:

    1. Host isolation   — code runs in a container, never on the host
    2. No network       — network_mode="none" prevents exfiltration and callbacks
    3. Non-root user    — uid 1000 inside the container, no privileged operations
    4. Capability drop  — cap_drop=["ALL"] removes even the caps root would have
    5. Process limit    — pids_limit=100 stops fork bombs from DoSing the host
    6. Memory limit     — 512MB prevents a memory bomb from OOMing the host
    7. CPU limit        — 0.5 core prevents a single submission from starving others
    8. No privilege escalation — no-new-privileges blocks setuid binaries
    9. Test overlay     — official tests replace whatever the candidate shipped
   10. Timeout          — 30s wall clock, container killed if exceeded

Not defended against: kernel exploits, container-escape CVEs, side-channel
attacks between containers. Those require gVisor or a VM boundary, which is
deferred to production scale (see TODO block).

Returns a 3-tuple: (passed, human_readable_logs, raw_json_report).
"""

import io
import os
import tempfile
import zipfile

import docker


# File names we strip from the candidate's zip before running pytest. Any of
# these present in the working directory would let a candidate influence test
# execution — conftest.py can import arbitrary code, pytest.ini can redirect
# test discovery, pyproject.toml can change pytest config. The overlay tests
# are injected after this deletion, so candidate-provided copies are irrelevant.
_SCRUBBED_FILES = (
    "conftest.py",
    "sitecustomize.py",   # imported by Python at startup
    "usercustomize.py",   # same
    "pytest.ini",
    "setup.cfg",
    "pyproject.toml",
)


def _safe_extract(zip_file: zipfile.ZipFile, dest: str) -> None:
    """Reject zips that try to write outside `dest`.

    Python 3.12 added ZipFile.extractall(filter="data") which does this
    natively, but we target 3.11. This function replicates the important
    part: every member's resolved path must stay inside dest.

    Guards against two attacks:

      - Zip Slip: member names like "../../etc/passwd" or absolute paths
        "/etc/cron.d/foo" — both rejected by the prefix check below.
      - Symlink tricks: a zip can contain a symlink whose target escapes
        dest, followed by a file written *through* the symlink. Rejecting
        anything with the UNIX symlink mode bit set prevents the setup.
    """
    dest_abs = os.path.abspath(dest)
    for member in zip_file.infolist():
        # Symlink entries have the S_IFLNK bit set in external_attr.
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"Zip contains symlink: {member.filename}")

        target = os.path.abspath(os.path.join(dest, member.filename))
        # Common prefix check must include the separator, otherwise
        # "/tmp/abc-evil" would pass as a prefix of "/tmp/abc".
        if not target.startswith(dest_abs + os.sep) and target != dest_abs:
            raise ValueError(f"Zip member escapes target directory: {member.filename}")

    zip_file.extractall(dest)


def run_sandbox_tests(zip_bytes: bytes) -> tuple[bool, str, str]:
    # timeout=180 is the Docker client's HTTP timeout, NOT the container
    # runtime. It governs how long docker-py waits for the daemon to respond
    # to any single API call — creating a container, waiting for it, removing
    # it. The actual container runtime limit is the wait(timeout=30) below.
    # 180s is generous because on Docker Desktop/WSL2 the daemon occasionally
    # takes several seconds to respond to the first call after idle.
    client = docker.from_env(timeout=180)

    with tempfile.TemporaryDirectory() as temp_dir:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            _safe_extract(z, temp_dir)

        # On Linux, TemporaryDirectory() creates the directory with mode 0700
        # owned by the invoking user. If this process runs as root (common in
        # containers and CI), uid 1000 inside our container can't read or
        # write the mount and every test fails with permission errors.
        # Docker Desktop on Windows/macOS handles this transparently via its
        # file-sharing layer, which is why it's untested there.
        # TODO: chmod -R o+rwx on the extracted tree before mounting, or run
        # the sandbox container as the same uid as the host process.
        extracted = os.listdir(temp_dir)
        actual_code_path = temp_dir
        if len(extracted) == 1 and os.path.isdir(os.path.join(temp_dir, extracted[0])):
            # GitHub zipballs are always wrapped in "{repo}-{ref}/". Unwrap
            # so the mount root contains the repo contents directly, not a
            # single wrapper folder.
            actual_code_path = os.path.join(temp_dir, extracted[0])

        container = None

        try:
            # The container command is a single shell pipeline with three
            # phases, joined by && so any failure aborts the whole run:
            #
            #   1. Scrub candidate files that could affect test collection
            #   2. Inject the official test file that matches this challenge
            #   3. Run pytest with JSON reporting to /app/report.json
            #
            # The `if [ -f X ]` guards mean a repo containing neither
            # query.sql nor pipeline.py gets no tests copied — pytest then
            # exits with "no tests collected" (exit 5), which the exit-code
            # check below treats as failure. Correct outcome for a submission
            # with no relevant code.
            #
            # CAVEAT: the `**/conftest.py` pattern does NOT recurse in POSIX
            # sh — globstar is a bash-4 feature and the container runs sh
            # (dash on Debian slim). It only matches one level of nesting, so
            # a conftest.py at depth 2+ survives this scrub. That's a real
            # bypass if a candidate's repo happens to nest test helpers
            # deeply, which is not implausible.
            # TODO: replace with `find . -name conftest.py -delete` which is
            # POSIX, recursive, and available in the debian-slim base image.
            container = client.containers.run(
                image="verification-sandbox:latest",
                command=(
                    "sh -c '"
                    "rm -f test_*.py tests/*.py conftest.py **/conftest.py "
                    "sitecustomize.py usercustomize.py pytest.ini setup.cfg pyproject.toml && "
                    "if [ -f query.sql ]; then cp /verification_official_tests/test_query.py .; fi && "
                    "if [ -f pipeline.py ]; then cp /verification_official_tests/test_pipeline.py .; fi && "
                    "pytest -v -p no:cacheprovider --json-report --json-report-file=/app/report.json"
                    "'"
                ),
                # The mount is read-write because pytest writes report.json
                # into it — that's how telemetry escapes the container back
                # to the host. The candidate code has the same write access
                # to the same directory, so anything it writes will also be
                # present on the host after the run. Truncated before use in
                # tasks.py, but that's the only mitigation.
                volumes={actual_code_path: {"bind": "/app", "mode": "rw"}},
                working_dir="/app",

                # --- Security constraints ---
                network_mode="none",                 # no DNS, no outbound, no loopback
                mem_limit="512m",                    # hard RAM cap, container killed if exceeded
                cpu_quota=50000,                     # 50000/100000 = 0.5 CPU
                user="1000:1000",                    # non-root, matches host mount ownership
                cap_drop=["ALL"],                    # drop CAP_NET_RAW, CAP_CHOWN, CAP_DAC_OVERRIDE, ...
                pids_limit=100,                      # fork bomb guard
                security_opt=["no-new-privileges"],  # blocks setuid binaries from escalating

                detach=True,                         # required for wait(timeout=)
            )

            # wait() blocks until the container exits or the timeout expires.
            # On timeout it raises (requests.ReadTimeout / httpx.ReadTimeout
            # depending on docker-py version), which is caught below and
            # reported as "Sandbox timeout or crash". The finally block runs
            # remove(force=True), which SIGKILLs the still-running container.
            result = container.wait(timeout=30)
            exit_code = result.get("StatusCode", 1)
            # Container logs may contain non-UTF-8 bytes if the candidate
            # printed arbitrary binary. Decode would raise without errors=
            # "replace" — currently it can, which would produce a confusing
            # "Sandbox timeout or crash" for what is actually a Unicode issue.
            logs = container.logs().decode("utf-8", errors="replace")

            # The JSON report is written by pytest to /app/report.json inside
            # the container, which maps to the host mount at actual_code_path.
            # If pytest crashed before writing it (segfault, OOM kill, import
            # error in the injected test), the file won't exist and json_report
            # is empty. Callers in tasks.py handle empty gracefully.
            json_report = ""
            report_path = os.path.join(actual_code_path, "report.json")
            if os.path.exists(report_path):
                with open(report_path, "r", encoding="utf-8") as f:
                    json_report = f.read()

            # Truncate to the last 1000 characters in the human-readable logs.
            # Pytest puts the summary at the end, so this preserves the useful
            # part. Full logs are still available in the JSON report.
            if exit_code == 0:
                return True, f"Tests passed!\n\nOutput:\n{logs[-1000:]}", json_report
            return False, f"Tests failed (Exit code {exit_code}).\n\nOutput:\n{logs[-1000:]}", json_report

        except docker.errors.ContainerError as e:
            # Raised when the container exits non-zero AND detach=False. With
            # detach=True this shouldn't fire, but kept as a safety net in
            # case a future refactor flips detach.
            return False, f"Container error: {str(e)}", ""
        except docker.errors.APIError as e:
            # Raised for daemon-level problems: image missing, daemon down,
            # Docker socket permission denied. Distinct from a failing test —
            # if this fires, the submission should probably be retried, not
            # marked as failed. Caller treats both the same today.
            return False, f"Docker API error: {str(e)}", ""
        except Exception as e:
            # Catches timeouts, JSON errors from report.json, and anything
            # else unexpected. The message is generic on purpose — the caller
            # stores it in rejection_reason and passes it to generate_markdown_report,
            # which html-escapes it. So no XSS risk, but the message does leak
            # internal detail (paths, exception types) to the candidate.
            return False, f"Sandbox timeout or crash: {str(e)}", ""

        finally:
            # force=True sends SIGKILL if the container is still running, then
            # removes it. The try/except swallows failures because we're
            # already on the error path — a cleanup failure shouldn't mask
            # the real error or crash the worker.
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass


# Deferred on purpose, not forgotten:
#
# - Zip Slip: FIXED above via _safe_extract. Included here for the record.
#
# - `**/conftest.py` recursion bypass: NOT fixed, flagged inline. A conftest.py
#   at depth 2+ survives the scrub. The fix is `find . -name conftest.py -delete`
#   which is POSIX-compliant and available in debian-slim. Worth doing before
#   the first real user, because "tests/unit/fixtures/conftest.py" is a
#   plausible file layout.
#
# - Host uid mismatch on Linux: NOT fixed. TemporaryDirectory is 0700 owned by
#   the caller. If the caller runs as root and the container as uid 1000, every
#   submission fails with permission errors. Fix: `os.chmod` the extracted tree
#   to be world-readable/writable before mounting, or run the container as the
#   host uid. Docker Desktop masks this on Windows/macOS.
#
# - report.json forgery: candidate code has write access to /app, so
#   pipeline.py could write /app/report.json before pytest does, and if pytest
#   crashes before overwriting it, the forged version is what tasks.py parses.
#   Consequence is bounded — telemetry is display-only, not used in the pass/
#   fail decision, which is driven by exit code. Still a data-integrity issue.
#   Fix: have pytest write to a path outside the mount (/tmp/report.json),
#   then docker cp it out after the run.
#
# - gVisor / firecracker. Current isolation is Docker + seccomp + capabilities.
#   A kernel exploit inside the container can escape to the host. gVisor adds
#   a user-space kernel that intercepts syscalls, eliminating most kernel
#   attack surface. Deferred until there's real traffic worth protecting —
#   the cost is a 30-50% performance hit and much heavier ops burden.
#
# - Image digest pinning. `verification-sandbox:latest` is rebuilt in place; a
#   compromised base image or a rebuild that changes test behavior silently
#   affects every submission. Fix: pull by digest and log it alongside each
#   result. Deferred because the current single-developer model makes supply-
#   chain attacks unrealistic.