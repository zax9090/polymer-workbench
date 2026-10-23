"""Shared validated structure analysis for direct calls and linked research workflows."""
from fastapi import HTTPException
from pydantic import ValidationError


def analyze_structure(manager, research_root, job_id, analysis, parameters, owner=None, cancelled=None):
    from .server import ANALYSIS_PARAMETERS
    from . import structure_analysis
    directory = manager.directory(job_id)
    if directory.is_symlink() or directory.resolve() != directory:
        raise HTTPException(422, "링크된 작업 폴더에서는 구조 분석을 실행할 수 없습니다.")
    # Read only these fixed names. Never follow a link to another job or outside the data root.
    for name in ("job.json", "eq3_last.data", "final.data", "eq3.dump", "bulk_complete.json"):
        path = directory / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise HTTPException(422, "구조 분석 입력 파일은 작업 폴더 안의 실제 파일이어야 합니다.")
    with manager.lock:
        job = manager._read(job_id)
    if job["status"] != "completed":
        raise HTTPException(409, "완료된 작업에서만 구조 분석을 실행할 수 있습니다.")
    config = job.get("config") or {}
    if config.get("route") != "polymer_atomistic" or config.get("atomistic_mode") not in {"bulk", "crosslink"}:
        raise HTTPException(422, "구조 분석은 완료된 전원자 벌크 셀 또는 가교 작업에서 실행하세요.")
    if analysis == "widom" and (config.get("atomistic_mode") != "bulk" or config.get("atomistic_forcefield", "gaff2") != "gaff2"):
        raise HTTPException(422, "Widom 삽입은 GAFF2 벌크 셀에서만 지원합니다. PCFF·가교 셀은 지원하지 않습니다.")
    try:
        parameters = ANALYSIS_PARAMETERS[analysis].model_validate(parameters).model_dump()
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        raise HTTPException(422, f"구조 분석 매개변수의 이름·형식·허용 범위를 확인하세요: {fields}") from exc
    if structure_analysis.native is None:
        raise HTTPException(503, "Rust 계산 커널이 설치되어 있지 않습니다. cargo가 있는 환경에서 setup.sh를 다시 실행하세요.")
    try:
        result = manager.execution.compute("structure", {"directory": str(directory), "analysis": analysis,
            "parameters": parameters, "job_id": job_id}, owner=owner or "analysis:" + job_id, cancelled=cancelled)
        from .drive_backup import save_structure_analysis
        return save_structure_analysis(research_root, job_id, result, parameters)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, f"구조 분석 입력을 확인하세요: {exc}") from exc
