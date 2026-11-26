"""The installer gives the install root to the user who ran it through sudo."""

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from haniel.config import HanielConfig
from haniel.installer.orchestrator import InstallOrchestrator
from haniel.installer.state import InstallPhase, InstallState
from haniel.installer.utils import hand_over_install_root

SERVICE_USER = SimpleNamespace(pw_uid=1234, pw_gid=5678)


@pytest.fixture
def install_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "haniel.yaml").write_text("x: 1\n")
    (root / "haniel-runner.conf").write_text("A=1\n")
    (root / "venv" / "bin" / "python").write_text("")
    (root / "outside-link").symlink_to(tmp_path)
    return root


def hand_over(root: Path, *, euid=0, sudo_user="alice", chown=None, platform="linux"):
    env = {"SUDO_USER": sudo_user} if sudo_user else {}
    chown = chown or MagicMock()
    with (
        patch.dict(os.environ, env, clear=False),
        patch("haniel.installer.utils.sys.platform", platform),
        patch("haniel.installer.utils.os.geteuid", return_value=euid),
        patch("pwd.getpwnam", return_value=SERVICE_USER) as getpwnam,
        patch("haniel.installer.utils.os.chown", chown),
    ):
        if not sudo_user:
            os.environ.pop("SUDO_USER", None)
        hand_over_install_root(root)
    return chown, getpwnam


def all_paths(root: Path) -> set[Path]:
    found = {root}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        found.update(Path(dirpath) / n for n in dirnames + filenames)
    return found


def test_root_with_sudo_user_hands_over_everything_without_following_links(
    install_root, caplog
):
    caplog.set_level(logging.INFO)
    chown, getpwnam = hand_over(install_root)

    getpwnam.assert_called_once_with("alice")
    changed = {call.args[0] for call in chown.call_args_list}
    assert changed == all_paths(install_root)
    for call in chown.call_args_list:
        assert call.args[1:] == (1234, 5678)
        assert call.kwargs == {"follow_symlinks": False}
    assert install_root / "outside-link" in changed
    link = install_root / "outside-link"
    assert not any(link in p.parents for p in changed)
    assert f"Handed over {install_root} to alice" in caplog.text


def test_non_root_changes_nothing(install_root):
    chown, _ = hand_over(install_root, euid=1000)
    chown.assert_not_called()


def test_missing_sudo_user_changes_nothing(install_root):
    chown, _ = hand_over(install_root, sudo_user=None)
    chown.assert_not_called()


def test_non_linux_changes_nothing(install_root):
    chown, _ = hand_over(install_root, platform="darwin")
    chown.assert_not_called()


def test_one_failing_path_warns_and_the_rest_are_handed_over(install_root, caplog):
    failing = install_root / "haniel.yaml"

    def chown(path, uid, gid, follow_symlinks=True):
        calls.append(Path(path))
        if Path(path) == failing:
            raise PermissionError("denied")

    calls: list[Path] = []
    hand_over(install_root, chown=chown)

    assert set(calls) == all_paths(install_root)
    assert str(failing) in caplog.text
    assert "denied" in caplog.text


def test_finalize_hands_over_after_state_is_saved_complete(tmp_path):
    order: list[str] = []
    state = InstallState(phase=InstallPhase.FINALIZE)
    orchestrator = InstallOrchestrator(HanielConfig(), tmp_path, state)
    finalizer = MagicMock()
    finalizer.check_all_configs_filled.return_value = True
    finalizer.register_service.side_effect = lambda: order.append("register")
    orchestrator._finalizer = finalizer
    real_save = orchestrator.save_state

    def save():
        order.append(f"save:{state.phase.name}")
        real_save()

    orchestrator.save_state = save
    with patch(
        "haniel.installer.orchestrator.hand_over_install_root",
        side_effect=lambda root: order.append(f"handover:{root}"),
    ):
        assert orchestrator.run_finalize_phase() is True

    assert order[-2:] == [f"save:{InstallPhase.COMPLETE.name}", f"handover:{tmp_path}"]
    assert order.index("register") < order.index(f"handover:{tmp_path}")
