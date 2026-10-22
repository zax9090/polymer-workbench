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
        draft["warnings"].append("Retention time alone does not determine Mn/Mw. Supply calibration, detector response and integration bounds in a validated GPC analysis; this import is preview only.")
    else:
        draft["warnings"].append("TGA is preview only. Mass-loss thresholds require explicit initial-mass normalization, atmosphere, heating rate and a defined onset method.")
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
