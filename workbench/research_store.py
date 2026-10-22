"""Local research index. The immutable job artifacts remain the source of truth."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import threading
import uuid

SCHEMA_VERSION = 1
TERMINAL = {"completed", "prepared", "failed", "blocked", "cancelled", "interrupted"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
PROPERTY_UNITS = {
    "tg": "K", "young_modulus": "MPa", "tensile_strength": "MPa",
    "binding_affinity": "nM", "contact_count": "count", "hbond_count": "count",
    "protein_rmsd": "angstrom", "molecular_weight": "g/mol",
    "mn": "g/mol", "mw": "g/mol", "dispersity": "dimensionless", "decomposition_temperature": "K",
    "density": "g/cm^3", "melting_temperature": "K", "melting_enthalpy": "J/g", "crystallinity": "fraction",
    "hsp_delta_d": "MPa^0.5", "hsp_delta_p": "MPa^0.5", "hsp_delta_h": "MPa^0.5", "hsp_radius": "MPa^0.5",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _read(path, fallback=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if fallback is None else fallback


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def material_identity(material: dict) -> dict:
    """Keep unknown identity fields: sequence, tacticity and preparation are not discarded.

    This is a conservative identity key, not a claim of chemical equivalence. Different
    chain lengths/compositions/conditions remain different unless explicitly reconciled.
    """
    if not isinstance(material, dict) or not material:
        raise ValueError("A nonempty material identity is required")
    identity = json.loads(_json(material))
    for key in ("repeat_smiles", "smiles", "resin_smiles", "hardener_smiles"):
        if identity.get(key):
            from rdkit import Chem
            mol = Chem.MolFromSmiles(str(identity[key]))
            if mol is None:
                raise ValueError(f"Invalid {key}")
            identity[key] = Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True)
    return {"material_id": "mat_" + hashlib.sha256(_json(identity).encode()).hexdigest(), "identity": identity}


def material_from_job(job):
    config = job.get("config") or {}
    route = config.get("route") or job.get("route")
    fields = ("repeat_smiles", "monomers", "chains", "sequence", "tacticity", "end_caps",
              "composition", "accession", "source_job_id", "source_artifact", "polyply_library",
              "polyply_residue", "ligand_smiles", "protein_sequence", "material_label")
    if route == "polymer_atomistic" and config.get("atomistic_mode") == "crosslink":
        # A cured network is defined by its resin and hardener, not by the form's repeat unit.
        material = {key: config[key] for key in ("resin_smiles", "hardener_smiles", "resin_count", "hardener_count") if config.get(key) not in (None, "")}
        material["representation"] = "epoxy_amine_network"
        return material_identity(material)
    material = {key: config[key] for key in fields if config.get(key) not in (None, "")}
    material["representation"] = "unmapped_soft_bead_chain" if route in {"polymer_cg", "hoomd"} else str(route)
    # The generic bead simulation does not use the form's repeat-unit SMILES or protein fields.
    if route in {"polymer_cg", "hoomd"}:
        material = {key: material[key] for key in ("monomers", "chains", "representation") if key in material}
    elif route not in {"protein", "alphafold", "packmol", "mixture"}:
        for key in ("accession", "sequence", "protein_sequence"):
            material.pop(key, None)
    if config.get("pdb_text"):
        material["pdb_sha256"] = hashlib.sha256(config["pdb_text"].encode()).hexdigest()
    return material_identity(material)


def validate_measurement(record: dict) -> dict:
    if not isinstance(record, dict):
        raise ValueError("Measurement must be an object")
    row = json.loads(_json(record))
    prop = str(row.get("property", "")).strip()
    if prop not in PROPERTY_UNITS:
        raise ValueError("Unsupported property: " + prop)
    if isinstance(row.get("value"), bool):
        raise ValueError("Measurement value must be finite numeric data")
    try:
        value = float(row["value"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Measurement value must be finite numeric data") from exc
    if not math.isfinite(value):
        raise ValueError("Measurement value must be finite")
    unit = str(row.get("unit", "")).strip()
    units = {
        "tg": {"K": (1, 0), "C": (1, 273.15), "°C": (1, 273.15)},
        "young_modulus": {"Pa": (1e-6, 0), "kPa": (1e-3, 0), "MPa": (1, 0), "GPa": (1e3, 0)},
        "tensile_strength": {"Pa": (1e-6, 0), "kPa": (1e-3, 0), "MPa": (1, 0), "GPa": (1e3, 0)},
        "binding_affinity": {"M": (1e9, 0), "mM": (1e6, 0), "uM": (1e3, 0), "µM": (1e3, 0), "nM": (1, 0)},
        "contact_count": {"count": (1, 0)}, "hbond_count": {"count": (1, 0)},
        "protein_rmsd": {"angstrom": (1, 0), "Å": (1, 0), "nm": (10, 0)},
        "molecular_weight": {"g/mol": (1, 0), "kg/mol": (1e3, 0), "Da": (1, 0)},
        "mn": {"g/mol": (1, 0), "kg/mol": (1e3, 0), "Da": (1, 0)},
        "mw": {"g/mol": (1, 0), "kg/mol": (1e3, 0), "Da": (1, 0)},
        "dispersity": {"dimensionless": (1, 0)},
        "decomposition_temperature": {"K": (1, 0), "C": (1, 273.15), "°C": (1, 273.15)},
        "density": {"g/cm^3": (1, 0), "kg/m^3": (1e-3, 0)},
        "melting_temperature": {"K": (1, 0), "C": (1, 273.15), "°C": (1, 273.15)},
        "melting_enthalpy": {"J/g": (1, 0), "kJ/kg": (1, 0)},
        "crystallinity": {"fraction": (1, 0), "%": (0.01, 0)},
        **{name: {"MPa^0.5": (1, 0), "(cal/cm^3)^0.5": (2.0455, 0)} for name in ("hsp_delta_d", "hsp_delta_p", "hsp_delta_h", "hsp_radius")},
    }
    if unit not in units[prop]:
        raise ValueError(f"Unit {unit!r} is incompatible with {prop}")
    factor, offset = units[prop][unit]
    converted = value * factor + offset
    if not math.isfinite(converted) or converted < 0 or (prop in {"tg", "binding_affinity", "molecular_weight", "mn", "mw", "decomposition_temperature", "density", "hsp_radius"} and converted == 0):
        raise ValueError(f"Invalid physical value for {prop}")
    if prop == "crystallinity" and converted > 1:
        raise ValueError("Crystallinity cannot exceed 1 (100 %)")
    if prop == "melting_temperature" and converted == 0:
        raise ValueError("Invalid physical value for melting_temperature")
    if prop == "dispersity" and converted < 1:
        raise ValueError("Dispersity Mw/Mn cannot be below 1")
    if not str(row.get("method", "")).strip() or not row.get("source"):
        raise ValueError("Measurement method and source are required")
    if row.get("origin") not in {"experimental", "literature", "simulation", "demo"}:
        raise ValueError("origin must be experimental, literature, simulation, or demo")
    if not isinstance(row.get("conditions", {}), dict):
        raise ValueError("conditions must be an object with explicit test conditions")
    if not isinstance(row.get("verified", False), bool):
        raise ValueError("verified must be a boolean set after human review")
    if row.get("material"):
        identity = material_identity(row["material"])
        if row.get("material_id") and row["material_id"] != identity["material_id"]:
            raise ValueError("material_id disagrees with supplied material identity")
        row["material_id"] = identity["material_id"]
        row["material"] = identity["identity"]
    if not row.get("material_id") and not row.get("job_id"):
        raise ValueError("material, material_id, or an indexed job_id is required")
    row.update(property=prop, value=converted, unit=PROPERTY_UNITS[prop],
               original_value=row.get("original_value", value), original_unit=row.get("original_unit", unit),
               conditions=row.get("conditions", {}), verified=row.get("verified", False))
    if prop == "binding_affinity":
        row["quantity"] = "dissociation_constant_Kd"
    return row


class ResearchStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "research.duckdb"
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.database), threading.RLock())
        with self._connection() as con:
            con.execute("CREATE TABLE IF NOT EXISTS experiments (job_id VARCHAR PRIMARY KEY, material_id VARCHAR, status VARCHAR, route VARCHAR, updated_at VARCHAR, record JSON)")
            con.execute("CREATE TABLE IF NOT EXISTS measurements (id VARCHAR PRIMARY KEY, material_id VARCHAR, job_id VARCHAR, property VARCHAR, origin VARCHAR, value DOUBLE, unit VARCHAR, verified BOOLEAN, created_at VARCHAR, record JSON)")

    @contextmanager
    def _connection(self):
        # DuckDB has no automatic multi-process writer coordination. Every connection,
        # including reads and exports, lives inside the same OS + thread mutex.
        import duckdb
        with self.lock:
            with (self.root / ".research.lock").open("a+b") as handle:
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                else:
                    import msvcrt
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                con = None
                try:
                    con = duckdb.connect(str(self.database))
                    yield con
                finally:
                    if con is not None:
                        con.close()
                    if os.name == "posix":
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    else:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def index_job(self, job: dict, job_dir: Path) -> dict:
        from .descriptors import extract_job_features
        job_dir = Path(job_dir)
        if job.get("status") not in TERMINAL:
            raise ValueError("Only terminal jobs can be indexed consistently")
        job_id = str(job["id"])
        material = material_from_job(job)
        config = job.get("config") or _read(job_dir / "config.json")
        route = config.get("route", "unknown")
        record = {"id": job_id, "job_id": job_id, **{k: v for k, v in job.items() if k in ("name", "status", "created_at", "finished_at", "error")},
                  "material_id": material["material_id"], "material": material["identity"],
                  "route": route, "config": config, "result": _read(job_dir / "result.json"),
                  "origin": "simulation", "is_demo": route in {"polymer_cg", "hoomd"},
                  "features": extract_job_features(job_dir, job), "indexed_at": now(), "schema_version": SCHEMA_VERSION,
                  "provenance": {"job_directory": str(job_dir.resolve()), "manifest": _read(job_dir / "manifest.json"),
                                 "runtime": _read(job_dir / "runtime.json"),
                                 "manifest_sha256": _hash(job_dir / "manifest.json") if (job_dir / "manifest.json").is_file() else None}}
        with self._connection() as con:
            con.execute("INSERT OR REPLACE INTO experiments VALUES (?, ?, ?, ?, ?, ?)",
                        [job_id, record["material_id"], job["status"], route, record["indexed_at"], _json(record)])
        return record

    def sync_jobs(self, jobs_root: Path) -> dict:
        count, skipped, errors = 0, 0, []
        for path in sorted(Path(jobs_root).glob("*/job.json")):
            if path.is_symlink() or path.parent.is_symlink():
                skipped += 1
                continue
            job = _read(path)
            if job.get("status") not in TERMINAL:
                skipped += 1
                continue
            try:
                self.index_job(job, path.parent)
                count += 1
            except Exception as exc:
                errors.append({"job_id": job.get("id", path.parent.name), "error": str(exc)})
        return {"indexed": count, "skipped": skipped, "errors": errors}

    def list_experiments(self, filters=None) -> list:
        with self._connection() as con:
            records = [json.loads(row[0]) for row in con.execute("SELECT record FROM experiments ORDER BY updated_at DESC, job_id").fetchall()]
        return [r for r in records if all(r.get(k) == v for k, v in (filters or {}).items())]

    def _prepare_measurement(self, record: dict) -> dict:
        record = dict(record)
        if record.get("job_id"):
            rows = self.list_experiments({"job_id": record["job_id"]})
            if not rows:
                raise ValueError("job_id must reference an indexed experiment")
            experiment = rows[0]
            if record.get("material_id") and record["material_id"] != experiment["material_id"]:
                raise ValueError("Measurement and job material identities disagree")
            if record.get("material") and material_identity(record["material"])["material_id"] != experiment["material_id"]:
                raise ValueError("Measurement and job material identities disagree")
            record["material"] = experiment["material"]
            record["material_id"] = experiment["material_id"]
            if experiment["is_demo"] and record.get("origin") != "demo":
                raise ValueError("Unmapped reduced-unit demonstration jobs cannot label real-material measurements")
        elif record.get("material_id") and not record.get("material"):
            # A material reference must recover the exact registered identity;
            # otherwise it becomes an orphan that cannot supply model descriptors.
            identity = record["material_id"]
            matches = self.list_experiments({"material_id": identity}) + self.list_measurements({"material_id": identity})
            materials = [item["material"] for item in matches if item.get("material")]
            if not materials:
                raise ValueError("material_id must reference an existing material; supply its full material identity first")
            if any(material_identity(material)["material_id"] != identity for material in materials):
                raise ValueError("Registered material identity does not match material_id")
            record["material"] = materials[0]
            if record["material"].get("representation") == "unmapped_soft_bead_chain" and record.get("origin") != "demo":
                raise ValueError("Unmapped reduced-unit demonstration materials cannot label real-material measurements")
        row = validate_measurement(record)
        row.update(id=uuid.uuid4().hex, created_at=now(), schema_version=SCHEMA_VERSION)
        return row

    def add_measurement(self, record: dict) -> dict:
        return self.add_measurements([record])[0]

    def add_measurements(self, records: list[dict]) -> list[dict]:
        if not isinstance(records, list) or not records or len(records) > 10000:
            raise ValueError("Supply between 1 and 10000 measurement records")
        # All linked materials and units are validated before opening the write transaction.
        rows = [self._prepare_measurement(record) for record in records]
        with self._connection() as con:
            con.execute("BEGIN TRANSACTION")
            for row in rows:
                con.execute("INSERT INTO measurements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [row["id"], row["material_id"], row.get("job_id"), row["property"], row["origin"],
                         row["value"], row["unit"], row["verified"], row["created_at"], _json(row)])
            con.execute("COMMIT")
        return rows

    def list_measurements(self, filters=None) -> list:
        with self._connection() as con:
            rows = [json.loads(row[0]) for row in con.execute("SELECT record FROM measurements ORDER BY created_at DESC, id").fetchall()]
        return [r for r in rows if all(r.get(k) == v for k, v in (filters or {}).items())]

    def export_snapshot(self, destination: Path) -> dict:
        """Export an atomically published directory; never copy the live DuckDB file."""
        destination = Path(destination).resolve()
        if destination.exists():
            raise ValueError("Snapshot destination must be new; existing snapshots are immutable")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / (".snapshot-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            with self._connection() as con:
                con.execute("BEGIN TRANSACTION")
                counts = {}
                for table, filename in (("experiments", "experiments_master.parquet"), ("measurements", "measurements.parquet")):
                    target = str(staging / filename).replace("'", "''")
                    con.execute(f"COPY (SELECT * FROM {table}) TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)")
                    counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                con.execute("COMMIT")
            metadata = {"schema_version": SCHEMA_VERSION, "created_at": now(), "counts": counts,
                        "consistency": "Both tables from one transaction under an exclusive process lock",
                        "files": [{"name": p.name, "bytes": p.stat().st_size, "sha256": _hash(p)} for p in sorted(staging.glob("*.parquet"))],
                        "cloud_sync": "Only this immutable snapshot directory may be synced; never sync the live database"}
            (staging / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            staging.rename(destination)
            return {"path": str(destination), **metadata}
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
