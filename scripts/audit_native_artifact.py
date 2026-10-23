#!/usr/bin/env python3
"""Inspect the installed Rust extension with pinned REA, without loading or executing it.

Run with .venv/bin/python after rebuilding ./native. Complete REA Evidence records
are retained alongside a compact finding ledger. No client/MCP configuration is changed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
REA = ROOT / "tools/rea/node_modules/rea-agents/scripts/rea.mjs"
DEFAULT_OUTPUT = ROOT / "evidence/native"
OPERATIONS = {
    "rust-metadata": ["inspect-rust-binary"],
    "file-headers": ["inspect-with-objdump", "file_headers"],
    "section-headers": ["inspect-with-objdump", "section_headers"],
    "symbols": ["inspect-with-objdump", "symbols"],
    "relocations": ["inspect-with-objdump", "relocations"],
}


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installed_binary():
    # Read distribution metadata; importing workbench_native would execute the target.
    package = distribution("workbench-native")
    files = [Path(package.locate_file(path)).resolve() for path in package.files or []
             if str(path).endswith((".so", ".pyd", ".dylib"))]
    if len(files) != 1:
        raise SystemExit("Expected one installed workbench-native shared library; pass --binary explicitly.")
    return files[0]


def run_inspection(label, command, binary, expected_hash, output, node):
    record_path = output / f"{label}.json"
    log_path = output / f"{label}.stderr.log"
    argv = [node, str(REA), command[0], str(binary), *command[1:], "--format", "json"]
    # Reuse complete matching evidence rather than repeating an unchanged inspection.
    try:
        evidence = json.loads(record_path.read_text()) if record_path.is_file() else None
    except (ValueError, OSError):
        evidence = None
    # Cleanup errors may carry useful partial Evidence. Retain the whole failure
    # envelope and its limits; do not blindly repeat an explicitly nonretryable call.
    failure = evidence if evidence and "code" in evidence else None
    observation = failure.get("details", {}).get("partial_observation") if failure else evidence
    normalized = (observation or {}).get("normalized_result") or {}
    reusable = (observation is not None
                and observation.get("operation") == command[0].replace("-", "_")
                and observation.get("subject", {}).get("digest", {}).get("sha256") == expected_hash
                and not normalized.get("artifact_changed", False)
                and normalized.get("exit_code", 0) == 0)
    if not reusable:
        with record_path.open("w") as stdout, log_path.open("w") as stderr:
            completed = subprocess.run(argv, cwd=ROOT, stdout=stdout, stderr=stderr, text=True, timeout=180)
        evidence = json.loads(record_path.read_text())
        failure = evidence if "code" in evidence else None
        observation = failure.get("details", {}).get("partial_observation") if failure else evidence
        if not observation:
            raise SystemExit(f"REA {label} failed with exit {completed.returncode}; see {record_path} and {log_path}")
    normalized = observation.get("normalized_result") or {}
    observed_hash = observation.get("subject", {}).get("digest", {}).get("sha256")
    if observed_hash != expected_hash or normalized.get("artifact_changed", False):
        raise SystemExit(f"REA {label}: artifact identity changed or was not bound to the expected SHA-256")
    complete = not failure and not normalized.get("output_truncated", False)
    if normalized.get("exit_code", 0) != 0:
        raise SystemExit(f"REA {label}: analysis helper did not complete successfully")
    return observation, {
        "evidence_id": observation["evidence_id"], "operation": observation["operation"],
        "subject_sha256": observed_hash, "evidence": record_path.name,
        "evidence_sha256": sha256(record_path), "stderr": log_path.name,
        "command": [Path(node).name, str(REA.relative_to(ROOT)), command[0], str(binary), *command[1:], "--format", "json"],
        "output_complete": complete, "limitations": observation.get("limitations", []),
        "provider_failure": {"code": failure["code"], "message": failure["message"],
                             "reason": failure.get("details", {}).get("cleanup_reason")} if failure else None,
        "reused_matching_evidence": reusable,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, help="Explicit shared library; defaults to installed workbench-native")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not REA.is_file():
        raise SystemExit("Install pinned REA with npm_config_cache=/workspace/.cache/npm python3 tools/rea/bootstrap.py")
    node = shutil.which("node")
    if not node or not shutil.which("objdump"):
        raise SystemExit("A supported Node.js and GNU objdump are required.")
    binary = (args.binary or installed_binary()).resolve()
    digest = sha256(binary)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    records, raw = {}, {}
    for label, command in OPERATIONS.items():
        raw[label], records[label] = run_inspection(label, command, binary, digest, output, node)
    if sha256(binary) != digest:
        raise SystemExit("Shared library changed during the audit; rebuild first and rerun.")
    package = json.loads((REA.parent.parent / "package.json").read_text())
    lock = ROOT / "tools/rea/package-lock.json"
    lock_record = json.loads(lock.read_text())["packages"]["node_modules/rea-agents"]
    symbols = raw["symbols"].get("normalized_result", {}).get("stdout", "")
    headers = raw["file-headers"].get("normalized_result", {}).get("stdout", "")
    relevant = [line.strip() for line in symbols.splitlines()
                if any(name in line for name in ("PyInit_workbench_native", "doe", "trajectory", "free_volume", "widom"))]
    source_paths = [ROOT / "native/Cargo.toml", ROOT / "native/Cargo.lock", ROOT / "native/pyproject.toml",
                    *sorted((ROOT / "native/src").glob("*.rs"))]
    ledger = {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "artifact": {"path": str(binary), "sha256": digest, "bytes": binary.stat().st_size},
        "tool": {"name": package["name"], "version": package["version"],
                 "lock_sha256": sha256(lock), "registry_integrity": lock_record["integrity"]},
        "status": "complete_static_inspection" if all(r["output_complete"] for r in records.values()) else "partial_static_inspection",
        "inspections": records,
        "observations": {"file_headers": headers.strip(), "selected_symbol_lines": relevant,
                         "rust_metadata": {key: raw["rust-metadata"]["normalized_result"].get(key)
                                           for key in ("format", "architecture", "bits", "byte_order", "auditable_metadata", "rustc_commit")}},
        "source_snapshot_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in source_paths},
        "limitations": [
            "This is actual REA inspection of the compiled local extension, not an audit of proprietary Materials Studio or Minitab artifacts.",
            "ELF headers, sections, symbols and relocations are static observations; no target was loaded or executed and no decompiler was used.",
            "Symbol names do not establish numerical correctness or API behavior; regression and independent numerical comparison tests supply that evidence separately.",
            "The separately recorded source snapshot is build context, not an embedded or cryptographic source-to-binary attestation.",
            "A missing cargo-auditable .dep-v0 section means the embedded dependency list is unavailable, not that the binary has no dependencies.",
            "Any provider cleanup failure is preserved in the raw record and marks that inspection partial, even when its captured helper output reports exit 0.",
        ],
    }
    (output / "audit.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": ledger["status"], "artifact_sha256": digest,
                      "evidence_ids": {k: v["evidence_id"] for k, v in records.items()},
                      "ledger": str(output / "audit.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
