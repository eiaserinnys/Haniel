"""Independent import contracts for optional orchestrator client modules."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "haniel.integrations.orchestrator_client",
        "haniel.integrations.deploy_reporting",
        "haniel.core.orchestrated_deploy_execution",
    ],
)
def test_optional_orchestrator_modules_import_independently(
    module: str, tmp_path: Path
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(repo_root / "src")}

    completed = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
