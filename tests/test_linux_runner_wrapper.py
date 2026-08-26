"""Executable contract tests for the Linux self-update wrapper."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux wrapper contract")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_wrapper(
    tmp_path: Path,
    exit_codes: list[int],
    *,
    hang_first: bool = False,
    node_channel_fd: str | None = None,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "exit-codes"
    state.write_text("\n".join(str(code) for code in exit_codes) + "\n")
    fetch_log = tmp_path / "fetches"
    launch_log = tmp_path / "launches"
    release_env_log = tmp_path / "release-env"
    source_script = Path(__file__).resolve().parents[1] / "haniel-runner.sh"
    source_scripts = Path(__file__).resolve().parents[1] / "scripts"
    script = tmp_path / "haniel-runner.sh"
    shutil.copy2(source_script, script)
    helper_dir = tmp_path / "scripts"
    helper_dir.mkdir()
    for name in (
        "haniel_atomic_release.py",
        "haniel_release_fs.py",
        "haniel_release_inventory.py",
        "haniel_release_policy.py",
        "haniel_release_prune.py",
        "haniel_release_steps.py",
    ):
        shutil.copy2(source_scripts / name, helper_dir / name)
    release_commit = "a" * 40

    _write_executable(
        fake_bin / "git",
        f"""#!/usr/bin/env bash
if [[ "$3" == "fetch" ]]; then printf 'fetch\\n' >> "{fetch_log}"; fi
if [[ "$3" == "rev-parse" ]]; then printf '{release_commit}\\n'; fi
if [[ "$3" == "symbolic-ref" ]]; then printf 'main\\n'; fi
exit 0
""",
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "python",
        f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == *"haniel_atomic_release.py" ]]; then
  printf '%s\n' "${{NODE_CHANNEL_FD:-unset}}" > "{release_env_log}"
  exec "{sys.executable}" "$@"
fi
if [[ "${{1:-}}" == "-" ]]; then exec "{sys.executable}" "$@"; fi
if [[ "${{1:-}}" == "-c" ]]; then
  if [[ "${{2:-}}" == *"haniel.cli"* ]]; then exit 0; fi
  printf '2026-08-10T00:00:00+00:00\\n'
  exit 0
fi
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "haniel.cli" ]]; then
  code="$(head -n 1 "{state}")"
  tail -n +2 "{state}" > "{state}.next"
  mv "{state}.next" "{state}"
  printf '%s|active-self-head=%s|%s\\n' "$code" "${{HANIEL_ACTIVE_SELF_HEAD:-unset}}" "$*" >> "{launch_log}"
  if [[ "{1 if hang_first else 0}" == "1" && "$code" == "99" ]]; then
    mkdir -p "{tmp_path}/.local"
    : > "{tmp_path}/.local/self_update_exit_requested"
    while true; do :; done
  fi
  exit "$code"
fi
exit 0
""",
    )

    release_root = tmp_path / ".local" / "haniel-releases"
    release = release_root / "releases" / release_commit
    (release / ".venv" / "bin").mkdir(parents=True)
    shutil.copy2(fake_bin / "python", release / ".venv" / "bin" / "python")
    shutil.copy2(script, release / "haniel-runner.sh")
    (release / ".haniel-release-ready.json").write_text(
        json.dumps({"version": 1, "commit": release_commit}), encoding="utf-8"
    )
    release_root.mkdir(parents=True, exist_ok=True)
    (release_root / "current.txt").write_text(
        f"{release_commit}\n",
        encoding="utf-8",
    )

    (tmp_path / "haniel-runner.conf").write_text(
        "\n".join(
            [
                "WEBHOOK_URL=",
                "HANIEL_REPO=repo",
                "HANIEL_RELEASE_ROOT=.local/haniel-releases",
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
    (tmp_path / "haniel.yaml").write_text("repos: {{}}\nservices: {{}}\n")

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    if node_channel_fd is not None:
        env["NODE_CHANNEL_FD"] = node_channel_fd
    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    fetches = fetch_log.read_text().splitlines() if fetch_log.exists() else []
    launches = launch_log.read_text().splitlines() if launch_log.exists() else []
    return result, fetches, launches


def test_release_prune_starts_after_haniel_and_before_wait() -> None:
    script = (Path(__file__).resolve().parents[1] / "haniel-runner.sh").read_text(
        encoding="utf-8"
    )

    launch = script.index('"$ACTIVE_PYTHON" -m haniel.cli run "$CONFIG_PATH" &')
    prune = script.index("start_release_prune", launch)
    wait = script.index('wait "$CHILD_PID"', prune)

    assert launch < prune < wait


def test_wrapper_removes_inherited_node_channel_before_release_preparation(
    tmp_path: Path,
) -> None:
    release_env_log = tmp_path / "release-env"
    result, fetches, launches = _run_wrapper(tmp_path, [0], node_channel_fd="3")

    assert result.returncode == 0, result.stderr
    assert release_env_log.read_text(encoding="utf-8") == "unset\n"
    assert fetches == ["fetch"]
    expected = (
        f"0|active-self-head={'a' * 40}|-m haniel.cli run {tmp_path / 'haniel.yaml'}"
    )
    assert launches == [expected]


@pytest.mark.parametrize(
    ("first_exit", "expected_message"),
    [
        (10, "Self-update requested. Looping"),
        (137, "Unexpected exit code 137. Recovering"),
        (1, "Unexpected exit code 1. Recovering"),
    ],
)
def test_exit_code_contract_recovers_and_relaunches(
    tmp_path: Path, first_exit: int, expected_message: str
) -> None:
    result, fetches, launches = _run_wrapper(tmp_path, [first_exit, 0])

    assert result.returncode == 0, result.stderr
    assert expected_message in result.stdout
    assert fetches == ["fetch", "fetch"]
    suffix = (
        f"|active-self-head={'a' * 40}|-m haniel.cli run {tmp_path / 'haniel.yaml'}"
    )
    assert launches == [f"{first_exit}{suffix}", f"0{suffix}"]


def test_self_update_exit_watchdog_sigkills_and_recovers(tmp_path: Path) -> None:
    result, fetches, launches = _run_wrapper(tmp_path, [99, 0], hang_first=True)

    assert result.returncode == 0, result.stderr
    assert "did not exit within 0s; sending SIGKILL" in result.stdout
    assert "Forced self-update recovery" in result.stdout
    assert fetches == ["fetch", "fetch"]
    suffix = (
        f"|active-self-head={'a' * 40}|-m haniel.cli run {tmp_path / 'haniel.yaml'}"
    )
    assert launches == [f"99{suffix}", f"0{suffix}"]
