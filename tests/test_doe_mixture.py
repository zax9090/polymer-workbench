"""Mixture designs, Scheffé fits in pseudocomponents and Derringer-Suich desirability."""
import csv
import io
from math import comb

import numpy as np
import pytest

native = pytest.importorskip("workbench_native")
if not hasattr(native, "mixture_design"):
    pytest.skip("workbench_native was built before the mixture kernel", allow_module_level=True)

from workbench import doe  # noqa: E402

THREE = [{"name": n, "low": 0.0, "high": 1.0} for n in ("A", "B", "C")]
# Scheffé quadratic used throughout: f = 10A + 8B + 6C + 12AB + 4AC - 6BC.
TRUTH = [10.0, 8.0, 6.0, 12.0, 4.0, -6.0]


def scheffe(x, coefficients, cubic=False):
    a, b, c = x
    terms = [a, b, c, a * b, a * c, b * c] + ([a * b * c] if cubic else [])
    return float(np.dot(terms, coefficients))


def table(rows, names, responses):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([*names, *responses])
    writer.writerows(rows)
    return out.getvalue()


@pytest.mark.parametrize("q,m", [(3, 2), (3, 3), (4, 2), (5, 3)])
def test_lattice_and_centroid_point_counts(q, m):
    components = [{"name": f"X{i}", "low": 0.0, "high": 1.0} for i in range(q)]
    lattice = doe.mixture_design(components, "simplex_lattice", m, centroid=False)
    assert lattice["total_runs"] == comb(q + m - 1, m)
    centroid = doe.mixture_design(components, "simplex_centroid", axial=True)
    assert centroid["total_runs"] == 2 ** q - 1 + q
    for run in lattice["runs"] + centroid["runs"]:
        assert sum(run["values"].values()) == pytest.approx(1.0)


def test_extreme_vertices_honour_bounds_and_simplex_designs_refuse_cuts():
    cut = [{"name": "A", "low": 0.0, "high": 0.6}, {"name": "B", "low": 0.1, "high": 1.0}, {"name": "C", "low": 0.0, "high": 0.7}]
    design = doe.mixture_design(cut, "extreme_vertices", 2)
    for run in design["runs"]:
        x = run["values"]
        assert sum(x.values()) == pytest.approx(1.0)
        assert 0.0 <= x["A"] <= 0.6 + 1e-12 and 0.1 - 1e-12 <= x["B"] <= 1.0 + 1e-12 and x["C"] <= 0.7 + 1e-12
    types = [run["point_type"] for run in design["runs"]]
    assert types.count("vertex") >= 3 and "edge" in types and types.count("centroid") == 1
    with pytest.raises(ValueError, match="extreme_vertices"):
        doe.mixture_design(cut, "simplex_lattice", 2)
    with pytest.raises(ValueError, match="배합 영역"):
        doe.mixture_design([{"name": "A", "low": 0.6, "high": 1.0}, {"name": "B", "low": 0.5, "high": 1.0}], "extreme_vertices")


def noisy_runs(components, coefficients, seed=4, cubic=False, replicate=2):
    design = doe.mixture_design(components, "simplex_lattice", 2, axial=True, centroid=True, replicates=replicate, seed=seed)
    rng = np.random.default_rng(seed)
    names = [c["name"] for c in components]
    rows = []
    for run in design["runs"]:
        x = [run["values"][n] for n in names]
        rows.append([*x, scheffe(x, coefficients, cubic) + rng.normal(0, 0.3)])
    return rows, names


def test_scheffe_fit_matches_independent_least_squares():
    rows, names = noisy_runs(THREE, TRUTH)
    result = doe.analyze_mixture(table(rows, names, ["y"]), THREE, "y", "scheffe_quadratic")
    x, y = np.asarray([r[:3] for r in rows]), np.asarray([r[3] for r in rows])
    design = np.column_stack([x[:, 0], x[:, 1], x[:, 2], x[:, 0] * x[:, 1], x[:, 0] * x[:, 2], x[:, 1] * x[:, 2]])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    residual = y - design @ beta
    sigma2 = residual @ residual / (len(y) - 6)
    se = np.sqrt(np.diag(np.linalg.inv(design.T @ design)) * sigma2)
    assert [c["estimate"] for c in result["coefficients"]] == pytest.approx(beta.tolist(), rel=1e-9, abs=1e-9)
    assert [c["standard_error"] for c in result["coefficients"]] == pytest.approx(se.tolist(), rel=1e-8)
    sst = ((y - y.mean()) ** 2).sum()
    assert result["fit"]["r_squared"] == pytest.approx(1 - residual @ residual / sst, rel=1e-10)
    assert result["anova"]["regression"]["df"] == 5 and result["anova"]["residual"]["df"] == len(y) - 6
    linear = [c for c in result["coefficients"] if ":" not in c["term"]]
    assert all(c["p_value"] is None for c in linear) and result["coefficients"][3]["p_value"] is not None
    assert result["anova"]["pure_error"]["df"] == len(y) - 10  # ten distinct blends, two replicates each


def test_lower_bounds_fit_in_pseudocomponents_give_the_same_surface():
    bounded = [{"name": "A", "low": 0.2, "high": 1.0}, {"name": "B", "low": 0.1, "high": 1.0}, {"name": "C", "low": 0.0, "high": 1.0}]
    rows, names = noisy_runs(bounded, TRUTH, seed=9)
    result = doe.analyze_mixture(table(rows, names, ["y"]), bounded, "y", "scheffe_quadratic")
    assert result["region"]["pseudo_scale"] == pytest.approx(0.7)
    x, y = np.asarray([r[:3] for r in rows]), np.asarray([r[3] for r in rows])
    real = np.column_stack([x[:, 0], x[:, 1], x[:, 2], x[:, 0] * x[:, 1], x[:, 0] * x[:, 2], x[:, 1] * x[:, 2]])
    beta, *_ = np.linalg.lstsq(real, y, rcond=None)
    # The Scheffé quadratic space is invariant under the affine pseudocomponent map, so fitted values agree.
    assert result["diagnostics"]["fitted"] == pytest.approx((real @ beta).tolist(), abs=1e-9)
    assert result["recommendation"]["settings"]["A"] >= 0.2 - 1e-9 and sum(result["recommendation"]["settings"].values()) == pytest.approx(1.0)


def test_exact_quadratic_recommends_the_known_edge_optimum():
    design = doe.mixture_design(THREE, "simplex_lattice", 3, axial=True, centroid=True, replicates=2, seed=2)
    rows = [[run["values"][n] for n in "ABC"] for run in design["runs"]]
    rows = [[*x, scheffe(x, TRUTH) + (0.01 if i % 2 else -0.01)] for i, x in enumerate(rows)]
    result = doe.analyze_mixture(table(rows, list("ABC"), ["y"]), THREE, "y", "scheffe_quadratic", "maximize",
                                 prediction_points=[{"A": 0.5, "B": 0.5, "C": 0.0}])
    best = result["recommendation"]["settings"]
    assert best["A"] == pytest.approx(7 / 12, abs=2e-3) and best["C"] == pytest.approx(0.0, abs=1e-6)
    assert result["recommendation"]["validated"] is False and result["predictions"][0]["predicted_response"] == pytest.approx(12.0, abs=0.05)
    cubic = doe.analyze_mixture(table(rows, list("ABC"), ["y"]), THREE, "y", "scheffe_special_cubic")
    assert [m["model"] for m in cubic["model_comparison"]] == list(doe.MIXTURE_MODELS)


def test_mixture_rows_must_sum_to_one_and_stay_in_bounds():
    rows = [[0.5, 0.3, 0.1, 1.0]] * 8
    with pytest.raises(ValueError, match="합이 1"):
        doe.analyze_mixture(table(rows, list("ABC"), ["y"]), THREE, "y", "scheffe_linear")
    with pytest.raises(ValueError, match="special cubic"):
        doe.analyze_mixture("A,B,y\n0.5,0.5,1\n", THREE[:2], "y", "scheffe_special_cubic")


def test_desirability_balances_two_mixture_responses():
    design = doe.mixture_design(THREE, "simplex_lattice", 3, axial=True, centroid=True, seed=5)
    rows = []
    for run in design["runs"]:
        x = [run["values"][n] for n in "ABC"]
        # Strength rises with A, cost rises with A and falls with C.
        rows.append([*x, 10 * x[0] + 4 * x[1] + 2 * x[2], 5 * x[0] + 3 * x[1] + 1 * x[2]])
    goals = [{"name": "strength", "model": "scheffe_linear", "goal": "maximize", "lower": 4.0, "target": 8.0},
             {"name": "cost", "model": "scheffe_linear", "goal": "minimize", "target": 2.0, "upper": 4.0}]
    result = doe.optimize_desirability(table(rows, list("ABC"), ["strength", "cost"]), goals, components=THREE)
    best = result["optimum"]["settings"]
    strength, cost = (r["predicted"] for r in result["responses"])
    d = [((strength - 4) / 4) if strength < 8 else 1.0, ((4 - cost) / 2) if cost > 2 else 1.0]
    assert result["optimum"]["composite_desirability"] == pytest.approx(np.sqrt(np.clip(d, 0, 1).prod()), abs=1e-9)
    assert result["optimum"]["composite_desirability"] >= max(result["observed_composite_desirability"]) - 1e-9
    assert sum(best.values()) == pytest.approx(1.0) and 0 < result["optimum"]["composite_desirability"] < 1
    with pytest.raises(ValueError, match="하나만"):
        doe.optimize_desirability("x", goals)


def test_desirability_on_coded_factors_reaches_both_targets():
    factors = [{"name": "T", "low": 100.0, "high": 200.0}, {"name": "t", "low": 1.0, "high": 5.0}]
    design = doe.design(factors, "central_composite", 3, seed=3)
    rows = []
    for run in design["runs"]:
        a, b = run["coded"]["T"], run["coded"]["t"]
        rows.append([run["values"]["T"], run["values"]["t"], 5 + 3 * a, 2 + b ** 2])
    goals = [{"name": "y1", "model": "linear", "goal": "maximize", "lower": 2.0, "target": 8.0},
             {"name": "y2", "model": "quadratic", "goal": "minimize", "target": 2.0, "upper": 3.0, "importance": 2.0}]
    result = doe.optimize_desirability(table(rows, ["T", "t"], ["y1", "y2"]), goals, factors=factors)
    assert result["optimum"]["settings"]["T"] == pytest.approx(200.0, abs=1e-6)
    assert result["optimum"]["settings"]["t"] == pytest.approx(3.0, abs=1e-3)
    assert result["optimum"]["composite_desirability"] == pytest.approx(1.0, abs=1e-6)
    assert result["responses"][1]["importance"] == 2.0 and result["optimum"]["validated"] is False
