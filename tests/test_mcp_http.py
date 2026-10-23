"""MCP over Streamable HTTP: secret path, origin check, JSON-RPC replies and a real tool call."""
import json
import os
import stat
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from workbench import mcp_http


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYMER_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("POLYMER_PORT", "1")
    for name in ("POLYMER_LAB_URL", "POLYMER_LAB_TOKEN", "POLYMER_MCP_HTTP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    token = mcp_http.load_token()
    holder = {}
    mcp_http.serve(port=0, token=token, ready=lambda httpd: holder.setdefault("httpd", httpd))
    httpd = holder["httpd"]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", token
    httpd.shutdown()
    httpd.server_close()


def call(url, payload=None, method="POST", headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(url, data=data, method=method,
                      headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})})
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def test_token_is_private_and_stable(endpoint, tmp_path):
    _, token = endpoint
    path = mcp_http.token_path()
    assert path.parent == tmp_path / "jobs" and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert mcp_http.load_token() == token and len(token) >= 40
    assert mcp_http.load_token(renew=True) != token


def test_wrong_path_and_foreign_origin_are_refused(endpoint):
    base, token = endpoint
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}
    assert call(f"{base}/mcp/{token[:-1]}x", init)[0] == 404
    assert call(f"{base}/mcp", init)[0] == 404
    assert call(f"{base}/mcp/{token}", init, headers={"Origin": "https://evil.example"})[0] == 403
    assert call(f"{base}/mcp/{token}", init, headers={"Origin": "https://claude.ai"})[0] == 200
    assert call(f"{base}/mcp/{token}", method="GET")[0] == 405


def test_initialize_list_notification_and_tool_call(endpoint):
    base, token = endpoint
    url = f"{base}/mcp/{token}"
    status, init = call(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-ai"}}})
    assert status == 200 and init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "polymer-lab"
    assert call(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    status, listed = call(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {"submit_job", "lab_status", "add_measurement"} <= names
    # A batch answers every request in it; lab_status reports the missing lab instead of failing.
    status, batch = call(url, [{"jsonrpc": "2.0", "id": 3, "method": "ping"},
                               {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "lab_status", "arguments": {}}}])
    assert status == 200 and [item["id"] for item in batch] == [3, 4]
    report = json.loads(batch[1]["result"]["content"][0]["text"])
    assert report["connected"] is False
    assert call(url, {"jsonrpc": "2.0", "id": 5, "method": "nope"})[1]["error"]["code"] == -32601
    assert call(url, {"jsonrpc": "1.0", "id": 6})[1]["error"]["code"] == -32600


def test_parse_errors_answer_in_json_rpc(endpoint):
    base, token = endpoint
    request = Request(f"{base}/mcp/{token}", data=b"{not json", method="POST", headers={"Content-Type": "application/json"})
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=10)
    assert error.value.code == 400 and json.loads(error.value.read())["error"]["code"] == -32700
