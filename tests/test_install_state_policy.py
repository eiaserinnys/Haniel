"""Non-interactive install-state policy and immutable handover provenance."""

import json
from pathlib import Path

import pytest

from haniel.commands.install import InstallStatePolicyError, select_install_state
from haniel.installer.state import InstallPhase, InstallState


def test_noninteractive_existing_state_requires_explicit_policy_without_prompt(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "install.state"
    InstallState(phase=InstallPhase.MECHANICAL).save(state_file)
    prompts: list[str] = []

    with pytest.raises(InstallStatePolicyError) as exc_info:
        select_install_state(
            state_file=state_file,
            config_path=tmp_path / "haniel.yaml",
            state_policy=None,
            resume_alias=False,
            noninteractive=True,
            prompt=lambda message: prompts.append(message) or False,
        )

    assert exc_info.value.code == "STATE_POLICY_REQUIRED"
    assert prompts == []


def test_resume_requires_existing_state(tmp_path: Path) -> None:
    with pytest.raises(InstallStatePolicyError) as exc_info:
        select_install_state(
            state_file=tmp_path / "install.state",
            config_path=tmp_path / "haniel.yaml",
            state_policy="resume",
            resume_alias=False,
            noninteractive=True,
            prompt=lambda _message: False,
        )

    assert exc_info.value.code == "STATE_NOT_FOUND"


@pytest.mark.parametrize(
    "payload",
    ["{not-json", json.dumps({"phase": "unknown-phase"})],
    ids=["malformed-json", "schema-invalid"],
)
def test_resume_rejects_invalid_state_instead_of_creating_fresh_identity(
    tmp_path: Path,
    payload: str,
) -> None:
    state_file = tmp_path / "install.state"
    state_file.write_text(payload, encoding="utf-8")
    original = state_file.read_bytes()

    with pytest.raises(InstallStatePolicyError) as exc_info:
        select_install_state(
            state_file=state_file,
            config_path=tmp_path / "haniel.yaml",
            state_policy="resume",
            resume_alias=False,
            noninteractive=True,
            prompt=lambda _message: False,
        )

    assert exc_info.value.code == "INSTALL_STATE_INVALID"
    assert state_file.read_bytes() == original
    assert list(tmp_path.glob("install.state.*.bak")) == []


def test_start_fresh_archives_state_and_binds_handover_provenance(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "install.state"
    InstallState(phase=InstallPhase.INTERACTIVE).save(state_file)

    state = select_install_state(
        state_file=state_file,
        config_path=tmp_path / "haniel.yaml",
        state_policy="start-fresh",
        resume_alias=False,
        noninteractive=True,
        prompt=lambda _message: False,
        defer_manifest_handover=True,
        provenance="existing",
        expected_operation="upgrade",
    )

    assert state.phase == InstallPhase.NOT_STARTED
    assert state.deferred_manifest_handover is True
    assert state.install_provenance == "existing"
    assert state.expected_operation == "upgrade"
    assert list(tmp_path.glob("install.state.*.bak"))


def test_resume_rejects_immutable_handover_drift(tmp_path: Path) -> None:
    state_file = tmp_path / "install.state"
    original = InstallState()
    original.bind_handover(
        config_path=tmp_path / "haniel.yaml",
        deferred=True,
        provenance="initial",
        expected_operation="fresh_install",
    )
    original.save(state_file)

    with pytest.raises(InstallStatePolicyError) as exc_info:
        select_install_state(
            state_file=state_file,
            config_path=tmp_path / "haniel.yaml",
            state_policy="resume",
            resume_alias=False,
            noninteractive=True,
            prompt=lambda _message: False,
            defer_manifest_handover=True,
            provenance="existing",
            expected_operation="upgrade",
        )

    assert exc_info.value.code == "INSTALL_STATE_IDENTITY_MISMATCH"


def test_resume_alias_conflicts_with_start_fresh(tmp_path: Path) -> None:
    with pytest.raises(InstallStatePolicyError) as exc_info:
        select_install_state(
            state_file=tmp_path / "install.state",
            config_path=tmp_path / "haniel.yaml",
            state_policy="start-fresh",
            resume_alias=True,
            noninteractive=True,
            prompt=lambda _message: False,
        )

    assert exc_info.value.code == "STATE_POLICY_CONFLICT"


def test_invalid_deferred_contract_does_not_archive_existing_state(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "install.state"
    original = InstallState()
    original.save(state_file)
    before = state_file.read_bytes()

    with pytest.raises(
        InstallStatePolicyError, match="INSTALL_STATE_IDENTITY_MISMATCH"
    ):
        select_install_state(
            state_file=state_file,
            config_path=tmp_path / "haniel.yaml",
            state_policy="start-fresh",
            resume_alias=False,
            noninteractive=True,
            prompt=lambda _message: True,
            defer_manifest_handover=True,
            provenance=None,
            expected_operation=None,
        )

    assert state_file.read_bytes() == before
    assert list(tmp_path.glob("install.state.*.bak")) == []
