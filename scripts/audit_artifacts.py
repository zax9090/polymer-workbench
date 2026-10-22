#!/usr/bin/env python3
"""REA inventory of the exact installed engine packages and release archives that Polymer Lab executes.

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
import tarfile
import tempfile
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tools/rea/artifact_catalog.json"
BASE = ROOT / "evidence/artifacts"
REA = ROOT / "tools/rea/node_modules/rea-agents/scripts/rea.mjs"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def installed_root(package_file):
    top = package_file.split("/", 1)[0]
    if top == package_file and top.endswith(".py"):
        # Single-module distribution (jcamp.py): the module sits directly in site-packages.
        spec = importlib.util.find_spec(top[:-3])
        if spec is None or not spec.origin:
            raise SystemExit(f"{top[:-3]} is not installed in {sys.executable}")
        return Path(spec.origin).parent
    spec = importlib.util.find_spec(top)
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit(f"{top} is not installed in {sys.executable}")
    return Path(list(spec.submodule_search_locations)[0]).parent


def audit(entry):
    downloads = BASE / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    artifact = downloads / entry["artifact"]
    if not artifact.is_file() or sha256(artifact) != entry["sha256"]:
        subprocess.run([sys.executable, "-m", "pip", "download", "--quiet", "--no-deps", "--only-binary", ":all:", "--disable-pip-version-check",
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
    # Browser bundles copied into workbench/static instead of installing the Python package.
    for item in entry.get("vendored", []):
        vendored = ROOT / item["repository_path"]
        files.append({"path": item["artifact_path"], "artifact_sha256": listed.get(item["artifact_path"]),
                      "vendored_path": item["repository_path"], "vendored_sha256": sha256(vendored),
                      "modification": item["modification"],
                      "matches_installed": listed.get(item["artifact_path"]) == item["original_sha256"] and sha256(vendored) == item["vendored_sha256"]})
    anchors = []
    for path, needle, purpose in entry["anchors"]:
        source = ROOT / path if (ROOT / path).is_file() and path.startswith("workbench/") else installed_root(path) / path
        lines = source.read_text().splitlines()
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


def _download(entry, artifact):
    if artifact.is_file() and sha256(artifact) == entry["sha256"]:
        return
    if "url" in entry:
        request = Request(entry["url"], headers={"User-Agent": "PolymerLab-REA-audit/0.3"})
        with urlopen(request, timeout=300) as response, artifact.open("wb") as stream:
            shutil.copyfileobj(response, stream)
    else:
        subprocess.run([sys.executable, "-m", "pip", "download", "--quiet", "--no-deps", "--no-binary", ":all:", "--disable-pip-version-check",
                        f"{entry['id']}=={entry['version']}", "-d", str(artifact.parent)], check=True, timeout=300)
    if sha256(artifact) != entry["sha256"]:
        raise SystemExit(f"{entry['artifact']} does not match the pinned SHA-256")


def _rea_objdump(path, operation, raw):
    argv = [shutil.which("node") or "node", str(REA), "inspect-with-objdump", str(path), operation, "--format", "json"]
    with raw.open("w") as stdout:
        code = subprocess.run(argv, cwd=ROOT, stdout=stdout, stderr=subprocess.PIPE, text=True, timeout=600).returncode
    evidence = json.loads(raw.read_text())
    return {"argv": [Path(argv[0]).name, argv[2], Path(path).name, operation, "--format", "json"], "returncode": code,
            "subject_sha256": (evidence.get("subject") or {}).get("digest", {}).get("sha256"),
            "stdout_excerpt": ((evidence.get("raw_result") or {}).get("stdout") or "").strip().splitlines()[-3:], "output": raw.name}


def audit_archive(entry):
    """tar releases: REA inspect-artifact does not open tar, so members are hashed here and the
    executable itself is inspected with REA inspect-with-objdump."""
    downloads = BASE / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    artifact = downloads / entry["artifact"]
    _download(entry, artifact)
    members, links = {}, []
    with tarfile.open(artifact) as bundle:
        for member in bundle.getmembers():
            if member.issym() or member.islnk():
                links.append({"member": member.name, "target": member.linkname})
            elif member.isfile():
                members[member.name] = hashlib.sha256(bundle.extractfile(member).read()).hexdigest()
        files = []
        for name in entry["files"]:
            record = {"path": name, "archive_sha256": members.get(name)}
            if name in entry.get("installed", {}):
                installed = installed_root(entry["installed"][name]) / entry["installed"][name]
                record.update(installed_path=entry["installed"][name], installed_sha256=sha256(installed),
                              matches_installed=members.get(name) == sha256(installed))
            files.append(record)
        anchors = []
        for path, needle, purpose in entry["anchors"]:
            lines = bundle.extractfile(path).read().decode("utf-8", "replace").splitlines()
            hits = [number for number, line in enumerate(lines, 1) if needle in line]
            if not hits:
                raise SystemExit(f"Anchor not found in {entry['artifact']}:{path}: {needle!r}")
            anchors.append({"path": path, "needle": needle, "line": hits[0], "purpose": purpose})
        rea = None
        if entry.get("executable"):
            with tempfile.TemporaryDirectory() as scratch:
                target = Path(scratch) / Path(entry["executable"]).name
                target.write_bytes(bundle.extractfile(entry["executable"]).read())
                rea = {op: _rea_objdump(target, op, BASE / f"{entry['id']}.objdump-{op}.json") for op in ("file_headers", "section_headers")}
                rea["executable_sha256"] = members[entry["executable"]]
    ok = all(f["archive_sha256"] for f in files) and all(f.get("matches_installed", True) for f in files)
    ok = ok and (rea is None or all(rea[op]["returncode"] == 0 and rea[op]["subject_sha256"] == rea["executable_sha256"] for op in ("file_headers", "section_headers")))
    record = {**{k: entry[k] for k in ("id", "version", "license", "artifact", "sha256", "purpose")}, "kind": "archive",
              "url": entry.get("url"), "members": len(members), "links": links, "files": files, "anchors": anchors, "rea": rea,
              "audited_at": datetime.now(timezone.utc).isoformat(),
              "status": "verified_archive" if ok else "mismatch",
              "limitations": ["REA 6.7.0 inspect-artifact does not open tar archives; member hashes come from Python tarfile.",
                              "REA inspect-with-objdump reads ELF headers and sections; no deep disassembly provider (Ghidra, Hopper, IDA, Kuna) is configured.",
                              "A verified archive shows which release was used, not that its science is correct."]}
    (BASE / f"{entry['id']}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(f"{entry['id']} {entry['version']}: {record['status']}")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()
    if not REA.exists():
        raise SystemExit("Local REA unavailable; run tools/rea/bootstrap.py")
    records = [(audit_archive if entry.get("kind") == "archive" else audit)(entry)
               for entry in json.loads(CATALOG.read_text()) if not args.only or entry["id"] in args.only]
    return 0 if all(r["status"] in {"verified_installed_artifact", "verified_archive"} for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
