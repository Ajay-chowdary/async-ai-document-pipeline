"""Job status and result endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from app.api.dependencies import QueueDep, SessionDep
from app.core.enums import JobStatus
from app.core.exceptions import QueueError
from app.core.logging import get_logger
from app.db.models import ProcessingJob
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.schemas.job import JobDetail, JobPage, JobRetryResponse, JobSummary
from app.services import job_service, publisher

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["jobs"])


def _to_summary(job: ProcessingJob) -> JobSummary:
    """Flatten the joined document and result fields the jobs table displays."""
    summary = JobSummary.model_validate(job)
    summary.original_filename = job.document.original_filename if job.document else None
    summary.detected_document_type = job.result.detected_document_type if job.result else None
    return summary


@router.get("/jobs", response_model=JobPage, summary="List jobs, newest first")
async def list_jobs(
    session: SessionDep,
    status: Annotated[
        JobStatus | None, Query(description="Return only jobs in this state.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobPage:
    """Return a page of jobs, optionally filtered by status."""
    jobs, total = await job_service.list_jobs(session, status=status, limit=limit, offset=offset)
    return JobPage(
        items=[_to_summary(job) for job in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}", response_model=JobDetail, summary="Fetch one job and its result")
async def get_job(session: SessionDep, job_id: uuid.UUID) -> JobDetail:
    """Return a job's current state, plus its extraction result once available."""
    job = await job_service.get_job(session, job_id)
    detail = JobDetail.model_validate(job)
    detail.original_filename = job.document.original_filename if job.document else None
    detail.detected_document_type = job.result.detected_document_type if job.result else None
    return detail


@router.post(
    "/jobs/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobRetryResponse,
    summary="Re-queue a failed job",
)
async def retry_job(
    request: Request, session: SessionDep, queue: QueueDep, job_id: uuid.UUID
) -> JobRetryResponse:
    """Put a failed job back on the queue.

    Only ``failed`` jobs are eligible; anything else returns 409. That guard is
    what stops an operator clicking retry on a job a worker is actively
    processing and producing two concurrent attempts at the same document.

    The state change commits before the event is published, matching the upload
    path, and a publish failure fails the job again rather than leaving it
    ``queued`` with nothing to deliver it.
    """
    job = await job_service.get_job(session, job_id)
    await job_service.reset_for_manual_retry(session, job)

    try:
        await publisher.publish_job(queue, job, job.document)
    except QueueError:
        await job_service.mark_failed(
            session, job, error_message="The job could not be re-queued for processing."
        )
        raise

    logger.info("api.job_retried", job_id=str(job.id))
    return JobRetryResponse(
        job_id=job.id,
        status=job.status,
        retry_count=job.retry_count,
        status_url=str(request.url_for("get_job", job_id=job.id)),
    )
