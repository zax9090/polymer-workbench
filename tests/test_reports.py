"""Research reports copy saved numbers and review flags exactly; synthetic records and temporary folders only."""
import csv
import hashlib
import io
import json
import shutil
import xml.etree.ElementTree as ET
import zipfile

import pytest
from fastapi.testclient import TestClient

from workbench import drive_backup, reports
from workbench.server import create_app
from tests.test_workflows import create as create_workflow, synthesize

FACTORS = [{"name": "온도 <b>&", "low": 300.0, "high": 360.0}, {"name": "P$x", "low": 1.0, "high": 5.0}]
COMPONENTS = [{"name": "resin", "low": 0.1, "high": 1.0}, {"name": "hardener", "low": 0.0, "high": 1.0}, {"name": "filler", "low": 0.0, "high": 0.6}]
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        yield client


def table(text, columns, rows):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(columns)
    writer.writerows(rows)
    return stream.getvalue()


def doe_analysis(client):
    design = client.post("/api/research/doe/design", json={"factors": FACTORS, "kind": "central_composite", "seed": 5}).json()
    rows = []
    for run in design["runs"]:
        u, v = run["coded"][FACTORS[0]["name"]], run["coded"][FACTORS[1]["name"]]
        rows.append([run["values"][FACTORS[0]["name"]], run["values"][FACTORS[1]["name"]],
                     7 - (u - 0.3) ** 2 - 2 * (v + 0.2) ** 2 + 0.4 * u * v + 0.01 * (-1) ** run["run_order"]])
    text = table(None, [f["name"] for f in FACTORS] + ["y"], rows)
    response = client.post("/api/research/doe/analyze", json={"text": text, "factors": FACTORS, "response": "y", "model": "quadratic"})
    assert response.status_code == 201, response.text
    return response.json()


def report(client, kind, identifier=None):
    response = client.post("/api/research/reports", json={"kind": kind, "id": identifier})
    assert response.status_code == 201, response.text
    record = response.json()
    return record, client.app.state.research_root / "reports" / record["id"] / "content"


def read_csv(path):
    raw = path.read_bytes()
    assert raw.startswith("﻿".encode())
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))


def sheet_values(path):
    with zipfile.ZipFile(path) as book:
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        names = [s.get("name") for s in workbook.find("m:sheets", NS)]
        sheets = {}
        for index, name in enumerate(names, 1):
            root = ET.fromstring(book.read(f"xl/worksheets/sheet{index}.xml"))
            assert root.find("m:sheetViews/m:sheetView/m:pane", NS).get("state") == "frozen"
            grid = []
            for row in root.find("m:sheetData", NS):
                values = {}
                for cell in row:
                    column = "".join(c for c in cell.get("r") if c.isalpha())
                    kind = cell.get("t")
                    if kind == "inlineStr":
                        values[column] = cell.find("m:is/m:t", NS).text
                    elif kind == "b":
                        values[column] = cell.find("m:v", NS).text == "1"
                    else:
                        text = cell.find("m:v", NS).text
                        values[column] = float(text) if any(c in text for c in ".eE") or "inf" in text else int(text)
                grid.append(values)
            sheets[name] = grid
    return sheets


def test_xlsx_writer_round_trips_types_escapes_and_sheet_names(tmp_path):
    path = tmp_path / "t.xlsx"
    rows = [["<a & b>", 1, 2.5e-300, True, None, " pad ", "bad\x01char", {"k": [1, "한글"]}], [False, -0.0, 12345678901234, "", "=1+1", "x" * 40000, 0.1, "]]>"]]
    reports.write_xlsx(path, [("Sheet", ["a"] * 8, rows), ("Sheet", ["b"], []), ("bad[]:*?/\\name" + "z" * 40, ["c"], [[1]])])
    sheets = sheet_values(path)
    assert list(sheets) == ["Sheet", "Sheet (2)", "bad_______name" + "z" * 17]
    first = sheets["Sheet"]
    assert first[1] == {"A": "<a & b>", "B": 1, "C": 2.5e-300, "D": True, "F": " pad ", "G": "badchar", "H": '{"k": [1, "한글"]}'}
    assert first[2]["A"] is False and first[2]["B"] == 0.0 and first[2]["C"] == 12345678901234 and "D" not in first[2]
    assert first[2]["E"] == "=1+1" and len(first[2]["F"]) == 32767 and first[2]["G"] == 0.1 and first[2]["H"] == "]]>"
    with zipfile.ZipFile(path) as book:
        assert book.testzip() is None
        assert b'<cellStyle name="Normal"' in book.read("xl/styles.xml")


def test_csv_neutralizes_formulas_and_keeps_full_precision(tmp_path):
    path = tmp_path / "t.csv"
    reports.write_csv(path, ["=name", "value"], [["@cmd", 0.1 + 0.2], ["-1", -1.5], [True, None]])
    rows = read_csv(path)
    assert rows == [["'=name", "value"], ["'@cmd", repr(0.1 + 0.2)], ["'-1", "-1.5"], ["TRUE", ""]]
    assert float(rows[1][1]) == 0.1 + 0.2


def test_doe_report_copies_numbers_escapes_names_and_is_immutable(client):
    analysis = doe_analysis(client)
    source = client.app.state.research_root / "doe" / analysis["id"] / "result.json"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    record, content = report(client, "doe", analysis["id"])
    assert record["status"] == "local_ready" and record["drive"] is None and record["subject_id"] == analysis["id"]
    paths = {f["path"] for f in record["files"]}
    for name in ("fit", "residuals", "pareto", "surface", "profiles"):
        assert {f"figures/{name}.svg", f"figures/{name}.png"} <= paths
        ET.parse(content / "figures" / f"{name}.svg")
        assert (content / "figures" / f"{name}.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert {"summary.md", "report.html", "tables/coefficients.csv", "tables/anova.csv", "tables/diagnostics.csv", "tables/input.csv", "tables/report.xlsx"} <= paths
    for entry in record["files"]:
        assert hashlib.sha256((content / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
    manifest = json.loads((content / "manifest.json").read_text())
    assert hashlib.sha256((content / "manifest.json").read_bytes()).hexdigest() == record["manifest_sha256"]
    assert manifest["sources"][0] == {"type": "doe_analysis", "id": analysis["id"], "sha256": before, "input_sha256": analysis["input"]["sha256"]}
    coefficients = read_csv(content / "tables" / "coefficients.csv")[1:]
    assert [row[0] for row in coefficients] == [c["term"] for c in analysis["coefficients"]]
    assert [float(row[1]) for row in coefficients] == [c["estimate"] for c in analysis["coefficients"]]
    assert [float(row[4]) for row in coefficients] == [c["p_value"] for c in analysis["coefficients"]]
    recommendation = analysis["recommendation"]
    coded = [[recommendation["coded"][f["name"]] for f in FACTORS]]
    assert reports.predict(coded, analysis["coefficients"], "quadratic")[0] == pytest.approx(recommendation["predicted_response"], rel=1e-12)
    page = (content / "report.html").read_text()
    assert "온도 &lt;b&gt;&amp;" in page and "<b>&" not in page and "<script" not in page.lower()
    assert page.count("<svg") == 5 and "validated=false" in page
    summary = (content / "summary.md").read_text()
    assert "확인 실험 전" in summary and analysis["id"] in summary
    sheets = sheet_values(content / "tables" / "report.xlsx")
    assert sheets["모델 계수"][1]["B"] == analysis["coefficients"][0]["estimate"]
    view = client.get(record["view_url"])
    assert view.status_code == 200 and "default-src 'none'" in view.headers["content-security-policy"]
    with zipfile.ZipFile(io.BytesIO(client.get(record["download_url"]).content)) as bundle:
        for entry in record["files"]:
            assert hashlib.sha256(bundle.read(f"{record['folder_name']}/{entry['path']}")).hexdigest() == entry["sha256"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert client.get("/api/research/reports").json()[0]["id"] == record["id"]
    assert client.get(f"/api/research/reports/{record['id']}").json()["files"] == record["files"]


def test_mixture_and_desirability_reports_draw_their_own_figures(client):
    design = client.post("/api/research/doe/mixture-design", json={"components": COMPONENTS, "kind": "extreme_vertices", "degree": 2, "axial": True}).json()
    rows = []
    for run in design["runs"]:
        a, b, c = (run["values"][k["name"]] for k in COMPONENTS)
        rows.append([a, b, c, 10 * a + 8 * b + 6 * c + 12 * a * b + 0.01 * (-1) ** run["run_order"], 3 * a + c + 0.02 * (-1) ** run["run_order"]])
    text = table(None, [c["name"] for c in COMPONENTS] + ["strength", "cost"], rows)
    mixture = client.post("/api/research/doe/mixture-analyze", json={"text": text, "components": COMPONENTS, "response": "strength"})
    assert mixture.status_code == 201, mixture.text
    record, content = report(client, "doe", mixture.json()["id"])
    assert (content / "figures" / "ternary.svg").is_file() and not (content / "figures" / "surface.svg").exists()
    settings = {row[0]: float(row[1]) for row in read_csv(content / "tables" / "recommendation.csv")[1:4]}
    assert settings == mixture.json()["recommendation"]["settings"]
    goals = [{"name": "strength", "model": "scheffe_quadratic", "goal": "maximize", "lower": 8, "target": 12},
             {"name": "cost", "model": "scheffe_quadratic", "goal": "minimize", "target": 0.5, "upper": 3}]
    desirability = client.post("/api/research/doe/desirability", json={"text": text, "components": COMPONENTS, "responses": goals})
    assert desirability.status_code == 201, desirability.text
    record, content = report(client, "doe", desirability.json()["id"])
    assert (content / "figures" / "desirability.svg").is_file()
    responses = read_csv(content / "tables" / "responses.csv")[1:]
    assert [float(row[10]) for row in responses] == [r["desirability"] for r in desirability.json()["responses"]]
    summary = (content / "summary.md").read_text()
    assert f"{desirability.json()['optimum']['composite_desirability']:.5g}" in summary


def test_workflow_report_keeps_simulation_values_unverified(client):
    workflow = create_workflow(client)
    for row in workflow["rows"]:
        response, _ = synthesize(client, workflow, row)
        assert response.status_code == 200, response.text
    assert client.post(f"/api/research/workflows/{workflow['id']}/analyze", json={"rows": list(range(1, 12))}).status_code == 200
    optimized = client.post(f"/api/research/workflows/{workflow['id']}/optimize", json={})
    assert optimized.status_code == 200, optimized.text
    record, content = report(client, "workflow", workflow["id"])
    runs = read_csv(content / "tables" / "runs.csv")
    header = runs[0]
    stored = {row["run_order"]: row for row in client.app.state.workflows._read(workflow["id"])["rows"]}
    for line in runs[1:]:
        row = stored[int(line[0])]
        assert float(line[header.index("반응값")]) == row["observation"]["value"]
        assert line[header.index("검토")] == "FALSE"
    assert (content / "figures" / "surface.svg").is_file()
    assert any("No confirmation run has been analyzed" in w for w in record["warnings"])
    assert "측정값이 아니" in (content / "summary.md").read_text()
    assert client.get("/api/research/measurements").json() == []


def test_lab_report_separates_origin_and_review(client):
    store = client.app.state.research_store
    base = {"material": {"repeat_smiles": "[*]CC[*]"}, "property": "tg", "unit": "C", "conditions": {"heating_rate_K_min": 10},
            "source": "synthetic report test fixture, not research evidence"}
    store.add_measurement({**base, "value": 80, "method": "DSC", "origin": "experimental", "verified": True})
    store.add_measurement({**base, "value": 81, "method": "=HYPERLINK(\"x\")", "origin": "literature"})
    store.add_measurement({**base, "value": 79, "method": "DSC", "origin": "demo"})
    record, content = report(client, "lab")
    counts = {(row[0], row[1], row[2]): int(row[3]) for row in read_csv(content / "tables" / "counts.csv")[1:]}
    assert counts == {("tg", "demo", "FALSE"): 1, ("tg", "experimental", "TRUE"): 1, ("tg", "literature", "FALSE"): 1}
    measurements = read_csv(content / "tables" / "measurements.csv")
    method = measurements[0].index("방법")
    assert "'=HYPERLINK(\"x\")" in [row[method] for row in measurements[1:]]
    summary = (content / "summary.md").read_text()
    assert "**학습에 쓸 수 있는 값 (검토된 실험·문헌):** 1" in summary
    assert [s["type"] for s in record_sources(content)] == ["research_snapshot_experiments_master", "research_snapshot_measurements"]


def record_sources(content):
    return json.loads((content / "manifest.json").read_text())["sources"]


def fake_rclone(remote_root, corrupt=None):
    calls = []

    def run(arguments, timeout=30):
        calls.append(arguments)
        if arguments[0] == "copy":
            destination = remote_root / arguments[2].split(":", 1)[1]
            shutil.copytree(arguments[1], destination, dirs_exist_ok=True)
            if corrupt:
                (destination / corrupt).write_bytes(b"changed in transit")
            return ""
        if arguments[0] == "lsjson":
            destination = remote_root / arguments[1].split(":", 1)[1]
            return json.dumps([{"Path": p.relative_to(destination).as_posix(), "Size": p.stat().st_size, "IsDir": False,
                                "Hashes": {"md5": hashlib.md5(p.read_bytes()).hexdigest()}} for p in sorted(destination.rglob("*")) if p.is_file()])
        raise AssertionError(arguments)
    return run, calls


def test_drive_upload_claims_success_only_after_every_file_matches(client, tmp_path, monkeypatch):
    root = client.app.state.research_root
    first, _ = report(client, "lab")
    assert client.post(f"/api/research/reports/{first['id']}/drive").status_code == 409
    monkeypatch.setattr(drive_backup, "drive_status", lambda: {"configured": True, "remote": "gdrive", "message": "", "setup_instructions": ""})
    transport, calls = fake_rclone(tmp_path / "remote")
    monkeypatch.setattr(drive_backup, "_rclone", transport)
    response = client.post(f"/api/research/reports/{first['id']}/drive")
    assert response.status_code == 200, response.text
    uploaded = response.json()
    assert uploaded["status"] == "uploaded" and uploaded["drive"]["verified"] is True
    assert uploaded["drive"]["destination"] == f"gdrive:PolymerLab-reports/{first['folder_name']}"
    assert uploaded["drive"]["files"] == len(first["files"]) + 1
    assert calls[0][:1] == ["copy"] and "--immutable" in calls[0]
    second, content = report(client, "lab")
    transport, _ = fake_rclone(tmp_path / "remote", corrupt="summary.md")
    monkeypatch.setattr(drive_backup, "_rclone", transport)
    failed = client.post(f"/api/research/reports/{second['id']}/drive")
    assert failed.status_code == 502 and failed.json()["detail"]["status"] == "upload_failed"
    assert reports.read_report(root, second["id"])["status"] == "local_ready"
    (content / "summary.md").write_text("edited")
    transport, calls = fake_rclone(tmp_path / "remote")
    monkeypatch.setattr(drive_backup, "_rclone", transport)
    assert client.post(f"/api/research/reports/{second['id']}/drive").status_code == 422 and calls == []


def test_invalid_report_requests_create_nothing(client, tmp_path):
    assert client.post("/api/research/reports", json={"kind": "everything"}).status_code == 422
    assert client.post("/api/research/reports", json={"kind": "doe", "id": "../x"}).status_code == 422
    assert client.post("/api/research/reports", json={"kind": "doe", "id": "a" * 32}).status_code == 404
    assert client.post("/api/research/reports", json={"kind": "workflow", "id": "b" * 32}).status_code == 404
    assert client.post("/api/research/reports", json={"kind": "lab", "extra": 1}).status_code == 422
    assert client.get("/api/research/reports/" + "c" * 32 + "/view").status_code == 404
    assert client.get("/api/research/reports/not-an-id").status_code == 404
    assert client.get("/api/research/reports").json() == []
    folder = client.app.state.research_root / "reports"
    assert [p.name for p in folder.iterdir()] == []
    shutil.rmtree(folder)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    folder.symlink_to(outside, target_is_directory=True)
    assert client.post("/api/research/reports", json={"kind": "lab"}).status_code == 422
    assert list(outside.iterdir()) == []


def test_mcp_report_tool_summarizes_and_uploads_only_when_asked(client, tmp_path, monkeypatch):
    from workbench import mcp_server

    class AppClient:
        origin = "http://127.0.0.1:8765"

        def request(self, method, path, payload=None, timeout=30):
            response = client.request(method, path, json=payload)
            if response.is_error:
                raise mcp_server.ToolError(f"HTTP {response.status_code}: {response.text}")
            return response.json()
    tool = mcp_server.PolymerLabTools(AppClient()).tools["create_research_report"]
    assert tool["definition"]["annotations"]["readOnlyHint"] is False
    with pytest.raises(mcp_server.ToolError):
        mcp_server._check(tool["schema"], {"kind": "doe", "id": "../etc"})
    analysis = doe_analysis(client)
    transport, calls = fake_rclone(tmp_path / "remote")
    monkeypatch.setattr(drive_backup, "_rclone", transport)
    result = tool["handler"]({"kind": "doe", "id": analysis["id"]})
    assert result["status"] == "local_ready" and calls == []
    assert result["figures"] == ["fit", "pareto", "profiles", "residuals", "surface"] and "report.xlsx" in result["tables"]
    assert result["view_url"] == f"http://127.0.0.1:8765/api/research/reports/{result['id']}/view"
    monkeypatch.setattr(drive_backup, "drive_status", lambda: {"configured": True, "remote": "gdrive", "message": "", "setup_instructions": ""})
    uploaded = tool["handler"]({"kind": "lab", "upload_to_drive": True})
    assert uploaded["status"] == "uploaded" and uploaded["drive"]["verified"] is True and calls
