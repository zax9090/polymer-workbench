"""Cooling-scan Tg: hyperbola and two-line fits on synthetic data, scan inputs, log parsing, submission and indexing."""
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from workbench import glass_transition as tg
from workbench.server import create_app

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = "a" * 32
# Specific volume of a glass former: 2.4e-4 and 5.4e-4 cm^3/(g K) below and above Tg = 370 K.
TRUTH = [0.95, 370.0, 2.4e-4, 5.4e-4, 2 * np.log(12.0)]


def stages_for(x, y):
    return [{"temperature_mean_kelvin": t, "specific_volume_cm3_g": v} for t, v in zip(x, y)]


def test_temperature_ladder_includes_both_ends():
    assert tg.temperatures(500, 200, 20)[::5] == [500, 400, 300, 200] and len(tg.temperatures(500, 200, 20)) == 16
    assert tg.temperatures(450, 300, 40) == [450, 410, 370, 330]


def test_fits_recover_known_transitions():
    x = np.array(tg.temperatures(500, 200, 20))
    fit = tg.fit_hyperbola(x, tg.hyperbola(TRUTH, x))
    assert fit["tg_kelvin"] == pytest.approx(370, abs=1e-6) and fit["curvature_kelvin"] == pytest.approx(12, rel=1e-6)
    assert fit["slope_glass"] == pytest.approx(2.4e-4, rel=1e-6) and fit["slope_melt"] == pytest.approx(5.4e-4, rel=1e-6)
    kinked = 0.95 + np.where(x < 333, 2.4e-4 * (x - 333), 5.4e-4 * (x - 333))
    assert tg.fit_bilinear(x, kinked)["tg_kelvin"] == pytest.approx(333, abs=0.051)
    assert tg.fit_hyperbola(x, kinked)["tg_kelvin"] == pytest.approx(333, abs=0.05)


def test_noisy_scan_reports_interval_expansion_and_rate():
    x = np.array(tg.temperatures(500, 200, 20))
    y = tg.hyperbola(TRUTH, x) + np.random.default_rng(3).normal(0, 4e-4, len(x))
    result = tg.analyze(stages_for(x, y), 20, 50000, samples=100)
    low, high = result["tg_interval_95_kelvin"]
    assert result["transition_resolved"] and low < 370 < high and high - low < 20
    assert result["cooling_rate_k_per_ns"] == pytest.approx(400) and result["cooling_rate_k_per_s"] == pytest.approx(4e11)
    assert result["expansion_glass_per_kelvin"] == pytest.approx(2.4e-4 / 0.95, rel=0.2)
    assert result["expansion_melt_per_kelvin"] == pytest.approx(5.4e-4 / 0.95, rel=0.2)
    assert any("WLF" in w for w in result["warnings"]) and any("replicas" in w for w in result["warnings"])
    json.dumps(result, allow_nan=False)


def test_unresolved_scans_are_flagged():
    x = np.array(tg.temperatures(500, 200, 20))
    rng = np.random.default_rng(5)
    straight = tg.analyze(stages_for(x, 0.95 + 4e-4 * (x - 300) + rng.normal(0, 4e-4, len(x))), 20, 50000, samples=100)
    assert not straight["transition_resolved"] and "No clear change" in straight["warnings"][0]
    edge = tg.analyze(stages_for(x, tg.hyperbola([0.95, 225.0, 2.4e-4, 5.4e-4, 2 * np.log(12.0)], x) + rng.normal(0, 4e-4, len(x))), 20, 50000, samples=100)
    assert not edge["transition_resolved"] and any("one side of Tg" in w or "edge" in w for w in edge["warnings"])
    with pytest.raises(ValueError, match="at least 6"):
        tg.analyze(stages_for(x[:5], x[:5] * 0.001), 20, 50000)


def test_style_lines_reuse_only_force_field_commands():
    radonpy = ("log eq3.log append\nunits real\npair_style lj/charmm/coul/long 8.0 12.0\nkspace_style pppm 1e-6\nbond_style harmonic\n"
               "special_bonds amber\npair_modify mix arithmetic\nread_data eq3.data\nfix md1 all npt temp 300 300 100 iso 1 1 1000\npair_style lj/cut 2.5\n")
    assert tg.style_lines(radonpy, stop_at_read_data=True) == ["pair_style lj/charmm/coul/long 8.0 12.0", "bond_style harmonic",
                                                                "special_bonds amber", "pair_modify mix arithmetic"]
    params = (FIXTURES / "emc_ps.params").read_text()
    lines = tg.style_lines(params, stop_at_read_data=False)
    assert lines[0] == "pair_style lj/class2/coul/long ${cutoff} ${charge_cutoff}" and "special_bonds lj/coul 0 0 1" in lines
    with pytest.raises(ValueError, match="pair_style"):
        tg.style_lines("bond_style harmonic\n", stop_at_read_data=True)


def test_scan_input_holds_each_temperature_once():
    script = tg.scan_input(["pair_style lj/cut 9.5"], [400.0, 380.0, 360.0], 1000, 1234, shake=False).splitlines()
    assert script.index("pair_style lj/cut 9.5") < script.index("read_data source.data")
    assert script.index("velocity all create 400.0 1234 mom yes rot yes dist gaussian") < script.index("log tg_hold.log") < script.index("log tg_scan.log")
    holds = [line for line in script[script.index("log tg_scan.log"):] if line.startswith("fix hold")]
    assert [line.split()[5] for line in holds] == ["400.0", "380.0", "360.0"] and not any("shake" in line for line in script)
    assert any("shake" in line for line in tg.scan_input(["pair_style lj/cut 9.5"], [400.0], 1000, 1, shake=True).splitlines())


def test_stage_means_use_the_second_half_of_each_hold(tmp_path):
    lines = ["LAMMPS (22 Jul 2025)"]
    for i, (temp, density) in enumerate([(400, 1.00), (380, 1.01)]):
        lines.append("   Step          Temp          Press         Density         Volume         PotEng    ")
        for row in range(21):
            step = i * 1000 + row * 50
            value = density if row > 10 else 0.5  # the first half must not count
            lines.append(f"{step:8d} {temp + 0.5:.4f} 1.0 {value:.4f} 1000.0 -10.0")
        lines += ["WARNING: something harmless", f"Loop time of 1.0 on 4 procs for 1000 steps with 100 atoms"]
    path = tmp_path / "tg_scan.log"
    path.write_text("\n".join(lines) + "\n")
    stages = tg.stage_means(path, [400.0, 380.0], 1000)
    assert [s["density_g_cm3"] for s in stages] == pytest.approx([1.00, 1.01])
    assert stages[1]["specific_volume_cm3_g"] == pytest.approx(1 / 1.01) and stages[0]["temperature_mean_kelvin"] == pytest.approx(400.5)
    with pytest.raises(RuntimeError, match="2 of 3"):
        tg.stage_means(path, [400.0, 380.0, 360.0], 1000)


def source_job(root, *, status="completed", mode="bulk", files=("eq3_last.data", "eq3.in")):
    directory = root / SOURCE
    directory.mkdir(parents=True, exist_ok=True)
    config = {"route": "polymer_atomistic", "atomistic_mode": mode, "atomistic_forcefield": "gaff2", "repeat_smiles": "[*]CC([*])c1ccccc1",
              "monomers": 10, "chains": 8}
    (directory / "job.json").write_text(json.dumps({"id": SOURCE, "name": "synthetic source", "status": status, "config": config,
                                                  "created_at": "2026-10-23T00:00:00+00:00"}))
    (directory / "config.json").write_text(json.dumps(config))
    for name in files:
        (directory / name).write_text("synthetic\n")
    return directory


@pytest.mark.parametrize("status,mode,files,expected", [
    ("running", "bulk", ("eq3_last.data", "eq3.in"), "최종 구조 파일"),
    ("completed", "build", (), "벌크 셀 또는 가교"),
    ("completed", "bulk", ("eq3_last.data",), "eq3.in"),
])
def test_submission_needs_a_completed_cell_with_its_files(tmp_path, status, mode, files, expected):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        source_job(client.app.state.manager.root, status=status, mode=mode, files=files)
        response = client.post("/api/jobs", json={"route": "polymer_atomistic", "atomistic_mode": "tg_scan", "source_job_id": SOURCE})
        assert response.status_code == 422 and expected in response.json()["detail"]
        assert client.post("/api/jobs", json={"route": "polymer_atomistic", "atomistic_mode": "tg_scan"}).status_code == 422
        assert [job["id"] for job in client.get("/api/jobs").json()] == [SOURCE]


def test_scan_jobs_index_as_the_source_material(tmp_path):
    from workbench.research_store import ResearchStore
    root = tmp_path / "jobs"
    source_job(root)
    scan_id = "b" * 32
    directory = root / scan_id
    directory.mkdir()
    config = {"route": "polymer_atomistic", "atomistic_mode": "tg_scan", "source_job_id": SOURCE, "repeat_smiles": "[*]CC[*]"}
    (directory / "config.json").write_text(json.dumps(config))
    (directory / "tg_source.json").write_text(json.dumps({"config": json.loads((root / SOURCE / "config.json").read_text())}))
    stages = [{"temperature_set_kelvin": 300.0}] * 6
    complete = {"tg_kelvin": 371.0, "tg_interval_95_kelvin": [365.0, 377.0], "transition_resolved": True, "cooling_rate_k_per_ns": 400.0,
                "cooling_rate_k_per_s": 4e11, "expansion_glass_per_kelvin": 2.5e-4, "expansion_melt_per_kelvin": 5.7e-4, "stages": stages,
                "source": {"force_field": "GAFF2_mod (RadonPy cell)"}}
    (directory / "tg_complete.json").write_text(json.dumps(complete))
    record = ResearchStore(tmp_path / "research").index_job({"id": scan_id, "status": "completed", "config": config}, directory)
    from workbench.research_store import material_from_job
    source_material = material_from_job({"config": json.loads((root / SOURCE / "config.json").read_text())})
    assert record["material_id"] == source_material["material_id"] != material_from_job({"config": config})["material_id"]
    values = record["features"]["values"]
    assert values["tg_cooling_md"]["value"] == 371.0 and values["tg_cooling_md"]["unit"] == "K" and "400 K/ns" in values["tg_cooling_md"]["method"]
    assert values["expansion_melt_md"]["value"] == 5.7e-4


def test_scan_runs_a_pcff_cell_end_to_end(tmp_path):
    from workbench.external import executable
    lmp = executable("lmp", "LAMMPS_EXEC")
    if not lmp:
        pytest.skip("Requires a LAMMPS executable with CLASS2")
    root = tmp_path / "jobs"
    source = source_job(root, files=())
    config = json.loads((source / "config.json").read_text())
    (source / "config.json").write_text(json.dumps({**config, "atomistic_forcefield": "pcff"}))
    shutil.copyfile(FIXTURES / "emc_ps.params", source / "polymer.params")
    shutil.copyfile(FIXTURES / "emc_ps.data", source / "polymer.data")
    # A LAMMPS-written data file (with coefficient sections), as a completed PCFF job leaves behind.
    (source / "in.prepare").write_text("units real\natom_style full\nboundary p p p\nvariable cutoff index 9.5\nvariable charge_cutoff index 9.5\n"
                                       "read_data polymer.data\ninclude polymer.params\nkspace_style pppm 1e-6\n"
                                       "minimize 1.0e-4 1.0e-6 1000 10000\nwrite_data final.data\n")
    from workbench.external import class2_lammps
    class2_lammps(lmp, source, "in.prepare")
    from workbench.models import JobConfig
    from workbench.science import run_workflow
    scan = JobConfig.model_validate({"route": "polymer_atomistic", "atomistic_mode": "tg_scan", "source_job_id": SOURCE, "tg_high_kelvin": 400,
                                     "tg_low_kelvin": 300, "tg_step_kelvin": 20, "tg_stage_steps": 1000}).model_dump()
    result = run_workflow(scan, root / ("c" * 32))
    assert result["status"] == "completed", result
    complete = json.loads((root / ("c" * 32) / "tg_complete.json").read_text())
    assert [s["temperature_set_kelvin"] for s in complete["stages"]] == [400, 380, 360, 340, 320, 300]
    assert all(s["rows"] >= 8 and 0.2 < s["density_g_cm3"] < 2.0 for s in complete["stages"])
    assert complete["source"]["kind"] == "pcff" and complete["protocol"]["shake"] is True
    assert complete["source"]["files_sha256"]["final.data"] == __import__("hashlib").sha256((source / "final.data").read_bytes()).hexdigest()
    features = json.loads((root / ("c" * 32) / "features.json").read_text())
    assert features["values"]["tg_cooling_md"]["unit"] == "K"
