"""시도 한 건의 종결 증거 정의(AttemptEvidence). 요청 정의와 사실 표는 `definitions`에 있고 거기서 재노출한다.

2b-2에서 정의 모듈의 크기를 지키려 옮겼다. 내용은 그대로다.
"""

from __future__ import annotations

from statemachine import State

from .base import _Base


class AttemptEvidence(_Base):
    """시도 한 건의 종결 증거. 결과 글과 체크아웃 대조(정착) 글이 다 와야 성공이다.

    종결(terminal_ok, terminal_fail)과 이미 종결된 뒤 같은 글이 또 옴(dup)을 기호로 낸다. 호출부는 판 1 연결에 그 기호가
    나왔을 때만 「종결을 받았다」를 보낸다(design.md:79-82).
    """

    class ev(State.Compound):
        class open_(State.Compound):
            ev_none = State(initial=True)  # 아직 없음
            ev_result = State()  # 성공 결과만 옴
            ev_settled = State()  # 체크아웃 대조만 옴

        class closed(State.Compound):
            ev_both = State(initial=True)  # 성공으로 종결(결과와 대조, 또는 증거 회수)
            ev_failed = State()  # 실패로 종결
            ev_superseded = State()  # 노드가 스스로 한 배포의 성공에 밀려 닫힘

    result_ok = (
        ev.open_.ev_none.to(ev.open_.ev_result, cond="execute_mode")
        | ev.open_.ev_result.to(ev.open_.ev_result, cond="execute_mode")
        | ev.open_.ev_settled.to(ev.closed.ev_both, cond="execute_mode", on="terminal_ok")
        | ev.closed.ev_both.to.itself(cond="execute_mode", on="dup")
        | ev.closed.ev_failed.to.itself(cond="execute_mode", on="dup")
    )
    settled_ok = (
        ev.open_.ev_none.to(ev.open_.ev_settled, cond="execute_mode")
        | ev.open_.ev_settled.to(ev.open_.ev_settled, cond="execute_mode")
        | ev.open_.ev_result.to(ev.closed.ev_both, cond="execute_mode", on="terminal_ok")
        | ev.closed.ev_both.to.itself(cond="execute_mode", on="dup")
        | ev.closed.ev_failed.to.itself(cond="execute_mode", on="dup")
    )
    settled_noted = (
        ev.open_.ev_none.to(ev.open_.ev_settled, cond="not_execute_mode")
        | ev.open_.ev_settled.to(ev.open_.ev_settled, cond="not_execute_mode")
        | ev.closed.ev_both.to.itself(cond="not_execute_mode", on="dup")
        | ev.closed.ev_failed.to.itself(cond="not_execute_mode", on="dup")
    )
    fail = (
        ev.open_.ev_none.to(ev.closed.ev_failed, on="terminal_fail")
        | ev.open_.ev_result.to(ev.closed.ev_failed, on="terminal_fail")
        | ev.open_.ev_settled.to(ev.closed.ev_failed, on="terminal_fail")
        | ev.closed.ev_failed.to.itself(on="dup")
        | ev.closed.ev_both.to.itself(on="dup")
    )
    recovered = (
        ev.open_.ev_none.to(ev.closed.ev_both, cond="recovery_mode", on="terminal_ok")
        | ev.open_.ev_result.to(ev.closed.ev_both, cond="recovery_mode", on="terminal_ok")
        | ev.open_.ev_settled.to(ev.closed.ev_both, cond="recovery_mode", on="terminal_ok")
        | ev.closed.ev_both.to.itself(cond="recovery_mode", on="dup")
        | ev.closed.ev_failed.to.itself(cond="recovery_mode", on="dup")
    )
    superseded_by_node = (
        ev.open_.ev_none.to(ev.closed.ev_superseded)
        | ev.open_.ev_result.to(ev.closed.ev_superseded)
        | ev.open_.ev_settled.to(ev.closed.ev_superseded)
    )

    def execute_mode(self) -> bool:
        return self.fact("execute_mode")

    def not_execute_mode(self) -> bool:
        return not self.fact("execute_mode")

    def recovery_mode(self) -> bool:
        return self.fact("recovery_mode")

    def terminal_ok(self) -> None:
        self.out("terminal_ok")

    def terminal_fail(self) -> None:
        self.out("terminal_fail")

    def dup(self) -> None:
        self.out("dup")
