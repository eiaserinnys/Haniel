"""Redact dashboard query tokens from Uvicorn log records."""

import logging
import re


_QUERY_TOKEN_PATTERN = re.compile(r'([?&]token=)[^&\s"]*')


def redact_query_token(text: str) -> str:
    """Replace query token values while preserving the rest of the text."""
    return _QUERY_TOKEN_PATTERN.sub(r"\1***", text)


class QueryTokenRedactionFilter(logging.Filter):
    """Redact query token values before Uvicorn records reach handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_query_token(argument) if isinstance(argument, str) else argument
                for argument in record.args
            )
        if isinstance(record.msg, str):
            record.msg = redact_query_token(record.msg)
        return True


def install_query_token_redaction() -> None:
    """Install one redaction filter on each Uvicorn logger."""
    for logger_name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        if not any(
            isinstance(filter_, QueryTokenRedactionFilter)
            for filter_ in logger.filters
        ):
            logger.addFilter(QueryTokenRedactionFilter())
