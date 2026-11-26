"""Design measurements for the orchestrator dashboard (card #1884 spec)."""

ACCENT = {"light": "rgb(11, 122, 75)", "dark": "rgb(79, 214, 156)"}
BODY_BG = {"light": "rgb(255, 255, 255)", "dark": "rgb(12, 13, 12)"}

TONE = {
    "ok": ["Running", "Ready", "Connected", "Success"],
    "busy": ["Starting", "Stopping", "Deploying", "Pulling", "Reconnecting…", "Connecting…"],
    "wait": ["Pending", "Restart pending"],
    "off": ["Stopped", "Rejected", "Disabled", "Offline"],
    "fail": ["Crashed", "Circuit open", "Failed", "Disconnected", "Fetch error", "Error"],
}
LABEL_TONE = {label: tone for tone, labels in TONE.items() for label in labels}

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
        (all(l in LABEL_TONE for l in m["labels"]), f"unknown status labels {[l for l in m['labels'] if l not in LABEL_TONE]}"),
    ]
    for ok, msg in expect:
        if not ok:
            rep.fail(scene, msg)


def status_markers(S, rep, scene):
    """Every visible status chip: right tone for its label, 8px marker, no red, yellow, or blue."""
    p = S.page
    tok = p.evaluate(PROBE_JS, ["accent", "ink", "ink-2", "ink-3", "bg"])
    chips = p.evaluate(
        """() => Array.from(document.querySelectorAll('.st')).filter((e) => e.getClientRects().length).map((e) => {
          const b = getComputedStyle(e, '::before'), c = getComputedStyle(e);
          return { label: e.textContent.trim(), cls: e.className, w: b.width, h: b.height, bg: b.backgroundColor,
                   border: b.borderTopColor, bw: b.borderTopWidth, chipBg: c.backgroundColor, color: c.color };
        })"""
    )
    seen = set()
    for c in chips:
        tone = LABEL_TONE.get(c["label"])
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
                    time: Array.from(document.querySelectorAll('.h-time')).every((t) => /^\\d\\d:\\d\\d$/.test(t.textContent)) })"""
    )
    expect = [
        (m["tabs"] == ["All", "Pending", "Deploying", "Success", "Failed", "Rejected"], f"tabs {m['tabs']}"),
        (m["include"], "Include superseded missing"),
        (m["time"], "history time not HH:MM"),
    ]
    for ok, msg in expect:
        if not ok:
            rep.fail(scene, msg)
