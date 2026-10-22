"""Hansen solubility parameter (HSP) screens: solvents, solubility spheres and blends.

Distance: Ra^2 = 4(dD1-dD2)^2 + (dP1-dP2)^2 + (dH1-dH2)^2 (MPa), RED = Ra/R0.
Solvent chi follows Lindvig, Michelsen & Kontogeorgis, Fluid Phase Equilib. 203, 247 (2002):
chi = alpha * V_solvent * Ra^2 / (4RT) with alpha = 0.6. Blends use the same regular-solution
form with alpha = 1 and the Flory-Huggins critical value
chi_c = (1/sqrt(N_A) + 1/sqrt(N_B))^2 / 2 per reference volume. These are always >= 0 and
therefore cannot represent specific attractions (hydrogen-bonded or ionic blends).
The sphere fit is Hansen's classic DATAFIT search, implemented independently and checked
against HSPiPy 1.1.8 (REA-audited, MIT).
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
R_GAS = 8.314462618  # J/(mol K); V[cm^3/mol] * Ra^2[MPa] is J/mol
LINDVIG_ALPHA = 0.6
WEIGHTS = np.array([4.0, 1.0, 1.0])
LIMITS = {"delta_d": (5.0, 30.0), "delta_p": (0.0, 40.0), "delta_h": (0.0, 50.0)}
CAL_TO_MPA = 2.0455  # 1 (cal/cm^3)^0.5 = 2.0455 MPa^0.5
METHOD_VERSION = "polymer-lab-hsp/1"
_SOLVENTS, _LOCK = None, threading.Lock()


def solvents():
    global _SOLVENTS
    with _LOCK:
        if _SOLVENTS is None:
            meta = json.loads((DATA / "reference_data.json").read_text())["datasets"]["hsp_solvents"]
            raw = (DATA / meta["file"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != meta["file_sha256"]:
                raise RuntimeError("Bundled solvent table does not match reference_data.json")
            rows = []
            for row in csv.DictReader(raw.decode().splitlines()):
                rows.append({"name": row["name"], "cas": row["cas"], "smiles": row["smiles"],
                             "hsp": np.array([float(row["delta_d"]), float(row["delta_p"]), float(row["delta_h"])]),
                             "molar_volume": float(row["molar_volume"]),
                             "boiling_point_c": float(row["boiling_point_c"]) if row["boiling_point_c"] else None})
            _SOLVENTS = {"rows": rows, "meta": meta,
                         "index": {key: i for i, row in enumerate(rows) for key in (row["name"].lower(), row["cas"]) if key}}
        return _SOLVENTS


def source():
    meta = solvents()["meta"]
    return {key: meta[key] for key in ("repository", "commit", "path", "sha256", "license", "rows", "kind", "note")}


def list_solvents(query=""):
    text = str(query or "").strip().lower()
    return [{"name": r["name"], "cas": r["cas"], "smiles": r["smiles"], "delta_d": r["hsp"][0], "delta_p": r["hsp"][1], "delta_h": r["hsp"][2],
             "molar_volume": r["molar_volume"], "boiling_point_c": r["boiling_point_c"]}
            for r in solvents()["rows"] if not text or text in r["name"].lower() or text == r["cas"]]


def find_solvent(name):
    table = solvents()
    key = str(name or "").strip().lower()
    if key not in table["index"]:
        raise ValueError(f"용매 데이터베이스에 없는 이름 또는 CAS: {name}. delta_d/delta_p/delta_h를 직접 입력하세요.")
    return table["rows"][table["index"][key]]


def hsp_point(value, label="HSP", unit="MPa^0.5"):
    if not isinstance(value, dict):
        raise ValueError(f"{label}: delta_d, delta_p, delta_h가 필요합니다.")
    factor = {"MPa^0.5": 1.0, "(cal/cm^3)^0.5": CAL_TO_MPA}.get(value.get("unit", unit))
    if factor is None:
        raise ValueError(f"{label}: 단위는 MPa^0.5 또는 (cal/cm^3)^0.5여야 합니다.")
    point = []
    for key, (low, high) in LIMITS.items():
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError(f"{label}: {key}에 유한한 수치가 필요합니다.")
        number = float(number) * factor
        if not low <= number <= high:
            raise ValueError(f"{label}: {key} {number:g} MPa^0.5가 허용 범위 {low:g}-{high:g}를 벗어납니다.")
        point.append(number)
    return np.array(point)


def _positive(value, label, low=0.0, high=math.inf):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low < float(value) <= high:
        raise ValueError(f"{label}: {low:g}보다 크고 {high:g} 이하인 수치가 필요합니다.")
    return float(value)


def distance(a, b):
    difference = np.asarray(a, float) - np.asarray(b, float)
    return np.sqrt(np.sum(WEIGHTS * difference ** 2, axis=-1))


def chi(molar_volume, ra, temperature, alpha=LINDVIG_ALPHA):
    return alpha * molar_volume * ra ** 2 / (4.0 * R_GAS * temperature)


def _point_dict(point):
    return {"delta_d": float(point[0]), "delta_p": float(point[1]), "delta_h": float(point[2]), "unit": "MPa^0.5"}


def rank_solvents(polymer, temperature=298.15, alpha=LINDVIG_ALPHA, limit=25, names=None, blends=0):
    """Rank database solvents by RED (if R0 given) or Ra; optional best two-solvent blends."""
    center = hsp_point(polymer, "고분자 HSP")
    radius = polymer.get("radius")
    radius = _positive(radius, "R0", 0.0, 40.0) if radius is not None else None
    temperature = _positive(temperature, "온도", 0.0, 1000.0)
    alpha = _positive(alpha, "alpha", 0.0, 2.0)
    limit = int(_positive(limit, "limit", 0, 300))
    rows = solvents()["rows"] if not names else [find_solvent(name) for name in names]
    table = []
    for row in rows:
        ra = float(distance(row["hsp"], center))
        item = {"name": row["name"], "cas": row["cas"], **_point_dict(row["hsp"]), "molar_volume": row["molar_volume"],
                "boiling_point_c": row["boiling_point_c"], "ra": ra, "chi": float(chi(row["molar_volume"], ra, temperature, alpha))}
        if radius is not None:
            item["red"] = ra / radius
            item["prediction"] = "inside" if item["red"] < 1 else "outside"
            item["boundary"] = abs(item["red"] - 1) < 0.1
        table.append(item)
    table.sort(key=lambda r: (r.get("red", r["ra"]), r["name"]))
    result = {"polymer": {**_point_dict(center), "radius": radius}, "temperature_kelvin": temperature, "alpha": alpha,
              "solvents": table[:limit], "evaluated": len(table), "method_version": METHOD_VERSION,
              "solvent_source": source(),
              "notes": ["RED < 1 means inside the solubility sphere (likely dissolves or swells); it is a screening criterion, not a solubility measurement.",
                        "chi uses Lindvig et al. (2002), chi = alpha V Ra^2 / (4RT); compare with about 0.5 for long chains (Flory-Huggins critical value)."]}
    if radius is None:
        result["notes"].insert(0, "R0가 없어 RED를 계산하지 않았습니다. 용해 시험으로 구를 피팅하면 RED 기준 순위를 얻습니다.")
    if blends:
        result["blends"] = solvent_blends(center, radius, rows, int(_positive(blends, "blends", 0, 50)))
    return result


def solvent_blends(center, radius, rows, count):
    """Best volume fraction phi for every solvent pair (mix = phi*A + (1-phi)*B, closed form).

    "closest" lists the mixtures nearest the polymer; with R0, "nonsolvent_pairs" lists pairs
    whose components are both outside the sphere while the mixture is inside (Hansen's classic case).
    """
    points = np.array([row["hsp"] for row in rows])
    single = distance(points, center)
    found = []
    for i in range(len(rows) - 1):
        b = points[i + 1:]
        span = points[i][None, :] - b
        denominator = np.sum(WEIGHTS * span ** 2, axis=1)
        phi = np.clip(np.divide(np.sum(WEIGHTS * (center[None, :] - b) * span, axis=1), denominator,
                                out=np.zeros(len(b)), where=denominator > 0), 0.0, 1.0)
        ra = distance(b + phi[:, None] * span, center)
        own = np.minimum(single[i], single[i + 1:])
        for k in np.flatnonzero((phi > 0.05) & (phi < 0.95) & (ra < own - 0.5)):
            found.append((float(ra[k]), i, i + 1 + int(k), float(phi[k])))
    found.sort()

    def item(ra, i, j, phi):
        entry = {"solvents": [rows[i]["name"], rows[j]["name"]], "volume_fractions": [phi, 1 - phi],
                 **_point_dict(phi * points[i] + (1 - phi) * points[j]), "ra": ra, "component_ra": [float(single[i]), float(single[j])]}
        if radius is not None:
            entry["red"] = ra / radius
        return entry

    out = {"closest": [item(*row) for row in found[:count]]}
    if radius is not None:
        pairs = [row for row in found if row[0] < radius and single[row[1]] >= radius and single[row[2]] >= radius]
        out["nonsolvent_pairs"] = [item(*row) for row in pairs[:count]]
    return out


def _resolve_tests(tests):
    if not isinstance(tests, list) or not tests:
        raise ValueError("용해 시험 결과 목록이 필요합니다.")
    points, labels, names = [], [], []
    for number, test in enumerate(tests, 1):
        if not isinstance(test, dict):
            raise ValueError(f"{number}번째 시험: 객체가 필요합니다.")
        if all(key in test for key in ("delta_d", "delta_p", "delta_h")):
            point, name = hsp_point(test, f"{number}번째 시험"), str(test.get("solvent") or test.get("name") or f"solvent {number}")
        else:
            row = find_solvent(test.get("solvent") or test.get("name"))
            point, name = row["hsp"], row["name"]
        good = test.get("good")
        if not isinstance(good, bool):
            raise ValueError(f"{name}: good(true=용해·팽윤, false=불용)을 지정하세요.")
        points.append(point), labels.append(good), names.append(name)
    if len(set(n.lower() for n in names)) != len(names):
        raise ValueError("같은 용매가 두 번 이상 입력되었습니다. 한 용매에 하나의 판정을 기록하세요.")
    return np.array(points), np.array(labels), names


def _datafit(dist, radii, good):
    """DATAFIT for each candidate radius: geometric mean of per-solvent desirability (Hansen)."""
    delta = dist[:, None] - radii[None, :]
    fitness = np.ones_like(delta)
    miss = good[:, None] & (delta > 0)
    intrude = ~good[:, None] & (delta < 0)
    fitness[miss] = np.exp(-delta[miss])
    fitness[intrude] = np.exp(delta[intrude])
    return np.exp(np.mean(np.log(np.maximum(fitness, 1e-300)), axis=0))


def _best_radius(dist, good):
    radii = np.unique(np.concatenate([dist, dist + 1e-6, [max(dist.min() - 1e-6, 1e-6), dist.max() + 1.0]]))
    radii = radii[radii > 0]
    fit = _datafit(dist, radii, good)
    top = fit.max()
    return float(radii[np.flatnonzero(fit >= top - 1e-12)].min()), float(top)


def fit_sphere(tests, step=1.0):
    """Hansen sphere from good/bad solvent tests: grid seed over the tested region, then cube hill-climb."""
    points, good, names = _resolve_tests(tests)
    if good.sum() == 0:
        raise ValueError("용해·팽윤(good) 판정이 하나 이상 필요합니다.")
    low, high = points.min(axis=0), points.max(axis=0)
    axes = [np.arange(low[k], high[k] + step / 2, step / (2 if k == 0 else 1)) for k in range(3)]
    grid = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
    best = None
    for start in range(0, len(grid), 4096):
        block = grid[start:start + 4096]
        dist = distance(points[None, :, :], block[:, None, :])  # (centers, solvents)
        for center, d in zip(block, dist):
            radius, fit = _best_radius(d, good)
            if best is None or fit > best[2] + 1e-12 or (abs(fit - best[2]) <= 1e-12 and radius < best[1]):
                best = (center, radius, fit)
    center, radius, fit = best
    corners = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], float)
    for edge in (0.5, 0.2, 0.1, 0.05, 0.02):
        moved = True
        while moved:
            moved = False
            for trial in center + edge * corners:
                r, f = _best_radius(distance(points, trial), good)
                if f > fit + 1e-12 or (abs(f - fit) <= 1e-12 and r < radius - 1e-9):
                    center, radius, fit, moved = trial, r, f, True
    ra = distance(points, center)
    radius, fit = _best_radius(ra, good)
    # Solvents on the surface (equal distance up to round-off) count as inside, as their DATAFIT term is 1.
    inside = ra <= radius * (1 + 1e-9)
    rows = [{"solvent": name, **_point_dict(point), "observed": "good" if g else "bad", "ra": float(d), "red": float(d / radius),
             "predicted": "inside" if i else "outside", "correct": bool(i == g)} for name, point, g, d, i in zip(names, points, good, ra, inside)]
    warnings = []
    if len(points) < 10:
        warnings.append(f"시험 용매가 {len(points)}개뿐입니다. 일반적으로 20개 이상, 여러 HSP 영역에 퍼진 용매가 필요합니다.")
    if (~good).sum() == 0:
        warnings.append("불용(bad) 판정이 없어 R0는 하한값일 뿐입니다.")
    if good.sum() < 3:
        warnings.append("용해(good) 판정이 3개 미만이라 중심 위치가 불확실합니다.")
    for k, key in enumerate(("delta_d", "delta_p", "delta_h")):
        if center[k] <= low[k] + 1e-9 or center[k] >= high[k] - 1e-9:
            warnings.append(f"중심의 {key}가 시험 용매 범위의 경계에 있습니다. 그 방향의 용매를 추가하세요.")
    result = {"center": _point_dict(center), "radius": float(radius), "datafit": float(fit),
              "accuracy": float(np.mean(inside == good)), "n_tests": int(len(points)), "n_good": int(good.sum()),
              "wrong_inside": [r["solvent"] for r in rows if not r["correct"] and r["observed"] == "bad"],
              "wrong_outside": [r["solvent"] for r in rows if not r["correct"] and r["observed"] == "good"],
              "tests": rows, "warnings": warnings, "method_version": METHOD_VERSION,
              "method": "Hansen classic sphere fit: maximize DATAFIT (geometric mean desirability), smallest R0 among ties; grid seed then cube hill-climb",
              "solvent_source": source()}
    analysis = {"method_version": METHOD_VERSION, "datafit": result["datafit"], "accuracy": result["accuracy"], "n_tests": result["n_tests"],
                "tests": [{"solvent": r["solvent"], "observed": r["observed"]} for r in rows]}
    result["measurement_drafts"] = [
        {"property": prop, "value": value, "unit": "MPa^0.5", "method": "Hansen solubility sphere fit from solvent tests", "analysis": analysis}
        for prop, value in (("hsp_delta_d", float(center[0])), ("hsp_delta_p", float(center[1])),
                            ("hsp_delta_h", float(center[2])), ("hsp_radius", float(radius)))]
    return result


def blend_compatibility(a, b, temperature=298.15, reference_volume=100.0):
    """Regular-solution chi between two polymers from HSP and Flory-Huggins critical chi."""
    temperature = _positive(temperature, "온도", 0.0, 1000.0)
    reference_volume = _positive(reference_volume, "기준 부피", 0.0, 10000.0)
    parts = []
    for label, polymer in (("A", a), ("B", b)):
        if not isinstance(polymer, dict):
            raise ValueError(f"고분자 {label}: 객체가 필요합니다.")
        point = hsp_point(polymer, f"고분자 {label} HSP")
        volume, volume_source = polymer.get("molar_volume"), "input"
        if volume is None and polymer.get("repeat_smiles"):
            volume, volume_source = _repeat_volume(polymer["repeat_smiles"])
        volume = _positive(volume, f"고분자 {label} 반복단위 몰부피 (cm^3/mol)", 0.0, 5000.0)
        degree = _positive(polymer.get("degree_of_polymerization"), f"고분자 {label} 중합도", 0.0, 1e6)
        parts.append({"label": label, "name": str(polymer.get("name") or label), "hsp": point, "molar_volume": volume,
                      "molar_volume_source": volume_source, "degree_of_polymerization": degree,
                      "segments": degree * volume / reference_volume})
    ra = float(distance(parts[0]["hsp"], parts[1]["hsp"]))
    value = float(chi(reference_volume, ra, temperature, 1.0))
    critical = 0.5 * (1 / math.sqrt(parts[0]["segments"]) + 1 / math.sqrt(parts[1]["segments"])) ** 2
    return {"polymers": [{**{k: v for k, v in p.items() if k != "hsp"}, **_point_dict(p["hsp"])} for p in parts],
            "ra": ra, "chi": value, "chi_critical": critical, "chi_over_critical": value / critical,
            "prediction": "miscible" if value < critical else "phase_separation",
            "temperature_kelvin": temperature, "reference_volume": reference_volume, "method_version": METHOD_VERSION,
            "method": "chi = V_ref Ra^2 / (4RT) (regular solution, alpha = 1); chi_c = (N_A^-1/2 + N_B^-1/2)^2 / 2 with N = DP V_repeat / V_ref",
            "warnings": ["정규 용액 chi는 항상 0 이상이므로 수소결합·이온 상호작용으로 섞이는 블렌드를 비상용성으로 잘못 판정할 수 있습니다.",
                         "chi의 온도 의존성(LCST/UCST), 결정화, 분자량 분포는 고려하지 않습니다."]}


def _repeat_volume(smiles):
    """Repeat-unit molar volume from RDKit mass and the PI1070 surrogate density (300 K MD)."""
    from rdkit.Chem import Descriptors
    from .screening import SURROGATE, repeat_unit
    mass = Descriptors.MolWt(repeat_unit(smiles))  # dummy attachment atoms carry zero mass
    density = SURROGATE.predict(smiles, ["bulk_density"])["values"]["bulk_density"]["value"]
    return mass / density, f"PI1070 surrogate density {density:.3f} g/cm^3 at 300 K and repeat-unit mass {mass:.2f} g/mol"
