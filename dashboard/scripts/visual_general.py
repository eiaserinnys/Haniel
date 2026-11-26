"""Layout, color, theme, font and login-page measurements for the local dashboard visual check."""

from __future__ import annotations

from visual_checks import (
    OVERFLOW_AUDIT,
    Env,
    audit_common,
    close,
    off_palette_pixels,
    pixel_at,
    row,
    shot,
    text_contrast,
)

PALETTE = {
    "light": {"bg": (255, 255, 255), "ink": (15, 15, 15), "ink2": (92, 92, 92), "ink3": (154, 154, 154), "accent": (11, 122, 75)},
    "dark": {"bg": (12, 13, 12), "ink": (236, 237, 234), "ink2": (160, 163, 158), "ink3": (102, 106, 100), "accent": (79, 214, 156)},
}
TEXTS = [".r-name", ".r-meta", ".m-label", ".nav-count", ".side-kv", ".sect-title", ".kv-k", ".btn-outline"]
RECT = "(sel) => { const r = document.querySelector(sel).getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom, r.width, r.height]; }"


def _geometry(env: Env):
    r = env.report
    ctx, page, _ = env.open(1440, 900)
    g = lambda sel: page.evaluate(RECT, sel)  # noqa: E731
    r.check("g", "머리줄 64px, 사이드바 240px", g(".topbar")[5] == 64 and g(".sidebar")[4] == 240, f"{g('.topbar')[5]}, {g('.sidebar')[4]}")
    r.check("g", "수치 칸 176px와 장부 행 수치 20px", g("#sec-services .r-metric")[4] == 176 and page.evaluate("getComputedStyle(document.querySelector('#sec-services .m-num')).fontSize") == "20px")
    r.check("g", "단추 36px, 행 아이콘 단추 40px, 머리줄 아이콘 단추 44px", g(".btn")[5] == 36 and g("#sec-services .icon-btn")[5] == 40 and g(".topbar-end .icon-btn")[5] == 44, f"{g('.btn')[5]}, {g('#sec-services .icon-btn')[5]}, {g('.topbar-end .icon-btn')[5]}")
    lefts = [g(s)[0] for s in (".sect-title", "#sec-services .ledger", "#sec-deps .figure")]
    r.check("g", "구역 제목, 장부, 그림의 왼쪽 선이 한 줄", max(lefts) - min(lefts) <= 1, str(lefts))
    r.check("g", "구역 오른쪽 끝선: Add service 단추 글자 끝이 장부 오른쪽 끝과 맞는다", abs(g("#sec-services .sect-head .btn")[2] - 16 - g("#sec-services .ledger")[2]) <= 1)
    r.check("g", "글자 크기: 본문 15, 행 제목 16, 구역 18", page.evaluate("[getComputedStyle(document.body).fontSize, getComputedStyle(document.querySelector('.r-name')).fontSize, getComputedStyle(document.querySelector('.sect-title')).fontSize].join()") == "15px,16px,18px")
    r.check("g", "머리 줄 부제 Local은 초록 13px", page.evaluate("(() => { const s = getComputedStyle(document.querySelector('.brand-sub')); return s.fontSize + s.color; })()") == "13pxrgb(11, 122, 75)")
    r.check("g", "head 메타 haniel-ui=index", page.evaluate("document.querySelector('meta[name=haniel-ui]').content") == "index")
    r.check("g", "자기 갱신 행 위 괘선은 초록", page.evaluate("getComputedStyle(document.querySelector('.ledger-accent')).borderTopColor") == "rgb(11, 122, 75)")
    page.get_by_role("button", name="Repositories").click()
    page.wait_for_timeout(150)
    top, at_end = page.evaluate(
        "[document.querySelector('#sec-repos').getBoundingClientRect().top - document.querySelector('.content').getBoundingClientRect().top,"
        " (c => c.scrollTop >= c.scrollHeight - c.clientHeight - 1)(document.querySelector('.content'))]"
    )
    r.check("g", "사이드바 항목을 누르면 그 구역이 맨 위로(끝이면 끝까지) 오고 현재 표시가 옮겨간다", (top <= 40 or at_end) and page.get_by_role("button", name="Repositories").get_attribute("aria-current") == "true", f"top={top} at_end={at_end}")
    toggle = row(page, "eb_lore").locator(".r-toggle")
    toggle.focus()
    page.keyboard.press("Space")
    r.check("g", "행은 눌러서 펼친다(키보드 Space)", toggle.get_attribute("aria-expanded") == "true")
    page.keyboard.press("Tab")
    ring = page.evaluate("(() => { const s = getComputedStyle(document.activeElement); return s.outlineStyle + s.outlineWidth + s.outlineColor; })()")
    r.check("g", "포커스는 2px accent 외곽선", ring == "solid2pxrgb(11, 122, 75)", ring)
    ctx.close()

    ctx, page, _ = env.open(390, 844)
    g = lambda sel: page.evaluate(RECT, sel)  # noqa: E731
    r.check("g", "휴대폰 폭: 단추 44px, 쪽 글자 크기 유지, 하단 탭 막대 없음", g("#sec-services .btn")[5] == 44 and g("#sec-services .icon-btn")[5] == 44 and page.locator(".tabbar").count() == 0, f"{g('#sec-services .btn')[5]}, {g('#sec-services .icon-btn')[5]}")
    r.check("g", "휴대폰 폭: 의존 그래프는 목록으로 바뀐다", page.evaluate("getComputedStyle(document.querySelector('.graph-list')).display") == "block" and not page.locator(".graph-wrap").is_visible())
    ctx.close()


def _theme(env: Env):
    r = env.report
    ctx, page, _ = env.open(1440, 900, "light")
    r.check("g", "기본 테마는 System(저장값과 data-theme 없음)", page.evaluate("[document.documentElement.dataset.theme, localStorage.getItem('haniel-theme')]") == [None, None])
    page.get_by_label("Theme:", exact=False).click()
    page.get_by_role("menuitemradio", name="Dark").click()
    page.wait_for_timeout(100)
    r.check("g", "Dark를 고르면 data-theme과 localStorage가 바뀌고 바탕이 어둡다", page.evaluate("[document.documentElement.dataset.theme, localStorage.getItem('haniel-theme')]") == ["dark", "dark"] and close(pixel_at(page, 700, 20), PALETTE["dark"]["bg"]))
    page.reload()
    page.wait_for_selector(".row")
    r.check("g", "새로고침해도 Dark가 유지된다", page.evaluate("document.documentElement.dataset.theme") == "dark")
    page.get_by_label("Theme:", exact=False).click()
    page.get_by_role("menuitemradio", name="System").click()
    r.check("g", "System을 고르면 data-theme과 저장값이 지워진다", page.evaluate("[document.documentElement.dataset.theme, localStorage.getItem('haniel-theme')]") == [None, None])
    ctx.close()

    ctx, page, _ = env.open(1440, 900, "light", init=["localStorage.setItem('haniel-theme','dark')"], goto=False)
    page.route("**/assets/*.js", lambda route: route.abort())
    page.goto(env.base + "/")
    page.wait_for_timeout(200)
    r.check("g", "첫 그림 전에 적용: 스크립트 없이도 저장된 Dark 바탕", page.evaluate("document.documentElement.dataset.theme") == "dark" and close(pixel_at(page, 700, 300), PALETTE["dark"]["bg"]))
    ctx.close()


def _colors(env: Env):
    r = env.report
    for scheme, pal in PALETTE.items():
        ctx, page, _ = env.open(1440, 2200, scheme)
        row(page, "soulstream-event-ingress-dispatcher").locator(".r-toggle").click()
        page.wait_for_timeout(100)
        marker = lambda sel, dx=12: (lambda b: pixel_at(page, b["x"] + dx, b["y"] + b["height"] / 2))(page.locator(sel).first.bounding_box())  # noqa: E731
        r.check("g", f"{scheme}: Running은 채운 초록 네모", close(marker("#sec-services .st-ok"), pal["accent"]))
        r.check("g", f"{scheme}: Pending(Crash restarts queued)은 빈 ink-2 테두리 네모", close(marker(".side-restart .st-wait", 1), pal["ink2"]) and close(marker(".side-restart .st-wait", 4), pal["bg"]))
        r.check("g", f"{scheme}: Stopped는 채운 ink-3 네모", close(marker("#sec-services .st-off"), pal["ink3"]))
        fail = page.locator("#sec-services .st-fail").first.bounding_box()
        r.check("g", f"{scheme}: Crashed는 먹색 바탕 칩", close(pixel_at(page, fail["x"] + fail["width"] - 3, fail["y"] + 3), pal["ink"]))
        r.check("g", f"{scheme}: Starting은 차오르는 애니메이션 네모", page.evaluate("getComputedStyle(document.querySelector('.st-busy'), '::before').animationName") == "busy-fill")
        n = off_palette_pixels(shot(page))
        r.check("g", f"{scheme}: 빨강, 노랑, 파랑 화소 없음(초록과 먹색만)", n <= 20, f"{n}px")
        low = [(s, round(c, 1)) for s in TEXTS if (c := text_contrast(page, s)) is not None and c < 4.5]
        r.check("g", f"{scheme}: 글자 대비 4.5 이상(캡처 화소로 측정)", not low, str(low))
        ctx.close()


def _fonts_and_media(env: Env):
    r = env.report
    ctx, page, _ = env.open(1440, 900, goto=False)
    urls = []
    page.on("request", lambda req: urls.append(req.url))
    page.goto(env.base + "/")
    page.wait_for_selector(".row")
    page.wait_for_timeout(300)
    external = [u for u in urls if not u.startswith(env.base) and not u.startswith("ws://") and not u.startswith("data:")]
    r.check("g", "글꼴과 자원을 외부 CDN에서 받지 않는다", not external, str(external[:3]))
    fonts = page.evaluate("document.fonts.check('16px \"Pretendard Variable\"') && document.fonts.check('13px \"IBM Plex Mono\"')")
    r.check("g", "Pretendard Variable과 IBM Plex Mono가 번들에서 올라온다", fonts)
    r.check("g", "숫자와 커밋은 IBM Plex Mono", "IBM Plex Mono" in page.evaluate("getComputedStyle(document.querySelector('.m-num')).fontFamily"))
    ctx.close()

    ctx, page, _ = env.open(1440, 900, goto=False)
    page.emulate_media(reduced_motion="reduce", forced_colors="active", contrast="more")
    page.goto(env.base + "/")
    page.wait_for_selector(".row")
    r.check("g", "reduced-motion: 차오름 애니메이션이 멈춘다", page.evaluate("getComputedStyle(document.querySelector('.st-busy'), '::before').animationName") == "none")
    r.check("g", "forced-colors: 실패 칩에 1px 테두리가 생긴다", page.evaluate("getComputedStyle(document.querySelector('.st-fail')).borderTopWidth") == "1px")
    ctx.close()
    ctx, page, _ = env.open(1440, 900, goto=False)
    page.emulate_media(contrast="more")
    page.goto(env.base + "/")
    page.wait_for_selector(".row")
    r.check("g", "prefers-contrast: 괘선이 ink로 진해진다", page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--rule').trim() === getComputedStyle(document.documentElement).getPropertyValue('--rule-strong').trim()"))
    ctx.close()


def _login(env: Env):
    r = env.report
    for scheme in ("light", "dark"):
        for width, height in ((1440, 900), (390, 844)):
            ctx = env.browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
            page = ctx.new_page()
            page.goto(env.base + "/auth/login")
            page.wait_for_selector("#login-form")
            env.png(page, f"login-{scheme}-{width}")
            audit_common(env, page, f"login {scheme} {width}")
            if width == 390:
                r.check("16", f"로그인 390px 넘침 없음({scheme})", not (bad := page.evaluate(OVERFLOW_AUDIT.replace(".content", ".login"))), str(bad))
            c = text_contrast(page, ".field-label")
            r.check("g", f"로그인 글자 대비 4.5 이상({scheme})", c is not None and c >= 4.5, str(c))
            ctx.close()
    ctx = env.browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(env.base + "/auth/login")
    page.fill("#token", "  secret-token  ")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(env.base + "/")
    r.check("g", "로그인: 토큰을 저장하고 대시보드로 돌아간다", page.evaluate("localStorage.getItem('haniel-token')") == "secret-token")
    ctx.close()
    ctx = env.browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.add_init_script("Storage.prototype.setItem = () => { throw new Error('denied') }")
    page.goto(env.base + "/auth/login")
    page.fill("#token", "x")
    page.get_by_role("button", name="Sign in").click()
    r.check("17", "로그인: 저장소를 못 쓰면 그 자리에 오류가 보인다", page.locator("#login-error").is_visible())
    env.png(page, "login-error-light-390")
    ctx.close()


def run(env: Env) -> None:
    _geometry(env)
    _theme(env)
    _colors(env)
    _fonts_and_media(env)
    _login(env)
