"""Run the ``validate`` command of an install config key against a chosen value."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from ..config.model import ConfigKeyConfig

VALIDATE_TIMEOUT_SECONDS = 60
OUTPUT_TAIL_LINES = 5


class ConfigValueValidationError(RuntimeError):
    """A config value was rejected by its key's ``validate`` command."""

    def __init__(
        self, config_name: str, key: str, command: str, reason: str, output_tail: str
    ) -> None:
        self.config_name = config_name
        self.key = key
        self.command = command
        self.reason = reason
        self.output_tail = output_tail
        message = (
            f"Validation failed for {config_name}.{key}: {reason}\n  command: {command}"
        )
        if output_tail:
            message += "\n  output (last lines):\n" + "\n".join(
                f"    {line}" for line in output_tail.splitlines()
            )
        super().__init__(message)


def _as_text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output or ""


def _tail(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-OUTPUT_TAIL_LINES:])


def run_validate_cmd(
    config_name: str, key_cfg: ConfigKeyConfig, value: str, config_dir: Path
) -> None:
    """Run ``key_cfg.validate_cmd`` with ``{value}`` replaced by the value.

    The command runs through the platform shell in the config directory, as
    docs/configuration.md describes (``{value}`` is substituted, non-zero exit
    means invalid). Does nothing when the key has no validate command.

    Raises:
        ConfigValueValidationError: non-zero exit, timeout, or the shell could
            not run the command. The message names the key and the command
            template (not the substituted secret) and ends with the last
            output lines.
    """
    template = key_cfg.validate_cmd
    if not template:
        return
    command = template.replace("{value}", value)
    posix = os.name == "posix"
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=config_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=posix,
        )
    except OSError as error:
        raise ConfigValueValidationError(
            config_name, key_cfg.key, template, f"could not run: {error}", ""
        ) from error
    try:
        stdout, stderr = process.communicate(timeout=VALIDATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        if posix:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        stdout, stderr = process.communicate()
        raise ConfigValueValidationError(
            config_name,
            key_cfg.key,
            template,
            f"timed out after {VALIDATE_TIMEOUT_SECONDS}s",
            _tail(_as_text(stdout) + _as_text(stderr)),
        ) from error
    if process.returncode != 0:
        raise ConfigValueValidationError(
            config_name,
            key_cfg.key,
            template,
            f"exit code {process.returncode}",
            _tail(_as_text(stdout) + _as_text(stderr)),
        )
