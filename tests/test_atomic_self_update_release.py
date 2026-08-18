"""Executable A/B release contracts for the Linux self-update wrapper."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux shell harness")
REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_GIT = shutil.which("git")


@dataclass(frozen=True)
class AtomicRun:
    result: subprocess.CompletedProcess[str]
    source: Path
    release_root: Path
    previous_release: Path | None
    previous_commit: str
    target_commit: str
    launch_log: Path
    webhook_log: Path


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_command_error_preserves_process_return_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    atomic_release = importlib.import_module("haniel_atomic_release")
    completed = subprocess.CompletedProcess(
        args=["pnpm", "install"],
        returncode=-6,
        stdout="Done in 1.2s using pnpm v10.32.1\n",
        stderr="",
    )

    assert atomic_release._command_error(completed).splitlines()[-1] == "[exit=-6]"


def test_dashboard_uses_hoisted_node_linker() -> None:
    npmrc = (REPO_ROOT / "dashboard" / ".npmrc").read_text(encoding="utf-8")

    assert npmrc.splitlines() == ["node-linker=hoisted"]


def _copy_release_helpers(destination: Path) -> None:
    destination.mkdir(exist_ok=True)
    for name in (
        "haniel_atomic_release.py",
        "haniel_release_fs.py",
        "haniel_release_inventory.py",
        "haniel_release_policy.py",
    ):
        shutil.copy2(REPO_ROOT / "scripts" / name, destination / name)


def _copy_release_sources(destination: Path) -> None:
    shutil.copy2(REPO_ROOT / "haniel-runner.sh", destination / "haniel-runner.sh")
    _copy_release_helpers(destination / "scripts")
    dashboard = destination / "dashboard"
    dashboard.mkdir(exist_ok=True)
    (dashboard / "package.json").write_text(
        json.dumps({"name": "haniel-dashboard-test", "private": True}),
        encoding="utf-8",
    )


def _create_source_repo(tmp_path: Path) -> tuple[Path, str, str]:
    assert REAL_GIT is not None
    git_env = os.environ.copy()
    git_env.update(
        {
            "GIT_AUTHOR_NAME": "Haniel Test",
            "GIT_AUTHOR_EMAIL": "haniel-test@example.com",
            "GIT_COMMITTER_NAME": "Haniel Test",
            "GIT_COMMITTER_EMAIL": "haniel-test@example.com",
        }
    )
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    source = tmp_path / "repo"
    _run([REAL_GIT, "init", "--bare", str(origin)], cwd=tmp_path, env=git_env)
    _run([REAL_GIT, "init", "-b", "main", str(seed)], cwd=tmp_path, env=git_env)
    _copy_release_sources(seed)
    (seed / "version.txt").write_text("previous\n", encoding="utf-8")
    _run([REAL_GIT, "add", "."], cwd=seed, env=git_env)
    _run([REAL_GIT, "commit", "-m", "previous"], cwd=seed, env=git_env)
    _run([REAL_GIT, "remote", "add", "origin", str(origin)], cwd=seed, env=git_env)
    _run([REAL_GIT, "push", "-u", "origin", "main"], cwd=seed, env=git_env)
    _run([REAL_GIT, "symbolic-ref", "HEAD", "refs/heads/main"], cwd=origin, env=git_env)
    _run([REAL_GIT, "clone", str(origin), str(source)], cwd=tmp_path, env=git_env)
    previous_commit = _run([REAL_GIT, "rev-parse", "HEAD"], cwd=source, env=git_env)

    (seed / "version.txt").write_text("target\n", encoding="utf-8")
    _copy_release_sources(seed)
    _run([REAL_GIT, "add", "."], cwd=seed, env=git_env)
    _run([REAL_GIT, "commit", "-m", "target"], cwd=seed, env=git_env)
    _run([REAL_GIT, "push", "origin", "main"], cwd=seed, env=git_env)
    target_commit = _run([REAL_GIT, "rev-parse", "HEAD"], cwd=seed, env=git_env)
    return source, previous_commit, target_commit


def _make_fake_commands(
    tmp_path: Path,
    source: Path,
    fetch_log: Path,
    launch_log: Path,
    webhook_log: Path,
    state: Path,
) -> Path:
    assert REAL_GIT is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_python = Path(sys.executable)

    _write_executable(
        fake_bin / "git",
        f"""#!/usr/bin/env bash
if [[ "$*" == *"-C {source} fetch origin"* ]]; then
  printf 'fetch\\n' >> "{fetch_log}"
  if [[ "$(wc -l < "{fetch_log}")" -eq 1 ]]; then exit 0; fi
fi
exec "{REAL_GIT}" "$@"
""",
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "curl",
        f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "{webhook_log}"\n',
    )
    _write_executable(
        fake_bin / "python",
        f"""#!/usr/bin/env bash
set -u
fetch_count=0
if [[ -f "{fetch_log}" ]]; then fetch_count="$(wc -l < "{fetch_log}")"; fi
if [[ "${{1:-}}" == *"haniel_atomic_release.py" ]]; then exec "{real_python}" "$@"; fi
if [[ "${{1:-}}" == "-" ]]; then exec "{real_python}" "$@"; fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
  exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "pip" ]]; then
  if [[ "${{HANIEL_TEST_FAIL_STAGE:-}}" == "baseline_install" && "$fetch_count" -eq 0 ]]; then
    printf 'injected baseline install failure\n' >&2
    exit 40
  fi
  if [[ "${{HANIEL_TEST_FAIL_STAGE:-}}" == "install" && "$fetch_count" -ge 2 ]]; then
    printf 'injected install failure\\n' >&2
    exit 41
  fi
  exit 0
fi
if [[ "${{1:-}}" == "-c" && "${{2:-}}" == *"haniel.cli"* ]]; then
  if [[ "${{HANIEL_TEST_FAIL_STAGE:-}}" == "import" && "$fetch_count" -ge 2 ]]; then
    printf 'injected import failure\\n' >&2
    exit 42
  fi
  exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "haniel.cli" ]]; then
  code="$(head -n 1 "{state}")"
  tail -n +2 "{state}" > "{state}.next"
  mv "{state}.next" "{state}"
  printf '%s|%s|active-self-head=%s|%s\\n' "$(readlink -f "$0")" "$code" "${{HANIEL_ACTIVE_SELF_HEAD:-unset}}" "$*" >> "{launch_log}"
  exit "$code"
fi
exec "{real_python}" "$@"
""",
    )
    _write_executable(
        fake_bin / "pnpm",
        f"""#!/usr/bin/env bash
fetch_count=0
if [[ -f "{fetch_log}" ]]; then fetch_count="$(wc -l < "{fetch_log}")"; fi
if [[ "$*" == *" build"* && "${{HANIEL_TEST_FAIL_STAGE:-}}" == "ui_build" && "$fetch_count" -ge 2 ]]; then
  printf 'injected UI build failure\\n' >&2
  exit 43
fi
exit 0
""",
    )
    return fake_bin


def _prepare_previous_release(
    release_root: Path, previous_commit: str, fake_python: Path
) -> Path:
    previous = release_root / "releases" / previous_commit
    (previous / ".venv" / "bin").mkdir(parents=True)
    shutil.copy2(fake_python, previous / ".venv" / "bin" / "python")
    shutil.copy2(REPO_ROOT / "haniel-runner.sh", previous / "haniel-runner.sh")
    _copy_release_helpers(previous / "scripts")
    (previous / ".haniel-release-ready.json").write_text(
        json.dumps({"version": 1, "commit": previous_commit}), encoding="utf-8"
    )
    release_root.mkdir(parents=True, exist_ok=True)
    (release_root / "current").symlink_to(
        Path("releases") / previous_commit, target_is_directory=True
    )
    return previous


def _prepare_ready_release(
    release_root: Path,
    commit: str,
    *,
    marker_commit: str | None = None,
    modified_ns: int,
) -> Path:
    release = release_root / "releases" / commit
    release.mkdir(parents=True)
    marker = release / ".haniel-release-ready.json"
    marker.write_text(
        json.dumps({"version": 1, "commit": marker_commit or commit}),
        encoding="utf-8",
    )
    os.utime(marker, ns=(modified_ns, modified_ns))
    return release


def _current_release(release_root: Path) -> Path:
    name = (release_root / "current.txt").read_text(encoding="utf-8").strip()
    return release_root / "releases" / name


def _run_atomic_wrapper(
    tmp_path: Path,
    *,
    fail_stage: str | None,
    legacy_layout: bool = False,
    min_free_mb: int = 1,
    retain_extra: int = 3,
    old_release_count: int = 0,
) -> AtomicRun:
    source, previous_commit, target_commit = _create_source_repo(tmp_path)
    fetch_log = tmp_path / "fetches"
    launch_log = tmp_path / "launches"
    webhook_log = tmp_path / "webhooks"
    state = tmp_path / "exit-codes"
    state.write_text("10\n0\n", encoding="utf-8")
    fake_bin = _make_fake_commands(
        tmp_path,
        source,
        fetch_log,
        launch_log,
        webhook_log,
        state,
    )
    release_root = tmp_path / ".local" / "haniel-releases"
    previous = None
    if not legacy_layout:
        previous = _prepare_previous_release(
            release_root, previous_commit, fake_bin / "python"
        )
    for index in range(old_release_count):
        _prepare_ready_release(
            release_root,
            f"{index + 1:040x}",
            modified_ns=(index + 1) * 1_000_000_000,
        )

    runner = tmp_path / "haniel-runner.sh"
    shutil.copy2(REPO_ROOT / "haniel-runner.sh", runner)
    _copy_release_helpers(tmp_path / "scripts")
    (tmp_path / "haniel.yaml").write_text("repos: {}\nservices: {}\n", encoding="utf-8")
    (tmp_path / "haniel-runner.conf").write_text(
        "\n".join(
            [
                "WEBHOOK_URL=https://hooks.example.invalid/test",
                "HANIEL_REPO=repo",
                "HANIEL_RELEASE_ROOT=.local/haniel-releases",
                f"HANIEL_RELEASE_RETAIN_EXTRA={retain_extra}",
                f"HANIEL_RELEASE_MIN_FREE_MB={min_free_mb}",
                "CONFIG=haniel.yaml",
                "MAX_GIT_FAILURES=1",
                "SELF_UPDATE_EXIT_TIMEOUT=0",
                "CRASH_RESTART_BASE_SECONDS=0",
                "CRASH_RESTART_MAX_SECONDS=0",
                "CRASH_RESET_SECONDS=300",
                f"PYTHON_BIN={fake_bin / 'python'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    if fail_stage:
        env["HANIEL_TEST_FAIL_STAGE"] = fail_stage
    result = subprocess.run(
        ["bash", str(runner)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return AtomicRun(
        result=result,
        source=source,
        release_root=release_root,
        previous_release=previous,
        previous_commit=previous_commit,
        target_commit=target_commit,
        launch_log=launch_log,
        webhook_log=webhook_log,
    )


@pytest.mark.parametrize(
    ("stage", "failed_step"),
    [
        ("install", "pip_install"),
        ("import", "import_smoke"),
        ("ui_build", "pnpm_build"),
    ],
)
def test_failed_candidate_preserves_previous_current(
    tmp_path: Path, stage: str, failed_step: str
) -> None:
    run = _run_atomic_wrapper(tmp_path, fail_stage=stage)
    marker = json.loads(
        (tmp_path / ".local" / "self_update_result.json").read_text(encoding="utf-8")
    )
    current = _current_release(run.release_root)
    source_head = _run([REAL_GIT, "rev-parse", "HEAD"], cwd=run.source)
    print(
        f"FAILURE_INJECTION stage={stage} current={current.name} "
        f"marker_ok={marker['ok']} error={marker['error']}"
    )

    assert run.result.returncode == 0, run.result.stderr
    assert current == run.previous_release
    assert source_head == run.previous_commit
    assert marker["ok"] is False
    assert any(
        step["name"] == failed_step and not step["ok"] for step in marker["steps"]
    )
    assert (
        run.launch_log.read_text(encoding="utf-8")
        .splitlines()[-1]
        .startswith(str(run.previous_release / ".venv" / "bin" / "python"))
    )


def test_success_switches_current_and_reexecs_release_wrapper(tmp_path: Path) -> None:
    run = _run_atomic_wrapper(tmp_path, fail_stage=None)
    marker = json.loads(
        (tmp_path / ".local" / "self_update_result.json").read_text(encoding="utf-8")
    )
    current = _current_release(run.release_root)
    source_head = _run([REAL_GIT, "rev-parse", "HEAD"], cwd=run.source)
    print(
        f"SUCCESS_SWITCH current={current.name} marker_ok={marker['ok']} "
        f"launch={run.launch_log.read_text(encoding='utf-8').splitlines()[-1]}"
    )

    assert run.result.returncode == 0, run.result.stderr
    assert current.name == run.target_commit[:12]
    assert source_head == run.previous_commit
    assert marker["ok"] is True
    preparation = json.loads(
        (tmp_path / ".local" / "haniel_release_preparation.json").read_text(
            encoding="utf-8"
        )
    )
    assert preparation["active_commit"] == run.target_commit
    assert "Re-executing current release wrapper" in run.result.stdout
    launch = run.launch_log.read_text(encoding="utf-8").splitlines()[-1]
    assert launch.startswith(str(current / ".venv" / "bin" / "python"))
    assert "--active-self-head" not in launch
    assert f"active-self-head={run.target_commit}" in launch


def test_new_wrapper_migrates_legacy_layout_before_target_switch(
    tmp_path: Path,
) -> None:
    run = _run_atomic_wrapper(tmp_path, fail_stage=None, legacy_layout=True)
    current = _current_release(run.release_root)
    source_head = _run([REAL_GIT, "rev-parse", "HEAD"], cwd=run.source)
    print(
        f"LEGACY_MIGRATION baseline={run.previous_commit[:7]} "
        f"current={current.name} source={source_head[:7]}"
    )

    assert run.result.returncode == 0, run.result.stderr
    assert "Migrated legacy checkout into release baseline" in run.result.stdout
    assert current.name == run.target_commit[:12]
    assert source_head == run.previous_commit


def test_legacy_migration_without_valid_baseline_fails_closed(tmp_path: Path) -> None:
    run = _run_atomic_wrapper(
        tmp_path,
        fail_stage="baseline_install",
        legacy_layout=True,
    )
    preparation = json.loads(
        (tmp_path / ".local" / "haniel_release_preparation.json").read_text(
            encoding="utf-8"
        )
    )
    current = run.release_root / "current.txt"
    print(
        f"LEGACY_BASELINE_FAILURE current_exists={current.exists()} "
        f"launched={run.launch_log.exists()} error={preparation['error']}"
    )

    assert run.result.returncode == 1
    assert not current.exists()
    assert not run.launch_log.exists()
    assert preparation["active_repo"] is None
    assert preparation["active_python"] is None
    assert "refusing to launch" in run.result.stderr


def test_prune_after_switch_keeps_current_previous_and_three_newest_extras(
    tmp_path: Path,
) -> None:
    run = _run_atomic_wrapper(
        tmp_path,
        fail_stage=None,
        retain_extra=3,
        old_release_count=5,
    )
    releases = run.release_root / "releases"
    current = _current_release(run.release_root)
    retained = sorted(path.name for path in releases.iterdir() if path.is_dir())
    print(
        f"RETENTION current={current.name} previous={run.previous_commit} "
        f"ready_count={len(retained)}"
    )

    assert run.result.returncode == 0, run.result.stderr
    assert current.name == run.target_commit[:12]
    assert run.previous_release is not None
    assert run.previous_release.exists()
    assert not (releases / f"{1:040x}").exists()
    assert not (releases / f"{2:040x}").exists()
    assert (releases / f"{3:040x}").exists()
    assert (releases / f"{4:040x}").exists()
    assert (releases / f"{5:040x}").exists()


def test_failed_switch_does_not_prune_before_activation(tmp_path: Path) -> None:
    run = _run_atomic_wrapper(
        tmp_path,
        fail_stage="install",
        retain_extra=0,
        old_release_count=2,
    )
    releases = run.release_root / "releases"

    assert run.result.returncode == 0, run.result.stderr
    assert (releases / f"{1:040x}").exists()
    assert (releases / f"{2:040x}").exists()


def test_prune_ignores_symlink_and_mismatched_ready_marker(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    release_root = run_root / ".local" / "haniel-releases"
    releases = release_root / "releases"
    releases.mkdir(parents=True)
    symlink_commit = "a" * 40
    mismatched_commit = "b" * 40
    valid_commit = "c" * 40
    (releases / symlink_commit).symlink_to(external, target_is_directory=True)
    _prepare_ready_release(
        release_root,
        mismatched_commit,
        marker_commit="d" * 40,
        modified_ns=1_000_000_000,
    )
    _prepare_ready_release(
        release_root,
        valid_commit,
        modified_ns=2_000_000_000,
    )

    run = _run_atomic_wrapper(
        run_root,
        fail_stage=None,
        retain_extra=0,
    )

    assert run.result.returncode == 0, run.result.stderr
    assert (releases / symlink_commit).is_symlink()
    assert (releases / mismatched_commit).exists()
    assert not (releases / valid_commit).exists()
    assert external.exists()


def test_low_disk_skips_candidate_and_launches_previous_release(tmp_path: Path) -> None:
    run = _run_atomic_wrapper(
        tmp_path,
        fail_stage=None,
        min_free_mb=1_000_000_000_000,
    )
    preparation = json.loads(
        (tmp_path / ".local" / "haniel_release_preparation.json").read_text(
            encoding="utf-8"
        )
    )
    current = _current_release(run.release_root)
    print(
        f"LOW_DISK current={current.name} target_exists="
        f"{(run.release_root / 'releases' / run.target_commit[:12]).exists()} "
        f"error={preparation['error']}"
    )

    assert run.result.returncode == 0, run.result.stderr
    assert current == run.previous_release
    assert not (run.release_root / "releases" / run.target_commit[:12]).exists()
    assert preparation["error_code"] == "insufficient_disk_space"
    assert (
        run.launch_log.read_text(encoding="utf-8")
        .splitlines()[-1]
        .startswith(str(run.previous_release / ".venv" / "bin" / "python"))
    )
    webhook = run.webhook_log.read_text(encoding="utf-8").lower()
    assert "insufficient" in webhook
    assert ":warning:" in webhook
