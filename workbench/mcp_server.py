"""Model Context Protocol (stdio) bridge so AI agents can drive Polymer Lab.

The MCP process is a thin client of the loopback HTTP API: calculations, review
gates and provenance stay in the existing server. It attaches to a running desktop
app or ./start.sh server, or starts its own server when asked. Agents can queue
calculations and record measurement drafts, but never mark data as reviewed.
stdout carries only JSON-RPC messages; diagnostics go to stderr.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from . import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
TEXT_SUFFIXES = {".json", ".jsonl", ".log", ".txt", ".pdb", ".gro", ".top", ".itp", ".mdp", ".in", ".inp", ".lmp",
                 ".data", ".xyz", ".mol", ".sdf", ".fasta", ".a3m", ".csv", ".xvg", ".leap", ".py", ".md"}
ROUTES = ["polymer_cg", "polymer_atomistic", "protein", "polyply", "hoomd", "pysimm", "packmol", "mixture"]
STATUSES = ["queued", "running", "cancelling", "completed", "prepared", "blocked", "failed", "cancelled", "interrupted"]
INSTRUCTIONS = """Polymer Lab is a local polymer/protein research workbench. Every calculation is a job with saved inputs, logs, versions and hashes.
- Status meanings: completed = the route finished; prepared = structures/topologies/inputs only; blocked = an engine or input is missing. None certifies scientific validity.
- Short simulations never establish equilibrium, Tg, modulus or binding free energy. Report results with their units, method and stated limitations.
- Generic CG bead jobs (polymer_cg, hoomd) are reduced-unit models with no chemical mapping; do not present them as material predictions.
- Measurements you add are always stored unreviewed (verified=false). Only a person can mark data reviewed in the Polymer Lab window, and only reviewed experimental/literature data trains models.
- Martini 3 protein-polymer MD (route mixture, bridge_mode simulate) needs a protein PDB, an explicit per-residue secondary structure from a reviewed DSSP assignment, a polyply block and polymer_net_charge equal to that block's chain charge.
- polymer_atomistic with atomistic_mode bulk runs RadonPy's packing + Larsen 21-step + NPT sampling on a periodic cell (>=200 atoms). protocol_scale 1 uses RadonPy's full stage lengths (hours to days); smaller values are exploratory and say so in the result, together with RadonPy's convergence verdict.
- Protein import/AlphaFold DB/ColabFold results include a SAP (spatial aggregation propensity) screen of one static structure. packmol_target "sap_patch" places polymer chains near the highest-SAP patch; that is a placement choice, not evidence of binding.
- Campaign 'start' runs up to its budget of calculations automatically; confirm budgets with the user first.
- If lab_status reports no server, ask the user to open Polymer Lab or run ./start.sh, or use start_lab_server (that server stops, cancelling running jobs, when this MCP session ends)."""


class ToolError(Exception):
    """A problem the agent can act on; returned as an isError tool result."""


def _log(message):
    print(f"[polymer-lab-mcp] {message}", file=sys.stderr, flush=True)


def _loopback(origin):
    parts = urlsplit(origin)
    if parts.scheme != "http" or parts.username or parts.password or parts.path not in ("", "/"):
        return False
    try:
        return parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        return False


def _data_roots():
    from .paths import default_jobs_root, resource_root, user_data_root
    roots = []
    for root in (os.environ.get("POLYMER_DATA_DIR"), default_jobs_root(), user_data_root() / "jobs", resource_root() / "data/jobs"):
        if root and Path(root).resolve() not in roots:
            roots.append(Path(root).resolve())
    return roots


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


class LabClient:
    def __init__(self):
        self.origin = self.token = self.source = None
        self.owned = None
        self.client = "mcp"
        self.lock = threading.Lock()

    def candidates(self):
        found = []
        if os.environ.get("POLYMER_LAB_URL"):
            found.append((os.environ["POLYMER_LAB_URL"].rstrip("/"), os.environ.get("POLYMER_LAB_TOKEN"), "POLYMER_LAB_URL"))
        for root in _data_roots():
            record = root / ".server.json"
            try:
                data = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and _alive(data.get("pid")) and isinstance(data.get("origin"), str):
                found.append((data["origin"], data.get("token"), f"desktop session {record}"))
        found.append((f"http://127.0.0.1:{os.environ.get('POLYMER_PORT', '8765')}", os.environ.get("POLYMER_LAB_TOKEN"), "default port (./start.sh)"))
        return found

    def _raw(self, origin, token, method, path, payload=None, timeout=30):
        headers = {"Accept": "application/json", "X-Polymer-Client": self.client}
        if token:
            headers["X-Polymer-Token"] = token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        with urlopen(Request(origin + path, data=data, method=method, headers=headers), timeout=timeout) as response:
            body = response.read()
            return json.loads(body) if body else None

    def connect(self):
        with self.lock:
            if self.origin:
                try:
                    self._raw(self.origin, self.token, "GET", "/api/health", timeout=3)
                    return
                except (OSError, ValueError):
                    self.origin = self.token = self.source = None
            tried = []
            for origin, token, source in self.candidates():
                if not _loopback(origin):
                    tried.append(f"{source}: refused non-loopback origin {origin}")
                    continue
                try:
                    health = self._raw(origin, token, "GET", "/api/health", timeout=3)
                except HTTPError as exc:
                    tried.append(f"{source}: HTTP {exc.code}")
                    continue
                except (OSError, ValueError) as exc:
                    tried.append(f"{source}: {getattr(exc, 'reason', exc)}")
                    continue
                if isinstance(health, dict) and health.get("name") == "Polymer Lab":
                    self.origin, self.token, self.source = origin, token, source
                    return
                tried.append(f"{source}: not a Polymer Lab server")
            raise ToolError("No running Polymer Lab server was found. Open the Polymer Lab desktop app or run ./start.sh, "
                            "or call start_lab_server. Tried: " + "; ".join(tried))

    def request(self, method, path, payload=None, timeout=60):
        self.connect()
        try:
            return self._raw(self.origin, self.token, method, path, payload, timeout)
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}").get("detail")
            except ValueError:
                detail = None
            if isinstance(detail, list):
                detail = "; ".join(f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}" for item in detail)
            raise ToolError(f"Polymer Lab returned HTTP {exc.code}: {detail or exc.reason}") from exc
        except URLError as exc:
            self.origin = None
            raise ToolError(f"Lost connection to Polymer Lab: {exc.reason}") from exc

    def start_owned(self, data_dir=None):
        from .desktop import BackendSupervisor
        from .paths import default_jobs_root, frozen, prepare_desktop_data
        try:
            self.connect()
            return {"started": False, "origin": self.origin, "source": self.source, "note": "Already connected to a running server"}
        except ToolError:
            pass
        root = Path(data_dir).expanduser().resolve() if data_dir else (prepare_desktop_data() if frozen() else default_jobs_root())
        supervisor = BackendSupervisor(root).start()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                origin = supervisor.poll_ready()
            except RuntimeError as exc:
                supervisor.cleanup()
                raise ToolError(f"{exc}. Another Polymer Lab server may already own {root}.") from exc
            if origin:
                with self.lock:
                    self.owned, self.origin, self.token, self.source = supervisor, origin, supervisor.token, "started by this MCP session"
                return {"started": True, "origin": origin, "data_dir": str(root),
                        "note": "This server stops, cancelling running jobs, when the MCP session ends."}
            time.sleep(0.2)
        supervisor.stop()
        raise ToolError("Polymer Lab server did not become ready within 90 s")

    def close(self):
        if self.owned:
            _log("stopping the server started by this MCP session")
            self.owned.stop()
            self.owned = None


# ---------------------------------------------------------------- compact views

def _compact_job(job, log_tail=0):
    result = job.get("result") or {}
    if isinstance(result.get("metrics"), dict):
        metrics = dict(result["metrics"])
        for key in ("series", "residues"):
            if isinstance(metrics.get(key), list):
                metrics[key] = f"{len(metrics[key])} entries; read metrics.json or result.json with read_artifact"
        result = {**result, "metrics": metrics}
    if isinstance(result.get("bridge"), dict):
        bridge = result["bridge"]
        result = {**result, "bridge": {key: bridge.get(key) for key in ("track", "mode", "status", "simulation_executed", "stages", "generated_components", "simulation")}}
    result.pop("artifacts", None)
    config = {key: value for key, value in (job.get("config") or {}).items()
              if key not in {"pdb_text", "a3m_text", "forcefield_itp_text", "mdp_text", "engine_input"}}
    for key in ("pdb_text", "a3m_text", "forcefield_itp_text", "mdp_text"):
        if (job.get("config") or {}).get(key):
            config[key] = f"<{len(job['config'][key])} characters>"
    view = {key: job.get(key) for key in ("id", "name", "status", "created_at", "started_at", "finished_at", "error", "submitted_via")}
    view.update(config=config, steps=job.get("steps"), events=(job.get("events") or [])[-5:], result=result or None,
                artifacts=[{"name": a["name"], "size": a["size"]} for a in (job.get("artifacts") or [])][:200])
    if log_tail:
        view["log_tail"] = (job.get("log") or "")[-log_tail:]
    return view


def _material(record):
    material = record.get("material") or {}
    return {key: material[key] for key in ("repeat_smiles", "smiles", "monomers", "chains", "accession", "polyply_residue", "representation", "protein_id") if key in material}


def _compact_experiment(row):
    features = row.get("features") or {}
    return {"job_id": row.get("job_id"), "name": row.get("name"), "route": row.get("route"), "status": row.get("status"),
            "is_demo": row.get("is_demo"), "material_id": row.get("material_id"), "material": _material(row),
            "actual_dynamics": features.get("actual_dynamics"), "frame_count": features.get("frame_count"),
            "features": {key: {"value": item.get("value"), "unit": item.get("unit")} for key, item in (features.get("values") or {}).items()},
            "unavailable": sorted((features.get("unavailable") or {}).keys())}


def _compact_measurement(row):
    return {key: row.get(key) for key in ("id", "material_id", "job_id", "property", "value", "unit", "original_value", "original_unit",
                                          "method", "conditions", "origin", "source", "verified", "created_at", "entry")} | {"material": _material(row)}


def _compact_campaign(record):
    candidates = record.get("candidates") or []
    return {key: record.get(key) for key in ("id", "name", "status", "automatic", "backend", "objective", "budget", "batch_size", "attempted", "pause_reason", "error", "created_at")} | {
        "candidates": [{"id": c.get("id"), "name": (c.get("config") or {}).get("name"), "status": c.get("status"), "job_id": c.get("job_id"),
                        "objective": c.get("objective"), "objective_status": c.get("objective_status")} for c in candidates],
        "iterations": len(record.get("iterations") or [])}


# ---------------------------------------------------------------- tools

def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


JOB_ID = {"type": "string", "pattern": "^[a-f0-9]{32}$", "description": "32-character Polymer Lab job id"}


def _job_config_schema():
    try:
        from .models import JobConfig
        schema = JobConfig.model_json_schema()
        schema.pop("title", None)
        return schema
    except ImportError:
        return {"type": "object", "description": "Polymer Lab JobConfig; the server validates every field."}


class PolymerLabTools:
    def __init__(self, client: LabClient):
        self.client = client
        self.tools = {}
        self._define()

    def tool(self, name, title, description, schema, handler, read_only=False, destructive=False, idempotent=False):
        self.tools[name] = {"definition": {"name": name, "title": title, "description": description, "inputSchema": schema,
                                           "annotations": {"title": title, "readOnlyHint": read_only, "destructiveHint": destructive,
                                                           "idempotentHint": idempotent, "openWorldHint": False}},
                            "handler": handler, "schema": schema}

    def _define(self):
        t = self.tool
        t("lab_status", "Polymer Lab status",
          "Connection, version, installed engines and available routes, Martini/GROMACS bridge readiness and research data counts. Call this first.",
          _schema({}), self.lab_status, read_only=True, idempotent=True)
        t("start_lab_server", "Start a Polymer Lab server",
          "Start a local Polymer Lab server owned by this MCP session when none is running. It stops, cancelling running jobs, when the session ends; prefer the desktop app for long calculations.",
          _schema({"data_dir": {"type": "string", "description": "Jobs folder; defaults to the standard Polymer Lab data folder"}}), self.start_lab_server)
        t("list_jobs", "List jobs", "Recent calculation jobs, newest first.",
          _schema({"status": {"type": "string", "enum": STATUSES}, "route": {"type": "string", "enum": ROUTES},
                   "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}}), self.list_jobs, read_only=True, idempotent=True)
        t("get_job", "Get job", "One job's status, workflow steps, result summary, warnings and artifact names. Large series are summarized; use read_artifact for files.",
          _schema({"job_id": JOB_ID, "log_tail_chars": {"type": "integer", "minimum": 0, "maximum": 20000, "default": 0}}, ["job_id"]),
          self.get_job, read_only=True, idempotent=True)
        t("wait_for_job", "Wait for job", "Wait until a job leaves queued/running (or the timeout passes) and return its summary.",
          _schema({"job_id": JOB_ID, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600, "default": 60}}, ["job_id"]),
          self.wait_for_job, read_only=True, idempotent=True)
        t("read_artifact", "Read job artifact", "Read a text artifact of a job (JSON, logs, PDB/GRO, topologies, CSV, XVG). Binary trajectories are refused.",
          _schema({"job_id": JOB_ID, "name": {"type": "string", "description": "Artifact path from get_job"},
                   "offset": {"type": "integer", "minimum": 0, "default": 0},
                   "max_chars": {"type": "integer", "minimum": 100, "maximum": 200000, "default": 20000}}, ["job_id", "name"]),
          self.read_artifact, read_only=True, idempotent=True)
        t("submit_job", "Submit calculation job",
          "Queue one calculation with a validated JobConfig. Routes: polymer_cg (reduced-unit CG), polymer_atomistic (RDKit build or RadonPy MD), protein (AlphaFold DB, PDB import, AF3 export, ColabFold), "
          "packmol (packmol_target sap_patch places chains near the protein's SAP patch), polyply, pysimm, hoomd, mixture (protein-polymer bridges; bridge_mode simulate runs Martini 3 GROMACS MD). "
          "polymer_atomistic atomistic_mode bulk builds a periodic cell and runs RadonPy's 21-step equilibration (protocol_scale scales stage lengths). source_job_id hands a finished PDB to packmol/mixture.",
          _schema({"config": _job_config_schema()}, ["config"]), self.submit_job)
        t("cancel_job", "Cancel job", "Stop a queued or running job; partial files are kept.", _schema({"job_id": JOB_ID}, ["job_id"]), self.cancel_job, destructive=True)
        t("rerun_job", "Rerun job", "Queue a new job with the same configuration (calculation restarts from the beginning).", _schema({"job_id": JOB_ID}, ["job_id"]), self.rerun_job)
        t("diagnose_job", "Diagnose job", "Offline log rules for a failed or blocked job, with evidence lines and any suggested configuration patch.",
          _schema({"job_id": JOB_ID}, ["job_id"]), self.diagnose_job, read_only=True, idempotent=True)
        t("retry_job_with_fix", "Retry with suggested fix", "Queue a new job applying a diagnose_job patch (rule_id). The patch does not guarantee physical validity.",
          _schema({"job_id": JOB_ID, "rule_id": {"type": "string"}}, ["job_id", "rule_id"]), self.retry_job_with_fix)
        t("research_overview", "Research data overview", "Counts of indexed experiments and measurements, supported properties and units, trained models, campaigns and recommender backends.",
          _schema({}), self.research_overview, read_only=True, idempotent=True)
        t("list_experiments", "List indexed experiments", "Indexed calculation records with material identity and computed features (value + unit). Jobs are indexed a moment after they finish.",
          _schema({"route": {"type": "string", "enum": ROUTES}, "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}}),
          self.list_experiments, read_only=True, idempotent=True)
        t("list_measurements", "List measurements", "Experimental, literature, simulation or demo measurements in normalized units with review status.",
          _schema({"property": {"type": "string"}, "verified": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}}),
          self.list_measurements, read_only=True, idempotent=True)
        t("analyze_instrument_curve", "Analyze instrument curve",
          "Draft measurements from an instrument CSV without saving: dsc (Tg midpoint), tensile (modulus), tga (mass-loss temperature), gpc (Mn/Mw/dispersity by conventional calibration). Options follow the Polymer Lab curve form.",
          _schema({"kind": {"type": "string", "enum": ["dsc", "tensile", "tga", "gpc"]}, "csv_text": {"type": "string", "maxLength": 2000000},
                   "options": {"type": "object"}}, ["kind", "csv_text"]), self.analyze_instrument_curve, read_only=True, idempotent=True)
        t("add_measurement", "Add measurement (unreviewed)",
          "Store one measurement with property, value, unit, method, conditions, source, origin and a material or indexed job_id. It is always saved unreviewed; a person must review it in Polymer Lab before models use it.",
          _schema({"measurement": {"type": "object"}}, ["measurement"]), self.add_measurement)
        t("train_property_model", "Train property model",
          "Fit a Gaussian-process baseline from reviewed experimental/literature measurements of one property under identical method and conditions (at least 8 chemical groups).",
          _schema({"request": {"type": "object", "description": "{property, unit?, domain?, method?, conditions?, feature_keys?}"}}, ["request"]), self.train_property_model)
        t("predict_property", "Predict with model", "Predict a property with a trained model; returns an uncalibrated standard deviation and applicability notes.",
          _schema({"model_id": {"type": "string"}, "request": {"type": "object", "description": "{repeat_smiles, features?, protein_context?}"}}, ["model_id", "request"]),
          self.predict_property, read_only=True, idempotent=True)
        t("create_campaign", "Create campaign",
          "Save a finite exploration campaign over explicit JobConfig candidates (<=200, budget <=50) with one feature or reviewed-measurement objective. Creating does not run anything.",
          _schema({"campaign": {"type": "object", "description": "{name, candidates:[JobConfig], objective:{source, name, unit, direction, method?, conditions?}, budget, batch_size, backend}"}}, ["campaign"]),
          self.create_campaign)
        t("control_campaign", "Control campaign",
          "get: refresh status; iterate: submit one batch; start: run batches automatically up to the budget; stop: stop scheduling (optionally cancel active jobs).",
          _schema({"campaign_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}, "action": {"type": "string", "enum": ["get", "iterate", "start", "stop"]},
                   "batch_size": {"type": "integer", "minimum": 1, "maximum": 10}, "cancel_active": {"type": "boolean", "default": False}}, ["campaign_id", "action"]),
          self.control_campaign, destructive=True)
        t("rea_evidence", "REA source evidence", "Audited upstream repositories behind each engine adapter: pinned commit, license and REA inventory status.",
          _schema({}), self.rea_evidence, read_only=True, idempotent=True)
        t("export_research_snapshot", "Export research snapshot", "Write an immutable Parquet snapshot of experiments and measurements (and copy it to the configured sync folder, if any).",
          _schema({}), self.export_research_snapshot)

    # handlers -----------------------------------------------------------
    def lab_status(self, args):
        try:
            self.client.connect()
        except ToolError as exc:
            return {"connected": False, "detail": str(exc)}
        health = self.client.request("GET", "/api/health")
        capabilities = self.client.request("GET", "/api/capabilities")
        bridges = self.client.request("GET", "/api/research/bridges")
        overview = self.client.request("GET", "/api/research/overview")
        return {"connected": True, "origin": self.client.origin, "source": self.client.source, "owned_by_this_session": bool(self.client.owned),
                "version": health.get("version"),
                "engines": [{key: e.get(key) for key in ("id", "available", "version")} for e in capabilities.get("engines", [])],
                "routes": capabilities.get("routes"),
                "bridge": {"executables": {k: bool(v) for k, v in (bridges.get("executables") or {}).items()},
                           "martini3_parameters": bridges.get("martini3_parameters"),
                           "martini3_polyply_blocks": (bridges.get("polyply_libraries") or {}).get("martini3")},
                "research": {"experiments": len(overview.get("experiments", [])), "measurements": len(overview.get("measurements", [])),
                             "models": len(overview.get("models", [])), "campaigns": len(overview.get("campaigns", []))}}

    def start_lab_server(self, args):
        return self.client.start_owned(args.get("data_dir"))

    def list_jobs(self, args):
        jobs = self.client.request("GET", "/api/jobs")
        rows = [j for j in jobs if (not args.get("status") or j.get("status") == args["status"]) and (not args.get("route") or (j.get("config") or {}).get("route") == args["route"])]
        return {"total": len(rows), "jobs": [{"id": j["id"], "name": j.get("name"), "route": (j.get("config") or {}).get("route"),
                                              "mode": {k: (j.get("config") or {}).get(k) for k in ("protein_mode", "atomistic_mode", "bridge_mode", "engine") if (j.get("config") or {}).get(k)},
                                              "status": j.get("status"), "created_at": j.get("created_at"), "finished_at": j.get("finished_at"),
                                              "error": j.get("error"), "submitted_via": j.get("submitted_via")} for j in rows[:args.get("limit", 20)]]}

    def get_job(self, args):
        return _compact_job(self.client.request("GET", f"/api/jobs/{args['job_id']}"), args.get("log_tail_chars", 0))

    def wait_for_job(self, args):
        deadline = time.monotonic() + args.get("timeout_seconds", 60)
        while True:
            job = self.client.request("GET", f"/api/jobs/{args['job_id']}")
            finished = job.get("status") not in {"queued", "running", "cancelling"}
            if finished or time.monotonic() >= deadline:
                view = _compact_job(job)
                view["finished"] = finished
                if finished:
                    # Research indexing runs just after the terminal status is written.
                    settle = time.monotonic() + 10
                    while True:
                        view["indexed"] = any(r.get("job_id") == job["id"] for r in self.client.request("GET", "/api/research/experiments"))
                        if view["indexed"] or time.monotonic() >= settle:
                            break
                        time.sleep(0.5)
                return view
            time.sleep(2)

    def read_artifact(self, args):
        name = args["name"]
        if Path(name).suffix.lower() not in TEXT_SUFFIXES:
            raise ToolError(f"{name} is not a supported text artifact; binary trajectories and archives must be opened in Polymer Lab or downloaded")
        self.client.connect()
        path = f"/api/jobs/{args['job_id']}/artifacts/{quote(name)}"
        headers = {"X-Polymer-Client": self.client.client, **({"X-Polymer-Token": self.client.token} if self.client.token else {})}
        try:
            with urlopen(Request(self.client.origin + path, headers=headers), timeout=60) as response:
                text = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raise ToolError(f"Artifact not available (HTTP {exc.code})") from exc
        offset, limit = args.get("offset", 0), args.get("max_chars", 20000)
        return {"name": name, "total_chars": len(text), "offset": offset, "truncated": offset + limit < len(text), "text": text[offset:offset + limit]}

    def submit_job(self, args):
        return _compact_job(self.client.request("POST", "/api/jobs", args["config"]))

    def cancel_job(self, args):
        return _compact_job(self.client.request("POST", f"/api/jobs/{args['job_id']}/cancel", {}))

    def rerun_job(self, args):
        return _compact_job(self.client.request("POST", f"/api/jobs/{args['job_id']}/rerun", {}))

    def diagnose_job(self, args):
        return self.client.request("GET", f"/api/research/diagnostics/{args['job_id']}")

    def retry_job_with_fix(self, args):
        return _compact_job(self.client.request("POST", f"/api/research/diagnostics/{args['job_id']}/retry", {"rule_id": args["rule_id"]}))

    def research_overview(self, args):
        data = self.client.request("GET", "/api/research/overview")
        return {"experiments": len(data.get("experiments", [])), "measurements": len(data.get("measurements", [])),
                "reviewed_measurements": sum(1 for m in data.get("measurements", []) if m.get("verified")),
                "properties": data.get("properties"), "backends": data.get("backends"),
                "models": [{key: m.get(key) for key in ("id", "property", "unit", "domain", "algorithm", "created_at", "feature_keys", "validation", "measurement_context")} for m in data.get("models", [])],
                "campaigns": [{key: c.get(key) for key in ("id", "name", "status", "automatic", "objective", "budget", "attempted")} for c in data.get("campaigns", [])],
                "initial_sync": data.get("initial_sync")}

    def list_experiments(self, args):
        rows = [r for r in self.client.request("GET", "/api/research/experiments") if not args.get("route") or r.get("route") == args["route"]]
        return {"total": len(rows), "experiments": [_compact_experiment(r) for r in rows[:args.get("limit", 50)]]}

    def list_measurements(self, args):
        rows = [r for r in self.client.request("GET", "/api/research/measurements")
                if (not args.get("property") or r.get("property") == args["property"]) and ("verified" not in args or r.get("verified") is args["verified"])]
        return {"total": len(rows), "measurements": [_compact_measurement(r) for r in rows[:args.get("limit", 100)]]}

    def analyze_instrument_curve(self, args):
        draft = self.client.request("POST", "/api/research/curves", {"kind": args["kind"], "text": args["csv_text"], "options": args.get("options", {})})
        draft["preview"] = f"{len((draft.get('preview') or {}).get('x', []))} downsampled points omitted"
        return draft

    def add_measurement(self, args):
        record = dict(args["measurement"])
        # Review is a human gate: agent-entered data never enters training as reviewed.
        record["verified"] = False
        record["entry"] = {"via": "mcp", "client": self.client.client}
        return _compact_measurement(self.client.request("POST", "/api/research/measurements", record))

    def train_property_model(self, args):
        model = self.client.request("POST", "/api/research/models", args["request"], timeout=300)
        return {key: value for key, value in model.items() if key != "training"}

    def predict_property(self, args):
        return self.client.request("POST", f"/api/research/models/{quote(args['model_id'])}/predict", args["request"], timeout=300)

    def create_campaign(self, args):
        return _compact_campaign(self.client.request("POST", "/api/research/campaigns", args["campaign"]))

    def control_campaign(self, args):
        identifier, action = args["campaign_id"], args["action"]
        if action == "get":
            return _compact_campaign(self.client.request("GET", f"/api/research/campaigns/{identifier}"))
        body = {"cancel_active": args.get("cancel_active", False)} if action == "stop" else {"batch_size": args.get("batch_size")}
        return _compact_campaign(self.client.request("POST", f"/api/research/campaigns/{identifier}/{action}", body, timeout=300))

    def rea_evidence(self, args):
        data = self.client.request("GET", "/api/evidence")
        def summary(repo):
            return {key: repo.get(key) for key in ("id", "name", "repository", "commit", "license", "rea_status", "snapshot_scope")}
        return {"rea": {key: (data.get("rea") or {}).get(key) for key in ("version", "status", "limitations")},
                "repositories": [summary(r) for r in data.get("repositories", [])],
                "extensions": [summary(r) for r in (data.get("extensions") or {}).get("repositories", [])]}

    def export_research_snapshot(self, args):
        return self.client.request("POST", "/api/research/snapshots", {}, timeout=300)


# ---------------------------------------------------------------- JSON-RPC

def _check(schema, args):
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in args:
            raise ToolError(f"missing required argument: {name}")
    for name, value in args.items():
        spec = properties.get(name)
        if spec is None:
            raise ToolError(f"unknown argument: {name}")
        kind = spec.get("type")
        valid = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
                 "boolean": isinstance(value, bool), "object": isinstance(value, dict), "array": isinstance(value, list)}.get(kind, True)
        if not valid:
            raise ToolError(f"{name} must be of type {kind}")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolError(f"{name} must be one of {spec['enum']}")
        if kind == "integer" and (value < spec.get("minimum", value) or value > spec.get("maximum", value)):
            raise ToolError(f"{name} must be between {spec.get('minimum')} and {spec.get('maximum')}")
        if kind == "string" and "pattern" in spec:
            import re
            if not re.fullmatch(spec["pattern"], value):
                raise ToolError(f"{name} has an invalid format")


class Server:
    def __init__(self, tools: PolymerLabTools, stdout=None):
        self.tools = tools
        self.stdout = stdout or sys.stdout
        self.write_lock = threading.Lock()
        self.protocol = PROTOCOL_VERSIONS[0]
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp-tool")

    def send(self, message):
        with self.write_lock:
            self.stdout.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
            self.stdout.flush()

    def call_tool(self, params):
        name, args = params.get("name"), params.get("arguments") or {}
        entry = self.tools.tools.get(name)
        if entry is None:
            raise LookupError(name)
        try:
            _check(entry["schema"], args)
            data = entry["handler"](args)
            text = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False, default=str)
            result = {"content": [{"type": "text", "text": text}], "isError": False}
            if self.protocol >= "2025-06-18" and isinstance(data, dict):
                result["structuredContent"] = data
            return result
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:
            _log(traceback.format_exc())
            return {"content": [{"type": "text", "text": f"Unexpected error: {type(exc).__name__}: {exc}"}], "isError": True}

    def handle(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None, "error": {"code": -32600, "message": "Invalid Request"}}
        method, identifier, params = message["method"], message.get("id"), message.get("params") or {}
        if "id" not in message:
            return None  # notifications (initialized, cancelled) need no reply
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.protocol = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            client = (params.get("clientInfo") or {}).get("name") or "client"
            self.tools.client.client = "mcp:" + "".join(c for c in str(client) if c.isalnum() or c in "._-")[:60]
            result = {"protocolVersion": self.protocol, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "polymer-lab", "title": "Polymer Lab", "version": __version__}, "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [entry["definition"] for entry in self.tools.tools.values()]}
        elif method == "tools/call":
            try:
                result = self.call_tool(params)
            except LookupError:
                return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}}
        else:
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def dispatch(self, message):
        reply = self.handle(message)
        if reply is not None:
            self.send(reply)

    def serve(self, stdin=None):
        pending = []
        for line in (stdin or sys.stdin):
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            for item in (message if isinstance(message, list) else [message]):
                # Long tools (wait_for_job, training) run off the reader so ping still answers.
                if isinstance(item, dict) and item.get("method") == "tools/call":
                    pending.append(self.pool.submit(self.dispatch, item))
                else:
                    self.dispatch(item)
        for future in pending:
            future.result()
        self.pool.shutdown(wait=True)


def main(arguments=None) -> int:
    if arguments:
        print("usage: PolymerLab --mcp   (stdio MCP server; configure via POLYMER_LAB_URL / POLYMER_LAB_TOKEN / POLYMER_DATA_DIR)", file=sys.stderr)
        return 2
    # JSON-RPC text is UTF-8 regardless of the host locale.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    client = LabClient()
    try:
        Server(PolymerLabTools(client)).serve()
    finally:
        client.close()
    return 0
