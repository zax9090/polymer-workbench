"""A target contract over existing independent-factor bulk workflows.

This envelope validates units and feasible inputs; it provides no new physical
screening model. Numerical design/rank/goal checks reuse the installed Rust DOE
kernel. Preparing a contract never submits a simulation or approves execution.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import autopilot, doe, preflight, workflows
from .models import JobConfig

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Variable(Strict):
    name: str = Field(min_length=1, max_length=80)
    field: str = Field(min_length=1, max_length=80)
    low: float
    high: float
    unit: str = Field(min_length=1, max_length=30)


class Target(Strict):
    name: str = Field(min_length=1, max_length=80)
    unit: str = Field(min_length=1, max_length=30)
    model: Literal["linear", "interaction", "quadratic"] = "quadratic"
    goal: Literal["maximize", "minimize", "target"]
    lower: float | None = None
    target: float
    upper: float | None = None
    lower_shape: float = Field(default=1.0, ge=0.1, le=10)
    upper_shape: float = Field(default=1.0, ge=0.1, le=10)
    importance: float = Field(default=1.0, ge=0.1, le=10)


class TargetSpec(Strict):
    name: str = Field(min_length=1, max_length=80)
    mode: str = Field(default="exploratory", max_length=40)
    template: JobConfig
    variables: list[Variable] = Field(min_length=2, max_length=4)
    targets: list[Target] = Field(min_length=1, max_length=5)
    design_kind: Literal["factorial", "central_composite"] | None = None
    center_points: int = Field(default=3, ge=0, le=20)
    replicates: int = Field(default=1, ge=1, le=10)
    seed: int = Field(default=1, ge=0, le=2**63 - 1)
    max_calculations: int = Field(default=20, ge=1, le=50)
    max_attempts: int = Field(default=1, ge=1, le=3)
    max_failed_rows: int = Field(default=0, ge=0, le=50)
    confirm: bool = True
    report: bool = True
    probe: float = Field(default=1.0, ge=0, le=5)
    spacing: float = Field(default=0.25, ge=0.05, le=1)



SCHEMA_VERSION = 1
WARNINGS = [
    "This target contract supports exploratory GAFF2 bulk calculations, not validated physical-property predictions.",
    "Input feasibility checks are not low-cost physical screening; no candidates are discarded by a predictive model.",
    "Independent-factor designs are not mixture designs. Statistical fit and confirmation do not establish equilibrium or experimental validity.",
]


def capabilities():
    fields = workflows.ROUTES["gaff2_bulk"]["fields"]
    names = workflows.ROUTES["gaff2_bulk"]["responses"]
    return {"route": "gaff2_bulk", "mode": "exploratory", "variables": {
                key: {**workflows.FIELDS[key], "bounds": workflows.field_bounds(key)} for key in fields},
            "targets": {key: {"unit": workflows.METRICS[key], "label": workflows.LABELS[key]} for key in sorted(names)},
            "independent_factors_only": True, "predictive_screening_available": False,
            "max_calculations": 50, "supports_validated_property_claims": False}


def inspect_spec(payload, manager, execution):
    """Return a no-write preview and private prepared inputs, or explicit blockers."""
    spec = deepcopy(payload)
    blockers, reports = [], []

    def block(code, message, **details):
        blockers.append({"code": code, "message": message, **details})

    if spec["mode"] != "exploratory":
        block("unsupported_mode", "현재 목표 계약은 탐색용 계산만 지원합니다. 검증된 물성 예측으로 실행할 수 없습니다.")
    template = JobConfig.model_validate(spec["template"]).model_dump()
    if workflows.detect_route(template) != "gaff2_bulk" or template["engine_input"] or template["source_job_id"]:
        block("unsupported_route", "목표 계약의 첫 경로는 GAFF2 자체 벌크입니다. 임의 엔진 입력이나 이전 구조 연결은 지원하지 않습니다.")
    allowed = workflows.ROUTES["gaff2_bulk"]["fields"]
    variables, fields, factors, bindings = spec["variables"], set(), [], {}
    for variable in variables:
        name, field = variable["name"], variable["field"]
        if field not in allowed:
            block("unsupported_variable", f"{name}: GAFF2 벌크에 연결할 수 없는 설정입니다: {field}", variable=name)
            continue
        if field in fields:
            block("duplicate_binding", f"{name}: 같은 계산 설정을 여러 인자에 연결할 수 없습니다.", variable=name)
        fields.add(field)
        expected = workflows.FIELDS[field]["unit"]
        if variable["unit"] != expected:
            block("variable_unit", f"{name}: {expected} 단위를 사용하세요. 단위를 자동 변환하지 않습니다.", variable=name, expected_unit=expected)
        low, high = workflows.field_bounds(field)
        if not low <= variable["low"] < variable["high"] <= high:
            block("variable_bounds", f"{name}: {field} 범위는 {low:g}~{high:g} 안이며 하한은 상한보다 작아야 합니다.", variable=name)
        factors.append({key: variable[key] for key in ("name", "low", "high")})
        bindings[name] = field
    try:
        doe._factors(factors, 4)
    except ValueError as exc:
        block("factors", str(exc))

    target_names, goals = set(), []
    supported = workflows.ROUTES["gaff2_bulk"]["responses"]
    for target in spec["targets"]:
        name = target["name"]
        if name in target_names:
            block("duplicate_target", "목표 반응값 이름은 서로 달라야 합니다.", target=name)
        target_names.add(name)
        if name not in supported:
            block("unsupported_target", f"{name}: 이 경로에서 계산할 수 없는 목표입니다. Tg·출처 미확인 상관식·다른 탄성률을 대체 값으로 바꾸지 않습니다.", target=name)
            continue
        expected = workflows.METRICS[name]
        if target["unit"] != expected:
            block("target_unit", f"{name}: {expected} 단위를 사용하세요. 단위를 자동 변환하지 않습니다.", target=name, expected_unit=expected)
        unused = "upper" if target["goal"] == "maximize" else "lower" if target["goal"] == "minimize" else None
        if unused and target.get(unused) is not None:
            block("target_bounds", f"{name}: {target['goal']} 목표에서 {unused}는 사용하지 않습니다. 조건을 없애거나 목표값 맞추기로 지정하세요.", target=name)
        goals.append({key: value for key, value in target.items() if key != "unit" and value is not None})
    try:
        goals = doe._goals(goals, False)
        # Existing Rust validation catches reversed/degenerate desirability bounds too.
        doe._mixture_kernel().desirability([doe._goal_spec(goal) for goal in goals], [goal["target"] for goal in goals])
    except ValueError as exc:
        block("targets", f"목표 범위를 확인하세요: {exc}")
    if target_names & set(bindings):
        block("target_factor_name", "반응값 이름과 독립 인자 이름은 서로 달라야 합니다.")

    kind = spec["design_kind"] or ("central_composite" if any(t["model"] == "quadratic" for t in spec["targets"]) else "factorial")
    parameters = {"factors": factors, "kind": kind, "center_points": spec["center_points"],
                  "replicates": spec["replicates"], "seed": spec["seed"]}
    expected_rows = ((2 ** len(variables)) + (2 * len(variables) if kind == "central_composite" else 0) + spec["center_points"]) * spec["replicates"]
    worst = (expected_rows + int(spec["confirm"])) * spec["max_attempts"]
    if expected_rows > spec["max_calculations"] or worst > spec["max_calculations"]:
        block("budget", f"현재 설계와 재시도·확인 계산에는 최대 {worst}개 계산이 필요합니다. 예산 {spec['max_calculations']}개를 초과합니다.")
    result = {"ready": False, "schema_version": SCHEMA_VERSION, "mode": "exploratory", "name": spec["name"],
              "capabilities": capabilities(), "blockers": blockers, "warnings": list(WARNINGS),
              "screening": {"method": "input_feasibility", "predictive_screening_available": False,
                            "discarded": 0, "message": "화학 구조·입력 범위·엔진 점검만 수행합니다. 물성 예측으로 후보를 탈락시키지 않습니다."},
              "design": {**parameters, "total_runs": expected_rows},
              "estimability": [], "cost": {"worst_case_runs": worst, "approved_budget_required": worst,
                       "max_calculations": spec["max_calculations"]}, "rows": reports, "automatic_execution": False}
    if blockers:
        return result, None
    runtime_missing = workflows.preflight(template)
    result["missing_engines"] = runtime_missing
    if runtime_missing:
        block("missing_engines", "실행할 계산 엔진이 필요합니다: " + ", ".join(runtime_missing))
        return result, None
    try:
        design = execution.compute("doe_design", parameters, wait=execution.request_wait)
        import numpy as np
        points = np.asarray([[run["coded"][factor["name"]] for factor in factors] for run in design["runs"]], dtype=float)
        for model in sorted({goal["model"] for goal in goals}):
            rank = doe._kernel().doe_fit(points, np.zeros(len(points)), model)
            result["estimability"].append({"model": model, "terms": len(rank["coefficients"]),
                "df_residual": rank["degrees"], "replicate_groups": rank["anova"]["replicate_groups"],
                "pure_error_df": rank["anova"]["pure_error"]["df"], "check": "rank_only_not_fit_quality"})
            if rank["degrees"] < 5:
                result["warnings"].append(f"Model {model} has fewer than five residual degrees of freedom; uncertainty estimates will be fragile.")
            if rank["anova"]["pure_error"]["df"] == 0:
                result["warnings"].append(f"Model {model} has no exact repeated settings; pure error and lack-of-fit significance cannot be estimated.")
        rows = []
        for run in design["runs"]:
            config = dict(template)
            for name, field in bindings.items():
                value = run["values"][name]
                if workflows.FIELDS[field].get("integer"):
                    if abs(value - round(value)) > 1e-9:
                        raise ValueError(f"{name}: 정수 설정은 모든 설계점이 정수여야 합니다.")
                    value = int(round(value))
                config[field] = value
            config["seed"] = (template["seed"] + run["run_order"] - 1) % 899999998 + 1
            config = workflows.validated(config, run["run_order"])
            checked = preflight.check(config)
            missing = [engine["engine"] for engine in checked["engines"] if not engine["available"] and not engine.get("note")]
            reports.append({"run_order": run["run_order"], "settings": run["values"], "seed": config["seed"],
                            "valid": checked["valid"] and not missing, "estimate": checked["estimate"],
                            "engines": checked["engines"], "warnings": checked["warnings"]})
            for error in checked["errors"]:
                block("preflight", f"{run['run_order']}행: {error['message']}", run_order=run["run_order"])
            if missing:
                block("missing_engines", f"{run['run_order']}행 계산 엔진이 필요합니다: {', '.join(missing)}", run_order=run["run_order"])
            rows.append({"run_order": run["run_order"], "state": "planned", "attempts": []})
        plan, cost = autopilot.normalize({"factors": factors, "rows": rows, "metric": goals[0]["name"],
            "responses": [g["name"] for g in goals], "route": "gaff2_bulk", "budget": spec["max_calculations"], "submitted": 0},
            {"max_attempts": spec["max_attempts"], "max_failed_rows": spec["max_failed_rows"],
             "goals": goals, "model": goals[0]["model"], "optimize": True, "confirm": spec["confirm"],
             "report": spec["report"], "upload_report": False})
    except ValueError as exc:
        block("design_or_model", f"설계·모델·입력의 연결을 확인하세요: {exc}")
        return result, None
    result.update(ready=not blockers, plan=plan)
    result["cost"].update(cost)
    contract = {"schema_version": SCHEMA_VERSION, "mode": "exploratory", "name": spec["name"],
                "template": template, "variables": variables, "targets": spec["targets"], "design": parameters,
                "budget": spec["max_calculations"], "plan": plan,
                "analysis": {"probe": spec["probe"], "spacing": spec["spacing"]},
                "screening": result["screening"], "estimability": result["estimability"], "warnings": result["warnings"]}
    return result, {"contract": contract, "design": design, "parameters": parameters,
                    "bindings": bindings, "goals": goals} if not blockers else None
