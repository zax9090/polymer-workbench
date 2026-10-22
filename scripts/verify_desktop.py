#!/usr/bin/env python3
"""Exercise a frozen Polymer Lab bundle without a GUI or source virtualenv.

Example:
    python3 scripts/verify_desktop.py --bundle dist/PolymerLab \
        --report evidence/desktop-runtime-validation.json

Only Python's standard library is used by this verifier. Every calculation runs
inside the supplied executable with temporary application data and minimal environment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile


TERMINAL = {"completed", "prepared", "blocked", "failed", "cancelled", "interrupted"}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def tail(path: Path, size=12000):
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - size))
        return stream.read().decode("utf-8", errors="replace")


class BundleService:
    def __init__(self, executable: Path, workspace: Path, label: str):
        self.executable = executable
        self.workspace = workspace
        self.data = workspace / "state" / "jobs"
        self.ready = workspace / f"{label}-ready.json"
        self.log_path = workspace / f"{label}-server.log"
        self.token = secrets.token_urlsafe(32)
        self.process = None
        self.origin = None
        self.stream = None

    def start(self, timeout=60):
        self.data.mkdir(parents=True, exist_ok=True)
        # Deliberately exclude all build-machine Python, engine, desktop and
        # shared-library environment variables. Bundle paths must be sufficient.
        environment = {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1",
            "XDG_DATA_HOME": str(self.workspace / "xdg-data"),
            "XDG_CONFIG_HOME": str(self.workspace / "xdg-config"),
            "XDG_CACHE_HOME": str(self.workspace / "xdg-cache"),
            "POLYMER_APP_DATA_DIR": str(self.workspace / "state"),
            "POLYMER_DATA_DIR": str(self.data), "POLYMER_DESKTOP_TOKEN": self.token,
            "POLYMER_PARENT_PID": str(os.getpid()),
        }
        if "HOME" in os.environ:
            environment["HOME"] = os.environ["HOME"]
        self.stream = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(self.executable), "--server", "--port", "0", "--ready-file", str(self.ready), "--data-dir", str(self.data)],
            cwd=self.workspace, env=environment, stdin=subprocess.DEVNULL,
            stdout=self.stream, stderr=subprocess.STDOUT, start_new_session=(os.name == "posix"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Bundle server exited with {self.process.returncode}:\n{tail(self.log_path)}")
            if self.ready.is_file():
                try:
                    ready = json.loads(self.ready.read_text())
                    require(ready["pid"] == self.process.pid, "Readiness PID does not match launched bundle")
                    require(0 < ready["port"] < 65536, "Invalid ephemeral listening port")
                    self.origin = f"http://127.0.0.1:{ready['port']}"
                    return self
                except json.JSONDecodeError:
                    pass
            time.sleep(0.05)
        raise TimeoutError(f"Bundle service did not start within {timeout}s:\n{tail(self.log_path)}")

    def fetch(self, path, *, payload=None, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["X-Polymer-Token"] = self.token
        request = Request(self.origin + path, headers=headers,
                          data=None if payload is None else json.dumps(payload).encode("utf-8"))
        with urlopen(request, timeout=15) as response:
            return response.status, response.headers, response.read()

    def json(self, path, *, payload=None):
        return json.loads(self.fetch(path, payload=payload)[2])

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                raise RuntimeError("Bundle service did not stop gracefully within 20 seconds")
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.process:
            require(self.process.returncode in (0, -15), f"Server exited abnormally: {self.process.returncode}")


def check_service(service, version):
    try:
        service.fetch("/api/jobs", authenticated=False)
    except HTTPError as error:
        require(error.code == 401, f"Unauthenticated service request returned {error.code}, expected 401")
    else:
        raise AssertionError("Unauthenticated client was allowed to read research jobs")
    health = service.json("/api/health")
    require(health.get("ok") is True, "Service is not healthy")
    require(health.get("local_only") is True, "Service did not identify as local-only")
    assets = {}
    for path, expected in (("/", b"Polymer Lab"), ("/static/app.js", b"showView"), ("/static/style.css", b"sidebar"), ("/static/vendor/3Dmol-min.js", b"3Dmol")):
        status, _, content = service.fetch(path)
        require(status == 200 and expected in content, f"Bundled static resource is missing or invalid: {path}")
        assets[path] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    return {"health": health, "version_matches": health.get("version") == version,
            "expected_version": version, "unauthenticated_status": 401, "static_assets": assets}


def check_zip(service, job_id, expected_files):
    status, headers, content = service.fetch(f"/api/jobs/{job_id}/download")
    require(status == 200 and "attachment" in headers.get("Content-Disposition", ""), "Results are not served as a ZIP attachment")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        require({"config.json", "result.json", "manifest.json", "runtime.json", "job.json"} | set(expected_files) <= names,
                f"Expected artifacts missing from result archive: {set(expected_files) - names}")
        require(archive.testzip() is None, "ZIP member CRC validation failed")
        manifest = json.loads(archive.read("manifest.json"))
        require(bool(manifest["files"]), "Artifact manifest is empty")
        for record in manifest["files"]:
            require(record["path"] in names, f"Manifest member missing from ZIP: {record['path']}")
            raw = archive.read(record["path"])
            require(hashlib.sha256(raw).hexdigest() == record["sha256"], f"Artifact SHA256 mismatch: {record['path']}")
            require(len(raw) == record["bytes"], f"Artifact size mismatch: {record['path']}")
        for name in expected_files:
            require(bool(archive.read(name)), f"Empty expected artifact: {name}")
        return {"zip_bytes": len(content), "zip_sha256": hashlib.sha256(content).hexdigest(),
                "manifest_members_verified": len(manifest["files"]), "files": sorted(names)}


def execute_job(service, key, config, expected_status, expected_engine, artifacts, timeout):
    started = time.monotonic()
    config = {"name": "Desktop bundle verification · " + key, "seed": 42, **config}
    created = service.json("/api/jobs", payload=config)
    job_id = created["id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.json("/api/jobs/" + job_id)
        if job["status"] in TERMINAL:
            break
        time.sleep(0.1)
    else:
        service.json(f"/api/jobs/{job_id}/cancel", payload={})
        cancellation_deadline = time.monotonic() + 20
        while time.monotonic() < cancellation_deadline:
            if service.json("/api/jobs/" + job_id)["status"] in TERMINAL:
                break
            time.sleep(0.1)
        raise TimeoutError(f"{key}: calculation exceeded {timeout}s; job {job_id}")
    detail = json.dumps(job.get("result"), ensure_ascii=False) + "\n" + job.get("log", "")[-4000:]
    require(job["status"] == expected_status, f"{key}: expected {expected_status}, got {job['status']}\n{detail}")
    result = job.get("result") or {}
    require(expected_engine.lower() in str(result.get("engine", "")).lower(), f"{key}: expected engine {expected_engine}\n{detail}")
    evidence = check_zip(service, job_id, artifacts)
    if key in {"packmol_handoff", "packmol_protein_handoff"}:
        require(result.get("metadata", {}).get("packing_completed") is True, "PACKMOL did not actually pack the system")
        require(result["metadata"].get("imported_components") == 1, "PACKMOL did not consume the earlier RDKit structure")
        runtime = service.json(f"/api/jobs/{job_id}/artifacts/runtime.json")
        require(runtime.get("source_handoff", {}).get("job_id") == config["source_job_id"], "Cross-job provenance was not retained")
    if key == "radonpy":
        require(result.get("metadata", {}).get("steps") == config["steps"], "RadonPy did not run the requested MD steps")
        require(result["metadata"].get("equilibrated") is False, "Short smoke run was incorrectly marked equilibrated")
    if key == "af3_export":
        exported = service.json(f"/api/jobs/{job_id}/artifacts/alphafold3_input.json")
        require(exported.get("dialect") == "alphafold3", "AF3 input format is missing")
        require(exported["sequences"][0]["protein"]["sequence"] == config["sequence"], "AF3 sequence changed during export")
    if key == "alphafold_database":
        require(result.get("provenance", {}).get("new_inference") is False, "Database import was incorrectly reported as new inference")
        require(result["provenance"].get("accession") == "P69905", "Unexpected AlphaFold DB accession")
        scores = result.get("metrics", {}).get("summary", {})
        require(scores.get("residues", 0) > 0 and 0 <= scores.get("mean_plddt", -1) <= 100,
                "AlphaFold DB structure or pLDDT analysis is missing")
    summary = {"key": key, "id": job_id, "status": job["status"], "engine": result["engine"],
               "elapsed_seconds": round(time.monotonic() - started, 3), "artifacts": evidence}
    print(json.dumps({"check": key, "status": "passed", "job_id": job_id, "engine": result["engine"]}), flush=True)
    return summary


def run_validation(executable, workspace, report, record, timeout, version, with_alphafold_db=False):
    service = BundleService(executable, workspace, "initial")
    try:
        service.start()
        record["service"] = check_service(service, version)
        if not record["service"]["version_matches"]:
            record["validation_errors"].append(f"Expected version {version}, got {record['service']['health'].get('version')}")
        record["capabilities"] = service.json("/api/capabilities")
        report()
        cases = [
            ("numpy", {"route": "polymer_cg", "engine": "numpy", "monomers": 8, "chains": 1, "steps": 20}, "completed", "numpy", ["metrics.json", "trajectory.json", "structure.pdb"]),
            ("lammps", {"route": "polymer_cg", "engine": "lammps", "monomers": 8, "chains": 1, "steps": 20}, "completed", "LAMMPS", ["metrics.json", "trajectory.json", "restart.lammps", "lammps.log"]),
            ("rdkit", {"route": "polymer_atomistic", "atomistic_mode": "build", "repeat_smiles": "[*]CC[*]", "monomers": 4, "chains": 1}, "prepared", "RDKit", ["structure.pdb", "structure.mol", "molecule.json"]),
            ("radonpy", {"route": "polymer_atomistic", "atomistic_mode": "simulate", "repeat_smiles": "[*]CC[*]", "monomers": 4, "chains": 1, "steps": 50, "temperature_kelvin": 350}, "completed", "RadonPy", ["radonpy_complete.json", "random_seeds.json", "final.mol"]),
            ("packmol_handoff", {"route": "packmol", "repeat_smiles": "[*]CC[*]", "monomers": 4, "chains": 2, "source_artifact": "structure.pdb"}, "prepared", "PACKMOL", ["packed.pdb", "imported_component.pdb", "packmol.log"]),
            ("polyply", {"route": "polyply", "polyply_library": "martini3", "polyply_residue": "PEO", "monomers": 4, "chains": 1}, "prepared", "polyply", ["polymer.itp", "system.top", "polyply_topology.log"]),
            ("pysimm", {"route": "pysimm", "repeat_smiles": "[*]CC[*]", "monomers": 4, "chains": 1}, "completed", "pysimm", ["pysimm_complete.json", "final.data", "pysimm.log"]),
            ("af3_export", {"route": "protein", "protein_mode": "export", "sequence": "ACDEFGHIKLMNPQRSTVWY"}, "prepared", "AlphaFold 3 input exporter", ["sequence.fasta", "alphafold3_input.json"]),
        ]
        if with_alphafold_db:
            cases.extend([
                ("alphafold_database", {"route": "protein", "protein_mode": "database", "accession": "P69905"}, "completed", "AlphaFold Protein Structure Database", ["structure.pdb", "alphafold_db_record.json", "metrics.json"]),
                ("packmol_protein_handoff", {"route": "packmol", "repeat_smiles": "[*]CC[*]", "monomers": 4, "chains": 2, "source_artifact": "structure.pdb"}, "prepared", "PACKMOL", ["packed.pdb", "imported_component.pdb", "packmol.log"]),
            ])
        rdkit_id = None
        protein_id = None
        for key, config, status, engine, artifacts in cases:
            try:
                if key == "packmol_handoff":
                    require(rdkit_id is not None, "PACKMOL handoff needs the preceding RDKit route to pass")
                    config["source_job_id"] = rdkit_id
                if key == "packmol_protein_handoff":
                    require(protein_id is not None, "Protein PACKMOL handoff needs the AlphaFold DB route to pass")
                    config["source_job_id"] = protein_id
                entry = execute_job(service, key, config, status, engine, artifacts, timeout)
                record["jobs"].append(entry)
                if key == "rdkit":
                    rdkit_id = entry["id"]
                if key == "alphafold_database":
                    protein_id = entry["id"]
            except Exception as exc:
                failure = {"key": key, "error": str(exc)}
                record["failed_jobs"].append(failure)
                record["validation_errors"].append(f"{key}: {exc}")
                print(json.dumps({"check": key, "status": "failed", "error": str(exc)}), flush=True)
            report()
        expected_history = {entry["id"]: entry["status"] for entry in service.json("/api/jobs")}
        service.stop()
        if service.ready.exists():
            record["validation_errors"].append("Readiness file survived graceful shutdown")
        record["graceful_shutdown"] = True
        report()
        restart = BundleService(executable, workspace, "restart")
        try:
            restart.start()
            history = restart.json("/api/jobs")
            actual = {entry["id"]: entry["status"] for entry in history}
            require(actual == expected_history, f"Research history changed on restart: {actual!r}")
            for entry in record["jobs"]:
                require((restart.data / entry["id"] / "result.json").is_file(), "Result file lost after restart")
            record["restart_persistence"] = {"verified": True, "jobs": len(history)}
            print(json.dumps({"check": "restart_persistence", "status": "passed", "jobs": len(history)}), flush=True)
        finally:
            restart.stop()
        require(not record["validation_errors"], "Verification failed: " + "; ".join(record["validation_errors"]))
    finally:
        try:
            service.stop()
        finally:
            record["server_log_tail"] = tail(service.log_path)
            record["worker_log_tails"] = {str(path.relative_to(workspace)): tail(path, 8000)
                                          for path in service.data.glob("*/*.log")}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True, help="Bundle directory or PolymerLab executable")
    parser.add_argument("--report", type=Path, help="Write incremental and final JSON verification evidence")
    parser.add_argument("--job-timeout", type=float, default=180, help="Maximum seconds per real calculation (default: 180)")
    parser.add_argument("--expected-version", default="0.2.0")
    parser.add_argument("--with-alphafold-db", action="store_true", help="Also download public P69905 from AlphaFold DB and pack it with a polymer; no new folding inference")
    parser.add_argument("--keep-workspace", action="store_true", help="Keep temporary calculations and logs for diagnosis")
    args = parser.parse_args()
    executable = args.bundle.resolve()
    if executable.is_dir():
        executable = executable / "PolymerLab"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        parser.error(f"Executable not found or not executable: {executable}")
    workspace = Path(tempfile.mkdtemp(prefix="polymer-bundle-verification-"))
    record = {"kind": "frozen_desktop_bundle_headless_integration", "started_at": datetime.now(timezone.utc).isoformat(),
              "bundle_executable": str(executable), "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
              "status": "running", "jobs": [], "failed_jobs": [], "validation_errors": [],
              "isolation": {"PATH": "/usr/bin:/bin", "temporary_application_and_XDG_directories": True,
              "PYTHONPATH": False, "inherited_loader_or_engine_variables": False,
              "network_services_used": args.with_alphafold_db,
              "network_services": ["https://alphafold.ebi.ac.uk"] if args.with_alphafold_db else []},
              "not_tested": ["Qt native window visual inspection", "AlphaFold/ColabFold new inference", "GROMACS", "HOOMD-blue"]}

    def write_report():
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.report.with_suffix(args.report.suffix + ".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(args.report)

    try:
        write_report()
        run_validation(executable, workspace, write_report, record, args.job_timeout, args.expected_version, args.with_alphafold_db)
        record["status"] = "passed"
        record["message"] = f"All {len(record['jobs'])} workflow cases, artifact hashes, private service and persistent history verified."
        returncode = 0
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        record["traceback"] = traceback.format_exc()
        print(record["traceback"], file=sys.stderr, flush=True)
        returncode = 1
    finally:
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        if args.keep_workspace:
            record["workspace"] = str(workspace)
        else:
            import shutil
            shutil.rmtree(workspace)
        write_report()
    print(json.dumps({"status": record["status"], "passed_routes": len(record["jobs"]), "report": str(args.report) if args.report else None}), flush=True)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
