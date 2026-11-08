"""Executable contract tests for the POSIX self-update wrapper."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX wrapper contract")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _conf_lines(base: Path, fake_bin: Path | None = None) -> list[str]:
    python_bin = (fake_bin or (base / "bin")) / "python"
    return [
        "WEBHOOK_URL=",
        "HANIEL_REPO=repo",
        "HANIEL_RELEASE_ROOT=.local/haniel-releases",
        "CONFIG=haniel.yaml",
        "MAX_GIT_FAILURES=1",
        "SELF_UPDATE_EXIT_TIMEOUT=0",
        "CRASH_RESTART_BASE_SECONDS=0",
        "CRASH_RESTART_MAX_SECONDS=0",
        "CRASH_RESET_SECONDS=300",
        f"PYTHON_BIN={python_bin}",
    ]


def _run_wrapper(
    tmp_path: Path,
    exit_codes: list[int],
    *,
    hang_first: bool = False,
    node_channel_fd: str | None = None,
    prestage_target: str | None = None,
    switch_to: str | None = None,
    invalid_helper_result: bool = False,
    conf_text: str | None = None,
    root_name: str | None = None,
    fake_bin_name: str = "bin",
    script_command: str | None = None,
    conf_argument: str | None = None,
    run_cwd: Path | None = None,
    fail_readlink: bool = False,
):
    base = tmp_path / root_name if root_name else tmp_path
    base.mkdir(parents=True, exist_ok=True)
    repo = base / "repo"
    repo.mkdir()
    fake_bin = base / fake_bin_name
    fake_bin.mkdir(parents=True)
    state = base / "exit-codes"
    state.write_text("\n".join(str(code) for code in exit_codes) + "\n")
    fetch_log = base / "fetches"
    launch_log = base / "launches"
    release_env_log = base / "release-env"
    reexec_log = base / "reexec-args"
    source_script = Path(__file__).resolve().parents[1] / "haniel-runner.sh"
    source_scripts = Path(__file__).resolve().parents[1] / "scripts"
    script = base / "haniel-runner.sh"
    shutil.copy2(source_script, script)
    helper_dir = base / "scripts"
    helper_dir.mkdir()
    for name in (
        "haniel_atomic_release.py",
        "haniel_dashboard_build.py",
        "haniel_release_fs.py",
        "haniel_release_inventory.py",
        "haniel_release_policy.py",
        "haniel_release_prune.py",
        "haniel_release_steps.py",
    ):
        shutil.copy2(source_scripts / name, helper_dir / name)
    release_commit = "a" * 40

    prestage_commit = prestage_target or ""
    switch_commit = switch_to or ""
    switch_release_root = (
        base / ".local" / "haniel-releases" / "releases" / switch_commit
        if switch_commit
        else None
    )
    if switch_release_root is not None:
        switch_release_root.mkdir(parents=True)
        _write_executable(
            switch_release_root / "haniel-runner.sh",
            f"""#!/usr/bin/env bash
printf '%s\\n' "$0" > "{reexec_log}"
printf '%s\\n' "${{1:-}}" >> "{reexec_log}"
exit 0
""",
        )
    _write_executable(
        fake_bin / "git",
        f"""#!/usr/bin/env bash
if [[ "$3" == "fetch" ]]; then printf 'fetch\\n' >> "{fetch_log}"; fi
if [[ "$3" == "rev-parse" ]]; then
  ref="${{@: -1}}"
  candidate="${{ref:0:40}}"
  if [[ "$candidate" =~ ^[0-9a-f]{{40}}$ ]]; then
    printf '%s\\n' "$candidate"
  else
    printf '{release_commit}\\n'
  fi
fi
if [[ "$3" == "symbolic-ref" ]]; then printf 'main\\n'; fi
exit 0
""",
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    if fail_readlink:
        _write_executable(fake_bin / "readlink", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(
        fake_bin / "python",
        f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == *"haniel_atomic_release.py" ]]; then
  printf '%s\\n' "${{NODE_CHANNEL_FD:-unset}}" > "{release_env_log}"
  if [[ -n "{switch_commit}" ]]; then
    mkdir -p "{switch_release_root}/.venv/bin"
    cp "$0" "{switch_release_root}/.venv/bin/python"
    printf '%s' '{{"version":1,"ok":true,"switched":true,"target_commit":"{switch_commit}","prepared_release":"{switch_release_root}","active_repo":"{switch_release_root}","active_python":"{switch_release_root}/.venv/bin/python","active_commit":"{switch_commit}","steps":[],"warnings":[],"error":null}}' > "{base}/.local/haniel_release_preparation.json"
    exit 0
  fi
  if [[ "{1 if invalid_helper_result else 0}" == "1" ]]; then
    mkdir -p "{base}/.local"
    printf '{{invalid' > "{base}/.local/haniel_release_preparation.json"
    exit 0
  fi
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
  if [[ "$code" == "10" && -n "{prestage_commit}" ]]; then
    prepared="{base}/.local/haniel-releases/releases/{prestage_commit[:12]}"
    mkdir -p "$prepared/.venv/bin" "{base}/.local"
    cp "$0" "$prepared/.venv/bin/python"
    printf '{{"version":1,"commit":"{prestage_commit}"}}' > "$prepared/.haniel-release-ready.json"
    printf '{{"version":1,"ok":true,"pre_staged":true,"switched":false,"target_commit":"{prestage_commit}","prepared_release":"%s","active_repo":"{base}/.local/haniel-releases/releases/{release_commit}","active_python":"{base}/.local/haniel-releases/releases/{release_commit}/.venv/bin/python","active_commit":"{release_commit}","steps":[],"warnings":[],"error":null}}' "$prepared" > "{base}/.local/haniel_release_preparation.json"
  fi
  if [[ "{1 if hang_first else 0}" == "1" && "$code" == "99" ]]; then
    mkdir -p "{base}/.local"
    : > "{base}/.local/self_update_exit_requested"
    while true; do :; done
  fi
  exit "$code"
fi
exit 0
""",
    )

    release_root = base / ".local" / "haniel-releases"
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

    if conf_text is None:
        conf_text = "\n".join(_conf_lines(base, fake_bin)) + "\n"
    (base / "haniel-runner.conf").write_text(conf_text, encoding="utf-8")
    (base / "haniel.yaml").write_text("repos: {{}}\nservices: {{}}\n")

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    if node_channel_fd is not None:
        env["NODE_CHANNEL_FD"] = node_channel_fd
    command = [
        "/bin/bash",
        script_command if script_command is not None else str(script),
    ]
    if conf_argument is not None:
        command.append(conf_argument)
    result = subprocess.run(
        command,
        cwd=run_cwd if run_cwd is not None else base,
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
    report_start = script.index("report_release_prune_result()")
    report_end = script.index("complete_release_prune()", report_start)
    assert "without a result" in script[report_start:report_end]


def test_wrapper_avoids_bash4_only_constructs() -> None:
    script = (Path(__file__).resolve().parents[1] / "haniel-runner.sh").read_text(
        encoding="utf-8"
    )

    assert "declare -A" not in script
    assert "mapfile" not in script
    assert "readlink -f" not in script


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


def test_wrapper_runs_from_relative_script_path(tmp_path: Path) -> None:
    result, fetches, launches = _run_wrapper(
        tmp_path, [0], script_command="haniel-runner.sh"
    )

    assert result.returncode == 0, result.stderr
    assert fetches == ["fetch"]
    assert len(launches) == 1


def test_conf_argument_from_other_directory_keeps_conf_root(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result, fetches, launches = _run_wrapper(
        tmp_path,
        [0],
        conf_argument=str(tmp_path / "haniel-runner.conf"),
        run_cwd=elsewhere,
    )

    assert result.returncode == 0, result.stderr
    assert fetches == ["fetch"]
    expected = (
        f"0|active-self-head={'a' * 40}|-m haniel.cli run {tmp_path / 'haniel.yaml'}"
    )
    assert launches == [expected]


def test_root_and_python_paths_with_spaces(tmp_path: Path) -> None:
    result, fetches, launches = _run_wrapper(
        tmp_path,
        [0],
        root_name="space root",
        fake_bin_name="bin with space",
        script_command="haniel-runner.sh",
    )

    base = tmp_path / "space root"
    assert result.returncode == 0, result.stderr
    assert fetches == ["fetch"]
    expected = f"0|active-self-head={'a' * 40}|-m haniel.cli run {base / 'haniel.yaml'}"
    assert launches == [expected]


def test_wrapper_does_not_depend_on_gnu_readlink(tmp_path: Path) -> None:
    result, _, launches = _run_wrapper(tmp_path, [0], fail_readlink=True)

    assert result.returncode == 0, result.stderr
    assert len(launches) == 1


def test_conf_allows_missing_trailing_newline(tmp_path: Path) -> None:
    lines = [line for line in _conf_lines(tmp_path) if not line.startswith("CONFIG=")]
    lines.append("CONFIG=haniel.yaml")
    conf = "\n".join(lines)
    result, fetches, launches = _run_wrapper(tmp_path, [0], conf_text=conf)

    assert result.returncode == 0, result.stderr
    assert fetches == ["fetch"]
    assert len(launches) == 1


def test_conf_crlf_and_duplicate_keys_use_last_value(tmp_path: Path) -> None:
    lines = ["CONFIG=missing.yaml"]
    lines.extend(_conf_lines(tmp_path))
    lines.append("CONFIG=haniel.yaml")
    conf = "\r\n".join(lines) + "\r\n"

    result, _, launches = _run_wrapper(tmp_path, [0], conf_text=conf)

    assert result.returncode == 0, result.stderr
    expected = (
        f"0|active-self-head={'a' * 40}|-m haniel.cli run {tmp_path / 'haniel.yaml'}"
    )
    assert launches == [expected]


def test_conf_empty_values_fall_back_to_defaults(tmp_path: Path) -> None:
    lines = _conf_lines(tmp_path)
    lines.insert(3, "HANIEL_RELEASE_RETAIN_EXTRA=")
    lines.insert(3, "HANIEL_RELEASE_MIN_FREE_MB=")
    lines.append("SELF_UPDATE_EXIT_TIMEOUT=")

    result, _, launches = _run_wrapper(tmp_path, [0], conf_text="\n".join(lines) + "\n")

    assert result.returncode == 0, result.stderr
    assert len(launches) == 1


def test_conf_values_are_data_not_shell_code(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    lines = _conf_lines(tmp_path)
    lines.append(f"CRASH_RESET_SECONDS=$(touch {marker})")

    result, _, launches = _run_wrapper(tmp_path, [0], conf_text="\n".join(lines) + "\n")

    assert result.returncode == 1
    assert "non-negative integers" in result.stderr
    assert not marker.exists()
    assert launches == []


def test_conf_value_keeps_equals_sign(tmp_path: Path) -> None:
    lines = _conf_lines(tmp_path)
    lines.append("WEBHOOK_URL=https://hooks.example.test/services/a=b")
    lines.append("CONFIG=haniel=yaml.yaml")

    result, _, launches = _run_wrapper(tmp_path, [0], conf_text="\n".join(lines) + "\n")

    assert result.returncode == 0, result.stderr
    expected = f"0|active-self-head={'a' * 40}|-m haniel.cli run {tmp_path / 'haniel=yaml.yaml'}"
    assert launches == [expected]


def test_wrapper_refuses_to_launch_without_valid_release(tmp_path: Path) -> None:
    result, _, launches = _run_wrapper(tmp_path, [0], invalid_helper_result=True)

    assert result.returncode == 1
    assert "No previously validated Haniel release is available" in result.stderr
    assert launches == []


def test_self_update_reexec_passes_absolute_conf_to_new_release(
    tmp_path: Path,
) -> None:
    target = "b" * 40
    result, _, _ = _run_wrapper(
        tmp_path,
        [0],
        switch_to=target,
        script_command="haniel-runner.sh",
    )

    assert result.returncode == 0, result.stderr
    release_runner = (
        tmp_path / ".local" / "haniel-releases" / "releases" / target
    ) / "haniel-runner.sh"
    reexec_args = (tmp_path / "reexec-args").read_text(encoding="utf-8").splitlines()
    assert reexec_args == [str(release_runner), str(tmp_path / "haniel-runner.conf")]


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


def test_restart_exit_skips_update_and_relaunches(tmp_path: Path) -> None:
    result, fetches, launches = _run_wrapper(tmp_path, [11, 0])

    assert result.returncode == 0, result.stderr
    assert "Restart requested. Skipping update..." in result.stdout
    assert fetches == ["fetch"]
    suffix = (
        f"|active-self-head={'a' * 40}|-m haniel.cli run {tmp_path / 'haniel.yaml'}"
    )
    assert launches == [f"11{suffix}", f"0{suffix}"]


def test_self_update_exit_reuses_prestaged_exact_target_without_fetch(
    tmp_path: Path,
) -> None:
    target = "b" * 40
    result, fetches, launches = _run_wrapper(
        tmp_path,
        [10, 0],
        prestage_target=target,
    )

    assert result.returncode == 0, result.stderr
    assert fetches == ["fetch"]
    assert "Reusing pre-staged release bbbbbbbbbbbb" in result.stdout
    config_path = tmp_path / "haniel.yaml"
    assert launches == [
        f"10|active-self-head={'a' * 40}|-m haniel.cli run {config_path}",
        f"0|active-self-head={target}|-m haniel.cli run {config_path}",
    ]


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
