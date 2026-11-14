"""배포 요청과 시도의 상태 정의(카드 #1259 PR 1에서 정의로 옮기고, PR 2a가 「점검 필요」 영역을, PR 2b-1이
닫힌 시도의 늦은 글 규칙과 「닫힌 뒤 적용」 꼬리표 영역을 더했다).

설계 정의(fable_r4_product.py의 OrchReq, AttemptEvidence)와 다른 전이는
`DIVERGENCE`에 표시를 달고 그림에도 드러낸다. 이 정의의 전이는 모두
저장소 변경 잠금 안의 한 걸음(`steps`)으로만 일어난다.

울타리(설계 4절): 내부 제자리 전이, 이력 상태, delay, invoke를 쓰지 않고,
최상위와 병렬 직속에 final을 두지 않는다. 시험이 지킨다.
"""

from __future__ import annotations

from statemachine import State, StateChart

from .divergence import (  # noqa: F401 (docs와 시험이 여기서 가져온다)
    ABSENT,
    BREAKS,
    CHANGES,
    DESIGN_LINES,
    DIVERGENCE,
)


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

    def has(self, *names: str) -> bool:
        """걸음 전의 구성에 이 잎들 가운데 하나가 들어 있는가(다른 영역의 잎을 조건에서 볼 때 쓴다)."""
        values = self.configuration_values
        return any(name in values for name in names)


class OrchRequest(_Base):
    """오케스트레이터의 배포 요청 한 건. 영역은 요청 상태, 점검 필요, 닫힌 뒤 적용 꼬리표 셋이다(PR 2b-2가 믿음을 더한다).

    점검 필요(lock)는 「실제로 돌다 실패한 커밋」을 점검 뒤 승인 말고는 시도로 열지 못하게 잠근다.
    옛 노드 규칙(design.md:429-430)에 따라 모든 시도의 닫힘(실패, 보내기 실패, 노드 스스로 한 배포의 실패,
    닫힌 시도의 늦은 실패)이 켠다. 닫힘(성공, 거절, 밀림)은 이 영역을 건드리지 않는다. 내리는 곳은 원천이 단건 승인인
    begin_attempt 하나다.

    닫힌 뒤 적용 꼬리표(tag)는 거절이나 밀림으로 닫힌 요청에 성공 증거(닫힌 시도의 늦은 성공, 노드가 스스로 한 배포의
    성공)가 닿았음을 드러낸다(어긋남 12, design.md:336, 414). 한 번 서면 내리지 않는다.
    """

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

        class lock(State.Compound):
            l_off = State(initial=True)  # 점검 필요가 서지 않음
            l_on = State()  # 점검 필요가 섬. 단건 승인이 새 시도를 열 때에만 내린다

        class tag(State.Compound):
            t_off = State(initial=True)  # 꼬리표 없음
            t_on = State()  # 닫힌 뒤 적용. 거절이나 밀림으로 닫힌 요청에 성공 증거가 닿았다

    # 승인해서 시도를 연다. 점검 필요가 켜져 있으면 원천이 단건 승인(manual_single)일 때만 열고, 그때 점검 필요를 내린다
    begin_attempt = (
        req.status.open_.pending.to(req.status.open_.deploying, cond="may_open")
        | req.status.open_.retry.to(req.status.open_.deploying, cond="may_open")
        | req.lock.l_on.to(req.lock.l_off, cond="manual_single")
    )
    attempt_succeeded = req.status.open_.deploying.to(req.status.closed.success)
    # 시도가 실패로 끝난다. 점검 필요는 요청이 이미 닫혀 있어도 켠다(닫힌 줄의 상태는 쓰지 않는다)
    attempt_failed = (
        req.status.open_.deploying.to(req.status.closed.superseded, cond="newer")
        | req.status.open_.deploying.to(req.status.open_.retry, cond="not_newer")
        | req.lock.l_off.to(req.lock.l_on)
    )
    # 허가가 노드에 닿지 못한 보내기 실패. 시도를 그 자리에서 닫는다. 옛 노드 규칙이라 점검 필요를 켠다(design.md:429-430)
    send_failed = (
        req.status.open_.deploying.to(req.status.closed.superseded, cond="newer")
        | req.status.open_.deploying.to(req.status.open_.retry, cond="not_newer")
        | req.lock.l_off.to(req.lock.l_on)
    )
    reject = req.status.open_.pending.to(
        req.status.closed.rejected
    ) | req.status.open_.retry.to(req.status.closed.rejected)
    newer_commit = (
        req.status.open_.pending.to(req.status.closed.superseded)
        | req.status.open_.retry.to(req.status.closed.superseded)
    )
    node_succeeded = (
        req.status.open_.pending.to(req.status.closed.success)
        | req.status.open_.retry.to(req.status.closed.success)
        | req.status.open_.deploying.to(req.status.closed.success)
    )
    # 노드가 스스로 한 배포의 실패. 열린 요청이면 점검 필요를 켠다. 도는 시도가 있으면 칸만 켜고 시도와 요청 상태는 그대로 둔다
    node_failed = (
        req.status.open_.pending.to(req.status.open_.retry)
        | req.status.open_.deploying.to(req.status.open_.retry, cond="no_active_attempt")
        | req.lock.l_off.to(req.lock.l_on, cond="open_request")
    )
    # 지금 시도가 아닌 실행의 성공 증거: 오케스트레이터가 스스로 닫은 시도의 늦은 성공(design.md:119, 123)과 거절이나 밀림으로
    # 닫힌 요청에 닿은 성공(노드가 스스로 한 배포 포함, design.md:336). 옛 노드 규칙(완화 A, design.md:431)이라
    # 「뒤 시도를 연 적이 없을 때만」 열린 대기를 성공으로 닫는다. 배포 중과 성공은 그대로다(증거만 남는다)
    success_evidence = (
        req.status.open_.pending.to(req.status.closed.success, cond="no_later")
        | req.status.open_.retry.to(req.status.closed.success, cond="no_later")
        | req.tag.t_off.to(req.tag.t_on, cond="closed_without_success")
    )
    # 지금 시도가 아닌 실행의 실패 증거(닫힌 시도의 늦은 실패, design.md:125): 처음 대기는 표시 줄 있는 대기로 가고,
    # 점검 필요는 요청 상태와 상관없이 켠다(배포 중이면 칸만 서고 도는 시도는 그대로, 닫힌 요청도 칸만 켠다)
    late_failed = req.status.open_.pending.to(req.status.open_.retry) | req.lock.l_off.to(
        req.lock.l_on
    )
    # 관측은 처음 대기이고 도는 시도가 없고 점검 필요가 꺼져 있을 때만 닫는다(design.md:382)
    observed = req.status.open_.pending.to(
        req.status.closed.success,
        cond=["heads_equal", "no_active_attempt", "no_failed_node_attempt", "unlocked"],
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
    # 보류 자기 갱신 되살림. 점검 필요가 켜져 있으면 표시 줄 있는 대기로 되살린다(점검 필요는 그대로 켜져 있다)
    reopen = (
        req.status.closed.rejected.to(
            req.status.open_.retry,
            cond=["self_update", "postponed_due", "no_other_open", "locked"],
        )
        | req.status.closed.rejected.to(
            req.status.open_.pending,
            cond=["self_update", "postponed_due", "no_other_open", "unlocked"],
        )
        # 바깥 status가 rejected이면 사유와 상관없이 되살린다(지금 코드). 잎 superseded는 사유 접두에서 온 구분이다
        | req.status.closed.superseded.to(
            req.status.open_.retry,
            cond=["self_update", "postponed_due", "no_other_open", "locked"],
        )
        | req.status.closed.superseded.to(
            req.status.open_.pending,
            cond=["self_update", "postponed_due", "no_other_open", "unlocked"],
        )
    )

    def newer(self) -> bool:
        return self.fact("newer")

    def not_newer(self) -> bool:
        return not self.fact("newer")

    def manual_single(self) -> bool:
        return self.fact("manual_single")

    def may_open(self) -> bool:
        """점검 필요가 꺼져 있거나, 켜져 있어도 원천이 단건 승인이면 시도를 연다."""
        return self.has("l_off") or self.fact("manual_single")

    def open_request(self) -> bool:
        return self.has("pending", "retry", "deploying")

    def no_later(self) -> bool:
        """이 증거의 시도보다 뒤 시도를 연 적이 없음(순서 칸이 비었거나 견줄 수 없으면 「있다」로 본다)."""
        return self.fact("no_later")

    def closed_without_success(self) -> bool:
        return self.has("rejected", "superseded")

    def locked(self) -> bool:
        return self.has("l_on")

    def unlocked(self) -> bool:
        return self.has("l_off")

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
        "begin_attempt": ("manual_single",),
        "attempt_succeeded": (),
        "attempt_failed": ("newer",),
        "send_failed": ("newer",),
        "reject": (),
        "newer_commit": (),
        "node_succeeded": (),
        "node_failed": ("no_active_attempt",),
        "success_evidence": ("no_later",),
        "late_failed": (),
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
