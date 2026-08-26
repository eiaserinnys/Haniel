"""Prepare one exact Haniel release while managed services stay online."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from ..config import RepoConfig
from .child_env import sanitized_child_env

logger = logging.getLogger(__name__)

_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_READY_MARKER = ".haniel-release-ready.json"
_RESULT_RELPATH = Path(".local") / "haniel_release_preparation.json"
_DEFAULT_RELEASE_ROOT = Path(".local") / "haniel-releases"
_CANCEL_FINALIZE_TIMEOUT_SECONDS = 5.0


class SelfUpdatePrestageError(RuntimeError):
    """An approved self-update could not be prepared safely."""


@dataclass(frozen=True)
class SelfUpdatePrestageResult:
    target_commit: str
    prepared_release: Path
    payload: dict[str, object]


PopenFactory = Callable[..., subprocess.Popen[str]]
TerminateProcessTree = Callable[..., object]


def _base_python() -> Path:
    executable = getattr(sys, "_base_executable", None) or sys.executable
    return Path(executable).resolve(strict=True)


def _read_runner_config(config_dir: Path) -> dict[str, str]:
    path = config_dir / "haniel-runner.conf"
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise SelfUpdatePrestageError(
            f"cannot read wrapper config {path}: {exc}"
        ) from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _resolve_config_path(config_dir: Path, value: str | Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = config_dir / candidate
    return candidate.resolve()


def _default_terminate_process_tree(
    process: subprocess.Popen[str], *, grace_seconds: float = 3.0
) -> None:
    if process.poll() is not None:
        process.wait()
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        process.wait(timeout=grace_seconds)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=grace_seconds)


class SelfUpdatePrestager:
    """Own the single active helper process and its durable handoff result."""

    def __init__(
        self,
        config_dir: Path,
        *,
        base_python: Path | None = None,
        helper_path: Path | None = None,
        popen_factory: PopenFactory = subprocess.Popen,
        terminate_process_tree: TerminateProcessTree = _default_terminate_process_tree,
    ) -> None:
        self.config_dir = Path(config_dir).resolve()
        self.base_python = base_python or _base_python()
        self.helper_path = helper_path or (
            Path(__file__).resolve().parents[3] / "scripts" / "haniel_atomic_release.py"
        )
        self.result_path = self.config_dir / _RESULT_RELPATH
        self._popen_factory = popen_factory
        self._terminate_process_tree = terminate_process_tree
        self._attempt_condition = threading.Condition()
        self._attempt_active = False
        self._cancel_requested = False
        self._termination_claimed = False
        self._active_process: subprocess.Popen[str] | None = None

    def release_root(self) -> Path:
        config = _read_runner_config(self.config_dir)
        return _resolve_config_path(
            self.config_dir,
            config.get("HANIEL_RELEASE_ROOT", _DEFAULT_RELEASE_ROOT),
        )

    def freeze_target(self, repo: RepoConfig, *, timeout: int) -> str:
        """Fetch refs and freeze origin/<branch> without advancing the checkout."""
        source = _resolve_config_path(self.config_dir, repo.path)
        env = sanitized_child_env()
        fetch = subprocess.run(
            ["git", "-C", str(source), "fetch", "origin"],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
        if fetch.returncode != 0:
            raise SelfUpdatePrestageError(
                f"git fetch failed: {(fetch.stderr or fetch.stdout).strip()}"
            )
        resolved = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "rev-parse",
                f"origin/{repo.branch}^{{commit}}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
        target = resolved.stdout.strip()
        if resolved.returncode != 0 or not _COMMIT_PATTERN.fullmatch(target):
            detail = (resolved.stderr or resolved.stdout).strip()
            raise SelfUpdatePrestageError(
                f"cannot resolve origin/{repo.branch} to a full commit SHA: {detail}"
            )
        return target

    def prepare(
        self,
        repo: RepoConfig,
        *,
        target_commit: str,
        timeout: int,
    ) -> SelfUpdatePrestageResult:
        if not _COMMIT_PATTERN.fullmatch(target_commit):
            raise SelfUpdatePrestageError(
                "target_commit must be a 40-character lowercase commit SHA"
            )
        with self._attempt_condition:
            duplicate = self._attempt_active
            if not duplicate:
                self._attempt_active = True
                self._cancel_requested = False
                self._termination_claimed = False
        if duplicate:
            self.cancel()
            raise SelfUpdatePrestageError(
                "self-update pre-stage is already active; cancelled duplicate approval"
            )

        process: subprocess.Popen[str] | None = None
        stderr_thread: threading.Thread | None = None
        release_root: Path | None = None
        cleanup_attempted = False
        try:
            self.discard_result()
            release_root = self.release_root()
            command = self._helper_command(repo, target_commit, release_root)
            with self._attempt_condition:
                if self._cancel_requested:
                    raise SelfUpdatePrestageError(
                        "self-update pre-stage cancelled before helper spawn"
                    )
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            process = self._popen_factory(
                command,
                env=sanitized_child_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
            terminate_after_publication = False
            with self._attempt_condition:
                self._active_process = process
                if self._cancel_requested and not self._termination_claimed:
                    self._termination_claimed = True
                    terminate_after_publication = True
            if terminate_after_publication:
                self._terminate_process_tree(process)
            stderr_thread = threading.Thread(
                target=self._forward_steps,
                args=(process.stderr,),
                name="haniel-self-update-prestage-log",
                daemon=True,
            )
            stderr_thread.start()
            try:
                return_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                self._terminate_process_tree(process)
                cleanup_attempted = True
                self._cleanup_failed_attempt(target_commit, release_root)
                raise SelfUpdatePrestageError(
                    f"self-update pre-stage timed out after {timeout}s"
                ) from exc
            if stderr_thread is not None:
                stderr_thread.join(timeout=2)
            if return_code != 0:
                error = self._result_error() or f"helper exited with code {return_code}"
                cleanup_attempted = True
                self._cleanup_failed_attempt(target_commit, release_root)
                raise SelfUpdatePrestageError(error)
            with self._attempt_condition:
                cancelled = self._cancel_requested
            if cancelled:
                cleanup_attempted = True
                self._cleanup_failed_attempt(target_commit, release_root)
                raise SelfUpdatePrestageError("self-update pre-stage cancelled")
            try:
                return self._validate_result(target_commit, release_root)
            except SelfUpdatePrestageError:
                cleanup_attempted = True
                self._cleanup_failed_attempt(target_commit, release_root)
                raise
        finally:
            try:
                with self._attempt_condition:
                    cancelled = self._cancel_requested
                if cancelled and release_root is not None and not cleanup_attempted:
                    cleanup_attempted = True
                    self._cleanup_failed_attempt(target_commit, release_root)
            finally:
                with self._attempt_condition:
                    if self._active_process is process:
                        self._active_process = None
                    self._attempt_active = False
                    self._cancel_requested = False
                    self._termination_claimed = False
                    self._attempt_condition.notify_all()

    def cancel(self) -> None:
        """Cancel and wait until the active helper and cleanup are complete."""
        process: subprocess.Popen[str] | None = None
        with self._attempt_condition:
            if not self._attempt_active:
                return
            self._cancel_requested = True
            if self._active_process is not None and not self._termination_claimed:
                self._termination_claimed = True
                process = self._active_process
        termination_error: BaseException | None = None
        if process is not None:
            try:
                self._terminate_process_tree(process)
            except BaseException as exc:
                termination_error = exc
                logger.exception(
                    "Could not terminate self-update helper process tree; "
                    "attempting direct kill"
                )
                try:
                    process.kill()
                    process.wait(timeout=3)
                except BaseException:
                    logger.exception(
                        "Direct self-update helper kill did not complete cleanly"
                    )
        with self._attempt_condition:
            if termination_error is None:
                while self._attempt_active:
                    self._attempt_condition.wait()
            else:
                deadline = time.monotonic() + _CANCEL_FINALIZE_TIMEOUT_SECONDS
                while self._attempt_active:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._attempt_condition.wait(timeout=remaining)
                attempt_active = self._attempt_active
        if termination_error is not None:
            detail = " and helper did not finalize" if attempt_active else ""
            raise SelfUpdatePrestageError(
                f"could not terminate helper process tree{detail}"
            ) from termination_error

    def discard_result(self) -> None:
        try:
            self.result_path.unlink()
        except FileNotFoundError:
            pass

    def _helper_command(
        self, repo: RepoConfig, target_commit: str, release_root: Path
    ) -> list[str]:
        source = _resolve_config_path(self.config_dir, repo.path)
        return [
            str(self.base_python),
            str(self.helper_path),
            "prepare",
            "--no-switch",
            "--target-commit",
            target_commit,
            "--source",
            str(source),
            "--release-root",
            str(release_root),
            "--bootstrap-python",
            str(self.base_python),
            "--result-json",
            str(self.result_path),
        ]

    @staticmethod
    def _forward_steps(stream: TextIO | None) -> None:
        if stream is None:
            return
        for raw_line in stream:
            line = raw_line.rstrip()
            if line:
                logger.info("Self-update pre-stage: %s", line)

    def _result_error(self) -> str | None:
        try:
            payload = json.loads(self.result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        return str(error) if error else None

    def _validate_result(
        self, target_commit: str, release_root: Path
    ) -> SelfUpdatePrestageResult:
        try:
            payload = json.loads(self.result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise SelfUpdatePrestageError(f"invalid helper result: {exc}") from exc
        if not isinstance(payload, dict):
            raise SelfUpdatePrestageError("invalid helper result: expected object")
        prepared_raw = payload.get("prepared_release")
        if (
            payload.get("ok") is not True
            or payload.get("pre_staged") is not True
            or payload.get("switched") is not False
            or payload.get("target_commit") != target_commit
            or not isinstance(prepared_raw, str)
        ):
            raise SelfUpdatePrestageError(
                "helper result does not match pre-stage contract"
            )
        try:
            prepared = Path(prepared_raw).resolve(strict=True)
            prepared.relative_to((release_root / "releases").resolve())
            marker = json.loads((prepared / _READY_MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise SelfUpdatePrestageError(
                f"prepared release validation failed: {exc}"
            ) from exc
        if not isinstance(marker, dict) or marker.get("commit") != target_commit:
            raise SelfUpdatePrestageError(
                "prepared release ready marker target mismatch"
            )
        return SelfUpdatePrestageResult(target_commit, prepared, payload)

    def _cleanup_failed_attempt(self, target_commit: str, release_root: Path) -> None:
        self.discard_result()
        releases = release_root / "releases"
        if not releases.is_dir():
            return
        for candidate in releases.glob(f"{target_commit[:12]}*"):
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            marker = candidate / _READY_MARKER
            try:
                ready = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                ready = None
            if isinstance(ready, dict) and ready.get("commit") == target_commit:
                continue
            shutil.rmtree(candidate)
