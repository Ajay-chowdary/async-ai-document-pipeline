"""Processing job response models."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.core.enums import DocumentType, JobStatus
from app.schemas.common import Page
from app.schemas.extraction import ExtractionResultResponse


class JobSummary(BaseModel):
    """A row in the jobs table.

    Carries the handful of joined fields the dashboard needs — filename and
    detected type — so rendering the list never requires a second request per
    row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    status: JobStatus
    retry_count: int
    max_retries: int
    error_message: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    processing_duration_ms: int | None
    created_at: datetime
    updated_at: datetime

    original_filename: str | None = None
    detected_document_type: DocumentType | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_terminal(self) -> bool:
        """Whether the dashboard can stop polling this job."""
        return self.status.is_terminal


class JobDetail(JobSummary):
    """A single job with its extraction result, when one exists."""

    result: ExtractionResultResponse | None = None
    consumer_name: str | None = Field(
        default=None, description="Worker that last owned this job, useful when debugging."
    )


JobPage = Page[JobSummary]


class JobRetryResponse(BaseModel):
    """Returned when a failed job is manually re-queued."""

    job_id: uuid.UUID
    status: JobStatus
    retry_count: int
    status_url: str
