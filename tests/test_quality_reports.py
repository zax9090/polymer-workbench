"""Rust-backed evidence reports over explicitly synthetic records, never physical validation."""
import hashlib
import json
from pathlib import Path

from tests.test_density_studies import client, TEMPLATE, job, reference
from tests.test_workflows import create as create_workflow
from workbench import mcp_server


def tools_for(client):
    class AppClient:
        origin='http://127.0.0.1:8765'
        def request(self, method, path, payload=None, timeout=30):
            response=client.request(method,path,json=payload)
            if response.is_error:raise mcp_server.ToolError(response.text)
            return response.json()
    return mcp_server.PolymerLabTools(AppClient()).tools


def test_mcp_density_record_to_rust_summary_and_immutable_report(client):
    tools=tools_for(client)
    create=tools['create_density_study']
    body={'name':'Synthetic representative evidence','template':TEMPLATE,'max_relative_sd':.02,'absolute_tolerance_g_cm3':.03}
    mcp_server._check(create['schema'],body)
    study=create['handler'](body)
    assert client.get('/api/jobs').json()==[]
    ids=[job(client,study,density=value,seed=200+i)[0] for i,value in enumerate([.99,1.,1.01])]
    control=tools['control_density_study']['handler']
    control({'action':'attach','study_id':study['id'],'job_ids':ids})
    ref=reference(client,study)
    control({'action':'reference','study_id':study['id'],'measurement_id':ref['id'],'standard_uncertainty':.005})
    assessment=control({'action':'assess','study_id':study['id']})
    assert assessment['statistics']['mean']==1.
    assert assessment['quality']['conclusion']=='review_required'
    assert not assessment['quality']['scientifically_validated'] and not assessment['verified']
    assert len(assessment['kernel']['sha256'])==64
    manager=client.app.state.density_studies
    before=(manager.root/study['id']/'result.json').read_bytes()
    references=client.app.state.research_store.list_measurements()
    result=tools['create_research_report']['handler']({'kind':'density_study','id':study['id']})
    identifier=result['id']
    content=client.app.state.research_root/'reports'/identifier/'content'
    assert (content/'figures/density_replicates.svg').is_file()
    assert (content/'tables/quality.csv').is_file()
    assert (content/'tables/next_checks.csv').is_file()
    archived=json.loads((content/'inputs/density-assessment.json').read_text())
    assert archived==assessment
    manifest=json.loads((content/'manifest.json').read_text())
    for entry in manifest['files']:
        assert hashlib.sha256((content/entry['path']).read_bytes()).hexdigest()==entry['sha256']
    assert (manager.root/study['id']/'result.json').read_bytes()==before
    assert client.app.state.research_store.list_measurements()==references
    assert '검토' in (content/'summary.md').read_text()


def test_workflow_evidence_endpoint_never_promotes_completion_to_validation(client):
    workflow=create_workflow(client)
    manager=client.app.state.workflows
    path=manager.root/workflow['id']/'result.json'
    before=path.read_bytes()
    tools=tools_for(client)
    quality=tools['get_research_quality']['handler']({'workflow_id':workflow['id']})
    checks={c['key']:c for c in quality['checks']}
    assert checks['execution']['status']=='pending'
    assert checks['independent_repeatability']['status']=='unknown'
    assert not quality['scientifically_validated'] and quality['conclusion']=='hold'
    assert path.read_bytes()==before and client.get('/api/jobs').json()==[]


def test_reference_uncertainty_outside_kernel_range_is_a_typed_input_error(client):
    from tests.test_density_studies import create
    study=create(client)
    ref=reference(client,study)
    response=client.post('/api/research/density-studies/'+study['id']+'/reference',json={
        'measurement_id':ref['id'],'standard_uncertainty':1e20})
    assert response.status_code==422
    assert response.json()['detail']['code']=='reference_uncertainty_range'
