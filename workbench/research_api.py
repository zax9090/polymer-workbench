"""Local, reviewable research data and bounded optimization API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import threading
from typing import Literal
import uuid
import zipfile

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .jobs import atomic_json, now
from .models import JobConfig
from .paths import resource_root
from .research_store import PROPERTY_UNITS, ResearchStore
from .server import client_label


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


class DoeFactor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=80)
    low: float
    high: float


class DoeDesign(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    factors: list[DoeFactor] = Field(min_length=2, max_length=6)
    kind: Literal["factorial", "central_composite"] = "factorial"
    center_points: int = Field(default=3, ge=0, le=20)
    replicates: int = Field(default=1, ge=1, le=10)
    seed: int = Field(default=1, ge=0, le=2**63 - 1)


class DoeAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=1_000_000)
    factors: list[DoeFactor] = Field(min_length=2, max_length=6)
    response: str = Field(min_length=1, max_length=80)
    model: Literal["linear", "interaction", "quadratic"] = "quadratic"
    objective: Literal["maximize", "minimize", "target"] = "maximize"
    target: float | None = None
    prediction_points: list[dict[str, float]] | None = Field(default=None, max_length=50)


def initialize_research(app, manager):
    from .learning import SurrogateManager
    from .campaigns import CampaignManager
    root = manager.root.parent / "research"
    store = ResearchStore(root)
    app.state.research_root = root
    app.state.doe_lock = threading.BoundedSemaphore(1)
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


def doe_root(request, create=False):
    root = request.app.state.research_root / "doe"
    if root.is_symlink() or root.resolve() != root:
        raise HTTPException(422, "실험계획 기록 폴더는 링크가 아닌 로컬 폴더여야 합니다.")
    if create:
        root.mkdir(exist_ok=True, mode=0o700)
    return root


def save_doe_record(request, result, parameters, csv_text, record_type):
    """Publish a complete immutable result and its CSV together; never touch reviewed measurements."""
    from . import doe
    root = doe_root(request, create=True)
    identifier = uuid.uuid4().hex
    staging = root / (".staging-" + identifier)
    final = root / identifier
    filename = "design.csv" if record_type == "doe_design" else "input.csv"
    raw = csv_text.encode("utf-8")
    kernel = doe._kernel()
    compiled = sys.modules.get(kernel.doe_fit.__module__, kernel)
    def file_hash(path):
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    record = {**result, "id": identifier, "created_at": now(), "record_type": record_type,
              "parameters": parameters, "input": {"file": filename, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
              "provenance": {"kernel_version": kernel.__version__,
                             "kernel_sha256": file_hash(Path(compiled.__file__)),
                             "analysis_code_sha256": file_hash(resource_root() / "workbench" / "doe.py")},
              "download_url": f"/api/research/doe/{identifier}/download", "csv_download_url": f"/api/research/doe/{identifier}/csv"}
    label = client_label(request)
    if label:
        record["submitted_via"] = label
    staging.mkdir(mode=0o700)
    try:
        (staging / filename).write_bytes(raw)
        atomic_json(staging / "result.json", record)
        staging.rename(final)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return record


def doe_record_file(identifier, request, filename="result.json"):
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise HTTPException(404, "실험계획 기록을 찾을 수 없습니다.")
    folder = doe_root(request) / identifier
    path = folder / filename
    if folder.is_symlink() or folder.resolve() != folder or path.is_symlink() or not path.is_file():
        raise HTTPException(404, "실험계획 기록을 찾을 수 없습니다.")
    return path


def doe_module():
    from . import doe
    try:
        doe._kernel()
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    return doe


@router.post("/doe/design", status_code=201)
def design_experiment(payload: DoeDesign, request: Request):
    doe = doe_module()
    if not request.app.state.doe_lock.acquire(blocking=False):
        raise HTTPException(429, "다른 실험계획 계산이 진행 중입니다. 완료 후 다시 실행하세요.")
    try:
        parameters = payload.model_dump()
        result = request.app.state.execution.compute("doe_design", parameters)
        return save_doe_record(request, result, parameters, result["csv"], "doe_design")
    finally:
        request.app.state.doe_lock.release()


@router.post("/doe/analyze", status_code=201)
def analyze_experiment(payload: DoeAnalysis, request: Request):
    if len(payload.text.encode("utf-8")) > 1_000_000:
        raise HTTPException(413, "실험 데이터 CSV는 UTF-8 기준 1 MB 이하여야 합니다.")
    doe = doe_module()
    if not request.app.state.doe_lock.acquire(blocking=False):
        raise HTTPException(429, "다른 실험계획 계산이 진행 중입니다. 완료 후 다시 실행하세요.")
    try:
        parameters = payload.model_dump(exclude={"text"})
        result = request.app.state.execution.compute("doe_analyze", {"text": payload.text, **parameters})
        return save_doe_record(request, result, parameters, payload.text, "doe_analysis")
    finally:
        request.app.state.doe_lock.release()


@router.get("/doe")
def list_doe_records(request: Request, limit: int = Query(default=30, ge=1, le=100)):
    root = doe_root(request)
    if not root.is_dir():
        return {"records": []}
    folders = [folder for folder in root.iterdir() if re.fullmatch(r"[a-f0-9]{32}", folder.name)
               and folder.is_dir() and not folder.is_symlink()]
    records, unreadable = [], 0
    for folder in sorted(folders, key=lambda path: path.stat().st_mtime_ns, reverse=True):
        if len(records) >= limit:
            break
        try:
            path = doe_record_file(folder.name, request)
            record = json.loads(path.read_text())
            records.append({key: record.get(key) for key in ("id", "created_at", "record_type", "parameters", "kind", "factors", "response",
                                                             "model", "objective", "total_runs", "fit", "recommendation",
                                                             "download_url", "csv_download_url")})
        except (OSError, ValueError, HTTPException):
            unreadable += 1
    return {"records": records, "unreadable_records": unreadable}


@router.get("/doe/{identifier}")
def get_doe_record(identifier: str, request: Request):
    return json.loads(doe_record_file(identifier, request).read_text())


@router.get("/doe/{identifier}/download")
def download_doe_record(identifier: str, request: Request):
    return FileResponse(doe_record_file(identifier, request), media_type="application/json", filename=f"PolymerLab-DOE-{identifier}.json")


@router.get("/doe/{identifier}/csv")
def download_doe_csv(identifier: str, request: Request):
    record = get_doe_record(identifier, request)
    filename = "design.csv" if record.get("record_type") == "doe_design" else "input.csv"
    return FileResponse(doe_record_file(identifier, request, filename), media_type="text/csv; charset=utf-8", filename=f"PolymerLab-DOE-{identifier}.csv")


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


class ScreenRequest(BaseModel):
    smiles: list[str] = Field(min_length=1, max_length=20)
    properties: list[str] | None = Field(default=None, max_length=10)


class SolventRequest(BaseModel):
    polymer: dict
    temperature: float = 298.15
    alpha: float = 0.6
    limit: int = Field(default=25, ge=1, le=300)
    names: list[str] | None = Field(default=None, max_length=300)
    blends: int = Field(default=0, ge=0, le=50)


class SphereRequest(BaseModel):
    tests: list[dict] = Field(min_length=2, max_length=300)


class BlendRequest(BaseModel):
    a: dict
    b: dict
    temperature: float = 298.15
    reference_volume: float = 100.0


class BlendContactRequest(BaseModel):
    base_smiles: str = Field(min_length=1, max_length=500)
    screen_smiles: str = Field(min_length=1, max_length=500)
    temperatures: list[float] | None = Field(default=None, max_length=200)
    samples: int = Field(default=200000, ge=1000, le=2000000)
    clusters: int = Field(default=2000, ge=20, le=5000)
    seed: int = Field(default=1, ge=1, le=2**31 - 1)
    cap: str = Field(default="hydrogen", pattern="^(hydrogen|methyl)$")
    contact_scale: float = Field(default=1.0, ge=0.8, le=1.5)
    n_base: float = Field(default=1.0, ge=1, le=1e6)
    n_screen: float = Field(default=1.0, ge=1, le=1e6)


@router.get("/screening")
def screening_card():
    from .compatibility import source
    from .screening import SURROGATE
    return {"surrogate": SURROGATE.card(), "solvents": source()}


@router.get("/screening/validation")
def screening_validation():
    from .screening import SURROGATE
    return SURROGATE.cross_validation()


@router.post("/screening/predict")
def screening_predict(payload: ScreenRequest, request: Request):
    from .screening import SURROGATE, own_simulations
    experiments = request.app.state.research_store.list_experiments({"route": "polymer_atomistic"})
    results = []
    for smiles in payload.smiles:
        prediction = SURROGATE.predict(smiles, payload.properties)
        results.append({**prediction, "own_simulations": own_simulations(prediction, experiments)})
    return {"predictions": results}


@router.get("/screening/solvents")
def screening_solvents(query: str = ""):
    from .compatibility import list_solvents, source
    return {"solvents": list_solvents(query[:100]), "source": source()}


@router.post("/screening/solvents")
def screening_rank(payload: SolventRequest):
    from .compatibility import rank_solvents
    return rank_solvents(payload.polymer, payload.temperature, payload.alpha, payload.limit, payload.names, payload.blends)


@router.post("/screening/hsp-fit")
def screening_fit(payload: SphereRequest):
    from .compatibility import fit_sphere
    return fit_sphere(payload.tests)


@router.post("/screening/blend")
def screening_blend(payload: BlendRequest):
    from .compatibility import blend_compatibility
    return blend_compatibility(payload.a, payload.b, payload.temperature, payload.reference_volume)


@router.post("/screening/blend-contacts")
def screening_blend_contacts(payload: BlendContactRequest, request: Request):
    from .blends import blend_chi
    try:
        return request.app.state.execution.compute("blend_contacts", payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


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
    return manager.create(config.model_dump(), submitted_via=client_label(request))


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
    return manager.create(config.model_dump(), submitted_via=client_label(request))
