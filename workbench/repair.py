"""Protein structure repair with PDBFixer before SAP, PROPKA and PDB2PQR.

PDBFixer (openmm/pdbfixer, MIT; REA-inventoried sdist in tools/rea/artifact_catalog.json) is used
for what experimental PDB files commonly lack: heavy atoms of incomplete side chains, internal gaps
listed in SEQRES, and modified residues with a standard parent.  Terminal SEQRES residues are not
built (they are usually disordered tails, and invented coordinates would dominate SAP and charge
patches), heterogens and water are removed, and no hydrogens are added (PDB2PQR protonates at the
chosen pH).  Every change is listed in the result.
"""
from __future__ import annotations

import io


def repair(pdb_text: str, seed: int = 1) -> tuple[str, dict]:
    try:
        from openmm.app import PDBFile
        from pdbfixer import PDBFixer
        import openmm
        import pdbfixer
    except ImportError as exc:
        raise ValueError("Protein repair needs PDBFixer and OpenMM (pip install pdbfixer openmm)") from exc
    fixer = PDBFixer(pdbfile=io.StringIO(pdb_text))
    chains = list(fixer.topology.chains())
    fixer.findMissingResidues()
    built, skipped = [], []
    for (chain_index, position), names in list(fixer.missingResidues.items()):
        chain = chains[chain_index]
        entry = {"chain": chain.id, "after_residue_index": position, "residues": list(names)}
        if position == 0 or position == len(list(chain.residues())):
            del fixer.missingResidues[(chain_index, position)]
            skipped.append(entry)
        else:
            built.append(entry)
    fixer.findNonstandardResidues()
    replaced = [{"chain": residue.chain.id, "residue": f"{residue.name}{residue.id}", "replaced_with": parent}
                for residue, parent in fixer.nonstandardResidues]
    fixer.replaceNonstandardResidues()
    heterogens = sorted({residue.name for residue in fixer.topology.residues()
                         if residue.name not in pdbfixer.pdbfixer.substitutions and residue.name not in _STANDARD})
    fixer.removeHeterogens(keepWater=False)
    fixer.findMissingAtoms()
    atoms = [{"chain": residue.chain.id, "residue": f"{residue.name}{residue.id}", "atoms": [a.name for a in missing]}
             for residue, missing in fixer.missingAtoms.items()]
    terminals = [{"chain": residue.chain.id, "residue": f"{residue.name}{residue.id}", "atoms": list(names)}
                 for residue, names in fixer.missingTerminals.items()]
    fixer.addMissingAtoms(seed=seed)
    stream = io.StringIO()
    PDBFile.writeFile(fixer.topology, fixer.positions, stream, keepIds=True)
    text = "\n".join(line for line in stream.getvalue().splitlines() if not line.startswith(("REMARK", "CRYST1")) or line.startswith("CRYST1")) + "\n"
    report = {"tool": f"PDBFixer {getattr(pdbfixer, '__version__', '1.12')} / OpenMM {openmm.__version__}",
              "missing_residues_built": built, "terminal_residues_not_built": skipped,
              "nonstandard_replaced": replaced, "heterogens_removed": heterogens,
              "missing_heavy_atoms_added": atoms, "terminal_atoms_added": terminals, "hydrogens_added": False,
              "changed": bool(built or replaced or heterogens or atoms or terminals)}
    return text, report


_STANDARD = {"ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE", "LEU", "LYS", "MET", "PHE", "PRO",
             "SER", "THR", "TRP", "TYR", "VAL", "HOH", "WAT"}
