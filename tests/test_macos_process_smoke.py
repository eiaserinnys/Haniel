"""Native macOS process smoke checks for the POSIX path.

These tests run only on Darwin (the macos-compat CI job). They verify the
real ps and lsof behaviour that Linux unit tests can only mock.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from haniel.core.lifecycle_storage import process_start_identity
from haniel.platform.posix import PosixHandler

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="native macOS process smoke"
)

_CHILD_SCRIPT = """\
import socket
import sys

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
sock.listen(1)
print(sock.getsockname()[1], flush=True)
while True:
    connection, _ = sock.accept()
    connection.close()
"""


def test_process_start_identity_is_stable() -> None:
    pid = os.getpid()
    first = process_start_identity(pid)

    assert first is not None
    assert first.startswith("ps-start-utc:")
    assert process_start_identity(pid) == first


def test_child_listener_and_process_tree() -> None:
    handler = PosixHandler()
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        port_line = child.stdout.readline().strip()
        assert port_line.isdigit(), f"child did not report a port: {port_line!r}"
        port = int(port_line)

        deadline = time.monotonic() + 10
        listener_pids: set[int] = set()
        tree: set[int] = set()
        while time.monotonic() < deadline:
            listener_pids = handler._get_listening_pids_from_lsof(port)
            tree = handler._get_process_tree(os.getpid())
            if child.pid in listener_pids and child.pid in tree:
                break
            time.sleep(0.2)

        assert child.pid in listener_pids, "lsof did not report the child listener"
        assert child.pid in tree, "ps snapshot did not connect the child"
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
