"""
POSIX-specific process management.

Handles Unix-like systems (Linux, macOS) with proper signal handling
and process group management.
"""

import os
import signal
import socket
import subprocess
from typing import TYPE_CHECKING

from . import PlatformHandler

if TYPE_CHECKING:
    pass


class PosixHandler(PlatformHandler):
    """POSIX-specific implementation of process handling."""

    def terminate_process(self, process: subprocess.Popen) -> None:
        """Send SIGTERM to the process group.

        Args:
            process: The subprocess to terminate
        """
        if process.poll() is not None:
            # Process already terminated
            return

        try:
            # Try to send SIGTERM to the process group
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            # Process or group doesn't exist, try direct termination
            try:
                process.terminate()
            except (ProcessLookupError, OSError):
                pass

    def kill_process(self, process: subprocess.Popen) -> None:
        """Send SIGKILL to the process group.

        Args:
            process: The subprocess to kill
        """
        if process.poll() is not None:
            # Process already terminated
            return

        try:
            # Try to send SIGKILL to the process group
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            # Process or group doesn't exist, try direct kill
            try:
                process.kill()
            except (ProcessLookupError, OSError):
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

    def is_port_owned_by_process_tree(self, port: int, root_pid: int) -> bool:
        """Check whether a LISTEN port is owned by root_pid or its descendants."""
        listener_pids = self._get_listening_pids(port)
        if not listener_pids:
            return False

        process_tree = self._get_process_tree(root_pid)
        return bool(listener_pids & process_tree)

    def _get_listening_pids(self, port: int) -> set[int]:
        """Return PIDs listening on a TCP port using common POSIX tools."""
        proc_pids = self._get_listening_pids_from_proc(port)
        if proc_pids:
            return proc_pids
        ss_pids = self._get_listening_pids_from_ss(port)
        if ss_pids:
            return ss_pids
        return self._get_listening_pids_from_lsof(port)

    @staticmethod
    def _get_listening_pids_from_proc(port: int) -> set[int]:
        proc = "/proc"
        if not os.path.isdir(proc):
            return set()

        inodes: set[str] = set()
        port_hex = f"{port:04X}"
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table, encoding="ascii") as f:
                    rows = f.readlines()[1:]
            except OSError:
                continue

            for row in rows:
                fields = row.split()
                if len(fields) < 10:
                    continue
                local_addr = fields[1]
                state = fields[3]
                inode = fields[9]
                if state == "0A" and local_addr.rsplit(":", 1)[-1] == port_hex:
                    inodes.add(inode)

        if not inodes:
            return set()

        pids: set[int] = set()
        for pid_name in os.listdir(proc):
            if not pid_name.isdigit():
                continue
            fd_dir = os.path.join(proc, pid_name, "fd")
            try:
                fd_names = os.listdir(fd_dir)
            except OSError:
                continue
            for fd_name in fd_names:
                try:
                    target = os.readlink(os.path.join(fd_dir, fd_name))
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    pids.add(int(pid_name))
                    break
        return pids

    @staticmethod
    def _get_listening_pids_from_ss(port: int) -> set[int]:
        try:
            result = subprocess.run(
                ["ss", "-H", "-ltnp", f"sport = :{port}"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return set()

        pids: set[int] = set()
        for token in result.stdout.replace(",", " ").split():
            if token.startswith("pid="):
                try:
                    pids.add(int(token.split("=", 1)[1]))
                except ValueError:
                    pass
        return pids

    @staticmethod
    def _get_listening_pids_from_lsof(port: int) -> set[int]:
        try:
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=2,
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
    def _get_process_tree(root_pid: int) -> set[int]:
        tree = {root_pid}
        frontier = [root_pid]
        while frontier:
            pid = frontier.pop()
            try:
                result = subprocess.run(
                    ["ps", "-o", "pid=", "--ppid", str(pid)],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue

            for line in result.stdout.splitlines():
                try:
                    child = int(line.strip())
                except ValueError:
                    continue
                if child not in tree:
                    tree.add(child)
                    frontier.append(child)
        return tree

    def setup_process_group(self, process: subprocess.Popen) -> None:
        """No additional setup needed on POSIX.

        Process group is created via start_new_session in Popen kwargs.

        Args:
            process: The subprocess to configure
        """
        # Process group is set up during Popen via start_new_session=True
        pass

    def get_subprocess_kwargs(self) -> dict:
        """Get POSIX-specific subprocess kwargs.

        Returns:
            Dict with start_new_session=True for process group isolation
        """
        return {
            "start_new_session": True,  # Create new process group
        }
