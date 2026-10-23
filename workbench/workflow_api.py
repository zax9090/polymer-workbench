"""Explicit stage transitions for connected bulk research; no implicit next-stage submission."""
from typing import Literal
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from .research_api import ResearchRoute
from .models import JobConfig
from .workflows import FIELDS, METRICS

router = APIRouter(prefix="/api/research/workflows", route_class=ResearchRoute)

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

class Create(Strict):
    design_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    template: JobConfig
    bindings: dict[str, str]
    metric: Literal["final_frame_density", "probe_accessible_fraction"] = "probe_accessible_fraction"
    probe: float = Field(default=1.0, ge=0, le=5)
    spacing: float = Field(default=0.25, ge=0.05, le=1)
    budget: int = Field(default=50, ge=1, le=50)

class Rows(Strict):
    rows: list[int] = Field(min_length=1, max_length=50)

class Submit(Rows):
    retry_failed: bool = False

class Attach(Strict):
    run_order: int = Field(ge=1, le=50)
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")

class Optimize(Strict):
    model: Literal["linear", "interaction", "quadratic"] = "quadratic"
    objective: Literal["maximize", "minimize", "target"] = "maximize"
    target: float | None = None
    exclusions: dict[str, str] = Field(default_factory=dict, max_length=50)

class Confirm(Strict):
    optimization_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    retry_failed: bool = False

class ConfirmationAnalysis(Strict):
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")

@router.get("/capabilities")
def capabilities():
    return {"fields": FIELDS, "metrics": METRICS, "max_budget": 50,
            "template_requirements": {"route": "polymer_atomistic", "atomistic_mode": "bulk", "atomistic_forcefield": "gaff2", "cell_builder": "native"}}

@router.get("")
def list_workflows(request: Request):
    return {"records": request.app.state.workflows.list()}

@router.post("", status_code=201)
def create_workflow(payload: Create, request: Request):
    return request.app.state.workflows.create(**payload.model_dump())

@router.get("/{identifier}")
def get_workflow(identifier: str, request: Request):
    return request.app.state.workflows.get(identifier)

@router.get("/{identifier}/download")
def download_workflow(identifier: str, request: Request):
    manager = request.app.state.workflows
    manager.get(identifier)
    return FileResponse(manager.root / identifier / "result.json", media_type="application/json", filename=f"PolymerLab-workflow-{identifier}.json")

@router.post("/{identifier}/submit")
def submit_workflow(identifier: str, payload: Submit, request: Request):
    return request.app.state.workflows.submit(identifier, payload.rows, payload.retry_failed)

@router.post("/{identifier}/attach")
def attach_job(identifier: str, payload: Attach, request: Request):
    return request.app.state.workflows.attach(identifier, **payload.model_dump())

@router.post("/{identifier}/analyze")
def analyze_workflow(identifier: str, payload: Rows, request: Request):
    return request.app.state.workflows.analyze(identifier, payload.rows)

@router.post("/{identifier}/optimize")
def optimize_workflow(identifier: str, payload: Optimize, request: Request):
    return request.app.state.workflows.optimize(identifier, **payload.model_dump())

@router.post("/{identifier}/confirm")
def confirm_workflow(identifier: str, payload: Confirm, request: Request):
    return request.app.state.workflows.confirm(identifier, payload.optimization_id, payload.retry_failed)

@router.post("/{identifier}/analyze-confirmation")
def analyze_confirmation(identifier: str, payload: ConfirmationAnalysis, request: Request):
    return request.app.state.workflows.analyze_confirmation(identifier, payload.job_id)

@router.post("/{identifier}/cancel")
def cancel_workflow(identifier: str, request: Request):
    return request.app.state.workflows.cancel(identifier)
