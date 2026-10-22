"""MCP stdio bridge: protocol behaviour, review gates and real jobs through a local server."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from workbench import mcp_server
from workbench.server import create_app

ROOT = Path(__file__).resolve().parents[1]


class Session:
    """One MCP client over the real `python -m workbench --mcp` stdio process."""

    def __init__(self, env):
        self.process = subprocess.Popen([sys.executable, "-m", "workbench", "--mcp"], cwd=ROOT, env=env, text=True,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.next_id = 0

    def send(self, payload):
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def request(self, method, params=None):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, **({"params": params} if params is not None else {})})
        return json.loads(self.process.stdout.readline())

    def tool(self, tool_name, **arguments):
        result = self.request("tools/call", {"name": tool_name, "arguments": arguments})["result"]
        text = result["content"][0]["text"]
        return result["isError"], text if result["isError"] else json.loads(text)

    def close(self):
        self.process.stdin.close()
        return self.process.wait(timeout=60)


@pytest.fixture
def env(tmp_path):
    values = {**os.environ, "POLYMER_DATA_DIR": str(tmp_path / "jobs"), "POLYMER_PORT": "1"}
    values.pop("POLYMER_LAB_URL", None)
    values.pop("POLYMER_LAB_TOKEN", None)
    return values


def test_protocol_negotiation_listing_and_errors(env):
    session = Session(env)
    try:
        init = session.request("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "pytest"}})["result"]
        assert init["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]
        assert init["serverInfo"]["name"] == "polymer-lab" and "verified=false" in init["instructions"]
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = {tool["name"]: tool for tool in session.request("tools/list")["result"]["tools"]}
        assert {"lab_status", "submit_job", "wait_for_job", "add_measurement", "control_campaign"} <= set(tools)
        assert tools["get_job"]["annotations"]["readOnlyHint"] is True
        assert tools["cancel_job"]["annotations"]["destructiveHint"] is True
        assert tools["submit_job"]["inputSchema"]["properties"]["config"]["properties"]["route"]["enum"][0] == "polymer_cg"
        assert session.request("ping")["result"] == {}
        assert session.request("resources/list")["error"]["code"] == -32601
        assert session.request("tools/call", {"name": "missing", "arguments": {}})["error"]["code"] == -32602
        error, text = session.tool("get_job", job_id="not-a-job")
        assert error and "invalid format" in text
        error, text = session.tool("list_jobs", limit=0)
        assert error and "between" in text
        error, status = session.tool("lab_status")
        assert not error and status["connected"] is False and "start_lab_server" in status["detail"]
        session.process.stdin.write("{not json\n")
        session.process.stdin.flush()
        assert json.loads(session.process.stdout.readline())["error"]["code"] == -32700
    finally:
        assert session.close() == 0


def test_owned_server_runs_job_and_keeps_review_gate(env, tmp_path):
    session = Session(env)
    try:
        session.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest"}})
        error, started = session.tool("start_lab_server")
        assert not error and started["started"] is True
        discovery = tmp_path / "jobs" / ".server.json"
        assert discovery.is_file() and oct(discovery.stat().st_mode & 0o777) == "0o600"
        error, job = session.tool("submit_job", config={"name": "MCP CG fixture", "route": "polymer_cg", "monomers": 6, "chains": 1, "steps": 50, "engine": "numpy"})
        assert not error and job["submitted_via"] == "mcp:pytest"
        error, done = session.tool("wait_for_job", job_id=job["id"], timeout_seconds=120)
        assert not error and done["status"] == "completed" and done["finished"] and done["indexed"]
        assert done["result"]["metrics"]["series"].startswith("51 entries")
        stored = json.loads((tmp_path / "jobs" / job["id"] / "job.json").read_text())
        assert stored["submitted_via"] == "mcp:pytest"
        error, artifact = session.tool("read_artifact", job_id=job["id"], name="result.json", max_chars=100)
        assert not error and artifact["truncated"] and len(artifact["text"]) == 100
        error, text = session.tool("read_artifact", job_id=job["id"], name="restart.lammps")
        assert error and "not a supported text artifact" in text
        error, text = session.tool("submit_job", config={"route": "polymer_cg", "monomers": 1})
        assert error and "HTTP 422" in text and "monomers" in text
        # An agent cannot approve data for training, even when it asks to.
        error, measurement = session.tool("add_measurement", measurement={
            "material": {"repeat_smiles": "[*]CC[*]"}, "property": "tg", "value": 100, "unit": "C", "method": "DSC",
            "conditions": {}, "source": "synthetic MCP test fixture", "origin": "demo", "verified": True})
        assert not error and measurement["verified"] is False and measurement["entry"] == {"via": "mcp", "client": "mcp:pytest"}
        assert measurement["value"] == pytest.approx(373.15)
        error, experiments = session.tool("list_experiments")
        assert not error and experiments["experiments"][0]["features"]["rg_mean"]["unit"] == "sigma"
        error, status = session.tool("lab_status")
        assert status["owned_by_this_session"] and status["research"] == {"experiments": 1, "measurements": 1, "models": 0, "campaigns": 0}
    finally:
        assert session.close() == 0
    deadline = time.monotonic() + 20
    while discovery.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not discovery.exists()


def test_attach_to_desktop_session_with_token(env, tmp_path):
    from workbench.desktop import BackendSupervisor
    backend = BackendSupervisor(tmp_path / "jobs").start()
    try:
        deadline = time.monotonic() + 60
        while not backend.poll_ready() and time.monotonic() < deadline:
            time.sleep(0.2)
        record = json.loads((tmp_path / "jobs" / ".server.json").read_text())
        assert record["origin"] == backend.origin and record["token"] == backend.token
        os.environ["POLYMER_DATA_DIR"] = env["POLYMER_DATA_DIR"]
        try:
            client = mcp_server.LabClient()
            client.connect()
            assert client.origin == backend.origin and client.source.startswith("desktop session")
            assert client.request("GET", "/api/health")["name"] == "Polymer Lab"
        finally:
            os.environ.pop("POLYMER_DATA_DIR")
    finally:
        backend.stop()


def test_remote_origins_are_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYMER_LAB_URL", "http://example.com:8765")
    monkeypatch.setenv("POLYMER_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("POLYMER_PORT", "1")
    with pytest.raises(mcp_server.ToolError, match="refused non-loopback"):
        mcp_server.LabClient().connect()
    assert mcp_server._loopback("http://127.0.0.1:8765") and mcp_server._loopback("http://[::1]:9")
    assert not mcp_server._loopback("http://user:pw@127.0.0.1:1") and not mcp_server._loopback("https://127.0.0.1:1")


def test_one_server_per_data_folder(tmp_path):
    with TestClient(create_app(tmp_path / "jobs")):
        with pytest.raises(RuntimeError, match="이미 이 데이터 폴더"):
            with TestClient(create_app(tmp_path / "jobs")):
                pass
    with TestClient(create_app(tmp_path / "jobs")) as client:
        assert client.get("/api/health").json()["ok"] is True
