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


# Synthetic Martini-style parameters for software tests; U has no nonbond_params rows, as in Martini 3.
MINI_FF = """[ defaults ]
1 2

[ atomtypes ]
P2 72.0 0.000 A 0.0 0.0
SN3r 54.0 0.000 A 0.0 0.0
W 72.0 0.000 A 0.0 0.0
U 72.0 0.000 A 0.0 0.0

[ nonbond_params ]
P2 P2 1 4.7e-01 4.5
P2 SN3r 1 4.3e-01 3.1
P2 W 1 4.7e-01 4.6
SN3r SN3r 1 4.1e-01 2.6
SN3r W 1 4.3e-01 3.2
W W 1 4.7e-01 4.65
"""


def nonbond(path):
    table = {}
    for section, raw in bridges._sections(path):
        fields = raw.split(";", 1)[0].split()
        if section == "nonbond_params" and len(fields) == 5:
            table[frozenset(fields[:2])] = (float(fields[3]), float(fields[4]))
    return table


def test_lambda_scales_only_protein_polymer_pairs(tmp_path):
    write_components(tmp_path)
    (tmp_path / "martini_v3.0.0.itp").write_text(MINI_FF)
    renamed, polymer, record = bridges.scale_protein_polymer(tmp_path, ["Protein_0.itp"], "polymer.itp", 0.7)
    table = nonbond(tmp_path / "martini_lambda.itp")
    assert table[frozenset(("P2_pr", "SN3r_pl"))] == pytest.approx((0.43, 3.1 * 0.7))
    assert table[frozenset(("P2_pr", "P2_pr"))] == (0.47, 4.5) and table[frozenset(("SN3r_pl", "SN3r_pl"))] == (0.41, 2.6)
    assert table[frozenset(("SN3r_pl", "W"))] == (0.43, 3.2) and table[frozenset(("P2_pr", "SN3r"))] == (0.43, 3.1)
    assert table[frozenset(("P2_pr", "U"))] == (0.0, 0.0)  # comb-rule 2 of the zero atomtype sigma/epsilon, as grompp generates
    assert record["pairs_written"] == len(table) == 11 and record["protein_bead_types"] == ["P2"]
    assert renamed == ["lambda_Protein_0.itp"] and polymer == "lambda_polymer.itp"
    protein = (tmp_path / "lambda_Protein_0.itp").read_text()
    assert " 1 P2_pr 1 ALA BB 1 3" in protein and "1 1 1000 1000 1000" in protein and " P2 " not in protein
    assert "SN3r_pl" in (tmp_path / "lambda_polymer.itp").read_text() and "; comment" in (tmp_path / "lambda_polymer.itp").read_text()
    cfg = bridges._validated({"bridge_mode": "simulate", "chains": 2, "box_nm": 6, "polymer_net_charge": -1, "protein_polymer_lambda": 0.7})
    _, solvation = bridges._assemble_solvated(cfg, tmp_path)
    top = (tmp_path / "system.top").read_text().splitlines()
    assert top[:2] == ['#include "martini_v3.0.0.itp"', '#include "martini_lambda.itp"']
    assert '#include "lambda_Protein_0.itp"' in top and '#include "lambda_polymer.itp"' in top and '#include "polymer.itp"' not in top
    assert solvation["protein_polymer_lambda"]["pairs_written"] == 11
    with pytest.raises(ValueError, match="protein_polymer_lambda"):
        bridges._validated({"protein_polymer_lambda": 0.3})


def test_lambda_scaling_changes_only_the_cross_energy_in_gromacs(tmp_path):
    import shutil
    import subprocess
    gmx = bridges.bridge_capabilities()["executables"]["gmx"]
    if not gmx:
        pytest.skip("Requires GROMACS")
    from workbench import forcefields
    try:
        directory, _ = forcefields.martini3_parameters()
    except OSError as exc:
        pytest.skip(f"Pinned Martini 3 parameters are not available offline: {exc}")
    shutil.copyfile(directory / "martini_v3.0.0.itp", tmp_path / "martini_v3.0.0.itp")
    (tmp_path / "Protein_0.itp").write_text("[ moleculetype ]\nProtein_0 1\n[ atoms ]\n1 P2 1 ALA BB 1 0\n2 TC3 1 ALA SC1 2 0\n[ bonds ]\n1 2 1 0.27 5000\n")
    (tmp_path / "polymer.itp").write_text("[ moleculetype ]\nPolymer 1\n[ atoms ]\n1 SN3r 1 PEO EO 1 0\n2 SN3r 2 PEO EO 2 0\n[ bonds ]\n1 2 1 0.33 7000\n")
    renamed, polymer, _ = bridges.scale_protein_polymer(tmp_path, ["Protein_0.itp"], "polymer.itp", 0.6)
    # Beads 0.5-0.6 nm apart: inside the attractive well of each protein-polymer pair.
    (tmp_path / "pair.gro").write_text("synthetic contact\n4\n    1ALA     BB    1   2.500   2.500   2.500\n    1ALA    SC1    2   2.770   2.500   2.500\n"
                                       "    2PEO     EO    3   2.500   3.000   2.500\n    3PEO     EO    4   2.830   3.050   2.500\n   5.00000   5.00000   5.00000\n")
    (tmp_path / "rerun.mdp").write_text("integrator = md\nnsteps = 0\ncutoff-scheme = Verlet\nnstlist = 20\ncoulombtype = reaction-field\nrcoulomb = 1.1\n"
                                        "epsilon_r = 15\nvdw_type = cutoff\nvdw-modifier = Potential-shift-verlet\nrvdw = 1.1\ncontinuation = yes\nenergygrps = Prot Poly\n")
    (tmp_path / "groups.ndx").write_text("[ System ]\n1 2 3 4\n[ Prot ]\n1 2\n[ Poly ]\n3 4\n")
    energies = {}
    for name, includes in (("plain", ['"martini_v3.0.0.itp"', '"Protein_0.itp"', '"polymer.itp"']),
                           ("scaled", ['"martini_v3.0.0.itp"', '"martini_lambda.itp"', f'"{renamed[0]}"', f'"{polymer}"'])):
        (tmp_path / f"{name}.top").write_text("".join(f"#include {i}\n" for i in includes) + "[ system ]\nx\n[ molecules ]\nProtein_0 1\nPolymer 1\n")
        run = lambda *argv: subprocess.run([gmx, *argv], cwd=tmp_path, capture_output=True, text=True, check=True, env={**bridges.runtime_environment(), "OMP_NUM_THREADS": "1"})
        run("grompp", "-f", "rerun.mdp", "-c", "pair.gro", "-n", "groups.ndx", "-p", f"{name}.top", "-o", f"{name}.tpr")
        run("mdrun", "-s", f"{name}.tpr", "-rerun", "pair.gro", "-deffnm", name, "-nt", "1")
        terms = "LJ-SR:Prot-Poly\nLJ-SR:Prot-Prot\nLJ-SR:Poly-Poly\n"
        subprocess.run([gmx, "energy", "-f", f"{name}.edr", "-o", f"{name}.xvg"], cwd=tmp_path, input=terms, capture_output=True, text=True, check=True)
        text = (tmp_path / f"{name}.xvg").read_text().splitlines()
        # gmx energy orders columns by its own term index, so read the legends.
        legends = [line.split("legend", 1)[1].strip().strip('"') for line in text if line.startswith("@ s") and "legend" in line]
        row = [line for line in text if not line.startswith(("#", "@"))][0]
        energies[name] = dict(zip(legends, map(float, row.split()[1:])))
    cross, protein, chain = ((energies["plain"][k], energies["scaled"][k]) for k in ("LJ-SR:Prot-Poly", "LJ-SR:Prot-Prot", "LJ-SR:Poly-Poly"))
    assert cross[0] < -1 and cross[1] == pytest.approx(0.6 * cross[0], rel=1e-5)
    assert protein[1] == pytest.approx(protein[0]) and chain[1] == pytest.approx(chain[0])


def plddt_pdb(values, chains="A"):
    lines = []
    for chain in chains:
        for i, value in enumerate(values, 1):
            lines.append(f"ATOM  {i:5d}  CA  ALA {chain}{i:4d}    {3.8 * i:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00{value:6.2f}           C")
    return "\n".join(lines) + "\nEND\n"


def test_plddt_regions_keep_confident_runs_only():
    values = [40, 45, 90, 91, 92, 93, 60, 88, 89, 30, 95, 96, 97, 98]
    regions = bridges.plddt_regions(plddt_pdb(values), 70)
    # A 2-residue run (8-9) is too short to hold an elastic network.
    assert regions["regions"] == [[3, 6], [11, 14]] and regions["eunit"] == "3:6,11:14" and regions["residues_in_regions"] == 8
    with pytest.raises(ValueError, match="single-chain"):
        bridges.plddt_regions(plddt_pdb(values, "AB"), 70)
    with pytest.raises(ValueError, match="No run"):
        bridges.plddt_regions(plddt_pdb([50] * 10), 70)
    with pytest.raises(ValueError, match="bfactor_is_plddt"):
        bridges._validated({"elastic_network": True, "elastic_plddt_min": 70})
    from workbench.models import JobConfig
    with pytest.raises(ValueError, match="pLDDT"):
        JobConfig.model_validate({"route": "mixture", "bridge_mode": "simulate", "elastic_plddt_min": 70, "elastic_network": True})
    with pytest.raises(ValueError, match="simulate"):
        JobConfig.model_validate({"route": "mixture", "bridge_mode": "protein_cg", "protein_polymer_lambda": 0.8})


def test_martinize2_builds_elastic_bonds_only_inside_confident_regions(tmp_path):
    if not bridges.bridge_capabilities()["executables"]["martinize2"]:
        pytest.skip("Requires installed martinize2")
    lines = (Path(__file__).parent / "fixtures" / "villin_1vii.pdb").read_text().splitlines()
    resids = sorted({int(line[22:26]) for line in lines if line.startswith("ATOM")})
    # Synthetic pLDDT: low at both termini, confident in residues 46-70 (input numbering).
    value = lambda resid: 90.0 if 46 <= resid <= 70 else 40.0
    pdb = "\n".join(line[:60] + f"{value(int(line[22:26])):6.2f}" + line[66:] if line.startswith("ATOM") else line for line in lines) + "\n"
    config = {"bridge_track": "martini3", "bridge_mode": "protein_cg", "pdb_text": pdb, "secondary_structure": "H" * len(resids),
              "monomers": 5, "chains": 1, "elastic_network": True, "elastic_plddt_min": 70, "bfactor_is_plddt": True}
    bridges.run_bridge(config, tmp_path)
    assert json.loads((tmp_path / "elastic_regions.json").read_text())["eunit"] == "46:70"
    atoms, bands, section, block = {}, [], None, None
    for raw in (tmp_path / "Protein_0.itp").read_text().splitlines():
        if raw.strip().startswith(";"):
            block = raw.strip().lstrip("; ").lower()
            continue
        fields = raw.split(";", 1)[0].split()
        if fields[:1] == ["["]:
            section, block = fields[1], None
        elif section == "atoms" and len(fields) > 3:
            atoms[int(fields[0])] = int(fields[2])
        elif section == "bonds" and block == "rubber band" and len(fields) >= 5:
            bands.append((atoms[int(fields[0])], atoms[int(fields[1])]))
    # martinize2 renumbers residues from 1 in the itp: input 46-70 are 6-30 here.
    first = resids.index(46) + 1
    assert bands and all(first <= a <= first + 24 and first <= b <= first + 24 for a, b in bands)
