"""Shared admission and isolated numerical workers; no scientific work runs under the state lock."""
from contextlib import contextmanager
from collections import deque
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .paths import frozen, resource_root, runtime_environment


class ExecutionCancelled(RuntimeError):
    pass


class ExecutionBusy(RuntimeError):
    """The lane stayed occupied for the caller's whole admission window."""


# Hour-long simulations, minute-scale analyses and sub-second statistics never wait on each other.
LANES = {"simulation": "batch", "doe_design": "quick", "doe_analyze": "quick"}
LANE_LABELS = {"batch": "MD 계산", "analysis": "구조·접촉 분석", "quick": "통계 계산"}
REQUEST_WAIT = 30.0


def lane_of(kind):
    return LANES.get(kind, "analysis")


class _FallbackQueue:
    """Keeps legacy non-native jobs usable while the optional extension is missing."""
    def __init__(self):
        self.waiting, self.active = deque(), None
    def enqueue(self, identifier):
        if len(self.waiting) >= 256:
            raise ValueError("실행 대기열이 가득 찼습니다.")
        self.waiting.append(identifier)
    def acquire(self, identifier):
        if self.active is None and self.waiting and self.waiting[0] == identifier:
            self.active = self.waiting.popleft()
            return True
        return False
    def release(self, identifier):
        if self.active == identifier:
            self.active = None
        if identifier in self.waiting:
            self.waiting.remove(identifier)


class ExecutionManager:
    def __init__(self):
        try:
            from workbench_native import ExecutionQueue
            factory, self.engine = ExecutionQueue, "rust"
        except ImportError:
            factory, self.engine = _FallbackQueue, "python_fallback"
        self.queues = {lane: factory() for lane in LANE_LABELS}
        self.condition = threading.Condition()
        self.records = {}
        self.closed = False
        self.request_wait = REQUEST_WAIT

    @contextmanager
    def slot(self, kind, owner=None, cancelled=None, wait=None):
        """FIFO admission within the kind's lane; wait=None waits until admitted or cancelled."""
        identifier = uuid.uuid4().hex
        lane = lane_of(kind)
        queue = self.queues[lane]
        with self.condition:
            if self.closed:
                raise ExecutionCancelled("실행 관리자가 종료 중입니다.")
            if sum(r["status"] in {"running", "queued"} for r in self.records.values()) >= 16:
                raise ExecutionBusy("실행 대기열이 가득 찼습니다. 완료된 뒤 다시 요청하세요.")
            queue.enqueue(identifier)
            record = {"id": identifier, "kind": kind, "lane": lane, "owner": owner, "status": "queued", "cancel_requested": False}
            self.records[identifier] = record
        deadline = None if wait is None else time.monotonic() + wait
        try:
            with self.condition:
                while True:
                    if self.closed or record["cancel_requested"] or (cancelled and cancelled()):
                        raise ExecutionCancelled("계산이 취소되었습니다.")
                    if queue.acquire(identifier):
                        record["status"] = "running"
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        raise ExecutionBusy(f"{LANE_LABELS[lane]}이 진행 중이라 {wait:g}초 안에 시작하지 못했습니다. 끝난 뒤 다시 실행하세요.")
                    self.condition.wait(0.1)
            yield record
            record["status"] = "completed"
        except ExecutionCancelled:
            record["status"] = "cancelled"
            raise
        except ExecutionBusy:
            record["status"] = "busy"
            raise
        except BaseException:
            record["status"] = "failed"
            raise
        finally:
            with self.condition:
                queue.release(identifier)
                self.condition.notify_all()
                completed = [key for key, value in self.records.items() if value["status"] not in {"queued", "running"}]
                for key in completed[:-100]:
                    self.records.pop(key, None)

    def cancel(self, identifier=None, owner=None):
        with self.condition:
            found = False
            for record in self.records.values():
                if (identifier == record["id"] or owner is not None and owner == record["owner"]) and record["status"] in {"queued", "running"}:
                    record["cancel_requested"] = True
                    found = True
            self.condition.notify_all()
            return found

    def status(self):
        with self.condition:
            return {"engine": self.engine, "limit": len(self.queues),
                    "lanes": {lane: {"label": label, "limit": 1} for lane, label in LANE_LABELS.items()},
                    "tasks": [dict(r) for r in self.records.values()]}

    def _threads(self):
        cores = os.cpu_count() or 1
        with self.condition:
            simulating = any(r["lane"] == "batch" and r["status"] == "running" for r in self.records.values())
        # A running simulation keeps its threads (jobs.py uses up to 4); analyses still get one core.
        return str(max(1, min(4, cores - (min(4, cores) if simulating else 0))))

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def compute(self, operation, payload, owner=None, cancelled=None, wait=None):
        with self.slot(operation, owner, cancelled, wait) as ticket:
            with tempfile.TemporaryDirectory(prefix="polymer-compute-") as folder:
                root = Path(folder)
                (root / "input.json").write_text(json.dumps({"operation": operation, "payload": payload}, allow_nan=False))
                prefix = [sys.executable] if frozen() else [sys.executable, "-m", "workbench"]
                env = runtime_environment()
                env["POLYMER_WORKER_PARENT_PID"] = str(os.getpid())
                env["RAYON_NUM_THREADS"] = self._threads()
                with (root / "worker.log").open("wb") as log:
                    proc = subprocess.Popen(prefix + ["--compute-worker", str(root)], cwd=resource_root(), env=env,
                                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    ticket["pid"] = proc.pid
                    deadline = time.monotonic() + 3600
                    try:
                        while proc.poll() is None:
                            if self.closed or ticket["cancel_requested"] or (cancelled and cancelled()):
                                raise ExecutionCancelled("계산이 취소되었습니다.")
                            if time.monotonic() > deadline:
                                raise RuntimeError("분석 실행 한도(1시간)를 초과했습니다.")
                            time.sleep(0.05)
                        if self.closed or ticket["cancel_requested"] or (cancelled and cancelled()):
                            raise ExecutionCancelled("계산이 취소되었습니다.")
                        output = root / "output.json"
                        if proc.returncode or not output.is_file():
                            raise RuntimeError(f"계산 프로세스가 종료되었습니다(코드 {proc.returncode}). 결과는 저장하지 않았습니다.")
                        message = json.loads(output.read_text())
                        if "error" in message:
                            raise ValueError(message["error"])
                        result = message["result"]
                        result["execution"] = {"id": ticket["id"], "scheduler": self.engine, "isolated_process": True}
                        return result
                    finally:
                        # Do not release the lease until descendants have been stopped too.
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait()
