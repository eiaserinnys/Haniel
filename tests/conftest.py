"""Pytest configuration and fixtures for haniel tests."""

import pytest
from pathlib import Path
from click.testing import CliRunner


@pytest.fixture
def cli_runner():
    """Provide a Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Create a minimal haniel.yaml config file for testing."""
    config_content = """\
poll_interval: 60

repos: {}

services: {}
"""
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(config_content)
    return config_file


@pytest.fixture
def sample_config(tmp_path: Path) -> Path:
    """Create a sample haniel.yaml with services for testing."""
    config_content = """\
poll_interval: 30

repos:
  test-repo:
    url: git@github.com:example/test.git
    branch: main
    path: ./projects/test

services:
  test-service:
    run: python -m http.server 8080
    cwd: ./projects/test
    repo: test-repo
"""
    config_file = tmp_path / "haniel.yaml"
    config_file.write_text(config_content)
    return config_file
