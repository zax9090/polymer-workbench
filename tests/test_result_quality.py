"""Quality dimensions must not turn computational completion into validation."""
import json
from pathlib import Path
from types import SimpleNamespace

from workbench.result_quality import build_quality, summarize_workflow


def test_no_checklist_automatically_validates_science():
    checks = [{'key': key, 'status': 'passed', 'title': key, 'detail': 'Synthetic criterion only.'}
              for key in ('execution', 'convergence', 'independent_repeatability', 'reference_comparison', 'provenance', 'review')]
    result = build_quality(checks)
    assert result['conclusion'] == 'review_required'
    assert not result['scientifically_validated'] and not result['automatic_approval']
    assert not result['authorizes_new_calculations'] and not result['proposed_runs_additive']
    checks[1]['status'] = 'failed'
    result = build_quality(checks)
    assert result['conclusion'] == 'hold'
    assert 'check_convergence' in {item['code'] for item in result['next_checks']}


def synthetic_flow(tmp_path, flag=False):
    jid = 'a' * 32
    folder = tmp_path / jid
    folder.mkdir()
    (folder / 'bulk_complete.json').write_text(json.dumps({'equilibrated': flag}))
    record = {'id': 'b' * 32, 'metric': 'final_frame_density', 'rows': [
        {'job_id': jid, 'attempts': [jid], 'state': 'observed', 'observation': {'verified': False}}], 'confirmations': []}
    jobs = SimpleNamespace(get=lambda _: {'status': 'completed', 'config': {'atomistic_mode': 'bulk'}}, directory=lambda _: folder)
    manager = SimpleNamespace(jobs=jobs, _validate_observation=lambda record, row: row['observation'])
    return record, manager, folder


def test_geometry_only_observation_still_reads_actual_nonconvergence(tmp_path):
    record, manager, _ = synthetic_flow(tmp_path)
    result = summarize_workflow(record, manager)
    checks = {check['key']: check for check in result['checks']}
    assert checks['execution']['status'] == 'passed'
    assert checks['convergence']['status'] == 'failed'
    assert checks['independent_repeatability']['status'] == 'unknown'
    assert checks['reference_comparison']['status'] == 'unknown'
    assert checks['review']['status'] == 'pending'
    assert result['conclusion'] == 'hold' and record['rows'][0]['observation']['verified'] is False


def test_unknown_convergence_and_tampered_lineage_do_not_pass(tmp_path):
    record, manager, folder = synthetic_flow(tmp_path, 'false')
    def changed(*_):
        raise ValueError('원본이 변경됐습니다.')
    manager._validate_observation = changed
    result = summarize_workflow(record, manager)
    checks = {check['key']: check for check in result['checks']}
    assert checks['convergence']['status'] == 'unknown'
    assert checks['provenance']['status'] == 'failed'
    (folder / 'bulk_complete.json').write_text('[]')
    assert summarize_workflow(record, manager)['conclusion'] == 'hold'
