"""Resident lifecycle spool worker with per-request failure isolation."""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any

from .handover_result import (
    build_handover_result,
    handover_error_code,
    request_error_code,
)
from .lifecycle_control import LifecycleConflict, LifecycleControl
from .safety_redaction import bounded_redact_text

if TYPE_CHECKING:
    from .runner import ServiceRunner

logger = logging.getLogger(__name__)


class LifecycleRequestServer:
    """Resident spool consumer shared by service and foreground run modes."""

    def __init__(
        self,
        *,
        control: LifecycleControl,
        runner: "ServiceRunner",
        instance_id: str,
        poll_interval: float = 0.1,
    ) -> None:
        self.control = control
        self.runner = runner
        self.instance_id = instance_id
        self.poll_interval = poll_interval
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._serve, name="haniel-lifecycle-control", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _serve(self) -> None:
        while not self._stopping.is_set():
            self.process_pending_once()
            self._stopping.wait(self.poll_interval)

    def process_pending_once(self) -> None:
        """Process every visible request while isolating each file and result write."""
        for path in sorted(self.control.requests_dir.glob("*.json")):
            request_id = path.stem
            try:
                self.handle_request(request_id)
            except Exception as error:
                code = request_error_code(error)
                try:
                    self._terminal_failure(request_id, code)
                except Exception:
                    logger.exception(
                        "Failed to persist isolated lifecycle terminal result for %s",
                        request_id,
                    )

    def handle_request(self, request_id: str) -> None:
        """Process one queued request synchronously through the resident owner."""
        if self.control.read_result(request_id).get("terminal"):
            return
        path = self.control.request_path(request_id)
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("MALFORMED_REQUEST") from error
        if not isinstance(request, dict):
            raise RuntimeError("MALFORMED_REQUEST")
        if request.get("request_id") != request_id:
            raise LifecycleConflict(
                "REQUEST_IDENTITY_CONFLICT: path and envelope request_id differ"
            )
        if request.get("config_identity") != self.control.identity:
            raise LifecycleConflict(
                "REQUEST_IDENTITY_CONFLICT: config identity does not match owner"
            )
        payload = request.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("MALFORMED_REQUEST")
        kind = payload.get("kind")
        if kind == "stop":
            self._validate_stop(payload)
            self._handle_stop(request_id, payload)
        elif kind == "handover":
            self._validate_handover(payload)
            self._handle_handover(request_id, payload)
        elif kind == "runtime-handover":
            self._validate_handover(payload, require_executor=True)
            self._handle_runtime_handover(request_id, payload)
        else:
            self._terminal_failure(request_id, "UNSUPPORTED_REQUEST_KIND")

    @staticmethod
    def _validate_stop(payload: dict[str, Any]) -> None:
        if not isinstance(payload.get("expected_instance"), str) or not payload.get(
            "expected_instance"
        ):
            raise RuntimeError("MALFORMED_REQUEST")

    @staticmethod
    def _validate_handover(
        payload: dict[str, Any], *, require_executor: bool = False
    ) -> None:
        if (
            not isinstance(payload.get("repo"), str)
            or not payload.get("repo")
            or not isinstance(payload.get("target_ref"), str)
            or not payload.get("target_ref")
            or payload.get("expected_operation") not in {"fresh_install", "upgrade"}
            or (
                require_executor
                and (
                    not isinstance(payload.get("executor_instance"), str)
                    or not payload.get("executor_instance")
                )
            )
        ):
            raise RuntimeError("MALFORMED_REQUEST")

    def _terminal_failure(self, request_id: str, code: str) -> None:
        self.control.ack(
            request_id,
            "terminal",
            {
                "schema_version": "haniel.lifecycle.error.v1",
                "ok": False,
                "request_id": request_id,
                "error": {
                    "code": code,
                    "message": f"lifecycle request failed with {code}",
                },
            },
        )

    def _handle_stop(self, request_id: str, payload: dict[str, Any]) -> None:
        if payload.get("expected_instance") != self.instance_id:
            terminal = {
                "schema_version": "haniel.lifecycle.stop.v1",
                "ok": False,
                "request_id": request_id,
                "error": {
                    "code": "EXPECTED_INSTANCE_MISMATCH",
                    "message": "resident instance changed before stop",
                },
            }
        else:
            self.control.ack(
                request_id,
                "accepted",
                {"owner_instance": self.instance_id},
            )
            self.runner.stop()
            terminal = {
                "schema_version": "haniel.lifecycle.stop.v1",
                "ok": True,
                "request_id": request_id,
                "owner_instance": self.instance_id,
            }
        self.control.ack(request_id, "terminal", terminal)

    def _handle_runtime_handover(
        self, request_id: str, payload: dict[str, Any]
    ) -> None:
        if payload["executor_instance"] == self.instance_id:
            return
        self._terminal_failure(request_id, "RUNTIME_OWNER_LOST")

    def _handle_handover(self, request_id: str, payload: dict[str, Any]) -> None:
        from .one_shot_handover import execute_owner_handover

        try:
            execute_owner_handover(
                self.runner,
                control=self.control,
                repo_name=payload["repo"],
                target_ref=payload["target_ref"],
                expected_operation=payload["expected_operation"],
                request_id=request_id,
            )
        except Exception as error:
            operation = payload.get("expected_operation", "upgrade")
            terminal = build_handover_result(
                self.control,
                request_id=request_id,
                operation=operation,
                phase="failed",
                previous_head=None,
                target_head=None,
                release_id=None,
                ok=False,
                recovered=False,
                retryable=True,
                error={
                    "code": handover_error_code(error),
                    "message": bounded_redact_text(str(error)),
                },
            )
            self.control.ack(request_id, "terminal", terminal.to_dict())
