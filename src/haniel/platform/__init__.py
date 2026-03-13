"""
Platform-specific process management abstractions.

This module provides a unified interface for platform-specific operations
like signal handling, process termination, and job control.

haniel doesn't care what platform it runs on - it just delegates to
the appropriate implementation.
"""

import sys
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess


class PlatformHandler(ABC):
    """Abstract base class for platform-specific process handling."""

    @abstractmethod
    def terminate_process(self, process: "subprocess.Popen[str]") -> None:
        """Send a graceful termination signal to the process.

        On POSIX: SIGTERM
        On Windows: CTRL_BREAK_EVENT or TerminateProcess

        Args:
            process: The subprocess to terminate
        """
        pass

    @abstractmethod
    def kill_process(self, process: "subprocess.Popen[str]") -> None:
        """Forcefully kill the process.

        On POSIX: SIGKILL
        On Windows: TerminateProcess

        Args:
            process: The subprocess to kill
        """
        pass

    @abstractmethod
    def is_port_listening(self, port: int) -> bool:
        """Check if a port is in LISTEN state.

        Args:
            port: Port number to check

        Returns:
            True if the port is listening, False otherwise
        """
        pass

    @abstractmethod
    def setup_process_group(self, process: "subprocess.Popen[str]") -> None:
        """Set up process group for proper signal propagation.

        On POSIX: Create new process group
        On Windows: Create Job Object

        Args:
            process: The subprocess to configure
        """
        pass

    @abstractmethod
    def get_subprocess_kwargs(self) -> dict:
        """Get platform-specific kwargs for subprocess.Popen.

        Returns:
            Dict of kwargs to pass to subprocess.Popen
        """
        pass


def get_platform_handler() -> PlatformHandler:
    """Get the appropriate platform handler for the current OS.

    Returns:
        PlatformHandler instance for the current platform
    """
    if sys.platform == "win32":
        from .windows import WindowsHandler

        return WindowsHandler()
    else:
        from .posix import PosixHandler

        return PosixHandler()


# Export the current platform handler
__all__ = ["PlatformHandler", "get_platform_handler"]
