"""Timestamp helpers.

Every timestamp in this system is timezone-aware UTC. Naive datetimes are a
recurring source of off-by-hours bugs once an API, a worker and a database are
in different places, so they are never produced here.
"""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def elapsed_ms(start: datetime, end: datetime | None = None) -> int:
    """Return whole milliseconds between two aware datetimes, never negative.

    Used for ``processing_duration_ms``; a clock adjustment must not produce a
    negative duration in the metrics.
    """
    delta = (end or utcnow()) - start
    return max(0, int(delta.total_seconds() * 1000))
