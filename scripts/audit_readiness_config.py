#!/usr/bin/env python3
"""Audit enabled-service readiness without disclosing config values."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml
from pydantic import ValidationError as PydanticValidationError

from haniel.config import HanielConfig
from haniel.config.validators import check_readiness


def audit(path: Path) -> tuple[str, ...]:
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

    return tuple(
        f"{error.location or 'config'}: {error.code or 'CONFIG_SEMANTIC_INVALID'}"
        for error in check_readiness(config)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path, nargs="+")
    args = parser.parse_args()
    found = False
    include_path = len(args.config) > 1
    for path in args.config:
        errors = audit(path)
        found = found or bool(errors)
        for error in errors:
            prefix = f"{path}: " if include_path else ""
            print(f"{prefix}{error}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
