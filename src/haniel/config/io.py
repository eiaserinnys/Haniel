"""YAML config read/write utilities."""

import os
import shutil
from pathlib import Path

import yaml

from .model import HanielConfig


def read_config(path: Path) -> HanielConfig:
    """Load haniel.yaml from disk."""
    from .model import load_config

    return load_config(path)


def write_config(path: Path, config: HanielConfig) -> None:
    """Write HanielConfig to YAML atomically."""
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
    """Copy config file to a .bak sidecar."""
    bak_path = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak_path)
    return bak_path


def restore_config(path: Path) -> None:
    """Restore config from the .bak sidecar if it exists."""
    bak_path = path.with_suffix(path.suffix + ".bak")
    if bak_path.exists():
        shutil.copy2(bak_path, path)
