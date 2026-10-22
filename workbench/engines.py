"""Pinned, hash-verified third-party engine releases (EMC, xtb); never an unpinned 'latest'.

The archives are downloaded once from the release recorded here, verified by SHA-256, unpacked
with path and link checks, and cached next to the job folders.  An existing reviewed installation
can be used instead through EMC_ROOT / XTB_EXEC.  tools/rea/artifact_catalog.json records the REA
inspection of the same archives.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tarfile
import urllib.request
import uuid

from .paths import default_jobs_root

ENGINES = {
    "emc": {
        "name": "EMC (Enhanced Monte Carlo)", "version": "9.4.4",
        "url": "https://sourceforge.net/projects/montecarlo/files/emc_linux_x86_64_v9.4.4_20260701.tgz/download",
        "archive": "emc_linux_x86_64_v9.4.4_20260701.tgz",
        "sha256": "97f8debaa50e5ea50500905c084e9947203a8853b6b09f594c29769b9826c904",
        "root": "v9.4.4", "executable": "bin/emc_linux_x86_64", "license": "GPL-3.0 (LICENSE shipped in the archive)",
        "citation": "P.J. in 't Veld and G.C. Rutledge, Macromolecules 36, 7358 (2003)",
        "note": "The download page states this build is valid until 2027-07-01.",
        "environment": "EMC_ROOT",
    },
    "xtb": {
        "name": "xtb", "version": "6.7.1",
        "url": "https://github.com/grimme-lab/xtb/releases/download/v6.7.1/xtb-6.7.1-linux-x86_64.tar.xz",
        "archive": "xtb-6.7.1-linux-x86_64.tar.xz",
        "sha256": "62a8d18778286e815292ee53d76ce447daf460a4dea3782c0f25cbac7019b5df",
        "root": "xtb-dist", "executable": "bin/xtb", "license": "LGPL-3.0-or-later",
        "citation": "C. Bannwarth, S. Ehlert, S. Grimme, J. Chem. Theory Comput. 15, 1652 (2019) (GFN2-xTB)",
        "environment": "XTB_EXEC",
    },
}


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _cache(name, spec):
    return default_jobs_root().parent / "engines" / f"{name}-{spec['version']}"


def _record(name, spec, source, root):
    return {"engine": name, "name": spec["name"], "version": spec["version"], "license": spec["license"],
            "citation": spec["citation"], "archive": spec["archive"], "archive_sha256": spec["sha256"],
            "url": spec["url"], "source": source, "root": str(root), "note": spec.get("note")}


def _configured(name, spec):
    value = os.environ.get(spec["environment"])
    if not value:
        return None
    path = Path(value).expanduser()
    if name == "emc":
        return path if (path / spec["executable"]).is_file() else None
    return path.parent.parent if path.is_file() else None


def status(name):
    """Discovery only: report a usable installation without downloading anything."""
    spec = ENGINES[name]
    configured = _configured(name, spec)
    if configured:
        return {"available": True, **_record(name, spec, spec["environment"], configured)}
    cache = _cache(name, spec) / spec["root"]
    if (cache / spec["executable"]).is_file() and (cache.parent / ".verified").is_file():
        return {"available": True, **_record(name, spec, "cache", cache)}
    return {"available": False, **_record(name, spec, None, cache),
            "detail": f"Downloaded from the pinned {spec['name']} {spec['version']} release on first use"}


def _extract(archive, destination):
    """Unpack regular files and in-tree links; links that point outside the archive are skipped."""
    skipped = []

    def guarded(member, path):
        try:
            return tarfile.data_filter(member, path)
        except tarfile.FilterError:
            skipped.append({"member": member.name, "link": member.linkname})
            return None

    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter=guarded)
    return skipped


def ensure(name, download=True):
    """Return (installation root, provenance record) for a verified engine."""
    spec = ENGINES[name]
    configured = _configured(name, spec)
    if configured:
        return configured, _record(name, spec, spec["environment"], configured)
    cache = _cache(name, spec)
    root = cache / spec["root"]
    if (root / spec["executable"]).is_file() and (cache / ".verified").is_file():
        return root, _record(name, spec, "cache", root)
    if not download:
        raise FileNotFoundError(f"{spec['name']} {spec['version']} is not installed")
    cache.parent.mkdir(parents=True, exist_ok=True)
    staging = cache.parent / f".download-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        archive = staging / spec["archive"]
        request = urllib.request.Request(spec["url"], headers={"User-Agent": "PolymerLab/0.3"})
        with urllib.request.urlopen(request, timeout=300) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        if _sha256(archive) != spec["sha256"]:
            raise ValueError(f"Downloaded {spec['archive']} does not match the pinned SHA-256")
        unpacked = staging / "unpacked"
        unpacked.mkdir()
        skipped = _extract(archive, unpacked)
        if not (unpacked / spec["root"] / spec["executable"]).is_file():
            raise ValueError(f"{spec['archive']} does not contain {spec['root']}/{spec['executable']}")
        (unpacked / ".verified").write_text(f"{spec['sha256']}\nskipped links: {skipped}\n")
        if cache.exists():
            shutil.rmtree(cache)
        unpacked.rename(cache)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return root, _record(name, spec, "downloaded", root)


def executable(name, download=True):
    root, record = ensure(name, download)
    return root / ENGINES[name]["executable"], record
