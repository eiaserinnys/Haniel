"""
Log capture and management for haniel services.

haniel captures stdout/stderr from each service and writes to log files.
It also supports real-time pattern matching for ready: log:{pattern} conditions.
"""

import codecs
import os
import re
import selectors
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from io import TextIOWrapper
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class PatternCallbackHandle:
    """Exact identity for one registered pattern callback."""

    identifier: int


class LogCapture:
    """Captures and manages logs for a single service.

    Features:
    - Writes to a log file
    - Maintains a rolling buffer for recent lines
    - Supports real-time pattern matching callbacks
    """

    DEFAULT_BUFFER_SIZE = 1000  # Keep last 1000 lines in memory

    def __init__(
        self,
        service_name: str,
        log_dir: Path,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
    ):
        """Initialize log capture for a service.

        Args:
            service_name: Name of the service
            log_dir: Directory to write log files to
            buffer_size: Number of recent lines to keep in memory
        """
        self.service_name = service_name
        self.log_dir = log_dir
        self.buffer_size = buffer_size

        self._buffer: deque[str] = deque(maxlen=buffer_size)
        self._pattern_callbacks: list[
            tuple[PatternCallbackHandle, re.Pattern[str], Callable[[str], None]]
        ] = []
        self._next_callback_identifier = 1
        self._lock = threading.Lock()
        self._log_file: TextIOWrapper | None = None
        self._log_path: Path | None = None

    @property
    def log_path(self) -> Path | None:
        """Get the path to the log file."""
        return self._log_path

    def start(self) -> None:
        """Start log capture, opening the log file."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.log_dir / f"{self.service_name}.log"

        # Open log file in append mode with line buffering
        self._log_file = open(self._log_path, "a", encoding="utf-8", buffering=1)

        # Write startup marker
        timestamp = datetime.now().isoformat()
        self._log_file.write(f"\n=== Service started at {timestamp} ===\n")
        self._log_file.flush()

    def stop(self) -> None:
        """Stop log capture, closing the log file."""
        if self._log_file:
            timestamp = datetime.now().isoformat()
            self._log_file.write(f"=== Service stopped at {timestamp} ===\n")
            self._log_file.close()
            self._log_file = None

    def write_line(self, line: str, source: str = "stdout") -> None:
        """Write a line to the log.

        Args:
            line: The log line to write
            source: Source of the line ("stdout" or "stderr")
        """
        # Strip trailing newline if present (we'll add our own)
        line = line.rstrip("\n\r")
        if not line:
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] [{source}] {line}"

        matched_callbacks: list[Callable[[str], None]] = []
        with self._lock:
            # Add to buffer
            self._buffer.append(formatted)

            # Write to file
            if self._log_file:
                self._log_file.write(f"{formatted}\n")

            # Check pattern callbacks
            for _handle, pattern, callback in self._pattern_callbacks:
                if pattern.search(line):
                    matched_callbacks.append(callback)

        # Readiness callbacks only record evidence. Calling outside the capture
        # lock gives their lifecycle owner an explicit happens-before boundary.
        for callback in matched_callbacks:
            callback(line)

    def add_pattern_callback(
        self,
        pattern: str,
        callback: Callable[[str], None],
    ) -> PatternCallbackHandle:
        """Add a callback to be called when a pattern is matched.

        Args:
            pattern: Regex pattern to match
            callback: Function to call with the matching line
        """
        compiled = re.compile(pattern)
        with self._lock:
            handle = PatternCallbackHandle(self._next_callback_identifier)
            self._next_callback_identifier += 1
            self._pattern_callbacks.append((handle, compiled, callback))
            return handle

    def remove_pattern_callback(self, handle: PatternCallbackHandle | str) -> None:
        """Remove a pattern callback.

        Args:
            pattern: The pattern string to remove
        """
        with self._lock:
            if isinstance(handle, PatternCallbackHandle):
                self._pattern_callbacks = [
                    entry for entry in self._pattern_callbacks if entry[0] != handle
                ]
            else:
                self._pattern_callbacks = [
                    entry
                    for entry in self._pattern_callbacks
                    if entry[1].pattern != handle
                ]

    @property
    def callback_count(self) -> int:
        with self._lock:
            return len(self._pattern_callbacks)

    def get_recent_lines(self, n: int | None = None) -> list[str]:
        """Get recent log lines from the buffer.

        Args:
            n: Number of lines to return (default: all in buffer)

        Returns:
            List of recent log lines
        """
        with self._lock:
            if n is None:
                return list(self._buffer)
            return list(self._buffer)[-n:]

    def search_pattern(self, pattern: str) -> list[str]:
        """Search for a pattern in the recent log buffer.

        Args:
            pattern: Regex pattern to search for

        Returns:
            List of matching lines
        """
        compiled = re.compile(pattern)
        with self._lock:
            return [line for line in self._buffer if compiled.search(line)]


class LogManager:
    """Manages log captures for multiple services."""

    def __init__(self, log_dir: Path):
        """Initialize the log manager.

        Args:
            log_dir: Base directory for log files
        """
        self.log_dir = log_dir
        self._captures: dict[str, LogCapture] = {}
        self._lock = threading.Lock()

    def get_capture(self, service_name: str) -> LogCapture:
        """Get or create a log capture for a service.

        Args:
            service_name: Name of the service

        Returns:
            LogCapture instance for the service
        """
        with self._lock:
            if service_name not in self._captures:
                self._captures[service_name] = LogCapture(
                    service_name=service_name,
                    log_dir=self.log_dir,
                )
            return self._captures[service_name]

    def start_capture(self, service_name: str) -> LogCapture:
        """Start log capture for a service.

        Args:
            service_name: Name of the service

        Returns:
            LogCapture instance (started)
        """
        capture = self.get_capture(service_name)
        capture.start()
        return capture

    def stop_capture(self, service_name: str) -> None:
        """Stop log capture for a service.

        Args:
            service_name: Name of the service
        """
        with self._lock:
            if service_name in self._captures:
                self._captures[service_name].stop()

    def stop_all(self) -> None:
        """Stop all log captures."""
        with self._lock:
            for capture in self._captures.values():
                capture.stop()

    def list_services(self) -> list[str]:
        """List all services with log captures.

        Returns:
            List of service names
        """
        with self._lock:
            return list(self._captures.keys())

    def get_log_tail(self, service_name: str, n: int = 50) -> list[str]:
        """Get the last N lines from a service's log file.

        Args:
            service_name: Name of the service
            n: Number of lines to return (default: 50)

        Returns:
            List of the last N lines, or empty list if service not found
        """
        log_path = self.log_dir / f"{service_name}.log"
        return get_log_tail(log_path, n)


class StreamReader(threading.Thread):
    """Reads from a stream and writes to a LogCapture.

    Used to capture stdout/stderr from subprocesses in a non-blocking way.
    """

    def __init__(
        self,
        stream: TextIOWrapper,
        log_capture: LogCapture,
        source: str = "stdout",
    ):
        """Initialize the stream reader.

        Args:
            stream: The stream to read from
            log_capture: LogCapture to write to
            source: Source identifier ("stdout" or "stderr")
        """
        super().__init__(daemon=True, name=f"haniel-stream-{source}")
        self.stream = stream
        self.log_capture = log_capture
        self.source = source
        self._stop_event = threading.Event()
        self._wake_read: int | None = None
        self._wake_write: int | None = None
        self._closed = threading.Event()
        if os.name != "nt":
            self._wake_read, self._wake_write = os.pipe()
            os.set_blocking(self._wake_read, False)
            os.set_blocking(self._wake_write, False)

    def run(self) -> None:
        """Read bytes until EOF or the private wakeup becomes readable.

        The reader is the sole owner allowed to close the buffered wrapper after
        it starts. Closing a ``TextIOWrapper`` from another thread can block in
        buffered I/O while an escaped descendant still owns the pipe writer.
        """
        try:
            self.stream.fileno()
        except (AttributeError, OSError, ValueError):
            try:
                for line in self.stream:
                    if self._stop_event.is_set():
                        break
                    self.log_capture.write_line(line, self.source)
            finally:
                self._close_owned_resources()
            return

        decoder = codecs.getincrementaldecoder(self.stream.encoding or "utf-8")(
            errors=self.stream.errors or "replace"
        )
        pending = ""
        try:
            chunks = self._windows_chunks() if os.name == "nt" else self._posix_chunks()
            for chunk in chunks:
                pending += decoder.decode(chunk)
                pending = self._emit_complete_lines(pending)
            pending += decoder.decode(b"", final=True)
            if pending:
                self.log_capture.write_line(pending, self.source)
        except (ValueError, OSError):
            pass
        finally:
            self._close_owned_resources()

    def _posix_chunks(self):
        assert self._wake_read is not None
        selector = selectors.DefaultSelector()
        stream_fd = self.stream.fileno()
        selector.register(stream_fd, selectors.EVENT_READ, "stream")
        selector.register(self._wake_read, selectors.EVENT_READ, "wake")
        try:
            while True:
                events = selector.select()
                if any(key.data == "wake" for key, _mask in events):
                    return
                for key, _mask in events:
                    if key.data != "stream":
                        continue
                    chunk = os.read(stream_fd, 65536)
                    if not chunk:
                        return
                    yield chunk
        finally:
            selector.close()

    def _windows_chunks(self):
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.PeekNamedPipe.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        kernel32.PeekNamedPipe.restype = wintypes.BOOL
        fd = self.stream.fileno()
        handle = msvcrt.get_osfhandle(fd)
        available = wintypes.DWORD()
        while not self._stop_event.is_set():
            ok = kernel32.PeekNamedPipe(
                handle, None, 0, None, ctypes.byref(available), None
            )
            if not ok:
                error = ctypes.get_last_error()
                if error in {6, 109, 232}:  # invalid/broken/no-data pipe
                    return
                raise ctypes.WinError(error)
            if available.value:
                chunk = os.read(fd, min(available.value, 65536))
                if not chunk:
                    return
                yield chunk
                continue
            self._stop_event.wait(0.02)

    def _emit_complete_lines(self, content: str) -> str:
        lines = content.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            pending = lines.pop()
        else:
            pending = ""
        for line in lines:
            self.log_capture.write_line(line, self.source)
        return pending

    def _close_owned_resources(self) -> None:
        try:
            if not self.stream.closed:
                self.stream.close()
        except (OSError, ValueError):
            pass
        for attribute in ("_wake_read", "_wake_write"):
            descriptor = getattr(self, attribute)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, attribute, None)
        self._closed.set()

    def stop(self) -> None:
        """Signal the reader through its private wakeup without closing its stream."""
        self._stop_event.set()
        if self._wake_write is not None:
            try:
                os.write(self._wake_write, b"x")
            except OSError:
                pass

    def close_before_start(self) -> None:
        """Close resources when thread startup failed before ownership transfer."""
        if self.ident is not None or self.is_alive():
            raise RuntimeError("reader thread already owns its stream")
        self.stop()
        self._close_owned_resources()


def get_log_tail(
    log_path: Path,
    n: int = 50,
    max_bytes: int = 10 * 1024 * 1024,
) -> list[str]:
    """Read the last N lines from a log file efficiently.

    For large files, only reads the last `max_bytes` bytes to avoid
    memory issues.

    Args:
        log_path: Path to the log file
        n: Number of lines to return (default: 50)
        max_bytes: Maximum bytes to read for large files (default: 10MB)

    Returns:
        List of the last N lines from the file
    """
    if not log_path.exists():
        return []

    try:
        file_size = log_path.stat().st_size

        # For small files, read all
        if file_size <= max_bytes:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                lines = [line.rstrip("\n\r") for line in lines]
                lines = [line for line in lines if line]
                return lines[-n:] if n < len(lines) else lines

        # For large files, read from end to save memory
        with open(log_path, "rb") as f:
            f.seek(max(0, file_size - max_bytes))
            # Skip partial first line if we didn't start at beginning
            if file_size > max_bytes:
                f.readline()  # Discard partial line
            content = f.read().decode("utf-8", errors="replace")
            lines = content.splitlines()
            lines = [line for line in lines if line.strip()]
            return lines[-n:] if n < len(lines) else lines
    except OSError:
        return []
