"""Dry-run checks reject worker-only chemistry failures before a job exists; MCP results stay small."""
import json

import pytest
from fastapi.testclient import TestClient

from workbench import mcp_server, preflight
from workbench.server import create_app

pytest.importorskip("rdkit")
BULK = {"route": "polymer_atomistic", "atomistic_mode": "bulk", "repeat_smiles": "[*]CC[*]", "monomers": 24, "chains": 2}


def test_valid_bulk_config_reports_atom_counts_and_engines():
    report = preflight.check(BULK)
    assert report["valid"] and not report["errors"]
    assert report["estimate"]["repeat_unit"]["atoms"] == 6
    assert report["estimate"]["atoms_per_chain"] == 6 * 24 + 2
    assert report["estimate"]["total_atoms"] == 2 * (6 * 24 + 2)
    assert {row["engine"] for row in report["engines"]} >= {"RDKit", "LAMMPS", "RadonPy"}
    assert any("hours to days" in w for w in report["warnings"])


@pytest.mark.parametrize("monomer,candidate", [("C=Cc1ccccc1", "[*]CC([*])c1ccccc1"), ("C=C(C)C(=O)OC", "[*]CC([*])(C)C(=O)OC")])
def test_monomer_smiles_returns_repeat_unit_candidates(monomer, candidate):
    report = preflight.check({**BULK, "repeat_smiles": monomer})
    assert not report["valid"] and report["errors"][0]["field"] == "repeat_smiles"
    from rdkit import Chem
    found = {Chem.CanonSmiles(c) for c in report["estimate"]["repeat_unit"]["candidates"]}
    assert Chem.CanonSmiles(candidate) in found
    assert preflight.check({**BULK, "repeat_smiles": sorted(found)[0]})["valid"]


@pytest.mark.parametrize("change,field", [
    ({"monomers": 6, "chains": 3}, "chains"),
    ({"repeat_smiles": "[*]CC"}, "repeat_smiles"),
    ({"repeat_smiles": "[*]CC[*].CC"}, "repeat_smiles"),
    ({"repeat_smiles": "[*]=CC[*]"}, "repeat_smiles"),
])
def test_route_limits_and_attachment_rules(change, field):
    report = preflight.check({**BULK, **change})
    assert not report["valid"] and report["errors"][0]["field"] == field


def test_crosslink_resin_checks_and_schema_errors():
    cure = {"route": "polymer_atomistic", "atomistic_mode": "crosslink", "atomistic_forcefield": "pcff"}
    assert preflight.check(cure)["valid"]
    bad = preflight.check({**cure, "resin_smiles": "CCC1CO1"})
    assert not bad["valid"] and "epoxide" in bad["errors"][0]["message"]
    schema = preflight.check({"route": "quantum"})
    assert not schema["valid"] and schema["errors"][0]["field"] == "route"


def test_submission_rejects_chemistry_before_creating_a_job(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        response = client.post("/api/jobs", json={**BULK, "repeat_smiles": "C=Cc1ccccc1"})
        assert response.status_code == 422 and "연결점" in response.json()["detail"]
        assert client.get("/api/jobs").json() == []
        dry = client.post("/api/jobs/preflight", json={**BULK, "repeat_smiles": "C=Cc1ccccc1"}).json()
        assert dry["estimate"]["repeat_unit"]["candidates"]
        assert client.get("/api/jobs").json() == []


def test_mcp_validate_job_and_summaries_stay_small(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        class AppClient:
            def request(self, method, path, payload=None, timeout=30):
                response = client.request(method, path, json=payload)
                if response.is_error:
                    raise mcp_server.ToolError(f"HTTP {response.status_code}: {response.text}")
                return response.json()
        tools = mcp_server.PolymerLabTools(AppClient()).tools
        validate = tools["validate_job"]
        assert validate["definition"]["annotations"]["readOnlyHint"] is True
        assert not validate["handler"]({"config": {**BULK, "repeat_smiles": "C=C"}})["valid"]
        values = [100 + (i % 7) * 0.5 for i in range(3000)] + [130]
        text = "y\n" + "\n".join(str(v) for v in values)
        arguments = {"kind": "control_chart", "csv_text": text, "options": {"value": "y"}}
        summary = tools["analyze_process_data"]["handler"](arguments)
        full = tools["analyze_process_data"]["handler"]({**arguments, "detail": "full"})
        assert len(json.dumps(summary)) < 4000 < 100_000 < len(json.dumps(full))
        assert summary["points"] == {"omitted_items": 3001}
        assert summary["signalled_count"] > 20 and len(summary["signalled_points"]) == 20
        assert summary["signalled_points"][-1]["value"] == 130 and "beyond_3_sigma" in summary["signalled_points"][-1]["signals"]
        assert summary["dispersion"]["values"]["omitted_values"] == 3000


def test_summarize_keeps_scalars_and_short_lists():
    result = {"analysis": "rdf", "r_angstrom": list(range(200)), "pairs": [{"pair": "C-C", "g": [0.0] * 199 + [None],
              "highest_peak": {"r_angstrom": 1.5, "g": 3.2}}], "warnings": ["w"], "download_url": "/x"}
    summary = mcp_server.summarize(result)
    assert summary["r_angstrom"] == {"omitted_values": 200, "min": 0, "max": 199}
    assert summary["pairs"][0]["highest_peak"] == {"r_angstrom": 1.5, "g": 3.2}
    assert summary["pairs"][0]["g"]["omitted_values"] == 200
    assert summary["warnings"] == ["w"] and summary["download_url"] == "/x"


def test_rejection_message_lists_repeat_unit_candidates(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        detail = client.post("/api/jobs", json={**BULK, "repeat_smiles": "C=Cc1ccccc1"}).json()["detail"]
        assert "반복단위 후보" in detail and "[*]" in detail
