"""Fixed-condition bulk-density evidence, without submitting or approving calculations.

Replicates are independent preparations only provisionally: distinct seeds and
structure hashes support that interpretation, but cannot prove statistical independence.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import threading
import uuid

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .jobs import atomic_json, now
from .models import JobConfig
from .research_store import material_identity
from .workflows import digest

SCHEMA_VERSION = 1
IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
MAX_BYTES = 20 * 1024 * 1024
FILES = ("job.json", "config.json", "bulk_complete.json", "eq3_last.data", "result.json")
WARNINGS = [
    "Simulation density is not a reviewed experimental measurement. Execution, convergence, reproducibility, reference agreement and human review are separate evidence.",
    "Distinct seeds and structure hashes support independent preparation but do not prove independence or adequate within-run sampling.",
    "A Student-t interval across prepared cells assumes independent, approximately normal run means; it excludes force-field bias and unmeasured model discrepancy.",
    "Meeting a tolerance or repeatability criterion is not scientific validation. Small oligomer cells may differ from experimental polymer molecular weight and sample preparation.",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Create(Strict):
    name: str = Field(min_length=1, max_length=80)
    template: JobConfig
    minimum_replicates: int = Field(default=3, ge=3, le=10)
    max_replicates: int = Field(default=5, ge=3, le=10)
    max_relative_sd: float | None = Field(default=None, gt=0)
    absolute_tolerance_g_cm3: float | None = Field(default=None, gt=0)


class Attach(Strict):
    job_ids: list[str] = Field(min_length=1, max_length=10)


class Reference(Strict):
    measurement_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    standard_uncertainty: float | None = Field(default=None, gt=0)


class StudyError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise StudyError(code, message)


def _positive(value, name, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        _fail("invalid_number", f"{name}은(는) 유한한 양수여야 합니다.")
    return float(value)


def _canonical(smiles):
    return material_identity({"repeat_smiles": smiles})["identity"]["repeat_smiles"]


def _config(config):
    if not isinstance(config, dict):
        _fail("config_object", "작업 설정은 JSON 객체여야 합니다.")
    value = JobConfig.model_validate(config).model_dump()
    if (value["route"] != "polymer_atomistic" or value["atomistic_mode"] != "bulk"
            or value["atomistic_forcefield"] != "gaff2" or value["cell_builder"] != "native"
            or value["source_job_id"] or value["engine_input"]):
        _fail("unsupported_route", "밀도 반복 연구는 이전 구조·임의 입력 없는 GAFF2 자체 벌크만 지원합니다.")
    value["repeat_smiles"] = _canonical(value["repeat_smiles"])
    return value


def _condition_key(config):
    return digest({key: value for key, value in _config(config).items() if key not in {"seed", "name"}})


def _read_file(root, name, required=False):
    path = root / name
    if root.is_symlink() or path.is_symlink():
        _fail("source_symlink", "연결된 연구 자료에 심볼릭 링크를 사용할 수 없습니다.")
    if not path.is_file():
        if required:
            _fail("source_missing", f"필요한 원본 파일이 없습니다: {name}")
        return None, None
    before = path.stat()
    if not 0 < before.st_size <= MAX_BYTES:
        _fail("source_size", f"원본 파일은 비어 있지 않은 20 MiB 이하 파일이어야 합니다: {name}")
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
        _fail("source_changed", "자료를 읽는 중 원본이 변경됐습니다. 작업이 끝난 뒤 다시 확인하세요.")
    return raw, hashlib.sha256(raw).hexdigest()


def _object(raw, name):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StudyError("source_json", f"원본 {name}의 JSON 형식을 읽을 수 없습니다.") from exc
    if not isinstance(value, dict):
        _fail("source_json", f"원본 {name}은 JSON 객체여야 합니다.")
    return value


def _numeric_equal(a, b):
    return (not isinstance(a, bool) and isinstance(a, (int, float)) and math.isfinite(a)
            and math.isclose(float(a), float(b), rel_tol=1e-10, abs_tol=1e-10))


class DensityStudyManager:
    def __init__(self, research_root, jobs, store):
        base = Path(research_root).absolute()
        self.root = base / "density-studies"
        if base.resolve() != base or self.root.is_symlink():
            _fail("study_symlink", "밀도 연구 폴더에 심볼릭 링크를 사용할 수 없습니다.")
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs, self.store = jobs, store
        self.lock = threading.RLock()

    def _folder(self, identifier):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise KeyError(identifier)
        folder = self.root / identifier
        if self.root.is_symlink() or folder.is_symlink():
            _fail("study_symlink", "밀도 연구 폴더에 심볼릭 링크를 사용할 수 없습니다.")
        return folder

    def _read(self, identifier):
        folder = self._folder(identifier)
        raw, _ = _read_file(folder, "result.json", required=True)
        record = _object(raw, "result.json")
        if record.get("id") != identifier:
            _fail("study_identity", "밀도 연구 ID가 저장된 기록과 다릅니다.")
        return record

    def _write(self, record):
        atomic_json(self._folder(record["id"]) / "result.json", record)

    def create(self, name, template, minimum_replicates=3, max_replicates=5, max_relative_sd=None,
               absolute_tolerance_g_cm3=None):
        template = _config(template)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            _fail("invalid_name", "연구 이름은 비어 있지 않은 80자 이하 문자열이어야 합니다.")
        if (isinstance(minimum_replicates, bool) or isinstance(max_replicates, bool)
                or not 3 <= minimum_replicates <= max_replicates <= 10):
            _fail("replicate_budget", "최소 반복 수는 3 이상이며 전체 반복 기록 수는 10 이하여야 합니다.")
        precision = _positive(max_relative_sd, "상대 표준편차 기준", optional=True)
        tolerance = _positive(absolute_tolerance_g_cm3, "밀도 절대 허용 차이", optional=True)
        # Check chemistry/size without making engine availability a prerequisite for reading past results.
        from .preflight import check
        checked = check(template)
        if checked["errors"]:
            _fail("template_preflight", " ".join(error["message"] for error in checked["errors"]))
        identifier = uuid.uuid4().hex
        folder = self._folder(identifier)
        folder.mkdir()
        record = {"id": identifier, "schema_version": SCHEMA_VERSION, "record_type": "density_study",
                  "name": name.strip(), "created_at": now(), "property": "density", "unit": "g/cm^3",
                  "template": template, "condition_key": _condition_key(template),
                  "conditions": {"temperature_kelvin": template["temperature_kelvin"], "pressure_atm": 1.0},
                  "minimum_replicates": minimum_replicates, "max_replicates": max_replicates,
                  "max_relative_sd": precision, "absolute_tolerance_g_cm3": tolerance,
                  "entries": [], "reference": None, "assessment_ids": [], "latest_assessment_id": None,
                  "warnings": list(WARNINGS), "automatic_execution": False, "verified": False}
        with self.lock:
            self._write(record)
        return self.get(identifier)

    def _capture(self, job_id, study, baseline=None):
        if not isinstance(job_id, str) or not IDENTIFIER.fullmatch(job_id):
            raise KeyError(job_id)
        folder = self.jobs.directory(job_id)
        if self.jobs.root.resolve() != self.jobs.root.absolute() or folder.resolve() != folder.absolute():
            _fail("source_symlink", "연결 작업 폴더에 심볼릭 링크를 사용할 수 없습니다.")
        files, hashes = {}, {}
        for name in FILES:
            raw, sha = _read_file(folder, name, required=name == "job.json")
            files[name], hashes[name] = raw, sha
        job = _object(files["job.json"], "job.json")
        if job.get("id") != job_id or not isinstance(job.get("config"), dict):
            _fail("job_identity", "작업 ID와 설정을 원본 job.json에서 확인할 수 없습니다.")
        config = _config(job["config"])
        if _condition_key(config) != study["condition_key"]:
            _fail("condition_mismatch", "같은 재료·온도·초기 패킹·크기·힘장·전하·프로토콜의 작업만 연결하세요. 이름과 seed만 달라질 수 있습니다.")
        row = {"job_id": job_id, "status": job.get("status"), "seed": config["seed"], "eligible": False,
               "density": None, "equilibrated": None, "issues": [], "provenance": hashes,
               "config_sha256": digest(config), "verified": False}
        def issue(code, message):
            row["issues"].append({"code": code, "message": message})
        if files["config.json"] is not None and digest(_config(_object(files["config.json"], "config.json"))) != digest(config):
            issue("config_file_mismatch", "작업 설정과 원본 config.json이 일치하지 않습니다.")
        if baseline:
            for name, old in baseline.get("snapshot", {}).items():
                # Status/finish timestamps evolve normally before first completion.
                if name == "job.json" and baseline.get("snapshot_status") != "completed":
                    continue
                if old is not None and hashes[name] != old:
                    issue("source_changed", f"연결 이후 원본 {name}이 변경됐습니다.")
        if row["status"] != "completed":
            issue("execution_incomplete", "완료된 작업의 NPT 평균 밀도만 반복 통계에 사용합니다.")
            return row
        if not files["bulk_complete.json"] or not files["eq3_last.data"]:
            issue("source_missing", "bulk_complete.json과 최종 구조가 모두 있어야 합니다.")
            return row
        complete = _object(files["bulk_complete.json"], "bulk_complete.json")
        properties = complete.get("properties")
        if not isinstance(properties, dict) or not isinstance(properties.get("density"), dict):
            _fail("source_json", "원본 NPT 밀도 속성이 JSON 객체가 아닙니다.")
        prop = properties["density"]
        if prop.get("unit") != "g/cm^3":
            issue("density_unit", "NPT 평균 밀도의 단위 g/cm^3를 확인할 수 없습니다.")
        try:
            density = _positive(prop.get("value"), "NPT 평균 밀도")
            if not 1e-12 <= density <= 1e6:
                raise ValueError()
            row["density"] = density
        except ValueError:
            issue("density_value", "NPT 평균 밀도가 유효한 범위의 유한한 양수가 아닙니다.")
        checks = {"temperature_kelvin": config["temperature_kelvin"], "pressure_atm": 1.0,
                  "chains": config["chains"], "protocol_scale": config["protocol_scale"]}
        for name, expected in checks.items():
            if not _numeric_equal(complete.get(name), expected):
                issue("metadata_condition", f"원본 결과의 {name}이 고정된 연구 조건과 다르거나 없습니다.")
        forcefield, charge = str(complete.get("force_field", "")).lower(), str(complete.get("charge_method", "")).lower()
        if forcefield not in {"gaff2", "gaff2_mod"} or charge != config["charge_method"]:
            issue("metadata_model", "원본 결과의 GAFF2 힘장·전하 방법이 작업 설정과 일치하지 않습니다.")
        # Gasteiger is capitalized in original RadonPy records; aliases are not physical substitutions.
        if config["charge_method"] == "xtb_cm5" and charge in {"xtb cm5", "xtb_cm5"}:
            row["issues"] = [i for i in row["issues"] if i["code"] != "metadata_model" or forcefield not in {"gaff2", "gaff2_mod"}]
        if not isinstance(complete.get("ensemble"), str) or not (complete["ensemble"].strip().upper() == "NPT" or complete["ensemble"].strip().upper().startswith("NPT ")):
            issue("metadata_protocol", "원본의 NPT 앙상블을 확인할 수 없습니다.")
        timestep = complete.get("time_step_fs")
        if isinstance(timestep, bool) or not isinstance(timestep, (int, float)) or not math.isfinite(timestep) or timestep <= 0:
            issue("metadata_protocol", "원본의 유효한 시간 간격이 기록되지 않았습니다.")
        steps = complete.get("stage_steps")
        if (not isinstance(steps, (dict, list)) or not steps
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
                       for v in (steps.values() if isinstance(steps, dict) else steps))):
            issue("metadata_protocol", "원본의 단계별 계산 길이를 확인할 수 없습니다.")
        row["equilibrated"] = complete.get("equilibrated") if isinstance(complete.get("equilibrated"), bool) else None
        protocol = {key: complete.get(key) for key in ("force_field", "charge_method", "ensemble", "protocol", "protocol_scale",
                    "stage_steps", "time_step_fs", "temperature_kelvin", "pressure_atm", "chains", "atoms")}
        row["runtime_protocol_sha256"] = digest(protocol)
        warnings = complete.get("convergence_warnings")
        if warnings is None:
            warnings = []
        if not isinstance(warnings, list) or any(not isinstance(value, str) for value in warnings):
            issue("source_warnings", "원본의 수렴 경고는 문자열 목록이어야 합니다.")
            warnings = []
        row["result_warnings"] = list(warnings)
        row["eligible"] = not row["issues"]
        return row

    def attach(self, identifier, job_ids):
        if not isinstance(job_ids, list) or not job_ids or len(job_ids) != len(set(job_ids)):
            _fail("duplicate_job", "연결할 작업 ID는 비어 있지 않고 서로 달라야 합니다.")
        with self.lock:
            study = self._read(identifier)
            old = {entry["job_id"] for entry in study["entries"]}
            if old & set(job_ids):
                _fail("duplicate_job", "이미 연결한 작업입니다. 같은 작업을 반복으로 세지 않습니다.")
            if len(old) + len(job_ids) > study["max_replicates"]:
                _fail("replicate_budget", "연결할 작업 수가 이 연구의 최대 반복 기록 수를 넘습니다.")
            captured = [self._capture(job_id, study) for job_id in job_ids]
            seeds = [entry["seed"] for entry in study["entries"]] + [row["seed"] for row in captured]
            if len(seeds) != len(set(seeds)):
                _fail("duplicate_seed", "같은 seed의 작업을 독립 반복으로 연결할 수 없습니다.")
            study["entries"].extend({"job_id": row["job_id"], "seed": row["seed"], "attached_at": now(),
                "snapshot_status": row["status"], "snapshot": row["provenance"]} for row in captured)
            self._write(study)
        return self.get(identifier)

    def _measurement(self, identifier):
        return next((row for row in self.store.list_measurements() if row["id"] == identifier), None)

    def reference(self, identifier, measurement_id, standard_uncertainty=None):
        uncertainty = _positive(standard_uncertainty, "대조값의 표준 불확도", optional=True)
        if uncertainty is not None and uncertainty > 1e6:
            _fail("reference_uncertainty_range", "표준 불확도는 Rust 계산 범위인 10^6 g/cm³ 이하여야 합니다.")
        measurement = self._measurement(measurement_id)
        if measurement is None:
            raise KeyError(measurement_id)
        with self.lock:
            study = self._read(identifier)
            study["reference"] = {"measurement_id": measurement_id, "standard_uncertainty": uncertainty,
                                  "snapshot_sha256": digest(measurement), "linked_at": now()}
            self._write(study)
        return self.get(identifier)

    def _reference(self, study):
        linked = study.get("reference")
        result = {"measurement_id": linked["measurement_id"] if linked else None, "eligible": False,
                  "issues": [], "record": None, "standard_uncertainty": linked.get("standard_uncertainty") if linked else None}
        def issue(code, message):
            result["issues"].append({"code": code, "message": message})
        if not linked:
            issue("reference_missing", "같은 재료·온도·압력의 실험 또는 문헌 대조값을 연결하세요.")
            return result
        row = self._measurement(linked["measurement_id"])
        if row is None:
            issue("reference_missing", "연결한 대조 기록이 없습니다.")
            return result
        result.update(record=row, snapshot_sha256=digest(row))
        if digest(row) != linked["snapshot_sha256"]:
            issue("reference_changed", "대조 기록이 연결 이후 변경됐습니다. 검토 후 다시 연결하세요.")
        if row.get("property") != "density" or row.get("unit") != "g/cm^3" or row.get("origin") not in {"experimental", "literature"}:
            issue("reference_scope", "대조는 g/cm^3 밀도의 실험 또는 문헌 기록이어야 합니다.")
        if row.get("verified") is not True:
            issue("reference_unreviewed", "사람이 검토한 대조 기록이 필요합니다. 이 기능은 검토 상태를 바꾸지 않습니다.")
        material = row.get("material") if isinstance(row.get("material"), dict) else {}
        try:
            matches = _canonical(material["repeat_smiles"]) == study["template"]["repeat_smiles"]
        except (KeyError, ValueError):
            matches = False
        if not matches:
            issue("reference_material", "대조의 반복단위 재료가 연구 재료와 다릅니다.")
        conditions = row.get("conditions") if isinstance(row.get("conditions"), dict) else {}
        for key, expected in study["conditions"].items():
            if not _numeric_equal(conditions.get(key), expected):
                issue("reference_conditions", f"대조의 {key}이 없거나 연구 조건과 다릅니다. 값을 추정하지 않습니다.")
        try:
            value = _positive(row.get("value"), "대조 밀도")
            if not 1e-12 <= value <= 1e6:
                raise ValueError()
        except ValueError:
            issue("reference_value", "대조 밀도가 유효한 양수가 아닙니다.")
        result["eligible"] = not result["issues"]
        return result

    def assess(self, identifier):
        import workbench_native as native
        from .result_quality import build_quality
        if not hasattr(native, "replicate_summary"):
            raise ImportError("밀도 반복 통계를 위한 Rust 커널을 다시 빌드하세요.")
        with self.lock:
            study = self._read(identifier)
            records = []
            for entry in study["entries"]:
                try:
                    row = self._capture(entry["job_id"], study, entry)
                except (StudyError, KeyError, OSError, ValueError) as exc:
                    row = {"job_id": entry["job_id"], "status": "unknown", "seed": entry["seed"], "eligible": False,
                           "density": None, "equilibrated": None, "provenance": {},
                           "issues": [{"code": getattr(exc, "code", "source_invalid"), "message": str(exc)}], "verified": False}
                if row["status"] == "completed" and entry["snapshot_status"] != "completed" and not any(i["code"] == "source_changed" for i in row["issues"]):
                    entry.update(snapshot_status="completed", snapshot=row["provenance"])
                records.append(row)
            candidates = [row for row in records if row["eligible"]]
            structure_hashes = [row["provenance"]["eq3_last.data"] for row in candidates]
            if len(structure_hashes) != len(set(structure_hashes)):
                for row in candidates:
                    row["issues"].append({"code": "duplicate_structure", "message": "같은 최종 구조가 반복되어 독립 준비 근거가 부족합니다."})
                    row["eligible"] = False
            if len({row["runtime_protocol_sha256"] for row in candidates}) > 1:
                for row in candidates:
                    row["issues"].append({"code": "runtime_protocol_mismatch", "message": "실제 실행 프로토콜이 다른 작업은 하나의 반복 통계로 합치지 않습니다."})
                    row["eligible"] = False
            eligible = [row for row in records if row["eligible"]]
            reference = self._reference(study)
            statistics = native.replicate_summary(np.array([row["density"] for row in eligible], dtype=float),
                reference["record"]["value"] if reference["eligible"] else None,
                reference["standard_uncertainty"] if reference["eligible"] else None) if eligible else None
            minimum = study["minimum_replicates"]
            count = len(eligible)
            converged = sum(row["equilibrated"] is True for row in eligible)
            source_ok = not any(i["code"] != "execution_incomplete" for row in records for i in row["issues"])
            execution_ok = len(records) >= minimum and all(row["status"] == "completed" for row in records)
            repeat_status = "pending"
            repeat_message = "같은 조건에서 독립적으로 준비한 반복을 모으고 산포 기준을 지정하세요."
            if count >= minimum and study["max_relative_sd"] is not None:
                repeat_status = "passed" if statistics["relative_sd"] <= study["max_relative_sd"] else "failed"
                repeat_message = "정한 상대 표준편차 기준을 만족합니다. 독립성 자체의 증명은 아닙니다." if repeat_status == "passed" else "반복 간 산포가 정한 기준보다 큽니다."
            zero_spread = bool(statistics and statistics.get("sample_sd") == 0.0)
            if zero_spread:
                repeat_status = "pending"
                repeat_message = "반복값이 모두 같습니다. 반올림·출력 분해능·같은 입력 재사용을 확인해야 하며 0 산포를 정밀도의 증거로 쓰지 않습니다."
            comparison = statistics.get("reference_comparison") if statistics else None
            compare_status, compare_message = "pending", "조건이 일치하는 검토된 대조값·표준 불확도·허용 차이가 필요합니다."
            if (reference["eligible"] and comparison and reference["standard_uncertainty"] is not None
                    and study["absolute_tolerance_g_cm3"] is not None and count >= minimum):
                compare_status = "passed" if abs(comparison["value_difference"]) <= study["absolute_tolerance_g_cm3"] else "failed"
                compare_message = "정한 절대 허용 차이 안입니다. 물성 검증 통과를 뜻하지 않습니다." if compare_status == "passed" else "실험·문헌 대조값과 차이가 정한 허용 범위보다 큽니다."
            checks = [
                {"key": "execution", "status": "failed" if any(r["status"] in {"failed", "blocked", "cancelled", "interrupted"} for r in records) else "passed" if execution_ok else "pending", "title": "실행 완료", "detail": "계획한 반복의 실행 완료 여부이며 수렴과는 다릅니다.", "evidence": {"attached": len(records), "minimum": minimum, "completed": sum(r["status"] == "completed" for r in records)}},
                {"key": "provenance", "status": "failed" if not source_ok else "passed" if records and count == len(records) else "pending", "title": "입력·출처 일치", "detail": "조건·단위·원본 해시가 같은 연구 범위인지 확인합니다.", "evidence": {"excluded": len(records) - count}},
                {"key": "convergence", "status": "passed" if count >= minimum and converged == count else "failed" if any(r["equilibrated"] is False for r in eligible) else "unknown", "title": "수렴 판정", "detail": "원본의 명시적 수렴 판정만 사용합니다. 판정 자체가 과학적 타당성을 보증하지 않습니다.", "evidence": {"converged": converged, "eligible": count}},
                {"key": "independent_repeatability", "code": "zero_spread" if zero_spread else "independent_repeatability", "status": repeat_status, "title": "독립 반복의 재현성", "detail": repeat_message, "evidence": {"count": count, "minimum": minimum, "relative_sd_limit": study["max_relative_sd"]}},
                {"key": "reference_comparison", "status": compare_status, "title": "실험·문헌 대조", "detail": compare_message, "evidence": {"measurement_id": reference["measurement_id"], "reference_eligible": reference["eligible"], "absolute_tolerance_g_cm3": study["absolute_tolerance_g_cm3"]}},
                {"key": "review", "status": "pending", "title": "사람 검토", "detail": "계산·반복·대조 기준을 만족해도 결과는 검토 전 시뮬레이션으로 유지합니다.", "evidence": {"verified": False}},
            ]
            next_checks = []
            if not source_ok:
                next_checks.append({"code": "inspect_sources", "message": "제외된 작업의 변경·조건·단위·중복 구조를 확인하고 별도 연구로 나누세요.", "additional_runs": 0})
            if any(row["equilibrated"] is False for row in eligible):
                next_checks.append({"code": "convergence_plan", "message": "미수렴 원인과 시계열을 검토하고 더 긴 계산의 별도 계획·예산을 정하세요. 여기서는 재실행하지 않습니다.", "additional_runs": minimum, "reason": "미수렴 자료를 더 모아도 수렴 근거가 되지 않습니다."})
            if count < minimum:
                next_checks.append({"code": "independent_replicates", "message": "같은 조건에서 새 seed로 독립 준비한 반복이 더 필요합니다.", "additional_runs": minimum - count})
            if zero_spread:
                next_checks.append({"code": "review_zero_spread", "message": "원본 밀도 출력의 유효 자릿수·반올림과 구조/입력 재사용을 확인한 뒤 반복 정밀도를 판단하세요.", "additional_runs": 0, "reason": "동일한 반복값의 0 산포는 실험·계산 정밀도를 입증하지 않습니다."})
            if repeat_status == "failed":
                next_checks.append({"code": "repeatability_investigation", "message": "검체 준비·초기 구조·시계열 산포를 확인하고 독립 반복 또는 계산 길이 비교의 별도 예산을 정하세요. 큰 산포의 작업을 자동 제외하지 않습니다.", "additional_runs": minimum, "reason": "반복 간 산포가 정한 기준을 넘었습니다."})
            if study["max_relative_sd"] is None:
                next_checks.append({"code": "repeatability_criterion", "message": "연구 목적에 맞는 반복 산포 기준을 정하세요. 자동 기준은 넣지 않습니다.", "additional_runs": 0})
            if compare_status != "passed":
                next_checks.append({"code": "reference_evidence", "message": "같은 재료·온도·압력의 검토된 대조값과 표준 불확도·허용 차이를 확인하세요.", "additional_runs": 0})
            next_checks.append({"code": "human_review", "message": "원자료와 모델 적용 범위를 사람이 검토하세요. 이 기능은 결과를 승인하지 않습니다.", "additional_runs": 0})
            quality = build_quality(checks, next_checks)
            from .paths import resource_root
            compiled = sys.modules.get(native.replicate_summary.__module__, native)
            compiled_path = Path(compiled.__file__)
            source_path = resource_root() / "workbench" / "density_studies.py"
            def sha(path):
                if not path.is_file():
                    return None
                with path.open("rb") as stream:
                    return hashlib.file_digest(stream, "sha256").hexdigest()
            aid = uuid.uuid4().hex
            assessment = {"id": aid, "study_id": identifier, "schema_version": SCHEMA_VERSION, "created_at": now(),
                          "property": "density", "unit": "g/cm^3", "method": "NPT sampling mean from bulk_complete.json",
                          "contract_sha256": digest({key: value for key, value in study.items() if key not in {"entries", "reference", "assessment_ids", "latest_assessment_id"}}),
                          "records": records, "excluded": [{"job_id": row["job_id"], "reasons": row["issues"]} for row in records if not row["eligible"]],
                          "statistics": statistics, "statistics_scope": "source-valid run means, including any unconverged runs; interpret quality gates separately",
                          "reference": reference, "quality": quality, "verified": False,
                          "kernel": {"engine": "rust", "version": native.__version__, "sha256": sha(compiled_path)},
                          "provenance": {"analysis_code_sha256": sha(source_path)}, "warnings": list(WARNINGS) + (statistics.get("warnings", []) if statistics else [])}
            folder = self._folder(identifier) / "assessments"
            if folder.is_symlink():
                _fail("study_symlink", "평가 폴더에 심볼릭 링크를 사용할 수 없습니다.")
            folder.mkdir(exist_ok=True)
            atomic_json(folder / (aid + ".json"), assessment)
            study["assessment_ids"].append(aid)
            study["latest_assessment_id"] = aid
            self._write(study)
        return assessment

    def assessment_path(self, identifier, assessment_id):
        study = self._read(identifier)
        if assessment_id not in study["assessment_ids"] or not IDENTIFIER.fullmatch(assessment_id):
            raise KeyError(assessment_id)
        folder = self._folder(identifier) / "assessments"
        if folder.is_symlink():
            _fail("study_symlink", "평가 폴더에 심볼릭 링크를 사용할 수 없습니다.")
        _read_file(folder, assessment_id + ".json", required=True)
        return folder / (assessment_id + ".json")

    def get(self, identifier):
        with self.lock:
            study = self._read(identifier)
            used = {entry["seed"] for entry in study["entries"]}
            configs = []
            seed = study["template"]["seed"]
            for index in range(max(0, study["minimum_replicates"] - len(study["entries"]))):
                while seed in used:
                    seed = seed % 899999998 + 1
                config = dict(study["template"], name=f"{study['name']} · 독립 반복 {index + 1}"[:120], seed=seed)
                configs.append(config)
                used.add(seed)
                seed = seed % 899999998 + 1
            study["planned_configs"] = configs
            study["job_ids"] = [entry["job_id"] for entry in study["entries"]]
            study["latest_assessment"] = (json.loads(self.assessment_path(identifier, study["latest_assessment_id"]).read_text())
                                           if study["latest_assessment_id"] else None)
            study["download_url"] = f"/api/research/density-studies/{identifier}/download"
            return study

    def list(self):
        return [self.get(folder.name) for folder in sorted(self.root.iterdir()) if IDENTIFIER.fullmatch(folder.name) and not folder.is_symlink()]
