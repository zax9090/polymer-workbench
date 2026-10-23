"""Structure analysis preserves job inputs and persists separate result records."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from workbench import mcp_server, structure_analysis
from workbench.server import create_app


JOB_ID = "a" * 32


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        yield client


def bulk_fixture(client, *, status="completed", route="polymer_atomistic", mode="bulk", pcff=False):
    directory = client.app.state.manager.root / JOB_ID
    directory.mkdir(exist_ok=True)
    config = {"route": route, "atomistic_mode": mode, "atomistic_forcefield": "pcff" if pcff else "gaff2"}
    (directory / "job.json").write_text(json.dumps({"id": JOB_ID, "name": "synthetic analysis fixture", "status": status, "config": config,
                                                  "created_at": "2026-10-23T00:00:00+00:00"}))
    coordinates = [(1, 1, 1), (3, 2, 1), (5, 5, 5), (7, 6, 7)]
    data = "Synthetic carbon cell, not research data\n\n4 atoms\n1 atom types\n\n0 12 xlo xhi\n0 12 ylo yhi\n0 12 zlo zhi\n\nMasses\n\n1 12.011\n\nPair Coeffs\n\n1 0.05 3.5\n\nAtoms # full\n\n"
    data += "\n".join(f"{i} {i} 1 0 {x} {y} {z} 0 0 0" for i, (x, y, z) in enumerate(coordinates, 1)) + "\n"
    (directory / ("final.data" if pcff else "eq3_last.data")).write_text(data)
    if not pcff:
        frames = []
        for frame in range(6):
            text = f"ITEM: TIMESTEP\n{frame * 100}\nITEM: NUMBER OF ATOMS\n4\nITEM: BOX BOUNDS pp pp pp\n0 12\n0 12\n0 12\nITEM: ATOMS id mol type x y z ix iy iz\n"
            text += "\n".join(f"{i} {i} 1 {x + frame * 0.05 * (-1)**i} {y} {z} 0 0 0" for i, (x, y, z) in enumerate(coordinates, 1)) + "\n"
            frames.append(text)
        (directory / "eq3.dump").write_text("".join(frames))
    (directory / "bulk_complete.json").write_text(json.dumps({"time_step_fs": 1.0, "temperature_kelvin": 300.0}))
    return directory


def post(client, analysis="rdf", parameters=None):
    return client.post(f"/api/jobs/{JOB_ID}/analysis", json={"analysis": analysis, "parameters": parameters or {}})


def hashes(directory):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir() if path.is_file()}


@pytest.mark.parametrize("analysis,parameters", [
    ("rdf", {"pairs": ["C-C"], "r_max": 5.0, "bins": 20}),
    ("msd", {"selection": "C", "remove_drift": False, "fit_window": [0.2, 0.8]}),
    ("free_volume", {"probes": [0.0, 1.0], "spacing": 1.0}),
    ("widom", {"gas": "He", "insertions": 100, "max_frames": 2, "seed": 7}),
])
def test_real_native_analysis_preserves_job_and_records_provenance(client, analysis, parameters):
    pytest.importorskip("workbench_native")
    directory = bulk_fixture(client)
    before = hashes(directory)
    response = post(client, analysis, parameters)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["analysis"] == analysis and "warnings" in result
    assert result["provenance"]["job_id"] == JOB_ID
    assert len(result["provenance"]["kernel_sha256"]) == 64
    assert len(result["provenance"]["analysis_code_sha256"]) == 64
    assert result["provenance"]["source_sha256"]["eq3_last.data"] == before["eq3_last.data"]
    assert hashes(directory) == before
    persisted = client.app.state.research_root / "analyses" / result["record_id"] / "result.json"
    assert json.loads(persisted.read_text()) == result
    assert client.get(result["download_url"]).json() == result
    json.dumps(result, allow_nan=False)
    if analysis == "rdf":
        assert len(result["r_angstrom"]) == 20 and len(result["pairs"][0]["g"]) == 20
    elif analysis == "msd":
        assert len(result["time_ps"]) == 6 and result["msd_angstrom2"][1] == pytest.approx(0.05**2)
    elif analysis == "free_volume":
        assert "grid_comparison" in result and 0 < result["occupied_fraction"] < 1
    else:
        assert result["frames"] == 2 and result["gas"] == "He"


@pytest.mark.parametrize("status", ["queued", "running", "cancelling", "failed", "prepared", "cancelled"])
def test_only_completed_jobs_can_be_analyzed(client, status):
    bulk_fixture(client, status=status)
    assert post(client).status_code == 409


def test_only_bulk_and_crosslink_are_supported(client):
    bulk_fixture(client, route="polymer_cg")
    assert post(client).status_code == 422
    bulk_fixture(client, mode="simulate")
    assert post(client).status_code == 422
    assert client.post("/api/jobs/not-a-job/analysis", json={"analysis": "rdf"}).status_code == 404


@pytest.mark.parametrize("analysis,parameters", [
    ("rdf", {"directory": "/tmp"}), ("rdf", {"bins": 0}), ("rdf", {"bins": True}),
    ("rdf", {"every": 0}), ("rdf", {"max_frames": 2001}), ("rdf", {"pairs": []}),
    ("msd", {"time_step_fs": -1}), ("msd", {"remove_drift": "false"}),
    ("free_volume", {"spacing": 0.001}), ("free_volume", {"probes": []}),
    ("widom", {"insertions": 200001}), ("widom", {"gas": "unsupported"}),
    ("widom", {"temperature": 0}), ("widom", {"seed": -1}),
])
def test_invalid_parameters_are_rejected_before_computing(client, analysis, parameters):
    bulk_fixture(client)
    response = post(client, analysis, parameters)
    assert response.status_code == 422, response.text
    assert "매개변수" in response.json()["detail"]


def test_unknown_analysis_and_extra_body_fields_rejected(client):
    bulk_fixture(client)
    assert post(client, "not-supported").status_code == 422
    response = client.post(f"/api/jobs/{JOB_ID}/analysis", json={"analysis": "rdf", "directory": "/tmp"})
    assert response.status_code == 422


def test_uninstalled_kernel_is_actionable(client, monkeypatch):
    bulk_fixture(client)
    monkeypatch.setattr(structure_analysis, "native", None)
    response = post(client)
    assert response.status_code == 503 and "Rust 계산 커널" in response.json()["detail"]


@pytest.mark.parametrize("name", ["job.json", "eq3_last.data", "final.data", "eq3.dump", "bulk_complete.json"])
def test_analysis_refuses_symlink_inputs(client, tmp_path, name):
    directory = bulk_fixture(client)
    outside = tmp_path / "outside"
    target = directory / name
    outside.write_bytes(target.read_bytes() if target.exists() else b"private")
    target.unlink(missing_ok=True)
    target.symlink_to(outside)
    assert post(client).status_code == 422


def test_analysis_refuses_symlink_job_directory(client, tmp_path):
    directory = bulk_fixture(client)
    outside = tmp_path / "outside-job"
    directory.rename(outside)
    directory.symlink_to(outside, target_is_directory=True)
    assert post(client).status_code == 422


def test_final_only_pcff_supports_rdf_but_rejects_msd_and_widom(client):
    pytest.importorskip("workbench_native")
    bulk_fixture(client, pcff=True)
    response = post(client, "rdf", {"r_max": 5.0})
    assert response.status_code == 200 and response.json()["frames"] == 1
    assert response.json()["warnings"]
    assert post(client, "msd").status_code == 422
    assert post(client, "widom").status_code == 422


def test_analysis_busy_response_keeps_health_endpoint_responsive(client, monkeypatch):
    pytest.importorskip("workbench_native")
    bulk_fixture(client)
    entered, release = threading.Event(), threading.Event()
    original = client.app.state.execution.compute
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(client.app.state.execution, "compute", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(post, client, "rdf", {"r_max": 5.0})
        try:
            assert entered.wait(5)
            assert post(client, "rdf", {"r_max": 5.0}).status_code == 429
            assert client.get("/api/health").json()["ok"]
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200


def test_mcp_adapter_runs_real_analysis_and_returns_server_errors(client):
    pytest.importorskip("workbench_native")
    bulk_fixture(client)
    class AppClient:
        def request(self, method, path, payload=None, timeout=30):
            response = client.request(method, path, json=payload)
            if response.is_error:
                raise mcp_server.ToolError(f"HTTP {response.status_code}: {response.text}")
            return response.json()
    tools = mcp_server.PolymerLabTools(AppClient())
    tool = tools.tools["analyze_job_structure"]
    assert tool["definition"]["annotations"]["readOnlyHint"] is False
    assert tool["definition"]["annotations"]["idempotentHint"] is False
    arguments = {"job_id": JOB_ID, "analysis": "rdf", "parameters": {"r_max": 5.0, "bins": 12}}
    mcp_server._check(tool["schema"], arguments)
    result = tool["handler"](arguments)
    assert result["analysis"] == "rdf" and len(result["pairs"][0]["g"]) == 12
    with pytest.raises(mcp_server.ToolError, match="HTTP 422"):
        tool["handler"]({**arguments, "parameters": {"bins": -1}})
    with pytest.raises(mcp_server.ToolError, match="invalid format"):
        mcp_server._check(tool["schema"], {**arguments, "job_id": "../../outside"})
