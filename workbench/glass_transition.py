"""Glass transition from a stepwise NPT cooling scan of a completed bulk or crosslinked cell.

The source cell is held at the top temperature, then at each lower temperature for a fixed number
of steps; the second half of each hold gives that temperature's mean specific volume.  Tg is where
the asymptotes of a hyperbola fitted to specific volume against temperature intersect (Patrone et
al., Polymer 87 (2016) 246-259); a continuous two-line fit is reported beside it.

MD cooling rates are about 1e9-1e12 K/s, many decades faster than calorimetry, so the simulated Tg
is expected to lie above a DSC value.  No rate (WLF) conversion is applied.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np

STYLE_COMMANDS = {"pair_style", "bond_style", "angle_style", "dihedral_style", "improper_style", "special_bonds", "pair_modify", "dielectric"}
SOURCES = {
    "gaff2": {"data": "eq3_last.data", "styles": "eq3.in", "shake": True, "force_field": "GAFF2_mod (RadonPy cell)"},
    "pcff": {"data": "final.data", "styles": "polymer.params", "shake": True, "force_field": "PCFF (EMC 9.4.4 typing, cff91 class2)"},
    "crosslink": {"data": "final.data", "styles": "polymer.params", "shake": False, "force_field": "PCFF (EMC 9.4.4 typing, cff91 class2)"},
}
MIN_TEMPERATURES = 6
MAX_TEMPERATURES = 81
BOOTSTRAP_SAMPLES = 400
DSC_RATE_K_PER_S = 10 / 60


def temperatures(high, low, step):
    count = int(np.floor((float(high) - float(low)) / float(step) + 1e-9)) + 1
    return [round(float(high) - float(step) * i, 6) for i in range(count)]


def hyperbola(params, x):
    """y0 + m1 (x - x0) + (m2 - m1)/2 [(x - x0) + sqrt((x - x0)^2 + 4 e^c)]: slope m1 below x0, m2 above."""
    y0, x0, m1, m2, c = params
    d = np.asarray(x, dtype=float) - x0
    return y0 + m1 * d + 0.5 * (m2 - m1) * (d + np.sqrt(d * d + 4.0 * np.exp(c)))


def fit_bilinear(x, y):
    """Continuous two-line fit; the break is searched on a 0.1 K grid with at least three points per side."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    xs = np.sort(x)
    best = None
    for b in np.arange(xs[2], xs[-3] + 1e-9, 0.1):
        design = np.column_stack([np.ones_like(x), np.minimum(x - b, 0.0), np.maximum(x - b, 0.0)])
        coef = np.linalg.lstsq(design, y, rcond=None)[0]
        rss = float(np.sum((design @ coef - y) ** 2))
        if best is None or rss < best[0]:
            best = (rss, float(b), coef)
    rss, b, coef = best
    return {"tg_kelvin": b, "value_at_tg": float(coef[0]), "slope_glass": float(coef[1]), "slope_melt": float(coef[2]), "rss": rss}


def fit_hyperbola(x, y, start=None):
    """Least-squares hyperbola; starts from the two-line fit at three curvatures unless a start is given."""
    from scipy.optimize import least_squares
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    span = float(x.max() - x.min())
    lower = [-np.inf, float(x.min()), -np.inf, -np.inf, 2 * np.log(span * 1e-3)]
    upper = [np.inf, float(x.max()), np.inf, np.inf, 2 * np.log(span)]
    if start is not None:
        starts = [np.clip(start, lower, upper)]
    else:
        line = fit_bilinear(x, y)
        starts = [[line["value_at_tg"], line["tg_kelvin"], line["slope_glass"], line["slope_melt"], 2 * np.log(span * f)] for f in (0.01, 0.05, 0.15)]
    best = None
    for p0 in starts:
        fit = least_squares(lambda p: hyperbola(p, x) - y, p0, bounds=(lower, upper), x_scale="jac")
        if best is None or fit.cost < best.cost:
            best = fit
    y0, x0, m1, m2, c = (float(v) for v in best.x)
    return {"tg_kelvin": x0, "value_at_tg": float(hyperbola(best.x, [x0])[0]), "slope_glass": m1, "slope_melt": m2,
            "curvature_kelvin": float(np.exp(c / 2)), "rss": float(2 * best.cost), "params": [y0, x0, m1, m2, c],
            "at_bound": bool(np.isclose(x0, lower[1]) or np.isclose(x0, upper[1]))}


def profile_interval(x, y, fit, level=0.95):
    """Profile-likelihood interval for Tg: x0 values whose best RSS stays within the F(1, n-5) bound."""
    from scipy.optimize import least_squares
    from scipy.stats import f as fisher
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    n = len(x)
    limit = fit["rss"] * (1 + fisher.ppf(level, 1, n - 5) / (n - 5))
    y0, x0, m1, m2, c = fit["params"]
    span = float(x.max() - x.min())
    bounds = ([-np.inf, -np.inf, -np.inf, 2 * np.log(span * 1e-3)], [np.inf, np.inf, np.inf, 2 * np.log(span)])
    ends = []
    for direction, edge in ((-1, float(x.min())), (1, float(x.max()))):
        start, previous, end = np.array([y0, m1, m2, c]), (x0, fit["rss"]), edge
        for t in np.arange(x0 + direction * span / 300, edge + direction * 1e-9, direction * span / 300):
            free = least_squares(lambda q: hyperbola([q[0], t, q[1], q[2], q[3]], x) - y, np.clip(start, *bounds), bounds=bounds, x_scale="jac")
            rss = float(2 * free.cost)
            if rss > limit:
                end = previous[0] + (t - previous[0]) * (limit - previous[1]) / (rss - previous[1])
                break
            start, previous = free.x, (t, rss)
        ends.append(float(end))
    return {"interval_kelvin": ends, "level": level, "reaches_scan_edge": [bool(np.isclose(ends[0], x.min())), bool(np.isclose(ends[1], x.max()))],
            "method": "profile likelihood: refit the other four parameters at fixed Tg, F(1, n-5) threshold on the residual sum of squares"}


def bootstrap(x, y, fit, samples=BOOTSTRAP_SAMPLES, seed=1):
    """Residual bootstrap of the hyperbola (residuals rescaled for the five fitted parameters)."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    fitted = hyperbola(fit["params"], x)
    residual = (y - fitted) * np.sqrt(len(x) / max(1, len(x) - 5))
    residual -= residual.mean()
    rng = np.random.default_rng(seed)
    tg, change = [], []
    for _ in range(samples):
        refit = fit_hyperbola(x, fitted + rng.choice(residual, size=len(x), replace=True), start=fit["params"])
        tg.append(refit["tg_kelvin"])
        change.append(refit["slope_melt"] - refit["slope_glass"])
    tg, change = np.asarray(tg), np.asarray(change)
    return {"samples": samples, "seed": seed, "tg_interval_95_kelvin": [float(v) for v in np.percentile(tg, [2.5, 97.5])],
            "tg_std_kelvin": float(tg.std(ddof=1)), "slope_change_interval_95": [float(v) for v in np.percentile(change, [2.5, 97.5])],
            "method": "residual bootstrap with the fitted hyperbola held as the mean; reflects scatter about this one scan only"}


def analyze(stages, step_kelvin, stage_steps, timestep_fs=1.0, seed=1, samples=BOOTSTRAP_SAMPLES):
    """Tg, expansion coefficients and diagnostics from per-temperature means of one cooling scan."""
    x = np.array([s["temperature_mean_kelvin"] for s in stages], dtype=float)
    y = np.array([s["specific_volume_cm3_g"] for s in stages], dtype=float)
    if len(x) < MIN_TEMPERATURES or not (np.all(np.isfinite(x)) and np.all(np.isfinite(y))):
        raise ValueError(f"A Tg fit needs at least {MIN_TEMPERATURES} finite temperature holds")
    hyper = fit_hyperbola(x, y)
    line = fit_bilinear(x, y)
    profile = profile_interval(x, y, hyper)
    spread = bootstrap(x, y, hyper, samples, seed)
    rate = float(step_kelvin) / (int(stage_steps) * float(timestep_fs) * 1e-15)
    v_tg = hyper["value_at_tg"]
    tg = hyper["tg_kelvin"]
    low, high = profile["interval_kelvin"]
    below, above = int(np.sum(x < tg)), int(np.sum(x > tg))
    warnings = [f"Cooling rate {rate:.2g} K/s is about {rate / DSC_RATE_K_PER_S:.0e} times a 10 K/min DSC scan; simulated Tg is expected "
                "to be higher than a calorimetric Tg, and no rate (WLF) conversion is applied.",
                "One cooling scan of one cell: the profile and bootstrap intervals reflect scatter about this fit, not cell-to-cell "
                "or run-to-run variation, which needs independent replicas.",
                "Short oligomer chains lower Tg through chain ends (Flory-Fox); compare cells of similar chain length only."]
    resolved = True
    if hyper["at_bound"] or low <= x.min() + float(step_kelvin) or high >= x.max() - float(step_kelvin):
        resolved = False
        warnings.insert(0, "Tg or its profile interval reaches the edge of the scanned range; widen the scan.")
    if min(below, above) < 3:
        resolved = False
        warnings.insert(0, f"Only {min(below, above)} temperatures lie on one side of Tg; at least three per side are needed.")
    change = spread["slope_change_interval_95"]
    if change[0] <= 0 <= change[1] or hyper["slope_melt"] <= 1.1 * hyper["slope_glass"]:
        resolved = False
        warnings.insert(0, "No clear change in expansion slope across the scan; the fitted Tg is not meaningful.")
    return {"method": "hyperbola fit of specific volume vs temperature (asymptote intersection; Patrone et al., Polymer 87 (2016) 246)",
            "tg_kelvin": tg, "tg_interval_95_kelvin": [low, high], "transition_resolved": resolved,
            "hyperbola": {k: hyper[k] for k in ("tg_kelvin", "slope_glass", "slope_melt", "curvature_kelvin", "rss", "params")},
            "bilinear": {**line, "method": "continuous two-line least squares, break on a 0.1 K grid"},
            "method_difference_kelvin": abs(line["tg_kelvin"] - tg), "profile": profile, "bootstrap": spread,
            "expansion_glass_per_kelvin": hyper["slope_glass"] / v_tg, "expansion_melt_per_kelvin": hyper["slope_melt"] / v_tg,
            "specific_volume_at_tg_cm3_g": v_tg, "cooling_rate_k_per_s": rate, "cooling_rate_k_per_ns": rate * 1e-9,
            "temperatures_below_tg": below, "temperatures_above_tg": above, "warnings": warnings}


def style_lines(text, stop_at_read_data):
    """Force-field style commands of the source run, in order; anything else is ignored."""
    lines = []
    for raw in text.splitlines():
        words = raw.split("#", 1)[0].split()
        if not words:
            continue
        if words[0] == "read_data" and stop_at_read_data:
            break
        if words[0] in STYLE_COMMANDS:
            lines.append(" ".join(words))
    if not any(line.startswith("pair_style") for line in lines):
        raise ValueError("The source run has no pair_style command to reuse")
    return lines


def scan_input(styles, temps, stage_steps, seed, shake):
    thermo = max(10, int(stage_steps) // 100)
    lines = ["# Polymer Lab Tg scan: hold at the top temperature, then stepwise NPT cooling at 1 atm",
             "units real", "atom_style full", "boundary p p p",
             "variable cutoff index 9.5", "variable charge_cutoff index 9.5", *styles,
             "read_data source.data", "kspace_style pppm 1e-6", "neighbor 2.0 bin", "neigh_modify delay 0 every 1 check yes",
             "timestep 1.0", "thermo_style custom step temp press density vol pe", "thermo_modify flush yes", f"thermo {thermo}"]
    if shake:
        lines.append("fix shake all shake 1e-4 1000 0 m 1.0")
    lines += [f"velocity all create {temps[0]} {seed} mom yes rot yes dist gaussian", "log tg_hold.log",
              f"fix hold all npt temp {temps[0]} {temps[0]} 100.0 iso 1.0 1.0 1000.0", f"run {stage_steps}", "unfix hold", "log tg_scan.log"]
    for t in temps:
        lines += [f"fix hold all npt temp {t} {t} 100.0 iso 1.0 1.0 1000.0", f"run {stage_steps}", "unfix hold"]
    lines.append("write_data tg_final.data")
    return "\n".join(lines) + "\n"


def thermo_runs(path):
    """Thermo rows of each run in a LAMMPS log, as lists of dicts keyed by column name."""
    runs, columns = [], None
    for line in Path(path).read_text(errors="replace").splitlines():
        words = line.split()
        if words[:1] == ["Step"]:
            columns, rows = words, []
            runs.append(rows)
        elif columns and line.startswith("Loop time"):
            columns = None
        elif columns and len(words) == len(columns):
            try:
                runs[-1].append(dict(zip(columns, map(float, words))))
            except ValueError:
                continue
    return runs


def stage_means(path, temps, stage_steps):
    runs = thermo_runs(path)
    if len(runs) < len(temps):
        raise RuntimeError(f"The scan log has {len(runs)} of {len(temps)} temperature holds")
    stages = []
    for target, rows in zip(temps, runs):
        start = rows[0]["Step"]
        kept = [r for r in rows if r["Step"] > start + stage_steps / 2]
        if len(kept) < 8:
            raise RuntimeError("Too few thermo rows in the second half of a hold")
        density = np.array([r["Density"] for r in kept])
        volume = 1.0 / density
        blocks = np.array([b.mean() for b in np.array_split(volume, 4)])
        stages.append({"temperature_set_kelvin": target, "temperature_mean_kelvin": float(np.mean([r["Temp"] for r in kept])),
                       "density_g_cm3": float(density.mean()), "specific_volume_cm3_g": float(volume.mean()),
                       "specific_volume_block_sem": float(blocks.std(ddof=1) / 2), "rows": len(kept)})
    return stages


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_kind(source_config):
    if source_config.get("route") != "polymer_atomistic" or source_config.get("atomistic_mode") not in {"bulk", "crosslink"}:
        raise ValueError("Tg 스캔은 완료된 전원자 벌크 셀 또는 가교 작업에서 시작합니다.")
    if source_config["atomistic_mode"] == "crosslink":
        return "crosslink"
    return "pcff" if source_config.get("atomistic_forcefield") == "pcff" else "gaff2"


def required_files(source_config):
    spec = SOURCES[source_kind(source_config)]
    return [spec["data"], spec["styles"]]


def scan(config, output, emit, run_lammps):
    """Copy the source cell, run the stepwise cooling scan and write tg_complete.json."""
    source = Path(output).parent / config["source_job_id"]
    source_config = json.loads((source / "config.json").read_text())
    kind = source_kind(source_config)
    spec = SOURCES[kind]
    files = {}
    for name in (spec["data"], spec["styles"]):
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"원본 작업에 {name} 파일이 없습니다.")
        files[name] = _sha256(path)
    shutil.copyfile(source / spec["data"], output / "source.data")
    styles = style_lines((source / spec["styles"]).read_text(errors="replace"), stop_at_read_data=kind == "gaff2")
    kept = {k: v for k, v in source_config.items() if k not in {"pdb_text", "a3m_text", "forcefield_itp_text", "mdp_text", "engine_input"}}
    (output / "tg_source.json").write_text(json.dumps({"job_id": config["source_job_id"], "kind": kind, "files_sha256": files,
                                                        "config": kept}, indent=2))
    temps = temperatures(config["tg_high_kelvin"], config["tg_low_kelvin"], config["tg_step_kelvin"])
    steps = int(config["tg_stage_steps"])
    seed = int(config["seed"]) % 900000 + 1000
    (output / "in.tg_scan").write_text(scan_input(styles, temps, steps, seed, spec["shake"]))
    emit("simulate", 0.3, f"LAMMPS: hold at {temps[0]:g} K, then {len(temps)} NPT holds down to {temps[-1]:g} K ({steps} steps each)")
    run_lammps(output, "in.tg_scan")
    stages = stage_means(output / "tg_scan.log", temps, steps)
    emit("analyze", 0.9, "Hyperbola and two-line fits of specific volume vs temperature")
    result = analyze(stages, config["tg_step_kelvin"], steps, seed=int(config["seed"]))
    complete = {"source": {"job_id": config["source_job_id"], "kind": kind, "force_field": spec["force_field"], "files_sha256": files},
                "protocol": {"temperatures_kelvin": temps, "steps_per_temperature": steps, "hold_steps_at_top": steps, "time_step_fs": 1.0,
                             "ensemble": "NPT, Nose-Hoover, 1 atm, Tdamp 100 fs, Pdamp 1000 fs", "averaging": "second half of each hold",
                             "shake": spec["shake"], "lammps_velocity_seed": seed},
                "stages": stages, **result}
    (output / "tg_complete.json").write_text(json.dumps(complete, indent=2))
    return complete
