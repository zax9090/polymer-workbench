"""Local, measurement-backed surrogate baseline; no pretrained property claims.

Only verified experimental/literature targets train models. Models are JSON training
snapshots, never executable pickle uploads. Predictions refit this bounded dataset.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import threading
import uuid
import warnings

import numpy as np


PROPERTIES = {
    "tg": ("K", "polymer"), "young_modulus": ("MPa", "polymer"),
    "tensile_strength": ("MPa", "polymer"), "molecular_weight": ("g/mol", "polymer"),
    "mn": ("g/mol", "polymer"), "mw": ("g/mol", "polymer"), "dispersity": ("dimensionless", "polymer"),
    "decomposition_temperature": ("K", "polymer"),
    "binding_affinity": ("nM", "protein_polymer"), "contact_count": ("count", "protein_polymer"),
    "hbond_count": ("count", "protein_polymer"), "protein_rmsd": ("angstrom", "protein_polymer"),
}
DESCRIPTOR_NAMES = ["repeat_mol_weight", "repeat_logp", "repeat_tpsa", "repeat_hbond_donors", "repeat_hbond_acceptors", "repeat_rotatable_bonds"]


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def runtime_provenance():
    versions = {name: importlib.metadata.version(name) for name in ("rdkit", "scikit-learn", "numpy")}
    sources = {}
    for name in ("learning.py", "descriptors.py"):
        path = Path(__file__).resolve().with_name(name)
        if path.is_file():
            sources["workbench/" + name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"package_versions": versions, "source_sha256": sources,
            "source_capture_complete": len(sources) == 2}


def save_json(path, value):
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}: 유한한 수치가 필요합니다. 누락값을 0으로 대체하지 않습니다.")
    return float(value)


def molecular_features(smiles):
    """Repeat-unit descriptors with dummy attachment sites retained explicitly."""
    from rdkit import Chem, DataStructs
    from rdkit.Chem import Descriptors, rdFingerprintGenerator
    if not isinstance(smiles, str) or not smiles.strip() or len(smiles) > 1000:
        raise ValueError("검증 가능한 반복단위 SMILES가 필요합니다.")
    mol = Chem.MolFromSmiles(smiles)
    if mol is None or not any(atom.GetAtomicNum() > 0 for atom in mol.GetAtoms()):
        raise ValueError("반복단위 SMILES를 읽을 수 없습니다.")
    canonical = Chem.MolToSmiles(mol, canonical=True)
    fp = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=128, includeChirality=True).GetFingerprint(mol)
    bits = np.zeros(128, dtype=np.float64)
    DataStructs.ConvertToNumpyArray(fp, bits)
    numerical = [Descriptors.MolWt(mol), Descriptors.MolLogP(mol), Descriptors.TPSA(mol),
                 Descriptors.NumHDonors(mol), Descriptors.NumHAcceptors(mol), Descriptors.NumRotatableBonds(mol)]
    return canonical, bits.tolist() + [float(v) for v in numerical]


def _feature_values(record):
    return record.get("features", {}).get("values", {})


def _compatibility(record, keys):
    """Keep descriptor units/method and simulation model separate from real targets."""
    config, result = record.get("config", {}), record.get("result") or {}
    metadata = result.get("metadata", {})
    model = result.get("model", {})
    return {
        "route": record.get("route", config.get("route")),
        "engine": result.get("engine"), "force_field": metadata.get("force_field"),
        "charge_method": metadata.get("charge_method"), "time_step_fs": metadata.get("time_step_fs"),
        "steps": config.get("steps"), "sampling_frames": record.get("features", {}).get("frame_count"),
        "ensemble": metadata.get("ensemble"), "model": model.get("name"),
        "units": model.get("units"), "temperature": config.get("temperature") if record.get("route") in {"polymer_cg", "hoomd"} else config.get("temperature_kelvin"),
        "features": {key: {k: _feature_values(record).get(key, {}).get(k) for k in ("unit", "method")} for key in keys},
    }


def _regressor(seed=42):
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    # Fixed kernel avoids unidentifiable hyperparameter optimization in tiny datasets.
    kernel = ConstantKernel(1.0, constant_value_bounds="fixed") * Matern(length_scale=5.0, length_scale_bounds="fixed", nu=1.5) + WhiteKernel(0.05, noise_level_bounds="fixed")
    return make_pipeline(StandardScaler(), GaussianProcessRegressor(kernel=kernel, normalize_y=True, optimizer=None, random_state=seed))


def _predict_gp(model, values):
    return model[-1].predict(model[0].transform(values), return_std=True)


class SurrogateManager:
    def __init__(self, root: Path, store):
        self.root = Path(root).resolve() / "models"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.lock = threading.RLock()

    def _path(self, model_id):
        if not isinstance(model_id, str) or len(model_id) != 32 or any(c not in "0123456789abcdef" for c in model_id):
            raise KeyError(model_id)
        path = self.root / f"{model_id}.json"
        if not path.is_file() or path.is_symlink():
            raise KeyError(model_id)
        return path

    def list(self):
        records = []
        with self.lock:
            for path in self.root.glob("*.json"):
                if path.is_symlink():
                    continue
                try:
                    value = json.loads(path.read_text())
                    records.append({k: v for k, v in value.items() if k != "training"})
                except (OSError, ValueError):
                    continue
        return sorted(records, key=lambda item: item["created_at"], reverse=True)

    def train(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("학습 요청은 JSON 객체여야 합니다.")
        prop = payload.get("property")
        if prop not in PROPERTIES:
            raise ValueError("지원하는 물성 또는 단백질-고분자 측정 목표를 선택하세요.")
        unit, domain = PROPERTIES[prop]
        if payload.get("unit", unit) != unit or payload.get("domain", domain) != domain:
            raise ValueError(f"{prop} 학습에는 {domain} 분야의 {unit} 단위를 사용하세요.")
        keys = payload.get("feature_keys", [])
        if not isinstance(keys, list) or len(keys) > 20 or any(not isinstance(k, str) or len(k) > 100 for k in keys) or len(set(keys)) != len(keys):
            raise ValueError("feature_keys는 중복 없는 최대 20개 피처 이름 목록이어야 합니다.")
        experiments = {item.get("job_id", item.get("id")): item for item in self.store.list_experiments()}
        selected, excluded = [], []
        for measurement in self.store.list_measurements():
            if measurement.get("property") != prop or measurement.get("unit") != unit:
                continue
            if not measurement.get("verified") or measurement.get("origin") not in {"experimental", "literature"}:
                excluded.append({"id": measurement.get("id"), "reason": "검토 확인된 실험/문헌 측정값만 사용"})
                continue
            if payload.get("method") is not None and measurement.get("method") != payload["method"]:
                continue
            if "conditions" in payload and measurement.get("conditions", {}) != payload["conditions"]:
                continue
            selected.append(measurement)
        if not selected:
            raise ValueError("선택 조건과 일치하는 검토 완료 실험/문헌 측정값이 없습니다.")
        contexts = {stable_json({"method": item.get("method"), "conditions": item.get("conditions", {})}) for item in selected}
        if len(contexts) != 1:
            raise ValueError("측정법·온도·조성 등 조건이 다릅니다. method와 conditions로 비교 가능한 집합을 선택하세요.")
        rows, compatibilities, proteins = [], set(), set()
        for item in selected:
            experiment = experiments.get(item.get("job_id"), {})
            material = item.get("material") or experiment.get("material") or {}
            smiles = material.get("repeat_smiles") or material.get("smiles")
            if not smiles and experiment.get("route") in {"polymer_atomistic", "packmol", "pysimm"}:
                smiles = experiment.get("config", {}).get("repeat_smiles")
            try:
                canonical, values = molecular_features(smiles)
                target = finite_number(item.get("value"), prop)
                material_id = item.get("material_id")
                if not material_id:
                    raise ValueError("material_id 누락")
                if keys:
                    if experiment.get("status") != "completed" or experiment.get("is_demo"):
                        raise ValueError("MD 피처에는 완료된 비데모 계산과 직접 연결된 측정값이 필요합니다.")
                    if experiment.get("features", {}).get("actual_dynamics") is not True:
                        raise ValueError("MD 피처에는 실제 동역학 궤적이 필요합니다. 구조 미리보기는 사용하지 않습니다.")
                    fv = _feature_values(experiment)
                    for key in keys:
                        if key not in fv:
                            raise ValueError(f"피처 {key} 누락")
                        values.append(finite_number(fv[key].get("value"), key))
                    compatibility = _compatibility(experiment, keys)
                    if any(not value.get("unit") or not value.get("method") for value in compatibility["features"].values()):
                        raise ValueError("MD 피처 단위/분석법 정보 누락")
                    compatibilities.add(stable_json(compatibility))
                # Uploaded coordinates are stronger identity evidence than a form's
                # default accession. Never merge unrelated PDBs under that default.
                protein = ({"pdb_sha256": material["pdb_sha256"]} if material.get("pdb_sha256") else
                           material.get("protein_id") or material.get("protein_sequence") or material.get("sequence") or
                           material.get("protein_accession") or material.get("accession"))
                if domain == "protein_polymer" and not protein:
                    raise ValueError("단백질-고분자 학습에는 material.protein_id 또는 sequence가 필요합니다.")
                proteins.add(stable_json(protein))
                rows.append({"measurement_id": item["id"], "material_id": material_id, "job_id": item.get("job_id"), "canonical_smiles": canonical,
                             "x": values, "y": target, "source": item.get("source"), "origin": item.get("origin"), "protein_context": protein})
            except ValueError as exc:
                excluded.append({"id": item.get("id"), "reason": str(exc)})
        if len(compatibilities) > 1:
            raise ValueError("MD 피처의 힘장·엔진·앙상블·온도·단위·분석법이 다릅니다. 호환 계산만 선택하세요.")
        if domain == "protein_polymer" and len(proteins) > 1:
            raise ValueError("현재 모델은 하나의 단백질 조건에서 고분자 후보를 비교합니다. 다른 단백질의 데이터를 섞을 수 없습니다.")
        # Merge groups sharing either material_id or repeat chemistry. A renamed or
        # differently sized copy of the same repeat cannot leak across CV folds.
        parent = list(range(len(rows)))
        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        seen = {}
        for i, row in enumerate(rows):
            for key in (("material", row["material_id"]), ("chemistry", row["canonical_smiles"])):
                if key in seen:
                    parent[find(i)] = find(seen[key])
                else:
                    seen[key] = i
        groups = np.asarray([str(find(i)) for i in range(len(rows))])
        n_groups = len(set(groups))
        if n_groups < 8:
            raise ValueError(f"서로 다른 고분자 화학구조/재료 그룹이 최소 8개 필요합니다. 현재 {n_groups}개, 사용 가능 측정 {len(rows)}개입니다.")
        if len(rows) > 500:
            raise ValueError("개인용 GP 기준 모델은 최대 500개 측정값을 지원합니다. 조건별로 나누어 학습하세요.")
        from sklearn.model_selection import GroupKFold
        from sklearn.metrics import mean_absolute_error, mean_squared_error
        x = np.asarray([row["x"] for row in rows], dtype=float)
        y = np.asarray([row["y"] for row in rows], dtype=float)
        predicted = np.zeros(len(rows)); folds = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=UserWarning)
            for train, test in GroupKFold(n_splits=min(5, n_groups)).split(x, y, groups):
                regressor = _regressor()
                regressor.fit(x[train], y[train])
                predicted[test] = regressor.predict(x[test])
                folds.append({"train_material_ids": sorted({rows[i]["material_id"] for i in train}),
                              "test_material_ids": sorted({rows[i]["material_id"] for i in test}),
                              "train_chemistries": sorted({rows[i]["canonical_smiles"] for i in train}),
                              "test_chemistries": sorted({rows[i]["canonical_smiles"] for i in test})})
        model_id = uuid.uuid4().hex
        model = {"id": model_id, "name": str(payload.get("name", f"{prop} 연구용 기준 모델"))[:160], "created_at": utcnow(),
                 "schema_version": 1, "algorithm": "scikit-learn fixed-kernel GaussianProcessRegressor",
                 "sklearn_version": importlib.metadata.version("scikit-learn"), "property": prop, "unit": unit, "domain": domain,
                 "training_kind": "paired_descriptor_experimental_regression" if keys else "repeat_unit_descriptor_regression",
                 "is_multifidelity": False, "feature_keys": keys,
                 "feature_names": [f"morgan_{i}" for i in range(128)] + DESCRIPTOR_NAMES + keys,
                 "measurement_context": json.loads(next(iter(contexts))),
                 "protein_context": rows[0]["protein_context"] if domain == "protein_polymer" else None,
                 "compatibility": json.loads(next(iter(compatibilities))) if compatibilities else None,
                 "validation": {"method": "grouped_cross_validation_by_material_and_repeat_chemistry", "folds": len(folds), "mae": float(mean_absolute_error(y, predicted)), "rmse": float(math.sqrt(mean_squared_error(y, predicted))), "unit": unit, "n_measurements": len(rows), "n_groups": n_groups, "splits": folds},
                 "excluded": excluded, "training_sha256": digest(rows), "training": rows,
                 "provenance": runtime_provenance(),
                 "warnings": ["검증된 사전학습 물성 모델이 아닌 사용자 데이터 기반 연구용 기준 모델입니다.", "짧은 MD 피처가 있어도 실제 다중 충실도 모델/정확도 보장을 의미하지 않습니다.", "반복단위 기술자는 분자량 분포·공중합체 서열·입체규칙성의 전체 효과를 표현하지 않습니다.", "GP 표준편차는 보정된 신뢰구간이 아닙니다. 외삽과 작은 표본에서는 과신하지 마세요."]}
        with self.lock:
            save_json(self.root / f"{model_id}.json", model)
        return {k: v for k, v in model.items() if k != "training"}

    def predict(self, model_id, payload):
        if not isinstance(payload, dict):
            raise ValueError("예측 요청은 JSON 객체여야 합니다.")
        with self.lock:
            model = json.loads(self._path(model_id).read_text())
        rows = model["training"]
        if digest(rows) != model["training_sha256"]:
            raise ValueError("모델 학습 스냅샷 해시가 일치하지 않습니다.")
        canonical, values = molecular_features(payload.get("repeat_smiles"))
        if model["domain"] == "protein_polymer" and payload.get("protein_context") != model["protein_context"]:
            raise ValueError("학습과 같은 protein_context를 명시해야 합니다.")
        keys = model["feature_keys"]
        if keys:
            if payload.get("compatibility") != model["compatibility"]:
                raise ValueError("MD 피처의 힘장·단위·분석법 compatibility가 학습 데이터와 일치해야 합니다.")
            features = payload.get("features", {})
            if not isinstance(features, dict):
                raise ValueError("features는 이름과 수치가 연결된 JSON 객체여야 합니다.")
            for key in keys:
                entry = features.get(key)
                values.append(finite_number(entry.get("value") if isinstance(entry, dict) else entry, key))
        x, y = np.asarray([row["x"] for row in rows]), np.asarray([row["y"] for row in rows])
        regressor = _regressor()
        regressor.fit(x, y)
        mean, std = _predict_gp(regressor, [values])
        query = np.asarray(values)
        fingerprint = query[:128] > 0
        similarities = []
        for row in x:
            union = np.logical_or(fingerprint, row[:128] > 0).sum()
            similarities.append(float(np.logical_and(fingerprint, row[:128] > 0).sum() / union) if union else 1.0)
        outside = [model["feature_names"][i] for i in range(128, len(values)) if query[i] < x[:, i].min() - 1e-10 or query[i] > x[:, i].max() + 1e-10]
        nearest = max(similarities)
        out = bool(outside or nearest < 0.3)
        return {"model_id": model_id, "property": model["property"], "value": float(mean[0]), "unit": model["unit"], "canonical_smiles": canonical,
                "training_provenance": model.get("provenance"), "prediction_provenance": runtime_provenance(),
                "model_standard_deviation": float(std[0]), "uncertainty_kind": "uncalibrated_gp_posterior_std", "is_multifidelity": False,
                "applicability": {"status": "outside_training_coverage" if out else "within_observed_descriptor_ranges", "outside_features": outside, "nearest_fingerprint_tanimoto": nearest, "chemistry_seen": canonical in {row["canonical_smiles"] for row in rows}, "is_validated_domain": False},
                "validation": {k: v for k, v in model["validation"].items() if k != "splits"}, "warnings": model["warnings"] + (["학습 범위 밖의 후보입니다. 예측을 물성 보증으로 사용하지 마세요."] if out else [])}
