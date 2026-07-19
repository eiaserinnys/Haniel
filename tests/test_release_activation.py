"""Release manifest activation is atomic and happens before a repository pull."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from haniel.config import HanielConfig, RepoConfig, ServiceConfig, load_config
from haniel.config.release_activation import (
    DEFAULT_RELEASE_MANIFEST,
    ReleaseManifestActivationRequired,
    activate_release_manifest,
    plan_release_manifest_activation,
)


def write_config(path: Path) -> None:
    config = HanielConfig(
        repos={
            "soulstream": RepoConfig(
                url="git@github.com:eiaserinnys/soulstream.git",
                path="./services/soulstream",
                auto_apply=False,
            )
        },
        services={
            "soulstream-orch-server": ServiceConfig(
                run="node orch.js", repo="soulstream", cwd="./services/soulstream"
            ),
            "soulstream-soul-server-ts": ServiceConfig(
                run="node soul.js",
                repo="soulstream",
                cwd="./services/soulstream",
                after=["soulstream-orch-server"],
            ),
        },
    )
    path.write_text(
        yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True), sort_keys=False
        ),
        encoding="utf-8",
    )


def test_plan_is_read_only_and_reports_compare_and_swap_hash(tmp_path: Path) -> None:
    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    before = config_path.read_bytes()

    plan = plan_release_manifest_activation(
        config_path, "soulstream", DEFAULT_RELEASE_MANIFEST
    )

    assert plan.changed is True
    assert plan.config_sha256 == hashlib.sha256(before).hexdigest()
    assert plan.release_manifest == DEFAULT_RELEASE_MANIFEST
    assert config_path.read_bytes() == before


def test_apply_updates_only_configured_repo_and_preserves_backup(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    before = config_path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()

    result = activate_release_manifest(
        config_path,
        "soulstream",
        DEFAULT_RELEASE_MANIFEST,
        expected_sha256=digest,
    )

    assert result.changed is True
    assert load_config(config_path).repos["soulstream"].release_manifest == (
        DEFAULT_RELEASE_MANIFEST
    )
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == before


def test_apply_rejects_stale_config_hash_without_writing(tmp_path: Path) -> None:
    config_path = tmp_path / "haniel.yaml"
    write_config(config_path)
    before = config_path.read_bytes()

    with pytest.raises(ReleaseManifestActivationRequired, match="changed"):
        activate_release_manifest(
            config_path,
            "soulstream",
            DEFAULT_RELEASE_MANIFEST,
            expected_sha256="0" * 64,
        )

    assert config_path.read_bytes() == before
