"""
Tests for Windows platform handler.

These tests mock ctypes.windll to allow running on Linux.
Tests cover:
- WindowsHandler process termination (CTRL_BREAK_EVENT)
- Job Object management
- Process killing
- Port checking
- Subprocess kwargs
"""

import socket
import subprocess
from unittest.mock import MagicMock, patch

import pytest


# We need to mock ctypes.windll before importing WindowsHandler
# since windll only exists on Windows


class MockKernel32:
    """Mock Windows kernel32 DLL."""

    def __init__(self):
        self._jobs: dict[int, set[int]] = {}  # job_handle -> set of pids
        self._next_handle = 1000

    def GenerateConsoleCtrlEvent(self, event: int, pid: int) -> int:
        """Mock GenerateConsoleCtrlEvent."""
        return 1  # Success

    def CreateJobObjectW(self, _attrs, _name) -> int:
        """Mock CreateJobObjectW."""
        handle = self._next_handle
        self._next_handle += 1
        self._jobs[handle] = set()
        return handle

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        """Mock OpenProcess."""
        return pid + 10000  # Return a fake process handle

    def AssignProcessToJobObject(self, job_handle: int, process_handle: int) -> int:
        """Mock AssignProcessToJobObject."""
        if job_handle in self._jobs:
            self._jobs[job_handle].add(process_handle)
            return 1  # Success
        return 0

    def TerminateJobObject(self, job_handle: int, exit_code: int) -> int:
        """Mock TerminateJobObject."""
        if job_handle in self._jobs:
            del self._jobs[job_handle]
            return 1
        return 0

    def CloseHandle(self, handle: int) -> int:
        """Mock CloseHandle."""
        return 1


class MockWindll:
    """Mock ctypes.windll."""

    def __init__(self):
        self.kernel32 = MockKernel32()


@pytest.fixture
def mock_windll():
    """Create a mock windll and patch it."""
    mock = MockWindll()

    # Patch ctypes.windll
    with patch("ctypes.windll", mock, create=True):
        yield mock


@pytest.fixture
def mock_process():
    """Create a mock subprocess.Popen."""
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 12345
    process.poll.return_value = None  # Process is running
    return process


@pytest.fixture
def terminated_process():
    """Create a mock subprocess.Popen that has already terminated."""
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 12345
    process.poll.return_value = 0  # Process has exited
    return process


class TestWindowsHandler:
    """Tests for WindowsHandler class."""

    def test_init(self, mock_windll):
        """Should initialize with empty job handles."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()
        assert handler._job_handles == {}

    def test_terminate_process_sends_ctrl_break(self, mock_windll, mock_process):
        """Should send CTRL_BREAK_EVENT to running process."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()
        handler.terminate_process(mock_process)

        # Should not call terminate since CTRL_BREAK succeeded
        mock_process.terminate.assert_not_called()

    def test_terminate_process_skips_terminated(self, mock_windll, terminated_process):
        """Should skip already terminated process."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()
        handler.terminate_process(terminated_process)

        # Should not try to terminate
        terminated_process.terminate.assert_not_called()

    def test_terminate_process_fallback_on_failure(self, mock_windll, mock_process):
        """Should fall back to terminate() if CTRL_BREAK fails."""
        from haniel.platform.windows import WindowsHandler

        # Make GenerateConsoleCtrlEvent fail
        mock_windll.kernel32.GenerateConsoleCtrlEvent = MagicMock(return_value=0)

        handler = WindowsHandler()
        handler.terminate_process(mock_process)

        # Should call terminate as fallback
        mock_process.terminate.assert_called_once()

    def test_terminate_process_handles_attribute_error(self, mock_windll, mock_process):
        """Should handle AttributeError when kernel32 access fails."""
        from haniel.platform.windows import WindowsHandler

        # Make kernel32.GenerateConsoleCtrlEvent raise AttributeError
        del mock_windll.kernel32

        handler = WindowsHandler()
        handler.terminate_process(mock_process)

        # Should fall back to terminate()
        mock_process.terminate.assert_called_once()

    def test_kill_process(self, mock_windll, mock_process):
        """Should call kill() on process."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()
        handler.kill_process(mock_process)

        mock_process.kill.assert_called_once()

    def test_kill_process_skips_terminated(self, mock_windll, terminated_process):
        """Should skip already terminated process."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()
        handler.kill_process(terminated_process)

        terminated_process.kill.assert_not_called()

    def test_kill_process_terminates_job(self, mock_windll, mock_process):
        """Should terminate Job Object if one exists."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()

        # Set up a job handle
        job_handle = 999
        handler._job_handles[mock_process.pid] = job_handle

        handler.kill_process(mock_process)

        mock_process.kill.assert_called_once()
        assert mock_process.pid not in handler._job_handles

    def test_kill_process_handles_os_error(self, mock_windll, mock_process):
        """Should handle OSError gracefully."""
        from haniel.platform.windows import WindowsHandler

        mock_process.kill.side_effect = OSError("Process not found")

        handler = WindowsHandler()
        # Should not raise
        handler.kill_process(mock_process)


class TestWindowsPortCheck:
    """Tests for port checking on Windows."""

    def test_is_port_listening_true(self, mock_windll):
        """Should return True for listening port."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()

        # Mock socket to simulate connection success
        with patch("socket.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_sock.connect_ex.return_value = 0
            mock_socket.return_value = mock_sock

            result = handler.is_port_listening(8080)
            assert result is True
            mock_sock.close.assert_called_once()

    def test_is_port_listening_false(self, mock_windll):
        """Should return False for non-listening port."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()

        # Mock socket to simulate connection failure
        with patch("socket.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_sock.connect_ex.return_value = 111  # Connection refused
            mock_socket.return_value = mock_sock

            result = handler.is_port_listening(8080)
            assert result is False

    def test_is_port_listening_handles_socket_error(self, mock_windll):
        """Should handle socket errors gracefully."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()

        with patch("socket.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_sock.connect_ex.side_effect = socket.error("Connection error")
            mock_socket.return_value = mock_sock

            result = handler.is_port_listening(8080)
            assert result is False


class TestWindowsJobObject:
    """Tests for Job Object management."""

    def test_setup_process_group_creates_job(self, mock_windll, mock_process):
        """Should create Job Object and assign process."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()
        handler.setup_process_group(mock_process)

        # Should have a job handle for this process
        assert mock_process.pid in handler._job_handles

    def test_setup_process_group_handles_failure(self, mock_windll, mock_process):
        """Should handle Job Object creation failure gracefully."""
        from haniel.platform.windows import WindowsHandler

        # Make CreateJobObjectW fail
        mock_windll.kernel32.CreateJobObjectW = MagicMock(return_value=0)

        handler = WindowsHandler()
        # Should not raise
        handler.setup_process_group(mock_process)

        # No job handle should be stored (0 is falsy)
        assert mock_process.pid not in handler._job_handles

    def test_create_job_object_success(self, mock_windll):
        """Should create Job Object successfully."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()
        job = handler._create_job_object()

        assert job is not None
        assert job >= 1000  # Our mock starts at 1000

    def test_create_job_object_failure(self, mock_windll):
        """Should return None when Job Object creation fails."""
        from haniel.platform.windows import WindowsHandler

        # Make CreateJobObjectW return 0 (failure)
        mock_windll.kernel32.CreateJobObjectW = MagicMock(return_value=0)

        handler = WindowsHandler()
        job = handler._create_job_object()
        assert job is None

    def test_assign_process_to_job_success(self, mock_windll):
        """Should assign process to Job Object."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()
        job = handler._create_job_object()
        result = handler._assign_process_to_job(job, 12345)

        assert result is True

    def test_assign_process_to_job_failure(self, mock_windll):
        """Should return False when assignment fails."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()

        # Invalid job handle
        result = handler._assign_process_to_job(99999, 12345)
        assert result is False

    def test_terminate_job(self, mock_windll):
        """Should terminate all processes in Job Object."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()
        job = handler._create_job_object()

        # Should not raise
        handler._terminate_job(job)


class TestWindowsSubprocessKwargs:
    """Tests for subprocess kwargs."""

    def test_get_subprocess_kwargs(self, mock_windll):
        """Should return correct kwargs for Windows."""
        from haniel.platform.windows import (
            CREATE_BREAKAWAY_FROM_JOB,
            CREATE_NEW_PROCESS_GROUP,
            WindowsHandler,
        )

        handler = WindowsHandler()
        kwargs = handler.get_subprocess_kwargs()

        expected_flags = CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        assert kwargs == {"creationflags": expected_flags}

    def test_constants(self, mock_windll):
        """Should have correct Windows constants."""
        from haniel.platform.windows import (
            CREATE_BREAKAWAY_FROM_JOB,
            CREATE_NEW_PROCESS_GROUP,
            CTRL_BREAK_EVENT,
        )

        assert CTRL_BREAK_EVENT == 1
        assert CREATE_NEW_PROCESS_GROUP == 0x00000200
        assert CREATE_BREAKAWAY_FROM_JOB == 0x01000000


class TestWindowsEdgeCases:
    """Tests for edge cases and error handling."""

    def test_terminate_process_os_error_on_terminate(self, mock_windll, mock_process):
        """Should handle OSError on terminate()."""
        from haniel.platform.windows import WindowsHandler

        # Make CTRL_BREAK fail
        mock_windll.kernel32.GenerateConsoleCtrlEvent = MagicMock(return_value=0)
        # Make terminate() raise OSError
        mock_process.terminate.side_effect = OSError("Process not found")

        handler = WindowsHandler()
        # Should not raise
        handler.terminate_process(mock_process)

    def test_terminate_process_with_os_error_on_ctrl_event(
        self, mock_windll, mock_process
    ):
        """Should handle case when GenerateConsoleCtrlEvent raises OSError."""
        from haniel.platform.windows import WindowsHandler

        # Simulate GenerateConsoleCtrlEvent raising OSError
        mock_windll.kernel32.GenerateConsoleCtrlEvent = MagicMock(
            side_effect=OSError("No kernel32")
        )

        handler = WindowsHandler()
        handler.terminate_process(mock_process)

        # Should fall back to terminate
        mock_process.terminate.assert_called_once()

    def test_multiple_job_handles(self, mock_windll):
        """Should manage multiple Job Objects correctly."""
        from haniel.platform.windows import WindowsHandler

        handler = WindowsHandler()

        # Create multiple processes
        process1 = MagicMock(spec=subprocess.Popen)
        process1.pid = 1001
        process1.poll.return_value = None

        process2 = MagicMock(spec=subprocess.Popen)
        process2.pid = 1002
        process2.poll.return_value = None

        handler.setup_process_group(process1)
        handler.setup_process_group(process2)

        assert 1001 in handler._job_handles
        assert 1002 in handler._job_handles
        assert handler._job_handles[1001] != handler._job_handles[1002]

        # Kill one
        handler.kill_process(process1)
        assert 1001 not in handler._job_handles
        assert 1002 in handler._job_handles
