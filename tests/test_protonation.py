"""PROPKA3 titration, surface charge patches, PDB2PQR protonation and their use in packing and Martini bridges."""
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("propka")
pytest.importorskip("mdtraj")
from workbench import protonation  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
VILLIN = (FIXTURES / "villin_1vii.pdb").read_text()
HPX = (FIXTURES / "propka_1HPX.pdb").read_text()


def test_titration_reproduces_upstream_propka_regression():
    reference = json.loads((FIXTURES / "propka_1HPX.json").read_text())
    result = protonation.titrate(HPX, 7.0)
    reported = [g["pka"] for g in result["groups"] if g["reported"]]
    assert reported == pytest.approx(reference["pKa"], abs=0.01)  # upstream tolerance, tests/test_basic_regression.py
    assert result["isoelectric_point"] == pytest.approx(reference["pI_folded"], abs=0.01)
    assert result["isoelectric_point_unfolded"] == pytest.approx(reference["pI_unfolded"], abs=0.01)
    by_site = {(g["type"], g["chain"], g["resid"]): g for g in result["groups"]}
    # The catalytic Asp25 pair titrates asymmetrically: one stays protonated at pH 7.
    assert {by_site[("ASP", "A", 25)]["state"], by_site[("ASP", "B", 25)]["state"]} == {"protonated", "deprotonated"}
    assert by_site[("HIS", "A", 69)]["ambiguous"] and result["summary"]["ambiguous"] == 2


def test_charges_follow_henderson_hasselbalch_and_the_charge_curve():
    result = protonation.titrate(VILLIN, 7.0)
    assert result["summary"]["groups"] == 12 and 1.5 < result["net_charge"] < 2.5
    for group in result["groups"]:
        expected = group["charge_sign"] / (1 + 10 ** (group["charge_sign"] * (7.0 - group["pka"])))
        assert group["charge_at_ph"] == pytest.approx(expected, abs=2e-3)
    assert sum(g["charge_at_ph"] for g in result["groups"]) == pytest.approx(result["net_charge"], abs=0.01)
    charges = [q for _, q in result["charge_curve"]]
    assert all(a >= b for a, b in zip(charges, charges[1:]))
    assert charges[0] > 0 > charges[-1]
    low = protonation.titrate(VILLIN, 2.0)
    assert low["net_charge"] > result["net_charge"] and low["ph"] == 2.0
    with pytest.raises(ValueError):
        protonation.titrate(VILLIN, 15)
    with pytest.raises(ValueError, match="ATOM"):
        protonation.titrate("HETATM    1  O   HOH A   1       0.000   0.000   0.000  1.00  0.00           O\n")


def test_screening_lengths():
    assert protonation.bjerrum_length(298.15) == pytest.approx(7.15, abs=0.01)
    assert protonation.debye_length(0.15) == pytest.approx(3.04 / 0.15 ** 0.5, rel=0.01)
    assert protonation.debye_length(0.0) == float("inf")


def test_surface_patches_find_the_acidic_cluster(tmp_path):
    titration = protonation.titrate(VILLIN, 7.0)
    surface = protonation.surface_potential(VILLIN, titration, 0.15)
    negative, positive = surface["patches"]["negative"], surface["patches"]["positive"]
    acidic = {(g["type"], g["resid"]) for g in negative["charged_groups"]}
    assert {("ASP", 44), ("GLU", 45), ("ASP", 46)} <= acidic
    assert negative["peak_potential"] < -0.5 < 0.5 < positive["peak_potential"]
    assert all(g["charge_at_ph"] > 0 for g in positive["charged_groups"])
    dilute = protonation.surface_potential(VILLIN, titration, 0.01)["patches"]["negative"]["peak_potential"]
    salty = protonation.surface_potential(VILLIN, titration, 1.0)["patches"]["negative"]["peak_potential"]
    assert dilute < negative["peak_potential"] < salty < 0  # screening shrinks the potential
    stored = protonation.write_outputs({**titration, "surface": surface, "warnings": []}, VILLIN, tmp_path)
    lines = [l for l in (tmp_path / "structure_potential.pdb").read_text().splitlines() if l.startswith("ATOM")]
    assert len(lines) == len(surface["atom_potential"]) and "atom_potential" not in stored["surface"]
    assert json.loads((tmp_path / "protonation.json").read_text())["surface"]["debye_length_angstrom"] == pytest.approx(7.85, abs=0.01)


def test_pdb2pqr_protonated_structure_stays_local(tmp_path, monkeypatch):
    pytest.importorskip("pdb2pqr")
    import pdb2pqr.io

    def offline(*args, **kwargs):
        raise AssertionError("PDB2PQR attempted a network download")
    monkeypatch.setattr(pdb2pqr.io.requests, "get", offline)
    neutral = protonation.protonate(HPX, 7.0, tmp_path)
    assert neutral["integer_charge"] == 6 and neutral["residue_states"]["ASH"] == 1 and "HIP" in neutral["residue_states"]
    assert neutral["omitted_residues"] == ["KNI B900"]
    atoms = [l for l in (tmp_path / "protein_protonated.pdb").read_text().splitlines() if l.startswith("ATOM")]
    assert any(l[12:16].strip().startswith("H") for l in atoms) and not any(l.startswith("HETATM") for l in atoms)
    acidic = protonation.protonate(HPX, 4.0, tmp_path)
    assert acidic["integer_charge"] > neutral["integer_charge"] and acidic["residue_states"]["GLH"] > 0


def test_protein_import_attaches_titration(tmp_path):
    from workbench.models import JobConfig
    from workbench.science import run_workflow
    config = JobConfig.model_validate({"route": "protein", "protein_mode": "import", "pdb_text": VILLIN, "protein_ph": 6.0,
                                       "ionic_strength_molar": 0.05}).model_dump()
    result = run_workflow(config, tmp_path)
    titration = result["protonation"]
    assert titration["available"] and titration["ph"] == 6.0 and titration["ionic_strength_molar"] == 0.05
    assert titration["patches"]["negative"]["residues"] and (tmp_path / "protonation.json").is_file()
    assert result["sap"]["available"]


def test_packmol_places_cationic_chains_at_the_acidic_patch(tmp_path):
    from workbench.external import executable
    if not executable("packmol", "PACKMOL_EXEC"):
        pytest.skip("Requires PACKMOL")
    from workbench.models import JobConfig
    from workbench.science import run_workflow
    config = JobConfig.model_validate({"route": "packmol", "repeat_smiles": "[*]CC([*])C[NH3+]", "monomers": 6, "chains": 2,
                                       "pdb_text": VILLIN, "packmol_target": "negative_patch", "protein_ph": 7.0}).model_dump()
    result = run_workflow(config, tmp_path)
    targeting = result["metadata"]["targeting"]
    assert result["status"] == "prepared" and targeting["mode"] == "negative_patch"
    assert targeting["polymer_formal_charge_per_chain"] == 6 and targeting["charge_complementary"]
    assert {("ASP", 44), ("GLU", 45)} <= {(g["type"], g["resid"]) for g in targeting["charged_groups"]}
    atoms = [l for l in (tmp_path / "packed.pdb").read_text().splitlines() if l.startswith(("ATOM", "HETATM"))]
    xyz = np.array([[float(l[30:38]), float(l[38:46]), float(l[46:54])] for l in atoms])
    per_chain = sum(l.startswith(("ATOM", "HETATM")) for l in (tmp_path / "chain.pdb").read_text().splitlines())
    assert np.linalg.norm(xyz[:2 * per_chain] - targeting["sphere_center_angstrom"], axis=1).max() <= targeting["sphere_radius_angstrom"] + 1e-3
    neutral = run_workflow({**config, "repeat_smiles": "[*]CCO[*]"}, tmp_path / "neutral")
    assert neutral["metadata"]["targeting"]["charge_complementary"] is False
    assert any("no electrostatic attraction" in w for w in neutral["warnings"])


def test_martini_bridge_uses_propka_states(tmp_path):
    pytest.importorskip("pdb2pqr")
    from workbench import bridges
    caps = bridges.bridge_capabilities()
    if not caps["executables"]["martinize2"] or not caps["executables"]["polyply"]:
        pytest.skip("Requires installed martinize2 and polyply")
    residues = len({(l[21], l[22:27]) for l in VILLIN.splitlines() if l.startswith("ATOM")})
    result = bridges.run_bridge({"bridge_track": "martini3", "bridge_mode": "protein_cg", "pdb_text": VILLIN, "secondary_structure": "C" * residues,
                                 "monomers": 5, "chains": 1, "protein_protonation": "propka", "protein_ph": 3.0}, tmp_path)
    assert result["status"] == "prepared"
    states = result["bridge"]["protonation"]
    assert states["residue_states"] == {"ASH": 2, "GLH": 2}
    charge = 0.0
    section = None
    for line in (tmp_path / "Protein_0.itp").read_text().splitlines():
        text = line.strip()
        if text.startswith("["):
            section = text.strip("[] ")
        elif section == "atoms" and text and not text.startswith(";"):
            charge += float(text.split()[6])
    assert charge == pytest.approx(states["integer_charge"])
    assert (tmp_path / "protein_submitted.pdb").read_text() == VILLIN
    with pytest.raises(ValueError, match="protein_protonation"):
        bridges.run_bridge({"pdb_text": VILLIN, "protein_protonation": "guess"}, tmp_path / "bad")
