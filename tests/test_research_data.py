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
