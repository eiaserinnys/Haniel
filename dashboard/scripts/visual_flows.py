"""Operation measurements for ui-design 0-section items 14 to 18 on the local dashboard."""

from __future__ import annotations

import visual_mock as vm
from visual_checks import DESCRIBE, OVERFLOW_AUDIT, Env, active, label, row

DISPATCHER = "soulstream-event-ingress-dispatcher"
STATE = "() => ({ top: document.querySelector('.content').scrollTop, open: document.querySelectorAll('[aria-expanded=true]').length })"
INSIDE = "(root) => root.contains(document.activeElement)"
SCROLL = "() => { const c = document.querySelector('.content'); return { top: c.scrollTop, max: c.scrollHeight - c.clientHeight }; }"


def _is_focused(el) -> bool:
    return el.evaluate("(e) => e === document.activeElement")


def dialog_flow(env: Env, target, open_how, opener_of, enter, *, width=1440, root=".dialog", mock_setup=None, scheme="light"):
    """Open, Tab-trap, Enter, Esc and focus return for one layer. Records items 14 and 15 and the dialog table row."""
    r = env.report
    ctx, page, mock = env.open(width, 844 if width < 800 else 900, scheme)
    if mock_setup:
        mock_setup(mock)
    opener = opener_of(page)
    opener.scroll_into_view_if_needed()
    before = page.evaluate(STATE)
    opener.click()
    layer = page.locator(root)
    layer.wait_for(state="visible")
    first = active(page)
    r.check("14", f"{target}: 열면 초점이 안쪽 첫 동작에 있다", layer.evaluate(INSIDE), first)

    trapped = True
    for key in ["Tab"] * 12 + ["Shift+Tab"] * 4:
        page.keyboard.press(key)
        trapped = trapped and layer.evaluate(INSIDE)
    r.check("15", f"{target}: Tab이 안쪽에서만 돈다", trapped)
    hidden = page.evaluate("() => { const e = document.activeElement; const r = e.getBoundingClientRect(); return !(r.width && r.height); }")
    r.check("15", f"{target}: 초점이 보이지 않는 요소에 가지 않는다", not hidden)

    enter_note = enter(page, mock, layer)
    if layer.count():
        page.keyboard.press("Escape")
    opener2 = opener_of(page)
    if not layer.count() and enter_note.startswith("닫힘"):
        opener2.click()
        layer.wait_for(state="visible")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    r.check("15", f"{target}: Esc로 닫힌다", layer.count() == 0)
    r.check("14", f"{target}: 닫으면 연 요소로 초점이 돌아온다", _is_focused(opener_of(page)), active(page))
    r.check("15", f"{target}: Esc 뒤 바깥 화면이 그대로다", page.evaluate(STATE) == before, f"{before} -> {page.evaluate(STATE)}")
    env.report.dialogs.append(
        {"target": target, "open": open_how, "first": first, "enter": enter_note, "esc": "닫힘, 바깥 화면 그대로", "after": active(page)}
    )
    ctx.close()


def _enter_editor(field_label, sent):
    def run(page, mock, layer):
        field = layer.get_by_label(field_label, exact=False).first
        field.fill("echo enter-test")
        n = len(mock.calls)
        field.press("Enter")
        page.wait_for_timeout(300)
        hit = [c for c in mock.calls[n:] if c in sent]
        return ("닫힘, " if not layer.count() else "열림, ") + (f"{hit[0][0]} 전송" if hit else "전송 없음")

    return run


def _enter_add_service(page, mock, layer):
    layer.get_by_label("Name", exact=False).first.fill("probe")
    return _enter_editor("Run command", [("POST", "/api/config/services")])(page, mock, layer)


def _enter_confirm(call):
    def run(page, mock, layer):
        n = len(mock.calls)
        page.keyboard.press("Enter")  # focus starts on Cancel
        page.wait_for_timeout(200)
        sent = [c for c in mock.calls[n:] if c == call]
        return ("닫힘, " if not layer.count() else "열림, ") + ("취소가 아닌 전송 발생" if sent else "첫 초점 Cancel이라 취소, 전송 없음")

    return run


def _enter_none(page, mock, layer):
    return "해당 없음"


def _dialogs(env: Env):
    plain = lambda sel: (lambda page: page.locator(sel))  # noqa: E731
    dialog_flow(
        env, "서비스 추가", "Add service 단추", plain("#sec-services .sect-head .btn"),
        _enter_add_service,
    )
    dialog_flow(
        env, "서비스 편집", "행의 연필 단추", lambda page: label(page, f"Edit {DISPATCHER}"),
        _enter_editor("Run command", [("PUT", f"/api/config/services/{DISPATCHER}")]),
    )
    dialog_flow(env, "리포 편집", "독립 리포 행의 연필 단추", lambda page: label(page, "Edit eb_lore"), _enter_editor("Branch", [("PUT", "/api/config/repos/eb_lore")]))
    dialog_flow(
        env, "서비스 삭제 확인", "행의 휴지통 단추", lambda page: label(page, f"Delete {DISPATCHER}"),
        _enter_confirm(("DELETE", f"/api/config/services/{DISPATCHER}")), root=".dialog.is-confirm",
    )
    dialog_flow(
        env, "리포 삭제 확인", "독립 리포 행의 휴지통 단추", lambda page: label(page, "Delete eb_lore"),
        _enter_confirm(("DELETE", "/api/config/repos/eb_lore")), root=".dialog.is-confirm", scheme="dark",
    )
    dialog_flow(
        env, "Haniel 재시작 확인", "self 행의 재시작 단추", lambda page: label(page, "Restart Haniel"),
        _enter_confirm(("POST", "/api/self/restart")), root=".dialog.is-confirm",
    )
    dialog_flow(
        env, "자기 갱신 승인 확인", "self 행의 Approve 단추", lambda page: page.get_by_role("button", name="Approve", exact=True),
        _enter_confirm(("POST", "/api/self-update/approve")), root=".dialog.is-confirm",
    )
    dialog_flow(env, "서랍 (휴대폰 폭)", "머리줄 메뉴 단추", lambda page: page.get_by_label("Open menu"), _enter_none, width=390, root=".drawer")
    dialog_flow(env, "편집 대화상자 (휴대폰 폭, 아래 시트)", "행의 연필 단추", lambda page: label(page, f"Edit {DISPATCHER}"), _enter_editor("Run command", [("PUT", f"/api/config/services/{DISPATCHER}")]), width=390)


def _theme_menu_and_overlay(env: Env):
    r = env.report
    ctx, page, _ = env.open(1440, 900)
    btn = page.get_by_label("Theme:", exact=False)
    btn.click()
    first = active(page)
    r.check("14", "테마 메뉴: 열면 지금 고른 항목에 초점", page.evaluate("document.activeElement.getAttribute('aria-checked')") == "true", first)
    page.keyboard.press("ArrowUp")
    page.keyboard.press("Enter")
    page.wait_for_timeout(100)
    chosen = page.evaluate("document.documentElement.dataset.theme")
    r.check("14", "테마 메뉴: 항목을 고르면 닫히고 단추로 초점이 돌아온다", page.locator(".menu").count() == 0 and _is_focused(btn), active(page))
    r.check("15", "테마 메뉴: 방향키와 Enter로 고른다", chosen == "dark", f"data-theme={chosen}")
    btn.click()
    page.keyboard.press("Escape")
    r.check("15", "테마 메뉴: Esc로 닫히고 초점이 단추로", page.locator(".menu").count() == 0 and _is_focused(btn))
    env.report.dialogs.append({"target": "테마 메뉴", "open": "머리줄 테마 단추", "first": first, "enter": "방향키로 옮겨 Enter, 고른 테마 적용", "esc": "닫힘", "after": active(page)})
    ctx.close()

    ctx, page, mock = env.open(1440, 900, clock=True)
    approve = page.get_by_role("button", name="Approve", exact=True)
    approve.click()
    page.locator(".dialog.is-confirm").get_by_role("button", name="Approve", exact=True).click()
    page.wait_for_selector(".status-panel")
    page.keyboard.press("Escape")
    r.check("15", "업데이트 덮개: 진행 중에는 Esc로 닫히지 않는다(화면을 막는 상태)", page.locator(".status-panel").count() == 1)
    page.clock.fast_forward(120_000)
    page.wait_for_selector("text=Server not responding")
    first = active(page)
    r.check("14", "업데이트 덮개(무응답): 열리면 Refresh 단추에 초점", "Refresh" in first, first)
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    r.check("15", "업데이트 덮개(무응답): Esc로 닫힌다", page.locator(".status-panel").count() == 0)
    r.check("14", "업데이트 덮개: 닫으면 Approve 단추로 초점", _is_focused(approve), active(page))
    env.report.dialogs.append({"target": "업데이트 덮개(무응답)", "open": "Approve, 확인창의 Approve", "first": first, "enter": "Refresh는 페이지 새로고침", "esc": "닫힘", "after": active(page)})
    ctx.close()


def _inline_focus(env: Env):
    r = env.report
    ctx, page, _ = env.open(1440, 900)
    toggle = row(page, DISPATCHER).locator(".r-toggle")
    toggle.focus()
    page.keyboard.press("Enter")
    r.check("14", "행 펼침 뒤에도 초점이 그 행 단추에 남는다", toggle.get_attribute("aria-expanded") == "true" and _is_focused(toggle))
    logs = label(page, f"Logs for {DISPATCHER}")
    logs.click()
    r.check("14", "로그 단추를 눌러도 초점이 그 단추에 남는다", _is_focused(logs) and logs.get_attribute("aria-pressed") == "true")
    fetch = page.get_by_role("button", name="Fetch now")
    fetch.click()
    page.wait_for_timeout(200)
    r.check("14", "Fetch now 뒤에도 초점이 단추에 남는다", _is_focused(fetch))
    reload_btn = label(page, "Reload config")
    reload_btn.click()
    page.wait_for_timeout(200)
    r.check("14", "Reload config 뒤에도 초점이 단추에 남는다", _is_focused(reload_btn))
    ctx.close()

    # Tab order: DOM order only, nothing hidden is reachable, phone width skips the hidden sidebar.
    for width in (1440, 390):
        ctx, page, _ = env.open(width, 900)
        bad = page.evaluate("() => [...document.querySelectorAll('[tabindex]')].filter((e) => Number(e.getAttribute('tabindex')) > 0).length")
        seen_hidden, in_sidebar = [], 0
        for _ in range(60):
            page.keyboard.press("Tab")
            info = page.evaluate(
                "() => { const e = document.activeElement; const r = e.getBoundingClientRect(); const s = getComputedStyle(e);"
                " return [!!(r.width && r.height) && s.visibility !== 'hidden', !!e.closest('.sidebar')]; }"
            )
            if not info[0]:
                seen_hidden.append(page.evaluate(f"() => ({DESCRIBE})(document.activeElement)"))
            in_sidebar += info[1]
        r.check("15", f"{width}px Tab 60회: 보이지 않는 요소에 초점이 가지 않고 양수 tabindex가 없다", not seen_hidden and not bad, f"{seen_hidden[:3]}")
        if width == 390:
            r.check("15", "390px Tab: 숨긴 사이드바로 초점이 들어가지 않는다", in_sidebar == 0, f"in_sidebar={in_sidebar}")
        ctx.close()


def _errors(env: Env):
    r = env.report
    # load failure: in-place error with Retry, then recovery
    mock = vm.Mock()
    mock.ws_mode = "drop"
    mock.overrides[("GET", "/api/status")] = (500, "upstream down")
    ctx, page, mock = env.open(1440, 900, mock=mock, wait="#sec-services [role=alert]")
    r.check("17", "처음 불러오기 실패: 같은 틀 안에서 Retry가 있는 실패 안내", page.get_by_role("button", name="Retry").count() == 1 and page.locator(".shell").count() == 1)
    r.check("17", "처음 불러오기 실패: 연결 칩이 Reconnecting…", "Reconnecting" in page.locator(".conn").inner_text())
    mock.overrides.clear()
    page.get_by_role("button", name="Retry").click()
    page.wait_for_selector("#sec-services .row")
    r.check("17", "Retry를 누르면 같은 자리에서 내용이 돌아온다", True)
    ctx.close()

    ctx, page, mock = env.open(1440, 900)
    mock.overrides[("POST", "/api/services/serendipity-api/stop")] = (500, "process did not exit")
    label(page, "Stop serendipity-api").click()
    page.wait_for_selector(".content-inner > .notice")
    shown = "500" in page.locator(".content-inner > .notice").inner_text()
    label(page, "Dismiss").click()
    mock.overrides.clear()
    label(page, "Stop serendipity-api").click()
    page.wait_for_timeout(200)
    r.check("17", "서비스 동작 실패: 위에 오류가 보이고 닫은 뒤 같은 단추로 다시 할 수 있다", shown and page.locator(".content-inner > .notice").count() == 0 and ("POST", "/api/services/serendipity-api/stop") in mock.calls)

    mock.overrides[("GET", "/api/services/soulstream-builder/logs")] = (404, "not found")
    label(page, "Logs for soulstream-builder").click()
    page.wait_for_selector(".logs .notice")
    mock.overrides.clear()
    page.locator(".logs .notice").get_by_role("button", name="Retry").click()
    page.wait_for_selector(".log-pane")
    r.check("17", "로그 실패: 로그 자리에서 안내와 Retry, 다시 받으면 로그가 나온다", True)

    mock.overrides[("PUT", f"/api/config/services/{DISPATCHER}")] = (422, "ready: expected port:N")
    label(page, f"Edit {DISPATCHER}").click()
    page.get_by_role("button", name="Save").click()
    page.wait_for_selector(".dialog .notice")
    still_open = page.locator(".dialog").count() == 1
    mock.overrides.clear()
    page.get_by_role("button", name="Save").click()
    page.wait_for_timeout(300)
    r.check("17", "저장 실패: 대화상자 안에 오류가 남고 같은 단추로 다시 저장해 닫힌다", still_open and page.locator(".dialog").count() == 0)

    mock.overrides[("DELETE", f"/api/config/services/{DISPATCHER}")] = (500, "locked")
    label(page, f"Delete {DISPATCHER}").click()
    page.get_by_role("button", name="Delete", exact=True).click()
    page.wait_for_selector(".dialog .notice")
    mock.overrides.clear()
    page.get_by_role("button", name="Delete", exact=True).click()
    page.wait_for_timeout(300)
    r.check("17", "삭제 실패: 확인창 안에 오류, 다시 눌러 삭제", page.locator(".dialog").count() == 0)

    mock.overrides[("POST", "/api/reload")] = (500, "reload failed")
    label(page, "Reload config").click()
    page.wait_for_selector(".content-inner > .notice")
    r.check("17", "설정 다시 읽기 실패: 위에 오류가 보인다", True)
    mock.overrides[("POST", "/api/reload")] = (401, "")
    page.evaluate("localStorage.setItem('haniel-token', 'stale')")
    label(page, "Reload config").click()
    page.wait_for_url("**/auth/login")
    r.check("17", "401이면 토큰을 지우고 로그인으로 간다", page.evaluate("localStorage.getItem('haniel-token')") is None)
    ctx.close()

    mock = vm.Mock()
    mock.ws_mode = "init-then-drop"
    ctx, page, mock = env.open(1440, 900, mock=mock)
    page.wait_for_timeout(300)
    r.check("17", "/ws가 끊기면 칩이 Reconnecting…, 화면 내용은 그대로", "Reconnecting" in page.locator(".conn").inner_text() and page.locator("#sec-services .row").count() > 3)
    ctx.close()


def _resize(env: Env):
    r = env.report
    ctx, page, mock = env.open(1440, 900)
    page.evaluate("document.querySelector('.content').scrollTop = 700")
    label(page, f"Edit {DISPATCHER}").click()
    field = page.get_by_label("Run command", exact=False).first
    field.fill("echo typed-before-resize")
    before = page.evaluate(SCROLL)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(250)
    sheet = page.locator(".dialog").bounding_box()
    r.check("18", "넓은→좁은: 대화상자가 아래 시트 모양으로 다시 선다", sheet and sheet["width"] == 390 and abs(sheet["y"] + sheet["height"] - 844) <= 1, str(sheet))
    r.check("18", "넓은→좁은: 입력 중인 글이 유지된다", field.input_value() == "echo typed-before-resize")
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    r.check("18", "넓은→좁은: 사이드바가 숨고 메뉴 단추가 나온다", not page.locator(".sidebar").is_visible() and page.get_by_label("Open menu").is_visible())
    after = page.evaluate(SCROLL)
    r.check("18", "넓은→좁은: 스크롤 위치가 맨 위로 튀지 않고 그대로다(남은 길이 안에서)", before["top"] > 0 and after["top"] == min(before["top"], after["max"]), f"{before} -> {after}")
    r.check("18", "넓은→좁은: 넘침과 잘림이 없다", not (bad := page.evaluate(OVERFLOW_AUDIT)), str(bad))
    stacked = page.evaluate("() => { const r = document.querySelector('#sec-services .row'); return r.querySelector('.r-metric').getBoundingClientRect().top >= r.querySelector('.r-toggle').getBoundingClientRect().bottom - 1; }")
    r.check("18", "넓은→좁은: 장부 행이 수치와 단추를 셋째 줄로 내린다", stacked)

    page.get_by_label("Open menu").click()
    page.wait_for_selector(".drawer")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(250)
    r.check("18", "좁은→넓은: 열린 서랍이 닫히고 사이드바가 다시 선다", page.locator(".drawer").count() == 0 and page.locator(".sidebar").is_visible())
    r.check("18", "좁은→넓은: 메뉴 단추가 사라진다", not page.get_by_label("Open menu").is_visible())

    page.get_by_label("Theme:", exact=False).click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(250)
    menu = page.locator(".menu")
    box = menu.bounding_box() if menu.count() else None
    r.check("18", "열린 테마 메뉴가 좁은 폭에서도 화면 안에 있다", box is None or (box["x"] >= 0 and box["x"] + box["width"] <= 390), str(box))
    ctx.close()


def _wide_360(env: Env):
    r = env.report
    ctx, page, mock = env.open(360, 800)
    row(page, DISPATCHER).locator(".r-toggle").click()
    row(page, "soulstream-server-src").locator(".r-toggle").click()
    label(page, f"Logs for {DISPATCHER}").click()
    page.wait_for_selector(".log-pane")
    r.check("16", "360px 최악 데이터(긴 이름, 긴 브랜치, 긴 명령): 가로 스크롤과 잘림 없음", not (bad := page.evaluate(OVERFLOW_AUDIT)), str(bad))
    env.png(page, "narrow-360-expanded", "extra")
    label(page, f"Edit {DISPATCHER}").click()
    page.wait_for_selector(".dialog")
    r.check("16", "360px 편집 시트: 넘침 없음", not (bad := page.evaluate(OVERFLOW_AUDIT)), str(bad))
    # The sheet fades in; capture it once the motion is over.
    page.wait_for_function("getComputedStyle(document.querySelector('.dialog')).opacity === '1'")
    page.wait_for_timeout(200)
    r.check("g", "360px 편집 시트는 등장 움직임이 끝난 불투명도 1에서 캡처한다", page.evaluate("getComputedStyle(document.querySelector('.dialog')).opacity") == "1")
    env.png(page, "narrow-360-editor", "extra")
    ctx.close()


STAGES = {
    "dialogs": _dialogs,
    "layers": _theme_menu_and_overlay,
    "focus": _inline_focus,
    "errors": _errors,
    "resize": _resize,
    "narrow360": _wide_360,
}


def run(env: Env, only: list[str] | None = None) -> None:
    for name, stage in STAGES.items():
        if not only or name in only:
            stage(env)
