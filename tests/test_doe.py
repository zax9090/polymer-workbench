"""Independent checks of DOE geometry, coded least squares and bounded recommendations."""
import csv
import io
import itertools
import json

import numpy as np
import pytest
from scipy import stats

from workbench import doe


FACTORS = [{"name": "temperature", "low": -2.0, "high": 2.0},
           {"name": "pressure", "low": 10.0, "high": 14.0}]


@pytest.fixture(autouse=True)
def native_kernel():
    pytest.importorskip("workbench_native")


def csv_data(rows, headers=("temperature", "pressure", "yield")):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue()


def quadratic_data():
    design = doe.design(FACTORS, kind="central_composite", center_points=3, seed=11)
    rows = []
    for run in design["runs"]:
        x, z = (run["values"][f["name"]] for f in FACTORS)
        u, v = x / 2, (z - 12) / 2
        rows.append([x, z, 10 - (u - .25)**2 - 2 * (v + .5)**2])
    return csv_data(rows)


def test_full_factorial_covers_every_corner_and_centers_reproducibly():
    result = doe.design(FACTORS, center_points=3, seed=17)
    assert result == doe.design(FACTORS, center_points=3, seed=17)
    points = [tuple(r["coded"][f["name"]] for f in FACTORS) for r in result["runs"]]
    assert sorted(p for p in points if any(p)) == list(itertools.product((-1., 1.), repeat=2))
    assert points.count((0., 0.)) == 3
    assert result["total_runs"] == 7
    records = list(csv.DictReader(io.StringIO(result["csv"])))
    assert len(records) == 7 and all(row["response"] == "" for row in records)
    assert [r["run_order"] for r in result["runs"]] == list(range(1, 8))
    json.dumps(result, allow_nan=False)


def test_face_centered_design_contains_axial_points_inside_real_bounds():
    result = doe.design(FACTORS, kind="central_composite", center_points=3, seed=2)
    points = [tuple(r["coded"][f["name"]] for f in FACTORS) for r in result["runs"]]
    assert set(points) == set(itertools.product((-1., 1.), repeat=2)) | {
        (-1., 0.), (1., 0.), (0., -1.), (0., 1.), (0., 0.)}
    assert len(points) == 11
    for run in result["runs"]:
        assert all(f["low"] <= run["values"][f["name"]] <= f["high"] for f in FACTORS)


def test_quadratic_surface_recovers_known_interior_optimum():
    result = doe.analyze(quadratic_data(), FACTORS, response="yield", model="quadratic", objective="maximize")
    expected = [9.4375, .5, -2, 0, -1, -2]
    np.testing.assert_allclose([r["estimate"] for r in result["coefficients"]], expected, atol=1e-10)
    recommendation = result["recommendation"]
    assert recommendation["settings"]["temperature"] == pytest.approx(.5, abs=1e-7)
    assert recommendation["settings"]["pressure"] == pytest.approx(11., abs=1e-7)
    assert recommendation["predicted_response"] == pytest.approx(10., abs=1e-9)
    assert recommendation["validated"] is False
    assert result["fit"]["r_squared"] == pytest.approx(1.)
    assert result["warnings"]
    json.dumps(result, allow_nan=False)


def test_regression_and_inference_match_independent_numpy_scipy():
    rng = np.random.default_rng(2026)
    coded = rng.uniform(-1, 1, (40, 2))
    x = np.column_stack([np.ones(40), coded])
    y = x @ np.array([3., 2., -4.]) + rng.normal(0, .15, 40)
    real = np.column_stack([coded[:, 0] * 2, 12 + coded[:, 1] * 2, y])
    result = doe.analyze(csv_data(real), FACTORS, response="yield", model="linear", objective="minimize")
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    residual = y - x @ beta
    mse = residual @ residual / 37
    se = np.sqrt(np.diag(np.linalg.inv(x.T @ x)) * mse)
    for row, b, s in zip(result["coefficients"], beta, se):
        assert row["estimate"] == pytest.approx(b, abs=1e-10)
        assert row["standard_error"] == pytest.approx(s, rel=1e-9)
        assert row["p_value"] == pytest.approx(2 * stats.t.sf(abs(b / s), 37), rel=1e-8, abs=1e-14)
    assert result["fit"]["rmse"] == pytest.approx(np.sqrt(mse), rel=1e-9)
    best = result["recommendation"]["settings"]
    assert best["temperature"] == pytest.approx(-2)
    assert best["pressure"] == pytest.approx(14)


def test_target_optimization_stays_bounded_and_reports_unattainable_target():
    result = doe.analyze(quadratic_data(), FACTORS, response="yield", model="quadratic", objective="target", target=9.0)
    assert result["recommendation"]["predicted_response"] == pytest.approx(9., abs=1e-6)
    assert all(f["low"] <= result["recommendation"]["settings"][f["name"]] <= f["high"] for f in FACTORS)
    impossible = doe.analyze(quadratic_data(), FACTORS, response="yield", model="quadratic", objective="target", target=10.5)
    assert impossible["recommendation"]["predicted_response"] <= 10. + 1e-8
    assert impossible["warnings"]


@pytest.mark.parametrize("scale", [1.0, 1e-20])
def test_unattained_target_warning_is_invariant_to_response_units(scale):
    design = doe.design(FACTORS, center_points=3)
    rows = []
    for run in design["runs"]:
        values, coded = run["values"], run["coded"]
        rows.append([values["temperature"], values["pressure"],
                     scale * (2 + coded["temperature"] + .3 * coded["pressure"])])
    result = doe.analyze(csv_data(rows), FACTORS, response="yield", model="linear", objective="target", target=0)
    assert result["recommendation"]["predicted_response"] == pytest.approx(.7 * scale, rel=1e-9, abs=0)
    assert any("did not reach the requested target" in warning for warning in result["warnings"])


def test_native_rejects_large_broadcast_vectors_before_copying():
    import workbench_native as native

    # This view occupies one float, but materializing it would allocate 800 MB.
    huge = np.broadcast_to(np.array([1.0]), (100000000,))
    with pytest.raises(ValueError, match="response length"):
        native.doe_fit(np.zeros((5, 2)), huge, "linear")
    with pytest.raises(ValueError, match="coefficient length"):
        native.doe_optimize(huge, 2, "linear", "maximize", None)


def test_singular_and_underdetermined_designs_are_rejected():
    singular = csv_data([[x, 12 + x, 1 + x] for x in np.linspace(-1, 1, 10)])
    with pytest.raises(ValueError):
        doe.analyze(singular, FACTORS, response="yield", model="linear")
    with pytest.raises(ValueError):
        doe.analyze(csv_data([[0, 12, 1], [1, 13, 2]]), FACTORS, response="yield", model="quadratic")


@pytest.mark.parametrize("bad", ["", "NaN", "Infinity", "not-a-number"])
def test_measurements_are_never_silently_dropped(bad):
    rows = list(csv.reader(io.StringIO(quadratic_data())))
    rows[2][2] = bad
    stream = io.StringIO(); csv.writer(stream).writerows(rows)
    with pytest.raises(ValueError):
        doe.analyze(stream.getvalue(), FACTORS, response="yield")


def test_out_of_range_conditions_and_constant_response():
    rows = list(csv.reader(io.StringIO(quadratic_data())))
    rows[1][0] = "100"
    stream = io.StringIO(); csv.writer(stream).writerows(rows)
    with pytest.raises(ValueError):
        doe.analyze(stream.getvalue(), FACTORS, response="yield")
    constant = csv_data([[x, z, 4.] for x, z in itertools.product([-1., 0., 1.], [11., 12., 13.])])
    result = doe.analyze(constant, FACTORS, response="yield", model="quadratic")
    json.dumps(result, allow_nan=False)
    assert result["recommendation"]["predicted_response"] == pytest.approx(4.)
    assert result["warnings"]


@pytest.mark.parametrize("factors,options", [
    ([FACTORS[0], FACTORS[0]], {}),
    ([{**FACTORS[0], "low": 3}, FACTORS[1]], {}),
    ([{**FACTORS[0], "high": float("inf")}, FACTORS[1]], {}),
    ([{**FACTORS[0], "name": "response"}, FACTORS[1]], {}),
    (FACTORS, {"center_points": -1}),
    (FACTORS, {"replicates": 0}),
    (FACTORS, {"seed": -1}),
    (FACTORS, {"kind": "unknown"}),
])
def test_design_validation(factors, options):
    with pytest.raises(ValueError):
        doe.design(factors, **options)
