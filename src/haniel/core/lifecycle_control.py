"""Cross-process resident ownership and request/result lifecycle control."""

from __future__ import annotations

import hashlib
import os
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .lifecycle_storage import (
    atomic_json,
    current_process_start_identity,
    process_start_identity,
    read_json,
)
from .lifecycle_locks import (
    FileLease as _FileLease,
    LifecycleConflict,
    SerialFileLock as _SerialFileLock,
)
from .safety_redaction import redact_value


def config_identity(config_path: Path) -> str:
    """Hash the normalized canonical config path without requiring it to exist."""
    canonical = os.path.normcase(str(config_path.expanduser().resolve(strict=False)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RequestSubmission:
    request_id: str
    attached: bool
    path: Path


class ResidentOwner(AbstractContextManager["ResidentOwner"]):
    def __init__(self, control: "LifecycleControl", instance_id: str) -> None:
        self.control = control
        self.instance_id = instance_id
        with control._owner_metadata_transaction():
            self._lease = _FileLease(
                control.owner_lock_path,
                instance_id,
                "LIFECYCLE_OWNER_CONFLICT",
            )
            try:
                self._identity = control._prepare_owner_metadata(instance_id)
            except Exception:
                self._lease.release()
                raise

    def metadata(self) -> dict[str, Any]:
        return self.control.read_owner()

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            with self.control._owner_metadata_transaction():
                metadata = self.control.read_owner(optional=True)
                identity_keys = (
                    "config_identity",
                    "instance_id",
                    "pid",
                    "process_start_identity",
                )
                if metadata and all(
                    metadata.get(key) == self._identity.get(key)
                    for key in identity_keys
                ):
                    self.control.owner_path.unlink(missing_ok=True)
        finally:
            self._lease.release()


class DeploymentLease(AbstractContextManager["DeploymentLease"]):
    def __init__(
        self,
        control: "LifecycleControl",
        repo_name: str,
        request_id: str,
        *,
        attached: bool,
        lease: _FileLease | None,
    ) -> None:
        self.control = control
        self.repo_name = repo_name
        self.request_id = request_id
        self.attached = attached
        self._lease = lease

    def __enter__(self) -> "DeploymentLease":
        return self

    def acknowledge_quiesced(self, stopped_services: list[str]) -> None:
        self.control.ack(
            self.request_id,
            "quiesced",
            {"repo": self.repo_name, "stopped_services": stopped_services},
        )

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._lease is None:
            return
        self.control._release_deployment(self.repo_name, self.request_id)
        self._lease.release()


class LifecycleControl:
    _deployment_guard = threading.Lock()
    _deployments: dict[tuple[str, str], str] = {}

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser().resolve(strict=False)
        self.identity = config_identity(self.config_path)
        self.config_dir = self.config_path.parent
        self.root = self.config_dir / ".haniel" / "runtime" / self.identity
        self.requests_dir = self.root / "requests"
        self.results_dir = self.root / "results"
        self.leases_dir = self.root / "leases"
        self.transactions_dir = self.root / "transactions"
        self.owner_path = self.root / "owner.json"
        self.owner_lock_path = self.root / "owner.lock"
        self.owner_metadata_lock_path = self.root / "owner.metadata.lock"

    def acquire_owner(self, instance_id: str) -> ResidentOwner:
        return ResidentOwner(self, instance_id)

    def acquire_deployment(self, repo_name: str, request_id: str) -> DeploymentLease:
        key = (self.identity, repo_name)
        with self._deployment_guard:
            current = self._deployments.get(key)
            if current == request_id:
                return DeploymentLease(
                    self, repo_name, request_id, attached=True, lease=None
                )
            if current is not None:
                raise LifecycleConflict(
                    "DEPLOYMENT_LEASE_CONFLICT",
                    f"{repo_name} is owned by {current}",
                )
            lease_path = self.leases_dir / f"{_safe(repo_name)}.lock"
            try:
                lease = _FileLease(
                    lease_path,
                    request_id,
                    "DEPLOYMENT_LEASE_CONFLICT",
                )
            except LifecycleConflict as error:
                raise LifecycleConflict(
                    "DEPLOYMENT_LEASE_CONFLICT", f"{repo_name} is active"
                ) from error
            self._deployments[key] = request_id
        return DeploymentLease(self, repo_name, request_id, attached=False, lease=lease)

    def _release_deployment(self, repo_name: str, request_id: str) -> None:
        key = (self.identity, repo_name)
        with self._deployment_guard:
            if self._deployments.get(key) == request_id:
                self._deployments.pop(key, None)

    def submit_request(
        self, request_id: str, payload: dict[str, Any]
    ) -> RequestSubmission:
        request = {
            "schema_version": "haniel.lifecycle.request.v1",
            "request_id": request_id,
            "config_identity": self.identity,
            "payload": payload,
        }
        path = self.request_path(request_id)
        with self._request_transaction(request_id):
            if path.exists():
                existing = read_json(path)
                if existing != request:
                    raise LifecycleConflict(
                        "REQUEST_IDENTITY_CONFLICT",
                        "request_id has different payload",
                    )
                return RequestSubmission(request_id, True, path)
            atomic_json(path, request)
            return RequestSubmission(request_id, False, path)

    def request_stop(
        self, *, expected_instance: str, request_id: str
    ) -> RequestSubmission:
        owner = self.read_active_owner()
        if owner.get("instance_id") != expected_instance:
            raise LifecycleConflict(
                "EXPECTED_INSTANCE_MISMATCH",
                "refusing to stop a different resident owner",
            )
        return self.submit_request(
            request_id,
            {
                "kind": "stop",
                "expected_instance": expected_instance,
            },
        )

    def ack(
        self,
        request_id: str,
        state: Literal["accepted", "quiesced", "terminal"],
        detail: dict[str, Any],
    ) -> None:
        safe_detail = redact_value(detail)
        path = self.result_path(request_id)
        with self._request_transaction(request_id):
            result = (
                read_json(path)
                if path.exists()
                else {
                    "schema_version": "haniel.lifecycle.result.v1",
                    "request_id": request_id,
                    "config_identity": self.identity,
                    "acks": [],
                }
            )
            acks = result.setdefault("acks", [])
            order = {"accepted": 0, "quiesced": 1, "terminal": 2}
            duplicate = next(
                (entry for entry in acks if entry.get("state") == state), None
            )
            if duplicate is not None:
                previous_detail = {
                    key: value
                    for key, value in duplicate.items()
                    if key not in {"state", "at"}
                }
                if previous_detail == safe_detail:
                    return
                raise ValueError(f"{state} lifecycle acknowledgement identity changed")
            if acks and order[state] < order[acks[-1]["state"]]:
                raise ValueError("lifecycle acknowledgement cannot move backwards")
            if result.get("terminal") is not None:
                raise ValueError("terminal lifecycle acknowledgement is immutable")
            entry = {
                "state": state,
                "at": datetime.now(timezone.utc).isoformat(),
                **safe_detail,
            }
            acks.append(entry)
            if state == "terminal":
                result["terminal"] = safe_detail
            atomic_json(path, result)

    def cancel_request(
        self, request_id: str, *, code: str, message: str
    ) -> dict[str, Any]:
        terminal = {
            "schema_version": "haniel.lifecycle.cancelled.v1",
            "ok": False,
            "request_id": request_id,
            "error": {"code": code, "message": message},
        }
        path = self.result_path(request_id)
        with self._request_transaction(request_id):
            result = (
                read_json(path)
                if path.exists()
                else {
                    "schema_version": "haniel.lifecycle.result.v1",
                    "request_id": request_id,
                    "config_identity": self.identity,
                    "acks": [],
                }
            )
            existing = result.get("terminal")
            if isinstance(existing, dict):
                return existing
            if any(entry.get("state") == "accepted" for entry in result["acks"]):
                raise LifecycleConflict(
                    "REQUEST_IN_PROGRESS",
                    "resident owner already accepted request",
                )
            result["acks"].append(
                {
                    "state": "terminal",
                    "at": datetime.now(timezone.utc).isoformat(),
                    **terminal,
                }
            )
            result["terminal"] = terminal
            atomic_json(path, result)
            return terminal

    def read_owner(self, *, optional: bool = False) -> dict[str, Any]:
        if optional and not self.owner_path.exists():
            return {}
        if not self.owner_path.exists():
            raise LifecycleConflict("LIFECYCLE_OWNER_MISSING", "no resident owner")
        return read_json(self.owner_path)

    def read_active_owner(self) -> dict[str, Any]:
        try:
            probe = _FileLease(
                self.owner_lock_path,
                "owner-probe",
                "LIFECYCLE_OWNER_ACTIVE",
            )
        except LifecycleConflict:
            with self._owner_metadata_transaction():
                try:
                    recheck = _FileLease(
                        self.owner_lock_path,
                        "owner-recheck",
                        "LIFECYCLE_OWNER_ACTIVE",
                    )
                except LifecycleConflict:
                    metadata = self.read_owner()
                    if metadata.get("config_identity") != self.identity:
                        raise LifecycleConflict(
                            "LIFECYCLE_OWNER_CONFLICT",
                            "owner config identity mismatch",
                        )
                    if not metadata.get("instance_id") or not metadata.get(
                        "process_start_identity"
                    ):
                        raise LifecycleConflict(
                            "LIFECYCLE_OWNER_CONFLICT",
                            "owner metadata is incomplete",
                        )
                    pid = metadata.get("pid")
                    if not isinstance(pid, int):
                        raise LifecycleConflict(
                            "LIFECYCLE_OWNER_CONFLICT",
                            "owner metadata is incomplete",
                        )
                    observed_start = process_start_identity(pid)
                    if (
                        observed_start is not None
                        and observed_start != metadata["process_start_identity"]
                    ):
                        raise LifecycleConflict(
                            "LIFECYCLE_OWNER_CONFLICT",
                            "owner process identity is stale",
                        )
                    return metadata
                else:
                    recheck.release()
                    raise LifecycleConflict(
                        "LIFECYCLE_OWNER_MISSING", "no resident owner"
                    )
        else:
            probe.release()
            raise LifecycleConflict("LIFECYCLE_OWNER_MISSING", "no resident owner")

    def read_result(self, request_id: str) -> dict[str, Any]:
        path = self.result_path(request_id)
        if not path.exists():
            return {
                "schema_version": "haniel.lifecycle.result.v1",
                "request_id": request_id,
                "config_identity": self.identity,
                "acks": [],
            }
        return read_json(path)

    def request_path(self, request_id: str) -> Path:
        return self.requests_dir / f"{_safe(request_id)}.json"

    def result_path(self, request_id: str) -> Path:
        return self.results_dir / f"{_safe(request_id)}.json"

    def _request_transaction(self, request_id: str) -> _SerialFileLock:
        return _SerialFileLock(self.transactions_dir / f"{_safe(request_id)}.lock")

    def _owner_metadata_transaction(self) -> _SerialFileLock:
        return _SerialFileLock(self.owner_metadata_lock_path)

    def _write_owner(self, instance_id: str) -> dict[str, Any]:
        metadata = {
            "schema_version": "haniel.lifecycle.owner.v1",
            "config_identity": self.identity,
            "config_path": str(self.config_path),
            "instance_id": instance_id,
            "pid": os.getpid(),
            "process_start_identity": current_process_start_identity(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_json(
            self.owner_path,
            metadata,
        )
        return metadata

    def _prepare_owner_metadata(self, instance_id: str) -> dict[str, Any]:
        if self.owner_path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            stale = self.owner_path.with_name(f"owner.json.stale-{stamp}")
            os.replace(self.owner_path, stale)
        return self._write_owner(instance_id)


def _safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
