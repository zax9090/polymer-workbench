"""One fixed-density evidence path; all data and references here are synthetic fixtures."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

import numpy as np
import pytest
from scipy import stats
from fastapi.testclient import TestClient

from workbench.density_studies import DensityStudyManager
from workbench.jobs import atomic_json
from workbench.server import create_app
from workbench.density_api import router

TEMPLATE = {'name': 'Synthetic density study', 'route': 'polymer_atomistic', 'atomistic_mode': 'bulk',
            'atomistic_forcefield': 'gaff2', 'cell_builder': 'native', 'repeat_smiles': '[*]CC[*]',
            'chains': 2, 'monomers': 18, 'protocol_scale': .001, 'seed': 81}
BASE = '/api/research/density-studies'


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'jobs')
    with TestClient(app) as client:
        if not hasattr(app.state, 'density_studies'):
            app.state.density_studies = DensityStudyManager(app.state.research_root, app.state.manager, app.state.research_store)
        yield client


def create(client, **changes):
    response = client.post(BASE, json={'name': 'Synthetic density evidence', 'template': TEMPLATE,
        'max_relative_sd': .02, 'absolute_tolerance_g_cm3': .03, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def job(client, study, density=1., seed=82, equilibrated=True, status='completed', **changes):
    config = {**study['template'], 'name': f'Synthetic replicate {seed}', 'seed': seed, **changes}
    identifier = uuid.uuid4().hex
    folder = client.app.state.manager.root / identifier
    folder.mkdir()
    atomic_json(folder / 'job.json', {'id': identifier, 'name': config['name'], 'config': config, 'status': status,
        'created_at': '2026-10-24T00:00:00+00:00', 'finished_at': '2026-10-24T00:00:01+00:00' if status == 'completed' else None})
    atomic_json(folder / 'config.json', config)
    if status == 'completed':
        complete(folder, config, density, equilibrated)
    return identifier, folder


def complete(folder, config, density, equilibrated=True):
    metadata = {'force_field': 'GAFF2_mod', 'charge_method': 'Gasteiger', 'ensemble': 'NPT', 'protocol': 'synthetic fixture only',
        'temperature_kelvin': config['temperature_kelvin'], 'pressure_atm': 1., 'chains': config['chains'],
        'protocol_scale': config['protocol_scale'], 'time_step_fs': 1., 'stage_steps': {'sample': 10}, 'atoms': 220,
        'equilibrated': equilibrated, 'properties': {'density': {'value': density, 'unit': 'g/cm^3'}},
        'convergence_warnings': [] if equilibrated is True else ['Synthetic unresolved convergence; not physical evidence.']}
    atomic_json(folder / 'bulk_complete.json', metadata)
    atomic_json(folder / 'result.json', {'status': 'completed', 'warnings': ['Synthetic fixture; not a validated physical material.'],
        'metrics': {'equilibrated': equilibrated}})
    (folder / 'eq3_last.data').write_text(f'Synthetic independent cell seed={config["seed"]}, not MD\n')


def attach(client, study, ids):
    response = client.post(BASE + '/' + study['id'] + '/attach', json={'job_ids': ids})
    assert response.status_code == 200, response.text
    return response.json()


def assess(client, study):
    response = client.post(BASE + '/' + study['id'] + '/assess')
    assert response.status_code == 200, response.text
    return response.json()


def reference(client, study, *, verified=True, conditions=None, material=None, uncertainty=.005):
    row = client.app.state.research_store.add_measurement({'property': 'density', 'value': 1.005, 'unit': 'g/cm^3',
        'material': material or {'repeat_smiles': '[*]CC[*]'}, 'conditions': conditions if conditions is not None else study['conditions'],
        'method': 'Synthetic reference fixture, not a published measurement', 'source': 'Synthetic validation only',
        'origin': 'literature', 'verified': verified})
    linked = client.post(BASE + '/' + study['id'] + '/reference', json={'measurement_id': row['id'], 'standard_uncertainty': uncertainty})
    assert linked.status_code == 200, linked.text
    return row


def statuses(assessment):
    return {check['key']: check['status'] for check in assessment['quality']['checks']}


def test_prepare_is_nonexecuting_and_replicate_statistics_match_numpy_scipy(client):
    study = create(client)
    assert client.get('/api/jobs').json() == []
    assert len(study['planned_configs']) == 3
    assert len({config['seed'] for config in study['planned_configs']}) == 3
    assert study['automatic_execution'] is False
    values = [.99, 1., 1.01]
    ids = [job(client, study, value, seed=82 + i)[0] for i, value in enumerate(values)]
    attach(client, study, ids)
    ref = reference(client, study)
    original_ref = deepcopy(client.app.state.research_store.list_measurements())
    result = assess(client, study)
    summary = result['statistics']
    expected_mean = np.mean(values)
    sd = np.std(values, ddof=1)
    se = sd / np.sqrt(3)
    np.testing.assert_allclose(summary['confidence_interval'], expected_mean + stats.t.ppf(.975, 2) * se * np.array([-1, 1]), rtol=1e-10)
    assert summary['mean'] == pytest.approx(expected_mean)
    assert summary['sample_sd'] == pytest.approx(sd)
    assert summary['standard_error'] == pytest.approx(se)
    comparison = summary['reference_comparison']
    combined = np.sqrt(se ** 2 + .005 ** 2)
    assert comparison['combined_standard_uncertainty'] == pytest.approx(combined)
    assert comparison['standardized_difference'] == pytest.approx((expected_mean - ref['value']) / combined)
    assert statuses(result) == {'execution': 'passed', 'provenance': 'passed', 'convergence': 'passed',
        'independent_repeatability': 'passed', 'reference_comparison': 'passed', 'review': 'pending'}
    assert result['quality']['conclusion'] == 'review_required'
    assert result['verified'] is False and result['quality']['automatic_approval'] is False
    assert client.app.state.research_store.list_measurements() == original_ref
    stored = client.get(BASE + '/' + study['id']).json()
    assert stored['latest_assessment']['id'] == result['id']
    download = client.get(BASE + f"/{study['id']}/assessments/{result['id']}/download")
    assert download.status_code == 200 and download.json() == result
    raw = download.content
    second = assess(client, study)
    assert second['id'] != result['id']
    assert client.get(BASE + f"/{study['id']}/assessments/{result['id']}/download").content == raw
    json.dumps(result, allow_nan=False)


def test_success_and_low_scatter_do_not_override_nonconvergence_or_missing_evidence(client):
    study = create(client, max_relative_sd=None, absolute_tolerance_g_cm3=None)
    ids = [job(client, study, 1., seed=90 + i, equilibrated=False)[0] for i in range(3)]
    attach(client, study, ids)
    reference(client, study, uncertainty=None)
    result = assess(client, study)
    assert result['statistics']['n'] == 3 and result['statistics']['sample_sd'] == 0
    assert result['statistics']['reference_comparison']['combined_standard_uncertainty'] is None
    assert statuses(result)['execution'] == 'passed'
    assert statuses(result)['convergence'] == 'failed'
    assert statuses(result)['independent_repeatability'] == 'pending'
    assert statuses(result)['reference_comparison'] == 'pending'
    assert result['quality']['conclusion'] == 'hold'
    assert any(item['code'] == 'convergence_plan' for item in result['quality']['next_checks'])
    assert client.get('/api/jobs').json() and len(client.get('/api/jobs').json()) == 3


@pytest.mark.parametrize('change', [{'temperature_kelvin': 310.}, {'build_density': .9}, {'protocol_scale': .1}])
def test_cannot_pool_different_conditions(client, change):
    study = create(client)
    identifier, _ = job(client, study, **change)
    response = client.post(BASE + '/' + study['id'] + '/attach', json={'job_ids': [identifier]})
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'condition_mismatch'
    assert client.get(BASE + '/' + study['id']).json()['job_ids'] == []


def test_duplicate_jobs_seeds_structures_and_runtime_protocols_are_not_independent_replicates(client):
    study = create(client)
    first, f = job(client, study, seed=101)
    second, s = job(client, study, seed=101)
    response = client.post(BASE + '/' + study['id'] + '/attach', json={'job_ids': [first, second]})
    assert response.status_code == 422 and response.json()['detail']['code'] == 'duplicate_seed'
    attach(client, study, [first])
    assert client.post(BASE + '/' + study['id'] + '/attach', json={'job_ids': [first]}).json()['detail']['code'] == 'duplicate_job'
    second, s = job(client, study, seed=102)
    (s / 'eq3_last.data').write_bytes((f / 'eq3_last.data').read_bytes())
    attach(client, study, [second])
    result = assess(client, study)
    assert result['statistics'] is None
    assert all(any(i['code'] == 'duplicate_structure' for i in row['issues']) for row in result['records'])
    other = create(client)
    jobs = [job(client, other, seed=110 + i) for i in range(2)]
    path = jobs[1][1] / 'bulk_complete.json'
    value = json.loads(path.read_text()); value['stage_steps'] = {'sample': 20}; atomic_json(path, value)
    attach(client, other, [entry[0] for entry in jobs])
    mixed = assess(client, other)
    assert mixed['statistics'] is None
    assert all(any(i['code'] == 'runtime_protocol_mismatch' for i in row['issues']) for row in mixed['records'])


def test_changed_sources_units_and_unknown_boolean_are_visible(client):
    study = create(client)
    jobs = [job(client, study, seed=120 + i) for i in range(3)]
    # Unknown convergence must remain unknown; the string 'false' is not True.
    unknown = json.loads((jobs[0][1] / 'bulk_complete.json').read_text())
    unknown['equilibrated'] = 'false'; atomic_json(jobs[0][1] / 'bulk_complete.json', unknown)
    attach(client, study, [entry[0] for entry in jobs])
    first = assess(client, study)
    assert first['records'][0]['equilibrated'] is None
    assert statuses(first)['convergence'] == 'unknown'
    changed = jobs[1][1] / 'bulk_complete.json'
    value = json.loads(changed.read_text()); value['properties']['density']['unit'] = 'kg/m^3'; atomic_json(changed, value)
    changed_result = assess(client, study)
    row = next(r for r in changed_result['records'] if r['job_id'] == jobs[1][0])
    assert row['eligible'] is False
    assert {'source_changed', 'density_unit'} <= {i['code'] for i in row['issues']}
    assert len(changed_result['excluded']) == 1
    assert changed_result['statistics']['n'] == 2
    assert statuses(changed_result)['provenance'] == 'failed'


@pytest.mark.parametrize('settings', [{'verified': False}, {'conditions': {'temperature_kelvin': 300.}},
                                    {'conditions': {'temperature_kelvin': 350., 'pressure_atm': 1.}},
                                    {'material': {'repeat_smiles': '[*]CCO[*]'}}])
def test_unreviewed_or_mismatched_references_do_not_support_comparison(client, settings):
    study = create(client)
    attach(client, study, [job(client, study, seed=130 + i)[0] for i in range(3)])
    reference(client, study, **settings)
    result = assess(client, study)
    assert not result['reference']['eligible']
    assert result['statistics']['reference_comparison'] is None
    assert statuses(result)['reference_comparison'] == 'pending'
    assert result['quality']['conclusion'] == 'hold'


def test_pending_job_can_finish_without_false_source_change_and_assessment_folders_reject_links(client, tmp_path):
    study = create(client)
    identifier, folder = job(client, study, seed=141, status='queued')
    attach(client, study, [identifier])
    before = assess(client, study)
    assert before['statistics'] is None
    assert statuses(before)['execution'] == 'pending'
    record = json.loads((folder / 'job.json').read_text()); record['status'] = 'completed'; atomic_json(folder / 'job.json', record)
    complete(folder, record['config'], 1.)
    finished = assess(client, study)
    assert finished['statistics']['n'] == 1 and not finished['records'][0]['issues']
    assert finished['statistics']['sample_sd'] is None
    manager = client.app.state.density_studies
    other = create(client)
    outside = tmp_path / 'outside'; outside.mkdir()
    (manager.root / other['id'] / 'assessments').symlink_to(outside, target_is_directory=True)
    response = client.post(BASE + '/' + other['id'] + '/assess')
    assert response.status_code == 422 and response.json()['detail']['code'] == 'study_symlink'
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('filename,value', [('job.json', []), ('config.json', []),
                                         ('bulk_complete.json', []), ('bulk_complete.json', {'properties': []})])
def test_corrupt_source_objects_are_excluded_without_server_error(client, filename, value):
    study = create(client)
    identifier, folder = job(client, study, seed=151)
    attach(client, study, [identifier])
    original = (folder / filename).read_bytes()
    try:
        atomic_json(folder / filename, value)
        result = assess(client, study)
        assert result['statistics'] is None and result['excluded']
        assert statuses(result)['provenance'] == 'failed'
        assert result['quality']['conclusion'] == 'hold'
    finally:
        (folder / filename).write_bytes(original)


@pytest.mark.parametrize('warnings', [7, 'not a warning list', ['warning', 7]])
def test_malformed_convergence_warnings_exclude_only_the_affected_source(client, warnings):
    study = create(client)
    malformed, folder = job(client, study, seed=155)
    valid, _ = job(client, study, seed=156)
    attach(client, study, [malformed, valid])
    path = folder / 'bulk_complete.json'
    record = json.loads(path.read_text())
    record['convergence_warnings'] = warnings
    atomic_json(path, record)
    result = assess(client, study)
    assert result['statistics']['n'] == 1
    assert result['excluded'][0]['job_id'] == malformed
    assert any(issue['code'] == 'source_warnings' for issue in result['excluded'][0]['reasons'])
    assert next(row for row in result['records'] if row['job_id'] == valid)['eligible']
    assert statuses(result)['provenance'] == 'failed'
    assert result['quality']['conclusion'] == 'hold'


@pytest.mark.parametrize('field,value', [('ensemble', 'NVT'), ('time_step_fs', None), ('stage_steps', {})])
def test_unknown_or_non_npt_runtime_protocol_cannot_pass(client, field, value):
    study = create(client)
    identifier, folder = job(client, study, seed=161)
    path = folder / 'bulk_complete.json'
    record = json.loads(path.read_text()); record[field] = value; atomic_json(path, record)
    attach(client, study, [identifier])
    result = assess(client, study)
    assert result['statistics'] is None
    assert any(i['code'] == 'metadata_protocol' for i in result['records'][0]['issues'])
    assert statuses(result)['provenance'] == 'failed'


def test_zero_spread_replicates_do_not_prove_precision(client):
    study = create(client)
    attach(client, study, [job(client, study, 1., seed=171 + i)[0] for i in range(3)])
    reference(client, study)
    result = assess(client, study)
    assert result['statistics']['sample_sd'] == 0.
    assert statuses(result)['execution'] == 'passed' and statuses(result)['convergence'] == 'passed'
    assert statuses(result)['independent_repeatability'] == 'pending'
    assert result['quality']['conclusion'] == 'hold'
    assert any(item['code'] == 'zero_spread' for item in result['quality']['hold_reasons'])
    assert any(item['code'] == 'review_zero_spread' for item in result['quality']['next_checks'])
