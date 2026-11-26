# Haniel local dashboard

React + Vite + TypeScript. Index design (tokens in `src/index.css`, components in `src/ui.css`), light/dark/system theme
(`haniel-theme` in localStorage, `data-theme` on `<html>`). Fonts (Pretendard Variable, IBM Plex Mono) are bundled; nothing loads from a CDN.

```
pnpm install --frozen-lockfile
pnpm run build   # tsc -b && vite build -> dist/ (served by the Haniel node)
pnpm test
pnpm run lint
```

## Visual check

`scripts/visual_check.py` serves `dist/` with a fake Haniel node (`scripts/visual_mock.py`), captures every scene in light and dark at
1440 and 390 px, measures the ui-design operation items (focus, Tab and Esc, narrow widths, errors, resizing) and exits non-zero on any failure.

```
pnpm run build
python3 scripts/visual_check.py --out <folder>                       # captures + measurements + <folder>/operations.md
python3 scripts/visual_check.py --out <folder> --merge --stages errors,resize   # re-run some groups into the same folder
```

Stages: `captures`, `general`, and the flow groups `dialogs`, `layers`, `focus`, `errors`, `resize`, `narrow360`.
Needs Playwright and Pillow for the system `python3`.
