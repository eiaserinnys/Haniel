"""Node-side permission gate for auto deploy attempts."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


class DeployPermissionError(RuntimeError):
    pass


@dataclass
class _PendingPermission:
    deploy_id: str
    event: threading.Event
    response: dict[str, Any] | None = None


class DeployAttemptGate:
    """Correlate one auto request with exactly one generation-bound ACK."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingPermission] = {}
        self._generation: str | None = None

    def observe_generation(self, generation: str) -> None:
        with self._lock:
            if self._generation is not None and self._generation != generation:
                for pending in self._pending.values():
                    pending.response = {"accepted": False, "error": "connection_generation_changed"}
                    pending.event.set()
            self._generation = generation

    def reset_connection(self) -> None:
        """Close old waiters and forget the server generation on reconnect."""
        with self._lock:
            for pending in self._pending.values():
                pending.response = {
                    "accepted": False,
                    "error": "connection_generation_changed",
                }
                pending.event.set()
            self._generation = None

    def register(self, requested_orchestrator_attempt_id: str, deploy_id: str) -> None:
        with self._lock:
            if requested_orchestrator_attempt_id in self._pending:
                raise DeployPermissionError("duplicate orchestrator attempt request")
            self._pending[requested_orchestrator_attempt_id] = _PendingPermission(
                deploy_id=deploy_id, event=threading.Event()
            )

    def accept_ack(self, ack: dict[str, Any]) -> bool:
        requested = ack.get("requested_orchestrator_attempt_id")
        if not isinstance(requested, str):
            raise DeployPermissionError("ACK lacks requested_orchestrator_attempt_id")
        self._validate_ack_shape(ack)
        with self._lock:
            pending = self._pending.get(requested)
            if pending is None:
                return False
            if ack["deploy_id"] != pending.deploy_id:
                return False
            if self._generation is not None and ack["connection_generation"] != self._generation:
                return False
            if pending.response is not None:
                return True
            pending.response = dict(ack)
            pending.event.set()
            return True

    def wait(self, requested_orchestrator_attempt_id: str, timeout: float) -> dict[str, Any]:
        with self._lock:
            pending = self._pending.get(requested_orchestrator_attempt_id)
        if pending is None:
            raise DeployPermissionError("unknown orchestrator attempt request")
        if not pending.event.wait(timeout):
            with self._lock:
                self._pending.pop(requested_orchestrator_attempt_id, None)
            raise DeployPermissionError("deploy attempt ACK timeout")
        with self._lock:
            completed = self._pending.pop(requested_orchestrator_attempt_id, pending)
        response = completed.response or {"accepted": False, "error": "ACK lost"}
        if not response.get("accepted"):
            raise DeployPermissionError(str(response.get("error", "deploy attempt rejected")))
        return response

    @staticmethod
    def _validate_ack_shape(ack: dict[str, Any]) -> None:
        common = {
            "type",
            "accepted",
            "requested_orchestrator_attempt_id",
            "deploy_id",
            "connection_generation",
        }
        accepted_only = {
            "begun_orchestrator_attempt_id",
            "probe_id",
            "execution_mode",
            "preflight_fingerprint",
        }
        rejected_only = {"error"}
        keys = set(ack)
        if ack.get("type") != "deploy_attempt_ack" or not common <= keys:
            raise DeployPermissionError("malformed deploy attempt ACK")
        if ack.get("accepted") is True:
            if keys != common | accepted_only:
                raise DeployPermissionError("accepted ACK fields are contaminated")
            if ack["begun_orchestrator_attempt_id"] != ack["requested_orchestrator_attempt_id"]:
                raise DeployPermissionError("accepted ACK changed orchestrator attempt ID")
        elif ack.get("accepted") is False:
            if (
                keys != common | rejected_only
                or ack.get("error") != "retry_requires_manual_approval"
            ):
                raise DeployPermissionError("rejected ACK fields are contaminated")
        else:
            raise DeployPermissionError("ACK accepted tag must be boolean")
