"""
Windows-specific process management.

Handles Windows systems with Job Objects and proper signal emulation.
Windows doesn't have Unix-style signals, so we use different mechanisms:
- CTRL_BREAK_EVENT for graceful termination (console apps)
- TerminateProcess for forceful termination
- Job Objects for process group management
"""

import ctypes
import socket
import subprocess
from typing import TYPE_CHECKING

from . import PlatformHandler

if TYPE_CHECKING:
    pass


# Windows constants
CTRL_BREAK_EVENT = 1
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class WindowsHandler(PlatformHandler):
    """Windows-specific implementation of process handling."""

    def __init__(self):
        """Initialize Windows handler with Job Object for process management."""
        self._job_handles: dict[int, int] = {}  # pid -> job handle
        self._breakaway_allowed: bool | None = None  # lazy-probed

    def terminate_process(self, process: subprocess.Popen) -> None:
        """Send CTRL_BREAK_EVENT to the process.

        For console applications, this is similar to SIGTERM on Unix.
        For GUI applications, this may not work and we fall back to TerminateProcess.

        Args:
            process: The subprocess to terminate
        """
        if process.poll() is not None:
            # Process already terminated
            return

        try:
            # Try CTRL_BREAK_EVENT first (graceful for console apps)
            # This requires CREATE_NEW_PROCESS_GROUP flag when creating the process
            kernel32 = ctypes.windll.kernel32
            result = kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, process.pid)

            if not result:
                # CTRL_BREAK_EVENT failed, try process.terminate()
                # which calls TerminateProcess on Windows
                process.terminate()
        except (OSError, AttributeError):
            # Fallback to standard terminate
            try:
                process.terminate()
            except OSError:
                pass

    def kill_process(self, process: subprocess.Popen) -> None:
        """Forcefully terminate the process using TerminateProcess.

        Args:
            process: The subprocess to kill
        """
        if process.poll() is not None:
            # Process already terminated
            return

        try:
            # On Windows, kill() calls TerminateProcess
            process.kill()

            # Also terminate any child processes via Job Object if we have one
            pid = process.pid
            if pid in self._job_handles:
                self._terminate_job(self._job_handles[pid])
                del self._job_handles[pid]
        except OSError:
            pass

    def is_port_listening(self, port: int) -> bool:
        """Check if a port is in LISTEN state by attempting to connect.

        Args:
            port: Port number to check

        Returns:
            True if the port is listening, False otherwise
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            result = sock.connect_ex(("127.0.0.1", port))
            return result == 0
        except (socket.error, OSError):
            return False
        finally:
            sock.close()

    def setup_process_group(self, process: subprocess.Popen) -> None:
        """Set up Job Object for the process.

        Job Objects allow us to:
        - Track all child processes
        - Terminate all processes in the job at once
        - Set resource limits (future)

        Args:
            process: The subprocess to configure
        """
        try:
            # Create a Job Object and assign the process to it
            job_handle = self._create_job_object()
            if job_handle:
                self._assign_process_to_job(job_handle, process.pid)
                self._job_handles[process.pid] = job_handle
        except (OSError, AttributeError):
            # Job Object creation failed, continue without it
            pass

    def get_subprocess_kwargs(self) -> dict:
        """Get Windows-specific subprocess kwargs.

        Uses CREATE_NEW_PROCESS_GROUP so we can send CTRL_BREAK_EVENT for
        graceful shutdown. CREATE_BREAKAWAY_FROM_JOB is added only if the
        current environment permits it — some job objects (e.g. WinSW
        service wrappers) disallow breakaway, causing PermissionError.

        Returns:
            Dict with creationflags for process group creation.
            Falls back to CREATE_NEW_PROCESS_GROUP only if breakaway
            is not available (e.g. restricted Job Object environments).
        """
        flags = CREATE_NEW_PROCESS_GROUP
        if self._breakaway_allowed is None:
            self._breakaway_allowed = self._probe_breakaway()
        if self._breakaway_allowed:
            flags |= CREATE_BREAKAWAY_FROM_JOB
        return {"creationflags": flags}

    @staticmethod
    def _probe_breakaway() -> bool:
        """Test whether CREATE_BREAKAWAY_FROM_JOB is permitted.

        Spawns a trivial subprocess with the flag. If PermissionError
        is raised, the current job object disallows breakaway.
        """
        import sys

        try:
            p = subprocess.Popen(
                [sys.executable, "-c", "0"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB,
            )
            p.wait(timeout=5)
            return True
        except PermissionError:
            return False
        except Exception:
            return False

    def _create_job_object(self) -> int | None:
        """Create a Windows Job Object.

        Returns:
            Handle to the job object, or None if creation failed
        """
        try:
            kernel32 = ctypes.windll.kernel32
            job = kernel32.CreateJobObjectW(None, None)
            if job:
                return job
        except (OSError, AttributeError):
            pass
        return None

    def _assign_process_to_job(self, job_handle: int, pid: int) -> bool:
        """Assign a process to a Job Object.

        Args:
            job_handle: Handle to the job object
            pid: Process ID to assign

        Returns:
            True if successful, False otherwise
        """
        try:
            kernel32 = ctypes.windll.kernel32
            # Open the process with PROCESS_SET_QUOTA | PROCESS_TERMINATE
            process_handle = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)
            if process_handle:
                result = kernel32.AssignProcessToJobObject(job_handle, process_handle)
                kernel32.CloseHandle(process_handle)
                return bool(result)
        except (OSError, AttributeError):
            pass
        return False

    def _terminate_job(self, job_handle: int) -> None:
        """Terminate all processes in a Job Object.

        Args:
            job_handle: Handle to the job object
        """
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.TerminateJobObject(job_handle, 1)
            kernel32.CloseHandle(job_handle)
        except (OSError, AttributeError):
            pass
