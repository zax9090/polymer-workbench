"""Connected workflow provenance and analytic end-to-end checks using only temporary synthetic jobs."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from workbench import workflows
from workbench.jobs import atomic_json
from workbench.server import create_app

FACTORS=[{'name':'temp','low':280.,'high':340.},{'name':'packing','low':.6,'high':1.}]
TEMPLATE={'name':'Synthetic workflow test','route':'polymer_atomistic','atomistic_mode':'bulk','atomistic_forcefield':'gaff2','cell_builder':'native','protocol_scale':.001}

@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path/'jobs')) as client:yield client


def create(client,**changes):
    design=client.post('/api/research/doe/design',json={'factors':FACTORS,'kind':'central_composite','seed':7}).json()
    payload={'design_id':design['id'],'template':TEMPLATE,'bindings':{'temp':'temperature_kelvin','packing':'build_density'},'metric':'final_frame_density','spacing':1.,**changes}
    result=client.post('/api/research/workflows',json=payload)
    assert result.status_code==201,result.text
    return result.json()


def synthesize(client,record,row,*,changed=False):
    # Analytic response encoded in a synthetic carbon box, not a physical MD result.
    u=(row['factors']['temp']-310)/30;v=(row['factors']['packing']-.8)/.2
    density=1.1-.1*(u-.25)**2-.15*(v+.4)**2
    edge=(4*12.011/6.02214076e23/density*1e24)**(1/3)
    job_id=row['job_id'];directory=client.app.state.manager.root/job_id;directory.mkdir()
    config=dict(row['config'])
    if changed:config['charge_method']='xtb_cm5'
    atomic_json(directory/'job.json',{'id':job_id,'name':config['name'],'status':'completed','created_at':'2026-10-23T00:00:00+00:00','config':config})
    atomic_json(directory/'config.json',config)
    atomic_json(directory/'result.json',{'status':'completed','warnings':['Synthetic fixture; not a validated physical material.']})
    text=f'Synthetic validation cell, not MD\n\n4 atoms\n1 atom types\n\n0 {edge:.17g} xlo xhi\n0 {edge:.17g} ylo yhi\n0 {edge:.17g} zlo zhi\n\nMasses\n\n1 12.011\n\nAtoms # full\n\n'
    text+='\n'.join(f'{i} {i} 1 0 {edge*x:.17g} {edge*y:.17g} {edge*z:.17g} 0 0 0' for i,(x,y,z) in enumerate([(0.2,.2,.2),(.7,.2,.2),(.2,.7,.2),(.2,.2,.7)],1))+'\n'
    (directory/'eq3_last.data').write_text(text)
    result=client.post(f"/api/research/workflows/{record['id']}/attach",json={'run_order':row['run_order'],'job_id':job_id})
    return result,directory


def test_complete_real_analysis_optimization_and_backup(client,monkeypatch):
    record=create(client);identifier=record['id']
    for row in record['rows']:
        response,_=synthesize(client,record,row);assert response.status_code==200,response.text
    response=client.post(f'/api/research/workflows/{identifier}/analyze',json={'rows':list(range(1,12))})
    assert response.status_code==200,response.text
    analyzed=response.json()
    assert all(row['state']=='observed' for row in analyzed['rows']),[r.get('error') for r in analyzed['rows']]
    assert all('Synthetic fixture; not a validated physical material.' in row['observation']['warnings'] for row in analyzed['rows'])
    # Damaged observation values/units must not masquerade as the immutable analysis result.
    store=client.app.state.workflows
    original=store._read(identifier)
    import copy
    for key,bad in [('value',999.),('unit','kg/m^3')]:
        altered=copy.deepcopy(original);altered['rows'][0]['observation'][key]=bad;store._save(altered)
        assert client.post(f'/api/research/workflows/{identifier}/optimize',json={}).status_code==422
        store._save(copy.deepcopy(original))
    response=client.post(f'/api/research/workflows/{identifier}/optimize',json={})
    assert response.status_code==200,response.text
    result=response.json()['optimizations'][-1]
    assert result['recommendation']['settings']==pytest.approx({'temp':317.5,'packing':.72},abs=1e-4)
    assert result['recommendation']['predicted_response']==pytest.approx(1.1,abs=1e-9)
    assert result['unit']=='g/cm^3' and result['recommendation']['validated'] is False
    assert len(result['observations'])==11
    assert client.get('/api/research/measurements').json()==[]
    before=client.app.state.workflows._read(identifier)
    first=before['rows'][0]
    assert first['observation']['verified'] is False
    source=client.app.state.manager.directory(first['job_id'])/'eq3_last.data'
    source.write_text(source.read_text()+'\n')
    denied=client.post(f'/api/research/workflows/{identifier}/optimize',json={})
    assert denied.status_code==422 and '변경' in denied.text
    assert len(client.app.state.workflows._read(identifier)['optimizations'])==1
    from workbench.drive_backup import create_archive
    archive,report=create_archive(Path(__file__).parents[1],client.app.state.manager,client.app.state.research_store)
    with zipfile.ZipFile(archive) as z:
        names=z.namelist()
        assert f'research/workflows/{identifier}/result.json' in names
        assert any(f'research/workflows/{identifier}/revisions/' in name for name in names)
        assert any(f'research/workflows/{identifier}/optimizations/' in name for name in names)
    monkeypatch.setattr(workflows,'preflight',lambda config:[])
    monkeypatch.setattr(client.app.state.manager.pool,'submit',lambda *a,**k:None)
    # Explicit confirmation creates exactly one fresh independent run, never an approved measurement.
    request={'optimization_id':result['id']}
    a=client.post(f'/api/research/workflows/{identifier}/confirm',json=request)
    b=client.post(f'/api/research/workflows/{identifier}/confirm',json=request)
    assert a.status_code==b.status_code==200
    assert len(a.json()['confirmations'])==len(b.json()['confirmations'])==1
    confirmation=a.json()['confirmations'][0]
    assert confirmation['config']['seed'] not in [r['config']['seed'] for r in record['rows']]
    assert confirmation['config']['temperature_kelvin']==pytest.approx(317.5,abs=1e-4)


def test_submit_idempotency_retry_budget_and_restart(client,monkeypatch):
    record=create(client,budget=12);identifier=record['id'];manager=client.app.state.manager
    monkeypatch.setattr(workflows,'preflight',lambda config:[])
    monkeypatch.setattr(manager.pool,'submit',lambda *a,**k:None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:client.post(f'/api/research/workflows/{identifier}/submit',json={'rows':[1,2]}),range(2)))
    assert all(r.status_code==200 for r in results)
    saved=client.app.state.workflows.get(identifier)
    assert saved['submitted']==2 and len(manager.list())==2
    assert client.get('/api/execution').json()['pending_jobs']==2
    first=saved['rows'][0]['job_id'];manager._update(first,status='failed')
    retry=client.post(f'/api/research/workflows/{identifier}/submit',json={'rows':[1],'retry_failed':True}).json()
    assert retry['submitted']==3 and retry['rows'][0]['job_id']!=first
    assert retry['rows'][0]['attempts'][0]==first
    reserved=client.app.state.workflows._read(identifier);reserved.update(busy=True,state='analyzing');client.app.state.workflows._save(reserved)
    restarted=workflows.WorkflowManager(client.app.state.research_root,manager)
    assert restarted.get(identifier)['state']=='interrupted'
    assert len(manager.list())==3
    client.post(f'/api/research/workflows/{identifier}/cancel')
    assert all(job['status'] in {'failed','cancelled'} for job in manager.list())


@pytest.mark.parametrize('change',[{'template':{**TEMPLATE,'cell_builder':'radonpy'}},{'bindings':{'temp':'temperature_kelvin','packing':'temperature_kelvin'}},{'budget':6},{'template':{**TEMPLATE,'engine_input':'arbitrary'}},{'probe':-1.}])
def test_invalid_connection_creates_no_workflow(client,change):
    design=client.post('/api/research/doe/design',json={'factors':FACTORS}).json()
    payload={'design_id':design['id'],'template':TEMPLATE,'bindings':{'temp':'temperature_kelvin','packing':'build_density'},**change}
    assert client.post('/api/research/workflows',json=payload).status_code==422
    assert client.get('/api/research/workflows').json()['records']==[]


def test_missing_rows_not_silently_dropped_and_incompatible_job_rejected(client):
    record=create(client);identifier=record['id']
    response,_=synthesize(client,record,record['rows'][0],changed=True)
    assert response.status_code==422
    assert client.post(f'/api/research/workflows/{identifier}/optimize',json={}).status_code==422
    assert client.post(f'/api/research/workflows/{identifier}/optimize',json={'exclusions':{'1':''}}).status_code==422
    assert client.post(f'/api/research/workflows/{identifier}/optimize',json={'exclusions':{'99':'not real'}}).status_code==422
    assert client.app.state.workflows._read(identifier)['optimizations']==[]
    assert client.post(f'/api/research/workflows/{identifier}/analyze',json={'rows':[1]}).json()['rows'][0]['state']=='analysis_failed'


def test_analysis_cancellation_does_not_hold_workflow_lock(client):
    record=create(client);identifier=record['id'];response,_=synthesize(client,record,record['rows'][0]);assert response.status_code==200
    gate=client.app.state.execution
    with gate.slot('simulation','unrelated'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.post,f'/api/research/workflows/{identifier}/analyze',json={'rows':[1]})
            deadline=time.monotonic()+8
            while not any(t['owner']=='workflow:'+identifier for t in gate.status()['tasks']):
                assert time.monotonic()<deadline;time.sleep(.02)
            assert client.get('/api/health').status_code==200
            assert client.get(f'/api/research/workflows/{identifier}').json()['busy'] is True
            assert client.post(f'/api/research/workflows/{identifier}/cancel').status_code==200
            result=future.result(timeout=5).json()
    assert result['state']=='cancelled' and result['busy'] is False
    assert result['rows'][0]['observation'] is None


def test_missing_engine_blocks_submission_before_reserving_budget(client,monkeypatch):
    record=create(client)
    monkeypatch.setattr(workflows,'preflight',lambda template:['LAMMPS'])
    response=client.post(f"/api/research/workflows/{record['id']}/submit",json={'rows':[1]})
    assert response.status_code==409
    assert client.app.state.workflows.get(record['id'])['submitted']==0
    assert client.app.state.manager.list()==[]


def test_confirmation_analysis_and_failed_confirmation_retry(client,monkeypatch):
    record=create(client);identifier=record['id'];manager=client.app.state.manager
    monkeypatch.setattr(workflows,'preflight',lambda config:[])
    monkeypatch.setattr(manager.pool,'submit',lambda *args,**kwargs:None)
    # A saved candidate isolates confirmation lifecycle from regression fitting (covered end-to-end above).
    saved=client.app.state.workflows._read(identifier)
    optimization_id='b'*32
    saved['optimizations']=[{'id':optimization_id,'recommendation':{'settings':{'temp':310.,'packing':.8},'predicted_response':1.0}}]
    client.app.state.workflows._save(saved)
    a=client.post(f'/api/research/workflows/{identifier}/confirm',json={'optimization_id':optimization_id}).json()
    first=a['confirmations'][0]['job_id'];manager._update(first,status='failed')
    response=client.post(f'/api/research/workflows/{identifier}/confirm',json={'optimization_id':optimization_id,'retry_failed':True})
    assert response.status_code==200,response.text
    result=response.json();assert len(result['confirmations'])==2 and result['submitted']==2
    second=result['confirmations'][-1]['job_id'];assert second!=first
    _,source=synthesize(client,record,record['rows'][0])
    destination=manager.directory(second)
    (destination/'eq3_last.data').write_bytes((source/'eq3_last.data').read_bytes())
    manager._update(second,status='completed')
    response=client.post(f'/api/research/workflows/{identifier}/analyze-confirmation',json={'job_id':second})
    assert response.status_code==200,response.text
    result=response.json();observation=result['confirmations'][-1]['observation']
    assert result['state']=='confirmation_review' and observation['verified'] is False
    assert observation['prediction_error']==pytest.approx(observation['value']-1.)
    assert result['confirmations'][-1]['validated'] is False


def test_reserved_submission_without_job_needs_explicit_retry(client,monkeypatch):
    record=create(client);identifier=record['id'];manager=client.app.state.manager
    monkeypatch.setattr(workflows,'preflight',lambda config:[])
    monkeypatch.setattr(manager.pool,'submit',lambda *args,**kwargs:None)
    saved=client.app.state.workflows._read(identifier);row=saved['rows'][0]
    previous=row['job_id'];row['attempts']=[previous];row['state']='submitted';saved['submitted']=1
    client.app.state.workflows._save(saved)
    result=client.post(f'/api/research/workflows/{identifier}/submit',json={'rows':[1]}).json()
    assert result['submitted']==1 and manager.list()==[]
    result=client.post(f'/api/research/workflows/{identifier}/submit',json={'rows':[1],'retry_failed':True}).json()
    assert result['submitted']==2 and result['rows'][0]['job_id']!=previous
    assert len(manager.list())==1


def test_workflow_records_reject_symlink_and_invalid_download(client,tmp_path):
    record=create(client);root=client.app.state.workflows.root
    original=root/record['id']/'result.json';outside=tmp_path/'outside.json'
    original.rename(outside);original.symlink_to(outside)
    assert client.get(f"/api/research/workflows/{record['id']}").status_code==404
    assert client.get(f"/api/research/workflows/{record['id']}/download").status_code==404
    assert client.get('/api/research/workflows/not-an-id').status_code==404


def test_mcp_workflow_bridge_preserves_review_gate(client):
    from workbench.mcp_server import PolymerLabTools,ToolError
    class Adapter:
        def request(self,method,path,payload=None,timeout=30):
            response=client.request(method,path,json=payload)
            if response.is_error:raise ToolError(response.text)
            return response.json()
    tools=PolymerLabTools(Adapter())
    assert tools.tools['get_research_workflow']['definition']['annotations']['readOnlyHint'] is True
    assert tools.tools['control_research_workflow']['definition']['annotations']['readOnlyHint'] is False
    design=client.post('/api/research/doe/design',json={'factors':FACTORS}).json()
    record=tools.create_research_workflow({'design_id':design['id'],'template':TEMPLATE,'bindings':{'temp':'temperature_kelvin','packing':'build_density'}})
    assert record['state']=='planned' and client.app.state.manager.list()==[]
    assert tools.get_research_workflow({'workflow_id':record['id']})['id']==record['id']
    assert tools.control_research_workflow({'workflow_id':record['id'],'action':'cancel'})['state']=='cancelled'
