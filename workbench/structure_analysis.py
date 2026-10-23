"""Structure and trajectory analysis of finished bulk cells with the Rust kernels (workbench_native).

Materials Studio counterparts, written from the published definitions:
- rdf:         radial distribution functions g(r) between element groups (Forcite Analysis); normalised
               like MDAnalysis InterRDF so the two agree number for number
- msd:         mean-squared displacement and the Einstein diffusion coefficient (Forcite Analysis)
- free_volume: van der Waals occupied volume, probe-accessible and probe-occupiable volume, Bondi FFV
               (Atom Volumes & Surfaces)
- widom:       Widom insertion of a single-site Lennard-Jones gas: excess chemical potential and the
               Henry's-law solubility coefficient (the low-pressure limit of Sorption)

Inputs are a job's LAMMPS files: the sampling trajectory eq3.dump (GAFF2 bulk cells) and the final
data file (eq3_last.data, or final.data for PCFF and crosslinked cells).
"""
from __future__ import annotations
import math
from pathlib import Path

import numpy as np

try:
    import workbench_native as native
except ImportError:  # pragma: no cover
    native = None

R_KCAL = 0.0019872043
BOLTZMANN_KCAL = R_KCAL  # per mole: energies in kcal/mol, epsilon/k in K -> kcal/mol via R
# Bondi van der Waals radii (angstrom), keyed by element.
VDW = {"H": 1.10, "C": 1.70, "N": 1.55, "O": 1.52, "F": 1.47, "Si": 2.10, "P": 1.80, "S": 1.80, "Cl": 1.75, "Br": 1.85, "I": 1.98}
MASSES = {"H": 1.008, "C": 12.011, "N": 14.007, "O": 15.999, "F": 18.998, "Si": 28.085, "P": 30.974, "S": 32.06, "Cl": 35.45, "Br": 79.904, "I": 126.904}
# Single-site Lennard-Jones gases (sigma angstrom, epsilon/k K) from viscosity data, Bird, Stewart and
# Lightfoot, Transport Phenomena, 2nd ed., Table E.1.
GASES = {"He": (2.576, 10.2), "H2": (2.915, 38.0), "N2": (3.667, 99.8), "O2": (3.433, 113.0), "Ar": (3.432, 122.4),
         "CH4": (3.780, 154.0), "CO2": (3.996, 190.0)}
MAX_FRAMES = 2000


def _require_kernel():
    if native is None:
        raise ValueError("Rust 계산 커널(workbench_native)이 설치되어 있지 않습니다. setup.sh를 cargo가 있는 환경에서 다시 실행하세요.")


def _is_number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def element_of(mass):
    symbol, reference = min(MASSES.items(), key=lambda item: abs(item[1] - mass))
    if abs(reference - mass) > 0.2:
        raise ValueError(f"질량 {mass:g}에 맞는 원소를 찾지 못했습니다.")
    return symbol


def read_data(path):
    """Atoms (sorted by id), per-type masses and pair coefficients, and the box of a LAMMPS data file."""
    lines = Path(path).read_text().splitlines()
    box, sections, current = {}, {}, None
    for line in lines:
        parts = line.split("#")[0].split()
        if not parts:
            continue
        if len(parts) == 4 and parts[2:] in (["xlo", "xhi"], ["ylo", "yhi"], ["zlo", "zhi"]):
            box[parts[2][0]] = (float(parts[0]), float(parts[1]))
            continue
        if not _is_number(parts[0]):
            # A section header ("Masses", "Pair Coeffs", "Atoms", ...) or the title line.
            current = " ".join(parts)
            sections.setdefault(current, [])
        elif current:
            sections[current].append(parts)
    if set(box) != {"x", "y", "z"} or "Atoms" not in sections or "Masses" not in sections:
        raise ValueError(f"{Path(path).name}: 상자, Masses, Atoms가 있는 LAMMPS 데이터 파일이 아닙니다.")
    masses = {int(r[0]): float(r[1]) for r in sections["Masses"]}
    pair = {int(r[0]): (float(r[1]), float(r[2])) for r in sections.get("Pair Coeffs", [])}
    rows = sorted(sections["Atoms"], key=lambda r: int(r[0]))
    # atom_style full: id mol type q x y z [ix iy iz]
    atoms = {"id": np.array([int(r[0]) for r in rows]), "mol": np.array([int(r[1]) for r in rows]),
             "type": np.array([int(r[2]) for r in rows]), "charge": np.array([float(r[3]) for r in rows]),
             "position": np.array([[float(v) for v in r[4:7]] for r in rows])}
    lo = np.array([box[k][0] for k in "xyz"])
    hi = np.array([box[k][1] for k in "xyz"])
    return {"atoms": atoms, "masses": masses, "pair": pair, "lo": lo, "length": hi - lo}


def read_dump(path, every=1, max_frames=MAX_FRAMES):
    """Frames of a LAMMPS custom dump with id type mol x y z ix iy iz, sorted by id: wrapped and unwrapped
    coordinates (unwrapped = x + i L with that frame's box) and box edge lengths."""
    frames, steps = [], []
    with open(path) as stream:
        index = 0
        while True:
            line = stream.readline()
            if not line:
                break
            if not line.startswith("ITEM: TIMESTEP"):
                continue
            step = int(stream.readline())
            stream.readline()
            count = int(stream.readline())
            stream.readline()
            bounds = [tuple(float(v) for v in stream.readline().split()[:2]) for _ in range(3)]
            header = stream.readline().split()[2:]
            rows = [stream.readline().split() for _ in range(count)]
            if index % every == 0:
                col = {name: k for k, name in enumerate(header)}
                for needed in ("id", "type", "x", "y", "z", "ix", "iy", "iz"):
                    if needed not in col:
                        raise ValueError(f"덤프에 {needed} 열이 없습니다.")
                data = np.array([[float(r[col[c]]) for c in ("id", "type", "x", "y", "z", "ix", "iy", "iz")] for r in rows])
                data = data[np.argsort(data[:, 0])]
                lo = np.array([b[0] for b in bounds])
                length = np.array([b[1] - b[0] for b in bounds])
                wrapped = data[:, 2:5] - lo
                frames.append({"step": step, "length": length, "wrapped": wrapped, "unwrapped": wrapped + data[:, 5:8] * length,
                               "type": data[:, 1].astype(int)})
                steps.append(step)
                if len(frames) >= max_frames:
                    break
            index += 1
    if not frames:
        raise ValueError(f"{Path(path).name}에 프레임이 없습니다.")
    return frames


def job_files(directory):
    directory = Path(directory)
    data = next((directory / n for n in ("eq3_last.data", "final.data") if (directory / n).is_file()), None)
    if data is None:
        raise ValueError("벌크 셀의 최종 데이터 파일(eq3_last.data 또는 final.data)이 없습니다. 완료된 비정질 벌크·가교 작업에서 실행하세요.")
    dump = directory / "eq3.dump"
    return data, dump if dump.is_file() else None


def _elements(data):
    return np.array([element_of(data["masses"][t]) for t in data["atoms"]["type"]])


def _frames(directory, every, max_frames):
    data_path, dump = job_files(directory)
    data = read_data(data_path)
    if dump is None:
        frames = [{"step": None, "length": data["length"], "wrapped": data["atoms"]["position"] - data["lo"], "unwrapped": None,
                   "type": data["atoms"]["type"]}]
    else:
        frames = read_dump(dump, every, max_frames)
        if len(frames[0]["type"]) != len(data["atoms"]["type"]) or (frames[0]["type"] != data["atoms"]["type"]).any():
            raise ValueError("덤프와 데이터 파일의 원자가 일치하지 않습니다.")
    return data, frames, data_path.name, dump.name if dump else None


def _selection(elements, spec):
    spec = str(spec or "all").strip()
    if spec in ("all", "*"):
        return np.arange(len(elements))
    if spec == "heavy":
        return np.flatnonzero(elements != "H")
    chosen = np.flatnonzero(np.isin(elements, [s.strip() for s in spec.split(",") if s.strip()]))
    if not len(chosen):
        raise ValueError(f"선택한 원소({spec})에 해당하는 원자가 없습니다. 있는 원소: {', '.join(sorted(set(elements)))}")
    return chosen


def rdf(directory, pairs=("C-C",), r_max=10.0, bins=200, every=1, max_frames=MAX_FRAMES, exclude_same_molecule=False):
    """g(r) between element groups ("C-C", "C-O", "heavy-heavy", "all-all"), MDAnalysis InterRDF normalisation:
    g = counts / (n_frames * n_a * n_b / <V> * shell volume), counting ordered pairs and excluding i = j."""
    _require_kernel()
    data, frames, data_name, dump_name = _frames(directory, every, max_frames)
    elements = _elements(data)
    smallest = min(float(f["length"].min()) for f in frames)
    if not 0 < r_max <= smallest / 2:
        raise ValueError(f"r_max는 0보다 크고 가장 짧은 상자 길이의 절반({smallest / 2:.2f} Å) 이하여야 합니다.")
    positions = np.ascontiguousarray(np.stack([f["wrapped"] for f in frames]))
    boxes = np.ascontiguousarray(np.stack([f["length"] for f in frames]))
    molecule = np.ascontiguousarray(data["atoms"]["mol"].astype(np.int64)) if exclude_same_molecule else None
    edges = np.linspace(0.0, r_max, bins + 1)
    centers = 0.5 * (edges[1:] + edges[:-1])
    shells = 4.0 / 3.0 * math.pi * (edges[1:] ** 3 - edges[:-1] ** 3)
    volume = float(np.prod(boxes, axis=1).mean())
    results = []
    for pair in pairs:
        left, _, right = str(pair).partition("-")
        a, b = _selection(elements, left), _selection(elements, right or left)
        counts = native.rdf_counts(positions, boxes, a.astype(np.int64), b.astype(np.int64), float(r_max), int(bins), molecule)
        # Pairs that can be counted: i = j never, and same-molecule pairs when they are excluded.
        if exclude_same_molecule:
            mol = data["atoms"]["mol"]
            ids, in_a = np.unique(mol[a], return_counts=True)
            in_b = dict(zip(*np.unique(mol[b], return_counts=True)))
            excluded = sum(int(n) * int(in_b.get(m, 0)) for m, n in zip(ids, in_a))
        else:
            excluded = len(np.intersect1d(a, b))
        pairs_counted = len(a) * len(b) - excluded
        g = counts / (len(frames) * pairs_counted / volume * shells)
        peak = int(np.argmax(g))
        results.append({"pair": pair, "atoms": [int(len(a)), int(len(b))], "g": g.tolist(),
                        "first_peak": {"r_angstrom": float(centers[peak]), "g": float(g[peak])}})
    return {"analysis": "rdf", "r_angstrom": centers.tolist(), "pairs": results, "frames": len(frames),
            "source": {"data": data_name, "trajectory": dump_name}, "exclude_same_molecule": bool(exclude_same_molecule),
            "normalisation": "MDAnalysis InterRDF: ordered pairs over the pairs that can be counted (i != j, and other molecules only when excluded), mean box volume",
            "warnings": [] if dump_name else ["No trajectory: g(r) of the final structure only (one frame)."]}


def msd(directory, selection="heavy", every=1, max_frames=MAX_FRAMES, remove_drift=True, time_step_fs=None, fit_window=(0.1, 0.5)):
    """Windowed MSD of the selected atoms and D = slope / 6 over the lag window `fit_window` (fractions of the run)."""
    _require_kernel()
    import json
    data, frames, data_name, dump_name = _frames(directory, every, max_frames)
    if dump_name is None or len(frames) < 4:
        raise ValueError("MSD에는 궤적(eq3.dump, 프레임 4개 이상)이 필요합니다. GAFF2 비정질 벌크 셀 작업에서 실행하세요.")
    if time_step_fs is None:
        complete = Path(directory) / "bulk_complete.json"
        time_step_fs = float(json.loads(complete.read_text()).get("time_step_fs", 1.0)) if complete.is_file() else 1.0
    elements = _elements(data)
    atoms = _selection(elements, selection)
    positions = np.stack([f["unwrapped"] for f in frames])
    if remove_drift:
        # Remove the centre-of-mass motion of the whole cell, frame by frame.
        mass = np.array([data["masses"][t] for t in data["atoms"]["type"]])
        com = (positions * mass[None, :, None]).sum(axis=1) / mass.sum()
        positions = positions - (com - com[0])[:, None, :]
    values = native.msd(np.ascontiguousarray(positions), atoms.astype(np.int64))
    steps = np.array([f["step"] for f in frames], dtype=float)
    lag_steps = steps - steps[0]
    if len(set(np.round(np.diff(steps), 6))) != 1:
        raise ValueError("덤프 간격이 일정하지 않습니다.")
    times_ps = lag_steps * time_step_fs / 1000.0
    lo, hi = max(1, int(fit_window[0] * len(frames))), max(2, int(fit_window[1] * len(frames)))
    slope, intercept = np.polyfit(times_ps[lo:hi + 1], values[lo:hi + 1], 1)
    diffusion = slope / 6.0 * 1e-8  # angstrom^2/ps -> m^2/s
    log_t, log_m = np.log(times_ps[lo:hi + 1]), np.log(np.clip(values[lo:hi + 1], 1e-12, None))
    exponent = float(np.polyfit(log_t, log_m, 1)[0])
    warnings = []
    if exponent < 0.9:
        warnings.append(f"MSD ~ t^{exponent:.2f} in the fit window: not yet diffusive (needs t^1); D is an upper-bound estimate, not a diffusion coefficient.")
    return {"analysis": "msd", "selection": selection, "atoms": int(len(atoms)), "frames": len(frames), "time_ps": times_ps.tolist(),
            "msd_angstrom2": values.tolist(), "fit": {"window_ps": [float(times_ps[lo]), float(times_ps[hi])], "slope_angstrom2_per_ps": float(slope),
            "diffusion_m2_s": float(diffusion), "loglog_exponent": exponent}, "remove_drift": bool(remove_drift),
            "source": {"data": data_name, "trajectory": dump_name}, "warnings": warnings}


def free_volume(directory, probes=(0.0, 1.0, 1.5), spacing=0.25, frame="last"):
    """Occupied, accessible and occupiable volume fractions for each probe radius, and Bondi's FFV."""
    _require_kernel()
    data, frames, data_name, dump_name = _frames(directory, 1, MAX_FRAMES)
    chosen = frames[-1] if frame == "last" else frames[int(frame)]
    elements = _elements(data)
    radii = np.array([VDW.get(e, 2.0) for e in elements])
    if not 0.05 <= spacing <= 1.0:
        raise ValueError("격자 간격은 0.05~1.0 Å이어야 합니다.")
    length = [float(v) for v in chosen["length"]]
    volume = float(np.prod(length))
    rows, occupied = [], None
    for probe in probes:
        if not 0 <= float(probe) <= 5:
            raise ValueError("탐침 반지름은 0~5 Å이어야 합니다.")
        result = native.free_volume(np.ascontiguousarray(chosen["wrapped"]), radii, length, float(probe), float(spacing), 0.1, 100)
        occupied = result["occupied_fraction"]
        rows.append({"probe_radius_angstrom": float(probe), "accessible_fraction": result["accessible_fraction"],
                     "occupiable_fraction": result["occupiable_fraction"],
                     "accessible_volume_angstrom3": result["accessible_fraction"] * volume,
                     "occupiable_volume_angstrom3": result["occupiable_fraction"] * volume})
        histogram = np.asarray(result["surface_distance_histogram"])
    mass = sum(data["masses"][t] for t in data["atoms"]["type"])
    return {"analysis": "free_volume", "box_angstrom": length, "density_g_cm3": mass / 6.02214076e23 / volume * 1e24,
            "grid": result["grid"], "spacing_angstrom": spacing, "occupied_fraction": occupied,
            "fractional_free_volume_probe0": 1.0 - occupied,
            # Bondi: FFV = (V - 1.3 V_w) / V with the van der Waals volume V_w.
            "bondi_ffv": 1.0 - 1.3 * occupied, "probes": rows,
            "surface_distance": {"bin_angstrom": 0.1, "counts": histogram.tolist()},
            "radii": "Bondi van der Waals radii", "source": {"data": data_name, "trajectory": dump_name, "frame": frame},
            "warnings": ["Grid volumes carry an error of order the spacing over the atomic radius; halve spacing to check convergence."]}


def widom(directory, gas="CO2", temperature=None, insertions=20000, every=1, max_frames=200, cutoff=None, seed=1):
    """Widom insertion of a single-site LJ gas into the GAFF2 cell frames: mu_ex = -RT ln<exp(-U/RT)> and the
    solubility coefficient S = (273.15 K / T) <exp(-U/RT)> in cm^3(STP) / (cm^3 polymer atm)."""
    _require_kernel()
    import json
    if gas not in GASES:
        raise ValueError(f"지원하는 기체: {', '.join(GASES)}")
    data_path, dump = job_files(directory)
    data = read_data(data_path)
    if not data["pair"] or data_path.name == "final.data":
        raise ValueError("Widom 삽입은 12-6 LJ(GAFF2) 셀에서만 계산합니다. PCFF(9-6 LJ)·가교 셀은 지원하지 않습니다.")
    frames = read_dump(dump, every, max_frames) if dump else [{"length": data["length"], "wrapped": data["atoms"]["position"] - data["lo"]}]
    if temperature is None:
        complete = Path(directory) / "bulk_complete.json"
        temperature = float(json.loads(complete.read_text()).get("temperature_kelvin", 300.0)) if complete.is_file() else 300.0
    sigma_gas, eps_k = GASES[gas]
    eps_gas = eps_k * R_KCAL
    types = data["atoms"]["type"]
    eps = np.array([data["pair"][t][0] for t in types])
    sig = np.array([data["pair"][t][1] for t in types])
    smallest = min(float(f["length"].min()) for f in frames)
    cutoff = float(cutoff or min(12.0, 0.5 * smallest - 1e-6))
    if not 0 < cutoff <= smallest / 2:
        raise ValueError(f"차단 거리는 상자 길이의 절반({smallest / 2:.2f} Å) 이하여야 합니다.")
    positions = np.ascontiguousarray(np.stack([f["wrapped"] for f in frames]))
    boxes = np.ascontiguousarray(np.stack([f["length"] for f in frames]))
    factors = np.asarray(native.widom_insertion(positions, boxes, sig, eps, sigma_gas, eps_gas, float(temperature), int(insertions), cutoff, int(seed), True))
    mean = float(factors.mean())
    error = float(factors.std(ddof=1) / math.sqrt(len(factors))) if len(factors) > 1 else None
    mu = -R_KCAL * temperature * math.log(mean) if mean > 0 else math.inf
    solubility = 273.15 / temperature * mean
    warnings = ["Single-site LJ gas (Bird-Stewart-Lightfoot viscosity parameters) without charges or quadrupoles; CO2 and N2 solubilities in polar polymers are underestimated.",
                "Plain 12-6 LJ with a cutoff and analytic tail correction, not the cell's CHARMM switching; frames are taken as fixed (no polymer relaxation around the gas)."]
    if mean > 0 and len(factors) > 1 and error / mean > 0.2:
        warnings.insert(0, f"The frame-to-frame error is {100 * error / mean:.0f}% of <exp(-U/RT)>; use more frames or insertions.")
    return {"analysis": "widom", "gas": gas, "gas_lj": {"sigma_angstrom": sigma_gas, "epsilon_over_k_kelvin": eps_k}, "temperature_kelvin": temperature,
            "frames": len(frames), "insertions_per_frame": int(insertions), "cutoff_angstrom": cutoff,
            "boltzmann_factor": {"mean": mean, "standard_error": error},
            "excess_chemical_potential_kcal_mol": mu,
            "solubility_cm3stp_per_cm3_atm": solubility,
            "solubility_standard_error": 273.15 / temperature * error if error is not None else None,
            "source": {"data": data_path.name, "trajectory": dump.name if dump else None}, "warnings": warnings}


ANALYSES = {"rdf": rdf, "msd": msd, "free_volume": free_volume, "widom": widom}
