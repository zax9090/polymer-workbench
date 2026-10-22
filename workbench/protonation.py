"""Protein titration at a chosen pH and the surface charge it leaves exposed.

PROPKA 3.5.1 (Olsson et al., JCTC 7, 525 (2011); Sondergaard et al., JCTC 7, 2284 (2011))
predicts pKa values of ionizable groups in one structure; group charges at the pH follow its
Henderson-Hasselbalch form. The surface potential at exposed heavy atoms is a Debye-Hueckel
screened Coulomb sum of those group charges in water (relative permittivity 78.4): a
screening estimate that ignores the low-dielectric protein interior, not a Poisson-Boltzmann
solution. PDB2PQR 3.7.1 writes the matching protonated all-atom structure (hydrogens, AMBER
residue names such as ASH/GLH/HID/HIE/HIP/LYN) for martinize2 -from amber, tleap or APBS.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import math
from pathlib import Path
import tempfile

import numpy as np

METHOD_VERSION = "polymer-lab-protonation/1"
PERMITTIVITY = 78.4
COULOMB_EV_ANGSTROM = 14.399645  # e^2 / (4 pi eps0) in eV * angstrom
BOLTZMANN_EV = 8.617333262e-5
AVOGADRO = 6.02214076e23
MIN_DISTANCE = 3.0  # angstrom; caps the point-charge singularity at the charge centre
EXPOSED_SASA = 1.0  # angstrom^2 of heavy-atom SASA for a surface point
PATCH_REACH = 10.0
AMBIGUOUS_WINDOW = 1.0
PROTONATED_NAMES = {"ASH": "ASP neutral", "GLH": "GLU neutral", "HIP": "HIS charged (both N protonated)", "HID": "HIS neutral (ND1-H)",
                    "HIE": "HIS neutral (NE2-H)", "LYN": "LYS neutral", "CYM": "CYS deprotonated", "CYX": "CYS disulfide", "TYM": "TYR deprotonated"}
CITATION = "Olsson, Sondergaard, Rostkowski & Jensen, J. Chem. Theory Comput. 7, 525 (2011); Sondergaard et al., J. Chem. Theory Comput. 7, 2284 (2011)"


def first_model(pdb_text):
    """ATOM/HETATM records of the first model; ligands stay because they shift pKa values."""
    if not isinstance(pdb_text, str) or not pdb_text.strip():
        raise ValueError("단백질 PDB 텍스트가 필요합니다.")
    kept = []
    for line in pdb_text.splitlines():
        if line.startswith("ENDMDL"):
            break
        if line.startswith(("ATOM  ", "HETATM", "TER")):
            kept.append(line)
    if not any(line.startswith("ATOM  ") for line in kept):
        raise ValueError("PROPKA에는 단백질 ATOM 레코드가 필요합니다.")
    return "\n".join(kept + ["END"]) + "\n"


def bjerrum_length(temperature=298.15):
    return COULOMB_EV_ANGSTROM / (PERMITTIVITY * BOLTZMANN_EV * temperature)


def debye_length(ionic_strength, temperature=298.15):
    """Debye length in angstrom; infinite (unscreened) at zero ionic strength."""
    if ionic_strength <= 0:
        return math.inf
    kappa_sq = 8 * math.pi * bjerrum_length(temperature) * ionic_strength * AVOGADRO * 1e-27
    return 1 / math.sqrt(kappa_sq)


@contextlib.contextmanager
def _quiet(name):
    logger = logging.getLogger(name)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    previous, propagate = logger.level, logger.propagate
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
        logger.propagate = propagate


def _check(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}은(는) {low:g}~{high:g} 범위의 수치여야 합니다.")
    return float(value)


def titrate(pdb_text, ph=7.0):
    """PROPKA pKa values, charges at pH, folded charge curve and isoelectric point."""
    import propka
    import propka.run
    ph = _check(ph, "pH", 0, 14)
    text = first_model(pdb_text)
    with _quiet("propka") as records:
        molecule = propka.run.single("input.pdb", optargs=["--quiet"], stream=io.StringIO(text), write_pka=False)
    average = molecule.conformations["AVR"]
    first = molecule.conformations[next(name for name in molecule.conformations if name != "AVR")]
    located = {group.label: group for group in first.groups}
    parameters = molecule.version.parameters
    groups = []
    # PROPKA's summary order (residue type, then sequence) keeps reports comparable with .pka files.
    ordered = [g for kind in parameters.write_out_order for g in average.groups if g.residue_type == kind]
    for group in ordered:
        if not group.use_in_calculations():
            continue
        site = located.get(group.label, group)
        titratable = group.pka_value < 99 and group.charge != 0
        charge = float(group.calculate_charge(parameters, ph=ph)) if titratable else 0.0
        groups.append({"label": group.label.strip(), "type": group.residue_type.strip(), "chain": group.atom.chain_id.strip(),
                       "resid": int(group.atom.res_num), "pka": round(float(group.pka_value), 2), "model_pka": float(group.model_pka),
                       "shift": round(float(group.pka_value - group.model_pka), 2), "charge_sign": int(group.charge),
                       "charge_at_ph": round(charge, 4), "buried_fraction": round(float(site.buried), 3),
                       "center_angstrom": [round(float(site.x), 3), round(float(site.y), 3), round(float(site.z), 3)],
                       "titratable": titratable, "state": None if not titratable else ("protonated" if (charge > 0.5 if group.charge > 0 else charge > -0.5) else "deprotonated"),
                       "ambiguous": titratable and abs(group.pka_value - ph) < AMBIGUOUS_WINDOW,
                       "coupled": group.coupled_titrating_group is not None,
                       # PROPKA's own summary discards pKa values of groups coupled to another titrating group.
                       "reported": not (group.coupled_titrating_group is not None and parameters.remove_penalised_group)})
    unfolded, folded = average.calculate_charge(parameters, ph=ph)
    pi_folded, pi_unfolded = molecule.get_pi()
    curve = [[round(p, 2), round(f, 3)] for p, _, f in molecule.get_charge_profile(grid=[0.0, 14.0, 0.5])]
    return {"method": "PROPKA3 empirical pKa prediction on one structure (first model, ligands kept)", "method_version": METHOD_VERSION,
            "propka_version": propka.__version__, "citation": CITATION, "ph": ph, "net_charge": round(float(folded), 3),
            "net_charge_unfolded": round(float(unfolded), 3), "isoelectric_point": round(float(pi_folded), 2),
            "isoelectric_point_unfolded": round(float(pi_unfolded), 2), "charge_curve": curve, "groups": groups,
            "summary": {"groups": len(groups), "coupled_discarded": sum(not g["reported"] for g in groups),
                        "ambiguous": sum(g["ambiguous"] for g in groups),
                        "shifted": sum(g["titratable"] and abs(g["shift"]) >= 1.5 for g in groups),
                        "positive": round(sum(g["charge_at_ph"] for g in groups if g["charge_at_ph"] > 0), 3),
                        "negative": round(sum(g["charge_at_ph"] for g in groups if g["charge_at_ph"] < 0), 3)},
            "propka_messages": sorted({record.getMessage()[:300] for record in records})[:20]}


def surface_potential(pdb_text, titration, ionic_strength=0.15, temperature=298.15):
    """Screened Coulomb potential (kT/e) at exposed heavy atoms, and the strongest +/- surface patches."""
    from scipy.spatial import cKDTree
    from .sap import _clean_pdb, _sasa
    ionic_strength = _check(ionic_strength, "이온 세기", 0, 5)
    trajectory, sasa = _sasa(_clean_pdb(pdb_text))
    atoms = list(trajectory.topology.atoms)
    coords = trajectory.xyz[0] * 10.0
    charged = [g for g in titration["groups"] if abs(g["charge_at_ph"]) > 1e-3]
    lb, debye = bjerrum_length(temperature), debye_length(ionic_strength, temperature)
    potential = np.zeros(len(atoms))
    if charged:
        centers = np.array([g["center_angstrom"] for g in charged])
        charges = np.array([g["charge_at_ph"] for g in charged])
        distance = np.maximum(np.linalg.norm(coords[:, None, :] - centers[None, :, :], axis=2), MIN_DISTANCE)
        potential = lb * np.sum(charges[None, :] * np.exp(-distance / debye) / distance, axis=1)
    exposed = sasa >= EXPOSED_SASA
    residues = {}
    for atom, phi, open_ in zip(atoms, potential, exposed):
        if open_:
            key = (atom.residue.chain.chain_id or "", int(atom.residue.resSeq), atom.residue.name)
            residues.setdefault(key, []).append(float(phi))
    rows = [{"chain": c, "resid": r, "resname": n, "potential": float(np.mean(v)), "exposed_atoms": len(v)} for (c, r, n), v in residues.items()]
    tree = cKDTree(coords)
    patches = {}
    for sign, name in ((1, "positive"), (-1, "negative")):
        candidates = np.flatnonzero(exposed & (sign * potential > 0))
        if not len(candidates):
            patches[name] = None
            continue
        anchor = candidates[np.argmax(sign * potential[candidates])]
        peak = float(potential[anchor])
        if abs(peak) < 0.5:
            patches[name] = None
            continue
        near = np.array(tree.query_ball_point(coords[anchor], PATCH_REACH))
        members = near[exposed[near] & (sign * potential[near] >= 0.5 * abs(peak))]
        center = coords[members].mean(axis=0)
        keys = sorted({(atoms[i].residue.chain.chain_id or "", int(atoms[i].residue.resSeq), atoms[i].residue.name) for i in members})
        by_key = {(r["chain"], r["resid"], r["resname"]): r for r in rows}
        radius = float(np.linalg.norm(coords[members] - center, axis=1).max())
        groups = [{k: g[k] for k in ("type", "chain", "resid", "pka", "charge_at_ph")} for g in charged
                  if np.sign(g["charge_at_ph"]) == sign and np.linalg.norm(np.array(g["center_angstrom"]) - center) <= radius + 4.0]
        patches[name] = {"center_angstrom": center.round(3).tolist(), "radius_angstrom": radius, "peak_potential": peak,
                         "mean_potential": float(potential[members].mean()), "atoms": int(len(members)),
                         "residues": sorted((by_key[k] for k in keys), key=lambda r: -sign * r["potential"]), "charged_groups": groups,
                         "definition": f"exposed heavy atoms within {PATCH_REACH:g} A of the extreme-potential surface atom whose potential has the same sign and at least half its magnitude"}
    return {"method": "Debye-Hueckel screened Coulomb sum of PROPKA group charges at exposed heavy atoms (no protein dielectric boundary)",
            "units": "kT/e", "temperature_kelvin": temperature, "ionic_strength_molar": ionic_strength, "relative_permittivity": PERMITTIVITY,
            "bjerrum_length_angstrom": round(lb, 3), "debye_length_angstrom": None if math.isinf(debye) else round(debye, 3),
            "exposed_sasa_threshold_angstrom2": EXPOSED_SASA, "residues": rows, "patches": patches, "atom_potential": potential.round(4).tolist()}


def analyze(pdb_text, ph=7.0, ionic_strength=0.15, temperature=298.15):
    titration = titrate(pdb_text, ph)
    surface = surface_potential(pdb_text, titration, ionic_strength, temperature)
    warnings = ["PROPKA pKa values come from one static structure; buried or coupled groups and flexible loops carry the largest errors.",
                "The surface potential is a screened Coulomb estimate with a uniform water dielectric; use the PQR with APBS for a Poisson-Boltzmann map.",
                "Patch placement is a starting-configuration choice; it is not evidence that a polymer binds there."]
    if titration["summary"]["ambiguous"]:
        warnings.insert(0, f"{titration['summary']['ambiguous']}개 기의 pKa가 pH ±1 안에 있어 양성자화 상태가 혼재합니다. 해당 잔기를 직접 검토하세요.")
    return {**titration, "surface": surface, "warnings": warnings}


def write_outputs(result, pdb_text, output, stem="protonation"):
    """protonation.json plus a PDB whose B-factor column carries the surface potential (kT/e)."""
    from .sap import _clean_pdb
    output = Path(output)
    atoms = [line for line in _clean_pdb(pdb_text).splitlines() if line.startswith("ATOM  ")]
    values = result["surface"]["atom_potential"]
    if len(atoms) != len(values):
        raise RuntimeError("Potential atom count differs from the cleaned structure")
    lines = ["REMARK 900 B-FACTOR COLUMN = SCREENED COULOMB POTENTIAL IN KT/E AT PH %.2f, NOT A TEMPERATURE FACTOR" % result["ph"]]
    lines += [line[:60].ljust(60) + f"{max(min(v, 99.99), -9.99):6.2f}" + line[66:] for line, v in zip(atoms, values)]
    (output / "structure_potential.pdb").write_text("\n".join(lines + ["END"]) + "\n")
    stored = {**result, "surface": {k: v for k, v in result["surface"].items() if k != "atom_potential"},
              "structure_file": "structure_potential.pdb", "input_sha256": hashlib.sha256(pdb_text.encode()).hexdigest()}
    (output / f"{stem}.json").write_text(json.dumps(stored, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return stored


def protonate(pdb_text, ph, output, prefix="protein_protonated"):
    """PDB2PQR + PROPKA protonated structure at pH with AMBER names (protein ATOM records only)."""
    from pdb2pqr.main import run_pdb2pqr
    ph = _check(ph, "pH", 0, 14)
    output = Path(output)
    text = first_model(pdb_text)
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "input.pdb"
        source.write_text(text)
        if not source.is_file():  # PDB2PQR treats a missing path as an RCSB ID to download
            raise RuntimeError("PDB2PQR input file was not written")
        pqr, pdb = Path(folder) / "out.pqr", Path(folder) / "out.pdb"
        with _quiet("PDB2PQR3.7.1") as records, _quiet("pdb2pqr"), _quiet("propka"):
            missed, _, _ = run_pdb2pqr(["--ff=AMBER", "--ffout=AMBER", "--titration-state-method=propka", f"--with-ph={ph}",
                                        "--keep-chain", "--drop-water", "--pdb-output", str(pdb), str(source), str(pqr)])
        pqr_lines = [line for line in pqr.read_text().splitlines() if line.startswith("ATOM")]
        protein = [line for line in pdb.read_text().splitlines() if line.startswith("ATOM  ")]
    if not protein:
        raise RuntimeError("PDB2PQR produced no protein atoms")
    (output / f"{prefix}.pdb").write_text("\n".join(protein + ["END"]) + "\n")
    (output / f"{prefix}.pqr").write_text("\n".join(pqr_lines + ["END"]) + "\n")
    charge = sum(float(line[54:62]) for line in pqr_lines)
    residues = {}
    for line in protein:
        residues.setdefault((line[21], line[22:27], line[17:20].strip()), None)
    names = [name for (_, _, name) in residues]
    states = {name: names.count(name) for name in PROTONATED_NAMES if name in names}
    skipped = sorted({f"{atom.res_name} {atom.chain_id}{atom.res_seq}" for atom in missed or []})
    return {"files": [f"{prefix}.pdb", f"{prefix}.pqr"], "ph": ph, "force_field": "AMBER (PDB2PQR charges and radii)", "residue_naming": "AMBER",
            "integer_charge": int(round(charge)), "pqr_charge": round(charge, 4), "residue_states": states,
            "state_meaning": {k: PROTONATED_NAMES[k] for k in states}, "omitted_residues": skipped[:50], "omitted_count": len(skipped),
            "messages": sorted({r.getMessage()[:300] for r in records})[:20],
            "note": "Hetero groups without PDB2PQR parameters (ligands, ions) are omitted from the protonated structure."}
