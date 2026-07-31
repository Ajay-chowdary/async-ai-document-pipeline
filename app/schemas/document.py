"""Document request and response models."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import DocumentType, JobStatus


class DocumentResponse(BaseModel):
    """Public view of an uploaded document.

    ``storage_path`` and ``stored_filename`` are deliberately absent: they are
    internal placement details, and exposing them would invite clients to
    construct paths of their own.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    content_type: str
    file_size: int
    checksum: str = Field(description="SHA-256 of the uploaded bytes.")
    requested_document_type: DocumentType | None
    created_at: datetime


class UploadAcceptedResponse(BaseModel):
    """Returned with HTTP 202 once an upload is durably queued.

    The response is issued after the database transaction commits and the
    event is published, so a client holding this body can rely on the job
    existing and being visible at ``status_url``.
    """

    document_id: uuid.UUID
    job_id: uuid.UUID
    status: JobStatus
    status_url: str = Field(description="Poll this URL for the job's progress and result.")
