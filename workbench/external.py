"""Explicit adapters, dependency preflight, and inspectable handoff artifacts.

External executables are selected through deployment environment variables,
never shell fragments supplied by a browser client. Every process uses argv.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

import numpy as np
from workbench.science import build_linear_polymer, write_json
from workbench.paths import runtime_environment, script_command, subprocess_environment


def executable(name, env):
    process_env = runtime_environment()
    configured = process_env.get(env)
    if configured:
        return shutil.which(configured)
    sibling = Path(sys.executable).parent / name
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which(name, path=process_env.get("PATH"))


def available(module):
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def get_capabilities():
    modules = {name: available(name) for name in ("numpy", "rdkit", "MDAnalysis", "lammps", "radonpy", "pysimm", "hoomd")}
    commands = {name: executable(name, env) for name, env in (("packmol", "PACKMOL_EXEC"), ("polyply", "POLYPLY_EXEC"), ("gmx", "GROMACS_EXEC"), ("colabfold_batch", "COLABFOLD_EXEC"), ("lmp", "LAMMPS_EXEC"))}
    return {"modules": modules, "executables": commands,
            "routes": {"polymer_cg": "available" if modules["numpy"] else "blocked",
                       "polymer_atomistic": "available" if modules["rdkit"] else "blocked",
                       "protein": "export/database/import available; prediction requires ColabFold",
                       "packmol": "available" if commands["packmol"] else "blocked",
                       "polyply": "available" if commands["polyply"] else "blocked",
                       "pysimm": "available" if modules["pysimm"] and commands["lmp"] else "blocked",
                       "hoomd": "available" if modules["hoomd"] else "blocked"},
            "note": "Module detection is not a successful simulation; job preflight checks load/runtime dependencies."}


def outcome(output, route, status, message, **kwargs):
    return {"status": status, "route": route, "message": message,
            "artifacts": [p.name for p in output.iterdir() if p.is_file()], **kwargs}


def blocked(output, route, missing, message, **kwargs):
    write_json(output / "requirements.json", {"missing": missing, "message": message})
    return outcome(output, route, "blocked", message, missing=missing, **kwargs)


def run_command(argv, output, logname, stdin=None, timeout=7200, seed=None):
    process_env = subprocess_environment(str(argv[0]))
    process_env.setdefault("LAMMPS_EXEC", executable("lmp", "LAMMPS_EXEC") or "lmp")
    if seed is not None:
        process_env["PYTHONHASHSEED"] = str(seed)
    # PACKMOL rewinds stdin; an anonymous regular file is required, not a pipe.
    with (output / logname).open("w") as log, tempfile.TemporaryFile(mode="w+") as input_stream:
        if stdin is not None:
            input_stream.write(stdin)
            input_stream.seek(0)
        subprocess.run([str(a) for a in argv], stdin=input_stream if stdin is not None else subprocess.DEVNULL,
                       text=True, cwd=output, stdout=log, stderr=subprocess.STDOUT,
                       check=True, timeout=timeout, env=process_env)


def molecule_artifacts(config, output, emit):
    from rdkit import Chem
    emit("build", 0.15, "RDKit: head-to-tail assembly and 3D conformer generation")
    mol, metadata = build_linear_polymer(config["repeat_smiles"], config["monomers"], config["seed"])
    Chem.MolToPDBFile(mol, str(output / "structure.pdb"))
    Chem.MolToMolFile(mol, str(output / "structure.mol"))
    with Chem.SDWriter(str(output / "structure.sdf")) as writer:
        writer.write(mol)
    write_json(output / "molecule.json", metadata)
    coords = mol.GetConformer().GetPositions()
    write_json(output / "trajectory.json", {"unit": "angstrom", "kind": "atomistic", "frames": [{"step": 0, "positions": coords.round(6).tolist()}],
               "elements": [a.GetSymbol() for a in mol.GetAtoms()], "bonds": [[b.GetBeginAtomIdx(), b.GetEndAtomIdx()] for b in mol.GetBonds()]})
    return mol, metadata


RADONPY_SCRIPT = '''import json, os, random, shlex
from pathlib import Path
import numpy as np
from rdkit import Chem
from radonpy.ff.gaff2_mod import GAFF2_mod
from radonpy.sim import md, lammps
config=json.loads(Path("config.json").read_text())
seed=int(config["seed"])
random.seed(seed)
np.random.seed(seed)
temperature=float(config.get("temperature_kelvin",300.0))
mol=Chem.MolFromMolFile("structure.mol",removeHs=False)
ff=GAFF2_mod()
if not ff.ff_assign(mol,charge="gasteiger"):
    raise RuntimeError("GAFF2_mod parameter or Gasteiger charge assignment failed")
solver_path=os.environ.get("LAMMPS_EXEC")
solver=lammps.LAMMPS(work_dir=str(Path.cwd()),solver_path=shlex.quote(solver_path) if solver_path and os.name=="posix" else solver_path)
protocol=md.MD(mol=mol)
protocol.pbc=False
protocol.boundary="f f f"
protocol.dump_freq=max(1,config["steps"]//100)
protocol.thermo_freq=max(1,config["steps"]//100)
protocol.set_init_velocity=temperature
protocol.add_min(maxiter=1000,maxeval=10000)
protocol.add_md("nvt",config["steps"],time_step=0.5,t_start=temperature,t_stop=temperature)
# Let the explicitly seeded LAMMPS velocity command initialize momenta once.
solver.make_dat(mol,velocity=False)
result=solver.run(protocol,mol=mol,omp=1,mpi=0,gpu=0)
if result is None:
    raise RuntimeError("RadonPy did not return a final structure")
Chem.MolToPDBFile(result,"structure.pdb")
Chem.MolToMolFile(result,"final.mol")
actual_seeds=[]
for line in Path(solver.input_file).read_text().splitlines():
    tokens=line.split()
    if len(tokens)>4 and tokens[0]=="velocity" and tokens[2]=="create":
        actual_seeds.append({"seed":int(tokens[4]),"command":line.strip()})
    elif len(tokens)>7 and tokens[0]=="fix" and tokens[3] in ("langevin","temp/csvr","temp/csld"):
        actual_seeds.append({"seed":int(tokens[7]),"command":line.strip()})
seed_record={"configured":seed,"numpy_legacy":seed,"python_random":seed,"python_hash":os.environ.get("PYTHONHASHSEED"),"lammps":actual_seeds,"scope":"same engine versions, serial environment and numerical settings; not a cross-platform bitwise guarantee"}
Path("random_seeds.json").write_text(json.dumps(seed_record,indent=2))
Path("radonpy_complete.json").write_text(json.dumps({"force_field":"GAFF2_mod","charge_method":"Gasteiger","ensemble":"isolated-chain NVT","temperature_kelvin":temperature,"time_step_fs":0.5,"steps":config["steps"],"equilibrated":False,"random_seeds":seed_record}))
'''


RADONPY_BULK_SCRIPT = """import contextlib, io, json, os, random, shlex
from pathlib import Path
import numpy as np
from rdkit import Chem
from radonpy.core import poly, utils
from radonpy.ff.gaff2_mod import GAFF2_mod
from radonpy.sim import lammps
from radonpy.sim.preset import eq
config = json.loads(Path("config.json").read_text())
seed = int(config["seed"])
random.seed(seed)
np.random.seed(seed)
temp, scale, chains = float(config.get("temperature_kelvin", 300.0)), float(config.get("protocol_scale", 1.0)), int(config["chains"])
threads = int(os.environ.get("POLYMER_MD_THREADS") or min(4, os.cpu_count() or 1))
mol = Chem.MolFromMolFile("structure.mol", removeHs=False)
ff = GAFF2_mod()
xtb = json.loads(Path("xtb_engine.json").read_text()) if Path("xtb_engine.json").is_file() else None
if not ff.ff_assign(mol, charge=None if xtb else "gasteiger"):
    raise RuntimeError("GAFF2_mod parameter or Gasteiger charge assignment failed")
if xtb:
    # GFN1-xTB CM5 charges of the whole capped chain at its RDKit geometry (single point).
    import subprocess
    Chem.MolToXYZFile(mol, "xtb_chain.xyz")
    total = Chem.GetFormalCharge(mol)
    env = {**os.environ, "XTBPATH": xtb["parameters"], "OMP_NUM_THREADS": str(min(4, os.cpu_count() or 1)), "OMP_STACKSIZE": "1G"}
    with open("xtb.log", "w") as log:
        subprocess.run([xtb["executable"], "xtb_chain.xyz", "--gfn", "1", "--sp", "--chrg", str(total)], stdout=log, stderr=subprocess.STDOUT, env=env, check=True, timeout=3600)
    lines = Path("xtb.log").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if "Mulliken/CM5 charges" in line) + 1
    charges = [float(line.split()[2]) for line in lines[start:start + mol.GetNumAtoms()]]
    if len(charges) != mol.GetNumAtoms() or abs(sum(charges) - total) > 0.01:
        raise RuntimeError("xtb CM5 charges do not match the chain atoms or its formal charge")
    residual = (total - sum(charges)) / len(charges)
    for atom, q in zip(mol.GetAtoms(), charges):
        atom.SetDoubleProp("AtomicCharge", q + residual)
if mol.GetNumAtoms() * chains > 30000:
    raise ValueError("This desktop bulk route is limited to 30000 atoms per cell")
# Smaller periodic cells let bonded terms span half the box and corrupt the sampling.
if mol.GetNumAtoms() * chains < 200:
    raise ValueError(f"A {mol.GetNumAtoms() * chains}-atom cell is too small for periodic bulk sampling; use at least 200 atoms (more chains or longer chains)")
native = config.get("cell_builder", "radonpy") == "native"
if native:
    # Polymer Lab's own builder (native_amorphous.py, copied into this job): chains grown at the target density.
    from rdkit import Geometry
    import native_amorphous
    positions, edge, builder_report = native_amorphous.build_cell(mol, chains, float(config.get("build_density", 0.8)), seed=seed)
    cell = Chem.Mol()
    for _ in range(chains):
        cell = poly.combine_mols(cell, mol)
    conformer = cell.GetConformer(0)
    for i, (x, y, z) in enumerate(positions):
        conformer.SetAtomPosition(i, Geometry.Point3D(float(x), float(y), float(z)))
    setattr(cell, "cell", utils.Cell(edge, 0.0, edge, 0.0, edge, 0.0))
else:
    builder_report = {"builder": "RadonPy amorphous_cell (rigid copies at 0.05 g/cm^3)"}
    cell = poly.amorphous_cell(mol, chains, density=0.05)
solver_path = os.environ.get("LAMMPS_EXEC")
solver_path = shlex.quote(solver_path) if solver_path and os.name == "posix" else solver_path
work = str(Path.cwd())
pre = eq.EQ21step(cell, work_dir=work, solver_path=solver_path)
lmp = lammps.LAMMPS(work_dir=work, solver_path=solver_path)
utils.MolToPDBFile(pre.mol, pre.pdb_file)
lmp.make_dat(pre.mol, file_name=pre.dat_file1)
# RadonPy's stage definitions are used unchanged; only their lengths are multiplied by scale.
def scaled(md):
    total = 0
    for stage in md.wf:
        if getattr(stage, "type", "") == "md":
            stage.step = max(100, int(round(stage.step * scale)))
            total += stage.step
    md.thermo_freq = max(10, min(1000, total // 200))
    md.dump_freq = max(10, min(1000, total // 50))
    return total
md1 = pre.packing(f_density=0.8)
if native:
    # The cell is already dense: keep RadonPy's minimisation and 0.1 fs NVT, then one 1 fs NVT at the
    # sampling temperature in place of its heating to 700 K and compression from 0.05 g/cm^3.
    md1.wf = md1.wf[:3]
    md1.wf[2].step, md1.wf[2].t_start, md1.wf[2].t_stop, md1.wf[2].deform = 100000, temp, temp, None
packing_steps = scaled(md1)
pre.mol = lmp.run(md1, mol=pre.mol, input_file=pre.in_file1, last_str=pre.last_str1, last_data=pre.last_data1, omp=threads, mpi=0, gpu=0)
step_list = [[max(100, int(round(n * scale))) for n in row] for row in
             [[50000, 50000, 50000], [50000, 100000, 50000], [50000, 100000, 50000], [50000, 100000, 5000],
              [5000, 10000, 5000], [5000, 10000, 5000], [5000, 10000, 800000]]]
md2 = pre.eq21step(max_temp=600.0, temp=temp, press=1.0, max_press=50000, step_list=step_list, time_step=1.0, set_init_velocity=True)
md2.thermo_freq = max(10, min(1000, sum(map(sum, step_list)) // 200))
md2.dump_freq = max(10, min(1000, sum(map(sum, step_list)) // 50))
pre.mol = lmp.run(md2, mol=pre.mol, input_file=pre.in_file2, last_str=pre.last_str2, last_data=pre.last_data2, omp=threads, mpi=0, gpu=0)
# The MSD fix averages over thermo_freq steps, so the run length must be a multiple of it.
thermo = max(10, min(1000, int(5000000 * scale) // 400))
sampling_steps = thermo * max(400, int(5000000 * scale) // thermo)
md3 = pre.sampling(temp=temp, press=1.0, step=sampling_steps)
md3.thermo_freq, md3.dump_freq, md3.wf[-1].rg_ave_length = thermo, thermo * 4, thermo
pre.mol = lmp.run(md3, mol=pre.mol, input_file=pre.in_file, last_str=pre.last_str, last_data=pre.last_data, omp=threads, mpi=0, gpu=0)
analysis = pre.analyze()
if not analysis.dfs:
    warnings = sorted({line.strip() for line in Path(pre.log_file).read_text(errors="replace").splitlines() if line.startswith("WARNING")})
    raise RuntimeError("RadonPy could not parse the sampling log. LAMMPS warnings: " + " | ".join(warnings[:5]))
rows = len(analysis.dfs[-1])
window = max(2, int(0.4 * rows))
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    values = analysis.get_all_prop(temp=temp, press=1.0, init=window, width=window, f_width=window, do_traj=False)
    criteria_met = bool(analysis.check_eq())
print(captured.getvalue())
def number(key, factor=1.0):
    value = values.get(key)
    return float(value) * factor if value is not None and np.isfinite(value) else None
properties = {"density": {"value": number("density"), "unit": "g/cm^3"},
              "chain_rg": {"value": number("Rg"), "unit": "angstrom"},
              "heat_capacity_cp": {"value": number("Cp"), "unit": "J/(kg K)"},
              "isothermal_compressibility": {"value": number("compressibility", 1e9), "unit": "1/GPa"},
              "bulk_modulus": {"value": number("bulk_modulus", 1e-9), "unit": "GPa"},
              "volume_expansion": {"value": number("volume_expansion"), "unit": "1/K"},
              "self_diffusion": {"value": number("self-diffusion"), "unit": "m^2/s"}}
density_series = [float(v) for v in analysis.dfs[-1]["Density"].to_numpy()]
utils.MolToPDBFile(pre.mol, "structure.pdb")
seeds = []
for name in (pre.in_file1, pre.in_file2, pre.in_file):
    for line in Path(name).read_text().splitlines():
        tokens = line.split()
        if len(tokens) > 4 and tokens[0] == "velocity" and tokens[2] == "create":
            seeds.append({"input": name, "seed": int(tokens[4])})
complete = {"force_field": "GAFF2_mod", "charge_method": "CM5 (GFN1-xTB single point)" if xtb else "Gasteiger", "xtb": xtb, "ensemble": "NPT sampling after Larsen 21-step compression/decompression",
            "protocol": ("Polymer Lab native cell, minimisation + NVT, then RadonPy EQ21step 21-step and sampling" if native
                         else "RadonPy EQ21step stages (packing, 21-step, sampling)"), "protocol_scale": scale, "builder": builder_report,
            "stage_steps": {"packing": packing_steps, "eq21": sum(map(sum, step_list)), "sampling": sampling_steps},
            "sampling_thermo_freq": thermo, "time_step_fs": 1.0, "temperature_kelvin": temp, "pressure_atm": 1.0, "max_temp_kelvin": 600.0, "max_press_atm": 50000,
            "chains": chains, "atoms": pre.mol.GetNumAtoms(), "threads": threads, "properties": properties,
            "analysis_window_rows": {"rows": rows, "init": window, "width": window}, "radonpy_criteria_met": criteria_met,
            "convergence_warnings": [line.split("RadonPy warning:", 1)[1].strip() for line in captured.getvalue().splitlines() if "RadonPy warning:" in line],
            "density_series": density_series[-2000:], "lammps_velocity_seeds": seeds, "equilibrated": False}
Path("bulk_complete.json").write_text(json.dumps(complete, indent=2))
"""


def atomistic(config, output, emit):
    mode = config.get("atomistic_mode", "build")
    if mode == "crosslink":
        return cure_cell(config, output, emit)
    if mode == "tg_scan":
        return tg_scan(config, output, emit)
    mol, metadata = molecule_artifacts(config, output, emit)
    if mode == "build":
        return outcome(output, "polymer_atomistic", "prepared", "RDKit polymer geometry prepared; molecular dynamics has not been run.", engine="RDKit", metadata=metadata, warnings=metadata["warnings"])
    if mode == "bulk":
        return bulk_cell(config, output, emit, metadata)
    if mode != "simulate":
        raise ValueError("atomistic_mode must be build, simulate or bulk")
    (output / "run_radonpy.py").write_text(RADONPY_SCRIPT)
    python = os.environ.get("RADONPY_PYTHON", sys.executable)
    missing = []
    if python == sys.executable and not available("radonpy"):
        missing.append("RadonPy (or RADONPY_PYTHON pointing to its environment)")
    if not executable("lmp", "LAMMPS_EXEC"):
        missing.append("LAMMPS executable / LAMMPS_EXEC")
    if missing:
        return blocked(output, "polymer_atomistic", missing, "Geometry and a RadonPy→LAMMPS runner were exported; required engine is unavailable.")
    emit("simulate", 0.35, "RadonPy GAFF2_mod/Gasteiger → LAMMPS isolated-chain NVT")
    run_command(script_command(python, "run_radonpy.py"), output, "radonpy.log", seed=config["seed"])
    if not (output / "radonpy_complete.json").is_file():
        raise RuntimeError("RadonPy did not produce its completion artifact")
    # Replace the prepared frame with the computed final atomistic geometry.
    from rdkit import Chem
    final = Chem.MolFromMolFile(str(output / "final.mol"), removeHs=False)
    traj = json.loads((output / "trajectory.json").read_text())
    traj["frames"].append({"step": config["steps"], "positions": final.GetConformer().GetPositions().round(6).tolist()})
    write_json(output / "trajectory.json", traj)
    return outcome(output, "polymer_atomistic", "completed", "RadonPy→LAMMPS isolated-chain trajectory completed.", engine="RadonPy / LAMMPS", metadata=json.loads((output / "radonpy_complete.json").read_text()),
                   warnings=["GAFF2_mod with approximate Gasteiger charges; not a validated material model.", "A short isolated-chain run does not estimate bulk properties or establish equilibration.", "The atomistic route currently simulates one chain; use PACKMOL for packing."])


def class2_lammps(lmp, directory, script):
    """Run a LAMMPS input (EMC/class2 cell or a reused RadonPy cell) with OpenMP styles; surface the last LAMMPS ERROR line."""
    threads = int(os.environ.get("POLYMER_MD_THREADS") or min(4, os.cpu_count() or 1))
    argv = [lmp, "-in", script, "-log", "equilibration.log", "-sf", "omp", "-pk", "omp", str(threads)]
    try:
        run_command(argv, directory, "lammps_class2.log", timeout=7 * 86400)
    except subprocess.CalledProcessError as exc:
        lines = [line for line in (directory / "lammps_class2.log").read_text(errors="replace").splitlines() if line.startswith("ERROR")]
        raise RuntimeError(f"LAMMPS class2 run failed (exit {exc.returncode}): {lines[-1] if lines else 'see lammps_class2.log'}") from exc


def class2_missing():
    missing = []
    if not executable("lmp", "LAMMPS_EXEC"):
        missing.append("LAMMPS executable / LAMMPS_EXEC")
    if not available("radonpy"):
        missing.append("RadonPy (its Analyze class evaluates the sampling log)")
    if not shutil.which("perl"):
        missing.append("perl (runs the EMC setup script)")
    return missing


def cure_cell(config, output, emit):
    """Epoxy–amine network: EMC PCFF cell, LAMMPS fix bond/react curing, cooled NPT sampling."""
    from workbench import crosslink
    missing = class2_missing()
    if missing:
        return blocked(output, "polymer_atomistic", missing, "Crosslinking needs EMC (downloaded on first use), perl, LAMMPS with REACTION/CLASS2 and RadonPy.")
    lmp = executable("lmp", "LAMMPS_EXEC")
    complete = crosslink.cure(config, output, emit, lambda directory, script: class2_lammps(lmp, directory, script))
    (output / "trajectory.json").unlink(missing_ok=True)
    conversion = complete["conversion"]
    gel = (complete["gel_point_ideal"] or {}).get("epoxide_conversion")
    warnings = ["Only epoxide ring opening by primary and secondary amines is modelled; etherification and homopolymerization are not.",
                "Reactions happen whenever reactive atoms come within the cutoff (secondary amines with the configured assumed probability); "
                "this is a structural model, not cure kinetics.",
                "PCFF with bond-increment charges is not validated for this resin/hardener pair here.",
                "Small cells give few independent network paths; density and moduli of such networks scatter strongly between seeds."]
    if not conversion["target_reached"]:
        warnings.insert(0, f"Target conversion {complete['target_conversion']:.0%} was not reached within {complete['stage_steps']['cure']} cure steps (reached {conversion['epoxide']:.1%}).")
    if gel and conversion["epoxide"] < gel:
        warnings.append(f"Epoxide conversion {conversion['epoxide']:.1%} is below the ideal Flory–Stockmayer gel point {gel:.1%}; the cell is not a percolated network.")
    if not complete["radonpy_criteria_met"]:
        warnings.append("RadonPy's convergence criteria were not met in the post-cure sampling: " + "; ".join(complete["convergence_warnings"][:4]))
    values = {name: item["value"] for name, item in complete["properties"].items()}
    summary = {"epoxide_conversion": conversion["epoxide"], "amine_hydrogen_conversion": conversion["amine_hydrogen"],
               "density_g_cm3": values["density"], "bulk_modulus_gpa": values["bulk_modulus"], "atoms": complete["atoms"],
               "gel_point_epoxide_conversion": gel}
    return outcome(output, "polymer_atomistic", "completed", f"Epoxy–amine network cured to {conversion['epoxide']:.1%} epoxide conversion with LAMMPS fix bond/react and sampled under NPT.",
                   engine="EMC / LAMMPS bond/react / RadonPy analysis",
                   metadata={key: complete[key] for key in ("force_field", "charge_method", "reaction", "species", "counts", "templates", "builder", "cure_temperature_kelvin", "target_conversion", "reaction_cutoff_angstrom", "stage_steps", "gel_point_ideal", "parity", "radonpy_criteria_met")},
                   metrics={"summary": {k: v for k, v in summary.items() if v is not None}, "properties": complete["properties"],
                            "conversion_series": conversion["series"], "equilibrated": False},
                   warnings=warnings)


def tg_scan(config, output, emit):
    """Stepwise NPT cooling of a completed bulk or crosslinked cell; Tg from a hyperbola fit."""
    from workbench import glass_transition
    lmp = executable("lmp", "LAMMPS_EXEC")
    if not lmp:
        return blocked(output, "polymer_atomistic", ["LAMMPS executable / LAMMPS_EXEC"], "The Tg scan reruns the source cell in LAMMPS.")
    complete = glass_transition.scan(config, output, emit, lambda directory, script: class2_lammps(lmp, directory, script))
    low, high = complete["tg_interval_95_kelvin"]
    summary = {"tg_kelvin": complete["tg_kelvin"], "tg_interval_low_kelvin": low, "tg_interval_high_kelvin": high,
               "tg_bilinear_kelvin": complete["bilinear"]["tg_kelvin"], "cooling_rate_k_per_ns": complete["cooling_rate_k_per_ns"],
               "expansion_glass_per_kelvin": complete["expansion_glass_per_kelvin"], "expansion_melt_per_kelvin": complete["expansion_melt_per_kelvin"],
               "transition_resolved": complete["transition_resolved"], "temperatures": len(complete["stages"])}
    message = (f"Stepwise NPT cooling scan of job {config['source_job_id'][:8]}: hyperbola Tg {complete['tg_kelvin']:.0f} K "
               f"(profile 95% {low:.0f}-{high:.0f} K) at {complete['cooling_rate_k_per_ns']:.3g} K/ns.")
    if not complete["transition_resolved"]:
        message += " The transition is not resolved; see warnings."
    return outcome(output, "polymer_atomistic", "completed", message, engine="LAMMPS",
                   metadata={"source": complete["source"], "protocol": complete["protocol"], "method": complete["method"]},
                   metrics={"summary": summary, "stages": complete["stages"], "hyperbola_params": complete["hyperbola"]["params"], "equilibrated": False},
                   warnings=complete["warnings"])


def pcff_bulk_cell(config, output, emit):
    """PCFF class2 cell: EMC build and typing, then the same 21-step schedule and RadonPy analysis."""
    from workbench import pcff
    missing = class2_missing()
    lmp = executable("lmp", "LAMMPS_EXEC")
    if missing:
        return blocked(output, "polymer_atomistic", missing, "PCFF bulk cell needs EMC (downloaded on first use), perl, LAMMPS and RadonPy.")
    complete = pcff.bulk(config, output, emit, lambda directory: class2_lammps(lmp, directory, "in.pcff_bulk"))
    (output / "trajectory.json").unlink(missing_ok=True)
    values = {name: item["value"] for name, item in complete["properties"].items()}
    scale = complete["protocol_scale"]
    check = complete["parity"]
    warnings = ["PCFF with bond-increment charges was parameterized for organic polymers; it is not validated for this specific polymer here.",
                "Fluctuation properties (Cp, compressibility, bulk modulus, expansion) need long sampling; short runs can give unphysical signs or magnitudes.",
                "Oligomer chains and small cells do not represent high-molecular-weight polymer density or Tg; no Tg is inferred."]
    if scale < 1:
        warnings.insert(0, f"Stage lengths were multiplied by {scale:g} relative to RadonPy's EQ21step defaults; this is an exploratory protocol.")
    if check["coefficients_not_in_reference"]:
        warnings.append(f"{check['coefficients_not_in_reference']} coefficient lines came from EMC's automatic (cff91_auto) fallbacks that msi2lmp would not assign from pcff.frc; see pcff_parity.json.")
    if check["coefficients_matching"] != check["coefficients_checked"]:
        warnings.append(f"Only {check['coefficients_matching']} of {check['coefficients_checked']} EMC coefficients equal LAMMPS pcff.frc values; see pcff_parity.json.")
    if not complete["radonpy_criteria_met"]:
        warnings.append("RadonPy's convergence criteria were not met: " + "; ".join(complete["convergence_warnings"][:6]))
    summary = {"density_g_cm3": values["density"], "bulk_modulus_gpa": values["bulk_modulus"], "cp_j_kg_k": values["heat_capacity_cp"],
               "chain_rg_angstrom": values["chain_rg"], "atoms": complete["atoms"], "chains": complete["chains"],
               "sampling_ps": complete["stage_steps"]["sampling"] * complete["time_step_fs"] / 1000}
    return outcome(output, "polymer_atomistic", "completed", "PCFF (EMC-typed) bulk amorphous cell equilibrated by the Larsen 21-step protocol and sampled under NPT.",
                   engine="EMC / LAMMPS class2 / RadonPy analysis",
                   metadata={key: complete[key] for key in ("force_field", "charge_method", "ensemble", "protocol", "protocol_scale", "stage_steps", "time_step_fs", "temperature_kelvin", "pressure_atm", "chains", "atoms", "radonpy_criteria_met", "builder", "parity")},
                   metrics={"summary": {k: v for k, v in summary.items() if v is not None}, "properties": complete["properties"], "equilibrated": False},
                   warnings=warnings)


def bulk_cell(config, output, emit, metadata):
    """Periodic amorphous cell through RadonPy's own packing, 21-step and sampling presets."""
    if config.get("atomistic_forcefield", "gaff2") == "pcff":
        return pcff_bulk_cell(config, output, emit)
    if config.get("charge_method", "gasteiger") == "xtb_cm5":
        from workbench import engines
        emit("charges", 0.25, "xtb 6.7.1: GFN1-xTB CM5 charges for the capped chain")
        binary, record = engines.executable("xtb")
        # A separate file keeps machine-specific paths out of the job's stored config.json.
        write_json(output / "xtb_engine.json", {**record, "executable": str(binary), "parameters": str(binary.parent.parent / "share" / "xtb")})
    (output / "run_radonpy_bulk.py").write_text(RADONPY_BULK_SCRIPT)
    if config.get("cell_builder", "radonpy") == "native":
        shutil.copyfile(Path(__file__).with_name("amorphous.py"), output / "native_amorphous.py")
    python = os.environ.get("RADONPY_PYTHON", sys.executable)
    missing = []
    if python == sys.executable and not available("radonpy"):
        missing.append("RadonPy (or RADONPY_PYTHON pointing to its environment)")
    if not executable("lmp", "LAMMPS_EXEC"):
        missing.append("LAMMPS executable / LAMMPS_EXEC")
    if missing:
        return blocked(output, "polymer_atomistic", missing, "Chain geometry and a RadonPy bulk-cell runner were exported; required engine is unavailable.")
    scale = float(config.get("protocol_scale", 1.0))
    native = config.get("cell_builder", "radonpy") == "native"
    start = (f"Polymer Lab native cell ({config['chains']} chains grown at {config.get('build_density', 0.8):g} g/cm^3) → minimisation + NVT"
             if native else f"RadonPy amorphous cell ({config['chains']} chains) → packing")
    emit("simulate", 0.3, f"{start} → Larsen 21-step → NPT sampling (stage length x{scale:g})")
    # Full-length RadonPy protocols take hours to days on a desktop.
    try:
        run_command(script_command(python, "run_radonpy_bulk.py"), output, "radonpy_bulk.log", timeout=7 * 86400, seed=config["seed"])
    except subprocess.CalledProcessError as exc:
        lines = [line for line in (output / "radonpy_bulk.log").read_text(errors="replace").splitlines() if re.match(r"^\w*(Error|Exception)\b", line)]
        raise RuntimeError(f"RadonPy bulk run failed (exit {exc.returncode}): {lines[-1] if lines else 'see radonpy_bulk.log'}") from exc
    if not (output / "bulk_complete.json").is_file():
        raise RuntimeError("RadonPy did not produce its bulk completion artifact")
    complete = json.loads((output / "bulk_complete.json").read_text())
    # The single-chain preview no longer represents the periodic cell.
    (output / "trajectory.json").unlink(missing_ok=True)
    values = {name: item["value"] for name, item in complete["properties"].items()}
    charges = ("GAFF2_mod with unscaled GFN1-xTB CM5 charges from one RDKit conformer; RadonPy's reference protocol uses RESP."
               if complete.get("xtb") else "GAFF2_mod with approximate Gasteiger charges; not a validated material model.")
    warnings = [charges,
                "Fluctuation properties (Cp, compressibility, bulk modulus, expansion) need long sampling; short runs can give unphysical signs or magnitudes.",
                "Oligomer chains and small cells do not represent high-molecular-weight polymer density or Tg; no Tg is inferred."]
    if scale < 1:
        warnings.insert(0, f"Stage lengths were multiplied by {scale:g} relative to RadonPy's EQ21step defaults; this is an exploratory protocol.")
    if not complete["radonpy_criteria_met"]:
        warnings.append("RadonPy's convergence criteria were not met: " + "; ".join(complete["convergence_warnings"][:6]))
    summary = {"density_g_cm3": values["density"], "bulk_modulus_gpa": values["bulk_modulus"], "cp_j_kg_k": values["heat_capacity_cp"],
               "chain_rg_angstrom": values["chain_rg"], "atoms": complete["atoms"], "chains": complete["chains"],
               "sampling_ps": complete["stage_steps"]["sampling"] * complete["time_step_fs"] / 1000}
    cell = "Polymer Lab native amorphous cell" if native else "RadonPy bulk amorphous cell"
    return outcome(output, "polymer_atomistic", "completed", f"{cell} equilibrated by the Larsen 21-step protocol and sampled under NPT.",
                   engine="RadonPy / LAMMPS", metadata={**{key: complete[key] for key in ("force_field", "charge_method", "ensemble", "protocol", "protocol_scale", "stage_steps", "time_step_fs", "temperature_kelvin", "pressure_atm", "chains", "atoms", "radonpy_criteria_met")}, "builder": complete.get("builder")},
                   metrics={"summary": {k: v for k, v in summary.items() if v is not None}, "properties": complete["properties"], "equilibrated": False},
                   warnings=warnings)


def packmol(config, output, emit):
    mol, metadata = molecule_artifacts(config, output, emit)
    shutil.copyfile(output / "structure.pdb", output / "chain.pdb")
    coords = mol.GetConformer().GetPositions()
    box = max(30.0, float(np.ptp(coords, axis=0).max()) + 15, config["chains"] ** (1/3) * 20)
    imported = config.get("pdb_text", "")
    imported_block = ""
    if imported:
        # Multi-model (NMR) files would place every model as overlapping copies; keep the first.
        kept = []
        for line in imported.splitlines():
            if line.startswith("ENDMDL"):
                break
            if not line.startswith(("MODEL", "ENDMDL")):
                kept.append(line)
        imported = "\n".join(kept) + "\n"
        points = []
        for line in imported.splitlines():
            if line.startswith(("ATOM  ", "HETATM")):
                points.append([float(line[i:i+8]) for i in (30, 38, 46)])
        if not points or not np.isfinite(points).all():
            raise ValueError("Imported packing component must contain finite PDB atom coordinates")
        box = max(box, float(np.ptp(np.asarray(points), axis=0).max()) + 30)
        (output / "imported_component.pdb").write_text(imported)
        imported_block = f"structure imported_component.pdb\n number 1\n inside box 0. 0. 0. {box:.6f} {box:.6f} {box:.6f}\nend structure\n"
    chain_constraint = f" inside box 0. 0. 0. {box:.6f} {box:.6f} {box:.6f}\n"
    targeting = None
    target = config.get("packmol_target", "none")
    if target != "none":
        if not imported:
            raise ValueError("Targeted packing needs a protein structure (source_job_id or pdb_text)")
        if target == "sap_patch":
            from workbench.sap import sap_scan, write_sap_outputs
            sap = write_sap_outputs(sap_scan(imported, float(config.get("sap_radius_angstrom", 5.0))), imported, output)
            if not sap["patch"]:
                raise ValueError("No positive-SAP surface patch was found; use random placement")
            patch, targeting = sap["patch"], {"mode": "sap_patch", "patch_residues": sap["patch"]["residues"], "sap_file": "sap.json"}
        else:
            from rdkit import Chem
            from workbench import protonation
            ph, ionic = float(config.get("protein_ph", 7.0)), float(config.get("ionic_strength_molar", 0.15))
            stored = protonation.write_outputs(protonation.analyze(imported, ph, ionic), imported, output)
            sign = "negative" if target == "negative_patch" else "positive"
            patch = stored["surface"]["patches"][sign]
            if not patch:
                raise ValueError(f"No {sign} surface-potential patch (|potential| >= 0.5 kT/e) at pH {ph:g}; use random placement")
            chain_charge = Chem.GetFormalCharge(mol)
            complementary = chain_charge * (1 if sign == "negative" else -1) > 0
            targeting = {"mode": target, "patch_residues": patch["residues"][:30], "charged_groups": patch["charged_groups"],
                         "peak_potential_kT_per_e": patch["peak_potential"], "protein_ph": ph, "ionic_strength_molar": ionic,
                         "protein_net_charge": stored["net_charge"], "isoelectric_point": stored["isoelectric_point"],
                         "polymer_formal_charge_per_chain": chain_charge, "charge_complementary": complementary,
                         "protonation_file": "protonation.json", "potential_structure": "structure_potential.pdb"}
        # PACKMOL 'fixed' translates the input coordinates; the protein keeps its orientation at the box centre.
        shift = box / 2 - np.asarray(points).mean(axis=0)
        center = np.asarray(patch["center_angstrom"]) + shift
        radius = patch["radius_angstrom"] + float(np.ptp(coords, axis=0).max()) / 2 + 4.0
        imported_block = f"structure imported_component.pdb\n number 1\n fixed {shift[0]:.6f} {shift[1]:.6f} {shift[2]:.6f} 0. 0. 0.\nend structure\n"
        chain_constraint += f" inside sphere {center[0]:.6f} {center[1]:.6f} {center[2]:.6f} {radius:.6f}\n"
        targeting.update({"protein_translation_angstrom": shift.round(6).tolist(), "sphere_center_angstrom": center.round(6).tolist(),
                          "sphere_radius_angstrom": radius,
                          "rule": "all chain atoms inside a sphere of patch radius + half the chain extent + 4 A around the patch centre"})
    text = f"tolerance 2.0\nfiletype pdb\noutput packed.pdb\nseed {config['seed']}\nstructure chain.pdb\n number {config['chains']}\n{chain_constraint}end structure\n"
    text += imported_block
    (output / "packmol.inp").write_text(text)
    cmd = executable("packmol", "PACKMOL_EXEC")
    if not cmd:
        return blocked(output, "packmol", ["PACKMOL executable / PACKMOL_EXEC"], "Chain geometry and packing input prepared; install PACKMOL to execute.")
    emit("pack", 0.4, "PACKMOL: packing atomistic chains in an angstrom box")
    run_command([cmd], output, "packmol.log", stdin=text, timeout=1800)
    if not (output / "packed.pdb").exists() or "Success!" not in (output / "packmol.log").read_text():
        raise RuntimeError("PACKMOL did not report successful packing; inspect packmol.log")
    shutil.copyfile(output / "packed.pdb", output / "structure.pdb")
    # Prepared single-chain trajectory no longer represents the packed system.
    (output / "trajectory.json").unlink(missing_ok=True)
    warnings = ["Geometric packing is not equilibrium or force-field assignment.", "Protein/polymer mixtures require a compatible joint topology and parameter validation before MD."]
    if targeting and target == "sap_patch":
        warnings.append("Chains start near the highest-SAP surface patch by construction; this is a placement choice, not evidence of binding there.")
    elif targeting:
        warnings.append(f"Chains start near the most {'negative' if target == 'negative_patch' else 'positive'} screened-Coulomb surface patch at pH {targeting['protein_ph']:g}; this is a placement choice, not evidence of binding there.")
        if not targeting["charge_complementary"]:
            warnings.append(f"The polymer chain's formal charge from its SMILES is {targeting['polymer_formal_charge_per_chain']:+d}; it is not opposite to this patch, so no electrostatic attraction is implied.")
    return outcome(output, "packmol", "prepared", "PACKMOL packing completed; force-field assignment and dynamics remain separate stages.", engine="PACKMOL", metadata={"box_angstrom": [box]*3, "chains": config["chains"], "tolerance_angstrom": 2, "imported_components": int(bool(imported)), "packing_completed": True, "targeting": targeting}, warnings=warnings)


def attach_sap(output, pdb_text, config):
    """SAP screen for protein results; a failed screen never changes the structure outcome."""
    try:
        from workbench.sap import sap_scan, write_sap_outputs
        stored = write_sap_outputs(sap_scan(pdb_text, float(config.get("sap_radius_angstrom", 5.0))), pdb_text, output)
    except (ImportError, ValueError, RuntimeError) as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "summary": stored["summary"], "patch": stored["patch"], "radius_angstrom": stored["radius_angstrom"],
            "top_residues": sorted(({k: r[k] for k in ("chain", "resid", "resname", "sap")} for r in stored["residues"]), key=lambda r: -r["sap"])[:10],
            "files": ["sap.json", stored["structure_file"]]}


def attach_protonation(output, pdb_text, config):
    """PROPKA titration, surface charge patches and a PDB2PQR protonated structure; never changes the structure outcome."""
    from workbench import protonation
    ph, ionic = float(config.get("protein_ph", 7.0)), float(config.get("ionic_strength_molar", 0.15))
    try:
        stored = protonation.write_outputs(protonation.analyze(pdb_text, ph, ionic), pdb_text, output)
    except (ImportError, ValueError, RuntimeError, KeyError) as exc:
        return {"available": False, "reason": str(exc)}
    summary = {"available": True, "ph": ph, "ionic_strength_molar": ionic, "net_charge": stored["net_charge"],
               "isoelectric_point": stored["isoelectric_point"], "summary": stored["summary"],
               "ambiguous_groups": [{k: g[k] for k in ("type", "chain", "resid", "pka")} for g in stored["groups"] if g["ambiguous"]][:20],
               "shifted_groups": sorted(({k: g[k] for k in ("type", "chain", "resid", "pka", "shift")} for g in stored["groups"] if g["titratable"] and abs(g["shift"]) >= 1.5),
                                        key=lambda g: -abs(g["shift"]))[:10],
               "patches": {name: patch and {k: patch[k] for k in ("center_angstrom", "radius_angstrom", "peak_potential", "mean_potential")} | {"residues": patch["residues"][:12]}
                           for name, patch in stored["surface"]["patches"].items()},
               "files": ["protonation.json", "structure_potential.pdb"]}
    try:
        summary["protonated_structure"] = protonation.protonate(pdb_text, ph, output)
        summary["files"] += summary["protonated_structure"]["files"]
    except (ImportError, ValueError, RuntimeError) as exc:
        summary["protonated_structure"] = {"available": False, "reason": str(exc)}
    return summary


def _checked_af_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "alphafold.ebi.ac.uk" or parsed.username:
        raise ValueError("Only official HTTPS AlphaFold DB URLs are accepted")
    return url


def _fetch_af(url):
    req = urllib.request.Request(_checked_af_url(url), headers={"User-Agent": "PolymerWorkbench/0.1"})
    with urllib.request.urlopen(req, timeout=30) as response:
        _checked_af_url(response.url)
        data = response.read(10_000_001)
    if len(data) > 10_000_000:
        raise ValueError("AlphaFold download exceeds the 10 MB import limit")
    return data


def protein_metrics(pdb_text, plddt=False):
    rows, points = [], []
    for line in pdb_text.splitlines():
        if line.startswith("ENDMDL"):
            break
        if line.startswith(("ATOM  ", "HETATM")):
            point = [float(line[i:i+8]) for i in (30, 38, 46)]
            if not np.isfinite(point).all() or max(abs(value) for value in point) > 100000:
                raise ValueError("Protein coordinates must be finite and within 100000 angstroms")
        if line.startswith("ATOM  ") and line[12:16].strip() == "CA" and line[16:17] in (" ", "A"):
            points.append(point)
            row = {"residue": line[22:26].strip(), "chain": line[21:22], "resname": line[17:20].strip()}
            if plddt:
                value = float(line[60:66])
                if not 0 <= value <= 100:
                    raise ValueError("pLDDT must be between 0 and 100")
                row["plddt"] = value
            rows.append(row)
    if not rows:
        raise ValueError("No protein CA atoms found in PDB input")
    coords = np.asarray(points)
    summary = {"residues": len(rows), "ca_radius_of_gyration_angstrom": float(np.sqrt(np.mean(np.sum((coords-coords.mean(0))**2, axis=1))))}
    if plddt:
        scores = np.asarray([row["plddt"] for row in rows])
        summary.update(mean_plddt=float(scores.mean()), fraction_plddt_below_70=float(np.mean(scores < 70)))
    return {"summary": summary, "residues": rows, "units": {"ca_radius_of_gyration_angstrom": "angstrom", "plddt": "0-100"}, "plddt_interpreted": plddt}


def protein(config, output, emit):
    mode = config.get("protein_mode", "export")
    if mode == "database":
        accession = str(config.get("accession", "P69905")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{6,10}(?:-\d{1,3})?", accession):
            raise ValueError("Provide a UniProt accession such as P69905")
        emit("import", 0.2, f"Downloading existing AlphaFold DB prediction: {accession}")
        records = json.loads(_fetch_af(f"https://alphafold.ebi.ac.uk/api/prediction/{accession}"))
        if not isinstance(records, list) or not records:
            raise ValueError("AlphaFold DB has no prediction for this accession")
        record = next((r for r in records if "-F1-" in r.get("entryId", "") or "-F1-" in r.get("pdbUrl", "")), records[0])
        pdb_text = _fetch_af(record["pdbUrl"]).decode("utf-8")
        (output / "structure.pdb").write_text(pdb_text)
        write_json(output / "alphafold_db_record.json", record)
        metrics = protein_metrics(pdb_text, plddt=True)
        write_json(output / "metrics.json", metrics)
        sap = attach_sap(output, pdb_text, config)
        titration = attach_protonation(output, pdb_text, config)
        return outcome(output, "protein", "completed", "Imported an existing AlphaFold DB prediction; no new folding inference was run.", engine="AlphaFold Protein Structure Database", metrics=metrics, sap=sap, protonation=titration,
                       provenance={"accession": accession, "url": record["pdbUrl"], "entry_id": record.get("entryId"), "new_inference": False},
                       warnings=["pLDDT is local model confidence, not experimental validation or binding affinity.", "The selected database fragment may not cover the full protein."])
    if mode == "import":
        text = config.get("pdb_text", "")
        if not isinstance(text, str) or not text or len(text) > 10_000_000:
            raise ValueError("Supply pdb_text containing a protein PDB file (at most 10 MB)")
        plddt = config.get("bfactor_is_plddt") is True
        warnings = ["B factors are interpreted as pLDDT only when explicitly requested."]
        fixes = None
        if config.get("protein_repair", "none") == "pdbfixer":
            from workbench import repair
            emit("repair", 0.15, "PDBFixer: missing heavy atoms, internal SEQRES gaps, nonstandard residues")
            (output / "structure_submitted.pdb").write_text(text)
            text, fixes = repair.repair(text, seed=int(config.get("seed", 1)))
            write_json(output / "repair.json", fixes)
            if fixes["missing_residues_built"]:
                warnings.append("PDBFixer built missing internal residues from SEQRES with idealized geometry; their coordinates are not experimental.")
            if fixes["heterogens_removed"]:
                warnings.append("Ligands, ions and water were removed before analysis: " + ", ".join(fixes["heterogens_removed"][:10]))
        metrics = protein_metrics(text, plddt=plddt)
        (output / "structure.pdb").write_text(text)
        write_json(output / "metrics.json", metrics)
        sap = attach_sap(output, text, config)
        titration = attach_protonation(output, text, config)
        return outcome(output, "protein", "completed", "Protein structure imported and analyzed; no folding inference run.", engine="PDB import" + (" + PDBFixer" if fixes else ""),
                       metrics=metrics, sap=sap, protonation=titration, repair=fixes, warnings=warnings)
    sequence = "".join(str(config.get("sequence", "")).split()).upper()
    if not 4 <= len(sequence) <= 2000 or re.search(r"[^ACDEFGHIKLMNPQRSTVWY]", sequence):
        raise ValueError("Protein sequence must contain 4–2000 standard amino-acid one-letter codes")
    (output / "sequence.fasta").write_text(">protein_A\n" + sequence + "\n")
    af_input = {"name": "polymer_workbench_protein", "modelSeeds": [config["seed"]],
                "sequences": [{"protein": {"id": "A", "sequence": sequence}}], "dialect": "alphafold3", "version": 1}
    ligand = config.get("ligand_smiles", "")
    if ligand:
        from rdkit import Chem
        mol = Chem.MolFromSmiles(ligand)
        if mol is None or any(a.GetAtomicNum() == 0 for a in mol.GetAtoms()) or mol.GetNumHeavyAtoms() > 100:
            raise ValueError("AF3 ligand export needs a finite molecule without '*' and at most 100 heavy atoms")
        af_input["sequences"].append({"ligand": {"id": "B", "smiles": Chem.MolToSmiles(mol)}})
    write_json(output / "alphafold3_input.json", af_input)
    warnings = ["AlphaFold models biological structures; this is not a general synthetic-polymer conformation or materials-property predictor.",
                "An AF3 input export is not a prediction; AF3 weights, databases and a compatible compute environment are required."]
    if mode == "export":
        return outcome(output, "protein", "prepared", "FASTA and AlphaFold 3 input JSON prepared; no prediction executed.", engine="AlphaFold 3 input exporter", warnings=warnings)
    cmd = executable("colabfold_batch", "COLABFOLD_EXEC")
    msa_mode = config.get("msa_mode", "single_sequence")
    if msa_mode not in ("single_sequence", "server"):
        raise ValueError("msa_mode must be single_sequence or server")
    argv = [cmd or "colabfold_batch", "sequence.fasta", "predictions", "--msa-mode", "single_sequence" if msa_mode == "single_sequence" else "mmseqs2_uniref_env", "--num-models", "1", "--num-recycle", "3", "--random-seed", str(config["seed"])]
    if config.get("a3m_text"):
        (output / "sequence.a3m").write_text(config["a3m_text"])
        argv[1] = "sequence.a3m"
    write_json(output / "colabfold_command.json", {"argv": argv, "sequence_sent_to_msa_server": msa_mode == "server" and not bool(config.get("a3m_text")), "model_parameters_may_download": True})
    if not cmd:
        return blocked(output, "protein", ["ColabFold / COLABFOLD_EXEC", "AlphaFold2 model parameters and compatible JAX runtime"], "ColabFold input and command prepared; folding runtime is unavailable.", warnings=warnings)
    emit("predict", 0.25, "Running ColabFold (single sequence by default; no MSA server submission)")
    run_command(argv, output, "colabfold.log", timeout=21600)
    predictions = sorted((output / "predictions").glob("*rank_001*.pdb"))
    if not predictions:
        raise RuntimeError("ColabFold did not produce a ranked PDB prediction")
    shutil.copyfile(predictions[0], output / "structure.pdb")
    metrics = protein_metrics((output / "structure.pdb").read_text(), plddt=True)
    write_json(output / "metrics.json", metrics)
    sap = attach_sap(output, (output / "structure.pdb").read_text(), config)
    titration = attach_protonation(output, (output / "structure.pdb").read_text(), config)
    return outcome(output, "protein", "completed", "ColabFold structure prediction completed.", engine="ColabFold / AlphaFold2", metrics=metrics, sap=sap, protonation=titration,
                   warnings=["Single-sequence mode has no evolutionary MSA evidence and can substantially reduce accuracy.", "ColabFold predicts the protein; the optional AF3 ligand is only exported for separate AF3 inference."])


def polyply(config, output, emit):
    library = config.get("polyply_library", "martini3")
    residue = config.get("polyply_residue", "PEO")
    if not re.fullmatch(r"[A-Za-z0-9_]+", library) or not re.fullmatch(r"[A-Za-z0-9_]+", residue):
        raise ValueError("polyply library/residue must be alphanumeric names")
    cmd = executable("polyply", "POLYPLY_EXEC")
    argv = [cmd or "polyply", "gen_itp", "-name", "POLY", "-lib", library, "-seq", f"{residue}:{config['monomers']}", "-o", "polymer.itp"]
    reproducibility = {"requested_seed": config["seed"], "coordinate_seed_controlled": False,
                       "reason": "The audited polyply 1.8 CLI has no random-seed option; gen_coords uses upstream Python/NumPy random state."}
    write_json(output / "polyply_command.json", {"argv": argv, "library": library, "residue": residue, "repeat_smiles_used": False, "reproducibility": reproducibility})
    write_json(output / "random_seeds.json", reproducibility)
    if not cmd:
        return blocked(output, "polyply", ["polyply / POLYPLY_EXEC"], "Polyply topology-generation command prepared.")
    emit("topology", 0.2, f"polyply gen_itp: {library}/{residue}")
    run_command(argv, output, "polyply_topology.log")
    if not (output / "polymer.itp").exists():
        raise RuntimeError("polyply did not produce polymer.itp")
    topology = '#include "forcefield.itp"\n#include "polymer.itp"\n\n[ system ]\nPolymer Workbench\n\n[ molecules ]\nPOLY '+str(config["chains"])+"\n"
    (output / "system.top").write_text(topology)
    forcefield = config.get("forcefield_itp_text", "")
    if not forcefield:
        return outcome(output, "polyply", "prepared", "Polyply molecular topology generated; supply the matching nonbonded forcefield_itp_text before coordinate generation.", engine="polyply", requirements=["Matching full force field including defaults/atomtypes/nonbonded terms"], warnings=["Library residue names define the chemistry here; repeat SMILES is not used.", "Molecular ITP alone is not a complete GROMACS topology."])
    (output / "forcefield.itp").write_text(forcefield)
    box = float(config.get("box_nm", 15))
    if not math.isfinite(box) or not 5 <= box <= 100:
        raise ValueError("box_nm must be 5–100")
    coords_command = [cmd, "gen_coords", "-p", "system.top", "-o", "system.gro", "-box", *([str(box)]*3)]
    write_json(output / "polyply_coords_command.json", {"argv": coords_command, "reproducibility": reproducibility})
    emit("build", 0.4, "polyply gen_coords: creating system coordinates")
    run_command(coords_command, output, "polyply_coords.log")
    if not (output / "system.gro").exists():
        raise RuntimeError("polyply did not create coordinates")
    mdp = config.get("mdp_text", "")
    if not mdp:
        return outcome(output, "polyply", "prepared", "Polyply topology and coordinates prepared; supply mdp_text for a GROMACS run.", engine="polyply", reproducibility=reproducibility, warnings=["Polyply coordinate generation does not honor the workflow seed in this CLI version."])
    (output / "simulation.mdp").write_text(mdp)
    gmx = executable("gmx", "GROMACS_EXEC")
    commands = [[gmx or "gmx", "grompp", "-f", "simulation.mdp", "-c", "system.gro", "-p", "system.top", "-o", "run.tpr"], [gmx or "gmx", "mdrun", "-deffnm", "run", "-nt", "1"]]
    write_json(output / "gromacs_commands.json", commands)
    if not gmx:
        return blocked(output, "polyply", ["GROMACS / GROMACS_EXEC"], "Polyply coordinates and GROMACS inputs prepared; engine is missing.")
    emit("simulate", 0.6, "GROMACS: grompp validation then mdrun")
    for number, command in enumerate(commands):
        run_command(command, output, f"gromacs_{number}.log")
    if not (output / "run.gro").exists():
        raise RuntimeError("GROMACS did not create run.gro")
    return outcome(output, "polyply", "completed", "polyply→GROMACS run completed using supplied force field and MDP.", engine="polyply / GROMACS", reproducibility=reproducibility, warnings=["Completion does not establish equilibration or validate the supplied physical model.", "Polyply coordinate generation does not honor the workflow seed; GROMACS random seeds are controlled by the supplied MDP."])


PYSIMM_SCRIPT = '''from pathlib import Path
from pysimm import system, forcefield, lmps
s=system.read_mol("structure.mol")
s.apply_forcefield(forcefield.Gaff2(),charges="gasteiger",box_padding=30)
lmps.quick_min(s,np=None,min_style="cg")
s.write_pdb("structure.pdb")
s.write_lammps("final.data")
Path("pysimm_complete.json").write_text('{"operation":"GAFF2/Gasteiger energy minimization","equilibrated":false}')
'''


def pysimm(config, output, emit):
    molecule_artifacts(config, output, emit)
    (output / "run_pysimm.py").write_text(PYSIMM_SCRIPT)
    python = os.environ.get("PYSIMM_PYTHON", sys.executable)
    missing = []
    if python == sys.executable and not available("pysimm"):
        missing.append("pysimm or PYSIMM_PYTHON")
    if not executable("lmp", "LAMMPS_EXEC"):
        missing.append("LAMMPS executable / LAMMPS_EXEC")
    if missing:
        return blocked(output, "pysimm", missing, "RDKit geometry and pysimm→LAMMPS minimization runner exported.")
    emit("minimize", 0.5, "pysimm: GAFF2 assignment and LAMMPS minimization")
    run_command(script_command(python, "run_pysimm.py"), output, "pysimm.log")
    if not (output / "pysimm_complete.json").exists():
        raise RuntimeError("pysimm did not produce a completed minimization")
    (output / "trajectory.json").unlink(missing_ok=True)
    return outcome(output, "pysimm", "completed", "pysimm→LAMMPS energy minimization completed.", engine="pysimm / LAMMPS", warnings=["This route performs energy minimization, not an equilibrated molecular-dynamics trajectory."])


HOOMD_SCRIPT = '''import json, math
from pathlib import Path
import numpy as np
import hoomd
c=json.loads(Path("config.json").read_text())
n,m=c["monomers"],c["chains"]
count=n*m
box=float(max(n*2+30,m*8+30))
s=hoomd.Snapshot()
s.configuration.box=[box,box,box,0,0,0]
s.particles.N=count
s.particles.types=["B"]
coords=np.zeros((m,n,3));coords[:,:,0]=np.arange(n)-(n-1)/2;coords[:,:,1]=(np.arange(m)[:,None]-(m-1)/2)*4
s.particles.position[:]=coords.reshape(-1,3)
bonds=[[k*n+i,k*n+i+1] for k in range(m) for i in range(n-1)]
s.bonds.N=len(bonds);s.bonds.types=["chain"];s.bonds.group[:]=bonds
sim=hoomd.Simulation(device=hoomd.device.CPU(),seed=c["seed"]%65536)
sim.create_state_from_snapshot(s)
nlist=hoomd.md.nlist.Cell(buffer=0.4,exclusions=["bond"])
pair=hoomd.md.pair.Gaussian(nlist=nlist,default_r_cut=3.0,mode="shift")
pair.params[("B","B")]=dict(epsilon=5.0,sigma=1.0)
bond=hoomd.md.bond.Harmonic();bond.params["chain"]=dict(k=30.,r0=1.)
method=hoomd.md.methods.Langevin(filter=hoomd.filter.All(),kT=c["temperature"])
sim.operations.integrator=hoomd.md.Integrator(dt=c["timestep"],methods=[method],forces=[pair,bond])
sim.state.thermalize_particle_momenta(filter=hoomd.filter.All(),kT=c["temperature"])
thermo=hoomd.md.compute.ThermodynamicQuantities(filter=hoomd.filter.All());sim.operations.computes.append(thermo)
frames=[];series=[];step=0;stride=max(1,math.ceil(c["steps"]/100));sim.run(0)
while True:
    snap=sim.state.get_snapshot();xyz=np.array(snap.particles.position)+np.array(snap.particles.image)*box
    chain=xyz.reshape(m,n,3);centered=chain-chain.mean(1,keepdims=True)
    rg=float(np.sqrt((centered**2).sum(2).mean(1)).mean())
    frames.append(dict(step=step,positions=xyz.round(6).tolist()))
    series.append(dict(step=step,time=step*c["timestep"],rg=rg,potential_energy=float(thermo.potential_energy),temperature=float(thermo.kinetic_temperature)))
    if step==c["steps"]:break
    chunk=min(stride,c["steps"]-step);sim.run(chunk);step+=chunk
Path("trajectory.json").write_text(json.dumps(dict(unit="sigma",kind="coarse_grained",frames=frames,bonds=bonds,chains=m,monomers=n)))
Path("metrics.json").write_text(json.dumps(dict(series=series,summary=dict(initial_rg=series[0]["rg"],final_rg=series[-1]["rg"],beads=count,frames=len(frames)),units=dict(rg="sigma",potential_energy="epsilon",time="tau"))))
Path("hoomd_complete.json").write_text(json.dumps(dict(version=hoomd.version.version,model="Gaussian repulsion plus harmonic bonds; reduced units",boundary="periodic dilute box",equilibrated=False)))
'''


def hoomd(config, output, emit):
    (output / "run_hoomd.py").write_text(HOOMD_SCRIPT)
    python = os.environ.get("HOOMD_PYTHON", sys.executable)
    if python == sys.executable and not available("hoomd"):
        return blocked(output, "hoomd", ["HOOMD-blue v4+ or HOOMD_PYTHON"], "Runnable HOOMD-blue CPU bead-spring script exported; HOOMD runtime is missing.")
    emit("simulate", 0.3, "HOOMD-blue: harmonic chains with Gaussian soft repulsion")
    run_command(script_command(python, "run_hoomd.py"), output, "hoomd.log")
    if not (output / "hoomd_complete.json").exists():
        raise RuntimeError("HOOMD did not produce its completion artifact")
    from workbench.science import write_cg_pdb
    trajectory = json.loads((output / "trajectory.json").read_text())
    write_cg_pdb(output / "structure.pdb", trajectory["frames"][-1]["positions"], trajectory["bonds"], config["monomers"])
    return outcome(output, "hoomd", "completed", "HOOMD-blue bead-spring simulation completed.", engine="HOOMD-blue", metrics=json.loads((output / "metrics.json").read_text()),
                   model=json.loads((output / "hoomd_complete.json").read_text()), warnings=["Uncalibrated reduced-unit Gaussian model; distinct from the default cosine-soft LAMMPS model.", "No equilibration or real-material property claim."])


def run_external(config, output, emit):
    functions = {"polymer_atomistic": atomistic, "protein": protein, "packmol": packmol,
                 "polyply": polyply, "pysimm": pysimm, "hoomd": hoomd}
    return functions[config["route"]](config, output, emit)
