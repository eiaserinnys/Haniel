"""Static server, fake orchestrator API, and browser session for visual_check.py."""

import json
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import fixtures

AUDIT_JS = (Path(__file__).parent / "audit.js").read_text(encoding="utf-8")


class DistServer:
    """Serve a built dist under /dashboard/ with the SPA fallback the hub uses."""

    def __init__(self, dist):
        root = Path(dist).resolve()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlparse(self.path).path
                if not path.startswith("/dashboard"):
                    self.send_error(404)
                    return
                rel = path[len("/dashboard"):].lstrip("/")
                target = (root / rel).resolve()
                if not rel or not target.is_file() or root not in target.parents:
                    target = root / "index.html"
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/dashboard/"

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeApi:
    """Per-endpoint modes: ok, empty, error, hold. Records mutating calls."""

    def __init__(self):
        self.mode = {"pending": "ok", "nodes": "ok", "history": "ok"}
        self.long = False
        self.returned = False
        self.approve_error = None
        self.held = []
        self.calls = []

    def reset(self):
        self.mode = {"pending": "ok", "nodes": "ok", "history": "ok"}
        self.long = False
        # the deploying self-update came back as a pending request with last_failure
        self.returned = False
        # (status, body) the next approve answers with; None approves
        self.approve_error = None
        self.held = []
        self.calls = []

    def _body(self, ep, query):
        if ep == "pending":
            return {"deploys": [] if self.mode[ep] == "empty" else fixtures.pending(self.long, self.returned)}
        if ep == "nodes":
            return {"nodes": [] if self.mode[ep] == "empty" else fixtures.nodes(self.long)}
        sup = "include_superseded=1" in query
        return {"deploys": [] if self.mode[ep] == "empty" else fixtures.history(sup, self.long)}

    def release(self):
        held, self.held = self.held, []
        for route, ep, query in held:
            route.fulfill(json=self._body(ep, query))

    def handle(self, route):
        req = route.request
        url = urlparse(req.url)
        name = url.path.rsplit("/", 1)[-1]
        if req.method == "POST":
            self.calls.append((name, req.post_data))
            if name == "service-command":
                route.fulfill(json={"command_id": "cmd-1", "status": "sent"})
            elif name == "approve-all":
                route.fulfill(json={"approved": [], "failed": []})
            elif name == "approve" and self.approve_error:
                status, body = self.approve_error
                route.fulfill(status=status, content_type="application/json", body=body)
            else:
                route.fulfill(json={"deploy_id": "x", "status": "ok"})
            return
        mode = self.mode.get(name, "ok")
        if mode == "error":
            route.fulfill(status=502, body="Bad Gateway")
        elif mode == "hold":
            self.held.append((route, name, url.query))
        else:
            route.fulfill(json=self._body(name, url.query))


class Session:
    """One page in a shared context per theme, width, and socket mode, with the fake API attached."""

    contexts = {}

    def __init__(self, browser, base_url, api, theme, width, height=900, ws_closed=False, ready=".page-title"):
        self.api, self.theme, self.width, self.base_url = api, theme, width, base_url
        self.height = height
        self.ready = ready
        self.external = []
        self.ws = None
        key = (theme, width, ws_closed)
        if key not in Session.contexts:
            ctx = browser.new_context(
                viewport={"width": width, "height": height},
                color_scheme="dark" if theme == "dark" else "light",
                reduced_motion="no-preference",
            )
            if theme in ("light", "dark"):
                ctx.add_init_script(f"localStorage.setItem('haniel-theme', '{theme}')")
            ctx.add_init_script(
                "document.addEventListener('DOMContentLoaded', () => { window.__themeAtDcl = document.documentElement.getAttribute('data-theme'); })"
            )
            Session.contexts[key] = ctx
        self.ctx = Session.contexts[key]
        self.page = self.ctx.new_page()
        self.page.set_viewport_size({"width": width, "height": height})
        self.page.on("request", self._on_request)
        self.page.route(re.compile(r".*/api/orch/.*"), api.handle)
        if not ws_closed:
            # a refused socket (the static server answers the upgrade with 404) is the real failure path
            self.page.route_web_socket(re.compile(r".*/ws/dashboard.*"), self._on_ws)

    def _on_ws(self, ws):
        self.ws = ws

    def push(self, event):
        self.ws.send(json.dumps(event))

    def _on_request(self, req):
        host = urlparse(req.url).hostname
        if host not in ("127.0.0.1", "localhost"):
            self.external.append(req.url)

    def open(self, wait_content=True):
        self.page.goto(self.base_url)
        self.page.wait_for_selector(self.ready)
        if wait_content:
            self.settle()

    def settle(self, ms=350):
        self.page.evaluate("document.fonts.ready.then(() => true)")
        self.page.wait_for_timeout(ms)

    def go(self, page_name):
        if self.width <= 760:
            label = {"pending": "Pending", "nodes": "Nodes", "history": "History"}[page_name]
            self.page.locator(".tabbar .tab-item", has_text=label).click()
        else:
            label = {"pending": "Pending Deploys", "nodes": "Nodes", "history": "Deploy History"}[page_name]
            self.page.locator(".sidebar .nav-row", has_text=label).click()
        self.settle(200)

    def audit(self):
        return self.page.evaluate(AUDIT_JS)

    def active(self):
        return self.page.evaluate(
            """() => { const a = document.activeElement; if (!a || a === document.body) return 'body';
              const t = (a.getAttribute('aria-label') || a.textContent || '').trim().slice(0, 24);
              return a.tagName.toLowerCase() + (a.className && typeof a.className === 'string' ? '.' + a.className.split(' ')[0] : '') + ' "' + t + '"'; }"""
        )

    def shot(self, path, tall=False):
        page = self.page
        if tall:
            need = page.evaluate(
                """() => { const c = document.querySelector('.content'); const t = document.querySelector('.topbar');
                  const b = document.querySelector('.tabbar');
                  return c.scrollHeight + t.offsetHeight + (b && getComputedStyle(b).display !== 'none' ? b.offsetHeight : 0); }"""
            )
            page.set_viewport_size({"width": self.width, "height": max(self.height, min(need, 3200))})
            page.wait_for_timeout(120)
        page.screenshot(path=str(path), animations="disabled")
        if tall:
            page.set_viewport_size({"width": self.width, "height": self.height})
            page.wait_for_timeout(80)

    def close(self):
        self.page.close()

    @classmethod
    def shutdown(cls):
        for ctx in cls.contexts.values():
            ctx.close()
        cls.contexts = {}
