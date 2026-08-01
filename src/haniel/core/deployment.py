"""Auditable, migration-aware repository deployment orchestration."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .child_env import sanitized_child_env


class CommandSpec(BaseModel):
    """One explicit release command."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    command: str = Field(min_length=1)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


class MigrationSpec(BaseModel):
    """Migration commands executed around the process handover."""

    model_config = ConfigDict(extra="forbid")

    destructive: bool = False
    preflight: CommandSpec
    backup: CommandSpec | None = None
    verify_backup: CommandSpec | None = None
    apply: CommandSpec

    @model_validator(mode="after")
    def validate_destructive_gate(self) -> "MigrationSpec":
        if self.destructive and (self.backup is None or self.verify_backup is None):
            raise ValueError(
                "destructive migration requires both backup and verify_backup"
            )
        return self


class RecoverySpec(BaseModel):
    """Automatic compensation after a failed process handover."""

    model_config = ConfigDict(extra="forbid")

    strategy: Literal["rollback", "roll_forward"]
    command: CommandSpec
    fallback: CommandSpec | None = None

    @model_validator(mode="after")
    def validate_availability_fallback(self) -> "RecoverySpec":
        if self.strategy == "roll_forward" and self.fallback is None:
            raise ValueError(
                "roll_forward recovery requires a previous-release fallback"
            )
        return self


class ReleaseManifest(BaseModel):
    """Repository-provided contract consumed by Haniel."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["haniel.release.v1"]
    release_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    environment_service: str | None = Field(default=None, min_length=1)
    migration: MigrationSpec | None = None
    post_start_verify: list[CommandSpec] = Field(min_length=1)
    recovery: RecoverySpec

    @classmethod
    def load(cls, path: Path) -> "ReleaseManifest":
        try:
            payload = path.read_text(encoding="utf-8")
            return cls.model_validate_json(payload)
        except Exception as exc:
            raise ValueError(
                f"invalid release manifest {path}; expected haniel.release.v1: {exc}"
            ) from exc


@dataclass(frozen=True)
class DeploymentCallbacks:
    """ServiceRunner boundary used by the state machine."""

    build: Callable[[], None]
    stop: Callable[[], None]
    start_and_wait: Callable[[], None]
    rollback: Callable[[], None]
    prepare_roll_forward: Callable[[], None]


@dataclass(frozen=True)
class DeploymentResult:
    status: Literal["success", "failed"]
    recovered: bool
    skipped: bool = False


class DeploymentError(RuntimeError):
    """A deployment failed after compensation was attempted."""

    def __init__(
        self,
        message: str,
        *,
        recovered: bool,
        recovery_error: Exception | None = None,
    ) -> None:
        self.recovered = recovered
        self.recovery_error = recovery_error
        super().__init__(message)


class DeploymentStateStore:
    """Repo-scoped deployment journal with atomic replacement writes."""

    TERMINAL_STATES = frozenset({"success", "failed", "interrupted", "aborted"})

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, repo_name: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", repo_name)
        return self.directory / f"{safe_name}.json"

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
    ) -> str:
        previous_attempts: list[dict[str, Any]] = []
        existing = self.read(repo_name)
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
            }
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
        }
        if previous_attempts:
            current["previous_attempts"] = previous_attempts
        self._write(repo_name, current)
        return current["journal_attempt_id"]

    def transition(
        self,
        repo_name: str,
        state: str,
        *,
        message: str | None = None,
        recovered: bool | None = None,
    ) -> None:
        current = self.read(repo_name)
        if current is None:
            raise ValueError(f"deployment journal does not exist for {repo_name}")
        current["state"] = state
        if state in self.TERMINAL_STATES:
            current["completed_at"] = datetime.now(timezone.utc).isoformat()
        if recovered is not None:
            current["recovered"] = recovered
        current.setdefault("history", []).append(self._entry(state, message))
        self._write(repo_name, current)

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
            entry["message"] = message
        return entry

    def _write(self, repo_name: str, value: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(repo_name)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


CommandRunner = Callable[[CommandSpec, dict[str, str]], None]

_STDERR_TAIL_CHARS = 8192
_STDOUT_TAIL_CHARS = 4096


def _output_tail(output: str | bytes | None, max_chars: int) -> str | None:
    if output is None:
        return None
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    output = output.rstrip()
    if not output:
        return None
    if len(output) <= max_chars:
        return output
    omitted = len(output) - max_chars
    return f"[{omitted} earlier chars omitted]\n{output[-max_chars:]}"


def _command_failure_message(
    command: CommandSpec, error: subprocess.CalledProcessError
) -> str:
    parts = [f"command {command.name!r} failed with exit code {error.returncode}"]
    stderr = _output_tail(error.stderr, _STDERR_TAIL_CHARS)
    stdout = _output_tail(error.stdout, _STDOUT_TAIL_CHARS)
    if stderr:
        parts.append(f"stderr (last {_STDERR_TAIL_CHARS} chars):\n{stderr}")
    if stdout:
        parts.append(f"stdout (last {_STDOUT_TAIL_CHARS} chars):\n{stdout}")
    return "\n".join(parts)


class DeploymentCoordinator:
    """Execute one manifest release and compensate every failed handover."""

    def __init__(
        self,
        *,
        state_store: DeploymentStateStore,
        command_runner: CommandRunner,
    ) -> None:
        self.state_store = state_store
        self.command_runner = command_runner

    def execute(
        self,
        *,
        repo_name: str,
        previous_head: str,
        target_head: str,
        manifest: ReleaseManifest,
        callbacks: DeploymentCallbacks,
        orchestrator_attempt_id: str | None = None,
        node_id: str | None = None,
        branch: str | None = None,
        manifest_identity: str | None = None,
        manifest_digest: str | None = None,
        journal_attempt_id: str | None = None,
    ) -> DeploymentResult:
        if self.state_store.is_success(repo_name, target_head, manifest.release_id):
            return DeploymentResult(status="success", recovered=False, skipped=True)

        environment = {
            "HANIEL_DEPLOY_REPO": repo_name,
            "HANIEL_RELEASE_ID": manifest.release_id,
            "HANIEL_PREVIOUS_HEAD": previous_head,
            "HANIEL_TARGET_HEAD": target_head,
        }
        migration_started = False
        self.state_store.begin(
            repo_name,
            previous_head,
            target_head,
            manifest.release_id,
            orchestrator_attempt_id=orchestrator_attempt_id,
            node_id=node_id,
            branch=branch,
            manifest_identity=manifest_identity,
            manifest_digest=manifest_digest,
            journal_attempt_id=journal_attempt_id,
        )

        try:
            callbacks.build()
            self.state_store.transition(repo_name, "preflight")
            if manifest.migration:
                self._run(manifest.migration.preflight, environment)

            callbacks.stop()
            if manifest.migration and (
                manifest.migration.backup or manifest.migration.verify_backup
            ):
                self.state_store.transition(repo_name, "backing_up")
                if manifest.migration.backup:
                    self._run(manifest.migration.backup, environment)
                if manifest.migration.verify_backup:
                    self._run(manifest.migration.verify_backup, environment)

            self.state_store.transition(repo_name, "migrating")
            if manifest.migration:
                migration_started = True
                self._run(manifest.migration.apply, environment)

            self.state_store.transition(repo_name, "starting")
            callbacks.start_and_wait()
            self.state_store.transition(repo_name, "verifying")
            self._verify(manifest, environment)
            self.state_store.transition(repo_name, "success")
            return DeploymentResult(status="success", recovered=False)
        except Exception as deployment_error:
            return self._recover_and_raise(
                repo_name=repo_name,
                manifest=manifest,
                environment=environment,
                callbacks=callbacks,
                migration_started=migration_started,
                deployment_error=deployment_error,
            )

    def _recover_and_raise(
        self,
        *,
        repo_name: str,
        manifest: ReleaseManifest,
        environment: dict[str, str],
        callbacks: DeploymentCallbacks,
        migration_started: bool,
        deployment_error: Exception,
    ) -> DeploymentResult:
        self.state_store.transition(
            repo_name, "recovering", message=str(deployment_error)
        )
        try:
            if migration_started:
                try:
                    if manifest.recovery.strategy == "roll_forward":
                        callbacks.prepare_roll_forward()
                    self._run(manifest.recovery.command, environment)
                    if manifest.recovery.strategy == "roll_forward":
                        callbacks.start_and_wait()
                        self._verify(manifest, environment)
                    else:
                        callbacks.rollback()
                except Exception:
                    if manifest.recovery.fallback is None:
                        raise
                    callbacks.prepare_roll_forward()
                    self._run(manifest.recovery.fallback, environment)
                    callbacks.rollback()
            else:
                callbacks.rollback()
        except Exception as recovery_error:
            self.state_store.transition(
                repo_name,
                "failed",
                message=f"recovery failed: {recovery_error}",
                recovered=False,
            )
            raise DeploymentError(
                f"deployment failed: {deployment_error}; recovery failed: {recovery_error}",
                recovered=False,
                recovery_error=recovery_error,
            ) from deployment_error

        self.state_store.transition(
            repo_name,
            "failed",
            message=f"deployment failed but availability recovered: {deployment_error}",
            recovered=True,
        )
        raise DeploymentError(
            f"deployment failed but availability recovered: {deployment_error}",
            recovered=True,
        ) from deployment_error

    def _verify(self, manifest: ReleaseManifest, environment: dict[str, str]) -> None:
        for command in manifest.post_start_verify:
            self._run(command, environment)

    def _run(self, command: CommandSpec, environment: dict[str, str]) -> None:
        self.command_runner(command, environment)


def subprocess_command_runner(repo_path: Path) -> CommandRunner:
    """Create a non-shell command runner rooted at one repository."""

    def run(command: CommandSpec, deploy_env: dict[str, str]) -> None:
        env = sanitized_child_env()
        env.update(deploy_env)
        try:
            subprocess.run(
                shlex.split(command.command),
                cwd=repo_path,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
            )
        except subprocess.CalledProcessError as error:
            raise RuntimeError(_command_failure_message(command, error)) from error

    return run
