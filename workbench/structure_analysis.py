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
import json
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
MAX_ATOMS = 100000
MAX_FRAME_ATOMS = 2000000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_GRID_POINTS = 8000000


def _require_kernel():
    if native is None:
        raise ValueError("Rust 계산 커널(workbench_native)이 설치되어 있지 않습니다. setup.sh를 cargo가 있는 환경에서 다시 실행하세요.")


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or not low <= value <= high:
        raise ValueError(f"{name}은(는) {low}~{high} 범위의 정수여야 합니다.")
    return int(value)


def _number(value, name, low, high, *, open_low=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ValueError(f"{name}은(는) 유한한 숫자여야 합니다.")
    value = float(value)
    if not math.isfinite(value) or value > high or (value <= low if open_low else value < low):
        raise ValueError(f"{name} 값이 허용 범위({low}~{high}) 밖입니다.")
    return value


def _boolean(value, name):
    if not isinstance(value, bool):
        raise ValueError(f"{name}은(는) true 또는 false여야 합니다.")
    return value


def _input_path(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name}: 일반 입력 파일이 필요합니다.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{path.name}: 분석 입력 파일은 512 MiB 이하여야 합니다.")
    return path


def _finite_result(result):
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, OverflowError) as exc:
        raise ValueError("분석 결과가 유한한 수가 아닙니다. 입력 구조와 계산 조건을 확인하세요.") from exc
    return result


def _metadata(directory):
    path = Path(directory) / "bulk_complete.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(_input_path(path).read_text())
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("bulk_complete.json 메타데이터를 읽을 수 없습니다.") from exc
    if not isinstance(value, dict):
        raise ValueError("bulk_complete.json은 객체여야 합니다.")
    return value


def _is_number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def element_of(mass):
    if not math.isfinite(mass) or mass <= 0:
        raise ValueError("원자 질량은 양의 유한한 수여야 합니다.")
    symbol, reference = min(MASSES.items(), key=lambda item: abs(item[1] - mass))
    if abs(reference - mass) > 0.2:
        raise ValueError(f"질량 {mass:g}에 맞는 원소를 찾지 못했습니다.")
    return symbol


def read_data(path):
    """Atoms (sorted by id), per-type masses and pair coefficients, and the box of a LAMMPS data file."""
    lines = _input_path(path).read_text().splitlines()
    box, sections, current, declared_atoms = {}, {}, None, None
    for line in lines:
        parts = line.split("#")[0].split()
        if not parts:
            continue
        if len(parts) == 2 and parts[1] == "atoms":
            if declared_atoms is not None:
                raise ValueError("LAMMPS 데이터 파일의 원자 수 선언이 중복됐습니다.")
            declared_atoms = _integer(int(parts[0]), "원자 수", 1, MAX_ATOMS)
            continue
        if parts[-3:] == ["xy", "xz", "yz"] or parts[-1:] in (["avec"], ["bvec"], ["cvec"]):
            raise ValueError("기울어진 셀은 지원하지 않습니다. 직교 주기 상자가 필요합니다.")
        if len(parts) == 4 and parts[2:] in (["xlo", "xhi"], ["ylo", "yhi"], ["zlo", "zhi"]):
            if parts[2][0] in box:
                raise ValueError("LAMMPS 데이터 파일의 상자 범위가 중복됐습니다.")
            box[parts[2][0]] = (float(parts[0]), float(parts[1]))
            continue
        if not _is_number(parts[0]):
            # A section header ("Masses", "Pair Coeffs", "Atoms", ...) or the title line.
            current = " ".join(parts)
            if current == "Atoms" and "#" in line:
                style = line.split("#", 1)[1].split()
                if style and style[0] != "full":
                    raise ValueError("LAMMPS atom_style full 데이터가 필요합니다.")
            sections.setdefault(current, [])
        elif current:
            sections[current].append(parts)
    if set(box) != {"x", "y", "z"} or "Atoms" not in sections or "Masses" not in sections:
        raise ValueError(f"{Path(path).name}: 상자, Masses, Atoms가 있는 LAMMPS 데이터 파일이 아닙니다.")
    try:
        mass_rows = sections["Masses"]
        masses = {int(r[0]): float(r[1]) for r in mass_rows}
        pair_rows = sections.get("Pair Coeffs", [])
        pair = {int(r[0]): (float(r[1]), float(r[2])) for r in pair_rows}
        rows = sorted(sections["Atoms"], key=lambda r: int(r[0]))
        if any(len(r) not in (7, 10) for r in rows):
            raise ValueError("Atoms 행은 id mol type q x y z [ix iy iz] 형식이어야 합니다.")
        _integer(len(rows), "원자 수", 1, MAX_ATOMS)
        if declared_atoms != len(rows):
            raise ValueError("선언된 원자 수와 Atoms 행 수가 다릅니다.")
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"{Path(path).name}: Masses, Pair Coeffs 또는 Atoms 형식이 잘못됐습니다.") from exc
    # atom_style full: id mol type q x y z [ix iy iz]
    atoms = {"id": np.array([int(r[0]) for r in rows]), "mol": np.array([int(r[1]) for r in rows]),
             "type": np.array([int(r[2]) for r in rows]), "charge": np.array([float(r[3]) for r in rows]),
             "position": np.array([[float(v) for v in r[4:7]] for r in rows])}
    lo = np.array([box[k][0] for k in "xyz"])
    hi = np.array([box[k][1] for k in "xyz"])
    if (len(masses) != len(mass_rows) or len(pair) != len(pair_rows)
            or any(t <= 0 or not math.isfinite(m) or m <= 0 for t, m in masses.items())
            or any(not np.isfinite(v).all() or v[0] < 0 or v[1] <= 0 for v in pair.values())):
        raise ValueError("질량·LJ 계수는 중복 없이 유한한 물리적 값이어야 합니다.")
    if (len(np.unique(atoms["id"])) != len(rows) or (atoms["id"] <= 0).any()
            or (atoms["mol"] < 0).any() or not set(atoms["type"]) <= masses.keys()
            or not np.isfinite(atoms["position"]).all() or not np.isfinite(atoms["charge"]).all()
            or not np.isfinite(lo).all() or not np.isfinite(hi).all() or (hi <= lo).any()):
        raise ValueError("원자 ID·타입·좌표 또는 상자 범위가 잘못됐습니다.")
    with np.errstate(over="ignore", invalid="ignore"):
        length = hi - lo
    if not np.isfinite(length).all() or (length > 1e6).any():
        raise ValueError("상자 길이는 유한하고 100만 Å 이하여야 합니다.")
    return {"atoms": atoms, "masses": masses, "pair": pair, "lo": lo, "length": length}


def read_dump(path, every=1, max_frames=MAX_FRAMES):
    """Frames of a LAMMPS custom dump with id type mol x y z ix iy iz, sorted by id: wrapped and unwrapped
    coordinates (unwrapped = x + i L with that frame's box) and box edge lengths."""
    every = _integer(every, "every", 1, 10000)
    max_frames = _integer(max_frames, "max_frames", 1, MAX_FRAMES)
    frames, previous_step = [], None
    with _input_path(path).open() as stream:
        index = 0
        while True:
            line = stream.readline()
            if not line:
                break
            if not line.startswith("ITEM: TIMESTEP"):
                raise ValueError("LAMMPS 덤프의 TIMESTEP 헤더가 잘못됐습니다.")
            step = int(stream.readline())
            if previous_step is not None and step <= previous_step:
                raise ValueError("덤프 시간 스텝은 중복 없이 증가해야 합니다.")
            previous_step = step
            if stream.readline().strip() != "ITEM: NUMBER OF ATOMS":
                raise ValueError("LAMMPS 덤프의 원자 수 헤더가 잘못됐습니다.")
            count = int(stream.readline())
            _integer(count, "원자 수", 1, MAX_ATOMS)
            if stream.readline().strip() != "ITEM: BOX BOUNDS pp pp pp":
                raise ValueError("덤프에는 직교 주기 상자(BOX BOUNDS pp pp pp)가 필요합니다.")
            bounds = [tuple(float(v) for v in stream.readline().split()) for _ in range(3)]
            if any(len(b) != 2 or not np.isfinite(b).all() or b[1] <= b[0] for b in bounds):
                raise ValueError("덤프 상자 범위가 잘못됐습니다.")
            atom_header = stream.readline().split()
            if atom_header[:2] != ["ITEM:", "ATOMS"]:
                raise ValueError("LAMMPS 덤프의 ATOMS 헤더가 잘못됐습니다.")
            header = atom_header[2:]
            if len(set(header)) != len(header):
                raise ValueError("덤프 원자 열 이름이 중복됐습니다.")
            rows = [stream.readline().split() for _ in range(count)]
            if any(len(r) != len(header) for r in rows):
                raise ValueError("덤프 원자 행이 누락되었거나 열 수가 맞지 않습니다.")
            if index % every == 0:
                if sum(len(f["type"]) for f in frames) + count > MAX_FRAME_ATOMS:
                    raise ValueError("분석할 원자×프레임 수가 200만을 넘습니다. every를 늘리거나 max_frames를 줄이세요.")
                col = {name: k for k, name in enumerate(header)}
                for needed in ("id", "type", "x", "y", "z", "ix", "iy", "iz"):
                    if needed not in col:
                        raise ValueError(f"덤프에 {needed} 열이 없습니다.")
                data = np.array([[float(r[col[c]]) for c in ("id", "type", "x", "y", "z", "ix", "iy", "iz")] for r in rows])
                if (not np.isfinite(data).all() or not np.equal(data[:, [0, 1, 5, 6, 7]], np.floor(data[:, [0, 1, 5, 6, 7]])).all()
                        or (data[:, :2] <= 0).any() or len(np.unique(data[:, 0])) != count):
                    raise ValueError("덤프 원자 ID·타입·이미지 또는 좌표가 잘못됐습니다.")
                data = data[np.argsort(data[:, 0])]
                lo = np.array([b[0] for b in bounds])
                length = np.array([b[1] - b[0] for b in bounds])
                if not np.isfinite(length).all() or (length > 1e6).any():
                    raise ValueError("덤프 상자 길이는 유한하고 100만 Å 이하여야 합니다.")
                wrapped = data[:, 2:5] - lo
                frames.append({"step": step, "length": length, "wrapped": wrapped, "unwrapped": data[:, 2:5] + data[:, 5:8] * length,
                               "type": data[:, 1].astype(int), "id": data[:, 0].astype(np.int64)})
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
    every = _integer(every, "every", 1, 10000)
    max_frames = _integer(max_frames, "max_frames", 1, MAX_FRAMES)
    data_path, dump = job_files(directory)
    data = read_data(data_path)
    if dump is None:
        frames = [{"step": None, "length": data["length"], "wrapped": data["atoms"]["position"] - data["lo"], "unwrapped": None,
                   "type": data["atoms"]["type"], "id": data["atoms"]["id"]}]
    else:
        frames = read_dump(dump, every, max_frames)
        if any(not np.array_equal(f["id"], data["atoms"]["id"]) or not np.array_equal(f["type"], data["atoms"]["type"]) for f in frames):
            raise ValueError("덤프와 데이터 파일의 원자가 일치하지 않습니다.")
    return data, frames, data_path.name, dump.name if dump else None


def _selection(elements, spec):
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("원소 선택을 입력하세요(all, heavy 또는 C,H 등).")
    spec = spec.strip()
    if spec in ("all", "*"):
        return np.arange(len(elements))
    if spec == "heavy":
        chosen = np.flatnonzero(elements != "H")
    else:
        symbols = [s.strip() for s in spec.split(",")]
        if any(s not in MASSES for s in symbols):
            raise ValueError("알 수 없는 원소 선택입니다. all, heavy 또는 C,H 등의 원소 기호를 사용하세요.")
        chosen = np.flatnonzero(np.isin(elements, symbols))
    if not len(chosen):
        raise ValueError(f"선택한 원소({spec})에 해당하는 원자가 없습니다. 있는 원소: {', '.join(sorted(set(elements)))}")
    return chosen


def rdf(directory, pairs=("C-C",), r_max=10.0, bins=200, every=1, max_frames=MAX_FRAMES, exclude_same_molecule=False):
    """g(r) between element groups ("C-C", "C-O", "heavy-heavy", "all-all"), MDAnalysis InterRDF normalisation:
    g = counts / (n_frames * n_a * n_b / <V> * shell volume), counting ordered pairs and excluding i = j."""
    _require_kernel()
    bins = _integer(bins, "bins", 1, 2000)
    r_max = _number(r_max, "r_max", 0, 1000000, open_low=True)
    exclude_same_molecule = _boolean(exclude_same_molecule, "exclude_same_molecule")
    if not isinstance(pairs, (list, tuple)) or not 1 <= len(pairs) <= 8 or any(not isinstance(p, str) for p in pairs):
        raise ValueError("RDF 원소 쌍을 1~8개 입력하세요(C-C, C-H 등).")
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
    work = 0
    for pair in pairs:
        if pair.count("-") != 1 or any(not p.strip() for p in pair.split("-")):
            raise ValueError("RDF 원소 쌍은 C-C, heavy-heavy 등의 형식이어야 합니다.")
        left, _, right = str(pair).partition("-")
        a, b = _selection(elements, left), _selection(elements, right)
        work += len(frames) * len(a) * len(b)
        if work > 2000000000:
            raise ValueError("RDF 계산량이 너무 큽니다. 원소 선택 또는 프레임 수를 줄이세요.")
        # Pairs that can be counted: i = j never, and same-molecule pairs when they are excluded.
        if exclude_same_molecule:
            mol = data["atoms"]["mol"]
            ids, in_a = np.unique(mol[a], return_counts=True)
            in_b = dict(zip(*np.unique(mol[b], return_counts=True)))
            excluded = sum(int(n) * int(in_b.get(m, 0)) for m, n in zip(ids, in_a))
        else:
            excluded = len(np.intersect1d(a, b))
        pairs_counted = len(a) * len(b) - excluded
        if pairs_counted <= 0:
            raise ValueError(f"{pair}: 제외 조건을 적용하면 계산할 원자 쌍이 없습니다.")
        counts = native.rdf_counts(positions, boxes, a.astype(np.int64), b.astype(np.int64), r_max, bins, molecule)
        g = counts / (len(frames) * pairs_counted / volume * shells)
        peak = int(np.argmax(g))
        results.append({"pair": pair, "atoms": [int(len(a)), int(len(b))], "g": g.tolist(),
                        "highest_peak": {"r_angstrom": float(centers[peak]), "g": float(g[peak])} if g[peak] > 0 else None})
    return _finite_result({"analysis": "rdf", "r_angstrom": centers.tolist(), "pairs": results, "frames": len(frames),
            "source": {"data": data_name, "trajectory": dump_name}, "exclude_same_molecule": bool(exclude_same_molecule),
            "normalisation": "MDAnalysis InterRDF: ordered pairs over the pairs that can be counted (i != j, and other molecules only when excluded), mean box volume",
            "warnings": [] if dump_name else ["No trajectory: g(r) of the final structure only (one frame)."]})


def msd(directory, selection="heavy", every=1, max_frames=MAX_FRAMES, remove_drift=True, time_step_fs=None, fit_window=(0.1, 0.5)):
    """Windowed MSD of the selected atoms and D = slope / 6 over the lag window `fit_window` (fractions of the run)."""
    _require_kernel()
    remove_drift = _boolean(remove_drift, "remove_drift")
    if not isinstance(fit_window, (tuple, list)) or len(fit_window) != 2:
        raise ValueError("fit_window는 시작·끝 비율 두 개여야 합니다.")
    fit_window = [_number(v, "fit_window", 0, 1) for v in fit_window]
    if fit_window[0] >= fit_window[1]:
        raise ValueError("fit_window 시작은 끝보다 작아야 합니다.")
    data, frames, data_name, dump_name = _frames(directory, every, max_frames)
    if dump_name is None or len(frames) < 4:
        raise ValueError("MSD에는 궤적(eq3.dump, 프레임 4개 이상)이 필요합니다. GAFF2 비정질 벌크 셀 작업에서 실행하세요.")
    if time_step_fs is None:
        time_step_fs = _metadata(directory).get("time_step_fs")
        if time_step_fs is None:
            raise ValueError("시간 간격을 확인할 수 없습니다. time_step_fs를 지정하세요.")
    time_step_fs = _number(time_step_fs, "time_step_fs", 1e-6, 1000000)
    elements = _elements(data)
    atoms = _selection(elements, selection)
    if len(atoms) * len(frames) ** 2 > 250000000:
        raise ValueError("MSD 계산량이 너무 큽니다. 원소 선택 또는 프레임 수를 줄이세요.")
    positions = np.stack([f["unwrapped"] for f in frames])
    if remove_drift:
        # Remove the centre-of-mass motion of the whole cell, frame by frame.
        mass = np.array([data["masses"][t] for t in data["atoms"]["type"]])
        com = (positions * mass[None, :, None]).sum(axis=1) / mass.sum()
        positions = positions - (com - com[0])[:, None, :]
    steps = np.array([f["step"] for f in frames], dtype=float)
    lag_steps = steps - steps[0]
    if (np.diff(steps) <= 0).any() or len(set(np.round(np.diff(steps), 6))) != 1:
        raise ValueError("덤프 간격이 일정하지 않습니다.")
    times_ps = lag_steps * time_step_fs / 1000.0
    lo, hi = max(1, int(fit_window[0] * len(frames))), min(len(frames) - 1, int(fit_window[1] * len(frames)))
    if hi <= lo:
        raise ValueError("피팅 구간에는 서로 다른 양의 지연 시간 두 개 이상이 필요합니다.")
    values = native.msd(np.ascontiguousarray(positions), atoms.astype(np.int64))
    slope, intercept = np.polyfit(times_ps[lo:hi + 1], values[lo:hi + 1], 1)
    diffusion = slope / 6.0 * 1e-8  # angstrom^2/ps -> m^2/s
    # Do not fit a log slope to cancellation noise after subtracting centre-of-mass motion.
    roundoff_squared = (64 * np.finfo(float).eps * max(1.0, float(np.abs(positions).max()))) ** 2
    positive = (values[lo:hi + 1] > roundoff_squared).all()
    exponent = float(np.polyfit(np.log(times_ps[lo:hi + 1]), np.log(values[lo:hi + 1]), 1)[0]) if positive else None
    diffusive = bool(exponent is not None and 0.9 <= exponent <= 1.1 and slope > 0)
    warnings = ["A near-linear MSD window alone does not establish equilibrium or a converged diffusion coefficient."]
    if not diffusive:
        warnings.append("The selected fit window is not consistent with normal diffusion (positive slope and log-log exponent 0.9–1.1); no diffusion coefficient is reported.")
    if any(not np.allclose(f["length"], frames[0]["length"], rtol=1e-6, atol=1e-8) for f in frames):
        warnings.append("Box lengths vary: unwrapping includes box deformation; this can bias a diffusion estimate.")
    return _finite_result({"analysis": "msd", "selection": selection, "atoms": int(len(atoms)), "frames": len(frames), "time_ps": times_ps.tolist(),
            "msd_angstrom2": values.tolist(), "fit": {"window_ps": [float(times_ps[lo]), float(times_ps[hi])], "slope_angstrom2_per_ps": float(slope),
            "diffusion_m2_s": float(diffusion) if diffusive else None, "loglog_exponent": exponent, "diffusive_window": diffusive}, "remove_drift": remove_drift,
            "time_step_fs": time_step_fs, "source": {"data": data_name, "trajectory": dump_name}, "warnings": warnings})


def free_volume(directory, probes=(0.0, 1.0, 1.5), spacing=0.25, frame="last"):
    """Occupied, accessible and occupiable volume fractions for each probe radius, and Bondi's FFV."""
    _require_kernel()
    spacing = _number(spacing, "spacing", 0.05, 1.0)
    if not isinstance(probes, (list, tuple)) or not 1 <= len(probes) <= 8:
        raise ValueError("탐침 반지름은 1~8개 입력하세요.")
    probes = [_number(p, "탐침 반지름", 0, 5) for p in probes]
    if frame == "last":
        # The final data file is the last structure, even when the dump exceeds MAX_FRAMES.
        data_path, _ = job_files(directory)
        data = read_data(data_path)
        data_name, dump_name = data_path.name, None
        chosen = {"length": data["length"], "wrapped": data["atoms"]["position"] - data["lo"]}
    else:
        frame = _integer(frame, "frame", 0, MAX_FRAMES - 1)
        data, frames, data_name, dump_name = _frames(directory, 1, frame + 1)
        if frame >= len(frames):
            raise ValueError(f"프레임 {frame}이 없습니다. 프레임 수: {len(frames)}")
        chosen = frames[frame]
    elements = _elements(data)
    radii = np.array([VDW[e] for e in elements])
    length = [float(v) for v in chosen["length"]]
    volume = float(np.prod(length))
    fine_grid = [max(1, math.ceil(v / spacing)) for v in length]
    grid_points = math.prod(fine_grid)
    stencil_work = sum(grid_points * math.prod(2 * math.floor(p / (v / n)) + 1 for v, n in zip(length, fine_grid)) for p in probes)
    if grid_points > MAX_GRID_POINTS or stencil_work > 30000000000:
        raise ValueError("자유 부피 격자 계산량이 너무 큽니다. 격자 간격을 늘리거나 탐침 수·반지름을 줄이세요.")
    rows, occupied = [], None
    for probe in probes:
        result = native.free_volume(np.ascontiguousarray(chosen["wrapped"]), radii, length, float(probe), float(spacing), 0.1, 100)
        coarse = native.free_volume(np.ascontiguousarray(chosen["wrapped"]), radii, length, float(probe), 2.0 * spacing, 0.1, 100)
        occupied = result["occupied_fraction"]
        rows.append({"probe_radius_angstrom": float(probe), "accessible_fraction": result["accessible_fraction"],
                     "occupiable_fraction": result["occupiable_fraction"],
                     "accessible_volume_angstrom3": result["accessible_fraction"] * volume,
                     "occupiable_volume_angstrom3": result["occupiable_fraction"] * volume,
                     "coarse_accessible_fraction": coarse["accessible_fraction"],
                     "coarse_occupiable_fraction": coarse["occupiable_fraction"],
                     "accessible_fraction_absolute_difference": abs(result["accessible_fraction"] - coarse["accessible_fraction"]),
                     "occupiable_fraction_absolute_difference": abs(result["occupiable_fraction"] - coarse["occupiable_fraction"])})
        histogram = np.asarray(result["surface_distance_histogram"])
    mass = sum(data["masses"][t] for t in data["atoms"]["type"])
    difference = abs(occupied - coarse["occupied_fraction"])
    comparison = {"coarse_grid": coarse["grid"], "coarse_spacing_angstrom": 2.0 * spacing,
                  "fine_spacing_angstrom": [v / n for v, n in zip(length, result["grid"])],
                  "coarse_actual_spacing_angstrom": [v / n for v, n in zip(length, coarse["grid"])],
                  "coarse_occupied_fraction": coarse["occupied_fraction"],
                  "occupied_fraction_absolute_difference": difference, "bondi_ffv_absolute_difference": 1.3 * difference,
                  "note": "Absolute differences between requested spacings h and 2h are a sensitivity diagnostic, not an error bound or proof of convergence."}
    return _finite_result({"analysis": "free_volume", "box_angstrom": length, "density_g_cm3": mass / 6.02214076e23 / volume * 1e24,
            "grid": result["grid"], "spacing_angstrom": spacing, "occupied_fraction": occupied,
            "fractional_free_volume_probe0": 1.0 - occupied,
            # Bondi: FFV = (V - 1.3 V_w) / V with the van der Waals volume V_w.
            "bondi_ffv": 1.0 - 1.3 * occupied, "probes": rows, "grid_comparison": comparison,
            "surface_distance": {"bin_angstrom": 0.1, "counts": histogram.tolist()},
            "radii": "Bondi van der Waals radii", "source": {"data": data_name, "trajectory": dump_name, "frame": frame},
            "warnings": ["Probe-occupiable volume can miss narrow accessible pockets on a coarse grid. Compare h and 2h and refine further; a small two-grid difference does not establish convergence.",
                         "Accessible volume counts all geometrically accessible probe centres, including isolated pockets; it does not establish a connected transport pathway."]})


def widom(directory, gas="CO2", temperature=None, insertions=20000, every=1, max_frames=200, cutoff=None, seed=1):
    """Widom insertion into GAFF2 cells: mu_ex = -RT ln(<V exp(-U/RT)>/<V>), valid for
    fluctuating-volume NPT frames and reducing to the usual mean for constant volume.
    S = (273.15 K / T) <V exp(-U/RT)>/<V> in cm^3(STP) / (cm^3 polymer atm)."""
    _require_kernel()
    if not isinstance(gas, str) or gas not in GASES:
        raise ValueError(f"지원하는 기체: {', '.join(GASES)}")
    insertions = _integer(insertions, "insertions", 1, 200000)
    seed = _integer(seed, "seed", 0, 2 ** 63 - 1)
    data, frames, data_name, dump_name = _frames(directory, every, max_frames)
    if not data["pair"] or data_name == "final.data":
        raise ValueError("Widom 삽입은 12-6 LJ(GAFF2) 셀에서만 계산합니다. PCFF(9-6 LJ)·가교 셀은 지원하지 않습니다.")
    if temperature is None:
        temperature = _metadata(directory).get("temperature_kelvin")
        if temperature is None:
            raise ValueError("온도를 확인할 수 없습니다. temperature를 지정하세요.")
    temperature = _number(temperature, "temperature", 0, 10000, open_low=True)
    sigma_gas, eps_k = GASES[gas]
    eps_gas = eps_k * R_KCAL
    types = data["atoms"]["type"]
    if not set(types) <= data["pair"].keys():
        raise ValueError("일부 원자 타입의 LJ 계수가 없습니다.")
    if len(frames) * insertions > 5000000 or len(frames) * insertions * len(types) > 5000000000:
        raise ValueError("Widom 계산량이 너무 큽니다. 삽입 수 또는 프레임 수를 줄이세요.")
    eps = np.array([data["pair"][t][0] for t in types])
    sig = np.array([data["pair"][t][1] for t in types])
    smallest = min(float(f["length"].min()) for f in frames)
    cutoff = min(12.0, 0.5 * smallest - 1e-6) if cutoff is None else _number(cutoff, "cutoff", 0, 1000000, open_low=True)
    if not 0 < cutoff <= smallest / 2:
        raise ValueError(f"차단 거리는 상자 길이의 절반({smallest / 2:.2f} Å) 이하여야 합니다.")
    positions = np.ascontiguousarray(np.stack([f["wrapped"] for f in frames]))
    boxes = np.ascontiguousarray(np.stack([f["length"] for f in frames]))
    factors = np.asarray(native.widom_insertion(positions, boxes, sig, eps, sigma_gas, eps_gas, float(temperature), int(insertions), cutoff, int(seed), True))
    if not np.isfinite(factors).all() or (factors < 0).any():
        raise ValueError("Widom 볼츠만 인자가 유한하지 않습니다. 온도와 LJ 계수를 확인하세요.")
    volumes = np.prod(boxes, axis=1)
    weights = volumes / volumes.mean()
    mean = float(np.mean(weights * factors))
    # Ratio-estimator linearization; this still assumes statistically independent frames.
    residuals = weights * (factors - mean)
    error = float(residuals.std(ddof=1) / math.sqrt(len(factors))) if len(factors) > 1 else None
    mu = -R_KCAL * temperature * math.log(mean) if mean > 0 else None
    solubility = 273.15 / temperature * mean
    warnings = ["Single-site LJ gas (Bird-Stewart-Lightfoot viscosity parameters) without charges or quadrupoles; the magnitude and direction of error in CO2 and N2 solubilities are not established.",
                "Plain 12-6 LJ with a cutoff and analytic tail correction, not the cell's CHARMM switching; frames are taken as fixed (no polymer relaxation around the gas).",
                "Frame standard errors assume independent frames, exclude uncertainty from rare insertions and model bias, and do not establish convergence. Short unequilibrated polymer cells can give strongly biased solubilities."]
    if mean == 0:
        warnings.append("All sampled Boltzmann factors are zero: the finite-sample estimator cannot determine a finite excess chemical potential or establish zero solubility.")
    if len(frames) == 1:
        warnings.append("Only one structure was sampled; no frame-to-frame standard error can be estimated.")
    if mean > 0 and len(factors) > 1 and error / mean > 0.2:
        warnings.insert(0, f"The frame-to-frame error is {100 * error / mean:.0f}% of <exp(-U/RT)>; use more frames or insertions.")
    return _finite_result({"analysis": "widom", "gas": gas, "gas_lj": {"sigma_angstrom": sigma_gas, "epsilon_over_k_kelvin": eps_k}, "temperature_kelvin": temperature,
            "frames": len(frames), "insertions_per_frame": int(insertions), "cutoff_angstrom": cutoff,
            "boltzmann_factor": {"mean": mean, "standard_error": error},
            "estimator": {"mean": "<V exp(-U/RT)> / <V>", "volume_weighted": True,
                          "standard_error": "Independent-frame ratio linearization: SEM((V/<V>) * (factor - mean)); no autocorrelation correction"},
            "excess_chemical_potential_kcal_mol": mu,
            "solubility_cm3stp_per_cm3_atm": solubility,
            "solubility_standard_error": 273.15 / temperature * error if error is not None else None,
            "seed": seed, "tail_correction": True,
            "source": {"data": data_name, "trajectory": dump_name}, "warnings": warnings})


ANALYSES = {"rdf": rdf, "msd": msd, "free_volume": free_volume, "widom": widom}
