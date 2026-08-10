#!/usr/bin/env python3
"""Audit enabled-service readiness without disclosing config values."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml
from pydantic import ValidationError as PydanticValidationError

from haniel.config import HanielConfig
from haniel.config.validators import check_readiness


CONTRACT_SCHEMA = "haniel.readiness-contract.v1"


def _audit_contracts(
    config: HanielConfig,
    paths: tuple[Path, ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    seen_services: set[str] = set()
    for path in paths:
        location = f"contracts.{path.name}"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(
                f"{location}: READINESS_CONTRACT_READ_FAILED ({type(error).__name__})"
            )
            continue
        if not isinstance(payload, dict) or payload.get("schema_version") != (
            CONTRACT_SCHEMA
        ):
            errors.append(f"{location}: READINESS_CONTRACT_SCHEMA_INVALID")
            continue
        service = payload.get("service")
        marker = payload.get("marker")
        ready = payload.get("ready")
        if not all(
            isinstance(value, str) and value for value in (service, marker, ready)
        ):
            errors.append(f"{location}: READINESS_CONTRACT_FIELDS_INVALID")
            continue
        assert isinstance(service, str)
        assert isinstance(marker, str)
        assert isinstance(ready, str)
        if service in seen_services:
            errors.append(f"services.{service}: READINESS_CONTRACT_DUPLICATE")
            continue
        seen_services.add(service)
        if not ready.startswith("log:"):
            errors.append(f"services.{service}: READINESS_CONTRACT_READY_INVALID")
            continue
        try:
            matches_exactly = re.fullmatch(ready.removeprefix("log:"), marker)
        except re.error:
            matches_exactly = None
        if matches_exactly is None:
            errors.append(f"services.{service}: READINESS_CONTRACT_REGEX_MISMATCH")
            continue
        configured = config.services.get(service)
        if configured is None:
            errors.append(f"services.{service}: READINESS_CONTRACT_SERVICE_MISSING")
        elif configured.ready != ready:
            errors.append(f"services.{service}: READINESS_CONTRACT_CONFIG_MISMATCH")
    return tuple(errors)


def audit(
    path: Path,
    readiness_contracts: tuple[Path, ...] = (),
) -> tuple[str, ...]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = HanielConfig.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        return (f"config: CONFIG_READ_FAILED ({type(error).__name__})",)
    except PydanticValidationError as error:
        return tuple(
            "schema."
            + ".".join(str(part) for part in detail["loc"])
            + f": CONFIG_SCHEMA_INVALID ({detail['type']})"
            for detail in error.errors()
        )

    if not config.services:
        return ("services: READINESS_AUDIT_EMPTY",)

    readiness_errors = tuple(
        f"{error.location or 'config'}: {error.code or 'CONFIG_SEMANTIC_INVALID'}"
        for error in check_readiness(config)
    )
    return readiness_errors + _audit_contracts(config, readiness_contracts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--readiness-contract",
        type=Path,
        action="append",
        default=[],
    )
    parser.add_argument("config", type=Path, nargs="+")
    args = parser.parse_args()
    found = False
    include_path = len(args.config) > 1
    for path in args.config:
        errors = audit(path, tuple(args.readiness_contract))
        found = found or bool(errors)
        for error in errors:
            prefix = f"{path}: " if include_path else ""
            print(f"{prefix}{error}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
