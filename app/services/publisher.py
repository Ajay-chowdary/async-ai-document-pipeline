"""Publishing processing events, with the failure case handled honestly.

An upload writes to PostgreSQL and to Redis, and those cannot be one atomic
operation. The order chosen here is: commit the job first, then publish. That
way a crash between them leaves a ``queued`` job that no worker will ever see —
visible in the dashboard and recoverable with a manual retry — rather than an
event referring to a job that does not exist.

When the publish itself fails, the job is marked ``failed`` immediately with a
clear reason, so it is actionable rather than silently stuck at ``queued``. The
production answer is a transactional outbox: write the event to a table in the
same transaction and have a relay push it to Redis. That is deliberately out of
scope here, and the gap is recorded in the README.
"""

from app.core.exceptions import QueueError
from app.core.logging import get_correlation_id, get_logger
from app.db.models import Document, ProcessingJob
from app.schemas.events import DocumentProcessingEvent
from app.services.queue import RedisQueue

logger = get_logger(__name__)


def build_event(
    job: ProcessingJob, document: Document, *, attempt: int = 0
) -> DocumentProcessingEvent:
    """Construct the event describing a job that needs processing."""
    return DocumentProcessingEvent(
        job_id=job.id,
        document_id=document.id,
        storage_path=document.storage_path,
        requested_document_type=document.requested_document_type,
        attempt=attempt,
        correlation_id=get_correlation_id(),
    )


async def publish_job(
    queue: RedisQueue, job: ProcessingJob, document: Document, *, attempt: int = 0
) -> str:
    """Publish a job to the stream and return the message ID.

    Raises:
        QueueError: publishing failed; the caller is responsible for marking
            the job so it does not sit at ``queued`` forever.
    """
    event = build_event(job, document, attempt=attempt)
    try:
        return await queue.publish(event)
    except QueueError:
        logger.exception("publisher.publish_failed", job_id=str(job.id))
        raise
