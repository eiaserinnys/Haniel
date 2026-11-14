"""Admission and duplicate-permit contracts."""

import pytest

from haniel.core.node_run_registry import NodeRunRegistry


def test_held_attempt_blocks_new_permit_and_repeats_current_state(tmp_path):
    runs = NodeRunRegistry(tmp_path)
    assert runs.permit("d", "a", auto=False, waiting=True) == ("acked", None)
    assert runs.permit("d", "a", auto=False, waiting=True) == ("acked", None)
    assert runs.permit("d", "b", auto=False, waiting=True) == (
        "declined",
        "already_held",
    )
    assert runs.start("a") == ["started"]
    assert runs.permit("d", "a", auto=False, waiting=True) == ("started", None)


def test_owed_success_and_failure_guards(tmp_path):
    runs = NodeRunRegistry(tmp_path)
    runs.permit("d", "a", auto=False, waiting=True)
    runs.start("a")
    runs.finish(
        "a",
        [
            {
                "type": "deploy_result",
                "deploy_id": "d",
                "orchestrator_attempt_id": "a",
                "status": "failed",
            }
        ],
        success=False,
    )
    assert runs.permit("d", "b", auto=True, waiting=True) == (
        "declined",
        "failure_owed",
    )
    assert runs.permit("d", "c", auto=False, waiting=True) == ("acked", None)


def test_success_debt_and_unknown_auto_are_refused(tmp_path):
    runs = NodeRunRegistry(tmp_path)
    runs.permit("d", "a", auto=False, waiting=True)
    runs.start("a")
    runs.finish(
        "a",
        [
            {
                "type": "deploy_result",
                "deploy_id": "d",
                "orchestrator_attempt_id": "a",
                "status": "success",
            }
        ],
        success=True,
    )
    assert runs.permit("d", "b", auto=False, waiting=True) == (
        "declined",
        "success_owed",
    )
    assert runs.permit("other", "c", auto=True, waiting=False) == (
        "declined",
        "no_waiter",
    )


@pytest.mark.parametrize("ending", ["timeout", "disconnect"])
def test_late_auto_permit_after_wait_ends_is_no_waiter(tmp_path, ending):
    runs = NodeRunRegistry(tmp_path)
    runs.ask("d", "a")
    if ending == "timeout":
        runs.give_up("a")
    else:
        runs.connection(False)
    assert runs.permit("d", "a", auto=True, waiting=False) == ("declined", "no_waiter")


def test_held_list_transfers_to_debt_under_one_lock(tmp_path):
    runs = NodeRunRegistry(tmp_path)
    runs.permit("d", "a", auto=False, waiting=True)
    assert runs.held()[0]["state"] == "queued"
    runs.start("a")
    assert runs.held()[0]["state"] == "running"
    runs.finish(
        "a",
        [
            {
                "type": "deploy_result",
                "deploy_id": "d",
                "orchestrator_attempt_id": "a",
                "status": "success",
            }
        ],
        success=True,
    )
    assert runs.held()[0]["state"] == "owed"
    assert NodeRunRegistry(tmp_path).held()[0]["state"] == "owed"


def test_hello_cannot_observe_gap_during_finish(tmp_path, monkeypatch):
    import threading

    runs = NodeRunRegistry(tmp_path)
    runs.permit("d", "a", auto=False, waiting=True)
    runs.start("a")
    writing, release, reading, done = (threading.Event() for _ in range(4))
    original = runs.outbox._save

    def paused_save():
        writing.set()
        assert release.wait(3)
        return original()

    monkeypatch.setattr(runs.outbox, "_save", paused_save)
    result = []

    def finish():
        runs.finish(
            "a",
            [
                {
                    "type": "deploy_result",
                    "deploy_id": "d",
                    "orchestrator_attempt_id": "a",
                    "status": "success",
                }
            ],
            success=True,
        )

    def read():
        reading.set()
        result.extend(runs.held())
        done.set()

    writer = threading.Thread(target=finish)
    reader = threading.Thread(target=read)
    writer.start()
    assert writing.wait(3)
    reader.start()
    assert reading.wait(3)
    assert not done.is_set()
    release.set()
    writer.join(3)
    reader.join(3)
    assert not writer.is_alive() and not reader.is_alive()
    assert result[0]["state"] == "owed"
