"""Finite, explicitly started research campaigns over existing validated JobConfigs."""
from __future__ import annotations

import importlib.metadata
import json
import math
from pathlib import Path
import threading
import uuid

import numpy as np

from .jobs import TERMINAL
from .learning import PROPERTIES, digest, finite_number, molecular_features, save_json, stable_json, utcnow, _regressor, _predict_gp
from .models import JobConfig

FEATURE_OBJECTIVES = {
    "rg_mean": {"sigma", "angstrom"}, "rg_final": {"sigma", "angstrom"}, "rg_std": {"sigma", "angstrom"},
    "end_to_end_mean": {"sigma", "angstrom"}, "end_to_end_std": {"sigma", "angstrom"},
    "adjacent_tangent_correlation": {"dimensionless"}, "backbone_dihedral_entropy": {"nats"},
    "dipole_fluctuation": {"e*angstrom"}, "potential_energy_mean": {"epsilon", "kcal/mol", "kJ/mol"},
    "contact_count": {"count"}, "hbond_count": {"count"}, "protein_rmsd": {"angstrom"},
}
COARSE = {"polymer_cg", "hoomd"}
ACTIVE = {"queued", "running", "cancelling"}
VARIABLES = {"monomers", "chains", "temperature", "temperature_kelvin", "box_nm", "repeat_smiles", "name"}


def backend_capabilities():
    output = []
    for package, identifier in (("scikit-learn", "sklearn"), ("baybe", "baybe")):
        try:
            installed = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            installed = None
        output.append({"id": identifier, "available": installed is not None, "version": installed,
                       "detail": "실행 시 API 및 의존성을 확인합니다." if installed else "별도 설치 필요"})
    return output


def _effective_config(config):
    if config["route"] in COARSE:
        keys = ("route", "monomers", "chains", "steps", "temperature", "timestep", "seed", "engine", "engine_input")
        return {key: config[key] for key in keys if key in config}
    return {key: value for key, value in config.items() if key != "name"}


def _vector(config):
    route = config["route"]
    if route in COARSE:
        # Repeat SMILES has no physical mapping in the generic bead model.
        return [float(config[key]) for key in ("monomers", "chains", "temperature")]
    _, molecular = molecular_features(config["repeat_smiles"])
    return molecular + [float(config[key]) for key in ("monomers", "chains", "temperature_kelvin", "box_nm")]


class CampaignManager:
    def __init__(self, root: Path, job_manager, store):
        self.root = Path(root).resolve() / "campaigns"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs, self.store = job_manager, store
        self.lock = threading.RLock()
        self.closed = False
        self.threads = {}
        self.stop_events = {}
        # Restart never silently restarts automatic calculations.
        for path in self.root.glob("*.json"):
            if path.is_symlink():
                continue
            try:
                record = json.loads(path.read_text())
                for candidate in record.get("candidates", []):
                    if candidate.get("status") == "submitting" and not candidate.get("job_id"):
                        candidate.update(status="interrupted", objective_status="submission_interrupted", error="중단된 제출 예약은 재실행하지 않습니다.")
                if record.get("automatic"):
                    record.update(automatic=False, status="paused", pause_reason="앱 재시작 후 명시적으로 재개하세요.")
                save_json(path, record)
            except (OSError, ValueError):
                continue

    def _path(self, campaign_id):
        if not isinstance(campaign_id, str) or len(campaign_id) != 32 or any(c not in "0123456789abcdef" for c in campaign_id):
            raise KeyError(campaign_id)
        return self.root / f"{campaign_id}.json"

    def _read(self, campaign_id):
        path = self._path(campaign_id)
        if not path.is_file() or path.is_symlink():
            raise KeyError(campaign_id)
        return json.loads(path.read_text())

    def _save(self, record):
        record["updated_at"] = utcnow()
        save_json(self._path(record["id"]), record)

    def list(self):
        with self.lock:
            return sorted([self._read(path.stem) for path in self.root.glob("*.json") if not path.is_symlink()], key=lambda r: r["created_at"], reverse=True)

    def get(self, campaign_id):
        return self.refresh(campaign_id)

    def create(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("캠페인 요청은 JSON 객체여야 합니다.")
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 200:
            raise ValueError("캠페인에는 1~200개의 명시적인 후보 JobConfig가 필요합니다.")
        objective = payload.get("objective", {})
        if not isinstance(objective, dict):
            raise ValueError("목표 objective는 JSON 객체여야 합니다.")
        if objective.get("direction") not in {"min", "max"}:
            raise ValueError("목표 direction은 min 또는 max여야 합니다.")
        source, name, unit = objective.get("source"), objective.get("name"), objective.get("unit")
        if source == "feature":
            if name not in FEATURE_OBJECTIVES or unit not in FEATURE_OBJECTIVES[name]:
                raise ValueError("지원 피처 이름과 호환 단위를 사용하세요.")
        elif source == "measurement":
            if name not in PROPERTIES or unit != PROPERTIES[name][0]:
                raise ValueError("지원 측정 물성과 정규화 단위를 사용하세요.")
            if not isinstance(objective.get("method"), str) or not objective["method"].strip() or not isinstance(objective.get("conditions"), dict):
                raise ValueError("실험 목표에는 method와 명시적인 conditions가 필요합니다.")
        else:
            raise ValueError("목표 source는 feature 또는 measurement여야 합니다.")
        budget, batch_size, seed = payload.get("budget", min(10, len(candidates))), payload.get("batch_size", 3), payload.get("seed", 42)
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 50:
            raise ValueError("계산 예산은 1~50개 정수여야 합니다.")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 10:
            raise ValueError("배치 크기는 1~10개여야 합니다.")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
            raise ValueError("추천 seed는 0~2147483647 정수여야 합니다.")
        backend = payload.get("backend", "sklearn")
        if backend not in {"sklearn", "baybe"}:
            raise ValueError("추천 backend는 sklearn 또는 baybe여야 합니다.")
        if backend == "baybe":
            try:
                import baybe  # noqa: F401
            except ImportError as exc:
                raise ValueError("BayBE 실행 환경이 없습니다. baybe를 설치하거나 sklearn을 선택하세요.") from exc
        rows, hashes, protocols = [], set(), set()
        for raw in candidates:
            config = JobConfig.model_validate(raw).model_dump()
            if config["route"] not in {"polymer_cg", "polymer_atomistic", "pysimm", "packmol", "hoomd"}:
                raise ValueError("캠페인은 고분자 CG·전원자·pysimm·PACKMOL·HOOMD 후보를 지원합니다.")
            if config.get("engine_input"):
                raise ValueError("캠페인은 비교 가능한 검증 내장 입력만 사용합니다. 임의 engine_input은 단일 작업에서 실행하세요.")
            if config.get("source_job_id"):
                source_job = self.jobs.get(config["source_job_id"])
                if config["route"] != "packmol" or source_job["status"] not in {"completed", "prepared"} or source_job["config"]["route"] in COARSE:
                    raise ValueError("완료된 원자 구조만 PACKMOL에 연결할 수 있습니다.")
                path = self.jobs.directory(config["source_job_id"]) / config["source_artifact"]
                if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".pdb" or path.stat().st_size > 5000000:
                    raise ValueError("후보 구조는 5 MB 이하 PDB 파일이어야 합니다.")
                config["pdb_text"] = path.read_text()
            effective = _effective_config(config)
            key = digest(effective)
            if key in hashes:
                continue
            hashes.add(key)
            protocols.add(stable_json({k: v for k, v in effective.items() if k not in VARIABLES}))
            if config["route"] in COARSE and source == "measurement":
                raise ValueError("일반 축약 단위 비드 모델의 결과를 실제 재료 물성 실험 목표로 연결할 수 없습니다.")
            if source == "feature" and name in {"rg_mean", "rg_final", "rg_std", "end_to_end_mean", "end_to_end_std"}:
                expected = "sigma" if config["route"] in COARSE else "angstrom"
                if unit != expected:
                    raise ValueError(f"이 경로의 길이 단위는 {expected}입니다.")
            rows.append({"id": key, "config": config, "vector": _vector(config), "status": "untried", "job_id": None,
                         "objective": None, "objective_status": "not_run"})
        if len(protocols) != 1:
            raise ValueError("하나의 캠페인에서는 경로·힘장·엔진·스텝·시드·타임스텝·단백질 입력을 고정하세요. 화학구조·사슬 길이·온도·박스 크기를 탐색할 수 있습니다.")
        record = {"schema_version": 1, "id": uuid.uuid4().hex, "name": str(payload.get("name", "새 연구 캠페인"))[:160],
                  "created_at": utcnow(), "status": "ready", "automatic": False, "backend": backend,
                  "objective": objective, "budget": min(budget, len(rows)), "batch_size": batch_size, "seed": seed,
                  "candidates": rows, "iterations": [], "attempted": 0,
                  "warnings": ["유한한 명시 후보만 실행합니다. 예산과 이미 실행한 후보 해시가 중복 실행을 제한합니다.", "실패한 정확한 후보는 재추천하지 않지만 주변 영역이 위험하다고 추정하지 않습니다.", "일반 CG 축약 단위 계산은 화학 물성 예측이 아닌 시연입니다."]}
        with self.lock:
            if self.closed:
                raise RuntimeError("Campaign manager is stopping")
            self._save(record)
        return record

    def refresh(self, campaign_id):
        with self.lock:
            record = self._read(campaign_id)
            experiments = {item.get("job_id", item.get("id")): item for item in self.store.list_experiments()}
            measurements = self.store.list_measurements() if record["objective"]["source"] == "measurement" else []
            objective = record["objective"]
            signatures = set()
            for candidate in record["candidates"]:
                if not candidate["job_id"]:
                    continue
                try:
                    job = self.jobs.get(candidate["job_id"])
                except KeyError:
                    candidate.update(status="interrupted", objective_status="job_missing", error="연결 작업을 찾을 수 없습니다.")
                    continue
                candidate["status"] = job["status"]
                candidate["error"] = job.get("error")
                if job["status"] not in TERMINAL:
                    continue
                experiment = experiments.get(job["id"])
                if not experiment or experiment.get("status") != job["status"]:
                    experiment = self.store.index_job(job, self.jobs.directory(job["id"]))
                    experiments[job["id"]] = experiment
                candidate["material_id"] = experiment.get("material_id")
                if job["status"] not in {"completed", "prepared"}:
                    candidate.update(objective=None, objective_status="failed_run")
                    continue
                if objective["source"] == "feature":
                    entry = experiment.get("features", {}).get("values", {}).get(objective["name"])
                    if not entry:
                        candidate.update(objective=None, objective_status="feature_unavailable")
                        continue
                    if entry.get("unit") != objective["unit"]:
                        candidate.update(objective=None, objective_status="incompatible_unit")
                        continue
                    signature = stable_json({"unit": entry.get("unit"), "method": entry.get("method"), "route": experiment.get("route"),
                                             "force_field": experiment.get("result", {}).get("metadata", {}).get("force_field")})
                    signatures.add(signature)
                    candidate.update(objective=finite_number(entry.get("value"), objective["name"]), objective_status="observed", objective_provenance={"feature": entry, "job_id": job["id"]})
                else:
                    matching = [m for m in measurements if m.get("job_id") == job["id"] and m.get("property") == objective["name"] and m.get("unit") == objective["unit"]
                                and m.get("verified") is True and m.get("origin") in {"experimental", "literature"} and m.get("method") == objective["method"] and m.get("conditions", {}) == objective.get("conditions", {})]
                    if not matching:
                        candidate.update(objective=None, objective_status="waiting_verified_measurement")
                        continue
                    candidate.update(objective=float(np.mean([finite_number(m["value"], objective["name"]) for m in matching])), objective_status="observed",
                                     objective_provenance={"measurement_ids": [m["id"] for m in matching], "aggregation": "arithmetic mean of same-method same-condition verified replicates"})
            if len(signatures) > 1:
                record.update(status="paused", automatic=False, pause_reason="분석법/힘장/단위가 다른 목표값이 발견되어 추천을 중단했습니다.")
                for candidate in record["candidates"]:
                    if candidate["objective_status"] == "observed":
                        candidate.update(objective=None, objective_status="incompatible_analysis")
            record["attempted"] = sum(c["status"] != "untried" for c in record["candidates"])
            active = any(c["status"] in ACTIVE or c["status"] == "submitting" for c in record["candidates"])
            if not active and record["status"] != "paused":
                if record["attempted"] >= record["budget"] or all(c["status"] != "untried" for c in record["candidates"]):
                    record.update(status="completed", automatic=False)
                elif record["status"] != "stopped":
                    record["status"] = "ready"
            elif active and record["status"] not in {"paused", "stopped"}:
                record["status"] = "running"
            self._save(record)
            return record

    def _recommend(self, record, remaining, count):
        observed = [candidate for candidate in record["candidates"] if candidate["objective"] is not None]
        if record["backend"] == "baybe":
            return self._recommend_baybe(record, remaining, observed, count)
        all_vectors = np.asarray([c["vector"] for c in record["candidates"]], dtype=float)
        spread = np.ptp(all_vectors, axis=0)
        spread[spread == 0] = 1
        values = np.asarray([c["vector"] for c in remaining], dtype=float) / spread
        if len(observed) >= 5:
            from scipy.special import ndtr
            train_x = np.asarray([c["vector"] for c in observed], dtype=float)
            sign = 1 if record["objective"]["direction"] == "max" else -1
            target = np.asarray([c["objective"] * sign for c in observed])
            model = _regressor(record["seed"])
            model.fit(train_x, target)
            mean, std = _predict_gp(model, np.asarray([c["vector"] for c in remaining]))
            improvement = mean - target.max()
            z = np.divide(improvement, std, out=np.zeros_like(improvement), where=std > 1e-12)
            ei = np.where(std > 1e-12, improvement * ndtr(z) + std * np.exp(-z * z / 2) / math.sqrt(2 * math.pi), np.maximum(improvement, 0))
            selected = np.argsort(-ei, kind="stable")[:count].tolist()
            return [remaining[i] for i in selected], {"backend": "sklearn", "strategy": "Gaussian process expected improvement", "comparable_outcomes": len(observed), "scores": {remaining[i]["id"]: float(ei[i]) for i in selected}}
        rng = np.random.default_rng(record["seed"] + len(record["iterations"]))
        selected = []
        prior = [np.asarray(c["vector"]) / spread for c in record["candidates"] if c["job_id"]]
        for _ in range(count):
            available = [i for i in range(len(remaining)) if i not in selected]
            if not prior:
                choice = int(rng.choice(available))
            else:
                distances = [min(float(np.linalg.norm(values[i] - previous)) for previous in prior) for i in available]
                choice = available[int(np.argmax(distances))]
            selected.append(choice); prior.append(values[choice])
        return [remaining[i] for i in selected], {"backend": "sklearn", "strategy": "seeded initial maximin descriptor diversity", "comparable_outcomes": len(observed), "note": "At least five comparable observed outcomes are needed for GP expected improvement"}

    @staticmethod
    def _recommend_baybe(record, remaining, observed, count):
        import pandas as pd
        from baybe import Campaign
        from baybe.objectives import SingleTargetObjective
        from baybe.parameters import NumericalDiscreteParameter
        from baybe.recommenders import BotorchRecommender, FPSRecommender
        from baybe.searchspace import SearchSpace
        from baybe.targets import NumericalTarget
        from baybe.utils.random import temporary_seed
        eligible = observed + remaining
        matrix = np.asarray([c["vector"] for c in eligible], dtype=float)
        varying = np.flatnonzero(np.ptp(matrix, axis=0) > 1e-12)
        if not len(varying):
            return remaining[:count], {"backend": "baybe", "strategy": "identical descriptor candidates; deterministic finite selection", "comparable_outcomes": len(observed)}
        columns = [f"descriptor_{i}" for i in varying]
        frame = pd.DataFrame(matrix[:, varying], columns=columns)
        # Coincident vectors cannot be distinguished by the surrogate. Select one
        # representative at a time, keeping the unselected valid configs for later.
        representatives = ~frame.duplicated()
        frame = frame.loc[representatives].copy()
        parameters = [NumericalDiscreteParameter(name=col, values=sorted(frame[col].unique()), tolerance=0.0) for col in columns]
        space = SearchSpace.from_dataframe(frame, parameters=parameters)
        # BayBE allows re-recommending measured points by default; only untried
        # candidates can be submitted, so exclude measured rows from acquisition.
        campaign = Campaign(searchspace=space, objective=SingleTargetObjective(NumericalTarget(name="objective")),
                            recommender=BotorchRecommender() if len(observed) >= 5 else FPSRecommender(),
                            allow_recommending_already_measured=False)
        sign = 1 if record["objective"]["direction"] == "max" else -1
        measurements = frame.loc[frame.index < len(observed)].copy()
        if len(measurements):
            measurements["objective"] = [observed[i]["objective"] * sign for i in measurements.index]
            campaign.add_measurements(measurements)
        available_unique = sum(i >= len(observed) for i in frame.index)
        if not available_unique:
            return remaining[:count], {"backend": "baybe", "strategy": "descriptor-duplicate finite selection", "comparable_outcomes": len(observed)}
        with temporary_seed(record["seed"] + len(record["iterations"])):
            recommendation = campaign.recommend(batch_size=min(count, available_unique))
        chosen = []
        for _, row in recommendation.iterrows():
            matches = [candidate for candidate in remaining if np.allclose(np.asarray(candidate["vector"])[varying], row[columns].to_numpy(dtype=float), rtol=0, atol=1e-12) and candidate not in chosen]
            if not matches:
                raise ValueError("BayBE returned an unavailable candidate; no job was submitted.")
            chosen.append(matches[0])
        return chosen, {"backend": "baybe", "version": importlib.metadata.version("baybe"), "strategy": "BoTorch acquisition" if len(observed) >= 5 else "BayBE farthest point sampling", "comparable_outcomes": len(observed), "descriptor_columns": columns}

    def iterate(self, campaign_id, batch_size=None, _automatic=False):
        with self.lock:
            if self.closed:
                raise RuntimeError("Campaign manager is stopping")
            record = self.refresh(campaign_id)
            if record["automatic"] and not _automatic:
                raise ValueError("자동 캠페인이 실행 중입니다. 수동 반복 전 자동 실행을 중지하세요.")
            if record["status"] == "stopped":
                raise ValueError("중지된 캠페인입니다. start로 명시적으로 재개하세요.")
            if record["status"] == "paused" and "분석법" in record.get("pause_reason", ""):
                raise ValueError(record["pause_reason"])
            if any(c["status"] in ACTIVE or c["status"] == "submitting" for c in record["candidates"]):
                raise ValueError("현재 배치 작업이 끝난 뒤 다음 반복을 실행하세요.")
            if record["objective"]["source"] == "measurement" and any(c["objective_status"] == "waiting_verified_measurement" for c in record["candidates"]):
                raise ValueError("현재 배치의 실험 측정값을 입력하고 검토 확인한 뒤 다음 반복을 실행하세요.")
            remaining = [c for c in record["candidates"] if c["status"] == "untried"]
            count = batch_size if batch_size is not None else record["batch_size"]
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
                raise ValueError("반복 배치 크기는 1~10개입니다.")
            count = min(count, record["budget"] - record["attempted"], len(remaining))
            if count <= 0:
                record.update(status="completed", automatic=False)
                self._save(record)
                return record
            chosen, recommendation = self._recommend(record, remaining, count)
            iteration = {"number": len(record["iterations"]) + 1, "created_at": utcnow(), "recommendation": recommendation, "candidate_ids": [], "job_ids": []}
            record["iterations"].append(iteration)
            for candidate in chosen:
                # Reservation is persisted before submission. A crash cannot silently
                # submit it again; an interrupted reservation consumes its budget.
                candidate.update(status="submitting", objective_status="pending")
                record["attempted"] += 1
                self._save(record)
                try:
                    job = self.jobs.create(candidate["config"])
                except Exception as exc:
                    candidate.update(status="failed", objective_status="submission_failed", error=str(exc))
                    iteration["candidate_ids"].append(candidate["id"])
                    self._save(record)
                    raise
                candidate.update(job_id=job["id"], status=job["status"])
                iteration["candidate_ids"].append(candidate["id"]); iteration["job_ids"].append(job["id"])
                self._save(record)
            record["status"] = "running"
            self._save(record)
            return record

    def start(self, campaign_id, batch_size=None):
        with self.lock:
            if self.closed:
                raise RuntimeError("Campaign manager is stopping")
            record = self.refresh(campaign_id)
            if record["status"] == "completed":
                return record
            if record["automatic"]:
                return record
            if record["status"] == "paused" and "분석법" in record.get("pause_reason", ""):
                raise ValueError(record["pause_reason"])
            if batch_size is not None:
                if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 10:
                    raise ValueError("배치 크기는 1~10개입니다.")
                record["batch_size"] = batch_size
            record.update(automatic=True, status="running", pause_reason=None)
            self._save(record)
            event = threading.Event()
            self.stop_events[campaign_id] = event
            worker = threading.Thread(target=self._loop, args=(campaign_id, event), name=f"campaign-{campaign_id[:8]}", daemon=True)
            self.threads[campaign_id] = worker
            worker.start()
            return record

    def _loop(self, campaign_id, event):
        while not event.is_set():
            try:
                with self.lock:
                    if self.closed or event.is_set():
                        return
                    record = self.refresh(campaign_id)
                    if not record["automatic"] or record["status"] in {"completed", "stopped", "paused"}:
                        return
                    active = any(c["status"] in ACTIVE or c["status"] == "submitting" for c in record["candidates"])
                    if not active:
                        self.iterate(campaign_id, _automatic=True)
            except Exception as exc:
                with self.lock:
                    record = self._read(campaign_id)
                    record.update(status="paused", automatic=False, pause_reason=str(exc))
                    self._save(record)
                return
            event.wait(0.5)

    def stop(self, campaign_id, cancel_active=False):
        with self.lock:
            record = self._read(campaign_id)
            event = self.stop_events.get(campaign_id)
            if event:
                event.set()
            record.update(status="stopped", automatic=False)
            self._save(record)
            if cancel_active:
                for candidate in record["candidates"]:
                    if candidate["job_id"] and candidate["status"] in ACTIVE:
                        self.jobs.cancel(candidate["job_id"])
            return record

    def close(self):
        with self.lock:
            self.closed = True
            for event in self.stop_events.values():
                event.set()
            for record in self.list():
                if record["automatic"]:
                    record.update(status="paused", automatic=False, pause_reason="앱 종료로 자동 추천을 중지했습니다. 재개는 수동입니다.")
                    self._save(record)
        for worker in list(self.threads.values()):
            if worker is not threading.current_thread():
                worker.join(timeout=5)
