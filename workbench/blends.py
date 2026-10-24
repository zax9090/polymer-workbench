"""Flory-Huggins chi from pair contacts, after the Blends method (Fan, Olafson, Blanco and Hsu,
Macromolecules 25, 3667 (1992); the method Materials Studio Blends implements).

Each component is one rigid molecule: a solvent, or a polymer repeat unit whose attachment points are
capped. For the four pairs (base-base, base-screen, screen-base, screen-screen) the Rust kernel
(native/, module workbench_native) samples random van der Waals contacts and their intermolecular
GAFF2 energies, and packs molecules around a central one to count coordination numbers Z. Then

    E_mix(T) = 1/2 [Z_bs <E_bs>_T + Z_sb <E_sb>_T - Z_bb <E_bb>_T - Z_ss <E_ss>_T]
    chi(T)   = E_mix(T) / (R T)

with Boltzmann averages <E>_T over the sampled contacts. chi refers to the base molecule's size.
Without the compiled kernel a numpy implementation of the same sampling runs (slower).
"""
from __future__ import annotations
import math

import numpy as np

R_KCAL = 0.0019872043  # kcal mol^-1 K^-1
# Bondi van der Waals radii (angstrom) define contact; other elements use 2.0.
VDW = {"H": 1.10, "C": 1.70, "N": 1.55, "O": 1.52, "F": 1.47, "Si": 2.10, "P": 1.80, "S": 1.80,
       "Cl": 1.75, "Br": 1.85, "I": 1.98}
PAIRS = ("bb", "bs", "sb", "ss")
MAX_ATOMS = 300

try:
    import workbench_native as native
except ImportError:  # pragma: no cover - exercised where the Rust kernel is not built
    native = None


def kernel():
    return {"engine": "rust", "version": native.__version__} if native else {"engine": "numpy", "version": np.__version__}


def capped(smiles, cap="hydrogen"):
    """The neutral single molecule a component becomes once its attachment points are capped (with hydrogens)."""
    from rdkit import Chem
    if cap not in ("hydrogen", "methyl"):
        raise ValueError("cap must be hydrogen or methyl")
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"SMILES를 읽을 수 없습니다: {smiles}")
    attachments = [atom for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0]
    if attachments:
        editable = Chem.RWMol(mol)
        for atom in attachments:
            atom = editable.GetAtomWithIdx(atom.GetIdx())
            atom.SetAtomicNum(1 if cap == "hydrogen" else 6)
            atom.SetIsotope(0)
            atom.SetNoImplicit(False)
        mol = editable.GetMol()
        Chem.SanitizeMol(mol)
        mol = Chem.MolFromSmiles(Chem.MolToSmiles(mol))
    if Chem.GetFormalCharge(mol) != 0 or len(Chem.GetMolFrags(mol)) != 1:
        raise ValueError("각 성분은 전하가 없는 분자 하나여야 합니다.")
    mol = Chem.AddHs(mol)
    if mol.GetNumAtoms() > MAX_ATOMS:
        raise ValueError(f"성분 하나는 원자 {MAX_ATOMS}개 이하여야 합니다.")
    return mol


def prepare(smiles, cap="hydrogen", seed=1):
    """3D conformer and GAFF2 nonbonded parameters (Gasteiger charges) of one component."""
    from rdkit import Chem
    from rdkit.Chem import AllChem
    mol = capped(smiles, cap)
    params = AllChem.ETKDGv3()
    params.randomSeed = int(seed)
    if AllChem.EmbedMolecule(mol, params) != 0:
        params.useRandomCoords = True
        if AllChem.EmbedMolecule(mol, params) != 0:
            raise ValueError(f"3D 구조를 만들 수 없습니다: {smiles}")
    if AllChem.MMFFHasAllMoleculeParams(mol):
        AllChem.MMFFOptimizeMolecule(mol, maxIters=2000)
    from radonpy.ff.gaff2_mod import GAFF2_mod
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        assigned = GAFF2_mod().ff_assign(mol, charge="gasteiger")
    if not assigned:
        raise ValueError(f"GAFF2 파라미터를 붙일 수 없습니다: {smiles}")
    atoms = list(mol.GetAtoms())
    return {"smiles": Chem.MolToSmiles(Chem.RemoveHs(mol)), "atoms": len(atoms),
            "positions": np.asarray(mol.GetConformer().GetPositions(), dtype=float),
            "sigma": np.array([a.GetDoubleProp("ff_sigma") for a in atoms]),
            "epsilon": np.array([a.GetDoubleProp("ff_epsilon") for a in atoms]),
            "charge": np.array([a.GetDoubleProp("AtomicCharge") for a in atoms]),
            "radius": np.array([VDW.get(a.GetSymbol(), 2.0) for a in atoms]),
            "mass": sum(a.GetMass() for a in atoms)}


def _arrays(component):
    return (np.ascontiguousarray(component["positions"]), component["sigma"], component["epsilon"], component["charge"], component["radius"])


# --- numpy reference implementation of the kernel --------------------------------------------------

def _rotation(rng):
    q = rng.normal(size=4)
    a, b, c, d = q / np.linalg.norm(q)
    return np.array([[a*a + b*b - c*c - d*d, 2*(b*c - a*d), 2*(b*d + a*c)],
                     [2*(b*c + a*d), a*a - b*b + c*c - d*d, 2*(c*d - a*b)],
                     [2*(b*d - a*c), 2*(c*d + a*b), a*a - b*b - c*c + d*d]])


def place_in_contact(fixed, moving_rotated, u, scale=1.0):
    """Moving coordinates shifted along u to first van der Waals contact with `fixed` (None if never)."""
    w = fixed["centered"][:, None, :] - moving_rotated[None, :, :]
    wu = w @ u
    contact = scale * (fixed["radius"][:, None] + fixed["moving_radius"][None, :])
    disc = wu ** 2 - (w ** 2).sum(-1) + contact ** 2
    if not (disc >= 0).any():
        return None
    t = np.max(np.where(disc >= 0, wu + np.sqrt(np.clip(disc, 0, None)), -np.inf))
    return moving_rotated + t * u


def pair_energy(a_positions, a, b_positions, b, dielectric=1.0):
    """Intermolecular LJ (Lorentz-Berthelot) + Coulomb energy, kcal/mol."""
    r = np.linalg.norm(a_positions[:, None, :] - b_positions[None, :, :], axis=-1)
    sigma = 0.5 * (a["sigma"][:, None] + b["sigma"][None, :])
    eps = np.sqrt(a["epsilon"][:, None] * b["epsilon"][None, :])
    s6 = (sigma / r) ** 6
    return float((4 * eps * (s6 * s6 - s6)).sum() + (332.06371 * np.outer(a["charge"], b["charge"]) / (dielectric * r)).sum())


def _numpy_statistics(fixed, moving, samples, clusters, seed, scale, dielectric, max_failures):
    rng = np.random.default_rng(seed)
    center = lambda c: c["positions"] - c["positions"].mean(axis=0)
    a = {**fixed, "centered": center(fixed), "moving_radius": moving["radius"]}
    body = center(moving)

    def contact():
        while True:
            u = rng.normal(size=3)
            u /= np.linalg.norm(u)
            placed = place_in_contact(a, body @ _rotation(rng).T, u, scale)
            if placed is not None:
                return placed
    energies = np.array([pair_energy(a["centered"], fixed, contact(), moving, dielectric) for _ in range(samples)])
    counts = []
    for _ in range(clusters):
        kept, failures = [], 0
        while failures < max_failures:
            placed = contact()
            limit = scale * (moving["radius"][:, None] + moving["radius"][None, :])
            if any((np.linalg.norm(placed[:, None] - other[None], axis=-1) < limit * (1 - 1e-9)).any() for other in kept):
                failures += 1
            else:
                kept.append(placed)
                failures = 0
        counts.append(len(kept))
    return energies, np.array(counts, dtype=float)


def contact_statistics(fixed, moving, samples, clusters, seed, scale=1.0, dielectric=1.0, max_failures=300):
    """Contact energies (kcal/mol) of `moving` around `fixed`, and coordination numbers per cluster."""
    if native is not None:
        energies = native.contact_energies(_arrays(fixed), _arrays(moving), int(samples), int(seed), float(scale), float(dielectric))
        counts = native.coordination_numbers(_arrays(fixed), _arrays(moving), int(clusters), int(seed), float(scale), int(max_failures))
        return np.asarray(energies), np.asarray(counts)
    return _numpy_statistics(fixed, moving, samples, clusters, seed, scale, dielectric, max_failures)


def boltzmann_average(energies, temperature):
    """<E>_T over uniformly sampled contacts; shifted by the minimum to keep the exponentials finite."""
    shifted = energies - energies.min()
    weights = np.exp(-shifted / (R_KCAL * temperature))
    return float((energies * weights).sum() / weights.sum())


def chi_from_statistics(stats, temperatures):
    """chi(T) and E_mix(T) (kcal/mol) from the four pairs' energies and mean coordination numbers."""
    z = {pair: float(stats[pair]["z"].mean()) for pair in PAIRS}
    rows = []
    for temperature in temperatures:
        mean = {pair: boltzmann_average(stats[pair]["energies"], temperature) for pair in PAIRS}
        e_mix = 0.5 * (z["bs"] * mean["bs"] + z["sb"] * mean["sb"] - z["bb"] * mean["bb"] - z["ss"] * mean["ss"])
        rows.append({"temperature_kelvin": float(temperature), "e_mix_kcal_mol": e_mix, "chi": e_mix / (R_KCAL * temperature),
                     "mean_energy_kcal_mol": mean})
    return rows


def phase_behaviour(a, b, n_base, n_screen):
    """Flory-Huggins critical chi, critical temperature and spinodal for chi(T) = a + b/T."""
    chi_c = 0.5 * (1 / math.sqrt(n_base) + 1 / math.sqrt(n_screen)) ** 2
    phi_c = math.sqrt(n_screen) / (math.sqrt(n_base) + math.sqrt(n_screen))
    out = {"critical_chi": chi_c, "critical_volume_fraction_base": phi_c, "critical_temperature_kelvin": None, "kind": None, "spinodal": []}
    if b == 0 or chi_c == a:
        out["kind"] = "temperature-independent chi"
        return out
    t_c = b / (chi_c - a)
    if t_c <= 0:
        out["kind"] = "miscible at all temperatures" if (a + b / 300.0) < chi_c else "immiscible at all temperatures"
        return out
    out["critical_temperature_kelvin"] = t_c
    out["kind"] = "UCST" if b > 0 else "LCST"
    for phi in np.linspace(0.02, 0.98, 49):
        chi_s = 0.5 * (1 / (n_base * phi) + 1 / (n_screen * (1 - phi)))
        t = b / (chi_s - a) if chi_s != a else None
        if t is not None and t > 0:
            out["spinodal"].append({"volume_fraction_base": round(float(phi), 4), "temperature_kelvin": float(t)})
    return out


def blend_chi(base_smiles, screen_smiles, temperatures=None, samples=50000, clusters=200, seed=1, cap="hydrogen",
              scale=1.0, dielectric=1.0, n_base=1.0, n_screen=1.0, bootstrap=50):
    """Blends-style chi(T) between two components, with bootstrap errors and Flory-Huggins phase behaviour."""
    if not 1000 <= samples <= 2_000_000:
        raise ValueError("samples는 1,000~2,000,000 사이여야 합니다.")
    if not 20 <= clusters <= 5000:
        raise ValueError("clusters는 20~5,000 사이여야 합니다.")
    if native is None and samples * 4 > 200_000:
        raise ValueError("Rust 커널(workbench_native)이 없어 numpy로 계산합니다. samples를 50,000 이하로 줄이거나 커널을 빌드하세요.")
    temperatures = [float(t) for t in (temperatures or np.arange(250.0, 501.0, 25.0))]
    if not temperatures or min(temperatures) <= 0 or len(temperatures) > 200:
        raise ValueError("온도는 양수 1~200개여야 합니다.")
    if n_base < 1 or n_screen < 1:
        raise ValueError("중합도(n_base, n_screen)는 1 이상이어야 합니다.")
    base, screen = prepare(base_smiles, cap, seed), prepare(screen_smiles, cap, seed + 1)
    components = {"b": base, "s": screen}
    stats = {}
    for k, pair in enumerate(PAIRS):
        energies, z = contact_statistics(components[pair[0]], components[pair[1]], samples, clusters, seed + 101 * (k + 1), scale, dielectric)
        stats[pair] = {"energies": energies, "z": z}
    rows = chi_from_statistics(stats, temperatures)
    inverse = np.array([1 / t for t in temperatures])
    chis = np.array([row["chi"] for row in rows])
    slope, intercept = np.polyfit(inverse, chis, 1) if len(temperatures) > 1 else (0.0, float(chis[0]))
    reference = min(temperatures, key=lambda t: abs(t - 298.15))
    rng = np.random.default_rng(seed + 7)
    replicates = []
    for _ in range(int(bootstrap)):
        resampled = {pair: {"energies": rng.choice(stats[pair]["energies"], size=len(stats[pair]["energies"])),
                            "z": rng.choice(stats[pair]["z"], size=len(stats[pair]["z"]))} for pair in PAIRS}
        replicates.append(chi_from_statistics(resampled, [reference])[0]["chi"])
    reference_row = next(row for row in rows if row["temperature_kelvin"] == reference)
    error = float(np.std(replicates)) if replicates else None
    warnings = [
        "Rigid single conformers and fixed Gasteiger charges: conformational and polarization effects are missing.",
        "chi is referenced to the base molecule's size; for polymers, to the capped repeat unit.",
        "Contact sampling omits the many-body packing of a real liquid; compare trends and orderings, not absolute values, with experiment.",
    ]
    if error is not None and error > 0.2:
        warnings.insert(0, f"The bootstrap error of chi (±{error:.2f}) is large; raise samples and clusters before reading the A + B/T fit or the phase result.")
    return {
        "method": "Blends-style pair contact sampling (Fan et al., Macromolecules 25, 3667 (1992))",
        "kernel": kernel(),
        "force_field": "GAFF2_mod (RadonPy) LJ with Lorentz-Berthelot mixing, Gasteiger charges, Coulomb with dielectric %g, no cutoff" % dielectric,
        "components": {name: {k: component[k] for k in ("smiles", "atoms", "mass")} for name, component in (("base", base), ("screen", screen))},
        "settings": {"samples_per_pair": samples, "clusters_per_pair": clusters, "seed": seed, "cap": cap, "contact_scale": scale,
                     "n_base": n_base, "n_screen": n_screen},
        "coordination": {pair: {"mean": float(stats[pair]["z"].mean()), "std": float(stats[pair]["z"].std())} for pair in PAIRS},
        "energies": {pair: {"mean_kcal_mol": float(stats[pair]["energies"].mean()), "min_kcal_mol": float(stats[pair]["energies"].min())} for pair in PAIRS},
        "chi_reference": {"temperature_kelvin": reference, "chi": reference_row["chi"],
                          "bootstrap_std": error},
        "chi_vs_temperature": rows,
        "fit": {"form": "chi = A + B/T", "A": float(intercept), "B_kelvin": float(slope)},
        "phase": phase_behaviour(float(intercept), float(slope), n_base, n_screen),
        "warnings": warnings,
    }
