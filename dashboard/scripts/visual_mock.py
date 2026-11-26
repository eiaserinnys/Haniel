"""Mock Haniel node for the dashboard visual check.

Serves a built dashboard/dist plus the login page on an ephemeral port the way the production server does
(/assets files, /auth/login, index.html for every other GET) and fills /api/* and /ws with production-shaped fake data. Scenes change the data through
`Mock.scene`; nothing here talks to a real Haniel node.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

LOGIN_HTML = Path(__file__).resolve().parents[2] / "src/haniel/dashboard/static/auth/login.html"

LONG_SERVICE = "soulstream-event-ingress-dispatcher-canary-eu-west-replica-02"
LONG_BRANCH = "release/2026-11-hotfix-event-ingress-dispatcher"
LONG_RUN = (
    "python -m ingress.dispatcher --port 3121 --outbox postgres://127.0.0.1:5432/soulstream "
    "--retry-backoff 4,8,16,32 --metrics-port 9121 --log-level info"
)
DIFF_STAT = (
    " src/haniel/core/runner.py              | 42 +++++++++++++++++++++++---------\n"
    " src/haniel/core/deploy_coordinator.py  | 18 ++++++++++++----\n"
    " 2 files changed, 47 insertions(+), 13 deletions(-)"
)

LOG_LINES = [
    "2026-11-26 12:40:58 INFO  dispatcher starting on 127.0.0.1:3121",
    "2026-11-26 12:40:59 INFO  connected to postgres (pool=4)",
    "2026-11-26 12:41:01 INFO  draining outbox: 18 pending deliveries",
    "2026-11-26 12:41:02 ERROR delivery loop crashed: ConnectionResetError: [Errno 104] Connection reset by peer",
    "2026-11-26 12:41:02 ERROR   at ingress/dispatcher.py:212 in _drain_outbox",
    "2026-11-26 12:41:02 INFO  exit code 1, restart 3 of 5 in 4s",
]


def _svc(state, up, run, repo=None, after=(), cwd=None, restarts=0, fails=0, enabled=True):
    return {
        "state": state,
        "uptime": up,
        "restart_count": restarts,
        "consecutive_failures": fails,
        "config": {"run": run, "cwd": cwd, "repo": repo, "after": list(after), "ready": None, "enabled": enabled},
    }


def _repo(branch, head, commits=None, stat="", pulling=False, fetch_error=None, url="https://github.com/eiaserinnys/x"):
    return {
        "url": url,
        "branch": branch,
        "path": "./repos/x",
        "last_head": head,
        "pending_changes": {"commits": commits, "stat": stat} if commits else None,
        "fetch_error": fetch_error,
        "pulling": pulling,
    }


def base_status() -> dict:
    last_poll = (datetime.now(timezone.utc) - timedelta(seconds=37)).isoformat()
    services = {
        "soulstream-server": _svc("ready", 8040, "node dist/main.js --port 3105", "soulstream", cwd="./soulstream/server"),
        "soulstream-orch": _svc("running", 7980, "node dist/orch.js --port 3110", "soulstream", ["soulstream-server"], "./soulstream/orch", 1),
        "soulstream-event-ingress-dispatcher": _svc(
            "crashed", 0, LONG_RUN, "soulstream", ["soulstream-server"], "./soulstream-server-src", 7, 3
        ),
        LONG_SERVICE: _svc("stopped", 0, "python -m ingress.dispatcher --replica 2", "soulstream", ["soulstream-server"], "./soulstream"),
        "soul-dashboard": _svc("starting", 12, "pnpm --dir dashboard preview --port 5173", "soulstream", ["soulstream-orch"], "./soulstream", 2),
        "serendipity-api": _svc("running", 529200, "node packages/api/dist/index.js", "serendipity", cwd="./serendipity"),
        "serendipity-web": _svc("stopped", 0, "node packages/web/server.js", "serendipity", ["serendipity-api"], "./serendipity"),
        "soulstream-builder": _svc("circuit_open", 0, "python -m builder.watch", "soulstream-server-src", [], "./soulstream-server-src", 5, 5),
        "sentinel": _svc("ready", 360000, "python -m sentinel.main", None, [], "./sentinel"),
        "mtg-edh-recommend": _svc("stopped", 0, "python -m edh.serve", None, [], "./edh", enabled=False),
    }
    commits_haniel = [
        "e9416d7 Restart services stopped by a deployment when the new config still declares them (#104)",
        "d143f0d Adopt declared services across self-update (#103)",
    ]
    repos = {
        "haniel": _repo("main", "e9416d71", commits_haniel, DIFF_STAT),
        "soulstream": _repo(
            "main",
            "a1b2c3d4",
            ["a1b2c3d Fix retry backoff for intervene delivery (#2091)", "9c0d1e2 Keep card brief when the assignee changes (#2090)", "77ab012 Show PAS queue depth in the folder header (#2088)", "5e6f7a8 Bump claude-agent-sdk to 0.9.4 (#2087)"],
            " server/src/intervene/delivery.ts | 96 ++++++++-----\n orch/src/routes/cards.ts         | 22 ++--\n 7 files changed, 248 insertions(+), 71 deletions(-)",
        ),
        "serendipity": _repo("main", "7f6be6c6"),
        "soulstream-server-src": _repo(
            LONG_BRANCH,
            "4f2a91c0",
            ["4f2a91c Reconnect the outbox drain after a reset by peer instead of exiting"],
            " ingress/dispatcher.py | 44 ++++++++++++++++++++----\n 3 files changed, 58 insertions(+), 4 deletions(-)",
            pulling=True,
        ),
        "eb_lore": _repo(
            "main",
            "3228dc8a",
            ["3228dc8 Add Selaphiel speech guide notes", "31f0a2b Fix Arielle portrait crop"],
            " content/characters/ns.yaml | 14 ++++---\n 2 files changed, 11 insertions(+), 3 deletions(-)",
        ),
        "notes": _repo("main", None, fetch_error="git fetch timed out after 30s (github.com:443)"),
    }
    graph = {n: {"dependencies": s["config"]["after"]} for n, s in services.items()}
    return {
        "services": services,
        "repos": repos,
        "self_update": {"repo": "haniel", "pending": True, "auto_update": False, "last_result": None},
        "dependency_graph": graph,
        "poll_count": 1284,
        "poll_interval": 60,
        "last_poll": last_poll,
        "pending_restarts": ["soul-dashboard"],
    }


def empty_status() -> dict:
    s = base_status()
    s["services"], s["repos"], s["dependency_graph"], s["pending_restarts"] = {}, {}, {}, []
    s["self_update"] = None
    return s


class Mock:
    """Per-scene knobs read by the route handlers."""

    def __init__(self) -> None:
        self.status = base_status()
        self.ws_mode = "ok"  # ok | hold (open, never send init) | drop | init-then-drop (the last two fake the socket in the page)
        self.status_delay_ms = 0
        self.overrides: dict[tuple[str, str], tuple[int, str]] = {}
        self.calls: list[tuple[str, str]] = []

    def reset(self) -> None:
        self.__init__()

    def handle_api(self, route) -> None:
        req = route.request
        path = urlparse(req.url).path
        key = (req.method, path)
        self.calls.append(key)
        if key in self.overrides:
            code, body = self.overrides[key]
            route.fulfill(status=code, body=body, content_type="text/plain")
            return
        if path == "/api/status":
            route.fulfill(status=200, body=json.dumps(self.status), content_type="application/json")
        elif path.endswith("/logs"):
            route.fulfill(status=200, body=json.dumps({"lines": LOG_LINES}), content_type="application/json")
        elif path == "/api/config/repos" and req.method == "GET":
            route.fulfill(status=200, body=json.dumps({"eb_lore": {"url": "https://github.com/eiaserinnys/eb_lore", "branch": "main", "path": "./repos/eb_lore"}}), content_type="application/json")
        else:
            route.fulfill(status=200, body=json.dumps({"ok": True}), content_type="application/json")

    def handle_ws(self, ws) -> None:
        if self.ws_mode == "hold":
            return
        ws.send(json.dumps({"type": "init", "status": deepcopy(self.status)}))

    def ws_stub(self) -> str | None:
        """Page script replacing WebSocket for the modes that close the socket.

        Closing from a Playwright route handler stalls the sync driver, so these modes fake the socket in the page.
        """
        if self.ws_mode not in ("drop", "init-then-drop"):
            return None
        init = json.dumps(json.dumps({"type": "init", "status": self.status}))
        send_init = f"this.onmessage && this.onmessage({{ data: {init} }});" if self.ws_mode == "init-then-drop" else ""
        open_ = "this.onopen && this.onopen({});" if self.ws_mode == "init-then-drop" else ""
        return (
            "window.WebSocket = class { constructor() { setTimeout(() => { "
            f"{open_} {send_init} setTimeout(() => {{ this.onerror && this.onerror({{}}); this.onclose && this.onclose({{}}); }}, 80); }}, 30); }} "
            "close() {} send() {} };"
        )


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, dist: Path, **kwargs):
        self._dist = dist
        super().__init__(*args, directory=str(dist), **kwargs)

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:
        """Same routes as src/haniel/dashboard/static.py: /assets files, the login page, index.html for any other GET."""
        path = urlparse(self.path).path
        if path == "/auth/login":
            self._send_file(LOGIN_HTML)
        elif path.startswith("/assets/"):
            if (self._dist / path.lstrip("/")).is_file():
                super().do_GET()
            else:
                self.send_error(404)
        else:
            self._send_file(self._dist / "index.html")

    def _send_file(self, file: Path) -> None:
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(dist: Path) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, dist=dist))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
