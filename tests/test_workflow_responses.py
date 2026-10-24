"""Several responses per workflow row: free-volume and NPT-sampling values, single fits and desirability; synthetic jobs only."""
import json

import numpy as np
import pytest

from tests.test_autopilot import PLAN, approve, client, modulus, runner, wait  # noqa: F401 (client is a fixture)
from tests.test_workflows import create

RESPONSES = ["npt_density", "bulk_modulus"]
GOALS = [{"name": "npt_density", "model": "quadratic", "goal": "maximize", "lower": 0.9, "target": 1.1},
         {"name": "bulk_modulus", "model": "interaction", "goal": "minimize", "target": 1.0, "upper": 2.6}]


def density(u, v):
    return 1.1 - .1 * (u - .25) ** 2 - .15 * (v + .4) ** 2


def brute_force():
    grid = np.linspace(-1, 1, 801)
    u, v = np.meshgrid(grid, grid)
    d1 = np.clip((density(u, v) - 0.9) / (1.1 - 0.9), 0, 1)
    d2 = np.clip((2.6 - modulus(u, v)) / (2.6 - 1.0), 0, 1)
    score = np.sqrt(d1 * d2)
    index = np.unravel_index(np.argmax(score), score.shape)
    return score[index], u[index], v[index]


def test_desirability_plan_collects_every_response_and_confirms_each(client):
    record = create(client, metric="final_frame_density", responses=RESPONSES)
    assert record["responses"] == ["final_frame_density", *RESPONSES]
    assert record["units"] == {"final_frame_density": "g/cm^3", "npt_density": "g/cm^3", "bulk_modulus": "GPa"}
    runner(client)
    _, response = approve(client, record["id"], {**PLAN, "goals": GOALS})
    assert response.status_code == 200, response.text
    done = wait(client, record["id"])
    assert done["autopilot"]["state"] == "completed", done["autopilot"]["events"]
    for row in done["rows"]:
        observation = row["observation"]
        u, v = (row["factors"]["temp"] - 310) / 30, (row["factors"]["packing"] - .8) / .2
        assert observation["values"]["npt_density"] == pytest.approx(density(u, v), rel=1e-12)
        assert observation["values"]["bulk_modulus"] == pytest.approx(modulus(u, v), rel=1e-12)
        assert observation["equilibrated"] is False and observation["verified"] is False and len(observation["result_sha256"]) == 64
        assert any("not judge this cell equilibrated" in w for w in observation["warnings"])
    best = done["optimizations"][-1]
    assert best["analysis"] == "doe_desirability" and best["metrics"] == RESPONSES
    score, u, v = brute_force()
    found = best["recommendation"]
    assert found["composite_desirability"] == pytest.approx(score, abs=2e-4) and found["composite_desirability"] >= score - 2e-4
    assert (found["settings"]["temp"] - 310) / 30 == pytest.approx(u, abs=0.02)
    assert (found["settings"]["packing"] - .8) / .2 == pytest.approx(v, abs=0.02)
    errors = done["confirmations"][0]["observation"]["prediction_errors"]
    assert set(errors) == set(RESPONSES) and all(abs(e) < 1e-8 for e in errors.values())
    assert done["confirmations"][0]["observation"]["prediction_error"] is None
    content = client.app.state.research_root / "reports" / done["autopilot"]["report_id"] / "content"
    assert (content / "figures" / "desirability.svg").is_file()
    header = (content / "tables" / "runs.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "npt_density (g/cm^3)" in header and "bulk_modulus (GPa)" in header
    confirmations = (content / "tables" / "confirmations.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(confirmations) == 1 + len(RESPONSES)


def test_single_response_choice_and_source_tampering(client):
    record = create(client, metric="final_frame_density", responses=RESPONSES)
    runner(client)
    _, response = approve(client, record["id"], {**PLAN, "optimize": False, "confirm": False, "report": False})
    assert response.status_code == 200, response.text
    assert wait(client, record["id"])["autopilot"]["state"] == "completed"
    url = f"/api/research/workflows/{record['id']}"
    assert client.post(url + "/optimize", json={"response": "volume_expansion"}).status_code == 422
    fitted = client.post(url + "/optimize", json={"model": "interaction", "objective": "minimize", "response": "bulk_modulus"})
    assert fitted.status_code == 200, fitted.text
    result = fitted.json()["optimizations"][-1]
    assert result["metric"] == "bulk_modulus" and result["unit"] == "GPa"
    assert result["recommendation"]["settings"] == pytest.approx({"temp": 280.0, "packing": 1.0}, abs=1e-6)
    assert result["recommendation"]["predicted_response"] == pytest.approx(modulus(-1, 1), abs=1e-9)
    bad_goals = [{**GOALS[0], "name": "volume_expansion"}]
    assert client.post(url + "/optimize-desirability", json={"goals": bad_goals}).status_code == 422
    assert client.post(url + "/autopilot/preview", json={**PLAN, "goals": bad_goals}).status_code == 422
    assert client.post(url + "/autopilot/preview", json={**PLAN, "response": "volume_expansion"}).status_code == 422
    first = record["rows"][0]["job_id"]
    path = client.app.state.manager.directory(first) / "bulk_complete.json"
    edited = json.loads(path.read_text())
    edited["properties"]["bulk_modulus"]["value"] += 0.5
    path.write_text(json.dumps(edited))
    denied = client.post(url + "/optimize-desirability", json={"goals": GOALS})
    assert denied.status_code == 422 and "변경" in denied.text


def test_unknown_response_is_rejected(client):
    from tests.test_workflows import FACTORS, TEMPLATE
    design = client.post("/api/research/doe/design", json={"factors": FACTORS, "kind": "central_composite", "seed": 7}).json()
    response = client.post("/api/research/workflows", json={"design_id": design["id"], "template": TEMPLATE, "metric": "final_frame_density",
                                                            "bindings": {"temp": "temperature_kelvin", "packing": "build_density"}, "responses": ["tg"]})
    assert response.status_code == 422


def test_response_names_agree_across_module_api_and_mcp():
    from typing import get_args
    from workbench import mcp_server, workflow_api, workflows
    assert list(workflows.METRICS) == mcp_server.WORKFLOW_METRICS == list(get_args(workflow_api.Metric)) == list(workflows.LABELS)
    assert list(workflows.FIELDS) == mcp_server.WORKFLOW_FIELDS
    assert set(workflows.FREE_VOLUME) | set(workflows.RESULT_PROPERTIES) | workflows.CURE_VALUES | set(workflows.TG_VALUES) == set(workflows.METRICS)
    assert all(set(spec["responses"]) <= set(workflows.METRICS) and set(spec.get("fields", [])) <= set(workflows.FIELDS) for spec in workflows.ROUTES.values())
