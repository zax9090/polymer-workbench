"""Real process-group cancellation regression, including a stubborn native child."""
import hashlib
import json
from pathlib import Path
import signal
import os
import textwrap
import threading
import time

import pytest
from fastapi.testclient import TestClient

from workbench import jobs
from workbench.models import JobConfig
from workbench.server import create_app


def until(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.03)
    pytest.fail("Timed out waiting for a real subprocess state change")


@pytest.mark.skipif(os.name != "posix", reason="Process group test requires POSIX")
def test_cancel_waits_for_stubborn_child_before_export(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    scripts = runtime / "scripts"
    scripts.mkdir(parents=True)
    # The leader uses the default TERM action. Its child deliberately ignores
    # TERM and continues writing, reproducing native subprocess orphaning.
    child = textwrap.dedent("""\
        from pathlib import Path
        import os, signal, sys, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        target = Path(sys.argv[1])
        (target / 'child.pid').write_text(str(os.getpid()))
        (target / 'ready').write_text('ready')
        with (target / 'stream.txt').open('a', buffering=1) as stream:
            while True:
                stream.write('native calculation still writing\\n')
                time.sleep(0.02)
    """)
    (scripts / "engine_worker.py").write_text(textwrap.dedent(f"""\
        import argparse, subprocess, sys, time
        parser = argparse.ArgumentParser()
        parser.add_argument('--config')
        parser.add_argument('--output')
        args = parser.parse_args()
        subprocess.Popen([sys.executable, '-c', {child!r}, args.output])
        while True:
            time.sleep(0.1)
    """))
    monkeypatch.setattr(jobs, "ROOT", runtime)
    data = tmp_path / "jobs"
    pgid = None
    try:
        with TestClient(create_app(data)) as client:
            created = client.post("/api/jobs", json={"name": "stubborn child cancellation"})
            assert created.status_code == 201
            job_id = created.json()["id"]
            directory = data / job_id
            until(lambda: (directory / "ready").exists())
            child_pid = int((directory / "child.pid").read_text())
            pgid = os.getpgid(child_pid)
            cancelled = client.post(f"/api/jobs/{job_id}/cancel")
            assert cancelled.json()["status"] == "cancelling"
            assert client.get(f"/api/jobs/{job_id}/download").status_code == 409

            def settled():
                job = client.get(f"/api/jobs/{job_id}").json()
                return job if job["status"] == "cancelled" else None

            finished = until(settled)
            assert finished["finished_at"] is not None
            proc_state = Path(f"/proc/{child_pid}/stat")
            if proc_state.exists():
                # A dead orphan may briefly remain a zombie until its reaper runs.
                assert proc_state.read_text().split()[2] in {"Z", "X"}
            before = (directory / "stream.txt").read_bytes()
            time.sleep(0.12)
            assert (directory / "stream.txt").read_bytes() == before
            manifest = json.loads((directory / "manifest.json").read_text())
            stream_record = next(row for row in manifest["files"] if row["path"] == "stream.txt")
            assert stream_record["sha256"] == hashlib.sha256(before).hexdigest()
            assert stream_record["bytes"] == len(before)
            assert client.get(f"/api/jobs/{job_id}/download").status_code == 200
            assert client.post(f"/api/jobs/{job_id}/cancel").json()["status"] == "cancelled"
    finally:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_queued_cancellation_never_starts_worker_and_keeps_manifest(tmp_path):
    manager = jobs.JobManager(tmp_path / "jobs")
    gate = threading.Event()
    held = manager.pool.submit(gate.wait)
    try:
        created = manager.create(JobConfig(name="queued cancellation").model_dump())
        job_id = created["id"]
        assert created["status"] == "queued"
        assert manager.cancel(job_id)["status"] == "cancelled"
        directory = manager.directory(job_id)
        manifest = json.loads((directory / "manifest.json").read_text())
        assert any(row["path"] == "config.json" for row in manifest["files"])
        assert not (directory / "worker.log").exists()
        gate.set()
        held.result(timeout=2)
    finally:
        gate.set()
        manager.close()
