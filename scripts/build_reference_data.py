#!/usr/bin/env python3
"""Rebuild the vendored screening reference data from REA-audited upstream bytes.

Inputs are the pinned files that scripts/audit_extensions.py fetched and inventoried
(evidence/extensions/sources/<id>/...). Each is re-hashed against the catalog before use.
Outputs: workbench/data/radonpy_pi1070.csv, hsp_solvents.csv, licenses/, reference_data.json.
--check rebuilds in memory and fails if the committed files differ.
--audit-wheel also inventories the PyPI HSPiPy wheel with REA and confirms its db.csv
is byte-identical to the pinned repository file (evidence/artifacts/hspipy.json).
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import io
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tools/rea/extension_catalog.json"
SOURCES = ROOT / "evidence/extensions/sources"
OUT = ROOT / "workbench/data"
REA = ROOT / "tools/rea/node_modules/rea-agents/scripts/rea.mjs"
WHEEL = {"package": "hspipy", "version": "1.1.8", "artifact": "hspipy-1.1.8-py3-none-any.whl",
         "sha256": "d916f2d6680a44491c55909efa0694b5d96c5a6dbe20c3fbd89524e9915f0f87", "license": "MIT"}
PI1070_PROPERTIES = ["density", "Cp", "bulk_modulus", "compressibility", "volume_expansion", "refractive_index", "thermal_conductivity"]


def pinned(repo_id, path):
    repos = {repo["id"]: repo for repo in json.loads(CATALOG.read_text())}
    repo = repos[repo_id]
    entry = next(item for item in repo["source_files"] if item["path"] == path)
    data = (SOURCES / repo_id / path).read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise SystemExit(f"{repo_id}/{path} differs from its pinned SHA-256; run scripts/audit_extensions.py --fetch-pinned")
    return data, {"repository": repo["repository"], "commit": repo["commit"], "path": path, "url": entry["source_url"],
                  "sha256": entry["sha256"], "license": repo["license"]}


def finite(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value)


def table(rows, columns):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def build_pi1070():
    data, source = pinned("radonpy-pi1070", "data/PI1070.csv")
    rows, dropped = [], []
    for row in csv.DictReader(io.StringIO(data.decode("utf-8"))):
        smiles = row["smiles"].strip()
        if smiles.count("*") != 2:
            dropped.append({"monomer_ID": row["monomer_ID"], "reason": "repeat unit does not have exactly two attachment points"})
            continue
        record = {"monomer_ID": row["monomer_ID"], "smiles": smiles, "polymer_class": row["polymer_class"], "mol_weight_monomer": row["mol_weight_monomer"],
                  "temp": row["temp"], "press": row["press"], "tacticity": row["tacticity"], "DP": row["DP"], "n_mol": row["n_mol"]}
        for name in PI1070_PROPERTIES:
            # Keep upstream strings so the subset is a byte-faithful projection, not a re-rounding.
            record[name] = row[name] if finite(row[name]) else ""
            record[name + "_std"] = row[name + "_std"] if finite(row[name + "_std"]) else ""
        rows.append(record)
    columns = list(rows[0])
    return table(rows, columns), {**source, "rows": len(rows), "dropped": dropped, "columns": columns,
                                  "citation": "Y. Hayashi, J. Shiomi, J. Morikawa, R. Yoshida, npj Comput. Mater. 8, 222 (2022)",
                                  "kind": "simulation", "conditions": "RadonPy all-atom MD, 300 K, 1 atm (upstream temp/press columns)"}


def build_solvents():
    data, source = pinned("hspipy", "hspipy/db.csv")
    from rdkit import Chem, RDLogger
    RDLogger.DisableLog("rdApp.*")
    rows, dropped = [], []
    for row in csv.DictReader(io.StringIO(data.decode("utf-8"))):
        values = [row[k] for k in ("D", "P", "H", "Mole_vol")]
        if not all(finite(v) for v in values) or float(row["Mole_vol"]) <= 0:
            dropped.append({"name": row["Name"].strip(), "reason": "missing Hansen parameter or molar volume"})
            continue
        smiles = row["SMILES"].strip()
        mol = Chem.MolFromSmiles(smiles) if smiles else None
        rows.append({"name": row["Name"].strip(), "cas": row["CAS"].strip(), "smiles": Chem.MolToSmiles(mol) if mol is not None else "",
                     "delta_d": row["D"].strip(), "delta_p": row["P"].strip(), "delta_h": row["H"].strip(), "molar_volume": row["Mole_vol"].strip(),
                     "boiling_point_c": row["bp"].strip(), "upstream_row": row["No."].strip()})
    columns = list(rows[0])
    return table(rows, columns), {**source, "rows": len(rows), "dropped": dropped, "columns": columns,
                                  "units": {"delta_d": "MPa^0.5", "delta_p": "MPa^0.5", "delta_h": "MPa^0.5", "molar_volume": "cm^3/mol", "boiling_point_c": "C"},
                                  "kind": "literature compilation",
                                  "note": "Values match Hansen's published solvent tables (spot-checked); SMILES are RDKit-canonicalized, unparsable SMILES left empty."}


def outputs():
    pi1070, pi_meta = build_pi1070()
    solvents, solvent_meta = build_solvents()
    files = {"radonpy_pi1070.csv": pi1070, "hsp_solvents.csv": solvents,
             "licenses/RadonPy-BSD-3-Clause.txt": pinned("radonpy-pi1070", "LICENSE")[0],
             "licenses/HSPiPy-MIT.txt": pinned("hspipy", "LICENSE")[0]}
    manifest = {"schema_version": 1, "builder": "scripts/build_reference_data.py",
                "datasets": {"radonpy_pi1070": {**pi_meta, "file": "radonpy_pi1070.csv", "file_sha256": hashlib.sha256(pi1070).hexdigest(),
                                                "license_file": "licenses/RadonPy-BSD-3-Clause.txt"},
                             "hsp_solvents": {**solvent_meta, "file": "hsp_solvents.csv", "file_sha256": hashlib.sha256(solvents).hexdigest(),
                                              "license_file": "licenses/HSPiPy-MIT.txt"}}}
    files["reference_data.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
    return files


def audit_wheel():
    evidence = ROOT / "evidence/artifacts"
    downloads = evidence / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    wheel = downloads / WHEEL["artifact"]
    if not wheel.is_file() or hashlib.sha256(wheel.read_bytes()).hexdigest() != WHEEL["sha256"]:
        subprocess.run([sys.executable, "-m", "pip", "download", "--quiet", "--no-deps", "--disable-pip-version-check",
                        f"{WHEEL['package']}=={WHEEL['version']}", "-d", str(downloads)], check=True, timeout=300)
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != WHEEL["sha256"]:
        raise SystemExit("HSPiPy wheel does not match the pinned SHA-256")
    argv = [shutil.which("node") or "node", str(REA), "inspect-artifact", str(wheel), "--format", "json"]
    raw = evidence / "hspipy.inspect.json"
    with raw.open("w") as stdout:
        code = subprocess.run(argv, cwd=ROOT, stdout=stdout, stderr=subprocess.PIPE, text=True, timeout=600).returncode
    inventory = json.loads(raw.read_text())["normalized_result"]["substeps"][0]["evidence"]["normalized_result"]
    by_id = {node["artifact_id"]: node["sha256"] for node in inventory["nodes"]}
    listed = {occ["logical_path"]: by_id.get(occ["artifact_id"]) for occ in inventory["occurrences"]}
    pinned_bytes, source = pinned("hspipy", "hspipy/db.csv")
    match = listed.get("hspipy/db.csv") == source["sha256"]
    record = {**WHEEL, "purpose": "Solvent Hansen table vendored (subset) as workbench/data/hsp_solvents.csv",
              "rea": {"argv": [Path(argv[0]).name, *argv[1:3], WHEEL["artifact"], *argv[4:]], "returncode": code,
                      "entries": len(inventory["occurrences"]), "coverage": inventory.get("coverage"),
                      "integrity_contradictions": inventory.get("integrity_contradictions"), "output": raw.name},
              "files": [{"path": "hspipy/db.csv", "artifact_sha256": listed.get("hspipy/db.csv"), "repository_sha256": source["sha256"],
                         "matches_repository": match}],
              "audited_at": datetime.now(timezone.utc).isoformat(),
              "status": "verified_vendored_source" if code == 0 and match else "mismatch",
              "limitations": ["REA inventories and hashes the release archive; it does not validate the Hansen values."]}
    (evidence / "hspipy.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(f"hspipy {WHEEL['version']}: {record['status']}")
    return record["status"] == "verified_vendored_source"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--audit-wheel", action="store_true")
    args = parser.parse_args()
    if args.audit_wheel and not audit_wheel():
        return 1
    built = outputs()
    if args.check:
        stale = [name for name, data in built.items() if not (OUT / name).is_file() or (OUT / name).read_bytes() != data]
        print("reference data up to date" if not stale else f"stale: {', '.join(stale)}")
        return 1 if stale else 0
    for name, data in built.items():
        (OUT / name).parent.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_bytes(data)
    print(f"wrote {', '.join(built)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
