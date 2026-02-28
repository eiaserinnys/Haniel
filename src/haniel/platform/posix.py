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
