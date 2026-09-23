"""Standalone vFleet launcher used by native desktop bundles.

The launcher owns the loopback listener so the Tauri shell never needs a fixed
port.  It also exposes a deliberately tiny stdin/stdout control protocol used
only by the parent process:

``VFLEET_READY <url>``
    The ASGI lifespan has started and the UI can be loaded.
``VFLEET_STATUS <count>``
    The number of queued, retrying, or running jobs changed.
``VFLEET_BUSY <count>``
    A graceful shutdown request was refused because work is still open.

No credentials, inventory data, or job payloads cross this protocol.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from typing import TextIO

import uvicorn


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the packaged vFleet desktop backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=0, type=int)
    parser.add_argument("--open-browser", action="store_true")
    return parser


def _listener(host: str, port: int) -> socket.socket:
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("The desktop backend may only bind to loopback")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(2048)
    return listener


def _open_job_count(app) -> int:
    store = getattr(app.state, "store", None)
    return len(store.open_jobs()) if store is not None else 0


def _request_shutdown(app, server: uvicorn.Server, *, force: bool = False) -> int:
    """Request shutdown and return the number of jobs that kept it open."""

    count = _open_job_count(app)
    if force or count == 0:
        server.should_exit = True
        return 0
    return count


def _control_loop(app, server: uvicorn.Server, stream: TextIO = sys.stdin) -> None:
    for raw in stream:
        command = raw.strip().upper()
        if command == "STATUS":
            print(f"VFLEET_STATUS {_open_job_count(app)}", flush=True)
        elif command == "SHUTDOWN":
            count = _request_shutdown(app, server)
            if count:
                print(f"VFLEET_BUSY {count}", flush=True)
            else:
                print("VFLEET_STOPPING", flush=True)
                return
        elif command == "FORCE_SHUTDOWN":
            _request_shutdown(app, server, force=True)
            print("VFLEET_STOPPING", flush=True)
            return


def _status_loop(app, server: uvicorn.Server) -> None:
    previous: int | None = None
    while not server.should_exit:
        count = _open_job_count(app)
        if count != previous:
            print(f"VFLEET_STATUS {count}", flush=True)
            previous = count
        time.sleep(0.5)


def main() -> None:
    args = _parser().parse_args()
    listener = _listener(args.host, args.port)
    port = int(listener.getsockname()[1])

    from .main import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info", access_log=False)
    )

    def announce() -> None:
        while not server.started and not server.should_exit:
            time.sleep(0.05)
        if server.started:
            url = f"http://127.0.0.1:{port}"
            print(f"VFLEET_READY {url}", flush=True)
            if args.open_browser:
                webbrowser.open(url)
            threading.Thread(
                target=_status_loop,
                args=(app, server),
                name="vfleet-status",
                daemon=True,
            ).start()

    threading.Thread(target=announce, name="vfleet-ready", daemon=True).start()
    threading.Thread(
        target=_control_loop,
        args=(app, server),
        name="vfleet-control",
        daemon=True,
    ).start()
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
