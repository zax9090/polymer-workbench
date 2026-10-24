"""Evidence gates distinguish execution from scientific validation; never approve a result."""
from __future__ import annotations

from datetime import datetime, timezone
import json

STATUS = {'passed', 'pending', 'failed', 'unknown'}
NEXT = {
    'execution': ('complete_runs', '완료하지 못한 계산과 로그를 먼저 확인하세요. 같은 설정 재시도는 기존 계획의 남은 예산 안에서만 수행하세요.'),
    'convergence': ('check_convergence', '미수렴/미기록 계산의 원시 시계열과 평형화 조건을 확인하고, 충분한 유지 시간·셀 크기의 별도 계산 예산을 정하세요.'),
    'independent_repeatability': ('plan_independent_replicates', '같은 재료·온도·압력·방법에서 독립적으로 준비한 셀을 반복 계산하고, 허용 산포와 반복 수를 먼저 정하세요. DOE의 서로 다른 조건은 합쳐 반복으로 세지 않습니다.'),
    'reference_comparison': ('obtain_comparable_reference', '동일 재료·온도·압력의 실험/문헌 밀도와 출처·불확실성을 등록하고 사람이 검토한 뒤 허용 차이와 비교하세요.'),
    'review': ('human_review', '실행·수렴·반복·대조 근거를 사람이 검토하세요. 이 기능은 측정값의 검토 상태를 바꾸지 않습니다.'),
    'provenance': ('restore_sources', '누락되거나 변경된 원본과 설정·분석 해시를 확인하세요. 바뀐 원본으로 과거 값을 재구성하지 않습니다.'),
}


def build_quality(checks, next_checks=None):
    """Pass means one stated criterion is met, never global scientific validity."""
    if any(check.get('status') not in STATUS for check in checks):
        raise ValueError('지원하지 않는 품질 근거 상태입니다.')
    holds = [{'code': check.get('code', check['key']), 'key': check['key'], 'message': check['detail']}
             for check in checks if check['status'] != 'passed']
    substantive = [check for check in checks if check['key'] != 'review']
    conclusion = 'review_required' if substantive and all(c['status'] == 'passed' for c in substantive) else 'hold'
    suggestions = list(next_checks or [])
    codes = {item['code'] for item in suggestions}
    for check in checks:
        if check['status'] == 'passed' or check['key'] not in NEXT:
            continue
        code, message = NEXT[check['key']]
        if code not in codes:
            suggestions.append({'code': code, 'message': message, 'reason': check['detail']})
            codes.add(code)
    return {'schema_version': 1, 'conclusion': conclusion,
            'message': '결론 보류 · 부족한 근거를 먼저 확인하세요.' if conclusion == 'hold' else '검토 자료 준비 · 사람이 최종 판단해야 합니다.',
            'checks': checks, 'hold_reasons': holds, 'next_checks': suggestions,
            'automatic_approval': False, 'scientifically_validated': False,
            'authorizes_new_calculations': False, 'proposed_runs_additive': False,
            'generated_at': datetime.now(timezone.utc).isoformat()}


def summarize_workflow(record, workflows):
    """Read actual source convergence and original observation lineage, including geometry-only flows."""
    jobs = workflows.jobs
    references = [(row.get('job_id') if row.get('attempts') else None, row) for row in record['rows']]
    for confirmation in record.get('confirmations', []):
        references.append((confirmation.get('job_id'), None))
        if confirmation.get('scan_job_id'):
            references.append((confirmation['scan_job_id'], None))
    execution, convergence, provenance = [], [], []
    for identifier, row in references:
        if identifier is None:
            execution.append({'job_id': None, 'status': 'not_submitted'})
            continue
        try:
            job = jobs.get(identifier)
        except (KeyError, OSError, ValueError):
            execution.append({'job_id': identifier, 'status': 'missing'})
            provenance.append({'job_id': identifier, 'valid': False, 'reason': '작업 기록 없음'})
            continue
        execution.append({'job_id': identifier, 'status': job['status']})
        if job['status'] != 'completed':
            continue
        directory = jobs.directory(identifier)
        mode = job['config'].get('atomistic_mode')
        name = {'bulk': 'bulk_complete.json', 'crosslink': 'cure_complete.json', 'tg_scan': 'tg_complete.json'}.get(mode)
        result = None
        try:
            path = directory / name if name else None
            if path and not directory.is_symlink() and not path.is_symlink() and path.is_file() and path.stat().st_size <= 10 * 1024 * 1024:
                result = json.loads(path.read_text())
        except (ValueError, OSError):
            pass
        if not isinstance(result, dict):
            result = None
        if mode == 'bulk':
            flag = (result or {}).get('equilibrated')
            known = flag if type(flag) is bool else None
            convergence.append({'job_id': identifier, 'criterion': 'reported_equilibration', 'met': known})
        elif mode == 'tg_scan':
            convergence.append({'job_id': identifier, 'criterion': 'resolved_transition',
                                'met': (result or {}).get('transition_resolved') if type((result or {}).get('transition_resolved')) is bool else None})
        else:
            convergence.append({'job_id': identifier, 'criterion': 'equilibration_not_assessed', 'met': None})
        if row and row.get('observation'):
            try:
                workflows._validate_observation(record, row)
                provenance.append({'job_id': identifier, 'valid': True})
            except (ValueError, KeyError, OSError, TypeError, AttributeError) as exc:
                provenance.append({'job_id': identifier, 'valid': False, 'reason': str(exc)})
    completed = sum(item['status'] == 'completed' for item in execution)
    execution_status = 'passed' if execution and completed == len(execution) else ('failed' if any(x['status'] in {'failed', 'blocked', 'cancelled', 'missing'} for x in execution) else 'pending')
    cstatus = 'failed' if any(x['met'] is False for x in convergence) else ('passed' if len(convergence) == len(execution) and convergence and all(x['met'] is True for x in convergence) else 'unknown')
    pstatus = 'failed' if any(not x['valid'] for x in provenance) else ('passed' if len(provenance) == len(record['rows']) and provenance else 'pending')
    checks = [
        {'key': 'execution', 'status': execution_status, 'title': '실행 완료', 'detail': f'{completed}/{len(execution)}개 계산 완료. 완료는 과학적 타당성 판정이 아닙니다.', 'evidence': execution},
        {'key': 'convergence', 'status': cstatus, 'title': '수렴·전이 근거', 'detail': f"기록 기준 충족 {sum(x['met'] is True for x in convergence)}개, 미충족 {sum(x['met'] is False for x in convergence)}개, 나머지는 미기록/미평가입니다. 기준 충족만으로 평형이 입증되지는 않습니다.", 'evidence': convergence},
        {'key': 'independent_repeatability', 'status': 'unknown', 'title': '독립 반복 재현성', 'detail': 'DOE 행과 확인 계산은 조건이 다를 수 있어 반복으로 합산하지 않습니다. 같은 조건의 대표 밀도 검토에서 따로 비교하세요.', 'evidence': {'assessed': False}},
        {'key': 'reference_comparison', 'status': 'unknown', 'title': '실험·문헌 대조', 'detail': '이 흐름에서는 일치하는 외부 참조값과의 대조를 평가하지 않았습니다.', 'evidence': {'assessed': False}},
        {'key': 'provenance', 'status': pstatus, 'title': '출처·원본 일치', 'detail': '저장된 행 관측의 설정·분석·원본 해시를 다시 확인한 결과입니다. 확인 계산의 원본 재검증은 대표 검토에서 별도로 수행하세요.', 'evidence': provenance},
        {'key': 'review', 'status': 'pending', 'title': '사람 검토', 'detail': '계산 관측은 검토 전 시뮬레이션입니다. 이 요약은 검토 완료를 승인하지 않습니다.', 'evidence': {'origin': 'simulation', 'verified': False}},
    ]
    result = build_quality(checks)
    result.update(workflow_id=record['id'], scientific_property_validation=False)
    return result
