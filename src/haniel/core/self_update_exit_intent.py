"""Durable signal consumed by the Linux wrapper's self-update watchdog."""

import os
from datetime import datetime, timezone
from pathlib import Path


MARKER_RELPATH = Path(".local") / "self_update_exit_requested"


def write(config_dir: Path) -> Path:
    """Atomically declare that this process is shutting down for self-update."""
    marker = config_dir / MARKER_RELPATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(marker.suffix + ".tmp")
    temporary.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    os.replace(temporary, marker)
    return marker
