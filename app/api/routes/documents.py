"""Upload and document metadata endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile, status

from app.api.dependencies import QueueDep, SessionDep, SettingsDep, StorageDep
from app.core.config import Settings
from app.core.enums import DocumentType
from app.core.exceptions import FileTooLargeError, QueueError
from app.core.logging import get_logger
from app.schemas.document import DocumentResponse, UploadAcceptedResponse
from app.services import job_service, publisher
from app.services.file_validation import validate_upload

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["documents"])

#: Uploads are streamed in chunks so an oversized file is rejected part-way
#: through rather than after the whole thing has been buffered.
READ_CHUNK_BYTES = 64 * 1024


async def _read_within_limit(upload: UploadFile, settings: Settings) -> bytes:
    """Read an upload into memory, aborting as soon as the limit is passed.

    Raises:
        FileTooLargeError: the payload exceeds ``MAX_UPLOAD_BYTES``.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(READ_CHUNK_BYTES):
        total += len(chunk)
        if total > settings.max_upload_bytes:
            raise FileTooLargeError(
                f"The file exceeds the {settings.max_upload_mb} MB limit.",
                details={"max_bytes": settings.max_upload_bytes},
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/documents",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=UploadAcceptedResponse,
    summary="Upload a document for asynchronous processing",
)
async def upload_document(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    queue: QueueDep,
    file: Annotated[UploadFile, File(description="PDF, DOCX or TXT, up to the configured limit.")],
    document_type: Annotated[
        DocumentType | None,
        Form(description="Skip automatic classification by naming the type yourself."),
    ] = None,
) -> UploadAcceptedResponse:
    """Validate, store and queue a document.

    Returns 202 rather than 201: the document exists, but the *result* the
    caller actually wants does not yet.

    Ordering is deliberate. The file is written before the rows are inserted,
    so a committed job always has a file to read — an orphaned file is a
    tolerable leak, a job pointing at nothing is not. The rows commit before
    the event is published, so an event never references a job that does not
    exist. When publishing fails the job is failed immediately, because a job
    stuck at ``queued`` with no event would look healthy and never progress.
    """
    data = await _read_within_limit(file, settings)
    safe_filename, extension = validate_upload(
        filename=file.filename,
        content_type=file.content_type,
        data=data,
        settings=settings,
    )

    stored = await storage.save(data=data, extension=extension)
    document, job = await job_service.create_document_and_job(
        session,
        stored=stored,
        original_filename=safe_filename,
        content_type=file.content_type or "application/octet-stream",
        requested_document_type=document_type,
        max_retries=settings.max_retries,
    )

    try:
        await publisher.publish_job(queue, job, document)
    except QueueError:
        await job_service.mark_failed(
            session,
            job,
            error_message="The document could not be queued for processing.",
        )
        raise

    return UploadAcceptedResponse(
        document_id=document.id,
        job_id=job.id,
        status=job.status,
        status_url=str(request.url_for("get_job", job_id=job.id)),
    )


@router.get(
    "/documents/{document_id}",
    response_model=DocumentResponse,
    summary="Fetch document metadata",
)
async def get_document(session: SessionDep, document_id: uuid.UUID) -> DocumentResponse:
    """Return metadata for one uploaded document."""
    document = await job_service.get_document(session, document_id)
    return DocumentResponse.model_validate(document)
