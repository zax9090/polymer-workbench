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
