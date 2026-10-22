#!/usr/bin/env python3
"""Package reviewable application sources and a small UTF-8 Gemini review document.

Run after final documentation and evidence updates. Outputs remain inside this
project's releases directory; no installed app, user data or cloud account is touched.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    '.gitignore', 'README.md', 'LICENSE', 'LICENSE.txt', 'NOTICE', 'pyproject.toml',
    'requirements.lock.txt', 'requirements.desktop.lock.txt', 'setup.sh', 'start.sh',
    'install-desktop.sh', 'launch-desktop.sh',
}
DIRECTORIES = {'assets', 'docs', 'examples', 'packaging', 'scripts', 'tests', 'workbench'}
TOOLS_FILES = {
    'tools/rea/.gitignore', 'tools/rea/README.md', 'tools/rea/bootstrap.py',
    'tools/rea/catalog.json', 'tools/rea/contracts.json', 'tools/rea/extension_catalog.json',
    'tools/rea/package.json', 'tools/rea/package-lock.json', 'tools/rea/npm-registry.json',
    'tools/rea/rea-agents-registry.json',
}
BLOCKED_PARTS = {'.git', '.venv', 'node_modules', 'build', 'dist', 'releases', 'data',
                 '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.cache', 'secrets'}
BLOCKED_SUFFIXES = {'.pyc', '.pyo', '.pem', '.key', '.p12', '.pfx', '.keystore', '.duckdb', '.parquet'}
TEXT_SUFFIXES = {'.py', '.js', '.css', '.html', '.md', '.txt', '.toml', '.json', '.sh', '.spec', '.xml', '.csv', '.pdb'}
MAX_REVIEW_BYTES = 2_000_000
MAX_ZIP_BYTES = 15_000_000


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(relative: str) -> Path:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or '..' in rel.parts or any(part in BLOCKED_PARTS for part in rel.parts):
        raise ValueError(f'Forbidden source path: {relative}')
    lower = rel.name.lower()
    if lower.startswith('.env') or any(word in lower for word in ('credential', 'secret', 'id_rsa', 'id_ed25519')) or rel.suffix.lower() in BLOCKED_SUFFIXES:
        raise ValueError(f'Private/runtime file excluded: {relative}')
    path = ROOT.joinpath(*rel.parts)
    # Reject symlinks in any component, even when their target is inside the project.
    for index in range(1, len(rel.parts) + 1):
        if ROOT.joinpath(*rel.parts[:index]).is_symlink():
            raise ValueError(f'Symlink excluded: {relative}')
    if not path.is_file():
        raise ValueError(f'Source file missing: {relative}')
    return path


def allowed(relative: str) -> bool:
    rel = PurePosixPath(relative)
    if relative in ROOT_FILES or relative in TOOLS_FILES:
        return True
    if rel.parts and rel.parts[0] in DIRECTORIES:
        # Files come from Git's tracked/nonignored inventory, further restricted to
        # ordinary source, build recipe, documentation, asset and fixture formats.
        return rel.suffix.lower() in TEXT_SUFFIXES | {'.svg', '.png', '.jpg', '.jpeg'}
    if rel.parts and rel.parts[0] == 'evidence':
        # Raw third-party snapshots are added only through the pinned manifests below.
        if relative.startswith(('evidence/sources/', 'evidence/extensions/sources/')):
            return False
        return rel.suffix.lower() in {'.json', '.log', '.xml', '.png'}
    return False


def inventory() -> tuple[dict[str, bytes], list[dict]]:
    raw = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT)
    names = {item.decode('utf-8') for item in raw.split(b'\0') if item}
    files, skipped = {}, []
    for relative in sorted(names):
        if not allowed(relative):
            continue
        try:
            path = safe_path(relative)
        except ValueError as exc:
            skipped.append({'path': relative, 'reason': str(exc)})
            continue
        files[relative] = path.read_bytes()
    # Include ignored historical reference sources only when an audit identifies
    # their exact bytes. Never recursively sweep arbitrary ignored directories.
    for manifest_name, base in (('evidence/repositories.json', 'evidence/sources'),
                                ('evidence/extensions/audit.json', 'evidence/extensions/sources')):
        manifest = json.loads(safe_path(manifest_name).read_text(encoding='utf-8'))
        for repo in manifest['repositories']:
            for source in repo['source_files']:
                relative = f"{base}/{repo['id']}/{source['path']}"
                path = safe_path(relative)
                data = path.read_bytes()
                expected = source.get('sha256')
                if not expected or sha256(data) != expected:
                    raise ValueError(f'Pinned upstream source hash mismatch: {relative}')
                files[relative] = data
    required = {'README.md', 'pyproject.toml', 'workbench/server.py', 'workbench/static/index.html',
                'tools/rea/catalog.json', 'evidence/repositories.json', 'evidence/extensions/audit.json'}
    if not required <= files.keys():
        raise ValueError(f'Required source files absent: {sorted(required - files.keys())}')
    return dict(sorted(files.items())), skipped


def is_review_text(relative: str) -> bool:
    # The review document explains our program. Full third-party/reference source,
    # graph output and vendor/minified assets remain inspectable in the source ZIP.
    if relative.startswith(('workbench/static/vendor/', 'evidence/sources/', 'evidence/rea/',
                            'evidence/extensions/sources/', 'evidence/extensions/rea/')):
        return False
    path = PurePosixPath(relative)
    if path.name.endswith('.min.js') or path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    return True


def review_document(version: str, files: dict[str, bytes], head: str) -> tuple[bytes, list[str]]:
    selected, omitted = [], []
    for relative, data in files.items():
        if not is_review_text(relative):
            continue
        # Very large generated metadata can overwhelm an AI review even though the
        # application sources are small. Record each omission explicitly below.
        if relative.startswith('evidence/') and len(data) > 200_000:
            omitted.append(relative)
            continue
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            omitted.append(relative)
            continue
        selected.append((relative, data, text))
    header = f'''Polymer Lab {version} — Gemini 코드 검토용 설명과 소스
생성 시각: {datetime.now(timezone.utc).isoformat()}
Git 기준 커밋: {head}

이 UTF-8 텍스트 파일에는 Polymer Lab의 자체 앱 코드, 설명서, 실행·검증 스크립트,
테스트 및 출처 메타데이터를 담았습니다. 각 구역에 원본 파일명과 SHA256을 표시합니다.
검토 대상: 기능 연결, 데이터 단위/출처, 과학적 한계, 오류 처리, 사용 흐름과 재현성.
합성 테스트 자료는 소프트웨어 동작 검증용이며 실제 실험 정확도의 근거가 아닙니다.

이 파일은 설치 프로그램이 아닙니다. 데스크톱 실행 압축파일은 크기가 크고 바이너리를
포함하므로 Gemini 코드 검토 업로드에는 이 텍스트 파일을 사용하세요. 서비스의 실제
업로드 허용 여부는 별도이며 이 파일을 만들거나 저장해도 외부 업로드는 수행하지 않습니다.
전체 자체 소스·테스트·이미지·라이선스·고정된 REA 원본과 출력은
PolymerLab-{version}-source.zip에 있습니다. 제3자 라이브러리의 큰 원본/최소화 코드,
REA 원시 그래프, 이미지, 실행 바이너리, 사용자 계산/측정 데이터와 비밀 설정은
이 텍스트에 포함하지 않습니다. .venv/node_modules도 배포 소스에서 제외합니다.

포함된 텍스트 파일: {len(selected)}개
추가로 생략한 대형/비텍스트 메타데이터: {', '.join(omitted) or '없음'}

파일 목차
'''
    header += '\n'.join(f'- {name} ({len(data)} bytes; SHA256 {sha256(data)})' for name, data, _ in selected)
    chunks = [header]
    for name, data, text in selected:
        chunks.append(f'\n\n{"=" * 80}\nFILE: {name}\nBYTES: {len(data)}\nSHA256: {sha256(data)}\n{"=" * 80}\n{text}')
    result = ('\n'.join(chunks) + '\n').encode('utf-8')
    if len(result) > MAX_REVIEW_BYTES:
        raise ValueError(f'Review document exceeds {MAX_REVIEW_BYTES} bytes ({len(result)}); split it deliberately rather than truncating source')
    return result, [name for name, _, _ in selected]


def version_string() -> str:
    import tomllib
    value = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.-]*)?', value):
        raise ValueError('Unsafe project version')
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Validate inventory/hash/text size and print planned outputs without writing archives')
    args = parser.parse_args()
    version = version_string()
    files, skipped = inventory()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT))
    review, text_files = review_document(version, files, head)
    output = ROOT / 'releases'
    archive = output / f'PolymerLab-{version}-source.zip'
    review_path = output / 'PolymerLab-Gemini-review.txt'
    manifest = {'schema_version': 1, 'application_version': version, 'created_at': datetime.now(timezone.utc).isoformat(),
                'git_head': head, 'git_worktree_dirty': dirty,
                'scope': 'Application sources, documentation, fixtures, and verified pinned REA evidence; no user data or installed dependencies',
                'files': [{'path': name, 'bytes': len(data), 'sha256': sha256(data)} for name, data in files.items()],
                'excluded_paths': skipped, 'review_text_files': text_files}
    plan = {'version': version, 'source_files': len(files), 'uncompressed_source_bytes': sum(map(len, files.values())),
            'review_files': len(text_files), 'review_bytes': len(review), 'archive': str(archive), 'review': str(review_path),
            'dry_run': args.dry_run}
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    output.mkdir(parents=True, exist_ok=True)
    # Publish complete files only. Existing source distributions survive failures.
    with tempfile.TemporaryDirectory(prefix='.source-package-', dir=output) as temp:
        temp = Path(temp)
        staged = temp / archive.name
        with zipfile.ZipFile(staged, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for name, data in files.items():
                entry = zipfile.ZipInfo('PolymerLab-source/' + name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if safe_path(name).stat().st_mode & stat.S_IXUSR else 0o644
                entry.external_attr = (stat.S_IFREG | mode) << 16
                bundle.writestr(entry, data)
            bundle.writestr('PolymerLab-source/SOURCE_MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
        if staged.stat().st_size > MAX_ZIP_BYTES:
            raise ValueError(f'Source archive exceeds {MAX_ZIP_BYTES} bytes; inspect evidence growth before publishing')
        with zipfile.ZipFile(staged) as bundle:
            damaged = bundle.testzip()
            if damaged:
                raise ValueError(f'ZIP CRC verification failed: {damaged}')
            for name, data in files.items():
                if sha256(bundle.read('PolymerLab-source/' + name)) != sha256(data):
                    raise ValueError(f'Archive content hash mismatch: {name}')
        staged_review = temp / review_path.name
        staged_review.write_bytes(review)
        plan.update(archive_bytes=staged.stat().st_size, archive_sha256=sha256(staged.read_bytes()), review_sha256=sha256(review))
        staged.replace(archive)
        staged_review.replace(review_path)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
