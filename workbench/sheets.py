"""Paste-to-run sheets: a line-based, declarative format that turns pasted text into a Blends chi
screen or a batch of jobs. A sheet only names settings; nothing in it is evaluated as code, and every
job passes the same checks as one submitted through the job API. Format: docs/RUN_SHEETS.md.

    # Polymer Lab 실행표 v1
    종류: blends
    이름: PS 용매 스크리닝
    기준: 폴리스타이렌 | [*]CC([*])c1ccccc1
    대상: 톨루엔 | Cc1ccccc1
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid

from pydantic import ValidationError

from .jobs import atomic_json, now
from .models import JobConfig

HEADER = "# polymer lab 실행표 v1"
MAX_TEXT = 100_000
MAX_LINES = 2000
MAX_PAIRS = 100
MAX_JOBS = 50
MAX_NAME = 80
TERMINAL = {"completed", "failed", "blocked", "cancelled", "prepared"}

KINDS = {"blends": "blends", "블렌드": "blends", "작업": "jobs", "jobs": "jobs"}
SHARED_KEYS = {"종류": "kind", "kind": "kind", "이름": "name", "name": "name", "메모": "note", "note": "note"}
BLEND_KEYS = {"기준": "base", "base": "base", "대상": "screen", "screen": "screen", "온도": "temperatures",
              "temperatures": "temperatures", "표본": "samples", "samples": "samples", "클러스터": "clusters",
              "clusters": "clusters", "시드": "seed", "seed": "seed", "말단": "cap", "cap": "cap",
              "접촉배율": "contact_scale", "contact_scale": "contact_scale"}
JOB_KEYS = {"공통": "common", "common": "common", "작업": "job", "job": "job"}
REPEATABLE = {"base", "screen", "common", "job"}
CAPS = {"수소": "hydrogen", "hydrogen": "hydrogen", "메틸": "methyl", "methyl": "methyl"}
# Large free-text inputs belong in the app's own forms, not in a pasted line.
FORBIDDEN_FIELDS = {"name", "engine_input", "mdp_text", "forcefield_itp_text", "pdb_text", "a3m_text"}
BLEND_DEFAULTS = {"samples": 200000, "clusters": 2000, "seed": 1, "cap": "hydrogen", "contact_scale": 1.0}
BLEND_LIMITS = {"samples": (1000, 2_000_000), "clusters": (20, 5000), "seed": (1, 2**31 - 1), "contact_scale": (0.8, 1.5)}
LABELS = {"samples": "표본", "clusters": "클러스터", "seed": "시드", "contact_scale": "접촉배율"}
BLEND_WARNINGS = [
    "Blends-style chi is enthalpic, from rigid single conformers with Gasteiger charges; against Hansen chi_H its rank "
    "correlation was 0.65 with wrong signs for aliphatic solvents of aromatic polymers (docs/BLENDS.md). Treat the ranking as exploratory.",
    "Pairs are ranked by chi at the sheet temperature closest to 298.15 K; lower chi favours mixing but does not establish miscibility.",
]

TEMPLATES = [
    {"id": "blends_solvents", "title": "PS 용매 스크리닝 (Blends χ)",
     "text": """# Polymer Lab 실행표 v1
종류: blends
이름: PS 용매 스크리닝
메모: 폴리스타이렌 반복단위와 용매 다섯 개의 χ(T). χ가 낮을수록 섞이는 쪽입니다.
기준: 폴리스타이렌 | [*]CC([*])c1ccccc1
대상: 톨루엔 | Cc1ccccc1
대상: 사이클로헥세인 | C1CCCCC1
대상: 아세톤 | CC(=O)C
대상: 에탄올 | CCO
대상: 헥세인 | CCCCCC
온도: 250~450/25
표본: 200000
클러스터: 2000
시드: 1
"""},
    {"id": "blends_polymers", "title": "고분자 블렌드 χ (중합도 포함)",
     "text": """# Polymer Lab 실행표 v1
종류: blends
이름: PS 블렌드 상대 비교
메모: 반복단위 χ와 중합도 100으로 본 Flory-Huggins 임계점입니다.
기준: 폴리스타이렌 | [*]CC([*])c1ccccc1 | 중합도=100
대상: PMMA | [*]CC([*])(C)C(=O)OC | 중합도=100
대상: 폴리(비닐 메틸 에테르) | [*]CC([*])OC | 중합도=100
대상: 폴리부타디엔 | [*]CC=CC[*] | 중합도=100
온도: 300, 350, 400, 450
"""},
    {"id": "jobs_bulk", "title": "GAFF2 벌크 셀 비교 (작업 묶음)",
     "text": """# Polymer Lab 실행표 v1
종류: 작업
이름: 벌크 밀도 비교
메모: 짧은 탐색용 프로토콜(protocol_scale 0.05)입니다. 평형 밀도를 주장하지 않습니다.
공통: route=polymer_atomistic | atomistic_mode=bulk | atomistic_forcefield=gaff2 | cell_builder=native
공통: chains=8 | monomers=10 | protocol_scale=0.05 | temperature_kelvin=300
작업: PE | repeat_smiles=[*]CC[*]
작업: PS | repeat_smiles=[*]CC([*])c1ccccc1
작업: PMMA | repeat_smiles=[*]CC([*])(C)C(=O)OC
작업: PS 350K | repeat_smiles=[*]CC([*])c1ccccc1 | temperature_kelvin=350
"""},
]


class SheetError(ValueError):
    """Every problem found in a sheet, each with its line number."""
    def __init__(self, errors):
        self.errors = errors
        super().__init__(" ".join(_message(error) for error in errors[:10]) + (f" 외 {len(errors) - 10}건" if len(errors) > 10 else ""))


def _message(error):
    return f"{error['line']}번째 줄: {error['message']}" if error.get("line") else error["message"]


def _number(text, integer=False):
    cleaned = text.strip().replace("_", "")
    if integer:
        cleaned = cleaned.replace(",", "")
        if not re.fullmatch(r"[+-]?\d+", cleaned):
            raise ValueError("정수가 필요합니다")
        return int(cleaned)
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError("숫자가 필요합니다") from None
    if not math.isfinite(value):
        raise ValueError("유한한 숫자가 필요합니다")
    return value


def _temperatures(text):
    text = text.replace("K", "").replace("k", "").strip()
    span = re.fullmatch(r"([\d.]+)\s*~\s*([\d.]+)\s*/\s*([\d.]+)", text)
    if span:
        start, stop, step = (_number(part) for part in span.groups())
        if step <= 0 or stop < start:
            raise ValueError("범위는 '시작~끝/간격'이고 끝이 시작보다 크거나 같아야 합니다")
        count = int((stop - start) / step + 1e-9) + 1
        if count > 200:
            raise ValueError("온도는 200개까지입니다")
        values = [round(start + i * step, 6) for i in range(count)]
    else:
        values = [_number(part) for part in re.split(r"[,\s]+", text) if part]
    if not values or len(values) > 200:
        raise ValueError("온도는 1~200개입니다")
    if any(not 50 <= value <= 1000 for value in values):
        raise ValueError("온도는 50~1000 K입니다")
    if len(set(values)) != len(values):
        raise ValueError("같은 온도가 두 번 있습니다")
    return values


def _lines(text):
    """(line number, key, value) for every setting line after the header, and the malformed lines.

    An empty or oversized sheet or a missing header raises SheetError at once."""
    if not isinstance(text, str) or not text.strip():
        raise SheetError([{"line": None, "message": "실행표가 비어 있습니다."}])
    if len(text) > MAX_TEXT:
        raise SheetError([{"line": None, "message": f"실행표는 {MAX_TEXT:,}자까지입니다."}])
    rows = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(rows) > MAX_LINES:
        raise SheetError([{"line": None, "message": f"실행표는 {MAX_LINES:,}줄까지입니다."}])
    start = next((i for i, row in enumerate(rows) if row.strip() and not row.strip().startswith("```")), None)
    if start is None:
        raise SheetError([{"line": None, "message": "실행표가 비어 있습니다."}])
    if " ".join(rows[start].split()).lower() != HEADER:
        raise SheetError([{"line": start + 1, "message": "첫 줄은 '# Polymer Lab 실행표 v1'이어야 합니다."}])
    out, errors = [], []
    for number, row in enumerate(rows[start + 1:], start + 2):
        row = row.strip()
        # Markdown code fences come along when a sheet is copied out of a chat message.
        if not row or row.startswith("#") or row.startswith("```"):
            continue
        key, sep, value = row.replace("：", ":", 1).partition(":")
        if not sep or not key.strip():
            errors.append({"line": number, "message": "'키: 값' 형식이 아닙니다."})
            continue
        out.append((number, key.strip().lower(), value.strip()))
    return out, errors


def _species(value, number, errors):
    parts = [part.strip() for part in value.split("|")]
    options = [part for part in parts[1:] if re.fullmatch(r"(중합도|n)\s*=.*", part, flags=re.IGNORECASE)]
    names = [part for part in parts if part not in options]
    if len(names) > 2:
        errors.append({"line": number, "message": f"'{names[2]}'은(는) 알 수 없는 항목입니다. '이름 | SMILES | 중합도=100' 형식으로 쓰세요."})
        return None
    name, smiles = (names[0], names[1]) if len(names) == 2 else (names[0], names[0])
    n = 1.0
    for option in options:
        match = re.fullmatch(r"(중합도|n)\s*=\s*(\S+)", option, flags=re.IGNORECASE)
        try:
            if not match:
                raise ValueError
            n = _number(match.group(2))
            if not 1 <= n <= 1e6:
                raise ValueError
        except ValueError:
            errors.append({"line": number, "message": "중합도는 1~1,000,000 사이 숫자입니다."})
    if not name or not smiles:
        errors.append({"line": number, "message": "'이름 | SMILES' 형식으로 쓰세요."})
        return None
    if len(name) > MAX_NAME or len(smiles) > 500:
        errors.append({"line": number, "message": f"이름은 {MAX_NAME}자, SMILES는 500자까지입니다."})
        return None
    try:
        from .blends import capped
        capped(smiles, "hydrogen")
    except ImportError:
        pass
    except Exception as exc:  # RDKit sanitization errors are not all ValueError
        errors.append({"line": number, "message": f"{name}: {exc}" if isinstance(exc, ValueError) else f"{name}: SMILES를 해석할 수 없습니다."})
        return None
    return {"name": name, "smiles": smiles, "n": n, "line": number}


def _job_fields(parts, number, errors):
    fields = {}
    for part in parts:
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            errors.append({"line": number, "message": f"'{part}'은(는) '설정=값' 형식이 아닙니다."})
        elif key == "name":
            errors.append({"line": number, "message": "작업 이름은 '작업: 이름 | 설정=값'의 첫 칸에 씁니다."})
        elif key in FORBIDDEN_FIELDS:
            errors.append({"line": number, "message": f"{key} 같은 긴 입력은 실행표에 넣지 않습니다. 작업 화면에서 입력하세요."})
        elif key not in JobConfig.model_fields:
            errors.append({"line": number, "message": f"알 수 없는 설정 이름입니다: {key}"})
        elif not value:
            errors.append({"line": number, "message": f"{key}의 값이 비어 있습니다."})
        elif key in fields:
            errors.append({"line": number, "message": f"{key}가 한 줄에 두 번 있습니다."})
        else:
            fields[key] = value
    return fields


def _korean(error):
    kind, ctx, field = error["type"], error.get("ctx") or {}, ".".join(str(p) for p in error["loc"])
    if kind == "value_error":
        return str(error["msg"]).removeprefix("Value error, ")
    texts = {"less_than_equal": f"{ctx.get('le')} 이하여야 합니다", "greater_than_equal": f"{ctx.get('ge')} 이상이어야 합니다",
             "less_than": f"{ctx.get('lt')}보다 작아야 합니다", "greater_than": f"{ctx.get('gt')}보다 커야 합니다",
             "int_parsing": "정수여야 합니다", "int_from_float": "정수여야 합니다", "float_parsing": "숫자여야 합니다",
             "bool_parsing": "true 또는 false여야 합니다", "literal_error": f"다음 중 하나여야 합니다: {ctx.get('expected')}",
             "string_too_long": f"{ctx.get('max_length')}자 이하여야 합니다", "string_pattern_mismatch": "형식이 맞지 않습니다",
             "finite_number": "유한한 숫자여야 합니다"}
    return f"{field} 값 {error.get('input')!s:.40}: {texts.get(kind, '값이 맞지 않습니다')}"


def _check_jobs(jobs, common, common_lines, manager, errors, warnings):
    from .preflight import check, prepare_submission
    sheet_name = jobs and jobs[0]["sheet_name"]
    for job in jobs:
        values = {**common, **job["fields"]}
        title = f"{sheet_name} · {job['name']}"
        values["name"] = title if len(title) <= 120 else job["name"]
        line_of = lambda field: job["line"] if field in job["fields"] else common_lines.get(field, job["line"])  # noqa: E731
        try:
            cfg = JobConfig.model_validate(values)
        except ValidationError as exc:
            for error in exc.errors():
                field = str(error["loc"][0]) if error["loc"] else ""
                # A bad shared setting is reported once, on its own line, not once per job.
                shared = field in common_lines and field not in job["fields"]
                problem = {"line": line_of(field), "message": f"[{'공통' if shared else job['name']}] {_korean(error)}"}
                if problem not in errors:
                    errors.append(problem)
            continue
        try:
            if manager is None:
                report = check(cfg.model_dump())
                if report["errors"]:
                    raise ValueError(" ".join(e["message"] for e in report["errors"]))
            else:
                cfg, report = prepare_submission(manager, cfg)
        except KeyError:
            errors.append({"line": line_of("source_job_id"), "message": f"[{job['name']}] source_job_id의 작업을 찾을 수 없습니다."})
            continue
        except ValueError as exc:
            errors.append({"line": job["line"], "message": f"[{job['name']}] {exc}"})
            continue
        job["config"] = cfg.model_dump(exclude={"pdb_text"})
        job["estimate"] = report.get("estimate")
        job["warnings"] = report.get("warnings", [])
        warnings.extend(w for w in job["warnings"] if w not in warnings and w.startswith("Missing engines"))


def inspect(text, manager=None):
    """Parse and check a sheet without starting anything. Returns the plan; raises SheetError listing every problem."""
    lines, errors = _lines(text)
    warnings, single = [], {}
    kind_line = next(((n, v) for n, k, v in lines if SHARED_KEYS.get(k) == "kind"), None)
    if kind_line is None:
        raise SheetError([{"line": None, "message": "'종류: blends' 또는 '종류: 작업' 줄이 필요합니다."}])
    kind = KINDS.get(kind_line[1].strip().lower())
    if kind is None:
        raise SheetError([{"line": kind_line[0], "message": "종류는 blends 또는 작업입니다."}])
    keys = {**SHARED_KEYS, **(BLEND_KEYS if kind == "blends" else JOB_KEYS)}
    repeated = {"base": [], "screen": [], "common": [], "job": []}
    for number, key, value in lines:
        canonical = keys.get(key)
        if canonical is None:
            allowed = ", ".join(k for k in keys if not k.isascii())
            errors.append({"line": number, "message": f"'{key}'은(는) 이 종류에서 쓰지 않는 키입니다. 쓸 수 있는 키: {allowed}"})
        elif canonical in REPEATABLE:
            repeated[canonical].append((number, value))
        elif canonical in single:
            errors.append({"line": number, "message": f"'{key}'는 한 번만 씁니다({single[canonical][0]}번째 줄에 이미 있음)."})
        else:
            single[canonical] = (number, value)
    name = single.get("name", (None, ""))[1] or "이름 없는 실행표"
    if len(name) > MAX_NAME:
        errors.append({"line": single["name"][0], "message": f"이름은 {MAX_NAME}자까지입니다."})
    plan = {"format": "Polymer Lab 실행표 v1", "kind": kind, "name": name[:MAX_NAME], "note": single.get("note", (None, ""))[1][:500]}
    if kind == "blends":
        plan.update(_inspect_blends(single, repeated, errors))
    else:
        plan.update(_inspect_jobs(name[:MAX_NAME], repeated, manager, errors, warnings))
    if errors:
        raise SheetError(sorted(errors, key=lambda e: e["line"] or 0))
    plan["warnings"] = warnings + (BLEND_WARNINGS if kind == "blends" else [])
    return plan


def _inspect_blends(single, repeated, errors):
    settings = dict(BLEND_DEFAULTS, temperatures=None)
    for key in ("samples", "clusters", "seed", "contact_scale"):
        if key in single:
            number, value = single[key]
            low, high = BLEND_LIMITS[key]
            try:
                parsed = _number(value, integer=key != "contact_scale")
                if not low <= parsed <= high:
                    raise ValueError
                settings[key] = parsed
            except ValueError:
                errors.append({"line": number, "message": f"{LABELS[key]}는 {low:,}~{high:,} 사이 {'숫자' if key == 'contact_scale' else '정수'}입니다."})
    if "cap" in single:
        number, value = single["cap"]
        settings["cap"] = CAPS.get(value.strip().lower())
        if settings["cap"] is None:
            errors.append({"line": number, "message": "말단은 수소 또는 메틸입니다."})
    if "temperatures" in single:
        number, value = single["temperatures"]
        try:
            settings["temperatures"] = _temperatures(value)
        except ValueError as exc:
            errors.append({"line": number, "message": f"온도: {exc}."})
    try:
        from .blends import native
        if native is None and settings["samples"] * 4 > 200_000:
            errors.append({"line": single.get("samples", (None,))[0], "message": "Rust 커널이 없어 numpy로 계산합니다. 표본을 50,000 이하로 줄이거나 커널을 빌드하세요."})
    except ImportError:
        pass
    sides = {}
    for side, label in (("base", "기준"), ("screen", "대상")):
        species = [s for s in (_species(value, number, errors) for number, value in repeated[side]) if s]
        if not repeated[side]:
            errors.append({"line": None, "message": f"'{label}:' 줄이 하나 이상 필요합니다."})
        seen = set()
        for s in species:
            if s["name"] in seen:
                errors.append({"line": s["line"], "message": f"{label} 이름 '{s['name']}'이(가) 겹칩니다."})
            seen.add(s["name"])
        sides[side] = species
    pairs = [{"base": b, "screen": s} for b in sides["base"] for s in sides["screen"]]
    if len(pairs) > MAX_PAIRS:
        errors.append({"line": None, "message": f"기준 × 대상 조합은 {MAX_PAIRS}개까지입니다(현재 {len(pairs)}개)."})
    for pair in pairs:
        if pair["base"]["smiles"] == pair["screen"]["smiles"]:
            errors.append({"line": pair["screen"]["line"], "message": f"{pair['base']['name']}와 {pair['screen']['name']}의 SMILES가 같습니다."})
    return {"settings": settings, "items": [{"index": i, **pair} for i, pair in enumerate(pairs)],
            "estimate": {"pairs": len(pairs), "contact_samples": len(pairs) * 4 * settings["samples"],
                         "note": "Run time is not estimated; it depends on the machine and the kernel."}}


def _inspect_jobs(sheet_name, repeated, manager, errors, warnings):
    common, common_lines = {}, {}
    for number, value in repeated["common"]:
        for key, field in _job_fields(value.split("|"), number, errors).items():
            if key in common:
                errors.append({"line": number, "message": f"공통 설정 {key}가 {common_lines[key]}번째 줄에 이미 있습니다."})
            common[key], common_lines[key] = field, number
    jobs = []
    for number, value in repeated["job"]:
        parts = value.split("|")
        title = parts[0].strip()
        if not title or "=" in title:
            errors.append({"line": number, "message": "'작업: 이름 | 설정=값 | ...' 형식으로 쓰세요. 첫 칸은 작업 이름입니다."})
            continue
        if len(title) > MAX_NAME:
            errors.append({"line": number, "message": f"작업 이름은 {MAX_NAME}자까지입니다."})
            continue
        if any(job["name"] == title for job in jobs):
            errors.append({"line": number, "message": f"작업 이름 '{title}'이(가) 겹칩니다."})
            continue
        jobs.append({"name": title, "line": number, "fields": _job_fields(parts[1:], number, errors), "sheet_name": sheet_name})
    if not repeated["job"]:
        errors.append({"line": None, "message": "'작업:' 줄이 하나 이상 필요합니다."})
    if len(jobs) > MAX_JOBS:
        errors.append({"line": None, "message": f"작업은 한 실행표에 {MAX_JOBS}개까지입니다(현재 {len(jobs)}개)."})
    elif not errors:
        _check_jobs(jobs, common, common_lines, manager, errors, warnings)
    items = [{"index": i, "name": job["name"], "line": job["line"], "fields": job["fields"], "config": job.get("config"),
              "estimate": job.get("estimate"), "warnings": job.get("warnings", [])} for i, job in enumerate(jobs)]
    return {"common": common, "items": items, "estimate": {"jobs": len(items),
            "note": "Jobs run one at a time in the MD queue; run time is not estimated."}}


def preview(text, manager=None):
    """The response for a pasted sheet: valid with the plan, or the line-numbered problems."""
    try:
        return {"valid": True, "errors": [], **inspect(text, manager)}
    except SheetError as exc:
        return {"valid": False, "errors": [{"line": e["line"], "message": e["message"], "text": _message(e)} for e in exc.errors],
                "items": [], "warnings": []}


def _summary(item, result):
    reference = result["chi_reference"]
    return {"chi_reference": reference["chi"], "chi_reference_temperature_kelvin": reference["temperature_kelvin"],
            "bootstrap_std": reference["bootstrap_std"], "A": result["fit"]["A"], "B_kelvin": result["fit"]["B_kelvin"],
            "critical_chi": result["phase"]["critical_chi"], "critical_temperature_kelvin": result["phase"]["critical_temperature_kelvin"],
            "phase_kind": result["phase"]["kind"],
            "chi": {f"{row['temperature_kelvin']:g}": row["chi"] for row in result["chi_vs_temperature"]}}


def ranking(items):
    done = sorted((item for item in items if item["status"] == "completed"), key=lambda item: item["result"]["chi_reference"])
    return [{"rank": rank, "index": item["index"], "base": item["base"]["name"], "screen": item["screen"]["name"],
             "chi_reference": item["result"]["chi_reference"], "bootstrap_std": item["result"]["bootstrap_std"]}
            for rank, item in enumerate(done, 1)]


class SheetManager:
    """Stores pasted sheets under research/sheets/<id>/ and runs them: Blends pairs one after another in the
    analysis lane, jobs through the job manager with the same pre-submission checks as the job API."""

    def __init__(self, research_root, manager, execution):
        self.root = Path(research_root) / "sheets"
        self.root.mkdir(parents=True, exist_ok=True)
        self.manager, self.execution = manager, execution
        self.lock = threading.RLock()
        self.events, self.threads = {}, {}
        self.closed = False
        for path in self.root.glob("*/record.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("status") in {"queued", "running", "submitting"}:
                record.update(status="interrupted", interrupted_at=now())
                for item in record.get("items", []):
                    if item.get("status") == "running":
                        item["status"] = "pending"
                atomic_json(path, record)

    def _path(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier or ""):
            raise KeyError(identifier)
        path = self.root / identifier / "record.json"
        if not path.is_file():
            raise KeyError(identifier)
        return path

    def _read(self, identifier):
        return json.loads(self._path(identifier).read_text(encoding="utf-8"))

    def _write(self, record):
        atomic_json(self.root / record["id"] / "record.json", record)

    def _update(self, identifier, change):
        with self.lock:
            record = self._read(identifier)
            change(record)
            self._write(record)
            return record

    def create(self, text, submitted_via=None):
        plan = inspect(text, self.manager)
        identifier = uuid.uuid4().hex
        folder = self.root / identifier
        folder.mkdir()
        raw = text.encode("utf-8")
        (folder / "sheet.txt").write_bytes(raw)
        record = {"id": identifier, "kind": plan["kind"], "name": plan["name"], "note": plan["note"], "created_at": now(),
                  "submitted_via": submitted_via, "sheet_sha256": hashlib.sha256(raw).hexdigest(), "status": "queued",
                  "warnings": plan["warnings"], "estimate": plan["estimate"], "finished_at": None}
        if plan["kind"] == "blends":
            (folder / "pairs").mkdir()
            record["settings"] = plan["settings"]
            record["items"] = [{**item, "status": "pending", "error": None, "result": None} for item in plan["items"]]
            self._write(record)
            self._start(identifier)
        else:
            record["common"] = plan["common"]
            record["items"] = [{**item, "job_id": uuid.uuid4().hex} for item in plan["items"]]
            record["status"] = "submitting"
            self._write(record)
            self._submit_jobs(identifier)
        return self.get(identifier)

    def _submit_jobs(self, identifier):
        from .preflight import prepare_submission
        record = self._read(identifier)
        try:
            for item in record["items"]:
                # Source files are read again at submission; reserved job IDs make a resumed submission idempotent.
                cfg, _ = prepare_submission(self.manager, JobConfig.model_validate(item["config"]))
                self.manager.create(cfg.model_dump(), submitted_via="sheet:" + identifier, job_id=item["job_id"])
        except (ValueError, KeyError, RuntimeError) as exc:
            self._update(identifier, lambda r: r.update(status="interrupted", error=str(exc), interrupted_at=now()))
            raise
        self._update(identifier, lambda r: r.update(status="submitted", submitted_at=now(), error=None))

    def _start(self, identifier):
        with self.lock:
            if self.closed:
                raise RuntimeError("서버가 종료 중입니다.")
            if identifier in self.threads and self.threads[identifier].is_alive():
                raise RuntimeError("이 실행표는 이미 실행 중입니다.")
            event = threading.Event()
            thread = threading.Thread(target=self._run_blends, args=(identifier, event), name=f"sheet-{identifier[:8]}", daemon=True)
            self.events[identifier], self.threads[identifier] = event, thread
            thread.start()

    def _run_blends(self, identifier, event):
        from .execution import ExecutionBusy, ExecutionCancelled
        stopped = lambda: event.is_set() or self.closed  # noqa: E731
        record = self._update(identifier, lambda r: r.update(status="running", started_at=r.get("started_at") or now()))
        settings = record["settings"]
        for item in record["items"]:
            if item["status"] in {"completed", "failed"}:
                continue
            if stopped():
                break
            index = item["index"]
            self._update(identifier, lambda r: r["items"][index].update(status="running", error=None))
            payload = {"base_smiles": item["base"]["smiles"], "screen_smiles": item["screen"]["smiles"],
                       "temperatures": settings["temperatures"], "samples": settings["samples"], "clusters": settings["clusters"],
                       "seed": settings["seed"], "cap": settings["cap"], "contact_scale": settings["contact_scale"],
                       "n_base": item["base"]["n"], "n_screen": item["screen"]["n"]}
            while True:
                try:
                    result = self.execution.compute("blend_contacts", payload, owner="sheet:" + identifier, cancelled=stopped)
                    outcome = {"status": "completed", "result": _summary(item, result), "finished_at": now()}
                    atomic_json(self.root / identifier / "pairs" / f"{index}.json", result)
                except ExecutionBusy:
                    if not stopped():
                        time.sleep(5)
                        continue
                    outcome = {"status": "pending"}
                except ExecutionCancelled:
                    outcome = {"status": "pending"}
                except (ValueError, RuntimeError, OSError) as exc:
                    outcome = {"status": "failed", "error": str(exc), "finished_at": now()}
                break
            self._update(identifier, lambda r: r["items"][index].update(outcome))
            if outcome["status"] == "pending":
                break  # cancelled here, by the execution queue or by shutdown
        def finish(r):
            statuses = [item["status"] for item in r["items"]]
            if "pending" in statuses:
                r["status"] = "cancelled" if event.is_set() else "interrupted"
            else:
                r["status"] = "failed" if all(s == "failed" for s in statuses) else "completed"
            r["finished_at"] = now()
            r["ranking"] = ranking(r["items"])
        record = self._update(identifier, finish)
        self._write_csv(record)

    def _write_csv(self, record):
        from .reports import write_csv
        temperatures = []
        for item in record["items"]:
            for key in (item.get("result") or {}).get("chi", {}):
                if key not in temperatures:
                    temperatures.append(key)
        ranks = {row["index"]: row["rank"] for row in record.get("ranking", [])}
        columns = ["rank", "base", "base_smiles", "base_n", "screen", "screen_smiles", "screen_n", "status",
                   "chi_reference_temperature_K", "chi_reference", "chi_reference_bootstrap_std", "A", "B_K",
                   "critical_chi", "critical_temperature_K", "phase_kind"] + [f"chi_{t}K" for t in temperatures] + ["error"]
        rows = []
        for item in sorted(record["items"], key=lambda i: (ranks.get(i["index"], 10**6), i["index"])):
            result = item.get("result") or {}
            rows.append([ranks.get(item["index"], ""), item["base"]["name"], item["base"]["smiles"], item["base"]["n"],
                         item["screen"]["name"], item["screen"]["smiles"], item["screen"]["n"], item["status"],
                         result.get("chi_reference_temperature_kelvin", ""), result.get("chi_reference", ""), result.get("bootstrap_std", ""),
                         result.get("A", ""), result.get("B_kelvin", ""), result.get("critical_chi", ""),
                         "" if result.get("critical_temperature_kelvin") is None else result["critical_temperature_kelvin"],
                         result.get("phase_kind") or ""] + [result.get("chi", {}).get(t, "") for t in temperatures] + [item.get("error") or ""])
        write_csv(self.root / record["id"] / "results.csv", columns, rows)

    def _jobs_view(self, record):
        counts = {}
        for item in record["items"]:
            try:
                job = json.loads((self.manager.directory(item["job_id"]) / "job.json").read_text())
                item["status"], item["error"] = job["status"], job.get("error")
            except (KeyError, OSError, ValueError):
                item["status"], item["error"] = "not_created", None
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        record["counts"] = counts
        if record["status"] == "submitted":
            active = any(item["status"] in {"queued", "running"} for item in record["items"])
            record["progress"] = "running" if active else "finished"
        return record

    def get(self, identifier):
        with self.lock:
            record = self._read(identifier)
        if record["kind"] == "jobs":
            record = self._jobs_view(record)
        else:
            record["counts"] = {}
            for item in record["items"]:
                record["counts"][item["status"]] = record["counts"].get(item["status"], 0) + 1
            record["ranking"] = ranking(record["items"])
        record["csv_url"] = f"/api/research/sheets/{identifier}/csv" if (self.root / identifier / "results.csv").is_file() else None
        return record

    def list(self):
        rows = []
        for path in self.root.glob("*/record.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append({key: record.get(key) for key in ("id", "kind", "name", "status", "created_at", "finished_at")} | {"items": len(record.get("items", []))})
        return sorted(rows, key=lambda r: r["created_at"] or "", reverse=True)

    def text(self, identifier):
        self._path(identifier)
        return (self.root / identifier / "sheet.txt").read_text(encoding="utf-8")

    def csv_path(self, identifier):
        self._path(identifier)
        path = self.root / identifier / "results.csv"
        if not path.is_file():
            raise KeyError(identifier)
        return path

    def cancel(self, identifier):
        record = self._read(identifier)
        if record["kind"] == "blends":
            event = self.events.get(identifier)
            if event is not None:
                event.set()
            self.execution.cancel(owner="sheet:" + identifier)
            thread = self.threads.get(identifier)
            if thread is not None:
                thread.join(timeout=10)
            if record["status"] in {"interrupted", "queued"}:
                self._update(identifier, lambda r: r.update(status="cancelled", finished_at=now()))
        else:
            for item in record["items"]:
                try:
                    self.manager.cancel(item["job_id"])
                except KeyError:
                    pass
            if record["status"] in {"interrupted", "submitting"}:
                self._update(identifier, lambda r: r.update(status="cancelled", finished_at=now()))
        return self.get(identifier)

    def resume(self, identifier):
        """Continue an interrupted or cancelled sheet: Blends pairs that never finished, or jobs never created."""
        record = self._read(identifier)
        if record["status"] not in {"interrupted", "cancelled"}:
            raise RuntimeError("중단되었거나 취소된 실행표만 이어서 실행합니다.")
        if record["kind"] == "blends":
            if not any(item["status"] == "pending" for item in record["items"]):
                raise RuntimeError("남은 조합이 없습니다.")
            self._update(identifier, lambda r: r.update(status="queued", finished_at=None))
            self._start(identifier)
        else:
            missing = [item for item in self._jobs_view(record)["items"] if item["status"] == "not_created"]
            if not missing:
                raise RuntimeError("만들지 못한 작업이 없습니다. 취소된 작업은 작업 화면에서 다시 실행하세요.")
            self._update(identifier, lambda r: r.update(status="submitting"))
            self._submit_jobs(identifier)
        return self.get(identifier)

    def close(self):
        with self.lock:
            self.closed = True
            threads = list(self.threads.values())
        for thread in threads:
            thread.join(timeout=10)
