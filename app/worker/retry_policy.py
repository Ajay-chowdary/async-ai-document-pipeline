"""Deciding whether to retry, and how long to wait.

Two questions, answered in one place so the processor never has to reason about
either inline.
"""

import random
import secrets

from sqlalchemy.exc import DBAPIError, DisconnectionError, OperationalError

from app.core.config import Settings
from app.core.exceptions import AppError

#: Failures that are transient by nature but are not ``AppError`` subclasses,
#: because they come from a driver or the standard library. Since Python 3.11
#: ``asyncio.TimeoutError`` is an alias of the builtin, so one entry covers both.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OperationalError,
    DisconnectionError,
    DBAPIError,
)


def is_retryable(error: BaseException) -> bool:
    """Return whether another attempt could plausibly succeed.

    ``AppError`` carries the answer on the exception class itself, so the
    classification lives with the failure rather than at the call site.
    Anything unrecognised is treated as **not** retryable: silently retrying an
    unknown bug three times turns one confusing failure into three.
    """
    if isinstance(error, AppError):
        return error.retryable
    return isinstance(error, RETRYABLE_EXCEPTIONS)


def compute_delay(
    attempt: int,
    settings: Settings,
    *,
    rng: random.Random | None = None,
) -> float:
    """Return the backoff before the next attempt, in seconds.

    Exponential from ``retry_base_delay_seconds``, capped at
    ``retry_max_delay_seconds``, then jittered. The jitter matters more than it
    looks: when a provider rate-limits a burst of jobs they all fail at once,
    and without it they would all come back at the same instant and be
    rate-limited again.

    Args:
        attempt: Completed attempts so far. ``0`` gives the base delay.
    """
    exponent = max(0, attempt)
    raw = settings.retry_base_delay_seconds * float(2**exponent)
    capped = float(min(raw, settings.retry_max_delay_seconds))

    if settings.retry_jitter_ratio <= 0:
        return capped

    spread = capped * settings.retry_jitter_ratio
    source = rng or _default_rng()
    jittered = capped + source.uniform(-spread, spread)
    return float(max(0.0, jittered))


def _default_rng() -> random.Random:
    """A per-call RNG seeded from the OS.

    Not for security, but seeding from ``secrets`` avoids every worker in a
    fleet starting from the same default seed and jittering identically.
    """
    return random.Random(secrets.randbits(64))  # noqa: S311 - jitter, not crypto


def should_retry(error: BaseException, *, retry_count: int, max_retries: int) -> bool:
    """Return whether this failure earns another attempt."""
    return is_retryable(error) and retry_count < max_retries


def describe(error: BaseException) -> str:
    """Render a failure as a short, safe message for storage and display.

    Only the exception type and its own message are used. Document contents
    never reach this path, and the string is surfaced to API clients.
    """
    if isinstance(error, AppError):
        return f"{error.code}: {error.message}"
    return f"{type(error).__name__}: {error}"
