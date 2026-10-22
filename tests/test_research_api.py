"""API boundary checks use isolated synthetic fixtures, never user research data."""
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from workbench.server import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        yield client


def measurement(**changes):
    return {"material": {"repeat_smiles": "[*]CC[*]"}, "property": "tg", "value": 80, "unit": "C",
            "method": "DSC", "conditions": {"heating_rate_K_min": 10},
            "source": "synthetic API test fixture, not research evidence", "origin": "demo", **changes}


def test_measurement_review_atomicity_and_snapshot(client, tmp_path):
    assert client.get("/api/research/overview").json()["measurements"] == []
    created = client.post("/api/research/measurements", json=measurement())
    assert created.status_code == 201, created.text
    assert created.json()["value"] == pytest.approx(353.15)
    assert created.json()["verified"] is False
    result = client.post("/api/research/measurements/import", json={"rows": [measurement(), measurement(unit="MPa")]})
    assert result.status_code == 422
    assert len(client.get("/api/research/measurements").json()) == 1
    assert client.post("/api/research/models", json={"property": "tg"}).status_code == 422
    backup = tmp_path / "sync folder"
    backup.mkdir()
    assert client.put("/api/research/settings", json={"backup_directory": str(backup)}).status_code == 200
    response = client.post("/api/research/snapshots", json={})
    assert response.status_code == 201, response.text
    snapshot = response.json()
    assert snapshot["counts"] == {"experiments": 0, "measurements": 1}
    assert snapshot["backup"]["copied"] and not snapshot["backup"]["cloud_upload_verified"]
    downloaded = client.get(snapshot["download_url"])
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert set(archive.namelist()) == {"metadata.json", "experiments_master.parquet", "measurements.parquet"}
        metadata = json.loads(archive.read("metadata.json"))
        for entry in metadata["files"]:
            assert hashlib.sha256(archive.read(entry["name"])).hexdigest() == entry["sha256"]
            assert (backup / ("PolymerLab-snapshot-" + snapshot["id"]) / entry["name"]).read_bytes() == archive.read(entry["name"])
    assert client.get("/api/research/snapshots/invalid/download").status_code == 404


def test_curve_pdf_and_input_limits(client):
    response = client.post("/api/research/curves", json={"kind": "tensile", "text": "strain,stress\n0,0\n0.1,2\n0.2,4\n0.3,6\n",
        "options": {"strain_unit": "%", "stress_unit": "MPa", "linear_region": [0, .3]}})
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["measurement_draft"]["value"] == pytest.approx(2000)
    assert draft["review_required"] and not draft["verified"]
    assert client.get("/api/research/measurements").json() == []
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(stream)
    document = client.post("/api/research/documents?filename=fixture.pdf", content=stream.getvalue(), headers={"Content-Type": "application/pdf"})
    assert document.status_code == 201, document.text
    assert document.json()["review_required"] and document.json()["measurements"] == []
    assert document.json()["source_sha256"] == hashlib.sha256(stream.getvalue()).hexdigest()
    assert client.post("/api/research/documents", content=b"not a pdf", headers={"Content-Type": "application/pdf"}).status_code == 422
    assert client.post("/api/research/measurements/preview", json={"text": "x" * 2_000_001}).status_code == 422


def test_auto_campaign_indexes_completed_jobs(client):
    payload = {"name": "isolated API fixture", "candidates": [
        {"name": f"chain {n}", "route": "polymer_cg", "engine": "numpy", "monomers": n, "chains": 1, "steps": 20}
        for n in (4, 5, 6)], "objective": {"source": "feature", "name": "rg_mean", "unit": "sigma", "direction": "min"},
        "budget": 2, "batch_size": 1, "backend": "sklearn"}
    response = client.post("/api/research/campaigns", json=payload)
    assert response.status_code == 201, response.text
    campaign_id = response.json()["id"]
    assert client.get("/api/jobs").json() == []
    assert client.post(f"/api/research/campaigns/{campaign_id}/start", json={}).status_code == 200
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        campaign = client.get(f"/api/research/campaigns/{campaign_id}").json()
        if not campaign["automatic"]:
            break
        time.sleep(.1)
    assert campaign["attempted"] == 2 and not campaign["automatic"], campaign
    experiments = client.get("/api/research/experiments").json()
    assert len(experiments) == 2
    assert all(r["features"]["values"]["rg_mean"]["unit"] == "sigma" for r in experiments)
    assert all(r["features"]["equilibrated"] is False for r in experiments)
    assert client.post("/api/research/measurements", json=measurement(material=None, job_id=experiments[0]["job_id"], origin="experimental")).status_code == 422
    assert client.post(f"/api/research/campaigns/{campaign_id}/iterate", json={}).status_code in {200, 409, 422}
    assert len(client.get("/api/jobs").json()) == 2


def test_bridge_preparation_is_not_simulation(client):
    response = client.post("/api/research/bridges", json={"bridge_track": "martini3", "bridge_mode": "prepare", "monomers": 5, "chains": 1})
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + identifier).json()
        if job["status"] not in {"queued", "running", "cancelling"}:
            break
        time.sleep(.1)
    assert job["status"] in {"prepared", "blocked"}, job
    assert job["result"]["simulation_executed"] is False
    diagnosis = client.get("/api/research/diagnostics/" + identifier).json()
    assert diagnosis["llm_used"] is False
    assert client.post(f"/api/research/diagnostics/{identifier}/retry", json={"rule_id": "unstable_atoms"}).status_code in {409, 422}


def test_gpc_curve_drafts_keep_analysis_provenance_when_saved(client):
    from tests.test_research_data import gpc_fixture
    text, options = gpc_fixture()
    response = client.post("/api/research/curves", json={"kind": "gpc", "text": text, "options": options})
    assert response.status_code == 200, response.text
    drafts = response.json()["measurement_drafts"]
    assert [d["property"] for d in drafts] == ["mn", "mw", "dispersity", "mz"]
    assert client.get("/api/research/measurements").json() == []
    saved = client.post("/api/research/measurements", json={**drafts[2], "material": {"repeat_smiles": "[*]CC[*]"},
                        "source": "synthetic API test fixture, not research evidence", "origin": "demo"})
    assert saved.status_code == 201, saved.text
    assert saved.json()["verified"] is False
    assert saved.json()["analysis"]["calibration_sha256"] == drafts[2]["analysis"]["calibration_sha256"]
    bad = client.post("/api/research/curves", json={"kind": "gpc", "text": text, "options": {**options, "calibration_order": 4}})
    assert bad.status_code == 422


def test_martini_simulation_job_is_indexed_with_protein_polymer_features(client):
    from workbench import bridges, forcefields
    caps = bridges.bridge_capabilities()
    if not all(caps["executables"][name] for name in ("martinize2", "polyply", "gmx")):
        pytest.skip("Requires installed martinize2, polyply and GROMACS")
    try:
        forcefields.martini3_parameters()
    except OSError as exc:
        pytest.skip(f"Pinned Martini 3 parameters are not available offline: {exc}")
    pdb = (Path(__file__).parent / "fixtures" / "alanine6.pdb").read_text()
    created = client.post("/api/research/bridges", json={"name": "synthetic Martini pilot", "bridge_mode": "simulate", "pdb_text": pdb,
                                                         "secondary_structure": "CCCCCC", "monomers": 6, "chains": 1, "box_nm": 5.5,
                                                         "equilibration_steps": 200, "steps": 500})
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    for _ in range(600):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            break
        time.sleep(0.5)
    assert job["status"] == "completed", job.get("error") or job["log"][-2000:]
    assert job["result"]["simulation_executed"] is True
    experiment = next(row for row in client.get("/api/research/experiments").json() if row["job_id"] == job_id)
    assert experiment["features"]["actual_dynamics"] is True
    assert experiment["features"]["values"]["contact_count"]["unit"] == "count"
    assert experiment["features"]["compatibility"]["force_field"].startswith("Martini 3.0.0")
    assert client.post("/api/research/bridges", json={"bridge_track": "amber", "bridge_mode": "simulate"}).status_code == 422
