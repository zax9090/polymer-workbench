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


def _curve(text, x_column, y_column):
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
    if np.any(np.diff(data[:, 0]) <= 0):
        raise ValueError("Choose one monotonic loading/heating branch with strictly increasing x values")
    return data[:, 0], data[:, 1]


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
    draft["fit"] = analysis
    draft["measurement_drafts"] = [{
        "property": "decomposition_temperature", "value": crossing + offset, "unit": "K",
        "method": "TGA first-crossing temperature at a stated mass loss relative to the mass at a stated reference temperature",
        "conditions": {"mass_loss_percent": loss, "atmosphere": atmosphere, "heating_rate_K_min": rate,
                       "reference_temperature_K": round(reference + offset, 2)},
        "analysis": analysis}]
    draft["warnings"].append("Td depends on heating rate, atmosphere, sample mass, pan and buoyancy correction; no blank subtraction or extrapolated tangent onset is applied.")


GPC_DETECTORS = {"RI", "UV", "ELSD"}
MOLAR_MASS_FACTORS = {"g/mol": 1.0, "Da": 1.0, "kg/mol": 1e3}


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
    before = _window(options.get("baseline_before"), "baseline_before")
    after = _window(options.get("baseline_after"), "baseline_after")
    if before[1] > start or after[0] < end:
        raise ValueError("Baseline windows must lie before and after the integration window")
    masks = [(x >= a) & (x <= b) for a, b in (before, after)]
    if any(mask.sum() < 3 for mask in masks):
        raise ValueError("Each GPC baseline window must contain at least three data points")
    baseline_mask = masks[0] | masks[1]
    baseline = np.polyfit(x[baseline_mask], y[baseline_mask], 1)
    inside = (x >= start) & (x <= end)
    if inside.sum() < 5:
        raise ValueError("integration_window must contain at least five data points")
    tx = x[inside]
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
    molar_mass = 10 ** np.polyval(coefficients, tx)
    mn = float(1 / np.sum(fraction / molar_mass))
    mw = float(np.sum(fraction * molar_mass))
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
                "baseline_before": list(before), "baseline_after": list(after), "integration_window": [start, end],
                "baseline": {"slope": float(baseline[0]), "intercept": float(baseline[1])},
                "slices": int(inside.sum()), "clipped_negative_area_fraction": negative / (positive + negative),
                "peak_molar_mass_g_mol": float(molar_mass[int(np.argmax(corrected))])}
    method = "SEC/GPC conventional calibration slice method on a baseline-corrected concentration-detector signal"
    draft["fit"] = analysis
    draft["measurement_drafts"] = [
        {"property": prop, "value": value, "unit": unit, "method": method, "conditions": conditions, "analysis": analysis}
        # Mw/Mn >= 1 exactly; the bound only absorbs rounding for very narrow peaks.
        for prop, value, unit in (("mn", mn, "g/mol"), ("mw", mw, "g/mol"), ("dispersity", max(1.0, mw / mn), "dimensionless"))]
    draft["warnings"].append(f"Molar masses are {standard}-equivalent relative values, not absolute; no universal calibration, light scattering or band-broadening correction is applied.")


def analyze_curve(kind: str, text: str, options: dict | None = None) -> dict:
    options = options or {}
    kind = kind.lower()
    default = {"dsc": ("temperature", "heat_flow"), "tensile": ("strain", "stress"),
               "tga": ("temperature", "mass"), "gpc": ("retention_time", "signal")}
    if kind not in default:
        raise ValueError("Supported curve kinds: dsc, tensile, tga, gpc")
    x_column, y_column = (options.get("x_column", default[kind][0]), options.get("y_column", default[kind][1]))
    x, y = _curve(text, x_column, y_column)
    draft = {"kind": kind, "review_required": True, "verified": False, "origin": "experimental",
             "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "options": options,
             "points": len(x), "measurement_draft": None,
             "preview": {"x": x[::max(1, len(x) // 500)].tolist(), "y": y[::max(1, len(x) // 500)].tolist(),
                         "x_column": x_column, "y_column": y_column}, "warnings": []}
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
    elif kind == "dsc":
        temperature_unit = options.get("temperature_unit")
        if temperature_unit not in {"K", "C", "°C"} or not options.get("heat_flow_unit"):
            raise ValueError("Explicit temperature_unit (K/C) and heat_flow_unit are required")
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
        kelvin = roots[0] + (0 if temperature_unit == "K" else 273.15)
        if kelvin <= 0:
            raise ValueError("DSC temperature is below absolute zero")
        draft["fit"] = {"before": fit_a, "after": fit_b, "midpoint_input_units": roots[0]}
        draft["measurement_draft"] = {"property": "tg", "value": kelvin, "unit": "K",
                                      "method": "DSC halfway-heat-flow crossing between two manually selected linear baselines",
                                      "conditions": {"baseline_before": list(before), "baseline_after": list(after), "temperature_unit_input": temperature_unit}}
        draft["warnings"].append("Review heating cycle, rate, thermal history, endothermic overshoot and baseline selection; midpoint Tg is method dependent.")
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
