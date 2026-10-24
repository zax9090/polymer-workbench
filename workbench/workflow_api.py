"""Explicit stage transitions for connected bulk research; no implicit next-stage submission."""
from typing import Literal
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from .research_api import ResearchRoute
from .models import JobConfig
from .workflows import FIELDS, LABELS, METRICS, ROUTES, field_bounds

router = APIRouter(prefix="/api/research/workflows", route_class=ResearchRoute)

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

Metric = Literal["final_frame_density", "probe_accessible_fraction", "npt_density", "bulk_modulus", "volume_expansion", "epoxide_conversion",
                 "tg_kelvin", "expansion_glass", "expansion_melt"]

class Create(Strict):
    design_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    template: JobConfig
    bindings: dict[str, str]
    metric: Metric = "probe_accessible_fraction"
    responses: list[Metric] = Field(default_factory=list, max_length=5)
    probe: float = Field(default=1.0, ge=0, le=5)
    spacing: float = Field(default=0.25, ge=0.05, le=1)
    budget: int = Field(default=50, ge=1, le=50)

class TgScan(Strict):
    tg_high_kelvin: float = Field(default=500, ge=150, le=800)
    tg_low_kelvin: float = Field(default=200, ge=50, le=700)
    tg_step_kelvin: float = Field(default=20, ge=5, le=100)
    tg_stage_steps: int = Field(default=200000, ge=1000, le=2000000)
    metric: Metric = "tg_kelvin"
    responses: list[Metric] = Field(default_factory=list, max_length=3)
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
    response: Metric | None = None

class Goal(Strict):
    name: Metric
    model: Literal["linear", "interaction", "quadratic"] = "quadratic"
    goal: Literal["maximize", "minimize", "target"]
    lower: float | None = None
    target: float
    upper: float | None = None
    lower_shape: float = Field(default=1.0, ge=0.1, le=10)
    upper_shape: float = Field(default=1.0, ge=0.1, le=10)
    importance: float = Field(default=1.0, ge=0.1, le=10)

class DesirabilityOptimize(Strict):
    goals: list[Goal] = Field(min_length=1, max_length=5)
    exclusions: dict[str, str] = Field(default_factory=dict, max_length=50)

class Confirm(Strict):
    optimization_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    retry_failed: bool = False

class ConfirmationAnalysis(Strict):
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")

class Plan(Strict):
    rows: list[int] | None = Field(default=None, min_length=1, max_length=50)
    max_attempts: int = Field(default=2, ge=1, le=3)
    max_failed_rows: int = Field(default=0, ge=0, le=50)
    optimize: bool = True
    model: Literal["linear", "interaction", "quadratic"] = "quadratic"
    objective: Literal["maximize", "minimize", "target"] = "maximize"
    target: float | None = None
    confirm: bool = True
    report: bool = True
    upload_report: bool = False
    response: Metric | None = None
    goals: list[Goal] | None = Field(default=None, min_length=1, max_length=5)

class Approval(Strict):
    plan: Plan
    approved_budget: int = Field(ge=1, le=50)

@router.get("/capabilities")
def capabilities():
    return {"fields": {name: {**spec, "bounds": field_bounds(name)} for name, spec in FIELDS.items()}, "metrics": METRICS, "labels": LABELS, "max_budget": 50,
            "routes": {key: {"label": spec["label"], "template": {"route": "polymer_atomistic", **spec.get("match", {})},
                             "fields": spec.get("fields", []), "responses": spec["responses"]} for key, spec in ROUTES.items()}}

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

@router.post("/{identifier}/tg-scan", status_code=201)
def create_tg_scan(identifier: str, payload: TgScan, request: Request):
    data = payload.model_dump()
    return request.app.state.workflows.create_tg_scan(identifier, data, data.pop("metric"), data.pop("responses"), data.pop("budget"))

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

@router.post("/{identifier}/optimize-desirability")
def optimize_desirability(identifier: str, payload: DesirabilityOptimize, request: Request):
    goals = [{key: value for key, value in goal.model_dump().items() if value is not None} for goal in payload.goals]
    return request.app.state.workflows.optimize_desirability(identifier, goals, payload.exclusions)

@router.post("/{identifier}/confirm")
def confirm_workflow(identifier: str, payload: Confirm, request: Request):
    return request.app.state.workflows.confirm(identifier, payload.optimization_id, payload.retry_failed)

@router.post("/{identifier}/analyze-confirmation")
def analyze_confirmation(identifier: str, payload: ConfirmationAnalysis, request: Request):
    return request.app.state.workflows.analyze_confirmation(identifier, payload.job_id)

@router.post("/{identifier}/autopilot/preview")
def preview_autopilot(identifier: str, payload: Plan, request: Request):
    return request.app.state.autopilot.preview(identifier, payload.model_dump())

@router.post("/{identifier}/autopilot")
def start_autopilot(identifier: str, payload: Approval, request: Request):
    from .server import client_label
    return request.app.state.autopilot.start(identifier, payload.plan.model_dump(), payload.approved_budget, client_label(request) or "local")

@router.post("/{identifier}/autopilot/stop")
def stop_autopilot(identifier: str, request: Request):
    return request.app.state.autopilot.stop(identifier)

@router.post("/{identifier}/cancel")
def cancel_workflow(identifier: str, request: Request):
    return request.app.state.workflows.cancel(identifier)
