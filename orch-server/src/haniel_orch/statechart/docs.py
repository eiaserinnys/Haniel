"""정의에서 그림과 표를 뽑는 생성기(내장 Mermaid 렌더러는 쓰지 않는다).

    python -m haniel_orch.statechart.docs            # 생성물을 docs/deploy-state/generated에 쓴다
    python -m haniel_orch.statechart.docs --render   # 쓴 뒤 statechart 스킬의 render.py로 .svg도 만든다
    python -m haniel_orch.statechart.docs --check    # 쓰지 않고 정의와 다르면 0이 아닌 코드로 끝낸다

정의의 상태와 전이를 읽고, 한글 표시 이름 사전(`labels`)으로 statechart 스킬 규약의
Mermaid `stateDiagram-v2`를 쓴다. 표시 이름이 없는 상태, 사건, 조건이 있으면 실패한다.
Pele는 층을 건너는 화살표가 있으면 하위 상태를 선언한 상자 밖으로 끌어내므로 요청 상태의
열림과 닫힘 묶음은 상자 대신 상태 이름 앞에 단다.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .definitions import BREAKS, CHANGES, DIVERGENCE, AttemptEvidence, OrchRequest
from .labels import COND_LABELS, EFFECTS, EVENT_LABELS, STATE_LABELS
from .restore import initial_row, regions

MAX_ARROWS = 25  # 스킬 규약: 한 장의 화살표는 20개 안팎, 생성기 상한 25개
DEFINITIONS = (OrchRequest, AttemptEvidence)
START_LABEL = {OrchRequest: "요청이 들어온다", AttemptEvidence: "시도를 시작한다"}
SVG_STAMP = "statechart-source-sha256"


class DocsError(Exception):
    """표시 이름이 없거나 표시가 정의와 맞지 않는다."""


@dataclass(frozen=True)
class Arrow:
    definition: str
    source: str
    event: str
    target: str
    conds: tuple[str, ...]


def repo_root() -> Path:
    # orch-server/src/haniel_orch/statechart/docs.py -> 저장소 루트
    return Path(__file__).resolve().parents[4]


def default_out_dir() -> Path:
    return repo_root() / "docs" / "deploy-state" / "generated"


def transitions(cls: type) -> list[Arrow]:
    """정의의 사건 전이를 문서 순서로 읽는다. 시작 구성을 만드는 암시 전이는 뺀다."""
    regions(cls)
    arrows: list[Arrow] = []
    states = sorted(cls.states_map.values(), key=lambda s: s.document_order)
    for state in states:
        for transition in state.transitions:
            if not transition.event:
                continue
            conds = tuple(
                spec.func
                for spec in transition.cond.list
                if not spec.is_convention and isinstance(spec.func, str)
            )
            for target in transition.targets:
                arrows.append(
                    Arrow(cls.__name__, state.id, transition.event, target.id, conds)
                )
    return arrows


def validate(cls: type) -> list[Arrow]:
    """표시 이름 누락, 표시와 정의의 불일치를 모아 DocsError로 낸다."""
    name = cls.__name__
    problems: list[str] = []
    labels = STATE_LABELS.get(name, {})
    for state in cls.states_map.values():
        if state.id not in labels:
            problems.append(f"{name}: 상태 '{state.id}'의 표시 이름이 없다")
    arrows = transitions(cls)
    for arrow in arrows:
        if arrow.event not in EVENT_LABELS.get(name, {}):
            problems.append(f"{name}: 사건 '{arrow.event}'의 표시 이름이 없다")
        for cond in arrow.conds:
            if cond not in COND_LABELS.get(name, {}):
                problems.append(f"{name}: 조건 '{cond}'의 표시 이름이 없다")
        if (arrow.event, None) not in EFFECTS.get(name, {}) and (
            arrow.event,
            arrow.target,
        ) not in EFFECTS.get(name, {}):
            problems.append(f"{name}: 사건 '{arrow.event}'의 「함께 쓰는 것」이 없다")
    keys = {(a.definition, a.source, a.event, a.target) for a in arrows}
    for key in DIVERGENCE:
        if key[0] == name and key not in keys:
            problems.append(f"{name}: 정의에 없는 전이에 표시가 달려 있다: {key[1:]}")
    if problems:
        raise DocsError("\n".join(problems))
    return arrows


def _state_display(cls: type, state_id: str) -> str:
    """잎의 보이는 이름. 영역 아래 묶음(열림, 닫힘)은 이름 앞에 단다."""
    name = cls.__name__
    state = cls.states_map[state_id]
    region_ids = set(regions(cls))
    groups: list[str] = []
    node = state.parent
    while node is not None and node.id not in region_ids:
        groups.append(STATE_LABELS[name][node.id])
        node = node.parent
    prefix = " · ".join(reversed(groups))
    label = STATE_LABELS[name][state_id]
    return f"{prefix} · {label}" if prefix else label


def _arrow_label(arrow: Arrow) -> str:
    name = arrow.definition
    text = EVENT_LABELS[name][arrow.event]
    if arrow.conds:
        text += " [" + " 그리고 ".join(COND_LABELS[name][c] for c in arrow.conds) + "]"
    mark = DIVERGENCE.get((name, arrow.source, arrow.event, arrow.target))
    if mark:
        text += f" ({mark[1]})"
    return text


@dataclass(frozen=True)
class View:
    """한 장의 그림. marked가 None이면 모든 전이, True면 표시 단 전이만, False면 표시 없는 전이만."""

    stem: str
    cls: type
    marked: bool | None


VIEWS = (
    View("request-status", OrchRequest, False),
    View("request-divergence", OrchRequest, True),
    View("attempt-evidence", AttemptEvidence, None),
)


def _view_arrows(view: View) -> list[Arrow]:
    arrows = validate(view.cls)
    if view.marked is None:
        return arrows
    return [
        a
        for a in arrows
        if (
            (a.definition, a.source, a.event, a.target) in DIVERGENCE
        )
        == view.marked
    ]


def mermaid(view: View) -> str:
    cls = view.cls
    name = cls.__name__
    arrows = _view_arrows(view)
    if len(arrows) + 1 > MAX_ARROWS:
        raise DocsError(f"{view.stem}: 한 장의 화살표가 {MAX_ARROWS}개를 넘는다")
    region = next(iter(regions(cls)))
    initial = initial_row(cls)[region]
    start_here = view.marked is not True
    touched = {a.source for a in arrows} | {a.target for a in arrows}
    if start_here:
        touched.add(initial)
    leaves = [
        s
        for s in sorted(cls.states_map.values(), key=lambda s: s.document_order)
        if not s.states and s.id in touched
    ]
    lines = [
        "stateDiagram-v2",
        "    direction TB",
        "    classDef breaks fill:#ffecee,stroke:#e72e3b",
        "    classDef mayBreak fill:#fff3e6,stroke:#e8730c",
        f'    state "{STATE_LABELS[name][region]}" as {region} {{',
    ]
    for leaf in leaves:
        lines.append(f"        {leaf.id}: {_state_display(cls, leaf.id)}")
    if start_here:
        lines.append(f"        [*] --> {initial}: {START_LABEL[cls]}")
    seen: set[tuple[str, str, str]] = set()
    for arrow in arrows:
        label = _arrow_label(arrow)
        key = (arrow.source, arrow.target, label)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"        {arrow.source} --> {arrow.target}: {label}")
    lines.append("    }")
    all_arrows = validate(cls)
    marks = {
        (a.source, a.event, a.target): DIVERGENCE.get((name, a.source, a.event, a.target))
        for a in all_arrows
    }
    breaks = {t for (_, _, t), m in marks.items() if m and m[0] == BREAKS}
    # 들어오는 화살표(시작 화살표 포함)가 모두 「PR 2에서 바뀜」인 상태는 설계 정의에 없는 상태다
    incoming: dict[str, list[bool]] = {initial: [False]}
    for (_, _, target), mark in marks.items():
        incoming.setdefault(target, []).append(bool(mark and mark[0] == CHANGES))
    changes = {t for t, flags in incoming.items() if all(flags)} - breaks
    for state_id in sorted(breaks & touched):
        lines.append(f"    class {state_id} breaks")
    for state_id in sorted(changes & touched):
        lines.append(f"    class {state_id} mayBreak")
    return "\n".join(lines) + "\n"


def table(cls: type) -> str:
    """전이 표. 사건 코드 이름은 개발자를 위해 이 표에만 쓴다."""
    arrows = validate(cls)
    name = cls.__name__
    rows = [
        f"## {name}",
        "",
        "| 출발 | 사건 | 조건 | 도착 | 함께 쓰는 것 | 설계와 다른 점 |",
        "|---|---|---|---|---|---|",
    ]
    for arrow in arrows:
        effects = EFFECTS[name]
        effect = effects.get((arrow.event, arrow.target)) or effects[(arrow.event, None)]
        mark = DIVERGENCE.get((name, arrow.source, arrow.event, arrow.target))
        conds = " 그리고 ".join(COND_LABELS[name][c] for c in arrow.conds) or "-"
        rows.append(
            f"| {_state_display(cls, arrow.source)} | {EVENT_LABELS[name][arrow.event]} (`{arrow.event}`) "
            f"| {conds} | {_state_display(cls, arrow.target)} | {effect} | {mark[1] if mark else '-'} |"
        )
    return "\n".join(rows) + "\n"


def transitions_markdown() -> str:
    head = (
        "# 배포 상태 전이 표\n\n"
        "`python -m haniel_orch.statechart.docs`가 정의에서 만든 파일이다. 손으로 고치지 않는다.\n"
        "「설계와 다른 점」에 번호가 있으면 설계가 어긋남으로 적은 동작이고, "
        "`PR 2에서 바뀜`은 지금 동작을 그대로 옮겼으나 설계 정의에는 없는 전이다.\n\n"
    )
    return head + "\n".join(table(cls) for cls in DEFINITIONS)


def outputs() -> dict[str, str]:
    files = {f"{view.stem}.mmd": mermaid(view) for view in VIEWS}
    files["transitions.md"] = transitions_markdown()
    return files


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stamp_svg(svg_path: Path, mmd_text: str) -> None:
    body = svg_path.read_text(encoding="utf-8")
    marker = f"<!-- {SVG_STAMP}:"
    body = body.split(marker)[0].rstrip("\n")
    svg_path.write_text(f"{body}\n{marker} {_sha(mmd_text)} -->\n", encoding="utf-8")


def svg_is_current(svg_path: Path, mmd_text: str) -> bool:
    if not svg_path.exists():
        return False
    return f"<!-- {SVG_STAMP}: {_sha(mmd_text)} -->" in svg_path.read_text(encoding="utf-8")


def render_svgs(out_dir: Path) -> None:
    common = os.environ.get("AGENT_COMMON_FILES_DIR")
    render = os.environ.get("STATECHART_RENDER") or (
        str(Path(common) / "skills" / "statechart" / "scripts" / "render.py")
        if common
        else None
    )
    if not render or not Path(render).exists():
        raise DocsError(
            "render.py를 찾지 못했다. AGENT_COMMON_FILES_DIR 또는 STATECHART_RENDER를 정한다"
        )
    subprocess.run([sys.executable, render, str(out_dir)], check=True)
    for view in VIEWS:
        mmd = out_dir / f"{view.stem}.mmd"
        stamp_svg(mmd.with_suffix(".svg"), mmd.read_text(encoding="utf-8"))


def check(out_dir: Path) -> list[str]:
    """생성물이 정의와 다른 곳을 모아 돌려준다. 비어 있으면 같다."""
    problems: list[str] = []
    for filename, text in outputs().items():
        path = out_dir / filename
        if not path.exists():
            problems.append(f"{filename}: 파일이 없다")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(f"{filename}: 정의에서 만든 것과 다르다")
    for view in VIEWS:
        if not svg_is_current(out_dir / f"{view.stem}.svg", mermaid(view)):
            problems.append(f"{view.stem}.svg: 없거나 .mmd와 다르다(--render로 다시 만든다)")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m haniel_orch.statechart.docs")
    parser.add_argument("--check", action="store_true", help="쓰지 않고 다르면 1로 끝낸다")
    parser.add_argument("--render", action="store_true", help="쓴 뒤 render.py로 .svg도 만든다")
    parser.add_argument("--out", type=Path, default=None, help="생성물 폴더(기본: docs/deploy-state/generated)")
    args = parser.parse_args(argv)
    out_dir = args.out or default_out_dir()
    try:
        if args.check:
            problems = check(out_dir)
            for problem in problems:
                print(problem, file=sys.stderr)
            if problems:
                return 1
            print(f"ok: {out_dir}")
            return 0
        out_dir.mkdir(parents=True, exist_ok=True)
        for filename, text in outputs().items():
            (out_dir / filename).write_text(text, encoding="utf-8")
        if args.render:
            render_svgs(out_dir)
        print(f"wrote {out_dir}")
        return 0
    except DocsError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
