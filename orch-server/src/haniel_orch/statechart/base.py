"""정의 둘이 함께 쓰는 바탕: 한 걸음의 입력과 출력(Facts), 울타리 설정과 사실 읽기(_Base).

2b-2에서 정의 모듈의 크기를 지키려 옮겼다. 내용은 그대로다.
"""

from __future__ import annotations

from statemachine import StateChart


class Facts:
    """한 걸음의 입력과 출력. 줄에서 되살린 잎, 걸음마다 읽은 사실, 정의가 낸 할 일 기호를 담는다."""

    def __init__(self, state, facts: dict[str, bool]) -> None:
        self.state = state
        self.facts = facts
        self.todo: list[str] = []


class _Base(StateChart):
    allow_event_without_transition = False
    catch_errors_as_events = False
    validate_trap_states = False

    def fact(self, name: str) -> bool:
        """선언하지 않은 사실을 읽으면 KeyError로 바로 실패한다."""
        return bool(self.model.facts[name])

    def has(self, *names: str) -> bool:
        """걸음 전의 구성에 이 잎들 가운데 하나가 들어 있는가(다른 영역의 잎을 조건에서 볼 때 쓴다)."""
        values = self.configuration_values
        return any(name in values for name in names)

    def out(self, symbol: str) -> None:
        self.model.todo.append(symbol)
