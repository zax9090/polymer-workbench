"""Process data analysis for fab-style tables: data dictionary checks, Shewhart charts with run
rules, process capability, balanced nested variance components and stratified defect rates.

Each analysis reads one CSV whose columns are named explicitly. Rows are never dropped silently:
a missing or non-numeric value in a selected column is an error that names its CSV line.
"""
from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import inspect
import io
import math

import numpy as np
from scipy import stats

MAX_BYTES = 20_000_000
MAX_ROWS = 200_000
KINDS = ("dictionary", "control_chart", "capability", "variance_components", "stratified_rates",
         "classification_metrics", "regression_metrics", "split_validation")

# Individuals charts use moving ranges of two points: d2 = E[R]/sigma, d3 = sd(R)/sigma for n = 2.
D2_TWO = 2 / math.sqrt(math.pi)
D3_TWO = math.sqrt(2 - 4 / math.pi)
D4_TWO = 1 + 3 * D3_TWO / D2_TWO


def c4(n):
    """E[s]/sigma for n normal observations."""
    return math.sqrt(2 / (n - 1)) * math.exp(math.lgamma(n / 2) - math.lgamma((n - 1) / 2))


def _table(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("CSV 내용이 비어 있습니다.")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("공정 데이터 CSV는 UTF-8 기준 20 MB 이하여야 합니다.")
    try:
        reader = csv.reader(io.StringIO(text.lstrip("﻿")), strict=True)
        header = [name.strip() for name in next(reader)]
        rows = []
        for line, row in enumerate(reader, 2):
            if not row:
                continue
            if len(row) != len(header):
                raise ValueError(f"CSV {line}행의 열 수가 머리글과 다릅니다.")
            rows.append((line, [value.strip() for value in row]))
            if len(rows) > MAX_ROWS:
                raise ValueError(f"CSV는 {MAX_ROWS:,}행 이하여야 합니다.")
    except (csv.Error, StopIteration) as exc:
        raise ValueError("CSV 형식을 읽을 수 없습니다.") from exc
    if any(not name for name in header) or len(set(header)) != len(header):
        raise ValueError("CSV 머리글은 비어 있지 않고 중복이 없어야 합니다.")
    if not rows:
        raise ValueError("CSV에 데이터 행이 없습니다.")
    return header, rows


def _column(header, name, role):
    if not isinstance(name, str) or name not in header:
        raise ValueError(f"{role} 열 {name!r}이(가) CSV 머리글에 없습니다.")
    return header.index(name)


def _numbers(header, rows, name, role="측정값"):
    index = _column(header, name, role)
    values = []
    for line, row in rows:
        try:
            value = float(row[index])
        except ValueError as exc:
            raise ValueError(f"CSV {line}행 {name}: 비어 있거나 숫자가 아닙니다. 행을 자동으로 제외하지 않습니다.") from exc
        if not math.isfinite(value):
            raise ValueError(f"CSV {line}행 {name}: 유한한 숫자가 아닙니다.")
        values.append(value)
    return np.asarray(values)


def _order_key(text):
    try:
        return float(text)
    except ValueError:
        return datetime.fromisoformat(text).timestamp()


def _ordered(header, rows, order):
    """File order unless an explicit numeric or ISO-8601 order column is given (ties keep file order)."""
    if order is None:
        return rows
    index = _column(header, order, "순서")
    try:
        keys = [_order_key(row[index]) for _, row in rows]
    except ValueError as exc:
        raise ValueError(f"순서 열 {order!r}은 숫자 또는 ISO-8601 시각이어야 합니다.") from exc
    return [rows[i] for i in sorted(range(len(rows)), key=lambda i: keys[i])]


def _source(text, kind, options):
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "kind": kind, "options": options}


def dictionary(text, keys, value=None, unit=None, order=None):
    """What one row means: key completeness, duplicates, hierarchy, balance and value coverage."""
    header, rows = _table(text)
    if not isinstance(keys, list) or not 1 <= len(keys) <= 6 or len(set(keys)) != len(keys):
        raise ValueError("연결 키는 상위에서 하위 순서로 1~6개, 중복 없이 지정하세요.")
    columns = [_column(header, key, "연결 키") for key in keys]
    issues, levels = [], []
    missing = {key: [line for line, row in rows if not row[c]] for key, c in zip(keys, columns)}
    for key, lines in missing.items():
        if lines:
            issues.append({"code": "missing_key", "column": key, "count": len(lines), "lines": lines[:10]})
    complete = [(line, tuple(row[c] for c in columns)) for line, row in rows if all(row[c] for c in columns)]
    seen, duplicates = {}, []
    for line, path in complete:
        if path in seen:
            duplicates.append({"lines": [seen[path], line], "key": dict(zip(keys, path))})
        else:
            seen[path] = line
    if duplicates:
        issues.append({"code": "duplicate_key", "count": len(duplicates), "examples": duplicates[:10]})
    for depth, key in enumerate(keys, 1):
        paths = {path[:depth] for _, path in complete}
        per_parent = {}
        for path in paths:
            per_parent[path[:-1]] = per_parent.get(path[:-1], 0) + 1
        children = list(per_parent.values()) or [0]
        labels = {path[-1] for path in paths}
        level = {"column": key, "distinct_values": len(labels), "groups": len(paths),
                 "children_per_parent": {"min": min(children), "max": max(children)}}
        if depth > 1:
            # Wafer slot numbers are often reused in every lot; such IDs only join together with their parents.
            level["unique_without_parent"] = len(labels) == len(paths)
        levels.append(level)
    counts = {}
    for _, path in complete:
        counts[path] = counts.get(path, 0) + 1
    rows_per_key = sorted(set(counts.values()))
    result = {"analysis": "process_dictionary", "rows": len(rows), "columns": header, "keys": keys, "levels": levels,
              "rows_per_full_key": {"min": rows_per_key[0], "max": rows_per_key[-1]} if rows_per_key else None,
              "observation_unit": f"one row per {' / '.join(keys)}" if not duplicates and not any(missing.values()) else None,
              "issues": issues, "warnings": []}
    result["balanced"] = all(level["children_per_parent"]["min"] == level["children_per_parent"]["max"] for level in levels)
    if value is not None:
        index = _column(header, value, "측정값")
        numbers, bad = [], []
        for line, row in rows:
            try:
                number = float(row[index])
                if not math.isfinite(number):
                    raise ValueError
                numbers.append(number)
            except ValueError:
                bad.append(line)
        result["value"] = {"column": value, "unit": unit, "numeric": len(numbers), "missing_or_invalid": len(bad),
                           "lines": bad[:10], "min": min(numbers) if numbers else None, "max": max(numbers) if numbers else None,
                           "mean": float(np.mean(numbers)) if numbers else None}
        if bad:
            issues.append({"code": "invalid_value", "column": value, "count": len(bad), "lines": bad[:10]})
        if not unit:
            result["warnings"].append("No unit was declared for the value column; record it before comparing or joining tables.")
    if order is not None:
        index = _column(header, order, "순서")
        try:
            keys_in_order = [_order_key(row[index]) for _, row in rows]
            result["order"] = {"column": order, "file_order_is_sorted": all(a <= b for a, b in zip(keys_in_order, keys_in_order[1:]))}
        except ValueError:
            issues.append({"code": "invalid_order", "column": order})
    if not result["balanced"]:
        result["warnings"].append("Groups have unequal numbers of children; balanced nested variance components do not apply as is.")
    return result


def _rules(z):
    """Western Electric rules 1-4 and Nelson's six-point trend on standardized points."""
    signals = {}
    def flag(i, rule):
        signals.setdefault(i, []).append(rule)
    for i in range(len(z)):
        if abs(z[i]) > 3:
            flag(i, "beyond_3_sigma")
        for side in (1, -1):
            if i >= 2 and sum(side * v > 2 for v in z[i - 2:i + 1]) >= 2 and side * z[i] > 2:
                flag(i, "2_of_3_beyond_2_sigma")
            if i >= 4 and sum(side * v > 1 for v in z[i - 4:i + 1]) >= 4 and side * z[i] > 1:
                flag(i, "4_of_5_beyond_1_sigma")
            if i >= 7 and all(side * v > 0 for v in z[i - 7:i + 1]):
                flag(i, "8_on_one_side")
        if i >= 5:
            steps = np.diff(z[i - 5:i + 1])
            if np.all(steps > 0) or np.all(steps < 0):
                flag(i, "6_point_trend")
    return signals


RULES = {"beyond_3_sigma": "One point beyond 3 sigma", "2_of_3_beyond_2_sigma": "2 of 3 consecutive points beyond 2 sigma on one side",
         "4_of_5_beyond_1_sigma": "4 of 5 consecutive points beyond 1 sigma on one side",
         "8_on_one_side": "8 consecutive points on one side of the centre line", "6_point_trend": "6 points steadily increasing or decreasing"}


def _subgroups(header, rows, value, subgroup):
    y = _numbers(header, rows, value)
    index = _column(header, subgroup, "부분군")
    members = {}
    for position, (line, row) in enumerate(rows):
        if not row[index]:
            raise ValueError(f"CSV {line}행 {subgroup}: 부분군 이름이 비어 있습니다.")
        members.setdefault(row[index], []).append(position)
    sizes = {len(m) for m in members.values()}
    if len(sizes) != 1 or min(sizes) < 2:
        raise ValueError("X̄-S 관리도는 모든 부분군 크기가 2 이상으로 같아야 합니다. 크기가 다르면 부분군을 다시 정의하세요.")
    labels = list(members)
    groups = [y[members[label]] for label in labels]
    return labels, groups, sizes.pop()


def _baseline(count, baseline):
    if baseline is None:
        return count
    if isinstance(baseline, bool) or not isinstance(baseline, int) or not 2 <= baseline <= count:
        raise ValueError(f"기준 구간(baseline)은 2~{count} 사이의 정수여야 합니다.")
    return baseline


def control_chart(text, value, subgroup=None, order=None, baseline=None, unit=None):
    """Individuals-moving range chart, or Xbar-S chart when a subgroup column is given."""
    header, rows = _table(text)
    rows = _ordered(header, rows, order)
    warnings = []
    if order is None:
        warnings.append("No order column was given; file order is treated as time order.")
    if subgroup is None:
        x = _numbers(header, rows, value)
        if len(x) < 3:
            raise ValueError("개별값 관리도에는 3개 이상의 측정값이 필요합니다.")
        base = _baseline(len(x), baseline)
        moving = np.abs(np.diff(x))
        mr_bar = float(np.mean(moving[:base - 1]))
        centre = float(np.mean(x[:base]))
        sigma = mr_bar / D2_TWO
        if sigma == 0:
            raise ValueError("이동 범위가 모두 0이라 관리 한계를 계산할 수 없습니다. 측정 분해능을 확인하세요.")
        points, labels = x, [line for line, _ in rows]
        chart = {"type": "individuals_moving_range", "sigma_estimate": {"value": sigma, "method": "average moving range / d2(2)"},
                 "centre": centre, "upper": centre + 3 * sigma, "lower": centre - 3 * sigma,
                 "dispersion": {"name": "moving_range", "positions": list(range(2, len(x) + 1)), "values": moving.tolist(),
                                "centre": mr_bar, "upper": D4_TWO * mr_bar, "lower": 0.0}}
        z = (x - centre) / sigma
        dispersion_signals = [i + 2 for i, v in enumerate(moving) if v > D4_TWO * mr_bar]
    else:
        labels, groups, n = _subgroups(header, rows, value, subgroup)
        if len(groups) < 3:
            raise ValueError("X̄-S 관리도에는 3개 이상의 부분군이 필요합니다.")
        base = _baseline(len(groups), baseline)
        means = np.array([g.mean() for g in groups])
        sds = np.array([g.std(ddof=1) for g in groups])
        s_bar = float(np.mean(sds[:base]))
        centre = float(np.mean(means[:base]))
        sigma = s_bar / c4(n)
        if sigma == 0:
            raise ValueError("부분군 표준편차가 모두 0이라 관리 한계를 계산할 수 없습니다.")
        spread = 3 * math.sqrt(1 - c4(n) ** 2) / c4(n)
        upper_s, lower_s = (1 + spread) * s_bar, max(0.0, 1 - spread) * s_bar
        points = means
        chart = {"type": "xbar_s", "subgroup_size": n, "sigma_estimate": {"value": sigma, "method": "mean subgroup s / c4(n)"},
                 "centre": centre, "upper": centre + 3 * sigma / math.sqrt(n), "lower": centre - 3 * sigma / math.sqrt(n),
                 "dispersion": {"name": "subgroup_sd", "positions": list(range(1, len(sds) + 1)), "values": sds.tolist(),
                                "centre": s_bar, "upper": upper_s, "lower": lower_s}}
        z = (means - centre) / (sigma / math.sqrt(n))
        dispersion_signals = [i + 1 for i, v in enumerate(sds) if v > upper_s or v < lower_s]
        # Within-subgroup sigma ignores run-to-run variance; nested data then puts most means outside.
        ratio = float(np.std(means[:base], ddof=1) / (sigma / math.sqrt(n)))
        chart["between_to_within_ratio"] = ratio
        if ratio > 2:
            warnings.append(f"Subgroup means vary {ratio:.1f} times more than within-subgroup variation predicts. "
                            "Limits from within-subgroup sigma then flag most subgroups; estimate variance components before treating every point as a special cause.")
    signals = _rules(z)
    chart["points"] = [{"position": i + 1, "label": labels[i], "value": float(points[i]), "z": float(z[i]),
                        "signals": signals.get(i, [])} for i in range(len(points))]
    chart["dispersion"]["signals"] = dispersion_signals
    if base < len(points):
        chart["baseline"] = base
    else:
        warnings.append("Limits are estimated from the same points they judge (retrospective Phase I); set baseline to monitor new points against fixed limits.")
    if signals:
        warnings.append("Run-rule signals mark points to investigate with production and maintenance records; they do not identify a cause.")
    return {"analysis": "control_chart", "value": value, "unit": unit, "rows": len(rows), **chart,
            "rules": RULES, "method": "Shewhart limits at 3 sigma; Western Electric rules 1-4 and a 6-point trend rule",
            "warnings": warnings}


def capability(text, value, lsl=None, usl=None, subgroup=None, order=None, unit=None):
    """Cp/Cpk from within-subgroup (or moving-range) sigma, Pp/Ppk from the overall sample sd."""
    if lsl is None and usl is None:
        raise ValueError("규격 하한(lsl)이나 상한(usl) 중 하나 이상을 지정하세요.")
    for name, limit in (("lsl", lsl), ("usl", usl)):
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit)):
            raise ValueError(f"{name}은(는) 유한한 숫자여야 합니다.")
    if lsl is not None and usl is not None and not lsl < usl:
        raise ValueError("규격 하한은 상한보다 작아야 합니다.")
    header, rows = _table(text)
    rows = _ordered(header, rows, order)
    x = _numbers(header, rows, value)
    n = len(x)
    if n < 10:
        raise ValueError("공정 능력 지수에는 10개 이상의 측정값이 필요합니다.")
    mean, overall = float(np.mean(x)), float(np.std(x, ddof=1))
    if subgroup is None:
        within = float(np.mean(np.abs(np.diff(x)))) / D2_TWO
        within_method = "average moving range / d2(2) in row order"
    else:
        _, groups, size = _subgroups(header, rows, value, subgroup)
        within = float(np.mean([g.std(ddof=1) for g in groups])) / c4(size)
        within_method = f"mean subgroup s / c4({size})"
    if within == 0 or overall == 0:
        raise ValueError("측정값의 변동이 0이라 공정 능력을 계산할 수 없습니다.")
    def indices(sigma):
        upper = (usl - mean) / (3 * sigma) if usl is not None else None
        lower = (mean - lsl) / (3 * sigma) if lsl is not None else None
        both = (usl - lsl) / (6 * sigma) if lsl is not None and usl is not None else None
        return both, upper, lower, min(v for v in (upper, lower) if v is not None)
    cp, cpu, cpl, cpk = indices(within)
    pp, ppu, ppl, ppk = indices(overall)
    def expected_ppm(sigma):
        tail = (stats.norm.sf((usl - mean) / sigma) if usl is not None else 0.0) + (stats.norm.cdf((lsl - mean) / sigma) if lsl is not None else 0.0)
        return float(tail * 1e6)
    outside = int(np.sum(x > usl) if usl is not None else 0) + int(np.sum(x < lsl) if lsl is not None else 0)
    # Bissell (1990) normal-theory interval; valid for the overall sd with n - 1 degrees of freedom.
    margin = 1.959963984540054 * math.sqrt(1 / (9 * n) + ppk ** 2 / (2 * (n - 1)))
    warnings = ["Capability indices assume a stable, normally distributed process; check the control chart first.",
                "Cp/Cpk use short-term within variation and Pp/Ppk the overall variation; a large gap means between-group or drift variation."]
    normality = None
    if n <= 5000:
        normality = {"test": "Shapiro-Wilk", "p_value": float(stats.shapiro(x).pvalue)}
        if normality["p_value"] < 0.05:
            warnings.append("Shapiro-Wilk rejects normality at 0.05; normal-theory indices and expected ppm may be misleading.")
    if order is None and subgroup is None:
        warnings.append("Within sigma uses moving ranges in file order; give an order column if rows are not in time order.")
    return {"analysis": "process_capability", "value": value, "unit": unit, "n": n, "mean": mean,
            "limits": {"lsl": lsl, "usl": usl},
            "within": {"sigma": within, "method": within_method, "cp": cp, "cpu": cpu, "cpl": cpl, "cpk": cpk, "expected_ppm": expected_ppm(within)},
            "overall": {"sigma": overall, "method": "sample standard deviation", "pp": pp, "ppu": ppu, "ppl": ppl, "ppk": ppk,
                        "ppk_95_interval": [ppk - margin, ppk + margin], "expected_ppm": expected_ppm(overall)},
            "observed": {"outside_spec": outside, "ppm": outside / n * 1e6}, "normality": normality,
            "method": "Process capability with normal-theory expected ppm; Ppk interval by Bissell's approximation",
            "warnings": warnings}


def variance_components(text, value, factors, unit=None):
    """Balanced nested random-effects ANOVA by expected mean squares; the residual is replication
    within the lowest factor (for example wafers within zone within run)."""
    if not isinstance(factors, list) or not 1 <= len(factors) <= 4 or len(set(factors)) != len(factors):
        raise ValueError("계층 인자는 상위에서 하위 순서로 1~4개, 중복 없이 지정하세요.")
    header, rows = _table(text)
    y = _numbers(header, rows, value)
    columns = [_column(header, factor, "계층 인자") for factor in factors]
    paths = []
    for line, row in rows:
        if not all(row[c] for c in columns):
            raise ValueError(f"CSV {line}행: 계층 인자 값이 비어 있습니다.")
        paths.append(tuple(row[c] for c in columns))
    n_total = len(y)
    levels = []
    for depth in range(len(factors) + 1):
        groups = {}
        for position, path in enumerate(paths):
            groups.setdefault(path[:depth], []).append(position)
        sizes = {len(m) for m in groups.values()}
        if len(sizes) != 1:
            raise ValueError(f"{(factors[depth - 1] if depth else '전체')} 단계의 관측 수가 그룹마다 다릅니다. 균형 설계만 지원합니다.")
        levels.append({"groups": groups, "size": sizes.pop()})
    if levels[-1]["size"] < 2:
        raise ValueError("최하위 인자 안에 반복 측정이 2개 이상 있어야 잔차를 추정할 수 있습니다.")
    means = [{key: float(y[m].mean()) for key, m in level["groups"].items()} for level in levels]
    rows_out = []
    for depth in range(1, len(levels)):
        ss = sum(levels[depth]["size"] * (means[depth][key] - means[depth - 1][key[:-1]]) ** 2 for key in means[depth])
        df = len(levels[depth]["groups"]) - len(levels[depth - 1]["groups"])
        if df <= 0:
            raise ValueError(f"{factors[depth - 1]} 단계에 상위 그룹마다 2개 이상의 하위 그룹이 있어야 합니다.")
        rows_out.append({"source": factors[depth - 1] if depth == 1 else f"{factors[depth - 1]} within {factors[depth - 2]}",
                         "factor": factors[depth - 1], "df": df, "ss": ss, "observations_per_group": levels[depth]["size"]})
    lowest = means[-1]
    residual_ss = float(sum((y[p] - lowest[path]) ** 2 for p, path in enumerate(paths)))
    rows_out.append({"source": "residual", "factor": "residual", "df": n_total - len(levels[-1]["groups"]), "ss": residual_ss,
                     "observations_per_group": 1})
    for row in rows_out:
        row["ms"] = row["ss"] / row["df"]
    warnings = []
    names = [f"var_{row['factor']}" for row in rows_out]
    for i, row in enumerate(rows_out):
        terms = ["var_residual"] + [f"{rows_out[j]['observations_per_group']}*{names[j]}" for j in reversed(range(i, len(rows_out) - 1))]
        row["expected_mean_square"] = " + ".join(terms)
        if i < len(rows_out) - 1:
            below = rows_out[i + 1]
            row["f"] = row["ms"] / below["ms"] if below["ms"] > 0 else None
            row["f_denominator"] = below["source"]
            row["p_value"] = float(stats.f.sf(row["f"], row["df"], below["df"])) if row["f"] is not None else None
            estimate = (row["ms"] - below["ms"]) / row["observations_per_group"]
        else:
            estimate = row["ms"]
        row["variance_component"] = max(estimate, 0.0)
        row["truncated_negative_estimate"] = estimate < 0
        if estimate < 0:
            warnings.append(f"The {row['source']} variance estimate was negative ({estimate:.6g}) and is reported as 0.")
    total = sum(row["variance_component"] for row in rows_out)
    for row in rows_out:
        row["percent_of_total"] = 100 * row["variance_component"] / total if total > 0 else None
        row["standard_deviation"] = math.sqrt(row["variance_component"])
    top = rows_out[0]
    if top["df"] < 10:
        warnings.append(f"The {top['source']} component rests on {top['df']} degrees of freedom; treat it as imprecise.")
    warnings.append("Random-effects interpretation: levels are treated as samples from a population of runs, zones or wafers.")
    return {"analysis": "variance_components", "value": value, "unit": unit, "variance_unit": f"({unit})^2" if unit else None,
            "n": n_total, "grand_mean": float(y.mean()), "factors": factors, "anova": rows_out, "total_variance": total,
            "method": "Balanced nested random-effects ANOVA, components from expected mean squares (method of moments)",
            "warnings": warnings}


def _counts(header, rows, group, stratum, defects, inspected, outcome):
    g_index, s_index = _column(header, group, "비교 그룹"), _column(header, stratum, "층")
    if outcome is not None:
        o = _numbers(header, rows, outcome, "결과")
        if not np.all(np.isin(o, (0, 1))):
            raise ValueError(f"결과 열 {outcome}은 0(정상) 또는 1(불량)이어야 합니다.")
        d, t = o, np.ones(len(o))
    else:
        d, t = _numbers(header, rows, defects, "불량 수"), _numbers(header, rows, inspected, "검사 수")
        if np.any(d < 0) or np.any(d != np.round(d)) or np.any(t != np.round(t)) or np.any(d > t):
            raise ValueError("불량 수와 검사 수는 0 이상의 정수이고 불량 수가 검사 수를 넘지 않아야 합니다.")
    cells = {}
    for (line, row), defect, total in zip(rows, d, t):
        if not row[g_index] or not row[s_index]:
            raise ValueError(f"CSV {line}행: 비교 그룹이나 층 이름이 비어 있습니다.")
        cell = cells.setdefault((row[g_index], row[s_index]), [0, 0])
        cell[0] += int(defect)
        cell[1] += int(total)
    return cells


def _mantel_haenszel(tables):
    """Pooled estimates over 2x2 tables [a, n1, b, n0] (compared defects/inspected, reference defects/inspected)."""
    informative = [(a, n1, b, n0) for a, n1, b, n0 in tables if n1 > 0 and n0 > 0]
    r = s = rr_num = rr_den = 0.0
    pr = ps_qr = qs = 0.0
    observed = expected = variance = 0.0
    for a, n1, b, n0 in informative:
        total = n1 + n0
        c, d = n1 - a, n0 - b
        p, q = (a + d) / total, (c + b) / total
        ri, si = a * d / total, c * b / total
        r, s = r + ri, s + si
        pr, ps_qr, qs = pr + p * ri, ps_qr + p * si + q * ri, qs + q * si
        rr_num, rr_den = rr_num + a * n0 / total, rr_den + b * n1 / total
        events = a + b
        observed += a
        expected += n1 * events / total
        if total > 1:
            variance += n1 * n0 * events * (total - events) / (total ** 2 * (total - 1))
    result = {"informative_strata": len(informative),
              "risk_ratio": rr_num / rr_den if rr_den > 0 else None,
              "odds_ratio": r / s if s > 0 else None, "odds_ratio_95_interval": None,
              "cmh_chi_square": None, "cmh_p_value": None, "breslow_day": None}
    if r > 0 and s > 0:
        se = math.sqrt(pr / (2 * r * r) + ps_qr / (2 * r * s) + qs / (2 * s * s))
        log_or = math.log(r / s)
        result["odds_ratio_95_interval"] = [math.exp(log_or - 1.959963984540054 * se), math.exp(log_or + 1.959963984540054 * se)]
        result["breslow_day"] = _breslow_day(informative, r / s)
    if variance > 0:
        chi = (observed - expected) ** 2 / variance
        result["cmh_chi_square"], result["cmh_p_value"] = chi, float(stats.chi2.sf(chi, 1))
    return result


def _breslow_day(tables, odds):
    """Breslow-Day homogeneity test of the odds ratio across strata (no Tarone correction)."""
    statistic, used = 0.0, 0
    for a, n1, b, n0 in tables:
        events = a + b
        lo, hi = max(0, events - n0), min(n1, events)
        if lo == hi:
            continue
        if odds == 1:
            fitted = n1 * events / (n1 + n0)
        else:
            qa, qb, qc = 1 - odds, n0 - events + odds * (n1 + events), -odds * n1 * events
            roots = np.roots([qa, qb, qc]).real
            fitted = float(next(v for v in roots if lo - 1e-9 <= v <= hi + 1e-9))
        cells = [fitted, n1 - fitted, events - fitted, n0 - events + fitted]
        if min(cells) <= 0:
            continue
        statistic += (a - fitted) ** 2 / (1 / sum(1 / v for v in cells))
        used += 1
    if used < 2:
        return None
    return {"chi_square": float(statistic), "df": used - 1, "p_value": float(stats.chi2.sf(statistic, used - 1))}


def stratified_rates(text, group, stratum, defects=None, inspected=None, outcome=None, reference=None):
    """Defect rates by group within strata: crude vs stratum-specific rates, composition,
    direct standardization and Mantel-Haenszel pooled comparisons against a reference group."""
    if (outcome is None) == (defects is None or inspected is None):
        raise ValueError("불량 수(defects)와 검사 수(inspected)를 함께 지정하거나, 0/1 결과 열(outcome) 하나만 지정하세요.")
    header, rows = _table(text)
    cells = _counts(header, rows, group, stratum, defects, inspected, outcome)
    groups = sorted({g for g, _ in cells})
    strata = sorted({s for _, s in cells})
    if len(groups) < 2:
        raise ValueError("비교 그룹이 2개 이상 있어야 합니다.")
    reference = groups[0] if reference is None else reference
    if reference not in groups:
        raise ValueError(f"기준 그룹 {reference!r}이(가) 데이터에 없습니다.")
    def cell(g, s):
        return cells.get((g, s), [0, 0])
    pooled = {s: sum(cell(g, s)[1] for g in groups) for s in strata}
    total_inspected = sum(pooled.values())
    summary = []
    for g in groups:
        d = sum(cell(g, s)[0] for s in strata)
        n = sum(cell(g, s)[1] for s in strata)
        by_stratum = [{"stratum": s, "defects": cell(g, s)[0], "inspected": cell(g, s)[1],
                       "rate": cell(g, s)[0] / cell(g, s)[1] if cell(g, s)[1] else None,
                       "share_of_group": cell(g, s)[1] / n if n else None} for s in strata]
        complete = all(item["rate"] is not None for item in by_stratum)
        standardized = sum(pooled[s] / total_inspected * item["rate"] for s, item in zip(strata, by_stratum)) if complete else None
        summary.append({"group": g, "defects": d, "inspected": n, "crude_rate": d / n if n else None,
                        "standardized_rate": standardized, "strata": by_stratum})
    comparisons, warnings = [], []
    for g in groups:
        if g == reference:
            continue
        tables = [(cell(g, s)[0], cell(g, s)[1], cell(reference, s)[0], cell(reference, s)[1]) for s in strata]
        pooled_estimates = _mantel_haenszel(tables)
        crude = next(item for item in summary if item["group"] == g)["crude_rate"]
        crude_ref = next(item for item in summary if item["group"] == reference)["crude_rate"]
        within = [(a / n1) - (b / n0) for a, n1, b, n0 in tables if n1 and n0]
        flips = crude is not None and crude_ref is not None and within and (
            (crude > crude_ref and all(v < 0 for v in within)) or (crude < crude_ref and all(v > 0 for v in within)))
        comparisons.append({"group": g, "reference": reference, "crude_rate_difference": crude - crude_ref if crude is not None and crude_ref is not None else None,
                            "direction_reverses_within_strata": bool(flips), **pooled_estimates})
        if flips:
            warnings.append(f"{g} vs {reference}: the crude comparison and every stratum-specific comparison point in opposite directions "
                            "(Simpson's paradox); the crude rates mostly reflect different stratum mixes.")
        if pooled_estimates["breslow_day"] and pooled_estimates["breslow_day"]["p_value"] < 0.05:
            warnings.append(f"{g} vs {reference}: Breslow-Day rejects a common odds ratio across strata at 0.05; report stratum-specific rates instead of one pooled estimate.")
    if any(item["standardized_rate"] is None for item in summary):
        warnings.append("Some groups have no inspections in some strata; direct standardization is not reported for them.")
    warnings.append("Stratifying by the recorded strata removes only that confounder; lot, period, maintenance and rework history may still differ.")
    return {"analysis": "stratified_rates", "group": group, "stratum": stratum, "strata": strata, "groups": summary,
            "standard_population": {s: pooled[s] / total_inspected for s in strata}, "comparisons": comparisons,
            "method": "Direct standardization to the pooled stratum mix; Mantel-Haenszel risk and odds ratios "
                      "(Robins-Breslow-Greenland interval), CMH test without continuity correction, Breslow-Day homogeneity",
            "warnings": warnings}


OPTIONS = {"dictionary": {"keys", "value", "unit", "order"},
           "control_chart": {"value", "subgroup", "order", "baseline", "unit"},
           "capability": {"value", "lsl", "usl", "subgroup", "order", "unit"},
           "variance_components": {"value", "factors", "unit"},
           "stratified_rates": {"group", "stratum", "defects", "inspected", "outcome", "reference"}}
FUNCTIONS = {"dictionary": dictionary, "control_chart": control_chart, "capability": capability,
             "variance_components": variance_components, "stratified_rates": stratified_rates}


def analyze(kind, text, options=None):
    from . import evaluation
    functions, allowed = {**FUNCTIONS, **evaluation.FUNCTIONS}, {**OPTIONS, **evaluation.OPTIONS}
    options = dict(options or {})
    if kind not in functions:
        raise ValueError("분석 종류는 " + ", ".join(KINDS) + " 중 하나여야 합니다.")
    unknown = set(options) - allowed[kind]
    if unknown:
        raise ValueError(f"{kind} 분석에서 쓰지 않는 설정입니다: {', '.join(sorted(unknown))}")
    required = [name for name, parameter in inspect.signature(functions[kind]).parameters.items()
                if name != "text" and parameter.default is inspect.Parameter.empty]
    missing = [name for name in required if options.get(name) is None]
    if missing:
        raise ValueError(f"{kind} 분석에 필요한 설정이 없습니다: {', '.join(missing)}")
    result = functions[kind](text, **options)
    result["input"] = _source(text, kind, options)
    return result
