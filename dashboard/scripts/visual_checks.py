"""Measurements for the local dashboard visual check: DOM audits, pixel sampling, and the result report."""

from __future__ import annotations

import colorsys
import io
import json
from pathlib import Path
from dataclasses import dataclass, field

from PIL import Image

# ---------- result report ----------


@dataclass
class Report:
    rows: list[tuple[str, str, bool, str]] = field(default_factory=list)  # item, name, ok, detail
    dialogs: list[dict] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def check(self, item: str, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((item, name, bool(ok), detail))
        print(f"[{'ok' if ok else 'FAIL'}] {item} {name}" + (f" — {detail}" if detail and not ok else ""))
        return bool(ok)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({"rows": self.rows, "dialogs": self.dialogs}, ensure_ascii=False, indent=1), encoding="utf-8")

    def merge(self, path: Path) -> None:
        """Take over rows from an earlier staged run; rows of the same measurement are replaced by the new ones.

        A failed old row is also dropped when this run measured the same item and name again (its detail text changed).
        """
        if not path.is_file():
            return
        old = json.loads(path.read_text(encoding="utf-8"))
        have = {(r[0], r[1], r[3]) for r in self.rows}
        redone = {(r[0], r[1]) for r in self.rows}
        self.rows = [tuple(r) for r in old["rows"] if (r[0], r[1], r[3]) not in have and (r[2] or (r[0], r[1]) not in redone)] + self.rows
        seen = {d["target"] for d in self.dialogs}
        self.dialogs = [d for d in old["dialogs"] if d["target"] not in seen] + self.dialogs

    @property
    def failures(self) -> list[tuple[str, str, bool, str]]:
        return [r for r in self.rows if not r[2]]

    def operations_md(self, command: str, out: str) -> str:
        lines = ["# 조작 결과 (ui-design 0절 14~18번, 카드 #1884 로컬 대시보드)", "", "## 14번부터 18번", "", "| 항목 | 결과 | 근거 |", "|---|---|---|"]
        for item in ("14", "15", "16", "17", "18"):
            rows = [r for r in self.rows if r[0] == item]
            if item in self.skipped:
                lines.append(f"| {item} | 해당 없음 | {self.skipped[item]} |")
                continue
            ok = rows and all(r[2] for r in rows)
            evidence = "; ".join(f"{r[1]}" + (f" ({r[3]})" if r[3] else "") for r in rows if r[2])
            bad = "; ".join(f"실패: {r[1]} ({r[3]})" for r in rows if not r[2])
            lines.append(f"| {item} | {'통과' if ok else '실패'} | {(bad + '; ' if bad else '') + evidence} |")
        lines += ["", "## 화면 기준 측정 (그 밖의 단언)", "", "| 단언 | 결과 | 근거 |", "|---|---|---|"]
        for name, ok, detail in _group_general(self.rows):
            lines.append(f"| {name} | {'통과' if ok else '실패'} | {detail} |")
        lines += ["", "## 대화상자와 줄 안 편집", "", "| 대상 | 여는 법 | 첫 초점 | Enter | Esc | 닫은 뒤 초점 |", "|---|---|---|---|---|---|"]
        for d in self.dialogs:
            lines.append(f"| {d['target']} | {d['open']} | {d['first']} | {d['enter']} | {d['esc']} | {d['after']} |")
        lines += ["", "## 실행", "", f"- 명령: `{command}`", f"- 산출물: `{out}` (PNG 캡처, 이 파일)", ""]
        return "\n".join(lines)


def _group_general(rows):
    seen: dict[str, list] = {}
    for item, name, ok, detail in rows:
        if item == "g":
            seen.setdefault(name, []).append((ok, detail))
    return [(n, all(o for o, _ in v), "; ".join(d for _, d in v if d)[:300]) for n, v in seen.items()]


# ---------- DOM audits (run in the page) ----------

FONT_AUDIT = """() => {
  const bad = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!node.textContent.trim()) continue;
    const el = node.parentElement;
    if (!el || el.closest('.sr-only, script, style, title')) continue;
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    const size = parseFloat(getComputedStyle(el).fontSize);
    if (size < 13) bad.push(el.className + ':' + size);
  }
  return bad.slice(0, 8);
}"""

SHAPE_AUDIT = """() => {
  const bad = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('svg') && el.tagName !== 'svg') continue;
    const s = getComputedStyle(el);
    if (s.borderTopLeftRadius !== '0px' || s.borderBottomRightRadius !== '0px') bad.push(el.className + ' radius ' + s.borderTopLeftRadius);
    if (s.boxShadow !== 'none') bad.push(el.className + ' shadow');
  }
  return bad.slice(0, 8);
}"""

OVERFLOW_AUDIT = """() => {
  const bad = [];
  const vw = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth > vw) bad.push('page scrollWidth ' + document.documentElement.scrollWidth + ' > ' + vw);
  const content = document.querySelector('.content');
  if (content && content.scrollWidth > content.clientWidth) bad.push('content scrollWidth ' + content.scrollWidth + ' > ' + content.clientWidth);
  const inScroller = (el) => el.closest('.scroll-x, .log-pane, .files');
  for (const el of document.querySelectorAll('.app *, .dialog *, .drawer *, .status-panel *')) {
    if (el.closest('.sr-only') || inScroller(el) || el.closest('svg')) continue;
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    const name = el.className && el.className.baseVal === undefined ? el.className : el.tagName;
    if (r.right > vw + 1 || r.left < -1) bad.push(name + ' outside viewport ' + Math.round(r.left) + '..' + Math.round(r.right));
    const s = getComputedStyle(el);
    const hidden = s.overflow === 'hidden' || s.overflowX === 'hidden';
    if (hidden && el.scrollWidth > el.clientWidth + 1 && s.textOverflow !== 'ellipsis' && el.children.length === 0) {
      bad.push(name + ' clipped without ellipsis');
    }
  }
  for (const el of document.querySelectorAll('.m-num, .nav-count, .side-kv .mono, .commit-hash, .st, .r-branch')) {
    if (el.getClientRects().length > 1) bad.push((el.className || el.tagName) + ' wraps onto lines');
  }
  for (const el of document.querySelectorAll('.r-name')) {
    const r = el.getBoundingClientRect();
    if (!r.width || el.scrollWidth <= el.clientWidth + 1) continue;
    const shown = Math.floor((el.clientWidth / el.scrollWidth) * el.textContent.length);
    if (shown < 8) bad.push('r-name shows only ' + shown + ' chars: ' + el.textContent.slice(0, 20));
  }
  for (const row of document.querySelectorAll('.row')) {
    const parts = ['.r-toggle', '.r-actions', '.r-metric'].map((s) => row.querySelector(s)).filter(Boolean).map((e) => e.getBoundingClientRect()).filter((r) => r.width && r.height);
    for (let i = 0; i < parts.length; i++) for (let j = i + 1; j < parts.length; j++) {
      const a = parts[i], b = parts[j];
      if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) bad.push('row parts overlap');
    }
  }
  return bad.slice(0, 10);
}"""

DESCRIBE = """(el) => {
  if (!el) return 'none';
  const text = el.getAttribute('aria-label') || el.placeholder || (el.textContent || '').trim().slice(0, 28) || el.tagName;
  return el.tagName.toLowerCase() + ' "' + text + '"';
}"""

# ---------- pixels ----------


def _lum(rgb) -> float:
    def c(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = rgb[:3]
    return 0.2126 * c(r) + 0.7152 * c(g) + 0.0722 * c(b)


def contrast_ratio(a, b) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def shot(page, clip=None) -> Image.Image:
    png = page.screenshot(clip=clip) if clip else page.screenshot()
    return Image.open(io.BytesIO(png)).convert("RGB")


def text_contrast(page, selector: str, index: int = 0) -> float | None:
    """Contrast of the text pixels in the element against its own background, measured on the capture."""
    box = page.locator(selector).nth(index).bounding_box()
    if not box or box["width"] < 4 or box["height"] < 4:
        return None
    img = shot(page, clip=box)
    pixels = list(img.getdata())
    bg = max(set(pixels), key=pixels.count)
    fg = max(pixels, key=lambda p: abs(_lum(p) - _lum(bg)))
    return contrast_ratio(fg, bg)


def pixel_at(page, x: float, y: float) -> tuple[int, int, int]:
    return shot(page, clip={"x": x, "y": y, "width": 1, "height": 1}).getpixel((0, 0))


def off_palette_pixels(img: Image.Image) -> int:
    """Saturated pixels that are not green. The design allows green and gray/ink only (no red, yellow, blue)."""
    count = 0
    for r, g, b in img.getdata():
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s > 0.45 and v > 0.25 and not (0.33 <= h <= 0.5):
            count += 1
    return count


def close(a, b, tol=3) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ---------- browser environment ----------

import re  # noqa: E402
from pathlib import Path  # noqa: E402

import visual_mock as vm  # noqa: E402

WS_URL = re.compile(r"/ws(\?|$)")


@dataclass
class Env:
    browser: object
    base: str
    out: Path
    report: Report

    def open(self, width, height, scheme="light", *, mock=None, init=None, clock=False, goto=True, wait=".row"):
        """New isolated page wired to a fresh mock node. Returns (context, page, mock)."""
        mock = mock or vm.Mock()
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
        page = ctx.new_page()
        page.route("**/api/**", mock.handle_api)
        page.route_web_socket(WS_URL, mock.handle_ws)
        for script in [*(init or []), mock.ws_stub()]:
            if script:
                page.add_init_script(script)
        if clock:
            page.clock.install()
        if goto:
            page.goto(self.base + "/")
            if wait:
                page.wait_for_selector(wait)
        return ctx, page, mock

    def png(self, page, name: str, folder: str = "") -> None:
        target = self.out / folder if folder else self.out
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / f"{name}.png"))


def row(page, name: str):
    rows = page.locator("#sec-services .row, #sec-repos .row")
    return rows.filter(has=page.locator(".r-name", has_text=re.compile(f"^{re.escape(name)}$")))


def label(page, text: str):
    return page.get_by_label(text, exact=True)


def active(page) -> str:
    return page.evaluate(f"() => ({DESCRIBE})(document.activeElement)")


def audit_common(env: Env, page, tag: str) -> None:
    """Font floor and square, shadowless shapes on whatever is on screen."""
    env.report.check("g", "글자 13px 이상", not (bad := page.evaluate(FONT_AUDIT)), f"{tag}: {bad}")
    env.report.check("g", "모서리 0과 그림자 없음", not (bad := page.evaluate(SHAPE_AUDIT)), f"{tag}: {bad}")
