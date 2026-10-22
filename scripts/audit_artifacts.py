#!/usr/bin/env python3
"""REA inventory of the exact installed engine packages that Polymer Lab executes.

Source snapshots in evidence/rea describe upstream repositories; this audit covers the
released artifact instead: download the pinned PyPI file, verify its SHA-256, run REA
inspect-artifact, and match every executed source file to the installed bytes.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tools/rea/artifact_catalog.json"
BASE = ROOT / "evidence/artifacts"
REA = ROOT / "tools/rea/node_modules/rea-agents/scripts/rea.mjs"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def installed_root(package_file):
    top = package_file.split("/", 1)[0]
    spec = importlib.util.find_spec(top)
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit(f"{top} is not installed in {sys.executable}")
    return Path(list(spec.submodule_search_locations)[0]).parent


def audit(entry):
    downloads = BASE / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    artifact = downloads / entry["artifact"]
    if not artifact.is_file() or sha256(artifact) != entry["sha256"]:
        subprocess.run([sys.executable, "-m", "pip", "download", "--quiet", "--no-deps", "--disable-pip-version-check",
                        f"{entry['id']}=={entry['version']}", "-d", str(downloads)], check=True, timeout=300)
    if sha256(artifact) != entry["sha256"]:
        raise SystemExit(f"{entry['artifact']} does not match the pinned SHA-256")
    argv = [shutil.which("node") or "node", str(REA), "inspect-artifact", str(artifact), "--format", "json"]
    raw = BASE / f"{entry['id']}.inspect.json"
    with raw.open("w") as stdout:
        code = subprocess.run(argv, cwd=ROOT, stdout=stdout, stderr=subprocess.PIPE, text=True, timeout=600).returncode
    inventory = json.loads(raw.read_text())["normalized_result"]["substeps"][0]["evidence"]["normalized_result"]
    by_id = {node["artifact_id"]: node for node in inventory["nodes"]}
    listed = {occ["logical_path"]: by_id[occ["artifact_id"]]["sha256"] for occ in inventory["occurrences"] if occ.get("artifact_id") in by_id}
    files = []
    for name in entry["files"]:
        installed = installed_root(name) / name
        files.append({"path": name, "artifact_sha256": listed.get(name), "installed_sha256": sha256(installed),
                      "matches_installed": listed.get(name) == sha256(installed)})
    anchors = []
    for path, needle, purpose in entry["anchors"]:
        lines = (installed_root(path) / path).read_text().splitlines()
        hits = [number for number, line in enumerate(lines, 1) if line.startswith(needle)]
        if not hits:
            raise SystemExit(f"Anchor not found in installed {path}: {needle!r}")
        anchors.append({"path": path, "needle": needle, "line": hits[0], "purpose": purpose})
    record = {**{k: entry[k] for k in ("id", "version", "license", "artifact", "sha256", "purpose")},
              "rea": {"argv": [Path(argv[0]).name, *argv[1:3], entry["artifact"], *argv[4:]], "returncode": code,
                      "coverage": inventory.get("coverage"), "entries": len(inventory["occurrences"]),
                      "integrity_contradictions": inventory.get("integrity_contradictions"), "output": raw.name},
              "files": files, "anchors": anchors, "audited_at": datetime.now(timezone.utc).isoformat(),
              "status": "verified_installed_artifact" if code == 0 and all(f["matches_installed"] for f in files) else "mismatch",
              "limitations": ["REA inventories and hashes the release archive; it does not execute or decompile it.",
                              "A matching file hash shows the executed code is the released code, not that its science is correct."]}
    (BASE / f"{entry['id']}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(f"{entry['id']} {entry['version']}: {record['status']}")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()
    if not REA.exists():
        raise SystemExit("Local REA unavailable; run tools/rea/bootstrap.py")
    records = [audit(entry) for entry in json.loads(CATALOG.read_text()) if not args.only or entry["id"] in args.only]
    return 0 if all(r["status"] == "verified_installed_artifact" for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
