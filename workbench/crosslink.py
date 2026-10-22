"""Epoxy–amine network curing with LAMMPS fix bond/react on an EMC-built PCFF cell.

Template recipe (REA-pinned LAMMPS examples/PACKAGES/reaction/tiny_epoxy, id lammps-reaction):
pre- and post-reaction molecule templates with Coords/Types/Charges/Molecules and every bonded
interaction among their atoms, plus a map file with InitiatorIDs, EdgeIDs and Equivalences.

Here the templates are cut from molecules that EMC typed itself, so every type id and coefficient
is the one in the system data file:

* EMC builds the resin/hardener mixture together with one copy of each reaction product
  (primary-amine adduct A, secondary-amine adduct B).  The products give the data file every
  post-reaction PCFF type; LAMMPS deletes them before the run.
* RDKit makes the product by editing bonds of the combined reactants (C-O ring bond -> C-N, N-H ->
  O-H), so pre and post atoms share indices.  networkx matches each EMC molecule to its RDKit graph.
* A template holds every atom within 3 bonds of an atom whose bonds, type or charge change; atoms
  bonded to anything outside are edge atoms.  Other reactive groups must not fall inside it.

Two reactions run concurrently: primary amine + epoxide and secondary amine + epoxide.  Etherification
(hydroxyl + epoxide) and homopolymerization are not modelled.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path

from . import engines, pcff

EPOXIDE = "[CH2:1]1[O:2][CX4;H1,H0:3]1"
AMINE = "[NX3;H2,H1;!$(N[C,S,P]=[O,S,N]);!$(N#*);!$(N=*)]"
TEMPLATE_RADIUS = 3
# Reacting atoms move under nve/limit for this many steps before rejoining the thermostat.
STABILIZE_STEPS = 100
RELAX_STEPS = 2000
MAX_ATOMS = 30000


def _mol(smiles):
    from rdkit import Chem
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"SMILES could not be parsed: {smiles}")
    if any(atom.GetAtomicNum() == 0 for atom in mol.GetAtoms()):
        raise ValueError("Resin and hardener are whole molecules; remove [*] attachment points")
    if Chem.GetFormalCharge(mol):
        raise ValueError("Resin and hardener must be neutral")
    return Chem.AddHs(mol)


def sites(resin, hardener):
    from rdkit import Chem
    epoxides = resin.GetSubstructMatches(Chem.MolFromSmarts(EPOXIDE))
    amines = [m[0] for m in hardener.GetSubstructMatches(Chem.MolFromSmarts(AMINE)) if hardener.GetAtomWithIdx(m[0]).GetTotalNumHs(includeNeighbors=True) == 2]
    if len(epoxides) < 2:
        raise ValueError("The resin needs at least two terminal epoxide (glycidyl) groups to form a network")
    if not amines:
        raise ValueError("The hardener needs a primary amine (-NH2)")
    hydrogens = sum(hardener.GetAtomWithIdx(n).GetTotalNumHs(includeNeighbors=True) for n in amines)
    return epoxides, amines, hydrogens


def react(epoxy_mol, epoxide, amine_mol, nitrogen):
    """Ring-opening addition on combined indices: epoxy atoms first, then the amine molecule."""
    from rdkit import Chem
    combined = Chem.RWMol(Chem.CombineMols(epoxy_mol, amine_mol))
    offset = epoxy_mol.GetNumAtoms()
    carbon, oxygen = epoxide[0], epoxide[1]
    n = nitrogen + offset
    hydrogen = next(a.GetIdx() for a in combined.GetAtomWithIdx(n).GetNeighbors() if a.GetAtomicNum() == 1)
    combined.RemoveBond(carbon, oxygen)
    combined.AddBond(n, carbon, Chem.BondType.SINGLE)
    combined.RemoveBond(n, hydrogen)
    combined.AddBond(oxygen, hydrogen, Chem.BondType.SINGLE)
    product = combined.GetMol()
    Chem.SanitizeMol(product)
    return product, {"carbon": carbon, "oxygen": oxygen, "nitrogen": n, "hydrogen": hydrogen}


def species(resin_smiles, hardener_smiles):
    from rdkit import Chem
    resin, hardener = _mol(resin_smiles), _mol(hardener_smiles)
    epoxides, amines, hydrogens = sites(resin, hardener)
    adduct_a, centre_a = react(resin, epoxides[0], hardener, amines[0])
    # Secondary amine: the same nitrogen of adduct A now opens a second resin.
    nitrogen_in_a = amines[0] + resin.GetNumAtoms()
    adduct_b, centre_b = react(resin, epoxides[0], adduct_a, nitrogen_in_a)
    smiles = {name: Chem.MolToSmiles(Chem.RemoveHs(m)) for name, m in (("resin", resin), ("hardener", hardener), ("adduct_a", adduct_a), ("adduct_b", adduct_b))}
    return {"resin": resin, "hardener": hardener, "adduct_a": adduct_a, "adduct_b": adduct_b, "smiles": smiles,
            "epoxides_per_resin": len(epoxides), "amine_hydrogens_per_hardener": hydrogens,
            "reactions": [{"name": "primary", "pre": ("resin", "hardener"), "post": "adduct_a", "centre": centre_a},
                          {"name": "secondary", "pre": ("resin", "adduct_a"), "post": "adduct_b", "centre": centre_b}],
            "epoxides": epoxides, "amines": amines}


def chemistry(spec, resins, hardeners, seed):
    groups = "\n".join(f"{name}\t\t{spec['smiles'][name]}" for name in ("resin", "hardener", "adduct_a", "adduct_b"))
    return f"""ITEM	OPTIONS

replace		true
field		pcff
number		true
density		{pcff.BUILD_DENSITY}
seed		{seed}
emc_execute	false

ITEM	END

ITEM	GROUPS

{groups}

ITEM	END

ITEM	CLUSTERS

resin		resin,{resins}
hardener	hardener,{hardeners}
adduct_a	adduct_a,1
adduct_b	adduct_b,1

ITEM	END
"""


def read_topology(path):
    """Every section of an EMC LAMMPS data file, keyed by atom and molecule."""
    masses, atoms = {}, {}
    interactions = {"Bonds": [], "Angles": [], "Dihedrals": [], "Impropers": []}
    section = None
    for line in Path(path).read_text().splitlines():
        body = line.split("#")[0].strip()
        if not body:
            continue
        tokens = body.split()
        if tokens[0][0].isalpha():
            section = tokens[0]
            continue
        if section == "Masses":
            masses[int(tokens[0])] = float(tokens[1])
        elif section == "Atoms":
            atoms[int(tokens[0])] = {"molecule": int(tokens[1]), "type": int(tokens[2]), "charge": float(tokens[3]),
                                     "xyz": [float(v) for v in tokens[4:7]]}
        elif section in interactions:
            interactions[section].append((int(tokens[1]), [int(v) for v in tokens[2:]]))
    return masses, atoms, interactions


def match_molecule(rdkit_mol, atom_ids, atoms, masses, bonds):
    """RDKit atom index -> data-file atom id for one EMC molecule (element-labelled graph isomorphism)."""
    import networkx as nx
    from networkx.algorithms import isomorphism
    members = set(atom_ids)
    emc = nx.Graph()
    for i in atom_ids:
        emc.add_node(i, element=pcff.element(masses[atoms[i]["type"]]))
    for _, (i, j) in bonds:
        if i in members and j in members:
            emc.add_edge(i, j)
    reference = nx.Graph()
    for atom in rdkit_mol.GetAtoms():
        reference.add_node(atom.GetIdx(), element=atom.GetSymbol())
    for bond in rdkit_mol.GetBonds():
        reference.add_edge(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())
    matcher = isomorphism.GraphMatcher(reference, emc, node_match=lambda a, b: a["element"] == b["element"])
    mapping = next(matcher.isomorphisms_iter(), None)
    if mapping is None:
        raise RuntimeError("EMC molecule does not match the RDKit graph; EMC may have changed the chemistry")
    return mapping


def template_atoms(pre, post, changed):
    """Atoms within TEMPLATE_RADIUS bonds of a changing atom in the pre or post graph."""
    from rdkit import Chem
    before, after = Chem.GetDistanceMatrix(pre), Chem.GetDistanceMatrix(post)
    return sorted(i for i in range(post.GetNumAtoms())
                  if min(min(before[c][i], after[c][i]) for c in changed) <= TEMPLATE_RADIUS)


def build_templates(spec, topology, molecules):
    """Pre/post molecule templates and maps for both reactions, in the system's own type ids."""
    masses, atoms, interactions = topology
    by_molecule = defaultdict(list)
    for index, atom in atoms.items():
        by_molecule[atom["molecule"]].append(index)
    mapped = {name: match_molecule(spec[name], by_molecule[molecules[name]], atoms, masses, interactions["Bonds"])
              for name in ("resin", "hardener", "adduct_a", "adduct_b")}
    templates = []
    for number, reaction in enumerate(spec["reactions"], 1):
        first, second = reaction["pre"]
        offset = spec[first].GetNumAtoms()
        # Combined index -> data atom id in the pre state (two molecules) and post state (one).
        pre_ids = {**{i: mapped[first][i] for i in range(offset)},
                   **{i + offset: mapped[second][i] for i in range(spec[second].GetNumAtoms())}}
        post_ids = mapped[reaction["post"]]
        centre = reaction["centre"]
        changed = {centre["carbon"], centre["oxygen"], centre["nitrogen"], centre["hydrogen"]}
        for i in pre_ids:
            before, after = atoms[pre_ids[i]], atoms[post_ids[i]]
            if before["type"] != after["type"] or abs(before["charge"] - after["charge"]) > 1e-6:
                changed.add(i)
        from rdkit import Chem
        pre_mol, product = Chem.CombineMols(spec[first], spec[second]), spec[reaction["post"]]
        selected = template_atoms(pre_mol, product, changed)
        selected_set = set(selected)
        # Other reactive groups inside the template would make it match only some network states.
        resin_atoms = spec["resin"].GetNumAtoms()
        hardener_offset = offset if second == "hardener" else offset + resin_atoms
        others = {i for epoxide in spec["epoxides"][1:] for i in epoxide}
        if second == "adduct_a":
            others |= {offset + i for epoxide in spec["epoxides"][1:] for i in epoxide}
        for nitrogen in spec["amines"][1:]:
            others.update(a.GetIdx() + hardener_offset for a in spec["hardener"].GetAtomWithIdx(nitrogen).GetNeighbors() if a.GetAtomicNum() == 1)
        if others & selected_set:
            raise ValueError("Reactive groups lie within 3 bonds of each other; fixed reaction templates would not match every cure state")
        edges = [i for i in selected
                 if ({a.GetIdx() for a in product.GetAtomWithIdx(i).GetNeighbors()} | {a.GetIdx() for a in pre_mol.GetAtomWithIdx(i).GetNeighbors()}) - selected_set]
        if set(edges) & changed:
            raise ValueError("A changing atom would sit on the template edge; the reacting molecules are too small for the 3-bond template")
        # LAMMPS bond/react rewrites every template charge unless a custom_charges fragment names the
        # atoms to update (fix_bond_react.cpp: recharged = 1 by default); edge atoms must keep theirs.
        recharged = [i for i in selected if abs(atoms[pre_ids[i]]["charge"] - atoms[post_ids[i]]["charge"]) > 1e-6]
        templates.append({"number": number, "name": reaction["name"],
                          "pre": _template(f"rxn{number}_pre", selected, pre_ids, atoms, interactions, molecule_of=lambda i: 1 if i < offset else 2,
                                           fragment=recharged),
                          "post": _template(f"rxn{number}_post", selected, post_ids, atoms, interactions, molecule_of=lambda i: 1),
                          "map": _map(selected, centre, edges), "atoms": len(selected), "edge_atoms": len(edges),
                          "changed_atoms": len(changed), "recharged_atoms": len(recharged)})
    return templates


def _template(title, selected, ids, atoms, interactions, molecule_of, fragment=None):
    local = {ids[i]: n for n, i in enumerate(selected, 1)}
    lines = [title, "", f"{len(selected)} atoms"]
    blocks = []
    for section in ("Bonds", "Angles", "Dihedrals", "Impropers"):
        rows = [(kind, [local[a] for a in members]) for kind, members in interactions[section] if all(a in local for a in members)]
        if rows:
            lines.append(f"{len(rows)} {section.lower()}")
            blocks.append((section, rows))
    if fragment:
        position = {i: n for n, i in enumerate(selected, 1)}
        lines += ["1 fragments", "", "Fragments", "", "recharge " + " ".join(str(position[i]) for i in fragment)]
    lines += ["", "Coords", ""] + [f"{n} {' '.join(f'{v:.6f}' for v in atoms[ids[i]]['xyz'])}" for n, i in enumerate(selected, 1)]
    lines += ["", "Types", ""] + [f"{n} {atoms[ids[i]]['type']}" for n, i in enumerate(selected, 1)]
    lines += ["", "Charges", ""] + [f"{n} {atoms[ids[i]]['charge']:.6f}" for n, i in enumerate(selected, 1)]
    lines += ["", "Molecules", ""] + [f"{n} {molecule_of(i)}" for n, i in enumerate(selected, 1)]
    for section, rows in blocks:
        lines += ["", section, ""] + [f"{n} {kind} {' '.join(map(str, members))}" for n, (kind, members) in enumerate(rows, 1)]
    return "\n".join(lines) + "\n"


def _map(selected, centre, edges):
    local = {i: n for n, i in enumerate(selected, 1)}
    lines = ["# Polymer Lab epoxy-amine map: pre and post templates share atom order", "",
             f"{len(edges)} edgeIDs", f"{len(selected)} equivalences", "", "InitiatorIDs", "",
             str(local[centre["nitrogen"]]), str(local[centre["carbon"]]), "", "EdgeIDs", ""]
    lines += [str(local[i]) for i in edges]
    lines += ["", "Equivalences", ""] + [f"{n} {n}" for n in range(1, len(selected) + 1)]
    return "\n".join(lines) + "\n"


def lammps_input(spec, molecules, counts, config, steps):
    temperature, cure = float(config.get("temperature_kelvin", 300.0)), float(config["cure_temperature_kelvin"])
    seed = int(config["seed"]) % 900000 + 1000
    epoxides = counts["resin"] * spec["epoxides_per_resin"]
    cutoff = float(config["reaction_cutoff_angstrom"])
    lines = ["# Polymer Lab epoxy-amine cure: EMC PCFF cell, LAMMPS fix bond/react (two concurrent reactions)",
             "units real", "atom_style full", "boundary p p p",
             "variable cutoff index 9.5", "variable charge_cutoff index 9.5",
             "read_data polymer.data extra/bond/per/atom 4 extra/angle/per/atom 16 extra/dihedral/per/atom 32 extra/improper/per/atom 8 extra/special/per/atom 40",
             "include polymer.params",
             # Products typed only to supply post-reaction types; remove them before the run.
             f"group products molecule {molecules['adduct_a']} {molecules['adduct_b']}", "delete_atoms group products bond yes", "group products delete",
             # Liquid mixture densified with the same 21-step schedule, ending at the cure temperature.
             *pcff.eq21_lines(cure, steps["eq21"], seed, shake=False),
             "write_data uncured.data", "reset_timestep 0", f"thermo {steps['thermo']}",
             "molecule pre1 rxn1_pre.molecule_template", "molecule post1 rxn1_post.molecule_template",
             "molecule pre2 rxn2_pre.molecule_template", "molecule post2 rxn2_post.molecule_template",
             f"fix rxns all bond/react stabilization yes statted_grp 0.03 reset_mol_ids yes "
             f"react rxn1 all 1 0.0 {cutoff} pre1 post1 rxn1.map stabilize_steps {STABILIZE_STEPS} custom_charges recharge "
             f"react rxn2 all 1 0.0 {cutoff} pre2 post2 rxn2.map stabilize_steps {STABILIZE_STEPS} custom_charges recharge",
             f"fix cure statted_grp_REACT nvt temp {cure} {cure} 100.0",
             f"variable conversion equal (f_rxns[1]+f_rxns[2])/{epoxides}",
             f"thermo_style custom {pcff.THERMO} f_rxns[1] f_rxns[2] v_conversion",
             "log cure.log",
             f"fix stop all halt {steps['thermo']} v_conversion >= {float(config['target_conversion'])} error continue",
             f"run {steps['cure']}", "log cooling.log", f"thermo_style custom {pcff.THERMO}",
             "unfix stop", "unfix cure", "unfix rxns",
             # Bonds made in the last steps are still stretched; relax them before any barostat.
             "minimize 1.0e-4 1.0e-6 5000 50000", "fix settle all nve/limit 0.05",
             f"fix calm all langevin {cure} {cure} 100.0 {seed}", f"run {RELAX_STEPS}", "unfix calm", "unfix settle",
             "write_data cured.data",
             f"fix cool all npt temp {cure} {temperature} 100.0 iso 1.0 1.0 1000.0", f"run {steps['cool']}", "unfix cool",
             "log sampling.log", "reset_timestep 0", f"thermo {steps['thermo']}",
             f"fix sample all npt temp {temperature} {temperature} 100.0 iso 1.0 1.0 1000.0", f"run {steps['sampling']}", "unfix sample",
             "write_data final.data"]
    return "\n".join(lines) + "\n"


def schedule(scale, cure_steps):
    eq21, sampling, thermo = pcff.schedule(scale)
    return {"eq21": eq21, "eq21_total": sum(map(sum, eq21)), "cure": int(cure_steps), "cool": max(1000, int(500000 * scale)),
            "sampling": sampling, "thermo": thermo}


def conversion_series(path, epoxides, hydrogens):
    """Epoxide and amine-hydrogen conversion from the cure log's thermo rows."""
    rows, header = [], None
    for line in Path(path).read_text(errors="replace").splitlines():
        tokens = line.split()
        if tokens and tokens[0] == "Step" and "f_rxns[1]" in tokens:
            header = tokens
            continue
        if header and len(tokens) == len(header):
            try:
                values = dict(zip(header, map(float, tokens)))
            except ValueError:
                header = None
                continue
            first, second = values["f_rxns[1]"], values["f_rxns[2]"]
            rows.append({"step": int(values["Step"]), "primary": int(first), "secondary": int(second),
                         "epoxide_conversion": (first + second) / epoxides, "amine_hydrogen_conversion": (first + second) / hydrogens,
                         "density": values.get("Density")})
    return rows


def gel_point(spec, counts):
    """Flory–Stockmayer ideal gel conversion of epoxide for an f_a amine-H / f_e epoxide system."""
    epoxides = counts["resin"] * spec["epoxides_per_resin"]
    hydrogens = counts["hardener"] * spec["amine_hydrogens_per_hardener"]
    f_e, f_a = spec["epoxides_per_resin"], spec["amine_hydrogens_per_hardener"]
    if f_e < 2 or f_a < 2 or not epoxides or not hydrogens:
        return None
    # Macosko–Miller: alpha_e^2 (f_e-1)(f_a-1) / r = 1 with r = amine-H / epoxide groups.
    ratio = hydrogens / epoxides
    value = (ratio / ((f_e - 1) * (f_a - 1))) ** 0.5
    # Epoxide conversion cannot exceed the amine hydrogens available (or 1); beyond that no gel forms.
    return {"epoxide_conversion": value if value <= min(1.0, ratio) else None, "amine_hydrogen_per_epoxide": ratio,
            "assumptions": "equal reactivity of all amine hydrogens, no intramolecular loops, no etherification"}


def cure(config, output, emit, run_lammps):
    resins, hardeners = int(config["resin_count"]), int(config["hardener_count"])
    spec = species(config["resin_smiles"], config["hardener_smiles"])
    emc_root, record = engines.ensure("emc")
    emit("build", 0.2, f"EMC {record['version']}: PCFF typing of resin, hardener and both products; {resins}+{hardeners} molecules at {pcff.BUILD_DENSITY} g/cm^3")
    from .external import subprocess_environment
    (output / "polymer.esh").write_text(chemistry(spec, resins, hardeners, int(config["seed"])))
    pcff.emc_run(output, emc_root, subprocess_environment("perl"))
    topology = read_topology(output / "polymer.data")
    molecules = {"resin": 1, "hardener": resins + 1, "adduct_a": resins + hardeners + 1, "adduct_b": resins + hardeners + 2}
    atoms = len(topology[1]) - sum(1 for a in topology[1].values() if a["molecule"] in (molecules["adduct_a"], molecules["adduct_b"]))
    if atoms > MAX_ATOMS:
        raise ValueError("This desktop cure route is limited to 30000 atoms per cell")
    if atoms < 200:
        raise ValueError(f"A {atoms}-atom cell is too small for periodic sampling; add molecules")
    templates = build_templates(spec, topology, molecules)
    for template in templates:
        n = template["number"]
        (output / f"rxn{n}_pre.molecule_template").write_text(template["pre"])
        (output / f"rxn{n}_post.molecule_template").write_text(template["post"])
        (output / f"rxn{n}.map").write_text(template["map"])
    check = pcff.parity(output)
    steps = schedule(float(config.get("protocol_scale", 1.0)), int(config["cure_steps"]))
    counts = {"resin": resins, "hardener": hardeners}
    (output / "in.cure").write_text(lammps_input(spec, molecules, counts, config, steps))
    emit("simulate", 0.35, f"LAMMPS bond/react at {config['cure_temperature_kelvin']} K until {config['target_conversion']:.0%} epoxide conversion or {steps['cure']} steps")
    run_lammps(output, "in.cure")
    epoxides = resins * spec["epoxides_per_resin"]
    hydrogens = hardeners * spec["amine_hydrogens_per_hardener"]
    series = conversion_series(output / "cure.log", epoxides, hydrogens)
    if not series:
        raise RuntimeError("The cure log has no bond/react thermo rows")
    temperature = float(config.get("temperature_kelvin", 300.0))
    values, criteria, printed, density, window = pcff.analyze(output, temperature)
    final = series[-1]
    complete = {"force_field": "PCFF (EMC 9.4.4 typing, cff91 class2)", "charge_method": "PCFF bond increments",
                "reaction": "epoxide + primary/secondary amine ring opening (LAMMPS fix bond/react)",
                "sampling_description": "NPT sampling of the cured network (21-step densified mixture, fix bond/react cure, NPT cooling)",
                "species": spec["smiles"], "counts": counts, "epoxide_groups": epoxides, "amine_hydrogens": hydrogens,
                "templates": [{k: t[k] for k in ("number", "name", "atoms", "edge_atoms", "changed_atoms", "recharged_atoms")} for t in templates],
                "builder": {**record, "build_density_g_cm3": pcff.BUILD_DENSITY},
                "cure_temperature_kelvin": float(config["cure_temperature_kelvin"]), "target_conversion": float(config["target_conversion"]),
                "reaction_cutoff_angstrom": float(config["reaction_cutoff_angstrom"]), "stage_steps": steps, "time_step_fs": 1.0,
                "conversion": {"epoxide": final["epoxide_conversion"], "amine_hydrogen": final["amine_hydrogen_conversion"],
                               "primary_reactions": final["primary"], "secondary_reactions": final["secondary"],
                               "target_reached": final["epoxide_conversion"] >= float(config["target_conversion"]), "series": series[-500:]},
                "gel_point_ideal": gel_point(spec, counts),
                "properties": {"density": {"value": pcff.number(values, "density"), "unit": "g/cm^3"},
                               "bulk_modulus": {"value": pcff.number(values, "bulk_modulus", 1e-9), "unit": "GPa"},
                               "heat_capacity_cp": {"value": pcff.number(values, "Cp"), "unit": "J/(kg K)"},
                               "isothermal_compressibility": {"value": pcff.number(values, "compressibility", 1e9), "unit": "1/GPa"},
                               "volume_expansion": {"value": pcff.number(values, "volume_expansion"), "unit": "1/K"}},
                "temperature_kelvin": temperature, "atoms": atoms, "radonpy_criteria_met": criteria,
                "convergence_warnings": [line.split("RadonPy warning:", 1)[1].strip() for line in printed.splitlines() if "RadonPy warning:" in line],
                "density_series": density[-2000:], "analysis_window_rows": window,
                "parity": {"coefficients_checked": check["coefficients"]["checked"], "coefficients_matching": check["coefficients"]["matching"],
                           "coefficients_not_in_reference": check["coefficients"]["not_in_reference"],
                           "charge_max_abs_difference": check["charges"]["max_abs_difference"], "net_charge": check["charges"]["net_charge"]},
                "protocol_scale": float(config.get("protocol_scale", 1.0)), "equilibrated": False}
    (output / "cure_complete.json").write_text(json.dumps(complete, indent=2))
    pcff.write_pdb(output / "final.data", output / "structure.pdb")
    return complete
