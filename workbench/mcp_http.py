"""Streamable HTTP transport for the Polymer Lab MCP server (remote MCP clients such as a claude.ai connector).

The listener binds 127.0.0.1 only; a tunnel (Tailscale Funnel, cloudflared) publishes it over HTTPS. The
endpoint path carries a random secret, POST /mcp/<token>, because a custom connector reaches the server with
nothing but its URL. Replies are plain application/json; the server never opens an SSE stream. The tools and
their review gates are the same as over stdio (mcp_server.PolymerLabTools).
"""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

from .mcp_server import LabClient, PolymerLabTools, Server

DEFAULT_PORT = 8766
MAX_BODY = 4 * 1024 * 1024
# Browser pages on other sites must not drive the server (DNS rebinding); connector backends send no Origin.
ALLOWED_ORIGINS = {"https://claude.ai", "https://claude.com"}


def token_path() -> Path:
    from .paths import default_jobs_root
    return default_jobs_root() / ".mcp_http_token"


def load_token(renew=False) -> str:
    """The endpoint secret: POLYMER_MCP_HTTP_TOKEN, else an owner-only file created on first use."""
    if os.environ.get("POLYMER_MCP_HTTP_TOKEN"):
        return os.environ["POLYMER_MCP_HTTP_TOKEN"]
    path = token_path()
    if not renew:
        try:
            token = path.read_text(encoding="utf-8").strip()
            if len(token) >= 32:
                return token
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    temporary = path.with_suffix(".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as stream:
        stream.write(token + "\n")
    temporary.replace(path)
    return token


def make_handler(server: Server, token: str):
    endpoint = "/mcp/" + token

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "PolymerLabMCP"

        def _ours(self):
            return hmac.compare_digest(urlsplit(self.path).path.rstrip("/").encode(), endpoint.encode())

        def _reply(self, status, body=None, headers=()):
            data = b"" if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _refuse(self):
            if not self._ours():
                self._reply(404, {"error": "not found"})
                return True
            origin = self.headers.get("Origin")
            if origin and origin.rstrip("/") not in ALLOWED_ORIGINS:
                self._reply(403, {"error": "origin not allowed"})
                return True
            return False

        def do_POST(self):
            if self._refuse():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                self._reply(413 if length > MAX_BODY else 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
                return
            try:
                message = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                return
            batch = isinstance(message, list)
            # Client responses (no method) and notifications get no reply.
            replies = [reply for item in (message if batch else [message])
                       if not (isinstance(item, dict) and "method" not in item and ("result" in item or "error" in item))
                       for reply in [server.handle(item)] if reply is not None]
            if not replies:
                self._reply(202)
            else:
                self._reply(200, replies if batch else replies[0])

        def do_GET(self):
            if not self._refuse():
                # No server-initiated stream: every reply travels on its POST.
                self._reply(405, {"error": "use POST"}, headers=[("Allow", "POST")])

        def do_DELETE(self):
            if not self._refuse():
                self._reply(405, {"error": "sessions are not tracked"}, headers=[("Allow", "POST")])

        def log_message(self, format, *args):
            # Never write the secret path to logs.
            print("mcp-http %s %s" % (self.command, "ok" if self._ours() else "rejected"), file=sys.stderr)

    return Handler


def serve(port=DEFAULT_PORT, token=None, ready=None):
    """Run the HTTP MCP endpoint on 127.0.0.1:port until interrupted. Returns the HTTP server when `ready` is given."""
    client = LabClient()
    server = Server(PolymerLabTools(client), stdout=sys.stderr)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(server, token or load_token()))
    httpd.daemon_threads = True
    if ready is not None:
        ready(httpd)
        return httpd
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        client.close()
    return None


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(prog="PolymerLab --mcp-http",
                                     description="Polymer Lab MCP over Streamable HTTP on 127.0.0.1, for a tunnel and a remote MCP client")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--public-url", help="the tunnel's HTTPS address; prints the full connector URL")
    parser.add_argument("--new-token", action="store_true", help="replace the endpoint secret (old connector URLs stop working)")
    options = parser.parse_args(arguments or [])
    if not 0 < options.port <= 65535:
        parser.error("port must be between 1 and 65535")
    token = load_token(renew=options.new_token)
    print(f"Polymer Lab MCP (Streamable HTTP) on http://127.0.0.1:{options.port}/mcp/<secret>", file=sys.stderr)
    if options.public_url:
        print("Connector URL (keep it private; it is the password):", file=sys.stderr)
        print(f"  {options.public_url.rstrip('/')}/mcp/{token}", file=sys.stderr)
    else:
        where = "POLYMER_MCP_HTTP_TOKEN" if os.environ.get("POLYMER_MCP_HTTP_TOKEN") else token_path()
        print(f"Secret path: /mcp/{token}  (from {where}; add it after your tunnel's https address)", file=sys.stderr)
    serve(options.port, token)
    return 0
