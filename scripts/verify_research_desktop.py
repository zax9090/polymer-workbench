#!/usr/bin/env python3
"""Verify the installed research dependencies through the frozen local API.

All measurements here are artificial software-test fixtures in a temporary DB.
They do not validate predictive accuracy or add records to the user's database.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import time
import traceback
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from verify_desktop import BundleService, require, tail


def wait_job(service, identifier, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.json("/api/jobs/" + identifier)
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(.1)
    raise TimeoutError(identifier)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    executable = args.bundle.resolve() / "PolymerLab" if args.bundle.is_dir() else args.bundle.resolve()
    workspace = Path(tempfile.mkdtemp(prefix="polymer-research-verification-"))
    service = BundleService(executable, workspace, "research")
    record = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "checks": {},
              "bundle_executable": str(executable), "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
              "fixture_notice": "Artificial software-test measurements only; not a scientific model validation.",
              "not_tested": ["cloud upload/account authorization", "LLM/OCR extraction", "new AlphaFold inference", "solvated mixed-system MD", "prediction accuracy on real experimental data", "native window visual interaction"]}
    try:
        service.start(timeout=90)
        require(service.json("/api/health")["version"] == "0.3.0", "Wrong frozen application version")
        overview = service.json("/api/research/overview")
        require(not overview["measurements"], "Verification data isolation failed")
        require(any(b["id"] == "baybe" and b["available"] for b in overview["backends"]), "BayBE not bundled")
        for asset in ("research.js", "research.css"):
            require(service.fetch("/static/" + asset)[0] == 200, "Research UI asset missing")
        payload = {"candidates": [{"name": f"fixture {n}", "engine": "numpy", "route": "polymer_cg", "monomers": n, "chains": 1, "steps": 100} for n in range(4, 12)],
                   "objective": {"source": "feature", "name": "rg_mean", "unit": "sigma", "direction": "min"},
                   "budget": 6, "batch_size": 5, "backend": "baybe", "seed": 42}
        campaign = service.json("/api/research/campaigns", payload=payload)
        service.json(f"/api/research/campaigns/{campaign['id']}/start", payload={})
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            campaign = service.json(f"/api/research/campaigns/{campaign['id']}")
            if not campaign["automatic"]:
                break
            time.sleep(.2)
        require(campaign["attempted"] == 6 and not campaign["automatic"], "Automatic finite budget failed: " + str(campaign))
        require(all(c["objective"] is not None for c in campaign["candidates"] if c["job_id"]), "Campaign objective missing")
        require(len(campaign["iterations"]) == 2, "Expected initial and outcome-informed batches")
        record["checks"]["baybe_automatic_campaign"] = {"attempted": campaign["attempted"], "iterations": campaign["iterations"], "status": campaign["status"]}
        experiments = service.json("/api/research/experiments")
        require(len(experiments) == 6, "Finished jobs not indexed")
        require(all(e["features"]["values"]["rg_mean"]["unit"] == "sigma" for e in experiments), "Descriptor units missing")
        require(any(e["features"].get("equilibration", {}).get("rg", {}).get("available") for e in experiments), "Bundled PyMBAR did not run")
        record["checks"]["automatic_features"] = {"experiments": len(experiments), "sample": experiments[0]["features"]}

        smiles = ["[*]CC[*]", "[*]CO[*]", "[*]CN[*]", "[*]CCO[*]", "[*]CCN[*]", "[*]CCC[*]", "[*]CC(C)[*]", "[*]CC(=O)O[*]", "[*]C(CO)C[*]", "[*]CS[*]"]
        rows = [{"material": {"repeat_smiles": s}, "property": "tg", "value": 280 + i*7, "unit": "K", "method": "artificial fixture method",
                 "conditions": {"fixture": True}, "origin": "experimental", "source": "Artificial verification fixture; NOT measured research evidence", "verified": True} for i,s in enumerate(smiles)]
        result = service.json("/api/research/measurements/import", payload={"rows": rows})
        require(result["count"] == 10, "Atomic import failed")
        model = service.json("/api/research/models", payload={"property": "tg", "method": "artificial fixture method", "conditions": {"fixture": True}})
        prediction = service.json(f"/api/research/models/{model['id']}/predict", payload={"repeat_smiles": smiles[0]})
        require(model["validation"]["n_groups"] == 10 and prediction["unit"] == "K", "Model pipeline failed")
        require(prediction["is_multifidelity"] is False and not prediction["applicability"]["is_validated_domain"], "Unsupported accuracy claim")
        record["checks"]["model_fit_predict"] = {"algorithm": model["algorithm"], "groups": model["validation"]["n_groups"], "uncertainty_kind": prediction["uncertainty_kind"], "scientific_accuracy_validated": False}
        curve = service.json("/api/research/curves", payload={"kind": "tensile", "text": "strain,stress\n0,0\n0.1,2\n0.2,4\n0.3,6\n", "options": {"strain_unit": "%", "stress_unit": "MPa", "linear_region": [0,.3]}})
        require(abs(curve["measurement_draft"]["value"] - 2000) < 1e-6 and curve["review_required"], "Curve units/review failed")
        record["checks"]["tensile_curve"] = curve

        # A tiny hand-built, valid one-page text PDF exercises the bundled parser.
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        content = b"BT /F1 12 Tf 20 100 Td (Artificial fixture) Tj ET"
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
        pdf, offsets = b"%PDF-1.4\n", [0]
        for i,obj in enumerate(objects, 1):
            offsets.append(len(pdf)); pdf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
        xref = len(pdf)
        pdf += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(f"{o:010} 00000 n \n".encode() for o in offsets[1:])
        pdf += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        request = Request(service.origin + "/api/research/documents?filename=fixture.pdf", data=pdf, headers={"Content-Type": "application/pdf", "X-Polymer-Token": service.token})
        with urlopen(request, timeout=15) as response:
            document = json.load(response)
        require("Artificial fixture" in document["pages"][0]["text"] and not document["measurements"], "PDF extraction/review failed")
        record["checks"]["pdf_text"] = {"pages": document["page_count"], "review_required": document["review_required"], "source_sha256": document["source_sha256"]}

        pdb = (Path(__file__).resolve().parents[1] / "tests/fixtures/alanine6.pdb").read_text()
        created = service.json("/api/research/bridges", payload={"bridge_mode": "protein_cg", "bridge_track": "martini3", "pdb_text": pdb, "secondary_structure": "CCCCCC", "polyply_residue": "PEO", "monomers": 5, "chains": 1})
        bridge = wait_job(service, created["id"])
        require(bridge["status"] == "prepared", "Actual martinize2/polyply failed: " + json.dumps(bridge))
        require(bridge["result"]["simulation_executed"] is False, "Component topology mislabeled as MD")
        for filename in ("protein_cg.pdb", "protein.top", "polymer.itp", "01_minimize.mdp", "04_production.mdp"):
            require(service.fetch(f"/api/jobs/{created['id']}/artifacts/{filename}")[2], "Bridge artifact missing: " + filename)
        record["checks"]["actual_martini_components"] = {"status": bridge["status"], "generated_components": bridge["result"]["generated_components"], "simulation_executed": False}
        service.json("/api/research/sync", payload={})
        snapshot = service.json("/api/research/snapshots", payload={})
        raw = service.fetch(snapshot["download_url"])[2]
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for entry in snapshot["files"]:
                require(hashlib.sha256(archive.read(entry["name"])).hexdigest() == entry["sha256"], "Snapshot hash mismatch")
        require(snapshot["counts"] == {"experiments": 7, "measurements": 10}, "Snapshot counts disagree")
        record["checks"]["parquet_snapshot"] = {"counts": snapshot["counts"], "files": snapshot["files"], "backup": snapshot["backup"]}
        service.stop()
        service = BundleService(executable, workspace, "restart")
        service.start(timeout=90)
        restored = service.json("/api/research/overview")
        require(len(restored["measurements"]) == 10 and len(restored["models"]) == 1 and len(restored["campaigns"]) == 1, "Research persistence failed")
        record["checks"]["restart_persistence"] = "passed"
        record["status"] = "passed"
    except Exception as exc:
        record.update(status="failed", error=str(exc), traceback=traceback.format_exc(), workspace=str(workspace))
    finally:
        try:
            service.stop()
        except Exception as exc:
            record.update(status="failed", shutdown_error=str(exc))
        record["server_log_tail"] = tail(service.log_path)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        if record["status"] == "passed":
            shutil.rmtree(workspace)
    print(json.dumps({"status": record["status"], "checks": list(record["checks"]), "error": record.get("error"), "report": str(args.report)}, ensure_ascii=False))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
