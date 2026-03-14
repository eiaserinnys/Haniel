"""
Installer utility functions.

Shared helpers used across installer phases.
"""

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def find_winsw(config_dir: Path) -> Path | None:
    """Find the WinSW executable.

    Walks up from config_dir looking for bin/winsw.exe, then falls back
    to PATH. This handles the standard layout where winsw.exe lives in
    the haniel install root's bin/ directory, regardless of how deeply
    nested the service config directory is.

    Args:
        config_dir: The service configuration directory

    Returns:
        Path to winsw.exe, or None if not found
    """
    current = config_dir.resolve()
    logger.debug(f"Searching for WinSW starting from: {current}")
    for _ in range(5):
        candidate = current / "bin" / "winsw.exe"
        logger.debug(f"  Checking: {candidate} (exists={candidate.exists()})")
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break  # Reached filesystem root
        current = parent

    found = shutil.which("winsw")
    if found:
        logger.debug(f"  Found in PATH: {found}")
        return Path(found)

    logger.debug("  WinSW not found anywhere")
    return None
