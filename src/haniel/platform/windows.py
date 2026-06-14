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

    def get_listening_pids(self, port: int) -> set[int]:
        """Return PIDs listening on a TCP port."""
        pids = self._get_listening_pids_from_netstat(port)
        if pids:
            return pids

        script = f"""
$listeners = @(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
$listeners | ForEach-Object {{ Write-Output $_ }}
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return set()

        pids: set[int] = set()
        for line in result.stdout.splitlines():
            try:
                pids.add(int(line.strip()))
            except ValueError:
                pass
        return pids

    @staticmethod
    def _get_listening_pids_from_netstat(port: int) -> set[int]:
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return set()

        pids: set[int] = set()
        target_port = str(port)
        for line in result.stdout.splitlines():
            columns = line.split()
            if len(columns) < 5 or columns[0].upper() != "TCP":
                continue
            local_address = columns[1]
            state = columns[3].upper()
            pid = columns[4]
            if state != "LISTENING":
                continue
            if local_address.rsplit(":", 1)[-1] != target_port:
                continue
            try:
                pids.add(int(pid))
            except ValueError:
                pass
        return pids

    def get_process_command_line(self, pid: int) -> str | None:
        """Return a process command line using CIM."""
        script = (
            f'$proc = Get-CimInstance Win32_Process -Filter "ProcessId={pid}" '
            "-ErrorAction SilentlyContinue; "
            "if ($null -ne $proc) { Write-Output $proc.CommandLine }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        command = result.stdout.strip()
        return command or None

    def is_pid_running(self, pid: int) -> bool:
        """Return True when a PID exists."""
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Get-Process -Id {pid} -ErrorAction SilentlyContinue",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False

        return result.returncode == 0 and bool(result.stdout.strip())

    def terminate_pid(self, pid: int) -> None:
        """Ask a process tree to terminate without force."""
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def kill_pid(self, pid: int) -> None:
        """Forcefully terminate a process tree."""
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def is_port_owned_by_process_tree(self, port: int, root_pid: int) -> bool:
        """Check whether a LISTEN port is owned by root_pid or its descendants."""
        listener_pids = self.get_listening_pids(port)
        if not listener_pids:
            return False

        return any(
            self._is_descendant_pid(listener_pid, root_pid)
            for listener_pid in listener_pids
        )

    @staticmethod
    def _is_descendant_pid(pid: int, root_pid: int) -> bool:
        if pid == root_pid:
            return True

        script = f"""
$targetPid = {root_pid}
$currentPid = {pid}
while ($currentPid -gt 0) {{
    if ($currentPid -eq $targetPid) {{
        Write-Output "true"
        exit 0
    }}
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$currentPid" -ErrorAction SilentlyContinue
    if ($null -eq $proc) {{ break }}
    $currentPid = [int]$proc.ParentProcessId
}}
Write-Output "false"
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False

        return result.stdout.strip().lower() == "true"

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
