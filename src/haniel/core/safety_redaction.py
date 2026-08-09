"""Central redaction for release command, journal, and lifecycle boundaries."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?:token|password|secret|auth(?:orization)?|credential|database_url)",
    re.IGNORECASE,
)
_LABELED_VALUE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    r"([A-Za-z0-9_.-]*(?:token|password|secret|auth(?:orization)?|credential|database_url)"
    r"[A-Za-z0-9_.-]*)"
    r"(\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s]+)@")
_ERROR_TEXT_MAX_CHARS = 4096


def is_sensitive_key(key: str) -> bool:
    """Return whether a key names credential-bearing data."""
    return _SENSITIVE_KEY.search(key) is not None


def sensitive_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Extract secret values without ever formatting their names or values."""
    return tuple(
        sorted(
            {
                value
                for key, value in environment.items()
                if value and is_sensitive_key(key)
            },
            key=len,
            reverse=True,
        )
    )


def redact_text(text: str, values: Iterable[str] = ()) -> str:
    """Redact known values, labeled assignments, and URL userinfo."""
    redacted = text
    for value in sorted({value for value in values if value}, key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    redacted = _LABELED_VALUE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        redacted,
    )
    return _URL_CREDENTIALS.sub(r"\1[REDACTED]@", redacted)


def bounded_redact_text(
    text: str,
    values: Iterable[str] = (),
    *,
    max_chars: int = _ERROR_TEXT_MAX_CHARS,
) -> str:
    """Redact then bound an error string before it crosses a public boundary."""
    redacted = redact_text(text, values)
    if len(redacted) <= max_chars:
        return redacted
    suffix = "... [truncated]"
    return redacted[: max_chars - len(suffix)] + suffix


def redact_value(value: Any, values: Iterable[str] = ()) -> Any:
    """Recursively redact JSON-compatible evidence before persistence."""
    known_values = tuple(values)
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if is_sensitive_key(str(key))
                else redact_value(item, known_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, known_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, known_values) for item in value)
    if isinstance(value, str):
        return redact_text(value, known_values)
    return value
