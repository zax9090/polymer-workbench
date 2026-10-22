"""Offline, inspectable log rules. No model API or automatic parameter changes."""
from __future__ import annotations
from pathlib import Path


def diagnose_job(directory: Path, job: dict) -> dict:
    chunks = []
    for path in sorted(Path(directory).glob("*.log")):
        if path.is_symlink():
            continue
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 40000))
            chunks.append((path.name, stream.read().decode("utf-8", errors="replace")))
    text = "\n".join(s for _, s in chunks).lower()
    matches = []
    rules = [
        ("unstable_atoms", ("lost atoms", "out of range atoms", "bond atoms missing", "non-numeric atom coords", "nan coords"),
         "원자 이동 또는 수치 불안정", "초기 겹침, 힘장·단위, 경계조건과 시간 간격을 확인하세요. 먼저 최소화하고 궤적의 발산 시점을 검토하세요."),
        ("parameters", ("not all bond coeffs", "not all pair coeffs", "no default bond types", "unknown atom type", "ff_assign"),
         "힘장 파라미터 확인 필요", "누락된 원자·결합 타입을 확인하고 동일 힘장과 비결합 규칙으로 전체 토폴로지를 구성하세요."),
        ("dependency", ("modulenotfounderror", "cannot open shared object", "command not found", "no such file or directory"),
         "의존성 또는 경로 오류", "계산 엔진 화면과 로그의 파일 경로를 확인하세요. 별도 Python 환경과 네이티브 엔진은 호환되는 설치를 지정해야 합니다."),
        ("memory", ("out of memory", "cannot allocate memory", "std::bad_alloc"),
         "메모리 부족", "사슬 길이·개수나 저장 프레임 수를 줄이고 같은 모델 조건에서 작은 시스템으로 먼저 검증하세요."),
    ]
    for key, patterns, title, advice in rules:
        evidence = [{"file": name, "line": line[:500]} for name, raw in chunks
                    for line in raw.splitlines() if any(p in line.lower() for p in patterns)][:8]
        if evidence:
            item = {"id": key, "title": title, "advice": advice, "evidence": evidence, "patch": None}
            cfg = job.get("config", {})
            if key == "unstable_atoms" and cfg.get("route") in {"polymer_cg", "hoomd"}:
                value = float(cfg.get("timestep", .002))
                if value > .0001:
                    item["patch"] = {"timestep": max(.0001, value / 2)}
                    item["patch_note"] = "환산 시간 간격을 절반으로 줄인 새 작업입니다. 오류 해결이나 물리적 타당성을 보장하지 않습니다."
            matches.append(item)
    return {"method": "versioned_local_rules_v1", "llm_used": False, "matches": matches,
            "status": job.get("status"), "note": "로그 패턴을 근거로 제시한 검토 항목입니다. 실패 원인의 확정 진단은 아닙니다."}
