import pytest

from .explore import run
from .system import System


FULL = pytest.mark.sweep_full
TIMEOUT = pytest.mark.timeout(1200)


def test_manual_approval_opens_an_attempt_through_the_product_definition():
    system = System()
    successors = system.successors(system.initial())
    sent = [state for label, kind, state in successors
            if kind == "human" and ("permit", 1) in state.qon]
    assert sent
    assert all((state.st, state.bl, state.opened) == ("deploying", "b_unknown", 1) for state in sent)


BRANCHES = [
    pytest.param("가", {}, id="ga"),
    pytest.param("나", {"asym": True}, id="na"),
    pytest.param("다", {"restart": "legacy"}, id="da", marks=FULL),
    pytest.param("라", {"restart": "manifest", "asym": True}, id="ra", marks=FULL),
    pytest.param("마", {"auto": True}, id="ma", marks=FULL),
    pytest.param("바", {"auto": True, "restart": "legacy"}, id="ba", marks=FULL),
    pytest.param("사", {"newer": True, "restart": "legacy"}, id="sa", marks=FULL),
    pytest.param("아", {"orestart": True, "asym": True}, id="ah"),
    pytest.param("자", {"auto": True, "orestart": True}, id="ja", marks=FULL),
    pytest.param("차", {"auto": True, "restart": "manifest"}, id="cha", marks=FULL),
]

ZERO_METRICS = (
    "가 길 없음", "가 반드시 아님", "나 길 없음", "나 반드시 아님", "돌아오지 않으면 갇힘",
    "거짓 성공", "두 번 실행", "남는 빚", "약속 다", "잠금 불일치", "실패했는데 안 잠긴 대기",
    "빈칸", "넘침",
)


@TIMEOUT
@pytest.mark.parametrize("name, options", BRANCHES)
def test_product_promises_hold(name, options):
    metrics = run(name, show_paths=True, **options)
    assert {key: metrics[key] for key in ZERO_METRICS} == {key: 0 for key in ZERO_METRICS}


CONTROLS = [
    pytest.param("다 hello 제거", {"restart": "legacy", "without": ("hello",)},
                 ("나 길 없음", "나 반드시 아님", "돌아오지 않으면 갇힘"), id="no-hello", marks=FULL),
    pytest.param("다 유예 제거", {"restart": "legacy", "without": ("grace",)},
                 ("돌아오지 않으면 갇힘",), id="no-grace", marks=FULL),
    pytest.param("다 거절 가드 제거", {"restart": "legacy", "without": ("refuse",)},
                 ("거짓 성공", "두 번 실행"), id="no-refusal", marks=FULL),
    pytest.param("가 늦은 성공 조건 변경", {"without": ("startrule",)},
                 ("가 길 없음", "가 반드시 아님"), id="last-opened-only", marks=FULL),
    pytest.param("마 P8-1 제거", {"auto": True, "without": ("p81",)},
                 ("약속 다", "실패했는데 안 잠긴 대기"), id="no-p81"),
    pytest.param("마 P8-3 제거", {"auto": True, "without": ("p83",)},
                 ("약속 다",), id="no-p83"),
    pytest.param("마 P8-1과 P8-3 제거", {"auto": True, "without": ("p81", "p83")},
                 ("약속 다",), id="no-p81-p83"),
    pytest.param("차 P8-1과 P8-3과 저널 제거",
                 {"auto": True, "restart": "manifest", "without": ("p81", "p83", "journal")},
                 ("약속 다", "갇힘 중 다시 대기에 남음"), id="no-p81-p83-journal", marks=FULL),
    pytest.param("라 P8-1 제거", {"restart": "manifest", "asym": True, "without": ("p81",)},
                 ("갇힘 중 다시 대기에 남음",), id="ra-no-p81", marks=FULL),
]


@TIMEOUT
@pytest.mark.parametrize("name, options, violated", CONTROLS)
def test_removed_safeguards_are_observed(name, options, violated):
    metrics = run(name, show_paths=False, **options)
    assert all(metrics[key] > 0 for key in violated), metrics


@FULL
@TIMEOUT
def test_removing_the_journal_rule_alone_keeps_promise_three():
    metrics = run("차 저널만 제거", auto=True, restart="manifest", without=("journal",))
    assert metrics["약속 다"] == 0


def test_journal_gate_uses_the_production_lineage_rule():
    from .plan_rule import failed_journal_applies, plan_requires_inspection

    assert failed_journal_applies(1, (1,))
    assert not failed_journal_applies(2, (1,))
    assert plan_requires_inspection(True, True, 1)
    assert not plan_requires_inspection(False, True, 1)
    assert not plan_requires_inspection(True, False, 1)


def test_late_success_uses_the_product_protocol_rule():
    from .later_rule import no_later_attempt

    assert no_later_attempt(1, 1, 2, (1,))
    assert not no_later_attempt(1, 1, 2, (1, 2))
    assert not no_later_attempt(None, 1, 2, (1,))


def test_p81_counterfactual_only_removes_the_failure_transitions():
    from haniel_orch.statechart.definitions import OrchRequest
    from haniel_orch.statechart.restore import advance
    from .mutations import without_p81

    row = {"status": "again", "belief": "b_none", "lock": "l_off", "tag": "t_off"}
    product = advance(OrchRequest, row, "late_failed")
    altered = advance(without_p81(), row, "late_failed")
    assert (product["status"], product["lock"]) == ("retry", "l_on")
    assert altered == row
    assert advance(OrchRequest, row, "node_failed", {"no_active_attempt": True})["lock"] == "l_on"
    assert advance(without_p81(), row, "node_failed", {"no_active_attempt": True}) == row
    assert advance(without_p81(), row, "open_probe") == advance(OrchRequest, row, "open_probe")


def test_late_success_preserves_the_superseded_attempt_evidence():
    system = System()
    state = system.initial()._replace(
        st="deploying", bl="b_unknown", cur=2, opened=2,
        ev=("ev_result", "ev_none"), sm=(1,),
    )
    after = system.orch_recv(state, "set", 1)[0]
    assert after.st == "success" and after.cur == 0
    assert after.ev == ("ev_both", "ev_none")


def test_product_node_own_start_and_slot_revalidation():
    from .node_model import NodeRun, step

    assert NodeRun.__module__ == "haniel.core.node_run"
    initial = {"exec_": "arrived", "report": "r_none"}
    row, todo = step(NodeRun, initial, "own_start")
    assert row["exec_"] == "running" and todo == ("started",)
    system = System()
    node = system.node(system.initial(), 0, "own_start")
    assert node.inst == ((0, "running", "r_none"),) and node.qno == ()
    queued = {"exec_": "queued", "report": "r_none"}
    assert step(NodeRun, queued, "slot_free")[0]["exec_"] == "running"
    assert step(NodeRun, queued, "slot_free", holds_other=True)[0]["exec_"] == "declined"


def test_failure_debt_is_acknowledged_after_own_success():
    system = System()
    state = system.initial()._replace(
        st="deploying", bl="b_queued", cur=1, opened=1, tgt=True,
        inst=((0, "ok", "r_owed"), (1, "failed", "r_owed")),
    )
    success = system.orch_recv(state, "own_ok", 0)[0]
    assert success.st == "success" and success.ev[0] == "ev_none"
    failed = system.orch_recv(success, "fail", 1)[0]
    assert failed.ev[0] == "ev_failed" and ("tack", 1) in failed.qon
