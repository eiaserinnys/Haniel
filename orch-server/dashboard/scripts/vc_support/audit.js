() => {
  const out = [];
  const visible = (el) => {
    const s = getComputedStyle(el);
    return el.getClientRects().length > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const label = (el) => el.tagName.toLowerCase() + (typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\s+/)[0] : '');
  const root = document.documentElement;
  const content = document.querySelector('.content');
  if (root.scrollWidth > window.innerWidth) out.push(`page scrolls sideways (${root.scrollWidth} > ${window.innerWidth})`);
  if (content && content.scrollWidth > content.clientWidth) out.push(`content scrolls sideways (${content.scrollWidth} > ${content.clientWidth})`);

  const offPalette = (css, where) => {
    const m = /rgba?\(([\d.]+)[, ]+([\d.]+)[, ]+([\d.]+)(?:[,/ ]+([\d.]+%?))?\)/.exec(css);
    if (!m) return;
    const a = m[4] === undefined ? 1 : (m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
    if (a === 0) return;
    const [r, g, b] = [m[1], m[2], m[3]].map((v) => parseFloat(v) / 255);
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    if (d === 0) return;
    const l = (max + min) / 2, sat = d / (1 - Math.abs(2 * l - 1));
    if (sat < 0.2) return;
    let h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h = (h * 60 + 360) % 360;
    if (h < 100 || h > 180) out.push(`color outside the ink and green palette: ${where} ${css}`);
  };
  const all = Array.from(document.querySelectorAll('body *')).filter(visible);
  for (const el of all) {
    const s = getComputedStyle(el);
    const ownText = Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim());
    if (ownText && !el.classList.contains('sr-only') && parseFloat(s.fontSize) < 13) {
      out.push(`font under 13px: ${label(el)} ${s.fontSize}`);
    }
    offPalette(s.color, label(el) + ' color');
    offPalette(s.backgroundColor, label(el) + ' background');
    offPalette(s.borderTopColor, label(el) + ' border');
    if (s.boxShadow !== 'none') out.push(`shadow on ${label(el)}`);
    if (['borderTopLeftRadius', 'borderTopRightRadius', 'borderBottomLeftRadius', 'borderBottomRightRadius'].some((k) => s[k] !== '0px')) {
      out.push(`rounded corner on ${label(el)}`);
    }
    if (ownText && !el.classList.contains('sr-only') && s.overflowX === 'hidden' && el.scrollWidth > el.clientWidth + 1 && s.textOverflow !== 'ellipsis') {
      out.push(`clipped without ellipsis: ${label(el)} "${el.textContent.trim().slice(0, 30)}"`);
    }
    if (el.matches('.mono, .m-num, .h-time, .commit-hash, .nav-count, .r-node') && new Set(Array.from(el.getClientRects()).map((r) => Math.round(r.top))).size > 1) {
      out.push(`number or id broken across lines: ${label(el)} "${el.textContent.trim().slice(0, 30)}"`);
    }
    if (el.matches('button, input, .btn, .icon-btn')) {
      const r = el.getBoundingClientRect();
      if (r.left < -0.5 || r.right > window.innerWidth + 0.5) out.push(`control outside viewport: ${label(el)} "${(el.getAttribute('aria-label') || el.textContent).trim().slice(0, 20)}"`);
    }
  }
  return out;
}
