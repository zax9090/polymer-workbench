#!/usr/bin/env python3
"""Isolated process entry point: no engine runs in the HTTP server process."""
import argparse
import json
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.paths import install_worker_lifecycle
install_worker_lifecycle()
from workbench.science import run_workflow, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    try:
        result = run_workflow(json.loads(Path(args.config).read_text()), output)
        print(json.dumps({"status": result["status"], "result": str(output / "result.json")}))
        return 0
    except Exception as exc:
        traceback.print_exc()
        write_json(output / "result.json", {"status": "failed", "message": str(exc), "error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
