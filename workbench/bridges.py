"""Inspectable protein/polymer preparation recipes; never certify force-field compatibility.

``protein_cg`` generates martinize2/polyply component topologies. The opt-in
``simulate`` mode additionally assembles a solvated, neutralized Martini 3 system
from pinned parameters and runs staged GROMACS minimization, restrained NVT/NPT
and a short production pilot. Completion is not evidence of equilibrium.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import warnings
from datetime import datetime, timezone

from workbench.forcefields import link_into, martini3_parameters, martini3_status
from workbench.paths import runtime_environment, subprocess_environment

PACKAGES = {
    "vermouth": "vermouth", "polyply": "polyply", "insane": "insane",
    "openff.toolkit": "openff-toolkit", "openff.nagl": "openff-nagl",
    "parmed": "ParmEd", "duckdb": "duckdb", "sklearn": "scikit-learn",
    "pymbar": "pymbar", "baybe": "baybe",
}
COMMANDS = {
    "martinize2": "MARTINIZE_EXEC", "polyply": "POLYPLY_EXEC",
    "insane": "INSANE_EXEC", "gmx": "GROMACS_EXEC", "tleap": "TLEAP_EXEC",
    "antechamber": "ANTECHAMBER_EXEC", "parmchk2": "PARMCHK2_EXEC", "sander": "SANDER_EXEC",
}
STANDARD_RESIDUES = set("ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL".split())
# Martini 3 W is one bead for four waters (72 g/mol); ~1000 kg/m^3 gives 8.36 beads/nm^3.
WATER_BEADS_PER_NM3 = 8.36
# Formula units per nm^3 for a 1 mol/L solution.
PER_NM3_PER_MOLAR = 0.6022
MAX_SIMULATION_PARTICLES = 70000


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _module(name):
    try:
        spec = importlib.util.find_spec(name)
        location = str(Path(spec.origin).parent) if spec and spec.origin else None
        version = importlib.metadata.version(PACKAGES[name]) if spec else None
        return {"available": spec is not None, "path": location, "version": version}
    except (ImportError, ValueError, importlib.metadata.PackageNotFoundError):
        return {"available": False, "path": None, "version": None}


def _polymer_blocks(module_path):
    """Read actual installed library declarations; no arbitrary SMILES mapping."""
    libraries = {}
    if not module_path:
        return libraries
    for library in sorted((Path(module_path) / "data").glob("*")):
        if not library.is_dir() or library.name.startswith("_"):
            continue
        blocks = set()
        for source in sorted(library.glob("*.ff")):
            pending = False
            for raw in source.read_text(errors="replace").splitlines():
                line = raw.split(";", 1)[0].strip()
                if not line or line.startswith("#"):
                    continue
                if re.fullmatch(r"\[\s*moleculetype\s*\]", line):
                    pending = True
                elif line.startswith("["):
                    pending = False
                elif pending:
                    blocks.add(line.split()[0]); pending = False
        libraries[library.name] = sorted(blocks)
    return libraries


def bridge_capabilities():
    env = runtime_environment()
    modules = {name: _module(name) for name in PACKAGES}
    commands = {name: shutil.which(env.get(variable, name), path=env.get("PATH"))
                for name, variable in COMMANDS.items()}
    return {"schema_version": 1, "modules": modules, "executables": commands,
            "polyply_libraries": _polymer_blocks(modules["polyply"]["path"]),
            "tracks": ["martini3", "amber", "openff"], "martini3_parameters": martini3_status(),
            "execution_scope": "Martini 3 components, optional solvated GROMACS pilot; all-atom mixtures are recipes only",
            "note": "Discovery reports paths/versions, not successful scientific execution."}


def _validated(config):
    values = {"bridge_track": "martini3", "bridge_mode": "prepare", "polyply_library": "martini3",
              "polyply_residue": "PEO", "monomers": 24, "chains": 2, "temperature_kelvin": 300.0,
              "box_nm": 15.0, "seed": 42, "secondary_structure": "", "pdb_text": "",
              "polymer_net_charge": 0, "steps": 10000, "equilibration_steps": 50000, "salt_molar": 0.15,
              "contact_cutoff_nm": 0.6, "elastic_network": False, "protein_protonation": "as_input", "protein_ph": 7.0,
              "protein_polymer_lambda": 1.0, "elastic_plddt_min": 0.0, "bfactor_is_plddt": False, **config}
    if values["bridge_track"] not in {"martini3", "amber", "openff"}:
        raise ValueError("bridge_track must be martini3, amber or openff")
    if values["bridge_mode"] not in {"prepare", "protein_cg", "simulate"}:
        raise ValueError("bridge_mode must be prepare, protein_cg or simulate")
    if values["bridge_mode"] in {"protein_cg", "simulate"} and values["bridge_track"] != "martini3":
        raise ValueError("protein_cg and simulate execution are supported only for martini3")
    for key, low, high in (("monomers", 2, 200), ("chains", 1, 16), ("seed", 1, 899999998), ("polymer_net_charge", -100, 100),
                           ("steps", 10, 100000), ("equilibration_steps", 100, 500000)):
        v = values[key]
        if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
            raise ValueError(f"{key} must be an integer in [{low}, {high}]")
    if values["protein_protonation"] not in {"as_input", "propka"}:
        raise ValueError("protein_protonation must be as_input or propka")
    for key, low, high in (("temperature_kelvin", 250, 400), ("box_nm", 5, 100), ("salt_molar", 0, 1), ("contact_cutoff_nm", 0.3, 1.5), ("protein_ph", 0, 14)):
        v = float(values[key])
        if not math.isfinite(v) or not low <= v <= high:
            raise ValueError(f"Solvated-mixture {key} must be in [{low}, {high}]")
        values[key] = v
    for key in ("polyply_library", "polyply_residue"):
        if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", values[key]):
            raise ValueError(f"Invalid {key}")
    if values["bridge_track"] == "martini3" and values["polyply_library"] != "martini3":
        raise ValueError("Martini 2/beta and Martini 3 libraries must not be mixed; select martini3")
    if not isinstance(values["elastic_network"], bool):
        raise ValueError("elastic_network must be true or false")
    for key, low, high in (("protein_polymer_lambda", 0.5, 1.0), ("elastic_plddt_min", 0.0, 95.0)):
        v = float(values[key])
        if not math.isfinite(v) or not low <= v <= high:
            raise ValueError(f"{key} must be in [{low}, {high}]")
        values[key] = v
    if values["elastic_plddt_min"] and not (values["elastic_network"] and values["bfactor_is_plddt"]):
        raise ValueError("elastic_plddt_min needs elastic_network and B-factors that hold pLDDT (bfactor_is_plddt)")
    if values["bridge_mode"] == "simulate" and WATER_BEADS_PER_NM3 * values["box_nm"] ** 3 > MAX_SIMULATION_PARTICLES:
        raise ValueError(f"A {values['box_nm']:g} nm box needs more than {MAX_SIMULATION_PARTICLES} particles; use a smaller box for this desktop pilot")
    return values


def _protein(text, ss):
    if not text:
        return {"residues": 0, "secondary_structure_supplied": bool(ss)}
    if len(text) > 5_000_000:
        raise ValueError("Protein PDB exceeds the 5 MB limit")
    atoms = [line for line in text.splitlines() if line.startswith("ATOM  ")]
    if not atoms:
        raise ValueError("Protein PDB requires ATOM records")
    if sum(line.startswith("MODEL ") for line in text.splitlines()) > 1:
        raise ValueError("Select a single protein model before preparing a mixed system")
    residues = {}
    for line in atoms:
        if len(line) < 54:
            raise ValueError("Malformed PDB atom record")
        if line[16] not in {" ", "A"}:
            raise ValueError("Resolve alternate conformations before parameterization")
        if line[17:20].strip() not in STANDARD_RESIDUES:
            raise ValueError("Modified/nonstandard residues require explicit reviewed parameters")
        xyz = [float(line[a:b]) for a,b in ((30,38),(38,46),(46,54))]
        if not all(math.isfinite(v) for v in xyz):
            raise ValueError("Protein coordinates must be finite")
        residues[(line[21],line[22:27])] = line[17:20]
    if any(line.startswith("HETATM") for line in text.splitlines()):
        raise ValueError("Separate ligands/waters/ions from protein input and parameterize them explicitly")
    if ss and (len(ss) != len(residues) or any(c not in "HBEGITSC" for c in ss)):
        raise ValueError("Provide one DSSP H/B/E/G/I/T/S/C code per protein residue; uniform guessing is not accepted")
    return {"residues": len(residues), "secondary_structure_supplied": bool(ss),
            "note": "Coordinate syntax checks do not establish protonation, folding quality or parameter validity."}


def _gromacs_templates(output, temperature, seed, nvt_steps=50000, npt_steps=100000, production_steps=100000):
    common = f"""; REVIEW REQUIRED: Martini 3 solvated mixed-system starting templates.
; Requires a complete version-matched topology and solvated coordinates.
cutoff-scheme = Verlet
nstlist = 20
pbc = xyz
coulombtype = Reaction-Field
rcoulomb = 1.1
epsilon-r = 15
epsilon-rf = 0
vdwtype = Cut-off
vdw-modifier = Potential-shift-Verlet
rvdw = 1.1
constraints = none
nstenergy = 100
nstlog = 100
"""
    stages = [
        ("01_minimize.mdp", "integrator = steep\nnsteps = 5000\nemtol = 100\nemstep = 0.01\n", "minimization", 0),
        ("02_nvt.mdp", f"integrator = md\ndt = 0.002\nnsteps = {nvt_steps}\ndefine = -DPOSRES\ntcoupl = v-rescale\ntc-grps = System\ntau-t = 1.0\nref-t = {temperature}\npcoupl = no\ngen-vel = yes\ngen-temp = {temperature}\ngen-seed = {seed}\n", "restrained NVT", 0.002 * nvt_steps),
        ("03_npt.mdp", f"integrator = md\ndt = 0.005\nnsteps = {npt_steps}\ndefine = -DPOSRES\ncontinuation = yes\ntcoupl = v-rescale\ntc-grps = System\ntau-t = 1.0\nref-t = {temperature}\npcoupl = C-rescale\npcoupltype = isotropic\ntau-p = 5.0\nref-p = 1.0\ncompressibility = 3e-4\nrefcoord-scaling = com\ngen-vel = no\n", "restrained NPT", 0.005 * npt_steps),
        ("04_production.mdp", f"integrator = md\ndt = 0.010\nnsteps = {production_steps}\ncontinuation = yes\ntcoupl = v-rescale\ntc-grps = System\ntau-t = 1.0\nref-t = {temperature}\npcoupl = C-rescale\npcoupltype = isotropic\ntau-p = 5.0\nref-p = 1.0\ncompressibility = 3e-4\ngen-vel = no\nnstxout-compressed = {max(1, production_steps // 100)}\n", "production pilot", 0.010 * production_steps),
    ]
    result = []
    for filename, text, name, duration in stages:
        (output / filename).write_text(common + text)
        result.append({"name": name, "input": filename, "duration_ps": duration, "state": "requires_review"})
    return result


def _amber_templates(output, temperature, seed):
    header = "REVIEW REQUIRED: solvated protein/polymer pilot, AMBER units ps/K/atm\n"
    stages = [
        ("01_minimize.in", "&cntrl imin=1, maxcyc=5000, ncyc=2500, ntb=1, cut=10.0, ntr=1, restraint_wt=5.0, restraintmask='!:WAT,Na+,Cl-', /\n", "restrained minimization", 0),
        ("02_nvt.in", f"&cntrl imin=0, irest=0, ntx=1, ntb=1, cut=10.0, ntt=3, gamma_ln=1.0, tempi=10.0, temp0={temperature}, ig={seed}, dt=0.001, nstlim=100000, ntc=2, ntf=2, ntr=1, restraint_wt=2.0, restraintmask='!:WAT,Na+,Cl-', ntpr=100, ntwx=1000, nmropt=1, /\n&wt TYPE='TEMP0', istep1=0, istep2=100000, value1=10.0, value2={temperature}, /\n&wt TYPE='END', /\n", "restrained NVT heating", 100),
        ("03_npt.in", f"&cntrl imin=0, irest=1, ntx=5, ntb=2, ntp=1, barostat=2, pres0=1.0, taup=2.0, cut=10.0, ntt=3, gamma_ln=1.0, temp0={temperature}, ig={seed}, dt=0.001, nstlim=500000, ntc=2, ntf=2, ntr=1, restraint_wt=1.0, restraintmask='!:WAT,Na+,Cl-', ntpr=100, ntwx=1000, /\n", "restrained NPT", 500),
        ("04_production.in", f"&cntrl imin=0, irest=1, ntx=5, ntb=2, ntp=1, barostat=2, pres0=1.0, taup=2.0, cut=10.0, ntt=3, gamma_ln=1.0, temp0={temperature}, ig={seed}, dt=0.002, nstlim=500000, ntc=2, ntf=2, ntr=0, ntpr=100, ntwx=1000, /\n", "production pilot", 1000),
    ]
    for filename,text,_,_ in stages:
        (output / filename).write_text(header + text)
    return [{"name":name,"input":filename,"duration_ps":duration,"state":"requires_review"} for filename,_,name,duration in stages]


def _execute(command, output, logname, timeout=180, env=None, stdin=None, what="Component parameterization"):
    with (output / logname).open("w") as log:
        result = subprocess.run(command, cwd=output, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, text=True,
                                env=env or subprocess_environment(command[0]), timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{what} failed (exit {result.returncode}); inspect {logname}. No warning override applied.")


def _itp_molecules(path):
    """Bead counts and summed charges per [ moleculetype ] of a GROMACS include file."""
    molecules, current, section = {}, None, None
    for raw in Path(path).read_text(errors="replace").splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        header = re.fullmatch(r"\[\s*(\w+)\s*\]", line)
        if header:
            section = header.group(1).lower()
        elif section == "moleculetype":
            current = line.split()[0]
            molecules[current] = {"atoms": 0, "charge": 0.0}
            section = None
        elif section == "atoms" and current:
            fields = line.split()
            molecules[current]["atoms"] += 1
            # An omitted charge column inherits the atom type charge, which is 0 for Martini 3 types.
            if len(fields) >= 7:
                molecules[current]["charge"] += float(fields[6])
    return molecules


def _martinize_topology(path):
    """Protein includes and molecule counts written by martinize2 (its martini.itp stub is replaced)."""
    includes, molecules, section = [], [], None
    for raw in Path(path).read_text().splitlines():
        line = raw.split(";", 1)[0].strip()
        if line.startswith("#include"):
            name = line.split(None, 1)[1].strip().strip('"')
            if name != "martini.itp":
                if not re.fullmatch(r"[A-Za-z0-9_.-]+\.itp", name) or not (Path(path).parent / name).is_file():
                    raise RuntimeError(f"Unexpected martinize2 include {name!r}")
                includes.append(name)
        elif re.fullmatch(r"\[\s*molecules\s*\]", line):
            section = "molecules"
        elif line.startswith("["):
            section = None
        elif section == "molecules" and line:
            name, count = line.split()[:2]
            molecules.append((name, int(count)))
    if not includes or not molecules:
        raise RuntimeError("martinize2 topology lacks protein includes or molecule counts")
    return includes, molecules


def plddt_regions(pdb_text, minimum, shortest=3):
    """Runs of consecutive residues whose CA pLDDT (B-factor) is at least minimum, as martinize2 -eunit ranges."""
    residues = {}
    for line in pdb_text.splitlines():
        if line.startswith("ATOM  ") and line[12:16].strip() == "CA":
            residues[(line[21], int(line[22:26]))] = float(line[60:66])
    if len({chain for chain, _ in residues}) != 1:
        raise ValueError("pLDDT-limited elastic networks support single-chain proteins only")
    runs, current = [], []
    for (_, resid), plddt in sorted(residues.items(), key=lambda item: item[0][1]):
        if plddt >= minimum and current and resid == current[-1] + 1:
            current.append(resid)
        else:
            if len(current) >= shortest:
                runs.append([current[0], current[-1]])
            current = [resid] if plddt >= minimum else []
    if len(current) >= shortest:
        runs.append([current[0], current[-1]])
    if not runs:
        raise ValueError(f"No run of {shortest}+ residues has pLDDT >= {minimum:g}; disable the elastic network or lower elastic_plddt_min")
    kept = sum(b - a + 1 for a, b in runs)
    return {"minimum_plddt": minimum, "regions": runs, "residues": len(residues), "residues_in_regions": kept,
            "eunit": ",".join(f"{a}:{b}" for a, b in runs),
            "rule": f"elastic bonds only within runs of at least {shortest} consecutive residues with CA pLDDT >= {minimum:g} (martinize2 -eunit)"}


def _sections(path):
    """(section, raw line) for every line of a GROMACS include file."""
    section = None
    for raw in Path(path).read_text(errors="replace").splitlines():
        header = re.fullmatch(r"\s*\[\s*(\w+)\s*\]\s*(;.*)?", raw)
        if header:
            section = header.group(1).lower()
        yield section, raw


def _bead_types(path):
    return {raw.split(";", 1)[0].split()[1] for section, raw in _sections(path)
            if section == "atoms" and len(raw.split(";", 1)[0].split()) > 1 and not raw.lstrip().startswith(("#", "["))}


def _retype(source, target, rename):
    lines = []
    for section, raw in _sections(source):
        body, _, comment = raw.partition(";")
        fields = body.split()
        if section == "atoms" and len(fields) > 1 and not body.lstrip().startswith(("#", "[")):
            fields[1] = rename[fields[1]]
            raw = " " + " ".join(fields) + (f" ;{comment}" if comment else "")
        lines.append(raw)
    Path(target).write_text("\n".join(lines) + "\n")


def scale_protein_polymer(output, protein_includes, polymer_include, scale, forcefield="martini_v3.0.0.itp"):
    """Copy every protein and polymer bead type so that only protein-polymer Lennard-Jones epsilon is multiplied by scale.

    Protein-protein, polymer-polymer and solute-solvent pairs keep their Martini 3 values; sigma is unchanged.
    """
    atomtypes, pairs, rule = {}, {}, None
    for section, raw in _sections(output / forcefield):
        fields = raw.split(";", 1)[0].split()
        if section == "defaults" and len(fields) >= 2:
            rule = fields[:2]
        elif section == "atomtypes" and len(fields) >= 6:
            atomtypes[fields[0]] = fields[1:]
        elif section == "nonbond_params" and len(fields) >= 5:
            pairs[frozenset(fields[:2])] = (float(fields[3]), float(fields[4]))
    if rule != ["1", "2"]:
        raise RuntimeError("Expected Martini [ defaults ] nbfunc 1, comb-rule 2")

    def pair(a, b):
        # Pairs absent from nonbond_params (the non-interacting U type) fall back to comb-rule 2, as in grompp.
        if frozenset((a, b)) in pairs:
            return pairs[frozenset((a, b))]
        (sa, ea), (sb, eb) = (map(float, atomtypes[t][-2:]) for t in (a, b))
        return (sa + sb) / 2, (ea * eb) ** 0.5

    protein = set().union(*(_bead_types(output / name) for name in protein_includes))
    polymer = _bead_types(output / polymer_include)
    copies = {**{f"{t}_pr": (t, "protein") for t in sorted(protein)}, **{f"{t}_pl": (t, "polymer") for t in sorted(polymer)}}
    if set(copies) & set(atomtypes) or not (protein | polymer) <= set(atomtypes):
        raise RuntimeError("Bead types of the protein or polymer are missing from, or collide with, the Martini 3 atom types")
    lines = [f"; Polymer Lab: protein-polymer epsilon x {scale:g}; generated from {forcefield}", "[ atomtypes ]"]
    lines += [f"{name} {' '.join(atomtypes[base])}" for name, (base, _) in copies.items()]
    lines += ["", "[ nonbond_params ]"]
    written = 0
    names = list(copies)
    for i, name in enumerate(names):
        base, owner = copies[name]
        partners = [(other, other, None) for other in atomtypes] + [(other, *copies[other]) for other in names[i:]]
        for label, other_base, other_owner in partners:
            sigma, epsilon = pair(base, other_base)
            if {owner, other_owner} == {"protein", "polymer"}:
                epsilon *= scale
            lines.append(f"{name} {label} 1 {sigma:.6e} {epsilon:.6e}")
            written += 1
    (output / "martini_lambda.itp").write_text("\n".join(lines) + "\n")
    renamed = []
    for name in protein_includes:
        _retype(output / name, output / f"lambda_{name}", {t: f"{t}_pr" for t in protein})
        renamed.append(f"lambda_{name}")
    _retype(output / polymer_include, output / f"lambda_{polymer_include}", {t: f"{t}_pl" for t in polymer})
    record = {"protein_polymer_lambda": scale, "protein_bead_types": sorted(protein), "polymer_bead_types": sorted(polymer),
              "pairs_written": written, "file": "martini_lambda.itp",
              "sha256": hashlib.sha256((output / "martini_lambda.itp").read_bytes()).hexdigest(),
              "meaning": "Lennard-Jones epsilon of every protein-bead/polymer-bead pair multiplied by the factor; an assumed correction "
                         "for Martini 3 over-aggregation, not a parameter fitted to data for this system"}
    return renamed, f"lambda_{polymer_include}", record


def _assemble_solvated(cfg, output):
    """Combined Martini 3 topology with neutralizing ions and an explicit salt estimate."""
    import numpy as np
    import MDAnalysis as mda
    includes, proteins = _martinize_topology(output / "protein.top")
    definitions = {}
    for name in includes:
        definitions.update(_itp_molecules(output / name))
    polymer = _itp_molecules(output / "polymer.itp").get("Polymer")
    if not polymer or any(name not in definitions for name, _ in proteins):
        raise RuntimeError("Component topologies do not define the expected Protein/Polymer molecules")
    if abs(polymer["charge"] - cfg["polymer_net_charge"]) > 1e-3:
        raise ValueError(f"The polyply {cfg['polyply_residue']} chain carries {polymer['charge']:+.3g} e; set polymer_net_charge to {round(polymer['charge'])} to confirm it")
    protein_atoms = sum(definitions[name]["atoms"] * count for name, count in proteins)
    charge = sum(definitions[name]["charge"] * count for name, count in proteins) + cfg["chains"] * polymer["charge"]
    if abs(charge - round(charge)) > 1e-3:
        raise ValueError(f"Combined solute charge {charge:.4f} e is not an integer; review the component topologies")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        universe = mda.Universe(str(output / "protein_cg.pdb"))
    if len(universe.atoms) != protein_atoms:
        raise RuntimeError("protein_cg.pdb and the protein topology have different bead counts")
    box = cfg["box_nm"]
    span = float(np.ptp(universe.atoms.positions, axis=0).max()) / 10
    if span + 2 > box:
        raise ValueError(f"The CG protein spans {span:.1f} nm; use box_nm of at least {span + 2:.1f} nm for a 1 nm margin per side")
    universe.atoms.translate(np.full(3, box * 5) - universe.atoms.center_of_geometry())
    universe.dimensions = [box * 10] * 3 + [90] * 3
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        universe.atoms.write(str(output / "protein_centered.gro"))
    solute = protein_atoms + cfg["chains"] * polymer["atoms"]
    # Solute beads displace water roughly one-for-one; NPT relaxes the remaining density error.
    available = WATER_BEADS_PER_NM3 * box ** 3 - solute
    pairs = int(round(cfg["salt_molar"] * PER_NM3_PER_MOLAR * available / WATER_BEADS_PER_NM3))
    net = int(round(charge))
    sodium, chloride = pairs + max(0, -net), pairs + max(0, net)
    water = int(round(available)) - sodium - chloride
    if water < 100:
        raise ValueError("The box leaves fewer than 100 water beads; increase box_nm")
    molecules = [*proteins, ("Polymer", cfg["chains"]), ("W", water), ("NA", sodium), ("CL", chloride)]
    scaling, extra, polymer_include = None, [], "polymer.itp"
    if cfg["protein_polymer_lambda"] != 1:
        includes, polymer_include, scaling = scale_protein_polymer(output, includes, "polymer.itp", cfg["protein_polymer_lambda"])
        _json(output / "lambda_scaling.json", scaling)
        extra = ['#include "martini_lambda.itp"']
    lines = ['#include "martini_v3.0.0.itp"', *extra, '#include "martini_v3.0.0_solvents_v1.itp"', '#include "martini_v3.0.0_ions_v1.itp"',
             *[f'#include "{name}"' for name in includes], f'#include "{polymer_include}"', "", "[ system ]",
             f"Polymer Lab Martini 3 protein + {cfg['chains']} x {cfg['polyply_residue']}{cfg['monomers']} in W/NaCl", "", "[ molecules ]",
             *[f"{name} {count}" for name, count in molecules if count]]
    (output / "system.top").write_text("\n".join(lines) + "\n")
    layout = {"protein": [0, protein_atoms],
              "polymer_chains": [[protein_atoms + i * polymer["atoms"], protein_atoms + (i + 1) * polymer["atoms"]] for i in range(cfg["chains"])],
              "particles": solute + water + sodium + chloride}
    solvation = {"box_nm": box, "water_beads": water, "waters_represented": 4 * water, "sodium": sodium, "chloride": chloride,
                 "salt_pairs": pairs, "salt_molar_target": cfg["salt_molar"],
                 "salt_molar_estimated": pairs / (PER_NM3_PER_MOLAR * water / WATER_BEADS_PER_NM3),
                 "solute_charge_e": net, "system_charge_e": net + sodium - chloride, "protein_beads": protein_atoms,
                 "polymer_beads_per_chain": polymer["atoms"], "polymer_charge_per_chain_e": polymer["charge"],
                 "water_bead_density_per_nm3": WATER_BEADS_PER_NM3, "molecules": [[name, count] for name, count in molecules],
                 "protein_polymer_lambda": scaling,
                 "method": "W count from bulk density minus solute beads; Na+/Cl- pairs from target molarity in the water volume plus counter-ions"}
    _json(output / "solvation.json", solvation)
    return layout, solvation


def _md_threads():
    configured = os.environ.get("POLYMER_MD_THREADS", "")
    if configured:
        if not configured.isdigit() or int(configured) < 1:
            raise ValueError("POLYMER_MD_THREADS must be a positive integer")
        return int(configured)
    return max(1, min(4, os.cpu_count() or 1))


def _gromacs_version(gmx, env):
    result = subprocess.run([gmx, "--version"], capture_output=True, text=True, env=env, timeout=60)
    for line in result.stdout.splitlines():
        if line.strip().startswith("GROMACS version:"):
            return line.split(":", 1)[1].strip()
    return None


def _simulate_martini(cfg, output, caps, emit):
    """Solvate, neutralize and run staged GROMACS MD; refuse any unverifiable step."""
    from workbench.mixture_analysis import analyze_martini_mixture
    try:
        directory, forcefield = martini3_parameters()
    except (OSError, ValueError) as exc:
        return {"state": "blocked", "commands": [], "requirements": [f"Verified Martini 3.0.0 parameters from the pinned upstream commit: {exc}"]}
    link_into(directory, output, {"martini_v3.0.0.itp": "martini_v3.0.0.itp", "martini_v3.0.0_solvents_v1.itp": "martini_v3.0.0_solvents_v1.itp",
                                  "martini_v3.0.0_ions_v1.itp": "martini_v3.0.0_ions_v1.itp", "LICENSE": "MARTINI3_LICENSE.txt"})
    _json(output / "forcefield.json", forcefield)
    emit("solvation", 0.35, "Combining protein, polymer, W water and NaCl into one Martini 3 topology")
    layout, solvation = _assemble_solvated(cfg, output)
    box = str(cfg["box_nm"])
    placement = [caps["executables"]["polyply"], "gen_coords", "-p", "system.top", "-c", "protein_centered.gro", "-o", "system.gro", "-name", "PolymerLab", "-box", box, box, box]
    _execute(placement, output, "polyply_system_coords.log", timeout=3600, what="polyply system coordinate generation")
    lines = (output / "system.gro").read_text().splitlines()
    if len(lines) < 3 or not lines[1].strip().isdigit() or int(lines[1]) != layout["particles"]:
        raise RuntimeError("polyply system.gro particle count disagrees with system.top")
    commands = [{"stage": "solvated_coordinates", "argv": placement, "executed": True, "log": "polyply_system_coords.log",
                 "reproducibility": "polyply 1.8 gen_coords has no seed option; placement is not bitwise reproducible"}]
    gmx = caps["executables"]["gmx"]
    if not gmx:
        return {"state": "blocked", "commands": commands, "requirements": ["Executable: gmx (GROMACS) to run the assembled system"],
                "solvation": solvation, "forcefield": forcefield, "layout": layout}
    threads = _md_threads()
    env = subprocess_environment(gmx)
    # GROMACS refuses an OMP_NUM_THREADS that disagrees with -ntomp.
    env["OMP_NUM_THREADS"] = str(threads)
    version = _gromacs_version(gmx, env)
    plan = [("01_minimize", ["-c", "system.gro"]), ("02_nvt", ["-c", "01_minimize.gro", "-r", "01_minimize.gro"]),
            ("03_npt", ["-c", "02_nvt.gro", "-r", "01_minimize.gro", "-t", "02_nvt.cpt"]), ("04_production", ["-c", "03_npt.gro", "-t", "03_npt.cpt"])]
    for index, (stem, inputs) in enumerate(plan):
        emit("simulate", 0.4 + 0.12 * index, f"GROMACS {stem}: grompp without warning overrides, then mdrun")
        grompp = [gmx, "grompp", "-f", f"{stem}.mdp", *inputs, "-p", "system.top", "-o", f"{stem}.tpr"]
        mdrun = [gmx, "mdrun", "-deffnm", stem, "-ntmpi", "1", "-ntomp", str(threads)]
        _execute(grompp, output, f"gmx_{stem}_grompp.log", timeout=600, env=env, what=f"GROMACS grompp ({stem})")
        _execute(mdrun, output, f"gmx_{stem}_mdrun.log", timeout=43200, env=env, what=f"GROMACS mdrun ({stem})")
        if not (output / f"{stem}.gro").is_file():
            raise RuntimeError(f"GROMACS did not write {stem}.gro")
        if stem == "01_minimize" and "converged to Fmax" not in (output / "gmx_01_minimize_mdrun.log").read_text(errors="replace"):
            raise RuntimeError("Energy minimization did not converge to the requested Fmax; inspect the initial placement before dynamics")
        notes = (output / f"gmx_{stem}_grompp.log").read_text(errors="replace").count("\nNOTE ")
        commands += [{"stage": stem, "argv": grompp, "executed": True, "log": f"gmx_{stem}_grompp.log", "grompp_notes": notes},
                     {"stage": stem, "argv": mdrun, "executed": True, "log": f"gmx_{stem}_mdrun.log"}]
    emit("analyze", 0.9, "Analyzing protein RMSD, polymer contacts and energies")
    analysis = analyze_martini_mixture(output, gmx, env, layout, cfg["contact_cutoff_nm"])
    return {"state": "completed", "commands": commands, "requirements": [], "solvation": solvation, "forcefield": forcefield,
            "layout": layout, "analysis": analysis, "gromacs_version": version, "threads": threads}


def run_bridge(config: dict, output: Path, emit=lambda *args: None):
    """Write reproducible handoff artifacts; optionally generate CG components or run a Martini 3 pilot."""
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    cfg = _validated(config)
    protein = _protein(cfg["pdb_text"], cfg["secondary_structure"])
    caps = bridge_capabilities()
    commands, requirements, generated, notes = [], [], [], []
    simulation = None
    track = cfg["bridge_track"]
    emit("bridge_preflight", 0.1, "Checking force-field components and mixed-system preparation requirements")
    protonation = None
    if cfg["pdb_text"]:
        (output / "protein_input.pdb").write_text(cfg["pdb_text"])
        if cfg["protein_protonation"] == "propka":
            # PROPKA states at the chosen pH, written with AMBER residue names (ASH/GLH/HID/HIE/HIP/LYN) that
            # martinize2 (-from amber), tleap and OpenFF all read; the submitted structure is kept alongside.
            from workbench.protonation import protonate
            emit("protonation", 0.12, f"PDB2PQR + PROPKA: assigning protonation states at pH {cfg['protein_ph']:g}")
            (output / "protein_submitted.pdb").write_text(cfg["pdb_text"])
            protonation = protonate(cfg["pdb_text"], cfg["protein_ph"], output)
            shutil.copyfile(output / "protein_protonated.pdb", output / "protein_input.pdb")
            notes.append(f"Protein protonation assigned by PDB2PQR + PROPKA at pH {protonation['ph']:g}: integer charge {protonation['integer_charge']:+d}, states {protonation['residue_states'] or 'all standard'}.")
            if protonation["omitted_count"]:
                notes.append(f"PDB2PQR omitted {protonation['omitted_count']} hetero group(s) without parameters: {', '.join(protonation['omitted_residues'][:5])}.")
    else:
        requirements.append("protein_input.pdb with reviewed termini, protonation and cofactors")
    if track == "martini3":
        blocks = caps["polyply_libraries"].get("martini3", [])
        if cfg["polyply_residue"] not in blocks:
            requirements.append(f"Installed Martini 3 polyply block {cfg['polyply_residue']}; arbitrary SMILES are not mapped")
        if not cfg["secondary_structure"]:
            requirements.append("Explicit per-residue secondary structure from a reviewed DSSP assignment")
        martinize = [caps["executables"]["martinize2"] or "martinize2", "-f", "protein_input.pdb", "-x", "protein_cg.pdb", "-o", "protein.top", "-ff", "martini3001", "-name", "Protein", "-p", "backbone"]
        if protonation:
            martinize += ["-from", "amber"]
        if cfg["secondary_structure"]:
            martinize += ["-ss", cfg["secondary_structure"]]
        # An elastic network is never assumed; the folded/IDR model is an explicit choice.
        elastic_regions = None
        if cfg["elastic_network"]:
            martinize += ["-elastic", "-ef", "700", "-el", "0.5", "-eu", "0.9"]
            if cfg["elastic_plddt_min"] and cfg["pdb_text"]:
                # Low-confidence (often disordered) residues get no elastic bonds, and confident regions are not tied together.
                elastic_regions = plddt_regions(cfg["pdb_text"], cfg["elastic_plddt_min"])
                _json(output / "elastic_regions.json", elastic_regions)
                martinize += ["-eunit", elastic_regions["eunit"]]
        polyply = [caps["executables"]["polyply"] or "polyply", "gen_params", "-lib", "martini3", "-name", "Polymer", "-seq", f"{cfg['polyply_residue']}:{cfg['monomers']}", "-o", "polymer.itp"]
        commands += [{"stage":"protein_component", "argv":martinize, "requires":["protein_input.pdb", "secondary structure"]},
                     {"stage":"polymer_component", "argv":polyply, "requires":["known Martini 3 polymer block"]}]
        for name in ("martinize2", "polyply", "insane", "gmx"):
            if not caps["executables"][name]: requirements.append(f"Executable: {name}")
        requirements += ["Version-matched Martini 3 nonbonded, water and ion parameters with licenses",
                         "Reviewed polymer mapping, terminal groups and matching protein/polymer nonbonded conventions",
                         "Combined solute coordinates and topology with matching molecule names/order/counts",
                         "Position-restraint includes controlled by POSRES and reviewed index groups",
                         "Solvated system topology with checked charge, neutralization and salt concentration",
                         "GROMACS grompp without warning overrides and minimized energy/force validation"]
        commands.append({"stage":"solvation_after_combined_coordinates_review", "argv":[caps["executables"]["insane"] or "insane", "-f", "mixed_solute.pdb", "-o", "solvated.gro", "-p", "solvation_counts.top", "-pbc", "cubic", "-box", ",".join([str(cfg["box_nm"])]*3), "-sol", "W", "-salt", "0.15"], "requires":["reviewed mixed_solute.pdb", "verified net charge", "merge counts into complete topology"]})
        if cfg["bridge_mode"] == "simulate":
            stages = _gromacs_templates(output, cfg["temperature_kelvin"], cfg["seed"], cfg["equilibration_steps"], cfg["equilibration_steps"], cfg["steps"])
        else:
            stages = _gromacs_templates(output, cfg["temperature_kelvin"], cfg["seed"])
        previous = "solvated.gro"
        for i,stage in enumerate(stages):
            stem = stage["input"].removesuffix(".mdp")
            argv = [caps["executables"]["gmx"] or "gmx", "grompp", "-f", stage["input"], "-c", previous, "-p", "system.top", "-o", stem+".tpr"]
            if i in {1,2}: argv += ["-r", "solvated.gro"]
            if i > 1: argv += ["-t", stages[i-1]["input"].removesuffix(".mdp")+".cpt"]
            commands += [{"stage":stage["name"], "argv":argv, "requires":["all scientific review gates", "previous stage validation"]},
                         {"stage":stage["name"], "argv":[caps["executables"]["gmx"] or "gmx", "mdrun", "-deffnm", stem], "requires":[stem+".tpr"]}]
            previous = stem + ".gro"
        if cfg["bridge_mode"] in {"protein_cg", "simulate"}:
            if not cfg["pdb_text"] or not cfg["secondary_structure"] or cfg["polyply_residue"] not in blocks:
                notes.append("Component execution blocked: input protein, exact secondary structure and known polymer block are required.")
            elif not caps["executables"]["martinize2"] or not caps["executables"]["polyply"]:
                notes.append("Component execution blocked: martinize2 and polyply executables are required.")
            else:
                emit("protein_component", 0.3, "Generating actual protein Martini topology with martinize2")
                _execute(martinize, output, "martinize2.log")
                _execute(polyply, output, "polyply.log")
                for required in ("protein_cg.pdb", "protein.top", "polymer.itp"):
                    if not (output / required).is_file(): raise RuntimeError(f"Missing expected component artifact {required}")
                generated = ["protein_cg.pdb", "protein.top", "polymer.itp"]
                if cfg["bridge_mode"] == "simulate":
                    simulation = _simulate_martini(cfg, output, caps, emit)
                    commands = commands[:2] + simulation["commands"]
                    # Steps executed and checked here are no longer open review gates.
                    requirements = [item for item in requirements if item.startswith(("Installed", "Explicit", "Reviewed polymer"))]
                    requirements += simulation["requirements"]
                    if simulation["state"] == "completed":
                        requirements.append("Longer production and independent replicas before interpreting contacts or adsorption")
                        for stage in stages:
                            stage["state"] = "executed"
        if not cfg["elastic_network"] and cfg["bridge_mode"] == "simulate":
            notes.append("No elastic network: folded proteins can lose tertiary structure in Martini 3; inspect the protein RMSD.")
        if elastic_regions:
            notes.append(f"Elastic bonds only within residues {elastic_regions['eunit']} (CA pLDDT >= {elastic_regions['minimum_plddt']:g}); "
                         "other residues and the links between regions move without elastic restraint.")
        if cfg["protein_polymer_lambda"] != 1 and cfg["bridge_mode"] == "simulate":
            notes.append(f"Protein-polymer Lennard-Jones epsilon scaled by {cfg['protein_polymer_lambda']:g} (experimental): an assumed correction "
                         "for Martini 3 over-aggregation, not fitted for this system; compare with an unscaled run before interpreting contacts.")
        notes += ["SMILES is not converted into a Martini mapping; the explicitly selected library block is the chemical model.",
                  "Protein elastic networks, IDR handling, protonation and cofactors require system-specific choices.",
                  "CG timescale is model-dependent; no microsecond performance or physical-time equivalence is promised."]
    else:
        stages = _amber_templates(output, cfg["temperature_kelvin"], cfg["seed"]) if track == "amber" else []
        requirements += ["Reviewed full polymer chemical graph, stereochemistry, termini and integer net charge",
                         "Protein protonation/termini/cofactors and water/ion model review",
                         "Complete topology with validated exclusions, 1-4 scaling, combination rules and charge sum",
                         "Solvated coordinates, neutralization, ion count and minimum-contact checks"]
        if track == "amber":
            for name in ("antechamber", "parmchk2", "tleap", "sander"):
                if not caps["executables"][name]: requirements.append(f"Executable: {name}")
            requirements.append("Reviewed packed polymer.mol2 and protein_input.pdb in consistent coordinates; AM1-BCC applicability to the full chain")
            commands = [
                {"stage":"polymer_charge", "argv":[caps["executables"]["antechamber"] or "antechamber", "-i", "polymer.sdf", "-fi", "sdf", "-o", "polymer.mol2", "-fo", "mol2", "-at", "gaff2", "-c", "bcc", "-nc", str(cfg["polymer_net_charge"])], "requires":["reviewed capped chain, atom order and input charge"]},
                {"stage":"polymer_parameters", "argv":[caps["executables"]["parmchk2"] or "parmchk2", "-i", "polymer.mol2", "-f", "mol2", "-s", "gaff2", "-o", "polymer.frcmod"], "requires":["inspect missing/estimated parameters"]},
                {"stage":"solvation", "argv":[caps["executables"]["tleap"] or "tleap", "-f", "prepare.leap"], "requires":["replace packed input coordinate placeholders; reviewed charges/parameters"]},
            ]
            (output / "prepare.leap").write_text("""# REVIEW TEMPLATE: this combines ONE reviewed packed polymer chain with protein.
# For multiple chains supply reviewed packed molecules explicitly; do not duplicate coordinates.
source leaprc.protein.ff14SB
source leaprc.gaff2
source leaprc.water.tip3p
loadamberparams polymer.frcmod
polymer = loadmol2 polymer.mol2
protein = loadpdb protein_input.pdb
system = combine {protein polymer}
check system
charge system
solvatebox system TIP3PBOX 12.0
addionsrand system Na+ 0
addionsrand system Cl- 0
# Neutralization only. Add reviewed salt pair count for the desired ionic strength.
saveamberparm system system.prmtop system.inpcrd
savepdb system solvated.pdb
quit
""")
            previous = "system.inpcrd"
            for i,stage in enumerate(stages):
                stem=stage["input"].removesuffix(".in")
                argv=[caps["executables"]["sander"] or "sander", "-O", "-i", stage["input"], "-p", "system.prmtop", "-c", previous, "-r", stem+".rst7", "-o", stem+".out", "-x", stem+".nc"]
                if i < 3: argv += ["-ref", "system.inpcrd"]
                commands.append({"stage":stage["name"], "argv":argv, "requires":["reviewed all-atom topology and each preceding stage validation"]})
                previous=stem+".rst7"
            notes.append("ff14SB/GAFF2/TIP3P is a candidate AMBER family workflow, not evidence that a given polymer is parameterized correctly.")
            if cfg["chains"] != 1:
                requirements.append(f"Amber recipe is a one-chain template; requested {cfg['chains']} packed chains need an explicitly reviewed multi-chain topology")
        else:
            for name in ("openff.toolkit", "openff.nagl", "parmed"):
                if not caps["modules"][name]["available"]: requirements.append(f"Python package: {name}")
            requirements += ["Pinned OpenFF Sage parameter file plus validated polymer coverage",
                             "Pinned charge model plus supported chemical domain (NAGL is optional, not assumed accurate)",
                             "Reviewed protein+SMIRNOFF assembly implementation and an export supported by its engine"]
            notes.append("OpenFF is an integration specification only: this version does not synthesize or execute a mixed protein/SMIRNOFF force field.")
        notes += ["No universal ParmEd/InterMol to LAMMPS conversion is assumed; functional forms and exclusions must survive any export.",
                  "NAGL charge assignment speed and accuracy depend on model, molecule, hardware and applicability domain."]
    notes += ["No bulk 21-step compression is applied to a solvated protein system.",
              "Pilot duration is not proof of equilibration; inspect forces, energy, density, structural stability and independent replicas.",
              "Only the explicit Martini 3 simulate mode runs solvated MD; AMBER/OpenFF recipes never run automatically."]
    _json(output / "command_plan.json", {"schema_version":1,"shell":False,"commands":commands})
    _json(output / "requirements.json", {"missing_or_review_required":requirements,"notes":notes})
    _json(output / "bridge_capabilities.json", caps)
    state = simulation["state"] if simulation else "prepared" if generated else "blocked"
    executed = state == "completed"
    manifest = {"schema_version":1,"created_at":datetime.now(timezone.utc).isoformat(),"track":track,
                "mode":cfg["bridge_mode"],"status":state,"simulation_executed":executed,
                "protein":protein,"protonation":protonation,"requested_polymer":{"library":cfg["polyply_library"],"residue":cfg["polyply_residue"],"monomers":cfg["monomers"],"chains":cfg["chains"]},
                "temperature_kelvin":cfg["temperature_kelvin"],"seed":cfg["seed"],"stages":stages,
                "generated_components":generated,"requirements":requirements,"notes":notes,
                "simulation":{key: simulation.get(key) for key in ("solvation", "forcefield", "layout", "gromacs_version", "threads")} if simulation else None,
                "input_sha256":hashlib.sha256(json.dumps(cfg,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                "artifacts":[{"file":str(p.relative_to(output)),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(output.rglob("*")) if p.is_file()]}
    _json(output / "bridge_manifest.json", manifest)
    emit("bridge_ready", 0.95, "Preparation artifacts written; unresolved scientific gates remain explicit")
    result = {"status":state,"route":"mixture","engine":"bridge_preparation","message":"구성요소 생성 완료; 혼합계 힘장·용매·평형화 검토가 필요합니다." if generated else "준비 레시피를 저장했습니다. 필수 도구·파라미터·과학적 검토 항목을 먼저 해결하세요.",
              "simulation_executed":executed,"generated_components":generated,"requirements":requirements,
              "artifacts":[str(p.relative_to(output)) for p in sorted(output.rglob("*")) if p.is_file()],"bridge":manifest}
    if simulation and simulation["state"] == "blocked":
        result["message"] = "Martini 3 시뮬레이션에 필요한 항목이 없어 중단했습니다. 준비된 파일과 누락 항목을 확인하세요."
    if executed:
        summary = simulation["analysis"]["summary"]
        result.update(engine=f"Martini 3 / polyply / GROMACS {simulation['gromacs_version'] or ''}".strip(),
                      message="용매화한 Martini 3 단백질–고분자 계의 단계별 평형화와 짧은 생산 계산을 완료했습니다. 평형 도달이나 결합력을 뜻하지 않습니다.",
                      metadata={"force_field": "Martini 3.0.0 (martinize2 martini3001 protein; polyply martini3 polymer)",
                                "ensemble": "NPT production after restrained NVT/NPT", "time_step_fs": 10.0,
                                "temperature_kelvin": cfg["temperature_kelvin"], "production_steps": cfg["steps"],
                                "equilibration_steps": cfg["equilibration_steps"], "contact_cutoff_nm": cfg["contact_cutoff_nm"],
                                "elastic_network": cfg["elastic_network"], "salt_molar": cfg["salt_molar"],
                                "elastic_plddt_min": cfg["elastic_plddt_min"] or None, "protein_polymer_lambda": cfg["protein_polymer_lambda"]},
                      metrics={"summary": {"protein_rmsd_mean": summary["protein_rmsd_mean"], "contact_count_mean": summary["contact_count_mean"],
                                           "protein_rg_mean": summary["protein_rg_mean"], "polymer_rg_mean": summary["polymer_rg_mean"],
                                           **({"density_mean": summary["density_mean"]} if "density_mean" in summary else {}),
                                           "production_ps": summary["production_ps"], "frames": summary["frames"],
                                           "particles": simulation["layout"]["particles"]},
                               "units": simulation["analysis"]["units"], "equilibrated": False},
                      warnings=simulation["analysis"]["warnings"] + [note for note in notes if note.startswith(("No elastic network", "Elastic bonds only", "Protein-polymer Lennard-Jones"))])
    return result
