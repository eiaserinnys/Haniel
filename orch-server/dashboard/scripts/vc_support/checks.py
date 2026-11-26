"""Design measurements for the orchestrator dashboard (card #1884 spec)."""

import re

ACCENT = {"light": "rgb(11, 122, 75)", "dark": "rgb(79, 214, 156)"}
BODY_BG = {"light": "rgb(255, 255, 255)", "dark": "rgb(12, 13, 12)"}

TONE = {
    "ok": ["Running", "Ready", "Connected", "Success"],
    "busy": ["Starting", "Stopping", "Deploying", "Pulling", "Reconnecting…", "Connecting…", "Restarting (self-update)"],
    "wait": ["Pending", "Restart pending", "Manual approval"],
    "off": ["Stopped", "Rejected", "Disabled", "Offline"],
    "fail": ["Crashed", "Circuit open", "Failed", "Disconnected", "Fetch error", "Error"],
}
LABEL_TONE = {label: tone for tone, labels in TONE.items() for label in labels}
STAGES = ["Preparing", "Building", "Checking", "Backing up", "Migrating", "Starting services", "Verifying", "Recovering"]


def tone_of(label):
    """A deploying row with a reported stage reads "Deploying · <stage>" and keeps the Deploying marker."""
    if label.startswith("Deploying · ") and label[len("Deploying · "):] in STAGES:
        return "busy"
    return LABEL_TONE.get(label)

PROBE_JS = """(names) => {
  const probe = document.createElement('i'); document.body.append(probe);
  const out = {};
  for (const n of names) { probe.style.color = `var(--${n})`; out[n] = getComputedStyle(probe).color; }
  probe.remove(); return out;
}"""

FRAME_JS = """() => {
  const q = (s) => document.querySelector(s);
  const cs = (el, k) => getComputedStyle(el)[k];
  const side = q('.sidebar'), tab = q('.tabbar'), bs = q('.brand-sub');
  const loaded = (fam) => Array.from(document.fonts).some((f) => f.family.includes(fam) && f.status === 'loaded');
  return {
    topH: q('.topbar').offsetHeight, topBtn: q('.topbar .icon-btn').offsetHeight,
    brand: q('.brand-name').textContent, sub: bs.textContent, subColor: cs(bs, 'color'), subSize: cs(bs, 'fontSize'),
    sideW: side ? side.offsetWidth : null, navH: side ? q('.sidebar .nav-row').offsetHeight : null,
    tabDisplay: cs(tab, 'display'), tabH: tab.offsetHeight, tabItems: tab.querySelectorAll('.tab-item').length,
    title: cs(q('.page-title'), 'fontSize'), theme: document.documentElement.getAttribute('data-theme'),
    bodyBg: cs(document.body, 'backgroundColor'), bodyFont: cs(document.body, 'fontFamily'),
    meta: !!q('meta[name="haniel-ui"][content="index"]'),
    mono: cs(q('.nav-count') || q('.mono'), 'fontFamily'),
    pretendard: loaded('Pretendard Variable'), plex: loaded('IBM Plex Mono'),
    text: document.body.innerText, bell: !!q('.bell-badge, [aria-label*="otification" i]'),
    navSection: !!q('.nav-section'), conn: q('.conn').textContent.trim(),
  };
}"""


def frame(S, rep, scene):
    p, w = S.page, S.width
    f = p.evaluate(FRAME_JS)
    wide = w > 760
    expect = [
        (f["topH"] == 64, f"topbar height {f['topH']} (want 64)"),
        (f["topBtn"] == 44, f"topbar icon button {f['topBtn']} (want 44)"),
        (f["brand"] == "Haniel" and f["sub"] == "Orchestrator", f"brand {f['brand']!r} {f['sub']!r}"),
        (f["subColor"] == ACCENT[S.theme], f"brand sub color {f['subColor']}"),
        (f["subSize"] == "13px", f"brand sub size {f['subSize']}"),
        (f["theme"] == S.theme, f"data-theme {f['theme']}"),
        (f["bodyBg"] == BODY_BG[S.theme], f"body background {f['bodyBg']}"),
        (f["meta"], 'missing <meta name="haniel-ui" content="index">'),
        ("Pretendard Variable" in f["bodyFont"], f"body font {f['bodyFont']}"),
        ("IBM Plex Mono" in f["mono"], f"mono font {f['mono']}"),
        (f["pretendard"] and f["plex"], f"bundled fonts loaded: pretendard={f['pretendard']} plex={f['plex']}"),
        (not S.external, f"external requests: {S.external}"),
        (f["conn"] in ("Connected", "Reconnecting…", "Disconnected"), f"connection chip {f['conn']!r}"),
        (not f["bell"] and not f["navSection"], "bell or Dashboard section still present"),
    ]
    for gone in ("Orchestrator Dashboard", "/ws/dashboard", "haniel orchestrator", "changes detected", "awaiting approval"):
        expect.append((gone not in f["text"], f"removed text still shown: {gone!r}"))
    if wide:
        expect += [
            (f["sideW"] == 240, f"sidebar width {f['sideW']} (want 240)"),
            (f["navH"] == 40, f"nav row height {f['navH']} (want 40)"),
            (f["tabDisplay"] == "none", "tab bar visible on wide"),
            (f["title"] == "32px", f"page title {f['title']} (want 32px)"),
        ]
    else:
        expect += [
            (f["sideW"] is None, "sidebar rendered on phone width"),
            (f["title"] == "28px", f"page title {f['title']} (want 28px)"),
            (f["tabDisplay"] != "none" and f["tabH"] == 64 and f["tabItems"] == 3, f"tab bar {f['tabDisplay']} h={f['tabH']} n={f['tabItems']}"),
        ]
    for ok, msg in expect:
        if not ok:
            rep.fail(scene, msg)


def pending(S, rep, scene):
    p, w = S.page, S.width
    wide_content = w - 240 >= 720 if w > 760 else False
    m = p.evaluate(
        """() => {
          const q = (s) => document.querySelector(s); const cs = (el, k) => getComputedStyle(el)[k];
          const sel = q('.row .check'); const blocked = Array.from(document.querySelectorAll('.row')).find((r) => r.textContent.includes('Waits for self-update'));
          const apv = Array.from(document.querySelectorAll('.page-tools .btn')).find((b) => b.textContent.startsWith('Approve'));
          return {
            metricW: q('.r-metric').getBoundingClientRect().width, num: cs(q('.m-num'), 'fontSize'),
            btnH: q('.row .btn').offsetHeight, check: [sel.offsetWidth, sel.offsetHeight],
            ledgerTop: cs(q('.ledger'), 'borderTopWidth'), rowBorder: cs(q('.row'), 'borderBottomWidth'),
            rule: cs(q('.ledger'), 'backgroundImage').includes('linear-gradient'),
            accentTop: cs(q('.ledger-accent'), 'borderTopColor'),
            blockedApprove: blocked ? Array.from(blocked.querySelectorAll('.btn')).find((b) => b.textContent === 'Approve').disabled : null,
            approveLabel: apv ? apv.textContent : null, approveDisabled: apv ? apv.disabled : null,
            selectAll: !!Array.from(document.querySelectorAll('.check-label')).find((l) => l.textContent.includes('Select all')),
          };
        }"""
    )
    expect = [
        (m["metricW"] == 176 or not wide_content, f"metric column {m['metricW']} (want 176 on wide)"),
        (m["num"] == ("20px" if wide_content else "16px"), f"metric number {m['num']}"),
        (m["btnH"] == (36 if w > 760 else 44), f"row button height {m['btnH']}"),
        (m["check"] == [16, 16], f"checkbox {m['check']}"),
        (m["ledgerTop"] == "2px" and m["rowBorder"] == "1px", f"ledger rules {m['ledgerTop']}/{m['rowBorder']}"),
        (m["rule"] == wide_content, f"metric column rule present={m['rule']} (want {wide_content})"),
        (m["accentTop"] == ACCENT[S.theme], f"self-update ledger rule {m['accentTop']}"),
        (m["blockedApprove"] is True, f"blocked row Approve disabled={m['blockedApprove']}"),
        (m["approveLabel"] == "Approve selected" and m["approveDisabled"] is True, f"Approve selected {m['approveLabel']!r} disabled={m['approveDisabled']}"),
        (m["selectAll"], "Select all missing"),
    ]
    for ok, msg in expect:
        if not ok:
            rep.fail(scene, msg)


def nodes(S, rep, scene):
    w = S.width
    m = S.page.evaluate(
        """() => {
          const vis = (el) => el && getComputedStyle(el).display !== 'none';
          const act = Array.from(document.querySelectorAll('.act-group .icon-btn')).find((b) => b.getClientRects().length);
          const table = document.querySelector('.table-wrap'); const list = document.querySelector('.svc-list');
          const off = Array.from(document.querySelectorAll('.notice')).map((n) => n.textContent);
          return { table: vis(table), list: vis(list), act: act ? [act.offsetWidth, act.offsetHeight] : null,
                   labels: Array.from(document.querySelectorAll('.st')).map((s) => s.textContent.trim()), off };
        }"""
    )
    narrow = w - (240 if w > 760 else 0) < 720
    size = 44 if w <= 760 else 40
    expect = [
        (m["table"] == (not narrow) and m["list"] == narrow, f"service table={m['table']} list={m['list']} (narrow={narrow})"),
        (m["act"] == [size, size], f"service action button {m['act']} (want {size})"),
        (any("Last seen" in t for t in m["off"]), "offline notice missing"),
        (all(tone_of(l) for l in m["labels"]), f"unknown status labels {[l for l in m['labels'] if not tone_of(l)]}"),
    ]
    for ok, msg in expect:
        if not ok:
            rep.fail(scene, msg)


def status_markers(S, rep, scene):
    """Every visible status chip: right tone for its label, 8px marker, no red, yellow, or blue."""
    p = S.page
    tok = p.evaluate(PROBE_JS, ["accent", "ink", "ink-2", "ink-3", "bg"])
    # toast chips are measured by toast_markers
    chips = p.evaluate(
        """() => Array.from(document.querySelectorAll('.st:not(.toast .st)')).filter((e) => e.getClientRects().length).map((e) => {
          const b = getComputedStyle(e, '::before'), c = getComputedStyle(e);
          return { label: e.textContent.trim(), cls: e.className, w: b.width, h: b.height, bg: b.backgroundColor,
                   border: b.borderTopColor, bw: b.borderTopWidth, chipBg: c.backgroundColor, color: c.color };
        })"""
    )
    seen = set()
    for c in chips:
        tone = tone_of(c["label"])
        seen.add(c["label"])
        if tone is None or f"st-{tone}" not in c["cls"]:
            rep.fail(scene, f"chip {c['label']!r} has class {c['cls']!r}, want tone {tone}")
            continue
        if c["w"] != "8px" or c["h"] != "8px":
            rep.fail(scene, f"chip {c['label']!r} marker {c['w']}x{c['h']}")
        if tone == "ok" and c["bg"] != tok["accent"]:
            rep.fail(scene, f"{c['label']} marker should be a filled green square, got {c['bg']}")
        if tone == "busy" and not (c["bw"] == "2px" and c["border"] == tok["accent"]):
            rep.fail(scene, f"{c['label']} marker should be a hollow green square")
        if tone == "wait" and not (c["bw"] == "2px" and c["border"] == tok["ink-2"]):
            rep.fail(scene, f"{c['label']} marker should be a hollow ink-2 square")
        if tone == "off" and c["bg"] != tok["ink-3"]:
            rep.fail(scene, f"{c['label']} marker should be a filled ink-3 square, got {c['bg']}")
        if tone == "fail" and not (c["chipBg"] == tok["ink"] and c["color"] == tok["bg"]):
            rep.fail(scene, f"{c['label']} should be an ink chip with background-colored text")
    return seen


def pixels(S, rep, scene, out_png):
    """Colors are measured on the capture, not on computed styles."""
    from PIL import Image

    img = Image.open(out_png).convert("RGB")
    box = S.page.get_by_role("button", name="Approve", exact=True).first.bounding_box()
    px = img.getpixel((int(box["x"]) + 4, int(box["y"]) + 4))
    want = {"light": (11, 122, 75), "dark": (79, 214, 156)}[S.theme]
    if max(abs(a - b) for a, b in zip(px, want)) > 6:
        rep.fail(scene, f"primary button pixel {px}, want {want}")
    bg = img.getpixel((img.width // 2, img.height - 6 if S.width > 760 else 70))
    wbg = {"light": (255, 255, 255), "dark": (12, 13, 12)}[S.theme]
    if max(abs(a - b) for a, b in zip(bg, wbg)) > 3:
        rep.fail(scene, f"page background pixel {bg}, want {wbg}")


def history(S, rep, scene):
    m = S.page.evaluate(
        """() => ({ tabs: Array.from(document.querySelectorAll('[role=tab]')).map((t) => t.textContent.replace(/\\d+$/, '')),
                    fail: !!document.querySelector('.st-fail'), include: document.body.innerText.includes('Include superseded'),
                    time: Array.from(document.querySelectorAll('.h-time')).every((t) => /^\\d\\d:\\d\\d$/.test(t.textContent)),
                    dur: Array.from(document.querySelectorAll('.h-row .m-num')).map((t) => t.textContent) })"""
    )
    expect = [
        (m["tabs"] == ["All", "Pending", "Deploying", "Success", "Failed", "Rejected"], f"tabs {m['tabs']}"),
        (m["include"], "Include superseded missing"),
        (m["time"], "history time not HH:MM"),
        (all(re.fullmatch(r"\d+ms|\d+\.\ds|\d+m \d\ds|\d+h \d\dm", d) for d in m["dur"]) and bool(m["dur"]), f"durations {m['dur']}"),
        (not any(d in ("72.0s", "600.0s") for d in m["dur"]) and {"1m 12s", "10m 00s", "41.2s"} <= set(m["dur"]), f"durations {m['dur']}"),
    ]
    for ok, msg in expect:
        if not ok:
            rep.fail(scene, msg)


def pending_gap(S, rep, scene):
    """Phone widths: on a shared line the metric number ends 16px or more before the first button."""
    if S.width > 760:
        return
    rows = S.page.evaluate(
        """() => Array.from(document.querySelectorAll('.row')).filter((r) => r.querySelector('.r-metric .m-num') && r.getClientRects().length).map((r) => {
          const num = r.querySelector('.m-num'), nr = num.getBoundingClientRect();
          const btns = Array.from(r.querySelectorAll('.r-actions > *')).map((b) => b.getBoundingClientRect());
          const first = btns[0] || null;
          const same = first ? Math.min(nr.bottom, first.bottom) - Math.max(nr.top, first.top) > 0 : false;
          return { name: r.querySelector('.r-name').textContent.slice(0, 24), gap: first ? first.left - nr.right : null, same,
                   clipped: num.scrollWidth > num.clientWidth, btnH: Array.from(r.querySelectorAll('.r-actions .btn')).map((b) => b.getBoundingClientRect().height),
                   lines: Array.from(r.querySelectorAll('.r-actions .btn')).map((b) => new Set(Array.from(b.getClientRects()).map((q) => Math.round(q.top))).size),
                   right: Math.max(0, ...btns.map((b) => b.right)), width: document.documentElement.clientWidth };
        })"""
    )
    if not rows:
        rep.fail(scene, "no pending rows to measure")
    for r in rows:
        if r["same"] and r["gap"] < 16:
            rep.fail(scene, f"{r['name']}: metric number ends {r['gap']:.0f}px before the first button (want 16 or more)")
        if r["clipped"]:
            rep.fail(scene, f"{r['name']}: metric number is clipped")
        if any(h not in (44, 0) for h in r["btnH"]) or any(n > 1 for n in r["lines"]):
            rep.fail(scene, f"{r['name']}: button height {r['btnH']} or wrapped label {r['lines']}")
        if r["right"] > r["width"]:
            rep.fail(scene, f"{r['name']}: buttons end at {r['right']:.0f}, past the {r['width']}px viewport")


def nav_count_blank(S, rep, scene, label):
    """Sidebar and drawer counts stay empty while their list is loading or failed."""
    p = S.page
    if S.width <= 760:
        p.get_by_role("button", name="Open menu").click()
        p.wait_for_selector(".drawer .nav-row")
    rows = p.evaluate(
        "() => Array.from(document.querySelectorAll('.nav-row')).map((r) => [r.querySelector('span').textContent, r.querySelector('.nav-count').textContent])"
    )
    got = dict(rows)
    if label not in got:
        rep.fail(scene, f"nav row {label!r} not found in {rows}")
    elif got[label].strip() != "":
        rep.fail(scene, f"nav count for {label!r} reads {got[label]!r} before its list is loaded")
    if S.width <= 760:
        p.keyboard.press("Escape")
        p.wait_for_selector(".drawer", state="detached")
    if label == "Pending Deploys" and S.width <= 760:
        tab = p.evaluate("() => document.querySelector('.tabbar .tab-item .nav-count')?.textContent ?? ''")
        if tab:
            rep.fail(scene, f"tab bar shows a pending count {tab!r} before the list is loaded")


def native_clear_button(S, rep, scene):
    """The browser's blue search-clear cross never shows: no saturated blue pixel at the input's right end."""
    from io import BytesIO
    from PIL import Image

    inp = S.page.locator(".search input")
    inp.focus()
    box = inp.bounding_box()
    clip = {"x": box["x"] + box["width"] - 40, "y": box["y"], "width": 40, "height": box["height"]}
    img = Image.open(BytesIO(S.page.screenshot(clip=clip, animations="disabled"))).convert("RGB")
    blue = [p for p in img.getdata() if p[2] - p[0] > 50 and p[2] - p[1] > 30]
    if blue:
        rep.fail(scene, f"{len(blue)} saturated blue pixels at the filter's right end, e.g. {blue[0]}")


TOAST_WANT = {
    "Approved deploy": "ok",
    "Node disconnected": "wait", "Superseded": "wait", "New pending": "wait",
    "Deploy succeeded": "ok", "Node connected": "ok", "restart soulstream-orch: success": "ok",
    "Deploying": "busy", "restart soulstream-orch: timeout": "fail",
}

# A failed approve and a failed deploy are failure chips; a node restarting for its own self-update is the
# hollow green marker of a state that is going to end.
TOAST_WANT_FAILURES = {
    "Deploy failed: ": "fail",
    "Approve failed: node not connected": "fail",
    "Approve failed: self-update ": "fail",
    "Approve failed: 502 ": "fail",
    "silent-manari-ts is restarting for a self-update": "busy",
}


def toast_markers(S, rep, scene, want=TOAST_WANT):
    """Toast marker shape follows the state: filled green only for finished success."""
    p = S.page
    tok = p.evaluate(PROBE_JS, ["accent", "ink", "ink-2", "bg"])
    toasts = p.evaluate(
        """() => Array.from(document.querySelectorAll('.toast')).map((t) => {
          const s = t.querySelector('.st'), b = getComputedStyle(s, '::before'), c = getComputedStyle(s);
          return { text: s.textContent.trim(), bg: b.backgroundColor, border: b.borderTopColor, bw: b.borderTopWidth,
                   chipBg: getComputedStyle(t).backgroundColor, color: c.color };
        })"""
    )
    seen = set()
    for t in toasts:
        key = next((k for k in want if t["text"].startswith(k)), None)
        if key is None:
            rep.fail(scene, f"unexpected toast {t['text']!r}")
            continue
        seen.add(key)
        tone = want[key]
        hollow = lambda col: t["bw"] == "2px" and t["border"] == col and t["bg"] in ("rgba(0, 0, 0, 0)", "transparent")
        ok = {
            "ok": t["bg"] == tok["accent"],
            "busy": hollow(tok["accent"]),
            "wait": hollow(tok["ink-2"]),
            "fail": t["chipBg"] == tok["ink"] and t["color"] == tok["bg"],
        }[tone]
        if not ok:
            rep.fail(scene, f"toast {t['text']!r} should have the {tone} marker, got bg={t['bg']} border={t['border']} {t['bw']}")
    for k in want:
        if k not in seen:
            rep.fail(scene, f"toast {k!r} never appeared")
    texts = [t["text"] for t in toasts]
    if any("{" in x or '"error"' in x for x in texts):
        rep.fail(scene, f"a toast shows raw JSON: {texts}")
    if want is TOAST_WANT_FAILURES:
        reason = [x for x in texts if x.startswith("Deploy failed: ")]
        if not reason or "Rolled back to d143f0d" not in reason[0]:
            rep.fail(scene, f"the failure toast should carry the reason from last_failure, got {reason}")


def toast_markers_failures(S, rep, scene):
    toast_markers(S, rep, scene, TOAST_WANT_FAILURES)


def pending_failed(S, rep, scene):
    """Failure rows: chip and one-line reason in the head, Retry in place of Approve, services next to commits."""
    rows = S.page.evaluate(
        """() => Array.from(document.querySelectorAll('.row')).filter((r) => r.querySelector('.r-metric.has-sub')).map((r) => {
          const fail = r.querySelector('.r-fail'), text = r.querySelector('.r-fail-text');
          const btns = Array.from(r.querySelectorAll('.r-actions .btn')).map((b) => b.textContent.trim());
          const chip = fail ? fail.querySelector('.st') : null;
          const act = r.querySelector('.r-actions .st');
          return { name: r.querySelector('.r-name').textContent.slice(0, 24), hasFail: !!fail,
                   chip: chip ? chip.textContent.trim() : null, line: text ? text.textContent : null,
                   oneLine: text ? text.getBoundingClientRect().height < 24 : null,
                   btns, sub: r.querySelector('.m-sub').textContent.trim(),
                   manual: Array.from(r.querySelectorAll('.r-meta .st')).some((s) => s.textContent.trim() === 'Manual approval'),
                   deploying: act ? act.textContent.trim() : null };
        })"""
    )
    if not rows:
        rep.fail(scene, "no pending rows to measure")
    if not any(r["hasFail"] for r in rows):
        rep.fail(scene, "no failed pending row in the fake data")
    for r in rows:
        if r["hasFail"]:
            if r["chip"] != "Failed" or not (r["line"] or "").startswith("Last deploy failed: "):
                rep.fail(scene, f"{r['name']}: failure head is {r['chip']!r} {r['line']!r}")
            if not r["oneLine"]:
                rep.fail(scene, f"{r['name']}: the failure line wraps to more than one line")
            if "Retry" not in r["btns"] or "Approve" in r["btns"]:
                rep.fail(scene, f"{r['name']}: buttons {r['btns']}, want Retry in place of Approve")
        elif r["deploying"] is None and "Approve" not in r["btns"]:
            rep.fail(scene, f"{r['name']}: buttons {r['btns']}, want Approve")
        if not re.fullmatch(r"\d+ services?|Restarts Haniel", r["sub"]):
            rep.fail(scene, f"{r['name']}: services line {r['sub']!r}")
    if not any(r["manual"] for r in rows):
        rep.fail(scene, "no Manual approval chip shown")
    if not any(r["deploying"] and r["deploying"].startswith("Deploying · ") for r in rows):
        rep.fail(scene, "no deploying row with a stage")


def history_outcome(S, rep, scene):
    got = S.page.evaluate("() => Array.from(document.querySelectorAll('.h-outcome')).map((e) => e.textContent)")
    want = {"Failed while starting services · recovery attempted", "Failed while migrating · recovery attempted",
            "Failed while verifying", "No response from node"}
    if not want <= set(got):
        rep.fail(scene, f"history failure lines {got}, want {sorted(want)}")


def nodes_ledger(S, rep, scene):
    """Expanded connected nodes: the mini ledger of serving, fetch, and polling; an old node says none is recorded."""
    m = S.page.evaluate(
        """() => Array.from(document.querySelectorAll('.mini')).map((m) => ({
          text: m.textContent.replace(/\\s+/g, ' ').trim(),
          commits: Array.from(m.querySelectorAll('.mini-serving .mono')).map((e) => e.textContent),
          fonts: Array.from(m.querySelectorAll('.mini-serving .mono')).map((e) => getComputedStyle(e).fontFamily) }))"""
    )
    if not m:
        rep.fail(scene, "no repository mini ledger in an expanded node")
        return
    edges = S.page.evaluate(
        """() => Array.from(document.querySelectorAll('.mini-rows li')).map((li) => {
          const serving = li.querySelector('.mini-serving'), checked = li.querySelector('.mini-checked');
          const range = document.createRange(); range.selectNodeContents(checked);
          const t = range.getBoundingClientRect(), mono = checked.querySelector('.mono');
          const sr = document.createRange(); sr.selectNodeContents(serving);
          const st = sr.getBoundingClientRect();
          return { repo: li.querySelector('.r-name').textContent, checked: checked.textContent.length > 0,
                   row_right: li.getBoundingClientRect().right, text_right: t.right, text_left: t.left,
                   serving_text_right: st.right, serving_text_top: st.top, checked_top: t.top, line_h: t.height,
                   gap_token: parseFloat(getComputedStyle(li).columnGap),
                   checked_font: mono ? getComputedStyle(mono).fontFamily : getComputedStyle(checked).fontFamily }; })
          .filter((r) => r.checked)"""
    )
    first = m[0]
    if "Polls every 60s" not in first["text"] or "Last successful deploy" not in first["text"]:
        rep.fail(scene, f"first node ledger: {first['text']!r}")
    if not re.search(r"checked \d+m ago", first["text"]):
        rep.fail(scene, f"first node ledger shows no fetch time: {first['text']!r}")
    sha = [c for c in first["commits"] if re.fullmatch(r"[0-9a-f]{8}", c)]
    if not sha or not all("IBM Plex Mono" in f for f in first["fonts"]):
        rep.fail(scene, f"serving commits {first['commits']} should be 8 characters in Plex Mono")
    for want in (r"checked 1h \d+m ago", r"checked 2d \d+h ago"):
        if not re.search(want, first["text"]):
            rep.fail(scene, f"first node ledger should show {want!r} (65 and 3060 minutes ago): {first['text']!r}")
    for row in edges:
        if row["text_right"] > row["row_right"] + 0.5:
            rep.fail(scene, f"ledger row {row['repo']!r}: checked text ends at {row['text_right']:.1f}, past the row end {row['row_right']:.1f}")
        same_line = abs(row["checked_top"] - row["serving_text_top"]) < row["line_h"] / 2
        gap = row["text_left"] - row["serving_text_right"]
        if same_line and gap < row["gap_token"] - 0.5:
            rep.fail(scene, f"ledger row {row['repo']!r}: serving and checked sit on one line with a {gap:.1f}px gap, want at least {row['gap_token']:.1f}px (the column gap token)")
        if row["checked"] and "IBM Plex Mono" not in row["checked_font"]:
            rep.fail(scene, f"ledger row {row['repo']!r}: the checked time is not Plex Mono ({row['checked_font']})")
    if not any("serving " in x["text"] and re.search(r"\d\d:\d\d", x["text"]) for x in m):
        rep.fail(scene, "no 'serving <commit> · HH:MM' line")
    old = [x for x in m if "No successful deploy recorded" in x["text"] and "Polls every" not in x["text"]]
    if len(m) > 1 and not old:
        rep.fail(scene, "the old node should say 'No successful deploy recorded' with no polling line")


def batch_dialog(S, rep, scene):
    """The batch confirmation: count, restart warning, targets, the skipped note, Cancel and Approve."""
    d = S.page.evaluate(
        """() => { const dlg = document.querySelector('[role=dialog]'); if (!dlg) return null;
          return { title: dlg.querySelector('.dialog-title').textContent, sub: dlg.querySelector('.dialog-sub').textContent,
                   targets: dlg.querySelectorAll('.targets li').length,
                   notes: Array.from(dlg.querySelectorAll('.dialog-note')).map((n) => n.textContent),
                   btns: Array.from(dlg.querySelectorAll('.dialog-foot .btn')).map((b) => [b.textContent.trim(), b.offsetHeight]) }; }"""
    )
    if d is None:
        rep.fail(scene, "no confirmation dialog")
        return
    if not re.fullmatch(r"Approve \d+ deployments?\?", d["title"]) or d["sub"] != "Services on those nodes restart.":
        rep.fail(scene, f"dialog heading {d['title']!r} {d['sub']!r}")
    if not any(re.fullmatch(r"\d+ needs? a look first — retry them one by one", n) for n in d["notes"]):
        rep.fail(scene, f"missing the skipped-requests note: {d['notes']}")
    if [b[0] for b in d["btns"]] != ["Cancel", "Approve"]:
        rep.fail(scene, f"dialog buttons {d['btns']}")
    want_h = 36 if S.width > 760 else 44
    if any(b[1] != want_h for b in d["btns"]):
        rep.fail(scene, f"dialog button heights {d['btns']}, want {want_h}")


def dialog_note(want):
    def check(S, rep, scene):
        text = S.page.locator("[role=dialog] .dialog-note").first.inner_text()
        if text != want:
            rep.fail(scene, f"dialog note {text!r}, want {want!r}")
    return check


def empty_line(S, rep, scene):
    text = S.page.locator(".empty-text").inner_text()
    want = "Nothing waiting. Auto-applied repositories deploy without stopping here — see History."
    if text != want:
        rep.fail(scene, f"empty pending line {text!r}")


def self_update_note(S, rep, scene):
    note = S.page.locator(".sect-note").first.inner_text()
    if not note.startswith("Approving restarts Haniel on that node.") or "overlap and adopt services keep running" not in note:
        rep.fail(scene, f"self-update section note {note!r}")


def restarting_node(S, rep, scene):
    """A disconnected node whose self-update is deploying reads Restarting, not Disconnected."""
    chips = S.page.evaluate(
        """() => Object.fromEntries(Array.from(document.querySelectorAll('.row')).map((r) =>
          [r.querySelector('.r-name').textContent, r.querySelector('.r-meta .st').textContent.trim()]))"""
    )
    if chips.get("eiaserinnys-canary") != "Restarting (self-update)" or chips.get("silent-manari-ts") != "Disconnected":
        rep.fail(scene, f"node chips {chips}")
