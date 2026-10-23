"""Run one approved bulk-workflow plan to its end without asking at every stage.

A person approves the plan once: which rows, how many identical attempts per run, how many failed rows
end the run, whether to fit, confirm and report, and the largest number of new calculations. Inside that
plan the runner submits, retries, analyzes, fits and confirms by itself. A retry resubmits the identical
configuration; no physical setting is ever changed. Simulated values stay verified=false and the
recommendation stays a candidate until people review it.
"""
from __future__ import annotations

import threading

from .jobs import now
from .workflows import autopilot_context, digest

RETRYABLE = {"failed", "interrupted"}
FINAL = {"blocked", "cancelled", "missing"}
RUNNING = {"queued", "running", "cancelling"}
MAX_EVENTS = 400
STATUS = {"failed": "실패", "interrupted": "중단", "blocked": "엔진·입력 부족", "cancelled": "취소", "missing": "작업 기록 없음"}


def model_terms(factors, model):
    k = len(factors)
    return 1 + k + (k * (k - 1) // 2 if model != "linear" else 0) + (k if model == "quadratic" else 0)


def normalize(record, plan):
    """Validate a plan against the saved workflow and return it with its worst-case calculation count."""
    known = [row["run_order"] for row in record["rows"]]
    rows = sorted(set(plan.get("rows") or known))
    if len(rows) != len(plan.get("rows") or known) or not set(rows) <= set(known):
        raise ValueError("계획의 행은 이 설계에 있는 행을 중복 없이 지정하세요.")
    attempts, allowed = int(plan.get("max_attempts", 2)), int(plan.get("max_failed_rows", 0))
    if not 1 <= attempts <= 3:
        raise ValueError("행마다 시도 횟수는 1~3회입니다.")
    if not 0 <= allowed <= len(rows):
        raise ValueError("허용 실패 행 수는 0부터 계획한 행 수까지입니다.")
    optimize, confirm = bool(plan.get("optimize", True)), bool(plan.get("confirm", True))
    if confirm and not optimize:
        raise ValueError("확인 계산은 반응표면 최적화를 함께 계획해야 합니다.")
    model, objective, target = plan.get("model", "quadratic"), plan.get("objective", "maximize"), plan.get("target")
    if model not in {"linear", "interaction", "quadratic"} or objective not in {"maximize", "minimize", "target"}:
        raise ValueError("모델과 목표를 확인하세요.")
    if (objective == "target") != (target is not None):
        raise ValueError("목표값은 목표값 맞추기에서만, 반드시 지정하세요.")
    needed = model_terms(record["factors"], model) + 1
    if optimize and len(rows) - allowed < needed:
        raise ValueError(f"{model} 모델에는 관측 {needed}행 이상이 필요합니다. 허용 실패 행 수를 줄이거나 행을 늘리세요.")
    if bool(plan.get("upload_report")) and not plan.get("report", True):
        raise ValueError("Drive 업로드는 보고서를 함께 계획해야 합니다.")
    normalized = {"rows": rows, "max_attempts": attempts, "max_failed_rows": allowed, "optimize": optimize, "model": model,
                  "objective": objective, "target": target, "confirm": confirm, "report": bool(plan.get("report", True)),
                  "upload_report": bool(plan.get("upload_report", False))}
    selected = [row for row in record["rows"] if row["run_order"] in rows and row["state"] != "observed"]
    runs = sum(max(0, attempts - len(row["attempts"])) for row in selected) + (attempts if confirm else 0)
    outside = [row["run_order"] for row in record["rows"] if row["run_order"] not in rows and row["state"] != "observed"]
    return normalized, {"worst_case_runs": runs, "remaining_budget": record["budget"] - record["submitted"],
                        "rows_to_run": [row["run_order"] for row in selected], "excluded_by_plan": outside if optimize else [],
                        "minimum_observations": needed if optimize else None}


class StopRun(Exception):
    def __init__(self, state, reason):
        super().__init__(reason)
        self.state, self.reason = state, reason


class Autopilot:
    def __init__(self, workflows, research_root, store, poll=5.0):
        self.workflows, self.research_root, self.store, self.poll = workflows, research_root, store, poll
        self.events, self.threads, self.cancelled = {}, {}, set()
        self.lock = threading.Lock()
        workflows.on_cancel = self.cancel

    def preview(self, identifier, plan):
        record = self.workflows.get(identifier)
        normalized, cost = normalize(record, plan)
        return {"plan": normalized, **cost, "missing_engines": record["missing_engines"]}

    def start(self, identifier, plan, approved_budget, approved_by=None):
        with self.workflows.lock:
            record = self.workflows.get(identifier)
            if record["busy"] or (record.get("autopilot") or {}).get("state") == "running":
                raise RuntimeError("이 연구 흐름의 단계 또는 자동 실행이 진행 중입니다.")
            if record["state"] == "cancelled":
                raise RuntimeError("취소된 연구 흐름입니다. 새 흐름을 만드세요.")
            normalized, cost = normalize(record, plan)
            if record["missing_engines"]:
                raise RuntimeError("계산 엔진이 필요합니다: " + ", ".join(record["missing_engines"]))
            if not isinstance(approved_budget, int) or isinstance(approved_budget, bool) or approved_budget < cost["worst_case_runs"]:
                raise ValueError(f"이 계획은 최대 {cost['worst_case_runs']}개 계산을 씁니다. 그 수 이상을 승인 예산으로 지정하세요.")
            if approved_budget > cost["remaining_budget"]:
                raise ValueError(f"승인 예산이 이 흐름의 남은 예산 {cost['remaining_budget']}개를 넘습니다.")
            stored = self.workflows._read(identifier)
            history = (stored.get("autopilot") or {}).get("events", [])
            stored["autopilot"] = {"state": "running", "plan": normalized, "plan_sha256": digest(normalized), "approved_budget": approved_budget,
                                   "approved_at": now(), "approved_by": approved_by, "submitted_at_approval": stored["submitted"],
                                   "stopped_reason": None, "report_id": None, "events": history[-MAX_EVENTS:]}
            self._append(stored, "approve", f"계획 승인 · 행 {len(normalized['rows'])}개 · 최대 {cost['worst_case_runs']}개 계산 · 승인 예산 {approved_budget}")
            self.workflows._save(stored)
            event = threading.Event()
            with self.lock:
                self.events[identifier] = event
                thread = threading.Thread(target=self._run, args=(identifier, event), name="workflow-autopilot", daemon=True)
                self.threads[identifier] = thread
            thread.start()
        return self.workflows.get(identifier)

    def request_stop(self, identifier):
        with self.lock:
            event = self.events.get(identifier)
        if event:
            event.set()
        return bool(event)

    def cancel(self, identifier):
        # Called by WorkflowManager.cancel before it cancels jobs, so the runner reports cancellation, not a failure.
        with self.lock:
            if identifier in self.events:
                self.cancelled.add(identifier)
        self.request_stop(identifier)

    def stop(self, identifier):
        requested = self.request_stop(identifier)
        record = self.workflows.get(identifier)
        if record.get("autopilot"):
            record["autopilot"]["stop_requested"] = requested
        return record

    def close(self):
        with self.lock:
            for event in self.events.values():
                event.set()

    # ------------------------------------------------------------ runner

    @staticmethod
    def _append(record, stage, message):
        events = record["autopilot"].setdefault("events", [])
        events.append({"time": now(), "stage": stage, "message": message})
        del events[:-MAX_EVENTS]

    def _log(self, identifier, stage, message, **fields):
        with self.workflows.lock:
            record = self.workflows._read(identifier)
            record["autopilot"].update(fields)
            self._append(record, stage, message)
            self.workflows._save(record)

    @staticmethod
    def _check(event):
        if event.is_set():
            raise StopRun("stopped", "사용자가 자동 실행을 멈췄습니다. 진행 중인 계산은 계속되며 직접 이어서 진행할 수 있습니다.")

    def _wait(self, event):
        if event.wait(self.poll):
            raise StopRun("stopped", "사용자가 자동 실행을 멈췄습니다. 진행 중인 계산은 계속되며 직접 이어서 진행할 수 있습니다.")

    def _plan(self, identifier):
        return self.workflows._read(identifier)["autopilot"]

    def _spent(self, identifier):
        record = self.workflows._read(identifier)
        return record["submitted"] - record["autopilot"]["submitted_at_approval"]

    def _submit(self, identifier, rows, approved):
        if self._spent(identifier) + len(rows) > approved:
            raise StopRun("stopped", "승인 예산을 모두 썼습니다.")
        self.workflows.submit(identifier, rows, retry_failed=True)

    def _compute(self, identifier, event, plan, approved):
        failed = {}
        while True:
            record = self.workflows.get(identifier)
            pending, running = [], []
            for row in record["rows"]:
                if row["run_order"] not in plan["rows"] or row["state"] in {"observed", "analysis_failed"} or row["run_order"] in failed:
                    continue
                status = row.get("job_status") if row["attempts"] else None
                if status is None:
                    pending.append(row["run_order"])
                elif status in RUNNING:
                    running.append(row["run_order"])
                elif status in RETRYABLE and len(row["attempts"]) < plan["max_attempts"]:
                    pending.append(row["run_order"])
                elif status in RETRYABLE or status in FINAL:
                    failed[row["run_order"]] = f"자동 실행: 계산 {STATUS[status]} ({len(row['attempts'])}회 시도)"
                    self._log(identifier, "failure", f"{row['run_order']}행 계산 {STATUS[status]} · 시도 {len(row['attempts'])}회 · 더 재시도하지 않음")
                    if status == "blocked":
                        raise StopRun("stopped", f"{row['run_order']}행이 엔진·입력 부족(blocked)으로 멈췄습니다. 같은 설정의 재시도로 해결되지 않습니다.")
            if len(failed) > plan["max_failed_rows"]:
                raise StopRun("stopped", f"실패한 행 {len(failed)}개가 허용 수 {plan['max_failed_rows']}개를 넘었습니다.")
            if pending:
                retries = [n for n in pending if next(r for r in record["rows"] if r["run_order"] == n)["attempts"]]
                self._submit(identifier, pending, approved)
                self._log(identifier, "submit", f"{len(pending)}행 제출" + (f" · 같은 설정으로 재시도 {', '.join(map(str, retries))}행" if retries else ""))
                continue
            if not running:
                return failed
            self._wait(event)

    def _analyze(self, identifier, event, plan, failed):
        record = self.workflows.get(identifier)
        rows = [r["run_order"] for r in record["rows"] if r["run_order"] in plan["rows"] and r["run_order"] not in failed
                and r["state"] != "observed" and r.get("job_status") == "completed"]
        if rows:
            record = self.workflows.analyze(identifier, rows)
            self._check(event)
            self._log(identifier, "analyze", f"{len(rows)}행 반응값 분석")
        for row in record["rows"]:
            if row["run_order"] in plan["rows"] and row["state"] == "analysis_failed" and row["run_order"] not in failed:
                failed[row["run_order"]] = "자동 실행: 반응값 분석 실패 · " + str(row.get("error") or "")[:200]
        if len(failed) > plan["max_failed_rows"]:
            raise StopRun("stopped", f"계산·분석에 실패한 행 {len(failed)}개가 허용 수 {plan['max_failed_rows']}개를 넘었습니다.")
        return failed

    def _confirm(self, identifier, event, plan, approved, optimization_id):
        for attempt in range(plan["max_attempts"]):
            if self._spent(identifier) + 1 > approved:
                raise StopRun("stopped", "확인 계산에 쓸 승인 예산이 남지 않았습니다.")
            record = self.workflows.confirm(identifier, optimization_id, retry_failed=attempt > 0)
            job_id = [c for c in record["confirmations"] if c["optimization_id"] == optimization_id][-1]["job_id"]
            self._log(identifier, "confirm", f"추천 조건 확인 계산 제출 · 시도 {attempt + 1}회")
            while True:
                status = next(c for c in self.workflows.get(identifier)["confirmations"] if c["job_id"] == job_id).get("job_status")
                if status not in RUNNING:
                    break
                self._wait(event)
            if status == "completed":
                record = self.workflows.analyze_confirmation(identifier, job_id)
                self._check(event)
                observation = next(c for c in record["confirmations"] if c["job_id"] == job_id)["observation"]
                self._log(identifier, "confirm", f"확인 계산 관측값 {observation['value']:.6g} · 예측과 차이 {observation['prediction_error']:.3g} (검토 전)")
                return
            if status not in RETRYABLE:
                raise StopRun("stopped", f"확인 계산이 {STATUS.get(status, status)} 상태로 끝났습니다.")
        raise StopRun("stopped", f"확인 계산이 {plan['max_attempts']}회 모두 실패했습니다.")

    def _report(self, identifier, plan):
        from . import reports
        record = reports.create_report(self.research_root, "workflow", identifier, self.store, self.workflows)
        self._log(identifier, "report", "보고서를 만들었습니다.", report_id=record["id"])
        if plan["upload_report"]:
            from .drive_backup import drive_status
            status = drive_status()
            if not status["configured"]:
                self._log(identifier, "report", "Drive가 설정되지 않아 보고서를 올리지 않았습니다.")
                return
            try:
                reports.upload_report(self.research_root, record["id"], status["remote"])
                self._log(identifier, "report", "보고서를 Drive에 올리고 모든 파일의 MD5를 확인했습니다.")
            except (RuntimeError, ValueError) as exc:
                self._log(identifier, "report", "Drive 업로드를 확인하지 못했습니다: " + str(exc))

    def _run(self, identifier, event):
        state, reason = "completed", None
        try:
            with autopilot_context(identifier):
                autopilot = self._plan(identifier)
                plan, approved = autopilot["plan"], autopilot["approved_budget"]
                failed = self._compute(identifier, event, plan, approved)
                failed = self._analyze(identifier, event, plan, failed)
                if plan["optimize"]:
                    exclusions = {str(n): text for n, text in sorted(failed.items())}
                    for row in self.workflows._read(identifier)["rows"]:
                        if row["run_order"] not in plan["rows"] and row["state"] != "observed":
                            exclusions[str(row["run_order"])] = "자동 실행 계획에 포함하지 않은 행"
                    record = self.workflows.optimize(identifier, plan["model"], plan["objective"], plan["target"], exclusions)
                    self._check(event)
                    best = record["optimizations"][-1]
                    self._log(identifier, "optimize", f"반응표면 적합 · 제외 {len(exclusions)}행 · 추천 예측값 {best['recommendation']['predicted_response']:.6g} (확인 전)")
                    if plan["confirm"]:
                        self._confirm(identifier, event, plan, approved, best["id"])
                if plan["report"]:
                    self._report(identifier, plan)
        except StopRun as stop:
            state, reason = stop.state, stop.reason
        except Exception as exc:  # the stage methods raise ValueError/RuntimeError/KeyError with user-facing messages
            state, reason = "failed", str(getattr(exc, "detail", exc))
        finally:
            with self.lock:
                self.events.pop(identifier, None)
                self.threads.pop(identifier, None)
                cancelled = identifier in self.cancelled
                self.cancelled.discard(identifier)
            try:
                if state != "completed" and (cancelled or self.workflows._read(identifier)["state"] == "cancelled"):
                    state, reason = "cancelled", "연구 흐름이 취소되었습니다."
                self._log(identifier, state, reason or "계획을 끝까지 실행했습니다. 결과와 추천은 사람이 검토해야 합니다.", state=state, stopped_reason=reason)
            except (KeyError, ValueError, OSError):
                pass
