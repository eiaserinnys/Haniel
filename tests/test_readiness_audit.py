"""Secret-free readiness migration audit contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_readiness_config.py"


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
