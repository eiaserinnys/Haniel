"""Operation measurements for ui-design section 0, items 14 to 18."""

DIALOG_TAB_JS = "() => !!document.activeElement.closest('[role=dialog]')"
IS_ACTIVE_JS = "(el) => el === document.activeElement"


def _focused_in_dialog(p):
    return p.evaluate(DIALOG_TAB_JS)


def dialog_ops(S, rep, scene, post):
    """Reject or Postpone dialog: focus, Tab trap, Enter, Esc, return focus."""
    p, api = S.page, S.api
    name = "Postpone" if post else "Reject"
    opener = p.get_by_role("button", name=name, exact=True).first
    # a selected checkbox and the scroll position stand in for "the outside screen is unchanged"
    p.locator(".row .check:not(:disabled)").first.check()
    opener.scroll_into_view_if_needed()  # the press itself scrolls a button that starts below the fold; measure after that
    before = p.evaluate("() => [document.querySelectorAll('.check:checked').length, document.querySelector('.content').scrollTop]")
    opener.click()
    p.wait_for_selector("[role=dialog]")
    S.settle(300)
    row = {"target": f"{name} dialog", "open": f"row {name} button"}
    first = S.active()
    row["first"] = first
    if not first.startswith("textarea"):
        rep.fail(scene, f"{name} dialog first focus is {first}, want the reason textarea")
    trapped = True
    for key in ["Tab"] * 8 + ["Shift+Tab"] * 3:
        p.keyboard.press(key)
        p.wait_for_timeout(60)  # the focus guards hand focus back on the next frame
        trapped = trapped and _focused_in_dialog(p)
    if not trapped:
        rep.fail(scene, f"{name} dialog: focus left the dialog while tabbing")
    p.locator("[role=dialog] textarea").click()
    p.keyboard.type("x")
    p.keyboard.press("Enter")
    value = p.locator("[role=dialog] textarea").input_value()
    still_open = p.locator("[role=dialog]").count() == 1
    if value != "x\n" or not still_open:
        rep.fail(scene, f"{name} dialog: Enter in the textarea gave {value!r}, open={still_open}")
    row["enter"] = "textarea: new line; confirm button: submits"
    p.keyboard.press("Escape")
    S.settle(250)
    closed = p.locator("[role=dialog]").count() == 0
    returned = opener.evaluate(IS_ACTIVE_JS)
    after = p.evaluate("() => [document.querySelectorAll('.check:checked').length, document.querySelector('.content').scrollTop]")
    if not closed or not returned or after != before:
        rep.fail(scene, f"{name} dialog Esc: closed={closed} focus returned={returned} outside {before}->{after}")
    row["esc"] = "closes, outside screen unchanged"
    row["after"] = f"back on the {name} button"
    # Enter on the confirm button submits
    opener.click()
    p.wait_for_selector("[role=dialog]")
    S.settle(250)
    p.locator("[role=dialog] .preset").first.click()
    p.locator("[role=dialog] .btn-danger").focus()
    p.keyboard.press("Enter")
    S.settle(300)
    posted = any(c[0] == "reject" for c in api.calls)
    if not posted or p.locator("[role=dialog]").count() != 0:
        rep.fail(scene, f"{name} dialog: Enter on the confirm button posted={posted}")
    return row


def batch_dialog_ops(S, rep, scene):
    """Approve N selected asks first: focus starts on Cancel, Tab stays inside, Esc cancels, Approve sends the batch."""
    p, api = S.page, S.api
    p.get_by_text("Select all").click()
    opener = p.locator(".page-tools .btn-primary")
    opener.click()
    p.wait_for_selector("[role=dialog]")
    S.settle(300)
    row = {"target": "Batch approve confirmation", "open": "Approve N selected button"}
    first = S.active()
    row["first"] = first
    if "Cancel" not in first:
        rep.fail(scene, f"batch dialog first focus is {first}, want the Cancel button")
    targets = p.locator("[role=dialog] .targets").first.locator("li").count()
    trapped = True
    for key in ["Tab"] * 6 + ["Shift+Tab"] * 3:
        p.keyboard.press(key)
        p.wait_for_timeout(60)
        trapped = trapped and _focused_in_dialog(p)
    if not trapped:
        rep.fail(scene, "batch dialog: focus left the dialog while tabbing")
    before = p.evaluate("() => document.querySelectorAll('.check:checked').length")
    p.keyboard.press("Escape")
    S.settle(250)
    after = p.evaluate("() => document.querySelectorAll('.check:checked').length")
    if p.locator("[role=dialog]").count() != 0 or not opener.evaluate(IS_ACTIVE_JS) or after != before or api.calls:
        rep.fail(scene, f"batch dialog Esc: open={p.locator('[role=dialog]').count()} focus={S.active()} selection {before}->{after} calls={api.calls}")
    row["esc"] = "cancels: nothing is sent, selection kept"
    row["after"] = "back on the Approve N selected button"
    opener.click()
    p.wait_for_selector("[role=dialog]")
    S.settle(250)
    p.locator("[role=dialog] .dialog-foot .btn-primary").click()
    S.settle(400)
    sent = [c for c in api.calls if c[0] == "approve"]
    if len(sent) != targets or p.locator("[role=dialog]").count() != 0:
        rep.fail(scene, f"batch dialog Approve sent {len(sent)} approvals for {targets} listed targets, open={p.locator('[role=dialog]').count()}")
    row["enter"] = "Approve: sends one approval per listed target (requests that need a look stay out)"
    return row


def drawer_ops(S, rep, scene):
    p = S.page
    opener = p.get_by_role("button", name="Open menu")
    opener.click()
    p.wait_for_selector(".drawer")
    S.settle(300)
    row = {"target": "Menu drawer (phone)", "open": "menu button in the header", "first": S.active()}
    trapped = True
    for _ in range(6):
        p.keyboard.press("Tab")
        p.wait_for_timeout(60)
        trapped = trapped and p.evaluate("() => !!document.activeElement.closest('.drawer')")
    if not trapped:
        rep.fail(scene, "drawer: focus left the drawer while tabbing")
    p.keyboard.press("Escape")
    S.settle(250)
    if p.locator(".drawer").count() != 0 or not opener.evaluate(IS_ACTIVE_JS):
        rep.fail(scene, f"drawer Esc: open={p.locator('.drawer').count()} focus={S.active()}")
    opener.click()
    p.wait_for_selector(".drawer")
    S.settle(250)
    p.locator(".drawer .nav-row", has_text="Nodes").focus()
    p.keyboard.press("Enter")
    S.settle(300)
    navigated = p.locator(".page-title").inner_text() == "Nodes" and p.locator(".drawer").count() == 0
    if not navigated:
        rep.fail(scene, "drawer: Enter on a page row did not navigate and close")
    row.update(enter="page row: opens the page and closes the drawer", esc="closes", after="back on the menu button")
    return row


def theme_menu_ops(S, rep, scene):
    p = S.page
    trigger = p.locator("button[aria-label^='Theme']")
    trigger.click()
    p.wait_for_selector("[role=menu]")
    S.settle(250)
    row = {"target": "Theme menu", "open": "theme button in the header", "first": S.active()}
    if p.locator("[role=menu]").count() != 1:
        rep.fail(scene, "theme menu did not open")
    p.keyboard.press("Escape")
    S.settle(250)
    if p.locator("[role=menu]").count() != 0 or not trigger.evaluate(IS_ACTIVE_JS):
        rep.fail(scene, f"theme menu Esc: menu={p.locator('[role=menu]').count()} focus={S.active()}")
    trigger.click()
    p.wait_for_selector("[role=menu]")
    S.settle(200)
    p.keyboard.press("ArrowDown")
    p.keyboard.press("Enter")
    S.settle(250)
    stored = p.evaluate("() => [localStorage.getItem('haniel-theme'), document.documentElement.getAttribute('data-theme')]")
    if p.locator("[role=menu]").count() != 0 or stored[0] is None and stored[1] is not None:
        rep.fail(scene, f"theme menu Enter: menu={p.locator('[role=menu]').count()} stored={stored}")
    # restore the theme the session started with
    trigger.click()
    p.wait_for_selector("[role=menu]")
    p.locator("[role=menuitemradio]", has_text=S.theme.capitalize()).click()
    S.settle(250)
    if p.evaluate("() => document.documentElement.getAttribute('data-theme')") != S.theme:
        rep.fail(scene, "theme menu: could not restore the theme")
    row.update(enter="item: applies the theme and closes", esc="closes", after="back on the theme button")
    return row


def theme_menu_keyboard_ops(S, rep, scene):
    """Opened from the keyboard, the first focus is the first item, Light; a mouse open focuses the menu box."""
    p = S.page
    trigger = p.locator("button[aria-label^='Theme']")
    firsts = []
    for key in ("Enter", "Space", "ArrowDown"):
        trigger.focus()
        p.keyboard.press(key)
        p.wait_for_selector("[role=menu]")
        S.settle(250)
        got = p.evaluate("() => { const a = document.activeElement; return [a.getAttribute('role'), a.textContent.trim()]; }")
        firsts.append(got[1] if got[0] == "menuitemradio" else f"{got[0]}")
        if got != ["menuitemradio", "Light"]:
            rep.fail(scene, f"theme menu opened with {key}: first focus {got}, want the Light item")
        p.keyboard.press("Escape")
        S.settle(250)
    return {"target": "Theme menu, from the keyboard", "open": "Enter, Space, or Down arrow on the theme button",
            "first": f"first item: {firsts[0]} (same for all three keys)", "enter": "item: applies the theme and closes",
            "esc": "closes", "after": "back on the theme button"}


def filter_ops(S, rep, scene):
    p = S.page
    box = p.get_by_placeholder("Filter nodes")
    box.click()
    p.keyboard.type("zz")
    p.keyboard.press("Enter")
    kept = box.input_value() == "zz" and p.locator(".page-title").count() == 1
    p.keyboard.press("Escape")
    cleared = box.input_value() == ""
    if not (kept and cleared and box.evaluate(IS_ACTIVE_JS)):
        rep.fail(scene, f"filter input: Enter kept={kept}, Esc cleared={cleared}")
    return {"target": "Nodes filter (in-line edit)", "open": "click the field", "first": "the field itself",
            "enter": "no submit, text stays", "esc": "clears the text", "after": "stays in the field"}


def tab_order(S, rep, scene):
    """Tab walks the DOM order, never lands on a hidden or covered element."""
    p = S.page
    p.evaluate("() => { document.activeElement.blur(); window.scrollTo(0, 0); }")
    p.mouse.click(2, 2)
    last = -1
    steps = 0
    for _ in range(26):
        p.keyboard.press("Tab")
        info = p.evaluate(
            """() => { const a = document.activeElement; if (a === document.body) return null;
              const r = a.getBoundingClientRect(); const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
              return { idx: Array.from(document.querySelectorAll('*')).indexOf(a), size: r.width * r.height,
                       covered: !(hit && (a === hit || a.contains(hit) || hit.contains(a))), tabindex: a.tabIndex,
                       name: (a.getAttribute('aria-label') || a.textContent || '').trim().slice(0, 20) }; }"""
        )
        if info is None:
            break
        steps += 1
        if info["size"] == 0 or info["covered"] or info["tabindex"] > 0:
            rep.fail(scene, f"Tab reached a hidden or covered element: {info}")
        if info["idx"] < last:
            if steps < 8:
                rep.fail(scene, f"Tab order jumped back at {info['name']!r}")
            break
        last = info["idx"]
    if steps < 6:
        rep.fail(scene, f"Tab walk reached only {steps} controls")


def error_retry(S, rep, scene, page_name):
    """The error state keeps the frame, offers Retry, and Retry loads the data."""
    p, api = S.page, S.api
    alert = p.locator("[role=alert]")
    if alert.count() != 1 or p.get_by_role("button", name="Retry").count() != 1:
        rep.fail(scene, f"{page_name}: no in-place error with Retry")
        return
    if p.locator(".topbar").count() != 1 or p.locator(".page-title").count() != 1:
        rep.fail(scene, f"{page_name}: error replaced the frame")
    for ep in api.mode:
        api.mode[ep] = "ok"
    p.get_by_role("button", name="Retry").click()
    p.wait_for_selector("[role=alert]", state="detached", timeout=5000)
    try:
        p.wait_for_selector(".row", timeout=5000)
    except Exception:
        rep.fail(scene, f"{page_name}: Retry did not load rows")


def resize_ops(S, rep, scene):
    """Window width changes keep layout, input, expansion, scroll, and drop stale overlays."""
    p = S.page
    p.get_by_placeholder("Filter nodes").fill("e")
    p.locator(".r-toggle").first.click()
    S.settle(200)
    p.evaluate("() => { document.querySelector('.content').scrollTop = 120; }")
    scroll = p.evaluate("() => document.querySelector('.content').scrollTop")
    if scroll <= 0:
        rep.fail(scene, "resize: nothing to scroll, the measurement is empty")
    p.set_viewport_size({"width": 390, "height": 844})
    S.settle(250)
    narrow = p.evaluate(
        """() => ({ side: !!document.querySelector('.sidebar'), tab: getComputedStyle(document.querySelector('.tabbar')).display,
                    filter: document.querySelector('input[type=search]').value,
                    open: document.querySelector('.r-toggle').getAttribute('aria-expanded'),
                    list: getComputedStyle(document.querySelector('.svc-list')).display })"""
    )
    if narrow["side"] or narrow["tab"] == "none" or narrow["filter"] != "e" or narrow["open"] != "true" or narrow["list"] != "block":
        rep.fail(scene, f"resize to 390: {narrow}")
    p.set_viewport_size({"width": 1440, "height": 700})
    S.settle(250)
    wide = p.evaluate(
        """() => ({ side: !!document.querySelector('.sidebar'), filter: document.querySelector('input[type=search]').value,
                    open: document.querySelector('.r-toggle').getAttribute('aria-expanded'),
                    scroll: document.querySelector('.content').scrollTop })"""
    )
    if not wide["side"] or wide["filter"] != "e" or wide["open"] != "true" or abs(wide["scroll"] - scroll) > 1:
        rep.fail(scene, f"resize back to 1440: {wide} (scroll was {scroll})")
    # an open theme menu is placed inside the viewport after the resize
    p.locator("button[aria-label^='Theme']").click()
    p.wait_for_selector("[role=menu]")
    p.set_viewport_size({"width": 390, "height": 844})
    S.settle(300)
    rect = p.evaluate("() => { const m = document.querySelector('[role=menu]'); if (!m) return null; const r = m.getBoundingClientRect(); return [r.left, r.right, r.top, r.bottom]; }")
    if rect and (rect[0] < -1 or rect[1] > 391 or rect[2] < -1 or rect[3] > 845):
        rep.fail(scene, f"theme menu outside the viewport after resize: {rect}")
    p.keyboard.press("Escape")
    # an open drawer disappears when the window grows past the phone width
    S.settle(200)
    p.get_by_role("button", name="Open menu").click()
    p.wait_for_selector(".drawer")
    p.set_viewport_size({"width": 1440, "height": 700})
    S.settle(300)
    if p.locator(".drawer").count() or p.locator(".scrim").count():
        rep.fail(scene, "drawer or scrim left behind after growing the window")
    # a dialog turns into a bottom sheet on the phone width and keeps its text
    S.go("pending")
    p.get_by_role("button", name="Reject", exact=True).first.click()
    p.wait_for_selector("[role=dialog]")
    p.locator("[role=dialog] textarea").fill("keep this text")
    p.set_viewport_size({"width": 390, "height": 844})
    S.settle(300)
    sheet = p.evaluate(
        """() => { const d = document.querySelector('[role=dialog]'); const r = d.getBoundingClientRect();
          return { w: r.width, bottom: r.bottom, text: d.querySelector('textarea').value }; }"""
    )
    if abs(sheet["w"] - 390) > 1 or abs(sheet["bottom"] - 844) > 1 or sheet["text"] != "keep this text":
        rep.fail(scene, f"dialog after resize to phone width: {sheet}")
    p.keyboard.press("Escape")
    p.set_viewport_size({"width": 1440, "height": 900})
    S.settle(200)
