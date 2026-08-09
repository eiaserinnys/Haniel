from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from haniel.config.model import RepoConfig, ServiceConfig


OPS_ROOT = Path(__file__).parents[1] / "ops" / "eiaserinnys" / "project-z"


def _write_fake_npm(bin_dir: Path) -> None:
    npm = bin_dir / "npm"
    npm.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
project_root=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--prefix" ]]; then
    project_root=$2
    shift 2
    continue
  fi
  if [[ "$1" == "run" && "${2:-}" == "build" ]]; then
    if [[ "${FAIL_BUILD:-0}" == "1" ]]; then
      exit 7
    fi
    mkdir -p "$project_root/dist/assets"
    printf '<main>new release</main>\\n' > "$project_root/dist/index.html"
    printf 'export const ready = true;\\n' > "$project_root/dist/assets/app.js"
  fi
  shift
done
""",
        encoding="utf-8",
    )
    npm.chmod(0o755)


def _git_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(
        ["git", "-C", path, "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", path, "config", "user.name", "Test"], check=True)
    (path / "source.txt").write_text("source\n", encoding="utf-8")
    subprocess.run(["git", "-C", path, "add", "source.txt"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-qm", "fixture"], check=True)
    return subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_hook(
    tmp_path: Path, *, fail_build: bool = False
) -> subprocess.CompletedProcess[str]:
    project_root = tmp_path / "project-z"
    publish_root = tmp_path / "publish"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    expected_sha = _git_repo(project_root)
    _write_fake_npm(fake_bin)

    old_release = publish_root / "releases" / "old"
    old_release.mkdir(parents=True)
    (old_release / "index.html").write_text("old release\n", encoding="utf-8")
    (old_release / ".release-sha").write_text("old\n", encoding="utf-8")
    publish_root.joinpath("current").symlink_to("releases/old")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PROJECT_Z_ROOT": str(project_root),
            "PROJECT_Z_PUBLISH_ROOT": str(publish_root),
            "PROJECT_Z_BUILD_LOCK": str(tmp_path / "build.lock"),
            "PROJECT_Z_MIN_AVAILABLE_MB": "0",
            "PROJECT_Z_BUILD_TIMEOUT_SECONDS": "30",
            "FAIL_BUILD": "1" if fail_build else "0",
        }
    )
    result = subprocess.run(
        ["bash", str(OPS_ROOT / "build-project-z.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    result.expected_sha = expected_sha  # type: ignore[attr-defined]
    result.publish_root = publish_root  # type: ignore[attr-defined]
    return result


def test_project_z_haniel_fragment_matches_runtime_schema() -> None:
    fragment = yaml.safe_load((OPS_ROOT / "haniel.yaml").read_text(encoding="utf-8"))

    repo = RepoConfig.model_validate(fragment["repos"]["project-z"])
    service = ServiceConfig.model_validate(fragment["services"]["project-z-static"])

    assert repo.auto_apply is True
    assert repo.path == "./services/project-z"
    assert service.repo == "project-z"
    assert service.hooks is not None
    assert (
        service.hooks.post_pull == "/home/eias/services/haniel/bin/build-project-z.sh"
    )


def test_project_z_nginx_contract_separates_assets_and_spa_fallback() -> None:
    config = (OPS_ROOT / "project-z.conf").read_text(encoding="utf-8")

    assert "location = /project-z" in config
    assert "location ^~ /project-z/assets/" in config
    assert 'Cache-Control "public, max-age=31536000, immutable"' in config
    assert "try_files $uri =404;" in config
    assert "try_files $uri $uri/ /project-z/index.html;" in config
    assert 'Cache-Control "no-cache"' in config


@pytest.mark.skipif(os.name == "nt", reason="production hook targets Linux")
def test_project_z_build_publishes_a_complete_release_atomically(
    tmp_path: Path,
) -> None:
    result = _run_hook(tmp_path)
    publish_root = result.publish_root  # type: ignore[attr-defined]

    assert result.returncode == 0, result.stderr
    assert publish_root.joinpath("current").is_symlink()
    assert (
        publish_root.joinpath("current", "index.html").read_text()
        == "<main>new release</main>\n"
    )
    assert (
        publish_root.joinpath("current", ".release-sha").read_text().strip()
        == result.expected_sha
    )  # type: ignore[attr-defined]
    assert (
        publish_root.joinpath("releases", "old", "index.html").read_text()
        == "old release\n"
    )
    assert list(publish_root.joinpath("releases").glob(".staging-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="production hook targets Linux")
def test_project_z_build_failure_preserves_current_release(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, fail_build=True)
    publish_root = result.publish_root  # type: ignore[attr-defined]

    assert result.returncode != 0
    assert os.readlink(publish_root / "current") == "releases/old"
    assert publish_root.joinpath("current", "index.html").read_text() == "old release\n"
    assert list(publish_root.joinpath("releases").glob(".staging-*")) == []
