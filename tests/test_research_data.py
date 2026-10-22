import csv
import io
import json
from pathlib import Path
import threading

import duckdb
import numpy as np
import pytest

from workbench.descriptors import extract_job_features
from workbench.ingestion import analyze_curve, parse_measurements_csv
from workbench.research_store import ResearchStore, material_identity, validate_measurement


def measurement(**changes):
    return {"material": {"repeat_smiles": "[*]CC[*]", "monomers": 20, "tacticity": "unspecified"},
            "property": "tg", "value": 100, "unit": "C", "method": "DSC midpoint; second heating",
            "origin": "experimental", "source": "lab notebook sample 17", "conditions": {"heating_rate_K_min": 10},
            **changes}


def make_cg(directory, status="completed"):
    directory.mkdir(parents=True, exist_ok=True)
    config = {"route": "polymer_cg", "monomers": 3, "chains": 2, "repeat_smiles": "[*]CC[*]"}
    job = {"id": directory.name, "name": "two chains", "status": status, "config": config, "created_at": "2026-01-01"}
    frames = []
    for scale in (1, 2):
        frames.append({"positions": [[0, 0, 0], [scale, 0, 0], [2*scale, 0, 0],
                                     [100, 0, 0], [100+scale, 0, 0], [100+2*scale, 0, 0]]})
    trajectory = {"unit": "sigma", "kind": "coarse_grained", "monomers": 3, "chains": 2,
                  "bonds": [[0, 1], [1, 2], [3, 4], [4, 5]], "frames": frames}
    for name, value in (("job.json", job), ("config.json", config), ("trajectory.json", trajectory), ("result.json", {"status": status})):
        (directory / name).write_text(json.dumps(value))
    return job


def test_units_identity_and_invalid_physical_values():
    assert validate_measurement(measurement())["value"] == pytest.approx(373.15)
    assert validate_measurement(measurement(property="young_modulus", value=2, unit="GPa"))["value"] == 2000
    assert validate_measurement(measurement(property="binding_affinity", value=2, unit="uM"))["value"] == 2000
    a = material_identity({"repeat_smiles": "[*]CC[*]", "monomers": 20})
    b = material_identity({"repeat_smiles": "C(C[*])[*]", "monomers": 20})
    assert a == b
    assert a["material_id"] != material_identity({"repeat_smiles": "[*]CC[*]", "monomers": 21})["material_id"]
    for change in ({"unit": "MPa"}, {"value": float("nan")}, {"value": -400}, {"verified": "true"}, {"source": ""}):
        with pytest.raises(ValueError):
            validate_measurement(measurement(**change))


def test_chain_metrics_do_not_combine_chain_separation(tmp_path):
    job = make_cg(tmp_path / "job")
    report = extract_job_features(tmp_path / "job", job)
    assert report["chain_count"] == 2
    assert report["values"]["rg_mean"]["value"] == pytest.approx(1.5 * np.sqrt(2/3))
    assert report["values"]["end_to_end_mean"]["value"] == 3
    assert report["values"]["rg_std"]["value"] == pytest.approx(np.sqrt(1/3))
    assert report["values"]["rg_mean"]["unit"] == "sigma"
    assert "density" in report["unavailable"]
    assert "dipole_fluctuation" in report["unavailable"]
    assert report["equilibrated"] is False
    trajectory_path = tmp_path / "job" / "trajectory.json"
    data = json.loads(trajectory_path.read_text())
    data["bonds"].append([2, 3])
    trajectory_path.write_text(json.dumps(data))
    broken = extract_job_features(tmp_path / "job", job)
    assert not broken["values"]
    assert "mapping" in broken["unavailable"]["trajectory_features"]


def test_atomistic_geometry_preview_does_not_claim_dynamics(tmp_path):
    (tmp_path / "trajectory.json").write_text(json.dumps({"unit": "angstrom", "kind": "atomistic", "elements": ["C", "C"],
        "bonds": [[0, 1]], "frames": [{"positions": [[0, 0, 0], [2, 0, 0]]}, {"positions": [[0, 0, 0], [3, 0, 0]]}]}))
    report = extract_job_features(tmp_path, {"config": {"route": "polymer_atomistic"}})
    assert report["values"]["rg_mean"]["value"] == pytest.approx(1.25)
    assert "rg_std" not in report["values"]
    assert report["actual_dynamics"] is False


def test_duckdb_snapshot_atomic_batches_and_material_links(tmp_path):
    store = ResearchStore(tmp_path / "research")
    job_dir = tmp_path / "jobs" / ("a" * 32)
    job = make_cg(job_dir)
    indexed = store.index_job(job, job_dir)
    assert indexed["is_demo"] is True
    assert "repeat_smiles" not in indexed["material"]
    with pytest.raises(ValueError, match="demonstration"):
        store.add_measurement(measurement(material=None, job_id=job["id"]))
    with pytest.raises(ValueError):
        store.add_measurements([measurement(), measurement(unit="MPa")])
    assert store.list_measurements() == []
    row = store.add_measurement(measurement(verified=True))
    assert row["unit"] == "K" and row["original_unit"] == "C"
    snapshot = store.export_snapshot(tmp_path / "snapshot")
    assert snapshot["counts"] == {"experiments": 1, "measurements": 1}
    con = duckdb.connect()
    assert con.execute("SELECT value, unit FROM read_parquet(?)", [str(tmp_path / "snapshot" / "measurements.parquet")]).fetchone() == (373.15, "K")
    con.close()
    with pytest.raises(ValueError, match="immutable"):
        store.export_snapshot(tmp_path / "snapshot")
    assert (tmp_path / "snapshot" / "metadata.json").is_file()


def test_simultaneous_store_instances_share_lock(tmp_path):
    first, second = ResearchStore(tmp_path), ResearchStore(tmp_path)
    failures = []
    def add(store, suffix):
        try:
            for index in range(3):
                store.add_measurement(measurement(source=f"sample {suffix} {index}"))
        except Exception as exc:
            failures.append(exc)
    threads = [threading.Thread(target=add, args=(s, i)) for i, s in enumerate((first, second))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures
    assert len(first.list_measurements()) == 6


def test_measurement_csv_requires_units_and_manual_verification():
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=["material_json", "property", "value", "unit", "method", "source", "origin"])
    writer.writeheader()
    writer.writerow({"material_json": '{"repeat_smiles":"[*]CC[*]","sample_id":"S1"}', "property": "tg", "value": 80,
                     "unit": "C", "method": "DSC midpoint", "source": "lab note 2", "origin": "experimental"})
    rows = parse_measurements_csv(stream.getvalue())
    assert rows[0]["value"] == pytest.approx(353.15)
    assert rows[0]["verified"] is False
    with pytest.raises(ValueError):
        parse_measurements_csv(stream.getvalue().replace(",C,", ",MPa,"))
    shortcut = parse_measurements_csv("repeat_smiles,property,value,unit,method,source,origin,verified\n[*]CC[*],tg,80,C,DSC,test fixture,experimental,true\n")
    assert shortcut[0]["material"]["repeat_smiles"]
    assert shortcut[0]["verified"] is False


def test_tensile_percent_conversion_and_explicit_region():
    text = "strain,stress\n0,0\n0.1,2\n0.2,4\n0.3,6\n0.4,7\n"
    result = analyze_curve("tensile", text, {"strain_unit": "%", "stress_unit": "MPa", "linear_region": [0, .3]})
    assert result["measurement_draft"]["value"] == pytest.approx(2000)
    assert result["review_required"] is True
    with pytest.raises(ValueError):
        analyze_curve("tensile", text, {"stress_unit": "MPa", "linear_region": [0, .3]})


def test_dsc_manually_selected_baselines_and_gpc_refusal():
    text = "temperature,heat_flow\n" + "\n".join(f"{x},{0 if x <= 20 else 1 if x >= 80 else (x-20)/60}" for x in range(0, 101, 5))
    result = analyze_curve("dsc", text, {"temperature_unit": "C", "heat_flow_unit": "W/g", "baseline_before": [0, 20], "baseline_after": [80, 100]})
    assert result["measurement_draft"]["value"] == pytest.approx(323.15)
    with pytest.raises(ValueError):
        analyze_curve("dsc", text, {"temperature_unit": "C", "heat_flow_unit": "W/g"})
    gpc = analyze_curve("gpc", "retention_time,signal\n1,0\n2,1\n3,0\n", {})
    assert gpc["measurement_draft"] is None
    assert "calibration" in gpc["warnings"][0]


def test_existing_material_reference_resolves_identity_without_orphans(tmp_path):
    store = ResearchStore(tmp_path / 'db')
    first = store.add_measurement(measurement())
    linked = store.add_measurement(measurement(material=None, material_id=first['material_id'], value=120))
    assert linked['material'] == first['material']
    assert linked['material_id'] == first['material_id']
    with pytest.raises(ValueError, match='existing material'):
        store.add_measurement(measurement(material=None, material_id='mat_unknown'))
    job_dir = tmp_path / 'jobs' / ('b'*32)
    job = make_cg(job_dir)
    indexed = store.index_job(job, job_dir)
    with pytest.raises(ValueError, match='demonstration'):
        store.add_measurement(measurement(material=None, material_id=indexed['material_id']))
    assert len(store.list_measurements()) == 2


def test_curve_csv_ambiguous_headers_and_rows_rejected():
    options = {'strain_unit': '%', 'stress_unit': 'MPa', 'linear_region': [0, .3]}
    for text in ('strain,stress,stress\n0,0,99\n.1,2,99\n.2,4,99\n',
                 'strain,stress\n0,0,99\n.1,2\n.2,4\n',
                 'strain,stress\n0,0\n.1\n.2,4\n'):
        with pytest.raises(ValueError):
            analyze_curve('tensile', text, options)


def test_collinear_torsions_are_unavailable_not_zero_entropy(tmp_path):
    trajectory = {'unit': 'sigma', 'kind': 'coarse_grained', 'monomers': 4, 'chains': 1,
                  'bonds': [[0,1], [1,2], [2,3]],
                  'frames': [{'positions': [[0,0,0], [scale,0,0], [2*scale,0,0], [3*scale,0,0]]} for scale in (1,2,3)]}
    (tmp_path / 'trajectory.json').write_text(json.dumps(trajectory))
    report = extract_job_features(tmp_path, {'config': {'route': 'polymer_cg'}})
    assert report['actual_dynamics'] is True
    assert 'rg_mean' in report['values']
    assert 'backbone_dihedral_entropy' not in report['values']
    assert 'undefined torsion' in report['unavailable']['backbone_dihedral_entropy']


def gpc_fixture(sd=0.6):
    """Gaussian peak on a sloped baseline; linear calibration log10 M = 8 - 0.3 t.

    A Gaussian in t is a log-normal weight distribution, whose averages are analytic.
    """
    t = np.linspace(10, 22, 1201)
    signal = np.exp(-0.5 * ((t - 16) / sd) ** 2) + 0.01 * t + 0.2
    text = "retention_time,signal\n" + "\n".join(f"{a:.5f},{b:.8f}" for a, b in zip(t, signal))
    options = {"x_unit": "min", "detector": "RI", "calibration_standard": "polystyrene",
               "calibration": [[x, 10 ** (8 - 0.3 * x) / 1000] for x in (11, 13, 15, 17, 19, 21)],
               "molar_mass_unit": "kg/mol", "calibration_order": 1, "eluent": "THF",
               "baseline_before": [11, 12.5], "baseline_after": [19.5, 21], "integration_window": [12.5, 19.5]}
    return text, options


def test_gpc_conventional_calibration_matches_lognormal_averages():
    text, options = gpc_fixture()
    result = analyze_curve("gpc", text, options)
    values = {d["property"]: d for d in result["measurement_drafts"]}
    sigma, mu = np.log(10) * 0.3 * 0.6, np.log(10) * (8 - 0.3 * 16)
    assert values["mn"]["value"] == pytest.approx(np.exp(mu - sigma ** 2 / 2), rel=1e-5)
    assert values["mw"]["value"] == pytest.approx(np.exp(mu + sigma ** 2 / 2), rel=1e-5)
    assert values["dispersity"]["value"] == pytest.approx(np.exp(sigma ** 2), rel=1e-5)
    assert result["measurement_draft"] == values["mn"]
    # Per-sample windows stay in analysis provenance so replicates share comparable conditions.
    assert values["mn"]["conditions"] == {"calibration_standard": "polystyrene", "calibration_order": 1, "detector": "RI", "x_unit": "min", "eluent": "THF"}
    analysis = values["mw"]["analysis"]
    assert analysis["integration_window"] == [12.5, 19.5] and analysis["calibration"]["max_abs_deviation_percent"] < 1e-6
    assert len(analysis["calibration_sha256"]) == 64
    assert any("polystyrene-equivalent" in w for w in result["warnings"])
    for draft in result["measurement_drafts"]:
        row = validate_measurement({**draft, "material": {"repeat_smiles": "[*]CC[*]"}, "origin": "experimental", "source": "synthetic fixture"})
        assert row["analysis"] == draft["analysis"]


def test_gpc_refuses_unsupported_calibration_and_baselines():
    text, options = gpc_fixture()
    cases = [({"integration_window": [10.5, 19.5], "baseline_before": [10, 10.5]}, "extrapolation"),
             ({"calibration": [[11, 100], [13, 1000], [15, 10]]}, "decreasing molar mass"),
             ({"calibration_order": 3, "calibration": [[11, 1e5], [13, 1e4], [15, 1e3], [17, 1e2]]}, "at least 5 standards"),
             ({"calibration_standard": " "}, "calibration_standard"),
             ({"detector": "MALS"}, "detector"),
             ({"baseline_before": [11, 13]}, "before and after"),
             ({"baseline_before": [14.5, 15], "integration_window": [15, 19.5]}, "negative")]
    for change, message in cases:
        with pytest.raises(ValueError, match=message):
            analyze_curve("gpc", text, {**options, **change})
    truncated = analyze_curve("gpc", text, {**options, "integration_window": [15, 19.5], "baseline_before": [11, 12]})
    assert any("truncated" in w for w in truncated["warnings"])


def tga_fixture():
    temperature = np.arange(30, 701, 1.0)
    moisture = np.where(temperature < 100, 1.02, 1.0)
    mass = 10 * moisture * (1 - 0.9 / (1 + np.exp(-(temperature - 420) / 15)))
    text = "temperature,mass\n" + "\n".join(f"{a},{b}" for a, b in zip(temperature, mass))
    options = {"temperature_unit": "C", "mass_unit": "mg", "reference_temperature": 150,
               "mass_loss_percent": 5, "atmosphere": "N2", "heating_rate_K_min": 10}
    return text, options


def test_tga_mass_loss_temperature_relative_to_reference():
    text, options = tga_fixture()
    result = analyze_curve("tga", text, options)
    draft = result["measurement_draft"]
    reference = 1 - 0.9 / (1 + np.exp(-(150 - 420) / 15))
    # Invert 1 - 0.9/(1+exp(-(T-420)/15)) = 0.95 * reference analytically.
    expected = 420 - 15 * np.log(0.9 / (1 - 0.95 * reference) - 1)
    assert draft["property"] == "decomposition_temperature" and draft["unit"] == "K"
    assert draft["value"] == pytest.approx(expected + 273.15, abs=0.02)
    assert draft["conditions"] == {"mass_loss_percent": 5.0, "atmosphere": "N2", "heating_rate_K_min": 10.0, "reference_temperature_K": 423.15}
    assert draft["analysis"]["remaining_mass_percent_at_final"] == pytest.approx(10.0, abs=0.1)
    # Normalizing at the first point instead would include the moisture step.
    early = analyze_curve("tga", text, {**options, "reference_temperature": 30})["measurement_draft"]
    assert early["value"] < draft["value"] - 5
    cases = [({"mass_loss_percent": 95}, "does not lose"), ({"atmosphere": ""}, "atmosphere"),
             ({"reference_temperature": 800}, "inside the uploaded"), ({"heating_rate_K_min": 0}, "heating_rate"),
             ({"mass_unit": "g"}, "mass_unit")]
    for change, message in cases:
        with pytest.raises(ValueError, match=message):
            analyze_curve("tga", text, {**options, **change})
    with pytest.raises(ValueError, match="Negative mass"):
        analyze_curve("tga", "temperature,mass\n30,1\n40,0.5\n50,-0.1\n", options | {"reference_temperature": 30})
    assert analyze_curve("tga", text, {})["measurement_draft"] is None


def test_molar_mass_average_and_dispersity_units():
    base = {"material": {"repeat_smiles": "[*]CC[*]"}, "method": "GPC", "origin": "literature", "source": "synthetic fixture"}
    assert validate_measurement({**base, "property": "mn", "value": 12.5, "unit": "kg/mol"})["value"] == pytest.approx(12500)
    assert validate_measurement({**base, "property": "decomposition_temperature", "value": 350, "unit": "C"})["value"] == pytest.approx(623.15)
    assert validate_measurement({**base, "property": "dispersity", "value": 1.0, "unit": "dimensionless"})["unit"] == "dimensionless"
    with pytest.raises(ValueError, match="below 1"):
        validate_measurement({**base, "property": "dispersity", "value": 0.98, "unit": "dimensionless"})
    with pytest.raises(ValueError, match="incompatible"):
        validate_measurement({**base, "property": "mw", "value": 1, "unit": "MPa"})
