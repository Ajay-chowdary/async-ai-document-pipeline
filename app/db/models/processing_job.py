"""The unit of work tracked through the pipeline."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import JobStatus
from app.db.base import (
    Base,
    CreatedAt,
    DefaultedTimestamp,
    OptionalTimestamp,
    UpdatedAt,
    UUIDPrimaryKey,
)
from app.db.types import JobStatusEnum

if TYPE_CHECKING:
    from app.db.models.document import Document
    from app.db.models.extraction_result import ExtractionResult


class ProcessingJob(Base):
    """One attempt-tracked processing lifecycle for a document.

    This row is the single source of truth for whether work is outstanding.
    The worker claims a job with a conditional ``UPDATE`` against
    :attr:`status`, which is what makes at-least-once queue delivery safe: a
    duplicate message matches zero rows and is discarded.
    """

    __tablename__ = "processing_jobs"
    __table_args__ = (
        # Serves the dashboard's "newest first, optionally filtered by status".
        Index("ix_processing_jobs_status_created_at", "status", "created_at"),
        # Serves the pending/stale sweep, which looks for old active jobs.
        Index("ix_processing_jobs_status_started_at", "status", "started_at"),
    )

    id: Mapped[UUIDPrimaryKey]

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[JobStatus] = mapped_column(
        JobStatusEnum,
        nullable=False,
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
    )

    #: Completed attempts so far; ``0`` on the first delivery.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Snapshotted per job so changing the global default cannot alter the
    #: retry budget of work already in flight.
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: When the job last entered the queue; reset by a manual retry, which is
    #: why it is tracked separately from ``created_at``.
    queued_at: Mapped[DefaultedTimestamp]
    started_at: Mapped[OptionalTimestamp]
    completed_at: Mapped[OptionalTimestamp]
    #: Wall-clock duration of the successful attempt, for latency metrics.
    processing_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Redis consumer that currently owns, or last owned, this job.
    consumer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    document: Mapped["Document"] = relationship(back_populates="jobs", lazy="joined")
    # See the note on Document.jobs: "all" would include refresh-expire.
    result: Mapped["ExtractionResult | None"] = relationship(
        back_populates="job",
        cascade="save-update, merge, delete, delete-orphan",
        uselist=False,
    )

    @property
    def attempts_remaining(self) -> int:
        """Retries still available before the job is failed permanently."""
        return max(0, self.max_retries - self.retry_count)

    def __repr__(self) -> str:
        return f"<ProcessingJob id={self.id} status={self.status} retries={self.retry_count}>"
