"""Bounded subprocess execution for release manifest commands."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .child_env import sanitized_child_env


class CommandSpec(BaseModel):
    """One explicit release command."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    command: str = Field(min_length=1)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


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
