"""Cleanup helpers for stale service instances occupying ready ports."""

from __future__ import annotations

import os
import re
import shlex
import time

from ..config import ServiceConfig
from ..platform import PlatformHandler


class PortInUseError(RuntimeError):
    """Raised when a service ready port is occupied by an unrelated process."""


def extract_ready_port(config: ServiceConfig) -> int | None:
    """Return the configured ready port, if the service uses ``ready: port:N``."""
    if not config.ready or not config.ready.startswith("port:"):
        return None
    try:
        return int(config.ready.split(":", 1)[1])
    except ValueError:
        return None


class StaleInstanceCleaner:
    """Terminate stale service processes before spawning a replacement."""

    def __init__(self, platform: PlatformHandler, *, sleep=time.sleep):
        self.platform = platform
        self._sleep = sleep

    def cleanup_before_start(
        self,
        *,
        service_name: str,
        config: ServiceConfig,
        grace_timeout: float,
    ) -> None:
        """Clear stale same-service listeners or fail on unrelated listeners."""
        port = extract_ready_port(config)
        if port is None:
            return

        listener_pids = self.platform.get_listening_pids(port)
        if not listener_pids:
            return

        stale_pids, unrelated_pids = self._classify_pids(listener_pids, config)
        if unrelated_pids:
            pids = ", ".join(str(pid) for pid in sorted(unrelated_pids))
            raise PortInUseError(
                f"Port {port} for service {service_name} is already in use "
                f"by unrelated PID(s): {pids}"
            )

        for pid in sorted(stale_pids):
            self.platform.terminate_pid(pid)

        if not self._wait_until_exited(stale_pids, grace_timeout):
            for pid in sorted(stale_pids):
                if self.platform.is_pid_running(pid):
                    self.platform.kill_pid(pid)

        remaining = self.platform.get_listening_pids(port)
        if remaining:
            pids = ", ".join(str(pid) for pid in sorted(remaining))
            raise PortInUseError(
                f"Port {port} for service {service_name} is still in use "
                f"after stale instance cleanup by PID(s): {pids}"
            )

    def has_ready_port_occupants(self, config: ServiceConfig) -> bool:
        """Return True when the service ready port currently has listeners."""
        port = extract_ready_port(config)
        if port is None:
            return False
        return bool(self.platform.get_listening_pids(port))

    def _classify_pids(
        self,
        pids: set[int],
        config: ServiceConfig,
    ) -> tuple[set[int], set[int]]:
        stale: set[int] = set()
        unrelated: set[int] = set()

        for pid in pids:
            if self._pid_matches_service(pid, config):
                stale.add(pid)
            else:
                unrelated.add(pid)

        return stale, unrelated

    def _pid_matches_service(
        self,
        pid: int,
        config: ServiceConfig,
    ) -> bool:
        command_line = self.platform.get_process_command_line(pid)
        if not command_line:
            return False

        command = _normalize(command_line)
        return any(fragment in command for fragment in _identity_fragments(config))

    def _wait_until_exited(self, pids: set[int], timeout: float) -> bool:
        deadline = time.monotonic() + max(timeout, 0)
        while True:
            if all(not self.platform.is_pid_running(pid) for pid in pids):
                return True
            if time.monotonic() >= deadline:
                return False
            self._sleep(min(0.05, max(deadline - time.monotonic(), 0)))


_GENERIC_COMMAND_TOKENS = {
    "bash",
    "cmd",
    "cmd.exe",
    "deno",
    "node",
    "npm",
    "npx",
    "pnpm",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "python",
    "python.exe",
    "python3",
    "python3.exe",
    "sh",
    "uvicorn",
}

_GENERIC_ARGUMENT_TOKENS = {
    "-c",
    "-m",
    "dev",
    "run",
    "serve",
    "start",
}


def _identity_fragments(config: ServiceConfig) -> list[str]:
    fragments: list[str] = []
    for token in _split_command(config.run):
        fragments.extend(_token_fragments(token))

    if config.cwd:
        fragments.extend(_path_fragments(config.cwd))

    seen: set[str] = set()
    result: list[str] = []
    for fragment in fragments:
        if fragment and fragment not in seen:
            seen.add(fragment)
            result.append(fragment)
    return result


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return command.split()


def _token_fragments(token: str) -> list[str]:
    normalized = _normalize(token.strip("\"'"))
    if not normalized or normalized.startswith("-"):
        return []
    if normalized in _GENERIC_COMMAND_TOKENS or normalized in _GENERIC_ARGUMENT_TOKENS:
        return []
    if normalized.isdigit():
        return []

    fragments = [normalized]
    fragments.extend(_path_fragments(normalized))
    return [fragment for fragment in fragments if _is_specific_fragment(fragment)]


def _path_fragments(value: str) -> list[str]:
    parts = [part for part in re.split(r"[\\/]+", _normalize(value)) if part]
    if not parts:
        return []
    return [parts[-1]]


def _is_specific_fragment(fragment: str) -> bool:
    if len(fragment) < 3:
        return False
    if fragment in _GENERIC_COMMAND_TOKENS or fragment in _GENERIC_ARGUMENT_TOKENS:
        return False
    if fragment.isdigit():
        return False
    return True


def _normalize(value: str) -> str:
    return value.replace("\\", "/").lower()
