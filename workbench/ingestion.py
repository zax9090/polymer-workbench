"""Explicit-unit experimental imports. Curve estimates are review drafts, never labels."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import Path

import numpy as np

from .research_store import validate_measurement


def parse_measurements_csv(text: str) -> list[dict]:
    if len(text.encode("utf-8")) > 5_000_000:
        raise ValueError("Measurement CSV exceeds 5 MB")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    required = {"property", "value", "unit", "method", "source", "origin"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError("CSV requires property,value,unit,method,source,origin and material/material_id/job_id")
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError("Duplicate CSV column names are ambiguous")
    records = []
    for index, raw in enumerate(reader, start=2):
        if index > 10001:
            raise ValueError("CSV exceeds 10000 measurement rows")
        try:
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError("CSV field count differs from header")
            record = {k: v.strip() for k, v in raw.items() if v.strip()}
            for column in ("material", "conditions"):
                alias = column + "_json"
                value = record.pop(alias, None) or record.get(column)
                if value:
                    record[column] = json.loads(value)
            if record.get("repeat_smiles") and not record.get("material"):
                record["material"] = {"repeat_smiles": record.pop("repeat_smiles")}
            if "verified" in record:
                if record["verified"].lower() not in {"true", "false"}:
                    raise ValueError("verified must be true or false")
            # A CSV flag cannot replace the explicit preview/review step.
            record["verified"] = False
            if record.get("source", "").startswith("{"):
                record["source"] = json.loads(record["source"])
            records.append(validate_measurement(record))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"CSV row {index}: {exc}") from exc
    if not records:
        raise ValueError("CSV contains no measurements")
    return records


def _points(text, x_column, y_column):
    if len(text.encode("utf-8")) > 10_000_000:
        raise ValueError("Curve CSV exceeds 10 MB")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames or x_column not in reader.fieldnames or y_column not in reader.fieldnames:
        raise ValueError(f"Curve CSV needs {x_column!r} and {y_column!r} columns")
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError("Duplicate curve CSV column names are ambiguous")
    points = []
    for line, row in enumerate(reader, start=2):
        if line > 100002:
            raise ValueError("Curve exceeds 100000 data points")
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"Curve CSV field count differs from header at row {line}")
        try:
            x, y = float(row[x_column]), float(row[y_column])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Non-numeric curve coordinate at row {line}") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError(f"Nonfinite curve coordinate at row {line}")
        points.append([x, y])
    if len(points) < 3:
        raise ValueError("A curve requires at least three points")
    data = np.asarray(points)
    return data[:, 0], data[:, 1]


def _curve(text, x_column, y_column):
    x, y = _points(text, x_column, y_column)
    if np.any(np.diff(x) <= 0):
        raise ValueError("Choose one monotonic loading/heating branch with strictly increasing x values")
    return x, y


def _new_draft(kind, text, options, x, y, x_column, y_column):
    step = max(1, len(x) // 500)
    return {"kind": kind, "review_required": True, "verified": False, "origin": "experimental",
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "options": options,
            "points": len(x), "measurement_draft": None,
            "preview": {"x": x[::step].tolist(), "y": y[::step].tolist(), "x_column": x_column, "y_column": y_column}, "warnings": []}


def _smoothed_derivative(x, y, span):
    """Savitzky-Golay (quadratic) smoothing and first derivative over `span` x units, on a uniform resampling of x."""
    from scipy.signal import savgol_filter
    grid = np.linspace(x[0], x[-1], max(len(x), 101))
    step = float(grid[1] - grid[0])
    values = np.interp(grid, x, y)
    window = max(5, int(round(span / step)) | 1)
    if window > len(grid):
        window = len(grid) if len(grid) % 2 else len(grid) - 1
    smooth = savgol_filter(values, window, 2)
    slope = savgol_filter(values, window, 2, deriv=1, delta=step)
    return grid, smooth, slope, {"method": "Savitzky-Golay, quadratic, on a uniform resampling", "window_points": window,
                                 "span_input_units": window * step}


def _window(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must contain [lower, upper] in the uploaded x-axis units")
    low, high = map(float, value)
    if not np.isfinite([low, high]).all() or high <= low:
        raise ValueError(f"Invalid {label}")
    return low, high


def _fit(x, y, bounds, label):
    low, high = _window(bounds, label)
    mask = (x >= low) & (x <= high)
    if mask.sum() < 3:
        raise ValueError(f"{label} must contain at least three distinct data points")
    coefficients = np.polyfit(x[mask], y[mask], 1)
    fitted = np.polyval(coefficients, x[mask])
    sse = float(np.sum((y[mask] - fitted) ** 2))
    total = float(np.sum((y[mask] - np.mean(y[mask])) ** 2))
    r2 = 1 - sse / total if total > 0 else None
    return coefficients, {"window": [low, high], "points": int(mask.sum()), "slope": float(coefficients[0]),
                          "intercept": float(coefficients[1]), "r_squared": r2}


def _text(options, key, label, limit=100):
    value = options.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be stated explicitly")
    return value.strip()


def _positive(options, key, label, upper=None):
    value = options.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or (upper is not None and value >= upper):
        raise ValueError(f"{label} must be a finite number in (0, {upper})" if upper else f"{label} must be a finite positive number")
    return float(value)


def _trapezoid_weights(x):
    # Slice widths for non-uniform sampling; uniform sampling is not assumed.
    weights = np.zeros_like(x)
    widths = np.diff(x)
    weights[:-1] += widths / 2
    weights[1:] += widths / 2
    return weights


def _tga(x, y, options, draft):
    temperature_unit = options.get("temperature_unit")
    if temperature_unit not in {"K", "C", "°C"}:
        raise ValueError("temperature_unit must explicitly be K or C")
    mass_unit = options.get("mass_unit")
    if mass_unit not in {"mg", "%", "fraction"}:
        raise ValueError("mass_unit must explicitly be mg, % or fraction")
    loss = _positive(options, "mass_loss_percent", "mass_loss_percent", upper=100)
    rate = _positive(options, "heating_rate_K_min", "heating_rate_K_min")
    atmosphere = _text(options, "atmosphere", "TGA atmosphere (for example N2 or air)")
    reference = options.get("reference_temperature")
    if isinstance(reference, bool) or not isinstance(reference, (int, float)) or not math.isfinite(reference):
        raise ValueError("reference_temperature for 100% mass normalization must be stated in the uploaded temperature unit")
    reference = float(reference)
    if not x[0] <= reference < x[-1]:
        raise ValueError("reference_temperature must lie inside the uploaded heating range")
    if np.any(y < 0):
        raise ValueError("Negative mass values indicate an uncorrected blank/buoyancy offset; correct the curve first")
    reference_mass = float(np.interp(reference, x, y))
    if reference_mass <= 0:
        raise ValueError("Mass at the reference temperature must be positive")
    after = x > reference
    tx = np.concatenate([[reference], x[after]])
    remaining = np.concatenate([[100.0], y[after] / reference_mass * 100])
    threshold = 100 - loss
    below = np.flatnonzero(remaining <= threshold)
    if not len(below):
        raise ValueError(f"The curve does not lose {loss:g}% of the reference mass in the supplied range")
    i = int(below[0])
    # First crossing only; later re-crossings after mass gain are not averaged in.
    crossing = float(tx[i - 1] + (remaining[i - 1] - threshold) * (tx[i] - tx[i - 1]) / (remaining[i - 1] - remaining[i]))
    offset = 0 if temperature_unit == "K" else 273.15
    if crossing + offset <= 0:
        raise ValueError("TGA temperature is below absolute zero")
    if np.max(remaining[:i]) > 100.5:
        draft["warnings"].append("Mass rises above the reference before the crossing; check buoyancy, oxidation or a premature reference choice.")
    analysis = {"temperature_unit_input": temperature_unit, "mass_unit_input": mass_unit,
                "reference_temperature_input": reference, "reference_mass_input": reference_mass,
                "crossing_input_units": crossing,
                "bracketing_points": [[float(tx[i - 1]), float(remaining[i - 1])], [float(tx[i]), float(remaining[i])]],
                "final_temperature_K": float(x[-1] + offset), "remaining_mass_percent_at_final": float(y[-1] / reference_mass * 100)}
    conditions = {"mass_loss_percent": loss, "atmosphere": atmosphere, "heating_rate_K_min": rate,
                  "reference_temperature_K": round(reference + offset, 2)}
    dtg = _tga_derivative(tx, remaining, options, offset)
    analysis["dtg"] = dtg
    definition = options.get("td_definition", "mass_loss")
    if definition not in {"mass_loss", "onset"}:
        raise ValueError("td_definition must be mass_loss or onset")
    if definition == "onset":
        # ISO 11358-1 extrapolated onset: starting-mass line meets the tangent at the steepest mass loss.
        onset = dtg["extrapolated_onset_K"]
        if onset is None or onset - offset <= reference:
            raise ValueError("The extrapolated onset falls at or before the reference temperature; widen the curve or choose td_definition mass_loss")
        decomposition = {"property": "decomposition_temperature", "value": onset, "unit": "K",
                         "method": "TGA extrapolated onset: intersection of the starting-mass line with the tangent at the maximum mass-loss rate",
                         "conditions": {"atmosphere": atmosphere, "heating_rate_K_min": rate, "reference_temperature_K": conditions["reference_temperature_K"],
                                        "onset_baseline": dtg["onset_baseline"]["source"]}, "analysis": analysis}
    else:
        decomposition = {"property": "decomposition_temperature", "value": crossing + offset, "unit": "K",
                         "method": "TGA first-crossing temperature at a stated mass loss relative to the mass at a stated reference temperature",
                         "conditions": conditions, "analysis": analysis}
    drafts = [decomposition,
              {"property": "dtg_peak_temperature", "value": dtg["peak_K"], "unit": "K",
               "method": "TGA temperature of the maximum mass-loss rate (Savitzky-Golay derivative)",
               "conditions": {"atmosphere": atmosphere, "heating_rate_K_min": rate}, "analysis": analysis}]
    if options.get("residue_temperature") is not None:
        residue_at = options["residue_temperature"]
        if isinstance(residue_at, bool) or not isinstance(residue_at, (int, float)) or not tx[0] < residue_at <= tx[-1]:
            raise ValueError("residue_temperature must lie after the reference temperature and inside the uploaded range")
        residue = float(np.interp(residue_at, tx, remaining)) / 100
        analysis["residue_fraction"] = residue
        drafts.append({"property": "char_yield", "value": min(max(residue, 0.0), 1.0), "unit": "fraction",
                       "method": "TGA remaining mass at a stated temperature relative to the mass at a stated reference temperature",
                       "conditions": {"atmosphere": atmosphere, "heating_rate_K_min": rate, "reference_temperature_K": conditions["reference_temperature_K"],
                                      "residue_temperature_K": round(float(residue_at) + offset, 2)}, "analysis": analysis})
        if residue > 1:
            draft["warnings"].append("The residue exceeds the reference mass (oxidation or buoyancy); char yield was capped at 1.")
        if any(word in atmosphere.lower() for word in ("air", "o2", "oxygen")):
            draft["warnings"].append("In an oxidizing atmosphere the residue is ash or oxide, not char.")
    draft["fit"] = analysis
    draft["measurement_drafts"] = drafts
    if len(dtg["peaks"]) > 1:
        draft["warnings"].append(f"The DTG curve shows {len(dtg['peaks'])} mass-loss steps; the DTG peak and onset refer to the fastest one.")
    draft["warnings"].append("Td depends on heating rate, atmosphere, sample mass, pan and buoyancy correction; no blank subtraction is applied.")


def _tga_derivative(tx, remaining, options, offset):
    """DTG (-d mass%/dT), its peaks, and the ISO 11358-1 extrapolated onset of the fastest step."""
    from scipy.signal import find_peaks
    span = _positive(options, "dtg_smoothing", "dtg_smoothing") if options.get("dtg_smoothing") is not None else 5.0
    grid, smooth, slope, smoothing = _smoothed_derivative(tx, remaining, span)
    rate = -slope  # % per kelvin (identical per degree Celsius)
    search = np.ones_like(grid, dtype=bool)
    window = None
    if options.get("dtg_window") is not None:
        window = _window(options["dtg_window"], "dtg_window")
        search = (grid >= window[0]) & (grid <= window[1])
        if search.sum() < 3:
            raise ValueError("dtg_window must contain at least three points after the reference temperature")
    if np.max(rate[search]) <= 0:
        raise ValueError("No mass loss in the DTG search range")
    main = int(np.flatnonzero(search)[np.argmax(rate[search])])
    found, props = find_peaks(rate, prominence=0.05 * float(np.max(rate)))
    peaks = [{"temperature_K": float(grid[j] + offset), "rate_percent_per_K": float(rate[j])} for j in found]
    baseline = {"level_percent": 100.0, "source": "reference mass"}
    if options.get("onset_baseline_window") is not None:
        low, high = _window(options["onset_baseline_window"], "onset_baseline_window")
        mask = (tx >= low) & (tx <= high)
        if mask.sum() < 3:
            raise ValueError("onset_baseline_window must contain at least three points after the reference temperature")
        baseline = {"level_percent": float(np.mean(remaining[mask])), "source": "mean of onset_baseline_window", "window_input_units": [low, high]}
    onset = None
    if slope[main] < 0:
        onset = float(grid[main] + (baseline["level_percent"] - smooth[main]) / slope[main] + offset)
    return {"peak_K": float(grid[main] + offset), "peak_rate_percent_per_K": float(rate[main]), "window_input_units": list(window) if window else None,
            "peaks": peaks, "extrapolated_onset_K": onset, "onset_baseline": baseline, "smoothing": smoothing}


GPC_DETECTORS = {"RI", "UV", "ELSD"}
MOLAR_MASS_FACTORS = {"g/mol": 1.0, "Da": 1.0, "kg/mol": 1e3}


# Baselines under peaks: pybaselines' asymmetrically reweighted penalized least squares (arPLS,
# Baek et al., Analyst 140, 250 (2015)); lam is the smoothness penalty, in squared x-index units.
BASELINE_LAM = 1e6


def _arpls(x, y, lam):
    try:
        import pybaselines
        from pybaselines import Baseline
    except ImportError as exc:
        raise ValueError("Automatic baselines need pybaselines (pip install pybaselines)") from exc
    lam = float(lam)
    if not math.isfinite(lam) or not 1e2 <= lam <= 1e10:
        raise ValueError("baseline_lam must be between 1e2 and 1e10")
    baseline, params = Baseline(x_data=x).arpls(y, lam=lam)
    return baseline, {"method": "arpls", "lam": lam, "library": f"pybaselines {pybaselines.__version__}",
                      "iterations": len(params.get("tol_history", []))}


def _window_excess(x, signal, low, high, baseline=None):
    """Signal minus a baseline from low to high, with the window limits interpolated.

    The baseline is the given array, or the straight line joining the curve at the two limits.
    """
    inside = (x >= low) & (x <= high)
    if inside.sum() < 5:
        raise ValueError("Each integration window needs at least five data points")
    xs = np.concatenate([[low], x[inside], [high]])
    ys = np.interp(xs, x, signal)
    base = np.interp(xs, x, baseline) if baseline is not None else ys[0] + (ys[-1] - ys[0]) * (xs - low) / (high - low)
    return xs, ys - base


def _dsc_melting(x, y, options, draft, temperature_unit):
    """Melting endotherm above an arPLS or linear baseline; optional cold-crystallization exotherm."""
    direction = options.get("endotherm")
    if direction not in {"up", "down"}:
        raise ValueError("endotherm must state whether endothermic heat flow points up or down")
    unit = options.get("heat_flow_unit")
    if unit not in {"W/g", "mW/mg", "mW"}:
        raise ValueError("Melting enthalpy needs heat_flow_unit W/g, mW/mg (both per mass) or mW with sample_mass_mg")
    rate = _positive(options, "heating_rate_k_per_min", "heating_rate_k_per_min", 200)
    low, high = _window(options.get("integration_window"), "integration_window")
    method = options.get("baseline", "arpls")
    if method not in {"arpls", "linear"}:
        raise ValueError("baseline must be arpls or linear")
    signal = y if direction == "up" else -y
    if method == "arpls":
        if options.get("cold_crystallization_window") is not None:
            raise ValueError("With a cold-crystallization exotherm use baseline linear: arPLS takes every point below its curve as baseline and follows the exotherm")
        baseline, fit = _arpls(x, signal, options.get("baseline_lam", BASELINE_LAM))
        description = "an arPLS baseline"
    else:
        baseline, fit = None, {"method": "linear", "description": "straight line joining the curve at the integration limits"}
        description = "a linear baseline between the integration limits"
    tx, excess = _window_excess(x, signal, low, high, baseline)
    if baseline is not None:
        # A window that brackets the peak meets the baseline at both limits. A gap there means a truncated
        # peak or a displaced baseline: an exotherm anywhere in the upload drags arPLS, which treats
        # negative peaks as baseline.
        gap = float(max(abs(excess[0]), abs(excess[-1])) / max(float(np.max(excess)), 1e-12))
        fit["limit_gap_fraction_of_peak"] = gap
        if gap > 0.05:
            draft["warnings"].append(f"At the integration limits the curve is {gap:.0%} of the peak height away from the arPLS baseline: the window cuts the peak, or an exotherm or step elsewhere in the curve displaced the baseline. Widen the window, trim the curve or use baseline linear.")
    area = float(np.trapezoid(excess, tx))  # heat-flow unit x temperature unit
    negative = float(np.trapezoid(np.clip(excess, None, 0), tx))
    if area <= 0:
        raise ValueError("No endothermic area above the baseline inside the integration window")
    if abs(negative) > 0.05 * area:
        draft["warnings"].append("More than 5% of the window lies below the baseline; check the window and the endotherm direction.")
    # dT = beta dt, so area / beta (K/s) is energy per mass: 1 W/g x 1 s = 1 J/g, and 1 mW/mg = 1 W/g.
    to_j_per_g = 1 / (rate / 60.0)
    if unit == "mW":
        to_j_per_g /= _positive(options, "sample_mass_mg", "sample_mass_mg")
    per_mass = area * to_j_per_g
    peak_input = float(tx[int(np.argmax(excess))])
    peak = peak_input + (0 if temperature_unit == "K" else 273.15)
    # Conditions are what replicates share (training pools identical method and conditions); the
    # windows and baseline fit chosen for this curve are kept in analysis.
    conditions = {"heating_rate_k_per_min": rate, "baseline": method}
    report = {"integration_window": [low, high], "endotherm": direction, "heat_flow_unit": unit, "temperature_unit_input": temperature_unit,
              "baseline": fit, "peak_input_units": peak_input, "area_input_units": area}
    drafts = [{"property": "melting_temperature", "value": peak, "unit": "K", "method": f"DSC endotherm peak temperature above {description}",
               "conditions": conditions, "analysis": report},
              {"property": "melting_enthalpy", "value": per_mass, "unit": "J/g", "method": f"DSC endotherm area above {description} divided by the heating rate",
               "conditions": conditions, "analysis": report}]
    cold = 0.0
    if options.get("cold_crystallization_window") is not None:
        c_low, c_high = _window(options["cold_crystallization_window"], "cold_crystallization_window")
        if c_high > low and c_low < high:
            raise ValueError("cold_crystallization_window must not overlap the melting integration window")
        # Linear baseline per exotherm: arPLS treats points below its curve as baseline and would follow the exotherm.
        cx, c_excess = _window_excess(x, signal, c_low, c_high)
        exotherm = -float(np.trapezoid(c_excess, cx))
        if exotherm <= 0:
            raise ValueError("No exothermic area below the baseline inside cold_crystallization_window")
        cold = exotherm * to_j_per_g
        report["cold_crystallization"] = {"window": [c_low, c_high], "enthalpy_j_per_g": cold, "baseline": "linear between the window limits",
                                          "peak_input_units": float(cx[int(np.argmin(c_excess))])}
    if options.get("reference_enthalpy_j_per_g") is not None:
        reference = _positive(options, "reference_enthalpy_j_per_g", "reference_enthalpy_j_per_g")
        fraction = 1.0
        if options.get("polymer_weight_fraction") is not None:
            fraction = _positive(options, "polymer_weight_fraction", "polymer_weight_fraction")
            if fraction > 1:
                raise ValueError("polymer_weight_fraction must be in (0, 1]")
        # Crystallinity of the sample as loaded: Xc = (dHm - dHcc) / (w dH0).
        crystallinity = (per_mass - cold) / (fraction * reference)
        if crystallinity < 0:
            raise ValueError("Cold-crystallization enthalpy exceeds the melting enthalpy; check both windows")
        if crystallinity > 1:
            draft["warnings"].append("Measured enthalpy exceeds the 100%-crystalline reference; crystallinity is not reported.")
        else:
            method_text = "DSC melting enthalpy divided by the stated 100%-crystalline reference enthalpy"
            if cold or fraction != 1.0:
                method_text = "DSC (melting minus cold-crystallization enthalpy) divided by the polymer weight fraction and the stated 100%-crystalline reference enthalpy"
            drafts.append({"property": "crystallinity", "value": crystallinity, "unit": "fraction", "method": method_text,
                           "conditions": {**conditions, "reference_enthalpy_j_per_g": reference},
                           "analysis": {**report, "melting_enthalpy_j_per_g": per_mass, "cold_crystallization_enthalpy_j_per_g": cold, "polymer_weight_fraction": fraction}})
    draft["fit"] = report
    if baseline is not None:
        draft["baseline_preview"] = (baseline if direction == "up" else -baseline)[::max(1, len(x) // 500)].tolist()
    draft["measurement_drafts"] = drafts
    if cold:
        draft["warnings"].append("Melting enthalpy depends on the integration limits, baseline, heating rate and thermal history; overlapping transitions are not separated.")
    else:
        draft["warnings"].append("Melting enthalpy depends on the integration limits, baseline, heating rate and thermal history; cold crystallization and overlapping transitions are not separated.")


TG_DEFINITIONS = {"midpoint": "DSC halfway-heat-flow crossing between two manually selected linear baselines",
                  "inflection": "DSC Tg inflection: maximum slope of the heat-flow step between two manually selected linear baselines",
                  "onset": "DSC Tg extrapolated onset: pre-transition baseline meets the inflection tangent (manually selected linear baselines)",
                  "endset": "DSC Tg extrapolated end: post-transition baseline meets the inflection tangent (manually selected linear baselines)"}


def _dsc_tg(x, y, options, draft, temperature_unit):
    """Tg midpoint, inflection and extrapolated onset/end between two baselines (the ISO 11357-2 construction)."""
    definition = options.get("tg_definition", "midpoint")
    if definition not in TG_DEFINITIONS:
        raise ValueError("tg_definition must be midpoint, inflection, onset or endset")
    before = _window(options.get("baseline_before"), "baseline_before")
    after = _window(options.get("baseline_after"), "baseline_after")
    if before[1] >= after[0]:
        raise ValueError("DSC baselines must be disjoint and ordered before then after transition")
    a, fit_a = _fit(x, y, before, "baseline_before")
    b, fit_b = _fit(x, y, after, "baseline_after")
    transition = (x > before[1]) & (x < after[0])
    tx, ty = x[transition], y[transition]
    if len(tx) < 3:
        raise ValueError("At least three transition points are required between baselines")
    if np.any(np.abs(np.polyval(a - b, tx)) < 1e-10):
        raise ValueError("DSC baseline separation is too small for a Tg midpoint estimate")
    residual = ty - (np.polyval(a, tx) + np.polyval(b, tx)) / 2
    roots = []
    for i in range(len(tx) - 1):
        if residual[i] == 0:
            roots.append(float(tx[i]))
        elif residual[i] * residual[i + 1] < 0:
            roots.append(float(tx[i] - residual[i] * (tx[i + 1] - tx[i]) / (residual[i + 1] - residual[i])))
    if residual[-1] == 0:
        roots.append(float(tx[-1]))
    if len(roots) != 1:
        raise ValueError("Selected DSC region does not have one unambiguous midpoint crossing; refine baseline windows or pre-process with a documented method")
    midpoint = roots[0]
    offset = 0 if temperature_unit == "K" else 273.15
    fit = {"before": fit_a, "after": fit_b, "midpoint_input_units": midpoint}
    if len(tx) >= 5:
        # Steepest point in the step's own direction, so the falling flank of an enthalpy-recovery peak is not chosen.
        span = _positive(options, "derivative_smoothing", "derivative_smoothing") if options.get("derivative_smoothing") is not None else (after[0] - before[1]) / 20
        grid, smooth, slope, smoothing = _smoothed_derivative(tx, ty, span)
        sign = 1.0 if np.polyval(b - a, midpoint) > 0 else -1.0
        i = int(np.argmax(sign * slope))
        ti, yi, si = float(grid[i]), float(smooth[i]), float(slope[i])
        if sign * si > 0:
            values = {"inflection": ti}
            for name, line in (("onset", a), ("endset", b)):
                if abs(line[0] - si) > 1e-12 * max(1.0, abs(si)):
                    values[name] = float((yi - si * ti - line[1]) / (line[0] - si))
            fit.update({"inflection_input_units": ti, "inflection_slope": si, "onset_input_units": values.get("onset"),
                        "endset_input_units": values.get("endset"), "derivative": smoothing})
            if "onset" in values and "endset" in values and not values["onset"] < midpoint < values["endset"]:
                draft["warnings"].append("Onset, midpoint and endset are not in ascending order; check the baseline windows and derivative_smoothing.")
    values = {"midpoint": midpoint, **{k: fit.get(f"{k}_input_units") for k in ("inflection", "onset", "endset")}}
    if values[definition] is None:
        raise ValueError(f"The {definition} construction is undefined for this step (too few points or a tangent parallel to a baseline)")
    kelvin = values[definition] + offset
    if kelvin <= 0:
        raise ValueError("DSC temperature is below absolute zero")
    fit["tg_K"] = {k: v + offset for k, v in values.items() if v is not None}
    unit = options.get("heat_flow_unit")
    if options.get("heating_rate_k_per_min") is not None and unit in {"W/g", "mW/mg", "mW"}:
        rate = _positive(options, "heating_rate_k_per_min", "heating_rate_k_per_min", 200)
        step = abs(float(np.polyval(b - a, midpoint))) / (rate / 60.0)
        if unit == "mW":
            step /= _positive(options, "sample_mass_mg", "sample_mass_mg")
        fit["heat_capacity_step_j_per_g_k"] = step
    draft["fit"] = fit
    draft["measurement_draft"] = {"property": "tg", "value": kelvin, "unit": "K", "method": TG_DEFINITIONS[definition],
                                  "conditions": {"baseline_before": list(before), "baseline_after": list(after), "temperature_unit_input": temperature_unit}}
    draft["warnings"].append("Review heating cycle, rate, thermal history, endothermic overshoot and baseline selection; midpoint Tg is method dependent.")


def _gpc_calibration(options):
    unit = options.get("molar_mass_unit")
    if unit not in MOLAR_MASS_FACTORS:
        raise ValueError("molar_mass_unit must be g/mol, Da or kg/mol")
    order = options.get("calibration_order")
    if isinstance(order, bool) or order not in (1, 2, 3):
        raise ValueError("calibration_order must be 1, 2 or 3")
    points = options.get("calibration")
    if not isinstance(points, list) or not 3 <= len(points) <= 100:
        raise ValueError("calibration needs 3-100 narrow standards as [retention, molar_mass] pairs")
    standards = []
    for point in points:
        if isinstance(point, dict):
            retention, mass = point.get("retention"), point.get("molar_mass")
        elif isinstance(point, (list, tuple)) and len(point) == 2:
            retention, mass = point
        else:
            retention = mass = None
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (retention, mass)) or mass <= 0:
            raise ValueError("Each calibration standard needs a finite retention and a positive molar mass")
        standards.append((float(retention), float(mass) * MOLAR_MASS_FACTORS[unit]))
    standards.sort()
    t = np.array([s[0] for s in standards])
    log_m = np.log10([s[1] for s in standards])
    if np.any(np.diff(t) <= 0):
        raise ValueError("Calibration standards need distinct retention values")
    if len(t) < order + 2:
        raise ValueError(f"A polynomial of order {order} needs at least {order + 2} standards to leave a residual check")
    if np.any(np.diff(log_m) >= 0):
        raise ValueError("Size-exclusion standards must elute in order of decreasing molar mass")
    coefficients = np.polyfit(t, log_m, order)
    fitted = np.polyval(coefficients, t)
    total = float(np.sum((log_m - log_m.mean()) ** 2))
    deviations = (10 ** (log_m - fitted) - 1) * 100
    report = {"order": order, "coefficients_log10_g_mol": coefficients.tolist(),
              "r_squared_log10": 1 - float(np.sum((log_m - fitted) ** 2)) / total if total > 0 else None,
              "max_abs_deviation_percent": float(np.max(np.abs(deviations))),
              "retention_range": [float(t[0]), float(t[-1])],
              "standards": [{"retention": float(a), "molar_mass_g_mol": float(10 ** b), "fit_deviation_percent": float(d)}
                            for a, b, d in zip(t, log_m, deviations)]}
    return coefficients, report


def _gpc(x, y, options, draft):
    x_unit = options.get("x_unit")
    if x_unit not in {"min", "mL"}:
        raise ValueError("x_unit must explicitly be min or mL, matching the calibration retention values")
    detector = options.get("detector")
    if detector not in GPC_DETECTORS:
        raise ValueError("detector must be RI, UV or ELSD")
    standard = _text(options, "calibration_standard", "calibration_standard (for example polystyrene)")
    coefficients, calibration = _gpc_calibration(options)
    start, end = _window(options.get("integration_window"), "integration_window")
    low, high = calibration["retention_range"]
    if start < low or end > high:
        raise ValueError(f"integration_window must stay inside the calibrated retention range [{low:g}, {high:g}]; extrapolation is refused")
    slope = np.polyder(coefficients)
    if np.any(np.polyval(slope, np.linspace(start, end, 2001)) >= 0):
        raise ValueError("The calibration polynomial is not monotonic across the integration window; lower the order or add standards")
    inside = (x >= start) & (x <= end)
    if inside.sum() < 5:
        raise ValueError("integration_window must contain at least five data points")
    tx = x[inside]
    if options.get("baseline_method", "windows") == "arpls":
        curve, baseline_record = _arpls(x, y, options.get("baseline_lam", BASELINE_LAM))
        signal = (y - curve)[inside]
        before = after = None
    else:
        before = _window(options.get("baseline_before"), "baseline_before")
        after = _window(options.get("baseline_after"), "baseline_after")
        if before[1] > start or after[0] < end:
            raise ValueError("Baseline windows must lie before and after the integration window")
        masks = [(x >= a) & (x <= b) for a, b in (before, after)]
        if any(mask.sum() < 3 for mask in masks):
            raise ValueError("Each GPC baseline window must contain at least three data points")
        baseline_mask = masks[0] | masks[1]
        baseline = np.polyfit(x[baseline_mask], y[baseline_mask], 1)
        baseline_record = {"method": "linear fit to the two baseline windows", "slope": float(baseline[0]), "intercept": float(baseline[1])}
        signal = y[inside] - np.polyval(baseline, tx)
    widths = _trapezoid_weights(tx)
    positive = float(np.sum(np.clip(signal, 0, None) * widths))
    negative = float(np.sum(np.clip(-signal, 0, None) * widths))
    if positive <= 0:
        raise ValueError("No positive baseline-corrected signal inside the integration window")
    if negative / (positive + negative) > 0.02:
        raise ValueError("More than 2% of the baseline-corrected area is negative; revise the baseline windows")
    corrected = np.clip(signal, 0, None)
    # Concentration detector: corrected signal × slice width is the mass fraction.
    fraction = corrected * widths / positive
    log_m = np.polyval(coefficients, tx)
    universal = _mark_houwink(options.get("mark_houwink")) if options.get("mark_houwink") is not None else None
    if universal:
        # Benoit universal calibration: equal hydrodynamic volume [eta]M = K M^(1+a) at each elution slice.
        s, u = universal["standard"], universal["sample"]
        log_m = (math.log10(s["K"] / u["K"]) + (1 + s["a"]) * log_m) / (1 + u["a"])
    molar_mass = 10 ** log_m
    mn = float(1 / np.sum(fraction / molar_mass))
    mw = float(np.sum(fraction * molar_mass))
    mz = float(np.sum(fraction * molar_mass ** 2) / mw)
    peak = float(corrected.max())
    if max(corrected[0], corrected[-1]) > 0.05 * peak:
        draft["warnings"].append("Signal at an integration limit exceeds 5% of the peak height; the distribution may be truncated.")
    if detector != "RI":
        draft["warnings"].append(f"{detector} response is assumed proportional to polymer mass concentration; verify this for the analyte or use RI.")
    conditions = {"calibration_standard": standard, "calibration_order": calibration["order"], "detector": detector, "x_unit": x_unit}
    if options.get("eluent"):
        conditions["eluent"] = _text(options, "eluent", "eluent")
    analysis = {"calibration": calibration,
                "calibration_sha256": hashlib.sha256(json.dumps(calibration["standards"], sort_keys=True).encode()).hexdigest(),
                "baseline_before": list(before) if before else None, "baseline_after": list(after) if after else None,
                "integration_window": [start, end], "baseline": baseline_record,
                "slices": int(inside.sum()), "clipped_negative_area_fraction": negative / (positive + negative),
                "peak_molar_mass_g_mol": float(molar_mass[int(np.argmax(corrected))])}
    method = "SEC/GPC conventional calibration slice method on a baseline-corrected concentration-detector signal"
    if universal:
        conditions["universal_calibration"] = {"standard": universal["standard"], "K_unit": universal["K_unit"]}
        analysis["mark_houwink"] = universal
        method = "SEC/GPC universal calibration (Benoit, stated Mark-Houwink constants) slice method on a baseline-corrected concentration-detector signal"
    draft["fit"] = analysis
    draft["measurement_drafts"] = [
        {"property": prop, "value": value, "unit": unit, "method": method, "conditions": conditions, "analysis": analysis}
        # Mw/Mn >= 1 exactly; the bound only absorbs rounding for very narrow peaks.
        for prop, value, unit in (("mn", mn, "g/mol"), ("mw", mw, "g/mol"), ("dispersity", max(1.0, mw / mn), "dimensionless"), ("mz", max(mz, mw), "g/mol"))]
    if universal:
        draft["warnings"].append("Universal-calibration molar masses are only as good as the Mark-Houwink constants, which must hold for this eluent, temperature and molar-mass range; no light scattering or band-broadening correction is applied.")
    else:
        draft["warnings"].append(f"Molar masses are {standard}-equivalent relative values, not absolute; no universal calibration, light scattering or band-broadening correction is applied.")


def _mark_houwink(value):
    if not isinstance(value, dict):
        raise ValueError("mark_houwink must be {standard: {K, a}, sample: {K, a}, K_unit, source}")
    constants = {}
    for role in ("standard", "sample"):
        pair = value.get(role)
        if not isinstance(pair, dict):
            raise ValueError(f"mark_houwink.{role} needs K and a")
        k, a = pair.get("K"), pair.get("a")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (k, a)) or k <= 0 or not 0 < a < 2:
            raise ValueError(f"mark_houwink.{role} needs K > 0 and 0 < a < 2")
        constants[role] = {"K": float(k), "a": float(a)}
    if value.get("K_unit") not in {"mL/g", "dL/g"}:
        raise ValueError("mark_houwink.K_unit must be mL/g or dL/g (the same for both polymers)")
    return {**constants, "K_unit": value["K_unit"], "source": _text(value, "source", "mark_houwink.source (where the constants come from)", 300)}


CURVE_KINDS = ("dsc", "tensile", "tga", "gpc", "ir", "uvvis", "turbidity")


def analyze_curve(kind: str, text: str, options: dict | None = None) -> dict:
    options = options or {}
    kind = kind.lower()
    default = {"dsc": ("temperature", "heat_flow"), "tensile": ("strain", "stress"),
               "tga": ("temperature", "mass"), "gpc": ("retention_time", "signal")}
    if kind in {"ir", "uvvis", "turbidity"}:
        from .spectra import analyze_spectrum
        draft = analyze_spectrum(kind, text, options)
        draft["measurement_draft"] = draft["measurement_drafts"][0] if draft["measurement_drafts"] else None
        return draft
    if kind not in default:
        raise ValueError("Supported curve kinds: " + ", ".join(CURVE_KINDS))
    x_column, y_column = (options.get("x_column", default[kind][0]), options.get("y_column", default[kind][1]))
    x, y = _curve(text, x_column, y_column)
    draft = _new_draft(kind, text, options, x, y, x_column, y_column)
    if kind == "tensile":
        strain_unit, stress_unit = options.get("strain_unit"), options.get("stress_unit")
        if strain_unit not in {"fraction", "%"}:
            raise ValueError("strain_unit must explicitly be fraction or %")
        factors = {"Pa": 1e-6, "kPa": 1e-3, "MPa": 1, "GPa": 1e3}
        if stress_unit not in factors:
            raise ValueError("stress_unit must be Pa, kPa, MPa, or GPa")
        region = _window(options.get("linear_region"), "linear_region")
        strain_factor = .01 if strain_unit == "%" else 1
        coefficients, fit = _fit(x * strain_factor, y * factors[stress_unit],
                                 [v * strain_factor for v in region], "linear_region")
        if coefficients[0] <= 0:
            raise ValueError("Chosen tensile region has a non-positive slope")
        draft["fit"] = fit
        draft["measurement_draft"] = {"property": "young_modulus", "value": float(coefficients[0]), "unit": "MPa",
                                      "method": "OLS engineering stress versus strain in manually selected linear region",
                                      "conditions": {"linear_region_input": list(region), "strain_unit_input": strain_unit, "stress_unit_input": stress_unit}}
        draft["warnings"].append("Check extensometer calibration, specimen geometry, compliance/toe correction, strain rate and linear-region choice before saving.")
    elif kind == "dsc" and options.get("analysis") == "melting":
        temperature_unit = options.get("temperature_unit")
        if temperature_unit not in {"K", "C", "°C"}:
            raise ValueError("Explicit temperature_unit (K/C) is required")
        _dsc_melting(x, y, options, draft, temperature_unit)
    elif kind == "dsc":
        temperature_unit = options.get("temperature_unit")
        if temperature_unit not in {"K", "C", "°C"} or not options.get("heat_flow_unit"):
            raise ValueError("Explicit temperature_unit (K/C) and heat_flow_unit are required")
        _dsc_tg(x, y, options, draft, temperature_unit)
    elif kind == "gpc":
        if "calibration" in options:
            _gpc(x, y, options, draft)
        else:
            draft["warnings"].append("Retention time alone does not determine Mn/Mw. Supply calibration standards, detector, baseline and integration windows; this import is preview only.")
    elif "mass_loss_percent" in options:
        _tga(x, y, options, draft)
    else:
        draft["warnings"].append("TGA preview only. A mass-loss temperature needs a stated threshold, reference-temperature normalization, atmosphere and heating rate.")
    if "measurement_drafts" in draft:
        draft["measurement_draft"] = draft["measurement_drafts"][0]
    else:
        draft["measurement_drafts"] = [draft["measurement_draft"]] if draft["measurement_draft"] else []
    return draft


def ingest_pdf(path: Path) -> dict:
    from pypdf import PdfReader
    path = Path(path)
    if path.stat().st_size > 25_000_000:
        raise ValueError("PDF exceeds the 25 MB local extraction limit")
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF is not supported")
    if len(reader.pages) > 100:
        raise ValueError("PDF exceeds the 100 page extraction limit")
    pages, total, truncated = [], 0, False
    for number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        remaining = 500_000 - total
        if len(text) > remaining:
            text, truncated = text[:remaining], True
        pages.append({"page": number, "text": text})
        total += len(text)
        if total >= 500_000:
            truncated = True
            break
    return {"filename": path.name, "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "pages": pages, "page_count": len(reader.pages), "extracted_characters": total, "truncated": truncated,
            "review_required": True, "measurements": [], "method": "pypdf text extraction only",
            "warnings": ["No property values or structures are inferred. Review page evidence and manually create measurements.",
                         "Scanned pages, figures and tables may require OCR or manual transcription; no LLM or OCR service was called."]}
