"""믿음 영역과 again 잎, 닫힘 일곱, hello 대조, 받기 함수, 할 일 기호(카드 #1259 PR 2b-2, 정의의 한 걸음만).

기대값은 design.md 141-145(hello 대조 표), 153-164(닫힘 일곱)와 fable_r4_product.py OrchReq의 사건 가지다.
판 없는 시도(node_protocol NULL)는 2a와 2b-1 규칙 그대로다: 모든 닫힘이 점검 필요를 켜고 again이 없다.
"""

import itertools

import pytest

from haniel_orch.deploy_attempt_intake import (
    CLOSURE_EXECUTION_UNKNOWN,
    CLOSURE_FAILED,
    CLOSURE_NOT_EXECUTED,
    Intake,
    closure_of,
    intake_deadline,
    intake_hello,
    intake_progress,
    intake_received,
    intake_send_failed,
    intake_terminal,
)
from haniel_orch.statechart.columns import LEAF_COLUMNS, REGION_COLUMNS
from haniel_orch.statechart.definitions import (
    REQUEST_OUTER_STATUS,
    REQUIRED_FACTS,
    AttemptEvidence,
    OrchRequest,
)
from haniel_orch.statechart.restore import NoTransition, advance, advance_with_todo, leaf_ids, regions

HELD = ("b_unknown", "b_queued", "b_running")
NO = "no"  # 「아니오」 닫힘: 점검 필요를 세우지 않는다
YES = "yes"  # 「예」 닫힘: 점검 필요를 켠다


def row(status="deploying", belief="b_unknown", lock="l_off", tag="t_off"):
    return {"status": status, "belief": belief, "lock": lock, "tag": tag}


def step(before, event, facts=None):
    return advance(OrchRequest, before, event, facts or {})


# ---------------------------------------------------------------- 영역과 잎


def test_request_definition_has_four_regions_with_one_leaf_column_each():
    assert set(regions(OrchRequest)) == {"status", "belief", "lock", "tag"}
    assert REGION_COLUMNS["OrchRequest"]["belief"] == "sm_belief"
    assert "sm_belief" in LEAF_COLUMNS
    assert set(leaf_ids(OrchRequest)) >= {"again", "b_none", "b_probe", "b_unknown", "b_queued", "b_running"}
    assert REQUEST_OUTER_STATUS["again"] == "pending"


def test_fable_event_names_are_all_present():
    events = set(REQUIRED_FACTS[OrchRequest])
    assert events >= {
        "auto_request",
        "plan_failed_journal",
        "plan_closed",
        "probe_deadline",
        "link_down",
        "declined",
        "ack_deadline",
        "acked",
        "started",
        "lease_deadline",
        "grace_deadline",
        "hello_missing",
        "hello_holds",
    }
    assert REQUIRED_FACTS[OrchRequest]["send_failed"] == ("newer", "legacy")


# ---------------------------------------------------------------- 닫힘 일곱 x 판 x lock x 더 새 커밋 (design.md 153-164)

# (사건, 믿음) -> 「아니오」인가 「예」인가. 판 1 시도에서만 뜻이 있다
CLOSINGS = {
    ("send_failed", "b_unknown"): NO,  # 보내기 실패
    ("declined", "b_unknown"): NO,  # 노드의 「실행하지 않음」 답(닿았는지 모름)
    ("declined", "b_queued"): NO,  # 노드의 「실행하지 않음」 답(줄 서 있음)
    ("ack_deadline", "b_unknown"): NO,  # 짧은 마감
    ("hello_missing", "b_unknown"): NO,  # hello 목록에 없는 닿았는지 모름
    ("hello_missing", "b_queued"): NO,  # hello 목록에 없는 줄 서 있음
    ("grace_deadline", "b_queued"): YES,  # 유예 마감
    ("hello_missing", "b_running"): YES,  # hello 목록에 없는 시작했다(노드 재시작으로 중단됨)
    ("lease_deadline", "b_running"): YES,  # 실행 임대 마감
    ("attempt_failed", "b_running"): YES,  # 노드 실패 보고
    ("attempt_failed", "b_queued"): YES,  # 노드 실패 보고가 줄 서 있음에 닿음(판 1에서는 없지만 정의는 받는다)
}


def _facts(event, newer):
    names = REQUIRED_FACTS[OrchRequest][event]
    return {name: (newer if name == "newer" else False) for name in names}


@pytest.mark.parametrize(
    ("event", "belief", "kind", "lock", "newer"),
    [
        (event, belief, kind, lock, newer)
        for (event, belief), kind in CLOSINGS.items()
        for lock in ("l_off", "l_on")
        for newer in (False, True)
    ],
)
def test_protocol_one_closure_matrix(event, belief, kind, lock, newer):
    """판 1 시도의 닫힘: 「아니오」는 점검 필요를 세우지 않고 again으로(서 있으면 retry), 「예」는 lock을 켜고 retry로.
    더 새 커밋이 있으면 어느 닫힘이든 밀림이다. 믿음은 도는 시도 없음으로 돌아간다."""
    after = step(row(belief=belief, lock=lock), event, _facts(event, newer))
    if newer:
        expected_status = "superseded"
    elif kind == YES or lock == "l_on":
        expected_status = "retry"
    else:
        expected_status = "again"
    expected_lock = "l_on" if (kind == YES or lock == "l_on") else "l_off"
    assert after == row(status=expected_status, belief="b_none", lock=expected_lock), (event, belief, lock, newer)


@pytest.mark.parametrize("lock", ["l_off", "l_on"])
@pytest.mark.parametrize("newer", [False, True])
def test_legacy_send_failed_locks_like_before(lock, newer):
    """판 없는 시도의 보내기 실패는 2a 그대로 점검 필요를 켜고 retry(또는 밀림)로 간다. again은 없다."""
    after = step(row(lock=lock), "send_failed", {"newer": newer, "legacy": True})
    assert after == row(status="superseded" if newer else "retry", belief="b_none", lock="l_on")


def test_negative_control_a_no_closure_on_a_standing_lock_goes_to_retry_not_again():
    """P8-2: 배포 중에 선 칸은 「아니오」 닫힘을 다시 대기가 아니라 실패 뒤 대기로 보낸다."""
    for event, belief in (("send_failed", "b_unknown"), ("declined", "b_queued"), ("ack_deadline", "b_unknown"), ("hello_missing", "b_unknown")):
        after = step(row(belief=belief, lock="l_on"), event, _facts(event, False))
        assert after["status"] == "retry" and after["lock"] == "l_on", event


def test_negative_control_a_no_closure_never_raises_the_lock():
    for event, belief in (("send_failed", "b_unknown"), ("declined", "b_unknown"), ("declined", "b_queued"), ("ack_deadline", "b_unknown"), ("hello_missing", "b_queued")):
        after = step(row(belief=belief), event, _facts(event, False))
        assert after == row(status="again", belief="b_none", lock="l_off"), event


@pytest.mark.parametrize(
    ("event", "belief"),
    [("ack_deadline", "b_queued"), ("ack_deadline", "b_running"), ("grace_deadline", "b_unknown"), ("grace_deadline", "b_running"), ("lease_deadline", "b_unknown"), ("lease_deadline", "b_queued"), ("declined", "b_running"), ("send_failed", "b_queued")],
)
def test_a_closure_whose_belief_does_not_match_has_no_transition(event, belief):
    """마감과 답은 믿음이 맞을 때만 전이가 있다(낡은 타이머는 호출부가 거르지만 정의도 받지 않는다)."""
    with pytest.raises(NoTransition):
        step(row(belief=belief), event, _facts(event, False))


# ---------------------------------------------------------------- hello 대조 (design.md 141-145)


@pytest.mark.parametrize(
    ("belief", "held", "expected"),
    [
        ("b_unknown", True, row(belief="b_queued")),
        ("b_queued", True, row(belief="b_queued")),
        ("b_running", True, row(belief="b_running")),
        ("b_unknown", False, row(status="again", belief="b_none")),
        ("b_queued", False, row(status="again", belief="b_none")),
        ("b_running", False, row(status="retry", belief="b_none", lock="l_on")),
    ],
)
def test_hello_reconciliation_table(belief, held, expected):
    event = "hello_holds" if held else "hello_missing"
    assert step(row(belief=belief), event, _facts(event, False)) == expected


def test_hello_events_do_nothing_without_a_held_attempt():
    for belief in ("b_none", "b_probe"):
        for event in ("hello_holds", "hello_missing"):
            with pytest.raises(NoTransition):
                step(row(status="pending", belief=belief), event, _facts(event, False))


# ---------------------------------------------------------------- 믿음을 옮기는 글


def test_acked_and_started_walk_the_belief():
    assert step(row(belief="b_unknown"), "acked") == row(belief="b_queued")
    assert step(row(belief="b_queued"), "acked") == row(belief="b_queued")
    assert step(row(belief="b_running"), "acked") == row(belief="b_running")
    assert step(row(belief="b_unknown"), "started") == row(belief="b_running")
    assert step(row(belief="b_queued"), "started") == row(belief="b_running")
    assert step(row(belief="b_running"), "started") == row(belief="b_running")
    for event in ("acked", "started"):
        with pytest.raises(NoTransition):
            step(row(status="pending", belief="b_none"), event)


def test_begin_attempt_sends_the_permit_from_idle_or_from_a_probe():
    after, todo = advance_with_todo(OrchRequest, row(status="pending", belief="b_none"), "begin_attempt", {"manual_single": False, "via_probe": False})
    assert after == row(status="deploying", belief="b_unknown") and todo == ["send_permit"]
    after, todo = advance_with_todo(OrchRequest, row(status="again", belief="b_probe"), "begin_attempt", {"manual_single": False, "via_probe": True})
    assert after == row(status="deploying", belief="b_unknown") and todo == ["send_permit"]
    after, todo = advance_with_todo(OrchRequest, row(status="retry", belief="b_probe", lock="l_on"), "begin_attempt", {"manual_single": True, "via_probe": True})
    assert after == row(status="deploying", belief="b_unknown", lock="l_off") and todo == ["send_permit"]
    # 점검 필요가 켜진 채 단건 승인이 아니면 열지 않고 허가도 없다
    with pytest.raises(NoTransition):
        advance_with_todo(OrchRequest, row(status="retry", belief="b_probe", lock="l_on"), "begin_attempt", {"manual_single": False, "via_probe": True})
    # 계획 묻기가 열려 있을 때 바로 승인하거나, 도는 시도가 없는데 계획 답으로 열 수는 없다
    with pytest.raises(NoTransition):
        step(row(status="pending", belief="b_probe"), "begin_attempt", {"manual_single": True, "via_probe": False})
    with pytest.raises(NoTransition):
        step(row(status="pending", belief="b_none"), "begin_attempt", {"manual_single": True, "via_probe": True})


def test_auto_request_probes_from_first_or_again_and_rejects_on_retry():
    after, todo = advance_with_todo(OrchRequest, row(status="pending", belief="b_none"), "auto_request")
    assert after == row(status="pending", belief="b_probe") and todo == ["send_probe"]
    after, todo = advance_with_todo(OrchRequest, row(status="again", belief="b_none"), "auto_request")
    assert after == row(status="again", belief="b_probe") and todo == ["send_probe"]
    after, todo = advance_with_todo(OrchRequest, row(status="retry", belief="b_none", lock="l_on"), "auto_request")
    assert after == row(status="retry", belief="b_none", lock="l_on") and todo == ["auto_reject"]
    # 계획 묻기나 시도가 이미 있으면, 배포 중이면, 닫혔으면 제자리(기호 없음)
    for before in (row(status="pending", belief="b_probe"), row(status="again", belief="b_probe"), row(status="deploying", belief="b_unknown"), row(status="success", belief="b_none"), row(status="rejected", belief="b_none"), row(status="superseded", belief="b_none")):
        after, todo = advance_with_todo(OrchRequest, before, "auto_request")
        assert after == before and todo == [], before


def test_open_probe_for_an_inspected_approval_needs_a_marked_wait_and_no_running_attempt():
    after, todo = advance_with_todo(OrchRequest, row(status="retry", belief="b_none", lock="l_on"), "open_probe")
    assert after == row(status="retry", belief="b_probe", lock="l_on") and todo == ["send_probe"]
    after, todo = advance_with_todo(OrchRequest, row(status="again", belief="b_none"), "open_probe")
    assert after == row(status="again", belief="b_probe") and todo == ["send_probe"]
    for before in (row(status="pending", belief="b_none"), row(status="retry", belief="b_probe", lock="l_on"), row(status="deploying", belief="b_unknown")):
        with pytest.raises(NoTransition):
            step(before, "open_probe")


def test_probe_closings_return_the_belief_to_idle_with_their_symbols():
    probing = row(status="again", belief="b_probe")
    assert advance_with_todo(OrchRequest, probing, "plan_closed") == (row(status="again", belief="b_none"), ["closed_plan"])
    assert advance_with_todo(OrchRequest, probing, "plan_rejected") == (row(status="again", belief="b_none"), ["auto_reject"])
    assert advance_with_todo(OrchRequest, probing, "probe_deadline") == (row(status="again", belief="b_none"), [])
    assert advance_with_todo(OrchRequest, probing, "link_down") == (row(status="again", belief="b_none"), [])
    assert step(probing, "reject") == row(status="rejected", belief="b_none")
    assert step(probing, "newer_commit", {}) == row(status="superseded", belief="b_none")


def test_plan_failed_journal_locks_and_rejects_the_auto_probe():
    for status in ("pending", "again"):
        after, todo = advance_with_todo(OrchRequest, row(status=status, belief="b_probe"), "plan_failed_journal")
        assert after == row(status="retry", belief="b_none", lock="l_on") and todo == ["auto_reject"]
    after, todo = advance_with_todo(OrchRequest, row(status="retry", belief="b_probe", lock="l_on"), "plan_failed_journal")
    assert after == row(status="retry", belief="b_none", lock="l_on") and todo == ["auto_reject"]


def test_link_down_only_closes_the_probe_and_leaves_held_beliefs():
    for belief in ("b_none", "b_unknown", "b_queued", "b_running"):
        before = row(status="deploying" if belief != "b_none" else "pending", belief=belief)
        assert step(before, "link_down") == before


def test_newer_commit_is_accepted_everywhere_and_only_closes_waits():
    for status in ("pending", "again", "retry"):
        assert step(row(status=status, belief="b_none", lock="l_on" if status == "retry" else "l_off"), "newer_commit")["status"] == "superseded"
    for before in (row(status="deploying", belief="b_running"), row(status="success", belief="b_none"), row(status="rejected", belief="b_none"), row(status="superseded", belief="b_none")):
        assert step(before, "newer_commit") == before


def test_closing_the_current_attempt_returns_the_belief_to_idle():
    for belief in HELD:
        assert step(row(belief=belief), "attempt_succeeded") == row(status="success", belief="b_none")
        assert step(row(belief=belief), "attempt_failed", {"newer": False}) == row(status="retry", belief="b_none", lock="l_on")
    assert step(row(belief="b_queued"), "node_succeeded") == row(status="success", belief="b_none")
    assert step(row(status="again", belief="b_probe"), "node_succeeded") == row(status="success", belief="b_none")


def test_success_evidence_closes_the_belief_only_when_no_later_attempt_was_opened():
    assert step(row(status="again", belief="b_probe"), "success_evidence", {"no_later": True}) == row(status="success", belief="b_none")
    # 배포 중(도는 시도가 있어 사실은 거짓)에는 전이가 없다. 증거만 남는다(2b-1 그대로)
    with pytest.raises(NoTransition):
        step(row(status="deploying", belief="b_running"), "success_evidence", {"no_later": False})
    assert step(row(status="again", belief="b_none"), "success_evidence", {"no_later": True}) == row(status="success", belief="b_none")


def test_again_is_an_open_wait_that_approval_auto_request_and_late_failure_treat_like_a_marked_wait():
    assert step(row(status="again", belief="b_none"), "late_failed") == row(status="retry", belief="b_none", lock="l_on")
    assert step(row(status="again", belief="b_none"), "node_failed", {"no_active_attempt": True}) == row(status="retry", belief="b_none", lock="l_on")
    assert step(row(status="again", belief="b_none"), "reject") == row(status="rejected", belief="b_none")


def test_observed_closes_only_a_first_wait_without_a_running_attempt_and_without_the_lock():
    facts = {"heads_equal": True, "no_active_attempt": True, "no_failed_node_attempt": True}
    assert step(row(status="pending", belief="b_none"), "observed", facts) == row(status="success", belief="b_none")
    for before in (row(status="again", belief="b_none"), row(status="retry", belief="b_none", lock="l_on"), row(status="pending", belief="b_none", lock="l_on")):
        with pytest.raises(NoTransition):
            step(before, "observed", facts)


# ---------------------------------------------------------------- 할 일 기호와 증거 정의


def test_symbols_come_only_from_the_definition_and_advance_stays_row_only():
    after, todo = advance_with_todo(OrchRequest, row(status="pending", belief="b_none"), "node_failed", {"no_active_attempt": True})
    assert after == row(status="retry", belief="b_none", lock="l_on") and todo == ["ack"]
    after, todo = advance_with_todo(OrchRequest, row(status="rejected", belief="b_none"), "success_evidence", {"no_later": False})
    assert after == row(status="rejected", belief="b_none", tag="t_on") and todo == ["tag"]
    assert advance(OrchRequest, row(status="pending", belief="b_none"), "node_failed", {"no_active_attempt": True}) == row(status="retry", belief="b_none", lock="l_on")


@pytest.mark.parametrize(
    ("leaf", "event", "facts", "expected"),
    [
        ({"ev": "ev_settled"}, "result_ok", {"execute_mode": True}, ("ev_both", ["terminal_ok"])),
        ({"ev": "ev_result"}, "settled_ok", {"execute_mode": True}, ("ev_both", ["terminal_ok"])),
        ({"ev": "ev_none"}, "recovered", {"recovery_mode": True}, ("ev_both", ["terminal_ok"])),
        ({"ev": "ev_none"}, "fail", {}, ("ev_failed", ["terminal_fail"])),
        ({"ev": "ev_result"}, "fail", {}, ("ev_failed", ["terminal_fail"])),
        ({"ev": "ev_none"}, "result_ok", {"execute_mode": True}, ("ev_result", [])),
        ({"ev": "ev_both"}, "result_ok", {"execute_mode": True}, ("ev_both", ["dup"])),
        ({"ev": "ev_both"}, "settled_ok", {"execute_mode": True}, ("ev_both", ["dup"])),
        ({"ev": "ev_failed"}, "fail", {}, ("ev_failed", ["dup"])),
        ({"ev": "ev_failed"}, "result_ok", {"execute_mode": True}, ("ev_failed", ["dup"])),
        ({"ev": "ev_both"}, "fail", {}, ("ev_both", ["dup"])),
        ({"ev": "ev_both"}, "recovered", {"recovery_mode": True}, ("ev_both", ["dup"])),
    ],
)
def test_evidence_symbols_terminal_ok_terminal_fail_and_dup(leaf, event, facts, expected):
    after, todo = advance_with_todo(AttemptEvidence, leaf, event, facts)
    assert (after["ev"], todo) == expected


def test_evidence_superseded_by_node_has_no_dup_and_no_symbol():
    with pytest.raises(NoTransition):
        advance(AttemptEvidence, {"ev": "ev_superseded"}, "result_ok", {"execute_mode": True})
    assert advance_with_todo(AttemptEvidence, {"ev": "ev_none"}, "superseded_by_node") == ({"ev": "ev_superseded"}, [])


# ---------------------------------------------------------------- 받기 함수(순수)


def test_intake_terminal_is_declined_only_for_a_protocol_one_attempt_that_has_not_started():
    assert intake_terminal(node_protocol=1, belief="b_unknown", newer=False) == Intake("declined", {"newer": False})
    assert intake_terminal(node_protocol=1, belief="b_queued", newer=True) == Intake("declined", {"newer": True})
    # 시작했다 뒤에 온 「실행하지 않음」은 실제 실행으로 본다(4e)
    assert intake_terminal(node_protocol=1, belief="b_running", newer=False) == Intake("attempt_failed", {"newer": False})
    assert intake_terminal(node_protocol=None, belief="b_unknown", newer=False) == Intake("attempt_failed", {"newer": False})


def test_intake_deadline_matches_kind_and_belief_or_is_stale():
    assert intake_deadline(node_protocol=1, deadline_kind="ack", belief="b_unknown", newer=False) == Intake("ack_deadline", {"newer": False})
    assert intake_deadline(node_protocol=1, deadline_kind="grace", belief="b_queued", newer=False) == Intake("grace_deadline", {"newer": False})
    assert intake_deadline(node_protocol=1, deadline_kind="lease", belief="b_running", newer=True) == Intake("lease_deadline", {"newer": True})
    for kind, belief in (("ack", "b_queued"), ("grace", "b_running"), ("lease", "b_queued"), ("none", "b_queued"), ("ack", "b_running")):
        assert intake_deadline(node_protocol=1, deadline_kind=kind, belief=belief, newer=False) is None, (kind, belief)
    # 판 없는 시도는 지금 마감 하나(실행 임대)이고 믿음을 보지 않는다
    for belief in ("b_unknown", "b_running"):
        assert intake_deadline(node_protocol=None, deadline_kind=None, belief=belief, newer=False) == Intake("attempt_failed", {"newer": False})
        assert intake_deadline(node_protocol=None, deadline_kind="lease", belief=belief, newer=False) == Intake("attempt_failed", {"newer": False})


def test_intake_hello_compares_only_protocol_one_held_beliefs_against_a_present_list():
    assert intake_hello(node_protocol=1, belief="b_unknown", held={"a1"}, attempt_id="a1", newer=False) == Intake("hello_holds", {})
    assert intake_hello(node_protocol=1, belief="b_running", held=set(), attempt_id="a1", newer=False) == Intake("hello_missing", {"newer": False})
    assert intake_hello(node_protocol=1, belief="b_queued", held={"other"}, attempt_id="a1", newer=True) == Intake("hello_missing", {"newer": True})
    assert intake_hello(node_protocol=1, belief="b_unknown", held=None, attempt_id="a1", newer=False) is None
    assert intake_hello(node_protocol=None, belief="b_unknown", held=set(), attempt_id="a1", newer=False) is None
    assert intake_hello(node_protocol=1, belief="b_none", held=set(), attempt_id="a1", newer=False) is None


def test_intake_received_progress_and_send_failed():
    assert intake_received(node_protocol=1, active=True) == Intake("acked", {})
    assert intake_received(node_protocol=1, active=False) is None
    assert intake_received(node_protocol=None, active=True) is None
    assert intake_progress(active=True) == Intake("started", {})
    assert intake_progress(active=False) is None
    assert intake_send_failed(node_protocol=1, newer=False) == Intake("send_failed", {"newer": False, "legacy": False})
    assert intake_send_failed(node_protocol=None, newer=True) == Intake("send_failed", {"newer": True, "legacy": True})


def test_closure_kind_follows_the_seven_closings():
    assert closure_of("send_failed", "b_unknown") == CLOSURE_NOT_EXECUTED
    assert closure_of("declined", "b_queued") == CLOSURE_NOT_EXECUTED
    assert closure_of("ack_deadline", "b_unknown") == CLOSURE_EXECUTION_UNKNOWN
    assert closure_of("hello_missing", "b_queued") == CLOSURE_EXECUTION_UNKNOWN
    assert closure_of("hello_missing", "b_running") == CLOSURE_FAILED
    assert closure_of("grace_deadline", "b_queued") == CLOSURE_FAILED
    assert closure_of("lease_deadline", "b_running") == CLOSURE_FAILED
    assert closure_of("attempt_failed", "b_unknown") == CLOSURE_FAILED


def test_every_closure_the_intake_can_name_has_a_definition_transition():
    """받기 함수가 내는 (사건, 믿음) 짝마다 정의에 전이가 있다(둘이 어긋나면 노드 글이 조용히 사라진다)."""
    seen = set()
    for belief in HELD:
        for newer in (False, True):
            seen.add((intake_terminal(node_protocol=1, belief=belief, newer=newer).event, belief))
            for kind in ("ack", "grace", "lease"):
                intake = intake_deadline(node_protocol=1, deadline_kind=kind, belief=belief, newer=newer)
                if intake is not None:
                    seen.add((intake.event, belief))
            for held in (set(), {"a1"}):
                seen.add((intake_hello(node_protocol=1, belief=belief, held=held, attempt_id="a1", newer=newer).event, belief))
    for event, belief in seen:
        for lock, newer in itertools.product(("l_off", "l_on"), (False, True)):
            step(row(belief=belief, lock=lock), event, _facts(event, newer))
