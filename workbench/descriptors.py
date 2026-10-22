"""Topology-aware, unit-bearing research descriptors; absence is not a zero."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import warnings

import numpy as np


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _components(count, bonds):
    adjacency = [set() for _ in range(count)]
    for a, b in bonds:
        a, b = int(a), int(b)
        if a < 0 or b < 0 or a >= count or b >= count or a == b:
            raise ValueError("Invalid topology bond indices")
        adjacency[a].add(b)
        adjacency[b].add(a)
    groups, visited = [], set()
    for atom in range(count):
        if atom in visited:
            continue
        stack, group = [atom], []
        while stack:
            item = stack.pop()
            if item in visited:
                continue
            visited.add(item)
            group.append(item)
            stack.extend(adjacency[item] - visited)
        groups.append(sorted(group))
    return groups, adjacency


def _linear_path(group, adjacency, heavy=None):
    selected = set(group) if heavy is None else set(group).intersection(heavy)
    if len(selected) < 2:
        return None
    graph = {i: adjacency[i].intersection(selected) for i in selected}
    ends = sorted(i for i, neighbors in graph.items() if len(neighbors) == 1)
    if len(ends) != 2 or any(len(neighbors) not in (1, 2) for neighbors in graph.values()):
        return None
    path, current, previous = [], ends[0], None
    while current is not None:
        path.append(current)
        following = graph[current] - ({previous} if previous is not None else set())
        previous, current = current, next(iter(following), None)
        if len(path) > len(selected):
            return None
    return path if len(path) == len(selected) else None


def _frames_and_topology(directory, config, result, report):
    """Prefer the actual RadonPy dump over its two-frame UI preview."""
    data, dump = directory / "radon_md_lmp.data", directory / "radon_md.dump"
    if data.is_file() and dump.is_file():
        import MDAnalysis as mda
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            universe = mda.Universe(str(data), str(dump), topology_format="DATA", format="LAMMPSDUMP",
                                    atom_style="id resid type charge x y z", unwrap_images=True)
            coords = np.asarray([ts.positions.copy() for ts in universe.trajectory], dtype=float)
        bonds = universe.bonds.indices.tolist()
        masses = universe.atoms.masses.copy()
        charges = universe.atoms.charges.copy()
        # Masses are insufficient to determine chemistry; validate exact atom-order
        # correspondence to the original MOL before accepting its element labels.
        from rdkit import Chem
        mol = Chem.MolFromMolFile(str(directory / "structure.mol"), removeHs=False)
        heavy = None
        if mol is not None and mol.GetNumAtoms() == len(masses):
            mol_bonds = {tuple(sorted((b.GetBeginAtomIdx(), b.GetEndAtomIdx()))) for b in mol.GetBonds()}
            if mol_bonds == {tuple(sorted(b)) for b in bonds}:
                heavy = {a.GetIdx() for a in mol.GetAtoms() if a.GetAtomicNum() > 1}
        report["trajectory_source"] = "radon_md.dump + radon_md_lmp.data (MDAnalysis; image flags unwrapped)"
        report["source_files"] += ["radon_md.dump", "radon_md_lmp.data", "structure.mol"]
        return coords, bonds, masses, charges, heavy, "angstrom", False
    trajectory = _read(directory / "trajectory.json")
    if not trajectory.get("frames"):
        raise ValueError("No supported coordinate trajectory; a packed structure or topology alone is not dynamics")
    frames = np.asarray([f["positions"] for f in trajectory["frames"]], dtype=float)
    if frames.ndim != 3 or frames.shape[2] != 3 or not np.all(np.isfinite(frames)):
        raise ValueError("Trajectory coordinates must be finite frame × atom × 3 arrays")
    count = frames.shape[1]
    bonds = trajectory.get("bonds", [])
    unit = trajectory.get("unit")
    if unit not in {"sigma", "angstrom"}:
        raise ValueError("Trajectory coordinate unit is not explicitly supported")
    coarse = trajectory.get("kind") == "coarse_grained"
    if config.get("route") == "hoomd":
        raise ValueError("HOOMD preview contains wrapped coordinates without image flags; chain metrics would be ambiguous")
    if coarse:
        monomers, chains = trajectory.get("monomers"), trajectory.get("chains")
        if not isinstance(monomers, int) or not isinstance(chains, int) or monomers * chains != count:
            raise ValueError("Coarse-grained per-chain mapping is missing or inconsistent")
        expected = {tuple((c * monomers + i, c * monomers + i + 1)) for c in range(chains) for i in range(monomers - 1)}
        if {tuple(sorted(b)) for b in bonds} != expected:
            raise ValueError("Coarse-grained bonds disagree with the declared per-chain mapping")
        masses, heavy = np.ones(count), None
    else:
        from rdkit import Chem
        table = Chem.GetPeriodicTable()
        elements = trajectory.get("elements", [])
        if len(elements) != count:
            raise ValueError("Atomistic topology lacks an exact element/mass mapping")
        masses = np.array([table.GetAtomicWeight(e) for e in elements])
        heavy = {i for i, e in enumerate(elements) if e != "H"}
    report["trajectory_source"] = "trajectory.json (explicit bonds and chain/element mapping)"
    report["source_files"].append("trajectory.json")
    # Atomistic JSON is a prepared/final geometry preview, not a sampled MD trajectory.
    is_preview = not coarse
    return frames, bonds, masses, None, heavy, unit, is_preview


def _equilibration(series, unit):
    series = np.asarray(series, dtype=float)
    if len(series) < 20 or not np.all(np.isfinite(series)) or np.ptp(series) < 1e-12:
        return {"available": False, "reason": "At least 20 finite nonconstant samples are required; this is not an equilibration proof"}
    try:
        from pymbar.timeseries import detect_equilibration
        start, g, effective = detect_equilibration(series)
        return {"available": True, "candidate_start_index": int(start), "statistical_inefficiency": float(g),
                "effective_samples": float(effective), "post_candidate_mean": float(np.mean(series[int(start):])),
                "unit": unit, "equilibrated": False,
                "method": "pymbar.timeseries.detect_equilibration", "note": "Statistical stationarity candidate for this observable only; not physical equilibration"}
    except ImportError:
        return {"available": False, "reason": "Optional pymbar is unavailable"}


def extract_job_features(job_dir: Path, job=None) -> dict:
    directory = Path(job_dir)
    job = job or _read(directory / "job.json")
    config = job.get("config") or _read(directory / "config.json")
    result = _read(directory / "result.json")
    report = {"schema_version": 1, "values": {}, "unavailable": {}, "warnings": [], "source_files": [],
              "equilibrated": False, "method_version": "polymer-lab-descriptors/1"}
    missing = report["unavailable"]
    missing.update(density="No validated periodic bulk-cell mass/volume mapping; isolated chains and reduced units do not define material density",
                   dipole_fluctuation="Explicit per-atom charges and at least two actual MD frames are required",
                   protein_rmsd="Protein selection, aligned reference and a sampled protein trajectory are required",
                   contact_count="Explicit protein/polymer selections and a stated distance cutoff are required",
                   hbond_count="Donor/acceptor chemistry, hydrogens, distance and angle criteria are required",
                   sasa="Atomic radii, component selections and a validated SASA calculation are required",
                   rdf_first_peak="Periodic bulk coordinates, species selections, normalization and bin choices are required",
                   persistence_length="Adjacent tangent correlation is only a local stiffness proxy; no persistence length fit is performed")
    context = {"route": config.get("route"), "temperature_kelvin": config.get("temperature_kelvin"),
               "reduced_temperature": config.get("temperature") if config.get("route") in {"polymer_cg", "hoomd"} else None,
               "force_field": result.get("metadata", {}).get("force_field"),
               "ensemble": result.get("metadata", {}).get("ensemble"), "method_version": report["method_version"]}
    report["compatibility"] = context
    report["compatibility_key"] = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    def add(name, value, unit, method, **extra):
        if np.isfinite(value):
            report["values"][name] = {"value": float(value), "unit": unit, "method": method, **extra}
    try:
        frames, bonds, masses, charges, heavy, unit, preview = _frames_and_topology(directory, config, result, report)
        if frames.ndim != 3 or not np.all(np.isfinite(frames)):
            raise ValueError("Invalid coordinates")
        groups, adjacency = _components(frames.shape[1], bonds)
        # Every single disconnected atom is a genuine component, but is not a polymer chain.
        groups = [g for g in groups if len(g) > 1]
        if not groups:
            raise ValueError("No bonded chain components in topology")
        if np.any(masses <= 0) or not np.all(np.isfinite(masses)):
            raise ValueError("Invalid topology masses")
        rg, ends, correlations, torsions, dipoles = [], [], [], [], []
        paths = [_linear_path(g, adjacency, heavy) for g in groups]
        for group, path in zip(groups, paths):
            xyz, weights = frames[:, group, :], masses[group]
            center = np.average(xyz, axis=1, weights=weights)
            centered = xyz - center[:, None, :]
            rg.append(np.sqrt(np.sum(np.sum(centered ** 2, axis=2) * weights[None, :], axis=1) / weights.sum()))
            if charges is not None and abs(float(np.sum(charges[group]))) < 1e-4:
                dipoles.append(np.sum(centered * charges[group][None, :, None], axis=1))
            if path is None:
                continue
            chain = frames[:, path, :]
            ends.append(np.linalg.norm(chain[:, -1] - chain[:, 0], axis=1))
            tangent = np.diff(chain, axis=1)
            norms = np.linalg.norm(tangent, axis=2)
            if np.any(norms < 1e-10):
                continue
            tangent = tangent / norms[:, :, None]
            if len(path) >= 3:
                correlations.append(np.sum(tangent[:, :-1] * tangent[:, 1:], axis=2).mean(axis=1))
            if len(path) >= 4:
                from MDAnalysis.lib.distances import calc_dihedrals
                for index in range(len(path) - 3):
                    angles = calc_dihedrals(chain[:, index], chain[:, index + 1], chain[:, index + 2], chain[:, index + 3])
                    finite_angles = angles[np.isfinite(angles)]
                    # Collinear bonds have undefined torsion, not zero entropy.
                    # At least two actual observations are needed for a sampled distribution.
                    if len(finite_angles) >= 2:
                        torsions.append(finite_angles)
        rg = np.asarray(rg).T
        report.update(frame_count=int(frames.shape[0]), chain_count=len(groups), length_unit=unit, actual_dynamics=not preview and frames.shape[0] >= 2)
        report["series"] = {"rg_mean_per_frame": rg.mean(axis=1).tolist()}
        add("rg_mean", rg.mean(), unit, "mass-weighted radius of gyration; mean across validated chains and stored frames")
        add("rg_final", rg[-1].mean(), unit, "mass-weighted per-chain radius of gyration in final stored frame")
        dynamics = frames.shape[0] >= 2 and not preview
        if dynamics:
            add("rg_std", rg.std(axis=0, ddof=1).mean(), unit, "mean of within-chain sample standard deviations")
            report["equilibration"] = {"rg": _equilibration(rg.mean(axis=1), unit)}
        else:
            missing["rg_std"] = "Fewer than two sampled MD frames; prepared/final geometry previews are not dynamics"
        if ends and len(ends) == len(groups):
            ends = np.asarray(ends).T
            add("end_to_end_mean", ends.mean(), unit, "distance between topology-verified linear backbone endpoints")
            if dynamics:
                add("end_to_end_std", ends.std(axis=0, ddof=1).mean(), unit, "mean of within-chain endpoint-distance sample standard deviations")
        else:
            missing["end_to_end_mean"] = "Branched/ring backbone or incomplete heavy-atom mapping; endpoint assignment would be ambiguous"
        if correlations and len(correlations) == len(groups):
            add("adjacent_tangent_correlation", np.mean(correlations), "dimensionless", "mean cos(angle) between adjacent topology-verified backbone bond vectors; not persistence length")
        else:
            missing["adjacent_tangent_correlation"] = "A validated simple backbone path with at least three sites is required"
        if dynamics and torsions:
            entropies = []
            for angles in torsions:
                counts = np.histogram(angles, bins=18, range=(-np.pi, np.pi))[0]
                probability = counts[counts > 0] / counts.sum()
                entropies.append(-np.sum(probability * np.log(probability)))
            add("backbone_dihedral_entropy", np.mean(entropies), "nats", "mean per-defined-torsion discrete entropy with 18 fixed [-pi,pi] bins; finite-sampling biased, not thermodynamic entropy", defined_torsions=len(entropies))
        else:
            missing["backbone_dihedral_entropy"] = "Sampled MD, a verified simple backbone with at least four sites, and at least two finite observations per torsion are required; collinear bonds have undefined torsion"
        if dynamics and dipoles and len(dipoles) == len(groups):
            magnitudes = np.linalg.norm(np.asarray(dipoles), axis=2)
            add("dipole_fluctuation", magnitudes.std(axis=1, ddof=1).mean(), "e*angstrom", "mean within-chain sample std of dipole magnitude; explicitly charged neutral chains")
            missing.pop("dipole_fluctuation", None)
        report["warnings"].append("Short and correlated trajectories do not establish convergence or validate Tg, modulus, or binding affinity predictions.")
        if preview:
            report["warnings"].append("Only a prepared/final geometry preview is available; dynamic descriptors are unavailable.")
    except (ValueError, OSError, KeyError, IndexError, TypeError, ImportError) as exc:
        missing["trajectory_features"] = str(exc)
    metrics = _read(directory / "metrics.json")
    energy = [row.get("potential_energy") for row in metrics.get("series", [])]
    if energy and all(isinstance(x, (int, float)) and np.isfinite(x) for x in energy):
        energy_unit = metrics.get("units", {}).get("potential_energy")
        if energy_unit:
            add("potential_energy_mean", np.mean(energy), energy_unit, "mean over saved samples; not equilibrium free energy")
            report.setdefault("equilibration", {})["potential_energy"] = _equilibration(energy, energy_unit)
            report["source_files"].append("metrics.json")
    report["source_sha256"] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                                for name in report["source_files"] if (directory / name).is_file()}
    return report
