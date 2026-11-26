#!/usr/bin/env python3
"""Visual check and operation measurements for the local dashboard (card #1884).

Serves the built dist with a fake Haniel node, captures every scene in light and dark at 1440 and 390,
measures the ui-design 0-section operation items (14 to 18), and exits non-zero when any measurement fails.

    pnpm -C dashboard run build
    python3 dashboard/scripts/visual_check.py --out <folder>

Writes PNG captures into --out and operations.md (the operation result file) next to them.
Runs with the system python3 (Playwright and Pillow are installed there). Nothing is started except the
temporary mock server inside this process and a headless Chromium.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

import visual_fixes
import visual_flows
import visual_general
import visual_mock as vm
from visual_checks import OVERFLOW_AUDIT, Env, Report, audit_common, label, row

DASH = Path(__file__).resolve().parents[1]
THEMES = ("light", "dark")
SIZES = ((1440, 900), (390, 844))
LOGS_ICON = "Logs for soulstream-event-ingress-dispatcher"
DISPATCHER = "soulstream-event-ingress-dispatcher"


def _expanded(page):
    row(page, DISPATCHER).locator(".r-toggle").click()
    label(page, LOGS_ICON).click()
    page.wait_for_selector(".log-pane")
    row(page, "haniel").locator(".r-toggle").click()
    row(page, "soulstream").locator(".r-toggle").click()
    row(page, "eb_lore").locator(".r-toggle").click()


def _open_editor(page, aria: str, dialog: str = ".dialog"):
    page.get_by_role("button", name=aria, exact=True).first.click()
    page.wait_for_selector(dialog)


def _update_overlay(page, timeout: bool):
    page.get_by_role("button", name="Approve update").click()
    page.wait_for_selector(".status-panel")
    if timeout:
        page.clock.fast_forward(120_000)
        page.wait_for_selector("text=Server not responding")


def _logs_error(page):
    label(page, "Logs for soulstream-builder").click()
    page.wait_for_selector(".notice")


def _failed_save(page):
    _open_editor(page, f"Edit {DISPATCHER}")
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector(".dialog .notice")


def _control_fail(page):
    label(page, "Stop serendipity-api").click()
    page.wait_for_selector(".content-inner > .notice")


# name, mock setup, page action, sizes it applies to (None = all), page options
SCENES = [
    ("main", None, None, None, {}),
    ("main-full", None, None, None, {"tall": True}),
    ("expanded", None, _expanded, None, {}),
    ("empty", lambda m: setattr(m, "status", vm.empty_status()), None, None, {"wait": ".empty"}),
    ("loading", lambda m: setattr(m, "ws_mode", "hold"), None, None, {"wait": ".sk"}),
    (
        "error-load",
        lambda m: (setattr(m, "ws_mode", "drop"), m.overrides.update({("GET", "/api/status"): (500, "upstream down")})),
        None,
        None,
        {"wait": "[role=alert]"},
    ),
    (
        "error-action",
        lambda m: m.overrides.update({("POST", "/api/services/serendipity-api/stop"): (500, "process did not exit within 10s")}),
        _control_fail,
        None,
        {},
    ),
    (
        "error-logs",
        lambda m: m.overrides.update({("GET", "/api/services/soulstream-builder/logs"): (404, "not found")}),
        _logs_error,
        None,
        {},
    ),
    ("dialog-add-service", None, lambda p: _open_editor(p, "Add service"), None, {}),
    (
        "dialog-edit-service",
        lambda m: m.overrides.update({("PUT", f"/api/config/services/{DISPATCHER}"): (422, "ready: expected port:N, delay:N or log:TEXT")}),
        _failed_save,
        None,
        {},
    ),
    ("dialog-edit-repo", None, lambda p: _open_editor(p, "Edit eb_lore"), None, {}),
    ("dialog-delete", None, lambda p: _open_editor(p, f"Delete {DISPATCHER}"), None, {}),
    ("dialog-restart", None, lambda p: _open_editor(p, "Restart Haniel"), None, {}),
    ("update-running", None, lambda p: _update_overlay(p, False), None, {"clock": True}),
    ("update-timeout", None, lambda p: _update_overlay(p, True), None, {"clock": True}),
    ("theme-menu", None, lambda p: p.get_by_label("Theme:", exact=False).click(), None, {}),
    ("drawer", None, lambda p: p.get_by_label("Open menu").click(), {390}, {"wait": ".row"}),
]


def capture_all(env: Env) -> None:
    for name, setup, act, sizes, opts in SCENES:
        for scheme in THEMES:
            for width, height in SIZES:
                if sizes and width not in sizes:
                    continue
                mock = vm.Mock()
                if setup:
                    setup(mock)
                ctx, page, _ = env.open(
                    width, 2400 if opts.get("tall") else height, scheme, mock=mock, clock=opts.get("clock", False), wait=opts.get("wait", ".row")
                )
                if act:
                    act(page)
                page.wait_for_timeout(250)
                env.png(page, f"{name}-{scheme}-{width}")
                audit_common(env, page, f"{name} {scheme} {width}")
                if width == 390 and name != "loading":
                    bad = page.evaluate(OVERFLOW_AUDIT)
                    env.report.check("16", f"390px 장면 넘침과 잘림 없음: {name}", not bad, f"{scheme}: {bad}")
                ctx.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path, help="folder for captures and operations.md")
    parser.add_argument("--dist", type=Path, default=DASH / "dist", help="built dashboard (default: dashboard/dist)")
    parser.add_argument("--merge", action="store_true", help="keep the measurements of earlier --stages runs in the same --out folder")
    parser.add_argument("--stages", default="captures,flows,fixes,general", help="comma list of captures, general and fixes (screen-review fixes) and the flow groups dialogs, layers, focus, errors, resize, narrow360 (default: all)")
    args = parser.parse_args()

    if not (args.dist / "index.html").is_file():
        print(f"{args.dist}/index.html not found. Run `pnpm -C dashboard run build` first.", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)

    server, base = vm.serve(args.dist)
    report = Report()
    state = args.out / "visual-report.json"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            env = Env(browser=browser, base=base, out=args.out, report=report)
            stages = args.stages.split(",")
            if "captures" in stages:
                capture_all(env)
            flows = [x for x in stages if x in visual_flows.STAGES]
            if flows or "flows" in stages:
                visual_flows.run(env, flows)
            if "fixes" in stages:
                visual_fixes.run(env)
            if "general" in stages:
                visual_general.run(env)
            browser.close()
    finally:
        server.shutdown()

    if args.merge:
        report.merge(state)
    report.save(state)
    command = f"python3 dashboard/scripts/visual_check.py --out {args.out}"
    (args.out / "operations.md").write_text(report.operations_md(command, str(args.out)), encoding="utf-8")
    print(f"\n{len(report.rows)} measurements, {len(report.failures)} failed. operations.md written to {args.out}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
