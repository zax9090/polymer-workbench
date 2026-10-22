"""Loopback-only application API for the local research workbench."""
from contextlib import asynccontextmanager
import json
import os
import secrets
from pathlib import Path
import zipfile
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .jobs import JobManager, ROOT
from .models import JobConfig
from .paths import default_jobs_root
from . import __version__


def create_app(data_root=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = JobManager(Path(data_root or default_jobs_root()))
        from .research_api import initialize_research
        initialize_research(app, app.state.manager)
        yield
        app.state.campaigns.close()
        app.state.manager.close()

    app = FastAPI(title="Polymer Lab", version=__version__, lifespan=lifespan)
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
    def health():
        return {"ok": True, "name": "Polymer Lab", "version": __version__, "local_only": True}

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
        engines += [{"id": "3dmol", "name": "3Dmol.js", "available": True, "version": "local vendor", "detail": "로컬 분자 시각화 라이브러리"},
                    {"id": "alphafold_db", "name": "AlphaFold DB", "available": True, "version": None, "detail": "공개 예측 구조 조회 · 인터넷 연결 필요"}]
        rea_path = ROOT / "evidence" / "repositories.json"
        rea = json.loads(rea_path.read_text()).get("rea", {}) if rea_path.exists() else {}
        engines.append({"id": "rea", "name": "REA", "available": (ROOT / "tools" / "rea" / "node_modules").exists(), "version": rea.get("version"), "detail": "개발 분석 도구 · 데스크톱 배포본에는 분석 기록을 포함하며 REA 재실행 환경은 별도"})
        route_info = {
            "polymer_cg": ("조대화 고분자", "LAMMPS/NumPy → MDAnalysis → 궤적·관측값", ["numpy"]),
            "polymer_atomistic": ("전원자 고분자", "RDKit 구조 생성 또는 RadonPy/LAMMPS 고립 사슬 MD", ["rdkit"]),
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
        if config.source_job_id:
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
        return manager.create(config.model_dump())

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, request: Request):
        return request.app.state.manager.get(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        return request.app.state.manager.cancel(job_id)

    @app.post("/api/jobs/{job_id}/rerun", status_code=201)
    def rerun(job_id: str, request: Request):
        config = request.app.state.manager.get(job_id)["config"]
        return request.app.state.manager.create(JobConfig.model_validate(config).model_dump())

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

    from .research_api import router as research_router
    app.include_router(research_router)
    app.mount("/static", StaticFiles(directory=ROOT / "workbench" / "static"), name="assets")
    app.mount("/", StaticFiles(directory=ROOT / "workbench" / "static", html=True), name="frontend")
    return app


app = create_app()
