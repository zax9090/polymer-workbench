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
import math
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
- analyze_job_structure saves a separate immutable analysis record from a completed atomistic bulk/crosslink job without changing its artifacts. RDF and free volume support final structures; MSD needs an unwrapped trajectory; Widom only supports GAFF2 12-6 LJ cells. Report grid comparisons as sensitivity, not error bounds, and keep non-diffusive MSD and single-site gas limitations with every result.
- create_mixture_design, analyze_mixture_response and optimize_desirability cover mixture experiments (proportions summing to one, Scheffé models in L-pseudocomponents) and multi-response Derringer-Suich desirability. Desirability bounds and importances come from the user; never invent them. Present D with every individual d and the predicted values with intervals, and treat the optimum as a candidate for confirmation runs.
- create_research_report saves a report folder (summary, figures, tables, xlsx, hashes) from a DoE record, a workflow or the lab master table. It copies saved numbers and verified flags and never reviews anything; recommendations stay unconfirmed candidates and simulation values stay distinct from measurements. Upload to Drive only when the user asks.
- create_experiment_design saves a randomized factorial or face-centered central composite design with explicit factor bounds. analyze_response_surface saves a fitted linear, interaction or quadratic model and a bounded candidate setting from the supplied experimental CSV. These are local immutable records, not reviewed measurements. An optimum is a model recommendation, not experimentally validated; run confirmation experiments and retain model diagnostics and warnings.
- analyze_process_data reads one fab-style CSV and saves nothing: dictionary (keys, duplicates, reused child IDs, balance), control_chart (I-MR or Xbar-S, Western Electric rules), capability (Cp/Cpk within, Pp/Ppk overall), variance_components (balanced nested ANOVA, e.g. run > zone with wafers as residual) and stratified_rates (crude vs stratum rates, Mantel-Haenszel). Run-rule signals and pooled comparisons point to records worth checking; they never establish a cause. Report units, sigma method and warnings. For classifiers report recall and false alarms beside accuracy, and prefer lot- or time-separated estimates when split_validation flags the random split as optimistic.
- Call validate_job before submit_job: it reports chemistry errors, repeat-unit candidates for monomer SMILES, atom counts and missing engines without creating anything. analyze_job_structure and analyze_process_data return summaries (counts and ranges instead of long arrays) unless detail is full; full results stay downloadable.
- Generic CG bead jobs (polymer_cg, hoomd) are reduced-unit models with no chemical mapping; do not present them as material predictions.
- Measurements you add are always stored unreviewed (verified=false). Only a person can mark data reviewed in the Polymer Lab window, and only reviewed experimental/literature data trains models.
- Martini 3 protein-polymer MD (route mixture, bridge_mode simulate) needs a protein PDB, an explicit per-residue secondary structure from a reviewed DSSP assignment, a polyply block and polymer_net_charge equal to that block's chain charge. elastic_plddt_min (with elastic_network and bfactor_is_plddt on a single-chain AlphaFold model) builds elastic bonds only inside runs of residues above that pLDDT. protein_polymer_lambda below 1 is experimental: it scales only protein-bead/polymer-bead Lennard-Jones epsilon against Martini 3 over-aggregation; it is not fitted for the system, so report it beside an unscaled run.
- polymer_atomistic with atomistic_mode bulk runs RadonPy's packing + Larsen 21-step + NPT sampling on a periodic cell (>=200 atoms). protocol_scale 1 uses RadonPy's full stage lengths (hours to days); smaller values are exploratory and say so in the result, together with RadonPy's convergence verdict.
- Bulk cells take atomistic_forcefield "gaff2" (RadonPy cell; charge_method "gasteiger" or "xtb_cm5" = GFN1-xTB CM5 charges of the capped chain; cell_builder "radonpy" (rigid copies at 0.05 g/cm^3, then compression) or "native" (Polymer Lab grows every chain at build_density with configurational-bias trials and periodic overlap checks, then minimisation + NVT replace the compression)) or "pcff" (EMC 9.4.4 builds and types a PCFF class II cell with bond-increment charges; the same 21-step schedule and RadonPy estimators follow). PCFF results report how many EMC coefficients equal LAMMPS' public pcff.frc under msi2lmp lookup rules and how many came from EMC's automatic fallbacks.
- atomistic_mode "tg_scan" (source_job_id = a completed bulk or crosslink job; tg_high_kelvin, tg_low_kelvin, tg_step_kelvin, tg_stage_steps) holds the copied cell at each temperature under NPT and fits a hyperbola to specific volume (Patrone et al. 2016) for Tg with a profile-likelihood interval, a two-line Tg and glass/melt expansion coefficients. Always report the cooling rate with Tg: MD rates are 1e9-1e12 K/s, so the value sits above DSC Tg and is never rate-converted. transition_resolved false means the scan shows no usable slope change or Tg sits at its edge; widen or lengthen the scan instead of quoting it. One scan of one cell does not give cell-to-cell uncertainty.
- atomistic_mode "crosslink" cures an epoxy resin (resin_smiles, >=2 terminal glycidyl groups) with a primary-amine hardener (hardener_smiles) using LAMMPS fix bond/react on an EMC PCFF cell: 21-step densification, primary and secondary amine ring opening until target_conversion or cure_steps, then NPT cooling and sampling. It reports conversion versus the ideal Flory-Stockmayer gel point. Reactions occur on contact within reaction_cutoff_angstrom (cure_cutoff_schedule "stepwise" raises it from 3.5 A in 0.5 A stages; secondary_amine_probability below 1 is an assumed reactivity contrast), so this is network structure, not cure kinetics; etherification is not modelled. PCFF and crosslink jobs fail before LAMMPS when EMC lacks coefficients (e.g. Si-O, phosphates) or the data file has zero, missing or non-finite terms; use gaff2 then. EMC and xtb are downloaded once from pinned releases.
- Protein import accepts protein_repair "pdbfixer": missing heavy atoms, internal SEQRES gaps and modified residues are rebuilt, ligands and water removed, terminal tails not built; repair.json lists every change and the submitted file is kept.
- analyze_instrument_curve kind "dsc" with options.analysis "melting" integrates a melting endotherm above a pybaselines arPLS or (baseline "linear") straight baseline (endotherm direction, heating rate and integration window must be stated) into melting temperature, melting enthalpy and, with reference_enthalpy_j_per_g, crystallinity drafts; cold_crystallization_window (linear baseline only) and polymer_weight_fraction correct the crystallinity. The DSC Tg draft takes tg_definition midpoint, inflection, onset or endset (ISO 11357-2 tangents) and reports the heat-capacity step when heating_rate_k_per_min is given. TGA adds the DTG peak, the ISO 11358-1 extrapolated onset (td_definition "onset") and, with residue_temperature, char yield; GPC adds Mz, accepts baseline_method "arpls" and, with mark_houwink constants, universal calibration.
- analyze_instrument_curve kinds "ir", "uvvis" and "turbidity" read a CSV (x_unit, y_quantity and for transmittance y_unit must be stated) or, for ir/uvvis, one JCAMP-DX spectrum. IR always lists peaks with candidate group assignments from correlation tables (candidates, not identifications) and computes carbonyl_index (SAUB windows by default), band_ratio or epoxide_conversion (against reference_text, the uncured spectrum, or initial_ratio) with an explicit band_baseline and measure. UV-vis analysis "peaks" (absorption maximum), "concentration" (Beer-Lambert with molar or mass absorptivity, a calibration, or a protein_sequence with cystines at 280 nm; results describe the sample and are not stored) or "band_gap" (tauc_direct, tauc_indirect or absorption_onset over a stated fit window; diffuse reflectance through Kubelka-Munk). Turbidity gives a cloud point by a stated definition on one heating or cooling scan.
- Protein import/AlphaFold DB/ColabFold results include a SAP (spatial aggregation propensity) screen and a PROPKA3 titration at protein_ph (pKa values, net charge, isoelectric point, screened-Coulomb surface charge patches at ionic_strength_molar) of one static structure, plus a PDB2PQR protonated structure with AMBER residue names. packmol_target "sap_patch", "negative_patch" (for cationic polymers) or "positive_patch" (for anionic polymers) places chains near that patch; that is a placement choice, not evidence of binding. Flag groups whose pKa lies within 1 unit of the pH as uncertain.
- Mixture bridges with protein_protonation "propka" build the protein (martinize2 -from amber, tleap, OpenFF) from the PDB2PQR structure at protein_ph; "as_input" keeps the submitted protonation.
- screen_polymer predicts what RadonPy's published all-atom MD workflow (PI1070, 300 K) would report for a repeat unit, in seconds, with uncertainty, cross-validated error and nearest reference polymers. Treat it as a simulation-fidelity prior for choosing what to compute or make, never as measured data; check applicability.domain.
- HSP tools rank solvents by RED/chi, fit a Hansen sphere from good/bad solvent tests (drafts only; store them with add_measurement for human review) and screen polymer blends with regular-solution chi, which cannot capture hydrogen-bonded or ionic miscibility.
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

def summarize(value, limit=20):
    """Long numeric arrays become a count and range and long record lists keep their first items, so a
    tool result stays small; control-chart points keep the signalled ones. Full values stay on the server."""
    if isinstance(value, dict):
        if value.get("analysis") == "control_chart" and isinstance(value.get("points"), list):
            signalled = [point for point in value["points"] if point.get("signals")]
            # Points beyond 3 sigma first, then the earliest other signals, reported in chart order.
            kept = sorted(sorted(signalled, key=lambda p: "beyond_3_sigma" not in p["signals"])[:20], key=lambda p: p["position"])
            value = {**value, "points": {"omitted_items": len(value["points"])}, "signalled_points": kept,
                     "signalled_count": len(signalled)}
        return {key: summarize(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        numbers = [v for v in value if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(value) > limit and len(numbers) + value.count(None) == len(value):
            finite = [v for v in numbers if math.isfinite(v)]
            return {"omitted_values": len(value), "min": min(finite) if finite else None, "max": max(finite) if finite else None}
        if len(value) > 5 * limit:
            return {"omitted_items": len(value), "first": [summarize(item, limit) for item in value[:3]]}
        return [summarize(item, limit) for item in value]
    return value


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
MIXTURE_COMPONENTS = {"type": "array", "minItems": 2, "maxItems": 6,
                      "items": _schema({"name": {"type": "string", "minLength": 1, "maxLength": 80},
                                        "low": {"type": "number", "minimum": 0, "maximum": 1}, "high": {"type": "number", "minimum": 0, "maximum": 1}},
                                       ["name", "low", "high"]),
                      "description": "Component proportion bounds (fractions of the whole, 0 <= low < high <= 1); lower bounds must sum below 1 and upper bounds above 1. Names must match CSV headers."}
DOE_FACTORS = {"type": "array", "minItems": 2, "maxItems": 6,
               "items": _schema({"name": {"type": "string", "minLength": 1, "maxLength": 80},
                                  "low": {"type": "number"}, "high": {"type": "number"}}, ["name", "low", "high"]),
               "description": "Explicit factor bounds, low < high; 2-6 factors (2-4 for central composite/quadratic). Names must match CSV headers."}


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
        t("analyze_job_structure", "Analyze finished bulk structure",
          "Rust analysis of a completed polymer_atomistic bulk/crosslink job: rdf (element pairs), msd (unwrapped trajectory), "
          "free_volume (Bondi radii and h/2h grid comparison), or widom (single-site gas insertion, GAFF2 only). "
          "Saves a separate immutable result and returns its download link, units, source hashes, full numeric arrays and scientific warnings. "
          "Original job artifacts and reviewed measurements are unchanged; each successful call creates a new analysis record. "
          "Only parameters applicable to the chosen analysis are accepted. One analysis runs at a time; large requests may exceed the client's timeout.",
          _schema({"job_id": JOB_ID, "analysis": {"type": "string", "enum": ["rdf", "msd", "free_volume", "widom"]},
                   "parameters": _schema({
                       "every": {"type": "integer", "minimum": 1, "maximum": 10000, "description": "rdf/msd/widom: sample every Nth frame"},
                       "max_frames": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "rdf/msd/widom: frame limit"},
                       "pairs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8, "description": "rdf: e.g. C-C, C-H, heavy-heavy"},
                       "r_max": {"type": "number", "exclusiveMinimum": 0, "description": "rdf: maximum distance in angstrom, at most half the shortest box edge"},
                       "bins": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "rdf: histogram bins"},
                       "exclude_same_molecule": {"type": "boolean", "description": "rdf: exclude intramolecular pairs"},
                       "selection": {"type": "string", "description": "msd: all, heavy, or comma-separated elements"},
                       "remove_drift": {"type": "boolean", "description": "msd: subtract whole-cell center-of-mass motion"},
                       "time_step_fs": {"type": "number", "exclusiveMinimum": 0, "maximum": 1e6, "description": "msd: integrator time step in fs; omit to read bulk metadata"},
                       "fit_window": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1}, "minItems": 2, "maxItems": 2, "description": "msd: increasing fractions of the trajectory lag range"},
                       "probes": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 5}, "minItems": 1, "maxItems": 8, "description": "free_volume: probe radii in angstrom"},
                       "spacing": {"type": "number", "minimum": 0.05, "maximum": 1, "description": "free_volume: requested fine grid spacing in angstrom"},
                       "frame": {"anyOf": [{"type": "string", "enum": ["last"]}, {"type": "integer", "minimum": 0}], "description": "free_volume: last or zero-based frame index"},
                       "gas": {"type": "string", "enum": ["He", "H2", "N2", "O2", "Ar", "CH4", "CO2"], "description": "widom: single-site LJ gas"},
                       "temperature": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000, "description": "widom: kelvin; omit to read bulk metadata"},
                       "insertions": {"type": "integer", "minimum": 1, "maximum": 200000, "description": "widom: insertions per frame"},
                       "cutoff": {"type": "number", "exclusiveMinimum": 0, "description": "widom: LJ cutoff in angstrom, at most half the shortest box edge"},
                       "seed": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1, "description": "widom: deterministic random seed"},
                   }), "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary", "description": "summary replaces long numeric arrays with counts and ranges; full returns every value"}}, ["job_id", "analysis"]), self.analyze_job_structure, read_only=False, idempotent=False)
        t("validate_job", "Check a job before submitting",
          "Dry run of submit_job: the same JobConfig validation, the repeat-unit attachment check (exactly two '*' joined by single bonds; "
          "a monomer such as C=Cc1ccccc1 returns repeat-unit candidates to confirm), resin/hardener checks for crosslink, atom counts against route "
          "limits (bulk cells need 200 atoms) and required engines. Nothing is created. Call it before submit_job and show errors and candidates to the user.",
          _schema({"config": _job_config_schema()}, ["config"]), self.validate_job, read_only=True, idempotent=True)
        t("submit_job", "Submit calculation job",
          "Queue one calculation with a validated JobConfig. Routes: polymer_cg (reduced-unit CG), polymer_atomistic (RDKit build or RadonPy MD), protein (AlphaFold DB, PDB import, AF3 export, ColabFold), "
          "packmol (packmol_target sap_patch / negative_patch / positive_patch places chains near the protein's SAP or PROPKA surface-charge patch), polyply, pysimm, hoomd, mixture (protein-polymer bridges; bridge_mode simulate runs Martini 3 GROMACS MD). "
          "polymer_atomistic atomistic_mode bulk builds a periodic cell and runs RadonPy's 21-step equilibration (protocol_scale scales stage lengths); "
          "atomistic_mode tg_scan with source_job_id of a completed bulk/crosslink job reruns that cell through stepwise NPT cooling and fits Tg. "
          "source_job_id otherwise hands a finished PDB to packmol/mixture. "
          "Run validate_job first; chemistry errors are rejected before a job is created.",
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
          "Draft measurements from an instrument curve without saving: dsc (Tg midpoint/onset/endset, melting enthalpy, crystallinity), tensile (modulus), "
          "tga (mass-loss temperature, DTG peak, char yield), gpc (Mn/Mw/Mz/dispersity), ir (peaks with candidate assignments, carbonyl index, band ratio, epoxide conversion), "
          "uvvis (absorption maximum, Beer-Lambert concentration, optical gap) and turbidity (cloud point). csv_text may hold a JCAMP-DX spectrum for ir and uvvis. Options follow the Polymer Lab curve form.",
          _schema({"kind": {"type": "string", "enum": ["dsc", "tensile", "tga", "gpc", "ir", "uvvis", "turbidity"]}, "csv_text": {"type": "string", "maxLength": 2000000},
                   "options": {"type": "object"}}, ["kind", "csv_text"]), self.analyze_instrument_curve, read_only=True, idempotent=True)
        t("analyze_process_data", "Analyze process data table",
          "Read-only analysis of one process/fab CSV with explicitly named columns; rows are never dropped silently and nothing is stored. "
          "kind dictionary (options keys [top..bottom], value, unit, order), control_chart (value, optional subgroup for Xbar-S, order, baseline, unit), "
          "capability (value, lsl and/or usl, optional subgroup, order, unit), variance_components (value, factors [top..bottom]; balanced nested design, "
          "the residual is replication within the lowest factor), stratified_rates (group, stratum, and defects+inspected counts or a 0/1 outcome column, optional reference), "
          "classification_metrics (label, score, positive, thresholds, miss_cost with false_alarm_cost; compares with flagging nothing), "
          "regression_metrics (actual, predicted, lsl/usl, band near the limits) or split_validation (target, features, task, group and/or order; "
          "a fixed baseline model scored under random, lot-grouped and time-forward splits).",
          _schema({"kind": {"type": "string", "enum": ["dictionary", "control_chart", "capability", "variance_components", "stratified_rates",
                                                    "classification_metrics", "regression_metrics", "split_validation"]},
                   "csv_text": {"type": "string", "maxLength": 20000000},
                   "options": {"type": "object", "description": "Column names and settings for the chosen kind; unknown keys are rejected"},
                   "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary", "description": "summary replaces long numeric arrays with counts and ranges; full returns every value"}},
                  ["kind", "csv_text", "options"]), self.analyze_process_data, read_only=True, idempotent=True)
        t("get_research_workflow", "Read connected bulk workflow",
          "Read linked DOE rows, calculation status, observations and unvalidated recommendations. Omit workflow_id to list flows.",
          _schema({"workflow_id": JOB_ID}), self.get_research_workflow, read_only=True, idempotent=True)
        t("create_research_workflow", "Connect DOE to bulk calculations",
          "Save a reviewed two-factor DOE connection to GAFF2 native bulk jobs. Does not start calculations. Packing density is a preparation parameter. Templates, bindings and metrics follow /api/research/workflows/capabilities.",
          _schema({"design_id": JOB_ID, "template": _job_config_schema(), "bindings": {"type": "object", "additionalProperties": {"type": "string", "enum": ["temperature_kelvin", "build_density"]}},
                   "metric": {"type": "string", "enum": ["final_frame_density", "probe_accessible_fraction"]},
                   "budget": {"type": "integer", "minimum": 1, "maximum": 50}, "probe": {"type": "number", "minimum": 0, "maximum": 5},
                   "spacing": {"type": "number", "minimum": 0.05, "maximum": 1}}, ["design_id", "template", "bindings"]), self.create_research_workflow)
        t("control_research_workflow", "Advance one reviewed research stage",
          "Perform exactly one explicitly user-authorized stage: submit selected rows, attach a matching completed job, analyze selected rows, optimize reviewed observations with explicit exclusion reasons, confirm one recommendation, or cancel. Never automatically advance to the next stage. Simulation observations are not verified experimental measurements.",
          _schema({"workflow_id": JOB_ID, "action": {"type": "string", "enum": ["submit", "attach", "analyze", "optimize", "confirm", "analyze-confirmation", "cancel"]},
                   "parameters": {"type": "object", "description": "Stage-specific body from /api/research/workflows API; submit/analyze require rows, confirm requires optimization_id."}}, ["workflow_id", "action"]), self.control_research_workflow)
        t("create_experiment_design", "Create experimental design",
          "Generate and save a randomized full factorial or face-centered central composite design. Returns the run table, a CSV with a blank response column, and download links. "
          "Creates an immutable local DOE record; does not execute physical experiments or approve measurements. Factor bounds must reflect the user's feasible experimental range.",
          _schema({"factors": DOE_FACTORS, "kind": {"type": "string", "enum": ["factorial", "central_composite"], "default": "factorial"},
                   "center_points": {"type": "integer", "minimum": 0, "maximum": 20, "default": 3},
                   "replicates": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1},
                   "seed": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1, "default": 1}}, ["factors"]), self.create_experiment_design)
        t("analyze_response_surface", "Fit and optimize experimental response",
          "Fit a linear, interaction or quadratic response model to CSV experimental data, then recommend a candidate inside explicit factor bounds for maximize/minimize/target. "
          "Rust computes ANOVA, lack-of-fit/pure error, residual influence, PRESS predicted R-squared, model comparisons and 95% prediction intervals. Returns a blank confirmation-experiment CSV. "
          "Saves the original CSV and result as an immutable local DOE record. Coefficients, diagnostics, uncertainty and warnings must accompany any recommendation; confirm it experimentally. "
          "CSV is limited to 1 MB UTF-8 and selected numeric values cannot be missing. This never creates reviewed measurements.",
          _schema({"text": {"type": "string", "maxLength": 1000000, "description": "CSV with a header and measured response values"},
                   "factors": DOE_FACTORS, "response": {"type": "string", "minLength": 1, "maxLength": 80},
                   "model": {"type": "string", "enum": ["linear", "interaction", "quadratic"], "default": "quadratic"},
                   "objective": {"type": "string", "enum": ["maximize", "minimize", "target"], "default": "maximize"},
                   "target": {"type": "number", "description": "Required finite target response only for objective=target"},
                   "prediction_points": {"type": "array", "maxItems": 50,
                       "items": {"type": "object", "additionalProperties": {"type": "number"}},
                       "description": "Optional conditions to predict, each mapping every factor name to a real value within its declared bounds. Returns mean confidence and individual prediction intervals."}},
                  ["text", "factors", "response"]), self.analyze_response_surface)
        t("create_mixture_design", "Create mixture design",
          "Generate and save a randomized mixture design in proportions summing to one: simplex_lattice {q, degree}, simplex_centroid, or extreme_vertices (degree 2 adds edge midpoints) "
          "for upper-bounded regions; axial adds check blends, centroid adds the overall centroid. Lower bounds use L-pseudocomponents. Creates an immutable local DOE record; executes nothing.",
          _schema({"components": MIXTURE_COMPONENTS, "kind": {"type": "string", "enum": ["simplex_lattice", "simplex_centroid", "extreme_vertices"], "default": "simplex_lattice"},
                   "degree": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2}, "axial": {"type": "boolean", "default": False},
                   "centroid": {"type": "boolean", "default": True}, "replicates": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1},
                   "seed": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1, "default": 1}}, ["components"]), self.create_mixture_design)
        t("analyze_mixture_response", "Fit and optimize a mixture response",
          "Fit a Scheffé linear, quadratic or special-cubic model in L-pseudocomponents to mixture CSV data (rows must sum to one within bounds), with ANOVA, lack-of-fit, model comparison "
          "and a bounded, unvalidated blend recommendation with 95% prediction interval. Linear blending terms get no t-tests (they are pure-component responses, not effects). Saves an immutable record.",
          _schema({"text": {"type": "string", "maxLength": 1000000}, "components": MIXTURE_COMPONENTS, "response": {"type": "string", "minLength": 1, "maxLength": 80},
                   "model": {"type": "string", "enum": ["scheffe_linear", "scheffe_quadratic", "scheffe_special_cubic"], "default": "scheffe_quadratic"},
                   "objective": {"type": "string", "enum": ["maximize", "minimize", "target"], "default": "maximize"},
                   "target": {"type": "number"},
                   "prediction_points": {"type": "array", "maxItems": 50, "items": {"type": "object", "additionalProperties": {"type": "number"}}}},
                  ["text", "components", "response"]), self.analyze_mixture_response)
        t("optimize_desirability", "Multi-response desirability optimum",
          "Fit each response column on the same runs (process factors or mixture components, exactly one) and maximize the Derringer-Suich composite desirability D: per response goal "
          "maximize (lower, target), minimize (target, upper) or target (lower, target, upper), shape exponents and importance 0.1-10. Returns the candidate settings, predicted values with "
          "intervals, individual d and D, and the best observed run. Bounds and importances are the user's choices; the optimum is unvalidated.",
          _schema({"text": {"type": "string", "maxLength": 1000000},
                   "responses": {"type": "array", "minItems": 1, "maxItems": 10, "items": _schema({
                       "name": {"type": "string", "minLength": 1, "maxLength": 80},
                       "model": {"type": "string", "enum": ["linear", "interaction", "quadratic", "scheffe_linear", "scheffe_quadratic", "scheffe_special_cubic"]},
                       "goal": {"type": "string", "enum": ["maximize", "minimize", "target"]},
                       "lower": {"type": "number"}, "target": {"type": "number"}, "upper": {"type": "number"},
                       "lower_shape": {"type": "number", "minimum": 0.1, "maximum": 10}, "upper_shape": {"type": "number", "minimum": 0.1, "maximum": 10},
                       "importance": {"type": "number", "minimum": 0.1, "maximum": 10}}, ["name", "model", "goal", "target"])},
                   "factors": DOE_FACTORS, "components": MIXTURE_COMPONENTS}, ["text", "responses"]), self.optimize_desirability)
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
        t("screen_polymer", "Screen polymer properties (seconds)",
          "Predict RadonPy all-atom MD bulk properties at 300 K and 1 atm (density, Cp, bulk modulus, compressibility, volume expansion, refractive index, thermal conductivity) for up to 20 repeat units "
          "with a Gaussian process trained on RadonPy PI1070 (1,077 polymers). Returns value, std, 95% interval, applicability domain from nearest-neighbour similarity, the nearest reference polymers "
          "and this workspace's own bulk runs of the same repeat unit. include_validation adds 5-fold cross-validation metrics.",
          _schema({"repeat_smiles": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20, "description": "Repeat units with two attachment points, e.g. *CC(*)c1ccccc1"},
                   "properties": {"type": "array", "items": {"type": "string", "enum": ["bulk_density", "heat_capacity_cp", "bulk_modulus", "isothermal_compressibility", "volume_expansion", "refractive_index", "thermal_conductivity"]}},
                   "include_validation": {"type": "boolean", "default": False}}, ["repeat_smiles"]),
          self.screen_polymer, read_only=True, idempotent=True)
        t("rank_solvents", "Rank solvents by Hansen distance",
          "Rank 248 Hansen solvents (or the named subset) for a polymer HSP point: Ra, RED = Ra/R0 when radius is given, and Lindvig chi. blends > 0 also returns the best two-solvent mixtures, including pairs of non-solvents whose mixture lands inside the sphere.",
          _schema({"polymer": {"type": "object", "description": "{delta_d, delta_p, delta_h, radius?} in MPa^0.5"}, "temperature": {"type": "number", "default": 298.15},
                   "alpha": {"type": "number", "default": 0.6}, "limit": {"type": "integer", "minimum": 1, "maximum": 300, "default": 25},
                   "names": {"type": "array", "items": {"type": "string"}}, "blends": {"type": "integer", "minimum": 0, "maximum": 50, "default": 0}}, ["polymer"]),
          self.rank_solvents, read_only=True, idempotent=True)
        t("fit_hsp_sphere", "Fit Hansen solubility sphere",
          "Fit a polymer's HSP center and radius R0 from solvent tests (good = dissolves/swells). Solvents are database names/CAS or explicit delta values. Returns DATAFIT, misclassified solvents, warnings and unsaved measurement drafts.",
          _schema({"tests": {"type": "array", "items": {"type": "object", "description": "{solvent: name or CAS, good: bool} or {name, delta_d, delta_p, delta_h, good}"}, "minItems": 2, "maxItems": 300}}, ["tests"]),
          self.fit_hsp_sphere, read_only=True, idempotent=True)
        t("blend_compatibility", "Polymer blend compatibility",
          "Regular-solution chi from two polymers' HSP and Flory-Huggins critical chi from degrees of polymerization. Repeat-unit molar volume may be given or estimated from repeat_smiles via the PI1070 density surrogate.",
          _schema({"a": {"type": "object", "description": "{name?, delta_d, delta_p, delta_h, degree_of_polymerization, molar_volume? | repeat_smiles?}"},
                   "b": {"type": "object", "description": "same fields as a"}, "temperature": {"type": "number", "default": 298.15},
                   "reference_volume": {"type": "number", "default": 100.0}}, ["a", "b"]),
          self.blend_compatibility, read_only=True, idempotent=True)
        t("blend_chi_contacts", "Blends-style chi from pair contacts (experimental)",
          "Flory-Huggins chi(T) between two molecules (solvents or capped polymer repeat units) by the Blends method of Fan et al. (1992): "
          "random van der Waals contacts with GAFF2/Gasteiger energies and packed coordination numbers, computed by the Rust kernel. Returns chi(T), "
          "a fit chi = A + B/T, a bootstrap error, coordination numbers and the Flory-Huggins critical point/spinodal for n_base, n_screen. "
          "Enthalpic chi only, from rigid single conformers; validation against Hansen chi_H shows rank correlation 0.65 with wrong signs for "
          "aliphatic solvents of aromatic polymers, so report it as an exploratory estimate.",
          _schema({"base_smiles": {"type": "string"}, "screen_smiles": {"type": "string"},
                   "temperatures": {"type": "array", "items": {"type": "number"}, "maxItems": 200},
                   "samples": {"type": "integer", "default": 200000, "minimum": 1000, "maximum": 2000000},
                   "clusters": {"type": "integer", "default": 2000, "minimum": 20, "maximum": 5000},
                   "seed": {"type": "integer", "default": 1}, "cap": {"type": "string", "enum": ["hydrogen", "methyl"]},
                   "contact_scale": {"type": "number", "default": 1.0}, "n_base": {"type": "number", "default": 1}, "n_screen": {"type": "number", "default": 1}},
                  ["base_smiles", "screen_smiles"]),
          self.blend_chi_contacts, read_only=True, idempotent=True)
        t("rea_evidence", "REA source evidence", "Audited upstream repositories behind each engine adapter: pinned commit, license and REA inventory status.",
          _schema({}), self.rea_evidence, read_only=True, idempotent=True)
        t("create_research_report", "Create research report",
          "Save an immutable report folder for one DoE record (kind doe), one bulk workflow (kind workflow) or the lab master table of experiments and measurements (kind lab): "
          "summary.md, report.html, SVG/PNG figures, CSV tables plus report.xlsx and a manifest with SHA-256/MD5 per file. Numbers and verified flags are copied from saved records; "
          "nothing is refitted or reviewed. upload_to_drive copies the folder to the user's configured rclone Google Drive remote and reports success only after every file's size and MD5 match; "
          "set it only when the user asked for a Drive upload.",
          _schema({"kind": {"type": "string", "enum": ["doe", "workflow", "lab"]},
                   "id": {"type": "string", "pattern": "[0-9a-f]{32}", "description": "DoE record or workflow ID; omit for lab"},
                   "upload_to_drive": {"type": "boolean"}}, ["kind"]), self.create_research_report)
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

    def validate_job(self, args):
        return self.client.request("POST", "/api/jobs/preflight", args["config"])

    def analyze_job_structure(self, args):
        result = self.client.request("POST", f"/api/jobs/{args['job_id']}/analysis",
                                     {"analysis": args["analysis"], "parameters": args.get("parameters", {})}, timeout=600)
        return result if args.get("detail") == "full" else summarize(result)

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

    def analyze_process_data(self, args):
        result = self.client.request("POST", "/api/research/process", {"kind": args["kind"], "text": args["csv_text"], "options": args["options"]}, timeout=120)
        return result if args.get("detail") == "full" else summarize(result)

    def get_research_workflow(self, args):
        suffix = "/" + args["workflow_id"] if args.get("workflow_id") else ""
        return self.client.request("GET", "/api/research/workflows" + suffix)

    def create_research_workflow(self, args):
        return self.client.request("POST", "/api/research/workflows", args, timeout=300)

    def control_research_workflow(self, args):
        return self.client.request("POST", f"/api/research/workflows/{args['workflow_id']}/{args['action']}", args.get("parameters", {}), timeout=3600)

    def create_experiment_design(self, args):
        return self.client.request("POST", "/api/research/doe/design", args, timeout=300)

    def analyze_response_surface(self, args):
        return self.client.request("POST", "/api/research/doe/analyze", args, timeout=300)

    def create_mixture_design(self, args):
        return self.client.request("POST", "/api/research/doe/mixture-design", args, timeout=300)

    def analyze_mixture_response(self, args):
        return self.client.request("POST", "/api/research/doe/mixture-analyze", args, timeout=300)

    def optimize_desirability(self, args):
        return self.client.request("POST", "/api/research/doe/desirability", args, timeout=300)

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

    def screen_polymer(self, args):
        body = {"smiles": args["repeat_smiles"], "properties": args.get("properties")}
        result = self.client.request("POST", "/api/research/screening/predict", body, timeout=180)
        if args.get("include_validation"):
            result["cross_validation"] = self.client.request("GET", "/api/research/screening/validation", timeout=180)
        return result

    def rank_solvents(self, args):
        return self.client.request("POST", "/api/research/screening/solvents", args, timeout=120)

    def fit_hsp_sphere(self, args):
        return self.client.request("POST", "/api/research/screening/hsp-fit", args, timeout=120)

    def blend_compatibility(self, args):
        return self.client.request("POST", "/api/research/screening/blend", args, timeout=180)

    def blend_chi_contacts(self, args):
        return self.client.request("POST", "/api/research/screening/blend-contacts", args, timeout=600)

    def rea_evidence(self, args):
        data = self.client.request("GET", "/api/evidence")
        def summary(repo):
            return {key: repo.get(key) for key in ("id", "name", "repository", "commit", "license", "rea_status", "snapshot_scope")}
        return {"rea": {key: (data.get("rea") or {}).get(key) for key in ("version", "status", "limitations")},
                "repositories": [summary(r) for r in data.get("repositories", [])],
                "extensions": [summary(r) for r in (data.get("extensions") or {}).get("repositories", [])]}

    def create_research_report(self, args):
        record = self.client.request("POST", "/api/research/reports", {"kind": args["kind"], "id": args.get("id")}, timeout=300)
        if args.get("upload_to_drive"):
            record = self.client.request("POST", f"/api/research/reports/{record['id']}/drive", {}, timeout=900)
        paths = [f["path"] for f in record["files"]]
        return {"id": record["id"], "title": record["title"], "kind": record["kind"], "subject_id": record["subject_id"], "status": record["status"],
                "drive": record.get("drive"), "files": len(paths) + 1,
                "figures": sorted({p.split("/")[1].rsplit(".", 1)[0] for p in paths if p.startswith("figures/")}),
                "tables": sorted(p.split("/")[1] for p in paths if p.startswith("tables/")),
                "view_url": self.client.origin + record["view_url"], "download_url": self.client.origin + record["download_url"],
                "warnings": record["warnings"]}

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
