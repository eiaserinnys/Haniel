"""Durable node terminal bundles."""

import json

from haniel.core.orch_outbox import OrchOutbox


def test_bundle_survives_restart_until_matching_ack(tmp_path):
    box = OrchOutbox(tmp_path)
    letters = [
        {"type": "deploy_result", "deploy_id": "d", "orchestrator_attempt_id": "a"}
    ]
    assert box.put(letters)
    assert json.loads(box.path.read_text())["entries"]["o:a"]["letters"] == letters
    box = OrchOutbox(tmp_path)
    assert box.letters() == [letters]
    assert not box.ack("o:other")
    assert box.ack("o:a")
    assert OrchOutbox(tmp_path).letters() == []


def test_write_failure_keeps_memory(tmp_path, monkeypatch):
    box = OrchOutbox(tmp_path)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("haniel.core.orch_outbox.os.replace", fail)
    assert not box.put(
        [{"type": "deploy_result", "deploy_id": "d", "orchestrator_attempt_id": "a"}]
    )
    assert len(box.letters()) == 1


def test_latest_ack_per_repository_survives_restart(tmp_path):
    box = OrchOutbox(tmp_path)
    first = [
        {
            "type": "deploy_result",
            "deploy_id": "node:repo:main:sha",
            "orchestrator_attempt_id": "a",
        }
    ]
    second = [
        {
            "type": "deploy_result",
            "deploy_id": "node:repo:main:sha2",
            "orchestrator_attempt_id": "b",
        }
    ]
    box.put(first)
    box.ack("o:a")
    box.put(second)
    box.ack("o:b")
    restored = OrchOutbox(tmp_path)
    assert restored.was_acked("repo", "o:b")
    assert not restored.was_acked("repo", "o:a")


def test_inflight_correlation_survives_process_restart(tmp_path):
    box = OrchOutbox(tmp_path)
    assert box.remember("repo", "o:a", "g1")
    assert box.remember("repo", "o:b", "g2")
    assert OrchOutbox(tmp_path).correlation("repo", "o:a") == "g1"
    assert OrchOutbox(tmp_path).correlation("repo", "o:b") == "g2"
