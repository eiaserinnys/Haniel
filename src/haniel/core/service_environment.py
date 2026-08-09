"""Authoritative service environment-file loading.

The path declared by ``ServiceConfig.release_env_file`` is shared by the
long-running service process and release-manifest children.  Values are loaded
without interpolation so ambient process variables cannot change their
meaning.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import ServiceConfig
from .child_env import sanitized_child_env
from .path_identity import canonical_path_text

DATABASE_ENVIRONMENT_KEYS = (
    "DATABASE_URL",
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSERVICE",
    "PGSERVICEFILE",
)

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ServiceEnvironmentFile:
    """One validated environment-file byte snapshot."""

    path: Path
    normalized: bytes
    sha256: str
    values: dict[str, str]


def read_service_environment_file(path: Path) -> ServiceEnvironmentFile:
    """Read and parse a service env file without ambient interpolation."""

    resolved = path.expanduser().resolve(strict=False)
    try:
        normalized = resolved.read_bytes().replace(b"\r\n", b"\n")
    except OSError as error:
        raise RuntimeError(
            "SERVICE_ENV_FILE_INVALID: declared service env file cannot be read"
        ) from error
    try:
        content = normalized.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(
            "SERVICE_ENV_FILE_INVALID: declared service env file must be UTF-8"
        ) from error
    return ServiceEnvironmentFile(
        path=resolved,
        normalized=normalized,
        sha256=hashlib.sha256(normalized).hexdigest(),
        values=_parse_env_values(content),
    )


def service_process_environment(
    config_dir: Path,
    config: ServiceConfig,
    *,
    expected_env_path: str | None = None,
    expected_env_sha256: str | None = None,
    approved_snapshot: ServiceEnvironmentFile | None = None,
) -> dict[str, str]:
    """Build runtime env from the same declared file used by release phases."""

    environment = sanitized_child_env()
    if config.release_env_file is None:
        if expected_env_path is not None or expected_env_sha256 is not None:
            raise RuntimeError(
                "SERVICE_ENV_FILE_CHANGED: bound service env file is no longer declared"
            )
        return environment
    snapshot = approved_snapshot or read_service_environment_file(
        config_dir / config.release_env_file
    )
    if expected_env_path is not None and canonical_path_text(
        snapshot.path
    ) != canonical_path_text(Path(expected_env_path)):
        raise RuntimeError(
            "SERVICE_ENV_FILE_CHANGED: service env file path changed after binding"
        )
    if expected_env_sha256 is not None and snapshot.sha256 != expected_env_sha256:
        raise RuntimeError(
            "SERVICE_ENV_FILE_CHANGED: service env file content changed after binding"
        )
    for key in DATABASE_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment.update(snapshot.values)
    return environment


def _parse_env_values(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not _ENV_KEY.fullmatch(key):
            raise RuntimeError(
                f"SERVICE_ENV_FILE_INVALID: invalid assignment at line {line_number}"
            )
        values[key] = _parse_env_value(raw_value.strip(), line_number)
    return values


def _parse_env_value(value: str, line_number: int) -> str:
    if not value:
        return ""
    if value[0] == "'":
        if len(value) < 2 or value[-1] != "'":
            raise RuntimeError(
                f"SERVICE_ENV_FILE_INVALID: unterminated quote at line {line_number}"
            )
        return value[1:-1]
    if value[0] == '"':
        if len(value) < 2 or value[-1] != '"':
            raise RuntimeError(
                f"SERVICE_ENV_FILE_INVALID: unterminated quote at line {line_number}"
            )
        inner = value[1:-1]
        return (
            inner.replace(r"\n", "\n")
            .replace(r"\r", "\r")
            .replace(r"\t", "\t")
            .replace(r"\"", '"')
            .replace(r"\\", "\\")
        )
    comment = re.search(r"\s+#", value)
    if comment:
        value = value[: comment.start()].rstrip()
    return value
