"""Deterministic real-child fixture for readiness lifecycle contracts."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STOP = threading.Event()


def _write_status(path: Path, **values: object) -> None:
    current: dict[str, object] = {}
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
    current.update(values)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _wait_for_release(path: Path | None) -> None:
    if path is None:
        STOP.wait()
        return
    while not STOP.is_set() and not path.exists():
        STOP.wait(0.01)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("hold", "immediate-log", "tcp-before-log", "http-before-log"),
        required=True,
    )
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--marker", default="READY-MARKER")
    parser.add_argument("--exit-after-marker", action="store_true")
    parser.add_argument("--grandchild", action="store_true")
    parser.add_argument("--escaped-grandchild", action="store_true")
    args = parser.parse_args()

    def stop(_signum: int, _frame: object) -> None:
        STOP.set()

    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, stop)

    listener: socket.socket | None = None
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    grandchild: subprocess.Popen[bytes] | None = None

    try:
        if args.mode == "tcp-before-log":
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
        elif args.mode == "http-before-log":
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            port = server.server_address[1]
            server_thread = threading.Thread(target=server.serve_forever)
            server_thread.start()
        else:
            port = None

        if args.grandchild or args.escaped_grandchild:
            spawn_options: dict[str, object] = {}
            if args.escaped_grandchild:
                if os.name == "nt":
                    spawn_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    spawn_options["start_new_session"] = True
            grandchild = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(300)"],
                **spawn_options,
            )

        if args.mode == "immediate-log":
            print(args.marker, flush=True)

        _write_status(
            args.status,
            pid=os.getpid(),
            port=port,
            grandchild_pid=grandchild.pid if grandchild else None,
            marker_emitted=args.mode == "immediate-log",
        )

        if args.mode in ("tcp-before-log", "http-before-log"):
            _wait_for_release(args.release)
            if not STOP.is_set():
                print(args.marker, flush=True)
                _write_status(args.status, marker_emitted=True)

        if args.exit_after_marker:
            return 0
        STOP.wait()
        return 0
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        if listener is not None:
            listener.close()
        if (
            grandchild is not None
            and not args.escaped_grandchild
            and grandchild.poll() is None
        ):
            grandchild.terminate()
            try:
                grandchild.wait(timeout=5)
            except subprocess.TimeoutExpired:
                grandchild.kill()
                grandchild.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
