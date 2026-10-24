"""Checks a job configuration before it is queued: boundary validation, the route chemistry that
otherwise fails only inside the worker, atom counts against route limits, and required engines.
Nothing is created or started.
"""
from __future__ import annotations

from pydantic import ValidationError

from .models import JobConfig

REPEAT_ROUTES = {"pysimm", "packmol"}
BULK_MINIMUM_ATOMS = 200
BUILDER_HEAVY_ATOMS = 1000


def _uses_repeat_unit(cfg):
    if cfg.route == "polymer_atomistic":
        return cfg.atomistic_mode not in {"crosslink", "tg_scan"}
    return cfg.route in REPEAT_ROUTES and not cfg.source_job_id


def vinyl_candidates(mol, limit=4):
    """Repeat units from opening one non-aromatic C=C (1,2-addition); candidates for review, not a mechanism."""
    from rdkit import Chem
    out = []
    for bond in mol.GetBonds():
        a, b = bond.GetBeginAtom(), bond.GetEndAtom()
        if bond.GetBondType() != Chem.BondType.DOUBLE or bond.GetIsAromatic() or a.GetAtomicNum() != 6 or b.GetAtomicNum() != 6:
            continue
        rw = Chem.RWMol(mol)
        rw.GetBondWithIdx(bond.GetIdx()).SetBondType(Chem.BondType.SINGLE)
        for atom in (a.GetIdx(), b.GetIdx()):
            dummy = rw.AddAtom(Chem.Atom(0))
            rw.AddBond(atom, dummy, Chem.BondType.SINGLE)
        try:
            candidate = rw.GetMol()
            Chem.SanitizeMol(candidate)
        except (ValueError, RuntimeError):
            continue
        smiles = Chem.MolToSmiles(candidate).replace("*", "[*]")
        if smiles not in out:
            out.append(smiles)
        if len(out) >= limit:
            break
    return out


def repeat_unit(smiles):
    """Same attachment rules as science.build_linear_polymer, reported instead of raised."""
    from rdkit import Chem
    unit = Chem.MolFromSmiles(smiles)
    if unit is None:
        return {"valid": False, "error": "반복단위 SMILES를 읽을 수 없습니다."}
    dummies = [a for a in unit.GetAtoms() if a.GetAtomicNum() == 0]
    report = {"valid": True, "smiles": smiles, "attachments": len(dummies)}
    if len(Chem.GetMolFrags(unit)) != 1:
        return {**report, "valid": False, "error": "반복단위는 하나로 연결된 분자여야 합니다."}
    if len(dummies) != 2:
        report.update(valid=False, error=f"반복단위에는 연결점 '*'이 정확히 2개 필요합니다(현재 {len(dummies)}개).")
        if not dummies:
            candidates = vinyl_candidates(unit)
            if candidates:
                report["candidates"] = candidates
                report["candidate_note"] = "Opening one C=C gives these head-to-tail repeat units; confirm the chemistry before using one."
        return report
    if any(a.GetDegree() != 1 or a.GetBonds()[0].GetBondType() != Chem.BondType.SINGLE for a in dummies):
        return {**report, "valid": False, "error": "연결점 '*'은 각각 단일 결합 하나로만 이어져야 합니다."}
    heavy = sum(1 for a in unit.GetAtoms() if a.GetAtomicNum() > 1)
    with_h = Chem.AddHs(unit)
    hydrogens = sum(1 for a in with_h.GetAtoms() if a.GetAtomicNum() == 1)
    return {**report, "heavy_atoms": heavy, "atoms": heavy + hydrogens}


def _engines(cfg):
    from .external import available, executable
    from . import engines
    needs = []
    if cfg.route == "polymer_atomistic":
        if cfg.atomistic_mode != "tg_scan":
            needs.append(("RDKit", available("rdkit")))
        if cfg.atomistic_mode in {"bulk", "crosslink", "tg_scan"}:
            needs.append(("LAMMPS", bool(executable("lmp", "LAMMPS_EXEC"))))
        if cfg.atomistic_mode == "bulk" and cfg.atomistic_forcefield == "gaff2":
            needs.append(("RadonPy", available("radonpy")))
            if cfg.charge_method == "xtb_cm5":
                needs.append(("xtb", engines.status("xtb").get("available") or "download_on_first_use"))
        if cfg.atomistic_mode == "crosslink" or cfg.atomistic_forcefield == "pcff":
            needs.append(("EMC", engines.status("emc").get("available") or "download_on_first_use"))
    elif cfg.route == "packmol":
        needs += [("RDKit", available("rdkit")), ("PACKMOL", bool(executable("packmol", "PACKMOL_EXEC")))]
    elif cfg.route == "pysimm":
        needs += [("pysimm", available("pysimm")), ("LAMMPS", bool(executable("lmp", "LAMMPS_EXEC")))]
    elif cfg.route == "mixture" and cfg.bridge_mode == "simulate":
        needs += [("GROMACS", bool(executable("gmx", "GROMACS_EXEC"))), ("polyply", bool(executable("polyply", "POLYPLY_EXEC")))]
    return [{"engine": name, "available": state is True, "note": state if isinstance(state, str) else None} for name, state in needs]


def check(config):
    """Return what submission would do with this configuration, without submitting it."""
    try:
        cfg = JobConfig.model_validate(config)
    except ValidationError as exc:
        errors = [{"field": ".".join(str(p) for p in error["loc"]) or "config", "message": error["msg"]} for error in exc.errors()]
        return {"valid": False, "errors": errors, "warnings": [], "estimate": None, "engines": []}
    errors, warnings, estimate = [], [], {"chains": cfg.chains, "monomers": cfg.monomers}
    try:
        import rdkit  # noqa: F401
        chemistry = True
    except ImportError:
        chemistry = False
        warnings.append("RDKit is not installed here, so repeat-unit and resin chemistry were not checked.")
    if chemistry and _uses_repeat_unit(cfg):
        unit = repeat_unit(cfg.repeat_smiles)
        estimate["repeat_unit"] = unit
        if not unit["valid"]:
            errors.append({"field": "repeat_smiles", "message": unit["error"]})
        else:
            per_chain = unit["atoms"] * cfg.monomers + 2  # hydrogen caps on both chain ends
            estimate.update(atoms_per_chain=per_chain, total_atoms=per_chain * cfg.chains,
                            heavy_atoms_per_chain=unit["heavy_atoms"] * cfg.monomers)
            if cfg.route == "polymer_atomistic" and cfg.atomistic_mode in {"build", "simulate"} and unit["heavy_atoms"] * cfg.monomers > BUILDER_HEAVY_ATOMS:
                errors.append({"field": "monomers", "message": f"사슬 빌더는 중원자 {BUILDER_HEAVY_ATOMS:,}개까지입니다(현재 {unit['heavy_atoms'] * cfg.monomers:,}개)."})
            if cfg.atomistic_mode == "bulk" and cfg.route == "polymer_atomistic" and estimate["total_atoms"] < BULK_MINIMUM_ATOMS:
                errors.append({"field": "chains", "message": f"주기 벌크 셀은 {BULK_MINIMUM_ATOMS}개 이상의 원자가 필요합니다(현재 약 {estimate['total_atoms']}개). 사슬 수나 길이를 늘리세요."})
    if chemistry and cfg.route == "polymer_atomistic" and cfg.atomistic_mode == "crosslink":
        from . import crosslink
        try:
            spec = crosslink.species(cfg.resin_smiles, cfg.hardener_smiles)
            atoms = cfg.resin_count * spec["resin"].GetNumAtoms() + cfg.hardener_count * spec["hardener"].GetNumAtoms()
            estimate.update(total_atoms=atoms, epoxides_per_resin=spec["epoxides_per_resin"],
                            amine_hydrogens_per_hardener=spec["amine_hydrogens_per_hardener"])
            if atoms < BULK_MINIMUM_ATOMS:
                errors.append({"field": "resin_count", "message": f"가교 셀은 {BULK_MINIMUM_ATOMS}개 이상의 원자가 필요합니다(현재 {atoms}개)."})
        except ValueError as exc:
            errors.append({"field": "resin_smiles", "message": str(exc)})
    if cfg.route == "polymer_atomistic" and cfg.atomistic_mode == "tg_scan":
        from .glass_transition import temperatures
        temps = temperatures(cfg.tg_high_kelvin, cfg.tg_low_kelvin, cfg.tg_step_kelvin)
        estimate = {"temperatures_kelvin": temps, "steps_per_temperature": cfg.tg_stage_steps,
                    "total_steps": cfg.tg_stage_steps * (len(temps) + 1), "simulated_ps": cfg.tg_stage_steps * (len(temps) + 1) / 1000,
                    "cooling_rate_k_per_ns": round(cfg.tg_step_kelvin / (cfg.tg_stage_steps * 1e-6), 6)}
        warnings.append("MD cooling is many decades faster than DSC; the scan's Tg is expected above a calorimetric value and is not rate-converted.")
    if cfg.route == "polymer_atomistic" and cfg.atomistic_mode in {"bulk", "crosslink"}:
        estimate["protocol_scale"] = cfg.protocol_scale
        if cfg.protocol_scale == 1:
            warnings.append("protocol_scale 1 runs the full published stage lengths, which take hours to days on a desktop.")
        else:
            warnings.append("protocol_scale below 1 shortens every stage; the result is exploratory and does not establish equilibrium.")
    engine_rows = _engines(cfg)
    missing = [row["engine"] for row in engine_rows if not row["available"] and not row["note"]]
    if missing:
        warnings.append("Missing engines: " + ", ".join(missing) + "; the job would finish as blocked.")
    warnings.append("Run time is not estimated; it depends on the machine, thread count and stage lengths.")
    return {"valid": not errors, "errors": errors, "warnings": warnings, "estimate": estimate, "engines": engine_rows,
            "config": cfg.model_dump(exclude={"pdb_text", "a3m_text", "forcefield_itp_text", "mdp_text", "engine_input"})}


def prepare_submission(manager, cfg):
    """Resolve source-job files and reject what would fail in the worker; returns (config, report).

    Shared by the job API and paste-to-run sheets so both apply identical checks. Raises ValueError
    with a Korean message, or KeyError for an unknown source job.
    """
    if cfg.route == "polymer_atomistic" and cfg.atomistic_mode == "tg_scan":
        from .glass_transition import required_files
        source = manager.get(cfg.source_job_id)
        files = required_files(source["config"])
        directory = manager.directory(cfg.source_job_id)
        if source["status"] != "completed" or any((directory / n).is_symlink() or not (directory / n).is_file() for n in files):
            raise ValueError("완료된 벌크 셀 또는 가교 작업의 최종 구조 파일(" + ", ".join(files) + ")이 필요합니다.")
        cfg.source_artifact = files[0]
    elif cfg.source_job_id:
        source = manager.get(cfg.source_job_id)
        if cfg.route not in {"packmol", "mixture"} or source["status"] not in {"completed", "prepared"}:
            raise ValueError("완료된 원자 구조 작업을 PACKMOL 또는 혼합계 브릿지로 이어갈 수 있습니다.")
        if source["config"]["route"] in {"polymer_cg", "hoomd"}:
            raise ValueError("조대화 비드 좌표는 원자 구조와 단위·모델이 달라 직접 연결할 수 없습니다.")
        path = manager.directory(cfg.source_job_id) / cfg.source_artifact
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".pdb" or path.stat().st_size > 5000000:
            raise ValueError("5 MB 이하의 PDB 구조 파일이 필요합니다.")
        cfg.pdb_text = path.read_text()
    # Chemistry that would only fail inside the worker is rejected before a job exists.
    report = check(cfg.model_dump())
    if report["errors"]:
        message = " ".join(error["message"] for error in report["errors"])
        candidates = ((report.get("estimate") or {}).get("repeat_unit") or {}).get("candidates")
        if candidates:
            message += " 반복단위 후보(화학 구조를 확인한 뒤 사용): " + ", ".join(candidates)
        raise ValueError(message)
    return cfg, report
