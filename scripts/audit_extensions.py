#!/usr/bin/env python3
"""Pin and inspect selected extension interfaces with the installed REA toolkit.

--resolve explicitly selects today's upstream HEAD and downloads only listed files.
--fetch-pinned restores missing pinned files from their recorded commit URLs.
Subsequent runs verify pinned bytes; --run-rea performs source inventory, not
Python/native decompilation, dependency review or scientific validation.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'tools/rea/extension_catalog.json'
BASE = ROOT / 'evidence/extensions'
REA = ROOT / 'tools/rea/node_modules/rea-agents/scripts/rea.mjs'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def fingerprint(path):
    rows = [(str(p.relative_to(path)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(path.rglob('*')) if p.is_file()]
    return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def resolve(repo):
    commit = subprocess.check_output(['git', 'ls-remote', repo['url'] + '.git', 'HEAD'], text=True, timeout=45).split()[0]
    sources = []
    for source in repo['source_files']:
        url = f"https://raw.githubusercontent.com/{repo['repository']}/{commit}/{source['path']}"
        data = urlopen(Request(url, headers={'User-Agent':'PolymerLab-REA-audit/0.3'}), timeout=45).read()
        path = BASE / 'sources' / repo['id'] / source['path']
        path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(data)
        sources.append({**source,'source_url':url,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),
                        'github_blob_sha':hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()})
    return {**repo,'commit':commit,'resolved_at':datetime.now(timezone.utc).isoformat(),'source_files':sources}


def fetch_pinned(repo):
    for source in repo['source_files']:
        path = BASE / 'sources' / repo['id'] / source['path']
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == source['sha256']:
            continue
        data = urlopen(Request(source['source_url'], headers={'User-Agent':'PolymerLab-REA-audit/0.3'}), timeout=120).read()
        if hashlib.sha256(data).hexdigest() != source['sha256']:
            raise ValueError(f"Pinned download mismatch: {source['source_url']}")
        path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(data)


def verify(repo):
    for source in repo['source_files']:
        path = BASE / 'sources' / repo['id'] / source['path']
        if hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
            raise ValueError(f"Source mismatch: {path}")
    anchors=[]
    for anchor in repo.get('anchors', []):
        lines=(BASE/'sources'/repo['id']/anchor['path']).read_text().splitlines()
        matches=[i for i,line in enumerate(lines,1) if anchor['needle'] in line]
        if not matches: raise ValueError(f"Interface anchor not found: {repo['id']}: {anchor}")
        anchors.append({**anchor,'line':matches[0],'url':f"{repo['url']}/blob/{repo['commit']}/{anchor['path']}#L{matches[0]}"})
    return anchors


def rea(repo):
    target=BASE/'sources'/repo['id']
    argv=[shutil.which('node') or 'node',str(REA),'import-reference-source',str(target),'--detail','summary','--format','json']
    before=fingerprint(target);start=time.monotonic()
    out=BASE/'rea'/f"{repo['id']}.json";out.parent.mkdir(parents=True,exist_ok=True)
    err=out.with_suffix('.stderr.log')
    with out.open('w') as stdout,err.open('w') as stderr:
        try: code=subprocess.run(argv,cwd=ROOT,stdout=stdout,stderr=stderr,timeout=180).returncode
        except subprocess.TimeoutExpired: code=124
    record={'argv':argv,'returncode':code,'elapsed_seconds':round(time.monotonic()-start,3),'executed_at':datetime.now(timezone.utc).isoformat(),
            'source_sha256':before,'source_unchanged':before==fingerprint(target),'stdout':str(out.relative_to(ROOT)),'stderr':str(err.relative_to(ROOT))}
    write(out.with_suffix('.command.json'),record)
    print(f"REA {repo['id']}: {code}",flush=True)
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--resolve',action='store_true');parser.add_argument('--run-rea',action='store_true')
    parser.add_argument('--fetch-pinned',action='store_true');parser.add_argument('--only',action='append',default=[],help='repository id to re-run with --run-rea')
    args=parser.parse_args()
    repos=read_json(CATALOG)
    if args.resolve:
        with ThreadPoolExecutor(max_workers=3) as pool: repos=list(pool.map(resolve,repos))
        write(CATALOG,repos)
    if args.fetch_pinned:
        for repo in repos: fetch_pinned(repo)
    for repo in repos: verify(repo)
    if args.run_rea:
        if not REA.exists():raise RuntimeError('Local REA unavailable; see tools/rea/README.md')
        selected=[repo for repo in repos if not args.only or repo['id'] in args.only]
        with ThreadPoolExecutor(max_workers=3) as pool: list(pool.map(rea,selected))
    results=[]
    for repo in repos:
        output=read_json(BASE/'rea'/f"{repo['id']}.json");command=read_json(BASE/'rea'/f"{repo['id']}.command.json")
        success=output.get('schema')=='HistoricalSourceSummary' and command.get('returncode')==0 and command.get('source_unchanged') is True and command.get('source_sha256')==fingerprint(BASE/'sources'/repo['id'])
        results.append({**repo,'verified_anchors':verify(repo),'rea_status':'verified_inventory' if success else 'not_verified','rea_command':command})
    package=read_json(REA.parents[1]/'package.json')
    report={'schema_version':1,'created_at':datetime.now(timezone.utc).isoformat(),'rea_version':package.get('version'),'repositories':results,
            'scope':'Selected upstream source files, source hashes, and API/CLI anchors; not complete repositories.',
            'limitations':['REA import-reference-source inventories identity, size and hashes. It does not decompile Python or verify science.',
                           'Compatibility, performance, validated material predictions and mixed-system stability are not established by this audit.',
                           'Installability is separate from this historical audit. Inspect live bridge capabilities for actual dependencies.']}
    write(BASE/'audit.json',report)
    print(f"Verified {len(results)} repositories / {sum(len(r['source_files']) for r in results)} source files")
    return 0 if all(r['rea_status']=='verified_inventory' for r in results) or not args.run_rea else 1

if __name__=='__main__':
    raise SystemExit(main())
