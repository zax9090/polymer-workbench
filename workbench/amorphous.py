"""Periodic amorphous polymer cells grown chain by chain at the target density.

Each chain copy keeps the template conformer's bond lengths, angles and rigid groups (rings,
multiple bonds, amides); only rotatable single bonds get new dihedrals. The chain is cut at those
bonds into rigid fragments, and fragments are added one at a time to every chain in turn. For each
fragment, trial rotations about its joint bond are scored by a soft excluded-volume energy against
every atom already placed (minimum image), and one is chosen with Rosenbluth weights exp(-U) —
configurational-bias growth as in EMC's energetic grow and Theodorou-Suter amorphous cells, instead
of RadonPy's rigid copies of one conformer packed at 0.05 g/cm^3.

The module needs only numpy and RDKit so that a job directory can carry a copy of it.
"""
from __future__ import annotations
from collections import deque
import math

import numpy as np
from rdkit import Chem

AVOGADRO = 6.02214076e23
# Bondi van der Waals radii (angstrom); other elements use 2.0.
VDW = {"H": 1.10, "C": 1.70, "N": 1.55, "O": 1.52, "F": 1.47, "Si": 2.10, "P": 1.80, "S": 1.80,
       "Cl": 1.75, "Br": 1.85, "I": 1.98}
# RDKit's strict rotatable-bond pattern: no amide C-N, no triple-bond neighbours, no terminal atoms.
ROTATABLE = Chem.MolFromSmarts("[!$([NH]!@C(=O))&!D1&!$(*#*)]-&!@[!$([NH]!@C(=O))&!D1&!$(*#*)]")
OVERLAP_SCALE = 0.75   # pairs closer than 0.75 x (sum of vdW radii) are penalised
STIFFNESS = 20.0       # penalty (kT) for a pair at zero distance
CLASH = 5.0            # a placement scoring this much (kT) or more counts as a dead end


def box_length(mol, chains, density):
    """Cubic box edge (angstrom) holding `chains` copies of `mol` at `density` g/cm^3."""
    mass = sum(atom.GetMass() for atom in mol.GetAtoms()) * chains
    return (mass / AVOGADRO / density * 1e24) ** (1 / 3)


def _fragments(mol):
    """Rigid fragments split at rotatable bonds, in breadth-first order from fragment 0.

    Returns a list of (atoms, joint) where joint is (parent_atom, child_atom) for the bond that
    attaches the fragment to an earlier one (None for the root).
    """
    cut = {frozenset(pair) for pair in mol.GetSubstructMatches(ROTATABLE)}
    label = [-1] * mol.GetNumAtoms()
    groups = []
    for start in range(mol.GetNumAtoms()):
        if label[start] >= 0:
            continue
        label[start] = len(groups)
        members, queue = [], deque([start])
        while queue:
            atom = queue.popleft()
            members.append(atom)
            for neighbor in mol.GetAtomWithIdx(atom).GetNeighbors():
                other = neighbor.GetIdx()
                if label[other] < 0 and frozenset((atom, other)) not in cut:
                    label[other] = len(groups)
                    queue.append(other)
        groups.append(members)
    # Start from a fragment holding a chain end so that growth runs along the backbone.
    ends = [i for i, atom in enumerate(mol.GetAtoms()) if atom.GetAtomicNum() > 1 and atom.GetDegree() - atom.GetTotalNumHs() <= 1]
    root = label[ends[0]] if ends else 0
    order, joints, seen, queue = [], {root: None}, {root}, deque([root])
    while queue:
        group = queue.popleft()
        order.append(group)
        for atom in groups[group]:
            for neighbor in mol.GetAtomWithIdx(atom).GetNeighbors():
                other = neighbor.GetIdx()
                if label[other] not in seen:
                    seen.add(label[other])
                    joints[label[other]] = (atom, other)
                    queue.append(label[other])
    return [(np.array(groups[g]), joints[g]) for g in order]


def _near_pairs(mol, depth=3):
    """For every atom, the atoms within `depth` bonds (excluded from the overlap energy)."""
    near = []
    for start in range(mol.GetNumAtoms()):
        found, frontier = {start}, [start]
        for _ in range(depth):
            frontier = [n.GetIdx() for a in frontier for n in mol.GetAtomWithIdx(a).GetNeighbors() if n.GetIdx() not in found]
            found.update(frontier)
        near.append(found)
    return near


def _rotation(axis, angle):
    axis = axis / np.linalg.norm(axis)
    x, y, z = axis
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
                     [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
                     [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)]])


def _kabsch(source, target):
    """Rotation R and translation t with R @ source_i + t ~= target_i."""
    cs, ct = source.mean(axis=0), target.mean(axis=0)
    u, _, vt = np.linalg.svd((source - cs).T @ (target - ct))
    d = np.sign(np.linalg.det(vt.T @ u.T))
    rot = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    return rot, ct - rot @ cs


def _align(source, target):
    """Rotation taking the direction of `source` onto that of `target`."""
    a, b = source / np.linalg.norm(source), target / np.linalg.norm(target)
    cross, dot = np.cross(a, b), float(np.dot(a, b))
    if np.linalg.norm(cross) < 1e-9:
        if dot > 0:
            return np.eye(3)
        perpendicular = np.cross(a, [1.0, 0.0, 0.0] if abs(a[0]) < 0.9 else [0.0, 1.0, 0.0])
        return _rotation(perpendicular, math.pi)
    return _rotation(cross, math.acos(max(-1.0, min(1.0, dot))))


def _random_rotation(rng):
    q = rng.normal(size=4)
    a, b, c, d = q / np.linalg.norm(q)
    return np.array([[a*a + b*b - c*c - d*d, 2*(b*c - a*d), 2*(b*d + a*c)],
                     [2*(b*c + a*d), a*a - b*b + c*c - d*d, 2*(c*d - a*b)],
                     [2*(b*d - a*c), 2*(c*d + a*b), a*a - b*b - c*c + d*d]])


class _Grid:
    """Cell list of placed atoms in a periodic cubic box."""

    def __init__(self, length, cell):
        self.length = length
        self.n = max(3, int(length // cell))
        self.size = length / self.n
        self.cells = {}

    def key(self, point):
        return tuple((np.floor(point / self.size).astype(int)) % self.n)

    def add(self, indices, points):
        for index, point in zip(indices, points):
            self.cells.setdefault(self.key(point), []).append(index)

    def remove(self, indices, points):
        for index, point in zip(indices, points):
            self.cells[self.key(point)].remove(index)

    def around(self, center, radius):
        reach = min(self.n // 2, int(math.ceil(radius / self.size)))
        cx, cy, cz = np.floor(center / self.size).astype(int)
        found = []
        span = range(-reach, reach + 1)
        n = self.n
        seen = set()
        for dx in span:
            for dy in span:
                for dz in span:
                    key = ((cx + dx) % n, (cy + dy) % n, (cz + dz) % n)
                    if key not in seen:
                        seen.add(key)
                        found.extend(self.cells.get(key, ()))
        return np.array(found, dtype=int)


def build_cell(mol, chains, density, seed=0, trials=12, start_trials=30, extra_trials=48, backtracks=8):
    """Grow `chains` copies of the 3D chain `mol` in a periodic cube at `density` g/cm^3.

    Returns (positions, box, report): positions is (chains * atoms, 3) with every chain unwrapped
    (continuous bonds), box is the cube edge in angstrom with its origin at 0.
    """
    if mol.GetNumConformers() == 0:
        raise ValueError("The chain needs 3D coordinates")
    if chains < 1 or not 0 < density <= 3:
        raise ValueError("chains must be >= 1 and density in (0, 3] g/cm^3")
    rng = np.random.default_rng(seed)
    template = np.asarray(mol.GetConformer().GetPositions(), dtype=float)
    atoms = mol.GetNumAtoms()
    radius = np.array([VDW.get(atom.GetSymbol(), 2.0) for atom in mol.GetAtoms()])
    fragments = _fragments(mol)
    near = _near_pairs(mol)
    near_index = [np.fromiter(found, dtype=int) for found in near]
    length = box_length(mol, chains, density)
    if length < 2 * OVERLAP_SCALE * 2 * radius.max():
        raise ValueError("The box is too small for this chain at this density")
    total = atoms * chains
    positions = np.full((total, 3), np.nan)
    chain_of = np.repeat(np.arange(chains), atoms)
    local = np.tile(np.arange(atoms), chains)
    all_radius = np.tile(radius, chains)
    grid = _Grid(length, cell=2 * OVERLAP_SCALE * radius.max())
    # A rotation about a fragment's joint also fixes where the first atom of each child fragment goes
    # (bond angle and this dihedral), so trials are scored on the fragment plus those atoms.
    member = {int(a): k for k, (frag, _) in enumerate(fragments) for a in frag}
    scored = []
    for frag, _ in fragments:
        ahead = [child for _, joint in fragments if joint is not None and member[joint[0]] == member[int(frag[0])]
                 for child in [joint[1]]]
        scored.append(np.concatenate([frag, np.array(ahead, dtype=int)]))
    reach = {}
    for k, (frag, joint) in enumerate(fragments):
        if joint is not None:
            reach[joint] = float(np.linalg.norm(template[scored[k]] - template[joint[0]], axis=1).max())
    stats = {"fragments": len(fragments), "rotatable_bonds": len(fragments) - 1, "trials": trials,
             "extra_trial_steps": 0, "unresolved_steps": 0}

    def energy(candidates, chain, frag, center, span):
        """Overlap energy (kT) of each trial placement of fragment atoms `frag` in `chain`."""
        neighbors = grid.around(center, span + 2 * OVERLAP_SCALE * radius.max())
        if neighbors.size == 0:
            return np.zeros(len(candidates))
        others = positions[neighbors]
        delta = candidates[:, :, None, :] - others[None, None, :, :]
        delta -= length * np.round(delta / length)
        distance = np.sqrt((delta ** 2).sum(-1))
        limit = OVERLAP_SCALE * (radius[frag][:, None] + all_radius[neighbors][None, :])
        same = chain_of[neighbors] == chain
        if same.any():
            mine = local[neighbors]
            for row, atom in enumerate(frag):
                limit[row, same & np.isin(mine, near_index[atom])] = 0.0
        overlap = np.clip(1.0 - distance / np.where(limit > 0, limit, 1.0), 0.0, None) * (limit > 0)
        return STIFFNESS * (overlap ** 2).sum(axis=(1, 2))

    def place(chain, frag, coordinates):
        index = chain * atoms + frag
        positions[index] = coordinates
        grid.add(index, coordinates)

    # Root fragments: the least crowded of several random positions and orientations.
    root, root_scored = fragments[0][0], scored[0]
    for chain in range(chains):
        options = []
        for _ in range(start_trials):
            rot = _random_rotation(rng)
            body = (template[root_scored] - template[root].mean(axis=0)) @ rot.T + rng.uniform(0, length, 3)
            options.append(body)
        options = np.array(options)
        span = float(np.linalg.norm(template[root_scored] - template[root].mean(axis=0), axis=1).max())
        scores = np.array([energy(options[i:i + 1], chain, root_scored, options[i][:len(root)].mean(axis=0), span)[0]
                           for i in range(len(options))])
        place(chain, root, options[int(np.argmin(scores))][:len(root)])

    parent_of = [None] + [member[joint[0]] for _, joint in fragments[1:]]
    subtree = [[k] for k in range(len(fragments))]
    for k in range(len(fragments) - 1, 0, -1):
        subtree[parent_of[k]].extend(subtree[k])
    stats["backtracks"] = 0

    def grow(chain, k, accept):
        """Place fragment k of `chain`; False (nothing placed) when every trial overlaps and not `accept`."""
        frag, (parent, child) = fragments[k]
        atoms_scored, offset = scored[k], chain * atoms
        # Template atoms of the placed parent side fix the frame: the parent and its placed neighbours.
        anchor = [parent] + [n.GetIdx() for n in mol.GetAtomWithIdx(parent).GetNeighbors()
                             if n.GetIdx() != child and np.isfinite(positions[offset + n.GetIdx(), 0])]
        placed = positions[offset + np.array(anchor)]
        if len(anchor) >= 3:
            rot, shift = _kabsch(template[anchor], placed)
        else:
            # Two anchors fix only their bond direction; the turn about it is the previous dihedral, already sampled.
            rot = _align(template[anchor[1]] - template[anchor[0]], placed[1] - placed[0])
            shift = placed[0] - rot @ template[anchor[0]]
        base = template[atoms_scored] @ rot.T + shift
        pivot = positions[offset + parent]
        axis = base[list(frag).index(child)] - pivot
        for count in (trials, extra_trials):
            angles = rng.uniform(0, 2 * math.pi) + 2 * math.pi * np.arange(count) / count
            candidates = np.array([(base - pivot) @ _rotation(axis, a).T + pivot for a in angles])
            scores = energy(candidates, chain, atoms_scored, pivot, reach[(parent, child)])
            if scores.min() < CLASH:
                break
            stats["extra_trial_steps"] += count == trials
        if scores.min() >= CLASH and not accept:
            return False
        weights = np.exp(-(scores - scores.min()))
        place(chain, frag, candidates[rng.choice(len(candidates), p=weights / weights.sum())][:len(frag)])
        return True

    def unplace(chain, k):
        index = chain * atoms + fragments[k][0]
        grid.remove(index, positions[index])
        positions[index] = np.nan

    for k in range(1, len(fragments)):
        for chain in range(chains):
            # Dead end: take back a growing stretch of this chain's ancestors and grow it again.
            for attempt in range(backtracks):
                if grow(chain, k, accept=False):
                    break
                stats["backtracks"] += 1
                top = k
                for _ in range(1 + attempt // 2):
                    if parent_of[top]:
                        top = parent_of[top]
                redo = sorted(j for j in subtree[top] if j < k)
                for j in reversed(redo):
                    unplace(chain, j)
                for j in redo:
                    grow(chain, j, accept=True)
            else:
                grow(chain, k, accept=True)
                stats["unresolved_steps"] += 1

    report = {"builder": "native configurational-bias growth", "chains": chains, "atoms": total,
              "density_g_cm3": density, "box_angstrom": length, "seed": seed, **stats,
              **contact_report(positions, length, chain_of, local, near, all_radius)}
    return positions, length, report


def contact_report(positions, length, chain_of, local, near, radius):
    """Closest non-excluded atom pair (minimum image) and the number of pairs under 1.2 angstrom."""
    from scipy.spatial import cKDTree
    wrapped = np.mod(positions, length)
    # cKDTree's periodic box needs coordinates strictly inside [0, length).
    wrapped[wrapped >= length] = 0.0
    tree = cKDTree(wrapped, boxsize=length)
    pairs = tree.query_pairs(2.0, output_type="ndarray")
    if len(pairs):
        keep = ~((chain_of[pairs[:, 0]] == chain_of[pairs[:, 1]]) &
                 np.array([local[j] in near[local[i]] for i, j in pairs]))
        pairs = pairs[keep]
    if not len(pairs):
        return {"closest_contact_angstrom": None, "contacts_under_1_2_angstrom": 0}
    delta = wrapped[pairs[:, 0]] - wrapped[pairs[:, 1]]
    delta -= length * np.round(delta / length)
    distance = np.sqrt((delta ** 2).sum(1))
    return {"closest_contact_angstrom": round(float(distance.min()), 3),
            "contacts_under_1_2_angstrom": int((distance < 1.2).sum())}
