# -*- mode: python ; coding: utf-8 -*-
"""Linux desktop bundle. Invoke via scripts/build_desktop.py."""
from pathlib import Path
import importlib.metadata
import importlib.util
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

ROOT = Path(SPECPATH).parent
STAGE = ROOT / "build" / "desktop-resources"

datas = [
    (str(ROOT / "workbench" / "static"), "workbench/static"),
    (str(ROOT / "workbench" / "data"), "workbench/data"),
    (str(ROOT / "evidence"), "evidence"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "docs" / "MANUAL.md"), "docs"),
    (str(ROOT / "scripts" / "engine_worker.py"), "scripts"),
    (str(ROOT / "requirements.lock.txt"), "."),
    (str(ROOT / "requirements.desktop.lock.txt"), "."),
    (str(STAGE / "scripts" / "polyply_cli.py"), "scripts"),
    (str(STAGE / "scripts" / "martinize2_cli.py"), "scripts"),
    (str(STAGE / "licenses"), "licenses"),
    (str(STAGE / "build-manifest.json"), "."),
]
datas += [(str(path), "workbench") for path in (ROOT / "workbench").glob("*.py")]
binaries = []
hiddenimports = [
    "workbench.__main__", "workbench.desktop", "workbench.server",
    "workbench.science", "workbench.external", "workbench.jobs",
    "workbench.research_api", "workbench.research_store", "workbench.descriptors",
    "workbench.ingestion", "workbench.learning", "workbench.campaigns",
    "workbench.bridges", "workbench.diagnostics", "workbench.forcefields", "workbench.mixture_analysis", "workbench.mcp_server", "workbench.sap", "workbench.protonation", "workbench.screening", "workbench.compatibility",
    "workbench.class2", "workbench.pcff", "workbench.crosslink", "workbench.engines", "workbench.repair",
    "workbench.mcp_http", "workbench.amorphous", "workbench.blends", "workbench.structure_analysis", "workbench.manual",
    "workbench.doe", "workbench.drive_backup", "workbench.process", "workbench.evaluation", "workbench.preflight", "workbench.glass_transition", "workbench.correlations", "workbench.reports",
    "workbench.execution", "workbench.compute_worker", "workbench.analysis_service", "workbench.workflows", "workbench.workflow_api", "workbench.autopilot", "workbench.sheets", "workbench.sheet_api", "workbench.target_spec", "workbench.target_api", "workbench.result_quality", "workbench.density_studies", "workbench.density_api",
    "matplotlib.backends.backend_agg", "matplotlib.backends.backend_svg",
    "_duckdb", "torch", "sklearn.gaussian_process", "sklearn.model_selection",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineCore",
    "PySide6.QtWebChannel", "PySide6.QtPrintSupport",
]

# The Rust kernel is optional: bundled when the build environment has it installed.
if importlib.util.find_spec("workbench_native") is not None:
    hiddenimports.append("workbench_native")
    datas += copy_metadata("workbench-native")

def production_module(name):
    return not any(part in {"tests", "test", "testing", "conftest"} for part in name.split("."))

# Scientific adapters import modules dynamically and generated worker scripts
# are retained as provenance. Data includes force fields and residue templates.
for package in (
    "rdkit", "MDAnalysis", "lammps", "radonpy", "pysimm", "polyply", "vermouth",
    "pysmiles", "cgsmiles", "packmol", "mdtraj", "duckdb", "pymbar", "pypdf",
    "baybe", "botorch", "gpytorch", "linear_operator", "cattrs", "propka", "pdb2pqr",
    "pybaselines", "pdbfixer", "openmm",
):
    spec = importlib.util.find_spec(package)
    if spec is None:
        raise RuntimeError(f"Required build dependency is missing: {package}")
    package_datas, package_binaries, package_imports = collect_all(
        package, include_py_files=True, filter_submodules=production_module,
        exclude_datas=["**/tests/**", "**/test/**", "**/__pycache__/**"],
    )
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports
    if package == "polyply":
        # polyply resolves TEST_DATA at import time, even outside its test suite.
        datas += collect_data_files(package, includes=["tests/test_data/**"])
    package_root = Path(spec.origin).parent
    for vendor_dir in package_root.parent.glob(package.lower() + ".libs"):
        binaries += [(str(path), vendor_dir.name) for path in vendor_dir.iterdir() if path.is_file()]

# Retain library and entry point metadata for version reports and plugin loads.
for distribution in (
    "numpy", "scipy", "rdkit", "MDAnalysis", "lammps", "radonpy-pypi",
    "pysimm", "packmol", "polyply", "vermouth", "pysmiles", "cgsmiles",
    "mpich", "mdtraj", "fastapi", "uvicorn", "pandas", "matplotlib", "pbr",
    "PySide6", "PySide6-Essentials", "PySide6-Addons", "shiboken6",
    "pyside6-webengine", "pyside6-pdf",
    "duckdb", "scikit-learn", "pymbar", "pypdf", "narwhals", "numexpr",
    "baybe", "botorch", "gpytorch", "linear_operator", "torch", "attrs", "cattrs", "propka", "pdb2pqr",
    "pybaselines", "jcamp", "pdbfixer", "OpenMM",
):
    try:
        datas += copy_metadata(distribution)
    except importlib.metadata.PackageNotFoundError:
        if distribution not in {"pyside6-webengine", "pyside6-pdf"}:
            raise

# The maintained torch hook retains source files needed by torch.jit/inspect,
# collects its native CPU libraries, and suppresses unsafe top-level symlinks.
# Keep that hook enabled instead of replacing it with a generic data copy.

lammps_root = Path(importlib.util.find_spec("lammps").origin).parent
packmol_root = Path(importlib.util.find_spec("packmol").origin).parent
binaries += [
    (str(lammps_root / "lmp"), "lammps"),
    (str(packmol_root / "binaries" / "packmol"), "packmol/binaries"),
    (str(Path(sys.prefix) / "lib" / "libmpi.so.12"), "."),
]

a = Analysis(
    [str(ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(ROOT), str(Path(importlib.util.find_spec("pysimm").origin).parent.parent)], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / "packaging" / "hooks")],
    hooksconfig={"matplotlib": {"backends": ["Agg"]}},
    excludes=["PyQt5", "PyQt6", "PySide2", "tkinter", "IPython", "pytest", "jupyter", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="PolymerLab",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PolymerLab")
