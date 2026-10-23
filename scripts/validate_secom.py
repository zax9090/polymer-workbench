#!/usr/bin/env python3
"""Run the evaluation module on the UCI SECOM data (McCann & Johnston 2008, CC BY 4.0).

Checks the published counts (1567 runs, 104 fails), the flag-nothing baseline (accuracy 1463/1567
= 0.9336 with zero recall) and records random versus time-forward validation of a fixed baseline
model. The data are downloaded at run time and not committed; results go to evidence/process/.
  .venv/bin/python scripts/validate_secom.py                         # downloads from UCI
  .venv/bin/python scripts/validate_secom.py secom.data secom_labels.data
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
from workbench.evaluation import classification_metrics, split_validation  # noqa: E402

BASE = "https://archive.ics.uci.edu/ml/machine-learning-databases/secom/"


def fetch(arguments):
    if arguments:
        return [Path(path).read_bytes() for path in arguments[:2]]
    return [urlopen(BASE + name, timeout=120).read() for name in ("secom.data", "secom_labels.data")]


def main(arguments):
    data, labels = fetch(arguments)
    features = [line.split() for line in data.decode("ascii").splitlines() if line.strip()]
    entries = [line.split(None, 1) for line in labels.decode("ascii").splitlines() if line.strip()]
    if len(features) != len(entries) or len({len(row) for row in features}) != 1:
        raise SystemExit("secom.data and secom_labels.data do not line up; check the files.")
    stamps = [datetime.strptime(stamp.strip().strip('"'), "%d/%m/%Y %H:%M:%S").isoformat() for _, stamp in entries]
    names = [f"s{j:03d}" for j in range(len(features[0]))]
    text = ",".join(["time", "label", *names]) + "\n" + "\n".join(
        ",".join([stamp, label, *row]) for stamp, (label, _), row in zip(stamps, entries, features)) + "\n"
    fails = sum(label == "1" for label, _ in entries)
    baseline = classification_metrics("label,score\n" + "\n".join(f"{label},0" for label, _ in entries), "label", "score")
    split = split_validation(text, "label", names, order="time", impute_median=True, folds=5)
    record = {"evidence_kind": "public_dataset_run", "validated_at": datetime.now(timezone.utc).isoformat(),
              "source": "UCI Machine Learning Repository: SECOM (CC BY 4.0)",
              "sha256": {"secom.data": hashlib.sha256(data).hexdigest(), "secom_labels.data": hashlib.sha256(labels).hexdigest()},
              "runs": len(entries), "features": len(names), "fails": fails,
              "published_counts_match": len(entries) == 1567 and fails == 104 and len(names) == 590,
              "flag_nothing": {key: baseline["thresholds"][0][key] for key in ("accuracy", "recall", "false_negative")},
              "split_validation": split["schemes"], "warnings": split["warnings"],
              "note": "The data files are not redistributed; rerun this script to reproduce."}
    target = ROOT / "evidence" / "process" / "secom-validation.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2) + "\n")
    print(f"runs {len(entries)}, fails {fails}, features {len(names)}; published counts match: {record['published_counts_match']}")
    print(f"flag nothing: accuracy {record['flag_nothing']['accuracy']:.4f}, recall {record['flag_nothing']['recall']}")
    for name, scheme in split["schemes"].items():
        print(f"{name:13s} ROC AUC {scheme.get('roc_auc')}  tested rows {scheme['tested_rows']}")
    print("->", target.relative_to(ROOT))
    return 0 if record["published_counts_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
