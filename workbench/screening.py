"""Seconds-scale property screen before running a bulk simulation.

The reference is RadonPy's PI1070 (Hayashi et al., npj Comput. Mater. 8, 222 (2022)):
all-atom MD properties of 1,077 amorphous polymers at 300 K and 1 atm. A Gaussian process
with a MinMax (count Tanimoto) kernel over Morgan count fingerprints of a three-unit oligomer
predicts what that published workflow reports, with posterior uncertainty, cross-validated
error and the nearest reference polymers. It is a simulation-fidelity prior for choosing what
to compute or make, not an experimental value; it never enters the reviewed measurement store.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
import threading

import numpy as np

DATA = Path(__file__).resolve().with_name("data")
METHOD_VERSION = "polymer-lab-screening/1"
FP_RADIUS, FP_SIZE, OLIGOMER_UNITS = 2, 2048, 3
# Key: Polymer Lab bulk-route feature name -> (PI1070 column, factor to our unit, unit, label)
PROPERTIES = {
    "bulk_density": ("density", 1.0, "g/cm^3", "밀도"),
    "heat_capacity_cp": ("Cp", 1.0, "J/(kg K)", "정압 비열 Cp"),
    "bulk_modulus": ("bulk_modulus", 1e-9, "GPa", "체적 탄성률"),
    "isothermal_compressibility": ("compressibility", 1e9, "1/GPa", "등온 압축률"),
    "volume_expansion": ("volume_expansion", 1.0, "1/K", "체적 열팽창 계수"),
    "refractive_index": ("refractive_index", 1.0, "dimensionless", "굴절률"),
    "thermal_conductivity": ("thermal_conductivity", 1.0, "W/(m K)", "열전도도"),
}
AMPLITUDES = (0.25, 0.5, 1.0, 2.0)
NOISES = (0.001, 0.003, 0.01, 0.03, 0.1, 0.3)
CV_FOLDS, CV_SEED = 5, 0
# Held-out PI1070 error grows sharply below these nearest-neighbour similarities (see cross_validation()).
DOMAIN_EDGES = (0.35, 0.55)
WARNINGS = [
    "PI1070 values are classical all-atom MD results (GAFF2-family force field, 10 chains, about 1,000 atoms each), not measurements.",
    "Classical MD heat capacity counts every vibrational mode classically and is typically well above experimental Cp.",
    "Polymer Lab's exploratory bulk route uses Gasteiger charges and may use shortened stages; its numbers need not match PI1070 exactly.",
    "Tg, crystallinity, molecular-weight and processing effects are outside this reference set.",
]


def _manifest():
    return json.loads((DATA / "reference_data.json").read_text())["datasets"]


def repeat_unit(smiles):
    """Validated repeat unit with exactly two singly bonded attachment points (* or [*])."""
    from rdkit import Chem
    if not isinstance(smiles, str) or not smiles.strip() or len(smiles) > 500:
        raise ValueError("반복단위 SMILES가 필요합니다.")
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        raise ValueError("반복단위 SMILES를 읽을 수 없습니다.")
    stars = [atom for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0]
    if len(stars) != 2 or any(atom.GetDegree() != 1 or atom.GetBonds()[0].GetBondType() != Chem.BondType.SINGLE for atom in stars):
        raise ValueError("반복단위에는 단일 결합으로 연결된 연결점 * 가 정확히 두 개 있어야 합니다.")
    if not any(atom.GetAtomicNum() > 0 for atom in mol.GetAtoms()):
        raise ValueError("연결점 외에 원자가 하나 이상 필요합니다.")
    return mol


def oligomer(mol, units=OLIGOMER_UNITS):
    """Head-to-tail oligomer with hydrogen ends, so junction environments appear in the fingerprint."""
    from rdkit import Chem
    first, last = [atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0]
    size = mol.GetNumAtoms()
    chain = Chem.RWMol(mol)
    for _ in range(units - 1):
        chain = Chem.RWMol(Chem.CombineMols(chain, mol))
    neighbour = lambda index: chain.GetAtomWithIdx(index).GetNeighbors()[0].GetIdx()
    remove = []
    for k in range(units - 1):
        tail, head = k * size + last, (k + 1) * size + first
        chain.AddBond(neighbour(tail), neighbour(head), Chem.BondType.SINGLE)
        remove += [tail, head]
    for index in sorted(remove, reverse=True):
        chain.RemoveAtom(index)
    for atom in chain.GetAtoms():
        if atom.GetAtomicNum() == 0:
            atom.SetAtomicNum(1)
            atom.SetIsotope(0)
    result = chain.GetMol()
    Chem.SanitizeMol(result)
    return Chem.RemoveHs(result)


def fingerprint_counts(smiles):
    from rdkit.Chem import rdFingerprintGenerator
    mol = oligomer(repeat_unit(smiles))
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=FP_RADIUS, fpSize=FP_SIZE)
    return generator.GetCountFingerprintAsNumPy(mol).astype(np.int64)


def canonical_repeat(smiles):
    from rdkit import Chem
    return Chem.MolToSmiles(repeat_unit(smiles))


class _MinMax:
    """Exact sum(min(a, b)) for count vectors as a sparse dot product of thermometer codes."""

    def __init__(self, counts):
        from scipy import sparse
        self._sparse = sparse
        self.levels = counts.max(axis=0)
        self.offsets = np.concatenate([[0], np.cumsum(self.levels)[:-1]])
        self.width = int(self.levels.sum())
        self.codes = self.encode(counts)
        self.sums = counts.sum(axis=1).astype(float)

    def encode(self, counts):
        clipped = np.minimum(counts, self.levels)
        rows, cols = np.nonzero(clipped)
        repeats = clipped[rows, cols]
        within = np.arange(repeats.sum()) - np.repeat(np.cumsum(repeats) - repeats, repeats)
        columns = np.repeat(self.offsets[cols], repeats) + within
        data = np.ones(len(columns), dtype=np.float64)
        return self._sparse.csr_matrix((data, (np.repeat(rows, repeats), columns)), shape=(len(counts), self.width))

    def kernel(self, counts, index=None):
        codes, sums = self.codes, self.sums
        if index is not None:
            codes, sums = codes[index], sums[index]
        shared = (self.encode(counts) @ codes.T).toarray()
        union = counts.sum(axis=1)[:, None] + sums[None, :] - shared
        return np.divide(shared, union, out=np.zeros_like(shared), where=union > 0)


def _fit(K, y, amplitudes=AMPLITUDES, noises=NOISES):
    """Normalized-target GP; amplitude and noise chosen by exact log marginal likelihood.

    One eigendecomposition K = Q diag(l) Q^T serves every grid point:
    (aK + sI)^-1 = Q diag(1/(a l + s)) Q^T and log|aK + sI| = sum log(a l + s).
    """
    mean, scale = float(y.mean()), float(y.std()) or 1.0
    target = (y - mean) / scale
    eigenvalues, Q = np.linalg.eigh(K)
    eigenvalues = np.maximum(eigenvalues, 0.0)  # MinMax is positive semidefinite; clip round-off
    projected = Q.T @ target
    best = None
    for amplitude in amplitudes:
        for noise in noises:
            spectrum = amplitude * eigenvalues + noise
            evidence = -0.5 * float(np.sum(projected ** 2 / spectrum)) - 0.5 * float(np.log(spectrum).sum()) - 0.5 * len(K) * math.log(2 * math.pi)
            if best is None or evidence > best["log_marginal_likelihood"]:
                best = {"amplitude": amplitude, "noise": noise, "log_marginal_likelihood": evidence}
    inverse = 1.0 / (best["amplitude"] * eigenvalues + best["noise"])
    return {**best, "Q": Q, "inverse": inverse, "alpha": Q @ (projected * inverse), "mean": mean, "scale": scale}


def _predict(model, Ks):
    """Predictive mean/std for a new MD result (latent variance plus fitted noise)."""
    cross = model["amplitude"] * Ks
    mean = cross @ model["alpha"]
    projected = cross @ model["Q"]
    variance = np.maximum(model["amplitude"] - (projected ** 2 * model["inverse"]).sum(axis=1), 0.0) + model["noise"]
    return mean * model["scale"] + model["mean"], np.sqrt(variance) * model["scale"]


class ReferenceSurrogate:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = None
        self._cv = None

    def _load(self):
        with self._lock:
            if self._state is not None:
                return self._state
            meta = _manifest()["radonpy_pi1070"]
            raw = (DATA / meta["file"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != meta["file_sha256"]:
                raise RuntimeError("Bundled PI1070 subset does not match reference_data.json; rebuild with scripts/build_reference_data.py")
            rows = list(csv.DictReader(raw.decode().splitlines()))
            counts = np.array([fingerprint_counts(row["smiles"]) for row in rows])
            kernel = _MinMax(counts)
            K = kernel.kernel(counts)
            models = {}
            for name, (column, factor, unit, label) in PROPERTIES.items():
                index = np.array([i for i, row in enumerate(rows) if row[column] != ""])
                y = np.array([float(rows[i][column]) * factor for i in index])
                models[name] = {"index": index, "y": y, **_fit(K[np.ix_(index, index)], y)}
            self._state = {"rows": rows, "counts": counts, "kernel": kernel, "K": K, "models": models, "meta": meta}
            return self._state

    def card(self, include_cv=False):
        meta = _manifest()["radonpy_pi1070"]
        card = {"method": "Gaussian process, MinMax (count Tanimoto) kernel on Morgan counts (radius 2, 2048) of a head-to-tail trimer",
                "method_version": METHOD_VERSION, "reference": {key: meta[key] for key in ("repository", "commit", "path", "sha256", "license", "citation", "kind", "conditions", "rows")},
                "properties": {name: {"unit": unit, "label": label, "reference_column": column} for name, (column, _, unit, label) in PROPERTIES.items()},
                "domain_similarity_edges": list(DOMAIN_EDGES), "warnings": WARNINGS}
        if include_cv:
            card["cross_validation"] = self.cross_validation()
        return card

    def cross_validation(self):
        """Random 5-fold CV with each property's fitted hyperparameters held fixed."""
        state = self._load()
        with self._lock:
            if self._cv is not None:
                return self._cv
        rng = np.random.default_rng(CV_SEED)
        report = {}
        for name, model in state["models"].items():
            index, y = model["index"], model["y"]
            folds = np.array_split(rng.permutation(len(index)), CV_FOLDS)
            pred, std, similarity = np.zeros_like(y), np.zeros_like(y), np.zeros_like(y)
            for test in folds:
                train = np.setdiff1d(np.arange(len(index)), test)
                fold = _fit(state["K"][np.ix_(index[train], index[train])], y[train], (model["amplitude"],), (model["noise"],))
                Ks = state["K"][np.ix_(index[test], index[train])]
                pred[test], std[test] = _predict(fold, Ks)
                similarity[test] = Ks.max(axis=1)
            error = np.abs(pred - y)
            bins = {}
            for label, mask in (("outside", similarity < DOMAIN_EDGES[0]), ("edge", (similarity >= DOMAIN_EDGES[0]) & (similarity < DOMAIN_EDGES[1])),
                                ("inside", similarity >= DOMAIN_EDGES[1])):
                bins[label] = {"count": int(mask.sum()), "mae": float(error[mask].mean()) if mask.any() else None}
            report[name] = {"unit": PROPERTIES[name][2], "label": PROPERTIES[name][3], "n": int(len(y)), "mae": float(error.mean()),
                            "rmse": float(np.sqrt(np.mean(error ** 2))), "r2": float(1 - np.sum((pred - y) ** 2) / np.sum((y - y.mean()) ** 2)),
                            "coverage_1sigma": float(np.mean(error <= std)), "coverage_2sigma": float(np.mean(error <= 1.96 * std)),
                            "reference_std": float(y.std()), "by_domain": bins,
                            "hyperparameters": {"amplitude": model["amplitude"], "noise": model["noise"]}}
        report = {"scheme": f"random {CV_FOLDS}-fold, seed {CV_SEED}; held-out polymers are unseen repeat units of the same dataset",
                  "properties": report}
        with self._lock:
            self._cv = report
        return report

    def predict(self, smiles, properties=None, neighbours=3):
        state = self._load()
        requested = list(properties or PROPERTIES)
        unknown = [name for name in requested if name not in PROPERTIES]
        if unknown:
            raise ValueError("지원하지 않는 예측 물성: " + ", ".join(unknown))
        canonical = canonical_repeat(smiles)
        counts = fingerprint_counts(smiles)[None, :]
        similarity = state["kernel"].kernel(counts)[0]
        order = np.argsort(-similarity)
        top = float(similarity[order[0]])
        domain = "inside" if top >= DOMAIN_EDGES[1] else "edge" if top >= DOMAIN_EDGES[0] else "outside"
        exact = [state["rows"][i]["monomer_ID"] for i in order[:5] if similarity[i] >= 0.999999 and canonical_repeat(state["rows"][i]["smiles"]) == canonical]
        values = {}
        for name in requested:
            column, factor, unit, label = PROPERTIES[name]
            model = state["models"][name]
            mean, std = _predict(model, similarity[model["index"]][None, :])
            values[name] = {"value": float(mean[0]), "std": float(std[0]), "unit": unit, "label": label,
                            "interval_95": [float(mean[0] - 1.96 * std[0]), float(mean[0] + 1.96 * std[0])]}
        near = []
        for i in order[:neighbours]:
            row = state["rows"][i]
            near.append({"monomer_id": row["monomer_ID"], "smiles": row["smiles"], "similarity": float(similarity[i]),
                         "values": {name: float(row[PROPERTIES[name][0]]) * PROPERTIES[name][1] for name in requested if row[PROPERTIES[name][0]] != ""}})
        warnings = list(WARNINGS)
        if domain != "inside":
            warnings.insert(0, f"가장 가까운 PI1070 고분자와의 유사도가 {top:.2f}로 낮아 예측 신뢰도가 떨어집니다.")
        return {"repeat_smiles": canonical, "fidelity": "reference_md_surrogate", "method_version": METHOD_VERSION,
                "temperature_kelvin": 300.0, "pressure_atm": 1.0, "values": values,
                "applicability": {"max_similarity": top, "domain": domain, "edges": list(DOMAIN_EDGES), "in_reference_set": exact},
                "nearest_reference": near, "reference": {key: state["meta"][key] for key in ("repository", "commit", "sha256", "citation", "license")},
                "warnings": warnings}


def own_simulations(prediction, experiments):
    """This workspace's completed bulk runs of the same repeat unit, next to the reference prediction."""
    rows = []
    for record in experiments:
        if record.get("route") != "polymer_atomistic" or (record.get("material") or {}).get("repeat_smiles") != prediction["repeat_smiles"]:
            continue
        found = (record.get("features") or {}).get("values") or {}
        values = {}
        for name, predicted in prediction["values"].items():
            item = found.get(name)
            if isinstance(item, dict) and isinstance(item.get("value"), (int, float)) and item.get("unit") == predicted["unit"]:
                values[name] = {"value": item["value"], "unit": item["unit"], "minus_reference": item["value"] - predicted["value"],
                                "z": (item["value"] - predicted["value"]) / predicted["std"] if predicted["std"] > 0 else None}
        if values:
            config = record.get("config") or {}
            rows.append({"job_id": record.get("job_id"), "name": record.get("name"), "status": record.get("status"),
                         "temperature_kelvin": config.get("temperature_kelvin"), "protocol_scale": config.get("protocol_scale"),
                         "radonpy_criteria_met": next((v.get("radonpy_criteria_met") for v in found.values() if isinstance(v, dict) and "radonpy_criteria_met" in v), None),
                         "values": values})
    return rows


SURROGATE = ReferenceSurrogate()
