"""Tools adopted after REA review: pybaselines curve baselines, PDBFixer repair, HiPlot bundle, pinned engines and xtb charges."""
import hashlib
import json
import tarfile
from pathlib import Path

import numpy as np
import pytest

from workbench import engines
from workbench.ingestion import analyze_curve

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def _csv(x, y, names=("temperature", "heat_flow")):
    return f"{names[0]},{names[1]}\n" + "\n".join(f"{a:.6f},{b:.8f}" for a, b in zip(x, y))


def test_dsc_melting_enthalpy_and_crystallinity_from_an_arpls_baseline():
    pytest.importorskip("pybaselines")
    temperature = np.linspace(50, 200, 1501)
    area, width, centre, rate = 1.2, 4.0, 135.0, 10.0
    heat_flow = 0.05 + 0.0008 * (temperature - 50) + area * np.exp(-0.5 * ((temperature - centre) / width) ** 2)
    expected = area * width * np.sqrt(2 * np.pi) / (rate / 60)  # W/g * K / (K/s) = J/g
    options = {"analysis": "melting", "temperature_unit": "C", "heat_flow_unit": "W/g", "endotherm": "up",
               "heating_rate_k_per_min": rate, "integration_window": [110, 160], "reference_enthalpy_j_per_g": 293}
    draft = analyze_curve("dsc", _csv(temperature, heat_flow), options)
    values = {d["property"]: d for d in draft["measurement_drafts"]}
    assert values["melting_enthalpy"]["value"] == pytest.approx(expected, rel=1e-3)
    assert values["melting_temperature"]["value"] == pytest.approx(centre + 273.15, abs=0.11)
    assert values["crystallinity"]["value"] == pytest.approx(expected / 293, rel=1e-3) and values["crystallinity"]["unit"] == "fraction"
    assert values["melting_enthalpy"]["conditions"]["baseline"]["method"] == "arpls" and draft["review_required"]
    # Exo-up instruments: the same data mirrored gives the same enthalpy only when the direction is stated.
    flipped = analyze_curve("dsc", _csv(temperature, -heat_flow), {**options, "endotherm": "down"})
    assert flipped["measurement_drafts"][1]["value"] == pytest.approx(expected, rel=1e-3)
    milliwatts = analyze_curve("dsc", _csv(temperature, heat_flow * 5.0), {**options, "heat_flow_unit": "mW", "sample_mass_mg": 5.0})
    assert milliwatts["measurement_drafts"][1]["value"] == pytest.approx(expected, rel=1e-3)
    for broken in ({"endotherm": None}, {"heat_flow_unit": "W"}, {"heating_rate_k_per_min": 0}, {"integration_window": [160, 110]}):
        with pytest.raises(ValueError):
            analyze_curve("dsc", _csv(temperature, heat_flow), {**options, **broken})


def test_gpc_arpls_baseline_matches_the_window_baseline_on_a_linear_drift():
    pytest.importorskip("pybaselines")
    time = np.linspace(8, 22, 1401)
    signal = 0.02 + 0.003 * (time - 8) + np.exp(-0.5 * ((time - 15) / 0.7) ** 2)
    calibration = [[11, 1e6], [13, 2e5], [15, 4e4], [17, 8e3], [19, 1.6e3]]
    common = {"x_unit": "min", "detector": "RI", "calibration_standard": "polystyrene", "integration_window": [11.5, 18.5],
              "calibration": calibration, "molar_mass_unit": "g/mol", "calibration_order": 1}
    text = _csv(time, signal, ("retention_time", "signal"))
    windows = analyze_curve("gpc", text, {**common, "baseline_before": [9, 10.5], "baseline_after": [19.5, 21]})
    automatic = analyze_curve("gpc", text, {**common, "baseline_method": "arpls"})
    get = lambda draft, name: next(d["value"] for d in draft["measurement_drafts"] if d["property"] == name)
    for name in ("mn", "mw"):
        assert get(automatic, name) == pytest.approx(get(windows, name), rel=0.02)
    assert automatic["fit"]["baseline"]["method"] == "arpls" and automatic["fit"]["baseline_before"] is None


def test_melting_properties_are_registered_with_units():
    from workbench.research_store import PROPERTY_UNITS, validate_measurement
    assert PROPERTY_UNITS["melting_enthalpy"] == "J/g" and PROPERTY_UNITS["crystallinity"] == "fraction"
    row = validate_measurement({"property": "crystallinity", "value": 42, "unit": "%", "method": "DSC", "source": "test",
                                 "origin": "demo", "material": {"repeat_smiles": "[*]CC[*]"}})
    assert row["value"] == pytest.approx(0.42)
    with pytest.raises(ValueError):
        validate_measurement({**row, "value": 120, "unit": "%", "original_value": 120, "original_unit": "%"})


def test_pdbfixer_rebuilds_a_truncated_side_chain():
    pytest.importorskip("pdbfixer")
    from workbench import repair
    lines = [line for line in (FIXTURES / "villin_1vii.pdb").read_text().splitlines()
             if not (line.startswith("ATOM") and line[17:20] == "LYS" and line[22:26].strip() == "65"
                     and line[12:16].strip() not in {"N", "CA", "C", "O", "CB", "H", "HA"})]
    fixed, report = repair.repair("\n".join(lines) + "\n", seed=3)
    assert report["missing_heavy_atoms_added"] == [{"chain": "A", "residue": "LYS65", "atoms": ["CG", "CD", "CE", "NZ"]}]
    rebuilt = [line for line in fixed.splitlines() if line.startswith("ATOM") and line[17:20] == "LYS" and line[22:26].strip() == "65"]
    assert {"CG", "CD", "CE", "NZ"} <= {line[12:16].strip() for line in rebuilt}
    hpx, hpx_report = repair.repair((FIXTURES / "propka_1HPX.pdb").read_text())
    assert hpx_report["heterogens_removed"] == ["KNI"] and "HETATM" not in hpx and not hpx_report["hydrogens_added"]


def test_protein_import_with_repair_keeps_the_submitted_file(tmp_path):
    pytest.importorskip("pdbfixer")
    from workbench.models import JobConfig
    from workbench.science import run_workflow
    text = (FIXTURES / "propka_1HPX.pdb").read_text()
    config = JobConfig.model_validate({"route": "protein", "protein_mode": "import", "pdb_text": text, "protein_repair": "pdbfixer"}).model_dump()
    result = run_workflow(config, tmp_path)
    assert result["repair"]["heterogens_removed"] == ["KNI"] and (tmp_path / "structure_submitted.pdb").read_text() == text
    assert "KNI" not in (tmp_path / "structure.pdb").read_text() and any("removed before analysis" in w for w in result["warnings"])


def test_vendored_hiplot_bundle_matches_its_provenance():
    provenance = json.loads((ROOT / "workbench/static/vendor/hiplot-PROVENANCE.json").read_text())
    bundle = (ROOT / "workbench/static/vendor/hiplot.bundle.js").read_bytes()
    assert hashlib.sha256(bundle).hexdigest() == provenance["sha256"] and provenance["license"] == "MIT"
    assert b"fonts.googleapis.com/css?family=Lato:400,700,400italic&display=swap\\\"}" not in bundle
    assert (ROOT / "workbench/static/vendor/hiplot-LICENSE.txt").read_text().startswith("MIT License")
    catalog = {entry["id"]: entry for entry in json.loads((ROOT / "tools/rea/artifact_catalog.json").read_text())}
    assert catalog["hiplot"]["vendored"][0]["vendored_sha256"] == provenance["sha256"]
    assert "hiplot-open" in (ROOT / "workbench/static/index.html").read_text()


def test_engine_install_verifies_hash_and_skips_escaping_links(tmp_path, monkeypatch):
    payload = tmp_path / "payload"
    (payload / "pkg/bin").mkdir(parents=True)
    (payload / "pkg/bin/tool").write_text("#!/bin/sh\necho ok\n")
    archive = tmp_path / "tool.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload / "pkg", arcname="pkg")
        link = tarfile.TarInfo("pkg/escape")
        link.type, link.linkname = tarfile.SYMTYPE, "../../outside"
        bundle.addfile(link)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    spec = {"name": "Tool", "version": "1", "url": archive.as_uri(), "archive": "tool.tar.gz", "sha256": digest, "root": "pkg",
            "executable": "bin/tool", "license": "test", "citation": "test", "environment": "POLYMER_TEST_TOOL"}
    monkeypatch.setitem(engines.ENGINES, "tool", spec)
    monkeypatch.setenv("POLYMER_DATA_DIR", str(tmp_path / "data" / "jobs"))
    assert engines.status("tool")["available"] is False
    root, record = engines.ensure("tool")
    assert (root / "bin/tool").is_file() and not (root / "escape").exists() and record["source"] == "downloaded"
    assert "pkg/escape" in (root.parent / ".verified").read_text() and engines.status("tool")["source"] == "cache"
    monkeypatch.setitem(engines.ENGINES, "bad", {**spec, "sha256": "0" * 64})
    with pytest.raises(ValueError, match="SHA-256"):
        engines.ensure("bad")
    with pytest.raises(FileNotFoundError):
        engines.ensure("bad", download=False)


def test_engine_pins_match_the_rea_artifact_catalog():
    catalog = {entry["id"]: entry for entry in json.loads((ROOT / "tools/rea/artifact_catalog.json").read_text())}
    for name, spec in engines.ENGINES.items():
        assert catalog[name]["sha256"] == spec["sha256"] and catalog[name]["artifact"] == spec["archive"]
        assert catalog[name]["executable"] == f"{spec['root']}/{spec['executable']}"


def test_xtb_charges_are_limited_to_the_gaff2_bulk_route():
    from workbench.models import JobConfig
    base = {"route": "polymer_atomistic", "atomistic_mode": "bulk", "chains": 3, "charge_method": "xtb_cm5"}
    assert JobConfig.model_validate(base).charge_method == "xtb_cm5"
    for broken in ({"atomistic_forcefield": "pcff"}, {"atomistic_mode": "build"}):
        with pytest.raises(ValueError, match="xtb"):
            JobConfig.model_validate({**base, **broken})


@pytest.mark.skipif(not engines.status("xtb")["available"], reason="xtb 6.7.1 not installed (XTB_EXEC or the engine cache)")
def test_gaff2_bulk_cell_with_xtb_cm5_charges(tmp_path):
    pytest.importorskip("radonpy")
    from workbench.external import executable
    if not executable("lmp", "LAMMPS_EXEC"):
        pytest.skip("Requires a LAMMPS executable")
    from workbench.models import JobConfig
    from workbench.science import run_workflow
    config = JobConfig.model_validate({"route": "polymer_atomistic", "atomistic_mode": "bulk", "charge_method": "xtb_cm5", "repeat_smiles": "[*]CC([*])C(=O)OC",
                                       "monomers": 8, "chains": 3, "protocol_scale": 0.001}).model_dump()
    result = run_workflow(config, tmp_path)
    assert result["status"] == "completed" and result["metadata"]["charge_method"].startswith("CM5")
    lines = (tmp_path / "xtb.log").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if "Mulliken/CM5 charges" in line) + 1
    cm5 = [float(line.split()[2]) for line in lines[start:start + 98]]
    data = [line.split() for line in (tmp_path / "eq1.data").read_text().split("Atoms")[1].split("Bonds")[0].splitlines() if len(line.split()) >= 7]
    assert [float(row[3]) for row in data[:98]] == pytest.approx(cm5, abs=1e-4)
    assert "config.json" in result["artifacts"] and "xtb" not in json.loads((tmp_path / "config.json").read_text())
