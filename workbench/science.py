"""Small, reproducible scientific kernels for the local workflow.

The default model is an uncalibrated soft, penetrable bead-spring chain.
It deliberately does not attach chemical material properties to reduced units.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import time
from typing import Callable

import numpy as np


DEFAULTS = dict(route="polymer_cg", monomers=24, chains=2, steps=1000,
                temperature=1.0, timestep=0.002, seed=42, engine="auto",
                repeat_smiles="[*]CC[*]", sequence="", protein_mode="export")
ROUTES = ("polymer_cg", "polymer_atomistic", "protein", "polyply", "hoomd", "pysimm", "packmol", "mixture")
MODEL = {"name": "soft repulsive bead-spring", "bond": "U=15*(r-1)^2",
         "pair": "U=5*(1+cos(pi*r/1.2)) for r<1.2, otherwise 0",
         "exclusions": "Directly bonded pairs only", "boundary": "nonperiodic, dilute chains",
         "units": {"length": "sigma", "energy": "epsilon", "temperature": "k_B T/epsilon",
                   "time": "tau", "mass": "m=1"},
         "calibrated": False, "equilibrated": False}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def get_capabilities():
    from workbench.external import get_capabilities as probe
    return probe()


def validate_config(raw: dict) -> dict:
    config = {**DEFAULTS, **raw}
    if config["route"] in ("polymer_atomistic", "pysimm") and raw.get("atomistic_mode") != "bulk":
        config["chains"] = raw.get("chains", 1)
        if config["chains"] != 1:
            raise ValueError("The atomistic and pysimm routes currently support one isolated chain; set chains=1")
    if config["route"] not in ROUTES:
        raise ValueError(f"Unknown route: {config['route']}")
    for key, low, high in (("monomers", 2, 200), ("chains", 1, 16), ("steps", 10, 100000), ("seed", 1, 899999998)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be an integer in [{low}, {high}]")
    for key, low, high in (("temperature", 0.05, 5), ("timestep", 0.0001, 0.005)):
        value = float(config[key])
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be in [{low}, {high}]")
        config[key] = value
    if config["monomers"] * config["chains"] > 1024:
        raise ValueError("Interactive jobs are limited to 1024 beads")
    if config["engine"] not in ("auto", "lammps", "numpy"):
        raise ValueError("engine must be auto, lammps, or numpy")
    if len(str(config.get("repeat_smiles", ""))) > 2000:
        raise ValueError("Repeat SMILES is too long")
    if config["protein_mode"] not in ("export", "predict", "database", "import"):
        raise ValueError("protein_mode must be export, predict, database, or import")
    if "temperature_kelvin" in config:
        kelvin = float(config["temperature_kelvin"])
        if not math.isfinite(kelvin) or not 1 <= kelvin <= 2000:
            raise ValueError("temperature_kelvin must be in [1, 2000]")
        config["temperature_kelvin"] = kelvin
    return config


def initial_chains(monomers: int, chains: int, seed: int):
    rng = np.random.default_rng(seed)
    coords = np.zeros((chains, monomers, 3), dtype=float)
    coords[:, :, 0] = np.arange(monomers) - (monomers - 1) / 2
    coords[:, :, 1] = np.arange(chains)[:, None] * 4.0 - (chains - 1) * 2.0
    coords += rng.normal(scale=0.025, size=coords.shape)
    bonds = [[c * monomers + i, c * monomers + i + 1]
             for c in range(chains) for i in range(monomers - 1)]
    return coords.reshape(-1, 3), np.asarray(bonds, dtype=int)


def soft_forces(positions, bonds):
    """Force and total potential energy, exactly matching LAMMPS soft/harmonic."""
    delta = positions[:, None, :] - positions[None, :, :]
    distance = np.linalg.norm(delta, axis=2)
    active = distance < 1.2
    np.fill_diagonal(active, False)
    active[bonds[:, 0], bonds[:, 1]] = False
    active[bonds[:, 1], bonds[:, 0]] = False
    scale = np.zeros_like(distance)
    nonzero = active & (distance > 1e-12)
    scale[nonzero] = 5 * np.pi / 1.2 * np.sin(np.pi * distance[nonzero] / 1.2) / distance[nonzero]
    force = np.einsum("ij,ijk->ik", scale, delta)
    potential = 0.5 * np.sum(5 * (1 + np.cos(np.pi * distance[active] / 1.2)))
    separation = positions[bonds[:, 0]] - positions[bonds[:, 1]]
    lengths = np.linalg.norm(separation, axis=1)
    if np.any(lengths < 1e-10):
        raise ValueError("Degenerate zero-length bond")
    bond_force = -30 * (lengths - 1)[:, None] * separation / lengths[:, None]
    np.add.at(force, bonds[:, 0], bond_force)
    np.add.at(force, bonds[:, 1], -bond_force)
    potential += 15 * np.sum((lengths - 1) ** 2)
    return force, float(potential)


def chain_metrics(positions, monomers, chains):
    chain_coords = positions.reshape(chains, monomers, 3)
    centred = chain_coords - chain_coords.mean(axis=1, keepdims=True)
    rg = np.sqrt(np.sum(centred ** 2, axis=2).mean(axis=1))
    ends = np.linalg.norm(chain_coords[:, -1] - chain_coords[:, 0], axis=1)
    return {"rg": float(np.mean(rg)), "end_to_end": float(np.mean(ends)), "rg_per_chain": rg.tolist()}


def _sample(positions, step, config, energy, temperature=None):
    return {"step": int(step), "time": step * config["timestep"],
            "potential_energy": float(energy), "temperature": temperature,
            **chain_metrics(positions, config["monomers"], config["chains"])}


def simulate_numpy(config, output: Path, emit):
    positions, bonds = initial_chains(config["monomers"], config["chains"], config["seed"])
    if len(positions) > 256 or config["steps"] > 20000:
        raise ValueError("NumPy fallback supports at most 256 beads and 20000 steps; use LAMMPS for larger jobs")
    rng = np.random.default_rng(config["seed"] + 1)
    frames, samples = [], []
    stride = max(1, math.ceil(config["steps"] / 100))
    for step in range(config["steps"] + 1):
        force, energy = soft_forces(positions, bonds)
        if step % stride == 0 or step == config["steps"]:
            frames.append({"step": step, "positions": positions.round(6).tolist()})
            samples.append(_sample(positions, step, config, energy))
            emit("simulate", step / config["steps"], f"NumPy Brownian dynamics: {step}/{config['steps']}")
        if step < config["steps"]:
            positions += config["timestep"] * force + math.sqrt(2 * config["temperature"] * config["timestep"]) * rng.normal(size=positions.shape)
        if not np.isfinite(positions).all():
            raise RuntimeError("Simulation diverged; decrease timestep")
    return frames, samples, bonds, "numpy-overdamped-langevin"


def write_lammps_data(path, positions, bonds, monomers):
    lower = positions.min(axis=0) - 100
    upper = positions.max(axis=0) + 100
    lines = ["Soft bead-spring polymer; reduced LJ units", "", f"{len(positions)} atoms", f"{len(bonds)} bonds", "", "1 atom types", "1 bond types", ""]
    for i, dim in enumerate("xyz"):
        lines.append(f"{lower[i]:.10f} {upper[i]:.10f} {dim}lo {dim}hi")
    lines += ["", "Masses", "", "1 1.0", "", "Atoms # bond", ""]
    lines += [f"{i+1} {i//monomers+1} 1 {p[0]:.10f} {p[1]:.10f} {p[2]:.10f}" for i, p in enumerate(positions)]
    lines += ["", "Bonds", ""]
    lines += [f"{i+1} 1 {a+1} {b+1}" for i, (a, b) in enumerate(bonds)]
    Path(path).write_text("\n".join(lines) + "\n")


def simulate_lammps(config, output: Path, emit):
    from lammps import lammps
    positions, bonds = initial_chains(config["monomers"], config["chains"], config["seed"])
    write_lammps_data(output / "initial.data", positions, bonds, config["monomers"])
    commands = ["units lj", "atom_style bond", "boundary f f f", "atom_modify map array",
                f'read_data "{output / "initial.data"}"', "pair_style soft 1.2", "pair_coeff * * 5.0",
                "bond_style harmonic", "bond_coeff 1 15.0 1.0", "special_bonds lj 0 1 1",
                "neighbor 0.4 bin", "neigh_modify every 1 delay 0 check yes",
                f"velocity all create {config['temperature']} {config['seed']} mom yes rot no dist gaussian",
                f"fix thermostat all langevin {config['temperature']} {config['temperature']} 1.0 {config['seed'] + 1 if config['seed'] < 2147483647 else 1}",
                "fix integrate all nve", f"timestep {config['timestep']}",
                "thermo 100", "thermo_style custom step temp pe ke etotal", "thermo_modify norm no", "run 0"]
    (output / "input.lmp").write_text("\n".join(commands + [f"run {config['steps']}", f'write_restart "{output / "restart.lammps"}"']) + "\n")
    engine = lammps(cmdargs=["-screen", "none", "-log", str(output / "lammps.log")])
    frames, samples = [], []
    try:
        for command in commands:
            engine.command(command)
        stride = max(1, math.ceil(config["steps"] / 100))
        step = 0
        while True:
            positions = np.ctypeslib.as_array(engine.gather_atoms("x", 1, 3), shape=(len(positions) * 3,)).reshape(-1, 3).copy()
            if not np.isfinite(positions).all():
                raise RuntimeError("LAMMPS generated non-finite coordinates")
            frames.append({"step": step, "positions": positions.round(6).tolist()})
            samples.append(_sample(positions, step, config, engine.get_thermo("pe"), float(engine.get_thermo("temp"))))
            emit("simulate", step / config["steps"], f"LAMMPS: {step}/{config['steps']}")
            if step == config["steps"]:
                break
            count = min(stride, config["steps"] - step)
            engine.command(f"run {count} pre no post no")
            step += count
        engine.command(f'write_restart "{output / "restart.lammps"}"')
        version = engine.version()
    finally:
        engine.close()
    return frames, samples, bonds, f"LAMMPS {version} Langevin/NVE"


def write_cg_pdb(path, positions, bonds, monomers):
    lines = ["REMARK 900 COARSE-GRAINED VISUALIZATION; COORDINATES ARE SIGMA, NOT ANGSTROMS", "REMARK 900 CARBON LABELS REPRESENT BEADS, NOT CHEMICAL ATOMS"]
    for i, (x, y, z) in enumerate(positions):
        chain = chr(65 + i // monomers)
        lines.append(f"ATOM  {i+1:5d}  C   CGP {chain}{i%monomers+1:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           C")
    lines += [f"CONECT{a+1:5d}{b+1:5d}" for a, b in bonds]
    Path(path).write_text("\n".join(lines + ["END", ""]))


def analyse_mdanalysis(frames, monomers, chains):
    """Numerical cross-check only; no unit conversion or physical interpretation."""
    import MDAnalysis as mda
    coords = np.asarray([f["positions"] for f in frames], dtype=np.float32)
    universe = mda.Universe.empty(monomers * chains, trajectory=True)
    universe.add_TopologyAttr("masses", np.ones(monomers * chains))
    universe.load_new(coords, order="fac")
    rg_series = []
    for _ in universe.trajectory:
        rg_series.append(float(np.mean([universe.atoms[c*monomers:(c+1)*monomers].radius_of_gyration()
                                        for c in range(chains)])))
    return {"engine": f"MDAnalysis {mda.__version__}", "rg": rg_series, "unit": "sigma", "coordinate_unit_conversion": False}


def build_linear_polymer(repeat_smiles: str, monomers: int, seed=42):
    """Join exactly two singly bonded terminal dummy atoms per repeat; H-cap ends.

    This is explicit head-to-tail assembly, not reaction inference or tacticity
    prediction. RDKit preserves specified stereochemistry but does not assign it.
    """
    from rdkit import Chem
    from rdkit.Chem import AllChem
    unit = Chem.MolFromSmiles(repeat_smiles)
    if unit is None:
        raise ValueError("Invalid repeat-unit SMILES")
    dummies = [a.GetIdx() for a in unit.GetAtoms() if a.GetAtomicNum() == 0]
    if len(dummies) != 2 or len(Chem.GetMolFrags(unit)) != 1:
        raise ValueError("Use one connected repeat unit with exactly two '*' attachment atoms")
    for idx in dummies:
        atom = unit.GetAtomWithIdx(idx)
        if atom.GetDegree() != 1 or atom.GetBonds()[0].GetBondType() != Chem.BondType.SINGLE:
            raise ValueError("Each attachment '*' must have one single bond")
    # Prefer labels [*:1] and [*:2]; otherwise preserve SMILES atom order.
    dummies.sort(key=lambda i: (unit.GetAtomWithIdx(i).GetAtomMapNum() or 1000, i))
    neighbours = [unit.GetAtomWithIdx(i).GetNeighbors()[0].GetIdx() for i in dummies]
    keep = [i for i in range(unit.GetNumAtoms()) if i not in dummies]
    mapping = {old: new for new, old in enumerate(keep)}
    rw = Chem.RWMol(unit)
    for idx in sorted(dummies, reverse=True):
        rw.RemoveAtom(idx)
    core = rw.GetMol()
    count = core.GetNumAtoms()
    if count * monomers > 1000:
        raise ValueError("Atomistic builder is limited to 1000 heavy atoms")
    joined = core
    for _ in range(1, monomers):
        joined = Chem.CombineMols(joined, core)
    rw = Chem.RWMol(joined)
    for i in range(monomers - 1):
        rw.AddBond(i * count + mapping[neighbours[1]], (i + 1) * count + mapping[neighbours[0]], Chem.BondType.SINGLE)
    mol = rw.GetMol()
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    Chem.SanitizeMol(mol)
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = int(seed % 2147483647)
    params.useRandomCoords = True
    if AllChem.EmbedMolecule(mol, params) < 0:
        raise RuntimeError("RDKit conformer embedding failed; try shorter chains or another seed")
    method, converged = "ETKDGv3 only", None
    if AllChem.MMFFHasAllMoleculeParams(mol):
        method = "ETKDGv3 + MMFF94 geometry optimization"
        converged = AllChem.MMFFOptimizeMolecule(mol, maxIters=400) == 0
    return mol, {"method": method, "geometry_optimization_converged": converged,
                 "smiles": Chem.MolToSmiles(Chem.RemoveHs(mol)), "atoms": mol.GetNumAtoms(),
                 "monomers": monomers, "end_caps": "hydrogen", "length_unit": "angstrom",
                 "rdkit_random_seed": int(seed % 2147483647),
                 "warnings": ["One generated conformer is not an equilibrated polymer ensemble.",
                              "MMFF geometry optimization is not an MD force-field or charge assignment."]}


def run_workflow(raw, output_dir, event: Callable | None = None):
    config = validate_config(raw)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    def emit(stage, progress, message):
        record = {"time": time.time(), "stage": stage, "progress": float(progress), "message": message}
        with (output / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        if event:
            event(record)
    write_json(output / "config.json", config)
    emit("prepare", 0, "Validated workflow configuration")
    started = time.time()
    if config["route"] == "mixture":
        from workbench.bridges import run_bridge
        result = run_bridge(config, output, emit)
    elif config["route"] != "polymer_cg":
        from workbench.external import run_external
        result = run_external(config, output, emit)
    else:
        warnings = ["Uncalibrated, penetrable soft beads in reduced units; no mapping to a named polymer.",
                    "Short trajectories are not evidence of equilibration; no Tg, density, or modulus is inferred."]
        engine = config["engine"]
        if engine == "numpy":
            frames, samples, bonds, backend = simulate_numpy(config, output, emit)
        else:
            try:
                frames, samples, bonds, backend = simulate_lammps(config, output, emit)
            except (ImportError, OSError) as exc:
                if engine == "lammps":
                    raise RuntimeError(f"LAMMPS could not load: {exc}") from exc
                warnings.append(f"LAMMPS unavailable ({exc}); selected overdamped NumPy dynamics. Its time scale differs from inertial LAMMPS dynamics.")
                frames, samples, bonds, backend = simulate_numpy(config, output, emit)
        emit("analyze", 0.98, "Computing chain observables and MDAnalysis cross-check")
        analysis = None
        try:
            analysis = analyse_mdanalysis(frames, config["monomers"], config["chains"])
        except ImportError as exc:
            warnings.append(f"MDAnalysis not available: {exc}")
        metrics = {"series": samples, "units": {"rg": "sigma", "end_to_end": "sigma", "potential_energy": "epsilon (total system)", "temperature": "k_B T/epsilon", "time": "tau"},
                   "summary": {"initial_rg": samples[0]["rg"], "final_rg": samples[-1]["rg"],
                               "final_end_to_end": samples[-1]["end_to_end"], "final_potential_energy": samples[-1]["potential_energy"],
                               "frames": len(frames), "beads": config["monomers"]*config["chains"]},
                   "mdanalysis": analysis, "equilibrated": False}
        if analysis:
            metrics["mdanalysis"]["max_rg_difference"] = float(np.max(np.abs(np.asarray(analysis["rg"]) - [s["rg"] for s in samples])))
        write_json(output / "metrics.json", metrics)
        write_json(output / "trajectory.json", {"unit": "sigma", "kind": "coarse_grained", "frames": frames, "bonds": bonds.tolist(), "chains": config["chains"], "monomers": config["monomers"]})
        write_cg_pdb(output / "structure.pdb", frames[-1]["positions"], bonds, config["monomers"])
        xyz = [str(len(frames[-1]["positions"])), "CG beads; coordinates in sigma, NOT angstroms"] + ["C " + " ".join(map(str, p)) for p in frames[-1]["positions"]]
        (output / "structure.xyz").write_text("\n".join(xyz) + "\n")
        result = {"status": "completed", "route": config["route"], "engine": backend,
                  "model": MODEL, "metrics": metrics, "warnings": warnings,
                  "artifacts": [p.name for p in output.iterdir() if p.is_file()]}
    result["elapsed_seconds"] = round(time.time() - started, 3)
    write_json(output / "result.json", result)
    try:
        from workbench.descriptors import extract_job_features
        write_json(output / "features.json", extract_job_features(output, {"config": config, "result": result}))
    except Exception as exc:
        # Preserve the successful calculation when a separately versioned analysis fails.
        write_json(output / "feature_error.json", {"error": str(exc), "calculation_status_unchanged": True})
    emit("complete", 1, result.get("message", f"Workflow {result['status']}"))
    return result
