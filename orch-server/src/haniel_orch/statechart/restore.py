"""줄에서 되살리기와 한 걸음(순수 함수). 저장소를 모른다.

복원은 이 모듈의 `restore` 하나뿐이다. 영역 이름별 잎에서 조상을 펼쳐 문서 순서로
구성에 넣는다. 잎만 넣으면 조상이 구성에서 빠진다. 걸음마다 새 인스턴스를 만든다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from statemachine.exceptions import TransitionNotAllowed
from statemachine.orderedset import OrderedSet

from .definitions import REQUIRED_FACTS, Facts


# 전이가 없던 사유. 줄에는 아무것도 쓰이지 않는 점은 같고, 호출부가 삼킬 때 로그에 남긴다.
REASON_NO_ROW = "no_row"  # 줄이 없다
REASON_STATE_MISMATCH = "state_mismatch"  # 잎이 비었거나(옛 값) 바깥 칸과 어긋난다
REASON_NO_TRANSITION = "no_transition"  # 지금 잎에 이 사건의 전이가 없거나 조건이 맞지 않는다
REASON_UPDATE_ZERO = "update_zero"  # 전이는 있었으나 마지막 쓰기 재확인(UPDATE)이 0줄이었다

logger = logging.getLogger("haniel_orch.statechart")

# 전이 없음이 예상되는 사건(사유 no_transition일 때). 관측은 머리가 다르거나 처음 대기가 아닐 때마다, 되살림은 보류가
# 아닌 줄에 되풀이 알림이 올 때마다 전이가 없다. 운영 로그의 소음이라 경고 대신 DEBUG로 남긴다(2b-2 15항).
QUIET_NO_TRANSITION = frozenset({"observed", "reopen"})


class NoTransition(Exception):
    """지금 칸에 이 사건의 전이가 없다. 줄에는 아무것도 쓰이지 않는다."""

    def __init__(
        self,
        definition: str,
        row: Mapping[str, str],
        event: str,
        reason: str = REASON_NO_TRANSITION,
        key: str | None = None,
    ) -> None:
        super().__init__(
            f"{definition}: {dict(row)} 에서 '{event}' 사건의 전이가 없다 ({reason})"
        )
        self.definition = definition
        self.row = dict(row)
        self.event = event
        self.reason = reason
        self.key = key  # deploy_id 또는 orchestrator_attempt_id. 걸음이 채운다

    def warn(self, where: str) -> None:
        """호출부가 이 예외를 삼키는 자리마다 부른다. 동작은 그대로고 사유만 남긴다.

        예상된 (사건, 사유 no_transition) 짝은 DEBUG로 낮춘다(QUIET_NO_TRANSITION). 그 밖은 경고다.
        """
        level = (
            logging.DEBUG
            if self.event in QUIET_NO_TRANSITION and self.reason == REASON_NO_TRANSITION
            else logging.WARNING
        )
        logger.log(
            level,
            "전이 없음을 넘김: 자리=%s 정의=%s 사건=%s 사유=%s id=%s 줄=%s",
            where,
            self.definition,
            self.event,
            self.reason,
            self.key,
            self.row,
        )


_REGIONS: dict[type, dict] = {}
_BY_ID: dict[type, dict] = {}


def regions(cls: type) -> dict:
    """영역 이름 -> 그 영역의 상태. 최상위가 병렬이면 자식들이 영역이다."""
    if cls not in _REGIONS:
        tops = [s for s in cls.states_map.values() if s.parent is None]
        top = tops[0]
        if getattr(top, "parallel", False):
            _REGIONS[cls] = {region.id: region for region in top.states}
        else:
            _REGIONS[cls] = {top.id: top}
        _BY_ID[cls] = {s.id: s for s in cls.states_map.values()}
    return _REGIONS[cls]


def leaf_ids(cls: type) -> list[str]:
    """정의의 잎(자식이 없는 상태) 이름을 문서 순서로 돌려준다."""
    regions(cls)
    leaves = [s for s in cls.states_map.values() if not s.states]
    leaves.sort(key=lambda s: s.document_order)
    return [s.id for s in leaves]


def restore(cls: type, row: Mapping[str, str]) -> OrderedSet:
    """영역 이름별 잎에서 구성을 세운다. 조상을 펼쳐 문서 순서로 넣는다."""
    regions(cls)
    states, seen = [], set()
    for region_name, leaf in row.items():
        if region_name not in _REGIONS[cls]:
            raise ValueError(f"{cls.__name__}에 없는 영역: {region_name}")
        if leaf not in _BY_ID[cls]:
            raise ValueError(f"{cls.__name__}에 없는 잎: {leaf}")
        state = _BY_ID[cls][leaf]
        for node in [state, *state.ancestors()]:
            if node.id not in seen:
                seen.add(node.id)
                states.append(node)
    states.sort(key=lambda s: s.document_order)
    return OrderedSet(s.value for s in states)


def to_row(cls: type, machine) -> dict[str, str]:
    """구성을 영역 이름별 잎으로 적는다. 영역마다 잎이 정확히 하나여야 한다."""
    regs = regions(cls)
    out: dict[str, str] = {}
    for state in machine.configuration:
        if state.states:
            continue
        node = state
        while node.id not in regs:
            node = node.parent
        if node.id in out:
            raise AssertionError(f"영역 {node.id}에 잎이 둘: {out[node.id]}, {state.id}")
        out[node.id] = state.id
    if set(out) != set(regs):
        raise AssertionError(f"잎이 없는 영역: {set(regs) - set(out)}")
    return out


_INITIAL: dict[type, dict[str, str]] = {}


def initial_row(cls: type) -> dict[str, str]:
    """정의의 시작 구성을 영역 이름별 잎으로 돌려준다."""
    if cls not in _INITIAL:
        _INITIAL[cls] = to_row(cls, cls(model=Facts(None, {})))
    return dict(_INITIAL[cls])


def advance(
    cls: type,
    row: Mapping[str, str],
    event: str,
    facts: Mapping[str, bool] | None = None,
) -> dict[str, str]:
    """새 인스턴스를 줄에서 되살려 사건 하나를 넣고 결과 줄을 돌려준다(할 일 기호는 버린다).

    전이가 없으면 NoTransition. 사건이 요구하는 사실이 빠지면 KeyError.
    """
    return advance_with_todo(cls, row, event, facts)[0]


def advance_with_todo(
    cls: type,
    row: Mapping[str, str],
    event: str,
    facts: Mapping[str, bool] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """한 걸음의 결과 줄과 정의가 낸 할 일 기호 목록(전이의 on 콜백이 쌓은 순서)을 돌려준다."""
    required = REQUIRED_FACTS[cls]
    if event not in required:
        raise KeyError(f"{cls.__name__}에 없는 사건: {event}")
    given = dict(facts or {})
    missing = [name for name in required[event] if name not in given]
    if missing:
        raise KeyError(f"{cls.__name__}.{event}에 필요한 사실이 빠졌다: {missing}")
    model = Facts(restore(cls, row), given)
    machine = cls(model=model)
    try:
        machine.send(event)
    except TransitionNotAllowed as exc:
        raise NoTransition(cls.__name__, row, event) from exc
    return to_row(cls, machine), list(model.todo)
