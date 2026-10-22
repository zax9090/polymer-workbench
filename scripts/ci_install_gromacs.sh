#!/usr/bin/env bash
# CI: install GROMACS from apt without letting a stalled apt run hang the job.
#
# Two CI runs sat in a silent `apt-get update` until the step timed out. This script
# (1) installs from cached .deb files when the cache matches the runner image,
# (2) otherwise refreshes only the Ubuntu archive sources, never third-party runner
#     repositories, with per-attempt time limits and retries, and
# (3) logs any other apt/dpkg process that could hold the lists or dpkg lock.
# Usage: ci_install_gromacs.sh <directory for cached .deb files>   (CI_APT_PACKAGE overrides the package, for testing)
set -euo pipefail

DEBS="${1:?cache directory required}"
PACKAGE="${CI_APT_PACKAGE:-gromacs}"
mkdir -p "$DEBS/partial"
export DEBIAN_FRONTEND=noninteractive

installed() {
  if [ "$PACKAGE" = gromacs ]; then gmx --version 2>/dev/null | head -n 1; else dpkg -s "$PACKAGE" >/dev/null 2>&1; fi
}

if installed; then
  echo "$PACKAGE is already installed"
  exit 0
fi

if compgen -G "$DEBS/*.deb" >/dev/null; then
  echo "Installing $PACKAGE from $(ls "$DEBS"/*.deb | wc -l) cached packages"
  if sudo timeout 300 apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends "$DEBS"/*.deb && installed; then
    exit 0
  fi
  echo "Cached packages did not install on this image; refreshing from the Ubuntu archive" >&2
  rm -f "$DEBS"/*.deb
fi

# Process names (comm, at most 15 characters) of apt, dpkg, apt-daily and unattended-upgrades.
APT_PROCESSES='apt|apt-get|dpkg|apt.systemd.dai|unattended-upgr'
echo "Other apt/dpkg processes:"
pgrep -a -x "$APT_PROCESSES" || echo "  none"

# Only the Ubuntu archive. GitHub runner images point ubuntu.sources at mirror+file:/etc/apt/apt-mirrors.txt
# rather than an ubuntu.com URI, so select by file name as well as by URI.
SOURCES_DIR="${CI_APT_SOURCES_DIR:-/etc/apt/sources.list.d}"
PARTS="$(mktemp -d)"
for file in "$SOURCES_DIR"/*; do
  [ -f "$file" ] || continue
  if [ "$(basename "$file")" = ubuntu.sources ] || grep -q -E 'ubuntu\.com/ubuntu|mirror\+file:' "$file"; then
    cp "$file" "$PARTS/"
  fi
done
if [ -z "$(ls -A "$PARTS")" ] && ! grep -q -E '^[[:space:]]*deb[[:space:]]' /etc/apt/sources.list 2>/dev/null; then
  echo "No Ubuntu archive source file found; refreshing every configured source" >&2
  PARTS="$SOURCES_DIR"
fi
echo "Sources refreshed from $PARTS:"
grep -h -E '^[[:space:]]*(URIs:|deb[[:space:]])' /etc/apt/sources.list "$PARTS"/* 2>/dev/null | sed 's/^/  /' || true
OPTS=(-o Dir::Etc::sourcelist=/etc/apt/sources.list -o Dir::Etc::sourceparts="$PARTS"
      -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o DPkg::Lock::Timeout=120)

for attempt in 1 2 3; do
  if sudo timeout 180 apt-get "${OPTS[@]}" update; then
    break
  fi
  echo "apt-get update attempt $attempt failed or exceeded 180 s" >&2
  pgrep -a -x "$APT_PROCESSES" || true
  if [ "$attempt" = 3 ]; then
    exit 1
  fi
  sleep 10
done

# Download straight into the cache directory so only this package's missing dependencies are kept.
sudo timeout 600 apt-get "${OPTS[@]}" -o Dir::Cache::archives="$DEBS" install -y --no-install-recommends --download-only "$PACKAGE"
sudo rm -rf "$DEBS/partial" "$DEBS/lock"
sudo chown -R "$(id -u):$(id -g)" "$DEBS"
sudo timeout 300 apt-get "${OPTS[@]}" install -y --no-install-recommends "$DEBS"/*.deb
installed
