"""Structured logging setup shared by the API and the worker.

Every log line is a JSON object in containers and a readable, coloured line
locally. Two processors exist specifically to satisfy the project's security
rules: :func:`redact_sensitive` masks credential-shaped keys, and
:func:`truncate_long_values` prevents whole documents from ever reaching the
log stream.

Context such as ``correlation_id`` and ``job_id`` is bound to a contextvar
rather than threaded through function signatures, so it appears on every line
emitted while handling a request or a queue event.
"""

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.types import EventDict, Processor

#: Keys whose values are replaced with a placeholder before rendering.
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "credentials",
        "database_url",
        "openai_api_key",
        "password",
        "redis_url",
        "secret",
        "token",
    }
)

#: Any string value longer than this is truncated. Guards against document text
#: or full LLM payloads being written to logs.
MAX_VALUE_CHARS = 1_000

_REDACTED = "***redacted***"
_CORRELATION_ID = "correlation_id"


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered in SENSITIVE_KEYS:
        return True
    return (
        "api_key" in lowered
        or "password" in lowered
        or "secret" in lowered
        or lowered.endswith("_token")
    )


def redact_sensitive(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    """Mask credential-shaped keys anywhere in the event, including nested dicts."""
    for key, value in event_dict.items():
        if _is_sensitive(str(key)):
            event_dict[key] = _REDACTED
        elif isinstance(value, dict):
            event_dict[key] = {
                inner_key: (_REDACTED if _is_sensitive(str(inner_key)) else inner_value)
                for inner_key, inner_value in value.items()
            }
    return event_dict


def truncate_long_values(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    """Clip oversized string values so document contents cannot leak into logs."""
    for key, value in event_dict.items():
        if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
            event_dict[key] = f"{value[:MAX_VALUE_CHARS]}... [truncated {len(value)} chars]"
    return event_dict


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    """Configure structlog and route the standard library through it.

    Uvicorn and SQLAlchemy log via :mod:`logging`; sharing one renderer keeps
    the container's output a single parseable stream.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_sensitive,
        truncate_long_values,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Uvicorn installs its own handlers; drop them so nothing is logged twice.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, conventionally named after the calling module."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_correlation_id(correlation_id: str | None = None) -> str:
    """Bind a correlation ID to the current context, generating one if absent.

    Returns the value in use so callers can echo it in responses and events.
    """
    resolved = correlation_id or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(**{_CORRELATION_ID: resolved})
    return resolved


def get_correlation_id() -> str | None:
    """Return the correlation ID bound to the current context, if any."""
    value = structlog.contextvars.get_contextvars().get(_CORRELATION_ID)
    return str(value) if value is not None else None


def bind_context(**values: Any) -> None:
    """Attach arbitrary key/value pairs to every subsequent log line."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    """Drop all bound context, called between queue events and requests."""
    structlog.contextvars.clear_contextvars()


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Bind context for the duration of a block and restore it afterwards."""
    tokens = structlog.contextvars.bind_contextvars(**values)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
