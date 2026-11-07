import pytest
import io
import zipfile
import time

from app.services.sandbox import run_sandbox_tests


def _docker_daemon_reachable() -> bool:
    """
    True only if the Docker daemon is actually reachable.
    """
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


docker_available = pytest.mark.skipif(
    not _docker_daemon_reachable(),
    reason="Docker daemon not reachable (is Docker Desktop running?)",
)


@docker_available
def test_infinite_import_is_killed_within_timeout():
    """
    Ensures the 30-second Docker timeout actually kills hanging code.
    
    We put the infinite loop in `pipeline.py` (not the test file, which gets 
    deleted). When the official test file runs `from pipeline import process_data`,
    it will hang indefinitely.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("requirements.txt", "pytest\n")
        # Malicious code: hangs forever on import
        z.writestr("pipeline.py", "while True:\n    pass\n")

    start = time.time()
    # UPDATED: Unpack 3 values
    ok, logs, _ = run_sandbox_tests(buf.getvalue())
    elapsed = time.time() - start

    assert ok is False, "Infinite import should fail the sandbox, not pass"
    
    # We assert it finishes in under 90s to give room for container startup/teardown.
    assert elapsed < 90, f"Sandbox took {elapsed:.1f}s - the timeout did not trigger!"
    
    # Ensure the failure was due to the timeout/hang, not an ImportError
    assert "Sandbox timeout or crash" in logs, f"Expected timeout error, got: {logs}"