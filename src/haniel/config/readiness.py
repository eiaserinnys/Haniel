"""Canonical parsing for service readiness conditions."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


class ReadyConditionType(Enum):
    """Supported readiness probe kinds."""

    PORT = "port"
    DELAY = "delay"
    LOG = "log"
    HTTP = "http"


class ReadinessConfigError(ValueError):
    """A readiness condition is missing or semantically invalid."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ReadyCondition:
    """One validated readiness condition."""

    type: ReadyConditionType
    value: str

    @classmethod
    def parse(cls, condition: str) -> ReadyCondition:
        return parse_ready_condition(condition)

    @property
    def port(self) -> int | None:
        return int(self.value) if self.type is ReadyConditionType.PORT else None

    @property
    def delay(self) -> float | None:
        return float(self.value) if self.type is ReadyConditionType.DELAY else None

    @property
    def pattern(self) -> re.Pattern[str] | None:
        return re.compile(self.value) if self.type is ReadyConditionType.LOG else None

    @property
    def endpoint(self) -> str | None:
        if self.type is not ReadyConditionType.HTTP:
            return None
        if self.value.startswith("//"):
            return f"http:{self.value}"
        if self.value.startswith(("http://", "https://")):
            return self.value
        return f"http://{self.value}"


def parse_ready_condition(condition: str) -> ReadyCondition:
    """Parse and semantically validate one readiness condition."""

    if not isinstance(condition, str) or not condition.strip():
        raise ReadinessConfigError(
            "READINESS_REQUIRED", "enabled service requires a readiness condition"
        )
    kind, separator, raw_value = condition.partition(":")
    if not separator:
        raise ReadinessConfigError(
            "READINESS_UNKNOWN_TYPE", "condition must use kind:value syntax"
        )
    value = raw_value.strip()

    if kind == ReadyConditionType.PORT.value:
        try:
            port = int(value)
        except ValueError as error:
            raise ReadinessConfigError(
                "READINESS_PORT_INVALID", "port must be an integer"
            ) from error
        if not 1 <= port <= 65535:
            raise ReadinessConfigError(
                "READINESS_PORT_INVALID", "port must be between 1 and 65535"
            )
        return ReadyCondition(ReadyConditionType.PORT, str(port))

    if kind == ReadyConditionType.DELAY.value:
        try:
            delay = float(value)
        except ValueError as error:
            raise ReadinessConfigError(
                "READINESS_DELAY_INVALID", "delay must be a finite number"
            ) from error
        if not math.isfinite(delay) or delay <= 0:
            raise ReadinessConfigError(
                "READINESS_DELAY_INVALID", "delay must be finite and greater than zero"
            )
        return ReadyCondition(ReadyConditionType.DELAY, value)

    if kind == ReadyConditionType.LOG.value:
        if not value:
            raise ReadinessConfigError(
                "READINESS_LOG_INVALID", "log pattern must not be empty"
            )
        try:
            re.compile(value)
        except re.error as error:
            raise ReadinessConfigError(
                "READINESS_LOG_INVALID",
                "log pattern must be a valid regular expression",
            ) from error
        return ReadyCondition(ReadyConditionType.LOG, value)

    if kind == ReadyConditionType.HTTP.value:
        if not value:
            raise ReadinessConfigError(
                "READINESS_HTTP_INVALID", "HTTP endpoint must not be empty"
            )
        endpoint = f"http:{value}" if value.startswith("//") else value
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"http://{endpoint}"
        parsed = urlparse(endpoint)
        try:
            valid_port = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError:
            valid_port = False
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or not valid_port
        ):
            raise ReadinessConfigError(
                "READINESS_HTTP_INVALID", "HTTP endpoint must contain a valid host"
            )
        return ReadyCondition(ReadyConditionType.HTTP, value)

    raise ReadinessConfigError(
        "READINESS_UNKNOWN_TYPE", f"unsupported readiness type: {kind}"
    )


def ready_port(condition: str | None) -> int | None:
    """Return the canonical port probe value, or ``None`` for another kind."""

    if condition is None:
        return None
    try:
        parsed = parse_ready_condition(condition)
    except ReadinessConfigError:
        return None
    return parsed.port if parsed.type is ReadyConditionType.PORT else None
