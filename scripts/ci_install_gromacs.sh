#!/usr/bin/env bash
# CI: install GROMACS from the Ubuntu archive without letting one slow mirror stall the job.
#
# Earlier runs timed out while apt fetched ~110 MB from azure.archive.ubuntu.com at ~60 KB/s
# (rarely used universe packages are not cached at that mirror). This script
# (1) installs from cached .deb files, without network access, when they still fit the runner image;
#     a cache from an older image is kept, so only packages that changed are downloaded again,
# (2) refreshes only the Ubuntu archive sources, never third-party runner repositories,
# (3) downloads every missing .deb itself, in parallel, from the runner's mirror list and then
#     archive.ubuntu.com, abandoning a mirror whose transfer stays below MIN_SPEED for SLOW_SECONDS,
# (4) checks every downloaded or cached file against the hash in the signed index before apt
#     installs it (apt itself trusts any cached file of the right size).
# Usage: ci_install_gromacs.sh <directory for cached .deb files>
#   For testing: CI_APT_PACKAGE (package), CI_APT_MIRROR_FILE (mirror list), CI_APT_MIRRORS (fallback mirrors),
#   CI_MIN_SPEED / CI_SLOW_SECONDS (slow-mirror threshold).
set -euo pipefail

DEBS="${1:?cache directory required}"
PACKAGE="${CI_APT_PACKAGE:-gromacs}"
MIN_SPEED="${CI_MIN_SPEED:-250000}"   # bytes/s
SLOW_SECONDS="${CI_SLOW_SECONDS:-15}"
mkdir -p "$DEBS/partial"
export DEBIAN_FRONTEND=noninteractive

installed() {
  # sed reads the whole output, so gmx never dies of SIGPIPE under pipefail.
  if [ "$PACKAGE" = gromacs ]; then gmx --version 2>/dev/null | sed -n 1p
  else dpkg-query -W -f='${Status}' "$PACKAGE" 2>/dev/null | grep -q 'install ok installed'; fi
}

if installed; then
  echo "$PACKAGE is already installed"
  exit 0
fi

if compgen -G "$DEBS/*.deb" >/dev/null; then
  echo "Installing $PACKAGE from $(ls "$DEBS"/*.deb | wc -l) cached packages"
  # With no sources configured apt sees only dpkg's status and these files, so it cannot fall back to a
  # (slow) mirror: a package of the same version in the image's lists would otherwise be downloaded again.
  NO_SOURCES="$(mktemp -d)"
  if sudo timeout 300 apt-get -o Dir::Etc::sourcelist=/dev/null -o Dir::Etc::sourceparts="$NO_SOURCES" \
       -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= -o DPkg::Lock::Timeout=120 \
       install -y --no-install-recommends "$DEBS"/*.deb && installed; then
    exit 0
  fi
  # Keep the files: apt reuses every cached package whose hash still matches the new index.
  echo "Cached packages do not fit this image; refreshing from the Ubuntu archive" >&2
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

# Mirrors in the order apt would use them, then the primary archive as a fallback.
MIRRORS=()
MIRROR_FILE="${CI_APT_MIRROR_FILE:-/etc/apt/apt-mirrors.txt}"
if [ -f "$MIRROR_FILE" ]; then
  while read -r url _; do
    case "$url" in http://*|https://*) MIRRORS+=("${url%/}/") ;; esac
  done < "$MIRROR_FILE"
fi
while read -r url; do
  case "$url" in http://*|https://*) MIRRORS+=("${url%/}/") ;; esac
done < <(grep -h -o -E '(URIs:[[:space:]]*|deb[[:space:]]+)[^[:space:]]+' /etc/apt/sources.list "$PARTS"/* 2>/dev/null | awk '{print $NF}')
read -r -a FALLBACK <<< "${CI_APT_MIRRORS:-http://archive.ubuntu.com/ubuntu/}"
MIRRORS+=("${FALLBACK[@]}")
mapfile -t MIRRORS < <(printf '%s\n' "${MIRRORS[@]}" | awk '!seen[$0]++')
echo "Download mirrors: ${MIRRORS[*]}"

# apt trusts any file of the right size in its archive cache without checking the hash, so cached
# files are held aside, every needed file is listed with its index hash ('URI' file size hash),
# and only cached files that match are put back.
sudo chown -R "$(id -u):$(id -g)" "$DEBS"
STASH="$(mktemp -d)"
find "$DEBS" -maxdepth 1 -name '*.deb' -exec mv -t "$STASH" {} +
LIST="$(mktemp)"
sudo apt-get "${OPTS[@]}" -o Dir::Cache::archives="$DEBS" install -y --no-install-recommends --print-uris -qq "$PACKAGE" \
  | sed -n "s/^'\([^']*\)' \([^ ]*\) \([0-9]*\) \([^ ]*\)$/\1 \2 \3 \4/p" > "$LIST"
[ -s "$LIST" ] || echo "apt-get --print-uris listed no packages; apt downloads them itself" >&2

verified() {  # file size hash
  local tool
  case "$3" in
    SHA512:*) tool=sha512sum ;; SHA256:*) tool=sha256sum ;; SHA1:*) tool=sha1sum ;; MD5Sum:*) tool=md5sum ;; *) return 1 ;;
  esac
  [ "$(stat -c %s "$1")" = "$2" ] && [ "$($tool "$1" | cut -d' ' -f1)" = "${3#*:}" ]
}

fetch() {  # uri file size hash
  local uri=$1 name=$2 size=$3 hash=$4 path mirror
  path="pool/${uri#*/pool/}"
  for mirror in $FETCH_MIRRORS; do
    if curl -fsS -L --connect-timeout 15 --max-time 600 --speed-limit "$MIN_SPEED" --speed-time "$SLOW_SECONDS" \
         -o "$DEBS/partial/$name" "$mirror$path"; then
      if verified "$DEBS/partial/$name" "$size" "$hash"; then
        mv "$DEBS/partial/$name" "$DEBS/$name"
        echo "  $name from $mirror"
        return 0
      fi
      echo "  $name from $mirror does not match the index; trying the next mirror" >&2
    else
      echo "  $name: $mirror failed or was slower than $MIN_SPEED B/s for ${SLOW_SECONDS}s" >&2
    fi
    rm -f "$DEBS/partial/$name"
  done
  echo "  $name: no mirror delivered it; apt will retry" >&2
}

MISSING="$(mktemp)"
reused=0
while read -r uri name size hash; do
  if [ -f "$STASH/$name" ] && verified "$STASH/$name" "$size" "$hash"; then
    mv "$STASH/$name" "$DEBS/$name"
    reused=$((reused + 1))
  else
    echo "$uri $name $size $hash" >> "$MISSING"
  fi
done < "$LIST"
echo "$(wc -l < "$LIST") packages needed: $reused verified from the cache, $(wc -l < "$MISSING") to download ($(awk '{s+=$3} END {printf "%.1f", s/1e6}' "$MISSING") MB)"

export -f fetch verified
export DEBS MIN_SPEED SLOW_SECONDS
export FETCH_MIRRORS="${MIRRORS[*]}"
xargs -r -P 6 -L 1 bash -c 'fetch "$@"' _ < "$MISSING"

# Whatever no mirror delivered is downloaded by apt itself.
sudo timeout 600 apt-get "${OPTS[@]}" -o Dir::Cache::archives="$DEBS" install -y --no-install-recommends "$PACKAGE"
sudo rm -rf "$DEBS/partial" "$DEBS/lock"
sudo chown -R "$(id -u):$(id -g)" "$DEBS"
installed
