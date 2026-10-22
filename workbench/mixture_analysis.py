"""Protein-polymer observables from an executed Martini 3 GROMACS pilot.

Every value states its selection, reference, cutoff and unit. Contact counts depend
on the chosen cutoff; RMSD/Rg use unweighted bead positions. None of these are
binding free energies, and stationarity candidates are not proof of equilibrium.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import warnings

import numpy as np

ENERGY_UNITS = {"Potential": "kJ/mol", "Temperature": "K", "Pressure": "bar", "Density": "kg/m^3"}


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _gmx(argv, output, env, stdin, logname):
    with (Path(output) / logname).open("w") as log:
        result = subprocess.run(argv, cwd=output, input=stdin, text=True, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=1800)
    if result.returncode:
        raise RuntimeError(f"{argv[1]} failed (exit {result.returncode}); inspect {logname}")


def _xvg(path):
    legends, rows = [], []
    for line in Path(path).read_text().splitlines():
        if line.startswith("@") and " legend " in line and line.split()[1].startswith("s"):
            legends.append(line.split('"')[1])
        elif line and not line.startswith(("#", "@")):
            rows.append([float(value) for value in line.split()])
    data = np.asarray(rows)
    if data.ndim != 2 or data.shape[1] != len(legends) + 1:
        raise ValueError(f"Unexpected gmx energy output in {Path(path).name}")
    return data[:, 0], {name: data[:, i + 1] for i, name in enumerate(legends)}


def stage_energies(gmx, output, env, stem, terms):
    """Average GROMACS energy terms for one stage; a failed extraction is recorded, not fatal."""
    xvg = f"{stem}_energy.xvg"
    try:
        _gmx([gmx, "energy", "-f", f"{stem}.edr", "-o", xvg], output, env, "\n".join(terms) + "\n\n", f"gmx_{stem}_energy.log")
        time, series = _xvg(Path(output) / xvg)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "samples": int(len(time)), "time_ps": [float(time[0]), float(time[-1])],
            "terms": {name: {"mean": float(values.mean()), "std": float(values.std(ddof=1)) if len(values) > 1 else None,
                             "unit": ENERGY_UNITS.get(name), "series": values.tolist()} for name, values in series.items()}}


def _rg(positions):
    return float(np.sqrt(np.sum((positions - positions.mean(axis=0)) ** 2, axis=1).mean()))


def analyze_martini_mixture(output, gmx, env, layout, cutoff_nm):
    import MDAnalysis as mda
    from MDAnalysis.analysis import rms
    from MDAnalysis.lib.distances import capped_distance, distance_array
    from workbench.descriptors import _equilibration
    output = Path(output)
    # Molecules are made whole first so per-chain Rg never spans a periodic boundary.
    _gmx([gmx, "trjconv", "-s", "04_production.tpr", "-f", "04_production.xtc", "-o", "production_whole.xtc", "-pbc", "mol"],
         output, env, "0\n", "gmx_trjconv.log")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        universe = mda.Universe(str(output / "04_production.gro"), str(output / "production_whole.xtc"))
        reference = mda.Universe(str(output / "01_minimize.gro"))
    if len(universe.atoms) != layout["particles"] or len(reference.atoms) != layout["particles"]:
        raise RuntimeError("Trajectory particle count disagrees with the assembled topology")
    start, stop = layout["protein"]
    protein, reference_protein = universe.atoms[start:stop], reference.atoms[start:stop]
    backbone, reference_backbone = protein.select_atoms("name BB"), reference_protein.select_atoms("name BB")
    if len(backbone) < 3 or len(backbone) != len(reference_backbone):
        backbone, reference_backbone = protein, reference_protein
    chains = [universe.atoms[a:b] for a, b in layout["polymer_chains"]]
    polymer = universe.atoms[layout["polymer_chains"][0][0]:layout["polymer_chains"][-1][1]]
    chain_of = np.concatenate([np.full(len(chain), i) for i, chain in enumerate(chains)])
    cutoff = 10 * float(cutoff_nm)
    series = []
    for ts in universe.trajectory:
        pairs = capped_distance(protein.positions, polymer.positions, max_cutoff=cutoff, box=ts.dimensions, return_distances=False)
        touching = np.unique(pairs[:, 1]) if len(pairs) else np.array([], dtype=int)
        series.append({"time_ps": float(ts.time),
                       "protein_rmsd": float(rms.rmsd(backbone.positions, reference_backbone.positions, center=True, superposition=True)),
                       "protein_rg": _rg(protein.positions),
                       "polymer_rg_mean": float(np.mean([_rg(chain.positions) for chain in chains])),
                       "contact_count": int(len(touching)),
                       "contacting_chains": int(len(np.unique(chain_of[touching]))),
                       "min_distance": float(distance_array(protein.positions, polymer.positions, box=ts.dimensions).min())})
    if len(series) < 2:
        raise RuntimeError("Production trajectory has fewer than two frames")
    # Iteration rewinds the reader; select the final production frame explicitly.
    universe.trajectory[-1]
    final = protein + polymer
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        final.write(str(output / "structure.pdb"))
    text = (output / "structure.pdb").read_text()
    remark = f"REMARK 900 MARTINI 3 COARSE-GRAINED BEADS (PROTEIN + POLYMER); WATER AND IONS OMITTED; ANGSTROM\nREMARK 900 FINAL PRODUCTION FRAME AT {universe.trajectory.ts.time:.1f} PS\n"
    (output / "structure.pdb").write_text(remark + text)
    universe.trajectory.close()
    # MDAnalysis offset caches are reader internals, not research artifacts.
    for cache in output.glob(".*.xtc_offsets.*"):
        cache.unlink(missing_ok=True)
    production = stage_energies(gmx, output, env, "04_production", ["Potential", "Temperature", "Pressure", "Density"])
    npt = stage_energies(gmx, output, env, "03_npt", ["Potential", "Temperature", "Pressure", "Density"])
    columns = {key: np.asarray([row[key] for row in series], dtype=float) for key in ("protein_rmsd", "protein_rg", "polymer_rg_mean", "contact_count", "min_distance")}
    equilibration = {"contact_count": _equilibration(columns["contact_count"], "count"),
                     "protein_rmsd": _equilibration(columns["protein_rmsd"], "angstrom")}
    if production.get("available"):
        for term, unit in (("Potential", "kJ/mol"), ("Density", "kg/m^3")):
            if term in production["terms"]:
                equilibration[term.lower()] = _equilibration(production["terms"][term]["series"], unit)
    summary = {"frames": len(series), "production_ps": series[-1]["time_ps"] - series[0]["time_ps"],
               "protein_rmsd_mean": float(columns["protein_rmsd"].mean()), "protein_rg_mean": float(columns["protein_rg"].mean()),
               "polymer_rg_mean": float(columns["polymer_rg_mean"].mean()), "contact_count_mean": float(columns["contact_count"].mean()),
               "min_distance_mean": float(columns["min_distance"].mean()),
               "contact_count_final": int(series[-1]["contact_count"]), "protein_rmsd_final": float(series[-1]["protein_rmsd"]),
               "contact_fraction_of_frames": float(np.mean([row["contact_count"] > 0 for row in series]))}
    if production.get("available") and "Density" in production["terms"]:
        summary["density_mean"] = production["terms"]["Density"]["mean"]
    analysis = {"schema_version": 1, "method": "MDAnalysis on gmx trjconv -pbc mol production frames",
                "units": {"time_ps": "ps", "protein_rmsd": "angstrom", "protein_rg": "angstrom", "polymer_rg_mean": "angstrom",
                          "contact_count": "count", "contacting_chains": "count", "min_distance": "angstrom", "density_mean": "kg/m^3"},
                "definitions": {"protein_rmsd": "BB beads after optimal superposition to the energy-minimized structure (all protein beads if fewer than three BB beads)",
                                "protein_rg": "unweighted protein bead radius of gyration",
                                "polymer_rg_mean": "unweighted per-chain radius of gyration, mean over chains",
                                "contact_count": f"polymer beads within {cutoff_nm:g} nm of any protein bead (periodic minimum image)",
                                "contacting_chains": "polymer chains with at least one contacting bead",
                                "min_distance": "closest protein-polymer bead distance (periodic minimum image)"},
                "contact_cutoff_nm": float(cutoff_nm), "reference": "01_minimize.gro", "summary": summary, "series": series,
                "energies": {"03_npt": npt, "04_production": production}, "equilibration": equilibration, "equilibrated": False,
                "hbond_count": "Unavailable: Martini beads have no explicit hydrogens or donor/acceptor geometry",
                "warnings": ["A short pilot does not establish convergence, binding affinity or adsorption free energy.",
                             "Contact counts depend on the stated cutoff and on Martini 3 bead resolution.",
                             "Martini time is not physical time; compare runs only under identical settings."]}
    _write(output / "mixture_analysis.json", analysis)
    return analysis
