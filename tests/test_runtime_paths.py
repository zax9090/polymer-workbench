"""Desktop portability: read-only resources, preserved jobs and native workers."""
import json
import os
from pathlib import Path
import sys

import pytest

from workbench import paths


@pytest.fixture
def frozen_release(tmp_path, monkeypatch):
    release = tmp_path / "Polymer Lab release"
    resources = release / "_internal"
    resources.mkdir(parents=True)
    executable = release / "PolymerLab"
    executable.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(resources), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    for variable in ("POLYMER_DATA_DIR", "POLYMER_APP_DATA_DIR", "POLYMER_LEGACY_DATA_DIR",
                     "LAMMPS_EXEC", "PACKMOL_EXEC", "POLYPLY_EXEC"):
        monkeypatch.delenv(variable, raising=False)
    return release, resources, executable


def test_frozen_state_is_separate_from_installation(frozen_release, tmp_path, monkeypatch):
    _, resources, executable = frozen_release
    state = tmp_path / "user research data"
    monkeypatch.setenv("POLYMER_APP_DATA_DIR", str(state))
    assert paths.resource_root() == resources
    assert paths.default_jobs_root() == state / "jobs"
    config = state / "jobs/run/config.json"
    output = config.parent
    assert paths.worker_command(config, output) == [
        str(executable), "--worker", "--config", str(config), "--output", str(output)]
    assert paths.script_command(str(executable), "run_radonpy.py") == [
        str(executable), "--engine-script", "run_radonpy.py"]
    assert paths.script_command("/separate/env/python", "run_radonpy.py") == [
        "/separate/env/python", "run_radonpy.py"]


def _legacy_job(root, suffix, status):
    directory = root / (suffix * 32)
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(json.dumps({"id": directory.name, "status": status}))
    (directory / "structure.pdb").write_text("original research output\n")
    return directory


def test_migration_preserves_originals_and_skips_active_links_and_duplicates(
        frozen_release, tmp_path, monkeypatch):
    _, resources, _ = frozen_release
    legacy = resources / "data/jobs"
    state = tmp_path / "user state"
    monkeypatch.setenv("POLYMER_APP_DATA_DIR", str(state))
    completed = _legacy_job(legacy, "a", "completed")
    active = _legacy_job(legacy, "b", "running")
    duplicate = _legacy_job(legacy, "c", "prepared")
    existing = _legacy_job(state / "jobs", "c", "completed")
    (existing / "structure.pdb").write_text("newer desktop result\n")
    outside = tmp_path / "unrelated-secret.txt"
    outside.write_text("unrelated data")
    (completed / "linked.txt").symlink_to(outside)
    (completed / "linked-directory").symlink_to(tmp_path, target_is_directory=True)
    (legacy / ("d" * 32)).symlink_to(completed, target_is_directory=True)
    before = {p: p.read_bytes() for p in legacy.rglob("*") if p.is_file() and not p.is_symlink()}

    target = paths.prepare_desktop_data()
    assert target == state / "jobs"
    assert (target / completed.name / "structure.pdb").read_text() == "original research output\n"
    assert not (target / active.name).exists()
    assert not (target / ("d" * 32)).exists()
    assert not (target / completed.name / "linked.txt").exists()
    assert not (target / completed.name / "linked-directory").exists()
    assert (target / duplicate.name / "structure.pdb").read_text() == "newer desktop result\n"
    assert before == {p: p.read_bytes() for p in before}
    report = json.loads((state / "migration.json").read_text())
    assert report["copied"] == [completed.name]
    assert {row["reason"] for row in report["skipped"]} == {"active_job", "already_exists"}
    assert report["originals_modified"] is False
    (target / completed.name / "structure.pdb").write_text("edited desktop result\n")
    paths.prepare_desktop_data()
    assert (target / completed.name / "structure.pdb").read_text() == "edited desktop result\n"
    assert (completed / "structure.pdb").read_text() == "original research output\n"
    assert not list(state.glob(".migration-*"))


def test_explicit_jobs_location_is_not_overwritten_by_migration(
        frozen_release, tmp_path, monkeypatch):
    target = tmp_path / "chosen jobs"
    state = tmp_path / "state"
    monkeypatch.setenv("POLYMER_DATA_DIR", str(target))
    monkeypatch.setenv("POLYMER_APP_DATA_DIR", str(state))
    assert paths.prepare_desktop_data() == target
    assert target.is_dir()
    assert not (state / "migration.json").exists()


def test_migration_skips_invalid_and_linked_metadata(frozen_release, tmp_path, monkeypatch):
    _, resources, _ = frozen_release
    legacy = resources / "data/jobs"
    state = tmp_path / "state"
    monkeypatch.setenv("POLYMER_APP_DATA_DIR", str(state))
    invalid = _legacy_job(legacy, "a", "completed")
    (invalid / "job.json").write_text("[]")
    linked = _legacy_job(legacy, "b", "completed")
    (linked / "job.json").unlink()
    outside = tmp_path / "metadata.json"
    outside.write_text('{"status":"completed"}')
    (linked / "job.json").symlink_to(outside)

    target = paths.prepare_desktop_data()
    assert list(target.iterdir()) == []
    report = json.loads((state / "migration.json").read_text())
    assert report["copied"] == []
    assert report["skipped"] == [
        {"id": invalid.name, "reason": "unreadable_metadata"},
        {"id": linked.name, "reason": "unreadable_metadata"},
    ]
    assert outside.read_text() == '{"status":"completed"}'


@pytest.mark.parametrize("original", [None, "/external/science/lib"])
def test_native_library_environment_is_restored_for_external_engines(
        frozen_release, tmp_path, monkeypatch, original):
    release, resources, executable = frozen_release
    native = resources / "lammps/lmp"
    native.parent.mkdir()
    native.touch()
    native.chmod(0o755)
    external = tmp_path / "external python"
    external.touch()
    external.chmod(0o755)
    monkeypatch.setenv("LD_LIBRARY_PATH", str(resources))
    monkeypatch.setenv("PYTHONPATH", "/source/tree")
    if original is None:
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", original)

    internal_env = paths.subprocess_environment(str(native))
    assert str(resources) in internal_env["LD_LIBRARY_PATH"].split(os.pathsep)
    assert internal_env["LAMMPS_EXEC"] == str(native)
    assert internal_env["POLYMER_DESKTOP_EXE"] == str(executable)
    assert str(release / "bin") in internal_env["PATH"].split(os.pathsep)
    external_env = paths.subprocess_environment(str(external))
    assert external_env.get("LD_LIBRARY_PATH") == original
    assert "LD_LIBRARY_PATH_ORIG" not in external_env
    assert "PYTHONPATH" not in external_env
    assert external_env["OMP_NUM_THREADS"] == "1"


def test_user_engine_override_survives_bundled_runtime(frozen_release, monkeypatch):
    _, resources, _ = frozen_release
    native = resources / "lammps/lmp"
    native.parent.mkdir()
    native.touch()
    monkeypatch.setenv("LAMMPS_EXEC", "/custom/lammps/lmp")
    assert paths.runtime_environment()["LAMMPS_EXEC"] == "/custom/lammps/lmp"


def test_engine_environment_keeps_mpi_on_shared_memory_unless_configured(monkeypatch):
    # The MPICH wheel's UCX aborted MPI_Init on an Azure MANA adapter (ud_verbs) for a one-process LAMMPS run.
    monkeypatch.delenv("UCX_TLS", raising=False)
    assert paths.runtime_environment()["UCX_TLS"] == "self,sm,tcp"
    assert paths.subprocess_environment("lmp")["UCX_TLS"] == "self,sm,tcp"
    monkeypatch.setenv("UCX_TLS", "rc,sm")
    assert paths.runtime_environment()["UCX_TLS"] == "rc,sm"
    target = {}
    assert paths.apply_mpi_defaults(target) is target and target == {"UCX_TLS": "self,sm,tcp"}
