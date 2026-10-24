"""Target contracts reuse real Rust designs but never submit simulations."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from workbench import doe, preflight, workflows
from workbench.server import create_app
from workbench.target_api import router


PAYLOAD = {
    "name": "작은 목표 계획",
    "template": {"name": "탐색용 PE", "route": "polymer_atomistic", "atomistic_mode": "bulk",
        "atomistic_forcefield": "gaff2", "cell_builder": "native", "repeat_smiles": "[*]CC[*]",
        "chains": 2, "monomers": 18, "protocol_scale": .001, "seed": 51},
    "variables": [{"name": "temp", "field": "temperature_kelvin", "low": 300., "high": 320., "unit": "K"},
                  {"name": "packing", "field": "build_density", "low": .6, "high": .65, "unit": "g/cm^3"}],
    "targets": [{"name": "final_frame_density", "unit": "g/cm^3", "model": "linear",
                 "goal": "maximize", "lower": .5, "target": .8}],
    "design_kind": "factorial", "center_points": 0, "max_calculations": 5,
    "max_attempts": 1, "confirm": True, "report": True, "probe": 0., "spacing": 1.,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Keep chemistry and numerical design real; replace only engine availability and subprocess dispatch.
    monkeypatch.setattr(preflight, "_engines", lambda cfg: [{"engine": "test availability", "available": True, "note": None}])
    monkeypatch.setattr(workflows, "preflight", lambda template: [])
    app = create_app(tmp_path / "jobs")
    # The root integration adds this router to server.py after independent implementation review.
    if not any(getattr(route, "path", "") == "/api/research/targets/preview" for route in app.routes):
        app.include_router(router)
    with TestClient(app) as client:
        def compute(operation, payload, **kwargs):
            assert operation == "doe_design"
            return doe.design(**payload)
        monkeypatch.setattr(app.state.execution, "compute", compute)
        monkeypatch.setattr(app.state.manager, "create", lambda *a, **kw: pytest.fail("Preparing a target must never submit a simulation"))
        yield client


def assert_unprepared(client):
    root = client.app.state.research_root
    assert not (root / "doe").exists()
    assert client.app.state.workflows.list() == []
    assert client.app.state.manager.list() == []


def test_target_preview_checks_every_run_units_budget_and_no_physical_screening(client):
    response = client.post('/api/research/targets/preview', json=PAYLOAD)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['ready'] and preview['mode'] == 'exploratory'
    assert len(preview['rows']) == 4 and all(row['valid'] for row in preview['rows'])
    assert preview['cost']['worst_case_runs'] == 5
    assert preview['plan']['max_attempts'] == 1 and preview['plan']['goals'][0]['name'] == 'final_frame_density'
    assert preview['screening']['predictive_screening_available'] is False
    assert preview['screening']['discarded'] == 0
    assert preview['automatic_execution'] is False
    assert preview['estimability'] == [{'model': 'linear', 'terms': 3, 'df_residual': 1,
        'replicate_groups': 0, 'pure_error_df': 0, 'check': 'rank_only_not_fit_quality'}]
    assert any('uncertainty estimates will be fragile' in warning for warning in preview['warnings'])
    assert_unprepared(client)


def test_prepared_contract_and_plan_are_hashed_saved_and_inactive(client):
    response = client.post('/api/research/targets/prepare', json=PAYLOAD)
    assert response.status_code == 201, response.text
    prepared = response.json()
    record = prepared['workflow']
    assert record['state'] == 'planned' and record['submitted'] == 0 and record['busy'] is False
    assert not record.get('autopilot')
    assert record['target_contract'] == prepared['contract']
    assert record['target_contract_sha256'] == workflows.digest(prepared['contract']) == prepared['contract_sha256']
    assert prepared['contract']['design_id'] == prepared['design_id'] == record['design_id']
    assert prepared['contract']['plan'] == prepared['plan']
    assert prepared['cost']['worst_case_runs'] == 5
    assert prepared['next_action']['requires_approved_budget'] is True
    assert client.app.state.manager.list() == []
    assert client.get('/api/research/measurements').json() == []


@pytest.mark.parametrize('change,code', [
    (lambda p: p['variables'][0].update(unit='C'), 'variable_unit'),
    (lambda p: p['targets'][0].update(unit='GPa'), 'target_unit'),
    (lambda p: p['targets'][0].update(name='tg_kelvin', unit='K'), 'unsupported_target'),
    (lambda p: p.update(max_calculations=4), 'budget'),
    (lambda p: p.update(mode='validated_property'), 'unsupported_mode'),
    (lambda p: p['targets'][0].update(upper=.9), 'target_bounds'),
])
def test_blocked_contract_is_explicit_and_cannot_create_records(client, change, code):
    payload = deepcopy(PAYLOAD)
    change(payload)
    result = client.post('/api/research/targets/preview', json=payload)
    assert result.status_code == 200, result.text
    assert result.json()['ready'] is False
    assert code in {row['code'] for row in result.json()['blockers']}
    prepared = client.post('/api/research/targets/prepare', json=payload)
    assert prepared.status_code == 422
    assert_unprepared(client)


def test_nonestimable_quadratic_factorial_and_missing_engines_are_not_prepared(client, monkeypatch):
    quadratic = deepcopy(PAYLOAD)
    quadratic['targets'][0]['model'] = 'quadratic'
    quadratic.update(center_points=3, max_calculations=8)
    result = client.post('/api/research/targets/preview', json=quadratic).json()
    assert not result['ready'] and any(item['code'] == 'design_or_model' for item in result['blockers'])
    monkeypatch.setattr(preflight, '_engines', lambda cfg: [{'engine': 'LAMMPS', 'available': False, 'note': None}])
    result = client.post('/api/research/targets/preview', json=PAYLOAD).json()
    assert not result['ready'] and len(result['rows']) == 4
    assert all(not row['valid'] for row in result['rows'])
    assert any(item['code'] == 'missing_engines' for item in result['blockers'])
    assert_unprepared(client)


def test_schema_rejects_mixture_constraints_nonfinite_values_and_bad_goal_bounds(client):
    payload = deepcopy(PAYLOAD)
    payload['components'] = [{'name': 'A', 'low': 0., 'high': 1.}]
    assert client.post('/api/research/targets/preview', json=payload).status_code == 422
    payload = deepcopy(PAYLOAD)
    payload['targets'][0]['target'] = None
    assert client.post('/api/research/targets/preview', json=payload).status_code == 422
    payload['targets'][0].update(target=.4, lower=.5)
    result = client.post('/api/research/targets/preview', json=payload).json()
    assert not result['ready'] and any(item['code'] == 'targets' for item in result['blockers'])
    assert_unprepared(client)


def test_mcp_target_to_rust_optimization_confirmation_and_reproduction_report(client, monkeypatch):
    import hashlib
    import json

    from workbench import mcp_server
    from tests.test_autopilot import runner, wait

    class AppClient:
        origin = 'http://127.0.0.1:8765'

        def request(self, method, path, payload=None, timeout=30):
            response = client.request(method, path, json=payload)
            if response.is_error:
                raise mcp_server.ToolError(f'HTTP {response.status_code}: {response.text}')
            return response.json()

    # Restore the real API and isolated Rust computation path. Only the MD executor
    # is replaced, with the independently specified analytic density fixture.
    manager, execution = client.app.state.manager, client.app.state.execution
    monkeypatch.setattr(manager, 'create', type(manager).create.__get__(manager))
    monkeypatch.setattr(execution, 'compute', type(execution).compute.__get__(execution))
    client.app.state.autopilot.poll = .01
    attempts = runner(client)
    tools = mcp_server.PolymerLabTools(AppClient()).tools
    target_tool = tools['prepare_target_workflow']
    payload = deepcopy(PAYLOAD)
    payload['variables'][0].update(low=280., high=340.)
    payload['variables'][1].update(low=.6, high=1.)
    payload['targets'][0].update(model='quadratic', lower=.5, target=1.2)
    payload.update(design_kind='central_composite', center_points=3, max_calculations=12)
    arguments = {'action': 'preview', 'spec': payload}
    mcp_server._check(target_tool['schema'], arguments)
    preview = target_tool['handler'](arguments)
    assert preview['ready'] and preview['cost']['worst_case_runs'] == 12
    assert client.app.state.manager.list() == []
    prepared = target_tool['handler']({'action': 'prepare', 'spec': payload})
    assert prepared['schema_version'] == 1
    identifier = prepared['workflow']['id']
    pilot_tool = tools['run_research_workflow_plan']
    pilot_tool['handler']({'action': 'start', 'workflow_id': identifier,
                          'plan': prepared['plan'], 'approved_budget': 12})
    done = wait(client, identifier, timeout=90)
    assert done['autopilot']['state'] == 'completed', done['autopilot']
    assert done['submitted'] == 12 and sum(attempts.values()) == 12
    best = done['optimizations'][-1]['recommendation']
    assert best['settings'] == pytest.approx({'temp': 317.5, 'packing': .72}, abs=1e-4)
    assert best['predicted']['final_frame_density'] == pytest.approx(1.1, abs=1e-10)
    assert best['validated'] is False
    assert all(row['observation']['verified'] is False for row in done['rows'])
    confirmation = done['confirmations'][0]['observation']
    assert confirmation['verified'] is False and abs(confirmation['prediction_errors']['final_frame_density']) < .02
    report_id = done['autopilot']['report_id']
    report = client.get('/api/research/reports/' + report_id).json()
    content = client.app.state.research_root / 'reports' / report_id / 'content'
    manifest = json.loads((content / 'manifest.json').read_text())
    paths = {entry['path'] for entry in manifest['files']}
    assert {'inputs/workflow.json', 'inputs/target-contract.json', 'inputs/plan.json',
            'inputs/design.json', 'inputs/design.csv', 'inputs/job-configs.json'} <= paths
    for entry in manifest['files']:
        assert hashlib.sha256((content / entry['path']).read_bytes()).hexdigest() == entry['sha256']
    assert hashlib.sha256((content / 'manifest.json').read_bytes()).hexdigest() == report['manifest_sha256']
    saved_contract = json.loads((content / 'inputs/target-contract.json').read_text())
    assert workflows.digest(saved_contract) == done['target_contract_sha256']
    assert client.get('/api/research/measurements').json() == []


def test_runtime_engine_gate_blocks_a_download_later_requirement(client, monkeypatch):
    # Generic preflight may advertise download_on_first_use; the actual workflow
    # runner requires its cached engine before it can start.
    monkeypatch.setattr(preflight, '_engines', lambda cfg: [
        {'engine': 'xtb', 'available': False, 'note': 'download_on_first_use'}])
    monkeypatch.setattr(workflows, 'preflight', lambda cfg: ['xtb'])
    payload = deepcopy(PAYLOAD)
    payload['template']['charge_method'] = 'xtb_cm5'
    response = client.post('/api/research/targets/preview', json=payload)
    assert response.status_code == 200
    assert response.json()['ready'] is False
    assert response.json()['missing_engines'] == ['xtb']
    assert response.json()['blockers'][0]['code'] == 'missing_engines'
    assert client.post('/api/research/targets/prepare', json=payload).status_code == 422
    assert_unprepared(client)
