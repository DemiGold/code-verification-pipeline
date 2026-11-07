# Code Verification Pipeline

An event-driven pipeline that runs untrusted candidate code in an isolated
Docker sandbox and routes verified results to a human reviewer. Candidates
submit fixes to realistic engineering challenges via `git push`; the pipeline
runs their code against official tests, and only submissions that pass are
queued for a live defence with a human reviewer.

The output is a verified **Talent Passport** (a machine-verified + human-signed
profile) that states exactly what was tested, what was defended, and — explicitly
— what was not.

This repo contains the backend I built: the API, the worker, the sandbox, the
validator dashboard, and the Passport API. The candidate-facing and employer-facing
frontends are separate codebases.

## The pipeline

```text
[ candidate git push ]
        │
        ▼
[ FastAPI webhook ]  ── HMAC-signed, loop-prevented, repo-matched to a challenge
        │
        ▼
[ Edge filter ]      ── zip structure + Python AST check, ~$0 cost, rejects before compute
        │
        ▼
[ Redis + Celery ]   ── decouples the HTTP request from the 30s sandbox run
        │
        ▼
[ Docker sandbox ]   ── injects official tests, runs pytest, emits JSON telemetry
        │                (no network · 512 MB · 0.5 CPU · non-root · 30s timeout)
        ▼
[ PostgreSQL ]       ── submissions, per-test telemetry, reviewer audit trail
        │
        ├──▶ report generator ── HTML email (Resend) + markdown commit back to the repo
        │
        ▼
[ Streamlit ]        ── validator dashboard, auth-gated, routes writes through the API
        │
        ▼
[ Passport API ]     ── public, employer-readable verified profile
```

The whole thing is a vertical slice. One candidate, one push, one sandbox run,
one reviewer, one Passport — proven end-to-end against two challenge tracks.

---

## Design notes

**The edge filter is intentionally cheap.** Before any compute is spent, the zip
is loaded into memory and every `.py` file is parsed with `ast.parse()`. No
execution, no disk I/O. A submission missing required files or containing a
syntax error gets rejected in milliseconds for free. The filter is *not* a
security boundary — it checks structure, not intent. Safety lives in the
sandbox.

**Sandbox isolation is layered.** A single control isn't enough for untrusted
code. The container runs with no network, 512 MB memory limit, 0.5 CPU quota,
non-root uid 1000, all Linux capabilities dropped, a process-count limit to
prevent fork bombs, `no-new-privileges` to block setuid escalation, and a 30s
wall-clock timeout. The candidate's test files and pytest configs are deleted
before the official tests are copied in — otherwise a candidate could shadow
the tests with their own.

**Automated pass is not verification.** A submission that clears the sandbox
lands in `PASSED` state and appears on the validator dashboard. It only becomes
`VERIFIED` after a human reviewer conducts a live defence and writes down what
was and wasn't proven. The `known_limitations` field is stored alongside the
`defence_notes` and surfaced on the Passport — the claim is bounded, on purpose.
Employers see both sides.

**Writes route through the API, not the dashboard.** The Streamlit UI reads
submissions directly from Postgres but every state change goes through
`POST /api/validator/verify`. The API enforces: bearer token, `VALIDATOR` or
`ADMIN` role, submission must be in `PASSED` state, and a race-condition guard
that returns 409 if two reviewers act on the same submission. Keeping this
enforcement in one place means the dashboard could be replaced with a CLI or a
next.js admin panel without duplicating the rules.

---

## Known limitations

These are known and deferred, not forgotten. Listing them because a verification
system that hides its own gaps isn't credible.

- **No SQL behavioural verification.** The SQL challenge test checks that
  `CREATE INDEX ... ON sales(sale_date)` appears in the submitted file. It does
  not run the query or measure latency. A candidate who adds the index but
  doesn't rewrite the query passes the automated test — the live defence is
  expected to catch this.
- **`conftest.py` scrub isn't fully recursive.** The sandbox command uses
  `**/conftest.py`, which behaves like `*/conftest.py` in `sh` (no globstar). A
  nested `tests/unit/conftest.py` would survive. Fix is `find . -name
  conftest.py -delete`.
- **`requirements.txt` isn't installed.** The sandbox ships with a fixed
  set of libraries. A candidate importing `pandas` or `requests` fails with
  `ModuleNotFoundError` even if their logic is correct. Fix requires either a
  pip-install phase with network (defeats the sandbox) or a mounted wheel cache.
- **The Celery task isn't idempotent.** A retry re-runs the sandbox and can
  duplicate the email and GitHub commit. Correct fix is to split the task into
  "run + persist" (retryable) and "notify" (idempotency-keyed).
- **`ssl_cert_reqs=ssl.CERT_NONE`** in the Celery config disables TLS
  verification for the Redis connection. Necessary for local Windows
  development; must be gated behind `app_env == "production"` before deploy.
- **Sandbox image isn't digest-pinned.** `python:3.11-slim` is a moving tag.
  Same Dockerfile, different images on different days.

---

## Tech stack

| Layer | Tool | Why |
|---|---|---|
| Language | Python 3.11 | Type hints, `asyncio`, `asyncpg` — the whole pipeline fits in one language. |
| API | FastAPI | Async, Pydantic-validated requests, OpenAPI generated for free. |
| Database | PostgreSQL (Supabase) | Relational integrity for the submission/verification chain, JSONB for telemetry payloads, RLS for the employer schema. |
| Queue | Redis (Upstash) + Celery | The sandbox takes 30s; the HTTP request cannot. Broker and result backend both live in Upstash. |
| Sandbox | Docker (`docker-py`) | Container isolation is the only reasonable barrier for untrusted Python. Runs are one-shot, not long-lived. |
| Email | Resend + `markdown` | Markdown → styled HTML for the audit report. Plain-text fallback included. |
| Validator UI | Streamlit | Pure Python. Runs as a separate process from the API. |
| Telemetry | `pytest-json-report` | Per-test durations and outcomes, captured inside the container and written to a file that maps back to the host mount. |

---

## Running locally

Three processes: the API, the Celery worker, and the Streamlit dashboard.

**1. Environment**

```bash
git clone https://github.com/DemiGold/code-verification-pipeline.git
cd code-verification-pipeline
python -m venv .venv
source .venv/Scripts/activate          # Windows Git Bash
# source .venv/bin/activate            # Linux / macOS
pip install -e ".[dev]"
```

**2. Configuration**

```bash
cp .env.example .env
```

Fill in Supabase, Upstash, GitHub, and Resend credentials. `VALIDATOR_TOKEN`
and `GITHUB_WEBHOOK_SECRET` have no defaults — the app fails at startup if
they're missing, rather than running with a placeholder secret.

**3. Database and sandbox image**

```bash
supabase link --project-ref <your-project-ref>
supabase db push
docker build --load -t verification-sandbox:latest docker/pytest-runner/
```

**4. Three terminals**

```bash
# API
uvicorn app.main:app --reload

# Worker
celery -A app.workers.celery_app.celery_app worker --loglevel=info --pool=solo

# Dashboard
streamlit run admin/dashboard.py
```

The API docs are at **[http://localhost:8000/docs](http://localhost:8000/docs)**.

To exercise the pipeline without a real GitHub repo:

```bash
curl -X POST http://localhost:8000/api/submissions/upload \
  -H "Authorization: Bearer $VALIDATOR_TOKEN" \
  -F "github_username=testuser" \
  -F "challenge_slug=data-engineering-etl-pool-exhaustion" \
  -F "file=@/path/to/challenge.zip"
```

---

## Repository layout

```text
code-verification-pipeline/
├── app/
│   ├── main.py                   # FastAPI entry + lifespan
│   ├── core/                     # config, database pool
│   ├── models/                   # Pydantic schemas (webhook, user)
│   ├── routes/                   # health · users · webhook · submissions · validator · passport
│   ├── services/                 # edge_filter · sandbox · github · report · email
│   └── workers/                  # celery_app, tasks
├── admin/
│   └── dashboard.py              # Streamlit validator UI
├── docker/
│   └── pytest-runner/
│       ├── Dockerfile            # sandbox image
│       ├── test_pipeline.py      # official ETL test
│       └── test_query.py         # official SQL test
├── supabase/migrations/          # 001 → 009 schema migrations
├── tests/                        # edge filter, schema shape, sandbox limits
├── docs/                         # onboarding + roadmap
├── .env.example
└── pyproject.toml
```

---

## What I'd build next

Concrete improvements, ordered by impact:

1. **Behavioural SQL verification** — seed a Postgres instance inside the
   sandbox, run `EXPLAIN ANALYZE` on the candidate's query and the reference
   query, compare timings. This closes the biggest gap in the current test
   suite.
2. **Split the Celery task** — separate the sandbox run (idempotent, retryable)
   from the email + GitHub push (needs idempotency keys). Without this, retries
   can duplicate side effects.
3. **gVisor or Firecracker** — Docker + seccomp + capability drops prevent most
   escapes, but a kernel exploit is still a path out. gVisor adds a user-space
   kernel that intercepts syscalls. Deferred until the traffic justifies the
   overhead.
4. **Digest-pin the sandbox image** — replace `python:3.11-slim` with
   `python:3.11-slim@sha256:…`. Same Dockerfile, same image, every time.
5. **Gate `CERT_NONE` on `app_env`** — flip TLS verification back on for
   non-development environments.

---

## Author

Built by [DemiGold](https://github.com/DemiGold).
Questions, corrections, and pull requests welcome.