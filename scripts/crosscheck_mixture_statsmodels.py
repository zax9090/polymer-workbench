#!/usr/bin/env python3
"""Cross-check workbench.doe mixture fits against statsmodels 0.14.5 OLS without an intercept.

statsmodels is not a Polymer Lab dependency. Install it beside the project environment, for example:
  .venv/bin/python -m pip install --target /tmp/smlib --no-deps statsmodels==0.14.5 patsy==1.0.1
  PYTHONPATH=/tmp/smlib .venv/bin/python scripts/crosscheck_mixture_statsmodels.py .
"""
import csv
import io
import sys

import numpy as np
# Only the regression module: statsmodels.api also imports time-series code that pandas 3 breaks.
from statsmodels.regression.linear_model import OLS

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else ".")
from workbench import doe  # noqa: E402


def features(z, model):
    q = z.shape[1]
    columns = [z[:, i] for i in range(q)]
    if model != "scheffe_linear":
        columns += [z[:, i] * z[:, j] for i in range(q) for j in range(i + 1, q)]
    if model == "scheffe_special_cubic":
        columns += [z[:, i] * z[:, j] * z[:, k] for i in range(q) for j in range(i + 1, q) for k in range(j + 1, q)]
    return np.column_stack(columns)


rng = np.random.default_rng(2026)
worst = {"coefficient": 0.0, "standard_error": 0.0, "p_value": 0.0, "r_squared": 0.0, "f": 0.0, "prediction_interval": 0.0}
cases = 0
for case in range(60):
    q = int(rng.integers(3, 6))
    lower = np.round(rng.uniform(0, 0.15, q), 3) if case % 2 else np.zeros(q)
    components = [{"name": f"X{i}", "low": float(lower[i]), "high": 1.0} for i in range(q)]
    model = ["scheffe_linear", "scheffe_quadratic", "scheffe_special_cubic"][case % 3]
    kind, degree = ("simplex_lattice", 3) if model != "scheffe_linear" else ("simplex_centroid", 2)
    design = doe.mixture_design(components, kind, degree, axial=True, centroid=True, replicates=2, seed=case)
    x = np.array([[run["values"][c["name"]] for c in components] for run in design["runs"]])
    z = (x - lower) / (1 - lower.sum())
    beta = rng.normal(0, 5, features(z, model).shape[1])
    y = features(z, model) @ beta + rng.normal(0, 0.5, len(x))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([c["name"] for c in components] + ["y"])
    writer.writerows([[*row, value] for row, value in zip(x, y)])
    ours = doe.analyze_mixture(out.getvalue(), components, "y", model, prediction_points=[dict(zip([c["name"] for c in components], x[0]))])
    reference = OLS(y, features(z, model)).fit()
    scale = np.abs(reference.params).max()
    worst["coefficient"] = max(worst["coefficient"], np.abs(np.array([c["estimate"] for c in ours["coefficients"]]) - reference.params).max() / scale)
    worst["standard_error"] = max(worst["standard_error"], np.abs(np.array([c["standard_error"] for c in ours["coefficients"]]) / reference.bse - 1).max())
    cross = [i for i, c in enumerate(ours["coefficients"]) if ":" in c["term"]]
    if cross:
        worst["p_value"] = max(worst["p_value"], np.abs(np.array([ours["coefficients"][i]["p_value"] for i in cross]) - reference.pvalues[cross]).max())
    # statsmodels reports an uncentred R-squared without a constant; the centred one follows from its residuals.
    centred = 1 - reference.ssr / ((y - y.mean()) ** 2).sum()
    worst["r_squared"] = max(worst["r_squared"], abs(ours["fit"]["r_squared"] - centred))
    p = features(z, model).shape[1]
    f_centred = ((((y - y.mean()) ** 2).sum() - reference.ssr) / (p - 1)) / (reference.ssr / reference.df_resid)
    worst["f"] = max(worst["f"], abs(ours["anova"]["regression"]["f_statistic"] / f_centred - 1))
    band = reference.get_prediction(features(z[:1], model)).summary_frame(alpha=0.05)
    interval = ours["predictions"][0]["prediction_interval"]
    worst["prediction_interval"] = max(worst["prediction_interval"], abs(interval[0] - band["obs_ci_lower"].iloc[0]), abs(interval[1] - band["obs_ci_upper"].iloc[0]))
    cases += 1
print(f"{cases} random mixture fits (q = 3-5, lower bounds in half, linear/quadratic/special cubic)")
for key, value in worst.items():
    print(f"  max {key} difference: {value:.3g}")
