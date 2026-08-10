"""Secret-free readiness migration audit contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_readiness_config.py"
WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "test.yml"


def test_audit_names_service_and_error_without_config_values(tmp_path: Path) -> None:
    secret = "TOKEN-never-print-this"
    config = tmp_path / "haniel.yaml"
    config.write_text(
        f"services:\n  missing-ready:\n    run: python app.py --token {secret}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(config)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout.strip() == ("services.missing-ready.ready: READINESS_REQUIRED")
    assert secret not in result.stdout + result.stderr
    assert "python app.py" not in result.stdout + result.stderr


def test_audit_accepts_explicit_valid_readiness(tmp_path: Path) -> None:
    config = tmp_path / "haniel.yaml"
    config.write_text(
        "services:\n  app:\n    run: python app.py\n    ready: delay:0.01\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(config)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_audit_rejects_empty_service_inventory(tmp_path: Path) -> None:
    config = tmp_path / "empty.yaml"
    config.write_text("repos: {}\nservices: {}\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(config)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout.strip() == "services: READINESS_AUDIT_EMPTY"


def test_audit_accepts_multiple_config_paths(tmp_path: Path) -> None:
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text(
        "services:\n  first:\n    run: python first.py\n",
        encoding="utf-8",
    )
    second.write_text(
        "services:\n  second:\n    run: python second.py\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(first), str(second)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"{first}: services.first.ready: READINESS_REQUIRED",
        f"{second}: services.second.ready: READINESS_REQUIRED",
    ]


def test_ci_runs_repository_readiness_audit() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in workflow
    assert (
        "python scripts/audit_readiness_config.py "
        "tests/fixtures/readiness_ci_config.yaml"
    ) in workflow
    fixture = WORKFLOW.parents[2] / "tests" / "fixtures" / "readiness_ci_config.yaml"
    assert "services:" in fixture.read_text(encoding="utf-8")
