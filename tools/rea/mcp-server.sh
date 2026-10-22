#!/usr/bin/env bash
# Start the pinned, project-local REA MCP server on stdio.
# The first launch in a fresh checkout installs it with bootstrap.py.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
rea="$here/node_modules/rea-agents/scripts/rea.mjs"
if [[ ! -f "$rea" ]]; then
  # stdout is the MCP channel; installation output must not reach it.
  python3 "$here/bootstrap.py" >&2
fi
exec node "$rea" mcp
