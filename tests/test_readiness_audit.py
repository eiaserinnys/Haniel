"""Secret-free readiness migration audit contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_readiness_config.py"
WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "test.yml"
EXAMPLE = Path(__file__).parents[1] / "haniel.yaml.example"


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


def test_repository_example_is_a_nonempty_audit_target() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(EXAMPLE)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_repository_example_missing_ready_mutation_is_rejected(tmp_path: Path) -> None:
    mutated = tmp_path / "haniel.yaml.example"
    original = EXAMPLE.read_text(encoding="utf-8")
    mutated.write_text(
        original.replace("    ready: port:3101\n", "", 1),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(mutated)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout.strip() == "services.mcp-slack.ready: READINESS_REQUIRED"


def test_audit_binds_machine_contract_to_exact_config_ready(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"
    config.write_text(
        "services:\n"
        "  remiel:\n"
        "    run: node app.js\n"
        r"    ready: log:\[Remiel\] Bot is running!" + "\n",
        encoding="utf-8",
    )
    contract = tmp_path / "readiness-contract.json"
    contract.write_text(
        """{
  "schema_version": "haniel.readiness-contract.v1",
  "service": "remiel",
  "marker": "[Remiel] Bot is running!",
  "ready": "log:\\\\[Remiel\\\\] Bot is running!"
}
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(config),
            "--readiness-contract",
            str(contract),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_simultaneous_marker_and_regex_rename_requires_config_mapping_update(
    tmp_path: Path,
) -> None:
    config = tmp_path / "haniel.yaml"
    config.write_text(
        "services:\n"
        "  remiel:\n"
        "    run: node app.js\n"
        r"    ready: log:\[Remiel\] Bot is running!" + "\n",
        encoding="utf-8",
    )
    contract = tmp_path / "readiness-contract.json"
    contract.write_text(
        """{
  "schema_version": "haniel.readiness-contract.v1",
  "service": "remiel",
  "marker": "[Renamed] Boot complete",
  "ready": "log:\\\\[Renamed\\\\] Boot complete"
}
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(config),
            "--readiness-contract",
            str(contract),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "READINESS_CONTRACT_CONFIG_MISMATCH" in result.stdout


def test_ci_runs_repository_readiness_audit() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in workflow
    assert (
        "python scripts/audit_readiness_config.py "
        "tests/fixtures/readiness_ci_config.yaml haniel.yaml.example"
    ) in workflow
    fixture = WORKFLOW.parents[2] / "tests" / "fixtures" / "readiness_ci_config.yaml"
    assert "services:" in fixture.read_text(encoding="utf-8")
    assert "--readiness-contract tests/fixtures/readiness_contracts/remiel.json" in (
        workflow
    )
    contract_fixture = (
        WORKFLOW.parents[2] / "tests" / "fixtures" / "readiness_contract_config.yaml"
    )
    assert contract_fixture.read_text(encoding="utf-8").count("ready: 'log:") == 4
