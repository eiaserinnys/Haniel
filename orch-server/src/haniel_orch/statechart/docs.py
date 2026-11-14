"""정의에서 그림과 표를 뽑는 생성기(내장 Mermaid 렌더러는 쓰지 않는다).

    python -m haniel_orch.statechart.docs            # 생성물을 docs/deploy-state/generated에 쓴다
    python -m haniel_orch.statechart.docs --render   # 쓴 뒤 statechart 스킬의 render.py로 .svg도 만든다
    python -m haniel_orch.statechart.docs --check    # 쓰지 않고 정의와 다르면 0이 아닌 코드로 끝낸다

정의의 상태와 전이를 읽고, 한글 표시 이름 사전(`labels`)으로 statechart 스킬 규약의
Mermaid `stateDiagram-v2`를 쓴다. 표시 이름이 없는 상태, 사건, 조건이 있으면 실패한다.
Pele는 층을 건너는 화살표가 있으면 하위 상태를 선언한 상자 밖으로 끌어내므로 열림과 닫힘 묶음은
상자로 그리지 않고 잎 이름 뒤에 「(열림)」 꼴로 붙인다. 한 묶음의 모든 잎에서 같은 사건과 조건으로 같은
곳에 가는 화살표는 묶음 이름의 합류 막대 하나로 모아 한 줄만 그린다.
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

from .definitions import (
    BREAKS,
    DESIGN_LINES,
    DIVERGENCE,
    LEAVES_NOT_IN_DESIGN,
    AttemptEvidence,
    OrchRequest,
)
from .labels import COND_LABELS, EVENT_LABELS, STATE_LABELS, effect_for
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
        if effect_for(name, arrow.event, arrow.target, arrow.source) is None:
            problems.append(f"{name}: 사건 '{arrow.event}'의 「함께 쓰는 것」이 없다")
    keys = {(a.definition, a.source, a.event, a.target) for a in arrows}
    for key in DIVERGENCE:
        if key[0] == name and key not in keys:
            problems.append(f"{name}: 정의에 없는 전이에 표시가 달려 있다: {key[1:]}")
    if problems:
        raise DocsError("\n".join(problems))
    return arrows


def _group_of(cls: type, state_id: str) -> str | None:
    """잎이 속한 묶음(열림, 닫힘)의 id. 영역 바로 아래 잎이면 None."""
    parent = cls.states_map[state_id].parent
    if parent is None or parent.id in regions(cls):
        return None
    return parent.id


def _state_display(cls: type, state_id: str) -> str:
    """잎의 보이는 이름. 묶음 상자를 그리지 않으므로 묶음은 「처음 대기 (열림)」 꼴로 뒤에 붙인다."""
    name = cls.__name__
    label = STATE_LABELS[name][state_id]
    group = _group_of(cls, state_id)
    return f"{label} ({STATE_LABELS[name][group]})" if group else label


def _group_leaves(cls: type) -> dict[str, list[str]]:
    """묶음 id -> 그 묶음의 잎들(둘 이상인 묶음만)."""
    out: dict[str, list[str]] = {}
    for state in sorted(cls.states_map.values(), key=lambda s: s.document_order):
        if state.states or state.parent is None:
            continue
        group = _group_of(cls, state.id)
        if group is not None:
            out.setdefault(group, []).append(state.id)
    return {g: leaves for g, leaves in out.items() if len(leaves) > 1}


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
    """한 장의 그림. marked가 None이면 모든 전이, True면 표시 단 전이만, False면 표시 없는 전이만.

    region이 있으면 그 영역 안의 전이만 그린다(병렬 영역이 둘인 요청 정의의 보기를 나눈다).
    """

    stem: str
    cls: type
    marked: bool | None
    region: str | None = None


VIEWS = (
    View("request-status", OrchRequest, False, "status"),
    View("request-divergence", OrchRequest, True, "status"),
    View("attempt-evidence", AttemptEvidence, None),
    View("request-lock", OrchRequest, None, "lock"),
)


def _region_of(cls: type, state_id: str) -> str:
    """잎이 속한 영역의 id."""
    names = regions(cls)
    node = cls.states_map[state_id]
    while node.id not in names:
        node = node.parent
    return node.id


def _view_arrows(view: View) -> list[Arrow]:
    arrows = validate(view.cls)
    if view.region is not None:
        arrows = [a for a in arrows if _region_of(view.cls, a.source) == view.region]
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


def _mark(arrow: Arrow):
    return DIVERGENCE.get((arrow.definition, arrow.source, arrow.event, arrow.target))


def _fold(cls: type, arrows: list[Arrow]) -> tuple[list[Arrow], list[tuple[list[str], Arrow]]]:
    """같은 사건, 조건, 도착, 표시의 화살표를 묶음 단위로 접는다.

    한 묶음(열림, 닫힘)의 모든 잎이 출발인 화살표는 합류 막대 하나로 모은다. 접히지 않은 화살표와
    접힌 (출발 잎들, 대표 화살표)를 돌려준다.
    """
    groups = _group_leaves(cls)
    plain: list[Arrow] = []
    folded: list[tuple[list[str], Arrow]] = []
    buckets: dict[tuple, list[Arrow]] = {}
    for arrow in arrows:
        buckets.setdefault((arrow.event, arrow.conds, arrow.target, _mark(arrow)), []).append(arrow)
    for items in buckets.values():
        sources = [a.source for a in items]
        covered: list[str] = []
        for leaves in groups.values():
            if set(leaves) <= set(sources):
                covered += leaves
        if covered:
            folded.append((covered, items[0]))
        plain += [a for a in items if a.source not in covered]
    order = {a: i for i, a in enumerate(arrows)}
    plain.sort(key=lambda a: order[a])
    return plain, folded


def mermaid(view: View) -> str:
    cls = view.cls
    name = cls.__name__
    arrows = _view_arrows(view)
    plain, folded = _fold(cls, arrows)
    if len(plain) + len(folded) + 1 > MAX_ARROWS:
        raise DocsError(f"{view.stem}: 한 장의 화살표가 {MAX_ARROWS}개를 넘는다")
    region = view.region or next(iter(regions(cls)))
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
        "    classDef notInDesign fill:#fff3e6,stroke:#e8730c",
        f'    state "{STATE_LABELS[name][region]}" as {region} {{',
    ]
    for index in range(len(folded)):
        lines.append(f"        state join{index + 1} <<join>>")
    for leaf in leaves:
        lines.append(f"        {leaf.id}: {_state_display(cls, leaf.id)}")
    if start_here:
        lines.append(f"        [*] --> {initial}: {START_LABEL[cls]}")
    seen: set[tuple[str, str, str]] = set()
    for arrow in plain:
        label = _arrow_label(arrow)
        key = (arrow.source, arrow.target, label)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"        {arrow.source} --> {arrow.target}: {label}")
    for index, (sources, arrow) in enumerate(folded):
        join = f"join{index + 1}"
        for source in sources:
            lines.append(f"        {source} --> {join}")
        lines.append(f"        {join} --> {arrow.target}: {_arrow_label(arrow)}")
    lines.append("    }")
    # 색은 이 그림에 그린 화살표와 이름으로 적어 둔 설계 밖 잎만으로 정한다
    breaks = {a.target for a in arrows if (_mark(a) or ("",))[0] == BREAKS}
    not_in_design = set(LEAVES_NOT_IN_DESIGN[cls]) - breaks
    for state_id in sorted(breaks & touched):
        lines.append(f"    class {state_id} breaks")
    for state_id in sorted(not_in_design & touched):
        lines.append(f"    class {state_id} notInDesign")
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
        effect = effect_for(name, arrow.event, arrow.target, arrow.source)
        mark = DIVERGENCE.get((name, arrow.source, arrow.event, arrow.target))
        line = DESIGN_LINES.get((name, arrow.source, arrow.event, arrow.target))
        mark_text = "-" if not mark else (f"{mark[1]} ({line})" if line else mark[1])
        conds = " 그리고 ".join(COND_LABELS[name][c] for c in arrow.conds) or "-"
        rows.append(
            f"| {_state_display(cls, arrow.source)} | {EVENT_LABELS[name][arrow.event]} (`{arrow.event}`) "
            f"| {conds} | {_state_display(cls, arrow.target)} | {effect} | {mark_text} |"
        )
    return "\n".join(rows) + "\n"


def transitions_markdown() -> str:
    head = (
        "# 배포 상태 전이 표\n\n"
        "`python -m haniel_orch.statechart.docs`가 정의에서 만든 파일이다. 손으로 고치지 않는다.\n"
        "「설계와 다른 점」은 설계 글(design.md)과 견준 표시다. `어긋남 N`은 설계가 어긋남으로 적은 동작이고, "
        "`PR 2에서 바뀜`은 괄호 속 design.md 줄이 옛 노드 규칙이나 PR 2에서 바꾼다고 적은 동작이며, "
        "`설계에 없음`은 설계에 그 전이가 없는 동작이다. 표시가 없는 전이는 설계 글이 받친다. "
        "요청 정의의 점검 필요(lock) 영역은 요청 상태 영역과 함께 한 사건으로 움직이므로, 두 영역의 화살표가 같은 사건 이름으로 표에 나란히 있다.\n\n"
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
