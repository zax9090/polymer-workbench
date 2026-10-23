"""Process analysis against closed forms, hand calculations and an independent least-squares ANOVA."""
import math

import numpy as np
import pytest
from scipy import integrate, stats

from workbench import process


def table(header, rows):
    return ",".join(header) + "\n" + "\n".join(",".join(str(v) for v in row) for row in rows) + "\n"


def nested(runs=6, zones=3, wafers=2, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for run in range(runs):
        r = rng.normal(0, 4)
        for zone in range(zones):
            z = rng.normal(0, 3)
            rows += [(f"R{run}", f"Z{zone}", wafer, 560 + r + z + rng.normal(0, 2)) for wafer in range(wafers)]
    return table(["run", "zone", "wafer", "thickness"], rows), rows


def test_chart_constants_match_definitions_and_tables():
    d2 = 2 * integrate.quad(lambda x: x * stats.norm.pdf(x, scale=math.sqrt(2)), 0, np.inf)[0]
    assert process.D2_TWO == pytest.approx(d2, rel=1e-12)
    assert process.D4_TWO == pytest.approx(3.267, abs=5e-4)
    for n, tabulated in [(2, .7979), (5, .9400), (10, .9727), (25, .9896)]:
        assert process.c4(n) == pytest.approx(tabulated, abs=5e-5)


def test_individuals_chart_limits_follow_the_moving_range():
    values = [10, 12, 11, 13, 12, 11, 10, 12]
    chart = process.control_chart(table(["y"], [[v] for v in values]), "y")
    mr = np.mean(np.abs(np.diff(values)))
    assert chart["centre"] == pytest.approx(np.mean(values))
    assert chart["upper"] == pytest.approx(np.mean(values) + 3 * mr / process.D2_TWO)
    assert chart["dispersion"]["upper"] == pytest.approx(process.D4_TWO * mr)
    assert chart["dispersion"]["positions"][0] == 2
    assert any("file order" in w for w in chart["warnings"])


def test_xbar_s_limits_and_between_subgroup_warning():
    text, rows = nested(runs=8, zones=1, wafers=5)
    chart = process.control_chart(text, "thickness", subgroup="run")
    groups = [np.array([r[3] for r in rows if r[0] == f"R{i}"]) for i in range(8)]
    sigma = np.mean([g.std(ddof=1) for g in groups]) / process.c4(5)
    assert chart["sigma_estimate"]["value"] == pytest.approx(sigma)
    assert chart["upper"] == pytest.approx(np.mean([g.mean() for g in groups]) + 3 * sigma / math.sqrt(5))
    assert chart["between_to_within_ratio"] > 2
    assert any("variance components" in w for w in chart["warnings"])


@pytest.mark.parametrize("values,position,rule", [
    ([0] * 9 + [5], 10, "beyond_3_sigma"),
    ([0] * 9 + [2.5, 0, 2.5], 12, "2_of_3_beyond_2_sigma"),
    ([0] * 9 + [1.5, 1.5, 0, 1.5, 1.5], 14, "4_of_5_beyond_1_sigma"),
    ([0, -0.5] * 5 + [0.5] * 8, 18, "8_on_one_side"),
    ([0] * 9 + [-1, -.6, -.2, .2, .6, 1.0], 15, "6_point_trend"),
])
def test_each_run_rule_flags_the_completing_point(values, position, rule):
    z = np.asarray(values, float)
    signals = process._rules(z)
    assert rule in signals[position - 1]
    assert all(rule not in rules for i, rules in signals.items() if i < position - 1)


def test_order_column_sorts_iso_times_and_baseline_fixes_limits():
    rows = [("2026-10-03T10:00:00", 13), ("2026-10-01T10:00:00", 10), ("2026-10-02T10:00:00", 11), ("2026-10-04T10:00:00", 30)]
    chart = process.control_chart(table(["time", "y"], rows), "y", order="time", baseline=3)
    assert [p["value"] for p in chart["points"]] == [10, 11, 13, 30]
    assert chart["centre"] == pytest.approx(np.mean([10, 11, 13]))
    assert chart["baseline"] == 3
    assert "beyond_3_sigma" in chart["points"][-1]["signals"]


def test_unequal_subgroups_and_constant_data_are_rejected():
    with pytest.raises(ValueError, match="부분군 크기"):
        process.control_chart(table(["g", "y"], [("a", 1), ("a", 2), ("b", 3)]), "y", subgroup="g")
    with pytest.raises(ValueError, match="이동 범위"):
        process.control_chart(table(["y"], [[1]] * 5), "y")


def test_capability_matches_hand_calculation():
    rng = np.random.default_rng(4)
    x = rng.normal(100, 2, 60)
    result = process.capability(table(["y"], [[v] for v in x]), "y", lsl=94, usl=108)
    within = np.mean(np.abs(np.diff(x))) / process.D2_TWO
    overall = x.std(ddof=1)
    assert result["within"]["cp"] == pytest.approx(14 / (6 * within))
    assert result["within"]["cpk"] == pytest.approx(min(108 - x.mean(), x.mean() - 94) / (3 * within))
    assert result["overall"]["ppk"] == pytest.approx(min(108 - x.mean(), x.mean() - 94) / (3 * overall))
    expected = (stats.norm.sf((108 - x.mean()) / overall) + stats.norm.cdf((94 - x.mean()) / overall)) * 1e6
    assert result["overall"]["expected_ppm"] == pytest.approx(expected)
    one_sided = process.capability(table(["y"], [[v] for v in x]), "y", usl=108)
    assert one_sided["within"]["cp"] is None and one_sided["within"]["cpk"] == one_sided["within"]["cpu"]


def test_variance_components_match_independent_least_squares():
    text, rows = nested(runs=7, zones=4, wafers=3)
    result = process.variance_components(text, "thickness", ["run", "zone"], unit="angstrom")
    y = np.array([r[3] for r in rows])
    runs = sorted({r[0] for r in rows})
    cells = sorted({(r[0], r[1]) for r in rows})
    def sse(design):
        fitted = design @ np.linalg.lstsq(design, y, rcond=None)[0]
        return float(np.sum((y - fitted) ** 2))
    ones = np.ones((len(y), 1))
    run_design = np.array([[r[0] == g for g in runs] for r in rows], float)
    cell_design = np.array([[(r[0], r[1]) == c for c in cells] for r in rows], float)
    expected = [sse(ones) - sse(run_design), sse(run_design) - sse(cell_design), sse(cell_design)]
    assert [row["ss"] for row in result["anova"]] == pytest.approx(expected, rel=1e-10)
    assert [row["df"] for row in result["anova"]] == [6, 21, 56]
    run, zone, residual = result["anova"]
    assert residual["variance_component"] == pytest.approx(residual["ms"])
    assert zone["variance_component"] == pytest.approx((zone["ms"] - residual["ms"]) / 3)
    assert run["variance_component"] == pytest.approx((run["ms"] - zone["ms"]) / 12)
    assert run["f"] == pytest.approx(run["ms"] / zone["ms"])
    assert run["expected_mean_square"] == "var_residual + 3*var_zone + 12*var_run"
    assert sum(row["percent_of_total"] for row in result["anova"]) == pytest.approx(100)
    assert result["variance_unit"] == "(angstrom)^2"


def test_variance_components_reject_unbalanced_data_and_truncate_negative_estimates():
    text, rows = nested(runs=3, zones=2, wafers=2)
    with pytest.raises(ValueError, match="균형 설계"):
        process.variance_components(text + "R0,Z0,9,560\n", "thickness", ["run", "zone"])
    flat = table(["run", "y"], [("a", 1), ("a", 5), ("b", 1), ("b", 5), ("c", 1), ("c", 5)])
    result = process.variance_components(flat, "y", ["run"])
    assert result["anova"][0]["variance_component"] == 0 and result["anova"][0]["truncated_negative_estimate"]
    assert any("negative" in w for w in result["warnings"])


DECK = table(["tool", "product", "defects", "inspected"],
             [("A", "general", 18, 900), ("A", "hard", 12, 100), ("B", "general", 1, 100), ("B", "hard", 90, 900)])


def test_stratified_rates_expose_simpsons_paradox():
    result = process.stratified_rates(DECK, "tool", "product", defects="defects", inspected="inspected")
    a, b = result["groups"]
    assert (a["crude_rate"], b["crude_rate"]) == pytest.approx((0.03, 0.091))
    assert (a["standardized_rate"], b["standardized_rate"]) == pytest.approx((0.07, 0.055))
    comparison = result["comparisons"][0]
    assert comparison["reference"] == "A" and comparison["direction_reverses_within_strata"]
    # Hand Mantel-Haenszel: strata (a, n1, b, n0) = (1, 100, 18, 900), (90, 900, 12, 100)
    rr = (1 * 900 / 1000 + 90 * 100 / 1000) / (18 * 100 / 1000 + 12 * 900 / 1000)
    odds = (1 * 882 / 1000 + 90 * 88 / 1000) / (99 * 18 / 1000 + 810 * 12 / 1000)
    assert comparison["risk_ratio"] == pytest.approx(rr)
    assert comparison["odds_ratio"] == pytest.approx(odds)
    assert comparison["odds_ratio_95_interval"][0] < odds < comparison["odds_ratio_95_interval"][1]
    assert any("Simpson" in w for w in result["warnings"])


def test_unit_outcomes_aggregate_to_the_same_counts():
    rows = []
    for tool, product, defects, inspected in [("A", "g", 2, 6), ("A", "h", 3, 4), ("B", "g", 1, 5), ("B", "h", 4, 5)]:
        rows += [(tool, product, 1)] * defects + [(tool, product, 0)] * (inspected - defects)
    units = process.stratified_rates(table(["tool", "product", "fail"], rows), "tool", "product", outcome="fail")
    counts = process.stratified_rates(table(["tool", "product", "d", "n"], [("A", "g", 2, 6), ("A", "h", 3, 4), ("B", "g", 1, 5), ("B", "h", 4, 5)]),
                                      "tool", "product", defects="d", inspected="n")
    assert units["comparisons"][0]["odds_ratio"] == pytest.approx(counts["comparisons"][0]["odds_ratio"])
    with pytest.raises(ValueError, match="불량 수"):
        process.stratified_rates(table(["t", "s", "d", "n"], [("A", "x", 5, 3), ("B", "x", 1, 3)]), "t", "s", defects="d", inspected="n")


def test_dictionary_reports_duplicates_reused_ids_and_missing_values():
    rows = [("L1", "W1", 562), ("L1", "W2", 558), ("L2", "W1", ""), ("L2", "W1", 560), ("", "W3", 559)]
    result = process.dictionary(table(["lot_id", "wafer_id", "thickness"], rows), ["lot_id", "wafer_id"], value="thickness")
    codes = {issue["code"] for issue in result["issues"]}
    assert codes == {"missing_key", "duplicate_key", "invalid_value"}
    assert result["levels"][1]["unique_without_parent"] is False
    assert result["observation_unit"] is None
    assert any("unit" in w for w in result["warnings"])


def test_analyze_rejects_unknown_and_missing_options_and_records_input_hash():
    with pytest.raises(ValueError, match="쓰지 않는 설정"):
        process.analyze("capability", DECK, {"value": "defects", "lsl": 0, "colour": "red"})
    with pytest.raises(ValueError, match="필요한 설정"):
        process.analyze("variance_components", DECK, {"value": "defects"})
    with pytest.raises(ValueError, match="숫자가 아닙니다"):
        process.analyze("control_chart", DECK, {"value": "tool"})
    result = process.analyze("stratified_rates", DECK, {"group": "tool", "stratum": "product", "defects": "defects", "inspected": "inspected"})
    assert len(result["input"]["sha256"]) == 64 and result["input"]["kind"] == "stratified_rates"


def test_api_and_mcp_run_in_the_statistics_lane_without_storing(tmp_path):
    from fastapi.testclient import TestClient
    from workbench import mcp_server
    from workbench.server import create_app
    with TestClient(create_app(tmp_path / "jobs")) as client:
        before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file() and "research" in p.parts)
        with client.app.state.execution.slot("simulation", "job:held"):
            response = client.post("/api/research/process", json={"kind": "stratified_rates", "text": DECK,
                                   "options": {"group": "tool", "stratum": "product", "defects": "defects", "inspected": "inspected"}})
        assert response.status_code == 200
        assert response.json()["comparisons"][0]["direction_reverses_within_strata"] is True
        task = next(t for t in client.app.state.execution.status()["tasks"] if t["kind"] == "process")
        assert task["lane"] == "quick"
        bad = client.post("/api/research/process", json={"kind": "capability", "text": DECK, "options": {"value": "defects"}})
        assert bad.status_code == 422 and "규격" in bad.json()["detail"]
        assert client.post("/api/research/process", json={"kind": "regression", "text": DECK}).status_code == 422
        after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file() and "research" in p.parts)
        assert after == before

        class AppClient:
            def request(self, method, path, payload=None, timeout=30):
                response = client.request(method, path, json=payload)
                if response.is_error:
                    raise mcp_server.ToolError(f"HTTP {response.status_code}: {response.text}")
                return response.json()
        tool = mcp_server.PolymerLabTools(AppClient()).tools["analyze_process_data"]
        assert tool["definition"]["annotations"]["readOnlyHint"] is True
        text, _ = nested(runs=4, zones=2, wafers=2)
        arguments = {"kind": "variance_components", "csv_text": text, "options": {"value": "thickness", "factors": ["run", "zone"]}}
        mcp_server._check(tool["schema"], arguments)
        assert tool["handler"](arguments)["anova"][0]["source"] == "run"
        with pytest.raises(mcp_server.ToolError, match="HTTP 422"):
            tool["handler"]({**arguments, "options": {"value": "thickness", "factors": ["run", "zone"], "extra": 1}})
