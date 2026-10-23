"""How a defect classifier or virtual-metrology model would perform where it is used: confusion
counts at stated thresholds against the flag-nothing baseline, ranking metrics, cost-based
thresholds, errors near the specification limits, and lot- or time-separated validation.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import stats

from .process import _column, _numbers, _ordered, _table

MAX_CELLS = 2_000_000


def _labels(header, rows, label, positive):
    index = _column(header, label, "정답")
    values = []
    for line, row in rows:
        if not row[index]:
            raise ValueError(f"CSV {line}행 {label}: 정답 값이 비어 있습니다.")
        values.append(row[index] == positive)
    y = np.asarray(values)
    if y.all() or not y.any():
        raise ValueError(f"정답 열에 불량({positive!r})과 정상이 모두 있어야 합니다.")
    return y


def _confusion(y, flagged):
    tp, fp = int(np.sum(flagged & y)), int(np.sum(flagged & ~y))
    fn, tn = int(np.sum(~flagged & y)), int(np.sum(~flagged & ~y))
    def ratio(a, b):
        return a / b if b else None
    recall, specificity, precision = ratio(tp, tp + fn), ratio(tn, tn + fp), ratio(tp, tp + fp)
    denominator = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    return {"true_positive": tp, "false_positive": fp, "false_negative": fn, "true_negative": tn,
            "accuracy": (tp + tn) / len(y), "recall": recall, "specificity": specificity, "precision": precision,
            "f1": ratio(2 * tp, 2 * tp + fp + fn),
            "balanced_accuracy": (recall + specificity) / 2 if recall is not None and specificity is not None else None,
            "mcc": (tp * tn - fp * fn) / denominator if denominator else 0.0, "alarm_rate": (tp + fp) / len(y)}


def _ranking(y, score):
    """ROC AUC by the Mann-Whitney rank sum (ties averaged) and average precision as the
    recall-weighted mean of precision at each distinct score threshold."""
    positives, negatives = int(y.sum()), int((~y).sum())
    ranks = stats.rankdata(score)
    auc = (ranks[y].sum() - positives * (positives + 1) / 2) / (positives * negatives)
    order = np.argsort(-score, kind="stable")
    sorted_score, sorted_y = score[order], y[order]
    last = np.r_[np.flatnonzero(np.diff(sorted_score)), len(score) - 1]
    tp = np.cumsum(sorted_y)[last]
    fp = (last + 1) - tp
    recall = tp / positives
    precision = tp / (tp + fp)
    ap = float(np.sum(np.diff(np.r_[0.0, recall]) * precision))
    return float(auc), ap


def _minimum_cost(y, score, miss, false_alarm):
    """Every distinct cutoff (flag score >= t) plus flag-nothing; ties prefer the higher cutoff."""
    order = np.argsort(-score, kind="stable")
    sorted_score, sorted_y = score[order], y[order]
    last = np.r_[np.flatnonzero(np.diff(sorted_score)), len(score) - 1]
    tp = np.cumsum(sorted_y)[last]
    costs = np.r_[miss * y.sum(), miss * (y.sum() - tp) + false_alarm * (last + 1 - tp)]
    best = int(np.argmin(costs))
    return (np.inf if best == 0 else float(sorted_score[last[best - 1]])), float(costs[best])


def classification_metrics(text, label, score, positive="1", thresholds=None, miss_cost=None, false_alarm_cost=None):
    """Confusion counts for 'score >= threshold' alarms against the flag-nothing baseline."""
    header, rows = _table(text)
    y = _labels(header, rows, label, positive)
    s = _numbers(header, rows, score, "점수")
    thresholds = [0.5] if thresholds is None else thresholds
    if not isinstance(thresholds, list) or not 1 <= len(thresholds) <= 20 or any(
            isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) for t in thresholds):
        raise ValueError("판정 기준(thresholds)은 유한한 숫자 1~20개의 목록이어야 합니다.")
    costs = (miss_cost, false_alarm_cost)
    if (miss_cost is None) != (false_alarm_cost is None) or any(
            c is not None and (isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or c < 0) for c in costs):
        raise ValueError("놓침 비용과 오탐 비용은 0 이상의 숫자로 함께 지정하세요.")
    warnings = []
    if np.any(s < 0) or np.any(s > 1):
        warnings.append("Scores lie outside [0, 1]; thresholds are cut-offs on the score scale, not probabilities.")
    else:
        warnings.append("Scores are treated as ranking scores; a score of 0.7 is not a calibrated 70% defect probability unless the model was calibrated.")
    rows_out = []
    for t in thresholds:
        entry = {"threshold": float(t), **_confusion(y, s >= t)}
        if miss_cost is not None:
            entry["cost"] = miss_cost * entry["false_negative"] + false_alarm_cost * entry["false_positive"]
        rows_out.append(entry)
    baseline = _confusion(y, np.zeros(len(y), bool))
    auc, ap = _ranking(y, s)
    result = {"analysis": "classification_metrics", "label": label, "positive": positive, "score": score, "n": len(y),
              "positives": int(y.sum()), "prevalence": float(y.mean()), "thresholds": rows_out,
              "flag_nothing_baseline": baseline, "roc_auc": auc, "average_precision": ap,
              "method": "Alarm when score >= threshold; ROC AUC by Mann-Whitney ranks; average precision as in scikit-learn",
              "warnings": warnings}
    for entry in rows_out:
        if entry["recall"] == 0:
            warnings.append(f"At threshold {entry['threshold']:g} no defect is caught although accuracy is {entry['accuracy']:.4f}; report recall and false alarms, not accuracy alone.")
    if miss_cost is not None:
        t, cost = _minimum_cost(y, s, miss_cost, false_alarm_cost)
        result["costs"] = {"miss": miss_cost, "false_alarm": false_alarm_cost,
                           "flag_nothing": miss_cost * int(y.sum()), "flag_everything": false_alarm_cost * int((~y).sum()),
                           "minimum": {"threshold": None if np.isinf(t) else float(t), "cost": cost,
                                       **_confusion(y, s >= t)}}
        warnings.append("The minimum-cost threshold is chosen on these same rows and is optimistic; choose it on validation lots and confirm it on later lots.")
    return result


def regression_metrics(text, actual, predicted, lsl=None, usl=None, band=None, unit=None):
    """Virtual-metrology error overall, near the specification limits, and as pass/fail decisions."""
    header, rows = _table(text)
    a, p = _numbers(header, rows, actual, "실측값"), _numbers(header, rows, predicted, "예측값")
    if len(a) < 3:
        raise ValueError("오차를 평가하려면 3개 이상의 행이 필요합니다.")
    for name, limit in (("lsl", lsl), ("usl", usl), ("band", band)):
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit)):
            raise ValueError(f"{name}은(는) 유한한 숫자여야 합니다.")
    if lsl is not None and usl is not None and not lsl < usl:
        raise ValueError("규격 하한은 상한보다 작아야 합니다.")
    if band is not None and (band <= 0 or (lsl is None and usl is None)):
        raise ValueError("경계 구간(band)은 양수이며 규격 하한이나 상한과 함께 지정해야 합니다.")
    error = p - a
    total = float(np.sum((a - a.mean()) ** 2))
    def summary(mask):
        e = error[mask]
        return {"n": int(mask.sum()), "mae": float(np.mean(np.abs(e))), "rmse": float(math.sqrt(np.mean(e ** 2))),
                "bias": float(np.mean(e)), "max_abs_error": float(np.max(np.abs(e)))} if mask.any() else {"n": 0}
    result = {"analysis": "regression_metrics", "actual": actual, "predicted": predicted, "unit": unit, "n": len(a),
              "overall": {**summary(np.ones(len(a), bool)), "r_squared": 1 - float(np.sum(error ** 2)) / total if total > 0 else None},
              "method": "Errors are predicted minus actual; the boundary band holds rows whose actual value lies within band of a limit",
              "warnings": []}
    if lsl is not None or usl is not None:
        def out(values):
            return (values < lsl if lsl is not None else False) | (values > usl if usl is not None else False)
        actual_out, predicted_out = out(a), out(p)
        result["decisions"] = {"limits": {"lsl": lsl, "usl": usl}, "actual_out_of_spec": int(actual_out.sum()),
                               "predicted_out_of_spec": int(predicted_out.sum()),
                               "missed_out_of_spec": int(np.sum(actual_out & ~predicted_out)),
                               "false_alarms": int(np.sum(~actual_out & predicted_out)),
                               "agreement": float(np.mean(actual_out == predicted_out))}
        if result["decisions"]["missed_out_of_spec"]:
            result["warnings"].append(f"{result['decisions']['missed_out_of_spec']} out-of-spec rows would pass on the predicted value.")
        if band is not None:
            near = np.zeros(len(a), bool)
            for limit in (lsl, usl):
                if limit is not None:
                    near |= np.abs(a - limit) <= band
            result["boundary"] = {"band": band, **summary(near)}
            if near.any() and result["boundary"]["mae"] > 1.5 * result["overall"]["mae"]:
                result["warnings"].append("Errors near the specification limits are much larger than overall; the overall MAE overstates how well pass/fail decisions are predicted.")
            if not near.any():
                result["warnings"].append("No actual value lies within the boundary band; the error where decisions matter is not evaluated.")
    result["warnings"].append("These rows evaluate the model only if they were not used to train it; use later lots for the final check.")
    return result


def _design(header, rows, features, impute_median):
    if not isinstance(features, list) or not 1 <= len(features) <= 2000 or len(set(features)) != len(features):
        raise ValueError("입력 변수(features)는 중복 없는 1~2000개의 열 이름이어야 합니다.")
    if len(rows) * len(features) > MAX_CELLS:
        raise ValueError(f"행 수 × 입력 변수 수는 {MAX_CELLS:,} 이하여야 합니다.")
    columns = [_column(header, name, "입력 변수") for name in features]
    x = np.empty((len(rows), len(features)))
    for i, (line, row) in enumerate(rows):
        for j, c in enumerate(columns):
            value = row[c]
            try:
                x[i, j] = float(value) if value and value.lower() not in {"nan", "na"} else np.nan
            except ValueError as exc:
                raise ValueError(f"CSV {line}행 {features[j]}: 숫자가 아닙니다.") from exc
            if np.isinf(x[i, j]):
                raise ValueError(f"CSV {line}행 {features[j]}: 유한한 숫자가 아닙니다.")
    missing = int(np.isnan(x).sum())
    if missing and not impute_median:
        raise ValueError(f"입력 변수에 빈 값이 {missing:,}개 있습니다. 행을 정리하거나 impute_median=true로 학습 구간 중앙값 대치를 명시하세요.")
    return x, missing


def split_validation(text, target, features, task="classification", positive="1", group=None, order=None, folds=5,
                     impute_median=False, seed=0):
    """The same fixed baseline model scored under random, lot-grouped and time-forward splits.
    A random split that scores much better than the others is leaking lot or time information."""
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.model_selection import GroupKFold, KFold, StratifiedKFold, TimeSeriesSplit
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if task not in {"classification", "regression"}:
        raise ValueError("task는 classification 또는 regression이어야 합니다.")
    if isinstance(folds, bool) or not isinstance(folds, int) or not 2 <= folds <= 10:
        raise ValueError("분할 수(folds)는 2~10 사이의 정수여야 합니다.")
    if not isinstance(impute_median, bool):
        raise ValueError("impute_median은 true 또는 false여야 합니다.")
    header, rows = _table(text)
    rows = _ordered(header, rows, order)
    x, missing = _design(header, rows, features, impute_median)
    if task == "classification":
        y = _labels(header, rows, target, positive)
        if min(int(y.sum()), int((~y).sum())) < folds:
            raise ValueError(f"불량과 정상이 각각 {folds}개 이상 있어야 {folds}개로 나눌 수 있습니다.")
        model = lambda: make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                                      LogisticRegression(class_weight="balanced", max_iter=2000))
    else:
        y = _numbers(header, rows, target, "목표값")
        model = lambda: make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=1.0))
    schemes = {"random": list((StratifiedKFold if task == "classification" else KFold)(folds, shuffle=True, random_state=seed)
                              .split(x, y))}
    if group is not None:
        index = _column(header, group, "그룹")
        groups = np.asarray([row[index] for _, row in rows])
        if len(set(groups)) < 2:
            raise ValueError("그룹 분할에는 2개 이상의 그룹이 필요합니다.")
        schemes["group"] = list(GroupKFold(min(folds, len(set(groups)))).split(x, y, groups))
    if order is not None:
        schemes["time_forward"] = list(TimeSeriesSplit(folds).split(x))
    results, warnings = {}, []
    for name, splits in schemes.items():
        predicted = np.full(len(y), np.nan)
        skipped = 0
        for train, test in splits:
            if task == "classification" and (y[train].all() or not y[train].any()):
                skipped += 1
                continue
            fitted = model().fit(x[train], y[train])
            predicted[test] = fitted.predict_proba(x[test])[:, 1] if task == "classification" else fitted.predict(x[test])
        tested = ~np.isnan(predicted)
        entry = {"folds": len(splits), "skipped_folds": skipped, "tested_rows": int(tested.sum())}
        if task == "classification":
            yt, st = y[tested], predicted[tested]
            if yt.all() or not yt.any():
                entry["roc_auc"] = None
            else:
                entry["roc_auc"], entry["average_precision"] = _ranking(yt, st)
                entry.update({key: _confusion(yt, st >= 0.5)[key] for key in ("recall", "specificity", "balanced_accuracy", "precision")})
        else:
            e = predicted[tested] - y[tested]
            entry.update({"mae": float(np.mean(np.abs(e))), "rmse": float(math.sqrt(np.mean(e ** 2)))})
        results[name] = entry
    key = "roc_auc" if task == "classification" else "mae"
    for name in ("group", "time_forward"):
        if name in results and results[name].get(key) is not None and results["random"].get(key) is not None:
            optimistic = (results["random"][key] - results[name][key] > 0.05) if task == "classification" else (results[name][key] > 1.25 * results["random"][key])
            results[name]["random_is_optimistic"] = bool(optimistic)
            if optimistic:
                warnings.append(f"The random split scores clearly better than the {name.replace('_', '-')} split; rows from the same "
                                f"{'lot' if name == 'group' else 'period'} leak into training. Report the {name.replace('_', '-')} estimate.")
    if "time_forward" in results:
        warnings.append("Time-forward folds never test the earliest block; their scores cover later rows only.")
    if missing:
        warnings.append(f"{missing} missing feature values were replaced by training-fold medians.")
    warnings.append("The baseline model is fixed (standardized logistic or ridge regression); compare split schemes with it, not model quality.")
    return {"analysis": "split_validation", "task": task, "target": target, "features": len(features), "n": len(y),
            "schemes": results, "model": "median imputation + standardization + " + (
                "class-balanced logistic regression" if task == "classification" else "ridge regression (alpha 1)"),
            "method": "Out-of-fold predictions under random (stratified) K-fold, GroupKFold by the group column and TimeSeriesSplit by the order column",
            "seed": seed, "warnings": warnings}


FUNCTIONS = {"classification_metrics": classification_metrics, "regression_metrics": regression_metrics,
             "split_validation": split_validation}
OPTIONS = {"classification_metrics": {"label", "score", "positive", "thresholds", "miss_cost", "false_alarm_cost"},
           "regression_metrics": {"actual", "predicted", "lsl", "usl", "band", "unit"},
           "split_validation": {"target", "features", "task", "positive", "group", "order", "folds", "impute_median", "seed"}}
