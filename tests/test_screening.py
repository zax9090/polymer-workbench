"""Reference-data screening: PI1070 surrogate, Hansen solvent/sphere/blend screens and their API/MCP boundary."""
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from workbench import compatibility, screening
from workbench.server import create_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PS = {"delta_d": 18.5, "delta_p": 4.5, "delta_h": 2.9, "radius": 8.0}


def test_vendored_reference_data_matches_manifest():
    manifest = json.loads((ROOT / "workbench/data/reference_data.json").read_text())["datasets"]
    for meta in manifest.values():
        data = (ROOT / "workbench/data" / meta["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == meta["file_sha256"]
        assert (ROOT / "workbench/data" / meta["license_file"]).is_file()
    assert manifest["radonpy_pi1070"]["sha256"] == "4ee41d526db3d03eb5b83010672b4e63a4d51a114871a2187ca7fd57d30556a4"
    assert manifest["radonpy_pi1070"]["rows"] == 1077 and manifest["hsp_solvents"]["rows"] == 248
    catalog = {repo["id"]: repo for repo in json.loads((ROOT / "tools/rea/extension_catalog.json").read_text())}
    for name, repo_id in (("radonpy_pi1070", "radonpy-pi1070"), ("hsp_solvents", "hspipy")):
        pinned = next(f for f in catalog[repo_id]["source_files"] if f["path"] == manifest[name]["path"])
        assert pinned["sha256"] == manifest[name]["sha256"] and catalog[repo_id]["commit"] == manifest[name]["commit"]


@pytest.mark.skipif(not (ROOT / "evidence/extensions/sources/radonpy-pi1070/data/PI1070.csv").is_file(), reason="pinned upstream sources not fetched")
def test_vendored_subsets_rebuild_from_pinned_upstream():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_reference_data", ROOT / "scripts/build_reference_data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, data in module.outputs().items():
        assert (ROOT / "workbench/data" / name).read_bytes() == data, name


def test_repeat_unit_validation_and_oligomer():
    from rdkit import Chem
    for bad in ("CC", "*CC", "*C=*", "*CC*.*O*", "**"):
        with pytest.raises(ValueError):
            screening.fingerprint_counts(bad)
    assert Chem.MolToSmiles(screening.oligomer(screening.repeat_unit("*CC*"))) == "CCCCCC"
    assert Chem.MolToSmiles(screening.oligomer(screening.repeat_unit("[*]OCC[*]"))) == Chem.CanonSmiles("OCCOCCOCC")
    assert screening.canonical_repeat("[*]CC([*])c1ccccc1") == screening.canonical_repeat("*C(C*)c1ccccc1")


def test_minmax_kernel_is_exact():
    rng = np.random.default_rng(3)
    train, query = rng.integers(0, 5, (12, 30)), rng.integers(0, 8, (4, 30))
    kernel = screening._MinMax(train)
    brute = np.array([[np.minimum(a, b).sum() / np.maximum(a, b).sum() for b in train] for a in query])
    assert np.allclose(kernel.kernel(query), brute)


def test_surrogate_reproduces_reference_and_reports_domain():
    pe = screening.SURROGATE.predict("[*]CC[*]")
    assert pe["applicability"]["in_reference_set"] == ["PI1"] and pe["applicability"]["domain"] == "inside"
    density = pe["values"]["bulk_density"]
    assert density["unit"] == "g/cm^3" and abs(density["value"] - 0.838) < 0.03 and 0 < density["std"] < 0.05
    assert pe["values"]["bulk_modulus"]["unit"] == "GPa" and 1 < pe["values"]["bulk_modulus"]["value"] < 5
    assert pe["nearest_reference"][0]["monomer_id"] == "PI1"
    remote = screening.SURROGATE.predict("*[Si](*)(C)C", ["bulk_density"])
    assert list(remote["values"]) == ["bulk_density"]
    assert remote["values"]["bulk_density"]["std"] >= density["std"]
    with pytest.raises(ValueError):
        screening.SURROGATE.predict("*CC*", ["tg"])


def test_cross_validation_is_honest_about_error():
    report = screening.SURROGATE.cross_validation()["properties"]
    assert report["bulk_density"]["r2"] > 0.85 and report["refractive_index"]["r2"] > 0.9
    for name, row in report.items():
        assert row["coverage_2sigma"] > 0.9, name
        inside, outside = row["by_domain"]["inside"], row["by_domain"]["outside"]
        assert inside["mae"] < outside["mae"], name


def test_own_simulations_are_paired_by_repeat_unit_and_unit():
    prediction = screening.SURROGATE.predict("*CC*", ["bulk_density"])
    record = {"route": "polymer_atomistic", "job_id": "a" * 32, "name": "PE bulk", "status": "completed",
              "material": {"repeat_smiles": prediction["repeat_smiles"]}, "config": {"temperature_kelvin": 300, "protocol_scale": 0.01},
              "features": {"values": {"bulk_density": {"value": 0.80, "unit": "g/cm^3", "radonpy_criteria_met": False}}}}
    other = {**record, "material": {"repeat_smiles": "*CC(*)C"}}
    rows = screening.own_simulations(prediction, [record, other])
    assert len(rows) == 1 and rows[0]["radonpy_criteria_met"] is False
    assert rows[0]["values"]["bulk_density"]["minus_reference"] == pytest.approx(0.80 - prediction["values"]["bulk_density"]["value"])


def test_hansen_distance_chi_and_solvent_ranking():
    toluene = compatibility.find_solvent("toluene")
    assert list(toluene["hsp"]) == [18.0, 1.4, 2.0] and toluene["molar_volume"] == 106.8
    assert compatibility.distance([18.5, 4.5, 2.9], [18.0, 1.4, 2.0]) == pytest.approx(np.sqrt(4 * 0.25 + 3.1 ** 2 + 0.9 ** 2))
    ra = compatibility.distance([18.5, 4.5, 2.9], toluene["hsp"])
    assert compatibility.chi(106.8, ra, 298.15) == pytest.approx(0.6 * 106.8 * ra ** 2 / (4 * 8.314462618 * 298.15))
    ranked = compatibility.rank_solvents(PS, names=["Water", "Toluene", "Hexane", "Chloroform"], blends=3)
    verdict = {row["name"]: row["prediction"] for row in ranked["solvents"]}
    assert verdict == {"Toluene": "inside", "Chloroform": "inside", "Hexane": "outside", "Water": "outside"}
    assert [row["name"] for row in ranked["solvents"]][:2] == ["Toluene", "Chloroform"]
    full = compatibility.rank_solvents(PS, limit=5, blends=3)
    assert full["evaluated"] == 248 and len(full["solvents"]) == 5
    for pair in full["blends"]["nonsolvent_pairs"]:
        assert pair["red"] < 1 and min(pair["component_ra"]) >= PS["radius"]
        assert sum(pair["volume_fractions"]) == pytest.approx(1.0)
    in_cal = compatibility.rank_solvents({"delta_d": 18.5 / 2.0455, "delta_p": 4.5 / 2.0455, "delta_h": 2.9 / 2.0455, "unit": "(cal/cm^3)^0.5"}, names=["Toluene"])
    assert in_cal["solvents"][0]["ra"] == pytest.approx(ra)
    for bad in ({"delta_d": 18.5, "delta_p": 4.5}, {**PS, "delta_d": 50}, {**PS, "unit": "bar"}):
        with pytest.raises(ValueError):
            compatibility.rank_solvents(bad)


def test_sphere_fit_matches_or_beats_hspipy_on_its_example():
    rows = list(csv.DictReader((FIXTURES / "hspipy_hsp_example.csv").open()))
    tests = [{"name": r["Solvent"].strip(), "delta_d": float(r["D"]), "delta_p": float(r["P"]), "delta_h": float(r["H"]), "good": float(r["Score"]) == 1} for r in rows]
    fit = compatibility.fit_sphere(tests)
    assert fit["datafit"] >= 0.97361  # HSPiPy 1.1.8 classic fit on the same data
    assert abs(fit["center"]["delta_d"] - 16.58) < 0.5 and abs(fit["radius"] - 11.58) < 1.0
    assert {d["property"] for d in fit["measurement_drafts"]} == {"hsp_delta_d", "hsp_delta_p", "hsp_delta_h", "hsp_radius"}


def test_sphere_fit_separates_synthetic_tests_and_flags_problems():
    rng = np.random.default_rng(7)
    center, radius = np.array([18.0, 8.0, 7.0]), 7.0
    tests, seen = [], set()
    while len(tests) < 40:
        point = center + rng.uniform(-9, 9, 3) * np.array([0.5, 1, 1])
        point = np.maximum(point, 0.0)
        d = compatibility.distance(point, center)
        if abs(d - radius) < 0.8 or tuple(point.round(3)) in seen:
            continue
        seen.add(tuple(point.round(3)))
        tests.append({"name": f"s{len(tests)}", "delta_d": float(point[0]), "delta_p": float(point[1]), "delta_h": float(point[2]), "good": bool(d < radius)})
    fit = compatibility.fit_sphere(tests)
    assert fit["datafit"] == pytest.approx(1.0) and fit["accuracy"] == 1.0 and not fit["wrong_inside"] and not fit["wrong_outside"]
    named = compatibility.fit_sphere([{"solvent": "Toluene", "good": True}, {"solvent": "Water", "good": False}])
    assert any("시험 용매가 2개" in w for w in named["warnings"])
    only_good = compatibility.fit_sphere([{"solvent": n, "good": True} for n in ("Toluene", "Chloroform", "Tetrahydrofuran")])
    assert any("하한값" in w for w in only_good["warnings"])
    for bad in ([{"solvent": "Water", "good": False}, {"solvent": "Hexane", "good": False}],
                [{"solvent": "Toluene", "good": True}, {"solvent": "toluene", "good": False}],
                [{"solvent": "Unobtainium", "good": True}, {"solvent": "Water", "good": False}],
                [{"solvent": "Toluene", "good": "yes"}, {"solvent": "Water", "good": False}]):
        with pytest.raises(ValueError):
            compatibility.fit_sphere(bad)


def test_blend_compatibility():
    ps = {"name": "PS", "delta_d": 18.5, "delta_p": 4.5, "delta_h": 2.9, "repeat_smiles": "*CC(*)c1ccccc1", "degree_of_polymerization": 1000}
    pmma = {"name": "PMMA", "delta_d": 18.6, "delta_p": 10.5, "delta_h": 5.1, "molar_volume": 86.5, "degree_of_polymerization": 1000}
    result = compatibility.blend_compatibility(ps, pmma)
    assert result["prediction"] == "phase_separation" and result["chi"] > result["chi_critical"]
    assert 95 < result["polymers"][0]["molar_volume"] < 115 and "PI1070" in result["polymers"][0]["molar_volume_source"]
    same = compatibility.blend_compatibility({**pmma, "name": "A"}, {**pmma, "name": "B"})
    assert same["chi"] == 0 and same["prediction"] == "miscible"
    short = compatibility.blend_compatibility({**ps, "degree_of_polymerization": 2}, {**pmma, "delta_p": 5.0, "degree_of_polymerization": 2})
    assert short["prediction"] == "miscible"
    with pytest.raises(ValueError):
        compatibility.blend_compatibility({**pmma, "degree_of_polymerization": 0}, pmma)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        yield client


def test_screening_api_and_hsp_measurements(client):
    card = client.get("/api/research/screening").json()
    assert card["surrogate"]["reference"]["rows"] == 1077 and card["solvents"]["rows"] == 248
    predicted = client.post("/api/research/screening/predict", json={"smiles": ["*CC*", "*CC(*)C"], "properties": ["bulk_density"]})
    assert predicted.status_code == 200
    assert [p["repeat_smiles"] for p in predicted.json()["predictions"]] == ["*CC*", "*CC(*)C"]
    assert predicted.json()["predictions"][0]["own_simulations"] == []
    assert client.post("/api/research/screening/predict", json={"smiles": ["CC"]}).status_code == 422
    assert client.post("/api/research/screening/predict", json={"smiles": ["*CC*"] * 21}).status_code == 422
    found = client.get("/api/research/screening/solvents", params={"query": "tolu"}).json()
    assert [s["name"] for s in found["solvents"]] == ["Toluene"]
    ranked = client.post("/api/research/screening/solvents", json={"polymer": PS, "limit": 3}).json()
    assert len(ranked["solvents"]) == 3 and ranked["solvents"][0]["red"] < 1
    fit = client.post("/api/research/screening/hsp-fit", json={"tests": [{"solvent": n, "good": g} for n, g in (
        ("Toluene", True), ("Chloroform", True), ("Tetrahydrofuran", True), ("Water", False), ("Methanol", False), ("Hexane", False))]})
    assert fit.status_code == 200
    draft = fit.json()["measurement_drafts"][0]
    saved = client.post("/api/research/measurements", json={"material": {"repeat_smiles": "*CC(*)c1ccccc1"}, "property": draft["property"],
                                                            "value": 9.0, "unit": "(cal/cm^3)^0.5", "method": draft["method"], "conditions": {},
                                                            "source": "synthetic API test fixture, not research evidence", "origin": "demo"})
    assert saved.status_code == 201 and saved.json()["value"] == pytest.approx(9.0 * 2.0455) and saved.json()["unit"] == "MPa^0.5"
    blend = client.post("/api/research/screening/blend", json={"a": {**PS, "molar_volume": 99.0, "degree_of_polymerization": 500},
                                                              "b": {**PS, "molar_volume": 99.0, "degree_of_polymerization": 500}})
    assert blend.status_code == 200 and blend.json()["prediction"] == "miscible"


def test_mcp_screening_tools_route_to_api():
    from workbench import mcp_server

    class Recorder:
        client = "mcp:test"

        def __init__(self):
            self.calls = []

        def request(self, method, path, payload=None, timeout=60):
            self.calls.append((method, path, payload))
            return {"ok": True}

    recorder = Recorder()
    tools = mcp_server.PolymerLabTools(recorder)
    for name in ("screen_polymer", "rank_solvents", "fit_hsp_sphere", "blend_compatibility"):
        definition = tools.tools[name]["definition"]
        assert definition["annotations"]["readOnlyHint"] is True
    mcp_server._check(tools.tools["screen_polymer"]["schema"], {"repeat_smiles": ["*CC*"], "include_validation": True})
    with pytest.raises(mcp_server.ToolError):
        mcp_server._check(tools.tools["screen_polymer"]["schema"], {"repeat_smiles": "*CC*"})
    tools.screen_polymer({"repeat_smiles": ["*CC*"], "include_validation": True})
    tools.fit_hsp_sphere({"tests": [{"solvent": "Toluene", "good": True}]})
    assert recorder.calls[0] == ("POST", "/api/research/screening/predict", {"smiles": ["*CC*"], "properties": None})
    assert recorder.calls[1][:2] == ("GET", "/api/research/screening/validation")
    assert recorder.calls[2][1] == "/api/research/screening/hsp-fit"
