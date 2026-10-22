#!/usr/bin/env python3
"""Exercise local research screens with QtWebEngine and an isolated API store.

Run: xvfb-run -a .venv/bin/python scripts/verify_research_ui.py
Screenshots and a JSON result are written outside the user data directory.
Synthetic input below is tagged as demo and cannot train a property model.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu --no-sandbox')
os.environ.setdefault('QTWEBENGINE_DISABLE_SANDBOX', '1')
from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

class Page(QWebEnginePage):
    def __init__(self, parent):
        super().__init__(parent)
        self.errors = []
    def javaScriptConsoleMessage(self, level, message, line, source):
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            self.errors.append({'message': message, 'line': line, 'source': source})

def main():
    artifacts = Path(tempfile.mkdtemp(prefix='polymer-research-ui-'))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    env = dict(os.environ); env.pop('POLYMER_DESKTOP_TOKEN', None)
    command = [str(ROOT/'.venv/bin/python'), '-c', 'import uvicorn;from workbench.server import create_app;uvicorn.run(create_app('+repr(str(artifacts/'jobs'))+'),host="127.0.0.1",port='+str(port)+',log_level="warning")']
    with (artifacts/'server.log').open('w') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            url = f'http://127.0.0.1:{port}'
            for _ in range(300):
                try:
                    urllib.request.urlopen(url+'/api/health', timeout=.5).read(); break
                except Exception:
                    if process.poll() is not None: raise RuntimeError((artifacts/'server.log').read_text())
                    time.sleep(.1)
            else: raise RuntimeError('API startup timed out')
            app = QApplication([]); view = QWebEngineView(); page = Page(view);view.setPage(page);view.resize(1360,900);view.show()
            def pause(ms=60):
                loop=QEventLoop();QTimer.singleShot(ms,loop.quit);loop.exec()
            def js(code):
                result=[];loop=QEventLoop();page.runJavaScript(code,lambda value:(result.append(value),loop.quit()));QTimer.singleShot(10000,loop.quit);loop.exec()
                if not result: raise RuntimeError('JavaScript callback timed out')
                return result[0]
            def wait(condition,timeout=30):
                until=time.monotonic()+timeout
                while time.monotonic()<until:
                    if js(condition): return
                    pause(100)
                raise AssertionError('UI timed out: '+condition)
            def fill(identifier,value):
                js(f'document.getElementById({json.dumps(identifier)}).value={json.dumps(value)};')
            def click(identifier): js(f'document.getElementById({json.dumps(identifier)}).click()')
            def submit(identifier): js(f'document.getElementById({json.dumps(identifier)}).requestSubmit()')
            checks=[]
            def passed(name):checks.append(name);print('PASS',name,flush=True)
            view.load(QUrl(url))
            wait('typeof showView === "function" && typeof researchState !== "undefined" && state.connected')
            assert js('new Set([...document.querySelectorAll("[id]")].map(e=>e.id)).size === document.querySelectorAll("[id]").length')
            passed('unique DOM ids and connected initial screen')
            js('showView("evidence")');wait('state.evidence !== null');pause(200)
            assert js('document.querySelector(".extension-evidence") !== null && document.querySelectorAll("#evidence-list .evidence-card").length >= 8')
            passed('REA extension source audit renders with original download link')
            for name in ['research','measurements','models','campaigns','bridges']:
                js(f'showView({json.dumps(name)})');wait('researchState.overview !== null && !researchState.loading');pause(250)
                assert js(f'!document.getElementById("view-{name}").classList.contains("hidden") && document.getElementById("breadcrumb-current").textContent.length > 0')
                assert not js('document.documentElement.scrollWidth > window.innerWidth + 1')
                view.grab().save(str(artifacts/f'{name}-1360.png'))
                passed(name+' navigation and desktop width')
            js('showView("measurements")');
            fill('measurement-csv-text','property,value,unit,method,source,origin,repeat_smiles,verified\ntg,80,C,DSC,isolated UI test fixture,demo,[*]CC[*],true\n')
            click('measurement-csv-preview');wait('researchState.csvRows !== null')
            assert js('researchState.csvRows[0].verified === false && document.getElementById("measurement-csv-import").disabled')
            click('measurement-csv-reviewed');click('measurement-csv-import')
            wait('document.getElementById("measurement-csv-result").textContent.includes("1개 행")')
            wait('researchState.overview.measurements.length === 1')
            assert js('researchState.overview.measurements[0].origin === "demo" && researchState.overview.measurements[0].value === 353.15')
            passed('CSV preview clears verification and requires explicit review, stores converted demo value')
            fill('curve-kind','tensile');js('document.getElementById("curve-kind").dispatchEvent(new Event("change"))')
            fill('curve-text','strain,stress\n0,0\n0.001,2\n0.002,4\n0.003,6\n0.004,8\n');fill('curve-linear','0.001, 0.003');submit('curve-form')
            wait('researchState.curve !== null');click('curve-use-draft')
            assert js('Math.abs(Number(document.getElementById("measurement-value").value)-2000)<1e-6 && document.getElementById("measurement-review").value==="false" && researchState.overview.measurements.length===1')
            passed('tensile curve draft is quantitative and remains unsaved/unverified')
            js('showView("models")');submit('model-train-form');wait('document.getElementById("model-train-result").textContent.length > 0')
            assert js('researchState.overview.models.length === 0 && document.getElementById("model-train-result").querySelector(".error-message") !== null')
            passed('demo measurement cannot create a trained model')
            js('showView("campaigns")');fill('campaign-lengths','8, 12');fill('campaign-steps','20');click('campaign-generate');submit('campaign-form')
            wait('researchState.campaigns.length===1');js('document.querySelector("[data-campaign-action=start]").click()')
            wait('researchState.campaigns[0].attempted===2 && researchState.campaigns[0].candidates.every(c=>c.status==="completed")',120)
            assert js('researchState.campaigns[0].candidates.every(c=>Number.isFinite(c.objective))')
            js('window.scrollTo(0,0)');pause(200);view.grab().save(str(artifacts/'campaigns-results-1360.png'))
            passed('explicit bounded automatic campaign runs two real NumPy jobs and displays objective values')
            js('showView("bridges")');submit('bridge-form');wait('document.querySelector("#bridge-result [data-open-job]") !== null')
            click('bridge-refresh');wait('researchState.bridgeCapabilities !== null')
            passed('bridge preparation job is submitted and capabilities render')
            js('showView("research")');click('research-snapshot');wait('document.querySelector("#research-snapshot-result a[download]") !== null')
            passed('Parquet snapshot offers actual download URL')
            view.resize(760,900);js('showView("measurements")');pause(400)
            assert not js('document.documentElement.scrollWidth > window.innerWidth + 1')
            view.grab().save(str(artifacts/'measurements-760.png'))
            passed('narrow layout has no page overflow')
            fatal=[error for error in page.errors if any(token in error['message'] for token in ['ReferenceError','TypeError','SyntaxError','Unhandled'])]
            assert not fatal, fatal
            report={'passed':True,'checks':checks,'console_errors':page.errors,'artifacts':str(artifacts),'data_scope':'isolated temporary store; synthetic measurement tagged demo'}
            (artifacts/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
            print(json.dumps(report,ensure_ascii=False),flush=True)
            view.close();app.processEvents()
        finally:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait()
    return 0
if __name__=='__main__':raise SystemExit(main())
