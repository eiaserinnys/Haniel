"""The resident config lock is a memory-only snapshot/CAS boundary."""

from __future__ import annotations

import ast
import sys
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
