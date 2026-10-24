"""PCFF bulk, epoxy crosslink and Tg follow-up workflows with synthetic jobs that finish instantly; no engine runs."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from workbench import workflows
from workbench.jobs import atomic_json, now
from workbench.server import create_app

PLAN = {"max_attempts": 1, "max_failed_rows": 0, "model": "quadratic", "objective": "maximize", "report": False}
PCFF = {"name": "Synthetic PCFF flow", "route": "polymer_atomistic", "atomistic_mode": "bulk", "atomistic_forcefield": "pcff", "protocol_scale": .001}
CURE = {"name": "Synthetic cure flow", "route": "polymer_atomistic", "atomistic_mode": "crosslink", "protocol_scale": .001}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "preflight", lambda template: [])
    with TestClient(create_app(tmp_path / "jobs")) as client:
        client.app.state.autopilot.poll = 0.01
        yield client


def pcff_density(config):
    u, v = (config["temperature_kelvin"] - 310) / 30, (config["monomers"] - 20) / 10
    return 1.05 - .08 * (u - .3) ** 2 - .1 * (v - .37) ** 2


def cure_values(config):
    c, t = (config["target_conversion"] - .75) / .15, (config["cure_temperature_kelvin"] - 450) / 50
    return {"epoxide": .95 * config["target_conversion"] + .002 * t, "density": 1.15 + .05 * c - .02 * t ** 2}


def tg_of(config):
    c, t = (config["target_conversion"] - .75) / .15, (config["cure_temperature_kelvin"] - 450) / 50
    return 380 + 40 * c - 15 * t ** 2


def cell(directory, name, density):
    edge = (4 * 12.011 / 6.02214076e23 / density * 1e24) ** (1 / 3)
    text = (f"Synthetic validation cell, not MD\n\n4 atoms\n1 atom types\n\n0 {edge:.17g} xlo xhi\n0 {edge:.17g} ylo yhi\n0 {edge:.17g} zlo zhi\n\n"
            "Masses\n\n1 12.011\n\nAtoms # full\n\n")
    text += "\n".join(f"{i} {i} 1 0 {edge*x:.17g} {edge*y:.17g} {edge*z:.17g} 0 0 0" for i, (x, y, z) in enumerate([(.2, .2, .2), (.7, .2, .2), (.2, .7, .2), (.2, .2, .7)], 1)) + "\n"
    (directory / name).write_text(text)


def runner(client, unresolved=()):
    manager = client.app.state.manager

    def submit(function, job_id):
        config, directory = manager._read(job_id)["config"], manager.directory(job_id)
        atomic_json(directory / "result.json", {"status": "completed", "warnings": ["Synthetic fixture; not a validated physical material."]})
        if config["atomistic_mode"] == "bulk":
            density = pcff_density(config)
            cell(directory, "final.data", density)
            atomic_json(directory / "bulk_complete.json", {"equilibrated": False, "properties": {"density": {"value": density, "unit": "g/cm^3"},
                        "bulk_modulus": {"value": 2.0, "unit": "GPa"}, "volume_expansion": {"value": 4e-4, "unit": "1/K"}}})
        elif config["atomistic_mode"] == "crosslink":
            values = cure_values(config)
            cell(directory, "final.data", values["density"])
            atomic_json(directory / "cure_complete.json", {"equilibrated": False, "conversion": {"epoxide": values["epoxide"], "target_reached": False},
                        "properties": {"density": {"value": values["density"], "unit": "g/cm^3"}, "bulk_modulus": {"value": 3.0, "unit": "GPa"},
                                       "volume_expansion": {"value": 3e-4, "unit": "1/K"}}})
        else:
            source = manager._read(config["source_job_id"])["config"]
            atomic_json(directory / "tg_complete.json", {"tg_kelvin": tg_of(source), "transition_resolved": source["seed"] not in unresolved,
                        "expansion_glass_per_kelvin": 2e-4, "expansion_melt_per_kelvin": 6e-4, "warnings": ["Synthetic Tg fixture."]})
        manager._update(job_id, status="completed", finished_at=now())
    manager.pool.submit = submit


def flow(client, template, factors, bindings, metric, responses=(), **extra):
    design = client.post("/api/research/doe/design", json={"factors": factors, "kind": "central_composite", "seed": 3})
    assert design.status_code == 201, design.text
    return client.post("/api/research/workflows", json={"design_id": design.json()["id"], "template": template, "bindings": bindings,
                                                        "metric": metric, "responses": list(responses), **extra})


def run(client, identifier, plan):
    preview = client.post(f"/api/research/workflows/{identifier}/autopilot/preview", json=plan)
    assert preview.status_code == 200, preview.text
    started = client.post(f"/api/research/workflows/{identifier}/autopilot", json={"plan": plan, "approved_budget": preview.json()["worst_case_runs"]})
    assert started.status_code == 200, started.text
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        record = client.get(f"/api/research/workflows/{identifier}").json()
        if record["autopilot"]["state"] != "running":
            return preview.json(), record
        time.sleep(0.05)
    raise AssertionError("autopilot did not finish")


PCFF_FACTORS = [{"name": "temp", "low": 280., "high": 340.}, {"name": "length", "low": 10., "high": 30.}]
PCFF_BINDINGS = {"temp": "temperature_kelvin", "length": "monomers"}
CURE_FACTORS = [{"name": "conv", "low": .6, "high": .9}, {"name": "cureT", "low": 400., "high": 500.}]
CURE_BINDINGS = {"conv": "target_conversion", "cureT": "cure_temperature_kelvin"}


def test_pcff_flow_confirms_at_the_rounded_chain_length(client):
    response = flow(client, PCFF, PCFF_FACTORS, PCFF_BINDINGS, "npt_density", ["final_frame_density"])
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["route"] == "pcff_bulk" and {row["config"]["monomers"] for row in record["rows"]} == {10, 20, 30}
    assert all(isinstance(row["config"]["monomers"], int) for row in record["rows"])
    runner(client)
    _, done = run(client, record["id"], {**PLAN, "confirm": True})
    assert done["autopilot"]["state"] == "completed", done["autopilot"]["events"]
    recommendation = done["optimizations"][-1]["recommendation"]
    assert recommendation["settings"] == pytest.approx({"temp": 319.0, "length": 23.7}, abs=1e-6)
    rounded = recommendation["rounded"]
    assert rounded["settings"] == pytest.approx({"temp": 319.0, "length": 24.0}, abs=1e-6)
    expected = pcff_density({"temperature_kelvin": 319.0, "monomers": 24})
    assert rounded["predicted"]["npt_density"] == pytest.approx(expected, abs=1e-9) != recommendation["predicted_response"]
    confirmation = done["confirmations"][0]
    assert confirmation["config"]["monomers"] == 24 and confirmation["settings"]["length"] == 24.0
    assert abs(confirmation["observation"]["prediction_error"]) < 1e-9
    assert any("rounded to integers" in w for w in done["warnings"])


def test_route_rules_for_factors_and_responses(client):
    odd = flow(client, PCFF, [PCFF_FACTORS[0], {"name": "length", "low": 10., "high": 25.}], PCFF_BINDINGS, "npt_density")
    assert odd.status_code == 422 and "정수" in odd.text
    packing = flow(client, PCFF, PCFF_FACTORS, {"temp": "temperature_kelvin", "length": "build_density"}, "npt_density")
    assert packing.status_code == 422 and "PCFF" in packing.text
    gaff2 = {"name": "Synthetic GAFF2", "route": "polymer_atomistic", "atomistic_mode": "bulk", "atomistic_forcefield": "gaff2", "cell_builder": "native"}
    bindings = {"temp": "temperature_kelvin", "length": "build_density"}
    factors = [PCFF_FACTORS[0], {"name": "length", "low": .6, "high": 1.0}]
    assert flow(client, gaff2, factors, bindings, "epoxide_conversion").status_code == 422
    assert flow(client, gaff2, factors, bindings, "tg_kelvin").status_code == 422
    assert flow(client, {**CURE, "atomistic_mode": "build"}, CURE_FACTORS, CURE_BINDINGS, "npt_density").status_code == 422
    assert client.get("/api/research/workflows").json()["records"] == []
    routes = client.get("/api/research/workflows/capabilities").json()["routes"]
    assert set(routes) == {"gaff2_bulk", "pcff_bulk", "crosslink", "tg_scan"} and "epoxide_conversion" in routes["crosslink"]["responses"]


def test_crosslink_flow_then_tg_follow_up_with_chained_confirmation(client):
    response = flow(client, CURE, CURE_FACTORS, CURE_BINDINGS, "epoxide_conversion", ["npt_density"])
    assert response.status_code == 201, response.text
    parent = response.json()
    assert parent["route"] == "crosslink" and any("not cure kinetics" in w for w in parent["warnings"])
    unresolved = parent["rows"][2]["config"]["seed"]
    runner(client, unresolved={unresolved})
    _, done = run(client, parent["id"], {**PLAN, "confirm": False, "response": "npt_density"})
    assert done["autopilot"]["state"] == "completed", done["autopilot"]["events"]
    observation = done["rows"][0]["observation"]
    assert observation["result_file"] == "cure_complete.json"
    assert observation["values"]["epoxide_conversion"] == pytest.approx(cure_values(done["rows"][0]["config"])["epoxide"], rel=1e-12)
    assert any("before the target" in w for w in observation["warnings"])
    created = client.post(f"/api/research/workflows/{parent['id']}/tg-scan", json={"tg_high_kelvin": 600, "tg_low_kelvin": 200, "budget": 30})
    assert created.status_code == 201, created.text
    tg = created.json()
    assert tg["route"] == "tg_scan" and tg["parent_workflow_id"] == parent["id"] and tg["source_route"] == "crosslink"
    assert [row["source_job_id"] for row in tg["rows"]] == [row["job_id"] for row in done["rows"]]
    assert all(row["config"]["source_job_id"] == row["source_job_id"] and row["config"]["tg_high_kelvin"] == 600 for row in tg["rows"])
    preview, finished = run(client, tg["id"], {**PLAN, "max_failed_rows": 1, "confirm": True})
    assert preview["worst_case_runs"] == 11 + 2
    assert finished["autopilot"]["state"] == "completed", finished["autopilot"]["events"]
    failed = next(row for row in finished["rows"] if row["config"]["seed"] == unresolved)
    assert failed["state"] == "analysis_failed" and "transition_resolved" in failed["error"]
    best = finished["optimizations"][-1]
    assert list(best["exclusions"]) == [str(failed["run_order"])] and "분석 실패" in best["exclusions"][str(failed["run_order"])]
    assert best["recommendation"]["settings"] == pytest.approx({"conv": .9, "cureT": 450.0}, abs=1e-6)
    confirmation = finished["confirmations"][0]
    assert confirmation["config"]["atomistic_mode"] == "crosslink" and confirmation["scan_config"]["source_job_id"] == confirmation["job_id"]
    assert confirmation["final_status"] == "completed" and abs(confirmation["observation"]["prediction_error"]) < 1e-9
    assert finished["submitted"] == 11 + 2


def test_tg_follow_up_needs_finished_cells(client):
    parent = flow(client, CURE, CURE_FACTORS, CURE_BINDINGS, "epoxide_conversion").json()
    refused = client.post(f"/api/research/workflows/{parent['id']}/tg-scan", json={})
    assert refused.status_code == 422 and "완료된 계산" in refused.text
    runner(client)
    run(client, parent["id"], {**PLAN, "optimize": False, "confirm": False})
    tg = client.post(f"/api/research/workflows/{parent['id']}/tg-scan", json={"budget": 11}).json()
    assert client.post(f"/api/research/workflows/{tg['id']}/tg-scan", json={}).status_code == 422
    preview = client.post(f"/api/research/workflows/{tg['id']}/autopilot/preview", json={**PLAN, "confirm": True}).json()
    assert preview["worst_case_runs"] == 13 and preview["remaining_budget"] == 11
    assert client.post(f"/api/research/workflows/{tg['id']}/autopilot", json={"plan": {**PLAN, "confirm": True}, "approved_budget": 13}).status_code == 422


def test_tg_flow_report_and_mcp_follow_up(client):
    from workbench import mcp_server

    class AppClient:
        origin = "http://127.0.0.1:8765"

        def request(self, method, path, payload=None, timeout=30):
            response = client.request(method, path, json=payload)
            if response.is_error:
                raise mcp_server.ToolError(f"HTTP {response.status_code}: {response.text}")
            return response.json()
    tools = mcp_server.PolymerLabTools(AppClient()).tools
    assert set(tools["create_research_workflow"]["schema"]["properties"]["bindings"]["additionalProperties"]["enum"]) == set(workflows.FIELDS)
    parent = flow(client, CURE, CURE_FACTORS, CURE_BINDINGS, "epoxide_conversion").json()
    runner(client)
    run(client, parent["id"], {**PLAN, "optimize": False, "confirm": False})
    arguments = {"workflow_id": parent["id"], "tg_high_kelvin": 600, "budget": 20}
    mcp_server._check(tools["create_tg_scan_workflow"]["schema"], arguments)
    tg = tools["create_tg_scan_workflow"]["handler"](arguments)
    assert tg["route"] == "tg_scan" and len(tg["rows"]) == 11
    _, finished = run(client, tg["id"], {**PLAN, "confirm": True})
    assert finished["autopilot"]["state"] == "completed", finished["autopilot"]["events"]
    report = client.post("/api/research/reports", json={"kind": "workflow", "id": tg["id"]})
    assert report.status_code == 201, report.text
    content = client.app.state.research_root / "reports" / report.json()["id"] / "content"
    assert "Tg 스캔 · 앞 흐름의 셀" in (content / "summary.md").read_text()
    assert finished["confirmations"][0]["scan_job_id"] in (content / "tables" / "confirmations.csv").read_text(encoding="utf-8-sig")


def test_out_of_range_binding_explains_in_korean_and_capabilities_list_bounds(client):
    swapped = flow(client, CURE, CURE_FACTORS, {"conv": "cure_temperature_kelvin", "cureT": "target_conversion"}, "epoxide_conversion")
    assert swapped.status_code == 422
    detail = swapped.json()["detail"]
    assert "허용 범위" in detail and "경화 온도" in detail and "300~600" in detail and "validation error" not in detail
    fields = client.get("/api/research/workflows/capabilities").json()["fields"]
    assert fields["target_conversion"]["bounds"] == [0.05, 0.99] and fields["temperature_kelvin"]["bounds"] == [100.0, 500.0]
    assert fields["monomers"]["bounds"] == [2.0, 200.0] and fields["monomers"]["integer"] is True


def test_route_level_limits_read_as_sentences(client):
    one_chain = flow(client, PCFF, [PCFF_FACTORS[0], {"name": "length", "low": 1., "high": 3.}], {"temp": "temperature_kelvin", "length": "chains"}, "npt_density")
    assert one_chain.status_code == 422
    detail = one_chain.json()["detail"]
    assert "사슬 2개 이상" in detail and "{" not in detail
