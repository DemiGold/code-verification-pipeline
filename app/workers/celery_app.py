"""Celery application configuration.

The API never runs the Docker sandbox inline. A webhook that took 30+ seconds
would tie up a uvicorn worker, and uvicorn's default of one worker means the
second webhook arriving during the first would queue indefinitely. So the
webhook drops a message into Redis and returns immediately; the Celery worker
process picks it up and runs the sandbox out-of-band.

Not covered here: the task function itself (app/workers/tasks.py). This file
only configures the app object that Celery's CLI entry point imports.
"""

import ssl

from celery import Celery

from app.core.config import settings


# Redis serves both roles here:
#   - broker:  where the API enqueues tasks, and the worker pulls from
#   - backend: where the worker writes task state (PENDING/SUCCESS/FAILURE)
#
# They share the same URL for simplicity. In a production setup you'd usually
# split them across two instances to avoid the broker being starved by result
# writes, but at current volume (~tens of tasks/day) that would be premature.
celery_app = Celery(
    "verification_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
    # Tells Celery which module to import so the task functions register
    # themselves against this app. Without it, starting the worker would
    # connect to Redis but have no tasks to run.
    include=["app.workers.tasks"],
)


celery_app.conf.update(
    # JSON everywhere. Pickle is the Celery default but would allow arbitrary
    # code execution if a broker message were tampered with — a real risk
    # since Redis has no per-queue auth. JSON costs a little size for
    # serialization, none of it matters at this scale.
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # All timestamps UTC. Matches Postgres (TIMESTAMPTZ), the report generator,
    # and the sandbox's JSON report. Mixing timezones across those would
    # create subtle ordering bugs in the audit trail.
    timezone="UTC",
    enable_utc=True,

    # --- SSL for rediss:// ---
    # Upstash provides a TLS endpoint. Python on Windows can't validate their
    # cert chain out of the box, and Celery refuses to start with a rediss://
    # URL unless ssl_cert_reqs is explicitly provided — hence the errors this
    # config works around.
    #
    # CERT_NONE disables verification entirely. That's not just a Windows
    # convenience — it means a MITM attacker on the network could intercept
    # broker traffic. Acceptable today because the worker runs on a trusted
    # local network. NOT acceptable in production: replace with CERT_REQUIRED
    # and install the proper CA bundle on the deploy host.
    # TODO: gate this behind settings.app_env == "development" so the
    # disabled verification can't silently follow a build to production.
    broker_use_ssl={"ssl_cert_reqs": ssl.CERT_NONE},
    redis_backend_use_ssl={"ssl_cert_reqs": ssl.CERT_NONE},
)


# Deferred on purpose, not forgotten:
#
# - task_acks_late = True. Currently a task is acknowledged on receipt, so if
#   the worker is killed mid-sandbox (deploy, OOM, Ctrl-C), the submission is
#   stuck in QUEUED forever — nothing retries it. acks_late acknowledges only
#   on completion, so a killed worker's task returns to the queue. The reason
#   it's off is subtle: acks_late with the default visibility_timeout of 1h
#   means a genuinely slow task can be re-delivered and run twice. Fixing
#   this properly needs both acks_late AND a tuned visibility_timeout matched
#   to the 30s sandbox limit.
#
# - task_time_limit / task_soft_time_limit. There's a 30s container.wait()
#   timeout inside the sandbox, but nothing above it — a hung sandbox.py call
#   (unreachable Docker daemon, deadlock in report generation) leaves the
#   task running for as long as the worker lives. task_soft_time_limit=60
#   would raise SoftTimeLimitExceeded inside the task so the code can clean
#   up; task_time_limit=90 hard-kills the worker process for the pathological
#   case. Standard defense-in-depth pair.
#
# - worker_concurrency. Defaults to CPU count. Each concurrent task spawns a
#   Docker container with 512MB RAM and 0.5 CPU allocated, so on an 8-core
#   host that's 8 containers × 512MB = 4GB RAM pressure and 4 CPU cores
#   committed. Needs explicit tuning before deploy — probably 2 is correct
#   for a free-tier Supabase connection pool.
#
# - Flower for monitoring. A web UI for task state, queue depth, worker
#   health. Adds an HTTP endpoint to expose, which is why it's deferred —
#   it needs to live behind auth before it can be useful. Worth adding the
#   moment there's more than one worker.
#
# - Split broker/backend into separate Redis instances. Required once task
#   volume makes result writes contend with broker reads. Not a today problem.
#
# - Retry policy on the task. Currently max_retries=3, countdown=10s, defined
#   inline in tasks.py. A global policy (autoretry_for, retry_backoff) set
#   here would be cleaner once there's more than one task.