"""Repeat-unit topology for connectivity-index property correlations (the approach of Bicerano,
Prediction of Polymer Properties, used by Materials Studio Synthia).

The Rust kernel (native/src/topology.rs) computes Randić / Kier–Hall connectivity indices of the
hydrogen-suppressed repeat unit closed into a ring through the bond between its two attachment
atoms, so every value is per repeat unit of an infinitely long chain. Property correlations are not
included yet: their coefficients are entered only from a cited primary source (AGENTS.md principle 7).
"""
from __future__ import annotations

try:
    import workbench_native as native
except ImportError:  # pragma: no cover - exercised where the Rust kernel is not built
    native = None

METHOD = "Randić (1975) and Kier-Hall (1976) connectivity indices of the hydrogen-suppressed graph; valence delta (Zv - h)/(Z - Zv - 1)"
CONVENTION = "repeat unit closed into a ring through its two attachment atoms: values per repeat unit of an infinite chain"


def _kernel():
    if native is None or not hasattr(native, "connectivity_indices"):
        raise RuntimeError("Rust 커널(workbench_native)이 필요합니다. `.venv/bin/python -m pip install ./native`로 빌드하세요.")
    return native


def graph(mol):
    """Heavy atoms (dummy atoms excluded), their attached hydrogens and heavy-atom bonds."""
    heavy = [a.GetIdx() for a in mol.GetAtoms() if a.GetAtomicNum() > 1]
    index = {old: new for new, old in enumerate(heavy)}
    atomic = [mol.GetAtomWithIdx(i).GetAtomicNum() for i in heavy]
    hydrogens = [mol.GetAtomWithIdx(i).GetTotalNumHs(includeNeighbors=True) for i in heavy]
    bonds = [(index[b.GetBeginAtomIdx()], index[b.GetEndAtomIdx()]) for b in mol.GetBonds()
             if b.GetBeginAtomIdx() in index and b.GetEndAtomIdx() in index]
    return atomic, hydrogens, bonds, heavy


def _indices(result):
    return {key: float(result[key]) for key in ("chi0", "chi1", "chi0v", "chi1v")}


def molecule_indices(smiles):
    """Connectivity indices of an ordinary molecule (no attachment atoms)."""
    from rdkit import Chem
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError("SMILES를 읽을 수 없습니다.")
    atomic, hydrogens, bonds, _ = graph(mol)
    return _indices(_kernel().connectivity_indices(atomic, hydrogens, bonds))


def repeat_unit(smiles):
    """Per-repeat-unit connectivity indices, backbone and side-group atoms of a `[*]...[*]` repeat unit."""
    from rdkit import Chem
    from rdkit.Chem import Descriptors
    from .preflight import repeat_unit as check
    report = check(smiles)
    if not report["valid"]:
        raise ValueError(report["error"])
    mol = Chem.MolFromSmiles(smiles)
    dummies = sorted(a.GetIdx() for a in mol.GetAtoms() if a.GetAtomicNum() == 0)
    ends = [mol.GetAtomWithIdx(i).GetNeighbors()[0].GetIdx() for i in dummies]
    # Hydrogen counts come from the unit with its dummies in place, so the chain bonds are not refilled with H.
    atomic, hydrogens, bonds, heavy = graph(mol)
    index = {old: new for new, old in enumerate(heavy)}
    bonds.append((index[ends[0]], index[ends[1]]))
    kernel = _kernel()
    result = kernel.connectivity_indices(atomic, hydrogens, bonds)
    backbone = kernel.block_atoms(len(heavy), bonds, len(bonds) - 1)
    atoms = [{"index": old, "symbol": mol.GetAtomWithIdx(old).GetSymbol(), "hydrogens": h,
              "delta": float(d), "delta_v": float(dv), "backbone": bool(b)}
             for old, h, d, dv, b in zip(heavy, hydrogens, result["delta"], result["delta_v"], backbone)]
    warnings = ["Connectivity indices are structural descriptors; no property is estimated from them until correlations from a cited source are added."]
    if any(a.GetFormalCharge() for a in mol.GetAtoms()):
        warnings.append("Formal charges are ignored in the valence deltas, as in RDKit's Kier-Hall indices.")
    return {"smiles": smiles, "canonical_smiles": Chem.MolToSmiles(mol), "heavy_atoms": len(heavy), "hydrogens": sum(hydrogens),
            "repeat_unit_mass_g_mol": Descriptors.MolWt(mol), "connectivity": _indices(result),
            "backbone_atoms": sum(backbone), "side_group_atoms": len(heavy) - sum(backbone), "atoms": atoms,
            "kernel": {"engine": "rust", "version": native.__version__}, "method": METHOD, "convention": CONVENTION,
            "backbone_definition": "atoms of the biconnected component that holds the closing bond: every atom on a path between the two attachment atoms, whole main-chain rings included",
            "warnings": warnings}
