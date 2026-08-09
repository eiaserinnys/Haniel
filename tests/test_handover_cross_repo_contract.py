"""Platform path and pinned Soulstream consumer contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from importlib.resources import files
from pathlib import Path

import pytest

from haniel.core.handover_config import canonical_path_text, handover_config_digest


def _validate_pinned_soulstream_source(fixture: dict[str, object]) -> None:
    source = fixture["source"]
    assert isinstance(source, dict)
    commit = source["commit"]
    assert isinstance(commit, str) and len(commit) == 40
    extract_root_name = source["extract_root"]
    assert extract_root_name == f"soulstream-{commit[:8]}"
    extract_root = (Path(__file__).parent / "fixtures" / extract_root_name).resolve()
    manifests = source["manifests"]
    assert isinstance(manifests, list) and manifests
    observed_paths: set[str] = set()
    for evidence in manifests:
        assert isinstance(evidence, dict)
        relative_path = evidence["path"]
        assert isinstance(relative_path, str)
        assert relative_path not in observed_paths
        observed_paths.add(relative_path)
        source_path = (extract_root / relative_path).resolve()
        source_path.relative_to(extract_root)
        raw = source_path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == evidence["sha256"]
        document = json.loads(raw)
        assert document["schema_version"] == "haniel.release.v1"
        assert document["environment_service"] == evidence["environment_service"]


def test_canonical_path_text_matches_platform_identity(tmp_path: Path) -> None:
    path = tmp_path / "nested" / ".." / "Service.env"

    assert canonical_path_text(path) == os.path.normcase(
        str(path.expanduser().resolve(strict=False))
    )


def test_digest_canonicalizes_relative_and_absolute_env_paths(tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=sqlite:///isolated\n", encoding="utf-8")
    config = tmp_path / "haniel.yaml"

    def write_config(env_path: str) -> None:
        config.write_text(
            "\n".join(
                [
                    "repos:",
                    "  app:",
                    "    url: https://example.invalid/app.git",
                    "    path: ./repo",
                    "services:",
                    "  app:",
                    "    run: node app.js",
                    "    repo: app",
                    f"    release_env_file: {json.dumps(env_path)}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    write_config("service.env")
    relative_digest = handover_config_digest(config)
    write_config(str(env_file.resolve()))

    assert handover_config_digest(config) == relative_digest


def test_pinned_soulstream_fixture_consumes_packaged_haniel_contract() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "soulstream-config-environment-consumer.v1.json"
        ).read_text(encoding="utf-8")
    )
    contract = json.loads(
        files("haniel")
        .joinpath("contracts/manifest-handover-config-environment.v1.json")
        .read_text(encoding="utf-8")
    )
    required = fixture["required_haniel_contract"]
    environment = contract["service_environment"]

    _validate_pinned_soulstream_source(fixture)
    assert fixture["source"]["commit"] == ("1ada848e6b8d1efdcf2d7f50414361eb6cc510b5")
    assert fixture["planned_manifest_overlay"]["requires_service_env_file"] is True
    assert contract["schema_version"] == required["schema_version"]
    for field in (
        "service_config_field",
        "manifest_required_field",
        "child_path_env",
        "child_digest_env",
        "runtime_consumes_declared_file",
        "request_snapshot_used_through_recovery",
    ):
        assert environment[field] == required[field]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("path", "deploy/not-the-pinned-manifest.json"),
        ("sha256", "0" * 64),
        ("environment_service", "not-the-pinned-service"),
    ],
)
def test_pinned_soulstream_manifest_evidence_fails_closed_when_tampered(
    field: str, replacement: str
) -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "soulstream-config-environment-consumer.v1.json"
        ).read_text(encoding="utf-8")
    )
    tampered = copy.deepcopy(fixture)
    tampered["source"]["manifests"][0][field] = replacement

    with pytest.raises((AssertionError, FileNotFoundError)):
        _validate_pinned_soulstream_source(tampered)
