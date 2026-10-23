"""Connectivity indices from the Rust kernel against RDKit, and the per-repeat-unit convention against oligomers."""
import pytest

pytest.importorskip("rdkit")
native = pytest.importorskip("workbench_native")
if not hasattr(native, "connectivity_indices"):
    pytest.skip("workbench_native was built before the topology kernel", allow_module_level=True)

from rdkit import Chem  # noqa: E402
from rdkit.Chem import GraphDescriptors  # noqa: E402

from workbench import correlations  # noqa: E402

UNITS = {
    "polyethylene": "[*]CC[*]",
    "polymethylene": "[*]C[*]",
    "polystyrene": "[*]CC([*])c1ccccc1",
    "PMMA": "[*]CC([*])(C)C(=O)OC",
    "PVC": "[*]CC([*])Cl",
    "PTFE": "[*]C(F)(F)C([*])(F)F",
    "PEO": "[*]CCO[*]",
    "PDMS": "[*]O[Si](C)(C)[*]",
    "PET": "[*]OCCOC(=O)c1ccc(cc1)C(=O)[*]",
    "nylon-6,6": "[*]C(=O)CCCCC(=O)NCCCCCCN[*]",
    "BPA polycarbonate": "[*]Oc1ccc(cc1)C(C)(C)c1ccc(cc1)OC([*])=O",
    "polysulfone fragment": "[*]c1ccc(cc1)S(=O)(=O)c1ccc(cc1)[*]",
    "poly(vinyl bromide)": "[*]CC([*])Br",
}


def rdkit_indices(mol):
    return {"chi0": GraphDescriptors.Chi0(mol), "chi1": GraphDescriptors.Chi1(mol),
            "chi0v": GraphDescriptors.Chi0v(mol), "chi1v": GraphDescriptors.Chi1v(mol)}


@pytest.mark.parametrize("smiles", ["CCO", "c1ccccc1O", "CC(=O)OC", "ClC(Cl)C", "BrCCBr", "Ic1ccccc1", "CS(=O)(=O)C",
                                    "C[Si](C)(C)O", "FC(F)(F)C", "CN(C)C=O", "C1CCC2CCCCC2C1", "OC(=O)c1ccc(cc1)C(=O)O",
                                    "CC#N", "C[N+](=O)[O-]"])
def test_kernel_matches_rdkit_kier_hall_indices(smiles):
    expected = rdkit_indices(Chem.MolFromSmiles(smiles))
    assert correlations.molecule_indices(smiles) == pytest.approx(expected, abs=1e-10)


def oligomer(smiles, n):
    """H-capped head-to-tail n-mer: the first '*' of each unit is its head, the second its tail."""
    unit = Chem.MolFromSmiles(smiles)
    chain = Chem.RWMol(unit)
    head, local_tail = sorted(a.GetIdx() for a in unit.GetAtoms() if a.GetAtomicNum() == 0)
    tail = local_tail
    for _ in range(n - 1):
        offset = chain.GetNumAtoms()
        chain.InsertMol(unit)
        old_tail, new_head = tail, head + offset
        a = chain.GetAtomWithIdx(old_tail).GetNeighbors()[0].GetIdx()
        b = chain.GetAtomWithIdx(new_head).GetNeighbors()[0].GetIdx()
        chain.AddBond(a, b, Chem.BondType.SINGLE)
        # Mark the two joined dummies; the new unit's tail becomes the chain tail.
        chain.GetAtomWithIdx(old_tail).SetAtomicNum(1)
        chain.GetAtomWithIdx(new_head).SetAtomicNum(1)
        chain.GetAtomWithIdx(old_tail).SetIntProp("joined", 1)
        chain.GetAtomWithIdx(new_head).SetIntProp("joined", 1)
        tail = local_tail + offset
    for atom in sorted((a.GetIdx() for a in chain.GetAtoms() if a.HasProp("joined")), reverse=True):
        chain.RemoveAtom(atom)
    for atom in chain.GetAtoms():
        if atom.GetAtomicNum() == 0:
            atom.SetAtomicNum(1)  # end caps
    mol = chain.GetMol()
    Chem.SanitizeMol(mol)
    return Chem.RemoveHs(mol)


@pytest.mark.parametrize("name", sorted(UNITS))
def test_per_unit_indices_equal_the_oligomer_increment(name):
    # Each index is a sum of local terms, so one more middle unit adds exactly the infinite-chain value.
    smiles = UNITS[name]
    per_unit = correlations.repeat_unit(smiles)["connectivity"]
    five, six = (rdkit_indices(oligomer(smiles, n)) for n in (5, 6))
    increment = {key: six[key] - five[key] for key in five}
    assert per_unit == pytest.approx(increment, abs=1e-9)


@pytest.mark.parametrize("name,backbone,side", [("polystyrene", 2, 6), ("PMMA", 2, 5), ("BPA polycarbonate", 16, 3),
                                                ("nylon-6,6", 14, 2), ("polymethylene", 1, 0), ("PDMS", 2, 2)])
def test_backbone_holds_main_chain_rings_but_not_side_groups(name, backbone, side):
    report = correlations.repeat_unit(UNITS[name])
    assert (report["backbone_atoms"], report["side_group_atoms"]) == (backbone, side)


def test_polyethylene_values_and_report_fields():
    report = correlations.repeat_unit("[*]CC[*]")
    # Two CH2 with delta 2 and two backbone bonds per repeat unit.
    assert report["connectivity"] == pytest.approx({"chi0": 2 ** 0.5, "chi1": 1.0, "chi0v": 2 ** 0.5, "chi1v": 1.0})
    assert report["repeat_unit_mass_g_mol"] == pytest.approx(28.054, abs=0.01) and report["hydrogens"] == 4
    assert "per repeat unit" in report["convention"] and report["warnings"]


def test_invalid_units_are_rejected():
    with pytest.raises(ValueError, match="연결점"):
        correlations.repeat_unit("C=Cc1ccccc1")
    with pytest.raises(ValueError, match="transition-metal"):
        correlations.repeat_unit("[*]C[Fe]C[*]")
