# Sandbox image for running candidate pytest submissions.
#
# This image is built once (`docker build -t verification-sandbox:latest .`) and
# every submission reuses it. Nothing candidate-specific is baked in — the
# zip is mounted at /app at runtime by app/services/sandbox.py, which also
# applies the security constraints (network=none, cap_drop, pids_limit,
# non-root uid, memory/CPU caps). Those are runtime concerns, not image
# concerns, so they don't appear in this file.
#
# What this image needs to provide:
#   * A Python interpreter that can run the candidate's code
#   * pytest + the JSON reporter plugin
#   * The libraries the challenge starter repos depend on
#   * Official Verification tests, isolated from the candidate mount
#
# The security properties of the sandbox DO NOT come from this image. If you
# run `docker run verification-sandbox:latest` manually with default flags, it has
# network, root, and full capabilities. See sandbox.py for the actual
# confinement.
FROM python:3.11-slim

# Candidate code is mounted here at runtime. Matches working_dir="/app" in
# sandbox.py — if that ever changes, both must change together or the
# command in the container will exec in the wrong directory.
WORKDIR /app

# Upgrade build tooling first. The slim base image ships with an older pip,
# and some wheel resolution paths behave differently on older pip versions —
# these upgrades eliminate a class of "works on my machine" failures where
# the same requirements.txt resolves differently in this image than it does
# on a developer's machine.
#
# --no-cache-dir: pip normally caches downloads in /root/.cache, which
# bloats the image by 20-30% per package. We rebuild this image on every
# Dockerfile change, so caching only helps if we rebuild back-to-back.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Challenge runtime dependencies.
#
#   pytest, pytest-json-report   — test runner + telemetry output
#   sqlalchemy, psycopg2-binary  — imported by the ETL challenge's pipeline.py
#
# These are what the *current* challenge starter repos import. If a candidate
# adds `requests` or `pandas` to their pipeline.py, the sandbox import will
# fail with ModuleNotFoundError even though their logic is correct.
#
# TODO: this is a real gap — candidate requirements.txt is mounted but never
# installed. Fix is either (a) run `pip install -r requirements.txt` inside
# the container before pytest, which re-introduces network (defeats
# network_mode="none"), or (b) pre-bake a superset of reasonable libraries
# and document the allowed set. Option (b) is what we have today, just
# under-documented.
#
# --default-timeout=1000 makes pip wait up to ~16 minutes for a stalled
# connection before failing. This image is built on developer machines and
# CI, both of which occasionally have flaky networks. Without this, an
# interrupted build looks like a package-not-found error — the timeout
# message is what makes the real cause obvious.
RUN pip install --no-cache-dir --default-timeout=1000 \
    pytest \
    pytest-json-report \
    sqlalchemy \
    psycopg2-binary

# Official tests live outside /app on purpose.
#
# At runtime the candidate's zip is mounted at /app with rw permissions. If
# the official tests lived at /app/verification_official_tests, a candidate could
# ship a directory with the same name and shadow the originals — the
# sandbox's `cp /verification_official_tests/test_*.py .` command would then copy
# the candidate's version of the tests. Keeping them at the filesystem root,
# outside any mounted path, means the sandbox command reads them from a
# location the container's user can only read (not write), so nothing in the
# mounted zip can shadow them.
#
# The dir is created before COPY so we don't rely on COPY creating parents
# (which it does, but explicit is clearer for readers scanning for the
# candidate-vs-trusted boundary).
RUN mkdir -p /verification_official_tests
COPY test_pipeline.py /verification_official_tests/
COPY test_query.py /verification_official_tests/

# Deliberately CMD /bin/bash, not pytest. The container command is overridden
# at run time by sandbox.py with the actual test pipeline (scrub candidate
# files → inject official tests → run pytest). This default only matters if
# someone runs the image manually for debugging, where an interactive shell
# is the useful default.
CMD ["/bin/bash"]


# Deferred on purpose, not forgotten:
#
# - Digest pinning. `python:3.11-slim` is a moving tag — the same Dockerfile
#   can produce different images on different days because the base image
#   gets patched weekly. If a base-image change breaks candidate code (a
#   library updates its API, a system package gets renamed), it happens
#   silently mid-week. Fix: pin to `python:3.11-slim@sha256:...` and update
#   deliberately. Deferred because the current single-machine setup can't
#   roll out different images across environments anyway, but it's the
#   single most important reproducibility gap.
#
# - Non-root USER directive. sandbox.py passes user="1000:1000" at runtime,
#   which is enough for the pipeline. But if someone runs this image without
#   that flag, they get root in the container. Adding `USER 1000:1000` here
#   would make the safe behaviour the default — but that would also change
#   where pip installs packages (user-site instead of system), breaking the
#   pip lines above unless we add --user. Two changes, not one. Deferred.
#
# - requirements.txt installation. The gap flagged above. Fixing it properly
#   means either allowing network for pip, or shipping a wheel cache — both
#   are bigger design decisions than the fix looks. Currently the allow-list
#   is implicit in the packages installed above; a reviewer asking "what can
#   candidates import?" has to read this line to find out.
#
# - No HEALTHCHECK. Docker's orchestration tools (Swarm, Compose healthchecks)
#   use HEALTHCHECK to decide if a container is ready. Irrelevant here
#   because containers are short-lived one-shot runs, not long-lived services.
#   If the model ever changes to a persistent test-runner service, revisit.
#
# - Image size. Current image is ~250MB. Multi-stage build (builder + runtime)
#   could trim the pip cache and any build tooling, but --no-cache-dir already
#   removes most of it. The remaining weight is Python itself, which we can't
#   trim without going to alpine (glibc → musl swap, breaks psycopg2-binary).
#   Not worth the tradeoffs for a 30-50MB savings.