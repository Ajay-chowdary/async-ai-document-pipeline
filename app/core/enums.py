"""Enumerations shared by database models, API schemas and the worker.

This module exists to give ``app.db`` and ``app.schemas`` a common dependency
without either importing the other, which would create a circular import.
"""

from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle states of a :class:`ProcessingJob`."""

    QUEUED = "queued"
    PROCESSING = "processing"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Whether no worker will ever move the job out of this state on its own.

        ``failed`` is terminal for the worker but can still be revived by an
        explicit operator action (``POST /api/v1/jobs/{id}/retry``).
        """
        return self in _TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """Whether the job is currently owned by, or waiting for, a worker."""
        return self in _ACTIVE_STATUSES


_TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})
_ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.RETRYING})

#: States from which a worker is allowed to claim a job for processing.
CLAIMABLE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RETRYING})


class DocumentType(StrEnum):
    """Supported document classifications, one extraction schema each."""

    INVOICE = "invoice"
    RESUME = "resume"
    SUPPORT_TICKET = "support_ticket"
    GENERIC = "generic"


class LLMProviderName(StrEnum):
    """Selectable LLM backends.

    ``fake`` is a deterministic in-process provider used by the test suite and
    by ``LLM_PROVIDER=fake`` local runs, so the system is fully exercisable
    without an API key or paid calls.
    """

    OPENAI = "openai"
    FAKE = "fake"


class StorageBackend(StrEnum):
    """Selectable file storage backends."""

    LOCAL = "local"


class EventType(StrEnum):
    """Message types carried on the Redis stream."""

    DOCUMENT_PROCESSING_REQUESTED = "document.processing.requested"
