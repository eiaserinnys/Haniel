"""배포 요청과 시도의 상태 정의(카드 #1259 PR 1에서 정의로 옮기고, PR 2a가 「점검 필요」 영역을, PR 2b-1이
닫힌 시도의 늦은 글 규칙과 「닫힌 뒤 적용」 꼬리표 영역을, PR 2b-2가 「지금 시도에 대한 믿음」 영역과 다시 대기(again),
닫힘 일곱, hello 대조, 믿음별 마감 사건, 할 일 기호를 더했다).

설계 정의(fable_r4_product.py의 OrchReq, AttemptEvidence)와 다른 전이는 `divergence.DIVERGENCE`에 표시를 달고
그림에도 드러낸다. 이 정의의 전이는 모두 저장소 변경 잠금 안의 한 걸음(`steps`, `attempt_steps`)으로만 일어난다.
사건과 사실의 이름은 fable을 따르고, 2a에 이미 있던 제품 이름(begin_attempt, attempt_succeeded, attempt_failed,
node_succeeded, node_failed)은 그대로 둔다. fable의 사실 up, human, newer, later, p81은 제품에서 연결 확인(호출부),
manual_single, newer, no_later, 늘 참(P8-1을 빼지 않는다)에 대응하고, 판을 가르는 사실 legacy를 더했다.

할 일 기호는 전이의 on 콜백이 `model.todo`에 쌓고 `restore.advance_with_todo`가 돌려준다. DB에 적지 않는다.
호출부는 허가 보내기(send_permit), 탐침 보내기(send_probe), 자동 거절 답(auto_reject), 닫힌 계획 기록(closed_plan),
꼬리표(tag), 노드 스스로 한 배포 보고의 답(ack)을 그 기호가 나왔을 때만 한다. 시도 보고의 「종결을 받았다」는
증거 정의의 기호(terminal_ok, terminal_fail, dup)가 정한다.

울타리(설계 4절): 내부 제자리 전이, 이력 상태, delay, invoke를 쓰지 않고, 최상위와 병렬 직속에 final을 두지 않는다.
fable의 to.itself()는 external 제자리 전이로 둔다. 시험이 지킨다.
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


class OrchRequest(_Base):
    """오케스트레이터의 배포 요청 한 건. 영역은 요청 상태, 지금 시도에 대한 믿음, 점검 필요, 닫힌 뒤 적용 꼬리표 넷이다.

    믿음(belief)은 지금 시도에 대해 오케스트레이터가 아는 것이다: 도는 시도 없음, 계획을 물음, 허가를 보냈으나 닿았는지
    모름, 노드가 받았다고 답해 줄 서 있음, 노드가 시작했다고 알림. 판 1 노드(wire-v1)만 받았다를 보내고 hello 목록을
    실으므로 판 없는 시도는 닿았는지 모름에서 첫 진행 알림으로 시작했다로만 걷는다.

    점검 필요(lock)는 「실제로 돌다 실패한 커밋」을 점검 뒤 승인 말고는 시도로 열지 못하게 잠근다. 판 1 시도에서는
    실제로 돌다 실패한 닫힘(노드 실패 보고, 실행 임대 마감, 유예 마감, hello 목록에 없는 시작했다)과 늦은 실패만 켠다.
    실행하지 않았거나 실행 여부가 불명한 닫힘(보내기 실패, 노드의 「실행하지 않음」 답, 짧은 마감, hello 목록에 없는
    닿았는지 모름과 줄 서 있음)은 켜지 않고 다시 대기(again)로 가되, 이미 켜져 있으면 실패 뒤 대기(retry)로 간다(P8-2).
    판 없는 시도는 옛 노드 규칙(design.md:429-430)대로 모든 닫힘이 켠다. 내리는 곳은 원천이 단건 승인인 begin_attempt 하나다.

    닫힌 뒤 적용 꼬리표(tag)는 거절이나 밀림으로 닫힌 요청에 성공 증거가 닿았음을 드러낸다(어긋남 12). 한 번 서면 내리지 않는다.
    """

    class req(State.Parallel):
        class status(State.Compound):
            class open_(State.Compound):
                pending = State(initial=True)  # 처음 대기. 표시 줄 없음
                again = State()  # 다시 대기. 표시 줄 있고 점검 필요 꺼짐. 바깥 칸은 pending
                retry = State()  # 실패 뒤 대기. 표시 줄 있고 점검 필요 켜짐. 바깥 칸은 pending
                deploying = State()  # 배포 중. 시도가 돈다

            class closed(State.Compound):
                success = State(initial=True)
                rejected = State()
                superseded = State()  # 새 커밋에 밀림. 바깥 칸은 rejected, 사유가 superseded by

        class belief(State.Compound):
            b_none = State(initial=True)  # 도는 시도 없음
            b_probe = State()  # 계획을 물었고 답을 기다림(연결에 묶임)
            b_unknown = State()  # 허가를 보냄, 닿았는지 모름
            b_queued = State()  # 노드가 받았다고 답함, 줄 서 있음
            b_running = State()  # 노드가 시작했다고 알림

        class lock(State.Compound):
            l_off = State(initial=True)  # 점검 필요가 서지 않음
            l_on = State()  # 점검 필요가 섬. 단건 승인이 새 시도를 열 때에만 내린다

        class tag(State.Compound):
            t_off = State(initial=True)  # 꼬리표 없음
            t_on = State()  # 닫힌 뒤 적용. 거절이나 밀림으로 닫힌 요청에 성공 증거가 닿았다

    # 승인해서 시도를 연다. 처음 승인은 도는 시도 없음에서, 계획 답 뒤 승인은 계획을 물음에서 허가를 보낸다.
    # 점검 필요가 켜져 있으면 원천이 단건 승인(manual_single)일 때만 열고, 그때 점검 필요를 내린다
    begin_attempt = (
        req.status.open_.pending.to(req.status.open_.deploying, cond=["may_open", "opens_belief"])
        | req.status.open_.again.to(req.status.open_.deploying, cond=["may_open", "opens_belief"])
        | req.status.open_.retry.to(req.status.open_.deploying, cond=["may_open", "opens_belief"])
        | req.belief.b_none.to(req.belief.b_unknown, cond=["may_open", "not_via_probe"], on="send_permit")
        | req.belief.b_probe.to(req.belief.b_unknown, cond=["may_open", "via_probe"], on="send_permit")
        | req.lock.l_on.to(req.lock.l_off, cond=["manual_single", "opens_belief"])
    )
    # 점검 뒤 승인이 표시 줄 있는 대기(실행했을 수 있는 시도가 있는)에서 계획을 묻는다
    open_probe = req.belief.b_none.to(req.belief.b_probe, cond="marked_wait", on="send_probe")
    # 노드가 자동 배포를 청한다. 처음 대기나 다시 대기이고 도는 시도가 없으면 계획을 묻고, 점검 필요가 섰으면(retry)
    # 단건 승인이 필요하다고 답한다. 그 밖(이미 묻는 중, 배포 중, 닫힘)은 제자리다
    auto_request = (
        req.belief.b_none.to(req.belief.b_probe, cond="auto_may_probe", on="send_probe")
        | req.status.open_.retry.to.itself(on="send_auto_reject")
        | req.status.open_.pending.to.itself(cond="not_idle")
        | req.status.open_.again.to.itself(cond="not_idle")
        | req.status.open_.deploying.to.itself()
        | req.status.closed.success.to.itself()
        | req.status.closed.rejected.to.itself()
        | req.status.closed.superseded.to.itself()
    )
    # 자동 탐침의 계획 답에 「아니오」로 닫힌 계보 시도의 실패 저널이 실려 왔다. 허가를 보내지 않고 점검 필요를 세운다
    plan_failed_journal = (
        req.belief.b_probe.to(req.belief.b_none, on="send_auto_reject")
        | req.status.open_.pending.to(req.status.open_.retry)
        | req.status.open_.again.to(req.status.open_.retry)
        | req.status.open_.retry.to.itself()
        | req.lock.l_off.to(req.lock.l_on)
    )
    plan_closed = req.belief.b_probe.to(req.belief.b_none, on="note_closed_plan")  # 노드의 답이나 재확인이 계획을 닫았다
    plan_rejected = req.belief.b_probe.to(req.belief.b_none, on="send_auto_reject")  # 자동 탐침을 점검 필요가 막았다
    probe_deadline = req.belief.b_probe.to(req.belief.b_none)
    # 오케스트레이터가 끊김을 안다. 계획 묻기만 닫는다. 나머지 믿음은 마감이나 hello 대조가 닫는다
    link_down = (
        req.belief.b_probe.to(req.belief.b_none)
        | req.belief.b_none.to.itself()
        | req.belief.b_unknown.to.itself()
        | req.belief.b_queued.to.itself()
        | req.belief.b_running.to.itself()
    )
    attempt_succeeded = (
        req.status.open_.deploying.to(req.status.closed.success)
        | req.belief.b_unknown.to(req.belief.b_none)
        | req.belief.b_queued.to(req.belief.b_none)
        | req.belief.b_running.to(req.belief.b_none)
    )
    # 시도가 실패로 끝난다(노드 실패 보고, 판 없는 시도의 모든 마감, 시작한 뒤에 온 「실행하지 않음」).
    # 점검 필요는 요청이 이미 닫혀 있어도 켠다(닫힌 줄의 상태는 쓰지 않는다)
    attempt_failed = (
        req.status.open_.deploying.to(req.status.closed.superseded, cond="newer")
        | req.status.open_.deploying.to(req.status.open_.retry, cond="not_newer")
        | req.belief.b_unknown.to(req.belief.b_none)
        | req.belief.b_queued.to(req.belief.b_none)
        | req.belief.b_running.to(req.belief.b_none)
        | req.lock.l_off.to(req.lock.l_on)
    )
    # 허가가 노드에 닿지 못한 보내기 실패. 판 1 시도는 실행하지 않음이 확실해 점검 필요를 세우지 않고 다시 대기로
    # (서 있으면 실패 뒤 대기로) 간다. 판 없는 시도는 옛 노드 규칙이라 점검 필요를 켠다(design.md:429-430)
    send_failed = (
        req.belief.b_unknown.to(req.belief.b_none)
        | req.status.open_.deploying.to(req.status.closed.superseded, cond=["is_unknown", "newer"])
        | req.status.open_.deploying.to(req.status.open_.retry, cond=["is_unknown", "not_newer", "legacy"])
        | req.status.open_.deploying.to(req.status.open_.retry, cond=["is_unknown", "not_newer", "not_legacy", "locked"])
        | req.status.open_.deploying.to(req.status.open_.again, cond=["is_unknown", "not_newer", "not_legacy", "unlocked"])
        | req.lock.l_off.to(req.lock.l_on, cond=["is_unknown", "legacy"])
    )
    # 노드의 「실행하지 않음」 답(판 1, 시작하기 전). 실행하지 않음이 확실하다
    declined = (
        req.belief.b_unknown.to(req.belief.b_none)
        | req.belief.b_queued.to(req.belief.b_none)
        | req.status.open_.deploying.to(req.status.closed.superseded, cond=["is_unknown_or_queued", "newer"])
        | req.status.open_.deploying.to(req.status.open_.retry, cond=["is_unknown_or_queued", "not_newer", "locked"])
        | req.status.open_.deploying.to(req.status.open_.again, cond=["is_unknown_or_queued", "not_newer", "unlocked"])
    )
    # 닿았는지 모름의 짧은 마감(판 1). 실행 여부 불명. 다시 대기로
    ack_deadline = (
        req.belief.b_unknown.to(req.belief.b_none)
        | req.status.open_.deploying.to(req.status.closed.superseded, cond=["is_unknown", "newer"])
        | req.status.open_.deploying.to(req.status.open_.retry, cond=["is_unknown", "not_newer", "locked"])
        | req.status.open_.deploying.to(req.status.open_.again, cond=["is_unknown", "not_newer", "unlocked"])
    )
    acked = (
        req.belief.b_unknown.to(req.belief.b_queued)
        | req.belief.b_queued.to.itself()
        | req.belief.b_running.to.itself()
    )
    started = (
        req.belief.b_unknown.to(req.belief.b_running)
        | req.belief.b_queued.to(req.belief.b_running)
        | req.belief.b_running.to.itself()
    )
    # 시작했다 뒤의 실행 임대 마감(판 1). 실제로 돌다 멈춘 것으로 보아 점검 필요를 켠다
    lease_deadline = (
        req.belief.b_running.to(req.belief.b_none)
        | req.status.open_.deploying.to(req.status.closed.superseded, cond=["is_running", "newer"])
        | req.status.open_.deploying.to(req.status.open_.retry, cond=["is_running", "not_newer"])
        | req.lock.l_off.to(req.lock.l_on, cond="is_running")
    )
    # 줄 서 있음이고 끊긴 동안에만 세는 유예 마감(판 1). 살아 있는 노드가 실행했을 수 있으므로 점검 필요를 켠다
    grace_deadline = (
        req.belief.b_queued.to(req.belief.b_none)
        | req.status.open_.deploying.to(req.status.closed.superseded, cond=["is_queued", "newer"])
        | req.status.open_.deploying.to(req.status.open_.retry, cond=["is_queued", "not_newer"])
        | req.lock.l_off.to(req.lock.l_on, cond="is_queued")
    )
    # 다시 붙은 노드의 hello 목록에 지금 시도가 없다. 닿았는지 모름과 줄 서 있음은 실행 여부 불명이고,
    # 시작했다는 노드 재시작으로 중단된 실패다(design.md:141-145)
    hello_missing = (
        req.belief.b_unknown.to(req.belief.b_none)
        | req.belief.b_queued.to(req.belief.b_none)
        | req.belief.b_running.to(req.belief.b_none)
        | req.status.open_.deploying.to(req.status.closed.superseded, cond=["is_held", "newer"])
        | req.status.open_.deploying.to(req.status.open_.retry, cond=["is_unknown_or_queued", "not_newer", "locked"])
        | req.status.open_.deploying.to(req.status.open_.again, cond=["is_unknown_or_queued", "not_newer", "unlocked"])
        | req.status.open_.deploying.to(req.status.open_.retry, cond=["is_running", "not_newer"])
        | req.lock.l_off.to(req.lock.l_on, cond="is_running")
    )
    hello_holds = (
        req.belief.b_unknown.to(req.belief.b_queued)
        | req.belief.b_queued.to.itself()
        | req.belief.b_running.to.itself()
    )
    reject = (
        req.status.open_.pending.to(req.status.closed.rejected)
        | req.status.open_.again.to(req.status.closed.rejected)
        | req.status.open_.retry.to(req.status.closed.rejected)
        | req.belief.b_probe.to(req.belief.b_none)
    )
    # 더 새 커밋이 들어온다. 대기 셋은 밀리고 열린 계획 묻기는 닫힌다. 배포 중과 닫힘은 제자리다
    newer_commit = (
        req.status.open_.pending.to(req.status.closed.superseded)
        | req.status.open_.again.to(req.status.closed.superseded)
        | req.status.open_.retry.to(req.status.closed.superseded)
        | req.belief.b_probe.to(req.belief.b_none)
        | req.status.open_.deploying.to.itself()
        | req.status.closed.success.to.itself()
        | req.status.closed.rejected.to.itself()
        | req.status.closed.superseded.to.itself()
    )
    # 노드가 스스로 한 배포의 성공. 열린 요청을 닫고 도는 시도와 계획 묻기를 함께 닫는다
    node_succeeded = (
        req.status.open_.pending.to(req.status.closed.success, on="ack")
        | req.status.open_.again.to(req.status.closed.success, on="ack")
        | req.status.open_.retry.to(req.status.closed.success, on="ack")
        | req.status.open_.deploying.to(req.status.closed.success, on="ack")
        | req.belief.b_probe.to(req.belief.b_none)
        | req.belief.b_unknown.to(req.belief.b_none)
        | req.belief.b_queued.to(req.belief.b_none)
        | req.belief.b_running.to(req.belief.b_none)
    )
    # 노드가 스스로 한 배포의 실패. 열린 요청이면 점검 필요를 켠다. 도는 시도가 있으면 칸만 켜고 시도와 요청 상태는 그대로 둔다
    node_failed = (
        req.status.open_.pending.to(req.status.open_.retry, on="ack")
        | req.status.open_.again.to(req.status.open_.retry, on="ack")
        | req.status.open_.deploying.to(req.status.open_.retry, cond="no_active_attempt", on="ack")
        | req.lock.l_off.to(req.lock.l_on, cond="open_request")
    )
    # 지금 시도가 아닌 실행의 성공 증거: 오케스트레이터가 스스로 닫은 시도의 늦은 성공(design.md:119, 123)과 거절이나 밀림으로
    # 닫힌 요청에 닿은 성공(노드가 스스로 한 배포 포함, design.md:336). 옛 노드 규칙(완화 A, design.md:431)이라
    # 「뒤 시도를 연 적이 없을 때만」 열린 대기를 성공으로 닫는다. 배포 중과 성공은 그대로다(증거만 남는다)
    success_evidence = (
        req.status.open_.pending.to(req.status.closed.success, cond="no_later")
        | req.status.open_.again.to(req.status.closed.success, cond="no_later")
        | req.status.open_.retry.to(req.status.closed.success, cond="no_later")
        | req.belief.b_probe.to(req.belief.b_none, cond="no_later")
        | req.belief.b_unknown.to(req.belief.b_none, cond="no_later")
        | req.belief.b_queued.to(req.belief.b_none, cond="no_later")
        | req.belief.b_running.to(req.belief.b_none, cond="no_later")
        | req.tag.t_off.to(req.tag.t_on, cond="closed_without_success", on="note_tag")
    )
    # 지금 시도가 아닌 실행의 실패 증거(닫힌 시도의 늦은 실패, design.md:125): 처음 대기와 다시 대기는 실패 뒤 대기로 가고,
    # 점검 필요는 요청 상태와 상관없이 켠다(배포 중이면 칸만 서고 도는 시도는 그대로, 닫힌 요청도 칸만 켠다)
    late_failed = (
        req.status.open_.pending.to(req.status.open_.retry)
        | req.status.open_.again.to(req.status.open_.retry)
        | req.lock.l_off.to(req.lock.l_on)
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
        | req.status.open_.again.to(
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

    # ---- 조건. 사실은 fact()로, 다른 영역의 잎은 has()로 읽는다(걸음 전의 구성)
    def newer(self) -> bool:
        return self.fact("newer")

    def not_newer(self) -> bool:
        return not self.fact("newer")

    def legacy(self) -> bool:
        return self.fact("legacy")

    def not_legacy(self) -> bool:
        return not self.fact("legacy")

    def manual_single(self) -> bool:
        return self.fact("manual_single")

    def may_open(self) -> bool:
        """점검 필요가 꺼져 있거나, 켜져 있어도 원천이 단건 승인이면 시도를 연다."""
        return self.has("l_off") or self.fact("manual_single")

    def via_probe(self) -> bool:
        return self.fact("via_probe")

    def not_via_probe(self) -> bool:
        return not self.fact("via_probe")

    def opens_belief(self) -> bool:
        """허가를 보낼 믿음 자리가 맞는가: 처음 승인은 도는 시도 없음에서, 계획 답 뒤 승인은 계획을 물음에서."""
        return self.has("b_probe") if self.fact("via_probe") else self.has("b_none")

    def marked_wait(self) -> bool:
        return self.has("retry", "again")

    def auto_may_probe(self) -> bool:
        return self.has("pending", "again") and self.has("b_none")

    def not_idle(self) -> bool:
        return not self.has("b_none")

    def is_unknown(self) -> bool:
        return self.has("b_unknown")

    def is_queued(self) -> bool:
        return self.has("b_queued")

    def is_running(self) -> bool:
        return self.has("b_running")

    def is_unknown_or_queued(self) -> bool:
        return self.has("b_unknown", "b_queued")

    def is_held(self) -> bool:
        return self.has("b_unknown", "b_queued", "b_running")

    def open_request(self) -> bool:
        return self.has("pending", "again", "retry", "deploying")

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

    # ---- 할 일 기호
    def send_permit(self) -> None:
        self.out("send_permit")

    def send_probe(self) -> None:
        self.out("send_probe")

    def send_auto_reject(self) -> None:
        self.out("auto_reject")

    def note_closed_plan(self) -> None:
        self.out("closed_plan")

    def ack(self) -> None:
        self.out("ack")

    def note_tag(self) -> None:
        self.out("tag")


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


# 사건마다 호출부가 넣어야 하는 사실. 빠지면 한 걸음이 바로 실패한다.
REQUIRED_FACTS: dict[type, dict[str, tuple[str, ...]]] = {
    OrchRequest: {
        "begin_attempt": ("manual_single", "via_probe"),
        "open_probe": (),
        "auto_request": (),
        "plan_failed_journal": (),
        "plan_closed": (),
        "plan_rejected": (),
        "probe_deadline": (),
        "link_down": (),
        "attempt_succeeded": (),
        "attempt_failed": ("newer",),
        "send_failed": ("newer", "legacy"),
        "declined": ("newer",),
        "ack_deadline": ("newer",),
        "acked": (),
        "started": (),
        "lease_deadline": ("newer",),
        "grace_deadline": ("newer",),
        "hello_missing": ("newer",),
        "hello_holds": (),
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

# 믿음 가운데 노드가 시도를 쥐고 있을 수 있는 셋(hello 대조와 마감의 대상)
BELIEF_HELD = frozenset({"b_unknown", "b_queued", "b_running"})

# 바깥 칸(status)으로 쓰이는 값. 대기 셋은 pending, 밀림은 rejected다.
REQUEST_OUTER_STATUS = {
    "pending": "pending",
    "again": "pending",
    "retry": "pending",
    "deploying": "deploying",
    "success": "success",
    "rejected": "rejected",
    "superseded": "rejected",
}
