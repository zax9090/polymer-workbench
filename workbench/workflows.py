"""Review-gated DOE -> bulk -> geometric observation -> response surface workflows."""
from __future__ import annotations
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import threading
import uuid

from fastapi import HTTPException

from .jobs import TERMINAL, atomic_json, now
from .models import JobConfig
from .execution import ExecutionCancelled

ID = re.compile(r"[0-9a-f]{32}\Z")
FIELDS = {"temperature_kelvin": {"label": "온도", "unit": "K", "role": "physical_condition"},
          "build_density": {"label": "초기 패킹 밀도", "unit": "g/cm^3", "role": "preparation_condition"}}
METRICS = {"final_frame_density": "g/cm^3", "probe_accessible_fraction": "1"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_record(root, identifier):
    if not ID.fullmatch(identifier):
        raise KeyError(identifier)
    path = root / identifier / "result.json"
    if root.resolve() != root or root.is_symlink() or path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise KeyError(identifier)
    return json.loads(path.read_text())


def config_digest(config):
    return digest({key: value for key, value in JobConfig.model_validate(config).model_dump().items() if key != "name"})


def preflight(template):
    from .external import available, executable
    import os
    import sys
    missing = []
    if os.environ.get("RADONPY_PYTHON", sys.executable) == sys.executable and not available("radonpy"):
        missing.append("RadonPy")
    if not executable("lmp", "LAMMPS_EXEC"):
        missing.append("LAMMPS")
    if not available("rdkit"):
        missing.append("RDKit")
    if template["charge_method"] == "xtb_cm5":
        from .engines import status
        if not status("xtb").get("available"):
            missing.append("xtb")
    return missing


class WorkflowManager:
    def __init__(self, research_root, jobs):
        self.research_root = Path(research_root)
        self.root = self.research_root / "workflows"
        if self.root.is_symlink():
            raise ValueError("연구 흐름 폴더에 링크를 사용할 수 없습니다.")
        self.root.mkdir(exist_ok=True)
        self.jobs = jobs
        self.lock = threading.RLock()
        self.cancel_events = {}
        self.closed = False
        for path in self.root.glob("*/result.json"):
            try:
                record = self._read(path.parent.name)
                if record.get("busy"):
                    record.update(busy=False, state="interrupted", error="서버가 종료됐습니다. 확인 후 해당 단계를 다시 실행하세요.")
                    self._save(record)
            except (KeyError, ValueError, OSError):
                continue

    def _read(self, identifier):
        return read_record(self.root, identifier)

    def _save(self, record):
        folder = self.root / record["id"]
        if folder.is_symlink():
            raise ValueError("연구 흐름 폴더에 링크를 사용할 수 없습니다.")
        folder.mkdir(exist_ok=True)
        history = folder / "revisions"
        if history.is_symlink():
            raise ValueError("기록 폴더에 링크를 사용할 수 없습니다.")
        history.mkdir(exist_ok=True)
        revision = record.get("revision", 0) + 1
        # An interrupted write may have left a complete historical revision.
        while (history / f"{revision:08}.json").exists():
            revision += 1
        record.update(revision=revision, updated_at=now())
        atomic_json(history / f"{revision:08}.json", record)
        atomic_json(folder / "result.json", record)

    def create(self, design_id, template, bindings, metric, probe=1.0, spacing=0.25, budget=50):
        design = read_record(self.research_root / "doe", design_id)
        if design.get("record_type") != "doe_design":
            raise ValueError("저장된 DOE 설계 기록을 선택하세요.")
        names = [f["name"] for f in design["factors"]]
        if len(names) != 2 or set(bindings) != set(names) or set(bindings.values()) != set(FIELDS):
            raise ValueError("두 인자를 온도와 초기 패킹 밀도에 각각 한 번 연결하세요.")
        template = JobConfig.model_validate(template).model_dump()
        if (template["route"], template["atomistic_mode"], template["atomistic_forcefield"], template["cell_builder"]) != ("polymer_atomistic", "bulk", "gaff2", "native"):
            raise ValueError("초기 패킹 밀도가 실제 적용되는 GAFF2 벌크·자체 셀 빌더를 선택하세요.")
        if template["engine_input"] or template["source_job_id"]:
            raise ValueError("연구 흐름에는 임의 엔진 입력이나 이전 구조 연결을 사용할 수 없습니다.")
        if metric not in METRICS:
            raise ValueError("지원하는 반응값을 선택하세요.")
        if not 1 <= len(design["runs"]) <= budget <= 50:
            raise ValueError("전체 설계와 재시도·확인 계산을 포함해 계산 예산은 최대 50개입니다.")
        rows = []
        for run in design["runs"]:
            config = dict(template)
            config.update({bindings[name]: run["values"][name] for name in names})
            config["seed"] = (template["seed"] + run["run_order"] - 1) % 899999998 + 1
            config["name"] = f"DOE {run['run_order']} · {template['name']}"[:120]
            config = JobConfig.model_validate(config).model_dump()
            rows.append({"run_order": run["run_order"], "factors": run["values"], "config": config,
                         "config_sha256": config_digest(config), "job_id": uuid.uuid4().hex,
                         "attempts": [], "observation": None, "state": "planned", "error": None})
        identifier = uuid.uuid4().hex
        record = {"id": identifier, "schema_version": 1, "record_type": "bulk_workflow", "created_at": now(),
                  "design_id": design_id, "design_sha256": digest(design), "factors": design["factors"], "bindings": bindings,
                  "template": template, "metric": metric, "unit": METRICS[metric], "budget": budget, "submitted": 0,
                  "analysis_parameters": {"probes": [float(probe)], "spacing": float(spacing), "frame": "last"},
                  "rows": rows, "optimizations": [], "confirmations": [], "busy": False, "state": "planned",
                  "warnings": ["Initial packing density is a preparation parameter, not an equilibrium material property.",
                               "Final-frame density and geometric probe accessibility do not establish equilibrium or transport connectivity.",
                               "Every recommendation requires independent confirmation runs. Simulation results are not verified experimental measurements."]}
        with self.lock:
            self._save(record)
        return self.get(identifier)

    def _job(self, identifier):
        with self.jobs.lock:
            return self.jobs._read(identifier)

    def get(self, identifier):
        with self.lock:
            record = self._read(identifier)
        for row in record["rows"]:
            if row["attempts"]:
                try:
                    row["job_status"] = self._job(row["job_id"])["status"]
                except KeyError:
                    row["job_status"] = "missing"
        for confirmation in record["confirmations"]:
            try:
                confirmation["job_status"] = self._job(confirmation["job_id"])["status"]
            except KeyError:
                confirmation["job_status"] = "missing"
        record["missing_engines"] = preflight(record["template"])
        record["download_url"] = f"/api/research/workflows/{identifier}/download"
        return record

    def list(self):
        with self.lock:
            records = []
            for path in self.root.glob("*/result.json"):
                try:
                    record = self._read(path.parent.name)
                    records.append({key: record[key] for key in ("id", "created_at", "state", "metric", "revision")})
                except (KeyError, ValueError, OSError):
                    continue
            return sorted(records, key=lambda r: r["created_at"], reverse=True)

    def _available(self, record):
        if self.closed or record["busy"]:
            raise RuntimeError("이 연구 흐름의 단계가 진행 중입니다. 완료하거나 취소한 후 실행하세요.")

    @staticmethod
    def _rows(record, selected):
        if not selected or len(set(selected)) != len(selected):
            raise ValueError("중복 없이 실행할 행을 선택하세요.")
        rows = {row["run_order"]: row for row in record["rows"]}
        if not set(selected) <= set(rows):
            raise ValueError("설계에 없는 행입니다.")
        return [rows[number] for number in selected]

    def submit(self, identifier, selected, retry_failed=False):
        with self.lock:
            record = self._read(identifier)
            self._available(record)
            missing = preflight(record["template"])
            if missing:
                raise RuntimeError("계산 엔진이 필요합니다: " + ", ".join(missing))
            rows = self._rows(record, selected)
            pending = []
            for row in rows:
                if row["attempts"]:
                    try:
                        job = self._job(row["job_id"])
                    except KeyError:
                        job = {"status": "missing"}
                    if not retry_failed or job["status"] not in {"failed", "blocked", "cancelled", "interrupted", "missing"}:
                        continue
                    row["job_id"] = uuid.uuid4().hex
                    row["observation"] = None
                pending.append(row)
            if record["submitted"] + len(pending) > record["budget"]:
                raise ValueError("재시도·확인 계산을 포함한 실행 예산을 초과합니다.")
            # Reserve all IDs before starting workers. Repeated submissions reuse those IDs.
            for row in pending:
                row["attempts"].append(row["job_id"])
                row.update(state="submitted", error=None)
            record["submitted"] += len(pending)
            record["state"] = "submitted"
            self._save(record)
            for row in pending:
                try:
                    self.jobs.create(row["config"], submitted_via="workflow:" + identifier, job_id=row["job_id"])
                except Exception as exc:
                    row.update(state="submission_failed", error=str(exc))
                    self._save(record)
                    raise
        return self.get(identifier)

    def attach(self, identifier, run_order, job_id):
        with self.lock:
            record = self._read(identifier)
            self._available(record)
            row = self._rows(record, [run_order])[0]
            if row["attempts"]:
                if row["job_id"] == job_id:
                    return self.get(identifier)
                raise ValueError("이미 작업이 연결된 행입니다. 새 흐름에서 연결하세요.")
            if any(job_id in r["attempts"] for r in record["rows"]):
                raise ValueError("한 계산을 서로 다른 독립 반복 행에 중복 사용할 수 없습니다.")
            job = self._job(job_id)
            if job["status"] != "completed" or config_digest(job["config"]) != row["config_sha256"]:
                raise ValueError("완료된 작업의 인자·시드·힘장·나머지 설정이 설계 행과 일치해야 합니다.")
            row.update(job_id=job_id, attempts=[job_id], state="submitted")
            self._save(record)
        return self.get(identifier)

    def _begin(self, identifier, state):
        with self.lock:
            record = self._read(identifier)
            self._available(record)
            event = threading.Event()
            self.cancel_events[identifier] = event
            record.update(busy=True, state=state, error=None)
            self._save(record)
            return record, event

    def _finish(self, record, event, state, error=None):
        with self.lock:
            record.update(busy=False, state="cancelled" if event.is_set() else state, error=error)
            self._save(record)
            self.cancel_events.pop(record["id"], None)

    def analyze(self, identifier, selected):
        record, event = self._begin(identifier, "analyzing")
        try:
            from .analysis_service import analyze_structure
            for row in self._rows(record, selected):
                if event.is_set():
                    break
                try:
                    job = self._job(row["job_id"])
                    if not row["attempts"] or job["status"] != "completed":
                        raise ValueError("완료된 계산이 필요합니다.")
                    if config_digest(job["config"]) != row["config_sha256"]:
                        raise ValueError("설계와 계산 설정이 다릅니다.")
                    result = analyze_structure(self.jobs, self.research_root, row["job_id"], "free_volume",
                        record["analysis_parameters"], owner="workflow:" + identifier, cancelled=event.is_set)
                    value = result["density_g_cm3"] if record["metric"] == "final_frame_density" else result["probes"][0]["accessible_fraction"]
                    if not math.isfinite(value):
                        raise ValueError("반응값이 유한하지 않습니다.")
                    simulation = self.jobs.get(row["job_id"]).get("result") or {}
                    observation = {"value": value, "unit": record["unit"], "method": record["metric"],
                        "analysis_id": result["record_id"], "analysis_sha256": digest(result), "job_id": row["job_id"],
                        "config_sha256": row["config_sha256"], "warnings": result["warnings"] + simulation.get("warnings", []), "verified": False}
                    row.setdefault("observation_history", []).append(observation)
                    row.update(observation=observation, state="observed", error=None)
                except ExecutionCancelled:
                    event.set()
                    row.update(state="interrupted", error="분석이 취소되었습니다.")
                    break
                except (ValueError, KeyError, HTTPException, RuntimeError) as exc:
                    row.update(state="analysis_failed", error=str(getattr(exc, "detail", exc)))
                with self.lock:
                    self._save(record)
            self._finish(record, event, "review_results")
        except Exception as exc:
            self._finish(record, event, "failed", str(exc))
            raise
        return self.get(identifier)

    def _validate_observation(self, record, row):
        observation = row["observation"]
        if observation is None or row["state"] != "observed":
            raise ValueError(f"{row['run_order']}행 반응값이 없습니다. 제외 사유를 명시하거나 계산·분석을 완료하세요.")
        result = read_record(self.research_root / "analyses", observation["analysis_id"])
        expected_value = result["density_g_cm3"] if record["metric"] == "final_frame_density" else result["probes"][0]["accessible_fraction"]
        if (digest(result) != observation["analysis_sha256"] or result["provenance"]["job_id"] != row["job_id"]
                or not result["provenance"].get("kernel_sha256") or not result["provenance"].get("analysis_code_sha256")
                or observation["value"] != expected_value or observation["job_id"] != row["job_id"]
                or result["analysis"] != "free_volume"
                or result["provenance"]["parameters"] != record["analysis_parameters"]
                or observation["unit"] != METRICS[record["metric"]] or observation["method"] != record["metric"]
                or config_digest(self._job(row["job_id"])["config"]) != row["config_sha256"]):
            raise ValueError("연결된 분석의 출처·단위·방법·작업 설정이 변경됐습니다.")
        directory = self.jobs.directory(row["job_id"])
        for name, expected in result["provenance"]["source_sha256"].items():
            path = directory / name
            if path.is_symlink() or path.parent != directory or not path.is_file():
                raise ValueError("원본 계산 파일을 확인할 수 없습니다.")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError("원본 계산 파일이 변경됐습니다. 다시 분석하세요.")
        return observation

    def optimize(self, identifier, model, objective, target, exclusions):
        record, event = self._begin(identifier, "optimizing")
        try:
            excluded = {int(number): reason.strip() for number, reason in exclusions.items()}
            known = {row["run_order"] for row in record["rows"]}
            if not set(excluded) <= known or any(not reason for reason in excluded.values()):
                raise ValueError("제외할 행과 제외 사유를 모두 명시하세요.")
            stream = io.StringIO()
            names = [factor["name"] for factor in record["factors"]]
            writer = csv.writer(stream)
            writer.writerow([*names, "response"])
            observations = []
            analysis_versions = set()
            for row in record["rows"]:
                if row["run_order"] in excluded:
                    continue
                observation = self._validate_observation(record, row)
                analysis_record = read_record(self.research_root / "analyses", observation["analysis_id"])
                provenance = analysis_record["provenance"]
                analysis_versions.add((provenance["kernel_sha256"], provenance["analysis_code_sha256"]))
                if len(analysis_versions) > 1:
                    raise ValueError("분석 코드·커널 버전이 다른 결과를 섞을 수 없습니다. 같은 버전으로 다시 분석하세요.")
                observations.append({"run_order": row["run_order"], **observation})
                writer.writerow([*(row["factors"][name] for name in names), observation["value"]])
            text = stream.getvalue()
            result = self.jobs.execution.compute("doe_analyze", {"text": text, "factors": record["factors"],
                "response": "response", "model": model, "objective": objective, "target": target},
                owner="workflow:" + identifier, cancelled=event.is_set)
            if event.is_set():
                raise ExecutionCancelled("최적화가 취소되었습니다.")
            result.update(id=uuid.uuid4().hex, created_at=now(), workflow_id=identifier, unit=record["unit"],
                          metric=record["metric"], observations=observations, exclusions=excluded,
                          input_sha256=hashlib.sha256(text.encode()).hexdigest())
            folder = self.root / identifier / "optimizations"
            folder.mkdir(exist_ok=True)
            atomic_json(folder / (result["id"] + ".json"), result)
            record["optimizations"].append(result)
            self._finish(record, event, "recommendation_ready")
        except Exception as exc:
            self._finish(record, event, "review_results", str(exc))
            raise
        return self.get(identifier)

    def confirm(self, identifier, optimization_id, retry_failed=False):
        with self.lock:
            record = self._read(identifier)
            self._available(record)
            previous = [item for item in record["confirmations"] if item["optimization_id"] == optimization_id]
            if previous:
                try:
                    status = self._job(previous[-1]["job_id"])["status"]
                except KeyError:
                    status = "missing"
                if not retry_failed or status not in {"failed", "blocked", "cancelled", "interrupted", "missing"}:
                    return self.get(identifier)
            result = next((r for r in record["optimizations"] if r["id"] == optimization_id), None)
            if not result:
                raise KeyError(optimization_id)
            if record["submitted"] >= record["budget"]:
                raise ValueError("확인 계산을 위한 예산이 남아 있지 않습니다.")
            missing = preflight(record["template"])
            if missing:
                raise RuntimeError("계산 엔진이 필요합니다: " + ", ".join(missing))
            config = dict(record["template"])
            for name, value in result["recommendation"]["settings"].items():
                config[record["bindings"][name]] = value
            config["seed"] = (config["seed"] + len(record["rows"]) + len(record["confirmations"])) % 899999998 + 1
            config["name"] = "DOE 추천 확인 · " + identifier[:8]
            config = JobConfig.model_validate(config).model_dump()
            job_id = uuid.uuid4().hex
            record["confirmations"].append({"optimization_id": optimization_id, "job_id": job_id, "config": config, "validated": False})
            record["submitted"] += 1
            self._save(record)
            self.jobs.create(config, submitted_via="workflow-confirm:" + identifier, job_id=job_id)
        return self.get(identifier)

    def analyze_confirmation(self, identifier, job_id):
        record, event = self._begin(identifier, "analyzing")
        try:
            confirmation = next((item for item in record["confirmations"] if item["job_id"] == job_id), None)
            if not confirmation:
                raise KeyError(job_id)
            if config_digest(self._job(job_id)["config"]) != config_digest(confirmation["config"]):
                raise ValueError("확인 계산의 설정이 변경됐습니다.")
            from .analysis_service import analyze_structure
            result = analyze_structure(self.jobs, self.research_root, job_id, "free_volume", record["analysis_parameters"],
                owner="workflow:" + identifier, cancelled=event.is_set)
            value = result["density_g_cm3"] if record["metric"] == "final_frame_density" else result["probes"][0]["accessible_fraction"]
            optimization = next(item for item in record["optimizations"] if item["id"] == confirmation["optimization_id"])
            observation = {"value": value, "unit": record["unit"], "analysis_id": result["record_id"],
                           "analysis_sha256": digest(result), "prediction_error": value - optimization["recommendation"]["predicted_response"],
                           "warnings": result["warnings"], "verified": False}
            confirmation.setdefault("observation_history", []).append(observation)
            confirmation["observation"] = observation
            self._finish(record, event, "confirmation_review")
        except Exception as exc:
            self._finish(record, event, "recommendation_ready", str(exc))
            raise
        return self.get(identifier)

    def cancel(self, identifier):
        with self.lock:
            record = self._read(identifier)
            event = self.cancel_events.get(identifier)
            if event:
                event.set()
            self.jobs.execution.cancel(owner="workflow:" + identifier)
            for job_id in [r["job_id"] for r in record["rows"] if r["attempts"]] + [r["job_id"] for r in record["confirmations"]]:
                try:
                    self.jobs.cancel(job_id)
                except KeyError:
                    pass
            # The running stage owns its record and publishes its final cancellation state.
            if not event:
                record["state"] = "cancelled"
                self._save(record)
        return self.get(identifier)

    def close(self):
        with self.lock:
            self.closed = True
            for event in self.cancel_events.values():
                event.set()
