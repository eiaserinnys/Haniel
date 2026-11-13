"""Durable record of legacy repositories whose post_pull build completed.

The legacy update path rewrites the checkout that services run from. When the
node stops between that rewrite and the end of the ``post_pull`` hook, the
checkout can already point at the new commit while its artifacts are stale.
The next start would then skip the hook and run new code against an unfinished
build. This marker records the commit whose ``post_pull`` finished, so the next
start can tell a completed update from an interrupted one.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .lifecycle_storage import atomic_json

logger = logging.getLogger(__name__)

MARKER_STATE_DIR = ".haniel"
MARKER_FILE_NAME = "legacy_post_pull.json"


def marker_path(config_dir: Path) -> Path:
    """Return the marker file shared by every legacy repository."""
    return config_dir / MARKER_STATE_DIR / MARKER_FILE_NAME


def read_completed_commits(config_dir: Path) -> dict[str, str]:
    """Return repo name -> commit whose post_pull completed.

    A missing or unreadable file reads as an empty record: the next start then
    reruns post_pull once, which is the safe direction for a build marker.
    """
    path = marker_path(config_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        logger.warning(
            "Ignoring unreadable legacy post_pull marker %s: %s", path, error
        )
        return {}
    if not isinstance(payload, dict):
        logger.warning("Ignoring malformed legacy post_pull marker %s", path)
        return {}
    return {
        name: commit
        for name, commit in payload.items()
        if isinstance(name, str) and isinstance(commit, str)
    }


def write_completed_commits(config_dir: Path, commits: dict[str, str]) -> None:
    """Replace the completed post_pull record for every legacy repository."""
    atomic_json(marker_path(config_dir), dict(commits))


def sync_disk() -> None:
    """Flush written files to disk where the platform exposes os.sync."""
    sync = getattr(os, "sync", None)
    if sync is None:
        return
    sync()
