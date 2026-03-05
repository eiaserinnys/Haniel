"""Tests for haniel log capture and management."""

import tempfile
import threading
import time
from io import StringIO
from pathlib import Path

import pytest

from haniel.core.logs import LogCapture, LogManager, StreamReader


class TestLogCapture:
    """Tests for LogCapture class."""

    def test_init(self, tmp_path):
        """Test LogCapture initialization."""
        capture = LogCapture("test-service", tmp_path)
        assert capture.service_name == "test-service"
        assert capture.log_dir == tmp_path
        assert capture.log_path is None  # Not started yet

    def test_start_creates_log_file(self, tmp_path):
        """Test that start() creates a log file."""
        capture = LogCapture("test-service", tmp_path)
        capture.start()
        try:
            assert capture.log_path is not None
            assert capture.log_path.exists()
            assert capture.log_path.name == "test-service.log"
        finally:
            capture.stop()

    def test_write_line_to_file(self, tmp_path):
        """Test writing lines to log file."""
        capture = LogCapture("test-service", tmp_path)
        capture.start()
        try:
            capture.write_line("Hello, world!")
            capture.write_line("Second line", source="stderr")

            # Read the log file
            content = capture.log_path.read_text()
            assert "Hello, world!" in content
            assert "Second line" in content
            assert "[stdout]" in content
            assert "[stderr]" in content
        finally:
            capture.stop()

    def test_write_line_to_buffer(self, tmp_path):
        """Test that lines are stored in buffer."""
        capture = LogCapture("test-service", tmp_path, buffer_size=10)
        capture.start()
        try:
            capture.write_line("Line 1")
            capture.write_line("Line 2")
            capture.write_line("Line 3")

            recent = capture.get_recent_lines()
            assert len(recent) == 3
            assert "Line 1" in recent[0]
            assert "Line 2" in recent[1]
            assert "Line 3" in recent[2]
        finally:
            capture.stop()

    def test_buffer_respects_size_limit(self, tmp_path):
        """Test that buffer doesn't exceed max size."""
        capture = LogCapture("test-service", tmp_path, buffer_size=3)
        capture.start()
        try:
            for i in range(10):
                capture.write_line(f"Line {i}")

            recent = capture.get_recent_lines()
            assert len(recent) == 3
            # Should have the last 3 lines
            assert "Line 7" in recent[0]
            assert "Line 8" in recent[1]
            assert "Line 9" in recent[2]
        finally:
            capture.stop()

    def test_get_recent_lines_with_n(self, tmp_path):
        """Test getting specific number of recent lines."""
        capture = LogCapture("test-service", tmp_path)
        capture.start()
        try:
            for i in range(10):
                capture.write_line(f"Line {i}")

            recent = capture.get_recent_lines(2)
            assert len(recent) == 2
            assert "Line 8" in recent[0]
            assert "Line 9" in recent[1]
        finally:
            capture.stop()

    def test_pattern_callback(self, tmp_path):
        """Test pattern matching callback."""
        capture = LogCapture("test-service", tmp_path)
        capture.start()
        try:
            matches = []

            def callback(line):
                matches.append(line)

            capture.add_pattern_callback(r"ERROR", callback)

            capture.write_line("Normal log line")
            capture.write_line("ERROR: Something went wrong")
            capture.write_line("Another normal line")
            capture.write_line("ERROR: Another error")

            # Give callbacks time to execute
            time.sleep(0.1)

            assert len(matches) == 2
            assert "Something went wrong" in matches[0]
            assert "Another error" in matches[1]
        finally:
            capture.stop()

    def test_remove_pattern_callback(self, tmp_path):
        """Test removing a pattern callback."""
        capture = LogCapture("test-service", tmp_path)
        capture.start()
        try:
            matches = []
            capture.add_pattern_callback(r"TEST", lambda line: matches.append(line))

            capture.write_line("TEST 1")
            time.sleep(0.1)
            assert len(matches) == 1

            capture.remove_pattern_callback(r"TEST")
            capture.write_line("TEST 2")
            time.sleep(0.1)
            assert len(matches) == 1  # No new matches
        finally:
            capture.stop()

    def test_search_pattern(self, tmp_path):
        """Test searching for patterns in buffer."""
        capture = LogCapture("test-service", tmp_path)
        capture.start()
        try:
            capture.write_line("Info: Starting service")
            capture.write_line("Warning: Low memory")
            capture.write_line("Info: Service ready")
            capture.write_line("Error: Connection failed")

            info_lines = capture.search_pattern(r"Info:")
            assert len(info_lines) == 2

            error_lines = capture.search_pattern(r"Error:")
            assert len(error_lines) == 1
        finally:
            capture.stop()

    def test_empty_lines_ignored(self, tmp_path):
        """Test that empty lines are ignored."""
        capture = LogCapture("test-service", tmp_path)
        capture.start()
        try:
            capture.write_line("")  # Empty - ignored
            capture.write_line("\n")  # Only newline - ignored
            capture.write_line("Actual content")

            recent = capture.get_recent_lines()
            # Only the actual content should be captured
            assert len(recent) == 1
            assert "Actual content" in recent[0]
        finally:
            capture.stop()

    def test_startup_marker_in_log(self, tmp_path):
        """Test that startup marker is written to log."""
        capture = LogCapture("test-service", tmp_path)
        capture.start()
        capture.stop()

        content = capture.log_path.read_text()
        assert "Service started at" in content
        assert "Service stopped at" in content


class TestLogManager:
    """Tests for LogManager class."""

    def test_get_capture_creates_new(self, tmp_path):
        """Test that get_capture creates a new LogCapture if not exists."""
        manager = LogManager(tmp_path)
        capture = manager.get_capture("service1")
        assert capture.service_name == "service1"

    def test_get_capture_returns_same(self, tmp_path):
        """Test that get_capture returns the same instance for same service."""
        manager = LogManager(tmp_path)
        capture1 = manager.get_capture("service1")
        capture2 = manager.get_capture("service1")
        assert capture1 is capture2

    def test_start_capture(self, tmp_path):
        """Test starting capture for a service."""
        manager = LogManager(tmp_path)
        capture = manager.start_capture("service1")
        try:
            assert capture.log_path is not None
            assert capture.log_path.exists()
        finally:
            manager.stop_capture("service1")

    def test_stop_all(self, tmp_path):
        """Test stopping all captures."""
        manager = LogManager(tmp_path)
        manager.start_capture("service1")
        manager.start_capture("service2")

        # Write some logs
        manager.get_capture("service1").write_line("Log 1")
        manager.get_capture("service2").write_line("Log 2")

        manager.stop_all()

        # Check logs are closed properly
        log1 = tmp_path / "service1.log"
        log2 = tmp_path / "service2.log"
        assert "Service stopped at" in log1.read_text()
        assert "Service stopped at" in log2.read_text()


class TestStreamReader:
    """Tests for StreamReader class."""

    def test_reads_stream_lines(self, tmp_path):
        """Test that StreamReader reads lines from a stream."""
        capture = LogCapture("test-service", tmp_path)
        capture.start()
        try:
            # Create a fake stream
            stream = StringIO("Line 1\nLine 2\nLine 3\n")

            reader = StreamReader(stream, capture, "stdout")
            reader.start()
            reader.join(timeout=1)

            recent = capture.get_recent_lines()
            assert len(recent) == 3
            assert "Line 1" in recent[0]
            assert "Line 2" in recent[1]
            assert "Line 3" in recent[2]
        finally:
            capture.stop()

    def test_handles_source_stderr(self, tmp_path):
        """Test that StreamReader correctly tags stderr source."""
        capture = LogCapture("test-service", tmp_path)
        capture.start()
        try:
            stream = StringIO("Error message\n")
            reader = StreamReader(stream, capture, "stderr")
            reader.start()
            reader.join(timeout=1)

            recent = capture.get_recent_lines()
            assert "[stderr]" in recent[0]
        finally:
            capture.stop()


class TestLogTail:
    """Tests for log file tail functionality."""

    def test_get_log_tail_from_file(self, tmp_path):
        """Test reading last N lines from a log file."""
        # Create a log file with content
        log_file = tmp_path / "test.log"
        log_file.write_text("\n".join([f"Line {i}" for i in range(100)]))

        # Import the function we need to implement
        from haniel.core.logs import get_log_tail

        tail = get_log_tail(log_file, n=10)
        assert len(tail) == 10
        assert tail[0] == "Line 90"
        assert tail[-1] == "Line 99"

    def test_get_log_tail_less_than_n(self, tmp_path):
        """Test when file has fewer lines than requested."""
        log_file = tmp_path / "test.log"
        log_file.write_text("Line 1\nLine 2\nLine 3")

        from haniel.core.logs import get_log_tail

        tail = get_log_tail(log_file, n=10)
        assert len(tail) == 3

    def test_get_log_tail_empty_file(self, tmp_path):
        """Test with empty file."""
        log_file = tmp_path / "test.log"
        log_file.write_text("")

        from haniel.core.logs import get_log_tail

        tail = get_log_tail(log_file, n=10)
        assert len(tail) == 0

    def test_get_log_tail_nonexistent_file(self, tmp_path):
        """Test with non-existent file."""
        log_file = tmp_path / "nonexistent.log"

        from haniel.core.logs import get_log_tail

        tail = get_log_tail(log_file, n=10)
        assert tail == []


class TestLogManagerApi:
    """Tests for LogManager API methods."""

    def test_list_services(self, tmp_path):
        """Test listing all services with logs."""
        manager = LogManager(tmp_path)
        manager.start_capture("service1")
        manager.start_capture("service2")
        manager.start_capture("service3")

        services = manager.list_services()
        assert "service1" in services
        assert "service2" in services
        assert "service3" in services

        manager.stop_all()

    def test_get_log_tail(self, tmp_path):
        """Test getting log tail through LogManager."""
        manager = LogManager(tmp_path)
        capture = manager.start_capture("service1")

        # Write some lines
        for i in range(20):
            capture.write_line(f"Log line {i}")

        manager.stop_capture("service1")

        tail = manager.get_log_tail("service1", n=5)
        assert len(tail) == 5
        # Last line should be the "Service stopped" marker
        assert "Service stopped" in tail[-1]
        # Second to last should be "Log line 19"
        assert "Log line 19" in tail[-2]

    def test_get_log_tail_nonexistent_service(self, tmp_path):
        """Test getting log tail for non-existent service."""
        manager = LogManager(tmp_path)

        tail = manager.get_log_tail("nonexistent", n=10)
        assert tail == []
