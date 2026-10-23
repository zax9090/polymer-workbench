#!/usr/bin/env python3
"""Compare the installed DOE kernel against NumPy and retain separate numerical evidence.

Run after rebuilding ./native:
  OPENBLAS_NUM_THREADS=1 .venv/bin/python scripts/validate_doe_numerics.py
This executes the extension. It is independent of the static REA artifact audit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys

import numpy as np
import workbench_native as native


ROOT = Path(__file__).resolve().parents[1]
SEED = 827


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def features(points, model):
    """Construct the documented model matrix independently of the Rust code."""
    n, k = points.shape
    columns = [np.ones(n), *points.T]
    if model in ("interaction", "quadratic"):
        columns.extend(points[:, i] * points[:, j] for i in range(k) for j in range(i + 1, k))
    if model == "quadratic":
        columns.extend(points[:, i] ** 2 for i in range(k))
    return np.column_stack(columns)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/native/doe-numerical-validation.json")
    args = parser.parse_args()
    binary = Path(sys.modules[native.doe_fit.__module__].__file__).resolve()
    digest = sha256(binary)
    rng = np.random.default_rng(SEED)
    cases = []
    for k in range(2, 7):
        for model in ("linear", "interaction", "quadratic"):
            if model == "quadratic" and k > 4:
                continue
            for repeat in range(10):
                points = rng.uniform(-1, 1, (90, k))
                matrix = features(points, model)
                n, p = matrix.shape
                truth = rng.normal(size=p)
                response = matrix @ truth + rng.normal(0, .05, n)
                actual = native.doe_fit(points, response, model)
                coefficients = np.linalg.lstsq(matrix, response, rcond=None)[0]
                residuals = response - matrix @ coefficients
                mse = (residuals @ residuals) / (n - p)
                standard_errors = np.sqrt(mse * np.diag(np.linalg.inv(matrix.T @ matrix)))
                np.testing.assert_allclose(actual["coefficients"], coefficients, rtol=1e-10, atol=1e-12)
                np.testing.assert_allclose(actual["standard_errors"], standard_errors, rtol=1e-10, atol=1e-12)
                cases.append({
                    "factors": k, "model": model, "repeat": repeat,
                    "max_coefficient_absolute_difference": float(np.max(np.abs(actual["coefficients"] - coefficients))),
                    "max_standard_error_absolute_difference": float(np.max(np.abs(actual["standard_errors"] - standard_errors))),
                })
    if sha256(binary) != digest:
        raise SystemExit("Installed extension changed during validation; rebuild first and rerun.")
    evidence = {
        "evidence_kind": "independent_numeric_comparison_not_REA",
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_sha256": digest,
        "algorithm_source_sha256": sha256(ROOT / "native/src/doe.rs"),
        "validation_script_sha256": sha256(__file__),
        "command": "OPENBLAS_NUM_THREADS=1 .venv/bin/python scripts/validate_doe_numerics.py",
        "python": platform.python_version(), "numpy": np.__version__, "random_seed": SEED,
        "generator": "numpy.random.default_rng(827); k=2..6; linear/interaction/quadratic (quadratic k<=4); 10 repeats each; 90 points uniform(-1,1); truth normal(p); y=X@truth+normal(0,0.05,90)",
        "feature_order": "intercept, linear, pairwise interactions(i<j), squares for quadratic only",
        "reference_coefficients": "numpy.linalg.lstsq(X,y,rcond=None)[0]",
        "reference_standard_errors": "sqrt(SSE/(n-p) * diag(inv(X.T@X)))",
        "rtol": 1e-10, "atol": 1e-12, "passed": len(cases), "failed": 0,
        "max_coefficient_absolute_difference": max(case["max_coefficient_absolute_difference"] for case in cases),
        "max_standard_error_absolute_difference": max(case["max_standard_error_absolute_difference"] for case in cases),
        "cases": cases,
        "limitations": [
            "This validates finite full-rank synthetic designs; it does not prove all ill-conditioned fits are stable.",
            "This checks coefficients and classical homoscedastic OLS standard errors, not experimental independence, model adequacy, predictive coverage or global optimization.",
            "REA evidence is separate static artifact inspection and does not supply these numerical results.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({key: evidence[key] for key in (
        "artifact_sha256", "passed", "max_coefficient_absolute_difference", "max_standard_error_absolute_difference")}))


if __name__ == "__main__":
    main()
