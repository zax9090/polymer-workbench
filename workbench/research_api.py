"""Local, reviewable research data and bounded optimization API."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid
import zipfile

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .jobs import atomic_json
from .models import JobConfig
from .research_store import PROPERTY_UNITS, ResearchStore


class ResearchRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                return await original(request)
            except HTTPException:
                raise
            except KeyError as exc:
                raise HTTPException(404, "연구 기록을 찾을 수 없습니다.") from exc
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc)) from exc
            except ImportError as exc:
                raise HTTPException(503, f"필요한 실행 의존성을 확인하세요: {exc}") from exc
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
        return handler


router = APIRouter(prefix="/api/research", route_class=ResearchRoute)


class CSVText(BaseModel):
    text: str = Field(max_length=2_000_000)


class Rows(BaseModel):
    rows: list[dict] = Field(min_length=1, max_length=1000)


class Curve(CSVText):
    kind: str = Field(max_length=20)
    options: dict = Field(default_factory=dict)


class Settings(BaseModel):
    backup_directory: str = Field(default="", max_length=4096)


class Batch(BaseModel):
    batch_size: int | None = Field(default=None, ge=1, le=10)


class Stop(BaseModel):
    cancel_active: bool = False


class Retry(BaseModel):
    rule_id: str = Field(max_length=100)


def initialize_research(app, manager):
    from .learning import SurrogateManager
    from .campaigns import CampaignManager
    root = manager.root.parent / "research"
    store = ResearchStore(root)
    app.state.research_root = root
    app.state.research_store = store
    app.state.surrogates = SurrogateManager(root, store)
    app.state.campaigns = CampaignManager(root, manager, store)
    manager.on_finished = lambda job_id: store.index_job(manager.get(job_id), manager.directory(job_id))
    app.state.research_initial_sync = store.sync_jobs(manager.root)


def settings_for(request):
    path = request.app.state.research_root / "settings.json"
    if path.is_file():
        return Settings.model_validate_json(path.read_text()).model_dump()
    return Settings().model_dump()


@router.get("/overview")
def overview(request: Request):
    from .campaigns import backend_capabilities
    state = request.app.state
    return {"experiments": state.research_store.list_experiments(),
            "measurements": state.research_store.list_measurements(),
            "models": state.surrogates.list(), "campaigns": state.campaigns.list(),
            "settings": settings_for(request), "backends": backend_capabilities(),
            "properties": PROPERTY_UNITS, "initial_sync": state.research_initial_sync}


@router.post("/sync")
def sync(request: Request):
    return request.app.state.research_store.sync_jobs(request.app.state.manager.root)


@router.get("/experiments")
def experiments(request: Request):
    return request.app.state.research_store.list_experiments()


@router.get("/measurements")
def measurements(request: Request):
    return request.app.state.research_store.list_measurements()


@router.post("/measurements", status_code=201)
def add_measurement(payload: dict, request: Request):
    return request.app.state.research_store.add_measurement(payload)


@router.post("/measurements/preview")
def preview_measurements(payload: CSVText):
    from .ingestion import parse_measurements_csv
    rows = parse_measurements_csv(payload.text)
    return {"rows": rows, "count": len(rows), "review_required": True}


@router.post("/measurements/import", status_code=201)
def import_measurements(payload: Rows, request: Request):
    rows = request.app.state.research_store.add_measurements(payload.rows)
    return {"rows": rows, "count": len(rows)}


@router.post("/curves")
def curves(payload: Curve):
    from .ingestion import analyze_curve
    return analyze_curve(payload.kind, payload.text, payload.options)


@router.post("/documents", status_code=201)
async def document(request: Request, filename: str = "document.pdf"):
    from .ingestion import ingest_pdf
    if request.headers.get("content-type", "").split(";")[0] != "application/pdf":
        raise HTTPException(415, "application/pdf 형식의 PDF를 업로드하세요.")
    folder = request.app.state.research_root / "imports" / uuid.uuid4().hex
    folder.mkdir(parents=True, mode=0o700)
    path = folder / "source.pdf"
    try:
        total = 0
        with path.open("wb") as stream:
            async for chunk in request.stream():
                total += len(chunk)
                if total > 10_000_000:
                    raise HTTPException(413, "PDF는 10 MB 이하여야 합니다.")
                stream.write(chunk)
        if not path.read_bytes()[:1024].lstrip().startswith(b"%PDF-"):
            raise HTTPException(422, "PDF 파일 헤더를 확인할 수 없습니다.")
        try:
            result = await run_in_threadpool(ingest_pdf, path)
        except Exception as exc:
            raise HTTPException(422, f"PDF 텍스트 추출 실패: {exc}") from exc
        result.update(id=folder.name, filename=Path(filename).name[:200])
        atomic_json(folder / "extracted.json", result)
        return result
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


@router.get("/settings")
def settings(request: Request):
    return settings_for(request)


@router.put("/settings")
def save_settings(payload: Settings, request: Request):
    value = payload.model_dump()
    if payload.backup_directory:
        target = Path(payload.backup_directory).expanduser().resolve()
        if not target.is_dir():
            raise HTTPException(422, "이미 존재하는 동기화 폴더를 지정하세요.")
        # Snapshots are the only cloud-facing data; the live database stays local.
        value["backup_directory"] = str(target)
    atomic_json(request.app.state.research_root / "settings.json", value)
    return value


@router.post("/snapshots", status_code=201)
def snapshot(request: Request):
    identifier = uuid.uuid4().hex
    destination = request.app.state.research_root / "exports" / identifier
    result = request.app.state.research_store.export_snapshot(destination)
    archive = destination.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(destination.iterdir()):
            if path.is_file() and not path.is_symlink():
                bundle.write(path, path.name)
    result.update(id=identifier, download_url=f"/api/research/snapshots/{identifier}/download",
                  backup={"configured": False, "copied": False, "cloud_upload_verified": False})
    directory = settings_for(request)["backup_directory"]
    if directory:
        result["backup"]["configured"] = True
        target = Path(directory) / ("PolymerLab-snapshot-" + identifier)
        staging = target.with_name("." + target.name + ".tmp")
        try:
            if not target.parent.is_dir():
                raise ValueError("동기화 폴더가 현재 연결되어 있지 않습니다.")
            shutil.copytree(destination, staging)
            staging.rename(target)
            result["backup"].update(copied=True, path=str(target))
        except (OSError, ValueError) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            result["backup"]["error"] = str(exc)
    return result


@router.get("/snapshots/{identifier}/download")
def download_snapshot(identifier: str, request: Request):
    if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise HTTPException(404, "스냅샷을 찾을 수 없습니다.")
    path = request.app.state.research_root / "exports" / (identifier + ".zip")
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "스냅샷을 찾을 수 없습니다.")
    return FileResponse(path, media_type="application/zip", filename=f"PolymerLab-data-{identifier[:8]}.zip")


@router.get("/models")
def models(request: Request):
    return request.app.state.surrogates.list()


@router.post("/models", status_code=201)
def train(payload: dict, request: Request):
    return request.app.state.surrogates.train(payload)


@router.post("/models/{identifier}/predict")
def predict(identifier: str, payload: dict, request: Request):
    return request.app.state.surrogates.predict(identifier, payload)


@router.get("/campaigns")
def campaigns(request: Request):
    return request.app.state.campaigns.list()


@router.post("/campaigns", status_code=201)
def create_campaign(payload: dict, request: Request):
    return request.app.state.campaigns.create(payload)


@router.get("/campaigns/{identifier}")
def campaign(identifier: str, request: Request):
    return request.app.state.campaigns.get(identifier)


@router.post("/campaigns/{identifier}/iterate")
def iterate(identifier: str, request: Request, payload: Batch = Body(default=Batch())):
    return request.app.state.campaigns.iterate(identifier, payload.batch_size)


@router.post("/campaigns/{identifier}/start")
def start(identifier: str, request: Request, payload: Batch = Body(default=Batch())):
    return request.app.state.campaigns.start(identifier, payload.batch_size)


@router.post("/campaigns/{identifier}/stop")
def stop(identifier: str, request: Request, payload: Stop = Body(default=Stop())):
    return request.app.state.campaigns.stop(identifier, payload.cancel_active)


@router.get("/bridges")
def bridges():
    from .bridges import bridge_capabilities
    return bridge_capabilities()


@router.post("/bridges", status_code=201)
def prepare_bridge(payload: dict, request: Request):
    manager = request.app.state.manager
    config = JobConfig.model_validate({**payload, "route": "mixture"})
    if config.source_job_id:
        source = manager.get(config.source_job_id)
        if source["status"] not in {"completed", "prepared"} or source["config"]["route"] in {"polymer_cg", "hoomd"}:
            raise ValueError("완료된 원자 구조 작업의 PDB를 연결하세요.")
        path = manager.directory(config.source_job_id) / config.source_artifact
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".pdb" or path.stat().st_size > 5_000_000:
            raise ValueError("5 MB 이하 PDB 구조 파일이 필요합니다.")
        config.pdb_text = path.read_text()
    return manager.create(config.model_dump())


@router.get("/diagnostics/{identifier}")
def diagnose(identifier: str, request: Request):
    from .diagnostics import diagnose_job
    manager = request.app.state.manager
    return diagnose_job(manager.directory(identifier), manager.get(identifier))


@router.post("/diagnostics/{identifier}/retry", status_code=201)
def retry(identifier: str, payload: Retry, request: Request):
    manager = request.app.state.manager
    job = manager.get(identifier)
    if job["status"] not in {"failed", "blocked"}:
        raise HTTPException(409, "실패한 작업의 진단에만 수정 재실행을 적용합니다.")
    result = diagnose(identifier, request)
    selected = next((r for r in result["matches"] if r["id"] == payload.rule_id and r.get("patch")), None)
    if selected is None:
        raise ValueError("적용 가능한 진단 수정안을 찾을 수 없습니다.")
    config = JobConfig.model_validate({**job["config"], **selected["patch"]})
    return manager.create(config.model_dump())
