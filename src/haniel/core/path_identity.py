"""Platform-canonical filesystem identity helpers."""

from __future__ import annotations

import os
from pathlib import Path


def canonical_path_text(path: Path) -> str:
    """Return the canonical path representation used by persisted identities."""

    return os.path.normcase(str(path.expanduser().resolve(strict=False)))
