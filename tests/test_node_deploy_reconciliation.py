"""Startup recovery of a journal-to-outbox interruption."""

from haniel.config import HanielConfig
from haniel.core.deployment_state import DeploymentStateStore
from haniel.core.node_deploy_reporting import enqueue_startup_retro_reports
from haniel.integrations.orchestrator_client import OrchestratorClient


def test_journal_reconciliation_respects_outbox_and_ack(tmp_path):
    config = HanielConfig(
        repos={
            "r": {
                "url": "git@example/r",
                "path": "r",
                "branch": "main",
                "release_manifest": "release.json",
            }
        },
        services={},
        orchestrator_client={"url": "ws://orch", "token": "t", "node_id": "n"},
    )
    store = DeploymentStateStore(tmp_path / "journal")
    store.begin(
        "r",
        "previous",
        "target",
        "release",
        orchestrator_attempt_id="a",
        node_id="n",
        branch="main",
    )
    store.transition("r", "success")
    client = OrchestratorClient(config.orchestrator_client, "test", config_dir=tmp_path)
    client._registry.outbox.remember("r", "o:a", "generation")

    def recover():
        enqueue_startup_retro_reports(
            config=config,
            config_dir=tmp_path,
            orchestrator_client=client,
            journal_store=store,
            get_local_head=lambda _: "target",
        )

    recover()
    box = client._registry.outbox
    assert len(box.entries["o:a"]["letters"]) == 2
    created = box.entries["o:a"]["created_at"]
    recover()
    assert box.entries["o:a"]["created_at"] == created
    box.ack("o:a")
    recover()
    assert not box.entries


def test_handover_failure_keeps_orchestrator_attempt_before_staging(tmp_path):
    config = HanielConfig(
        repos={
            "r": {
                "url": "git@example/r",
                "path": "r",
                "branch": "main",
                "release_manifest": "release.json",
            }
        },
        services={},
        orchestrator_client={"url": "ws://orch", "token": "t", "node_id": "n"},
    )
    store = DeploymentStateStore(tmp_path / "journal")
    store.begin_handover(
        "r",
        previous_head="old",
        target_ref="target",
        manifest_identity="release.json",
        request_id="a",
        expected_operation="upgrade",
        branch="main",
        node_id="n",
        orchestrator_attempt_id="a",
    )
    store.fail_handover_if_current("r", "a", "PREFLIGHT_FAILED", "staging failed")
    client = OrchestratorClient(config.orchestrator_client, "test", config_dir=tmp_path)
    client._registry.outbox.remember("r", "o:a", "g")
    enqueue_startup_retro_reports(
        config=config,
        config_dir=tmp_path,
        orchestrator_client=client,
        journal_store=store,
        get_local_head=lambda _: "old",
    )
    assert (
        client._registry.outbox.entries["o:a"]["letters"][0]["connection_generation"]
        == "g"
    )
