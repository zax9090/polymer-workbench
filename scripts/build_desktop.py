#!/usr/bin/env python3
"""Build a Linux desktop bundle plus portable archive and optional Debian package."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "build" / "desktop-resources"
DIST = ROOT / "dist" / "PolymerLab"
RELEASES = ROOT / "releases"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_resources():
    STAGE.mkdir(parents=True, exist_ok=True)
    (STAGE / "scripts").mkdir(exist_ok=True)
    shutil.copy2(Path(sys.executable).parent / "polyply", STAGE / "scripts" / "polyply_cli.py")
    shutil.copy2(Path(sys.executable).parent / "martinize2", STAGE / "scripts" / "martinize2_cli.py")
    import torch
    if torch.version.cuda is not None or not torch.__version__.endswith("+cpu"):
        raise RuntimeError("The desktop release requires the pinned CPU-only torch build; install requirements.desktop.lock.txt")
    license_dir = STAGE / "licenses"
    license_dir.mkdir(exist_ok=True)
    records = []
    for distribution in sorted(metadata.distributions(), key=lambda d: d.metadata.get("Name", "").lower()):
        name = distribution.metadata.get("Name", "unknown")
        record = {"name": name, "version": distribution.version,
                  "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"),
                  "project_urls": distribution.metadata.get_all("Project-URL") or [], "license_files": []}
        for file in distribution.files or []:
            if any(token in file.name.lower() for token in ("license", "copying", "notice")) and file.suffix.lower() not in {".py", ".pyc", ".so"}:
                source = distribution.locate_file(file)
                if source.is_file():
                    relative = Path(name) / str(file).replace("../", "_").lstrip("/")
                    target = license_dir / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    record["license_files"].append(str(relative))
        records.append(record)
    # Editable pysimm source has no recorded license files in its wheel metadata.
    pysimm_license = ROOT / "tools" / "engines" / "pysimm" / "LICENSE"
    if pysimm_license.is_file():
        (license_dir / "pysimm").mkdir(exist_ok=True)
        shutil.copy2(pysimm_license, license_dir / "pysimm" / "LICENSE")
    (license_dir / "python-distributions.json").write_text(json.dumps(records, indent=2))
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    manifest = {
        "application": "Polymer Lab", "version": version,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "machine": platform.machine(),
        "python": platform.python_version(), "libc": platform.libc_ver(),
        "bundler": "PyInstaller " + metadata.version("pyinstaller"),
        "bundle_kind": "onedir", "qt_sandbox_disabled": False,
        "optimization": {"backend": "BayBE", "torch_device": "cpu", "cuda_bundled": False},
        "rea": "Static source registration and audit evidence included; REA development tool is not bundled.",
        "not_bundled": ["AlphaFold/ColabFold model weights", "AlphaFold inference environment", "GROMACS", "HOOMD-blue", "Psi4", "REA node_modules"],
        "dependencies": [{"name": r["name"], "version": r["version"]} for r in records],
        "source_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in sorted((ROOT / "workbench").glob("*.py"))},
        "static_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in sorted((ROOT / "workbench" / "static").rglob("*")) if path.is_file()},
    }
    (STAGE / "build-manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def assemble_release(manifest, make_deb, archive=True):
    RELEASES.mkdir(exist_ok=True)
    for source, destination in (
        (ROOT / "packaging" / "portable-launch.sh", DIST / "launch.sh"),
        (ROOT / "install-desktop.sh", DIST / "install-desktop.sh"),
        (ROOT / "packaging" / "polyply-launch.sh", DIST / "bin" / "polyply"),
        (ROOT / "packaging" / "martinize-launch.sh", DIST / "bin" / "martinize2"),
        (ROOT / "assets" / "polymer-lab.png", DIST / "polymer-lab.png"),
        (ROOT / "assets" / "polymer-lab.svg", DIST / "polymer-lab.svg"),
        (ROOT / "docs" / "DESKTOP.md", DIST / "README-desktop.md"),
        (STAGE / "build-manifest.json", DIST / "build-manifest.json"),
    ):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    (DIST / ".polymer-lab-bundle").write_text("org.polymerlab.desktop\n")
    for path in (DIST / "launch.sh", DIST / "install-desktop.sh", DIST / "bin" / "polyply", DIST / "bin" / "martinize2"):
        path.chmod(0o755)
    (DIST / "THIRD-PARTY-NOTICES.txt").write_text(
        "Polymer Lab incorporates independently licensed components.\n"
        "See _internal/licenses/python-distributions.json and the associated license files.\n"
        "Qt/PySide6, RDKit, LAMMPS, MDAnalysis and the other scientific libraries retain their own licenses.\n"
        "Qt libraries are provided as replaceable shared files in _internal/PySide6.\n"
        "3Dmol.js license: _internal/workbench/static/vendor/3Dmol-LICENSE.txt.\n"
        "Source registrations and upstream URLs: _internal/evidence/repositories.json.\n"
        "Build recipe and application source are provided in the accompanying source archive.\n"
    )
    if not archive:
        print(f"Prepared runnable bundle: {DIST}", flush=True)
        return []
    version = manifest["version"]
    archive = RELEASES / f"PolymerLab-{version}-linux-x86_64.tar.gz"
    print(f"Creating {archive.name}", flush=True)
    with tarfile.open(archive, "w:gz", compresslevel=3) as tar:
        tar.add(DIST, arcname="PolymerLab")
    results = [archive]
    if make_deb:
        if not shutil.which("dpkg-deb"):
            raise RuntimeError("dpkg-deb is unavailable; omit --deb to produce only the portable archive")
        deb_root = ROOT / "build" / "debian-package"
        if deb_root.exists():
            shutil.rmtree(deb_root)
        (deb_root / "DEBIAN").mkdir(parents=True)
        app_target = deb_root / "opt" / "polymer-lab"
        shutil.copytree(DIST, app_target, symlinks=True)
        installed_kib = sum(p.stat().st_size for p in app_target.rglob("*") if p.is_file()) // 1024
        libc = manifest["libc"][1]
        (deb_root / "DEBIAN" / "control").write_text(
            f"Package: polymer-lab\nVersion: {version}\nSection: science\nPriority: optional\nArchitecture: amd64\n"
            "Maintainer: Polymer Lab local build <noreply@localhost>\n"
            f"Installed-Size: {installed_kib}\n"
            f"Depends: libc6 (>= {libc}), libnss3, libnspr4, libx11-6, libxcomposite1, libxdamage1, libxrandr2, libxkbcommon0, libxkbcommon-x11-0, libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libxcb-shape0, libxcb-xkb1, libasound2t64, libdbus-1-3, libgl1, libegl1\n"
            "Description: Polymer and protein research desktop workbench\n"
            " A local Qt desktop application with bundled molecular modelling tools.\n"
            " AlphaFold model weights and optional external compute engines are separate.\n"
        )
        applications = deb_root / "usr/share/applications"
        applications.mkdir(parents=True)
        (applications / "polymer-lab.desktop").write_text(
            "[Desktop Entry]\nVersion=1.0\nType=Application\nName=Polymer Lab\n"
            "Name[ko]=Polymer Lab 고분자 연구실\nComment=Polymer and protein research workflows\n"
            "Exec=/opt/polymer-lab/PolymerLab\nIcon=polymer-lab\nTerminal=false\n"
            "Categories=Science;Chemistry;\nStartupWMClass=polymer-lab\nStartupNotify=true\n"
        )
        icons = deb_root / "usr/share/icons/hicolor/512x512/apps"
        icons.mkdir(parents=True)
        shutil.copy2(ROOT / "assets/polymer-lab.png", icons / "polymer-lab.png")
        deb = RELEASES / f"polymer-lab_{version}_amd64.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zgzip", "-z3", "--build", str(deb_root), str(deb)], check=True)
        results.append(deb)
    checksum = RELEASES / "SHA256SUMS"
    checksum.write_text("".join(f"{sha256(p)}  {p.name}\n" for p in results))
    for result in results:
        print(f"Created {result} ({result.stat().st_size / 1024**2:.1f} MiB)", flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deb", action="store_true", help="Also create a Debian package; never installs it")
    parser.add_argument("--reuse-build", action="store_true", help="Repackage an existing dist/PolymerLab without running PyInstaller")
    parser.add_argument("--bundle-only", action="store_true", help="Prepare dist/PolymerLab for validation before creating archives")
    args = parser.parse_args()
    if sys.platform != "linux" or platform.machine() != "x86_64":
        parser.error("This build recipe currently supports Linux x86_64 only")
    if args.reuse_build:
        manifest = json.loads((DIST / "_internal" / "build-manifest.json").read_text())
    else:
        metadata.version("pyinstaller")
        manifest = prepare_resources()
        log_path = ROOT / "build" / "desktop-build.log"
        print(f"Building Polymer Lab {manifest['version']}; log: {log_path}", flush=True)
        with log_path.open("w") as log:
            subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                            "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "pyinstaller"),
                            str(ROOT / "packaging" / "polymer-lab.spec")],
                           cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    bundled_hashes = {**manifest.get("source_sha256", {}), **manifest.get("static_sha256", {})}
    for relative, expected in bundled_hashes.items():
        bundled = DIST / "_internal" / relative
        if not bundled.is_file() or sha256(bundled) != expected:
            raise RuntimeError(f"Bundled source differs from the build manifest: {relative}")
        if not args.reuse_build and sha256(ROOT / relative) != expected:
            raise RuntimeError(f"Source changed during the build; rebuild before release: {relative}")
    assemble_release(manifest, args.deb, archive=not args.bundle_only)


if __name__ == "__main__":
    main()
