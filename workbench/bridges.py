"""Inspectable protein/polymer preparation recipes; never certify force-field compatibility.

Only the opt-in ``protein_cg`` mode executes: martinize2 and polyply generate
component topologies. Solvation, combined topology and MD remain review gates.
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
from datetime import datetime, timezone

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
            "tracks": ["martini3", "amber", "openff"],
            "execution_scope": "component topology generation only; mixed-system simulation not certified",
            "note": "Discovery reports paths/versions, not successful scientific execution."}


def _validated(config):
    values = {"bridge_track": "martini3", "bridge_mode": "prepare", "polyply_library": "martini3",
              "polyply_residue": "PEO", "monomers": 24, "chains": 2, "temperature_kelvin": 300.0,
              "box_nm": 15.0, "seed": 42, "secondary_structure": "", "pdb_text": "",
              "polymer_net_charge": 0, **config}
    if values["bridge_track"] not in {"martini3", "amber", "openff"}:
        raise ValueError("bridge_track must be martini3, amber or openff")
    if values["bridge_mode"] not in {"prepare", "protein_cg"}:
        raise ValueError("bridge_mode must be prepare or protein_cg")
    if values["bridge_mode"] == "protein_cg" and values["bridge_track"] != "martini3":
        raise ValueError("protein_cg execution is supported only for martini3")
    for key, low, high in (("monomers", 2, 200), ("chains", 1, 16), ("seed", 1, 899999998), ("polymer_net_charge", -100, 100)):
        v = values[key]
        if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
            raise ValueError(f"{key} must be an integer in [{low}, {high}]")
    for key, low, high in (("temperature_kelvin", 250, 400), ("box_nm", 5, 100)):
        v = float(values[key])
        if not math.isfinite(v) or not low <= v <= high:
            raise ValueError(f"Solvated-mixture {key} must be in [{low}, {high}]")
        values[key] = v
    for key in ("polyply_library", "polyply_residue"):
        if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", values[key]):
            raise ValueError(f"Invalid {key}")
    if values["bridge_track"] == "martini3" and values["polyply_library"] != "martini3":
        raise ValueError("Martini 2/beta and Martini 3 libraries must not be mixed; select martini3")
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


def _gromacs_templates(output, temperature, seed):
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
        ("02_nvt.mdp", f"integrator = md\ndt = 0.002\nnsteps = 50000\ndefine = -DPOSRES\ntcoupl = v-rescale\ntc-grps = System\ntau-t = 1.0\nref-t = {temperature}\npcoupl = no\ngen-vel = yes\ngen-temp = {temperature}\ngen-seed = {seed}\n", "restrained NVT", 100),
        ("03_npt.mdp", f"integrator = md\ndt = 0.005\nnsteps = 100000\ndefine = -DPOSRES\ncontinuation = yes\ntcoupl = v-rescale\ntc-grps = System\ntau-t = 1.0\nref-t = {temperature}\npcoupl = C-rescale\npcoupltype = isotropic\ntau-p = 5.0\nref-p = 1.0\ncompressibility = 3e-4\nrefcoord-scaling = com\ngen-vel = no\n", "restrained NPT", 500),
        ("04_production.mdp", f"integrator = md\ndt = 0.010\nnsteps = 100000\ncontinuation = yes\ntcoupl = v-rescale\ntc-grps = System\ntau-t = 1.0\nref-t = {temperature}\npcoupl = C-rescale\npcoupltype = isotropic\ntau-p = 5.0\nref-p = 1.0\ncompressibility = 3e-4\ngen-vel = no\nnstxout-compressed = 1000\n", "production pilot", 1000),
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


def _execute(command, output, logname):
    with (output / logname).open("w") as log:
        result = subprocess.run(command, cwd=output, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, env=subprocess_environment(command[0]), timeout=180)
    if result.returncode:
        raise RuntimeError(f"Component parameterization failed (exit {result.returncode}); inspect {logname}. No warning override applied.")


def run_bridge(config: dict, output: Path, emit=lambda *args: None):
    """Write reproducible handoff artifacts, optionally generate two CG components."""
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    cfg = _validated(config)
    protein = _protein(cfg["pdb_text"], cfg["secondary_structure"])
    caps = bridge_capabilities()
    commands, requirements, generated, notes = [], [], [], []
    track = cfg["bridge_track"]
    emit("bridge_preflight", 0.1, "Checking force-field components and mixed-system preparation requirements")
    if cfg["pdb_text"]:
        (output / "protein_input.pdb").write_text(cfg["pdb_text"])
    else:
        requirements.append("protein_input.pdb with reviewed termini, protonation and cofactors")
    if track == "martini3":
        blocks = caps["polyply_libraries"].get("martini3", [])
        if cfg["polyply_residue"] not in blocks:
            requirements.append(f"Installed Martini 3 polyply block {cfg['polyply_residue']}; arbitrary SMILES are not mapped")
        if not cfg["secondary_structure"]:
            requirements.append("Explicit per-residue secondary structure from a reviewed DSSP assignment")
        martinize = [caps["executables"]["martinize2"] or "martinize2", "-f", "protein_input.pdb", "-x", "protein_cg.pdb", "-o", "protein.top", "-ff", "martini3001", "-name", "Protein", "-p", "backbone"]
        if cfg["secondary_structure"]:
            martinize += ["-ss", cfg["secondary_structure"]]
        # No elastic network is assumed. The appropriate folded/IDR model needs review.
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
        if cfg["bridge_mode"] == "protein_cg":
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
              "Only component topology generation can execute here; recipes never automatically run solvated MD."]
    _json(output / "command_plan.json", {"schema_version":1,"shell":False,"commands":commands})
    _json(output / "requirements.json", {"missing_or_review_required":requirements,"notes":notes})
    _json(output / "bridge_capabilities.json", caps)
    state = "prepared" if generated else "blocked"
    manifest = {"schema_version":1,"created_at":datetime.now(timezone.utc).isoformat(),"track":track,
                "mode":cfg["bridge_mode"],"status":state,"simulation_executed":False,
                "protein":protein,"requested_polymer":{"library":cfg["polyply_library"],"residue":cfg["polyply_residue"],"monomers":cfg["monomers"],"chains":cfg["chains"]},
                "temperature_kelvin":cfg["temperature_kelvin"],"seed":cfg["seed"],"stages":stages,
                "generated_components":generated,"requirements":requirements,"notes":notes,
                "input_sha256":hashlib.sha256(json.dumps(cfg,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                "artifacts":[{"file":str(p.relative_to(output)),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(output.rglob("*")) if p.is_file()]}
    _json(output / "bridge_manifest.json", manifest)
    emit("bridge_ready", 0.95, "Preparation artifacts written; unresolved scientific gates remain explicit")
    return {"status":state,"route":"mixture","engine":"bridge_preparation","message":"구성요소 생성 완료; 혼합계 힘장·용매·평형화 검토가 필요합니다." if generated else "준비 레시피를 저장했습니다. 필수 도구·파라미터·과학적 검토 항목을 먼저 해결하세요.",
            "simulation_executed":False,"generated_components":generated,"requirements":requirements,
            "artifacts":[str(p.relative_to(output)) for p in sorted(output.rglob("*")) if p.is_file()],"bridge":manifest}
