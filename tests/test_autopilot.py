"""Approved-plan execution of a bulk workflow with synthetic jobs that finish instantly; no MD engine runs."""
import time

import pytest
from fastapi.testclient import TestClient

from workbench import workflows
from workbench.jobs import atomic_json, now
from workbench.server import create_app
from tests.test_workflows import create

PLAN = {"max_attempts": 2, "max_failed_rows": 0, "model": "quadratic", "objective": "maximize"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "preflight", lambda template: [])
    with TestClient(create_app(tmp_path / "jobs")) as client:
        client.app.state.autopilot.poll = 0.01
        yield client


def runner(client, fail=lambda config, attempt: None, hold=False):
    """Replace the job pool: write the analytic cell of test_workflows and finish, fail or hold each job."""
    manager, attempts = client.app.state.manager, {}

    def submit(function, job_id):
        config = manager._read(job_id)["config"]
        key = (config["temperature_kelvin"], config["build_density"], config["seed"])
        attempts[key] = attempts.get(key, 0) + 1
        if hold:
            return
        status = fail(config, attempts[key])
        if status:
            manager._update(job_id, status=status, finished_at=now(), error="synthetic failure")
            return
        u, v = (config["temperature_kelvin"] - 310) / 30, (config["build_density"] - .8) / .2
        density = 1.1 - .1 * (u - .25) ** 2 - .15 * (v + .4) ** 2
        edge = (4 * 12.011 / 6.02214076e23 / density * 1e24) ** (1 / 3)
        directory = manager.directory(job_id)
        atomic_json(directory / "result.json", {"status": "completed", "warnings": ["Synthetic fixture; not a validated physical material."]})
        text = (f"Synthetic validation cell, not MD\n\n4 atoms\n1 atom types\n\n0 {edge:.17g} xlo xhi\n0 {edge:.17g} ylo yhi\n0 {edge:.17g} zlo zhi\n\n"
                "Masses\n\n1 12.011\n\nAtoms # full\n\n")
        text += "\n".join(f"{i} {i} 1 0 {edge*x:.17g} {edge*y:.17g} {edge*z:.17g} 0 0 0" for i, (x, y, z) in enumerate([(.2, .2, .2), (.7, .2, .2), (.2, .7, .2), (.2, .2, .7)], 1)) + "\n"
        (directory / "eq3_last.data").write_text(text)
        manager._update(job_id, status="completed", finished_at=now())
    client.app.state.manager.pool.submit = submit
    return attempts


def wait(client, identifier, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = client.get(f"/api/research/workflows/{identifier}").json()
        if record["autopilot"]["state"] != "running":
            return record
        time.sleep(0.05)
    raise AssertionError("autopilot did not finish")


def approve(client, identifier, plan=PLAN, budget=None):
    preview = client.post(f"/api/research/workflows/{identifier}/autopilot/preview", json=plan)
    assert preview.status_code == 200, preview.text
    return preview.json(), client.post(f"/api/research/workflows/{identifier}/autopilot",
                                       json={"plan": plan, "approved_budget": preview.json()["worst_case_runs"] if budget is None else budget})


def test_approved_plan_runs_to_confirmation_and_report(client):
    record = create(client)
    first = record["rows"][3]
    attempts = runner(client, fail=lambda c, n: "failed" if c["seed"] == first["config"]["seed"] and n == 1 else None)
    preview, response = approve(client, record["id"], budget=23)
    assert preview["worst_case_runs"] == 11 * 2 + 2 and preview["rows_to_run"] == list(range(1, 12))
    assert response.status_code == 422 and "24" in response.text
    _, response = approve(client, record["id"])
    assert response.status_code == 200, response.text
    done = wait(client, record["id"])
    pilot = done["autopilot"]
    assert pilot["state"] == "completed", pilot["events"]
    assert pilot["approved_by"] == "local" and len(pilot["plan_sha256"]) == 64
    retried = next(r for r in done["rows"] if r["run_order"] == first["run_order"])
    assert len(retried["attempts"]) == 2 and retried["config_sha256"] == first["config_sha256"]
    assert max(attempts.values()) == 2 and sum(attempts.values()) == 13
    assert any("재시도" in e["message"] for e in pilot["events"])
    best = done["optimizations"][-1]
    assert best["recommendation"]["settings"] == pytest.approx({"temp": 317.5, "packing": .72}, abs=1e-4)
    assert best["exclusions"] == {} and best["recommendation"]["validated"] is False
    confirmation = done["confirmations"][0]
    assert confirmation["observation"]["verified"] is False and confirmation["observation"]["prediction_error"] == pytest.approx(0, abs=0.02)
    assert all(row["observation"]["verified"] is False for row in done["rows"])
    assert done["submitted"] - pilot["submitted_at_approval"] == 13 <= pilot["approved_budget"]
    report = client.get(f"/api/research/reports/{pilot['report_id']}").json()
    assert report["kind"] == "workflow" and report["subject_id"] == record["id"]
    assert client.get("/api/research/measurements").json() == []


def test_failure_limit_stops_before_fitting(client):
    record = create(client)
    bad = {r["config"]["seed"] for r in record["rows"][:2]}
    runner(client, fail=lambda c, n: "failed" if c["seed"] in bad else None)
    _, response = approve(client, record["id"], {**PLAN, "max_failed_rows": 1})
    assert response.status_code == 200, response.text
    done = wait(client, record["id"])
    assert done["autopilot"]["state"] == "stopped" and "허용 수 1개" in done["autopilot"]["stopped_reason"]
    assert done["optimizations"] == [] and done["confirmations"] == []


def test_allowed_failures_are_excluded_with_reasons(client):
    record = create(client)
    bad = record["rows"][0]["config"]["seed"]
    runner(client, fail=lambda c, n: "failed" if c["seed"] == bad else None)
    _, response = approve(client, record["id"], {**PLAN, "max_failed_rows": 1, "confirm": False, "report": False})
    assert response.status_code == 200, response.text
    done = wait(client, record["id"])
    assert done["autopilot"]["state"] == "completed", done["autopilot"]["events"]
    exclusions = done["optimizations"][-1]["exclusions"]
    assert list(exclusions) == [str(record["rows"][0]["run_order"])] and "2회 시도" in exclusions[str(record["rows"][0]["run_order"])]


def test_blocked_job_is_not_retried(client):
    record = create(client)
    attempts = runner(client, fail=lambda c, n: "blocked")
    _, response = approve(client, record["id"])
    done = wait(client, record["id"])
    assert done["autopilot"]["state"] == "stopped" and "blocked" in done["autopilot"]["stopped_reason"]
    assert set(attempts.values()) == {1}


def test_stage_calls_wait_for_stop_and_cancel_stops(client):
    record = create(client)
    runner(client, hold=True)
    _, response = approve(client, record["id"])
    assert response.status_code == 200
    for path, body in (("submit", {"rows": [1]}), ("analyze", {"rows": [1]}), ("optimize", {})):
        blocked = client.post(f"/api/research/workflows/{record['id']}/{path}", json=body)
        assert blocked.status_code == 409 and "자동 실행 중" in blocked.text
    assert client.post(f"/api/research/workflows/{record['id']}/autopilot", json={"plan": PLAN, "approved_budget": 24}).status_code == 409
    stopped = client.post(f"/api/research/workflows/{record['id']}/autopilot/stop")
    assert stopped.status_code == 200 and stopped.json()["autopilot"]["stop_requested"] is True
    done = wait(client, record["id"])
    assert done["autopilot"]["state"] == "stopped"
    assert client.post(f"/api/research/workflows/{record['id']}/analyze", json={"rows": [1]}).status_code == 200
    _, response = approve(client, record["id"], {**PLAN, "confirm": False})
    assert response.status_code == 200
    client.post(f"/api/research/workflows/{record['id']}/cancel")
    done = wait(client, record["id"])
    assert done["autopilot"]["state"] == "cancelled"
    events = [e["stage"] for e in done["autopilot"]["events"]]
    assert events.count("approve") == 2 and events[-1] == "cancelled"


def test_plan_validation_and_restart(client, tmp_path, monkeypatch):
    record = create(client)
    url = f"/api/research/workflows/{record['id']}/autopilot"
    for plan in ({**PLAN, "optimize": False}, {**PLAN, "rows": [99]}, {**PLAN, "rows": [1, 2, 3]}, {**PLAN, "objective": "target"},
                 {**PLAN, "max_failed_rows": 5}, {**PLAN, "report": False, "upload_report": True}):
        assert client.post(url + "/preview", json=plan).status_code == 422, plan
    assert client.post(url, json={"plan": PLAN, "approved_budget": 51}).status_code == 422
    small = create(client, budget=20)
    assert client.post(f"/api/research/workflows/{small['id']}/autopilot", json={"plan": PLAN, "approved_budget": 20}).status_code == 422
    subset = client.post(url + "/preview", json={**PLAN, "rows": list(range(1, 9)), "confirm": False}).json()
    assert subset["worst_case_runs"] == 16 and subset["excluded_by_plan"] == [9, 10, 11]
    monkeypatch.setattr(workflows, "preflight", lambda template: ["LAMMPS"])
    assert client.post(url, json={"plan": PLAN, "approved_budget": 24}).status_code == 409
    manager = client.app.state.workflows
    saved = manager._read(record["id"])
    saved["autopilot"] = {"state": "running", "events": []}
    manager._save(saved)
    restarted = workflows.WorkflowManager(client.app.state.research_root, client.app.state.manager)
    assert restarted._read(record["id"])["autopilot"]["state"] == "interrupted"


def test_mcp_plan_tool_needs_an_approved_budget(client):
    from workbench import mcp_server

    class AppClient:
        def request(self, method, path, payload=None, timeout=30):
            response = client.request(method, path, json=payload, headers={"x-polymer-client": "mcp:pytest"})
            if response.is_error:
                raise mcp_server.ToolError(f"HTTP {response.status_code}: {response.text}")
            return response.json()
    tool = mcp_server.PolymerLabTools(AppClient()).tools["run_research_workflow_plan"]
    assert tool["definition"]["annotations"]["readOnlyHint"] is False
    record = create(client)
    runner(client)
    preview = tool["handler"]({"workflow_id": record["id"], "action": "preview", "plan": {"confirm": False, "report": False}})
    assert preview["worst_case_runs"] == 22
    with pytest.raises(mcp_server.ToolError, match="approved_budget"):
        tool["handler"]({"workflow_id": record["id"], "action": "start", "plan": {}})
    started = tool["handler"]({"workflow_id": record["id"], "action": "start", "plan": {"confirm": False, "report": False}, "approved_budget": 22})
    assert started["autopilot"]["approved_by"] == "mcp:pytest"
    assert wait(client, record["id"])["autopilot"]["state"] == "completed"
