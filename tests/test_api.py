import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from workbench.server import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        yield client


def wait_job(client, job_id, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.1)
    pytest.fail("Job did not finish before deadline")


def test_real_workflow_and_reproducible_bundle(client):
    response = client.post("/api/jobs", json={"name": "검증 작업", "route": "polymer_cg", "engine": "numpy", "monomers": 6, "chains": 1, "steps": 20, "seed": 7})
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    job = wait_job(client, job_id)
    assert job["status"] == "completed", job
    assert job["result"]["model"]["calibrated"] is False
    assert job["result"]["metrics"]["summary"]["beads"] == 6
    bundle = client.get(f"/api/jobs/{job_id}/download")
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
        assert {"config.json", "result.json", "manifest.json", "trajectory.json"} <= set(z.namelist())
        assert json.loads(z.read("config.json"))["seed"] == 7
    rerun = client.post(f"/api/jobs/{job_id}/rerun")
    other = wait_job(client, rerun.json()["id"])
    assert other["result"]["metrics"]["summary"] == job["result"]["metrics"]["summary"]


def test_export_is_not_reported_as_predicted(client):
    response = client.post("/api/jobs", json={"route": "protein", "protein_mode": "export", "sequence": ">test\nACDEFGHIK"})
    assert response.status_code == 201, response.text
    job = wait_job(client, response.json()["id"])
    assert job["status"] == "prepared", job


def test_input_rejections_and_local_origin(client):
    for data in ({"steps": 100001}, {"timestep": 0.1}, {"route": "fake"}, {"route": "polymer_atomistic", "chains": 2}, {"route": "protein", "sequence": "C=C"}, {"engine": "numpy", "monomers": 200, "chains": 2}):
        assert client.post("/api/jobs", json=data).status_code == 422
    assert client.post("/api/jobs", json={}, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "attacker.invalid"}).status_code == 400


def test_cancel_queued_or_running_job(client):
    response = client.post("/api/jobs", json={"engine": "numpy", "steps": 20000, "monomers": 120, "chains": 2})
    job_id = response.json()["id"]
    cancelled = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert wait_job(client, job_id)["status"] == "cancelled"


def test_cg_cannot_be_sent_to_atomistic_packing(client):
    response = client.post("/api/jobs", json={"engine": "numpy", "steps": 10, "monomers": 4, "chains": 1})
    job_id = response.json()["id"]
    assert wait_job(client, job_id)["status"] == "completed"
    response = client.post("/api/jobs", json={"route": "packmol", "source_job_id": job_id})
    assert response.status_code == 422


def test_artifact_path_and_symlink_escape(client, tmp_path):
    response = client.post("/api/jobs", json={"engine": "numpy", "steps": 10, "monomers": 4, "chains": 1})
    job_id = response.json()["id"]
    wait_job(client, job_id)
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    (tmp_path / "jobs" / job_id / "escape.txt").symlink_to(outside)
    assert client.get(f"/api/jobs/{job_id}/artifacts/escape.txt").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/artifacts/%2E%2E%2F%2E%2E%2Fprivate.txt").status_code == 404
