"""Canonical readiness parser ownership and compatibility contracts."""

from __future__ import annotations

from pathlib import Path

from haniel.config import (
    ReadyCondition as ConfigReadyCondition,
)
from haniel.config import (
    ReadyConditionType as ConfigReadyConditionType,
)
from haniel.config import (
    parse_ready_condition,
)
from haniel.core import (
    ReadyCondition as CoreReadyCondition,
)
from haniel.core import (
    ReadyConditionType as CoreReadyConditionType,
)
from haniel.core.process import (
    ReadyCondition as ProcessReadyCondition,
)
from haniel.core.process import (
    ReadyConditionType as ProcessReadyConditionType,
)

SOURCE_ROOT = Path(__file__).parents[1] / "src/haniel"
CANONICAL_PARSER = SOURCE_ROOT / "config/readiness.py"


def test_public_readiness_imports_resolve_to_canonical_types() -> None:
    assert CoreReadyCondition is ConfigReadyCondition
    assert ProcessReadyCondition is ConfigReadyCondition
    assert CoreReadyConditionType is ConfigReadyConditionType
    assert ProcessReadyConditionType is ConfigReadyConditionType
    assert ConfigReadyCondition.parse("delay:0.01") == parse_ready_condition(
        "delay:0.01"
    )


def test_no_consumer_reimplements_readiness_parser() -> None:
    forbidden_fragments = (
        'ready.startswith("port:")',
        'ready.startswith("delay:")',
        'ready.startswith("log:")',
        'ready.startswith("http:")',
        'removeprefix("port:")',
        'removeprefix("delay:")',
        'removeprefix("log:")',
        'removeprefix("http:")',
        "class ReadyConditionType",
        "class ReadyCondition:",
        "def parse_ready_condition(",
    )
    violations: list[str] = []

    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if path == CANONICAL_PARSER:
            continue
        source = path.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            if fragment in source:
                violations.append(f"{path.relative_to(SOURCE_ROOT)}: {fragment}")

    assert violations == []


def test_all_runtime_config_writers_use_canonical_semantic_gate() -> None:
    writer_paths = (
        SOURCE_ROOT / "core/service_lifecycle.py",
        SOURCE_ROOT / "dashboard/config_api.py",
        SOURCE_ROOT / "integrations/mcp_server.py",
    )
    violations: list[str] = []
    for path in writer_paths:
        source = path.read_text(encoding="utf-8")
        if "require_valid_config" not in source:
            violations.append(
                f"{path.relative_to(SOURCE_ROOT)}: missing require_valid_config"
            )
        if "validate_config(" in source:
            violations.append(
                f"{path.relative_to(SOURCE_ROOT)}: adapter validate_config"
            )
    assert violations == []
