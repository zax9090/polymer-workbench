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

echo "Other apt/dpkg processes:"
pgrep -a -f 'apt|dpkg|unattended' | grep -v -e pgrep -e ci_install_gromacs || echo "  none"

# Only the Ubuntu archive: copy the source files that point at *.ubuntu.com/ubuntu.
PARTS="$(mktemp -d)"
grep -l -E 'ubuntu\.com/ubuntu' /etc/apt/sources.list.d/* 2>/dev/null | while read -r file; do cp "$file" "$PARTS/"; done
echo "Sources refreshed: $(ls "$PARTS" | tr '\n' ' ')"
OPTS=(-o Dir::Etc::sourcelist=/etc/apt/sources.list -o Dir::Etc::sourceparts="$PARTS"
      -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o DPkg::Lock::Timeout=120)

for attempt in 1 2 3; do
  if sudo timeout 180 apt-get "${OPTS[@]}" update; then
    break
  fi
  echo "apt-get update attempt $attempt failed or exceeded 180 s" >&2
  pgrep -a -f 'apt|dpkg|unattended' | grep -v -e pgrep -e ci_install_gromacs || true
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
