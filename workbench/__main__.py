"""Application entry point shared by source installs and the frozen executable."""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import runpy
import signal
import socket
import sys
from . import __version__


def _run_script(path: Path, arguments: list[str]) -> int:
    sys.argv = [str(path), *arguments]
    runpy.run_path(str(path), run_name="__main__")
    return 0


def run_server(arguments: list[str]) -> int:
    import uvicorn
    from .server import create_app

    parser = argparse.ArgumentParser(prog="PolymerLab --server")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--data-dir", type=Path)
    options = parser.parse_args(arguments)
    if not 0 <= options.port <= 65535:
        parser.error("port must be between 0 and 65535")

    # On Linux, an unexpected desktop exit still asks the ASGI service to close
    # its jobs. Set this before binding, then recheck to close the parent race.
    parent_pid = os.environ.get("POLYMER_PARENT_PID")
    if parent_pid and sys.platform.startswith("linux"):
        import ctypes
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)
        if os.getppid() != int(parent_pid):
            return 0

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", options.port))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    from .paths import default_jobs_root
    # Lets local MCP agents attach to this session; owner-only, removed on shutdown.
    discovery = (options.data_dir or default_jobs_root()) / ".server.json"

    class DesktopServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started and options.ready_file:
                destination = options.ready_file
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(".tmp")
                with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as stream:
                    json.dump({"port": port, "pid": os.getpid()}, stream)
                temporary.replace(destination)
            if self.started:
                discovery.parent.mkdir(parents=True, exist_ok=True)
                temporary = discovery.with_suffix(".tmp")
                with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as stream:
                    json.dump({"origin": f"http://127.0.0.1:{port}", "pid": os.getpid(), "version": __version__,
                               "token": os.environ.get("POLYMER_DESKTOP_TOKEN")}, stream)
                temporary.replace(discovery)

    # Uvicorn replays a captured SIGTERM after graceful ASGI shutdown. Unwind
    # Python cleanup instead of letting the default handler bypass finally.
    def graceful_termination(signum, frame):
        raise SystemExit(0)

    previous_term_handler = signal.signal(signal.SIGTERM, graceful_termination)
    try:
        config = uvicorn.Config(create_app(data_root=options.data_dir), host="127.0.0.1", port=port,
                                access_log=False, log_level="info", timeout_graceful_shutdown=12)
        DesktopServer(config).run(sockets=[listener])
    finally:
        listener.close()
        if options.ready_file:
            options.ready_file.unlink(missing_ok=True)
        try:
            if json.loads(discovery.read_text()).get("pid") == os.getpid():
                discovery.unlink()
        except (OSError, ValueError):
            pass
        signal.signal(signal.SIGTERM, previous_term_handler)
    return 0


def main(arguments=None) -> int:
    # PyInstaller workers (including engine libraries that spawn) must not open
    # another desktop window when multiprocessing imports the executable.
    multiprocessing.freeze_support()
    args = list(sys.argv[1:] if arguments is None else arguments)
    if args and args[0] == "--compute-worker":
        from .compute_worker import main as run_compute
        return run_compute(args[1])
    if args and args[0] == "--server":
        return run_server(args[1:])
    if args and args[0] == "--mcp":
        from .mcp_server import main as run_mcp
        return run_mcp(args[1:])
    if args and args[0] == "--mcp-http":
        from .mcp_http import main as run_mcp_http
        return run_mcp_http(args[1:])
    if args and args[0] == "--worker":
        from .paths import resource_root
        return _run_script(resource_root() / "scripts" / "engine_worker.py", args[1:])
    if args and args[0] in {"--engine-script", "--run-script"}:
        if len(args) < 2:
            raise SystemExit("--engine-script requires a Python script path")
        return _run_script(Path(args[1]).resolve(), args[2:])
    if args and args[0] in {"--polyply", "--martinize"}:
        from .paths import resource_root
        tool = "polyply" if args[0] == "--polyply" else "martinize2"
        script = resource_root() / "scripts" / (tool + "_cli.py")
        if not script.is_file():
            script = resource_root() / ".venv" / "bin" / tool
        return _run_script(script, args[1:])
    parser = argparse.ArgumentParser(prog="PolymerLab", description="Polymer Lab 고분자·단백질 연구 데스크톱 프로그램")
    parser.add_argument("--data-dir", type=Path, help="별도의 연구 작업 폴더를 사용합니다")
    parser.add_argument("--version", action="version", version="Polymer Lab " + __version__)
    options = parser.parse_args(args)
    from .desktop import run_desktop
    return run_desktop(options.data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
