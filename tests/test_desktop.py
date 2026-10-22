"""Real service ownership, session authentication and durable desktop history."""
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from workbench.desktop import BackendSupervisor, command_for_mode, is_external_url, is_local_url


def until(function, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = function()
        if result:
            return result
        time.sleep(0.03)
    pytest.fail("Desktop backend did not reach the expected state")


def test_private_service_uses_ephemeral_port_and_rejects_other_clients(tmp_path):
    first = BackendSupervisor(tmp_path / "one" / "jobs").start()
    second = BackendSupervisor(tmp_path / "two" / "jobs").start()
    try:
        until(first.poll_ready)
        until(second.poll_ready)
        assert first.origin != second.origin
        assert first.request("/api/health")["ok"] is True
        assert second.request("/api/health")["local_only"] is True
        with pytest.raises(HTTPError) as error:
            urlopen(first.origin + "/api/jobs", timeout=2)
        assert error.value.code == 401
        first.token = "wrong-token"
        with pytest.raises(HTTPError) as error:
            first.request("/api/health")
        assert error.value.code == 401
        assert first.ready_file.stat().st_mode & 0o077 == 0
    finally:
        first.stop()
        second.stop()
    assert first.process.poll() is not None
    assert second.process.poll() is not None
    assert not first.ready_file.exists()


def test_workflow_result_survives_desktop_service_restart(tmp_path):
    data_dir = tmp_path / "jobs"
    backend = BackendSupervisor(data_dir).start()
    try:
        until(backend.poll_ready)
        job = backend.request("/api/jobs", payload={"name": "desktop lifecycle", "engine": "numpy", "monomers": 4, "chains": 1, "steps": 10})
        def completed():
            result = backend.request("/api/jobs/" + job["id"])
            return result if result["status"] not in {"queued", "running", "cancelling"} else None
        result = until(completed)
        assert result["status"] == "completed", result
        assert any(item["name"] == "metrics.json" for item in result["artifacts"])
    finally:
        backend.stop()
    restarted = BackendSupervisor(data_dir).start()
    try:
        until(restarted.poll_ready)
        history = restarted.request("/api/jobs")
        assert len(history) == 1
        assert history[0]["id"] == job["id"]
        assert history[0]["status"] == "completed"
    finally:
        restarted.stop()


@pytest.mark.skipif(os.name != "posix", reason="Worker shutdown uses POSIX process groups")
def test_closing_desktop_cancels_active_calculation_and_keeps_records(tmp_path):
    data_dir = tmp_path / "jobs"
    backend = BackendSupervisor(data_dir).start()
    try:
        until(backend.poll_ready)
        job = backend.request("/api/jobs", payload={"name": "close active desktop", "engine": "numpy", "monomers": 128, "chains": 1, "steps": 20000})
        until(lambda: backend.request("/api/jobs/" + job["id"])["status"] == "running")
        assert backend.active_jobs()
        backend.stop()
        record = json.loads((data_dir / job["id"] / "job.json").read_text())
        assert record["status"] == "cancelled"
        assert record["finished_at"]
        assert (data_dir / job["id"] / "manifest.json").is_file()
        assert backend.active_jobs() == []
    finally:
        backend.stop()


def test_navigation_trust_is_scoped_to_exact_backend_origin():
    origin = "http://127.0.0.1:41234"
    assert is_local_url(origin + "/static/app.js", origin)
    assert is_local_url(origin + "/#history", origin)
    for address in ("http://127.0.0.1:41235/", "http://localhost:41234/", "https://127.0.0.1:41234/", "file:///etc/passwd", "http://user@127.0.0.1:41234/", "http://127.0.0.1:bad/"):
        assert not is_local_url(address, origin)
    assert is_external_url("https://github.com/RadonPy/RadonPy")
    for address in ("file:///tmp/results", "http://example.com", "javascript:alert(1)", "https://user@example.com/", "https:///missing-host"):
        assert not is_external_url(address)


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM lifecycle regression requires POSIX")
def test_standalone_server_sigterm_unwinds_readiness_cleanup(tmp_path):
    """Uvicorn replays SIGTERM after shutdown; finally must still unlink readiness."""
    from workbench.paths import resource_root, runtime_environment
    ready = tmp_path / "ready.json"
    process = subprocess.Popen(command_for_mode("--server", "--port", "0", "--ready-file", str(ready),
        "--data-dir", str(tmp_path / "jobs")), cwd=resource_root(), env=runtime_environment(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        def started():
            if process.poll() is not None:
                pytest.fail("Standalone server failed to start: " + process.stdout.read())
            return ready.is_file()
        until(started)
        assert json.loads(ready.read_text())["pid"] == process.pid
        process.terminate()
        output, _ = process.communicate(timeout=15)
        assert process.returncode == 0, output
        assert not ready.exists(), output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
