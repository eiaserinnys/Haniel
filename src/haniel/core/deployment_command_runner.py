"""Bounded subprocess execution for release manifest commands."""

from __future__ import annotations

import json
import os
import signal
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
from .deployment_errors import StableDeploymentError
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


class DeploymentCommandError(StableDeploymentError):
    """One release child command failed at the subprocess boundary."""

    def __init__(
        self,
        code: str,
        command_name: str,
        message: str,
        *,
        returncode: int | None = None,
    ) -> None:
        self.command_name = command_name
        self.returncode = returncode
        super().__init__(code, message)


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
                    raise DeploymentCommandError(
                        "SERVICE_ENV_FILE_CHANGED",
                        command.name,
                        str(error),
                    ) from error
            else:
                snapshot = approved_service_environment
                if canonical_path_text(Path(service_env_file)) != canonical_path_text(
                    snapshot.path
                ):
                    raise DeploymentCommandError(
                        "SERVICE_ENV_FILE_CHANGED",
                        command.name,
                        "release env file path changed",
                    )
            if expected_digest is None or snapshot.sha256 != expected_digest:
                raise DeploymentCommandError(
                    "SERVICE_ENV_FILE_CHANGED",
                    command.name,
                    "release env file identity changed",
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
    argv = _split_command(command.command)
    executable = argv[0] if argv else ""
    resolved_executable = shutil.which(executable, path=env.get("PATH"))
    if resolved_executable is None:
        raise DeploymentCommandError(
            "COMMAND_NOT_FOUND",
            command.name,
            f"command {command.name!r} executable not found: {executable!r}",
        )
    argv[0] = resolved_executable
    try:
        completed = _run_process_tree(
            argv,
            cwd=repo_path,
            env=env,
            timeout=command.timeout_seconds,
        )
    except subprocess.CalledProcessError as error:
        raise DeploymentCommandError(
            "COMMAND_EXIT_NONZERO",
            command.name,
            _command_failure_message(command, error, secret_values),
            returncode=error.returncode,
        ) from error
    except subprocess.TimeoutExpired as error:
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
        stderr = _output_tail(
            redact_text(raw_stderr, secret_values), _STDERR_TAIL_CHARS
        )
        stdout = _output_tail(
            redact_text(raw_stdout, secret_values), _STDOUT_TAIL_CHARS
        )
        evidence = "\n".join(
            part
            for part in (
                f"command {command.name!r} timed out after {command.timeout_seconds}s",
                f"stderr:\n{stderr}" if stderr else "",
                f"stdout:\n{stdout}" if stdout else "",
            )
            if part
        )
        raise DeploymentCommandError(
            "COMMAND_TIMEOUT",
            command.name,
            evidence,
        ) from error
    except FileNotFoundError as error:
        raise DeploymentCommandError(
            "COMMAND_START_FAILED",
            command.name,
            f"command {command.name!r} could not start executable "
            f"{resolved_executable}: {error}",
        ) from error
    except OSError as error:
        raise DeploymentCommandError(
            "COMMAND_START_FAILED",
            command.name,
            f"command {command.name!r} could not start executable "
            f"{resolved_executable}: {error}",
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


def _run_process_tree(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run one command in an isolated process group and reap its descendants."""

    process_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_kwargs["start_new_session"] = True
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **process_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            argv,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from error
    completed = subprocess.CompletedProcess(
        argv,
        process.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode,
            argv,
            output=stdout,
            stderr=stderr,
        )
    return completed


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            pass
    if process.poll() is None:
        process.kill()


def _split_command(command: str, *, windows: bool | None = None) -> list[str]:
    """Split an explicit command without destroying Windows path separators."""

    use_windows_rules = os.name == "nt" if windows is None else windows
    if not use_windows_rules:
        return shlex.split(command)
    argv = shlex.split(command, posix=False)
    return [
        token[1:-1]
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}
        else token
        for token in argv
    ]


def _parse_json_result(command: CommandSpec, stdout: str) -> dict[str, Any] | None:
    if not stdout:
        return None
    if len(stdout) > _JSON_RESULT_MAX_CHARS:
        raise DeploymentCommandError(
            "COMMAND_RESULT_TOO_LARGE",
            command.name,
            f"JSON result exceeds {_JSON_RESULT_MAX_CHARS} characters",
        )
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        raise DeploymentCommandError(
            "COMMAND_RESULT_INVALID",
            command.name,
            "JSON result must be an object",
        )
    return value
