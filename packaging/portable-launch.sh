#!/bin/sh
# Portable launcher; scientific subprocesses use the same bundled interpreter.
set -eu
LAUNCHER=$(readlink -f -- "$0")
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$LAUNCHER")" && pwd)
exec "$APP_DIR/PolymerLab" "$@"
