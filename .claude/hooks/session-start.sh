#!/bin/bash
# Cloud sessions: install the pinned research environment and REA before work starts.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
# Satisfied pins are skipped, so repeated runs only verify the environment.
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.lock.txt
.venv/bin/python -m pip install --quiet --disable-pip-version-check --no-deps -e .

if [ ! -f tools/rea/node_modules/rea-agents/scripts/rea.mjs ]; then
  python3 tools/rea/bootstrap.py
fi

# Tests load the venv's native LAMMPS/MPI libraries (see README "CLI와 검증").
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\""
    echo "export LD_LIBRARY_PATH=\"$CLAUDE_PROJECT_DIR/.venv/lib:\${LD_LIBRARY_PATH:-}\""
  } >> "$CLAUDE_ENV_FILE"
fi
