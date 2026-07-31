"""Timestamp helpers."""

from datetime import UTC, datetime, timedelta

from app.core.time import elapsed_ms, utcnow


def test_utcnow_is_timezone_aware() -> None:
    """Naive datetimes compare incorrectly against database values."""
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_elapsed_ms_between_two_points() -> None:
    start = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
    end = start + timedelta(milliseconds=1500)
    assert elapsed_ms(start, end) == 1500


def test_elapsed_ms_defaults_to_now() -> None:
    assert elapsed_ms(utcnow() - timedelta(seconds=1)) >= 1000


def test_elapsed_ms_never_negative() -> None:
    """A clock adjustment must not push a negative duration into the metrics."""
    start = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
    assert elapsed_ms(start, start - timedelta(seconds=5)) == 0
