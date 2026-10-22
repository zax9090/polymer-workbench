"""SAP screen and SAP-targeted packing on a real small protein (PDB 1VII)."""
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("mdtraj")
from workbench import sap  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures" / "villin_1vii.pdb").read_text()


def test_black_mould_scale_matches_pinned_source():
    # Values as published (Black & Mould 1991) and verified in the REA extension audit.
    assert sap.BLACK_MOULD["PHE"] == 1.0 and sap.BLACK_MOULD["ARG"] == 0.0 and sap.BLACK_MOULD["GLY"] == 0.501
    assert len(sap.BLACK_MOULD) == 20 and all(0 <= v <= 1 for v in sap.BLACK_MOULD.values())


def test_sap_ranks_exposed_hydrophobics_above_charged_residues(tmp_path):
    result = sap.sap_scan(FIXTURE)
    by_residue = {(r["resname"], r["resid"]): r["sap"] for r in result["residues"]}
    assert result["summary"]["residues"] == 36
    # Exposed C-terminal Phe76 and Trp64 outrank the charged Asp44/Glu45 surface.
    assert by_residue[("PHE", 76)] == max(by_residue.values())
    assert by_residue[("TRP", 64)] > 0 > by_residue[("ASP", 44)] and by_residue[("GLU", 45)] < 0
    assert {"PHE"} <= {r["resname"] for r in result["patch"]["residues"]}
    assert set(result["reference_side_chain_sasa_angstrom2"]) == set(sap.BLACK_MOULD) - {"GLY"}
    stored = sap.write_sap_outputs(result, FIXTURE, tmp_path)
    lines = [l for l in (tmp_path / "structure_sap.pdb").read_text().splitlines() if l.startswith("ATOM")]
    assert len(lines) == len(result["atom_sap"]) and all(l[12:16].strip()[0] != "H" for l in lines)
    assert stored["input_sha256"] and "atom_sap" not in stored
    with pytest.raises(ValueError):
        sap.sap_scan(FIXTURE, radius=1)
    with pytest.raises(ValueError, match="standard-residue"):
        sap.sap_scan("HETATM    1  O   HOH A   1       0.000   0.000   0.000  1.00  0.00           O\n")


def test_packmol_places_chains_around_sap_patch(tmp_path):
    from workbench.external import executable
    if not executable("packmol", "PACKMOL_EXEC"):
        pytest.skip("Requires PACKMOL")
    from workbench.models import JobConfig
    from workbench.science import run_workflow
    three_models = "MODEL        0\n" + FIXTURE.replace("END\n", "") + "ENDMDL\nMODEL        1\n" + FIXTURE.replace("END\n", "") + "ENDMDL\n"
    config = JobConfig.model_validate({"route": "packmol", "repeat_smiles": "[*]CCO[*]", "monomers": 6, "chains": 2,
                                       "pdb_text": three_models, "packmol_target": "sap_patch"}).model_dump()
    result = run_workflow(config, tmp_path)
    targeting = result["metadata"]["targeting"]
    assert result["status"] == "prepared" and targeting["mode"] == "sap_patch"
    atoms = [l for l in (tmp_path / "packed.pdb").read_text().splitlines() if l.startswith(("ATOM", "HETATM"))]
    xyz = np.array([[float(l[30:38]), float(l[38:46]), float(l[46:54])] for l in atoms])
    per_chain = sum(l.startswith(("ATOM", "HETATM")) for l in (tmp_path / "chain.pdb").read_text().splitlines())
    chains, protein = xyz[:2 * per_chain], xyz[2 * per_chain:]
    source = np.array([[float(l[30:38]), float(l[38:46]), float(l[46:54])] for l in FIXTURE.splitlines() if l.startswith("ATOM")])
    # Only the first model is packed, rigidly translated to the box centre.
    assert len(protein) == len(source)
    assert np.abs(protein - (source + targeting["protein_translation_angstrom"])).max() < 1e-3
    distance = np.linalg.norm(chains - targeting["sphere_center_angstrom"], axis=1)
    assert distance.max() <= targeting["sphere_radius_angstrom"] + 1e-3
    assert any("placement choice" in w for w in result["warnings"])
    random = JobConfig.model_validate({**config, "packmol_target": "none", "pdb_text": ""}).model_dump()
    with pytest.raises(ValueError, match="needs a protein"):
        run_workflow({**random, "packmol_target": "sap_patch"}, tmp_path / "missing")
