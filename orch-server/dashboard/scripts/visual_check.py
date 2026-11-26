#!/usr/bin/env python3
"""Visual check for the orchestrator dashboard (orch-server/dashboard).

Serves the built dist, fakes /api/orch/* and /ws/dashboard with production-shaped
data, captures the scenes in light and dark at 1440 and 390 (plus 360 with the
longest names), measures the design contract and the ui-design section 0
operation items 14 to 18, and exits non-zero on any failure.

Run (system python3 with Playwright, after `pnpm run build`):
  python3 orch-server/dashboard/scripts/visual_check.py --out <shots dir>
  python3 orch-server/dashboard/scripts/visual_check.py --before-dist <old dist> --out <shots dir>
    (captures only the old screens into <out>/before/, no measurements)
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from vc_support import checks, ops  # noqa: E402
from vc_support.harness import DistServer, FakeApi, Session  # noqa: E402

DASH = Path(__file__).resolve().parent.parent
THEMES = ["light", "dark"]
WIDTHS = [1440, 390]


ONLY = ""


class Report:
    def __init__(self):
        self.failures = []
        self.scenes = 0

    def fail(self, scene, msg):
        self.failures.append((scene, msg))
        print(f"FAIL {scene}: {msg}")

    def failed(self, *prefixes):
        return any(s.startswith(p) for s, _ in self.failures for p in prefixes)


def expand_rows(S, *names):
    for name in names:
        S.page.locator(".row", has_text=name).locator(".r-toggle").first.click()
    S.settle(200)


def click_visible(S, selector):
    S.page.locator(f"{selector}:visible").first.click()


# ---- scenes: name -> setup(S); each returns nothing -------------------------

def pending_default(S):
    S.open()


def pending_expanded(S):
    S.open()
    expand_rows(S, "soulstream-server-src", "lore-search")
    S.page.get_by_role("button", name=re.compile(r"\d+ more commits?")).first.click()
    S.settle(150)


def pending_selected(S):
    S.open()
    S.page.get_by_text("Select all").click()
    S.settle(100)


def pending_reject(S):
    S.open()
    S.page.locator(".row", has_text="soulstream-server-src").get_by_role("button", name="Reject", exact=True).click()
    S.page.locator("[role=dialog] textarea").fill("WIP — 아직 검토 중")
    S.settle(300)


def pending_postpone(S):
    S.open()
    S.page.get_by_role("button", name="Postpone", exact=True).first.click()
    S.settle(300)


def with_mode(ep, mode, page_name, hold=False):
    def setup(S):
        S.api.mode[ep] = mode
        if hold:
            S.page.goto(S.base_url)
            S.page.wait_for_selector(".page-title")
            if page_name != "pending":
                S.go(page_name)
            S.page.wait_for_selector(".sk-row")
            S.settle(150)
            return
        S.open()
        if page_name != "pending":
            S.go(page_name)
    return setup


def nodes_page(S, mode="ok"):
    S.open()
    S.go("nodes")


def nodes_expanded(S):
    nodes_page(S)
    S.page.locator(".row .r-toggle").first.click()
    expand_rows(S, "eias-linegames-wsl", "silent-manari-ts")
    S.page.locator(".row", has_text="eias-linegames").nth(0)  # keep the second node collapsed
    S.settle(200)


def nodes_inflight(S):
    nodes_page(S)
    S.page.locator(".row .r-toggle").first.click()
    S.settle(200)
    click_visible(S, "button[aria-label='Restart soulstream-orch']")
    S.page.wait_for_selector(".spin", state="attached")
    S.settle(150)


def nodes_filter_none(S):
    nodes_page(S)
    S.page.get_by_placeholder("Filter nodes").fill("zzz")
    S.settle(100)


def history_page(S):
    S.open()
    S.go("history")


def history_open(S):
    history_page(S)
    S.page.get_by_role("button", name=re.compile("Show error")).first.click()
    S.settle(150)


def history_failed(S):
    history_page(S)
    S.page.get_by_role("tab", name=re.compile("^Failed")).click()
    S.settle(150)


def history_superseded(S):
    history_page(S)
    S.page.get_by_label("Include superseded").check()
    S.page.wait_for_selector("text=superseded by a1b2c3d")
    S.settle(150)


def theme_menu(S):
    S.open()
    S.page.locator("button[aria-label^='Theme']").click()
    S.page.wait_for_selector("[role=menu]")
    S.settle(250)


def drawer(S):
    S.open()
    S.page.get_by_role("button", name="Open menu").click()
    S.page.wait_for_selector(".drawer")
    S.settle(300)


def long_pending(S):
    S.api.long = True
    S.open()
    expand_rows(S, "extremely-long", "lore-search")


def long_nodes(S):
    S.api.long = True
    nodes_page(S)
    S.page.locator(".row .r-toggle").first.click()
    S.settle(200)


def long_history(S):
    S.api.long = True
    history_page(S)
    S.page.get_by_role("button", name=re.compile("Show error")).first.click()
    S.settle(150)


def conn_reconnecting(S):
    S.open()


def conn_disconnected(S):
    S.page.clock.install()
    S.page.goto(S.base_url)
    S.page.wait_for_selector(".page-title")
    for _ in range(10):  # each refused socket needs real time before the next retry is scheduled
        S.page.clock.run_for(16000)
        S.page.wait_for_timeout(150)
    S.settle(300)


# (name, setup, tall, checks, widths filter, themes filter, extra session kwargs)
def scene_table():
    c_pend = [checks.frame, checks.pending]
    return [
        ("pending-default", pending_default, True, c_pend + [checks.status_markers], None, None, {}),
        ("pending-expanded", pending_expanded, True, [], None, None, {}),
        ("pending-selected", pending_selected, True, [], None, None, {}),
        ("pending-reject", pending_reject, False, [], None, None, {}),
        ("pending-postpone", pending_postpone, False, [], None, None, {}),
        ("pending-empty", with_mode("pending", "empty", "pending"), False, [], None, None, {}),
        ("pending-error", with_mode("pending", "error", "pending"), False, [], None, None, {}),
        ("pending-loading", with_mode("pending", "hold", "pending", hold=True), False, [], None, None, {}),
        ("nodes-default", nodes_page, True, [], None, None, {}),
        ("nodes-expanded", nodes_expanded, True, [checks.nodes, checks.status_markers], None, None, {}),
        ("nodes-inflight", nodes_inflight, True, [], None, None, {}),
        ("nodes-filter-none", nodes_filter_none, False, [], None, None, {}),
        ("nodes-empty", with_mode("nodes", "empty", "nodes"), False, [], None, None, {}),
        ("nodes-error", with_mode("nodes", "error", "nodes"), False, [], None, None, {}),
        ("nodes-loading", with_mode("nodes", "hold", "nodes", hold=True), False, [], None, None, {}),
        ("history-default", history_page, True, [checks.history, checks.status_markers], None, None, {}),
        ("history-open", history_open, True, [], None, None, {}),
        ("history-failed", history_failed, True, [], None, None, {}),
        ("history-superseded", history_superseded, True, [], None, None, {}),
        ("history-empty", with_mode("history", "empty", "history"), False, [], None, None, {}),
        ("history-error", with_mode("history", "error", "history"), False, [], None, None, {}),
        ("history-loading", with_mode("history", "hold", "history", hold=True), False, [], None, None, {}),
        ("theme-menu", theme_menu, False, [], [1440], None, {}),
        ("drawer", drawer, False, [], [390], None, {}),
        ("conn-reconnecting", conn_reconnecting, False, [], None, None, {"ws_closed": True}),
        ("conn-disconnected", conn_disconnected, False, [], None, None, {"ws_closed": True}),
    ]


def run_scenes(browser, server, api, out, rep):
    for theme in THEMES:
        for width in WIDTHS:
            for name, setup, tall, chks, only_w, only_t, kw in scene_table():
                if (only_w and width not in only_w) or (only_t and theme not in only_t):
                    continue
                scene = f"{name}-{theme}-{width}"
                if ONLY not in scene:
                    continue
                print("scene", scene, flush=True)
                api.reset()
                S = Session(browser, server.url, api, theme, width, **kw)
                try:
                    setup(S)
                    png = out / f"{scene}.png"
                    S.shot(png, tall=tall)
                    if name == "pending-default":
                        checks.pixels(S, rep, scene, png)
                        if S.page.evaluate("window.__themeAtDcl") != theme:
                            rep.fail(scene, f"theme not applied before first paint: {S.page.evaluate('window.__themeAtDcl')}")
                    for c in chks:
                        c(S, rep, scene)
                    if name == "conn-reconnecting" or name == "conn-disconnected":
                        want = "Reconnecting…" if name == "conn-reconnecting" else "Disconnected"
                        got = S.page.locator(".conn").inner_text().strip()
                        if got != want:
                            rep.fail(scene, f"connection chip {got!r}, want {want!r}")
                    for v in S.audit():
                        rep.fail(scene, v)
                except Exception as e:  # a broken scene is a failure, not a crash
                    rep.fail(scene, f"scene error: {type(e).__name__}: {str(e)[:200]}")
                finally:
                    S.close()
                rep.scenes += 1


def run_long(browser, server, api, out, rep):
    for theme in THEMES:
        for width in (360, 1440):
            for name, setup in (("long-pending", long_pending), ("long-nodes", long_nodes), ("long-history", long_history)):
                scene = f"{name}-{theme}-{width}"
                api.reset()
                S = Session(browser, server.url, api, theme, width)
                try:
                    setup(S)
                    S.shot(out / f"{scene}.png", tall=True)
                    for v in S.audit():
                        rep.fail(scene, v)
                    squeezed = S.page.evaluate(
                        "() => Array.from(document.querySelectorAll('.r-name')).filter((e) => e.getClientRects().length && e.clientWidth < Math.min(e.scrollWidth, 48)).map((e) => e.clientWidth)"
                    )
                    if squeezed:
                        rep.fail(scene, f"a row title is squeezed to {min(squeezed)}px")
                except Exception as e:
                    rep.fail(scene, f"scene error: {type(e).__name__}: {str(e)[:200]}")
                finally:
                    S.close()
                rep.scenes += 1


def run_ops(browser, server, api, rep):
    rows = []
    for width in WIDTHS:
        def fresh(page_name=None, **kw):
            api.reset()
            S = Session(browser, server.url, api, "light", width, **kw)
            S.open()
            if page_name:
                S.go(page_name)
            return S

        for post in (False, True):
            scene = f"ops-dialog-{'postpone' if post else 'reject'}-{width}"
            S = fresh()
            try:
                row = ops.dialog_ops(S, rep, scene, post)
                rows.append((width, row))
            except Exception as e:
                rep.fail(scene, f"scene error: {type(e).__name__}: {str(e)[:200]}")
            S.close()
        if width <= 760:
            scene = f"ops-drawer-{width}"
            S = fresh()
            try:
                rows.append((width, ops.drawer_ops(S, rep, scene)))
            except Exception as e:
                rep.fail(scene, f"scene error: {type(e).__name__}: {str(e)[:200]}")
            S.close()
        scene = f"ops-theme-{width}"
        S = fresh()
        try:
            rows.append((width, ops.theme_menu_ops(S, rep, scene)))
        except Exception as e:
            rep.fail(scene, f"scene error: {type(e).__name__}: {str(e)[:200]}")
        S.close()
        scene = f"ops-filter-{width}"
        S = fresh("nodes")
        try:
            rows.append((width, ops.filter_ops(S, rep, scene)))
        except Exception as e:
            rep.fail(scene, f"scene error: {type(e).__name__}: {str(e)[:200]}")
        S.close()
        scene = f"ops-tab-order-{width}"
        S = fresh()
        try:
            ops.tab_order(S, rep, scene)
        except Exception as e:
            rep.fail(scene, f"scene error: {type(e).__name__}: {str(e)[:200]}")
        S.close()
        for page_name, ep in (("pending", "pending"), ("nodes", "nodes"), ("history", "history")):
            scene = f"ops-error-{page_name}-{width}"
            api.reset()
            api.mode[ep] = "error"
            S = Session(browser, server.url, api, "light", width)
            try:
                S.open()
                if page_name != "pending":
                    S.go(page_name)
                ops.error_retry(S, rep, scene, page_name)
            except Exception as e:
                rep.fail(scene, f"scene error: {type(e).__name__}: {str(e)[:200]}")
            S.close()
    scene = "ops-resize-1440"
    api.reset()
    S = Session(browser, server.url, api, "light", 1440, 700)
    try:
        S.open()
        S.go("nodes")
        ops.resize_ops(S, rep, scene)
    except Exception as e:
        rep.fail(scene, f"scene error: {type(e).__name__}: {str(e)[:200]}")
    S.close()
    return rows


def run_system_theme(browser, server, api, rep):
    for scheme, bg in (("light", "rgb(255, 255, 255)"), ("dark", "rgb(12, 13, 12)")):
        scene = f"system-theme-{scheme}"
        api.reset()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
        page = ctx.new_page()
        page.route(re.compile(r".*/api/orch/.*"), api.handle)
        page.route_web_socket(re.compile(r".*/ws/dashboard.*"), lambda ws: None)
        page.goto(server.url)
        page.wait_for_selector(".page-title")
        got = page.evaluate("() => [document.documentElement.getAttribute('data-theme'), getComputedStyle(document.body).backgroundColor, localStorage.getItem('haniel-theme')]")
        if got != [None, bg, None]:
            rep.fail(scene, f"System theme should follow the OS: {got}")
        ctx.close()
        rep.scenes += 1


OPS_ITEMS = [
    (14, "Focus after press", ("ops-dialog", "ops-drawer", "ops-theme", "ops-filter"),
     "Dialogs open on the reason field and return to the pressed row button; the drawer and theme menu return to their header button; the filter keeps focus."),
    (15, "Tab and Esc", ("ops-tab-order", "ops-dialog", "ops-drawer", "ops-theme"),
     "Tab walks the page in DOM order and never reaches a hidden or covered control; Tab and Shift+Tab stay inside an open dialog or drawer; Esc closes each layer and leaves the outside screen unchanged."),
    (16, "Overflow and clipping at the narrowest width", ("long-",),
     "360px and 1440px with the longest repo, node, branch, commit, and service names: no sideways scroll, titles ellipsize, numbers and ids stay on one line, no control leaves the viewport."),
    (17, "Error state", ("ops-error", "pending-error", "nodes-error", "history-error"),
     "Each screen's failed request becomes an in-place alert with Retry inside the same frame; Retry loads the rows."),
    (18, "Window resize", ("ops-resize",),
     "1440 to 390 to 1440: layout, filter text, expanded rows, and scroll position are kept; an open theme menu stays inside the viewport; the drawer is gone after growing; a dialog becomes a bottom sheet and keeps its text."),
]


def write_operations(out, rep, rows, command):
    lines = [
        "# Operation results (ui-design section 0, items 14 to 18)",
        "",
        "Measured by `orch-server/dashboard/scripts/visual_check.py`; a row says 통과 only when every measurement behind it passed in this run.",
        "",
        "## 14번부터 18번",
        "",
        "| 항목 | 결과 | 근거 |",
        "|---|---|---|",
    ]
    for num, title, prefixes, note in OPS_ITEMS:
        verdict = "실패" if rep.failed(*prefixes) else "통과"
        lines.append(f"| {num} {title} | {verdict} | {note} |")
    lines += [
        "",
        "## 대화상자와 줄 안 편집",
        "",
        "| 대상 | 폭 | 여는 법 | 첫 초점 | Enter | Esc | 닫은 뒤 초점 |",
        "|---|---|---|---|---|---|---|",
    ]
    for width, r in rows:
        lines.append(
            f"| {r['target']} | {width} | {r['open']} | {r['first']} | {r['enter']} | {r['esc']} | {r['after']} |"
        )
    lines += [
        "",
        "## 실행",
        "",
        f"- 명령: `{command}`",
        f"- 장면 수: {rep.scenes}, 실패: {len(rep.failures)}",
        f"- 캡처: `{out}` (장면-테마-폭.png)",
        "",
    ]
    (out / "operations.md").write_text("\n".join(lines), encoding="utf-8")


def capture_before(browser, server, api, out):
    """Old screens on the same fake data, for the before/after pair. No measurements."""
    out.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        for width in WIDTHS:
            api.reset()
            S = Session(browser, server.url, api, theme, width, ready="h1")
            S.open()
            S.shot(out / f"pending-default-{theme}-{width}.png")
            if width <= 760:
                S.page.locator(".mobile-bottom-nav button[aria-label=Nodes]").click()
            else:
                S.page.locator(".nav-item", has_text="Nodes").click()
            S.settle(250)
            S.page.locator(".node-card-header").first.click()
            S.settle(200)
            S.shot(out / f"nodes-expanded-{theme}-{width}.png")
            if width <= 760:
                S.page.locator(".mobile-bottom-nav button[aria-label=History]").click()
            else:
                S.page.locator(".nav-item", has_text="Deploy History").click()
            S.settle(250)
            S.shot(out / f"history-default-{theme}-{width}.png")
            S.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--dist", type=Path, default=DASH / "dist")
    ap.add_argument("--only", default="", help="run only scenes whose name contains this text (debugging)")
    ap.add_argument("--before-dist", type=Path, help="capture the old screens from this dist into <out>/before")
    args = ap.parse_args()
    global ONLY
    ONLY = args.only
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    api = FakeApi()
    dist = args.before_dist or args.dist
    if not (dist / "index.html").is_file():
        print(f"no build at {dist}; run `pnpm run build` first", file=sys.stderr)
        return 2
    server = DistServer(dist)
    server.start()
    rep = Report()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                if args.before_dist:
                    capture_before(browser, server, api, out / "before")
                    print(f"before captures in {out / 'before'}")
                    return 0
                run_scenes(browser, server, api, out, rep)
                run_long(browser, server, api, out, rep)
                rows = run_ops(browser, server, api, rep)
                run_system_theme(browser, server, api, rep)
            finally:
                Session.shutdown()
                browser.close()
    finally:
        server.stop()
    write_operations(out, rep, rows, "python3 orch-server/dashboard/scripts/visual_check.py --out " + str(out))
    print(f"{rep.scenes} scenes, {len(rep.failures)} failures")
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
