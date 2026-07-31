"""The job state machine, tested without a database."""

import itertools

import pytest

from app.core.enums import JobStatus
from app.core.exceptions import InvalidJobTransitionError
from app.services.job_service import (
    ALLOWED_TRANSITIONS,
    MAX_ERROR_MESSAGE_CHARS,
    _truncate_error,
    ensure_transition_allowed,
    is_transition_allowed,
)

LEGAL = [
    (JobStatus.QUEUED, JobStatus.PROCESSING),
    (JobStatus.QUEUED, JobStatus.FAILED),
    (JobStatus.PROCESSING, JobStatus.COMPLETED),
    (JobStatus.PROCESSING, JobStatus.RETRYING),
    (JobStatus.PROCESSING, JobStatus.FAILED),
    (JobStatus.RETRYING, JobStatus.PROCESSING),
    (JobStatus.RETRYING, JobStatus.FAILED),
    (JobStatus.FAILED, JobStatus.QUEUED),
]


@pytest.mark.parametrize(("current", "target"), LEGAL)
def test_legal_transitions_allowed(current: JobStatus, target: JobStatus) -> None:
    assert is_transition_allowed(current, target)
    ensure_transition_allowed(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        pair
        for pair in itertools.product(JobStatus, repeat=2)
        if pair not in LEGAL  # every pair the table above does not permit
    ],
)
def test_everything_else_rejected(current: JobStatus, target: JobStatus) -> None:
    assert not is_transition_allowed(current, target)
    with pytest.raises(InvalidJobTransitionError):
        ensure_transition_allowed(current, target)


def test_every_status_has_an_entry() -> None:
    """A status missing from the table would raise KeyError at runtime."""
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)


class TestTerminalGuarantees:
    def test_completed_is_absorbing(self) -> None:
        """The guarantee behind "never process the same completed job twice"."""
        assert ALLOWED_TRANSITIONS[JobStatus.COMPLETED] == frozenset()

    @pytest.mark.parametrize("target", list(JobStatus))
    def test_nothing_leaves_completed(self, target: JobStatus) -> None:
        assert not is_transition_allowed(JobStatus.COMPLETED, target)

    def test_failed_only_reopens_via_the_queue(self) -> None:
        """A manual retry re-queues; it cannot jump straight into processing."""
        assert ALLOWED_TRANSITIONS[JobStatus.FAILED] == frozenset({JobStatus.QUEUED})

    def test_no_status_is_self_looping(self) -> None:
        """Self-transitions would mask bugs; job claiming handles them in SQL."""
        for status in JobStatus:
            assert not is_transition_allowed(status, status)


class TestTransitionError:
    def test_is_a_conflict(self) -> None:
        with pytest.raises(InvalidJobTransitionError) as error:
            ensure_transition_allowed(JobStatus.COMPLETED, JobStatus.PROCESSING)
        assert error.value.http_status == 409

    def test_names_both_states(self) -> None:
        with pytest.raises(InvalidJobTransitionError) as error:
            ensure_transition_allowed(JobStatus.COMPLETED, JobStatus.PROCESSING)
        assert error.value.details == {"from": "completed", "to": "processing"}


class TestErrorTruncation:
    def test_none_preserved(self) -> None:
        assert _truncate_error(None) is None

    def test_short_message_unchanged(self) -> None:
        assert _truncate_error("boom") == "boom"

    def test_long_message_clipped(self) -> None:
        result = _truncate_error("x" * 10_000)
        assert result is not None
        assert len(result) <= MAX_ERROR_MESSAGE_CHARS + 20
        assert result.endswith("[truncated]")
