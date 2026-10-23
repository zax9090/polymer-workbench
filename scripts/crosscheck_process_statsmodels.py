#!/usr/bin/env python3
"""Cross-check workbench.process against statsmodels 0.14.5 on random inputs.

statsmodels is not a Polymer Lab dependency; run this in a separate environment:
  python3 -m venv /tmp/sm && /tmp/sm/bin/pip install statsmodels==0.14.5
  /tmp/sm/bin/python -I scripts/crosscheck_process_statsmodels.py .
"""
import sys
import numpy as np, pandas as pd
import statsmodels.formula.api as smf
from statsmodels.stats.anova import anova_lm
from statsmodels.stats.contingency_tables import StratifiedTable
sys.path.insert(0, sys.argv[1])
from workbench.process import variance_components, stratified_rates

rng = np.random.default_rng(2026)
worst = {"ss": 0.0, "or": 0.0, "rr": 0.0, "ci": 0.0, "cmh": 0.0, "bd": 0.0}
for case in range(40):
    runs, zones, reps = rng.integers(3, 25), rng.integers(2, 6), rng.integers(2, 4)
    rows = [(r, z, 500 + rng.normal(0, 15) * (r % 3) + rng.normal(0, 8) * z + rng.normal(0, 5))
            for r in range(runs) for z in range(zones) for _ in range(reps)]
    frame = pd.DataFrame(rows, columns=["run", "zone", "y"])
    ours = variance_components(frame.to_csv(index=False), "y", ["run", "zone"])
    table = anova_lm(smf.ols("y ~ C(run) + C(run):C(zone)", frame).fit(), typ=1)
    reference = [table.loc["C(run)", "sum_sq"], table.loc["C(run):C(zone)", "sum_sq"], table.loc["Residual", "sum_sq"]]
    dfs = [table.loc["C(run)", "df"], table.loc["C(run):C(zone)", "df"], table.loc["Residual", "df"]]
    assert [row["df"] for row in ours["anova"]] == [int(v) for v in dfs], (ours["anova"], dfs)
    worst["ss"] = max(worst["ss"], max(abs(row["ss"] - ref) / ref for row, ref in zip(ours["anova"], reference)))

    strata = rng.integers(2, 6)
    lines, tables = ["g,s,d,n"], []
    for s in range(strata):
        n1, n0 = rng.integers(20, 500, 2)
        a, b = rng.binomial(n1, rng.uniform(0.02, 0.3)), rng.binomial(n0, rng.uniform(0.02, 0.3))
        a, b = max(a, 1), max(b, 1)
        lines += [f"B,{s},{a},{n1}", f"A,{s},{b},{n0}"]
        tables.append([[a, n1 - a], [b, n0 - b]])
    result = stratified_rates("\n".join(lines), "g", "s", defects="d", inspected="n")["comparisons"][0]
    st = StratifiedTable([np.asarray(t, float) for t in tables])
    worst["or"] = max(worst["or"], abs(result["odds_ratio"] / st.oddsratio_pooled - 1))
    worst["rr"] = max(worst["rr"], abs(result["risk_ratio"] / st.riskratio_pooled - 1))
    worst["ci"] = max(worst["ci"], max(abs(x / y - 1) for x, y in zip(result["odds_ratio_95_interval"], st.oddsratio_pooled_confint())))
    worst["cmh"] = max(worst["cmh"], abs(result["cmh_chi_square"] / st.test_null_odds(correction=False).statistic - 1))
    worst["bd"] = max(worst["bd"], abs(result["breslow_day"]["chi_square"] - st.test_equal_odds(adjust=False).statistic))
print({k: f"{v:.2e}" for k, v in worst.items()})
