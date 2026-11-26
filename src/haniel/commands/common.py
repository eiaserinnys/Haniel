"""Shared configuration loading for CLI command modules."""

from pathlib import Path

import click
from pydantic import ValidationError as PydanticValidationError

from haniel.config import HanielConfig, load_config, validate_config


def validate_config_file(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> Path | None:
    """Validate that a config file exists and return its Path."""
    if value is None:
        return None
    path = Path(value)
    if not path.exists():
        raise click.BadParameter(f"Config file not found: {value}")
    return path


def load_and_validate(config_path: Path) -> tuple[HanielConfig | None, list[str]]:
    """Load a config and return schema plus semantic validation errors."""
    try:
        config = load_config(config_path)
    except PydanticValidationError as error:
        errors = []
        for detail in error.errors():
            location = ".".join(str(part) for part in detail["loc"])
            errors.append(f"Schema error at {location}: {detail['msg']}")
        return None, errors
    except Exception as error:
        return None, [f"Failed to load config: {error}"]

    return config, [str(error) for error in validate_config(config)]
