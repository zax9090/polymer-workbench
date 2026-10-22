import json
from pathlib import Path

import pytest

from workbench import bridges


def test_bridge_templates_stay_blocked_and_inspectable(tmp_path):
    result = bridges.run_bridge({"bridge_track":"martini3"}, tmp_path)
    assert result["status"] == "blocked"
    assert result["simulation_executed"] is False
    manifest = json.loads((tmp_path / "bridge_manifest.json").read_text())
    assert len(manifest["stages"]) == 4
    assert manifest["requirements"]
    assert "ref-p = 1.0" in (tmp_path / "03_npt.mdp").read_text()
    assert "gen-vel = no" in (tmp_path / "04_production.mdp").read_text()
    assert "-maxwarn" not in (tmp_path / "command_plan.json").read_text()
    assert json.loads((tmp_path / "command_plan.json").read_text())["shell"] is False
    assert all(len(item["sha256"]) == 64 for item in manifest["artifacts"])


def test_martini_versions_and_arbitrary_mapping_rejected(tmp_path):
    with pytest.raises(ValueError, match="must not be mixed"):
        bridges.run_bridge({"polyply_library":"martini2"}, tmp_path)
    result = bridges.run_bridge({"polyply_residue":"INVENTED"}, tmp_path)
    assert result["status"] == "blocked"
    assert any("arbitrary SMILES" in item for item in result["requirements"])
    assert not (tmp_path / "polymer.itp").exists()
    with pytest.raises(ValueError):
        bridges.run_bridge({"polyply_residue":"PEO;rm -rf /"}, tmp_path)


def test_amber_recipe_has_temperature_ramp_and_no_compression(tmp_path):
    result=bridges.run_bridge({"bridge_track":"amber","chains":1,"temperature_kelvin":310},tmp_path)
    assert result["status"]=="blocked"
    assert "source leaprc.protein.ff14SB" in (tmp_path / "prepare.leap").read_text()
    assert "value1=10.0, value2=310.0" in (tmp_path / "02_nvt.in").read_text()
    assert "pres0=1.0" in (tmp_path / "03_npt.in").read_text()
    assert not any(p.suffix==".data" for p in tmp_path.iterdir())


def test_openff_not_claimed_executable(tmp_path):
    result=bridges.run_bridge({"bridge_track":"openff"},tmp_path)
    assert result["status"] == "blocked"
    assert result["bridge"]["stages"] == []
    assert any("integration specification only" in note for note in result["bridge"]["notes"])
    with pytest.raises(ValueError,match="only for martini3"):
        bridges.run_bridge({"bridge_track":"openff","bridge_mode":"protein_cg"},tmp_path)


def test_invalid_protein_and_secondary_structure(tmp_path):
    with pytest.raises(ValueError,match="ATOM"):
        bridges.run_bridge({"pdb_text":"not a protein"},tmp_path)
    with pytest.raises(ValueError,match="Solvated-mixture"):
        bridges.run_bridge({"temperature_kelvin":1500},tmp_path)
    line="ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00 20.00           C  \n"
    with pytest.raises(ValueError,match="one DSSP"):
        bridges.run_bridge({"pdb_text":line,"secondary_structure":"HH"},tmp_path)
    with pytest.raises(ValueError,match="ligands"):
        bridges.run_bridge({"pdb_text":line+line.replace("ATOM  ","HETATM")},tmp_path)


def test_actual_martini_protein_and_polymer_components(tmp_path):
    caps=bridges.bridge_capabilities()
    if not caps["executables"]["martinize2"] or not caps["executables"]["polyply"]:
        pytest.skip("Requires installed martinize2 and polyply")
    Chem=pytest.importorskip("rdkit.Chem")
    from rdkit.Chem import AllChem
    mol=Chem.AddHs(Chem.MolFromFASTA("AAAAAA"))
    assert AllChem.EmbedMolecule(mol, randomSeed=42)==0
    AllChem.MMFFOptimizeMolecule(mol)
    pdb=Chem.MolToPDBBlock(Chem.RemoveHs(mol))
    # CCCCC C is an explicit synthetic test assumption, not inferred biology.
    result=bridges.run_bridge({"bridge_track":"martini3","bridge_mode":"protein_cg", "pdb_text":pdb,
                               "secondary_structure":"CCCCCC","monomers":5,"chains":1},tmp_path)
    assert result["status"] == "prepared" and result["simulation_executed"] is False
    assert (tmp_path / "protein_cg.pdb").stat().st_size > 100
    assert "[ atoms ]" in (tmp_path / "polymer.itp").read_text()
    assert "[ position_restraints ]" in (tmp_path / "Protein_0.itp").read_text()
    assert not (tmp_path / "solvated.gro").exists()
    assert result["requirements"]


def write_components(directory, protein_charge=3, polymer_charge=-1, beads=4):
    """Synthetic component files; charges are arbitrary software-test values."""
    atoms = "\n".join(f" {i+1} P2 {i+1} ALA BB {i+1} {protein_charge if i == 0 else 0}" for i in range(beads))
    (directory / "Protein_0.itp").write_text(f"[ moleculetype ]\nProtein_0 1\n\n[ atoms ]\n{atoms}\n#ifdef POSRES\n[ position_restraints ]\n1 1 1000 1000 1000\n#endif\n")
    (directory / "protein.top").write_text('#include "martini.itp"\n#include "Protein_0.itp"\n\n[ system ]\nx\n\n[ molecules ]\nProtein_0    1\n')
    (directory / "polymer.itp").write_text(f"[ moleculetype ]\nPolymer 1\n[ atoms ]\n1 SN3r 1 PEO EO 1 {polymer_charge}\n2 SN3r 2 PEO EO 2 0.0 ; comment\n")
    pdb = "".join(f"ATOM  {i+1:5d}  BB  ALA A{i+1:4d}    {3.0*i:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C\n" for i in range(beads))
    (directory / "protein_cg.pdb").write_text(pdb + "END\n")


def test_martini_topology_parsing_and_neutralizing_ion_counts(tmp_path):
    write_components(tmp_path)
    assert bridges._itp_molecules(tmp_path / "polymer.itp") == {"Polymer": {"atoms": 2, "charge": -1.0}}
    includes, molecules = bridges._martinize_topology(tmp_path / "protein.top")
    assert includes == ["Protein_0.itp"] and molecules == [("Protein_0", 1)]
    cfg = bridges._validated({"bridge_mode": "simulate", "chains": 2, "box_nm": 6, "polymer_net_charge": -1})
    layout, solvation = bridges._assemble_solvated(cfg, tmp_path)
    # +3 protein and two -1 chains leave +1, neutralized by one extra Cl-.
    assert solvation["solute_charge_e"] == 1 and solvation["system_charge_e"] == 0
    assert solvation["chloride"] == solvation["sodium"] + 1 == solvation["salt_pairs"] + 1
    assert solvation["salt_molar_estimated"] == pytest.approx(0.15, rel=0.05)
    assert layout["protein"] == [0, 4] and layout["polymer_chains"] == [[4, 6], [6, 8]]
    assert layout["particles"] == 8 + solvation["water_beads"] + solvation["sodium"] + solvation["chloride"]
    top = (tmp_path / "system.top").read_text()
    assert '#include "martini_v3.0.0.itp"' in top and '#include "martini.itp"' not in top
    assert top.strip().endswith(f"CL {solvation['chloride']}")
    assert "6.000" in (tmp_path / "protein_centered.gro").read_text().splitlines()[-1]
    with pytest.raises(ValueError, match="set polymer_net_charge to -1"):
        bridges._assemble_solvated({**cfg, "polymer_net_charge": 0}, tmp_path)
    with pytest.raises(ValueError, match="box_nm of at least"):
        bridges._assemble_solvated({**cfg, "box_nm": 2.5}, tmp_path)
    (tmp_path / "protein.top").write_text('#include "../escape.itp"\n[ molecules ]\nProtein_0 1\n')
    with pytest.raises(RuntimeError, match="Unexpected martinize2 include"):
        bridges._martinize_topology(tmp_path / "protein.top")


def test_simulate_mode_validation():
    with pytest.raises(ValueError, match="only for martini3"):
        bridges._validated({"bridge_track": "amber", "bridge_mode": "simulate"})
    with pytest.raises(ValueError, match="particles"):
        bridges._validated({"bridge_mode": "simulate", "box_nm": 25})
    with pytest.raises(ValueError, match="elastic_network"):
        bridges._validated({"bridge_mode": "simulate", "elastic_network": "yes"})


def test_martini_parameter_pins_reject_modified_local_copy(tmp_path, monkeypatch):
    from workbench import forcefields
    (tmp_path / "a.itp").write_text("[ defaults ]\n")
    spec = {**forcefields.MARTINI3, "files": {"a.itp": ("x/a.itp", forcefields._sha256(tmp_path / "a.itp"))}}
    monkeypatch.setenv("MARTINI3_FF_DIR", str(tmp_path))
    directory, record = forcefields.martini3_parameters(download=False, spec=spec)
    assert directory == tmp_path and record["source"] == "MARTINI3_FF_DIR"
    assert record["files"]["a.itp"]["url"].endswith(f"{spec['commit']}/x/a.itp")
    (tmp_path / "a.itp").write_text("[ defaults ]\n; edited\n")
    with pytest.raises(ValueError, match="differs from the pinned"):
        forcefields.martini3_parameters(download=False, spec=spec)
    assert forcefields.martini3_status(spec)["available"] is False


def test_actual_solvated_martini_pilot_with_gromacs(tmp_path):
    caps = bridges.bridge_capabilities()
    if not all(caps["executables"][name] for name in ("martinize2", "polyply", "gmx")):
        pytest.skip("Requires installed martinize2, polyply and GROMACS")
    from workbench import forcefields
    try:
        forcefields.martini3_parameters()
    except OSError as exc:
        pytest.skip(f"Pinned Martini 3 parameters are not available offline: {exc}")
    Chem = pytest.importorskip("rdkit.Chem")
    from rdkit.Chem import AllChem
    from workbench.descriptors import extract_job_features
    mol = Chem.AddHs(Chem.MolFromFASTA("AAAAAA"))
    assert AllChem.EmbedMolecule(mol, randomSeed=42) == 0
    AllChem.MMFFOptimizeMolecule(mol)
    # Synthetic hexa-alanine and CCCCCC secondary structure are software-test assumptions.
    config = {"bridge_track": "martini3", "bridge_mode": "simulate", "pdb_text": Chem.MolToPDBBlock(Chem.RemoveHs(mol)),
              "secondary_structure": "CCCCCC", "monomers": 8, "chains": 2, "box_nm": 5.5,
              "equilibration_steps": 500, "steps": 2000, "temperature_kelvin": 300}
    result = bridges.run_bridge(config, tmp_path)
    assert result["status"] == "completed" and result["simulation_executed"] is True
    assert result["bridge"]["simulation"]["solvation"]["system_charge_e"] == 0
    summary = result["metrics"]["summary"]
    assert summary["frames"] == 101 and summary["production_ps"] == pytest.approx(20, abs=0.01)
    assert 900 < summary["density_mean"] < 1100
    assert all(stage["state"] == "executed" for stage in result["bridge"]["stages"])
    assert not list(tmp_path.glob(".*_offsets.*"))
    plan = json.loads((tmp_path / "command_plan.json").read_text())
    assert "-maxwarn" not in json.dumps(plan) and all(item["executed"] for item in plan["commands"][2:])
    structure = (tmp_path / "structure.pdb").read_text()
    assert structure.startswith("REMARK 900 MARTINI 3") and "FINAL PRODUCTION FRAME AT 20.0 PS" in structure
    features = extract_job_features(tmp_path, {"config": {**config, "route": "mixture"}, "result": result})
    assert features["actual_dynamics"] is True
    assert {"protein_rmsd", "contact_count", "polymer_rg_mean"} <= set(features["values"])
    assert features["values"]["contact_count"]["method"].endswith("(periodic minimum image)")
    assert "hydrogens" in features["unavailable"]["hbond_count"]
    assert forcefields._sha256(tmp_path / "martini_v3.0.0.itp") == forcefields.MARTINI3["files"]["martini_v3.0.0.itp"][1]
