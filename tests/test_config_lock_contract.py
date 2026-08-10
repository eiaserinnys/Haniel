"""The resident config lock is a memory-only snapshot/CAS boundary."""

from __future__ import annotations

import ast
import concurrent.futures
import socket
import subprocess
import sys
import threading
from pathlib import Path

from haniel.config import HanielConfig, RepoConfig, ServiceConfig
from haniel.core.runner import ServiceRunner


RUNNER_HELPERS = {
    "__init__",
    "_snapshot_config_state",
    "_snapshot_repo_runtime",
    "_snapshot_repo_and_config",
    "_commit_repo_observation",
    "_replace_config_snapshot",
    "_require_config_generation",
}

FORBIDDEN_LOCK_CALLS = {
    "fetch_repo",
    "get_head",
    "get_remote_head",
    "pull_repo",
    "activate_repo_target",
    "reset_repo_to",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "open",
    "run",
    "Popen",
    "communicate",
    "check_output",
    "connect",
    "acquire",
    "start_service",
    "stop_service",
    "restart_service",
    "notify",
    "send",
    "result",
    "wait",
    "join",
    "sleep",
}

BOUNDARY_CALLS = {
    "git": {"fetch_repo", "get_head", "get_remote_head", "pull_repo"},
    "file": {"read_text", "read_bytes", "write_text", "write_bytes", "open"},
    "future": {"result", "wait"},
    "process": {"run", "Popen", "communicate", "check_output"},
    "network": {"connect", "send"},
    "callback": {"notify", "start_service", "stop_service", "restart_service"},
    "lock": {"acquire", "join", "sleep"},
}


def _functions_touching_config_lock(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    touching: set[str] = set()

    class Visitor(ast.NodeVisitor):
        current: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.current.append(node.name)
            self.generic_visit(node)
            self.current.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if node.attr == "_config_reload_lock":
                touching.add(self.current[-1] if self.current else "<module>")
            self.generic_visit(node)

    Visitor().visit(tree)
    return touching


def _calls_inside_config_lock(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: set[str] = set()

    def touches_lock(node: ast.AST) -> bool:
        return any(
            isinstance(candidate, ast.Attribute)
            and candidate.attr == "_config_reload_lock"
            for candidate in ast.walk(node)
        )

    class Visitor(ast.NodeVisitor):
        def visit_With(self, node: ast.With) -> None:
            if any(touches_lock(item.context_expr) for item in node.items):
                for candidate in ast.walk(node):
                    if not isinstance(candidate, ast.Call):
                        continue
                    if isinstance(candidate.func, ast.Attribute):
                        calls.add(candidate.func.attr)
                    elif isinstance(candidate.func, ast.Name):
                        calls.add(candidate.func.id)
            self.generic_visit(node)

    Visitor().visit(tree)
    return calls


def test_config_lock_is_only_used_by_snapshot_compare_and_atomic_commit_helpers() -> (
    None
):
    root = Path(__file__).parents[1]
    runner_uses = _functions_touching_config_lock(root / "src/haniel/core/runner.py")
    assert runner_uses <= RUNNER_HELPERS

    for relative in (
        "src/haniel/core/one_shot_handover.py",
        "src/haniel/core/orchestrated_deploy_execution.py",
        "src/haniel/core/handover_config.py",
        "src/haniel/core/service_lifecycle.py",
    ):
        assert _functions_touching_config_lock(root / relative) == set(), relative


def test_config_lock_contains_no_external_or_blocking_boundary_calls() -> None:
    root = Path(__file__).parents[1]
    calls = _calls_inside_config_lock(root / "src/haniel/core/runner.py")
    assert calls.isdisjoint(FORBIDDEN_LOCK_CALLS), sorted(calls & FORBIDDEN_LOCK_CALLS)
    for boundary, names in BOUNDARY_CALLS.items():
        assert calls.isdisjoint(names), (boundary, sorted(calls & names))


def test_boundary_inventory_is_complete_and_matches_static_contract() -> None:
    inventoried = set().union(*BOUNDARY_CALLS.values())
    assert inventoried <= FORBIDDEN_LOCK_CALLS
    assert {
        "git",
        "file",
        "future",
        "process",
        "network",
        "callback",
        "lock",
    } == set(BOUNDARY_CALLS)


def test_external_subprocess_and_git_boundaries_never_own_config_lock(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = ServiceRunner(
        HanielConfig(
            repos={"app": RepoConfig(url="unused", path="repo")},
            services={
                "svc": ServiceConfig(
                    run=f"{sys.executable} -c pass",
                    repo="app",
                    hooks={"post_pull": f"{sys.executable} -c pass"},
                )
            },
        ),
        config_dir=tmp_path,
    )
    observed: list[str] = []

    def assert_unlocked(*_args, **_kwargs):
        assert not runner._config_reload_lock._is_owned()
        observed.append("boundary")

        class Result:
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("haniel.core.runner.subprocess.run", assert_unlocked)
    assert runner.execute_hook("svc", "post_pull") is True
    assert observed == ["boundary"]


def test_each_inventory_boundary_runs_only_after_snapshot_lock_release(
    tmp_path: Path,
) -> None:
    """Exercise every R7 boundary class after the real snapshot helper returns."""

    runner = ServiceRunner(HanielConfig(), config_dir=tmp_path)
    observed: list[str] = []

    def outside_lock(label: str) -> None:
        assert not runner._config_reload_lock._is_owned(), label
        observed.append(label)

    runner._snapshot_config_state()

    # file
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("ok", encoding="utf-8")
    outside_lock("file.read_text")
    assert evidence.read_text(encoding="utf-8") == "ok"

    # process + communicate, using git as the real executable boundary
    outside_lock("git.process.Popen")
    process = subprocess.Popen(
        ["git", "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    outside_lock("process.communicate")
    stdout, _stderr = process.communicate(timeout=5)
    assert process.returncode == 0 and stdout.startswith("git version")

    # future.result
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: "done")
        outside_lock("future.result")
        assert future.result(timeout=5) == "done"

    # network.connect against a loopback-only listener
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted = threading.Event()

    def accept_once() -> None:
        connection, _address = listener.accept()
        connection.close()
        accepted.set()

    accept_thread = threading.Thread(target=accept_once)
    accept_thread.start()
    client = socket.socket()
    try:
        outside_lock("network.connect")
        client.connect(listener.getsockname())
        assert accepted.wait(5)
    finally:
        client.close()
        listener.close()
        accept_thread.join(timeout=5)

    # callback and independent lock.acquire
    def callback() -> None:
        outside_lock("callback")

    callback()
    boundary_lock = threading.Lock()
    outside_lock("lock.acquire")
    assert boundary_lock.acquire(timeout=1)
    boundary_lock.release()

    assert observed == [
        "file.read_text",
        "git.process.Popen",
        "process.communicate",
        "future.result",
        "network.connect",
        "callback",
        "lock.acquire",
    ]
