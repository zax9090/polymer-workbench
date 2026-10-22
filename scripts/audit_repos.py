#!/usr/bin/env python3
"""Verify pinned upstream snapshots and rerun actual local REA inspections.

No upstream application is imported or executed. REA's reference importer records
source identity; it does not decompile Python/C++/Fortran or validate scientific
behavior. The 3Dmol path additionally receives REA static JavaScript/TS analysis.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
REA = ROOT / "tools/rea/node_modules/rea-agents/scripts/rea.mjs"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def source_fingerprint(directory: Path) -> str:
    rows = [(str(path.relative_to(directory)), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(directory.rglob("*")) if path.is_file() and not path.is_symlink()]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def run_rea(label: str, arguments: list[str], timeout: int = 180) -> dict:
    out = EVIDENCE / "rea" / f"{label}.json"
    err = EVIDENCE / "rea" / f"{label}.stderr.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [shutil.which("node") or "node", str(REA), *arguments, "--format", "json"]
    source_dir = Path(arguments[1]) if arguments[0] in {"import-reference-source", "analyze-javascript-application"} else None
    before = source_fingerprint(source_dir) if source_dir else None
    started = time.monotonic()
    with out.open("w") as stdout, err.open("w") as stderr:
        try:
            result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=timeout, cwd=ROOT)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124
    record = {"command": command, "returncode": code, "elapsed_seconds": round(time.monotonic() - started, 3),
              "stdout": str(out.relative_to(ROOT)), "stderr": str(err.relative_to(ROOT)),
              "executed_at": datetime.now(timezone.utc).isoformat(), "source_set_sha256": before,
              "source_unchanged_during_analysis": before == source_fingerprint(source_dir) if source_dir else None}
    write_json(out.with_suffix(".command.json"), record)
    return record


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def verify_source(repo: dict, source: dict, refresh: bool) -> dict:
    path = EVIDENCE / "sources" / repo["id"] / source["path"]
    if refresh or not path.exists():
        # Commit-pinned official URL, no branch resolution and no local code execution.
        data = urlopen(source["source_url"], timeout=45).read()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if blob != source["github_blob_sha"]:
            raise ValueError(f"Upstream blob mismatch: {repo['id']}/{source['path']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    data = path.read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    if blob != source["github_blob_sha"]:
        raise ValueError(f"Snapshot changed: {path}; use --refresh-sources to restore the pinned source")
    return {**source, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "local_path": str(path.relative_to(ROOT)), "verified": True}


def generate_report(catalog: list[dict], contracts: dict, refresh: bool) -> dict:
    repos = []
    for row in catalog:
        contract = contracts[row["id"]]
        source_files = [verify_source(row, item, refresh) for item in row["source_files"]]
        by_path = {item["path"]: item for item in source_files}
        entrypoints = []
        for path, needle, description in contract["anchors"]:
            lines = (EVIDENCE / "sources" / row["id"] / path).read_text().splitlines()
            matching = [i for i, line in enumerate(lines, 1) if needle in line]
            if not matching:
                raise ValueError(f"Audit anchor not found: {row['id']}/{path}: {needle}")
            line = matching[0]
            entrypoints.append({"path": path, "line_start": line, "line_end": min(line + 8, len(lines)),
                                "symbol": needle.strip(), "description": description,
                                "sha256": by_path[path]["sha256"],
                                "url": f"{row['url']}/blob/{row['commit']}/{path}#L{line}"})
        reference_path = EVIDENCE / "rea" / f"{row['id']}.reference.json"
        reference = read_json(reference_path, {})
        reference_cmd = read_json(reference_path.with_suffix(".command.json"), {})
        fingerprint = source_fingerprint(EVIDENCE / "sources" / row["id"])
        successful = reference.get("schema") == "HistoricalSourceSummary" and reference_cmd.get("returncode") == 0
        current = reference_cmd.get("source_set_sha256") == fingerprint and reference_cmd.get("source_unchanged_during_analysis") is True
        status = "verified" if successful and current else "stale" if successful else "not_run"
        rea = {"status": status, "method": "historical_source_inventory", "raw_output": str(reference_path.relative_to(ROOT)),
               "manifest": reference.get("manifest"), "command": reference_cmd,
               "meaning": "REA 소스 인벤토리·해시 확인입니다. Python/C++/Fortran 디컴파일이나 과학적 검증이 아닙니다."}
        if row["id"] == "3dmol":
            js_path = EVIDENCE / "rea/3dmol.javascript.json"
            js = read_json(js_path, {})
            js_cmd = read_json(js_path.with_suffix(".command.json"), {})
            normalized = js.get("normalized_result", {})
            rea.update({"method": "historical_source_inventory_and_static_javascript", "javascript_raw_output": str(js_path.relative_to(ROOT)),
                        "javascript_evidence_id": js.get("evidence_id"), "javascript_stats": normalized.get("statistics"),
                        "javascript_limitations": js.get("limitations", normalized.get("limitations")),
                        "javascript_status": "verified" if js.get("predicate_type") == "rea.javascript-application-analysis" and js_cmd.get("source_set_sha256") == fingerprint and js_cmd.get("source_unchanged_during_analysis") is True else "not_run"})
        repos.append({**row, **{k: v for k, v in contract.items() if k != "anchors"}, "source_files": source_files,
                      "entrypoints": entrypoints, "rea": rea, "source_review": "manual_static_interface_review",
                      "snapshot_scope": "selected_entrypoints_not_complete_repository",
                      "license_url": f"{row['url']}/blob/{row['commit']}/{contract['license_file']}"})
    package = read_json(ROOT / "tools/rea/node_modules/rea-agents/package.json", {})
    return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_provider": "GitHub connector for pinned commit/file reads; official raw URLs for byte-exact refresh",
            "rea": {"version": package.get("version"), "installed_locally": REA.exists(), "installation": "tools/rea",
                    "capabilities": "evidence/rea/capabilities.json", "providers": "evidence/rea/providers.json",
                    "limitations": ["Partial source snapshots, not full repository or dependency audits.",
                                    "Historical reference import is identity/inventory analysis, not native/Python decompilation.",
                                    "Only the selected 3Dmol sources received JavaScript/TypeScript static graph analysis.",
                                    "No AlphaFold model inference or native decompiler execution is implied by this report."]},
            "repositories": repos}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-rea", action="store_true", help="Rerun local REA reference inspections and 3Dmol static analysis")
    parser.add_argument("--refresh-sources", action="store_true", help="Restore exact pinned source files from official raw GitHub URLs")
    args = parser.parse_args()
    catalog = read_json(ROOT / "tools/rea/catalog.json")
    contracts = read_json(ROOT / "tools/rea/contracts.json")
    # Integrity failures stop before any source inspection.
    generate_report(catalog, contracts, args.refresh_sources)
    if args.run_rea:
        if not REA.exists():
            raise RuntimeError("REA not installed; run python tools/rea/bootstrap.py (see tools/rea/README.md)")
        commands = [(f"{repo['id']}.reference", ["import-reference-source", str(EVIDENCE / "sources" / repo["id"]), "--detail", "summary"]) for repo in catalog]
        commands.extend([("3dmol.javascript", ["analyze-javascript-application", str(EVIDENCE / "sources/3dmol")]),
                         ("capabilities", ["capabilities"]), ("providers", ["providers"])])
        failures = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(run_rea, label, arguments): label for label, arguments in commands}
            for future in as_completed(futures):
                record = future.result()
                print(f"{futures[future]}: exit {record['returncode']}", flush=True)
                if record["returncode"] != 0:
                    failures.append(record)
        if failures:
            write_json(EVIDENCE / "rea/failures.json", failures)
            print("Some REA operations failed; retained raw diagnostics.", file=sys.stderr)
    report = generate_report(catalog, contracts, False)
    write_json(EVIDENCE / "repositories.json", report)
    print(f"Verified {len(report['repositories'])} repositories / {sum(len(r['source_files']) for r in report['repositories'])} pinned files")
    print(EVIDENCE / "repositories.json")
    return 1 if args.run_rea and failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Audit failed: {error}", file=sys.stderr)
        raise SystemExit(1)
