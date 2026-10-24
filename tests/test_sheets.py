"""Paste-to-run sheets: parsing with line-numbered errors, Blends pairs equal to direct calls, checked job batches."""
import csv
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from workbench import blends, sheets
from workbench.execution import ExecutionCancelled
from workbench.server import create_app

BLENDS = """# Polymer Lab 실행표 v1
종류: blends
이름: 작은 시험
기준: 폴리에틸렌 | [*]CC[*] | 중합도=50
대상: 메탄올 | CO
대상: 에테인 | CC
온도: 280~320/20
표본: 3000
클러스터: 30
시드: 5
"""

JOBS = """# Polymer Lab 실행표 v1
종류: 작업
이름: 묶음 시험
공통: route=polymer_atomistic | atomistic_mode=bulk | atomistic_forcefield=gaff2 | cell_builder=native
공통: chains=8 | monomers=10 | protocol_scale=0.05
작업: PE | repeat_smiles=[*]CC[*]
작업: PS 350K | repeat_smiles=[*]CC([*])c1ccccc1 | temperature_kelvin=350
"""


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")) as client:
        yield client


def wait(client, identifier, done):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        record = client.get(f"/api/research/sheets/{identifier}").json()
        if done(record):
            return record
        time.sleep(0.1)
    raise AssertionError(f"sheet did not finish: {record}")


def test_every_template_parses_and_errors_name_their_lines():
    for template in sheets.TEMPLATES:
        result = sheets.preview(template["text"])
        assert result["valid"], (template["id"], result["errors"])
    broken = ("```\n# Polymer Lab 실행표 v1\n종류: blends\n기준: PS | [*]CC([*])c1ccccc1 | 중합도=0\n기준: PS | CC\n"
              "대상: X | C1CC\n온도: 300, 300\n클러스터: 10\n말단: 불소\nfoo: bar\n이 줄은 키가 없음\n```")
    errors = {e["line"]: e["message"] for e in sheets.preview(broken)["errors"]}
    assert set(errors) == {4, 5, 6, 7, 8, 9, 10, 11}
    assert "중합도" in errors[4] and "겹칩니다" in errors[5] and "SMILES" in errors[6] and "같은 온도" in errors[7]
    assert "클러스터" in errors[8] and "말단" in errors[9] and "쓸 수 있는 키" in errors[10] and "'키: 값'" in errors[11]
    assert sheets.preview("종류: blends")["errors"][0]["line"] == 1
    assert "종류" in sheets.preview("# Polymer Lab 실행표 v1\n이름: x")["errors"][0]["message"]


def test_values_temperature_ranges_and_species_options():
    plan = sheets.inspect(BLENDS)
    assert plan["settings"]["temperatures"] == [280.0, 300.0, 320.0]
    assert plan["settings"] | {"temperatures": None} == {"samples": 3000, "clusters": 30, "seed": 5, "cap": "hydrogen", "contact_scale": 1.0, "temperatures": None}
    assert [(i["base"]["name"], i["base"]["n"], i["screen"]["name"]) for i in plan["items"]] == [("폴리에틸렌", 50, "메탄올"), ("폴리에틸렌", 50, "에테인")]
    bare = sheets.inspect("# Polymer Lab 실행표 v1\n종류: blends\n기준: [*]CC[*] | 중합도=5\n대상: CO\n온도: 300K 350k\n말단: 메틸\n표본: 4,000")
    item = bare["items"][0]
    assert item["base"] == {"name": "[*]CC[*]", "smiles": "[*]CC[*]", "n": 5.0, "line": 3} and item["screen"]["name"] == "CO"
    assert bare["settings"]["temperatures"] == [300.0, 350.0] and bare["settings"]["cap"] == "methyl" and bare["settings"]["samples"] == 4000
    too_many = "# Polymer Lab 실행표 v1\n종류: blends\n" + "".join(f"기준: B{i} | {'C' * (i + 1)}\n" for i in range(11)) + "".join(f"대상: S{i} | {'C' * (i + 1)}O\n" for i in range(10))
    assert "100개까지" in sheets.preview(too_many)["errors"][0]["message"]


def test_job_sheet_errors_point_at_the_shared_or_job_line(client):
    text = JOBS.replace("chains=8", "chains=20") + "작업: PE | repeat_smiles=[*]CC[*]\n작업: X | pdb_text=abc | foo=1 | name=y\n"
    errors = client.post("/api/research/sheets/preview", json={"text": text}).json()["errors"]
    messages = [(e["line"], e["message"]) for e in errors]
    assert (8, "작업 이름 'PE'이(가) 겹칩니다.") in messages
    assert sum("작업 화면" in m for line, m in messages if line == 9) == 1 and any("foo" in m for _, m in messages) and any("첫 칸" in m for _, m in messages)
    fixed = JOBS.replace("chains=8", "chains=20")
    errors = client.post("/api/research/sheets/preview", json={"text": fixed}).json()["errors"]
    assert errors == [{"line": 5, "message": "[공통] chains 값 20: 16 이하여야 합니다", "text": "5번째 줄: [공통] chains 값 20: 16 이하여야 합니다"}]
    scan = JOBS + "작업: Tg | atomistic_mode=tg_scan | cell_builder=radonpy | source_job_id=" + "a" * 32 + "\n"
    errors = client.post("/api/research/sheets/preview", json={"text": scan}).json()["errors"]
    assert errors[0]["line"] == 8 and "찾을 수 없습니다" in errors[0]["message"]
    created = client.post("/api/research/sheets", json={"text": fixed})
    assert created.status_code == 422 and "5번째 줄" in created.json()["detail"]
    assert client.app.state.manager.list() == [] and client.get("/api/research/sheets").json()["records"] == []


def test_job_sheet_creates_the_checked_jobs_and_cancels_them(client):
    manager = client.app.state.manager
    manager.pool.submit = lambda function, job_id: None  # jobs stay queued; no engine runs
    preview = client.post("/api/research/sheets/preview", json={"text": JOBS}).json()
    assert preview["valid"] and [i["estimate"]["total_atoms"] for i in preview["items"]] == [496, 1296]
    created = client.post("/api/research/sheets", json={"text": JOBS})
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["status"] == "submitted" and record["counts"] == {"queued": 2} and record["progress"] == "running"
    jobs = {job["name"]: job for job in manager.list()}
    assert set(jobs) == {"묶음 시험 · PE", "묶음 시험 · PS 350K"}
    pe, ps = jobs["묶음 시험 · PE"], jobs["묶음 시험 · PS 350K"]
    assert pe["submitted_via"] == ps["submitted_via"] == "sheet:" + record["id"]
    assert (pe["config"]["chains"], pe["config"]["monomers"], pe["config"]["protocol_scale"], pe["config"]["temperature_kelvin"]) == (8, 10, 0.05, 300)
    assert ps["config"]["temperature_kelvin"] == 350 and ps["config"]["repeat_smiles"] == "[*]CC([*])c1ccccc1" and ps["config"]["cell_builder"] == "native"
    assert [i["job_id"] for i in record["items"]] == [pe["id"], ps["id"]]
    assert client.get(f"/api/research/sheets/{record['id']}/text").text == JOBS
    cancelled = client.post(f"/api/research/sheets/{record['id']}/cancel").json()
    assert cancelled["counts"] == {"cancelled": 2} and cancelled["progress"] == "finished"
    assert client.post(f"/api/research/sheets/{record['id']}/resume").status_code == 409


@pytest.mark.parametrize("stop", ["cancel", "shutdown"])
def test_stop_during_job_publication_does_not_submit_remaining_jobs(client, monkeypatch, stop):
    manager = client.app.state.manager
    sheets_manager = client.app.state.sheets
    monkeypatch.setattr(manager.pool, "submit", lambda function, job_id: None)
    entered, release = threading.Event(), threading.Event()
    original = manager.create

    def delayed_create(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "create", delayed_create)
    with ThreadPoolExecutor(max_workers=1) as pool:
        submission = pool.submit(sheets_manager.create, JOBS)
        try:
            assert entered.wait(10)
            identifier = sheets_manager.list()[0]["id"]
            if stop == "cancel":
                cancelled = sheets_manager.cancel(identifier)
                assert cancelled["status"] == "cancelled"
                assert cancelled["counts"] == {"not_created": 2}
                with pytest.raises(RuntimeError, match="마무리"):
                    sheets_manager.resume(identifier)
            else:
                sheets_manager.close()
        finally:
            release.set()
        result = submission.result(timeout=10)
    assert result["status"] == ("cancelled" if stop == "cancel" else "interrupted")
    assert result["counts"] == {"cancelled": 1, "not_created": 1}
    assert [job["status"] for job in manager.list()] == ["cancelled"]


def test_blends_sheet_pairs_equal_direct_calls_and_rank_by_reference_chi(client):
    created = client.post("/api/research/sheets", json={"text": BLENDS})
    assert created.status_code == 201, created.text
    record = wait(client, created.json()["id"], lambda r: r["status"] not in {"queued", "running"})
    assert record["status"] == "completed" and record["counts"] == {"completed": 2}, record
    for item in record["items"]:
        direct = blends.blend_chi("[*]CC[*]", item["screen"]["smiles"], temperatures=[280.0, 300.0, 320.0], samples=3000, clusters=30, seed=5,
                                  n_base=50, n_screen=1)
        assert item["result"]["chi_reference"] == direct["chi_reference"]["chi"]
        assert item["result"]["chi"] == {f"{row['temperature_kelvin']:g}": row["chi"] for row in direct["chi_vs_temperature"]}
        assert item["result"]["critical_chi"] == pytest.approx(0.5 * (1 / 50 ** 0.5 + 1) ** 2)
        saved = json.loads((client.app.state.research_root / "sheets" / record["id"] / "pairs" / f"{item['index']}.json").read_text())
        assert saved["fit"] == direct["fit"]
    chis = [row["chi_reference"] for row in record["ranking"]]
    assert chis == sorted(chis) and [row["rank"] for row in record["ranking"]] == [1, 2]
    text = client.get(record["csv_url"]).content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [row["screen"] for row in rows] == [row["screen"] for row in record["ranking"]]
    assert {"chi_280K", "chi_300K", "chi_320K", "critical_temperature_K"} <= set(rows[0]) and rows[0]["chi_reference_temperature_K"] == "300.0"
    assert any("exploratory" in warning for warning in record["warnings"])


class FakeExecution:
    """Returns a canned blend result per pair; the second call fails like an unparsable component."""
    def __init__(self, cancel_after=None):
        self.calls, self.cancel_after = [], cancel_after

    def compute(self, operation, payload, owner=None, cancelled=None, wait=None):
        self.calls.append(payload)
        if self.cancel_after is not None and len(self.calls) > self.cancel_after:
            raise ExecutionCancelled("계산이 취소되었습니다.")
        if payload["screen_smiles"] == "CCN":
            raise ValueError("GAFF2 파라미터를 붙일 수 없습니다: CCN")
        chi = len(payload["screen_smiles"]) / 10
        return {"chi_reference": {"temperature_kelvin": 300.0, "chi": chi, "bootstrap_std": 0.01}, "fit": {"A": chi, "B_kelvin": 0.0},
                "phase": {"critical_chi": 2.0, "critical_temperature_kelvin": None, "kind": "temperature-independent chi"},
                "chi_vs_temperature": [{"temperature_kelvin": 300.0, "chi": chi}]}

    def cancel(self, owner=None):
        pass


def test_cancel_before_blend_thread_starts_preserves_the_stop(tmp_path, monkeypatch):
    execution = FakeExecution()
    manager = sheets.SheetManager(tmp_path, manager=None, execution=execution)
    start = manager._start

    def cancelled_start(identifier):
        manager.cancel(identifier)
        start(identifier)

    monkeypatch.setattr(manager, "_start", cancelled_start)
    record = manager.create(BLENDS)
    assert record["status"] == "cancelled"
    assert record["counts"] == {"pending": 2}
    assert execution.calls == []
    assert record["id"] not in manager.threads


def test_interrupted_sheet_resumes_only_unfinished_pairs(tmp_path):
    text = "# Polymer Lab 실행표 v1\n종류: blends\n기준: A | CC\n대상: B | CCCCO\n대상: C | CCN\n대상: D | CO\n온도: 300"
    first = sheets.SheetManager(tmp_path, manager=None, execution=FakeExecution(cancel_after=1))
    record = first.create(text)
    first.threads[record["id"]].join(10)
    stored = first.get(record["id"])
    assert [i["status"] for i in stored["items"]] == ["completed", "pending", "pending"] and stored["status"] == "interrupted"
    assert len(first.execution.calls) == 2  # a cancelled pair stops the run instead of trying the next one
    # A crash leaves the record running; a new manager marks it interrupted.
    path = tmp_path / "sheets" / record["id"] / "record.json"
    data = json.loads(path.read_text())
    data["status"], data["items"][1]["status"] = "running", "running"
    path.write_text(json.dumps(data))
    execution = FakeExecution()
    second = sheets.SheetManager(tmp_path, manager=None, execution=execution)
    assert second.get(record["id"])["status"] == "interrupted"
    second.resume(record["id"])
    second.threads[record["id"]].join(10)
    final = second.get(record["id"])
    assert [p["screen_smiles"] for p in execution.calls] == ["CCN", "CO"]
    assert [i["status"] for i in final["items"]] == ["completed", "failed", "completed"] and final["status"] == "completed"
    assert "GAFF2" in final["items"][1]["error"] and [r["screen"] for r in final["ranking"]] == ["D", "B"]
    rows = list(csv.reader(io.StringIO((tmp_path / "sheets" / record["id"] / "results.csv").read_text(encoding="utf-8-sig"))))
    assert [row[4] for row in rows[1:]] == ["D", "B", "C"] and rows[3][7] == "failed"
    with pytest.raises(RuntimeError):
        second.resume(record["id"])


def test_mcp_run_sheet_previews_runs_and_reports(client):
    from workbench import mcp_server

    class AppClient:
        origin = "http://127.0.0.1:8765"

        def request(self, method, path, payload=None, timeout=30):
            response = client.request(method, path, json=payload)
            if response.is_error:
                raise mcp_server.ToolError(f"HTTP {response.status_code}: {response.text}")
            return response.json()
    client.app.state.manager.pool.submit = lambda function, job_id: None
    tool = mcp_server.PolymerLabTools(AppClient()).tools["run_sheet"]
    templates = tool["handler"]({"action": "templates"})
    assert {t["id"] for t in templates["templates"]} == {t["id"] for t in sheets.TEMPLATES}
    bad = tool["handler"]({"action": "preview", "text": JOBS.replace("monomers=10", "monomers=1")})
    assert not bad["valid"] and bad["errors"][0]["line"] == 5
    with pytest.raises(mcp_server.ToolError):
        tool["handler"]({"action": "status"})
    arguments = {"action": "run", "text": JOBS}
    mcp_server._check(tool["schema"], arguments)
    record = tool["handler"](arguments)
    assert record["kind"] == "jobs" and len(record["items"]) == 2
    assert tool["handler"]({"action": "status", "sheet_id": record["id"]})["counts"] == {"queued": 2}
    assert tool["handler"]({"action": "list"})["records"][0]["id"] == record["id"]


def test_completion_waits_for_csv_publication(tmp_path, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    manager = sheets.SheetManager(tmp_path, manager=None, execution=FakeExecution())

    def delayed_export(record):
        entered.set()
        assert release.wait(10)
        (manager.root / record['id'] / 'results.csv').write_text('rank,screen\n1,test\n')

    monkeypatch.setattr(manager, '_write_csv', delayed_export)
    record = manager.create(BLENDS)
    try:
        assert entered.wait(10)
        exporting = manager.get(record['id'])
        assert exporting['status'] == 'running'
        assert exporting['counts'] == {'completed': 2}
        assert exporting['csv_url'] is None
    finally:
        release.set()
        manager.threads[record['id']].join(10)
    completed = manager.get(record['id'])
    assert completed['status'] == 'completed'
    assert completed['csv_url']
    manager.close()
