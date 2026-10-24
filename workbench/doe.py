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
    values, ignored = _table(text, factors, response)
    coded = np.empty((len(values), len(factors)))
    for j, factor in enumerate(factors):
        coded[:, j] = 2.0 * ((values[:, j] - factor["low"]) / (factor["high"] - factor["low"])) - 1.0
        invalid = np.flatnonzero(np.abs(coded[:, j]) > 1.0 + 1e-12)
        if invalid.size:
            raise ValueError(f"CSV {int(invalid[0]) + 2}행 {factor['name']}: 지정한 실험 범위를 벗어났습니다.")
    coded = np.clip(coded, -1.0, 1.0)
    return coded, np.ascontiguousarray(values[:, -1]), ignored


def _table(text, factors, response):
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
    return np.asarray(rows, dtype=float), [name for name in header if name not in names]



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


MIXTURE_MODELS = ("scheffe_linear", "scheffe_quadratic", "scheffe_special_cubic")
MIXTURE_KINDS = ("simplex_lattice", "simplex_centroid", "extreme_vertices")


def _mixture_kernel():
    kernel = _kernel()
    if not all(hasattr(kernel, name) for name in ("mixture_design", "mixture_region", "mixture_optimize", "desirability_optimize")):
        raise ValueError("혼합물 실험 Rust 커널이 없습니다. `.venv/bin/python -m pip install ./native`로 다시 빌드하세요.")
    return kernel


def _components(components):
    """Proportion bounds of 2-6 components; returns the declared bounds and the ones implied by Σx = 1."""
    out = _factors(components, 6)
    for item in out:
        if not 0.0 <= item["low"] < item["high"] <= 1.0:
            raise ValueError(f"{item['name']}: 성분 비율의 하한·상한은 0 이상 1 이하의 비율로 입력하세요.")
    try:
        lower, upper, simplex = _mixture_kernel().mixture_region([c["low"] for c in out], [c["high"] for c in out])
    except ValueError as exc:
        raise ValueError("성분 하한의 합은 1보다 작고 상한의 합은 1보다 커야 합니다(실행 가능한 배합 영역이 없습니다).") from exc
    region = {"lower": list(lower), "upper": list(upper), "simplex": bool(simplex), "pseudo_scale": 1.0 - sum(lower)}
    return out, region


def _pseudo(x, region):
    return (np.asarray(x, dtype=float) - np.asarray(region["lower"])) / region["pseudo_scale"]


def mixture_design(components, kind="simplex_lattice", degree=2, axial=False, centroid=True, replicates=1, seed=1):
    """Randomized simplex-lattice, simplex-centroid or extreme-vertices mixture design in proportions."""
    if kind not in MIXTURE_KINDS:
        raise ValueError("혼합물 설계는 simplex_lattice, simplex_centroid 또는 extreme_vertices를 선택하세요.")
    components, region = _components(components)
    degree = _integer(degree, "차수", 1, 4 if kind == "simplex_lattice" else 2)
    replicates = _integer(replicates, "반복 수", 1, 10)
    seed = _integer(seed, "시드", 0, 2 ** 63 - 1)
    if not isinstance(axial, bool) or not isinstance(centroid, bool):
        raise ValueError("axial과 centroid는 true 또는 false입니다.")
    if kind != "extreme_vertices" and not region["simplex"]:
        raise ValueError("상한이 배합 영역을 자르므로 simplex 설계를 쓸 수 없습니다. extreme_vertices를 선택하세요.")
    kernel = _mixture_kernel()
    try:
        raw = kernel.mixture_design([c["low"] for c in components], [c["high"] for c in components], kind, degree, axial, centroid, replicates, seed)
    except ValueError as exc:
        raise ValueError(f"혼합물 설계를 만들 수 없습니다: {exc}") from exc
    names = [c["name"] for c in components]
    runs = [{"run_order": i + 1, "point_type": point_type, "values": dict(zip(names, point)),
             "pseudo": dict(zip(names, _pseudo(point, region).tolist()))}
            for i, (point, point_type) in enumerate(zip(raw["points"], raw["types"]))]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["run_order", "point_type", *names, "response"])
    for run in runs:
        writer.writerow([run["run_order"], run["point_type"], *(format(run["values"][name], ".17g") for name in names), ""])
    labels = {"simplex_lattice": f"Simplex-lattice {{{len(names)}, {degree}}}", "simplex_centroid": "Simplex-centroid",
              "extreme_vertices": "Extreme vertices" + (" with edge midpoints" if degree == 2 else "")}
    warnings = ["This is an experimental plan, not executed or verified measurements. Proportions are fractions of the whole mixture and sum to one.",
                "Runs are randomized without blocks. Replicates should be independent mixtures prepared separately."]
    if any(c["low"] > 0 for c in components):
        warnings.append("Lower bounds are handled with L-pseudocomponents; models are fitted in pseudocomponents and reported as such.")
    if kind == "simplex_lattice" and degree == 2 and not axial and not centroid:
        warnings.append("A {q, 2} lattice alone has no interior blends and no degrees of freedom to check a quadratic model; add the centroid or axial check blends.")
    return _finite({"analysis": "doe_mixture_design", "kind": kind, "degree": degree, "components": components, "region": region,
                    "axial": axial, "centroid": centroid, "replicates": replicates, "seed": seed, "total_runs": len(runs), "runs": runs,
                    "csv": output.getvalue(), "method": labels[kind] + (" + axial check blends" if axial else ""),
                    "coding": "values are proportions; pseudo = (x - L) / (1 - ΣL) with L the lower bounds implied by Σx = 1",
                    "kernel": {"engine": "rust", "version": kernel.__version__}, "warnings": warnings})


def _mixture_data(text, components, region, response):
    values, ignored = _table(text, components, response)
    x = values[:, :-1]
    for row, total in enumerate(x.sum(axis=1)):
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"CSV {row + 2}행: 성분 비율의 합이 1이 아닙니다(합 {total:.6g}). 각 성분을 전체에 대한 비율로 입력하세요.")
    for j, component in enumerate(components):
        low, high = region["lower"][j], region["upper"][j]
        bad = np.flatnonzero((x[:, j] < low - 1e-9) | (x[:, j] > high + 1e-9))
        if bad.size:
            raise ValueError(f"CSV {int(bad[0]) + 2}행 {component['name']}: 지정한 성분 범위를 벗어났습니다.")
    z = np.clip(_pseudo(x, region), 0.0, 1.0)
    return np.ascontiguousarray(z), np.ascontiguousarray(values[:, -1]), ignored


def _mixture_terms(names, model):
    terms = list(names)
    if model != "scheffe_linear":
        terms += [f"{a}:{b}" for i, a in enumerate(names) for b in names[i + 1:]]
    if model == "scheffe_special_cubic":
        terms += [f"{a}:{b}:{c}" for i, a in enumerate(names) for j, b in enumerate(names[i + 1:], i + 1) for c in names[j + 1:]]
    return terms


def _mixture_points(points, components, region):
    if points is None:
        return [], []
    if not isinstance(points, list) or len(points) > 50:
        raise ValueError("예측 조건은 성분별 비율로 된 목록이며 최대 50개입니다.")
    names = [c["name"] for c in components]
    settings, pseudo = [], []
    for index, point in enumerate(points, 1):
        if not isinstance(point, dict) or set(point) != set(names):
            raise ValueError(f"예측 조건 {index}: 모든 성분의 비율을 하나씩 입력하세요.")
        x = [_number(point[name], f"예측 조건 {index} {name}") for name in names]
        if abs(sum(x) - 1.0) > 1e-6 or any(not region["lower"][j] - 1e-9 <= v <= region["upper"][j] + 1e-9 for j, v in enumerate(x)):
            raise ValueError(f"예측 조건 {index}: 비율의 합이 1이고 각 성분이 범위 안에 있어야 합니다.")
        settings.append(dict(zip(names, x)))
        pseudo.append(np.clip(_pseudo(x, region), 0.0, 1.0).tolist())
    return settings, pseudo


def analyze_mixture(text, components, response, model="scheffe_quadratic", objective="maximize", target=None, prediction_points=None):
    """Scheffé fit in L-pseudocomponents, ANOVA and a bounded, unvalidated blend recommendation."""
    if model not in MIXTURE_MODELS:
        raise ValueError("혼합물 모델은 scheffe_linear, scheffe_quadratic 또는 scheffe_special_cubic을 선택하세요.")
    if objective not in {"maximize", "minimize", "target"}:
        raise ValueError("목표는 maximize, minimize 또는 target을 선택하세요.")
    if objective == "target":
        target = _number(target, "목표 반응값")
    elif target is not None:
        raise ValueError("target 값은 목표값 맞추기에서만 지정하세요.")
    components, region = _components(components)
    names = [c["name"] for c in components]
    if model == "scheffe_special_cubic" and len(names) < 3:
        raise ValueError("special cubic 모델에는 성분이 3개 이상 필요합니다.")
    z, observed, ignored = _mixture_data(text, components, region, response)
    terms = _mixture_terms(names, model)
    n, p = len(observed), len(terms)
    if n <= p:
        raise ValueError(f"{p}개 계수를 추정하려면 {p + 1}개 이상의 독립 관측 행이 필요합니다.")
    kernel = _mixture_kernel()
    lower, upper = [c["low"] for c in components], [c["high"] for c in components]
    try:
        fit = kernel.doe_fit(z, observed, model)
        candidate = kernel.mixture_optimize(lower, upper, list(fit["coefficients"]), model, objective, target)
    except ValueError as exc:
        raise ValueError(f"혼합물 모델을 추정할 수 없습니다. 독립된 배합을 추가하거나 단순한 모델을 선택하세요: {exc}") from exc
    linear = set(names)
    coefficients = []
    for term, estimate, error, inference in zip(terms, fit["coefficients"], fit["standard_errors"], fit["coefficients_inference"]):
        row = {"term": term, "estimate": estimate, "standard_error": error, **inference}
        if term in linear:
            # A linear blending coefficient is the fitted response of the pure pseudocomponent, not an effect.
            row.update(t_statistic=None, p_value=None)
        coefficients.append(row)
    comparisons = []
    for other in MIXTURE_MODELS:
        if other == "scheffe_special_cubic" and len(names) < 3:
            continue
        try:
            fitted = fit if other == model else kernel.doe_fit(z, observed, other)
        except ValueError:
            comparisons.append({"model": other, "status": "unavailable", "reason": "현재 배합 점으로 추정할 수 없습니다. 독립된 배합을 추가하세요."})
            continue
        comparisons.append({"model": other, "status": "available", "terms": len(fitted["coefficients"]), "df_residual": fitted["degrees"],
                            "rmse": fitted["rmse"], "r_squared": fitted["r_squared"], "adjusted_r_squared": fitted["adjusted_r_squared"],
                            "predicted_r_squared": fitted["diagnostics"]["predicted_r_squared"],
                            "lack_of_fit_p_value": fitted["anova"]["lack_of_fit"]["p_value"]})
    settings, user_pseudo = _mixture_points(prediction_points, components, region)
    best_pseudo = np.clip(np.asarray(candidate["pseudo"], dtype=float), 0.0, 1.0)
    predicted = kernel.doe_predict(z, observed, model, np.asarray([best_pseudo, *user_pseudo], dtype=float))
    outside = [name for j, name in enumerate(names) if best_pseudo[j] < z[:, j].min() - 1e-10 or best_pseudo[j] > z[:, j].max() + 1e-10]
    recommendation = dict(zip(names, candidate["point"]))
    warnings = ["The recommended blend is a model candidate, not a validated experiment or a guaranteed global optimum. Confirm with new independent mixtures.",
                "Scheffé models have no intercept; coefficients are in L-pseudocomponents. Linear blending coefficients estimate pure-pseudocomponent responses, so their tests against zero are omitted.",
                "R-squared and the regression F test compare the model with the mean response, as for any model containing a constant through Σx = 1.",
                "Effects of individual components are not separable in a mixture: changing one proportion changes the others."]
    if fit["degrees"] < 5:
        warnings.append("Fewer than five residual degrees of freedom: uncertainty estimates are fragile; add independent replicate blends.")
    if fit["condition_indicator"] > 1e6:
        warnings.append("The model matrix is poorly conditioned; coefficient estimates are sensitive to small measurement changes.")
    if outside:
        warnings.append("The candidate extrapolates beyond observed pseudocomponent ranges for: " + ", ".join(outside) + ".")
    if objective == "target" and abs(candidate["predicted_response"] - target) > 1e-6 * max(float(np.max(np.abs(observed))), abs(target)):
        warnings.append("The search did not reach the requested target; this is the closest blend found, not proof that the target is unattainable.")
    if fit["anova"]["pure_error"]["df"] == 0:
        warnings.append("No exactly replicated blends: pure error and lack-of-fit significance cannot be estimated. Add independent replicate mixtures.")
    return _finite({"analysis": "doe_mixture_response", "components": components, "region": region, "response": response, "model": model,
                    "objective": objective, "confidence": 0.95, "coefficients": coefficients,
                    "fit": {"n": n, "terms": p, "df_residual": fit["degrees"], "r_squared": fit["r_squared"],
                            "adjusted_r_squared": fit["adjusted_r_squared"], "predicted_r_squared": fit["diagnostics"]["predicted_r_squared"],
                            "press": fit["diagnostics"]["press"], "rmse": fit["rmse"], "sse": fit["sse"], "sst": fit["sst"],
                            "condition_indicator": fit["condition_indicator"]},
                    "recommendation": {"settings": recommendation, "pseudo": dict(zip(names, best_pseudo.tolist())),
                                       "predicted_response": candidate["predicted_response"], "objective": objective, "target": target,
                                       "mean_confidence_interval": predicted[0]["confidence_interval"],
                                       "prediction_interval": predicted[0]["prediction_interval"], "validated": False,
                                       "extrapolated_components": outside,
                                       "search": {"method": "Multi-start pairwise proportion exchange with line searches", "starts": candidate["starts"],
                                                  "domain": "Declared component bounds with Σx = 1", "global_optimum_guaranteed": False}},
                    "predictions": [{"settings": s, **r} for s, r in zip(settings, predicted[1:])],
                    "diagnostics": {"observed": observed.tolist(), "fitted": fit["fitted"], "residuals": fit["residuals"], **fit["diagnostics"]},
                    "anova": fit["anova"], "model_comparison": comparisons, "research_workflow": _research_steps(fit, outside),
                    "confirmation_csv": _confirmation_csv(recommendation, components, response),
                    "coding": "Coefficients use L-pseudocomponents z = (x - L) / (1 - ΣL); response units remain those of the input CSV.",
                    "ignored_columns": ignored, "rows_dropped": 0,
                    "kernel": {"engine": "rust", "version": kernel.__version__, "solver": "Column-pivoted Householder QR with column scaling"},
                    "warnings": warnings})


GOAL_KEYS = {"name", "model", "goal", "lower", "target", "upper", "lower_shape", "upper_shape", "importance"}


def _goals(responses, mixture):
    if not isinstance(responses, list) or not 1 <= len(responses) <= 10:
        raise ValueError("만족도 최적화에는 반응을 1~10개 지정하세요.")
    out, names = [], set()
    allowed = MIXTURE_MODELS if mixture else ("linear", "interaction", "quadratic")
    for item in responses:
        if not isinstance(item, dict) or not {"name", "model", "goal"} <= set(item) or not set(item) <= GOAL_KEYS:
            raise ValueError("각 반응은 name, model, goal과 goal에 맞는 lower/target/upper로 지정하세요.")
        name, model, goal = item["name"], item["model"], item["goal"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("반응 이름은 비어 있지 않고 서로 달라야 합니다.")
        if model not in allowed:
            raise ValueError(f"{name}: 모델은 {', '.join(allowed)} 중에서 선택하세요.")
        if goal not in {"maximize", "minimize", "target"}:
            raise ValueError(f"{name}: goal은 maximize, minimize 또는 target입니다.")
        needed = {"maximize": ("lower", "target"), "minimize": ("target", "upper"), "target": ("lower", "target", "upper")}[goal]
        values = {key: _number(item.get(key), f"{name} {key}") for key in needed}
        shapes = {key: _number(item.get(key, 1.0), f"{name} {key}") for key in ("lower_shape", "upper_shape", "importance")}
        if any(not 0.1 <= v <= 10 for v in shapes.values()):
            raise ValueError(f"{name}: 모양 지수와 중요도는 0.1~10 사이입니다.")
        out.append({"name": name, "model": model, "goal": goal, "lower": values.get("lower"), "target": values["target"],
                    "upper": values.get("upper"), **shapes})
        names.add(name)
    return out


def _goal_spec(goal):
    return (goal["goal"], goal["lower"] if goal["lower"] is not None else 0.0, goal["target"],
            goal["upper"] if goal["upper"] is not None else 0.0, goal["lower_shape"], goal["upper_shape"], goal["importance"])


def optimize_desirability(text, responses, factors=None, components=None):
    """Fit each response on the same runs and maximize the Derringer-Suich composite desirability D."""
    if (factors is None) == (components is None):
        raise ValueError("공정 인자(factors) 또는 혼합물 성분(components) 중 하나만 지정하세요.")
    mixture = components is not None
    goals = _goals(responses, mixture)
    kernel = _mixture_kernel()
    if mixture:
        variables, region = _components(components)
        bounds = {"lower": [c["low"] for c in variables], "upper": [c["high"] for c in variables]}
    else:
        variables, region, bounds = _factors(factors, 6), None, {}
    names = [v["name"] for v in variables]
    if any(goal["name"] in names for goal in goals):
        raise ValueError("반응 이름은 인자·성분 이름과 달라야 합니다.")
    fits, points, observed_all = [], None, []
    for goal in goals:
        if mixture:
            points, observed, _ = _mixture_data(text, variables, region, goal["name"])
        else:
            if goal["model"] == "quadratic" and len(names) > 4:
                raise ValueError("2차 모델은 인자 4개까지입니다.")
            points, observed, _ = _data(text, variables, goal["name"])
        try:
            fits.append(kernel.doe_fit(points, observed, goal["model"]))
        except ValueError as exc:
            raise ValueError(f"{goal['name']} 모델을 추정할 수 없습니다: {exc}") from exc
        observed_all.append(observed)
    specs = [_goal_spec(goal) for goal in goals]
    models = [(list(fit["coefficients"]), goal["model"]) for fit, goal in zip(fits, goals)]
    try:
        best = kernel.desirability_optimize("mixture" if mixture else "cube", models, specs, len(names), bounds.get("lower", []), bounds.get("upper", []))
    except ValueError as exc:
        raise ValueError(f"만족도 최적화를 할 수 없습니다: {exc}") from exc
    point = np.asarray(best["point"], dtype=float)
    model_point = np.clip(_pseudo(point, region), 0.0, 1.0) if mixture else np.clip(point, -1.0, 1.0)
    settings = dict(zip(names, point.tolist())) if mixture else _real(point, variables)
    rows = []
    for goal, fit, observed, value, d in zip(goals, fits, observed_all, best["values"], best["individual"]):
        interval = kernel.doe_predict(points, observed, goal["model"], np.asarray([model_point], dtype=float))[0]
        rows.append({**goal, "predicted": value, "desirability": d, "mean_confidence_interval": interval["confidence_interval"],
                     "prediction_interval": interval["prediction_interval"],
                     "fit": {"r_squared": fit["r_squared"], "adjusted_r_squared": fit["adjusted_r_squared"],
                             "predicted_r_squared": fit["diagnostics"]["predicted_r_squared"], "rmse": fit["rmse"], "df_residual": fit["degrees"]}})
    observed_matrix = np.column_stack(observed_all)
    run_scores = [kernel.desirability(specs, row.tolist())[1] for row in observed_matrix]
    best_run = int(np.argmax(run_scores))
    warnings = ["The optimum is a model candidate, not a validated experiment or a guaranteed global optimum. Confirm with new independent runs.",
                "Desirability bounds, shapes and importances are the user's choices; D ranks settings under those choices and has no physical meaning.",
                "Each response is fitted on its own; correlation between responses and the uncertainty of D are not propagated."]
    if best["composite"] == 0:
        warnings.insert(0, "No setting gives every response a nonzero desirability; widen the acceptable ranges or reconsider the goals.")
    return _finite({"analysis": "doe_desirability", "domain": "mixture" if mixture else "coded factors",
                    "factors": None if mixture else variables, "components": variables if mixture else None, "region": region,
                    "optimum": {"settings": settings, "model_coordinates": dict(zip(names, model_point.tolist())),
                                "composite_desirability": best["composite"], "validated": False,
                                "search": {"method": "Multi-start line searches on the fitted models", "starts": best["starts"],
                                           "global_optimum_guaranteed": False}},
                    "responses": rows, "best_observed_run": {"row": best_run + 2, "composite_desirability": run_scores[best_run]},
                    "observed_composite_desirability": run_scores,
                    "method": "Derringer & Suich (1980) individual desirabilities, importance-weighted geometric mean D",
                    "kernel": {"engine": "rust", "version": kernel.__version__}, "warnings": warnings})
