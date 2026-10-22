#!/bin/sh
# Launch from this source checkout. The release archive needs no Python install.
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$APP_DIR/.venv/bin/python" ]; then
    cd "$APP_DIR"
    exec "$APP_DIR/.venv/bin/python" -m workbench "$@"
fi
printf '%s\n' '개발용 Python 환경이 없습니다. 배포 파일의 launch.sh를 실행하거나 setup.sh로 개발 환경을 준비하세요.' >&2
exit 1
