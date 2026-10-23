"""Independent numerical examples for the Rust DOE model-review workflow."""
import csv
import io
import json

import numpy as np
import pytest
from scipy import stats

from workbench import doe


FACTORS = [{"name": "temperature", "low": -1.0, "high": 1.0},
           {"name": "concentration", "low": -1.0, "high": 1.0}]


def repeated_experiment():
    points = np.repeat(np.array([(x, z) for x in (-1., 0., 1.) for z in (-1., 0., 1.)]), 3, axis=0)
    rng = np.random.default_rng(913)
    y = 4. + 1.2 * points[:, 0] - .7 * points[:, 1] + .4 * points[:, 0] ** 2
    y += rng.normal(0, .12, len(y))
    return points, np.ascontiguousarray(y)


def matrix(points, model="linear"):
    columns = [np.ones(len(points)), *points.T]
    if model != "linear":
        columns.append(points[:, 0] * points[:, 1])
    if model == "quadratic":
        columns.extend(points.T ** 2)
    return np.column_stack(columns)


def csv_text(points, y):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["temperature", "concentration", "response"])
    writer.writerows(np.column_stack([points, y]))
    return out.getvalue()


def reference(points, y, model="linear"):
    x = matrix(points, model)
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    residual = y - x @ beta
    inverse = np.linalg.inv(x.T @ x)
    mse = residual @ residual / (len(y) - x.shape[1])
    return x, beta, residual, inverse, mse


def test_rust_anova_and_coefficient_inference_match_scipy():
    native = pytest.importorskip("workbench_native")
    points, y = repeated_experiment()
    x, beta, residual, inverse, mse = reference(points, y)
    result = native.doe_fit(points, y, "linear")
    n, p = x.shape
    sse = residual @ residual
    sst = (y - y.mean()) @ (y - y.mean())
    f_value = ((sst - sse) / (p - 1)) / mse
    regression = result["anova"]["regression"]
    assert regression["df"] == p - 1
    assert regression["ss"] == pytest.approx(sst - sse, rel=1e-11)
    assert regression["f_statistic"] == pytest.approx(f_value, rel=1e-11)
    assert regression["p_value"] == pytest.approx(stats.f.sf(f_value, p - 1, n - p), rel=1e-8, abs=1e-16)
    assert result["anova"]["total"]["df"] == n - 1
    assert result["anova"]["residual"]["ms"] == pytest.approx(mse, rel=1e-11)
    critical = stats.t.ppf(.975, n - p)
    for estimate, variance, item in zip(beta, np.diag(inverse) * mse, result["coefficients_inference"]):
        standard_error = np.sqrt(variance)
        statistic = estimate / standard_error
        assert item["t_statistic"] == pytest.approx(statistic, rel=1e-10)
        assert item["p_value"] == pytest.approx(2 * stats.t.sf(abs(statistic), n - p), rel=1e-8, abs=1e-16)
        np.testing.assert_allclose(item["confidence_interval"], estimate + np.array([-1., 1.]) * critical * standard_error,
                                   rtol=1e-9, atol=1e-11)


def test_lack_of_fit_separates_replicate_noise_from_missing_curvature():
    native = pytest.importorskip("workbench_native")
    points, y = repeated_experiment()
    result = native.doe_fit(points, y, "linear")
    _, _, residual, _, _ = reference(points, y)
    groups = y.reshape(9, 3)
    pure_error = float(np.sum((groups - groups.mean(axis=1, keepdims=True)) ** 2))
    lack = float(residual @ residual - pure_error)
    anova = result["anova"]
    assert anova["replicate_groups"] == 9
    assert anova["pure_error"]["df"] == 18
    assert anova["pure_error"]["ss"] == pytest.approx(pure_error, rel=1e-10)
    assert anova["lack_of_fit"]["df"] == 6
    assert anova["lack_of_fit"]["ss"] == pytest.approx(lack, rel=1e-10)
    expected_f = (lack / 6) / (pure_error / 18)
    assert anova["lack_of_fit"]["f_statistic"] == pytest.approx(expected_f, rel=1e-10)
    assert anova["lack_of_fit"]["p_value"] == pytest.approx(stats.f.sf(expected_f, 6, 18), rel=1e-8, abs=1e-15)
    assert anova["lack_of_fit"]["p_value"] < .05


def test_residual_influence_and_press_match_independent_deleted_fits():
    native = pytest.importorskip("workbench_native")
    points, y = repeated_experiment()
    x, _, residual, inverse, mse = reference(points, y, "quadratic")
    result = native.doe_fit(points, y, "quadratic")
    diagnostic = result["diagnostics"]
    leverage = np.einsum("ij,jk,ik->i", x, inverse, x)
    standardized = residual / np.sqrt(mse * (1 - leverage))
    cook = residual ** 2 * leverage / (x.shape[1] * mse * (1 - leverage) ** 2)
    deleted_residuals = []
    for row in range(len(y)):
        keep = np.arange(len(y)) != row
        deleted_beta = np.linalg.lstsq(x[keep], y[keep], rcond=None)[0]
        deleted_residuals.append(y[row] - x[row] @ deleted_beta)
    press = np.dot(deleted_residuals, deleted_residuals)
    np.testing.assert_allclose(diagnostic["leverage"], leverage, rtol=1e-11, atol=1e-12)
    np.testing.assert_allclose(diagnostic["standardized_residuals"], standardized, rtol=1e-9, atol=1e-10)
    np.testing.assert_allclose(diagnostic["cooks_distance"], cook, rtol=1e-8, atol=1e-11)
    assert diagnostic["press"] == pytest.approx(press, rel=1e-10)
    expected_r2 = 1 - press / np.sum((y - y.mean()) ** 2)
    assert diagnostic["predicted_r_squared"] == pytest.approx(expected_r2, rel=1e-10)


def test_prediction_intervals_use_estimation_and_new_observation_variance():
    native = pytest.importorskip("workbench_native")
    points, y = repeated_experiment()
    x, beta, _, inverse, mse = reference(points, y, "quadratic")
    prediction_points = np.array([[.2, -.35], [-.8, .6]])
    result = native.doe_predict(points, y, "quadratic", prediction_points)
    critical = stats.t.ppf(.975, len(y) - x.shape[1])
    for row, item in zip(matrix(prediction_points, "quadratic"), result):
        mean = row @ beta
        leverage = row @ inverse @ row
        mean_error = np.sqrt(mse * leverage)
        observation_error = np.sqrt(mse * (1 + leverage))
        assert item["predicted_response"] == pytest.approx(mean, rel=1e-11)
        assert item["standard_error_mean"] == pytest.approx(mean_error, rel=1e-10)
        assert item["leverage"] == pytest.approx(leverage, rel=1e-10)
        np.testing.assert_allclose(item["confidence_interval"], mean + np.array([-1., 1.]) * critical * mean_error,
                                   rtol=1e-9, atol=1e-11)
        np.testing.assert_allclose(item["prediction_interval"], mean + np.array([-1., 1.]) * critical * observation_error,
                                   rtol=1e-9, atol=1e-11)


def test_model_review_keeps_measurements_and_exports_blank_confirmation_runs():
    pytest.importorskip("workbench_native")
    points, y = repeated_experiment()
    result = doe.analyze(csv_text(points, y), FACTORS, "response", model="linear",
                         prediction_points=[{"temperature": .2, "concentration": -.35}])
    assert result["research_workflow"]["stage"] == "model_review"
    assert result["research_workflow"]["automatic_execution"] is False
    assert result["research_workflow"]["issues"]
    assert {row["model"] for row in result["model_comparison"] if row["status"] == "available"} == {"linear", "interaction", "quadratic"}
    assert result["predictions"][0]["settings"] == {"temperature": .2, "concentration": -.35}
    assert result["diagnostics"]["observed"] == y.tolist()
    assert result["rows_dropped"] == 0
    records = list(csv.DictReader(io.StringIO(result["confirmation_csv"])))
    assert len(records) == 3
    assert all(row["response"] == "" for row in records)
    for row in records:
        for factor in FACTORS:
            assert float(row[factor["name"]]) == pytest.approx(result["recommendation"]["settings"][factor["name"]])
    json.dumps(result, allow_nan=False)


def test_constant_response_has_no_significance_and_nonestimable_models_are_explicit():
    pytest.importorskip("workbench_native")
    # Factorial points plus repeated centres cannot separately identify both squared terms.
    points = np.array([[-1., -1.], [-1., 1.], [1., -1.], [1., 1.], [0., 0.], [0., 0.], [0., 0.]])
    result = doe.analyze(csv_text(points, np.full(7, 4.)), FACTORS, "response", model="linear")
    assert result["fit"]["r_squared"] is None
    assert result["anova"]["regression"]["p_value"] is None
    assert all(row["p_value"] is None for row in result["coefficients"])
    assert result["diagnostics"]["predicted_r_squared"] is None
    quadratic = next(row for row in result["model_comparison"] if row["model"] == "quadratic")
    assert quadratic["status"] == "unavailable" and quadratic["reason"]
    json.dumps(result, allow_nan=False)
