"""Atomic, repo-scoped Haniel deployment journal storage."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .deployment_errors import (
    KNOWN_DEPLOYMENT_ERROR_CODES,
    stable_deployment_error_code,
)
from .safety_redaction import bounded_redact_text, redact_value

_JOURNAL_MESSAGE_MAX_CHARS = 16384


class DeploymentStateStore:
    """Repo-scoped deployment journal with atomic replacement writes."""

    TERMINAL_STATES = frozenset({"success", "failed", "interrupted", "aborted"})

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, repo_name: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", repo_name)
        return self.directory / f"{safe_name}.json"

    def path(self, repo_name: str) -> Path:
        """Return the canonical journal path for one repository."""
        return self._path(repo_name)

    def read(self, repo_name: str) -> dict[str, Any] | None:
        path = self._path(repo_name)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"deployment journal root is not an object: {path}")
        return value

    def begin(
        self,
        repo_name: str,
        previous_head: str,
        target_head: str,
        release_id: str,
        *,
        orchestrator_attempt_id: str | None = None,
        node_id: str | None = None,
        branch: str | None = None,
        manifest_identity: str | None = None,
        manifest_digest: str | None = None,
        journal_attempt_id: str | None = None,
        request_id: str | None = None,
        expected_operation: str | None = None,
        config_digest: str | None = None,
    ) -> str:
        previous_attempts: list[dict[str, Any]] = []
        existing = self.read(repo_name)
        if (
            existing is not None
            and existing.get("state") not in self.TERMINAL_STATES
            and orchestrator_attempt_id is None
            and existing.get("orchestrator_attempt_id")
        ):
            orchestrator_attempt_id = existing["orchestrator_attempt_id"]
            node_id = node_id or existing.get("node_id")
            branch = branch or existing.get("branch")
        if journal_attempt_id is not None and existing is not None:
            if existing.get("journal_attempt_id") != journal_attempt_id:
                raise ValueError(
                    "journal_attempt_id does not identify the live journal"
                )
            immutable = {
                "previous_head": previous_head,
                "target_head": target_head,
                "orchestrator_attempt_id": orchestrator_attempt_id,
                "node_id": node_id,
                "branch": branch,
                "manifest_identity": manifest_identity,
                "manifest_digest": manifest_digest,
                "request_id": request_id,
                "expected_operation": expected_operation,
            }
            if existing.get("config_digest") is not None or config_digest is not None:
                immutable["config_digest"] = config_digest
            changed = [
                key for key, value in immutable.items() if existing.get(key) != value
            ]
            if changed:
                raise ValueError(
                    "journal intent changed immutable fields: " + ", ".join(changed)
                )
            existing["release_id"] = release_id
            existing["state"] = "build"
            existing["recovered"] = False
            existing.setdefault("history", []).append(
                self._entry("build", "deployment intent activated")
            )
            self._write(repo_name, existing)
            return journal_attempt_id
        if existing is not None:
            previous_attempts = deepcopy(existing.get("previous_attempts", []))
            archived = {
                key: deepcopy(value)
                for key, value in existing.items()
                if key != "previous_attempts"
            }
            if archived.get("state") not in self.TERMINAL_STATES:
                prior_state = str(archived.get("state", "unknown"))
                archived["state"] = "aborted"
                archived["aborted_from"] = prior_state
                message = f"superseded by a new deployment attempt from {prior_state}"
                archived["abort_reason"] = message
                archived.setdefault("history", []).append(
                    self._entry("aborted", message)
                )
            previous_attempts.append(archived)

        current: dict[str, Any] = {
            "repo": repo_name,
            "journal_attempt_id": journal_attempt_id or str(uuid4()),
            "orchestrator_attempt_id": orchestrator_attempt_id,
            "node_id": node_id,
            "branch": branch,
            "release_id": release_id,
            "previous_head": previous_head,
            "target_head": target_head,
            "state": "build",
            "recovered": False,
            "history": [self._entry("build")],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "manifest_identity": manifest_identity,
            "manifest_digest": manifest_digest,
            "request_id": request_id,
            "expected_operation": expected_operation,
        }
        if config_digest is not None:
            current["config_digest"] = config_digest
        if previous_attempts:
            current["previous_attempts"] = previous_attempts
        self._write(repo_name, current)
        return current["journal_attempt_id"]

    def begin_handover(
        self,
        repo_name: str,
        *,
        previous_head: str,
        target_ref: str,
        manifest_identity: str,
        request_id: str,
        expected_operation: str,
        config_digest: str | None = None,
    ) -> str:
        """Commit rollback identity before target fetch or staging starts."""
        existing = self.read(repo_name)
        if (
            existing is not None
            and existing.get("request_id") == request_id
            and existing.get("state") not in self.TERMINAL_STATES
        ):
            immutable = {
                "previous_head": previous_head,
                "manifest_identity": manifest_identity,
                "expected_operation": expected_operation,
                "target_ref": target_ref,
                "config_digest": config_digest,
            }
            changed = [
                key for key, value in immutable.items() if existing.get(key) != value
            ]
            if changed:
                raise ValueError(
                    "handover request changed immutable fields: " + ", ".join(changed)
                )
            return existing["journal_attempt_id"]

        attempt_id = self.begin(
            repo_name,
            previous_head,
            target_ref,
            "handover-target-pending",
            manifest_identity=manifest_identity,
            request_id=request_id,
            expected_operation=expected_operation,
            config_digest=config_digest,
        )
        current = self.read(repo_name)
        assert current is not None
        current["target_ref"] = target_ref
        current["state"] = "target_resolving"
        current.setdefault("history", []).append(self._entry("target_resolving"))
        self._write(repo_name, current)
        return attempt_id

    def bind_handover_target(
        self,
        repo_name: str,
        *,
        request_id: str,
        target_head: str,
        release_id: str,
        manifest_digest: str,
    ) -> None:
        """Commit detached-probe evidence without permitting identity drift."""
        current = self.read(repo_name)
        if current is None or current.get("request_id") != request_id:
            raise ValueError("handover journal request identity mismatch")
        if current.get("state") != "target_resolving":
            immutable = {
                "target_head": target_head,
                "release_id": release_id,
                "manifest_digest": manifest_digest,
            }
            changed = [
                key for key, value in immutable.items() if current.get(key) != value
            ]
            if changed:
                raise ValueError(
                    "staged target changed committed identity: " + ", ".join(changed)
                )
            return
        current["target_head"] = target_head
        current["release_id"] = release_id
        current["manifest_digest"] = manifest_digest
        current["state"] = "staging_probed"
        current.setdefault("history", []).append(self._entry("staging_probed"))
        self._write(repo_name, current)

    def fail_handover_if_current(
        self,
        repo_name: str,
        request_id: str,
        code: str,
        message: str,
    ) -> bool:
        """Fail only the matching live handover without replacing newer evidence."""

        if code not in KNOWN_DEPLOYMENT_ERROR_CODES:
            raise ValueError(f"unregistered deployment error code: {code}")
        current = self.read(repo_name)
        if (
            current is None
            or current.get("request_id") != request_id
            or current.get("state") in self.TERMINAL_STATES
        ):
            return False
        safe_message = bounded_redact_text(
            message,
            max_chars=_JOURNAL_MESSAGE_MAX_CHARS,
        )
        current["state"] = "failed"
        current["error_code"] = code
        current["error"] = safe_message
        current["completed_at"] = datetime.now(timezone.utc).isoformat()
        current.setdefault("history", []).append(
            self._entry("failed", f"{code}: {safe_message}")
        )
        self._write(repo_name, current)
        return True

    def transition(
        self,
        repo_name: str,
        state: str,
        *,
        message: str | None = None,
        recovered: bool | None = None,
        error: BaseException | None = None,
    ) -> None:
        current = self.read(repo_name)
        if current is None:
            raise ValueError(f"deployment journal does not exist for {repo_name}")
        current["state"] = state
        if state in self.TERMINAL_STATES:
            current["completed_at"] = datetime.now(timezone.utc).isoformat()
        if error is not None and state != "failed":
            raise ValueError("typed transition errors are only valid for failed state")
        if state == "failed" and message:
            error_code = (
                stable_deployment_error_code(error)
                if error is not None
                else "HANDOVER_FAILED"
            )
            if error_code not in KNOWN_DEPLOYMENT_ERROR_CODES:
                raise ValueError(f"unregistered deployment error code: {error_code}")
            current["error_code"] = error_code
        if recovered is not None:
            current["recovered"] = recovered
        current.setdefault("history", []).append(self._entry(state, message))
        self._write(repo_name, current)

    def link_release_contract(
        self,
        repo_name: str,
        *,
        request_id: str | None,
        operation: str,
        database_journal_path: str,
        quiescence_receipt: dict[str, Any] | None = None,
    ) -> None:
        """Link DB-owned evidence without duplicating the DB journal."""
        current = self.read(repo_name)
        if current is None:
            raise ValueError(f"deployment journal does not exist for {repo_name}")
        current["request_id"] = request_id
        current["operation"] = operation
        current["database_journal_path"] = database_journal_path
        if quiescence_receipt is not None:
            current["quiescence_receipt"] = deepcopy(quiescence_receipt)
        self._write(repo_name, current)

    def record_database_result(
        self,
        repo_name: str,
        *,
        phase: str,
        result: dict[str, Any],
    ) -> None:
        """Link the latest bounded DB executor result without owning DB facts."""
        current = self.read(repo_name)
        if current is None:
            raise ValueError(f"deployment journal does not exist for {repo_name}")
        current["database_last_result_phase"] = phase
        current["database_last_result"] = redact_value(deepcopy(result))
        self._write(repo_name, current)

    def write_quiescence_receipt(self, repo_name: str, receipt: dict[str, Any]) -> Path:
        """Persist the resident acknowledgement beside the deployment journal."""
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", repo_name)
        path = self.directory / f"{safe_name}.quiescence.json"
        _atomic_json(path, redact_value(receipt))
        return path

    def is_success(self, repo_name: str, target_head: str, release_id: str) -> bool:
        current = self.read(repo_name)
        return bool(
            current
            and current.get("state") == "success"
            and current.get("target_head") == target_head
            and current.get("release_id") == release_id
        )

    def mark_interrupted(self, repo_name: str, *, reason: str) -> bool:
        """Close a stale nonterminal attempt while preserving its evidence."""
        current = self.read(repo_name)
        if current is None or current.get("state") in self.TERMINAL_STATES:
            return False

        prior_state = str(current.get("state", "unknown"))
        current["state"] = "interrupted"
        current["interrupted_from"] = prior_state
        current["interruption_reason"] = reason
        current.setdefault("history", []).append(self._entry("interrupted", reason))
        self._write(repo_name, current)
        return True

    @staticmethod
    def _entry(state: str, message: str | None = None) -> dict[str, str]:
        entry = {
            "state": state,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        if message:
            entry["message"] = bounded_redact_text(
                message, max_chars=_JOURNAL_MESSAGE_MAX_CHARS
            )
        return entry

    def _write(self, repo_name: str, value: dict[str, Any]) -> None:
        _atomic_json(self._path(repo_name), redact_value(value))


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
