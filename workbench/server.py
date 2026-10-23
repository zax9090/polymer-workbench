"""Loopback-only application API for the local research workbench."""
from contextlib import asynccontextmanager
import hashlib
import json
import os
import re
import secrets
import sys
import threading
from pathlib import Path
from typing import Literal
import zipfile
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .jobs import JobManager, ROOT
from .models import JobConfig
from .paths import default_jobs_root
from . import __version__


class AnalysisParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class TrajectoryParameters(AnalysisParameters):
    every: int = Field(default=1, ge=1, le=10000)
    max_frames: int = Field(default=2000, ge=1, le=2000)


class RdfParameters(TrajectoryParameters):
    pairs: list[str] = Field(default_factory=lambda: ["C-C"], min_length=1, max_length=8)
    r_max: float = Field(default=10.0, gt=0)
    bins: int = Field(default=200, ge=1, le=2000)
    exclude_same_molecule: bool = False


class MsdParameters(TrajectoryParameters):
    selection: str = Field(default="heavy", min_length=1, max_length=100)
    remove_drift: bool = True
    time_step_fs: float | None = Field(default=None, gt=0, le=1e6)
    fit_window: list[float] = Field(default_factory=lambda: [0.1, 0.5], min_length=2, max_length=2)


class FreeVolumeParameters(AnalysisParameters):
    probes: list[float] = Field(default_factory=lambda: [0.0, 1.0, 1.5], min_length=1, max_length=8)
    spacing: float = Field(default=0.25, ge=0.05, le=1.0)
    frame: Literal["last"] | int = "last"


class WidomParameters(TrajectoryParameters):
    max_frames: int = Field(default=200, ge=1, le=2000)
    gas: Literal["He", "H2", "N2", "O2", "Ar", "CH4", "CO2"] = "CO2"
    temperature: float | None = Field(default=None, gt=0, le=10000)
    insertions: int = Field(default=20000, ge=1, le=200000)
    cutoff: float | None = Field(default=None, gt=0)
    seed: int = Field(default=1, ge=0, le=2**63 - 1)


ANALYSIS_PARAMETERS = {"rdf": RdfParameters, "msd": MsdParameters,
                       "free_volume": FreeVolumeParameters, "widom": WidomParameters}


class StructureAnalysisRequest(AnalysisParameters):
    analysis: Literal["rdf", "msd", "free_volume", "widom"]
    parameters: dict = Field(default_factory=dict)


def acquire_data_lock(root: Path):
    """One server per data folder: two job managers would each requeue or interrupt the other's runs."""
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".server.lock").open("a+b")
    try:
        if os.name == "posix":
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        handle.close()
        raise RuntimeError(f"다른 Polymer Lab 서버가 이미 이 데이터 폴더를 사용 중입니다: {root}") from exc
    return handle


def client_label(request: Request):
    """Optional self-reported client name, e.g. an MCP agent; recorded for provenance only."""
    value = request.headers.get("x-polymer-client", "")
    return value if re.fullmatch(r"[A-Za-z0-9:._ /-]{1,80}", value) else None


def create_app(data_root=None):
    @asynccontextmanager
    async def lifespan(app):
        root = Path(data_root or default_jobs_root())
        lock = acquire_data_lock(root)
        try:
            from .execution import ExecutionManager
            app.state.execution = ExecutionManager()
            app.state.manager = JobManager(root, execution=app.state.execution)
            from .research_api import initialize_research
            initialize_research(app, app.state.manager)
            from .workflows import WorkflowManager
            app.state.workflows = WorkflowManager(app.state.research_root, app.state.manager)
            yield
            app.state.workflows.close()
            app.state.execution.close()
            app.state.campaigns.close()
            app.state.manager.close()
        finally:
            lock.close()

    app = FastAPI(title="Polymer Lab", version=__version__, lifespan=lifespan)
    analysis_lock = threading.BoundedSemaphore(1)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        desktop_token = os.environ.get("POLYMER_DESKTOP_TOKEN")
        if desktop_token and not secrets.compare_digest(request.headers.get("x-polymer-token", ""), desktop_token):
            return JSONResponse({"detail": "데스크톱 앱의 연결이 필요합니다."}, status_code=401)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "다른 웹사이트에서 로컬 연구 작업을 실행할 수 없습니다."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "작업 또는 파일을 찾을 수 없습니다."}, status_code=404)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "name": "Polymer Lab", "version": __version__, "local_only": True}

    @app.get("/api/execution")
    def execution_status(request: Request):
        status = request.app.state.execution.status()
        admitted = {task["owner"] for task in status["tasks"] if task["status"] in {"queued", "running"}}
        status["pending_jobs"] = sum(job["status"] == "queued" and "job:" + job["id"] not in admitted
                                     for job in request.app.state.manager.list())
        return status

    @app.post("/api/execution/{identifier}/cancel")
    def cancel_execution(identifier: str, request: Request):
        # Simulation cancellation needs the job manager's process-group cleanup.
        for task in request.app.state.execution.status()["tasks"]:
            if task["id"] == identifier and task["kind"] == "simulation":
                return request.app.state.manager.cancel(task["owner"].split(":", 1)[1])
        if not request.app.state.execution.cancel(identifier=identifier):
            raise HTTPException(404, "실행 중인 계산을 찾을 수 없습니다.")
        return {"cancel_requested": True}

    @app.exception_handler(RuntimeError)
    async def execution_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    from .execution import ExecutionBusy

    @app.exception_handler(ExecutionBusy)
    async def execution_busy(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.get("/api/capabilities")
    def capabilities():
        from .external import get_capabilities
        from importlib.metadata import version, PackageNotFoundError
        raw = get_capabilities()
        engines = []
        packages = {"numpy": "numpy", "rdkit": "rdkit", "MDAnalysis": "MDAnalysis", "lammps": "lammps", "radonpy": "radonpy-pypi", "pysimm": "pysimm", "hoomd": "hoomd", "packmol": "packmol", "polyply": "polyply", "colabfold_batch": "colabfold"}
        for name, found in {**raw["modules"], **{n: bool(p) for n,p in raw["executables"].items()}}.items():
            if name == "lmp":
                continue
            try:
                installed_version = version(packages.get(name, name))
            except PackageNotFoundError:
                installed_version = None
            engines.append({"id": name, "name": name, "available": bool(found), "version": installed_version,
                            "detail": "설치 감지됨 · 실제 실행 결과는 작업 기록에서 확인" if found else "현재 환경에 없음 · 별도 환경 연결 가능"})
        from .engines import ENGINES, status as engine_status
        for engine_id in ENGINES:
            pinned = engine_status(engine_id)
            engines.append({"id": engine_id, "name": pinned["name"], "available": pinned["available"], "version": pinned["version"],
                            "detail": f"고정 릴리스 확인됨 ({pinned['source']}) · {pinned['license']}" if pinned["available"]
                            else f"첫 사용 시 고정 릴리스를 내려받아 SHA-256 확인 · {pinned['license']}"})
        engines += [{"id": "3dmol", "name": "3Dmol.js", "available": True, "version": "local vendor", "detail": "로컬 분자 시각화 라이브러리"},
                    {"id": "hiplot", "name": "HiPlot", "available": True, "version": "0.1.33 bundle", "detail": "로컬 다변수 비교 화면 (브라우저 번들만 사용)"},
                    {"id": "alphafold_db", "name": "AlphaFold DB", "available": True, "version": None, "detail": "공개 예측 구조 조회 · 인터넷 연결 필요"}]
        rea_path = ROOT / "evidence" / "repositories.json"
        rea = json.loads(rea_path.read_text()).get("rea", {}) if rea_path.exists() else {}
        engines.append({"id": "rea", "name": "REA", "available": (ROOT / "tools" / "rea" / "node_modules").exists(), "version": rea.get("version"), "detail": "개발 분석 도구 · 데스크톱 배포본에는 분석 기록을 포함하며 REA 재실행 환경은 별도"})
        route_info = {
            "polymer_cg": ("조대화 고분자", "LAMMPS/NumPy → MDAnalysis → 궤적·관측값", ["numpy"]),
            "polymer_atomistic": ("전원자 고분자", "RDKit 구조 생성, RadonPy/LAMMPS MD, GAFF2·PCFF 벌크 셀, 에폭시–아민 가교", ["rdkit"]),
            "protein": ("AlphaFold / ColabFold", "기존 예측 가져오기·PDB 분석·새 예측 입력 내보내기. 새 추론은 별도 ColabFold 필요", []),
            "packmol": ("분자 혼합계", "RDKit 고분자와 이전 구조 배치. 토폴로지·힘장 설정은 후속 작업", ["rdkit", "packmol"]),
            "polyply": ("polyply → GROMACS", "토폴로지 생성. 좌표·MD는 전체 힘장 파일 및 GROMACS 필요", ["polyply"]),
            "hoomd": ("HOOMD-blue", "별도 HOOMD 환경에서 조대화 계산", ["hoomd"]),
            "pysimm": ("pysimm → LAMMPS", "별도 pysimm 환경에서 모델링·계산", ["pysimm", "lammps"]),
            "mixture": ("단백질–고분자 브릿지", "Martini 3 또는 AMBER 준비 계획·단계별 평형화 입력. 검증된 전체 힘장은 별도 필요", []),
        }
        found = {e["id"]: e["available"] for e in engines}
        import shutil
        for engine_id, variable in (("hoomd", "HOOMD_PYTHON"), ("pysimm", "PYSIMM_PYTHON"), ("radonpy", "RADONPY_PYTHON")):
            configured = os.environ.get(variable)
            if configured and shutil.which(configured):
                found[engine_id] = True
                for entry in engines:
                    if entry["id"] == engine_id:
                        entry.update(available=True, detail=f"별도 환경 {variable} 연결 설정됨 · 실행 시 모듈 확인", version=None)
        routes = [{"id": rid, "name": name, "description": desc, "available": all(found.get(d) for d in required),
                   "missing": [d for d in required if not found.get(d)]}
                  for rid, (name, desc, required) in route_info.items()]
        return {"engines": engines, "routes": routes, "note": raw["note"]}

    @app.get("/api/jobs")
    def jobs(request: Request):
        return request.app.state.manager.list()

    @app.post("/api/jobs", status_code=201)
    def new_job(config: JobConfig, request: Request):
        manager = request.app.state.manager
        if config.route == "polymer_atomistic" and config.atomistic_mode == "tg_scan":
            from .glass_transition import required_files
            source = manager.get(config.source_job_id)
            try:
                files = required_files(source["config"])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            directory = manager.directory(config.source_job_id)
            if source["status"] != "completed" or any((directory / n).is_symlink() or not (directory / n).is_file() for n in files):
                raise HTTPException(422, "완료된 벌크 셀 또는 가교 작업의 최종 구조 파일(" + ", ".join(files) + ")이 필요합니다.")
            config.source_artifact = files[0]
        elif config.source_job_id:
            source = manager.get(config.source_job_id)
            if config.route not in {"packmol", "mixture"} or source["status"] not in {"completed", "prepared"}:
                raise HTTPException(422, "완료된 원자 구조 작업을 PACKMOL 또는 혼합계 브릿지로 이어갈 수 있습니다.")
            if source["config"]["route"] in {"polymer_cg", "hoomd"}:
                raise HTTPException(422, "조대화 비드 좌표는 원자 구조와 단위·모델이 달라 직접 연결할 수 없습니다.")
            directory = manager.directory(config.source_job_id)
            path = directory / config.source_artifact
            if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".pdb" or path.stat().st_size > 5000000:
                raise HTTPException(422, "5 MB 이하의 PDB 구조 파일이 필요합니다.")
            config.pdb_text = path.read_text()
        from .preflight import check
        # Chemistry that would only fail inside the worker is rejected before a job exists.
        report = check(config.model_dump())
        if report["errors"]:
            message = " ".join(error["message"] for error in report["errors"])
            candidates = ((report.get("estimate") or {}).get("repeat_unit") or {}).get("candidates")
            if candidates:
                message += " 반복단위 후보(화학 구조를 확인한 뒤 사용): " + ", ".join(candidates)
            raise HTTPException(422, message)
        return manager.create(config.model_dump(), submitted_via=client_label(request))

    @app.post("/api/jobs/preflight")
    def preflight_job(config: dict):
        """Dry run: validation, chemistry, size estimates and engines; nothing is created."""
        from .preflight import check
        return check(config)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, request: Request):
        return request.app.state.manager.get(job_id)

    @app.post("/api/jobs/{job_id}/analysis")
    def analyze_structure(job_id: str, payload: StructureAnalysisRequest, request: Request):
        from .analysis_service import analyze_structure as run_analysis
        if not analysis_lock.acquire(blocking=False):
            raise HTTPException(429, "다른 구조 분석이 진행 중입니다. 완료 후 다시 실행하세요.")
        try:
            return run_analysis(request.app.state.manager, request.app.state.research_root,
                                job_id, payload.analysis, payload.parameters)
        finally:
            analysis_lock.release()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        return request.app.state.manager.cancel(job_id)

    @app.post("/api/jobs/{job_id}/rerun", status_code=201)
    def rerun(job_id: str, request: Request):
        config = request.app.state.manager.get(job_id)["config"]
        return request.app.state.manager.create(JobConfig.model_validate(config).model_dump(), submitted_via=client_label(request))

    @app.get("/api/jobs/{job_id}/artifacts/{name:path}")
    def artifact(job_id: str, name: str, request: Request):
        directory = request.app.state.manager.directory(job_id)
        path = directory / name
        if not path.resolve().is_relative_to(directory) or path.is_symlink() or not path.is_file():
            raise HTTPException(404, "파일을 찾을 수 없습니다.")
        # Only data is served. User-supplied text is never executed as web content.
        media = "application/json" if path.suffix == ".json" else "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media)

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str, request: Request):
        directory = request.app.state.manager.directory(job_id)
        if request.app.state.manager.get(job_id)["status"] in {"queued", "running", "cancelling"}:
            raise HTTPException(409, "작업을 완료하거나 중단한 뒤 결과 묶음을 다운로드하세요.")
        # Spool to disk for large scientific artifacts, without keeping all trajectories in RAM.
        import tempfile
        archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(directory.rglob("*")):
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(directory) and path.suffix != ".tmp":
                    bundle.write(path, path.relative_to(directory))
        archive.seek(0)
        def chunks():
            try:
                while chunk := archive.read(1024 * 1024):
                    yield chunk
            finally:
                archive.close()
        return StreamingResponse(chunks(), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="polymer-lab-{job_id[:8]}.zip"'})

    @app.get("/api/evidence")
    def evidence():
        path = ROOT / "evidence" / "repositories.json"
        if not path.exists():
            return {"schema_version": 1, "repositories": [], "rea": {"status": "pending", "limitations": ["분석 자료 준비 중"]}}
        result = json.loads(path.read_text())
        extensions = ROOT / "evidence" / "extensions" / "audit.json"
        if extensions.is_file():
            result["extensions"] = json.loads(extensions.read_text())
        return result

    @app.get("/api/evidence/files/{name:path}")
    def evidence_file(name: str):
        root = (ROOT / "evidence").resolve()
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "분석 자료를 찾을 수 없습니다.")
        return FileResponse(path, media_type="text/plain; charset=utf-8")

    @app.get("/manual", response_class=HTMLResponse)
    def user_manual():
        from .manual import manual_path, render_manual
        try:
            return HTMLResponse(render_manual(manual_path().read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/manual/download")
    def download_manual():
        from .manual import manual_path
        try:
            return FileResponse(manual_path(), media_type="text/markdown; charset=utf-8", filename="PolymerLab-설명서.md")
        except FileNotFoundError as exc:
            raise HTTPException(503, str(exc)) from exc

    from .research_api import router as research_router
    app.include_router(research_router)
    from .drive_backup import router as drive_router
    app.include_router(drive_router)
    from .workflow_api import router as workflow_router
    app.include_router(workflow_router)
    app.mount("/static", StaticFiles(directory=ROOT / "workbench" / "static"), name="assets")
    app.mount("/", StaticFiles(directory=ROOT / "workbench" / "static", html=True), name="frontend")
    return app


app = create_app()
