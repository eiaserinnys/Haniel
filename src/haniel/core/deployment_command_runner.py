"""Bounded subprocess execution for release manifest commands."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .child_env import sanitized_child_env
from .safety_redaction import (
    redact_text,
    redact_value,
    sensitive_values,
)
from .service_environment import (
    DATABASE_ENVIRONMENT_KEYS,
    ServiceEnvironmentFile,
    read_service_environment_file,
)
from .path_identity import canonical_path_text


class CommandSpec(BaseModel):
    """One explicit release command."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    command: str = Field(min_length=1)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


@dataclass(frozen=True)
class CommandResult:
    """Bounded command output and an optional JSON object contract."""

    stdout: str
    json_data: dict[str, Any] | None
    stderr: str = ""


CommandRunner = Callable[[CommandSpec, dict[str, str]], CommandResult | None]

_STDERR_TAIL_CHARS = 8192
_STDOUT_TAIL_CHARS = 4096
_JSON_RESULT_MAX_CHARS = 65536


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
    command: CommandSpec,
    error: subprocess.CalledProcessError,
    secret_values: tuple[str, ...],
) -> str:
    parts = [f"command {command.name!r} failed with exit code {error.returncode}"]
    raw_stderr = (
        error.stderr.decode("utf-8", errors="replace")
        if isinstance(error.stderr, bytes)
        else (error.stderr or "")
    )
    raw_stdout = (
        error.stdout.decode("utf-8", errors="replace")
        if isinstance(error.stdout, bytes)
        else (error.stdout or "")
    )
    stderr = _output_tail(redact_text(raw_stderr, secret_values), _STDERR_TAIL_CHARS)
    stdout = _output_tail(redact_text(raw_stdout, secret_values), _STDOUT_TAIL_CHARS)
    if stderr:
        parts.append(f"stderr (last {_STDERR_TAIL_CHARS} chars):\n{stderr}")
    if stdout:
        parts.append(f"stdout (last {_STDOUT_TAIL_CHARS} chars):\n{stdout}")
    return "\n".join(parts)


def subprocess_command_runner(
    repo_path: Path,
    *,
    approved_service_environment: ServiceEnvironmentFile | None = None,
) -> CommandRunner:
    """Create a non-shell command runner rooted at one repository."""

    def run(command: CommandSpec, deploy_env: dict[str, str]) -> CommandResult:
        env = sanitized_child_env()
        service_env_secret_values: tuple[str, ...] = ()
        snapshot_directory: tempfile.TemporaryDirectory[str] | None = None
        service_env_file = deploy_env.get("HANIEL_SERVICE_ENV_FILE")
        if service_env_file:
            for key in DATABASE_ENVIRONMENT_KEYS:
                env.pop(key, None)
            expected_digest = deploy_env.get("HANIEL_SERVICE_ENV_FILE_SHA256")
            if approved_service_environment is None:
                try:
                    snapshot = read_service_environment_file(Path(service_env_file))
                except RuntimeError as error:
                    raise RuntimeError(str(error)) from error
            else:
                snapshot = approved_service_environment
                if canonical_path_text(Path(service_env_file)) != canonical_path_text(
                    snapshot.path
                ):
                    raise RuntimeError(
                        "SERVICE_ENV_FILE_CHANGED: release env file path changed"
                    )
            if expected_digest is None or snapshot.sha256 != expected_digest:
                raise RuntimeError(
                    "SERVICE_ENV_FILE_CHANGED: release env file identity changed"
                )
            service_env_secret_values = sensitive_values(snapshot.values)
            snapshot_directory = tempfile.TemporaryDirectory(
                prefix="haniel-release-env-"
            )
            snapshot_path = Path(snapshot_directory.name) / "service.env"
            _write_private_snapshot(snapshot_path, snapshot.normalized)
            deploy_env = {
                **deploy_env,
                "HANIEL_SERVICE_ENV_FILE": str(snapshot_path),
            }
        try:
            env.update(deploy_env)
            secret_values = tuple(
                dict.fromkeys((*sensitive_values(env), *service_env_secret_values))
            )
            return _execute_subprocess(command, repo_path, env, secret_values)
        finally:
            if snapshot_directory is not None:
                snapshot_directory.cleanup()

    return run


def _write_private_snapshot(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _execute_subprocess(
    command: CommandSpec,
    repo_path: Path,
    env: dict[str, str],
    secret_values: tuple[str, ...],
) -> CommandResult:
    argv = shlex.split(command.command)
    executable = argv[0] if argv else ""
    resolved_executable = shutil.which(executable, path=env.get("PATH"))
    if resolved_executable is None:
        raise RuntimeError(
            f"command {command.name!r} executable not found: {executable!r}"
        )
    argv[0] = resolved_executable
    try:
        completed = subprocess.run(
            argv,
            cwd=repo_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=command.timeout_seconds,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            _command_failure_message(command, error, secret_values)
        ) from error
    except FileNotFoundError as error:
        raise RuntimeError(
            f"command {command.name!r} could not start executable "
            f"{resolved_executable}: {error}"
        ) from error

    raw_stdout = completed.stdout.rstrip()
    raw_stderr = completed.stderr.rstrip()
    json_data = _parse_json_result(command, raw_stdout)
    safe_stdout = redact_text(raw_stdout, secret_values)
    safe_stderr = redact_text(raw_stderr, secret_values)
    return CommandResult(
        stdout=_output_tail(safe_stdout, _STDOUT_TAIL_CHARS) or "",
        stderr=_output_tail(safe_stderr, _STDERR_TAIL_CHARS) or "",
        json_data=redact_value(json_data, secret_values),
    )


def _parse_json_result(command: CommandSpec, stdout: str) -> dict[str, Any] | None:
    if not stdout:
        return None
    if len(stdout) > _JSON_RESULT_MAX_CHARS:
        raise RuntimeError(
            f"command {command.name!r} JSON result exceeds "
            f"{_JSON_RESULT_MAX_CHARS} characters"
        )
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        raise RuntimeError(f"command {command.name!r} JSON result must be an object")
    return value
