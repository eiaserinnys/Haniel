"""라이브러리 울타리와 복원 시험(설계 4절, 카드 #1259).

- python-statemachine은 ==3.2.2로 고정한다.
- 정의에 내부 제자리 전이, 이력 상태, delay, invoke를 쓰지 않는다.
- 최상위와 병렬 직속에 final을 쓰지 않는다. 내장 Mermaid 렌더러를 쓰지 않는다.
- 복원은 영역 이름별 잎에서 조상을 문서 순서로 펼치는 함수 하나뿐이다. 걸음마다 새 인스턴스.
- 한 사건이 두 영역을 옮길 때 조건이 걸음 전의 구성을 본다.
- 닿는 구성을 모두 줄에 적었다 되살려 같다.
"""

import ast
import importlib.metadata
import itertools
import tomllib
from pathlib import Path

import pytest
from statemachine import State, StateChart

from haniel_orch.statechart import restore as restore_module
from haniel_orch.statechart.definitions import (
    REQUIRED_FACTS,
    AttemptEvidence,
    Facts,
    OrchRequest,
)
from haniel_orch.statechart.restore import (
    NoTransition,
    advance,
    initial_row,
    leaf_ids,
    regions,
    restore,
    to_row,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "haniel_orch"
DEFINITIONS = (OrchRequest, AttemptEvidence)


def test_library_version_is_pinned_in_runtime_dependencies():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "python-statemachine==3.2.2" in pyproject["project"]["dependencies"]
    assert importlib.metadata.version("python-statemachine") == "3.2.2"


def forbidden_library_features(cls) -> list[str]:
    """울타리가 막는 기능(이력 상태, final, invoke, 내부 제자리 전이, delay)을 쓴 곳을 모은다."""
    found: list[str] = []
    for state in cls.states_map.values():
        if state.is_history:
            found.append(f"{cls.__name__}.{state.id}: 이력 상태")
        if state.final:
            found.append(f"{cls.__name__}.{state.id}: final")
        if [spec for spec in list(state.invoke) if not spec.is_convention]:
            found.append(f"{cls.__name__}.{state.id}: invoke")
        for transition in state.transitions:
            if transition.internal:
                found.append(f"{cls.__name__}.{state.id}: 내부 제자리 전이")
            if getattr(transition, "delay", 0):
                found.append(f"{cls.__name__}.{state.id}: delay")
    return found


@pytest.mark.parametrize("cls", DEFINITIONS)
def test_definitions_use_no_forbidden_library_features(cls):
    assert forbidden_library_features(cls) == []


def test_the_fence_check_catches_an_internal_self_transition():
    """음성 대조: 내부 제자리 전이를 가진 정의를 넣으면 울타리 검사가 실패해야 한다."""

    class WithInternal(StateChart):
        class top(State.Compound):
            a = State(initial=True)
            b = State()

        go = top.a.to(top.b)
        stay = top.a.to.itself(internal=True)

    assert forbidden_library_features(WithInternal) == ["WithInternal.a: 내부 제자리 전이"]


@pytest.mark.parametrize("cls", DEFINITIONS)
def test_no_final_state_at_top_or_directly_under_a_parallel(cls):
    for state in cls.states_map.values():
        if state.parent is None or getattr(state.parent, "parallel", False):
            assert not state.final


def _source_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_definition_source_names_no_forbidden_features():
    names = _source_names(SRC / "statechart" / "definitions.py")
    assert not names & {"internal", "delay", "invoke", "HistoryState", "HistoryType", "final", "deep", "shallow"}


def test_builtin_mermaid_renderer_is_never_used():
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "contrib.diagram" not in node.module, f"{path.name}: 내장 렌더러"
            if isinstance(node, ast.Import):
                assert all("contrib.diagram" not in alias.name for alias in node.names)
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"_graph", "draw", "write_image"}, f"{path.name}: 내장 렌더러"


def test_restore_is_the_only_way_to_build_a_configuration():
    """구성을 만드는 OrderedSet 생성은 restore 하나뿐이고, 인스턴스는 advance 안에서만 만든다."""
    constructions = 0
    machine_builds = 0
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "OrderedSet":
                    constructions += 1
                    assert path.name == "restore.py"
                if isinstance(func, ast.Name) and func.id == "cls":
                    machine_builds += 1
                    assert path.name == "restore.py"
    assert constructions == 1
    assert machine_builds == 2  # advance와 initial_row


@pytest.mark.parametrize("cls", DEFINITIONS)
def test_restore_spreads_ancestors_in_document_order(cls):
    for leaf in leaf_ids(cls):
        region = next(iter(regions(cls)))
        configuration = restore(cls, {region: leaf})
        state = cls.states_map[leaf]
        chain = [state, *state.ancestors()]
        assert set(configuration) == {s.value for s in chain}
        order = [cls.states_map[v].document_order for v in configuration]
        assert order == sorted(order)


def test_each_step_builds_a_fresh_instance(monkeypatch):
    built = []

    class Counting(Facts):
        def __init__(self, state, facts):
            super().__init__(state, facts)
            built.append(self)

    monkeypatch.setattr(restore_module, "Facts", Counting)
    advance(OrchRequest, {"status": "pending"}, "begin_attempt")
    advance(OrchRequest, {"status": "pending"}, "begin_attempt")
    assert len(built) == 2 and built[0] is not built[1]


def _fact_combinations(cls, event):
    names = REQUIRED_FACTS[cls][event]
    for values in itertools.product([False, True], repeat=len(names)):
        yield dict(zip(names, values))


@pytest.mark.parametrize("cls", DEFINITIONS)
def test_every_reachable_configuration_round_trips_through_a_row(cls):
    """닿는 구성을 모두 줄에 적었다 되살려도 같다. 살아 있는 인스턴스의 경로와도 견준다."""
    region = next(iter(regions(cls)))
    start = initial_row(cls)
    queue = [(start, [])]
    seen = {start[region]}
    checked = 0
    while queue:
        row, path = queue.pop(0)
        live = cls(model=Facts(None, {}))
        for event, facts in path:
            live.model.facts = facts
            live.send(event)
        assert set(restore(cls, row)) == set(live.configuration_values)
        assert to_row(cls, live) == row
        checked += 1
        for event in REQUIRED_FACTS[cls]:
            for facts in _fact_combinations(cls, event):
                try:
                    nxt = advance(cls, row, event, facts)
                except NoTransition:
                    continue
                if nxt[region] not in seen:
                    seen.add(nxt[region])
                    queue.append((nxt, [*path, (event, facts)]))
    assert seen == set(leaf_ids(cls)), "닿지 않는 잎이 있다"
    assert checked == len(leaf_ids(cls))


def test_a_step_that_moves_two_regions_reads_conditions_before_the_step():
    """한 사건이 두 영역을 옮길 때 조건이 걸음 전 구성을 본다(병렬 영역을 쓰는 PR 2의 전제)."""

    class Two(StateChart):
        allow_event_without_transition = False
        catch_errors_as_events = False
        validate_trap_states = False

        class top(State.Parallel):
            class a(State.Compound):
                a0 = State(initial=True)
                a1 = State()

            class b(State.Compound):
                b0 = State(initial=True)
                b1 = State()
                b2 = State()

        go = top.a.a0.to(top.a.a1) | top.b.b0.to(top.b.b1, cond="a_still_a0") | top.b.b1.to(top.b.b2)

        def a_still_a0(self):
            return "a0" in self.configuration_values

    model = Facts(restore(Two, {"a": "a0", "b": "b0"}), {})
    machine = Two(model=model)
    machine.send("go")
    assert to_row(Two, machine) == {"a": "a1", "b": "b1"}


def test_unknown_event_and_missing_fact_fail_loudly():
    with pytest.raises(KeyError):
        advance(OrchRequest, {"status": "pending"}, "no_such_event")
    with pytest.raises(KeyError):
        advance(OrchRequest, {"status": "deploying"}, "attempt_failed")  # newer 사실이 빠졌다
    with pytest.raises(ValueError):
        restore(OrchRequest, {"status": "no_such_leaf"})
