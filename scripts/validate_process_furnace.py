#!/usr/bin/env python3
"""Compare workbench.process with the NIST/SEMATECH e-Handbook furnace case study.

The handbook's oxide-thickness data (21 runs x 4 zones x 2 wafers) are downloaded at run time and
never committed; only the file hash and the comparison are written to evidence/process/.
  .venv/bin/python scripts/validate_process_furnace.py            # downloads from NIST
  .venv/bin/python scripts/validate_process_furnace.py FURNACE.DAT  # or use a local copy
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.process import variance_components  # noqa: E402

URL = "https://www.itl.nist.gov/div898/handbook/datasets/FURNACE.DAT"
# https://itl.nist.gov/div898/handbook/ppc/section4/ppc44.htm and .../section5/ppc514.htm
# (nested ANOVA with zone within run tested against wafers; components from expected mean squares).
PUBLISHED = {"variance_run": 312.56, "variance_zone_within_run": 225.38, "variance_residual": 120.89,
             "f_run": 5.37404, "f_zone_within_run": 4.72864}


def rows(raw):
    """Lines of four numbers (run, zone, wafer, thickness); header text is skipped."""
    out = []
    for line in raw.decode("ascii", "replace").splitlines():
        fields = line.split()
        try:
            values = [float(v) for v in fields]
        except ValueError:
            continue
        if len(values) == 4:
            out.append(values)
    return out


def main(arguments):
    raw = Path(arguments[0]).read_bytes() if arguments else urlopen(URL, timeout=60).read()
    data = rows(raw)
    if len(data) != 168:
        raise SystemExit(f"Expected 168 observations, found {len(data)}; check the file.")
    text = "run,zone,wafer,thickness\n" + "\n".join(f"{int(r)},{int(z)},{int(w)},{t!r}" for r, z, w, t in data)
    result = variance_components(text, "thickness", ["run", "zone"], unit="angstrom")
    run, zone, residual = result["anova"]
    ours = {"variance_run": run["variance_component"], "variance_zone_within_run": zone["variance_component"],
            "variance_residual": residual["variance_component"], "f_run": run["f"], "f_zone_within_run": zone["f"]}
    # Published values are rounded to 2 decimals (components) and 5 decimals (F).
    tolerance = {key: (0.005 if key.startswith("variance") else 0.000005) for key in PUBLISHED}
    comparison = {key: {"ours": ours[key], "published": PUBLISHED[key], "difference": ours[key] - PUBLISHED[key],
                        "within_rounding": abs(ours[key] - PUBLISHED[key]) <= tolerance[key]} for key in PUBLISHED}
    record = {"evidence_kind": "independent_published_comparison", "validated_at": datetime.now(timezone.utc).isoformat(),
              "source": arguments[0] if arguments else URL, "source_sha256": hashlib.sha256(raw).hexdigest(),
              "observations": len(data), "grand_mean": result["grand_mean"], "comparison": comparison,
              "anova": result["anova"], "passed": all(item["within_rounding"] for item in comparison.values()),
              "note": "The data file is not redistributed; rerun this script to reproduce."}
    target = ROOT / "evidence" / "process" / "furnace-validation.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2) + "\n")
    for key, item in comparison.items():
        print(f"{key:28s} ours {item['ours']:.6f}  published {item['published']}  {'ok' if item['within_rounding'] else 'DIFFERENT'}")
    print("passed" if record["passed"] else "FAILED", "->", target.relative_to(ROOT))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
