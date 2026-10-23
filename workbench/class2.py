"""Class II (PCFF/COMPASS) parameter lookup ported from LAMMPS msi2lmp, used as an independent check.

LAMMPS tools/msi2lmp converts Materials Studio .frc force fields into LAMMPS class2 coefficients.
REA-pinned source (tools/rea/extension_catalog.json, id lammps-msi2lmp) defines the rules mirrored here:

* ReadFrcFile/SearchAndFill: the first section with each keyword is read; type names are cut to
  4 characters; a repeated type tuple keeps the higher version; missing trailing parameters of
  symmetric cross terms repeat the earlier ones.
* GetParameters/find_match: exact forward or reversed match first, then '*' wildcards; when the
  explicit types fail, the #equivalence column for that interaction is tried.  Cross terms reuse
  the r0/theta0 of the matched bonds and angles; bond-bond_1_3 is only looked up when a 'cp'
  atom type is present, as msi2lmp does.

msi2lmp does not use #auto_equivalence or #bond_increments.  Charges are checked separately from
the bond increments (with the "Bond Inct" auto-equivalence column), the convention EMC uses.
Out-of-plane and angle-angle (improper) terms are not compared.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import re

# keyword -> (members, parameters), from msi2lmp InitializeItems.c for class II
SECTIONS = {
    "atom_types": (1, 1), "equivalence": (6, 0), "auto_equivalence": (10, 0), "nonbond": (1, 2),
    "quartic_bond": (2, 4), "quartic_angle": (3, 4), "torsion_3": (4, 6), "bond-bond": (3, 1),
    "bond-angle": (3, 2), "angle-torsion_3": (4, 6), "angle-angle-torsion_1": (4, 1),
    "end_bond-torsion_3": (4, 6), "middle_bond-torsion_3": (4, 3), "bond-bond_1_3": (4, 1),
    "bond_increments": (2, 2),
}
EQUIVALENCE_COLUMN = {"nonbond": 1, "bond": 2, "angle": 3, "torsion": 4}
# Coefficient tolerance: .frc values have at most 4-5 decimals.
TOLERANCE = 5e-4


class FrcField:
    """The subset of an .frc file that msi2lmp reads for class II, with its matching rules."""

    def __init__(self, text: str, name: str = "frc"):
        self.name = name
        self.version = [line.split()[1:] for line in text.splitlines() if line.startswith("#version")]
        self.items = {key: self._read(text, key, *size) for key, size in SECTIONS.items()}
        self.equivalence = {row[0][0]: row[0] for row in self.items["equivalence"]}
        self.auto_equivalence = {row[0][0]: row[0] for row in self.items["auto_equivalence"]}

    @staticmethod
    def _read(text, keyword, members, parameters):
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines)
                      if line.startswith("#") and re.split(r"[ '\t(]", line, maxsplit=1)[0] == "#" + keyword), None)
        if start is None:
            return []
        entries, seen = [], {}
        started = False
        for line in lines[start + 1:]:
            if line.startswith("#"):
                break
            if line.startswith("!---"):
                started = True
                continue
            if not started or line.startswith(("!", ">", "@")) or not line.strip():
                continue
            tokens = line.split()
            if len(tokens) < 2 + members:
                continue
            version = float(tokens[0])
            types = tuple(t[:4] for t in tokens[2:2 + members])
            values = []
            for i, token in enumerate(tokens[2 + members:2 + members + parameters]):
                try:
                    values.append(float(token))
                except ValueError:
                    break
            # SearchAndFill: missing trailing parameters repeat the earlier ones (symmetric cross terms).
            count = len(values)
            for j in range(count, parameters):
                values.append(values[j - count] if count else 0.0)
            if types in seen:
                index = seen[types]
                if version > entries[index][1]:
                    entries[index] = (types, version, values)
                continue
            seen[types] = len(entries)
            entries.append((types, version, values))
        return entries

    def match(self, keyword, types, reverse=True):
        """msi2lmp find_match: (parameters, backwards) or None."""
        n = len(types)
        for wildcard in (False, True):
            for entry, _, values in self.items[keyword]:
                if all(a == b or (wildcard and b[0] == "*") for a, b in zip(types, entry)):
                    return values, False
                if reverse and n > 1 and all(a == b or (wildcard and b[0] == "*") for a, b in zip(types[::-1], entry)):
                    return values, True
        return None

    def equivalent(self, types, column):
        return tuple(self.equivalence[t][column] if t in self.equivalence else t for t in types)

    def lookup(self, keyword, types, column):
        found = self.match(keyword, types)
        if found is None:
            found = self.match(keyword, self.equivalent(types, column))
        return found

    def lookup_cross(self, keyword, types, column):
        # Cross terms: explicit types, then the equivalences of the parent interaction.
        return self.lookup(keyword, types, column)

    # --- LAMMPS class2 coefficients (order as written by msi2lmp WriteDataFile.c) -------------

    def pair(self, a):
        found = self.lookup("nonbond", (a,), EQUIVALENCE_COLUMN["nonbond"])
        return None if found is None else [found[0][1], found[0][0]]  # epsilon, r0

    def bond(self, a, b):
        found = self.lookup("quartic_bond", (a, b), EQUIVALENCE_COLUMN["bond"])
        return None if found is None else list(found[0])

    def angle(self, a, b, c):
        found = self.lookup("quartic_angle", (a, b, c), EQUIVALENCE_COLUMN["angle"])
        return None if found is None else list(found[0])

    def _r0(self, a, b):
        value = self.bond(a, b)
        return None if value is None else value[0]

    def _theta0(self, a, b, c):
        value = self.angle(a, b, c)
        return None if value is None else value[0]

    def bondbond(self, a, b, c):
        found = self.lookup_cross("bond-bond", (a, b, c), EQUIVALENCE_COLUMN["angle"])
        return None if found is None else [found[0][0], self._r0(a, b), self._r0(b, c)]

    def bondangle(self, a, b, c):
        found = self.lookup_cross("bond-angle", (a, b, c), EQUIVALENCE_COLUMN["angle"])
        if found is None:
            return None
        values, backwards = found
        k1, k2 = (values[1], values[0]) if backwards else (values[0], values[1])
        return [k1, k2, self._r0(a, b), self._r0(b, c)]

    def dihedral(self, a, b, c, d):
        found = self.lookup("torsion_3", (a, b, c, d), EQUIVALENCE_COLUMN["torsion"])
        return None if found is None else list(found[0])

    def _swap6(self, keyword, types):
        found = self.lookup_cross(keyword, types, EQUIVALENCE_COLUMN["torsion"])
        if found is None:
            return None
        values, backwards = found
        return values[3:6] + values[0:3] if backwards else list(values[:6])

    def endbondtorsion(self, a, b, c, d):
        values = self._swap6("end_bond-torsion_3", (a, b, c, d))
        return None if values is None else values + [self._r0(a, b), self._r0(c, d)]

    def middlebondtorsion(self, a, b, c, d):
        found = self.lookup_cross("middle_bond-torsion_3", (a, b, c, d), EQUIVALENCE_COLUMN["torsion"])
        return None if found is None else list(found[0][:3]) + [self._r0(b, c)]

    def angletorsion(self, a, b, c, d):
        values = self._swap6("angle-torsion_3", (a, b, c, d))
        return None if values is None else values + [self._theta0(a, b, c), self._theta0(b, c, d)]

    def angleangletorsion(self, a, b, c, d):
        found = self.lookup_cross("angle-angle-torsion_1", (a, b, c, d), EQUIVALENCE_COLUMN["torsion"])
        return None if found is None else [found[0][0], self._theta0(a, b, c), self._theta0(b, c, d)]

    def bondbond13(self, a, b, c, d, has_cp=True):
        if not has_cp:
            return [0.0, self._r0(a, b), self._r0(c, d)]
        found = self.lookup_cross("bond-bond_1_3", (a, b, c, d), EQUIVALENCE_COLUMN["torsion"])
        return None if found is None else [found[0][0], self._r0(a, b), self._r0(c, d)]

    def increment(self, a, b):
        """Charge moved to atom a from its bond to b; auto-equivalence column 2 is "Bond Inct" (header alignment)."""
        for pair in ((a, b), tuple(self.auto_equivalence[t][2] if t in self.auto_equivalence else t for t in (a, b))):
            for types, _, values in self.items["bond_increments"]:
                if types == pair:
                    return values[0]
                if types == pair[::-1]:
                    return values[1]
        return None

    def mass(self, a):
        found = self.match("atom_types", (a,))
        return None if found is None else found[0][0]


def read_frc(path) -> FrcField:
    path = Path(path)
    return FrcField(path.read_text(errors="replace"), path.name)


# --- comparison against coefficients written by another program ---------------------------------

LAMMPS_TERMS = {
    ("pair_coeff", None): ("pair", 1), ("bond_coeff", None): ("bond", 2), ("angle_coeff", None): ("angle", 3),
    ("angle_coeff", "bb"): ("bondbond", 3), ("angle_coeff", "ba"): ("bondangle", 3),
    ("dihedral_coeff", None): ("dihedral", 4), ("dihedral_coeff", "mbt"): ("middlebondtorsion", 4),
    ("dihedral_coeff", "ebt"): ("endbondtorsion", 4), ("dihedral_coeff", "at"): ("angletorsion", 4),
    ("dihedral_coeff", "aat"): ("angleangletorsion", 4), ("dihedral_coeff", "bb13"): ("bondbond13", 4),
}


def parse_lammps_parameters(text):
    """Coefficient lines with a trailing '# a,b,...' type comment, as written by EMC."""
    rows = []
    for line in text.splitlines():
        if "#" not in line:
            continue
        body, comment = line.split("#", 1)
        tokens = body.split()
        if not tokens or tokens[0] not in {"pair_coeff", "bond_coeff", "angle_coeff", "dihedral_coeff"}:
            continue
        names = tuple(t.strip()[:4] for t in comment.split(",") if t.strip())
        index = 3 if tokens[0] == "pair_coeff" else 2
        style = tokens[index] if len(tokens) > index and not _is_number(tokens[index]) else None
        values = [float(t) for t in tokens[index + (1 if style else 0):] if _is_number(t)]
        rows.append({"command": tokens[0], "style": style, "types": names, "values": values})
    return rows


def _is_number(token):
    try:
        float(token)
        return True
    except ValueError:
        return False


def compare_parameters(text, frc: FrcField, tolerance=TOLERANCE):
    """Recompute every coefficient line from frc with msi2lmp rules and report the differences."""
    rows = parse_lammps_parameters(text)
    has_cp = any("cp" in row["types"] for row in rows)
    report = defaultdict(lambda: {"checked": 0, "matching": 0, "not_in_reference": 0, "max_abs_difference": 0.0})
    differences, missing = [], []
    for row in rows:
        term = LAMMPS_TERMS.get((row["command"], row["style"]))
        if term is None:
            continue
        method, arity = term
        types = row["types"][:1] if method == "pair" else row["types"]
        if len(types) != arity:
            continue
        expected = (frc.bondbond13(*types, has_cp=has_cp) if method == "bondbond13" else getattr(frc, method)(*types))
        entry = report[method]
        if expected is None or any(v is None for v in expected):
            entry["not_in_reference"] += 1
            missing.append({"term": method, "types": list(types)})
            continue
        entry["checked"] += 1
        delta = max((abs(a - b) for a, b in zip(row["values"], expected)), default=0.0)
        entry["max_abs_difference"] = max(entry["max_abs_difference"], round(delta, 6))
        if len(row["values"]) == len(expected) and delta <= tolerance * max(1.0, max(abs(v) for v in expected)):
            entry["matching"] += 1
        else:
            differences.append({"term": method, "types": list(types), "written": row["values"], "reference": expected})
    checked = sum(e["checked"] for e in report.values())
    matching = sum(e["matching"] for e in report.values())
    return {"reference": frc.name, "reference_version": frc.version[-1] if frc.version else None,
            "terms": dict(report), "checked": checked, "matching": matching,
            "not_in_reference": len(missing), "differences": differences[:50], "missing": missing[:50],
            "not_compared": ["improper (wilson_out_of_plane)", "angle-angle"],
            "rules": "msi2lmp GetParameters: exact/reversed, then wildcard, then #equivalence; no #auto_equivalence"}


def read_lammps_data(text):
    """Atom types by name, charges and bonds from a LAMMPS data file whose Masses carry '# name' comments."""
    names, atoms, bonds = {}, {}, []
    section = None
    for line in text.splitlines():
        stripped = line.split("#")[0].strip()
        header = line.strip()
        if header in {"Masses", "Atoms", "Bonds", "Angles", "Dihedrals", "Impropers", "Velocities"} or header.startswith("Atoms "):
            section = header.split()[0]
            continue
        if not stripped or section is None:
            continue
        tokens = stripped.split()
        if section == "Masses" and "#" in line:
            names[int(tokens[0])] = line.split("#", 1)[1].strip()[:4]
        elif section == "Atoms" and len(tokens) >= 7:
            atoms[int(tokens[0])] = {"molecule": int(tokens[1]), "type": int(tokens[2]), "charge": float(tokens[3])}
        elif section == "Bonds" and len(tokens) == 4:
            bonds.append((int(tokens[2]), int(tokens[3])))
    for atom in atoms.values():
        atom["name"] = names.get(atom["type"])
    return atoms, bonds


def compare_charges(data_text, frc: FrcField, tolerance=1e-3):
    """PCFF charges are sums of bond increments; recompute them for every atom of a data file."""
    atoms, bonds = read_lammps_data(data_text)
    neighbours = defaultdict(list)
    for i, j in bonds:
        neighbours[i].append(j)
        neighbours[j].append(i)
    worst, unmatched, missing = 0.0, 0, Counter()
    for index, atom in atoms.items():
        total = 0.0
        for other in neighbours[index]:
            delta = frc.increment(atom["name"], atoms[other]["name"])
            if delta is None:
                missing[f"{atom['name']}-{atoms[other]['name']}"] += 1
                break
            total += delta
        else:
            difference = abs(total - atom["charge"])
            worst = max(worst, difference)
            unmatched += difference > tolerance
    net = sum(a["charge"] for a in atoms.values())
    return {"atoms": len(atoms), "max_abs_difference": round(worst, 6), "atoms_differing": int(unmatched),
            "missing_increments": dict(missing), "net_charge": round(net, 6)}


# Coefficient lines LAMMPS class2 styles need per declared type: (command, cross-term keyword or None, values).
REQUIRED_TERMS = {"bond": [("bond_coeff", None, 4)],
                  "angle": [("angle_coeff", None, 4), ("angle_coeff", "bb", 3), ("angle_coeff", "ba", 4)],
                  "dihedral": [("dihedral_coeff", None, 6), ("dihedral_coeff", "mbt", 4), ("dihedral_coeff", "ebt", 8),
                               ("dihedral_coeff", "at", 8), ("dihedral_coeff", "aat", 3), ("dihedral_coeff", "bb13", 3)],
                  "improper": [("improper_coeff", None, 2), ("improper_coeff", "aa", 6)]}


def check_coefficients(params_text, data_text):
    """Every declared type must have finite class2 coefficients before MD runs. Bonds and angles need
    positive force constants and equilibrium values and LJ sigma must be positive; torsion and
    out-of-plane terms may legitimately be zero. Zero-filled stretches collapse a chain silently."""
    declared = {}
    for line in data_text.splitlines()[:40]:
        tokens = line.split()
        if len(tokens) == 3 and tokens[2] == "types" and tokens[0].isdigit():
            declared[tokens[1]] = int(tokens[0])
    lines = {}
    for raw in params_text.splitlines():
        tokens = raw.split("#", 1)[0].split()
        if not tokens or tokens[0] not in {"pair_coeff", "bond_coeff", "angle_coeff", "dihedral_coeff", "improper_coeff"}:
            continue
        ids = 2 if tokens[0] == "pair_coeff" else 1
        rest = tokens[1 + ids:]
        keyword = rest[0] if rest and not _is_number(rest[0]) else None
        values = [float(t) for t in (rest[1:] if keyword else rest)]
        lines[(tokens[0], keyword, tuple(tokens[1:1 + ids]))] = values
    issues = []
    def problem(term, type_id, message):
        issues.append({"term": term, "type": type_id, "problem": message})
    for i in range(1, declared.get("atom", 0) + 1):
        values = lines.get(("pair_coeff", None, (str(i), str(i))))
        if values is None:
            problem("pair", i, "missing pair_coeff")
        elif len(values) < 2 or not all(map(_finite, values)) or values[0] < 0 or values[1] <= 0:
            problem("pair", i, f"LJ epsilon must be >= 0 and sigma > 0: {values}")
    for kind, terms in REQUIRED_TERMS.items():
        for i in range(1, declared.get(kind, 0) + 1):
            for command, keyword, count in terms:
                values = lines.get((command, keyword, (str(i),)))
                label = kind + (f" {keyword}" if keyword else "")
                if values is None:
                    problem(label, i, f"missing {command}{' ' + keyword if keyword else ''}")
                elif len(values) != count or not all(map(_finite, values)):
                    problem(label, i, f"expected {count} finite values, found {values}")
            main = lines.get((terms[0][0], None, (str(i),)))
            if main and len(main) == terms[0][2] and kind in {"bond", "angle"}:
                equilibrium, force = main[0], main[1]
                upper = 180.0 if kind == "angle" else float("inf")
                if not 0 < equilibrium <= upper or force <= 0:
                    problem(kind, i, f"equilibrium {equilibrium} and quadratic force constant {force} must be positive")
    return {"declared_types": declared, "issues": issues, "passed": not issues,
            "rule": "finite coefficients for every declared type; bond/angle K2 > 0 and r0/theta0 > 0; LJ sigma > 0"}


def _finite(value):
    return value == value and abs(value) != float("inf")
