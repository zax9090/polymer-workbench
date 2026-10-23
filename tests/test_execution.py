"""Cross-feature admission, cancellation and worker failure must not strand a lease."""
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import threading
import time
import pytest
from fastapi.testclient import TestClient
from workbench.execution import ExecutionManager, ExecutionCancelled
from workbench.server import create_app


def wait_for(predicate):
    deadline=time.monotonic()+8
    while not predicate():
        assert time.monotonic()<deadline
        time.sleep(.02)


def test_rust_fifo_cross_feature_and_waiter_cancellation():
    manager=ExecutionManager()
    assert manager.engine=='rust'
    entered, release=threading.Event(), threading.Event()
    seen=[]
    def task(kind):
        with manager.slot(kind) as ticket:
            seen.append(kind)
            if kind=='simulation':
                entered.set();assert release.wait(8)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first=pool.submit(task,'simulation');assert entered.wait(5)
        second=pool.submit(task,'doe')
        wait_for(lambda:len(manager.status()['tasks'])==2)
        third=pool.submit(task,'volume')
        wait_for(lambda:len(manager.status()['tasks'])==3)
        ticket=next(t for t in manager.status()['tasks'] if t['kind']=='doe')
        manager.cancel(identifier=ticket['id'])
        with pytest.raises(ExecutionCancelled):second.result(timeout=3)
        assert seen==['simulation']
        release.set();first.result(timeout=3);third.result(timeout=3)
    assert seen==['simulation','volume']
    assert all(t['status'] not in {'running','queued'} for t in manager.status()['tasks'])


def test_native_worker_crash_releases_and_real_next_compute_succeeds(monkeypatch):
    manager=ExecutionManager()
    original=subprocess.Popen
    with monkeypatch.context() as patch:
        patch.setattr(subprocess,'Popen',lambda argv,**kwargs:original([sys.executable,'-c','import os;os._exit(17)'],**kwargs))
        with pytest.raises(RuntimeError,match='17'):
            manager.compute('doe_design',{})
    result=manager.compute('doe_design',{'factors':[{'name':'x','low':-1.,'high':1.},{'name':'y','low':-1.,'high':1.}]})
    assert result['total_runs']==7
    assert manager.status()['tasks'][0]['status']=='failed'


def test_cancel_running_child_waits_for_process_exit(monkeypatch):
    manager=ExecutionManager()
    original=subprocess.Popen
    monkeypatch.setattr(subprocess,'Popen',lambda argv,**kwargs:original([sys.executable,'-c','import time;time.sleep(60)'],**kwargs))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(manager.compute,'doe_design',{},'cancel-me')
        wait_for(lambda:any('pid' in t for t in manager.status()['tasks']))
        pid=manager.status()['tasks'][0]['pid']
        manager.cancel(owner='cancel-me')
        with pytest.raises(ExecutionCancelled):future.result(timeout=4)
    with pytest.raises(ProcessLookupError):os.kill(pid,0)
    with manager.slot('next'):pass


def test_api_cross_kind_wait_is_visible_and_health_responds(tmp_path):
    with TestClient(create_app(tmp_path/'jobs')) as client:
        manager=client.app.state.execution
        with manager.slot('simulation','test-held'):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(client.post,'/api/research/doe/design',json={'factors':[{'name':'x','low':0.,'high':1.},{'name':'y','low':0.,'high':1.}]})
                wait_for(lambda:len(manager.status()['tasks'])==2)
                assert client.get('/api/health').status_code==200
                status=client.get('/api/execution').json()
                queued=next(t for t in status['tasks'] if t['status']=='queued')
                assert client.post('/api/execution/'+queued['id']+'/cancel').status_code==200
                assert future.result(timeout=4).status_code==409
        assert all(t['status'] not in {'running','queued'} for t in manager.status()['tasks'])


@pytest.mark.skipif(not sys.platform.startswith('linux'),reason='Linux parent-death signal integration')
def test_abrupt_parent_exit_stops_worker_and_descendant(tmp_path):
    import json
    from pathlib import Path
    child_code='''import json,os,subprocess,sys,time
from workbench.paths import install_worker_lifecycle
install_worker_lifecycle()
p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])
open(sys.argv[1],"w").write(json.dumps([os.getpid(),p.pid]))
time.sleep(60)
'''
    parent_code='''import os,subprocess,sys,time
p=subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]],start_new_session=True,env={**os.environ,"POLYMER_WORKER_PARENT_PID":str(os.getpid())})
time.sleep(60)
'''
    output=tmp_path/'pids.json'
    parent=subprocess.Popen([sys.executable,'-c',parent_code,child_code,str(output)])
    pids=[]
    def live(pid):
        path=Path(f'/proc/{pid}/stat')
        try:return path.read_text().split(') ',1)[1].split()[0] not in {'Z','X'}
        except FileNotFoundError:return False
    try:
        wait_for(lambda:output.is_file() and output.stat().st_size>0)
        pids=json.loads(output.read_text())
        assert all(live(pid) for pid in pids)
        parent.kill();parent.wait(timeout=3)
        wait_for(lambda:all(not live(pid) for pid in pids))
    finally:
        if parent.poll() is None:parent.kill()
        parent.wait()
        if pids:
            try:os.killpg(pids[0],9)
            except ProcessLookupError:pass
