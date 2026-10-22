"""Resource, state and subprocess locations in source and desktop releases."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from datetime import datetime, timezone


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])).resolve()


def user_data_root() -> Path:
    if os.environ.get("POLYMER_APP_DATA_DIR"):
        return Path(os.environ["POLYMER_APP_DATA_DIR"]).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "PolymerLab"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/PolymerLab"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "polymer-lab"


def default_jobs_root() -> Path:
    if os.environ.get("POLYMER_DATA_DIR"):
        return Path(os.environ["POLYMER_DATA_DIR"]).expanduser().resolve()
    return user_data_root() / "jobs" if frozen() else resource_root() / "data/jobs"


def prepare_desktop_data() -> Path:
    """Copy finished legacy jobs once, without changing or sharing their files."""
    if os.environ.get("POLYMER_DATA_DIR"):
        target = default_jobs_root()
        target.mkdir(parents=True, exist_ok=True)
        return target
    root = user_data_root()
    target = root / "jobs"
    target.mkdir(parents=True, exist_ok=True)
    marker = root / "migration.json"
    if marker.exists():
        return target
    legacy_override = os.environ.get("POLYMER_LEGACY_DATA_DIR")
    legacy = Path(legacy_override).expanduser() if legacy_override else resource_root() / "data/jobs"
    copied, skipped = [], []
    if legacy.resolve() != target.resolve() and legacy.is_dir():
        for source in sorted(legacy.iterdir()):
            if source.is_symlink() or not source.is_dir() or len(source.name) != 32 or any(c not in "0123456789abcdef" for c in source.name):
                continue
            destination = target / source.name
            if destination.exists():
                skipped.append({"id": source.name, "reason": "already_exists"})
                continue
            try:
                if (source / "job.json").is_symlink():
                    raise ValueError("symlink metadata")
                job = json.loads((source / "job.json").read_text())
                if not isinstance(job, dict):
                    raise ValueError("metadata is not an object")
            except (OSError, ValueError):
                skipped.append({"id": source.name, "reason": "unreadable_metadata"})
                continue
            if job.get("status") not in {"completed", "prepared", "failed", "blocked", "cancelled", "interrupted"}:
                skipped.append({"id": source.name, "reason": "active_job"})
                continue
            staging = root / (".migration-" + uuid.uuid4().hex)
            try:
                shutil.copytree(source, staging, ignore=lambda folder, names: [n for n in names if (Path(folder) / n).is_symlink()])
                if destination.exists():
                    skipped.append({"id": source.name, "reason": "already_exists"})
                else:
                    staging.rename(destination)
                    copied.append(source.name)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
    record = {"created_at": datetime.now(timezone.utc).isoformat(), "source": str(legacy),
              "destination": str(target), "copied": copied, "skipped": skipped, "originals_modified": False}
    tmp = marker.with_suffix("." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    tmp.replace(marker)
    return target


# LAMMPS links the MPICH wheel, whose bundled UCX probes RDMA devices even for a single process and
# aborts MPI_Init when a device cannot open its transports (Azure VMs with the MANA adapter:
# "uct_iface_open(ud_verbs/mana_0:1) failed"). Single-node runs only need shared memory and TCP.
MPI_DEFAULTS = {"UCX_TLS": "self,sm,tcp"}


def apply_mpi_defaults(env=None):
    """Set the single-node MPI transport defaults in env (default: this process) unless already configured."""
    target = os.environ if env is None else env
    for key, value in MPI_DEFAULTS.items():
        target.setdefault(key, value)
    return target


def runtime_environment() -> dict[str, str]:
    env = os.environ.copy()
    root = resource_root()
    if frozen():
        release = Path(sys.executable).resolve().parent
        env["PATH"] = os.pathsep.join([str(release / "bin"), str(release), env.get("PATH", "")])
        env["LD_LIBRARY_PATH"] = str(root) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        natives = {"LAMMPS_EXEC": root / "lammps/lmp", "PACKMOL_EXEC": root / "packmol/binaries/packmol", "POLYPLY_EXEC": release / "bin/polyply", "MARTINIZE_EXEC": release / "bin/martinize2"}
        for key, path in natives.items():
            if path.is_file():
                env.setdefault(key, str(path))
        env["POLYMER_DESKTOP_EXE"] = str(Path(sys.executable).resolve())
        env.pop("PYTHONPATH", None)
    else:
        executable_dir = Path(sys.executable).parent
        env["PATH"] = str(executable_dir) + os.pathsep + env.get("PATH", "")
        env["LD_LIBRARY_PATH"] = str(executable_dir.parent / "lib") + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        env["PYTHONPATH"] = str(root)
    env.update(PYTHONUNBUFFERED="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    return apply_mpi_defaults(env)


def worker_command(config: Path, output: Path, source_root: Path | None = None) -> list[str]:
    prefix = [sys.executable, "--worker"] if frozen() else [sys.executable, str((source_root or resource_root()) / "scripts/engine_worker.py")]
    return prefix + ["--config", str(config), "--output", str(output)]


def script_command(interpreter: str, script: str) -> list[str]:
    if frozen() and Path(interpreter).resolve() == Path(sys.executable).resolve():
        return [sys.executable, "--engine-script", script]
    return [interpreter, script]


def subprocess_environment(command: str) -> dict[str, str]:
    env = runtime_environment()
    executable = Path(shutil.which(command) or command).resolve()
    release = Path(sys.executable).resolve().parent
    if frozen() and not executable.is_relative_to(release):
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
        env.pop("PYTHONPATH", None)
    return env
