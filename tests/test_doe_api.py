"""DOE persistence, measured-CSV optimization and the MCP bridge through the actual API."""
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from workbench import mcp_server
from workbench.server import create_app


FACTORS = [{"name": "temperature", "low": -1.0, "high": 1.0},
           {"name": "pressure", "low": -1.0, "high": 1.0}]


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        yield client


def design(client, **changes):
    response = client.post("/api/research/doe/design", json={"factors": FACTORS, "kind": "central_composite", "seed": 17, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def measured_csv(design_record):
    stream = io.StringIO()
    reader = csv.DictReader(io.StringIO(design_record["csv"]))
    writer = csv.DictWriter(stream, fieldnames=reader.fieldnames)
    writer.writeheader()
    for row in reader:
        x, y = float(row["temperature"]), float(row["pressure"])
        row["response"] = 12 - 2 * (x - 0.25) ** 2 - 3 * (y + 0.5) ** 2
        writer.writerow(row)
    return stream.getvalue()


def test_design_csv_to_response_surface_records_and_downloads(client):
    generated = design(client)
    original = client.get(generated["csv_download_url"])
    assert original.status_code == 200 and original.text == generated["csv"]
    rows = list(csv.DictReader(io.StringIO(original.text)))
    assert len(rows) == generated["total_runs"] and all(row["response"] == "" for row in rows)
    text = measured_csv(generated)
    response = client.post("/api/research/doe/analyze", json={"text": text, "factors": FACTORS, "response": "response",
        "prediction_points": [{"temperature": 0.0, "pressure": 0.0}]})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["record_type"] == "doe_analysis" and result["id"] != generated["id"]
    optimum = result["recommendation"]
    assert optimum["validated"] is False
    assert optimum["settings"]["temperature"] == pytest.approx(0.25, abs=2e-3)
    assert optimum["settings"]["pressure"] == pytest.approx(-0.5, abs=2e-3)
    assert optimum["predicted_response"] == pytest.approx(12, abs=1e-5)
    assert result["fit"]["r_squared"] == pytest.approx(1)
    assert result["predictions"][0]["predicted_response"] == pytest.approx(11.125)
    assert result["anova"]["pure_error"]["df"] == 2
    assert len(result["model_comparison"]) == 3
    assert result["research_workflow"]["automatic_execution"] is False
    assert all(row["response"] == "" for row in csv.DictReader(io.StringIO(result["confirmation_csv"])))
    assert result["input"] == {"file": "input.csv", "sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text.encode())}
    from workbench import doe
    compiled = sys.modules[doe.native.doe_fit.__module__]
    assert result["provenance"]["kernel_sha256"] == hashlib.sha256(Path(compiled.__file__).read_bytes()).hexdigest()
    assert result["provenance"]["analysis_code_sha256"] == hashlib.sha256(Path(doe.__file__).read_bytes()).hexdigest()
    assert result["provenance"]["kernel_version"] == doe.native.__version__
    assert generated["provenance"] == result["provenance"]
    assert client.get(f"/api/research/doe/{result['id']}").json() == result
    download = client.get(result["download_url"])
    assert download.status_code == 200 and download.json() == result
    assert "attachment" in download.headers["content-disposition"]
    assert client.get(result["csv_download_url"]).text == text
    root = client.app.state.research_root / "doe"
    assert (root / result["id"] / "input.csv").read_bytes() == text.encode()
    assert not list(root.glob(".staging-*"))
    assert client.get("/api/research/measurements").json() == []
    assert client.get("/api/research/doe?limit=1").json()["records"][0]["id"] == result["id"]
    assert len(client.get("/api/research/doe").json()["records"]) == 2


def test_records_are_immutable_and_survive_server_restart(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        first = design(client)
        root = client.app.state.research_root / "doe"
        files_before = {path.name: path.read_bytes() for path in (root / first["id"]).iterdir()}
        second = design(client)
        assert first["id"] != second["id"] and first["csv"] == second["csv"]
        assert files_before == {path.name: path.read_bytes() for path in (root / first["id"]).iterdir()}
    with TestClient(create_app(tmp_path / "jobs")) as client:
        assert client.get(f"/api/research/doe/{first['id']}").json() == first
        assert len(client.get("/api/research/doe").json()["records"]) == 2


@pytest.mark.parametrize("changes", [
    {"factors": FACTORS[:1]},
    {"factors": [FACTORS[0], FACTORS[0]]},
    {"factors": [FACTORS[0], {"name": "pressure", "low": 2, "high": 1}]},
    {"kind": "not-supported"}, {"center_points": -1}, {"seed": True},
    {"kind": "central_composite", "center_points": 0},
    {"factors": [{"name": f"x{i}", "low": -1, "high": 1} for i in range(5)]},
    {"directory": "/tmp/private"},
])
def test_invalid_designs_do_not_create_records(client, changes):
    response = client.post("/api/research/doe/design", json={"factors": FACTORS, "kind": "central_composite", **changes})
    assert response.status_code == 422, response.text
    assert client.get("/api/research/doe").json()["records"] == []


@pytest.mark.parametrize("changes", [
    {"response": "missing"}, {"objective": "target"}, {"model": "not-supported"},
    {"text": "temperature,pressure,response\n0,0,1\n1,1,2\n-1,-1,3\n", "model": "linear"},
    {"text": "temperature,pressure,response\n0,0,1\n1,,2\n-1,1,3\n"},
    {"text": "temperature,pressure,response\n0,0,1\n1,1,NaN\n-1,1,3\n"},
])
def test_invalid_analysis_does_not_save_partial_results(client, changes):
    generated = design(client)
    payload = {"text": measured_csv(generated), "factors": FACTORS, "response": "response", **changes}
    response = client.post("/api/research/doe/analyze", json=payload)
    assert response.status_code == 422, response.text
    assert len(client.get("/api/research/doe").json()["records"]) == 1
    assert not list((client.app.state.research_root / "doe").glob(".staging-*"))


def test_csv_size_limit_is_in_utf8_bytes(client):
    response = client.post("/api/research/doe/analyze", json={"text": "가" * 350000, "factors": FACTORS, "response": "response"})
    assert response.status_code == 413
    assert client.get("/api/research/doe").json()["records"] == []


def test_unavailable_doe_kernel_does_not_persist(client, monkeypatch):
    from workbench import doe
    monkeypatch.setattr(doe, "native", None)
    response = client.post("/api/research/doe/design", json={"factors": FACTORS})
    assert response.status_code == 503 and "Rust" in response.json()["detail"]
    assert client.get("/api/research/doe").json()["records"] == []


def test_record_storage_and_download_refuse_symlinks(client, tmp_path):
    root = client.app.state.research_root / "doe"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    response = client.post("/api/research/doe/design", json={"factors": FACTORS})
    assert response.status_code == 422 and not list(outside.iterdir())
    root.unlink()
    record = design(client)
    path = root / record["id"] / "result.json"
    saved = outside / "private.json"
    path.rename(saved)
    path.symlink_to(saved)
    assert client.get(record["download_url"]).status_code == 404
    assert client.get(record["csv_download_url"]).status_code == 404
    assert client.get("/api/research/doe").json()["unreadable_records"] == 1
    path.unlink()
    saved.rename(path)
    csv_path = path.parent / "design.csv"
    csv_path.unlink()
    csv_path.symlink_to(saved)
    assert client.get(record["csv_download_url"]).status_code == 404


def test_doe_parallel_limit_leaves_server_responsive(client, monkeypatch):
    from workbench import doe
    original = client.app.state.execution.compute
    entered, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(client.app.state.execution, "compute", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/api/research/doe/design", json={"factors": FACTORS})
        try:
            assert entered.wait(5)
            assert client.post("/api/research/doe/design", json={"factors": FACTORS}).status_code == 429
            assert client.get("/api/health").json()["ok"]
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 201


def test_doe_mcp_tools_persist_real_results(client):
    class AppClient:
        def request(self, method, path, payload=None, timeout=30):
            response = client.request(method, path, json=payload, headers={"x-polymer-client": "mcp:pytest"})
            if response.is_error:
                raise mcp_server.ToolError(f"HTTP {response.status_code}: {response.text}")
            return response.json()
    tools = mcp_server.PolymerLabTools(AppClient())
    create = tools.tools["create_experiment_design"]
    analyze = tools.tools["analyze_response_surface"]
    assert create["definition"]["annotations"]["readOnlyHint"] is False
    assert analyze["definition"]["annotations"]["readOnlyHint"] is False
    arguments = {"factors": FACTORS, "kind": "central_composite"}
    mcp_server._check(create["schema"], arguments)
    generated = create["handler"](arguments)
    result = analyze["handler"]({"text": measured_csv(generated), "factors": FACTORS, "response": "response"})
    assert result["submitted_via"] == "mcp:pytest"
    assert result["recommendation"]["validated"] is False
    assert len(client.get("/api/research/doe").json()["records"]) == 2
