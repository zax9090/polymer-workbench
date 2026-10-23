"""PCFF (Class II) amorphous bulk cells: EMC builds and types the cell, LAMMPS runs RadonPy's schedule.

EMC 9.4.4 grows the chains at 0.5 g/cm^3 with its PCFF typing rules and bond-increment charges and
writes LAMMPS class2 data/parameter files.  The LAMMPS input below repeats RadonPy's EQ21step
stage list (600 K anneals, 0.02-1.00 x 50000 atm compression, decompression, Nose-Hoover NPT with
SHAKE on X-H bonds, 1 fs) and its NPT sampling, and RadonPy's own Analyze class turns the sampling
log into density, Cp, compressibility, bulk modulus and expansion, so the GAFF2 and PCFF routes
differ only in the force field.  Every coefficient EMC wrote is recomputed from LAMMPS' public
pcff.frc with the msi2lmp lookup rules (workbench/class2.py) and reported.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import numpy as np

from . import class2, engines

BUILD_DENSITY = 0.5
MAX_TEMP, MAX_PRESS = 600.0, 50000.0
# RadonPy radonpy/sim/preset/eq.py EQ21step defaults: [hot NVT, NVT, NPT] steps per pressure.
STEP_LIST = [[50000, 50000, 50000], [50000, 100000, 50000], [50000, 100000, 50000], [50000, 100000, 5000],
             [5000, 10000, 5000], [5000, 10000, 5000], [5000, 10000, 800000]]
PRESS_RATIO = [0.02, 0.60, 1.00, 0.50, 0.10, 0.01]
THERMO = "step time temp press enthalpy etotal ke pe ebond eangle edihed eimp evdwl ecoul elong etail vol lx ly lz density pxx pyy pzz pxy pxz pyz"
LAMMPS_PCFF = Path(__file__).resolve().with_name("data") / "forcefields" / "lammps_pcff.frc"
LAMMPS_PCFF_SHA256 = "e9c9da613d663c9613fba7823839c71cd07b8679363f629d8ad83c1b1349fd4c"
LAMMPS_PCFF_SOURCE = "lammps/lammps@a0e2bd6efb3d67a29c0c4e4de3b19556ffdb9d5b tools/msi2lmp/frc_files/pcff.frc (public domain per frc_files/README)"
ELEMENTS = {1.008: "H", 12.011: "C", 14.007: "N", 15.999: "O", 18.998: "F", 28.086: "Si", 30.974: "P", 32.06: "S", 35.45: "Cl", 79.904: "Br"}


def emc_smiles(repeat_smiles: str) -> str:
    """Repeat unit with exactly two attachment points, written with EMC's bare '*' connectors."""
    from rdkit import Chem
    mol = Chem.MolFromSmiles(repeat_smiles)
    if mol is None:
        raise ValueError("Repeat SMILES could not be parsed")
    stars = [atom for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0]
    if len(stars) != 2 or any(atom.GetDegree() != 1 for atom in stars):
        raise ValueError("The PCFF bulk route needs a repeat unit with exactly two terminal [*] attachment points")
    if Chem.GetFormalCharge(mol) != 0:
        raise ValueError("The PCFF bulk route builds neutral cells; charged repeat units need counter-ions")
    text = re.sub(r"\[\d*\*\]", "*", repeat_smiles)
    if text.count("*") != 2:
        raise ValueError("Write the attachment points as [*] or *")
    return text


def chemistry(repeat_smiles, monomers, chains, seed):
    """EMC setup (.esh): head-to-tail homopolymer chains capped with hydrogen."""
    unit = emc_smiles(repeat_smiles)
    return f"""ITEM	OPTIONS

replace		true
field		pcff
number		true
density		{BUILD_DENSITY}
seed		{seed}
emc_execute	false

ITEM	END

ITEM	GROUPS

rep		{unit}, 1,rep:2, 2,rep:1, 1,hterm:1, 2,hterm:1
hterm		*[H]

ITEM	END

ITEM	CLUSTERS

poly		alternate	{chains}

ITEM	END

ITEM	POLYMERS

poly
1		rep,{monomers},hterm,2

ITEM	END
"""


def schedule(scale):
    steps = [[max(100, int(round(n * scale))) for n in row] for row in STEP_LIST]
    thermo = max(10, min(1000, int(5000000 * scale) // 400))
    sampling = thermo * max(400, int(5000000 * scale) // thermo)
    return steps, sampling, thermo


def eq21_lines(temperature, steps, seed, shake=True):
    """Minimization and RadonPy's 21-step stages (600 K anneal, compression to 50000 atm, release)."""
    eq_thermo = max(10, min(1000, sum(map(sum, steps)) // 200))
    pressures = [ratio * MAX_PRESS for ratio in PRESS_RATIO] + [1.0]
    lines = ["kspace_style pppm 1e-6", "neighbor 2.0 bin", "neigh_modify delay 0 every 1 check yes",
             f"thermo_style custom {THERMO}", "thermo_modify flush yes", f"thermo {eq_thermo}",
             "min_style cg", "minimize 1.0e-4 1.0e-6 10000 100000", "reset_timestep 0", "timestep 1.0"]
    if shake:
        lines.append("fix shake all shake 1e-4 1000 0 m 1.0")
    lines.append(f"velocity all create {MAX_TEMP} {seed} mom yes rot yes dist gaussian")
    for number, (row, pressure) in enumerate(zip(steps, pressures), 1):
        lines += [f"# step group {number}: {pressure:g} atm",
                  f"fix hot all nvt temp {MAX_TEMP} {MAX_TEMP} 100.0", "neigh_modify delay 0 every 1 check yes",
                  f"run {row[0]}", "unfix hot",
                  f"fix cool all nvt temp {temperature} {temperature} 100.0", f"run {row[1]}", "unfix cool",
                  f"fix press all npt temp {temperature} {temperature} 100.0 iso {pressure} {pressure} 1000.0",
                  "neigh_modify delay 0 every 1 check no", f"run {row[2]}", "unfix press"]
    return lines


def lammps_input(temperature, scale, seed):
    """LAMMPS script mirroring RadonPy's EQ21step and sampling presets for an EMC class2 cell."""
    steps, sampling, thermo = schedule(scale)
    lines = ["# Polymer Lab PCFF bulk cell: RadonPy EQ21step schedule on an EMC-built class2 system",
             "units real", "atom_style full", "boundary p p p",
             "variable cutoff index 9.5", "variable charge_cutoff index 9.5",
             "read_data polymer.data", "include polymer.params", *eq21_lines(temperature, steps, seed)]
    lines += ["write_data equilibrated.data", "log sampling.log", "reset_timestep 0", f"thermo {thermo}",
              f"fix sample all npt temp {temperature} {temperature} 100.0 iso 1.0 1.0 1000.0",
              "neigh_modify delay 0 every 1 check yes", f"run {sampling}", "unfix sample", "write_data final.data"]
    return "\n".join(lines) + "\n", {"eq21": sum(map(sum, steps)), "sampling": sampling, "thermo": thermo, "step_list": steps}


def read_cell(path):
    """Masses, atoms (unwrapped with image flags) and box of a LAMMPS data file."""
    masses, atoms, box, section = {}, [], {}, None
    for line in Path(path).read_text().splitlines():
        body = line.split("#")[0].strip()
        if not body:
            continue
        tokens = body.split()
        if len(tokens) == 4 and tokens[2].endswith("lo") and tokens[3].endswith("hi"):
            box[tokens[2][0]] = (float(tokens[0]), float(tokens[1]))
            continue
        if tokens[0][0].isalpha():
            section = tokens[0]
            continue
        if section == "Masses":
            masses[int(tokens[0])] = float(tokens[1])
        elif section == "Atoms" and len(tokens) >= 7:
            x = np.array([float(v) for v in tokens[4:7]])
            image = np.array([int(v) for v in tokens[7:10]]) if len(tokens) >= 10 else np.zeros(3, int)
            atoms.append((int(tokens[0]), int(tokens[1]), int(tokens[2]), x, image))
    lengths = np.array([box[a][1] - box[a][0] for a in "xyz"])
    return masses, atoms, lengths


def chain_rg(path):
    masses, atoms, lengths = read_cell(path)
    chains = {}
    for _, molecule, kind, x, image in atoms:
        chains.setdefault(molecule, []).append((masses[kind], x + image * lengths))
    values = []
    for members in chains.values():
        weights = np.array([m for m, _ in members])
        xyz = np.array([p for _, p in members])
        center = (weights[:, None] * xyz).sum(0) / weights.sum()
        values.append(float(np.sqrt((weights * ((xyz - center) ** 2).sum(1)).sum() / weights.sum())))
    return float(np.mean(values)), len(values)


def element(mass):
    best = min(ELEMENTS, key=lambda m: abs(m - mass))
    return ELEMENTS[best] if abs(best - mass) < 0.1 else "X"


def write_pdb(data_path, pdb_path):
    masses, atoms, lengths = read_cell(data_path)
    lines = [f"CRYST1{lengths[0]:9.3f}{lengths[1]:9.3f}{lengths[2]:9.3f}  90.00  90.00  90.00 P 1           1"]
    for index, (_, molecule, kind, x, _) in enumerate(sorted(atoms), 1):
        symbol = element(masses[kind])
        lines.append(f"HETATM{index % 100000:5d} {symbol:<4} POL A{molecule % 10000:4d}    {x[0]:8.3f}{x[1]:8.3f}{x[2]:8.3f}  1.00  0.00          {symbol:>2}")
    Path(pdb_path).write_text("\n".join(lines) + "\nEND\n")


def _run(argv, output, logname, env, timeout, what):
    with (output / logname).open("w") as log:
        result = subprocess.run([str(a) for a in argv], cwd=output, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, text=True, env=env, timeout=timeout)
    if result.returncode:
        log_text = (output / logname).read_text(errors="replace")
        if "Missing force field parameters" in log_text:
            # EMC names each gap in a warning before aborting; a different force field is the user's choice.
            gaps = sorted({m.strip() for m in re.findall(r"Warning: (.+? not found)\.", log_text)})
            raise ValueError("PCFF (pcff.frc via EMC) has no parameters for this chemistry: " + ("; ".join(gaps) or "see " + logname)
                             + ". Use atomistic_forcefield gaff2 for this repeat unit; Polymer Lab does not mix force fields.")
        lines = [l.strip() for l in log_text.splitlines() if re.match(r"^\s*(ERROR|Error)", l)]
        raise RuntimeError(f"{what} failed (exit {result.returncode}): {lines[-1] if lines else 'see ' + logname}")


def parity(output):
    """Recompute EMC's coefficients and charges from LAMMPS' pcff.frc with the msi2lmp rules."""
    reference = class2.read_frc(LAMMPS_PCFF)
    report = {"reference_file": LAMMPS_PCFF_SOURCE, "reference_sha256": LAMMPS_PCFF_SHA256,
              "coefficients": class2.compare_parameters((output / "polymer.params").read_text(), reference),
              "charges": class2.compare_charges((output / "polymer.data").read_text(), reference)}
    (output / "pcff_parity.json").write_text(json.dumps(report, indent=2))
    return report


def build(config, output, emc_root, environment):
    (output / "polymer.esh").write_text(chemistry(config["repeat_smiles"], int(config["monomers"]), int(config["chains"]), int(config["seed"])))
    emc_run(output, emc_root, environment)


def emc_run(output, emc_root, environment):
    """EMC setup (perl) then build on polymer.esh; writes polymer.data and polymer.params."""
    env = {**environment, "EMC_ROOT": str(emc_root), "PATH": f"{emc_root / 'bin'}{os.pathsep}{emc_root / 'scripts'}{os.pathsep}{environment.get('PATH', '')}"}
    perl = shutil.which("perl", path=env["PATH"])
    if not perl:
        raise RuntimeError("EMC setup needs perl on PATH")
    _run([perl, emc_root / "scripts" / "emc.pl", "polymer.esh"], output, "emc_setup.log", env, 600, "EMC setup")
    _run([emc_root / engines.ENGINES["emc"]["executable"], "build.emc"], output, "emc_build.log", env, 3600, "EMC build")
    for name in ("polymer.data", "polymer.params"):
        if not (output / name).is_file():
            raise RuntimeError(f"EMC did not write {name}; inspect emc_build.log")


def coefficients(output):
    """Refuse to run MD on a cell whose class2 coefficients are missing or zero-filled."""
    report = class2.check_coefficients((output / "polymer.params").read_text(), (output / "polymer.data").read_text())
    (output / "coefficient_check.json").write_text(json.dumps(report, indent=2))
    if not report["passed"]:
        first = report["issues"][0]
        raise ValueError(f"{len(report['issues'])} class2 coefficient problems before MD (first: {first['term']} type {first['type']}: "
                         f"{first['problem']}); see coefficient_check.json. Use atomistic_forcefield gaff2 for this chemistry.")
    return report


def atoms_in(data_path):
    for line in Path(data_path).read_text().splitlines()[:20]:
        tokens = line.split()
        if len(tokens) == 2 and tokens[1] == "atoms":
            return int(tokens[0])
    raise RuntimeError("Atom count missing from the EMC data file")


def analyze(output, temperature):
    """RadonPy's Analyze on the sampling log; same estimators and convergence criteria as the GAFF2 route."""
    from radonpy.sim import lammps
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        analysis = lammps.Analyze(log_file=str(output / "sampling.log"))
    if not analysis.dfs:
        warnings = sorted({l.strip() for l in (output / "sampling.log").read_text(errors="replace").splitlines() if l.startswith("WARNING")})
        raise RuntimeError("RadonPy could not parse the sampling log. LAMMPS warnings: " + " | ".join(warnings[:5]))
    rows = len(analysis.dfs[-1])
    window = max(2, int(0.4 * rows))
    with contextlib.redirect_stdout(captured):
        values = analysis.get_all_prop(temp=temperature, press=1.0, init=window, width=window, f_width=window, do_traj=False)
        criteria = bool(analysis.check_eq())
    density = [float(v) for v in analysis.dfs[-1]["Density"].to_numpy()]
    return values, criteria, captured.getvalue(), density, {"rows": rows, "init": window, "width": window}


def number(values, key, factor=1.0):
    value = values.get(key)
    return float(value) * factor if value is not None and np.isfinite(value) else None


def bulk(config, output, emit, run_lammps):
    """EMC → LAMMPS class2 21-step → NPT sampling; writes bulk_complete.json like the RadonPy route."""
    temperature, scale, chains = float(config.get("temperature_kelvin", 300.0)), float(config.get("protocol_scale", 1.0)), int(config["chains"])
    emc_root, emc_record = engines.ensure("emc")
    emit("build", 0.2, f"EMC {emc_record['version']}: PCFF typing and chain growth ({chains} chains x {config['monomers']} units, {BUILD_DENSITY} g/cm^3)")
    from .external import subprocess_environment
    build(config, output, emc_root, subprocess_environment("perl"))
    atoms = atoms_in(output / "polymer.data")
    if atoms < 200:
        raise ValueError(f"A {atoms}-atom cell is too small for periodic bulk sampling; use at least 200 atoms (more chains or longer chains)")
    if atoms > 30000:
        raise ValueError("This desktop bulk route is limited to 30000 atoms per cell")
    check = parity(output)
    coefficients(output)
    script, steps = lammps_input(temperature, scale, int(config["seed"]) % 900000 + 1000)
    (output / "in.pcff_bulk").write_text(script)
    emit("simulate", 0.35, f"LAMMPS class2: Larsen 21-step ({steps['eq21']} steps) + NPT sampling ({steps['sampling']} steps)")
    run_lammps(output)
    values, criteria, printed, density, window = analyze(output, temperature)
    rg, chain_count = chain_rg(output / "final.data")
    write_pdb(output / "final.data", output / "structure.pdb")
    properties = {"density": {"value": number(values, "density"), "unit": "g/cm^3"},
                  "chain_rg": {"value": rg, "unit": "angstrom"},
                  "heat_capacity_cp": {"value": number(values, "Cp"), "unit": "J/(kg K)"},
                  "isothermal_compressibility": {"value": number(values, "compressibility", 1e9), "unit": "1/GPa"},
                  "bulk_modulus": {"value": number(values, "bulk_modulus", 1e-9), "unit": "GPa"},
                  "volume_expansion": {"value": number(values, "volume_expansion"), "unit": "1/K"}}
    complete = {"force_field": "PCFF (EMC 9.4.4 typing, cff91 class2)", "charge_method": "PCFF bond increments",
                "ensemble": "NPT sampling after Larsen 21-step compression/decompression",
                "protocol": "RadonPy EQ21step stage list and sampling length on an EMC-built LAMMPS class2 cell",
                "builder": {**emc_record, "build_density_g_cm3": BUILD_DENSITY}, "protocol_scale": scale,
                "stage_steps": {"eq21": steps["eq21"], "sampling": steps["sampling"]}, "sampling_thermo_freq": steps["thermo"],
                "time_step_fs": 1.0, "temperature_kelvin": temperature, "pressure_atm": 1.0, "max_temp_kelvin": MAX_TEMP,
                "max_press_atm": MAX_PRESS, "chains": chain_count, "atoms": atoms, "properties": properties,
                "analysis_window_rows": window, "radonpy_criteria_met": criteria,
                "convergence_warnings": [line.split("RadonPy warning:", 1)[1].strip() for line in printed.splitlines() if "RadonPy warning:" in line],
                "density_series": density[-2000:], "lammps_velocity_seed": int(config["seed"]) % 900000 + 1000,
                "parity": {"coefficients_checked": check["coefficients"]["checked"], "coefficients_matching": check["coefficients"]["matching"],
                           "coefficients_not_in_reference": check["coefficients"]["not_in_reference"],
                           "charge_max_abs_difference": check["charges"]["max_abs_difference"], "net_charge": check["charges"]["net_charge"]},
                "equilibrated": False}
    (output / "bulk_complete.json").write_text(json.dumps(complete, indent=2))
    return complete
