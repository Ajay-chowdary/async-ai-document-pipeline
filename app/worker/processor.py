"""Processing a single event, end to end.

The ordering in :meth:`DocumentProcessor.process` is the part worth reading
carefully:

1. **Claim before work.** A conditional ``UPDATE`` decides whether this worker
   owns the job. Zero rows matched means the job is finished or owned by
   someone else, and the message is acknowledged without doing anything. This
   is what makes at-least-once delivery safe.
2. **Persist before acknowledging.** The extraction result and the terminal
   status commit in one transaction; the caller acknowledges only after this
   returns. A crash anywhere earlier leaves the entry in the pending list,
   where the recovery sweeper finds it.
3. **Schedule the retry before recording it.** The Redis write happens first
   so that a failure to schedule falls through to a permanent failure with an
   accurate message, instead of leaving a job marked ``retrying`` that nothing
   will ever redeliver.
"""

from enum import StrEnum
from pathlib import PurePosixPath

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.enums import DocumentType
from app.core.exceptions import QueueError
from app.core.logging import get_logger, log_context
from app.db.models import ExtractionResult, ProcessingJob
from app.llm.base import ExtractionOutput, LLMProvider
from app.schemas.events import DocumentProcessingEvent
from app.services import job_service, text_extraction
from app.services.file_storage import FileStorage
from app.services.queue import DeliveredEvent, RedisQueue
from app.worker.retry_policy import compute_delay, describe, should_retry

logger = get_logger(__name__)


class ProcessingOutcome(StrEnum):
    """What happened to an event. Every value is safe to acknowledge."""

    COMPLETED = "completed"
    #: The job was not claimable — already done, or owned by a live worker.
    SKIPPED = "skipped"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    #: Redelivered too many times; failed permanently rather than left to
    #: cycle through the fleet.
    DEAD_LETTERED = "dead_lettered"


class DocumentProcessor:
    """Turns one queue event into a persisted outcome."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: FileStorage,
        queue: RedisQueue,
        provider: LLMProvider,
        settings: Settings,
        consumer_name: str,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._storage = storage
        self._queue = queue
        self._provider = provider
        self._settings = settings
        self._consumer_name = consumer_name

    async def process(self, delivered: DeliveredEvent) -> ProcessingOutcome:
        """Handle one delivered event and return its outcome."""
        event = delivered.event
        with log_context(
            **event.log_context(),
            message_id=delivered.message_id,
            consumer=self._consumer_name,
            delivery_count=delivered.delivery_count,
        ):
            async with self._sessionmaker() as session:
                job = await job_service.claim_job(
                    session,
                    event.job_id,
                    consumer_name=self._consumer_name,
                    stale_after_seconds=self._settings.stale_processing_seconds,
                )
                if job is None:
                    logger.info("worker.event_skipped", reason="job_not_claimable")
                    return ProcessingOutcome.SKIPPED

                if delivered.delivery_count > self._settings.max_delivery_count:
                    await job_service.mark_failed(
                        session,
                        job,
                        error_message=(
                            f"Message redelivered {delivered.delivery_count} times without "
                            "completing; failed permanently."
                        ),
                    )
                    logger.error("worker.dead_lettered")
                    return ProcessingOutcome.DEAD_LETTERED

                try:
                    return await self._run(session, job, event)
                except Exception as error:
                    # Broad on purpose: every failure is classified by
                    # retry_policy rather than swallowed, and an unrecognised
                    # one fails the job permanently with its message recorded.
                    return await self._handle_failure(session, job, event, error)

    async def _run(
        self, session: AsyncSession, job: ProcessingJob, event: DocumentProcessingEvent
    ) -> ProcessingOutcome:
        """Read, extract, and persist. Any failure here is classified by the caller."""
        data = await self._storage.read(event.storage_path)
        extension = PurePosixPath(job.document.stored_filename).suffix

        text = text_extraction.extract_text(data, extension)
        model_input, truncated = text_extraction.truncate(text, self._settings.llm_max_input_chars)
        if truncated:
            logger.warning(
                "worker.text_truncated",
                original_chars=len(text),
                sent_chars=len(model_input),
            )

        document_type = await self._resolve_type(event, model_input)
        output = await self._provider.extract(model_input, document_type)

        await self._persist(session, job, output, raw_text=text)
        return ProcessingOutcome.COMPLETED

    async def _resolve_type(self, event: DocumentProcessingEvent, text: str) -> DocumentType:
        """Use the client's declared type, or classify when none was given.

        Skipping the classification call when the type is already known halves
        the model spend on the common path.
        """
        if event.requested_document_type is not None:
            logger.info("worker.type_supplied", document_type=event.requested_document_type.value)
            return event.requested_document_type

        sample, _ = text_extraction.truncate(text, self._settings.classification_max_chars)
        classification = await self._provider.classify(sample)
        logger.info(
            "worker.type_classified",
            document_type=classification.document_type.value,
            confidence=classification.confidence_score,
        )
        return classification.document_type

    async def _persist(
        self,
        session: AsyncSession,
        job: ProcessingJob,
        output: ExtractionOutput,
        *,
        raw_text: str,
    ) -> None:
        """Write the result and the terminal status in a single transaction.

        Committing separately would allow a crash to leave a completed job with
        no result, or a result attached to a job still marked ``processing``.
        """
        stored_text, _ = text_extraction.truncate(raw_text, self._settings.raw_text_store_max_chars)
        session.add(
            ExtractionResult(
                job_id=job.id,
                detected_document_type=output.document_type,
                extracted_data=output.data,
                raw_text=stored_text,
                model_provider=output.model_provider,
                model_name=output.model_name,
                prompt_version=output.prompt_version,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
                confidence_score=output.confidence_score,
            )
        )
        await job_service.mark_completed(session, job, commit=False)

        try:
            await session.commit()
        except IntegrityError:
            # The unique constraint on job_id fired, so a result already
            # exists. The claim should have prevented this; treat the job as
            # done rather than overwriting a result someone else committed.
            await session.rollback()
            logger.warning("worker.duplicate_result_ignored", job_id=str(job.id))
            return

        logger.info(
            "worker.extraction_stored",
            document_type=output.document_type.value,
            model_provider=output.model_provider,
            model_name=output.model_name,
            input_tokens=output.input_tokens,
            output_tokens=output.output_tokens,
            confidence=output.confidence_score,
        )

    async def _handle_failure(
        self,
        session: AsyncSession,
        job: ProcessingJob,
        event: DocumentProcessingEvent,
        error: Exception,
    ) -> ProcessingOutcome:
        """Retry a transient failure, or fail the job permanently."""
        # The rollback discards whatever the failed attempt left in the
        # session, and expires every loaded instance with it. The job must be
        # re-read before its status or retry count can be looked at again.
        await session.rollback()
        await session.refresh(job)
        message = describe(error)

        if should_retry(error, retry_count=job.retry_count, max_retries=job.max_retries):
            delay = compute_delay(job.retry_count, self._settings)
            try:
                await self._queue.schedule_retry(event.next_attempt(), delay)
            except QueueError:
                logger.exception("worker.retry_scheduling_failed")
                message = f"{message} (retry could not be scheduled)"
            else:
                await job_service.mark_retrying(session, job, error_message=message)
                logger.warning(
                    "worker.retry_scheduled",
                    delay_seconds=round(delay, 2),
                    error=message,
                    retry_count=job.retry_count,
                )
                return ProcessingOutcome.RETRY_SCHEDULED

        await job_service.mark_failed(session, job, error_message=message)
        logger.error("worker.job_failed", error=message, retry_count=job.retry_count)
        return ProcessingOutcome.FAILED
