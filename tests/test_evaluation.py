"""Model evaluation against scikit-learn, the lecture's worked numbers and synthetic lot leakage."""
import numpy as np
import pytest
from sklearn import metrics

from workbench import evaluation, process


def table(header, rows):
    return ",".join(header) + "\n" + "\n".join(",".join(str(v) for v in row) for row in rows) + "\n"


def test_metrics_match_scikit_learn_with_tied_scores():
    rng = np.random.default_rng(7)
    for _ in range(60):
        n = int(rng.integers(20, 800))
        y = rng.random(n) < rng.uniform(0.05, 0.5)
        if y.all() or not y.any():
            continue
        score = np.round(rng.random(n) * 0.5 + y * rng.uniform(0, 0.6), int(rng.integers(1, 3)))
        flagged = score >= 0.4
        ours = evaluation._confusion(y, flagged)
        assert ours["recall"] == pytest.approx(metrics.recall_score(y, flagged))
        assert (ours["precision"] or 0) == pytest.approx(metrics.precision_score(y, flagged, zero_division=0))
        assert ours["balanced_accuracy"] == pytest.approx(metrics.balanced_accuracy_score(y, flagged))
        assert ours["mcc"] == pytest.approx(metrics.matthews_corrcoef(y, flagged), abs=1e-12)
        auc, ap = evaluation._ranking(y, score)
        assert auc == pytest.approx(metrics.roc_auc_score(y, score), abs=1e-12)
        assert ap == pytest.approx(metrics.average_precision_score(y, score), abs=1e-12)
        brute = min([5 * y.sum()] + [5 * np.sum(~(score >= t) & y) + np.sum((score >= t) & ~y) for t in np.unique(score)])
        assert evaluation._minimum_cost(y, score, 5.0, 1.0)[1] == pytest.approx(brute)


def test_flag_nothing_reaches_high_accuracy_and_zero_recall():
    text = table(["label", "score"], [(-1, 0)] * 1463 + [(1, 0)] * 104)
    result = evaluation.classification_metrics(text, "label", "score")
    at = result["thresholds"][0]
    assert at["accuracy"] == pytest.approx(1463 / 1567) and at["recall"] == 0
    assert result["flag_nothing_baseline"]["accuracy"] == pytest.approx(0.9336, abs=5e-5)
    assert any("no defect is caught" in w for w in result["warnings"])


def cost_example():
    # 10 defects and 990 normal parts: a cutoff at 0.7 misses 4 with 5 false alarms, at 0.3 misses 1 with 30.
    rows = [(1, .8)] * 6 + [(1, .5)] * 3 + [(1, .1)] + [(0, .8)] * 5 + [(0, .5)] * 25 + [(0, .1)] * 960
    return table(["defect", "score"], rows)


@pytest.mark.parametrize("miss,expected", [(1000, (4050, 1300)), (20, (130, 320))])
def test_threshold_costs_follow_the_stated_prices(miss, expected):
    result = evaluation.classification_metrics(cost_example(), "defect", "score", thresholds=[0.7, 0.3], miss_cost=miss, false_alarm_cost=10)
    high, low = result["thresholds"]
    assert (high["false_negative"], high["false_positive"], low["false_negative"], low["false_positive"]) == (4, 5, 1, 30)
    assert (high["cost"], low["cost"]) == expected
    assert result["costs"]["minimum"]["cost"] <= min(expected)
    assert result["costs"]["flag_nothing"] == 10 * miss


def test_regression_errors_near_limits_and_pass_fail_decisions():
    actual = [94, 96, 98, 99, 100, 101, 104, 106]
    predicted = [97, 99, 98.5, 98.5, 100.5, 100.5, 101, 103]
    text = table(["thickness", "vm"], list(zip(actual, predicted)))
    result = evaluation.regression_metrics(text, "thickness", "vm", lsl=95, usl=105, band=1.5, unit="nm")
    assert result["overall"]["mae"] == pytest.approx(np.mean(np.abs(np.subtract(predicted, actual))))
    assert result["boundary"]["n"] == 4 and result["boundary"]["mae"] == pytest.approx(3.0)
    assert result["decisions"]["missed_out_of_spec"] == 2 and result["decisions"]["false_alarms"] == 0
    assert any("near the specification limits" in w for w in result["warnings"])


def lots(signal, seed=3, count=24, per=30, dims=30):
    """Lot fingerprints in every feature; defects depend on the lot (leak) or on f0 (signal)."""
    rng = np.random.default_rng(seed)
    rows, t = [], 0
    for lot in range(count):
        centre = rng.normal(0, 3, dims) if not signal else np.zeros(dims)
        bad = rng.random() < 0.35
        for _ in range(per):
            x = centre + rng.normal(0, 1, dims)
            p = 1 / (1 + np.exp(-3 * x[0])) if signal else (0.45 if bad else 0.03)
            t += 1
            rows.append([f"L{lot}", t, *np.round(x, 4), int(rng.random() < p)])
    return table(["lot", "time", *[f"f{j}" for j in range(dims)], "defect"], rows), [f"f{j}" for j in range(dims)]


def test_random_split_is_flagged_when_lots_leak_into_training():
    text, features = lots(signal=False)
    result = evaluation.split_validation(text, "defect", features, group="lot", order="time")
    schemes = result["schemes"]
    assert schemes["random"]["roc_auc"] > schemes["group"]["roc_auc"] + 0.15
    assert schemes["group"]["random_is_optimistic"] and schemes["time_forward"]["random_is_optimistic"]
    assert any("leak" in w for w in result["warnings"])


def test_no_leak_warning_when_the_signal_generalizes_across_lots():
    text, features = lots(signal=True)
    result = evaluation.split_validation(text, "defect", features, group="lot")
    assert result["schemes"]["group"]["roc_auc"] > 0.8
    assert not result["schemes"]["group"]["random_is_optimistic"]


def test_missing_features_need_explicit_training_fold_imputation():
    text, features = lots(signal=True, count=6, per=10, dims=3)
    lines = text.splitlines()
    cells = lines[3].split(",")
    cells[2] = "NaN"
    lines[3] = ",".join(cells)
    broken = "\n".join(lines) + "\n"
    with pytest.raises(ValueError, match="impute_median"):
        evaluation.split_validation(broken, "defect", features, folds=3)
    result = evaluation.split_validation(broken, "defect", features, folds=3, impute_median=True)
    assert any("missing feature values" in w for w in result["warnings"])


def test_new_kinds_dispatch_through_process_analyze():
    result = process.analyze("classification_metrics", cost_example(), {"label": "defect", "score": "score", "thresholds": [0.7]})
    assert result["input"]["kind"] == "classification_metrics" and result["roc_auc"] > 0.5
    with pytest.raises(ValueError, match="필요한 설정"):
        process.analyze("regression_metrics", cost_example(), {"actual": "defect"})
    with pytest.raises(ValueError, match="쓰지 않는 설정"):
        process.analyze("split_validation", cost_example(), {"target": "defect", "features": ["score"], "colour": 1})


def test_api_accepts_the_evaluation_kinds(tmp_path):
    from fastapi.testclient import TestClient
    from workbench.server import create_app
    with TestClient(create_app(tmp_path / "jobs")) as client:
        response = client.post("/api/research/process", json={"kind": "classification_metrics", "text": cost_example(),
                               "options": {"label": "defect", "score": "score", "thresholds": [0.7, 0.3], "miss_cost": 1000, "false_alarm_cost": 10}})
        assert response.status_code == 200
        assert [t["cost"] for t in response.json()["thresholds"]] == [4050, 1300]
        bad = client.post("/api/research/process", json={"kind": "split_validation", "text": cost_example(),
                          "options": {"target": "defect", "features": ["score"], "folds": 1}})
        assert bad.status_code == 422 and "분할 수" in bad.json()["detail"]
