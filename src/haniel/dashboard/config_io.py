"""Backward-compatible re-export for config I/O helpers."""

from ..config.io import backup_config, read_config, restore_config, write_config

__all__ = ["backup_config", "read_config", "restore_config", "write_config"]
