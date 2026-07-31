"""Enum values and the terminal/claimable classifications built on them."""

import pytest

from app.core.enums import (
    CLAIMABLE_STATUSES,
    DocumentType,
    EventType,
    JobStatus,
    LLMProviderName,
)


def test_job_status_values_match_the_documented_contract() -> None:
    assert {status.value for status in JobStatus} == {
        "queued",
        "processing",
        "retrying",
        "completed",
        "failed",
    }


def test_document_type_values_match_the_documented_contract() -> None:
    assert {doc_type.value for doc_type in DocumentType} == {
        "invoice",
        "resume",
        "support_ticket",
        "generic",
    }


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (JobStatus.QUEUED, False),
        (JobStatus.PROCESSING, False),
        (JobStatus.RETRYING, False),
        (JobStatus.COMPLETED, True),
        (JobStatus.FAILED, True),
    ],
)
def test_terminal_classification(status: JobStatus, *, terminal: bool) -> None:
    assert status.is_terminal is terminal
    assert status.is_active is not terminal


def test_only_queued_and_retrying_are_claimable() -> None:
    assert frozenset({JobStatus.QUEUED, JobStatus.RETRYING}) == CLAIMABLE_STATUSES


def test_completed_is_never_claimable() -> None:
    """The worker must not reprocess a job that already produced a result."""
    assert JobStatus.COMPLETED not in CLAIMABLE_STATUSES


def test_enums_are_string_comparable() -> None:
    """StrEnum members serialise directly into JSON payloads and SQL values."""
    assert JobStatus.QUEUED == "queued"
    assert LLMProviderName.FAKE == "fake"
    assert EventType.DOCUMENT_PROCESSING_REQUESTED == "document.processing.requested"
