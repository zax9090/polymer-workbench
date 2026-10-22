#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
  echo '먼저 ./setup.sh 를 실행하세요.'
  exit 1
fi
export PATH="$PWD/.venv/bin:$PATH"
export LD_LIBRARY_PATH="$PWD/.venv/lib:${LD_LIBRARY_PATH:-}"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
echo 'Polymer Lab: http://127.0.0.1:8765 · 종료: Ctrl+C'
exec .venv/bin/python -m uvicorn workbench.server:app --host 127.0.0.1 --port "${POLYMER_PORT:-8765}"
