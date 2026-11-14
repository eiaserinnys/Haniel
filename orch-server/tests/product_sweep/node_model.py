"""Product node definition and fixed old-model support for the mixed sweep."""

import sys
from pathlib import Path

from statemachine import StateChart
from statemachine.exceptions import TransitionNotAllowed
from statemachine.orderedset import OrderedSet

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from haniel.core.node_run import NodeRun, step as product_step


class Row:
    def __init__(self):
        self.state = None
        self.todo: list[str] = []
        self.f: dict = {}


class Base(StateChart):
    allow_event_without_transition = False
    catch_errors_as_events = False
    validate_trap_states = False

    def has(self, *names):
        vals = self.configuration_values
        return any(n in vals for n in names)

    def out(self, what):
        self.model.todo.append(what)

    def fact(self, name):
        return bool(self.model.f.get(name))


def _regions(cls):
    """영역 이름 -> 그 영역의 상태. 최상위가 병렬이면 그 자식들이 영역이고, 아니면 최상위 하나가 영역이다."""
    tops = [s for s in cls.states_map.values() if s.parent is None]
    top = tops[0]
    if getattr(top, "parallel", False):
        return {r.id: r for r in top.states}
    return {top.id: top}


_REG: dict = {}
_BYID: dict = {}


def regions(cls):
    if cls not in _REG:
        _REG[cls] = _regions(cls)
        _BYID[cls] = {s.id: s for s in cls.states_map.values()}
    return _REG[cls]


def restore(cls, row: dict) -> OrderedSet:
    """영역 이름별 잎에서 구성을 세운다. 조상을 펼쳐 문서 순서로 넣는다."""
    regions(cls)
    states, seen = [], set()
    for leaf in row.values():
        st = _BYID[cls][leaf]
        for node in [st, *st.ancestors()]:
            if node.id not in seen:
                seen.add(node.id)
                states.append(node)
    states.sort(key=lambda s: s.document_order)
    return OrderedSet(s.value for s in states)


def to_row(cls, sm) -> dict:
    """구성을 영역 이름별 잎으로 적는다. 영역마다 잎이 정확히 하나여야 한다."""
    regs = regions(cls)
    out: dict = {}
    for s in sm.configuration:
        if s.states:
            continue
        node = s
        while node.id not in regs:
            node = node.parent
        if node.id in out:
            raise AssertionError(f"영역 {node.id}에 잎이 둘: {out[node.id]}, {s.id}")
        out[node.id] = s.id
    if set(out) != set(regs):
        raise AssertionError(f"잎이 없는 영역: {set(regs) - set(out)}")
    return out


_CACHE: dict = {}
STEPS = 0


def step(cls, row: dict, event: str, **facts):
    """Cache each definition, row, event and facts combination."""
    global STEPS
    if cls is NodeRun:
        facts = {"holds_other": False, "unexpected_auto": False, "free": False, **facts}
    key = (cls, tuple(sorted(row.items())), event, tuple(sorted(facts.items())))
    if key in _CACHE:
        return _CACHE[key]
    STEPS += 1
    try:
        if cls is NodeRun:
            new_row, todo = product_step(row, event, facts)
            result = (new_row, tuple(todo))
        else:
            model = Row()
            model.state = restore(cls, row)
            model.f = dict(facts)
            machine = cls(model=model)
            machine.send(event)
            result = (to_row(cls, machine), tuple(model.todo))
    except TransitionNotAllowed:
        result = None
    _CACHE[key] = result
    return result
