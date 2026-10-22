"""Pinned, hash-verified force-field parameter files; never an unpinned upstream branch.

The Martini 3 general parameters (16 MB) are not stored in the repository. They are
downloaded once from the audited upstream commit, verified byte-for-byte, and cached.
A reviewed local copy can be supplied with MARTINI3_FF_DIR; it must match the pins.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import urllib.request
import uuid

from .paths import default_jobs_root, resource_root

MARTINI3 = {
    "name": "Martini 3.0.0",
    "repository": "marrink-lab/martini-forcefields",
    "commit": "784591ebdc91d762ed4df986c4650546c938f776",
    "license": "Apache-2.0",
    "citation": "P.C.T. Souza et al., Nat. Methods 18, 382-388 (2021), doi:10.1038/s41592-021-01098-3",
    "files": {
        "martini_v3.0.0.itp": ("martini_forcefields/regular/v3.0.0/gmx_files/martini_v3.0.0.itp",
                               "0ecbf0ff334b2ec21dc5e3151aca42167b977bc2aed41eb2a44bd8c997754894"),
        "martini_v3.0.0_solvents_v1.itp": ("martini_forcefields/regular/v3.0.0/gmx_files/martini_v3.0.0_solvents_v1.itp",
                                           "e6a78414b317c38a1a816eded4aee6082a5da1ed229a126359f0cc92314dcb4e"),
        "martini_v3.0.0_ions_v1.itp": ("martini_forcefields/regular/v3.0.0/gmx_files/martini_v3.0.0_ions_v1.itp",
                                       "699aba132755b0ef13a0d7b41e6230a27777781bdc8075507413b89499c2be4f"),
        "LICENSE": ("LICENSE", "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"),
    },
}


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _url(spec, upstream_path):
    return f"https://raw.githubusercontent.com/{spec['repository']}/{spec['commit']}/{upstream_path}"


def _cache(spec):
    return default_jobs_root().parent / "forcefields" / "martini3" / spec["commit"]


def _verified(directory, spec):
    """Return None if every pinned file matches, otherwise the first problem."""
    for name, (_, expected) in spec["files"].items():
        path = Path(directory) / name
        if path.is_symlink() or not path.is_file():
            return f"{name} is missing"
        if _sha256(path) != expected:
            return f"{name} differs from the pinned {spec['name']} release"
    return None


def martini3_status(spec=MARTINI3):
    """Discovery only: report a verified local copy without downloading anything."""
    configured = os.environ.get("MARTINI3_FF_DIR")
    candidates = [("MARTINI3_FF_DIR", Path(configured).expanduser())] if configured else []
    candidates += [("bundled", resource_root() / "forcefields" / "martini3"), ("cache", _cache(spec))]
    for source, directory in candidates:
        if directory.is_dir() and _verified(directory, spec) is None:
            return {"available": True, "source": source, "path": str(directory), "commit": spec["commit"]}
    return {"available": False, "source": None, "path": None, "commit": spec["commit"],
            "detail": "Downloaded from the pinned upstream commit on first Martini 3 mixture simulation"}


def martini3_parameters(download=True, spec=MARTINI3):
    """Return a directory with verified Martini 3 files and its provenance record."""
    record = {"name": spec["name"], "repository": spec["repository"], "commit": spec["commit"],
              "license": spec["license"], "citation": spec["citation"],
              "files": {name: {"upstream_path": path, "sha256": sha, "url": _url(spec, path)}
                        for name, (path, sha) in spec["files"].items()}}
    configured = os.environ.get("MARTINI3_FF_DIR")
    if configured:
        directory = Path(configured).expanduser().resolve()
        problem = _verified(directory, spec)
        if problem:
            raise ValueError(f"MARTINI3_FF_DIR: {problem}; update the reviewed pin instead of mixing versions")
        return directory, {**record, "source": "MARTINI3_FF_DIR"}
    bundled = resource_root() / "forcefields" / "martini3"
    if bundled.is_dir() and _verified(bundled, spec) is None:
        return bundled, {**record, "source": "bundled"}
    cache = _cache(spec)
    if cache.is_dir() and _verified(cache, spec) is None:
        return cache, {**record, "source": "cache"}
    if not download:
        raise FileNotFoundError(f"Verified {spec['name']} parameters are not available locally")
    cache.parent.mkdir(parents=True, exist_ok=True)
    staging = cache.parent / f".download-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for name, (upstream_path, expected) in spec["files"].items():
            request = urllib.request.Request(_url(spec, upstream_path), headers={"User-Agent": "PolymerLab/0.3"})
            with urllib.request.urlopen(request, timeout=60) as response, (staging / name).open("wb") as stream:
                shutil.copyfileobj(response, stream)
            if _sha256(staging / name) != expected:
                raise ValueError(f"Downloaded {name} does not match the pinned SHA-256")
        if cache.exists():
            shutil.rmtree(cache)
        staging.rename(cache)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return cache, {**record, "source": "downloaded"}


def link_into(directory, destination, names):
    """Place verified files in a job folder; hard links avoid 16 MB copies per job."""
    for name, target_name in names.items():
        target = Path(destination) / target_name
        target.unlink(missing_ok=True)
        try:
            os.link(Path(directory) / name, target)
        except OSError:
            shutil.copyfile(Path(directory) / name, target)
