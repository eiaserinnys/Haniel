"""
YAML config read/write utilities for the config API.
Handles backup, restore, and atomic write operations.
"""

import os
import shutil
from pathlib import Path

import yaml

from ..config.model import HanielConfig


def read_config(path: Path) -> HanielConfig:
    """Load haniel.yaml from disk.

    Args:
        path: Path to the configuration file

    Returns:
        Validated HanielConfig instance

    Raises:
        FileNotFoundError: If the config file doesn't exist
        yaml.YAMLError: If the YAML is invalid
        pydantic.ValidationError: If the config doesn't match the schema
    """
    from ..config.model import load_config

    return load_config(path)


def write_config(path: Path, config: HanielConfig) -> None:
    """Write HanielConfig to YAML atomically.

    Writes to a temporary file first, then replaces the target using
    os.replace() so that a crash mid-write never leaves a partial file.
    Serializes using by_alias=True so that the `self_update` field is written
    as `self` (matching the original YAML key). Uses exclude_none=True to keep
    the output clean.

    Args:
        path: Destination path for the YAML file
        config: HanielConfig instance to serialize
    """
    data = config.model_dump(by_alias=True, exclude_none=True, mode="python")
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.dump(
            data,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
    os.replace(tmp_path, path)


def backup_config(path: Path) -> Path:
    """Copy config file to a .bak sidecar.

    Args:
        path: Path to the original config file

    Returns:
        Path to the backup file (.yaml.bak)
    """
    bak_path = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak_path)
    return bak_path


def restore_config(path: Path) -> None:
    """Restore config from the .bak sidecar if it exists.

    Args:
        path: Path to the config file (not the .bak)
    """
    bak_path = path.with_suffix(path.suffix + ".bak")
    if bak_path.exists():
        shutil.copy2(bak_path, path)
