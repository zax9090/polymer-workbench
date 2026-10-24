"""Drive archives preserve results without exporting credentials or claiming unverified uploads."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from workbench import drive_backup as backup
from workbench.research_store import ResearchStore


def put(path, text="example"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def context(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    put(source / "README.md", "source overview")
    put(source / "docs/MANUAL.md", "사용 설명서")
    put(source / "workbench/server.py", "# source")
    put(source / "native/src/volume.rs", "// kernel")
    put(source / "native/Cargo.lock", "# locked")
    put(source / ".gitignore", ".venv/\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=Backup Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], cwd=source, check=True)
    put(source / "workbench/new_feature.py", "# untracked current code")
    put(source / "workbench/google_token.json", "DO NOT EXPORT TOKEN")
    put(source / "workbench/.env", "DO NOT EXPORT ENV")
    put(source / "native/target/compiled.rs", "DO NOT EXPORT BUILD")
    put(source / ".venv/untracked.py", "DO NOT EXPORT VENV")
    outside = put(tmp_path / "outside.txt", "DO NOT EXPORT LINK")
    (source / "workbench/linked.py").symlink_to(outside)
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    finished, active, failed = "a" * 32, "b" * 32, "c" * 32
    for identifier, status in ((finished, "completed"), (active, "running"), (failed, "failed")):
        put(jobs_root / identifier / "job.json", json.dumps({"id": identifier, "status": status}))
        put(jobs_root / identifier / "result.json", json.dumps({"result": status}))
    put(jobs_root / finished / ".mcp_http_token", "DO NOT EXPORT MCP TOKEN")
    put(jobs_root / finished / "nested/plot.csv", "x,y\n1,2")
    (jobs_root / finished / "escape.txt").symlink_to(outside)
    linked_dir = tmp_path / "linked-dir"
    linked_dir.mkdir()
    put(linked_dir / "anything.txt", "DO NOT EXPORT PARENT LINK")
    (jobs_root / finished / "linked").symlink_to(linked_dir, target_is_directory=True)
    (jobs_root / ("d" * 32)).symlink_to(jobs_root / finished, target_is_directory=True)
    manager = SimpleNamespace(root=jobs_root, lock=threading.RLock())
    store = ResearchStore(tmp_path / "research")
    doe = store.root / "doe" / ("e" * 32)
    put(doe / "result.json", '{"record_type":"doe_design"}')
    put(doe / "design.csv", "factor,response\n-1,4\n1,5")
    put(store.root / "doe/.staging-unfinished/input.csv", "DO NOT EXPORT STAGING")
    return SimpleNamespace(source=source, manager=manager, store=store, finished=finished, active=active, failed=failed)


def test_archive_has_current_source_manual_terminal_results_and_snapshot_without_secrets(context):
    archive, report = backup.create_archive(context.source, context.manager, context.store)
    assert report["status"] == "local_ready" and report["verified"] is False
    assert report["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        expected = {"MANUAL.md", "source/native/src/volume.rs", "source/native/Cargo.lock",
                    "source/workbench/new_feature.py", f"jobs/{context.finished}/result.json",
                    f"jobs/{context.failed}/result.json", "research/snapshot/measurements.parquet",
                    "research/snapshot/experiments_master.parquet", "research/snapshot/metadata.json",
                    "research/doe/" + "e" * 32 + "/design.csv"}
        assert expected <= names
        assert not any(".duckdb" in name or context.active in name or "linked" in name or "escape" in name for name in names)
        assert not any(b"DO NOT EXPORT" in bundle.read(name) for name in names)
        manifest = json.loads(bundle.read("BACKUP_MANIFEST.json"))
        assert manifest["counts"]["jobs"] == 2
        assert manifest["counts"]["doe_records"] == 1
        assert manifest["excluded_active_jobs"] == [context.active]
        for entry in manifest["files"]:
            payload = bundle.read(entry["path"])
            assert entry["bytes"] == len(payload)
            assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    other, _ = backup.create_archive(context.source, context.manager, context.store)
    assert other != archive and archive.exists()


def test_backup_limit_does_not_publish_partial_archive(context, monkeypatch):
    monkeypatch.setattr(backup, "MAX_BYTES", 20)
    with pytest.raises(ValueError, match="4 GiB"):
        backup.create_archive(context.source, context.manager, context.store)
    assert not list((context.store.root / "drive-backups").glob("*.zip"))
    assert not list((context.store.root / "drive-backups").glob(".staging-*"))


def test_archive_includes_stopped_sheets_with_terminal_jobs_and_excludes_active_or_linked_sheets(context):
    root = context.store.root / "sheets"

    def sheet(identifier, kind, status, items):
        folder = root / identifier
        put(folder / "record.json", json.dumps({"id": identifier, "kind": kind, "status": status, "items": items}))
        put(folder / "sheet.txt", "# Polymer Lab 실행표 v1\n종류: " + kind)
        put(folder / "results.csv", "rank,chi\n1,0.1\n")
        return folder

    complete = sheet("1" * 32, "blends", "completed", [{"status": "completed"}])
    cancelled = sheet("2" * 32, "blends", "cancelled", [{"status": "completed"}, {"status": "pending"}])
    jobs = sheet("3" * 32, "jobs", "submitted", [{"job_id": context.finished}, {"job_id": context.failed}])
    active = sheet("4" * 32, "blends", "running", [{"status": "running"}])
    stopping = sheet("5" * 32, "jobs", "cancelled", [{"job_id": context.active}])
    submitting = sheet("6" * 32, "jobs", "submitting", [{"job_id": context.finished}])
    missing = sheet("7" * 32, "jobs", "interrupted", [{"job_id": "f" * 32}])
    put(complete / "pairs/0.json", '{"chi":0.1}')
    private = put(context.source.parent / "private-sheet.txt", "DO NOT EXPORT LINK")
    (complete / "linked.txt").symlink_to(private)
    (root / ("8" * 32)).symlink_to(complete, target_is_directory=True)
    linked = root / ("9" * 32)
    linked.mkdir()
    (linked / "record.json").symlink_to(complete / "record.json")
    archive, report = backup.create_archive(context.source, context.manager, context.store)
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        for folder in (complete, cancelled, jobs):
            assert {f"research/sheets/{folder.name}/{name}" for name in ("record.json", "sheet.txt", "results.csv")} <= names
        assert f"research/sheets/{complete.name}/pairs/0.json" in names
        for folder in (active, stopping, submitting, missing, linked, root / ("8" * 32)):
            assert not any(name.startswith(f"research/sheets/{folder.name}/") for name in names)
        assert not any("linked.txt" in name for name in names)
        manifest = json.loads(bundle.read("BACKUP_MANIFEST.json"))
        assert report["counts"]["sheet_records"] == manifest["counts"]["sheet_records"] == 3
        assert report["counts"]["excluded_active_sheets"] == 4
        assert set(manifest["excluded_active_sheets"]) == {folder.name for folder in (active, stopping, submitting, missing)}


def test_sheet_resumed_during_backup_does_not_publish_a_mixed_snapshot(context, monkeypatch):
    identifier = "1" * 32
    meta = put(context.store.root / "sheets" / identifier / "record.json", json.dumps({
        "id": identifier, "kind": "blends", "status": "cancelled", "items": [{"status": "pending"}]}))
    original_export = context.store.export_snapshot

    def resume_then_export(path):
        record = json.loads(meta.read_text())
        record["status"] = "running"
        meta.write_text(json.dumps(record))
        return original_export(path)

    monkeypatch.setattr(context.store, "export_snapshot", resume_then_export)
    with pytest.raises(ValueError, match="실행표 상태가 변경"):
        backup.create_archive(context.source, context.manager, context.store)
    assert not list((context.store.root / "drive-backups").glob("*.zip"))
    assert not list((context.store.root / "drive-backups").glob(".staging-*"))


def test_drive_status_only_reads_remote_names_and_requires_explicit_drive_selection(monkeypatch):
    calls = []
    monkeypatch.setattr(backup.shutil, "which", lambda name: "/usr/bin/rclone")
    monkeypatch.setattr(backup, "_rclone", lambda args: calls.append(args) or "personal: drive\nlocal: local\n")
    monkeypatch.delenv("POLYMER_DRIVE_REMOTE", raising=False)
    assert backup.drive_status()["status"] == "setup_required"
    monkeypatch.setenv("POLYMER_DRIVE_REMOTE", "local")
    assert backup.drive_status()["configured"] is False
    monkeypatch.setenv("POLYMER_DRIVE_REMOTE", "personal:/folder")
    assert backup.drive_status()["configured"] is False
    monkeypatch.setenv("POLYMER_DRIVE_REMOTE", "personal")
    status = backup.drive_status()
    assert status["configured"] and status["remote"] == "personal"
    assert all(call == ["listremotes", "--long"] for call in calls)


def test_rclone_does_not_use_platform_credentials_or_expose_diagnostics(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/platform/credentials.json")
    monkeypatch.setenv("RCLONE_DRIVE_TOKEN", "legacy-secret")
    monkeypatch.setenv("RCLONE_CONFIG", "/safe/user/rclone.conf")
    monkeypatch.setattr(backup.shutil, "which", lambda name: "/usr/bin/rclone")
    def run(args, **kwargs):
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in kwargs["env"]
        assert "RCLONE_DRIVE_TOKEN" not in kwargs["env"]
        assert kwargs["env"]["RCLONE_CONFIG"] == "/safe/user/rclone.conf"
        return SimpleNamespace(returncode=1, stdout="", stderr="private-token diagnostic")
    monkeypatch.setattr(backup.subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        backup._rclone(["listremotes", "--long"])
    assert "private-token" not in str(error.value)


def test_upload_requires_remote_size_and_hash_match(context, monkeypatch):
    archive, report = backup.create_archive(context.source, context.manager, context.store)
    commands = []
    def transport(args, **kwargs):
        commands.append(args)
        if args[0] == "lsjson":
            return json.dumps({"IsDir": False, "Size": archive.stat().st_size,
                               "Hashes": {"md5": hashlib.md5(archive.read_bytes()).hexdigest()}})
        return ""
    monkeypatch.setattr(backup, "_rclone", transport)
    uploaded = backup.upload_archive(archive, report, "personal")
    assert uploaded["status"] == "uploaded" and uploaded["verified"]
    assert commands[0] == ["copyto", str(archive), f"personal:{backup.DESTINATION}/{archive.name}", "--immutable"]
    assert commands[1][0] == "lsjson" and "--hash" in commands[1]
    monkeypatch.setattr(backup, "_rclone", lambda args, **kw: '{}' if args[0] == "lsjson" else "")
    with pytest.raises(RuntimeError, match="체크섬"):
        backup.upload_archive(archive, report, "personal")
    assert archive.exists()


def test_http_missing_config_and_failed_verification_never_claim_success(context, monkeypatch):
    app = FastAPI()
    app.include_router(backup.router)
    app.state.manager = context.manager
    app.state.research_store = context.store
    app.state.research_root = context.store.root
    monkeypatch.setattr(backup, "resource_root", lambda: context.source)
    monkeypatch.setattr(backup, "drive_status", lambda: {"configured": False, "message": "Drive 연결 필요"})
    with TestClient(app) as client:
        response = client.post("/api/research/drive-backups")
        assert response.status_code == 409 and response.json()["detail"]["status"] == "setup_required"
        monkeypatch.setattr(backup, "drive_status", lambda: {"configured": True, "remote": "personal"})
        def fail(*args):
            raise RuntimeError("Drive 체크섬 검증 실패")
        monkeypatch.setattr(backup, "upload_archive", fail)
        response = client.post("/api/research/drive-backups")
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert detail["status"] == "upload_failed"
        download = client.get(detail["download_url"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        assert client.get("/api/research/drive-backups/invalid/download").status_code == 404


def test_structure_results_are_immutable_and_included_in_backup(context):
    result = {"analysis": "rdf", "pairs": [{"pair": "C-C", "g": [0.0, 1.0]}], "warnings": [],
              "provenance": {"source_sha256": "original-source-hash"}}
    original_job = (context.manager.root / context.finished / "result.json").read_bytes()
    record = backup.save_structure_analysis(context.store.root, context.finished, result, {"bins": 2})
    assert "record_id" not in result
    assert record["parameters"] == {"bins": 2}
    assert record["provenance"] == result["provenance"]
    saved = context.store.root / "analyses" / record["record_id"] / "result.json"
    first = saved.read_bytes()
    other = backup.save_structure_analysis(context.store.root, context.finished, result)
    assert other["record_id"] != record["record_id"] and saved.read_bytes() == first
    assert (context.manager.root / context.finished / "result.json").read_bytes() == original_job
    archive, report = backup.create_archive(context.source, context.manager, context.store)
    with zipfile.ZipFile(archive) as bundle:
        path = f"research/analyses/{record['record_id']}/result.json"
        assert json.loads(bundle.read(path)) == record
        assert report["counts"]["analysis_records"] == 2
    assert not list((context.store.root / "analyses").glob(".staging-*"))


def test_failed_structure_record_never_publishes_or_changes_previous_results(context, monkeypatch):
    good = backup.save_structure_analysis(context.store.root, context.finished, {"analysis": "msd", "value": 1})
    root = context.store.root / "analyses"
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")}
    with pytest.raises(ValueError):
        backup.save_structure_analysis(context.store.root, context.finished, {"analysis": "msd", "value": float("nan")})
    write_text = Path.write_text
    def fail_write(path, *args, **kwargs):
        if path.name == "result.json" and path.parent.name.startswith(".staging-"):
            raise OSError("disk full")
        return write_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError):
        backup.save_structure_analysis(context.store.root, context.finished, {"analysis": "rdf"})
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")} == before
    assert not list(root.glob(".staging-*"))
    assert list(root.iterdir()) == [root / good["record_id"]]


def test_structure_record_rejects_symlinked_output_and_download_path(context):
    root = context.store.root / "analyses"
    outside = context.store.root.parent / "external-records"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="심볼릭"):
        backup.save_structure_analysis(context.store.root, context.finished, {"analysis": "rdf"})
    assert not list(outside.iterdir())


def test_local_backup_and_saved_analysis_download_need_no_drive_connection(context, monkeypatch):
    app = FastAPI()
    app.include_router(backup.router)
    app.state.manager = context.manager
    app.state.research_store = context.store
    app.state.research_root = context.store.root
    monkeypatch.setattr(backup, "resource_root", lambda: context.source)
    def no_network(*args, **kwargs):
        raise AssertionError("local backup must not invoke Drive")
    monkeypatch.setattr(backup, "_rclone", no_network)
    record = backup.save_structure_analysis(context.store.root, context.finished, {"analysis": "free_volume"})
    with TestClient(app) as client:
        response = client.post("/api/research/drive-backups/local")
        assert response.status_code == 201
        assert response.json()["status"] == "local_ready" and response.json()["verified"] is False
        assert client.get(response.json()["download_url"]).status_code == 200
        saved = client.get(record["download_url"])
        assert saved.status_code == 200 and saved.json() == record
        assert client.get("/api/research/analyses/invalid/download").status_code == 404
