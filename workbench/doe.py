"""Small, reviewable experimental designs and coded response-surface models.

Rust constructs designs, fits by column-pivoted Householder QR, computes statistical
inference and diagnostics, and searches bounded candidates. Python handles records and CSV.
"""
from __future__ import annotations

import csv
import io
import json
import math

import numpy as np

try:
    import workbench_native as native
except ImportError:  # pragma: no cover
    native = None

MAX_ROWS = 20000
MAX_CSV_BYTES = 2 * 1024 * 1024


def _kernel():
    if native is None or not all(hasattr(native, name) for name in ("doe_design", "doe_fit", "doe_optimize", "doe_predict")):
        raise ValueError("DOE Rust 계산 커널이 없습니다. Rust 환경에서 setup.sh를 다시 실행하세요.")
    return native


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise ValueError(f"{name}: 유한한 숫자를 입력하세요.")
    result = float(value)
    if not math.isfinite(result) or abs(result) > 1e100:
        raise ValueError(f"{name}: 유한하고 절댓값이 10^100 이하인 숫자를 입력하세요.")
    if 0 < abs(result) < 1e-100:
        raise ValueError(f"{name}: 숫자가 너무 작습니다. 단위를 바꿔 10^-100 이상의 크기로 입력하세요.")
    return result


def _integer(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{name}: {lo}~{hi} 사이의 정수를 입력하세요.")
    return value


def _factors(factors, maximum=6):
    if not isinstance(factors, (list, tuple)) or not 2 <= len(factors) <= maximum:
        raise ValueError(f"인자는 2~{maximum}개 지정하세요.")
    out, names = [], set()
    for factor in factors:
        if not isinstance(factor, dict) or set(factor) != {"name", "low", "high"}:
            raise ValueError("각 인자는 name, low, high로 지정하세요.")
        name = factor["name"]
        if (not isinstance(name, str) or name != name.strip() or not 1 <= len(name) <= 80
                or any(ord(c) < 32 for c in name) or name[0] in "=+-@"
                or name in {"run_order", "point_type", "response", "Intercept"} or ":" in name or "^" in name or name in names):
            raise ValueError("인자 이름은 고유한 1~80자여야 하며 run_order, point_type, response는 사용할 수 없습니다.")
        low, high = _number(factor["low"], name + " low"), _number(factor["high"], name + " high")
        if not low < high or not math.isfinite(high - low):
            raise ValueError(f"{name}: 하한은 상한보다 작아야 합니다.")
        if high - low < max(abs(low), abs(high), 1e-100) * 64 * np.finfo(float).eps:
            raise ValueError(f"{name}: 범위가 수치 정밀도보다 좁습니다. 인자의 단위나 기준점을 조정하세요.")
        out.append({"name": name, "low": low, "high": high})
        names.add(name)
    return out


def _real(point, factors):
    return {f["name"]: f["low"] + (float(x) + 1.0) * 0.5 * (f["high"] - f["low"]) for x, f in zip(point, factors)}


def _finite(result):
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, OverflowError) as exc:
        raise ValueError("계산 결과가 유한하지 않습니다. 반응값의 단위나 크기를 조정하세요.") from exc
    return result


def design(factors, kind="factorial", center_points=3, replicates=1, seed=1):
    """Randomized full factorial or face-centred CCD; centre_points is per replicate."""
    if kind not in {"factorial", "central_composite"}:
        raise ValueError("설계는 factorial 또는 central_composite를 선택하세요.")
    factors = _factors(factors, 4 if kind == "central_composite" else 6)
    center_points = _integer(center_points, "중심점 수", 1 if kind == "central_composite" else 0, 20)
    replicates = _integer(replicates, "반복 수", 1, 10)
    seed = _integer(seed, "시드", 0, 2 ** 63 - 1)
    kernel = _kernel()
    try:
        raw = kernel.doe_design(len(factors), kind, center_points, replicates, seed)
    except ValueError as exc:
        raise ValueError(f"실험 설계를 만들 수 없습니다: {exc}") from exc
    runs = [{"run_order": i + 1, "point_type": point_type,
             "values": _real(point, factors), "coded": dict(zip((f["name"] for f in factors), point))}
            for i, (point, point_type) in enumerate(zip(raw["points"], raw["types"]))]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    names = [f["name"] for f in factors]
    writer.writerow(["run_order", "point_type", *names, "response"])
    for run in runs:
        writer.writerow([run["run_order"], run["point_type"], *(format(run["values"][name], ".17g") for name in names), ""])
    warnings = ["This is an experimental plan, not executed or verified measurements. Check practical feasibility and keep factor units consistent.",
                "Runs are randomized without blocks. Replicates should be independent experimental runs; repeated instrument readings alone are not independent replicates."]
    if kind == "central_composite":
        warnings.append("Face-centred CCD uses alpha=1 and stays inside factor bounds; it is not a rotatable CCD.")
    else:
        warnings.append("Two-level factorial runs, even with centres, cannot separately identify all squared-factor terms. Use face-centred CCD for a full quadratic model.")
    return _finite({"analysis": "doe_design", "kind": kind, "factors": factors, "center_points": center_points,
                    "replicates": replicates, "seed": seed, "total_runs": len(runs), "runs": runs, "csv": output.getvalue(),
                    "coding": "low = -1, high = +1, midpoint = 0; center_points per replicate",
                    "method": "Full 2-level factorial" if kind == "factorial" else "Face-centred central composite (alpha=1)",
                    "kernel": {"engine": "rust", "version": kernel.__version__}, "warnings": warnings})


def _data(text, factors, response):
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError("측정 CSV는 비어 있지 않은 2 MiB 이하의 텍스트여야 합니다.")
    if not isinstance(response, str) or not response.strip() or response in {f["name"] for f in factors}:
        raise ValueError("인자와 다른 반응값 열을 지정하세요.")
    if response != response.strip() or response[0] in "=+-@" or any(ord(c) < 32 for c in response):
        raise ValueError("반응값 열 이름에는 수식 접두어, 제어 문자와 양끝 공백을 사용할 수 없습니다.")
    try:
        reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), strict=True)
        header = [name.strip() for name in next(reader)]
        if not 1 <= len(header) <= 64 or any(not name for name in header) or len(set(header)) != len(header):
            raise ValueError("CSV 머리글은 중복 없는 1~64개 열이어야 합니다.")
        names = [f["name"] for f in factors] + [response]
        if any(name not in header for name in names):
            raise ValueError("CSV에 지정한 인자 또는 반응값 열이 없습니다.")
        columns = [header.index(name) for name in names]
        rows = []
        for line, row in enumerate(reader, 2):
            if len(rows) >= MAX_ROWS:
                raise ValueError(f"CSV는 {MAX_ROWS}개 관측 행 이하여야 합니다.")
            if len(row) != len(header):
                raise ValueError(f"CSV {line}행의 열 수가 맞지 않습니다. 빈 행도 제거하거나 채우세요.")
            values = []
            for name, col in zip(names, columns):
                try:
                    value = float(row[col])
                except ValueError as exc:
                    raise ValueError(f"CSV {line}행 {name}: 누락되었거나 숫자가 아닙니다. 행을 자동 제외하지 않습니다.") from exc
                values.append(_number(value, f"CSV {line}행 {name}"))
            rows.append(values)
    except (csv.Error, StopIteration) as exc:
        raise ValueError("CSV 형식을 읽을 수 없습니다.") from exc
    if not rows:
        raise ValueError("CSV에 측정 행이 없습니다.")
    values = np.asarray(rows, dtype=float)
    coded = np.empty((len(rows), len(factors)))
    for j, factor in enumerate(factors):
        coded[:, j] = 2.0 * ((values[:, j] - factor["low"]) / (factor["high"] - factor["low"])) - 1.0
        invalid = np.flatnonzero(np.abs(coded[:, j]) > 1.0 + 1e-12)
        if invalid.size:
            raise ValueError(f"CSV {int(invalid[0]) + 2}행 {factor['name']}: 지정한 실험 범위를 벗어났습니다.")
    coded = np.clip(coded, -1.0, 1.0)
    return coded, np.ascontiguousarray(values[:, -1]), [name for name in header if name not in names]



def _prediction_points(points, factors):
    if points is None:
        return [], []
    if not isinstance(points, list) or len(points) > 50:
        raise ValueError("예측 조건은 인자별 값으로 된 목록이며 최대 50개입니다.")
    names = {factor["name"] for factor in factors}
    settings, coded = [], []
    for index, point in enumerate(points, 1):
        if not isinstance(point, dict) or set(point) != names:
            raise ValueError(f"예측 조건 {index}: 모든 인자의 값을 하나씩 입력하세요.")
        real, row = {}, []
        for factor in factors:
            name = factor["name"]
            value = _number(point[name], f"예측 조건 {index} {name}")
            if not factor["low"] <= value <= factor["high"]:
                raise ValueError(f"예측 조건 {index} {name}: 지정한 인자 범위 안의 값을 입력하세요.")
            real[name] = value
            row.append(2.0 * (value - factor["low"]) / (factor["high"] - factor["low"]) - 1.0)
        settings.append(real)
        coded.append(row)
    return settings, coded


def _compare_models(kernel, coded, observed, selected, current):
    comparisons = []
    for model in ("linear", "interaction", "quadratic"):
        try:
            fitted = current if model == selected else kernel.doe_fit(np.ascontiguousarray(coded), observed, model)
        except ValueError:
            comparisons.append({"model": model, "status": "unavailable",
                                "reason": "현재 인자 수·실험점으로 추정할 수 없습니다. 독립된 점을 추가하세요."})
            continue
        comparisons.append({"model": model, "status": "available", "terms": len(fitted["coefficients"]),
                            "df_residual": fitted["degrees"], "rmse": fitted["rmse"],
                            "r_squared": fitted["r_squared"], "adjusted_r_squared": fitted["adjusted_r_squared"],
                            "predicted_r_squared": fitted["diagnostics"]["predicted_r_squared"],
                            "lack_of_fit_p_value": fitted["anova"]["lack_of_fit"]["p_value"]})
    return comparisons


def _research_steps(fit, extrapolated):
    issues = []
    def add(code, message):
        issues.append({"code": code, "message": message})
    lack = fit["anova"]["lack_of_fit"]["p_value"]
    if lack is not None and lack < .05:
        add("lack_of_fit", "적합성 결여 p값이 0.05 미만입니다. 더 높은 차수의 모델이나 추가 실험점을 검토하세요.")
    if fit["anova"]["pure_error"]["df"] == 0:
        add("no_pure_error", "같은 조건의 독립 반복이 없어 순수 오차와 적합성 결여를 비교할 수 없습니다.")
    if fit["exact_fit"]:
        add("exact_fit", "잔차가 수치 정밀도 수준입니다. 독립 반복으로 실제 실험 변동을 확인하세요.")
    if fit["constant_response"]:
        add("constant_response", "반응값이 일정합니다. 측정 분해능과 실험 범위를 검토하세요.")
    if fit["degrees"] < 5:
        add("low_degrees", "잔차 자유도가 5 미만입니다. 독립 반복이나 추가 실험점을 검토하세요.")
    if fit["diagnostics"]["unusual_rows"]:
        add("unusual_rows", "잔차·영향도 표시 행의 원자료와 실험 조건을 검토하세요. 자동 제외하지 않습니다.")
    predicted = fit["diagnostics"]["predicted_r_squared"]
    if predicted is not None and predicted < 0:
        add("poor_prediction", "예측 R²가 음수입니다. 추천 조건을 실행하기 전에 모델과 설계를 검토하세요.")
    if fit["condition_indicator"] > 1e6:
        add("conditioning", "계수 추정이 불안정합니다. 인자를 구별할 수 있는 실험점을 추가하세요.")
    if extrapolated:
        add("extrapolation", "추천 조건이 일부 인자의 관측 범위를 벗어납니다. 실행 가능성을 검토하세요.")
    return {"stage": "model_review" if issues else "confirmation", "issues": issues,
            "next_steps": (["모델 비교표와 잔차를 확인하고 사용할 모델을 직접 선택하세요."] if issues else []) +
                ["추천 조건의 확인 실험표를 저장하고 독립된 실험으로 반응값을 채우세요.",
                 "확인 실험 결과를 예측값·예측구간과 비교하고 다음 실험 설계를 결정하세요."],
            "automatic_execution": False}


def _confirmation_csv(settings, factors, response):
    # Unique metadata names allow arbitrary response headers without duplicate columns.
    names = [factor["name"] for factor in factors]
    order = "run_order" if response != "run_order" else "confirmation_order"
    while order in names or order == response:
        order = "_" + order
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([order, *names, response])
    for index in range(1, 4):
        writer.writerow([index, *(format(settings[name], ".17g") for name in names), ""])
    return out.getvalue()


def analyze(text, factors, response, model="quadratic", objective="maximize", target=None, prediction_points=None):
    """Fit observed runs and recommend an unvalidated candidate within explicit factor bounds."""
    if model not in {"linear", "interaction", "quadratic"}:
        raise ValueError("모델은 linear, interaction 또는 quadratic을 선택하세요.")
    if objective not in {"maximize", "minimize", "target"}:
        raise ValueError("목표는 maximize, minimize 또는 target을 선택하세요.")
    if objective == "target":
        target = _number(target, "목표 반응값")
    elif target is not None:
        raise ValueError("target 값은 목표값 맞추기에서만 지정하세요.")
    factors = _factors(factors, 4 if model == "quadratic" else 6)
    coded, observed, ignored = _data(text, factors, response)
    names = [f["name"] for f in factors]
    prediction_settings, prediction_coded = _prediction_points(prediction_points, factors)
    terms = ["Intercept", *names]
    if model != "linear":
        terms += [f"{a}:{b}" for i, a in enumerate(names) for b in names[i + 1:]]
    if model == "quadratic":
        terms += [f"{name}^2" for name in names]
    n, p = len(observed), len(terms)
    if n <= p:
        raise ValueError(f"{p}개 계수를 추정하려면 {p + 1}개 이상의 독립 관측 행이 필요합니다.")
    kernel = _kernel()
    try:
        fit = kernel.doe_fit(np.ascontiguousarray(coded), observed, model)
        candidate = kernel.doe_optimize(np.asarray(fit["coefficients"], dtype=float), len(factors), model, objective, target)
    except ValueError as exc:
        raise ValueError(f"반응표면을 추정할 수 없습니다. 독립된 실험점을 추가하거나 단순한 모델을 선택하세요: {exc}") from exc
    degrees = fit["degrees"]
    constant, exact = fit["constant_response"], fit["exact_fit"]
    coefficients = [{"term": term, "estimate": estimate, "standard_error": error, **inference}
                    for term, estimate, error, inference in zip(terms, fit["coefficients"],
                        fit["standard_errors"], fit["coefficients_inference"])]
    comparisons = _compare_models(kernel, coded, observed, model, fit)
    prediction_data = kernel.doe_predict(np.ascontiguousarray(coded), observed, model,
                                         np.asarray([candidate["coded"], *prediction_coded], dtype=float))
    candidate_prediction = prediction_data[0]
    predictions = [{"settings": settings, **result} for settings, result
                   in zip(prediction_settings, prediction_data[1:])]
    warnings = ["The recommended settings are a model candidate, not a validated experiment or a guaranteed global optimum. Confirm with new independent runs.",
                "Coefficient confidence intervals assume independent, normally distributed residuals with constant variance and a correctly specified model. No automatic row removal or outlier rejection is performed.",
                "Optimization is constrained to declared factor bounds, but may leave the jointly observed design region; assess experimental feasibility and extrapolation before use."]
    if constant:
        warnings.append("All observed responses are constant: R-squared and factor-effect significance are not interpretable.")
    if exact:
        warnings.append("Residual error is at numerical precision. Significance tests are omitted; a perfect fitted surface does not establish experimental precision.")
    if degrees < 5:
        warnings.append("Fewer than five residual degrees of freedom: uncertainty estimates are fragile; add independent replicate runs.")
    if fit["condition_indicator"] > 1e6:
        warnings.append("The model matrix is poorly conditioned; coefficient estimates are sensitive to small measurement changes.")
    point = np.asarray(candidate["coded"], dtype=float)
    outside = [name for j, name in enumerate(names) if point[j] < coded[:, j].min() - 1e-10 or point[j] > coded[:, j].max() + 1e-10]
    if outside:
        warnings.append("The candidate extrapolates beyond observed marginal ranges for: " + ", ".join(outside) + ".")
    if objective == "target" and abs(candidate["predicted_response"] - target) > 1e-6 * max(float(np.max(np.abs(observed))), abs(target)):
        warnings.append("The bounded search did not reach the requested target; this is the closest candidate found, not a proof that the target is unattainable.")
    workflow = _research_steps(fit, outside)
    if fit["anova"]["pure_error"]["df"] == 0:
        warnings.append("No exact replicated factor settings: pure error and lack-of-fit significance cannot be estimated. Add independent replicate runs.")
    if fit["diagnostics"]["unusual_rows"]:
        warnings.append("Residual or influence diagnostics flagged observations for review; no rows were removed. Diagnostic thresholds are heuristics, not automatic rejection rules.")
    warnings.append("Prediction intervals describe one new observation under the fitted model assumptions; they exclude model selection and optimization uncertainty. Lack-of-fit uses exactly matching factor settings.")
    return _finite({"analysis": "doe_response_surface", "factors": factors, "response": response, "model": model,
                    "objective": objective, "confidence": 0.95, "coefficients": coefficients,
                    "fit": {"n": n, "terms": p, "df_residual": degrees, "r_squared": fit["r_squared"],
                            "adjusted_r_squared": fit["adjusted_r_squared"],
                            "predicted_r_squared": fit["diagnostics"]["predicted_r_squared"], "press": fit["diagnostics"]["press"],
                            "rmse": fit["rmse"], "sse": fit["sse"], "sst": fit["sst"],
                            "condition_indicator": fit["condition_indicator"]},
                    "recommendation": {"settings": _real(point, factors), "coded": dict(zip(names, candidate["coded"])),
                                       "predicted_response": candidate["predicted_response"], "objective": objective,
                                       "mean_confidence_interval": candidate_prediction["confidence_interval"],
                                       "prediction_interval": candidate_prediction["prediction_interval"],
                                       "target": target, "validated": False, "extrapolated_factors": outside,
                                       "search": {"method": "Multi-start exact coordinate search on the fitted polynomial", "starts": candidate["starts"],
                                                  "domain": "Declared bounds; coded [-1,1] for every factor", "global_optimum_guaranteed": False}},
                    "diagnostics": {"observed": observed.tolist(), "fitted": fit["fitted"], "residuals": fit["residuals"], **fit["diagnostics"]},
                    "anova": fit["anova"], "model_comparison": comparisons, "predictions": predictions,
                    "research_workflow": workflow,
                    "confirmation_csv": _confirmation_csv(_real(point, factors), factors, response),
                    "coding": "Coefficients use x=(value-midpoint)/(half range); response units remain those of the input CSV.",
                    "ignored_columns": ignored, "rows_dropped": 0,
                    "kernel": {"engine": "rust", "version": kernel.__version__, "solver": "Column-pivoted Householder QR with column scaling",
                               "inference": "Rust Student-t/F distributions, QR covariance, pure error, PRESS"},
                    "warnings": warnings})
