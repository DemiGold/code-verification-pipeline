"""Pydantic models for request bodies crossing the trust boundary.

Two categories: internal admin payloads (UserCreate) and GitHub webhook
payloads. The webhook models deliberately cover only the fields this app
reads — GitHub's real payload is ~80 fields, and validating all of it would
mean chasing their schema every time they add something we don't care about.
"""

from pydantic import BaseModel


class UserCreate(BaseModel):
    github_username: str
    email: str | None = None
    display_name: str | None = None


# GitHub nests each piece of the payload. We mirror that nesting so
# validation errors point at the exact failing field rather than "payload".


class GitHubSender(BaseModel):
    login: str  # maps to users.github_username in the DB


class GitHubRepository(BaseModel):
    clone_url: str


class GitHubCommit(BaseModel):
    # Only the message is read — used to detect the bot's own audit commits
    # and skip them (see AUDIT_COMMIT_MARKER in app/routes/webhook.py).
    message: str


class GitHubWebhookPayload(BaseModel):
    # Only "refs/heads/main" is processed; other refs are ignored downstream
    # rather than rejected, so force-pushes and tag pushes fail gracefully.
    ref: str

    sender: GitHubSender
    repository: GitHubRepository

    # Full SHA of the pushed commit. Not the same as head_commit.id — after
    # is the tip regardless of how many commits were batched in the push.
    after: str

    # Optional because GitHub omits head_commit on branch-delete events,
    # where the rest of the payload still validates.
    head_commit: GitHubCommit | None = None