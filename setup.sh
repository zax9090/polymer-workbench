#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
python3 -c 'import sys; sys.exit("Python 3.12 이상이 필요합니다. 이 환경은 3.13에서 검증했습니다.") if sys.version_info < (3, 12) else None'
python3 -m venv .venv
if [[ -f requirements.lock.txt ]]; then
  .venv/bin/python -m pip install -r requirements.lock.txt
  .venv/bin/python -m pip install --no-deps -e .
else
  .venv/bin/python -m pip install -e '.[science,builders,test]' mpich
fi
# Rust compute kernels (Blends-style contact sampling); without cargo the numpy implementation runs instead.
if command -v cargo >/dev/null 2>&1; then
  .venv/bin/python -m pip install ./native
else
  echo 'cargo가 없어 Rust 계산 커널을 건너뜁니다. 접촉 χ 계산은 느린 numpy 구현으로 돌아갑니다. (설치: https://rustup.rs)'
fi
echo '설치 완료. ./start.sh 로 실행하세요.'
