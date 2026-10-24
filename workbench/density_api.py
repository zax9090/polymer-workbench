"""Fixed-condition density evidence over existing jobs; never launches simulations."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .density_studies import Attach, Create, Reference, StudyError
from .research_api import ResearchRoute

router = APIRouter(prefix="/api/research/density-studies", route_class=ResearchRoute)


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except StudyError as exc:
        raise HTTPException(422, {"code": exc.code, "message": str(exc)}) from exc


@router.get("")
def list_studies(request: Request):
    return {"records": call(request.app.state.density_studies.list)}


@router.post("", status_code=201)
def create_study(payload: Create, request: Request):
    return call(request.app.state.density_studies.create, **payload.model_dump())


@router.get("/{identifier}")
def get_study(identifier: str, request: Request):
    return call(request.app.state.density_studies.get, identifier)


@router.get("/{identifier}/download")
def download_study(identifier: str, request: Request):
    manager = request.app.state.density_studies
    call(manager.get, identifier)
    return FileResponse(manager._folder(identifier) / "result.json", media_type="application/json", filename=f"PolymerLab-density-study-{identifier}.json")


@router.post("/{identifier}/attach")
def attach_study(identifier: str, payload: Attach, request: Request):
    return call(request.app.state.density_studies.attach, identifier, payload.job_ids)


@router.post("/{identifier}/reference")
def reference_study(identifier: str, payload: Reference, request: Request):
    return call(request.app.state.density_studies.reference, identifier, **payload.model_dump())


@router.post("/{identifier}/assess")
def assess_study(identifier: str, request: Request):
    return call(request.app.state.density_studies.assess, identifier)


@router.get("/{identifier}/assessments/{assessment_id}/download")
def download_assessment(identifier: str, assessment_id: str, request: Request):
    path = call(request.app.state.density_studies.assessment_path, identifier, assessment_id)
    return FileResponse(path, media_type="application/json", filename=f"PolymerLab-density-assessment-{assessment_id}.json")
