"""Self-update result marker (read/write/consume). See ADR-0002.

Written by haniel-runner.ps1 after Update-HanielRepo, consumed by
the new runner on start() and exposed via /api/status + WS broadcast.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MARKER_RELPATH = Path(".local") / "self_update_result.json"
SCHEMA_VERSION = 1


@dataclass
class SelfUpdateStep:
    name: str
    ok: bool
    error: str | None = None


@dataclass
class SelfUpdateResult:
    version: int
    started_at: str
    finished_at: str
    ok: bool
    steps: list[SelfUpdateStep] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "steps": [asdict(s) for s in self.steps],
            "error": self.error,
        }


def _marker_path(config_dir: Path) -> Path:
    return config_dir / MARKER_RELPATH


def read_and_consume(config_dir: Path) -> SelfUpdateResult | None:
    """Read marker if present, validate, delete it, return parsed result.

    On any I/O or schema error: warn, attempt deletion, return None.
    The caller treats None as "no result available".
    """
    path = _marker_path(config_dir)
    if not path.exists():
        return None
    result: SelfUpdateResult | None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("marker must be a JSON object")
        version = data.get("version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported marker version: {version!r}")
        steps_raw = data.get("steps", [])
        if not isinstance(steps_raw, list):
            raise ValueError("steps must be a list")
        steps = [
            SelfUpdateStep(name=str(s["name"]), ok=bool(s["ok"]), error=s.get("error"))
            for s in steps_raw
            if isinstance(s, dict) and "name" in s and "ok" in s
        ]
        result = SelfUpdateResult(
            version=version,
            started_at=str(data["started_at"]),
            finished_at=str(data["finished_at"]),
            ok=bool(data["ok"]),
            steps=steps,
            error=data.get("error"),
        )
    except Exception as e:
        logger.warning("Self-update marker malformed at %s: %s", path, e)
        result = None
    # Always delete (consume) — even on parse error so we don't loop on a bad file
    try:
        path.unlink()
    except Exception as e:
        logger.warning("Failed to delete marker %s: %s", path, e)
    return result


def write(config_dir: Path, result: SelfUpdateResult) -> None:
    """Write marker (used in tests / by future Python-side updaters)."""
    path = _marker_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
