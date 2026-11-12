"""Tests for the node-side deploy attempt ACK gate."""

import pytest

from haniel.integrations.deploy_attempt_gate import (
    DeployAttemptGate,
    DeployPermissionError,
)


def _rejected_ack(error: str) -> dict:
    return {
        "type": "deploy_attempt_ack",
        "accepted": False,
        "requested_orchestrator_attempt_id": "a1",
        "deploy_id": "d1",
        "connection_generation": "g1",
        "error": error,
    }


class TestRejectedAckErrors:
    def test_self_update_pending_rejection_is_a_rejection(self):
        """The hub's self-update block reason is a rejection, not contamination."""
        gate = DeployAttemptGate()
        gate.observe_generation("g1")
        gate.register("a1", "d1")

        assert gate.accept_ack(_rejected_ack("self_update_pending"))

        with pytest.raises(DeployPermissionError, match="self update is pending"):
            gate.wait("a1", 0.01)

    def test_unknown_rejection_error_stays_contaminated(self):
        """Unknown reasons keep the strict ACK shape contract."""
        gate = DeployAttemptGate()
        gate.register("a1", "d1")

        with pytest.raises(DeployPermissionError, match="contaminated"):
            gate.accept_ack(_rejected_ack("something_else"))
