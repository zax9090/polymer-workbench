"""Target-contract preview and preparation; simulation execution stays explicit."""
from fastapi import APIRouter, HTTPException, Request

from .research_api import ResearchRoute, save_doe_record
from .target_spec import TargetSpec, capabilities, inspect_spec
from .workflows import digest

router = APIRouter(prefix="/api/research/targets", route_class=ResearchRoute)


@router.get("/capabilities")
def target_capabilities():
    return capabilities()


def inspect(payload, request):
    state = request.app.state
    return inspect_spec(payload.model_dump(), state.manager, state.execution)


@router.post("/preview")
def preview_target(payload: TargetSpec, request: Request):
    result, _ = inspect(payload, request)
    return result


@router.post("/prepare", status_code=201)
def prepare_target(payload: TargetSpec, request: Request):
    result, prepared = inspect(payload, request)
    if not result["ready"]:
        raise HTTPException(422, {"message": "계획을 준비하기 전에 아래 항목을 해결하세요.", "blockers": result["blockers"]})
    state = request.app.state
    # All semantic validation and engine/chemistry checks finished before publishing any research record.
    design = prepared["design"]
    saved = save_doe_record(request, design, prepared["parameters"], design["csv"], "doe_design")
    goals = prepared["goals"]
    workflow = state.workflows.create(saved["id"], prepared["contract"]["template"], prepared["bindings"],
        goals[0]["name"], probe=payload.probe, spacing=payload.spacing, budget=payload.max_calculations,
        responses=[goal["name"] for goal in goals])
    contract = prepared["contract"]
    contract["design_id"] = saved["id"]
    contract_sha = digest(contract)
    with state.workflows.lock:
        stored = state.workflows._read(workflow["id"])
        stored["target_contract"] = contract
        stored["target_contract_sha256"] = contract_sha
        state.workflows._save(stored)
    workflow = state.workflows.get(workflow["id"])
    return {"schema_version": 1, "workflow": workflow, "contract": contract, "contract_sha256": contract_sha,
            "design_id": saved["id"], "plan": result["plan"], "cost": result["cost"],
            "automatic_execution": False,
            "next_action": {"method": "POST", "path": f"/api/research/workflows/{workflow['id']}/autopilot",
                            "requires_approved_budget": True}}
