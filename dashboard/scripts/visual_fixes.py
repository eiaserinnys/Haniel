"""Measurements for the first screen-review fixes (card #1884): favicon, login theme, row-head selection, current section."""

from __future__ import annotations

from pathlib import Path

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
HEAD_TOP = "() => document.getElementById('sec-repos').getBoundingClientRect().top - document.querySelector('.content').getBoundingClientRect().top"


def _favicon(env: Env):
    r = env.report
    expected = FAVICON.encode()
    ctx, page, _ = env.open(1440, 900)
    seen = {}
    for name, path in (("대시보드(/)", "/"), ("로그인(/auth/login)", "/auth/login")):
        page.goto(env.base + path)
        href = page.evaluate(ICON_HREF)
        body = page.request.get(env.base + href).body()
        seen[name] = body
        r.check("g", f"보완 1: {name} link[rel~=icon]이 받는 파일이 지정한 favicon과 바이트까지 같다", body.strip() == expected.strip(), f"{href} {len(body)}바이트")
    r.check("g", "보완 1: 대시보드와 로그인이 같은 favicon을 받는다", seen["대시보드(/)"] == seen["로그인(/auth/login)"])
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
    # 1440 is 640 high so that the Repositories head can reach the top edge with this much data.
    for width, height, scope in ((1440, 640, ".sidebar"), (390, 844, ".drawer")):
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
            if page.evaluate(HEAD_TOP) <= 34:
                break
            page.mouse.wheel(0, 120)
            page.wait_for_timeout(30)
        page.wait_for_timeout(200)
        top = page.evaluate(HEAD_TOP)
        got = now(True)
        r.check("g", f"보완 4: {tag}: 휠로 Repositories 머리까지 내리면 Repositories", top <= 34 and got == ["Repositories"], f"머리 {top:.0f}px, {got}")
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


def run(env: Env) -> None:
    _favicon(env)
    _login_theme(env)
    _selection(env)
    _current_section(env)
