"""The structured record produced by the LLM for a completed job."""

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import DocumentType
from app.db.base import Base, DefaultedTimestamp, UUIDPrimaryKey
from app.db.types import DocumentTypeEnum

if TYPE_CHECKING:
    from app.db.models.processing_job import ProcessingJob


class ExtractionResult(Base):
    """Validated extraction output for exactly one job.

    ``extracted_data`` is JSONB rather than four typed tables: the four
    document types have disjoint, evolving field sets, and a relational
    encoding would mean a migration per schema change plus a polymorphic join
    on every read. Type safety is enforced at the boundary instead — the
    payload is a Pydantic model dump on write and is re-validated on read — and
    JSONB still supports indexing into individual keys if that is ever needed.

    The unique constraint on ``job_id`` is a hard guarantee that a job cannot
    accumulate two results, which backstops the worker's idempotency check.
    """

    __tablename__ = "extraction_results"

    id: Mapped[UUIDPrimaryKey]

    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    #: The type actually used for extraction, whether client-supplied or classified.
    detected_document_type: Mapped[DocumentType] = mapped_column(
        DocumentTypeEnum, nullable=False, index=True
    )

    extracted_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: Truncated to ``RAW_TEXT_STORE_MAX_CHARS``; kept for debugging a bad
    #: extraction without re-reading the original file.
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Lets a change in prompt wording be correlated with a change in quality.
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Self-reported by the model, 0..1. Not a calibrated probability.
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[DefaultedTimestamp]

    job: Mapped["ProcessingJob"] = relationship(back_populates="result")

    def __repr__(self) -> str:
        return f"<ExtractionResult job_id={self.job_id} type={self.detected_document_type}>"
