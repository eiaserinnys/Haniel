"""Config files that can carry secrets are written owner-only on POSIX."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest
import yaml

from haniel.config import (
    ConfigFileConfig,
    ConfigKeyConfig,
    HanielConfig,
    InstallConfig,
    RepoConfig,
    ServiceConfig,
)
from haniel.config.release_activation import (
    DEFAULT_RELEASE_MANIFEST,
    activate_release_manifest,
)
from haniel.installer.state import InstallState

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file modes are not enforced on Windows"
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _write_public(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o644)


def _release_config(repo_path: str = "./services/soulstream") -> HanielConfig:
    return HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@github.com:eiaserinnys/soulstream.git",
                path=repo_path,
                auto_apply=False,
            )
        },
        services={
            "soulstream-orch-server": ServiceConfig(
                run="node orch.js", repo="soulstream", cwd=repo_path
            ),
        },
    )


def test_write_config_rewrites_existing_file_owner_only(tmp_path: Path) -> None:
    from haniel.config.io import write_config

    path = tmp_path / "haniel.yaml"
    _write_public(path, "repos: {}\nservices: {}\n")

    write_config(path, HanielConfig(repos={}, services={}))

    assert _mode(path) == 0o600


def test_backup_config_creates_owner_only_sidecar(tmp_path: Path) -> None:
    from haniel.config.io import backup_config

    path = tmp_path / "haniel.yaml"
    _write_public(path, "repos: {}\nservices: {}\n")

    bak_path = backup_config(path)

    assert bak_path == path.with_suffix(".yaml.bak")
    assert _mode(bak_path) == 0o600
    assert bak_path.read_bytes() == path.read_bytes()


def _finalizer_config() -> HanielConfig:
    return HanielConfig(
        install=InstallConfig(
            configs={
                "workspace-env": ConfigFileConfig(
                    path="./workspace/.env",
                    keys=[ConfigKeyConfig(key="SLACK_TOKEN", prompt="Token")],
                ),
                "workspace-json": ConfigFileConfig(
                    path="./workspace/creds.json",
                    keys=[ConfigKeyConfig(key="TOKEN", prompt="Token")],
                ),
            }
        )
    )


def test_write_env_file_rewrites_existing_file_owner_only(tmp_path: Path) -> None:
    from haniel.installer.finalize import Finalizer

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_path = workspace / ".env"
    _write_public(env_path, "OLD=1\n")

    state = InstallState()
    state.config_values["workspace-env"] = {"SLACK_TOKEN": "xoxb-secret"}
    Finalizer(_finalizer_config(), tmp_path, state).generate_config_files()

    assert _mode(env_path) == 0o600
    assert "SLACK_TOKEN=xoxb-secret" in env_path.read_text(encoding="utf-8")


def test_write_json_file_rewrites_existing_file_owner_only(tmp_path: Path) -> None:
    from haniel.installer.finalize import Finalizer

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    json_path = workspace / "creds.json"
    _write_public(json_path, '{"OLD": "1"}\n')

    state = InstallState()
    state.config_values["workspace-json"] = {"TOKEN": "secret-value"}
    Finalizer(_finalizer_config(), tmp_path, state).generate_config_files()

    assert _mode(json_path) == 0o600
    assert '"TOKEN": "secret-value"' in json_path.read_text(encoding="utf-8")


def test_activation_rewrites_existing_config_owner_only(tmp_path: Path) -> None:
    config_path = tmp_path / "haniel.yaml"
    _write_public(
        config_path,
        yaml.safe_dump(
            _release_config().model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        ),
    )
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()

    result = activate_release_manifest(
        config_path,
        "soulstream",
        DEFAULT_RELEASE_MANIFEST,
        expected_sha256=digest,
    )

    assert result.changed is True
    assert result.backup_path is not None
    assert _mode(config_path) == 0o600
    assert _mode(result.backup_path) == 0o600
