"""Timed result records shared by the atomic release operations."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


class ReleasePreparationError(RuntimeError):
    """A candidate failed before activation."""


@dataclass
class PreparationResult:
    ok: bool = False
    steps: list[dict[str, object]] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    warnings: list[str] = field(default_factory=list)
    active_repo: str | None = None
    active_python: str | None = None
    active_commit: str | None = None
    target_commit: str | None = None
    switched: bool = False
    migrated: bool = False

    def add_step(
        self,
        name: str,
        ok: bool,
        error: str | None = None,
        *,
        duration_sec: float,
    ) -> None:
        duration = max(0.0, float(duration_sec))
        self.steps.append(
            {
                "name": name,
                "ok": ok,
                "error": error,
                "duration_sec": duration,
            }
        )
        status = "ok" if ok else "failed"
        print(
            f"[haniel-release] step={name} status={status} duration_sec={duration:.3f}",
            file=sys.stderr,
        )
        if not ok and self.error is None:
            self.error = f"{name} failed: {error or 'no message'}"

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "ok": self.ok,
            "steps": self.steps,
            "error": self.error,
            "error_code": self.error_code,
            "warnings": self.warnings,
            "active_repo": self.active_repo,
            "active_python": self.active_python,
            "active_commit": self.active_commit,
            "target_commit": self.target_commit,
            "switched": self.switched,
            "migrated": self.migrated,
        }


def monotonic_time() -> float:
    return time.monotonic()


def elapsed_since(started_at: float) -> float:
    return max(0.0, monotonic_time() - started_at)


def command_error(completed: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if not output:
        output = f"command exited with code {completed.returncode}"
    lines = output.splitlines()
    lines.append(f"[exit={completed.returncode}]")
    return "\n".join(lines[-20:])[-4000:]


def run_step(
    result: PreparationResult,
    name: str,
    command: list[str],
    *,
    cwd: Path | None = None,
) -> None:
    started_at = monotonic_time()
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    duration = elapsed_since(started_at)
    if completed.returncode != 0:
        error = command_error(completed)
        result.add_step(name, False, error, duration_sec=duration)
        raise ReleasePreparationError(result.error or error)
    result.add_step(name, True, duration_sec=duration)
