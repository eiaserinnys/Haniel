"""Measurements for the first screen-review fixes (card #1884): favicon, login theme, row-head selection, current section."""

from __future__ import annotations

import base64
from urllib.parse import unquote

import visual_mock as vm  # noqa: F401
from visual_checks import Env, label, row

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" fill="#0B7A4B"/>'
    '<path d="M9 8v16M23 8v16M9 16h14" fill="none" stroke="#FFFFFF" stroke-width="3"/></svg>\n'
)
DARK, LIGHT = "rgb(12, 13, 12)", "rgb(255, 255, 255)"
COMMAND = "node packages/api/dist/index.js"
BG = "() => getComputedStyle(document.body).backgroundColor"
ICON_HREF = "() => document.querySelector('link[rel~=icon]').getAttribute('href')"
CURRENT = "() => [...document.querySelectorAll('{scope} .nav-row[aria-current]')].map((e) => e.textContent.trim().replace(/[0-9]+$/, ''))"
HALF = "() => document.querySelector('.content').clientHeight / 2"
HEAD_TOP = "() => document.getElementById('sec-repos').getBoundingClientRect().top - document.querySelector('.content').getBoundingClientRect().top"


def _icon_bytes(env: Env, page) -> tuple[str, str, bytes]:
    """The icon the page's link[rel~=icon] receives: href, content type, bytes (a data URI is decoded)."""
    href = page.evaluate(ICON_HREF)
    if href.startswith("data:"):
        head, _, data = href.partition(",")
        body = base64.b64decode(data) if head.endswith(";base64") else unquote(data).encode()
        return href[:40], head[5:].split(";")[0], body
    res = page.request.get(env.base + href)
    return href, res.headers.get("content-type", "").split(";")[0], res.body()


def _favicon(env: Env):
    r = env.report
    expected = FAVICON.encode()
    ctx, page, _ = env.open(1440, 900)
    seen = {}
    for name, path in (("대시보드(/)", "/"), ("로그인(/auth/login)", "/auth/login")):
        page.goto(env.base + path)
        href, kind, body = _icon_bytes(env, page)
        seen[name] = body
        r.check("g", f"보완 1: {name} 아이콘이 운영과 같은 길로 SVG로 오고 지정한 그림과 같다", kind == "image/svg+xml" and body.strip() == expected.strip(), f"{href} {kind} {len(body)}바이트")
    r.check("g", "보완 1: 대시보드와 로그인이 같은 그림을 받는다", seen["대시보드(/)"] == seen["로그인(/auth/login)"])
    ctx.close()


def _login_theme(env: Env):
    r = env.report
    cases = [("dark", "light", DARK), ("light", "dark", LIGHT), (None, "light", LIGHT), (None, "dark", DARK), ("broken", "dark", DARK), ("broken", "light", LIGHT)]
    for stored, scheme, want in cases:
        ctx = env.browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        if stored == "broken":
            page.add_init_script("Storage.prototype.getItem = () => { throw new Error('denied') }")
        elif stored:
            page.add_init_script(f"localStorage.setItem('haniel-theme', '{stored}')")
        page.goto(env.base + "/auth/login")
        page.wait_for_selector("#login-form")
        got = page.evaluate(BG)
        r.check("g", f"보완 2: 로그인 바탕 저장값 {stored or '없음'} + OS {scheme}", got == want and not errors, f"{got} 기대 {want} {errors}")
        if stored in ("dark", "light"):
            if (stored, scheme) == ("dark", "light"):
                env.png(page, "login-saved-dark-os-light-1440")
            page.goto(env.base + "/")
            page.wait_for_selector(".topbar")
            r.check("g", f"보완 2: 저장값 {stored}면 대시보드와 로그인의 바탕이 같다(OS {scheme})", page.evaluate(BG) == got)
        ctx.close()


def _selection(env: Env):
    r = env.report
    sel = "() => getSelection().toString()"
    ctx, page, _ = env.open(1440, 900)
    page.wait_for_timeout(300)
    svc = row(page, "serendipity-api")
    toggle = svc.locator(".r-toggle")
    cmd = svc.locator(".r-cmd")
    expanded = lambda: toggle.get_attribute("aria-expanded")  # noqa: E731

    cmd.dblclick()
    page.wait_for_timeout(150)
    r.check("g", "보완 3: 서비스 행 명령 글 더블클릭: 선택이 비고 행은 한 번만 토글", page.evaluate(sel) == "" and expanded() == "true", f"선택 {page.evaluate(sel)!r}, expanded {expanded()}")
    env.png(page, "row-select-dblclick-1440", "extra")
    toggle.click()
    r.check("g", "보완 3: 한 번 누르면 접는다", expanded() == "false")

    box = cmd.bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + 1, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, y, steps=4)
    page.mouse.move(box["x"] + box["width"] + 3, y, steps=4)
    page.mouse.up()
    page.wait_for_timeout(150)
    r.check("g", "보완 3: 명령 끌어 고르기: 선택이 명령 전체, 행은 그대로", page.evaluate(sel).strip() == COMMAND and expanded() == "false", f"선택 {page.evaluate(sel)!r}, expanded {expanded()}")
    env.png(page, "row-select-drag-1440", "extra")

    row(page, "serendipity-api").locator(".r-name").click()
    r.check("g", "보완 3: 선택이 남은 행도 다음 한 번 누르기는 펼친다", expanded() == "true" and page.evaluate(sel) == "", f"expanded {expanded()}, 선택 {page.evaluate(sel)!r}")
    toggle.focus()
    page.keyboard.press("Enter")
    r.check("g", "보완 3: Enter는 접는다", expanded() == "false")
    page.keyboard.press("Space")
    r.check("g", "보완 3: Space는 펼친다", expanded() == "true")

    for name in ("eb_lore", "soulstream", "haniel"):
        head = row(page, name)
        t = head.locator(".r-toggle")
        head.locator(".r-branch").dblclick()
        page.wait_for_timeout(100)
        r.check("g", f"보완 3: {name} 행 머리 더블클릭: 선택이 비고 한 번만 토글", page.evaluate(sel) == "" and t.get_attribute("aria-expanded") == "true", f"선택 {page.evaluate(sel)!r}")
    ctx.close()


def _current_section(env: Env):
    r = env.report
    scroll_to = "(top) => { document.querySelector('.content').scrollTop = top }"
    for width, height, scope in ((1440, 900, ".sidebar"), (390, 844, ".drawer")):
        ctx, page, _ = env.open(width, height)
        page.wait_for_timeout(300)
        query = CURRENT.replace("{scope}", scope)

        def now(open_drawer=False):
            if width == 390 and open_drawer:
                page.get_by_label("Open menu").click()
                page.wait_for_selector(".drawer")
            got = page.evaluate(query)
            if width == 390 and open_drawer:
                page.keyboard.press("Escape")
                page.wait_for_selector(".drawer", state="detached")
            return got

        tag = f"{width}px {'사이드바' if width == 1440 else '서랍'}"
        got = now(True)
        r.check("g", f"보완 4: {tag}: 처음에는 Services", got == ["Services"], str(got))
        page.mouse.move(width / 2, height / 2)
        for _ in range(60):
            if page.evaluate(HEAD_TOP) <= page.evaluate(HALF):
                break
            page.mouse.wheel(0, 120)
            page.wait_for_timeout(30)
        page.wait_for_timeout(200)
        top = page.evaluate(HEAD_TOP)
        got = now(True)
        r.check("g", f"보완 4: {tag}: 휠로 Repositories 머리가 스크롤 칸 가운데를 지나면 Repositories", top <= page.evaluate(HALF) and got == ["Repositories"], f"머리 {top:.0f}px, {got}")
        if width == 1440:
            env.png(page, "section-current-repos-1440", "extra")
        else:
            page.get_by_label("Open menu").click()
            page.wait_for_selector(".drawer")
            page.wait_for_timeout(600)  # the drawer slides in; capture it once it has settled
            env.png(page, "section-current-repos-drawer-390", "extra")
            page.keyboard.press("Escape")
            page.wait_for_selector(".drawer", state="detached")
        page.evaluate(scroll_to, 0)
        page.wait_for_timeout(200)
        got = now(True)
        r.check("g", f"보완 4: {tag}: 맨 위로 올리면 Services", got == ["Services"], str(got))
        if width == 1440:
            label(page, "Sections").get_by_role("button", name="Dependencies").click()
        else:
            page.get_by_label("Open menu").click()
            page.wait_for_selector(".drawer")
            page.locator(".drawer .nav-row", has_text="Dependencies").click()
            page.wait_for_selector(".drawer", state="detached")
        page.wait_for_timeout(300)
        got = now(True)
        r.check("g", f"보완 4: {tag}: Dependencies를 누르면 Dependencies(짧아 맨 위에 못 오는 경우 포함)", got == ["Dependencies"], str(got))
        page.evaluate(scroll_to, 0)
        page.wait_for_timeout(200)
        got = now(True)
        r.check("g", f"보완 4: {tag}: Dependencies를 누른 뒤 맨 위로 올리면 Services로 돌아온다", got == ["Services"], str(got))
        ctx.close()


SECTION_ORDER = ["Services", "Repositories", "Dependencies"]
SIZES_3 = ((1440, 900), (1440, 760), (1024, 768), (1440, 640), (390, 844), (360, 640))
SCROLL_TOP = "() => document.querySelector('.content').scrollTop"
BROKEN_WORDS = """() => {
  const bad = [];
  for (const bar of document.querySelectorAll('.log-bar')) {
    const walker = document.createTreeWalker(bar, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const re = /\\S+/g;
      let m;
      while ((m = re.exec(node.textContent))) {
        const range = document.createRange();
        range.setStart(node, m.index);
        range.setEnd(node, m.index + m[0].length);
        const tops = new Set([...range.getClientRects()].filter((r) => r.width > 0).map((r) => Math.round(r.top)));
        if (tops.size > 1) bad.push(m[0]);
      }
    }
  }
  return bad;
}"""
BAR_BUTTONS = "() => [...document.querySelectorAll('.log-bar button')].map((b) => { const r = b.getBoundingClientRect(); return { right: Math.round(r.right), height: Math.round(r.height) }; })"


def _expand_rows(page):
    row(page, "soulstream-event-ingress-dispatcher").locator(".r-toggle").click()
    label(page, "Logs for soulstream-event-ingress-dispatcher").click()
    page.wait_for_selector(".log-pane")
    row(page, "eb_lore").locator(".r-toggle").click()
    page.evaluate("document.querySelector('.content').scrollTop = 0")
    page.wait_for_timeout(200)


def _section_walk(env: Env):
    """Wheel down 100px at a time from the top: each section lights once, in order, never going back."""
    r = env.report
    for width, height in SIZES_3:
        phone = width <= 760
        for expanded in (False, True):
            ctx, page, _ = env.open(width, height)
            page.wait_for_timeout(300)
            if expanded:
                _expand_rows(page)
            query = CURRENT.replace("{scope}", ".drawer" if phone else ".sidebar")
            seen: list[list] = []  # [name, first scrollTop, last scrollTop]
            page.mouse.move(width * 0.7, height / 2)
            previous = -1
            for _ in range(80):
                top = page.evaluate(SCROLL_TOP)
                if phone:
                    page.get_by_label("Open menu").click()
                    page.wait_for_selector(".drawer")
                    got = page.evaluate(query)
                    page.keyboard.press("Escape")
                    page.wait_for_selector(".drawer", state="detached")
                    page.mouse.move(width * 0.7, height / 2)
                else:
                    got = page.evaluate(query)
                name = got[0] if len(got) == 1 else "-"
                if seen and seen[-1][0] == name:
                    seen[-1][2] = top
                else:
                    seen.append([name, top, top])
                if top == previous:
                    break
                previous = top
                page.mouse.wheel(0, 100)
                page.wait_for_timeout(60)
            tag = f"{width}x{height} {'로그·리포 펼침' if expanded else '접힘'}"
            r.check("g", f"보완 3-A: {tag}: 휠 100px씩 내리면 세 구역이 순서대로 하나씩 켜진다", [x[0] for x in seen] == SECTION_ORDER, " > ".join(f"{n}({a}~{b})" for n, a, b in seen))
            if (width, height, expanded) == (1440, 900, False) and [x[0] for x in seen] == SECTION_ORDER:
                span = seen[1][2] - seen[1][1]
                r.check("g", "보완 3-A: 1440x900 접힘에서 Repositories가 스크롤 200px 안팎 동안 켜져 있다", 150 <= span <= 260, f"scrollTop {seen[1][1]}~{seen[1][2]} (켜진 길이 {span}px)")
        ctx.close()


def _log_bar(env: Env):
    r = env.report
    for width, height in ((390, 844), (360, 800)):
        ctx, page, _ = env.open(width, height)
        row(page, "soulstream-event-ingress-dispatcher").locator(".r-toggle").click()
        label(page, "Logs for soulstream-event-ingress-dispatcher").click()
        page.wait_for_selector(".log-bar")
        page.wait_for_timeout(300)
        buttons = page.evaluate(BAR_BUTTONS)
        heights = [b["height"] for b in buttons]
        if width != 1440:
            r.check("g", f"보완 3-B: {width} 로그 막대 낱말이 중간에서 끊기지 않는다", page.evaluate(BROKEN_WORDS) == [], str(page.evaluate(BROKEN_WORDS)))
            limit = width - 20
            r.check("g", f"보완 3-B: {width} 로그 막대 단추 오른쪽 끝이 {limit}px 안이다", max(b["right"] for b in buttons) <= limit, str([b["right"] for b in buttons]))
            # Heights measured on commit 60c4cb73: the line-count buttons are 36px, the refresh icon button 44px on a phone.
            r.check("g", f"보완 3-B: {width} 로그 막대 단추 높이가 그대로다(줄 수 36, 다시 받기 44)", heights[:-1] == [36] * (len(heights) - 1) and heights[-1] == 44, str(heights))
            page.locator(".logs").first.screenshot(path=str(env.out / "extra" / f"log-bar-{width}.png"))
        ctx.close()


def run(env: Env) -> None:
    (env.out / "extra").mkdir(parents=True, exist_ok=True)
    _favicon(env)
    _login_theme(env)
    _selection(env)
    _current_section(env)
    _section_walk(env)
    _log_bar(env)
