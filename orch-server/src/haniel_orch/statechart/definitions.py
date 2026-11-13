"""배포 요청과 시도의 상태 정의. 지금 동작을 그대로 적는다(카드 #1259 PR 1).

설계 정의(fable_r4_product.py의 OrchReq, AttemptEvidence)와 다른 전이는
`labels.DIVERGENCE`에 표시를 달고 그림에도 드러낸다. 이 정의의 전이는 모두
저장소 변경 잠금 안의 한 걸음(`steps`)으로만 일어난다.

울타리(설계 4절): 내부 제자리 전이, 이력 상태, delay, invoke를 쓰지 않고,
최상위와 병렬 직속에 final을 두지 않는다. 시험이 지킨다.
"""

from __future__ import annotations

from statemachine import State, StateChart


class Facts:
    """한 걸음의 입력. 줄에서 되살린 잎과 걸음마다 읽은 사실을 담는다."""

    def __init__(self, state, facts: dict[str, bool]) -> None:
        self.state = state
        self.facts = facts


class _Base(StateChart):
    allow_event_without_transition = False
    catch_errors_as_events = False
    validate_trap_states = False

    def fact(self, name: str) -> bool:
        """선언하지 않은 사실을 읽으면 KeyError로 바로 실패한다."""
        return bool(self.model.facts[name])


class OrchRequest(_Base):
    """오케스트레이터의 배포 요청 한 건. 영역은 요청 상태 하나다(PR 2가 믿음과 점검 필요를 더한다)."""

    class req(State.Parallel):
        class status(State.Compound):
            class open_(State.Compound):
                pending = State(initial=True)  # 처음 대기. 표시 줄 없음
                retry = State()  # 표시 줄 있는 대기. 바깥 칸은 pending
                deploying = State()  # 배포 중. 시도가 돈다

            class closed(State.Compound):
                success = State(initial=True)
                rejected = State()
                superseded = State()  # 새 커밋에 밀림. 바깥 칸은 rejected, 사유가 superseded by

    begin_attempt = (
        req.status.open_.pending.to(req.status.open_.deploying)
        | req.status.open_.retry.to(req.status.open_.deploying)
    )
    attempt_succeeded = req.status.open_.deploying.to(req.status.closed.success)
    attempt_failed = (
        req.status.open_.deploying.to(req.status.closed.superseded, cond="newer")
        | req.status.open_.deploying.to(req.status.open_.retry)
    )
    reject = (
        req.status.open_.pending.to(req.status.closed.rejected)
        | req.status.open_.retry.to(req.status.closed.rejected)
        | req.status.open_.deploying.to(req.status.closed.rejected)  # 어긋남 8
        | req.status.closed.success.to(req.status.closed.rejected)  # 어긋남 8
        | req.status.closed.rejected.to(req.status.closed.rejected)  # 어긋남 8
        | req.status.closed.superseded.to(req.status.closed.rejected)  # 어긋남 8
    )
    newer_commit = (
        req.status.open_.pending.to(req.status.closed.superseded)
        | req.status.open_.retry.to(req.status.closed.superseded)
    )
    node_succeeded = (
        req.status.open_.pending.to(req.status.closed.success)
        | req.status.open_.retry.to(req.status.closed.success)
        | req.status.open_.deploying.to(req.status.closed.success)
    )
    node_failed = (
        req.status.open_.deploying.to(req.status.open_.retry, cond="marked")
        | req.status.open_.deploying.to(req.status.open_.pending, cond="unmarked")
    )
    observed = req.status.open_.pending.to(
        req.status.closed.success,
        cond=["heads_equal", "no_active_attempt", "no_failed_node_attempt"],
    )
    observed_self_update = (
        req.status.open_.pending.to(
            req.status.closed.success,
            cond=["self_update", "heads_equal", "no_active_attempt"],
        )
        | req.status.open_.retry.to(
            req.status.closed.success,
            cond=["self_update", "heads_equal", "no_active_attempt"],
        )
    )
    reopen = req.status.closed.rejected.to(
        req.status.open_.pending,
        cond=["self_update", "postponed_due", "no_other_open"],
    )

    def newer(self) -> bool:
        return self.fact("newer")

    def marked(self) -> bool:
        return self.fact("marked")

    def unmarked(self) -> bool:
        return not self.fact("marked")

    def heads_equal(self) -> bool:
        return self.fact("heads_equal")

    def no_active_attempt(self) -> bool:
        return self.fact("no_active_attempt")

    def no_failed_node_attempt(self) -> bool:
        return self.fact("no_failed_node_attempt")

    def self_update(self) -> bool:
        return self.fact("self_update")

    def postponed_due(self) -> bool:
        return self.fact("postponed_due")

    def no_other_open(self) -> bool:
        return self.fact("no_other_open")


class AttemptEvidence(_Base):
    """시도 한 건의 종결 증거. 결과 글과 체크아웃 대조(정착) 글이 다 와야 성공이다."""

    class ev(State.Compound):
        ev_none = State(initial=True)  # 아직 없음
        ev_result = State()  # 성공 결과만 옴
        ev_settled = State()  # 체크아웃 대조만 옴
        ev_both = State()  # 성공으로 종결(결과와 대조, 또는 증거 회수)
        ev_failed = State()  # 실패로 종결
        ev_superseded = State()  # 노드가 스스로 한 배포의 성공에 밀려 닫힘

    result_ok = (
        ev.ev_none.to(ev.ev_result, cond="execute_mode")
        | ev.ev_result.to(ev.ev_result, cond="execute_mode")
        | ev.ev_settled.to(ev.ev_both, cond="execute_mode")
    )
    settled_ok = (
        ev.ev_none.to(ev.ev_settled, cond="execute_mode")
        | ev.ev_settled.to(ev.ev_settled, cond="execute_mode")
        | ev.ev_result.to(ev.ev_both, cond="execute_mode")
    )
    settled_noted = (
        ev.ev_none.to(ev.ev_settled, cond="not_execute_mode")
        | ev.ev_settled.to(ev.ev_settled, cond="not_execute_mode")
    )
    fail = (
        ev.ev_none.to(ev.ev_failed)
        | ev.ev_result.to(ev.ev_failed)
        | ev.ev_settled.to(ev.ev_failed)
    )
    recovered = (
        ev.ev_none.to(ev.ev_both, cond="recovery_mode")
        | ev.ev_result.to(ev.ev_both, cond="recovery_mode")
        | ev.ev_settled.to(ev.ev_both, cond="recovery_mode")
    )
    superseded_by_node = (
        ev.ev_none.to(ev.ev_superseded)
        | ev.ev_result.to(ev.ev_superseded)
        | ev.ev_settled.to(ev.ev_superseded)
    )

    def execute_mode(self) -> bool:
        return self.fact("execute_mode")

    def not_execute_mode(self) -> bool:
        return not self.fact("execute_mode")

    def recovery_mode(self) -> bool:
        return self.fact("recovery_mode")


# 사건마다 호출부가 넣어야 하는 사실. 빠지면 한 걸음이 바로 실패한다.
REQUIRED_FACTS: dict[type, dict[str, tuple[str, ...]]] = {
    OrchRequest: {
        "begin_attempt": (),
        "attempt_succeeded": (),
        "attempt_failed": ("newer",),
        "reject": (),
        "newer_commit": (),
        "node_succeeded": (),
        "node_failed": ("marked",),
        "observed": ("heads_equal", "no_active_attempt", "no_failed_node_attempt"),
        "observed_self_update": ("self_update", "heads_equal", "no_active_attempt"),
        "reopen": ("self_update", "postponed_due", "no_other_open"),
    },
    AttemptEvidence: {
        "result_ok": ("execute_mode",),
        "settled_ok": ("execute_mode",),
        "settled_noted": ("execute_mode",),
        "fail": (),
        "recovered": ("recovery_mode",),
        "superseded_by_node": (),
    },
}

# 잎이 닫힘인 요청 상태와 끝난 시도 증거
REQUEST_CLOSED = frozenset({"success", "rejected", "superseded"})
ATTEMPT_TERMINAL = frozenset({"ev_both", "ev_failed", "ev_superseded"})

# 바깥 칸(status)으로 쓰이는 값. 대기 둘은 pending, 밀림은 rejected다.
REQUEST_OUTER_STATUS = {
    "pending": "pending",
    "retry": "pending",
    "deploying": "deploying",
    "success": "success",
    "rejected": "rejected",
    "superseded": "rejected",
}
