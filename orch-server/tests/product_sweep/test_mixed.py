import pytest

from .mixed_metrics import assert_no_new_classes, check_new_pair, compare_false_success


BRANCHES = [
    pytest.param("가", {}, id="ga"),
    pytest.param("나", {"asym": True}, id="na"),
    pytest.param("다", {"restart": "legacy"}, id="da"),
    pytest.param("라", {"restart": "manifest", "asym": True}, id="ra"),
    pytest.param("마", {"auto": True}, id="ma"),
    pytest.param("바", {"auto": True, "restart": "legacy"}, id="ba"),
    pytest.param("사", {"newer": True, "restart": "legacy"}, id="sa"),
    pytest.param("아", {"orestart": True, "asym": True}, id="ah"),
    pytest.param("자", {"auto": True, "orestart": True}, id="ja"),
    pytest.param("차", {"auto": True, "manifest": True}, id="cha"),
    pytest.param("카", {"auto": True, "max_attempts": 3, "max_probes": 3, "max_asks": 2}, id="ka"),
]


def test_comparison_detects_a_new_false_success_class():
    old = {("old",): ("old state", [])}
    new = {**old, ("extra",): ("new state", ["step"])}
    with pytest.raises(AssertionError):
        assert_no_new_classes("control", old, new)


@pytest.mark.sweep_full
@pytest.mark.timeout(1200)
@pytest.mark.parametrize("name, options", BRANCHES)
def test_new_orchestrator_adds_no_false_success_class_for_old_nodes(name, options):
    compare_false_success(name, options)


@pytest.mark.sweep_full
@pytest.mark.timeout(1200)
@pytest.mark.parametrize("name, options", BRANCHES)
def test_new_pair_keeps_promises(name, options):
    check_new_pair(name, options)


def test_own_success_preserves_the_superseded_attempt_evidence():
    from .mixed_system import MixedSystem

    system = MixedSystem(orch_ver="new", node_ver="old")
    state = system.initial()._replace(st="deploying", bl="b_unknown", cur=1, opened=1)
    after = system.orch_recv(state, "own_ok", 0)[0]
    assert after.st == "success" and after.cur == 0
    assert after.ev[0] == "ev_none"
    recorded = system.orch_recv(after, "res", 1)[0]
    assert recorded.st == "success" and recorded.ev[0] == "ev_result"
    settled = system.orch_recv(recorded, "set", 1)[0]
    assert settled.st == "success" and settled.ev[0] == "ev_both"
    assert not system.holes


def test_own_start_does_not_send_orchestrator_progress():
    from .mixed_system import MixedSystem

    system = MixedSystem(orch_ver="new", node_ver="new")
    node = system.node(system.initial(), 0, "own_start")
    assert node.inst == ((0, "running", "r_none", False),) and node.qno == ()


def test_closed_progress_is_recorded_only_by_the_new_orchestrator():
    from .mixed_system import MixedSystem

    new = MixedSystem(orch_ver="new", node_ver="new")
    state = new.initial()._replace(st="again", opened=2, ev=("ev_result", "ev_none"))
    started = new.orch_recv(state, "started", 2)[0]
    assert started.sm == (2,) and started.st == "again"
    assert new.orch_recv(started, "set", 1)[0].st == "again"

    old = MixedSystem(orch_ver="old", node_ver="old")
    old_state = old.initial()._replace(st="retry", opened=2)
    assert old.orch_recv(old_state, "started", 2)[0] == old_state


def test_new_node_can_request_an_automatic_deploy_while_failure_is_owed():
    from .mixed_system import MixedSystem

    system = MixedSystem(orch_ver="new", node_ver="new", auto=True)
    state = system.initial()._replace(opened=1, inst=((1, "failed", "r_owed", False),))
    assert any(label == "노드가 자동 배포를 청한다" for label, _, _ in system.successors(state))
