#!/usr/bin/env python3
"""Exercise paste-to-run sheets in Chromium against a temporary local research folder.

Requires Playwright and Chromium in the interpreter running this script. The app
itself uses --python (default: .venv/bin/python). No existing research data is used.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def check(base, chromium, artifacts):
    def records():
        with urllib.request.urlopen(base + '/api/research/sheets') as response:
            return json.load(response)['records']

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
        errors, creates = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: creates.append(request.post_data_json)
                if request.method == 'POST' and request.url == base + '/api/research/sheets' else None)
        page.goto(base + '/#sheets', wait_until='networkidle')
        page.wait_for_selector('[data-sheet-template="blends_solvents"]')
        assert page.locator('[data-sheet-template]').count() == 3
        page.locator('[data-sheet-template="blends_solvents"]').click()
        page.get_by_text('검사 통과', exact=False).wait_for()
        original = page.locator('#sheet-text').input_value()
        assert not records(), 'Preview must not create records'
        with page.expect_download() as download:
            page.locator('#sheet-download').click()
        assert Path(download.value.path()).read_text() == original
        # A rejected confirmation never starts a computation.
        page.once('dialog', lambda dialog: dialog.dismiss())
        page.locator('#sheet-form button[type="submit"]').click()
        page.wait_for_function("!document.querySelector('#sheet-text').readOnly")
        assert not creates and not records()
        # Errors retain line numbers and selecting one focuses the offending line.
        page.locator('#sheet-text').fill(original + 'unknown_key: value\n')
        page.locator('#sheet-check').click()
        page.wait_for_selector('[data-sheet-line]')
        page.locator('[data-sheet-line]').last.click()
        assert page.locator('#sheet-text').evaluate('(el) => el.value.slice(el.selectionStart,el.selectionEnd)') == 'unknown_key: value'
        page.locator('#sheet-text').fill(original)
        page.once('dialog', lambda dialog: dialog.accept())
        page.locator('#sheet-form').evaluate('(form) => { form.requestSubmit(); form.requestSubmit(); }')
        deadline = time.monotonic() + 240
        while True:
            saved = records()
            if saved:
                with urllib.request.urlopen(base + '/api/research/sheets/' + saved[0]['id']) as response:
                    state = json.load(response)
                if state['status'] not in {'queued', 'running'}:
                    break
            assert time.monotonic() < deadline, 'Sheet did not finish within four minutes'
            page.wait_for_timeout(200)
        page.wait_for_selector('#sheet-result a[download]')
        assert len(creates) == 1, 'Double submission must create exactly one sheet'
        identifier = records()[0]['id']
        with urllib.request.urlopen(base + '/api/research/sheets/' + identifier) as response:
            result = json.load(response)
        assert result['counts'] == {'completed': 5}, result
        assert len(result['ranking']) == 5
        with page.expect_download() as download:
            page.locator('#sheet-result a[download]').click()
        rows = list(csv.DictReader(io.StringIO(Path(download.value.path()).read_bytes().decode('utf-8-sig'))))
        assert len(rows) == 5
        assert [row['screen'] for row in rows] == [row['screen'] for row in result['ranking']]
        page.locator('[data-sheet-load]').click()
        assert page.locator('#sheet-text').input_value() == original
        if artifacts:
            artifacts.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifacts / 'sheets-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile page overflow'
        if artifacts:
            page.screenshot(path=str(artifacts / 'sheets-mobile.png'), full_page=True)
        # Fresh hash navigation restores templates/history and opens the saved result.
        page.reload(wait_until='networkidle')
        page.wait_for_selector('[data-sheet-template="blends_solvents"]')
        page.locator('[data-sheet-open]').first.click()
        page.wait_for_selector('#sheet-result a[download]')
        assert len(creates) == 1 and len(records()) == 1
        page.locator('[data-view="history"]').click()
        page.locator('[data-view="sheets"]').click()
        page.wait_for_selector('[data-sheet-template="blends_solvents"]')
        assert not errors, errors
        browser.close()
        return {'status': 'passed', 'completed_pairs': 5, 'created_records': 1,
                'checks': ['direct_hash', 'template_preview', 'text_export', 'rejected_confirmation',
                           'error_line_selection', 'double_submit', 'real_rust_blends', 'ranked_csv',
                           'saved_text_reload', 'mobile', 'history_reload', 'menu_navigation'],
                'scientific_validation': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, default=ROOT / '.venv/bin/python')
    parser.add_argument('--chromium', default=shutil.which('chromium'))
    parser.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='polymer-sheets-browser-') as folder:
        root = Path(folder)
        env = os.environ | {'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'RAYON_NUM_THREADS': '2'}
        env.pop('POLYMER_DESKTOP_TOKEN', None)
        env['PATH'] = str(args.python.absolute().parent) + os.pathsep + env.get('PATH', '')
        with (root / 'server.log').open('w+') as log:
            process = subprocess.Popen([str(args.python), '-m', 'workbench', '--server', '--port', '0',
                '--data-dir', str(root / 'jobs'), '--ready-file', str(root / 'ready.json')],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + 90
                while not (root / 'ready.json').exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        raise RuntimeError(log.read()[-4000:])
                    time.sleep(.1)
                port = json.loads((root / 'ready.json').read_text())['port']
                print(json.dumps(check(f'http://127.0.0.1:{port}', args.chromium, args.artifacts), ensure_ascii=False))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == '__main__':
    main()
