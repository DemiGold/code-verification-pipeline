import pytest
from pydantic import ValidationError
from app.models.schemas import GitHubWebhookPayload


def test_valid_github_payload_parses():
    payload = GitHubWebhookPayload(
        ref="refs/heads/main",
        sender={"login": "octocat"},
        repository={"clone_url": "https://github.com/o/r.git"},
        after="abc123",
        head_commit={"message": "Fix connection pooling"},
    )
    assert payload.sender.login == "octocat"
    assert payload.ref == "refs/heads/main"
    assert payload.head_commit.message == "Fix connection pooling"


def test_missing_sender_is_rejected():
    with pytest.raises(ValidationError):
        GitHubWebhookPayload(
            ref="refs/heads/main",
            repository={"clone_url": "https://github.com/o/r.git"},
            after="abc123",
        )


def test_missing_ref_is_rejected():
    """GitHub always sends `ref`; requiring it catches malformed or spoofed payloads."""
    with pytest.raises(ValidationError):
        GitHubWebhookPayload(
            sender={"login": "octocat"},
            repository={"clone_url": "https://github.com/o/r.git"},
            after="abc123",
        )