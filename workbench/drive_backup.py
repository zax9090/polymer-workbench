"""Immutable source/manual/research backups to a user's explicitly selected rclone Drive remote."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
import zipfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .jobs import TERMINAL, atomic_json
from .paths import resource_root

router = APIRouter(prefix="/api/research", tags=["Drive backup"])
DESTINATION = "PolymerLab-backups/results"
MAX_BYTES = 4 * 1024 ** 3
MAX_FILES = 50000
IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_BACKUP_LOCK = threading.Lock()
SETUP_INSTRUCTIONS = (
    "본인 Google 계정의 Google Drive 원격을 rclone config로 연결하세요(권장 권한: drive.file). "
    "설정 파일은 저장소 밖의 기본 rclone 위치 또는 RCLONE_CONFIG 경로에 보관하고, "
    "POLYMER_DRIVE_REMOTE에 원격 이름만 지정한 뒤 앱을 다시 실행하세요. "
    "예: POLYMER_DRIVE_REMOTE=gdrive. 토큰·비밀번호를 채팅이나 저장소에 넣지 마세요."
)
SOURCE_ROOT_FILES = {
    "AGENTS.md", "README.md", "LICENSE", "LICENSE.txt", "NOTICE", ".gitignore", "pyproject.toml",
    "requirements.lock.txt", "requirements.desktop.lock.txt", "setup.sh", "start.sh",
    "install-desktop.sh", "launch-desktop.sh",
}
SOURCE_DIRS = {"workbench", "native", "scripts", "tests", "docs", "assets", "examples", "packaging", ".github", "tools", "evidence"}
SOURCE_SUFFIXES = {
    ".py", ".rs", ".toml", ".lock", ".js", ".css", ".html", ".md", ".txt", ".json", ".jsonl",
    ".yaml", ".yml", ".sh", ".spec", ".svg", ".png", ".jpg", ".jpeg", ".csv", ".tsv",
    ".pdb", ".mol", ".sdf", ".data", ".params", ".xml", ".parquet",
}
BLOCKED_PARTS = {".git", ".venv", "node_modules", "target", "build", "dist", "releases", "__pycache__",
                 ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "secrets", "credentials"}
BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".duckdb", ".wal", ".pyc", ".pyo", ".tmp"}


def _private(relative: Path) -> bool:
    for part in relative.parts:
        lower = part.lower()
        if lower in BLOCKED_PARTS or lower.startswith((".env", ".mcp_", ".staging-")):
            return True
        if any(word in lower for word in ("credential", "secret", "token", "oauth", "id_rsa", "id_ed25519")):
            return True
        if lower in {"rclone.conf", "token.json", "tokens.json", "token", "access_token", "refresh_token"}:
            return True
    return relative.suffix.lower() in BLOCKED_SUFFIXES


def _safe_file(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
        if relative.is_absolute() or ".." in relative.parts or _private(relative):
            return False
        if any(root.joinpath(*relative.parts[:i]).is_symlink() for i in range(1, len(relative.parts) + 1)):
            return False
        return path.is_file() and path.resolve().is_relative_to(root.resolve())
    except (ValueError, OSError):
        return False


def _rclone_environment():
    env = os.environ.copy()
    # Never treat the host's service account or the legacy GitHub secret as the user's Drive login.
    for name in ("GOOGLE_APPLICATION_CREDENTIALS", "RCLONE_DRIVE_ENV_AUTH", "RCLONE_DRIVE_TOKEN"):
        env.pop(name, None)
    return env


def _rclone(arguments, timeout=30):
    executable = shutil.which("rclone")
    if not executable:
        raise RuntimeError("rclone이 설치되어 있지 않습니다.")
    try:
        result = subprocess.run([executable, *arguments, "--log-level", "ERROR", "--stats", "0"],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                timeout=timeout, env=_rclone_environment())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Google Drive 작업을 완료하지 못했습니다. 연결과 rclone 설정을 확인하세요.") from exc
    if result.returncode:
        # rclone's raw diagnostics may contain account/configuration details; never relay them.
        raise RuntimeError("Google Drive 작업에 실패했습니다. 본인 rclone 원격의 로그인·권한·네트워크 연결을 확인하세요.")
    return result.stdout


def drive_status():
    base = {"configured": False, "remote": None, "destination": DESTINATION, "setup_instructions": SETUP_INSTRUCTIONS}
    if not shutil.which("rclone"):
        return {**base, "status": "setup_required", "message": "rclone을 설치하고 본인 Google Drive를 연결하세요."}
    try:
        listing = _rclone(["listremotes", "--long"])
    except RuntimeError:
        return {**base, "status": "unavailable", "message": "rclone 원격 목록을 읽지 못했습니다. 보안 설정 파일을 확인하세요."}
    remotes = {}
    for line in listing.splitlines():
        name, separator, kind = line.partition(":")
        if separator and REMOTE_NAME.fullmatch(name.strip()):
            remotes[name.strip()] = kind.strip()
    drive_names = sorted(name for name, kind in remotes.items() if kind == "drive")
    selected = os.environ.get("POLYMER_DRIVE_REMOTE", "").strip()
    base["available_drive_remotes"] = drive_names
    if not REMOTE_NAME.fullmatch(selected) or remotes.get(selected) != "drive":
        return {**base, "status": "setup_required",
                "message": "POLYMER_DRIVE_REMOTE에 연결된 Google Drive 원격 이름을 지정하세요(콜론·폴더 경로 제외)."}
    return {**base, "status": "configured", "configured": True, "remote": selected,
            "message": "Drive 설정됨 · 실제 연결과 파일 검증은 업로드할 때 확인합니다."}


def _source_files(root: Path):
    try:
        names = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                        cwd=root, stderr=subprocess.DEVNULL).decode("utf-8").split("\0")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        raise ValueError("소스 백업에는 Git 저장소의 소스 실행 환경이 필요합니다.") from exc
    files = []
    for name in sorted(set(names)):
        relative = PurePosixPath(name)
        if not name or relative.is_absolute() or ".." in relative.parts:
            continue
        if name.startswith(("tools/rea/npm/", "tools/engines/", "evidence/sources/", "evidence/extensions/sources/", "evidence/artifacts/downloads/")):
            continue
        allowed = name in SOURCE_ROOT_FILES or (relative.parts[0] in SOURCE_DIRS and relative.suffix.lower() in SOURCE_SUFFIXES)
        if allowed and _safe_file(root, root / name):
            files.append((root / name, "source/" + name))
    included = {name for _, name in files}
    if not {"source/README.md", "source/workbench/server.py", "source/docs/MANUAL.md"} <= included:
        raise ValueError("소스 또는 사용자 설명서(docs/MANUAL.md)가 없습니다. 백업 전에 파일을 준비하세요.")
    return files, head


def _walk_files(root: Path):
    """Never recurse into links or private/configuration directories."""
    if root.is_symlink() or not root.is_dir():
        return
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        here = Path(directory)
        subdirectories[:] = sorted(name for name in subdirectories
                                   if not (here / name).is_symlink() and not _private((here / name).relative_to(root)))
        for name in sorted(filenames):
            path = here / name
            if _safe_file(root, path):
                yield path


def _hash_file(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def save_structure_analysis(research_root, job_id, result, parameters=None):
    """Atomically save a successful result beside research data, never inside the source job."""
    if not isinstance(job_id, str) or not IDENTIFIER.fullmatch(job_id):
        raise ValueError("구조 분석의 원본 작업 ID가 올바르지 않습니다.")
    if not isinstance(result, dict) or result.get("analysis") not in {"rdf", "msd", "free_volume", "widom"}:
        raise ValueError("저장할 구조 분석 결과가 올바르지 않습니다.")
    identifier = uuid.uuid4().hex
    record = {**result, "record_type": "structure_analysis", "record_id": identifier, "job_id": job_id,
              "created_at": datetime.now(timezone.utc).isoformat(), "parameters": parameters or {},
              "download_url": f"/api/research/analyses/{identifier}/download"}
    # Validate and detach the record before creating any persistent state.
    encoded = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
    record = json.loads(encoded)
    root = Path(research_root) / "analyses"
    if root.is_symlink():
        raise ValueError("구조 분석 저장 폴더에 심볼릭 링크를 사용할 수 없습니다.")
    root.mkdir(parents=True, exist_ok=True)
    final = root / identifier
    if final.exists() or final.is_symlink():
        raise ValueError("동일한 구조 분석 기록이 이미 있습니다. 다시 시도하세요.")
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=root) as staging:
        staged = Path(staging)
        (staged / "result.json").write_text(encoded, encoding="utf-8")
        staged.rename(final)
    return record


def create_archive(source_root, manager, store):
    """Prepare an immutable local ZIP; this function never uploads or changes source/research inputs."""
    source_root, research_root = Path(source_root).resolve(), store.root
    files, git_head = _source_files(source_root)
    files.append((source_root / "docs/MANUAL.md", "MANUAL.md"))
    identifier = uuid.uuid4().hex
    created = datetime.now(timezone.utc)
    filename = f"PolymerLab-results-{created.strftime('%Y%m%dT%H%M%SZ')}-{identifier}.zip"
    destination = research_root / "drive-backups"
    if destination.is_symlink():
        raise ValueError("백업 폴더에 심볼릭 링크를 사용할 수 없습니다.")
    destination.mkdir(parents=True, exist_ok=True)
    jobs, excluded_jobs = [], []
    with manager.lock:
        for directory in sorted(manager.root.iterdir()):
            if not IDENTIFIER.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            meta = directory / "job.json"
            if not _safe_file(manager.root, meta):
                continue
            try:
                record = json.loads(meta.read_text())
            except (OSError, ValueError):
                continue
            if record.get("id") != directory.name:
                continue
            if record.get("status") not in TERMINAL:
                excluded_jobs.append(directory.name)
                continue
            jobs.append({"id": directory.name, "status": record["status"]})
            files.extend((p, "jobs/" + p.relative_to(manager.root).as_posix()) for p in _walk_files(directory))
    doe_records, analysis_records = 0, 0
    for category in ("doe", "analyses", "statistics", "imports", "workflows", "density-studies"):
        root = research_root / category
        if not root.is_dir() or root.is_symlink():
            continue
        for folder in sorted(root.iterdir()):
            if not IDENTIFIER.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                continue
            if category in {"doe", "analyses", "statistics", "workflows", "density-studies"} and not _safe_file(root, folder / "result.json"):
                continue
            if category == "doe":
                doe_records += 1
            if category == "analyses":
                analysis_records += 1
            files.extend((p, "research/" + p.relative_to(research_root).as_posix()) for p in _walk_files(folder))
    for category in ("models", "campaigns"):
        root = research_root / category
        if root.is_dir() and not root.is_symlink():
            files.extend((p, "research/" + p.relative_to(research_root).as_posix())
                         for p in sorted(root.glob("*.json")) if IDENTIFIER.fullmatch(p.stem) and _safe_file(root, p))
    sheet_records, excluded_sheets, sheet_versions = 0, [], {}
    root = research_root / "sheets"
    terminal_jobs = {job["id"] for job in jobs}
    if root.is_dir() and not root.is_symlink():
        for folder in sorted(root.iterdir()):
            meta = folder / "record.json"
            if not IDENTIFIER.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir() or not _safe_file(root, meta):
                continue
            try:
                raw = meta.read_bytes()
                record = json.loads(raw)
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("id") != folder.name or record.get("kind") not in {"blends", "jobs"}:
                continue
            items = record.get("items")
            if not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items):
                continue
            stopped = record.get("status") in {"completed", "failed", "cancelled", "interrupted"}
            if record["kind"] == "jobs":
                # A cancelled sheet may still be stopping a child, or publishing a reserved job ID.
                stopped = (stopped or record.get("status") == "submitted") and all(
                    isinstance(item.get("job_id"), str) and item["job_id"] in terminal_jobs for item in items)
            else:
                stopped = stopped and all(item.get("status") in {"completed", "failed", "pending"} for item in items)
            if not stopped:
                excluded_sheets.append(folder.name)
                continue
            sheet_records += 1
            sheet_versions[meta] = hashlib.sha256(raw).hexdigest()
            files.extend((p, "research/" + p.relative_to(research_root).as_posix()) for p in _walk_files(folder))
    manifest = {"schema_version": 1, "id": identifier, "created_at": created.isoformat(), "git_head": git_head,
                "source_scope": "Current working-tree bytes of allowlisted tracked and nonignored source files; no Git history or installed dependencies",
                "jobs": jobs, "excluded_active_jobs": excluded_jobs, "excluded_active_sheets": excluded_sheets, "files": [],
                "consistency": "Research tables use one transaction; other files are individually checked for changes while copying.",
                "exclusions": ["active jobs", "active sheets", "symlinks", "credentials and environment files", "live DuckDB/WAL", "caches", "prior backups"]}
    total_bytes = 0
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=destination) as temp:
        temp = Path(temp)
        snapshot = store.export_snapshot(temp / "snapshot")
        files.extend((p, "research/snapshot/" + p.name) for p in sorted((temp / "snapshot").iterdir()) if _safe_file(temp, p))
        if len(files) > MAX_FILES:
            raise ValueError(f"백업 파일이 {MAX_FILES:,}개를 넘습니다. 결과를 나누어 보관하세요.")
        staged = temp / filename
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as bundle:
            for path, name in files:
                before = path.stat()
                if not stat.S_ISREG(before.st_mode) or path.is_symlink():
                    raise ValueError("백업 중 파일 형식이 변경됐습니다. 다시 시도하세요.")
                total_bytes += before.st_size
                if total_bytes > MAX_BYTES:
                    raise ValueError("백업 원본이 4 GiB를 넘습니다. 결과를 나누어 보관하세요.")
                digest = hashlib.sha256()
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                copied = 0
                with os.fdopen(descriptor, "rb") as source, bundle.open(name, "w", force_zip64=True) as target:
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > before.st_size:
                            raise ValueError("백업 중 원본 파일이 변경됐습니다. 다시 시도하세요.")
                        digest.update(chunk)
                        target.write(chunk)
                    after = os.fstat(source.fileno())
                if copied != before.st_size or (before.st_mtime_ns, before.st_ino) != (after.st_mtime_ns, after.st_ino):
                    raise ValueError("백업 중 원본 파일이 변경됐습니다. 다시 시도하세요.")
                manifest["files"].append({"path": name, "bytes": copied, "sha256": digest.hexdigest()})
            for path, expected in sheet_versions.items():
                if not _safe_file(research_root / "sheets", path) or _hash_file(path) != expected:
                    raise ValueError("백업 중 실행표 상태가 변경됐습니다. 계산을 멈춘 뒤 다시 시도하세요.")
            manifest["counts"] = {"files": len(files), "jobs": len(jobs), "doe_records": doe_records, "analysis_records": analysis_records,
                                  "sheet_records": sheet_records, "excluded_active_jobs": len(excluded_jobs),
                                  "excluded_active_sheets": len(excluded_sheets), **snapshot["counts"]}
            bundle.writestr("BACKUP_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False))
        with zipfile.ZipFile(staged) as bundle:
            if bundle.testzip() is not None:
                raise ValueError("백업 ZIP 검증에 실패했습니다.")
        final = destination / filename
        # UUID names plus exclusive creation keep prior local backups intact.
        os.link(staged, final)
    report = {"id": identifier, "status": "local_ready", "verified": False, "filename": filename,
              "bytes": final.stat().st_size, "sha256": _hash_file(final), "counts": manifest["counts"],
              "download_url": f"/api/research/drive-backups/{identifier}/download"}
    atomic_json(destination / (identifier + ".json"), report)
    return final, report


def upload_archive(path: Path, report: dict, remote: str):
    if not REMOTE_NAME.fullmatch(remote):
        raise ValueError("Google Drive 원격 이름이 올바르지 않습니다.")
    target = f"{remote}:{DESTINATION}/{path.name}"
    _rclone(["copyto", str(path), target, "--immutable"], timeout=600)
    try:
        entry = json.loads(_rclone(["lsjson", target, "--stat", "--hash"]))
        remote_md5 = entry.get("Hashes", {}).get("md5", "").lower()
        if entry.get("IsDir") or entry.get("Size") != path.stat().st_size or remote_md5 != _hash_file(path, "md5"):
            raise ValueError("remote file does not match")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("Drive 업로드 후 파일 크기·체크섬을 확인하지 못했습니다. 로컬 ZIP은 보존됐으며 업로드 성공으로 표시하지 않습니다.") from exc
    result = {**report, "status": "uploaded", "verified": True, "destination": target,
              "verification": {"algorithm": "md5", "remote_hash": remote_md5, "bytes": entry["Size"]}}
    atomic_json(path.parent / (report["id"] + ".json"), result)
    return result


@router.get("/drive-status")
def status_endpoint():
    return drive_status()


@router.post("/drive-backups/local", status_code=201)
def local_backup_endpoint(request: Request):
    """An explicit local-only action; no Drive configuration or network operation is needed."""
    if not _BACKUP_LOCK.acquire(blocking=False):
        raise HTTPException(409, "다른 백업이 진행 중입니다. 완료된 뒤 다시 시도하세요.")
    try:
        _, report = create_archive(resource_root(), request.app.state.manager, request.app.state.research_store)
        return report
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, "백업 파일을 읽거나 저장하지 못했습니다. 저장 공간과 권한을 확인하세요.") from exc
    finally:
        _BACKUP_LOCK.release()


@router.post("/drive-backups", status_code=201)
def backup_endpoint(request: Request):
    status = drive_status()
    if not status["configured"]:
        raise HTTPException(409, {"status": "setup_required", "message": status["message"], "setup_instructions": SETUP_INSTRUCTIONS})
    if not _BACKUP_LOCK.acquire(blocking=False):
        raise HTTPException(409, "다른 Drive 백업이 진행 중입니다. 완료된 뒤 다시 시도하세요.")
    report = None
    try:
        path, report = create_archive(resource_root(), request.app.state.manager, request.app.state.research_store)
        return upload_archive(path, report, status["remote"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else "백업 파일을 읽거나 저장하지 못했습니다. 저장 공간과 권한을 확인하세요."
        raise HTTPException(502, {"status": "upload_failed", "message": message,
                                  "download_url": report["download_url"] if report else None}) from exc
    finally:
        _BACKUP_LOCK.release()


@router.get("/drive-backups/{identifier}/download")
def download_endpoint(identifier: str, request: Request):
    root = request.app.state.research_root / "drive-backups"
    record = root / (identifier + ".json")
    if not IDENTIFIER.fullmatch(identifier) or root.is_symlink() or not _safe_file(root, record):
        raise HTTPException(404, "백업을 찾을 수 없습니다.")
    try:
        info = json.loads(record.read_text())
        filename = info["filename"]
        if Path(filename).name != filename:
            raise ValueError("invalid filename")
        path = root / filename
        if not _safe_file(root, path):
            raise ValueError("missing backup")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(404, "백업을 찾을 수 없습니다.") from exc
    return FileResponse(path, media_type="application/zip", filename=filename)


@router.get("/analyses/{identifier}/download")
def download_analysis_endpoint(identifier: str, request: Request):
    root = request.app.state.research_root / "analyses"
    path = root / identifier / "result.json"
    if not IDENTIFIER.fullmatch(identifier) or root.is_symlink() or not _safe_file(root, path):
        raise HTTPException(404, "구조 분석 기록을 찾을 수 없습니다.")
    return FileResponse(path, media_type="application/json", filename=f"PolymerLab-analysis-{identifier}.json")
