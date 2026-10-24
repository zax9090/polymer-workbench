"""Immutable research reports: one-page summary, figures, tables and a verified Drive folder upload.

A report copies numbers and review flags from saved records; it never fits, reviews or verifies anything itself.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import html
import io
import json
import logging
import math
from pathlib import Path
import re
import tempfile
import threading
import uuid
import warnings as python_warnings
import zipfile
from xml.sax.saxutils import escape as xml_escape

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict

from .jobs import atomic_json, now

router = APIRouter(prefix="/api/research", tags=["Research reports"])
IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
DESTINATION = "PolymerLab-reports"
KINDS = ("doe", "workflow", "lab", "density_study")
SCHEMA_VERSION = 1
# Single-hue blue ramp for a white page: low response light, high response dark.
RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
POINT, MUTED, REFERENCE, INK = "#2a78d6", "#b9c3cc", "#c47f1a", "#33404b"
TABLE_PREVIEW = 12
MAX_INPUT_BYTES = 20 * 1024 * 1024
_FIGURE_LOCK = threading.Lock()
_UPLOAD_LOCK = threading.Lock()

TITLES = {"doe_analysis": "반응표면 분석", "doe_mixture_analysis": "혼합물 분석", "doe_desirability": "다중 반응 만족도 최적화",
          "doe_design": "실험 설계", "doe_mixture_design": "혼합물 실험 설계", "bulk_workflow": "단계별 연구 흐름",
          "lab": "연구 데이터 마스터 표"}
MODELS = {"linear": "주효과", "interaction": "주효과 + 2인자 상호작용", "quadratic": "2차 반응표면",
          "scheffe_linear": "Scheffé 1차", "scheffe_quadratic": "Scheffé 2차", "scheffe_special_cubic": "Scheffé 특수 3차"}
OBJECTIVES = {"maximize": "최대화", "minimize": "최소화", "target": "목표값"}
ANOVA = {"regression": "회귀 모델", "residual": "잔차", "lack_of_fit": "적합 결여", "pure_error": "순수 오차", "total": "전체"}


def _num(value):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—" if value is None else str(value)
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return "—"
    if value != 0 and (abs(value) >= 1e5 or abs(value) < 1e-3):
        return f"{value:.3e}"
    return f"{value:.5g}"


def _interval(values):
    return f"{_num(values[0])} ~ {_num(values[1])}" if isinstance(values, (list, tuple)) and len(values) == 2 else "—"


def _sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _md5(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "md5").hexdigest()


# ---------------------------------------------------------------- tables

def _cell(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _csv_text(value):
    """Spreadsheet programs evaluate text starting with = + - @ as formulas (CSV injection)."""
    value = _cell(value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return repr(value)
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def write_csv(path, columns, rows):
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow([_csv_text(c) for c in columns])
    writer.writerows([[_csv_text(v) for v in row] for row in rows])
    Path(path).write_text("﻿" + stream.getvalue(), encoding="utf-8")


_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _column(index):
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_cell(reference, value, style=0):
    value = _cell(value)
    attribute = f' s="{style}"' if style else ""
    if value is None:
        return ""
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{attribute}><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)) and math.isfinite(value):
        return f'<c r="{reference}"{attribute}><v>{repr(float(value)) if isinstance(value, float) else value}</v></c>'
    text = _XML_INVALID.sub("", str(value))[:32767]
    if not text:
        return ""
    space = ' xml:space="preserve"' if text != text.strip() else ""
    return f'<c r="{reference}" t="inlineStr"{attribute}><is><t{space}>{xml_escape(text)}</t></is></c>'


def write_xlsx(path, sheets):
    """Minimal ECMA-376 SpreadsheetML workbook: inline strings, numbers, booleans, bold frozen header row."""
    names, used = [], set()
    for title, _, _ in sheets:
        name = re.sub(r"[\[\]:*?/\\]", "_", str(title))[:31] or "Sheet"
        base, counter = name, 2
        while name.lower() in used:
            suffix = f" ({counter})"
            name, counter = base[:31 - len(suffix)] + suffix, counter + 1
        used.add(name.lower())
        names.append(name)
    content_types = ['<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    content_types += [f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      for i in range(1, len(sheets) + 1)]
    header = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    parts = {
        "[Content_Types].xml": header + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>' + "".join(content_types) + "</Types>",
        "_rels/.rels": header + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": header + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(f'<sheet name="{xml_escape(name, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(names, 1))
            + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": header + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                      for i in range(1, len(sheets) + 1))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml": header + '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
    }
    for i, (_, columns, rows) in enumerate(sheets, 1):
        lines = ["<row r=\"1\">" + "".join(_xlsx_cell(f"{_column(j)}1", c, 1) for j, c in enumerate(columns)) + "</row>"]
        for r, row in enumerate(rows, 2):
            lines.append(f'<row r="{r}">' + "".join(_xlsx_cell(f"{_column(j)}{r}", v) for j, v in enumerate(row)) + "</row>")
        widths = "".join(f'<col min="{j}" max="{j}" width="{min(60, max(10, len(str(c)) + 4))}" customWidth="1"/>' for j, c in enumerate(columns, 1))
        parts[f"xl/worksheets/sheet{i}.xml"] = (header + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            + (f"<cols>{widths}</cols>" if columns else "") + "<sheetData>" + "".join(lines) + "</sheetData></worksheet>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
        for name, text in parts.items():
            book.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), text.encode("utf-8"), zipfile.ZIP_DEFLATED)


# ---------------------------------------------------------------- figures

def _label(text):
    # Matplotlib reads $...$ as mathtext; names are plain text.
    return str(text).replace("$", r"\$")


PREFERRED_FONTS = ("Noto Sans CJK KR", "Noto Sans KR", "Source Han Sans K", "NanumGothic", "NanumBarunGothic", "Malgun Gothic", "Apple SD Gothic Neo")


@lru_cache(maxsize=1)
def _hangul_font():
    from matplotlib import font_manager, ft2font
    def rank(entry):
        # Outline fonts that ship for Korean first; GNU Unifont is a bitmap-style last resort.
        return (PREFERRED_FONTS.index(entry.name) if entry.name in PREFERRED_FONTS else len(PREFERRED_FONTS), entry.name.startswith("Unifont"), entry.name)
    for entry in sorted(font_manager.fontManager.ttflist, key=rank):
        if entry.name.startswith("Last Resort") or "Sample" in entry.name:  # placeholder boxes, not glyphs
            continue
        try:
            if 0xAC00 in ft2font.FT2Font(entry.fname).get_charmap():
                return entry.name, entry.fname
        except (OSError, RuntimeError, ValueError):
            continue
    return None


@lru_cache(maxsize=4)
def _coverage(path):
    from matplotlib import ft2font
    return frozenset(ft2font.FT2Font(path).get_charmap())


def _missing_glyphs(figure):
    from matplotlib import font_manager
    from matplotlib.text import Text
    covered = set(_coverage(font_manager.findfont("DejaVu Sans")))
    if _hangul_font():
        covered |= _coverage(_hangul_font()[1])
    characters = {c for item in figure.findobj(Text) for c in item.get_text() if not c.isascii() and not c.isspace()}
    return sorted(c for c in characters if ord(c) not in covered)


def _ramp():
    from matplotlib.colors import LinearSegmentedColormap
    return LinearSegmentedColormap.from_list("polymerlab_blue", RAMP)


def _new_figure(width=6.0, height=4.0):
    from matplotlib.figure import Figure
    return Figure(figsize=(width, height), layout="constrained")


def _style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#9aa5ae")
    ax.grid(True, color="#e5e9ed", linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK, labelsize=8)
    ax.xaxis.label.set_color(INK)
    ax.yaxis.label.set_color(INK)


def _cube_features(points, model):
    points = np.atleast_2d(np.asarray(points, dtype=float))
    k = points.shape[1]
    columns = [np.ones(len(points)), *points.T]
    if model != "linear":
        columns += [points[:, i] * points[:, j] for i in range(k) for j in range(i + 1, k)]
    if model == "quadratic":
        columns += [points[:, i] ** 2 for i in range(k)]
    return np.column_stack(columns)


def _scheffe_features(points, model):
    points = np.atleast_2d(np.asarray(points, dtype=float))
    q = points.shape[1]
    columns = list(points.T)
    if model != "scheffe_linear":
        columns += [points[:, i] * points[:, j] for i in range(q) for j in range(i + 1, q)]
    if model == "scheffe_special_cubic":
        columns += [points[:, i] * points[:, j] * points[:, l] for i in range(q) for j in range(i + 1, q) for l in range(j + 1, q)]
    return np.column_stack(columns)


def predict(points, coefficients, model):
    """Evaluate a saved coded (cube) or pseudo-component (Scheffé) polynomial in the Rust kernel's term order."""
    features = _scheffe_features(points, model) if model.startswith("scheffe_") else _cube_features(points, model)
    beta = np.asarray([c["estimate"] for c in coefficients], dtype=float)
    if features.shape[1] != beta.size:
        raise ValueError("Saved coefficients do not match the model terms")
    return features @ beta


def _real(factor, coded):
    return factor["low"] + (np.asarray(coded) + 1.0) / 2.0 * (factor["high"] - factor["low"])


def figure_fit(observed, fitted, response):
    figure = _new_figure(4.6, 4.0)
    ax = figure.add_subplot()
    _style(ax)
    values = np.concatenate([observed, fitted])
    low, high = float(values.min()), float(values.max())
    pad = (high - low) * 0.06 or 1.0
    ax.plot([low - pad, high + pad], [low - pad, high + pad], color=MUTED, linestyle="--", linewidth=1)
    ax.scatter(fitted, observed, s=30, color=POINT, edgecolors="white", linewidths=0.8, zorder=3)
    ax.set_xlabel(_label(f"적합값 · {response}"))
    ax.set_ylabel("관측값")
    ax.set_title("관측값과 적합값 (점선: 일치)", fontsize=9, color=INK, loc="left")
    return figure


def figure_residuals(fitted, residuals):
    figure = _new_figure(7.2, 3.2)
    for index, (x, label) in enumerate(((np.asarray(fitted), "적합값"), (np.arange(2, len(residuals) + 2), "CSV 행"))):
        ax = figure.add_subplot(1, 2, index + 1)
        _style(ax)
        ax.axhline(0, color=MUTED, linestyle="--", linewidth=1)
        ax.scatter(x, residuals, s=26, color=POINT, edgecolors="white", linewidths=0.8, zorder=3)
        ax.set_xlabel(label)
        ax.set_ylabel("잔차")
    figure.suptitle("잔차 진단", fontsize=9, color=INK, x=0.02, ha="left")
    return figure


def figure_pareto(coefficients):
    rows = [c for c in coefficients if c["term"] != "Intercept" and isinstance(c.get("t_statistic"), (int, float))]
    reference = next((c for c in rows if c.get("standard_error") and isinstance(c.get("confidence_interval"), list)), None)
    if not rows or reference is None:
        return None
    critical = (reference["confidence_interval"][1] - reference["estimate"]) / reference["standard_error"]
    rows.sort(key=lambda c: abs(c["t_statistic"]), reverse=True)
    figure = _new_figure(6.0, 0.9 + 0.32 * len(rows))
    ax = figure.add_subplot()
    _style(ax)
    ax.grid(False, axis="y")
    sizes = [abs(c["t_statistic"]) for c in rows]
    ax.barh(range(len(rows)), sizes, height=0.62, color=[POINT if s >= critical else MUTED for s in sizes])
    ax.set_yticks(range(len(rows)), [_label(c["term"]) for c in rows])
    ax.invert_yaxis()
    ax.axvline(critical, color=REFERENCE, linestyle="--", linewidth=1.2)
    ax.text(critical, -0.75, f" t = {critical:.4g} (α = 0.05)", color=INK, fontsize=8, va="bottom")
    ax.set_xlabel("|t| (표준화 효과)")
    ax.set_title("표준화 효과 파레토 차트 (진한 막대: 5% 수준에서 유의)", fontsize=9, color=INK, loc="left", pad=16)
    return figure


def figure_surface(result, points=None):
    factors, model = result["factors"], result["model"]
    held = result["recommendation"]["coded"]
    grid = np.linspace(-1.0, 1.0, 61)
    a, b = np.meshgrid(grid, grid)
    coded = np.tile([held[f["name"]] for f in factors], (a.size, 1))
    coded[:, 0], coded[:, 1] = a.ravel(), b.ravel()
    surface = predict(coded, result["coefficients"], model).reshape(a.shape)
    figure = _new_figure(5.6, 4.4)
    ax = figure.add_subplot()
    x, y = _real(factors[0], a), _real(factors[1], b)
    filled = ax.contourf(x, y, surface, levels=12, cmap=_ramp())
    lines = ax.contour(x, y, surface, levels=filled.levels, colors="white", linewidths=0.5, alpha=0.75)
    ax.clabel(lines, lines.levels[1::2], inline=True, fontsize=7, fmt=_num)
    if points is not None and len(points):
        points = np.asarray(points)
        ax.scatter(_real(factors[0], points[:, 0]), _real(factors[1], points[:, 1]), s=14, color=INK, alpha=0.7, zorder=3, label="실험점")
    best = result["recommendation"]["settings"]
    ax.scatter([best[factors[0]["name"]]], [best[factors[1]["name"]]], s=110, facecolor="white", edgecolor="#111", linewidths=1.6, zorder=4, label="추천 조건")
    ax.set_xlabel(_label(factors[0]["name"]))
    ax.set_ylabel(_label(factors[1]["name"]))
    ax.legend(loc="upper right", fontsize=7, framealpha=0.9)
    bar = figure.colorbar(filled, ax=ax)
    bar.set_label(_label(f"예측 {result['response']}"))
    others = [f for f in factors[2:]]
    note = " · ".join(f"{f['name']} = {_num(result['recommendation']['settings'][f['name']])}" for f in others)
    ax.set_title(_label("반응표면 등고선 (모델 예측값)" + (f" · 고정: {note}" if note else "")), fontsize=9, color=INK, loc="left")
    return figure


def figure_profiles(result):
    factors, model, held = result["factors"], result["model"], result["recommendation"]["coded"]
    grid = np.linspace(-1.0, 1.0, 81)
    columns = min(3, len(factors))
    rows = math.ceil(len(factors) / columns)
    figure = _new_figure(2.6 * columns + 0.6, 2.3 * rows + 0.5)
    axes = figure.subplots(rows, columns, sharey=True, squeeze=False)
    base = np.array([held[f["name"]] for f in factors])
    for index, factor in enumerate(factors):
        ax = axes[index // columns][index % columns]
        _style(ax)
        coded = np.tile(base, (grid.size, 1))
        coded[:, index] = grid
        ax.plot(_real(factor, grid), predict(coded, result["coefficients"], model), color=POINT, linewidth=2)
        best = result["recommendation"]["settings"][factor["name"]]
        ax.axvline(best, color=REFERENCE, linestyle="--", linewidth=1)
        ax.scatter([best], [result["recommendation"]["predicted_response"]], s=28, facecolor="white", edgecolor="#111", zorder=3)
        ax.set_xlabel(_label(factor["name"]))
    for index in range(len(factors), rows * columns):
        axes[index // columns][index % columns].set_visible(False)
    axes[0][0].set_ylabel(_label(f"예측 {result['response']}"))
    figure.suptitle("인자별 예측 프로파일 (다른 인자는 추천 조건 값)", fontsize=9, color=INK, x=0.02, ha="left")
    return figure


def figure_ternary(result, points=None):
    from matplotlib.tri import Triangulation
    names = [c["name"] for c in result["components"]]
    region = result["region"]
    lower, upper, scale = np.asarray(region["lower"]), np.asarray(region["upper"]), region["pseudo_scale"]
    steps = 60
    z = np.array([(i / steps, j / steps, (steps - i - j) / steps) for i in range(steps + 1) for j in range(steps + 1 - i)])
    x = lower + scale * z
    feasible = np.all((x >= lower - 1e-9) & (x <= upper + 1e-9), axis=1)
    values = predict(z, result["coefficients"], result["model"])
    corners = np.array([[0.5, math.sqrt(3) / 2], [0.0, 0.0], [1.0, 0.0]])
    xy = z @ corners
    triangulation = Triangulation(xy[:, 0], xy[:, 1])
    triangulation.set_mask(~feasible[triangulation.triangles].all(axis=1))
    shown = values[feasible]
    low, high = float(shown.min()), float(shown.max())
    levels = np.linspace(low, high, 13) if high > low else np.array([low - 0.5, low + 0.5])
    figure = _new_figure(5.4, 4.6)
    ax = figure.add_subplot()
    filled = ax.tricontourf(triangulation, values, levels=levels, cmap=_ramp())
    ax.tricontour(triangulation, values, levels=levels, colors="white", linewidths=0.5, alpha=0.75)
    ax.plot(*np.vstack([corners, corners[:1]]).T, color="#9aa5ae", linewidth=1)
    for corner, name, offset in zip(corners, names, ((0, 0.04), (-0.04, -0.06), (0.04, -0.06))):
        ax.text(corner[0] + offset[0], corner[1] + offset[1], _label(name), ha="center", fontsize=9, color=INK)
    if points is not None and len(points):
        p = np.asarray(points) @ corners
        ax.scatter(p[:, 0], p[:, 1], s=14, color=INK, alpha=0.7, zorder=3, label="실험점")
    best = np.array([result["recommendation"]["pseudo"][n] for n in names]) @ corners
    ax.scatter([best[0]], [best[1]], s=110, facecolor="white", edgecolor="#111", linewidths=1.6, zorder=4, label="추천 배합")
    ax.set_xlim(-0.08, 1.08)
    ax.set_ylim(-0.1, 0.96)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.legend(loc="upper left", fontsize=7, framealpha=0.9)
    bar = figure.colorbar(filled, ax=ax, shrink=0.8)
    bar.set_label(_label(f"예측 {result['response']}"))
    ax.set_title("삼각 등고선 (유사성분, 빈 곳 = 범위 밖)", fontsize=9, color=INK, loc="left")
    return figure


def figure_desirability(result):
    rows = result["responses"]
    figure = _new_figure(5.6, 0.9 + 0.36 * len(rows))
    ax = figure.add_subplot()
    _style(ax)
    ax.grid(False, axis="y")
    ax.barh(range(len(rows)), [r["desirability"] for r in rows], height=0.6, color=POINT)
    ax.set_yticks(range(len(rows)), [_label(r["name"]) for r in rows])
    ax.invert_yaxis()
    ax.set_xlim(0, 1.05)
    total = result["optimum"]["composite_desirability"]
    ax.axvline(total, color=REFERENCE, linestyle="--", linewidth=1.2)
    ax.text(total, -0.7, f" D = {total:.3g}", color=INK, fontsize=8, va="bottom")
    ax.set_xlabel("개별 만족도 d")
    ax.set_title("최적점의 반응별 만족도", fontsize=9, color=INK, loc="left", pad=16)
    return figure


def figure_confirmations(rows):
    figure = _new_figure(5.0, 0.9 + 0.5 * len(rows))
    ax = figure.add_subplot()
    _style(ax)
    for index, row in enumerate(rows):
        ax.plot(row["prediction_interval"], [index, index], color=MUTED, linewidth=6, solid_capstyle="butt")
        ax.scatter([row["predicted"]], [index], marker="|", s=200, color=INK, zorder=3)
        ax.scatter([row["observed"]], [index], s=40, color=POINT, edgecolors="white", zorder=4)
    ax.set_yticks(range(len(rows)), [r["label"] for r in rows])
    ax.invert_yaxis()
    ax.set_xlabel("반응값")
    ax.set_title("확인 계산: 관측값(점)과 95% 예측구간(막대)", fontsize=9, color=INK, loc="left")
    return figure


# ---------------------------------------------------------------- report builder

class Builder:
    def __init__(self, title):
        self.title = title
        self.markdown, self.html = [], []
        self.figures, self.tables, self.warnings = {}, {}, []

    def heading(self, text):
        self.markdown += ["", f"## {text}", ""]
        self.html.append(f"<h2>{html.escape(text)}</h2>")

    def paragraph(self, text):
        self.markdown += [text, ""]
        self.html.append(f"<p>{html.escape(text)}</p>")

    def facts(self, pairs):
        pairs = [(k, v) for k, v in pairs if v not in (None, "")]
        self.markdown += [f"- **{k}:** {v}" for k, v in pairs] + [""]
        self.html.append("<dl>" + "".join(f"<dt>{html.escape(str(k))}</dt><dd>{html.escape(str(v))}</dd>" for k, v in pairs) + "</dl>")

    def bullets(self, items):
        self.markdown += [f"- {item}" for item in items] + [""]
        self.html.append("<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in items) + "</ul>")

    def table(self, name, title, columns, rows, preview=TABLE_PREVIEW):
        rows = [list(r) for r in rows]
        self.tables[name] = (title, list(columns), rows)
        if not preview:
            self.markdown += [f"**{title}:** {len(rows)}행 · `tables/{name}.csv`", ""]
            self.html.append(f"<p><strong>{html.escape(title)}:</strong> {len(rows)}행 · <code>tables/{html.escape(name)}.csv</code></p>")
            return
        shown = rows[:preview]
        more = f" · 처음 {preview}행만 표시, 전체는 tables/{name}.csv" if len(rows) > preview else ""
        def md(value):
            return (_num(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(_cell(value) if value is not None else "—")).replace("|", r"\|").replace("\n", " ")
        self.markdown += [f"**{title}** (`tables/{name}.csv`{more})", "", "| " + " | ".join(md(c) for c in columns) + " |",
                          "|" + "---|" * len(columns)] + ["| " + " | ".join(md(v) for v in row) + " |" for row in shown] + [""]
        def cell(value):
            return html.escape(_num(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(_cell(value) if value is not None else "—"))
        self.html.append(f"<h3>{html.escape(title)} <small>tables/{html.escape(name)}.csv{html.escape(more)}</small></h3><div class=\"scroll\"><table><thead><tr>"
                         + "".join(f"<th>{html.escape(str(c))}</th>" for c in columns) + "</tr></thead><tbody>"
                         + "".join("<tr>" + "".join(f"<td>{cell(v)}</td>" for v in row) + "</tr>" for row in shown) + "</tbody></table></div>")

    def figure(self, name, caption, figure):
        if figure is None:
            return
        self.figures[name] = (caption, figure)
        self.markdown += [f"![{caption}](figures/{name}.svg)", "", f"*{caption}* · PNG: `figures/{name}.png`", ""]
        self.html.append(f"<figure data-figure=\"{html.escape(name)}\"></figure>")


def _doe_facts(b, result):
    fit = result["fit"]
    anova = result.get("anova", {})
    b.facts([("모델", MODELS.get(result["model"], result["model"])), ("목표", OBJECTIVES.get(result.get("objective"), result.get("objective"))),
             ("반응", result["response"]), ("관측 행", fit["n"]), ("잔차 자유도", fit["df_residual"]),
             ("R² · 조정 R² · 예측 R²", f"{_num(fit['r_squared'])} · {_num(fit['adjusted_r_squared'])} · {_num(fit['predicted_r_squared'])}"),
             ("RMSE", _num(fit["rmse"])),
             ("회귀 F 검정 p", _num(anova.get("regression", {}).get("p_value"))),
             ("적합 결여 p", _num(anova.get("lack_of_fit", {}).get("p_value")) if anova.get("pure_error", {}).get("df") else "반복점이 없어 계산하지 않음")])


def _doe_tables(b, result):
    b.table("coefficients", "모델 계수", ["항", "계수", "표준 오차", "t", "p", "95% 하한", "95% 상한"],
            [[c["term"], c["estimate"], c.get("standard_error"), c.get("t_statistic"), c.get("p_value"),
              *(c.get("confidence_interval") or [None, None])] for c in result["coefficients"]])
    b.table("anova", "분산분석", ["변동 원인", "자유도", "제곱합", "평균 제곱", "F", "p"],
            [[ANOVA.get(key, key), row.get("df"), row.get("ss"), row.get("ms"), row.get("f_statistic"), row.get("p_value")]
             for key, row in result.get("anova", {}).items() if isinstance(row, dict)])
    if result.get("model_comparison"):
        b.table("model_comparison", "같은 관측값의 모델 비교", ["모델", "상태", "항 수", "잔차 자유도", "RMSE", "R²", "조정 R²", "예측 R²", "적합 결여 p"],
                [[MODELS.get(m["model"], m["model"]), m.get("status"), m.get("terms"), m.get("df_residual"), m.get("rmse"), m.get("r_squared"),
                  m.get("adjusted_r_squared"), m.get("predicted_r_squared"), m.get("lack_of_fit_p_value")] for m in result["model_comparison"]])
    d = result["diagnostics"]
    flagged = {u["row"] for u in d.get("unusual_rows", [])}  # 1-based observation index; the CSV row is one more
    b.table("diagnostics", "행별 잔차 진단", ["CSV 행", "관측값", "적합값", "잔차", "표준화 잔차", "레버리지", "Cook 거리", "검토 표시"],
            [[i + 2, d["observed"][i], d["fitted"][i], d["residuals"][i], d["standardized_residuals"][i], d["leverage"][i], d["cooks_distance"][i],
              (i + 1) in flagged] for i in range(len(d["observed"]))], preview=0)


def _recommendation(b, result, label="추천 조건"):
    rec = result["recommendation"]
    b.facts([*((name, _num(value)) for name, value in rec["settings"].items()),
             (f"예측 {result['response']}", _num(rec["predicted_response"])),
             ("평균의 95% 신뢰구간", _interval(rec.get("mean_confidence_interval"))),
             ("새 관측 하나의 95% 예측구간", _interval(rec.get("prediction_interval"))),
             ("범위 밖 외삽", ", ".join(rec.get("extrapolated_factors") or rec.get("extrapolated_components") or []) or "없음"),
             ("검증", "확인 실험 전 (validated=false)")])
    b.table("recommendation", label, ["항목", "값"],
            [*([name, value] for name, value in rec["settings"].items()), ["predicted_response", rec["predicted_response"]],
             ["mean_ci_low", (rec.get("mean_confidence_interval") or [None, None])[0]], ["mean_ci_high", (rec.get("mean_confidence_interval") or [None, None])[1]],
             ["prediction_interval_low", (rec.get("prediction_interval") or [None, None])[0]], ["prediction_interval_high", (rec.get("prediction_interval") or [None, None])[1]],
             ["validated", False]], preview=0)


def _input_table(b, text):
    if not text:
        return
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return
    def value(item):
        try:
            number = float(item)
            return number if math.isfinite(number) else item
        except ValueError:
            return item
    b.table("input", "입력 측정값 CSV", rows[0], [[value(v) for v in row] for row in rows[1:]], preview=0)


def _doe_analysis(b, result, points=None, text=None):
    b.heading("모델과 적합")
    _doe_facts(b, result)
    b.figure("fit", "관측값과 적합값", figure_fit(np.asarray(result["diagnostics"]["observed"]), np.asarray(result["diagnostics"]["fitted"]), result["response"]))
    b.figure("residuals", "잔차 진단", figure_residuals(result["diagnostics"]["fitted"], result["diagnostics"]["residuals"]))
    mixture = result["model"].startswith("scheffe_")
    if not mixture:
        b.figure("pareto", "표준화 효과 파레토 차트", figure_pareto(result["coefficients"]))
    _doe_tables(b, result)
    b.heading("추천 조건 (확인 실험 필요)")
    _recommendation(b, result, "추천 배합" if mixture else "추천 조건")
    if mixture and len(result["components"]) == 3:
        b.figure("ternary", "삼각 등고선", figure_ternary(result, points))
    if not mixture:
        b.figure("surface", "반응표면 등고선", figure_surface(result, points))
        b.figure("profiles", "인자별 예측 프로파일", figure_profiles(result))
    _input_table(b, text)


def build_doe(b, record, text):
    from . import doe
    kind = record.get("record_type")
    if kind == "doe_analysis":
        points = doe._data(text, record["factors"], record["response"])[0] if text else None
        b.paragraph("모델이 고른 추천 조건은 확인 실험 전의 후보입니다. 같은 데이터로 최적점을 고른 영향은 구간에 반영되지 않습니다.")
        _doe_analysis(b, record, points, text)
    elif kind == "doe_mixture_analysis":
        components, region = doe._components(record["components"])
        points = doe._mixture_data(text, components, region, record["response"])[0] if text else None
        b.paragraph("성분 비율의 합이 1인 범위에서 Scheffé 모델로 맞춘 결과입니다. 추천 배합은 확인 실험 전의 후보입니다.")
        b.facts([(c["name"], f"{_num(c['low'])} ~ {_num(c['high'])}") for c in record["components"]])
        _doe_analysis(b, record, points, text)
    elif kind == "doe_desirability":
        _desirability_section(b, record)
        _input_table(b, text)
    elif kind in {"doe_design", "doe_mixture_design"}:
        variables = record.get("factors") or record.get("components")
        b.paragraph("실험 순서는 무작위화한 설계표입니다. 반응값 열을 채워 분석에 사용하세요.")
        b.facts([("설계", record.get("method") or record.get("kind")), ("실험 횟수", record.get("total_runs")),
                 *((v["name"], f"{_num(v['low'])} ~ {_num(v['high'])}") for v in variables)])
        names = [v["name"] for v in variables]
        b.table("design", "설계표", ["순서", "종류", *names], [[r["run_order"], r.get("point_type"), *(r["values"][n] for n in names)] for r in record["runs"]])
    else:
        raise ValueError("보고서를 만들 수 없는 실험계획 기록입니다.")
    b.warnings += record.get("warnings", [])


def _desirability_section(b, record):
    opt = record["optimum"]
    b.paragraph("각 반응을 따로 맞춘 모델의 예측값으로 Derringer–Suich 만족도 D를 최대화했습니다. 경계·모양·중요도는 사용자가 정한 값입니다.")
    b.heading("최적점 (확인 실험 필요)")
    b.facts([("종합 만족도 D", _num(opt["composite_desirability"])), *((n, _num(v)) for n, v in opt["settings"].items()),
             ("관측 실험 중 최고 D", f"{_num(record['best_observed_run']['composite_desirability'])} (CSV {record['best_observed_run']['row']}행)"),
             ("검증", "확인 실험 전 (validated=false)")])
    b.figure("desirability", "반응별 만족도", figure_desirability(record))
    b.table("responses", "반응별 목표와 예측", ["반응", "모델", "목표", "하한", "목표값", "상한", "하한 모양", "상한 모양", "중요도", "예측값", "d",
                                          "예측구간 하한", "예측구간 상한", "R²", "예측 R²"],
            [[r["name"], MODELS.get(r["model"], r["model"]), OBJECTIVES.get(r["goal"], r["goal"]), r["lower"], r["target"], r["upper"], r["lower_shape"],
              r["upper_shape"], r["importance"], r["predicted"], r["desirability"], *(r.get("prediction_interval") or [None, None]),
              r["fit"]["r_squared"], r["fit"]["predicted_r_squared"]] for r in record["responses"]])
    b.table("optimum", "최적점 설정", ["변수", "값"], [[n, v] for n, v in opt["settings"].items()], preview=0)
    b.table("observed_desirability", "관측 실험별 D", ["CSV 행", "D"],
            [[i + 2, v] for i, v in enumerate(record["observed_composite_desirability"])], preview=0)


def build_quality_evidence(b, quality):
    b.heading("결과 근거와 다음 결정")
    b.paragraph(quality['message'])
    labels = {'passed':'해당 기준 충족','pending':'추가 확인','failed':'보류 원인','unknown':'미평가/미기록'}
    b.table('quality', '품질 근거', ['구분','상태','근거'],
            [[c['title'], labels[c['status']], c['detail']] for c in quality['checks']])
    b.table('next_checks', '다음 검사 제안', ['검사','근거','추가 계산 수'],
            [[c['message'], c.get('reason'), c.get('additional_runs')] for c in quality['next_checks']])
    b.paragraph('이 요약은 실험·문헌 검증이나 사람의 검토를 자동 승인하지 않습니다.')


def build_density_study(b, record, assessment):
    b.paragraph('고정 조건의 GAFF2 NPT 밀도 반복 기록입니다. 통계의 일치나 체크리스트 충족만으로 물성 정확도가 입증되지는 않습니다.')
    b.facts([('재료', record['template']['repeat_smiles']), ('온도', f"{record['conditions']['temperature_kelvin']} K"),
             ('압력', f"{record['conditions']['pressure_atm']} atm"), ('독립 반복 계획', f"최소 {record['minimum_replicates']} / 최대 {record['max_replicates']}"),
             ('관측 방법', assessment['method']), ('평가 시각',assessment['created_at']), ('단위', assessment['unit']), ('검토', '검토 전 시뮬레이션')])
    build_quality_evidence(b, assessment['quality'])
    b.table('replicates', '반복별 상태와 밀도', ['작업','시드','실행 상태','통계 포함','밀도 (g/cm³)','수렴 판정','보류 사유'],
            [[r['job_id'],r.get('seed'),r['status'],r.get('eligible'),r.get('density'),r.get('equilibrated'),
              '; '.join(i['message'] for i in r.get('issues',[]))] for r in assessment['records']])
    stats = assessment.get('statistics')
    if stats:
        b.table('repeat_statistics', '탐색용 반복 통계', ['N','평균','표본 SD','SE','평균 95% t 구간 하한','상한','상대 SD'],
                [[stats['n'],stats['mean'],stats['sample_sd'],stats['standard_error'],*(stats.get('confidence_interval') or [None,None]),stats['relative_sd']]])
        figure = _new_figure()
        ax = figure.subplots()
        _style(ax)
        rows = [r for r in assessment['records'] if r.get('eligible')]
        ax.scatter(range(1,len(rows)+1),[r['density'] for r in rows],color=[POINT if r.get('equilibrated') is True else REFERENCE for r in rows],label='반복값 · 주황은 미수렴/미기록')
        ax.axhline(stats['mean'],color=POINT,linestyle='--',label='반복 평균')
        if stats.get('confidence_interval'):
            low,high=stats['confidence_interval'];ax.axhspan(low,high,color=POINT,alpha=.12,label='평균 95% t 구간')
        comparison=stats.get('reference_comparison')
        if comparison:
            ax.axhline(comparison['reference'],color=REFERENCE,linestyle=':',label='비교 가능한 외부 참조')
            b.table('reference_comparison','외부 참조와의 기술적 차이', ['참조','평균−참조','상대 차이','결합 표준 불확실성','표준화 차이'],
                    [[comparison[k] for k in ['reference','value_difference','relative_difference','combined_standard_uncertainty','standardized_difference']]])
        ax.set_xlabel('기록 순서 (시간 순서 아님)');ax.set_ylabel('NPT 밀도 (g/cm³)');ax.legend(fontsize=7)
        b.figure('density_replicates','반복 산포와 평균 구간 · 수렴/독립성은 별도 근거',figure)
    b.warnings += assessment.get('warnings',[])


def build_workflow(b, record, measurements):
    names = [f["name"] for f in record["factors"]]
    b.paragraph("시뮬레이션 단계별 흐름입니다. 반응값은 구조 분석이나 시뮬레이션에서 얻은 관측값이며 측정값이 아니고, 모두 검토 전(verified=false)입니다.")
    states = {}
    for row in record["rows"]:
        states[row["state"]] = states.get(row["state"], 0) + 1
    collected = record.get("responses") or [record["metric"]]
    units = {name: (record.get("units") or {}).get(name, record["unit"] if name == record["metric"] else "") for name in collected}
    from .workflows import ROUTES, route_of
    route = route_of(record)
    b.facts([("계산 경로", ROUTES[route]["label"] + (f" · 원래 흐름 {record['parent_workflow_id']}" if record.get("parent_workflow_id") else "")),
             ("상태", record["state"]), ("반응값", ", ".join(f"{name} ({units[name]})" for name in collected)), ("계산 예산", f"{record['submitted']} / {record['budget']}"),
             ("행 상태", ", ".join(f"{k} {v}" for k, v in sorted(states.items()))), ("설계 기록", record["design_id"]), ("기록 판", record.get("revision"))])
    def value(observation, name):
        return (observation.get("values") or {record["metric"]: observation.get("value")}).get(name) if observation else None
    b.table("runs", "설계 행과 계산", ["순서", *names, "작업", "작업 상태", "행 상태", "시도 수", *(f"{n} ({units[n]})" for n in collected), "NPT 수렴 판정", "검토", "분석 기록", "오류"],
            [[r["run_order"], *(r["factors"][n] for n in names), r["job_id"], r.get("job_status"), r["state"], len(r["attempts"]),
              *(value(r.get("observation"), n) for n in collected), (r.get("observation") or {}).get("equilibrated"),
              bool((r.get("observation") or {}).get("verified", False)), (r.get("observation") or {}).get("analysis_id"), r.get("error")] for r in record["rows"]])
    if record["optimizations"]:
        latest = record["optimizations"][-1]
        observed = {o["run_order"] for o in latest.get("observations", [])}
        coded = np.array([[2 * (r["factors"][f["name"]] - f["low"]) / (f["high"] - f["low"]) - 1 for f in record["factors"]]
                          for r in record["rows"] if r["run_order"] in observed]) if observed else None
        desirability = latest.get("analysis") == "doe_desirability"
        b.heading(f"최근 {'다중 반응 만족도 최적화' if desirability else '반응표면 분석'} ({len(record['optimizations'])}회 중 마지막)")
        if latest.get("exclusions"):
            b.facts([(f"{k}행 제외", v) for k, v in latest["exclusions"].items()])
        if desirability:
            _desirability_section(b, latest)
        else:
            _doe_analysis(b, latest, coded)
        b.warnings += latest.get("warnings", [])
    confirmations = [c for c in record["confirmations"] if c.get("observation")]
    if record["confirmations"]:
        b.heading("확인 계산")
        rows, charts = [], {}
        for item in record["confirmations"]:
            optimization = next((o for o in record["optimizations"] if o["id"] == item["optimization_id"]), {})
            rec, observation = optimization.get("recommendation", {}), item.get("observation") or {}
            used = rec.get("rounded")
            if optimization.get("analysis") == "doe_desirability":
                source = used or rec
                targets = [(n, source["predicted"][n], source["prediction_intervals"][n]) for n in source.get("predicted", {})]
            else:
                metric = optimization.get("metric", record["metric"])
                targets = [(metric, used["predicted"][metric], used["prediction_intervals"][metric]) if used
                           else (metric, rec.get("predicted_response"), rec.get("prediction_interval") or [None, None])]
            for name, predicted, interval in targets:
                observed_value = value(observation, name)
                inside = None if observed_value is None or None in interval else interval[0] <= observed_value <= interval[1]
                error = (observation.get("prediction_errors") or {name: observation.get("prediction_error")}).get(name)
                rows.append([item["optimization_id"], item.get("scan_job_id") or item["job_id"], item.get("final_status", item.get("job_status")), name,
                             observed_value, predicted, *interval, inside, error, bool(observation.get("verified", False))])
                if observed_value is not None and None not in interval:
                    charts.setdefault(name, []).append({"label": item["job_id"][:8], "observed": observed_value, "predicted": predicted, "prediction_interval": interval})
        b.table("confirmations", "확인 계산과 예측", ["최적화", "작업", "작업 상태", "반응", "관측값", "예측값", "예측구간 하한", "예측구간 상한", "예측구간 안", "예측 오차", "검토"], rows)
        for index, (name, chart) in enumerate(charts.items()):
            b.figure("confirmations" if len(charts) == 1 else f"confirmations_{index + 1}", f"확인 계산과 예측구간 · {name} ({units.get(name, '')})", figure_confirmations(chart))
    _measurement_table(b, measurements, "연결된 측정값 기록")
    b.warnings += record.get("warnings", [])
    if not confirmations:
        b.warnings.append("No confirmation run has been analyzed; any recommendation in this report is unconfirmed.")


def _measurement_table(b, measurements, title):
    columns = ["측정 ID", "물질 ID", "작업", "물성", "값", "단위", "출처 종류", "검토", "방법", "출처", "조건", "원래 값", "원래 단위", "기록 시각"]
    b.table("measurements", title, columns,
            [[m.get("id"), m.get("material_id"), m.get("job_id"), m.get("property"), m.get("value"), m.get("unit"), m.get("origin"),
              bool(m.get("verified", False)), m.get("method"), m.get("source"), m.get("conditions"), m.get("original_value"), m.get("original_unit"),
              m.get("created_at")] for m in measurements], preview=TABLE_PREVIEW)


def build_lab(b, experiments, measurements):
    b.paragraph("계산 기록과 측정값 기록을 한 시점에 읽은 마스터 표입니다. 학습에는 사람이 검토한(verified=true) 실험·문헌값만 쓰입니다. "
                "AI·MCP가 넣은 값은 항상 검토 전입니다.")
    trainable = sum(1 for m in measurements if m.get("verified") and m.get("origin") in {"experimental", "literature"})
    b.facts([("계산 기록", len(experiments)), ("데모(축척 단위) 계산", sum(1 for e in experiments if e.get("is_demo"))),
             ("측정값 기록", len(measurements)), ("학습에 쓸 수 있는 값 (검토된 실험·문헌)", trainable),
             ("검토 전 실험·문헌값", sum(1 for m in measurements if not m.get("verified") and m.get("origin") in {"experimental", "literature"})),
             ("시뮬레이션 값", sum(1 for m in measurements if m.get("origin") == "simulation"))])
    counts = {}
    for m in measurements:
        key = (m.get("property"), m.get("origin"), bool(m.get("verified", False)))
        counts[key] = counts.get(key, 0) + 1
    b.table("counts", "물성 · 출처 종류 · 검토 여부별 개수", ["물성", "출처 종류", "검토", "개수"],
            [[*key, n] for key, n in sorted(counts.items(), key=lambda item: (str(item[0][0]), str(item[0][1]), item[0][2]))], preview=40)
    def smiles(e):
        material = e.get("material") or {}
        return next((material[k] for k in ("repeat_smiles", "smiles", "resin_smiles") if material.get(k)), None)
    b.table("experiments", "계산 기록", ["작업", "물질 ID", "이름", "경로", "상태", "출처 종류", "데모", "대표 SMILES", "생성", "종료"],
            [[e.get("job_id"), e.get("material_id"), e.get("name"), e.get("route"), e.get("status"), e.get("origin"), bool(e.get("is_demo")),
              smiles(e), e.get("created_at"), e.get("finished_at")] for e in experiments])
    _measurement_table(b, measurements, "측정값 기록")


# ---------------------------------------------------------------- publish

STYLE = """body{font:14px/1.6 system-ui,-apple-system,"Segoe UI","Noto Sans KR","Malgun Gothic",sans-serif;color:#1f2a33;background:#fff;margin:0}
main{max-width:980px;margin:0 auto;padding:32px 20px 64px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 10px;border-bottom:1px solid #dde3e8;padding-bottom:6px}
h3{font-size:14px;margin:18px 0 6px}h3 small{font-weight:400;color:#67737d}.meta{color:#67737d;font-size:12px}dl{display:grid;grid-template-columns:minmax(140px,max-content) 1fr;gap:4px 18px;margin:8px 0}
dt{color:#55626c}dd{margin:0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.scroll{overflow-x:auto}table{border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums;min-width:100%}
th,td{border-bottom:1px solid #e4e8ec;padding:5px 9px;text-align:left;vertical-align:top;white-space:nowrap}th{background:#f4f6f8;font-weight:600}
figure{margin:14px 0}figure svg{max-width:100%;height:auto}figcaption{font-size:12px;color:#55626c}.notice{background:#fff7e6;border:1px solid #f0d9a8;border-radius:6px;padding:10px 14px}
ul{padding-left:20px}code{font-size:12px;overflow-wrap:anywhere}.meta{overflow-wrap:anywhere}@media(prefers-color-scheme:dark){body{background:#fff}}@media print{main{padding:0}}"""


@contextmanager
def _figure_settings(report_id):
    # Text objects resolve their fonts when created, so figures are both built and saved inside these settings.
    from matplotlib import rc_context
    font = _hangul_font()
    params = {"svg.fonttype": "none", "svg.hashsalt": report_id, "font.family": ["DejaVu Sans", *([font[0]] if font else [])], "axes.unicode_minus": True}
    log = logging.getLogger("matplotlib.font_manager")
    level = log.level
    with _FIGURE_LOCK, rc_context(params), python_warnings.catch_warnings():
        python_warnings.filterwarnings("ignore", message="Glyph .* missing from")
        log.setLevel(logging.ERROR)  # weight-substitution notices for the fallback font
        try:
            yield
        finally:
            log.setLevel(level)


def _render(b, report_id, kind, subject_id, created, sources, folder):
    from matplotlib import __version__ as matplotlib_version
    figures_dir, tables_dir = folder / "figures", folder / "tables"
    figures_dir.mkdir()
    tables_dir.mkdir()
    svgs = {}
    for name, (caption, figure) in b.figures.items():
        figure.savefig(figures_dir / f"{name}.svg", format="svg", metadata={"Date": None})
        figure.savefig(figures_dir / f"{name}.png", format="png", dpi=160, facecolor="white")
        missing = _missing_glyphs(figure)
        if missing:
            b.warnings.append(f"PNG figure {name} lacks glyphs for {''.join(missing[:12])} in the installed fonts; the SVG keeps the text for the viewer's fonts.")
        text = (figures_dir / f"{name}.svg").read_text(encoding="utf-8")
        svgs[name] = (caption, text[text.index("<svg"):])
    for name, (title, columns, rows) in b.tables.items():
        write_csv(tables_dir / f"{name}.csv", columns, rows)
    limit = 1048575  # Excel rows per sheet below the header
    for name, (title, _, rows) in b.tables.items():
        if len(rows) > limit:
            b.warnings.append(f"report.xlsx sheet {title} holds the first {limit} of {len(rows)} rows; tables/{name}.csv holds every row.")
    write_xlsx(tables_dir / "report.xlsx", [(title, columns, rows[:limit]) for title, columns, rows in b.tables.values()])
    warnings = list(dict.fromkeys(b.warnings))
    header = [f"# {b.title}", "", f"- 만든 시각: {created}", f"- 대상: {kind} `{subject_id or '전체'}`", f"- 보고서 ID: `{report_id}`",
              "- 이 보고서는 저장된 기록의 숫자와 검토 표시를 그대로 옮깁니다. 새로 맞추거나 검토하지 않습니다.", ""]
    footer = ["", "## 계산 주의사항 (원문)", "", *(f"- {w}" for w in warnings), "", "## 출처 기록", "",
              *(f"- {s['type']} `{s['id']}` sha256 `{s['sha256']}`" for s in sources), "",
              "## 파일", "", "- `report.html`: 그림이 들어 있는 보고서", "- `figures/`: SVG(글자 유지)와 PNG", "- `tables/`: CSV(UTF-8, 엑셀용 BOM)와 `report.xlsx`",
              "- `manifest.json`: 모든 파일의 크기·SHA-256·MD5", ""]
    if (folder / "inputs").is_dir():
        footer += ["- `inputs/`: 원래 설정·시드·설계·저장된 실행 계획을 담은 로컬 재현 입력", ""]
    (folder / "summary.md").write_text("\n".join(header + b.markdown + footer), encoding="utf-8")
    body = []
    for fragment in b.html:
        match = re.fullmatch(r'<figure data-figure="([^"]+)"></figure>', fragment)
        if match:
            caption, svg = svgs[html.unescape(match.group(1))]
            fragment = f"<figure>{svg}<figcaption>{html.escape(caption)} · PNG: figures/{match.group(1)}.png</figcaption></figure>"
        body.append(fragment)
    page = (f"<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(b.title)}</title><style>{STYLE}</style></head><body><main><h1>{html.escape(b.title)}</h1>"
            f"<p class=\"meta\">{html.escape(created)} · {html.escape(kind)} {html.escape(subject_id or '전체')} · 보고서 {report_id}</p>"
            "<p class=\"notice\">저장된 기록의 숫자와 검토 표시를 그대로 옮긴 보고서입니다. 추천값은 확인 실험 전의 후보이고, 시뮬레이션 값은 측정값이 아닙니다.</p>"
            + "".join(body) + "<h2>계산 주의사항 (원문)</h2><ul>" + "".join(f"<li>{html.escape(w)}</li>" for w in warnings) + "</ul>"
            "<h2>출처 기록</h2><ul>" + "".join(f"<li>{html.escape(s['type'])} <code>{html.escape(s['id'])}</code> sha256 <code>{html.escape(s['sha256'])}</code></li>" for s in sources)
            + "</ul></main></body></html>")
    (folder / "report.html").write_text(page, encoding="utf-8")
    files = [{"path": p.relative_to(folder).as_posix(), "bytes": p.stat().st_size, "sha256": _sha256(p), "md5": _md5(p)}
             for p in sorted(folder.rglob("*")) if p.is_file()]
    manifest = {"schema_version": SCHEMA_VERSION, "id": report_id, "kind": kind, "subject_id": subject_id, "title": b.title, "created_at": created,
                "sources": sources, "files": files, "warnings": warnings,
                "code": {"reports_sha256": _sha256(Path(__file__)), "matplotlib": matplotlib_version},
                "review": "Values and verified flags are copied from saved records; this report verifies nothing."}
    atomic_json(folder / "manifest.json", manifest)
    return manifest


def _record_sha(path):
    return {"sha256": _sha256(path)} if path.is_file() else {"sha256": None}


def _workflow_inputs(builder, record, research_root, workflows, folder, sources):
    """Copy bounded reproduction inputs, never engines, raw trajectories or process environments."""
    from .workflows import digest
    folder.mkdir()
    size = 0

    def save(name, value, raw=False):
        nonlocal size
        encoded = value if raw else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        size += len(encoded)
        if size > MAX_INPUT_BYTES:
            raise ValueError("보고서의 재현 입력이 20 MiB를 넘습니다. 결과를 나누어 보관하세요.")
        (folder / name).write_bytes(encoded)

    def read_input(path):
        relative = path.relative_to(research_root)
        if any(research_root.joinpath(*relative.parts[:i]).is_symlink() for i in range(1, len(relative.parts) + 1)):
            raise ValueError("보고서 재현 입력에 심볼릭 링크를 사용할 수 없습니다.")
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("보고서의 재현 입력이 20 MiB를 넘습니다. 결과를 나누어 보관하세요.")
        return path.read_bytes()

    save("workflow.json", record)
    design_id = record.get("design_id")
    if isinstance(design_id, str) and IDENTIFIER.fullmatch(design_id):
        design_folder = research_root / "doe" / design_id
        raw = read_input(design_folder / "result.json")
        if raw is None:
            builder.warnings.append("The saved DOE design is missing; the reproduction bundle omits it rather than reconstructing it.")
        else:
            design = json.loads(raw)
            if record.get("design_sha256") and digest(design) != record["design_sha256"]:
                raise ValueError("연구 흐름의 실험 설계 기록이 저장 당시와 다릅니다.")
            save("design.json", raw, raw=True)
            metadata = design.get("input") or {}
            name = metadata.get("file", "design.csv")
            if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
                raise ValueError("실험 설계의 입력 파일 이름이 올바르지 않습니다.")
            csv_bytes = read_input(design_folder / name)
            if csv_bytes is None:
                builder.warnings.append("The original DOE design CSV is missing from the reproduction bundle.")
            elif not metadata.get("sha256"):
                builder.warnings.append("The original DOE design CSV has no saved checksum; it is omitted from the reproduction bundle.")
            elif hashlib.sha256(csv_bytes).hexdigest() != metadata["sha256"]:
                raise ValueError("실험 설계의 입력 CSV가 저장 당시와 다릅니다.")
            else:
                save("design.csv", csv_bytes, raw=True)
                sources.append({"type": "doe_design_input", "id": design_id, "sha256": metadata["sha256"]})
    else:
        builder.warnings.append("The workflow has no valid saved DOE design ID; no design is reconstructed.")

    entries, seen = [], set()

    def job(identifier, role, saved=None, reserved=False, **context):
        key = (identifier, role, context.get("run_order"), context.get("attempt"))
        if not identifier or key in seen:
            return
        seen.add(key)
        entry = {"job_id": identifier, "role": role, **context}
        if isinstance(identifier, str) and IDENTIFIER.fullmatch(identifier):
            job_root = workflows.jobs.root
            if job_root.is_symlink() or (job_root / identifier).is_symlink() or (job_root / identifier / "job.json").is_symlink():
                raise ValueError("보고서 재현 입력에 심볼릭 링크를 사용할 수 없습니다.")
        try:
            value = workflows.jobs.get(identifier)
        except (KeyError, OSError, ValueError):
            value = None
        if value is not None:
            entry.update(status=value["status"], config=value["config"], source="job")
        elif reserved and saved is not None:
            entry.update(status="reserved_unsubmitted", config=saved, source="saved_row")
        else:
            entry.update(status="missing", config=None, source="missing")
            builder.warnings.append(f"Job {identifier} ({role}) is missing; its original configuration is not reconstructed.")
        config = entry["config"]
        entry.update(seed=config.get("seed") if config else None, config_sha256=digest(config) if config else None)
        result = value.get("result") if value else None
        entry["result_summary"] = ({name: result.get(name) for name in ("engine", "metadata", "metrics", "warnings", "status")}
                                   if isinstance(result, dict) else None)
        metadata = result.get("metadata") if isinstance(result, dict) else None
        versions = result.get("engine_versions") if isinstance(result, dict) else None
        if versions is None and isinstance(metadata, dict):
            versions = metadata.get("engine_versions")
        entry.update(engine_versions=versions, engine_versions_status="recorded" if versions is not None else "not_recorded")
        entries.append(entry)
        if config and config.get("source_job_id"):
            # Tg inputs also need the original cell configuration, not another copy of its coordinates.
            source = config["source_job_id"]
            if not any(e["job_id"] == source for e in entries):
                job(source, "source_cell", source_for=identifier)

    for row in record["rows"]:
        for attempt, identifier in enumerate(row["attempts"], 1):
            job(identifier, "row_attempt", run_order=row["run_order"], attempt=attempt)
        if row["job_id"] not in row["attempts"]:
            job(row["job_id"], "row_reserved", row["config"], reserved=not row["attempts"], run_order=row["run_order"])
    for confirmation in record["confirmations"]:
        job(confirmation["job_id"], "confirmation", optimization_id=confirmation["optimization_id"])
        if confirmation.get("scan_job_id"):
            job(confirmation["scan_job_id"], "confirmation_scan", optimization_id=confirmation["optimization_id"])
    save("job-configs.json", {"schema_version": 1, "workflow_id": record["id"], "jobs": entries,
                              "checksum_convention": "SHA-256 of canonical sorted JSON including every saved configuration field"})
    if record.get("autopilot"):
        autopilot = record["autopilot"]
        if autopilot.get("plan_sha256") and digest(autopilot.get("plan")) != autopilot["plan_sha256"]:
            raise ValueError("저장된 자동 실행 계획의 해시가 일치하지 않습니다.")
        save("plan.json", autopilot)
    if record.get("target_contract") is not None:
        contract = record["target_contract"]
        if record.get("target_contract_sha256") and digest(contract) != record["target_contract_sha256"]:
            raise ValueError("저장된 연구 목표의 해시가 일치하지 않습니다.")
        save("target-contract.json", contract)
    save("README.md", ("# 로컬 재현 입력\n\nworkflow.json은 보고서 생성 시점의 흐름 기록입니다. design.json과 design.csv는 저장된 설계와 원본 CSV입니다. "
        "job-configs.json에는 행별 시도·확인·Tg 스캔의 원래 설정, 시드, 상태와 역할이 있습니다. 미제출 예약 행은 reserved_unsubmitted로, "
        "사라진 작업은 missing으로 표시합니다. result_summary는 저장된 엔진·힘장·프로토콜·수렴 판정과 경고를 옮깁니다. "
        "과거 엔진 버전이 저장되지 않았다면 engine_versions는 null/not_recorded이며 현재 환경의 버전으로 대체하지 않습니다. "
        "plan.json과 target-contract.json은 저장돼 있을 때만 포함됩니다.\n\n"
        "이 묶음은 로컬에서 입력을 검토하고 다시 설정하기 위한 자료입니다. 엔진·의존성·원본 MD 구조/궤적·환경 변수·로그인 정보는 포함하지 않으며, "
        "파일을 열어도 계산은 자동 실행되지 않습니다. 같은 결과나 물리적 타당성을 보장하지 않습니다. 시뮬레이션 결과는 검토 전 상태로 유지됩니다.\n").encode("utf-8"), raw=True)


def _reports_root(research_root):
    root = Path(research_root) / "reports"
    if root.is_symlink():
        raise ValueError("보고서 폴더에 링크를 사용할 수 없습니다.")
    root.mkdir(exist_ok=True)
    return root


def create_report(research_root, kind, subject_id=None, store=None, workflows=None, density_studies=None):
    """Build and publish an immutable report folder plus a ZIP of it; nothing in the sources changes."""
    from .workflows import read_record
    research_root = Path(research_root)
    if kind not in KINDS:
        raise ValueError("보고서 종류는 doe, workflow, lab, density_study 중 하나입니다.")
    if kind != "lab" and (not isinstance(subject_id, str) or not IDENTIFIER.fullmatch(subject_id)):
        raise ValueError("보고서로 만들 기록 ID(32자리)를 지정하세요.")
    report_id = uuid.uuid4().hex
    created = datetime.now(timezone.utc)
    sources = []
    with tempfile.TemporaryDirectory(prefix=".report-", dir=_reports_root(research_root)) as temp, _figure_settings(report_id):
        temp = Path(temp)
        if kind == "doe":
            try:
                record = read_record(research_root / "doe", subject_id)
            except KeyError as exc:
                raise KeyError("실험계획 기록을 찾을 수 없습니다.") from exc
            builder = Builder(TITLES.get(record.get("record_type"), "실험계획 보고서"))
            input_file = research_root / "doe" / subject_id / record.get("input", {}).get("file", "input.csv")
            raw = input_file.read_bytes() if input_file.is_file() and not input_file.is_symlink() else None
            if raw is not None and hashlib.sha256(raw).hexdigest() != record.get("input", {}).get("sha256"):
                raise ValueError("실험계획 기록의 입력 CSV가 저장 당시와 다릅니다.")
            text = raw.decode("utf-8") if raw is not None else None
            sources.append({"type": record.get("record_type"), "id": subject_id, **_record_sha(research_root / "doe" / subject_id / "result.json"),
                            "input_sha256": record.get("input", {}).get("sha256")})
            build_doe(builder, record, None if record.get("record_type") in {"doe_design", "doe_mixture_design"} else text)
        elif kind == "workflow":
            try:
                record = workflows.get(subject_id)
            except KeyError as exc:
                raise KeyError("연구 흐름을 찾을 수 없습니다.") from exc
            builder = Builder(TITLES["bulk_workflow"])
            sources.append({"type": "bulk_workflow", "id": subject_id, "revision": record.get("revision"),
                            **_record_sha(research_root / "workflows" / subject_id / "result.json")})
            jobs = {r["job_id"] for r in record["rows"]} | {c["job_id"] for c in record["confirmations"]}
            build_workflow(builder, record, [m for m in store.list_measurements() if m.get("job_id") in jobs])
            from .result_quality import summarize_workflow
            quality = summarize_workflow(record, workflows)
            build_quality_evidence(builder, quality)
        elif kind == "density_study":
            if density_studies is None:
                from .density_studies import DensityStudyManager
                density_studies = DensityStudyManager(research_root, workflows.jobs, store)
            record = density_studies.get(subject_id)
            assessment = record.get('latest_assessment')
            if assessment is None:
                raise ValueError('먼저 대표 밀도의 근거 평가를 저장하세요.')
            builder = Builder('대표 밀도 근거 보고서')
            sources.append({'type':'density_study','id':subject_id, **_record_sha(density_studies.root / subject_id / 'result.json')})
            sources.append({'type':'density_assessment','id':assessment['id'],**_record_sha(density_studies.assessment_path(subject_id,assessment['id']))})
            build_density_study(builder,record,assessment)
        else:
            import duckdb
            snapshot = store.export_snapshot(temp / "snapshot")
            with duckdb.connect() as con:
                def read(name):
                    path = str(temp / "snapshot" / name).replace("'", "''")
                    return [json.loads(row[0]) for row in con.execute(f"SELECT record FROM '{path}'").fetchall()]
                experiments, measurements = read("experiments_master.parquet"), read("measurements.parquet")
            experiments.sort(key=lambda e: (str(e.get("finished_at") or e.get("created_at") or ""), str(e.get("job_id"))), reverse=True)
            measurements.sort(key=lambda m: (str(m.get("created_at") or ""), str(m.get("id"))), reverse=True)
            sources += [{"type": "research_snapshot_" + f["name"].split(".")[0], "id": snapshot["created_at"], "sha256": f["sha256"]} for f in snapshot["files"]]
            builder = Builder(TITLES["lab"])
            build_lab(builder, experiments, measurements)
        folder_name = f"{created.strftime('%Y%m%d-%H%M%S')}-{kind}-{(subject_id or 'all')[:8]}-{report_id[:8]}"
        content = temp / "content"
        content.mkdir()
        if kind == "workflow":
            _workflow_inputs(builder, record, research_root, workflows, content / "inputs", sources)
            atomic_json(content / 'inputs' / 'quality.json', quality)
        elif kind == 'density_study':
            inputs = content / 'inputs'
            inputs.mkdir()
            atomic_json(inputs / 'density-study.json',record)
            atomic_json(inputs / 'density-assessment.json',assessment)
            atomic_json(inputs / 'planned-configs.json',record['planned_configs'])
        manifest = _render(builder, report_id, kind, subject_id, created.isoformat(), sources, content)
        zip_name = f"PolymerLab-report-{folder_name}.zip"
        with zipfile.ZipFile(temp / zip_name, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(content.rglob("*")):
                if path.is_file():
                    bundle.write(path, f"{folder_name}/{path.relative_to(content).as_posix()}")
        staging = temp / "publish"
        staging.mkdir()
        content.rename(staging / "content")
        (temp / zip_name).rename(staging / zip_name)
        record = {"id": report_id, "schema_version": SCHEMA_VERSION, "record_type": "research_report", "kind": kind, "subject_id": subject_id,
                  "title": builder.title, "created_at": created.isoformat(), "folder_name": folder_name, "files": manifest["files"],
                  "manifest_sha256": _sha256(staging / "content" / "manifest.json"),
                  "zip": {"filename": zip_name, "bytes": (staging / zip_name).stat().st_size, "sha256": _sha256(staging / zip_name)},
                  "warnings": manifest["warnings"], "status": "local_ready", "drive": None,
                  "view_url": f"/api/research/reports/{report_id}/view", "download_url": f"/api/research/reports/{report_id}/download"}
        atomic_json(staging / "result.json", record)
        staging.rename(_reports_root(research_root) / report_id)
    return record


def read_report(research_root, identifier):
    from .workflows import read_record
    try:
        return read_record(_reports_root(research_root), identifier)
    except KeyError as exc:
        raise KeyError("보고서를 찾을 수 없습니다.") from exc


def upload_report(research_root, identifier, remote):
    """Copy the report folder to the user's Drive remote and only claim success after every file's size and MD5 match."""
    from .drive_backup import REMOTE_NAME, _rclone
    if not REMOTE_NAME.fullmatch(remote):
        raise ValueError("Google Drive 원격 이름이 올바르지 않습니다.")
    record = read_report(research_root, identifier)
    folder = _reports_root(research_root) / identifier
    content = folder / "content"
    if content.is_symlink() or not content.is_dir():
        raise ValueError("보고서 파일을 찾을 수 없습니다.")
    expected = {f["path"]: f for f in record["files"]}
    expected["manifest.json"] = {"path": "manifest.json", "bytes": (content / "manifest.json").stat().st_size,
                                 "sha256": record["manifest_sha256"], "md5": _md5(content / "manifest.json")}
    local = {p.relative_to(content).as_posix() for p in content.rglob("*") if p.is_file() or p.is_symlink()}
    if local != set(expected) or any((content / p).is_symlink() or _sha256(content / p) != f["sha256"] for p, f in expected.items()):
        raise ValueError("보고서 파일이 만든 뒤 바뀌었습니다. 새 보고서를 만드세요.")
    target = f"{remote}:{DESTINATION}/{record['folder_name']}"
    with _UPLOAD_LOCK:
        _rclone(["copy", str(content), target, "--immutable"], timeout=600)
        try:
            listing = json.loads(_rclone(["lsjson", target, "--recursive", "--files-only", "--hash"], timeout=120))
            remote_files = {entry["Path"]: entry for entry in listing}
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("Drive 업로드 후 파일 목록을 확인하지 못했습니다. 업로드 성공으로 표시하지 않습니다.") from exc
        mismatched = sorted(p for p, f in expected.items() if p not in remote_files or remote_files[p].get("Size") != f["bytes"]
                            or str((remote_files[p].get("Hashes") or {}).get("md5", "")).lower() != f["md5"])
        extra = sorted(set(remote_files) - set(expected))
        if mismatched or extra:
            raise RuntimeError(f"Drive 폴더의 파일 {len(mismatched) + len(extra)}개가 보고서와 일치하지 않습니다. 업로드 성공으로 표시하지 않습니다.")
        record.update(status="uploaded", drive={"destination": target, "verified": True, "algorithm": "md5", "files": len(expected), "uploaded_at": now()})
        atomic_json(folder / "result.json", record)
    return record


# ---------------------------------------------------------------- HTTP

class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    id: str | None = None


def _http(call):
    try:
        return call()
    except KeyError as exc:
        raise HTTPException(404, exc.args[0] if exc.args and isinstance(exc.args[0], str) and not IDENTIFIER.fullmatch(exc.args[0]) else "기록을 찾을 수 없습니다.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/reports", status_code=201)
def create_report_endpoint(payload: ReportRequest, request: Request):
    state = request.app.state
    return _http(lambda: create_report(state.research_root, payload.kind, payload.id, state.research_store, state.workflows, getattr(state, 'density_studies', None)))


@router.get("/reports")
def list_reports(request: Request):
    root = _reports_root(request.app.state.research_root)
    records = []
    for path in root.glob("*/result.json"):
        try:
            record = read_report(request.app.state.research_root, path.parent.name)
        except (KeyError, ValueError, OSError):
            continue
        records.append({k: record.get(k) for k in ("id", "kind", "subject_id", "title", "created_at", "status", "view_url", "download_url")})
    return sorted(records, key=lambda r: r["created_at"], reverse=True)


@router.get("/reports/{identifier}")
def get_report(identifier: str, request: Request):
    return _http(lambda: read_report(request.app.state.research_root, identifier))


def _report_file(request, identifier, relative):
    record = _http(lambda: read_report(request.app.state.research_root, identifier))
    folder = _reports_root(request.app.state.research_root) / identifier
    path = folder / relative(record)
    if path.is_symlink() or not path.is_file() or folder.resolve() not in path.resolve().parents:
        raise HTTPException(404, "보고서 파일을 찾을 수 없습니다.")
    return record, path


@router.get("/reports/{identifier}/download")
def download_report(identifier: str, request: Request):
    record, path = _report_file(request, identifier, lambda r: r["zip"]["filename"])
    return FileResponse(path, media_type="application/zip", filename=record["zip"]["filename"])


@router.get("/reports/{identifier}/view")
def view_report(identifier: str, request: Request):
    _, path = _report_file(request, identifier, lambda r: "content/report.html")
    # Reports never need scripts; the policy keeps any copied text inert.
    return Response(path.read_bytes(), media_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:", "X-Content-Type-Options": "nosniff"})


@router.post("/reports/{identifier}/drive")
def upload_report_endpoint(identifier: str, request: Request):
    from .drive_backup import drive_status
    status = drive_status()
    if not status["configured"]:
        raise HTTPException(409, {"status": "setup_required", "message": status["message"], "setup_instructions": status["setup_instructions"]})
    try:
        return upload_report(request.app.state.research_root, identifier, status["remote"])
    except KeyError as exc:
        raise HTTPException(404, "보고서를 찾을 수 없습니다.") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, {"status": "upload_failed", "message": str(exc)}) from exc
