"""Spatial aggregation propensity (SAP) of a static protein structure.

Follows Chennamsetty et al., PNAS 106, 11937 (2009): for each heavy atom i,
SAP_i = sum over residues with at least half of their side-chain atoms within R of i of
  (SASA of those side-chain atoms within R / SASA of the fully exposed side chain) x hydrophobicity,
with the Black & Mould scale shifted so glycine is zero. The paper averages over MD; this
is one structure, so SAP here is a structural screen, not an aggregation measurement.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile
import warnings

import numpy as np

# Black & Mould, Anal. Biochem. 193, 72 (1991), normalized 0-1. Verified against
# althonos/peptides.py@eedec680 peptides/tables/hydrophobicity/BlackMould.csv (REA audited).
BLACK_MOULD = {"ALA": 0.616, "ARG": 0.0, "ASN": 0.236, "ASP": 0.028, "CYS": 0.68, "GLN": 0.251, "GLU": 0.043,
               "GLY": 0.501, "HIS": 0.165, "ILE": 0.943, "LEU": 0.943, "LYS": 0.283, "MET": 0.738, "PHE": 1.0,
               "PRO": 0.711, "SER": 0.359, "THR": 0.45, "TRP": 0.878, "TYR": 0.88, "VAL": 0.825}
SCALE_SOURCE = {"citation": "Black & Mould, Anal. Biochem. 193, 72-82 (1991)", "repository": "althonos/peptides.py",
                "commit": "eedec6802b9d17d8895fe5686ae9e371f6be551b", "path": "peptides/tables/hydrophobicity/BlackMould.csv",
                "sha256": "5425276ef209aa8c5b99797a06fbfcaae659f97a9265a18d5fe0eb2b996143ab"}
ONE_LETTER = {"ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
              "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V"}
BACKBONE = {"N", "CA", "C", "O", "OXT"}
_REFERENCE = {}


def _clean_pdb(text):
    """First model, standard residues, heavy atoms, first alternate location."""
    lines, seen = [], set()
    for line in text.splitlines():
        if line.startswith("ENDMDL"):
            break
        if not line.startswith("ATOM  ") or len(line) < 54 or line[17:20] not in BLACK_MOULD or line[16] not in " A":
            continue
        element = (line[76:78].strip() if len(line) >= 78 else "") or line[12:16].strip()[0]
        if element.upper() in {"H", "D"}:
            continue
        key = (line[21], line[22:27], line[12:16])
        if key in seen:
            continue
        seen.add(key)
        lines.append(line[:16] + " " + line[17:])
    if not lines:
        raise ValueError("SAP needs standard-residue ATOM records")
    return "\n".join(lines + ["END"]) + "\n"


def _sasa(pdb_text):
    import mdtraj
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "input.pdb"
        path.write_text(pdb_text)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            trajectory = mdtraj.load_pdb(str(path), standard_names=False)
    # mdtraj reports nm^2 (Shrake-Rupley, 0.14 nm probe, 960 sphere points).
    return trajectory, mdtraj.shrake_rupley(trajectory, mode="atom")[0] * 100.0


def reference_side_chains(seed=42):
    """Side-chain SASA of residue X in an Ala-X-Ala tripeptide (Chennamsetty's fully exposed reference)."""
    if _REFERENCE:
        return _REFERENCE
    from rdkit import Chem
    from rdkit.Chem import AllChem
    for name, letter in ONE_LETTER.items():
        if name == "GLY":
            continue
        mol = Chem.AddHs(Chem.MolFromFASTA("A" + letter + "A"))
        params = AllChem.ETKDGv3()
        params.randomSeed = seed
        if AllChem.EmbedMolecule(mol, params) != 0:
            raise RuntimeError(f"Could not embed the Ala-{name}-Ala reference")
        AllChem.MMFFOptimizeMolecule(mol)
        trajectory, sasa = _sasa(_clean_pdb(Chem.MolToPDBBlock(Chem.RemoveHs(mol))))
        middle = trajectory.topology.residue(1)
        _REFERENCE[name] = float(sum(sasa[a.index] for a in middle.atoms if a.name not in BACKBONE))
    return _REFERENCE


def sap_scan(pdb_text, radius=5.0):
    """Per-atom and per-residue SAP, plus the most aggregation-prone surface patch."""
    from scipy.spatial import cKDTree
    if not 3 <= radius <= 15 or not math.isfinite(radius):
        raise ValueError("SAP radius must be 3-15 angstrom")
    trajectory, sasa = _sasa(_clean_pdb(pdb_text))
    reference = reference_side_chains()
    topology, coords = trajectory.topology, trajectory.xyz[0] * 10.0
    atoms = list(topology.atoms)
    residue_of = np.array([a.residue.index for a in atoms])
    side = np.array([a.name not in BACKBONE and a.residue.name != "GLY" for a in atoms])
    side_index = np.flatnonzero(side)
    counts = np.bincount(residue_of[side_index], minlength=topology.n_residues)
    hydrophobicity = np.array([BLACK_MOULD[r.name] - BLACK_MOULD["GLY"] for r in topology.residues])
    exposed = np.array([reference.get(r.name, 0.0) for r in topology.residues])
    tree = cKDTree(coords[side_index])
    atom_sap = np.zeros(len(atoms))
    for i, neighbours in enumerate(tree.query_ball_point(coords, r=radius)):
        if not neighbours:
            continue
        members = side_index[neighbours]
        residues = residue_of[members]
        inside = np.bincount(residues, minlength=topology.n_residues)
        area = np.bincount(residues, weights=sasa[members], minlength=topology.n_residues)
        # Only residues with at least half their side chain within R contribute.
        eligible = np.flatnonzero((inside > 0) & (2 * inside >= counts) & (exposed > 0))
        atom_sap[i] = float(np.sum(area[eligible] / exposed[eligible] * hydrophobicity[eligible]))
    rows = []
    for residue in topology.residues:
        index = [a.index for a in residue.atoms]
        chain_side = [a.index for a in residue.atoms if a.name not in BACKBONE]
        rows.append({"chain": residue.chain.chain_id or "", "resid": int(residue.resSeq), "resname": residue.name,
                     "sap": float(atom_sap[index].mean()),
                     "side_chain_exposure": float(sasa[chain_side].sum() / reference[residue.name]) if residue.name in reference and chain_side else None})
    patch = _patch(rows, topology, coords, residue_of, side)
    return {"method": "SAP (Chennamsetty et al. 2009) on one static structure; per-residue value is the mean of its heavy-atom SAP",
            "radius_angstrom": float(radius), "hydrophobicity_scale": {**SCALE_SOURCE, "glycine_offset": BLACK_MOULD["GLY"]},
            "sasa": {"engine": "mdtraj.shrake_rupley", "probe_nm": 0.14, "sphere_points": 960, "atoms": "heavy atoms"},
            "reference_side_chain_sasa_angstrom2": {k: round(v, 3) for k, v in reference.items()},
            "residues": rows, "atom_sap": atom_sap.round(5).tolist(), "patch": patch,
            "summary": {"residues": len(rows), "sap_max": float(max(r["sap"] for r in rows)),
                        "positive_residues": sum(r["sap"] > 0 for r in rows)},
            "warnings": ["The original SAP averages over an MD ensemble; one structure can over- or under-weight flexible side chains.",
                         "SAP ranks hydrophobic surface exposure; it does not predict aggregation rates or polymer binding."]}


def _patch(rows, topology, coords, residue_of, side, reach=10.0):
    positive = [i for i, row in enumerate(rows) if row["sap"] > 0]
    if not positive:
        return None
    top = max(positive, key=lambda i: rows[i]["sap"])
    def side_coords(index):
        mask = (residue_of == index) & side
        return coords[mask] if mask.any() else coords[residue_of == index]
    anchor = side_coords(top).mean(axis=0)
    members = [i for i in positive if np.min(np.linalg.norm(side_coords(i) - anchor, axis=1)) <= reach]
    points = np.vstack([side_coords(i) for i in members])
    center = points.mean(axis=0)
    return {"center_angstrom": center.round(3).tolist(), "radius_angstrom": float(np.linalg.norm(points - center, axis=1).max()),
            "residues": [{key: rows[i][key] for key in ("chain", "resid", "resname", "sap")} for i in members],
            "definition": f"positive-SAP residues within {reach:g} A of the side-chain centroid of the highest-SAP residue"}


def write_sap_outputs(result, pdb_text, output, stem="sap"):
    """sap.json plus a PDB whose B-factor column carries per-atom SAP for visualization."""
    output = Path(output)
    cleaned = _clean_pdb(pdb_text).splitlines()
    values = result["atom_sap"]
    atoms = [line for line in cleaned if line.startswith("ATOM  ")]
    if len(atoms) != len(values):
        raise RuntimeError("SAP atom count differs from the cleaned structure")
    lines = ["REMARK 900 B-FACTOR COLUMN = SAP (SPATIAL AGGREGATION PROPENSITY), NOT A TEMPERATURE FACTOR"]
    lines += [line[:60].ljust(60) + f"{value:6.2f}" + line[66:] for line, value in zip(atoms, values)]
    (output / f"structure_{stem}.pdb").write_text("\n".join(lines + ["END"]) + "\n")
    stored = {key: value for key, value in result.items() if key != "atom_sap"}
    stored["structure_file"] = f"structure_{stem}.pdb"
    stored["input_sha256"] = hashlib.sha256(pdb_text.encode()).hexdigest()
    (output / f"{stem}.json").write_text(json.dumps(stored, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return stored
