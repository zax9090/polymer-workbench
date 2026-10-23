"""Native amorphous cell builder: geometry kept, periodic overlaps avoided, chains grown independently."""
import shutil

import numpy as np
import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from workbench import amorphous


def chain(smiles):
    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
    assert AllChem.EmbedMolecule(mol, randomSeed=1, useRandomCoords=True) == 0
    AllChem.MMFFOptimizeMolecule(mol, maxIters=200)
    return mol


@pytest.fixture(scope="module")
def polystyrene():
    return chain("C" + "CC(c1ccccc1)" * 6 + "C")


def test_cell_keeps_every_bond_and_angle_and_fills_the_target_density(polystyrene):
    positions, box, report = amorphous.build_cell(polystyrene, 6, 0.8, seed=3)
    atoms = polystyrene.GetNumAtoms()
    mass = sum(a.GetMass() for a in polystyrene.GetAtoms()) * 6
    assert mass / amorphous.AVOGADRO / box ** 3 * 1e24 == pytest.approx(0.8)
    assert positions.shape == (6 * atoms, 3) and np.isfinite(positions).all()
    template = polystyrene.GetConformer().GetPositions()
    bonds = [(b.GetBeginAtomIdx(), b.GetEndAtomIdx()) for b in polystyrene.GetBonds()]
    angles = [(n1.GetIdx(), a.GetIdx(), n2.GetIdx()) for a in polystyrene.GetAtoms()
              for i, n1 in enumerate(a.GetNeighbors()) for n2 in list(a.GetNeighbors())[i + 1:]]

    def angle(p, i, j, k):
        u, v = p[i] - p[j], p[k] - p[j]
        return np.degrees(np.arccos(u @ v / np.linalg.norm(u) / np.linalg.norm(v)))
    for c in range(6):
        p = positions[c * atoms:(c + 1) * atoms]
        assert max(abs(np.linalg.norm(p[i] - p[j]) - np.linalg.norm(template[i] - template[j])) for i, j in bonds) < 1e-9
        assert max(abs(angle(p, *t) - angle(template, *t)) for t in angles) < 1e-6
    assert report["closest_contact_angstrom"] > 0.3 and report["unresolved_steps"] <= 3


def test_chains_get_their_own_conformations(polystyrene):
    positions, _, _ = amorphous.build_cell(polystyrene, 4, 0.5, seed=5)
    atoms = polystyrene.GetNumAtoms()
    heavy = [a.GetIdx() for a in polystyrene.GetAtoms() if a.GetAtomicNum() > 1]
    shapes = []
    for c in range(4):
        p = positions[c * atoms:(c + 1) * atoms][heavy]
        p = p - p.mean(axis=0)
        shapes.append(np.sort(np.linalg.norm(p[:, None] - p[None], axis=-1).ravel()))
    # Internal distance profiles differ between chains (RadonPy's amorphous_cell copies one conformer).
    assert min(np.abs(shapes[i] - shapes[j]).max() for i in range(4) for j in range(i + 1, 4)) > 0.5


def test_overlap_check_uses_the_minimum_image():
    near = [{0}, {1}]
    positions = np.array([[0.1, 5.0, 5.0], [9.8, 5.0, 5.0]])
    report = amorphous.contact_report(positions, 10.0, np.array([0, 1]), np.array([0, 1]), near, None)
    assert report["closest_contact_angstrom"] == pytest.approx(0.3)


def test_same_seed_gives_the_same_cell(polystyrene):
    first, _, _ = amorphous.build_cell(polystyrene, 3, 0.6, seed=11)
    second, _, _ = amorphous.build_cell(polystyrene, 3, 0.6, seed=11)
    assert np.array_equal(first, second)


def test_native_builder_is_only_for_gaff2_bulk_cells():
    from workbench.models import JobConfig
    base = {"route": "polymer_atomistic", "atomistic_mode": "bulk", "chains": 3, "cell_builder": "native"}
    assert JobConfig.model_validate(base).build_density == 0.8
    with pytest.raises(ValueError, match="native"):
        JobConfig.model_validate({**base, "atomistic_forcefield": "pcff"})
    with pytest.raises(ValueError, match="native"):
        JobConfig.model_validate({**base, "atomistic_mode": "build", "chains": 1})


def test_native_cell_runs_through_the_bulk_route(tmp_path):
    from workbench.external import available, executable
    if not available("radonpy") or not executable("lmp", "LAMMPS_EXEC") or not shutil.which("lmp"):
        pytest.skip("Requires RadonPy and a LAMMPS executable")
    from workbench.models import JobConfig
    from workbench.science import run_workflow
    config = JobConfig.model_validate({"route": "polymer_atomistic", "atomistic_mode": "bulk", "cell_builder": "native",
                                       "repeat_smiles": "[*]CC([*])c1ccccc1", "monomers": 6, "chains": 4, "protocol_scale": 0.001}).model_dump()
    result = run_workflow(config, tmp_path)
    assert result["status"] == "completed" and result["message"].startswith("Polymer Lab native") and result["elapsed_seconds"] >= 0
    builder = result["metadata"]["builder"]
    assert builder["builder"].startswith("native") and builder["chains"] == 4 and builder["density_g_cm3"] == 0.8
    assert (tmp_path / "native_amorphous.py").read_text() == (amorphous.__file__ and open(amorphous.__file__).read())
