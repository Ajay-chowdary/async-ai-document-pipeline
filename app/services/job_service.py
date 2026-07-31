"""Job lifecycle and persistence.

Every state change in the system passes through this module. Keeping the
transition table in one place means the legal lifecycle is readable in about
ten lines, and a bug like "a completed job got reprocessed" has exactly one
place to look.

Transaction boundaries are explicit here rather than in a framework hook, so a
reader can see precisely which writes commit together.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import CLAIMABLE_STATUSES, DocumentType, JobStatus
from app.core.exceptions import InvalidJobTransitionError, NotFoundError
from app.core.logging import get_logger
from app.core.time import elapsed_ms, utcnow
from app.db.models import Document, ProcessingJob
from app.services.file_storage import StoredFile

logger = get_logger(__name__)

#: The complete job lifecycle. Anything not listed is rejected.
#:
#: ``completed`` has no outgoing edges at all: once a result is persisted the
#: job is finished forever, which is what stops a redelivered queue message
#: from producing a second extraction. ``failed -> queued`` exists only for the
#: explicit operator action behind ``POST /api/v1/jobs/{id}/retry``.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.PROCESSING, JobStatus.FAILED}),
    JobStatus.PROCESSING: frozenset({JobStatus.COMPLETED, JobStatus.RETRYING, JobStatus.FAILED}),
    JobStatus.RETRYING: frozenset({JobStatus.PROCESSING, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED}),
}

#: Error text is truncated before storage: it is surfaced in the API and the
#: dashboard, and an unbounded provider message should not be echoed whole.
MAX_ERROR_MESSAGE_CHARS = 2_000


def is_transition_allowed(current: JobStatus, target: JobStatus) -> bool:
    """Return whether ``current -> target`` is a legal move."""
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition_allowed(current: JobStatus, target: JobStatus) -> None:
    """Raise unless ``current -> target`` is a legal move.

    Raises:
        InvalidJobTransitionError: rendered by the API as HTTP 409.
    """
    if not is_transition_allowed(current, target):
        raise InvalidJobTransitionError(
            f"Cannot move a job from {current.value} to {target.value}.",
            details={"from": current.value, "to": target.value},
        )


def _truncate_error(message: str | None) -> str | None:
    if message is None:
        return None
    if len(message) <= MAX_ERROR_MESSAGE_CHARS:
        return message
    return f"{message[:MAX_ERROR_MESSAGE_CHARS]}... [truncated]"


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


async def create_document_and_job(
    session: AsyncSession,
    *,
    stored: StoredFile,
    original_filename: str,
    content_type: str,
    requested_document_type: DocumentType | None,
    max_retries: int,
) -> tuple[Document, ProcessingJob]:
    """Insert a document and its initial queued job in one transaction.

    Both rows commit together: a document without a job would never be
    processed, and a job without a document could never be processed.
    """
    document = Document(
        original_filename=original_filename,
        stored_filename=stored.stored_filename,
        content_type=content_type,
        file_size=stored.size,
        storage_path=stored.storage_path,
        checksum=stored.checksum,
        requested_document_type=requested_document_type,
    )
    job = ProcessingJob(
        document=document,
        status=JobStatus.QUEUED,
        retry_count=0,
        max_retries=max_retries,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    await session.refresh(document)

    logger.info(
        "job.created",
        job_id=str(job.id),
        document_id=str(document.id),
        checksum=stored.checksum,
        size_bytes=stored.size,
        requested_document_type=(
            requested_document_type.value if requested_document_type else None
        ),
    )
    return document, job


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> Document:
    """Return a document by ID.

    Raises:
        NotFoundError: no such document.
    """
    document = await session.get(Document, document_id)
    if document is None:
        raise NotFoundError(
            "No document exists with that ID.", details={"document_id": str(document_id)}
        )
    return document


def _job_query() -> Select[tuple[ProcessingJob]]:
    """Base job query with the relationships every response needs."""
    return select(ProcessingJob).options(
        selectinload(ProcessingJob.result),
        selectinload(ProcessingJob.document),
    )


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> ProcessingJob:
    """Return a job with its document and result loaded.

    Raises:
        NotFoundError: no such job.
    """
    result = await session.execute(_job_query().where(ProcessingJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise NotFoundError("No job exists with that ID.", details={"job_id": str(job_id)})
    return job


async def list_jobs(
    session: AsyncSession,
    *,
    status: JobStatus | None = None,
    limit: int,
    offset: int,
) -> tuple[Sequence[ProcessingJob], int]:
    """Return one page of jobs, newest first, plus the unpaginated total.

    The total is counted in a second statement rather than with a window
    function so the count query stays index-only.
    """
    filters = [ProcessingJob.status == status] if status is not None else []

    page_query = (
        _job_query()
        .where(*filters)
        .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
        .limit(limit)
        .offset(offset)
    )
    count_query = select(func.count()).select_from(ProcessingJob).where(*filters)

    jobs = (await session.execute(page_query)).scalars().unique().all()
    total = (await session.execute(count_query)).scalar_one()
    return jobs, total


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


async def claim_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    consumer_name: str,
    stale_after_seconds: int,
) -> ProcessingJob | None:
    """Atomically take ownership of a job, or return ``None``.

    This is the idempotency barrier for the whole pipeline. The conditional
    ``UPDATE`` matches a row only when it is genuinely available:

    * ``queued`` or ``retrying`` — waiting for a worker; or
    * ``processing`` but started long enough ago that the previous owner is
      presumed dead, which is how pending-entry reclaim re-claims work.

    A duplicate delivery of an already-``completed`` job matches zero rows, so
    the caller acknowledges the message and does no work. That is what makes
    at-least-once queue delivery safe without a separate dedupe store.
    """
    now = utcnow()
    stale_cutoff = now - timedelta(seconds=stale_after_seconds)

    statement = (
        update(ProcessingJob)
        .where(
            ProcessingJob.id == job_id,
            or_(
                ProcessingJob.status.in_(CLAIMABLE_STATUSES),
                and_(
                    ProcessingJob.status == JobStatus.PROCESSING,
                    ProcessingJob.started_at.is_not(None),
                    ProcessingJob.started_at < stale_cutoff,
                ),
            ),
        )
        .values(
            status=JobStatus.PROCESSING,
            started_at=now,
            consumer_name=consumer_name,
            updated_at=now,
        )
        .returning(ProcessingJob.id)
    )

    claimed = (await session.execute(statement)).scalar_one_or_none()
    if claimed is None:
        await session.rollback()
        logger.info("job.claim_rejected", job_id=str(job_id), consumer=consumer_name)
        return None

    await session.commit()
    job = await get_job(session, job_id)
    logger.info("job.claimed", job_id=str(job_id), consumer=consumer_name, attempt=job.retry_count)
    return job


async def mark_completed(
    session: AsyncSession, job: ProcessingJob, *, commit: bool = True
) -> ProcessingJob:
    """Move a job to ``completed`` and record how long the attempt took.

    Callers that persist an extraction result should pass ``commit=False`` and
    commit once, so the result row and the terminal status land in the same
    transaction. A partial commit here would let a crash leave a completed job
    with no result.
    """
    ensure_transition_allowed(job.status, JobStatus.COMPLETED)
    now = utcnow()
    job.status = JobStatus.COMPLETED
    job.completed_at = now
    job.error_message = None
    if job.started_at is not None:
        job.processing_duration_ms = elapsed_ms(job.started_at, now)
    if commit:
        await session.commit()

    logger.info(
        "job.completed",
        job_id=str(job.id),
        duration_ms=job.processing_duration_ms,
        attempts=job.retry_count + 1,
    )
    return job


async def mark_retrying(
    session: AsyncSession, job: ProcessingJob, *, error_message: str, commit: bool = True
) -> ProcessingJob:
    """Move a job to ``retrying`` and count the failed attempt."""
    ensure_transition_allowed(job.status, JobStatus.RETRYING)
    job.status = JobStatus.RETRYING
    job.retry_count += 1
    job.error_message = _truncate_error(error_message)
    if commit:
        await session.commit()

    logger.warning(
        "job.retrying",
        job_id=str(job.id),
        retry_count=job.retry_count,
        attempts_remaining=job.attempts_remaining,
    )
    return job


async def mark_failed(
    session: AsyncSession, job: ProcessingJob, *, error_message: str, commit: bool = True
) -> ProcessingJob:
    """Move a job to ``failed`` permanently, recording why."""
    ensure_transition_allowed(job.status, JobStatus.FAILED)
    now = utcnow()
    job.status = JobStatus.FAILED
    job.completed_at = now
    job.error_message = _truncate_error(error_message)
    if job.started_at is not None:
        job.processing_duration_ms = elapsed_ms(job.started_at, now)
    if commit:
        await session.commit()

    logger.error(
        "job.failed",
        job_id=str(job.id),
        retry_count=job.retry_count,
        error=job.error_message,
    )
    return job


async def reset_for_manual_retry(
    session: AsyncSession, job: ProcessingJob, *, commit: bool = True
) -> ProcessingJob:
    """Re-queue a failed job at the operator's request.

    Only ``failed`` jobs are eligible; attempting this on an active job raises
    a conflict, which is what prevents a manual retry from racing a worker and
    creating two concurrent attempts for the same document.

    The retry budget is reset, because the operator is asserting that whatever
    caused the original failures has been addressed.
    """
    ensure_transition_allowed(job.status, JobStatus.QUEUED)
    job.status = JobStatus.QUEUED
    job.retry_count = 0
    job.error_message = None
    job.started_at = None
    job.completed_at = None
    job.processing_duration_ms = None
    job.consumer_name = None
    job.queued_at = utcnow()
    if commit:
        await session.commit()

    logger.info("job.manual_retry", job_id=str(job.id), document_id=str(job.document_id))
    return job
