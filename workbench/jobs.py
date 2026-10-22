"""Persistent local jobs, subprocess isolation, cancellation and provenance."""
from __future__ import annotations
import hashlib
from importlib.metadata import version, PackageNotFoundError
import json
import os
from pathlib import Path
import signal
import platform
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from .paths import resource_root, runtime_environment, worker_command

ROOT = resource_root()
TERMINAL = {"completed", "failed", "blocked", "cancelled", "interrupted", "prepared"}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


class JobManager:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="polymer-job")
        self.processes = {}
        self.stop_events = {}
        self.cancelled = set()
        self.closed = False
        self.on_finished = None
        for meta in self.root.glob("*/job.json"):
            try:
                job = json.loads(meta.read_text())
                if job["status"] not in TERMINAL:
                    job.update(status="interrupted", finished_at=now(), error="서버가 종료되어 작업이 중단되었습니다. 다시 실행할 수 있습니다.")
                    atomic_json(meta, job)
            except (ValueError, KeyError, OSError):
                continue

    def directory(self, job_id):
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise KeyError(job_id)
        path = self.root / job_id
        if not (path / "job.json").is_file():
            raise KeyError(job_id)
        return path

    def _read(self, job_id):
        return json.loads((self.directory(job_id) / "job.json").read_text())

    def _update(self, job_id, **values):
        with self.lock:
            job = self._read(job_id)
            job.update(values)
            atomic_json(self.directory(job_id) / "job.json", job)
            return job

    def create(self, config, submitted_via=None):
        with self.lock:
            if self.closed:
                raise RuntimeError("Server is stopping")
            job_id = uuid.uuid4().hex
            directory = self.root / job_id
            directory.mkdir()
            atomic_json(directory / "config.json", config)
            job = {"id": job_id, "name": config["name"], "config": config, "status": "queued",
                   "created_at": now(), "started_at": None, "finished_at": None, "error": None}
            if submitted_via:
                job["submitted_via"] = submitted_via
            atomic_json(directory / "job.json", job)
            self.pool.submit(self._run, job_id)
            return self.get(job_id)

    def _run(self, job_id):
        directory = self.directory(job_id)
        try:
            with self.lock:
                if job_id in self.cancelled:
                    return
                self._update(job_id, status="running", started_at=now())
                self._runtime(directory)
                env = runtime_environment()
                # An isolated interpreter keeps long native MD/GPU work outside the API process.
                log = open(directory / "worker.log", "w", encoding="utf-8")
                try:
                    proc = subprocess.Popen(worker_command(directory / "config.json", directory, source_root=ROOT),
                        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                except Exception:
                    log.close()
                    raise
                self.processes[job_id] = proc
            try:
                rc = proc.wait()
            finally:
                log.close()
            # A native child may outlive its Python leader. Wait for group cleanup
            # before making a cancelled run downloadable.
            while True:
                with self.lock:
                    stop_event = self.stop_events.get(job_id)
                    if not stop_event or stop_event.is_set():
                        self.processes.pop(job_id, None)
                        if job_id in self.cancelled:
                            self._manifest(directory)
                            self._update(job_id, status="cancelled", finished_at=now())
                            return
                        result_file = directory / "result.json"
                        result = json.loads(result_file.read_text()) if result_file.exists() else {}
                        status = result.get("status", "completed" if rc == 0 and result else "failed")
                        if status not in {"completed", "blocked", "prepared", "failed"}:
                            status = "failed"
                        if rc != 0 and status not in {"blocked", "failed"}:
                            status = "failed"
                        error = (result.get("error") or result.get("message")) if status in {"blocked", "failed"} else None
                        if status == "failed" and not error:
                            error = f"작업 종료 코드 {rc}. 실행 로그를 확인하세요."
                        self._manifest(directory)
                        self._update(job_id, status=status, finished_at=now(), error=error)
                        return
                stop_event.wait(timeout=0.25)
        except Exception as exc:
            with self.lock:
                self.processes.pop(job_id, None)
                self._update(job_id, status="cancelled" if job_id in self.cancelled else "failed",
                             finished_at=now(), error=str(exc))
        finally:
            if self.on_finished is not None:
                try:
                    self.on_finished(job_id)
                except Exception as exc:
                    # Data indexing must never change a completed simulation.
                    import logging
                    logging.getLogger(__name__).exception("Research indexing failed for %s: %s", job_id, exc)

    def _manifest(self, directory):
        entries = []
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.name not in {"job.json", "manifest.json"}:
                h = hashlib.sha256()
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                entries.append({"path": str(path.relative_to(directory)), "sha256": h.hexdigest(), "bytes": path.stat().st_size})
        atomic_json(directory / "manifest.json", {"created_at": now(), "files": entries})

    def _runtime(self, directory):
        packages = {}
        for package in ("numpy", "scipy", "rdkit", "MDAnalysis", "lammps", "mpich", "radonpy-pypi", "packmol", "polyply", "pysimm", "hoomd", "colabfold", "vermouth", "duckdb", "scikit-learn", "pymbar", "pypdf", "baybe", "torch", "pybaselines", "pdbfixer", "openmm"):
            try:
                packages[package] = version(package)
            except PackageNotFoundError:
                packages[package] = None
        files = {}
        for name in ("workbench/science.py", "workbench/external.py", "workbench/bridges.py", "workbench/forcefields.py", "workbench/mixture_analysis.py", "workbench/sap.py", "workbench/protonation.py", "workbench/class2.py", "workbench/pcff.py", "workbench/crosslink.py", "workbench/engines.py", "workbench/repair.py", "workbench/descriptors.py", "workbench/campaigns.py", "scripts/engine_worker.py", "requirements.lock.txt", "requirements.desktop.lock.txt"):
            path = ROOT / name
            if path.is_file():
                files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        config = json.loads((directory / "config.json").read_text())
        handoff = None
        if config.get("source_job_id"):
            handoff = {"job_id": config["source_job_id"], "artifact": config.get("source_artifact"),
                       "sha256": hashlib.sha256(config.get("pdb_text", "").encode()).hexdigest()}
        atomic_json(directory / "runtime.json", {"captured_at": now(), "python": sys.version,
                    "platform": platform.platform(), "packages": packages, "source_sha256": files,
                    "source_handoff": handoff, "note": "Installed package versions; external environment versions are recorded by each engine when available."})

    def get(self, job_id):
        with self.lock:
            job = self._read(job_id)
        directory = self.directory(job_id)
        job["artifacts"] = [{"name": str(p.relative_to(directory)), "size": p.stat().st_size,
            "url": f"/api/jobs/{job_id}/artifacts/{p.relative_to(directory).as_posix()}"}
            for p in sorted(directory.rglob("*")) if p.is_file() and not p.is_symlink() and p.suffix != ".tmp"]
        log_path = directory / "worker.log"
        if log_path.exists():
            with log_path.open("rb") as log:
                log.seek(max(0, log_path.stat().st_size - 60000))
                job["log"] = log.read().decode("utf-8", errors="replace")
        else:
            job["log"] = ""
        job["result"] = None
        try:
            job["result"] = json.loads((directory / "result.json").read_text())
        except (FileNotFoundError, ValueError):
            pass
        events = []
        events_file = directory / "events.jsonl"
        if events_file.exists():
            for line in events_file.read_text().splitlines()[-200:]:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        job["events"] = events
        job["steps"] = self._steps(job, events)
        return job

    @staticmethod
    def _steps(job, events):
        labels = [("input", "입력"), ("build", "구조 생성 / 예측"), ("simulate", "계산"), ("analyze", "분석"), ("archive", "저장")]
        stage = (events[-1].get("stage") or events[-1].get("step")) if events else "input"
        lookup = {"validate": 0, "input": 0, "build": 1, "prepare": 1, "predict": 1, "simulate": 2, "simulation": 2, "analyze": 3, "analysis": 3, "archive": 4, "done": 4, "complete": 4}
        position = lookup.get(stage, 1)
        status = job["status"]
        return [{"name": label, "id": key, "status": ("completed" if status == "completed" else
            "pending" if status == "queued" else "completed" if index < position else
            status if index == position else "pending")} for index, (key, label) in enumerate(labels)]

    def list(self):
        with self.lock:
            jobs = []
            for meta in self.root.glob("*/job.json"):
                try:
                    jobs.append(json.loads(meta.read_text()))
                except (ValueError, OSError):
                    pass
            return sorted(jobs, key=lambda j: j["created_at"], reverse=True)

    def cancel(self, job_id):
        with self.lock:
            job = self._read(job_id)
            if job["status"] in TERMINAL:
                return self.get(job_id)
            self.cancelled.add(job_id)
            proc = self.processes.get(job_id)
            if proc is not None:
                if job_id in self.stop_events:
                    return self.get(job_id)
                stop_event = threading.Event()
                self.stop_events[job_id] = stop_event
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                threading.Thread(target=self._kill_later, args=(proc, stop_event), daemon=True).start()
                self._update(job_id, status="cancelling")
            else:
                self._manifest(self.directory(job_id))
                self._update(job_id, status="cancelled", finished_at=now())
            return self.get(job_id)

    @staticmethod
    def _kill_later(proc, stop_event):
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.05)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            stop_event.set()

    def close(self):
        with self.lock:
            self.closed = True
            for job in self.list():
                if job["status"] not in TERMINAL:
                    self.cancel(job["id"])
        self.pool.shutdown(wait=True, cancel_futures=True)
