"""배포 요청과 시도의 상태 정의. 지금 동작을 그대로 적는다(카드 #1259 PR 1).

설계 정의(fable_r4_product.py의 OrchReq, AttemptEvidence)와 다른 전이는
`DIVERGENCE`에 표시를 달고 그림에도 드러낸다. 이 정의의 전이는 모두
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
        | req.status.open_.deploying.to(req.status.open_.retry, cond="not_newer")
        # 요청이 배포 중이 아닌데 시도가 실패하는 길(배포 중에 거절된 뒤 시도가 실패하는 경쟁).
        # 지금 코드는 상태를 보지 않고 거절로 덮거나(더 새 커밋이 있을 때) 표시 줄과 계보 줄만 쓴다. 어긋남 8
        | req.status.open_.pending.to(req.status.closed.superseded, cond="newer")
        | req.status.open_.retry.to(req.status.closed.superseded, cond="newer")
        | req.status.closed.success.to(req.status.closed.superseded, cond="newer")
        | req.status.closed.rejected.to(req.status.closed.superseded, cond="newer")
        | req.status.closed.superseded.to(req.status.closed.superseded, cond="newer")
        | req.status.open_.pending.to(req.status.open_.retry, cond="not_newer")
        | req.status.open_.retry.to(req.status.open_.retry, cond="not_newer")
        | req.status.closed.success.to(req.status.closed.success, cond="not_newer")
        | req.status.closed.rejected.to(req.status.closed.rejected, cond="not_newer")
        | req.status.closed.superseded.to(req.status.closed.superseded, cond="not_newer")
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
    ) | req.status.closed.superseded.to(
        # 바깥 status가 rejected이면 사유와 상관없이 되살린다(지금 코드). 잎 superseded는 사유 접두에서 온 구분이다
        req.status.open_.pending,
        cond=["self_update", "postponed_due", "no_other_open"],
    )

    def newer(self) -> bool:
        return self.fact("newer")

    def not_newer(self) -> bool:
        return not self.fact("newer")

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
        | ev.open_.ev_settled.to(ev.closed.ev_both, cond="execute_mode")
    )
    settled_ok = (
        ev.open_.ev_none.to(ev.open_.ev_settled, cond="execute_mode")
        | ev.open_.ev_settled.to(ev.open_.ev_settled, cond="execute_mode")
        | ev.open_.ev_result.to(ev.closed.ev_both, cond="execute_mode")
    )
    settled_noted = (
        ev.open_.ev_none.to(ev.open_.ev_settled, cond="not_execute_mode")
        | ev.open_.ev_settled.to(ev.open_.ev_settled, cond="not_execute_mode")
    )
    fail = (
        ev.open_.ev_none.to(ev.closed.ev_failed)
        | ev.open_.ev_result.to(ev.closed.ev_failed)
        | ev.open_.ev_settled.to(ev.closed.ev_failed)
    )
    recovered = (
        ev.open_.ev_none.to(ev.closed.ev_both, cond="recovery_mode")
        | ev.open_.ev_result.to(ev.closed.ev_both, cond="recovery_mode")
        | ev.open_.ev_settled.to(ev.closed.ev_both, cond="recovery_mode")
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

# 설계(design.md와 fable_r4_product.py의 OrchReq, AttemptEvidence)와 다른 전이의 표시. 지금 동작 그대로 옮긴 것이다.
# 키는 (정의 이름, 출발 잎, 사건, 도착 잎). 값은 (종류, 그림에 쓰는 글).
# BREAKS는 설계가 어긋남으로 적은 것(번호), CHANGES는 설계 글이 PR 2에서 바꾼다고 적은 것(근거 줄을 DESIGN_LINES에 둔다),
# ABSENT는 설계에 없는 전이다. 바뀐다는 근거 줄이 없으면 CHANGES를 달지 않는다.
# 증거 정의의 실행 방식 조건, 증거 회수, 노드 성공이 시도를 밀어냄은 설계 글이 받치므로 표시가 없다.
BREAKS = "breaks"
CHANGES = "changes"
ABSENT = "absent"
_PR2 = "PR 2에서 바뀜"
_ABSENT = "설계에 없음"
_BREAK8 = (BREAKS, "어긋남 8")
DIVERGENCE: dict[tuple[str, str, str, str], tuple[str, str]] = {
    # 조건 없는 거절(W6): 설계는 대기에서만 거절한다
    ("OrchRequest", "deploying", "reject", "rejected"): _BREAK8,
    ("OrchRequest", "success", "reject", "rejected"): _BREAK8,
    ("OrchRequest", "rejected", "reject", "rejected"): _BREAK8,
    ("OrchRequest", "superseded", "reject", "rejected"): _BREAK8,
    # 배포 중이 아닌 요청에 시도의 실패가 닿는 길: 설계는 도는 시도의 실패만 요청을 옮긴다
    ("OrchRequest", "pending", "attempt_failed", "superseded"): _BREAK8,
    ("OrchRequest", "retry", "attempt_failed", "superseded"): _BREAK8,
    ("OrchRequest", "success", "attempt_failed", "superseded"): _BREAK8,
    ("OrchRequest", "rejected", "attempt_failed", "superseded"): _BREAK8,
    ("OrchRequest", "superseded", "attempt_failed", "superseded"): _BREAK8,
    ("OrchRequest", "pending", "attempt_failed", "retry"): _BREAK8,
    ("OrchRequest", "retry", "attempt_failed", "retry"): _BREAK8,
    ("OrchRequest", "success", "attempt_failed", "success"): _BREAK8,
    ("OrchRequest", "rejected", "attempt_failed", "rejected"): _BREAK8,
    ("OrchRequest", "superseded", "attempt_failed", "superseded"): _BREAK8,
    # 도는 시도 없는 노드 실패 보고(W17): design.md 125줄이 점검 필요를 세워 실패 뒤 대기로 보낸다고 바꾼다
    ("OrchRequest", "deploying", "node_failed", "retry"): (CHANGES, _PR2),
    ("OrchRequest", "deploying", "node_failed", "pending"): (CHANGES, _PR2),
    # 자기 갱신 관측이 고른 다른 줄을 닫음(W4): design.md 382줄은 처음 대기이고 점검 필요가 없을 때만 닫는다
    ("OrchRequest", "pending", "observed_self_update", "success"): (CHANGES, _PR2),
    ("OrchRequest", "retry", "observed_self_update", "success"): (CHANGES, _PR2),
    # 보류 자기 갱신 되살림(W5): 닫힘에서 열림으로 돌아가는 전이는 설계에 없다. 자기 갱신 영역은 둘째 걸음(design.md 489줄)
    ("OrchRequest", "rejected", "reopen", "pending"): (ABSENT, _ABSENT),
    ("OrchRequest", "superseded", "reopen", "pending"): (ABSENT, _ABSENT),
}

# 바뀐다는 표시(CHANGES)와 설계에 없다는 표시(ABSENT)의 근거: design.md 줄 번호
DESIGN_LINES: dict[tuple[str, str, str, str], str] = {
    ("OrchRequest", "deploying", "node_failed", "retry"): "design.md:125",
    ("OrchRequest", "deploying", "node_failed", "pending"): "design.md:125",
    ("OrchRequest", "pending", "observed_self_update", "success"): "design.md:382, 489",
    ("OrchRequest", "retry", "observed_self_update", "success"): "design.md:382, 489",
    ("OrchRequest", "rejected", "reopen", "pending"): "design.md:489",
    ("OrchRequest", "superseded", "reopen", "pending"): "design.md:489",
}

# 설계 증거 정의(design.md 「종결 증거」의 다섯 상태)에 없는 잎. 그림에서 주황으로 칠한다.
LEAVES_NOT_IN_DESIGN: dict[type, frozenset[str]] = {
    OrchRequest: frozenset(),
    AttemptEvidence: frozenset({"ev_superseded"}),
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
