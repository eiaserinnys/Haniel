"""Durable JSON and process identity primitives for lifecycle control."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_PROCESS_START_FALLBACK = uuid4().hex

_PS_WEEKDAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
_PS_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp"
    )
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


def current_process_start_identity() -> str:
    identity = process_start_identity(os.getpid())
    if identity is not None:
        return identity
    return f"process-instance:{os.getpid()}:{_PROCESS_START_FALLBACK}"


def process_start_identity(pid: int) -> str | None:
    """Resolve an OS process creation identity so PID reuse cannot impersonate owner."""
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start_identity(pid)
    if sys.platform == "darwin":
        return _posix_ps_start_identity(pid)
    stat = Path(f"/proc/{pid}/stat")
    try:
        fields_after_name = stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    except (FileNotFoundError, IndexError, OSError):
        return _posix_ps_start_identity(pid)
    if len(fields_after_name) <= 19:
        return _posix_ps_start_identity(pid)
    return f"proc-start-ticks:{fields_after_name[19]}"


def _posix_ps_start_identity(pid: int) -> str | None:
    """Read process start time from ps for POSIX systems without procfs."""
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["TZ"] = "UTC0"
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if len(lines) != 1:
        return None
    fields = lines[0].split()
    if len(fields) != 5:
        return None
    weekday, month, day, clock, year = fields
    if weekday not in _PS_WEEKDAYS:
        return None
    month_number = _PS_MONTHS.get(month)
    if month_number is None:
        return None
    try:
        hour, minute, second = (int(part) for part in clock.split(":"))
        started_at = datetime(int(year), month_number, int(day), hour, minute, second)
    except ValueError:
        return None
    return f"ps-start-utc:{started_at.strftime('%Y-%m-%dT%H:%M:%S')}"


def _windows_process_start_identity(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    try:
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return f"windows-filetime:{ticks}"
    finally:
        kernel32.CloseHandle(handle)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
