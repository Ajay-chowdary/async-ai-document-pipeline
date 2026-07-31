"""The worker: claiming, extraction, retries, idempotency and recovery.

Assertions re-read the job from the database rather than trusting the ORM
instance the test is holding. The processor commits and rolls back on the same
session, so an in-memory object can be stale — and what matters is what was
actually persisted, since that is all a restarted worker or the API will see.
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DocumentType, JobStatus
from app.core.exceptions import LLMRateLimitError, LLMTimeoutError, UnsupportedFileTypeError
from app.core.time import utcnow
from app.db.models import ExtractionResult, ProcessingJob
from app.llm.base import FakeLLMProvider
from app.schemas.events import DocumentProcessingEvent
from app.services import job_service
from app.services.queue import DeliveredEvent, RedisQueue
from app.worker.consumer import StreamConsumer, build_consumer_name
from app.worker.processor import DocumentProcessor, ProcessingOutcome
from tests.conftest import JobFactory

pytestmark = pytest.mark.integration

INVOICE_TEXT = b"INVOICE\nVendor: Northwind Ltd\nAmount due: 1284.50 EUR\nSubtotal: 1200.00\n"

Reload = Callable[[ProcessingJob], Awaitable[ProcessingJob]]


def event_for(job: ProcessingJob, *, attempt: int = 0) -> DocumentProcessingEvent:
    return DocumentProcessingEvent(
        job_id=job.id,
        document_id=job.document_id,
        storage_path=job.document.storage_path,
        requested_document_type=job.document.requested_document_type,
        attempt=attempt,
        correlation_id="corr-test",
    )


def delivery(job: ProcessingJob, *, attempt: int = 0, delivery_count: int = 1) -> DeliveredEvent:
    return DeliveredEvent(
        message_id=f"0-{attempt + 1}",
        event=event_for(job, attempt=attempt),
        delivery_count=delivery_count,
    )


class TestHappyPath:
    async def test_job_completes(
        self, processor: DocumentProcessor, job_factory: JobFactory, reload_job: Reload
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)

        outcome = await processor.process(delivery(job))

        assert outcome is ProcessingOutcome.COMPLETED
        assert (await reload_job(job)).status is JobStatus.COMPLETED

    async def test_result_is_persisted(
        self, processor: DocumentProcessor, job_factory: JobFactory, db_session: AsyncSession
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)

        await processor.process(delivery(job))

        result = (
            await db_session.execute(
                select(ExtractionResult).where(ExtractionResult.job_id == job.id)
            )
        ).scalar_one()
        assert result.extracted_data["vendor_name"] == "Northwind Ltd"
        assert result.extracted_data["confidence_score"] == 0.75
        assert result.model_provider == "fake"
        assert result.prompt_version == "fake-v1"
        assert result.input_tokens is not None
        assert result.confidence_score == 0.75

    async def test_duration_and_owner_are_recorded(
        self, processor: DocumentProcessor, job_factory: JobFactory, reload_job: Reload
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)

        await processor.process(delivery(job))

        stored = await reload_job(job)
        assert stored.processing_duration_ms is not None
        assert stored.completed_at is not None
        assert stored.consumer_name == "worker-test-1"

    async def test_type_is_classified_when_not_supplied(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        fake_llm: FakeLLMProvider,
        reload_job: Reload,
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT, requested_document_type=None)

        await processor.process(delivery(job))

        stored = await reload_job(job)
        assert stored.result is not None
        assert stored.result.detected_document_type is DocumentType.INVOICE
        assert fake_llm.calls == 2  # classify, then extract

    async def test_classification_is_skipped_when_type_is_supplied(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        fake_llm: FakeLLMProvider,
        reload_job: Reload,
    ) -> None:
        """Halves the model spend on the common path."""
        job = await job_factory.create(
            content=INVOICE_TEXT, requested_document_type=DocumentType.RESUME
        )

        await processor.process(delivery(job))

        stored = await reload_job(job)
        assert fake_llm.calls == 1
        assert stored.result is not None
        assert stored.result.detected_document_type is DocumentType.RESUME

    async def test_raw_text_is_stored_truncated(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        reload_job: Reload,
        settings,
    ) -> None:
        job = await job_factory.create(content=b"word " * 20_000)

        await processor.process(delivery(job))

        stored = await reload_job(job)
        assert stored.result is not None
        assert stored.result.raw_text is not None
        assert len(stored.result.raw_text) <= settings.raw_text_store_max_chars


class TestIdempotency:
    async def test_completed_job_is_not_reprocessed(
        self, processor: DocumentProcessor, job_factory: JobFactory, fake_llm: FakeLLMProvider
    ) -> None:
        """A redelivered message for finished work must cost nothing."""
        job = await job_factory.create(content=INVOICE_TEXT)
        redelivered = delivery(job)
        await processor.process(redelivered)
        calls_after_first = fake_llm.calls

        outcome = await processor.process(redelivered)

        assert outcome is ProcessingOutcome.SKIPPED
        assert fake_llm.calls == calls_after_first

    async def test_only_one_result_is_ever_written(
        self, processor: DocumentProcessor, job_factory: JobFactory, db_session: AsyncSession
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)
        job_id = job.id
        # One message, delivered three times: exactly what at-least-once
        # delivery does when acknowledgements are lost.
        redelivered = delivery(job)

        for _ in range(3):
            await processor.process(redelivered)

        db_session.expire_all()
        results = (
            (
                await db_session.execute(
                    select(ExtractionResult).where(ExtractionResult.job_id == job_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(results) == 1

    async def test_job_owned_by_a_live_worker_is_skipped(
        self, processor: DocumentProcessor, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(status=JobStatus.PROCESSING, started_at=utcnow())

        assert await processor.process(delivery(job)) is ProcessingOutcome.SKIPPED


class TestRetries:
    async def test_transient_failure_schedules_a_retry(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        fake_llm: FakeLLMProvider,
        queue: RedisQueue,
        reload_job: Reload,
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)
        fake_llm.failures = [LLMRateLimitError("slow down")]

        outcome = await processor.process(delivery(job))

        stored = await reload_job(job)
        assert outcome is ProcessingOutcome.RETRY_SCHEDULED
        assert stored.status is JobStatus.RETRYING
        assert stored.retry_count == 1
        assert (await queue.depth()).scheduled_retries == 1

    async def test_scheduled_retry_carries_an_incremented_attempt(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        fake_llm: FakeLLMProvider,
        queue: RedisQueue,
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)
        fake_llm.failures = [LLMTimeoutError()]

        await processor.process(delivery(job))

        # The backoff is real, so nothing is due yet; inspect the parked entry.
        parked = await queue.client.zrange(queue.retry_key, 0, -1)
        scheduled = DocumentProcessingEvent.model_validate_json(parked[0])
        assert scheduled.attempt == 1
        assert scheduled.job_id == job.id
        assert scheduled.correlation_id == "corr-test"

    async def test_retried_job_can_then_succeed(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        fake_llm: FakeLLMProvider,
        reload_job: Reload,
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)
        fake_llm.failures = [LLMRateLimitError()]
        await processor.process(delivery(job))

        outcome = await processor.process(delivery(job, attempt=1))

        stored = await reload_job(job)
        assert outcome is ProcessingOutcome.COMPLETED
        assert stored.status is JobStatus.COMPLETED
        assert stored.error_message is None

    async def test_exhausted_budget_fails_permanently(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        fake_llm: FakeLLMProvider,
        reload_job: Reload,
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT, retry_count=3, max_retries=3)
        fake_llm.failures = [LLMRateLimitError("still limited")]

        outcome = await processor.process(delivery(job))

        stored = await reload_job(job)
        assert outcome is ProcessingOutcome.FAILED
        assert stored.status is JobStatus.FAILED
        assert "llm_rate_limited" in (stored.error_message or "")

    async def test_repeated_failures_walk_to_failed(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        fake_llm: FakeLLMProvider,
        reload_job: Reload,
    ) -> None:
        """The full lifecycle: three retries, then permanent failure."""
        job = await job_factory.create(content=INVOICE_TEXT, max_retries=3)
        fake_llm.failures = [LLMTimeoutError() for _ in range(4)]

        outcomes = [await processor.process(delivery(job, attempt=n)) for n in range(4)]

        stored = await reload_job(job)
        assert outcomes == [
            ProcessingOutcome.RETRY_SCHEDULED,
            ProcessingOutcome.RETRY_SCHEDULED,
            ProcessingOutcome.RETRY_SCHEDULED,
            ProcessingOutcome.FAILED,
        ]
        assert stored.status is JobStatus.FAILED
        assert stored.retry_count == 3


class TestPermanentFailures:
    async def test_empty_document_fails_without_retrying(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        queue: RedisQueue,
        reload_job: Reload,
    ) -> None:
        job = await job_factory.create(content=b"   \n\n  \n")

        outcome = await processor.process(delivery(job))

        stored = await reload_job(job)
        assert outcome is ProcessingOutcome.FAILED
        assert stored.retry_count == 0
        assert (await queue.depth()).scheduled_retries == 0
        assert "empty_document" in (stored.error_message or "")

    async def test_missing_file_fails_without_retrying(
        self, processor: DocumentProcessor, job_factory: JobFactory, reload_job: Reload
    ) -> None:
        job = await job_factory.create(write_file=False)

        outcome = await processor.process(delivery(job))

        assert outcome is ProcessingOutcome.FAILED
        assert (await reload_job(job)).retry_count == 0

    async def test_unknown_exception_is_not_retried(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        fake_llm: FakeLLMProvider,
        reload_job: Reload,
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)
        fake_llm.failures = [RuntimeError("something unexpected")]

        outcome = await processor.process(delivery(job))

        assert outcome is ProcessingOutcome.FAILED
        assert "RuntimeError" in ((await reload_job(job)).error_message or "")

    async def test_unsupported_extension_fails(
        self, processor: DocumentProcessor, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(extension=".rtf", content=b"{\\rtf1}")

        assert await processor.process(delivery(job)) is ProcessingOutcome.FAILED


class TestDeadLettering:
    async def test_repeatedly_redelivered_message_is_failed(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        reload_job: Reload,
        settings,
    ) -> None:
        """A message that keeps killing whichever worker takes it must stop
        cycling through the fleet."""
        job = await job_factory.create(content=INVOICE_TEXT)

        outcome = await processor.process(
            delivery(job, delivery_count=settings.max_delivery_count + 1)
        )

        stored = await reload_job(job)
        assert outcome is ProcessingOutcome.DEAD_LETTERED
        assert stored.status is JobStatus.FAILED
        assert "redelivered" in (stored.error_message or "")

    async def test_within_the_limit_is_processed_normally(
        self, processor: DocumentProcessor, job_factory: JobFactory, settings
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)

        outcome = await processor.process(delivery(job, delivery_count=settings.max_delivery_count))

        assert outcome is ProcessingOutcome.COMPLETED


class TestConsumerLoop:
    async def test_end_to_end_through_the_stream(
        self,
        consumer: StreamConsumer,
        queue: RedisQueue,
        job_factory: JobFactory,
        reload_job: Reload,
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)
        await queue.publish(event_for(job))

        delivered = await queue.read(consumer_name="worker-test-1")
        outcome = await consumer.handle(delivered[0])

        assert outcome is ProcessingOutcome.COMPLETED
        assert (await reload_job(job)).status is JobStatus.COMPLETED

    async def test_message_is_acked_after_processing(
        self, consumer: StreamConsumer, queue: RedisQueue, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)
        await queue.publish(event_for(job))
        delivered = await queue.read(consumer_name="worker-test-1")

        await consumer.handle(delivered[0])

        assert (await queue.depth()).pending == 0

    async def test_failed_jobs_are_still_acked(
        self,
        consumer: StreamConsumer,
        queue: RedisQueue,
        job_factory: JobFactory,
        reload_job: Reload,
    ) -> None:
        """A permanently failed job is a durable outcome, not an open message."""
        job = await job_factory.create(content=b"  \n ")
        await queue.publish(event_for(job))
        delivered = await queue.read(consumer_name="worker-test-1")

        await consumer.handle(delivered[0])

        assert (await queue.depth()).pending == 0
        assert (await reload_job(job)).status is JobStatus.FAILED

    async def test_run_loop_stops_when_asked(
        self,
        consumer: StreamConsumer,
        queue: RedisQueue,
        job_factory: JobFactory,
        reload_job: Reload,
    ) -> None:
        """Graceful shutdown: the loop exits without abandoning work."""
        job = await job_factory.create(content=INVOICE_TEXT)
        await queue.publish(event_for(job))

        shutdown = asyncio.Event()
        task = asyncio.create_task(consumer.run(shutdown))
        await asyncio.sleep(0.3)
        shutdown.set()
        await asyncio.wait_for(task, timeout=5)

        assert (await reload_job(job)).status is JobStatus.COMPLETED
        assert consumer.processed == 1

    async def test_crash_leaves_the_message_pending(
        self,
        consumer: StreamConsumer,
        queue: RedisQueue,
        job_factory: JobFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The invariant recovery depends on: no ack means it comes back."""
        job = await job_factory.create(content=INVOICE_TEXT)
        await queue.publish(event_for(job))
        delivered = await queue.read(consumer_name="worker-test-1")

        async def explode(_delivered: DeliveredEvent) -> ProcessingOutcome:
            raise RuntimeError("worker died mid-job")

        monkeypatch.setattr(consumer._processor, "process", explode)
        outcome = await consumer.handle(delivered[0])

        assert outcome is None
        assert (await queue.depth()).pending == 1


class TestConsumerNaming:
    def test_names_are_unique_per_process(self) -> None:
        """Two workers sharing a name would claim each other's in-flight work."""
        assert build_consumer_name() != build_consumer_name()

    def test_name_fits_the_database_column(self) -> None:
        assert len(build_consumer_name()) <= 128


class TestManualRetryFlow:
    async def test_failed_job_reprocesses_after_a_manual_retry(
        self,
        processor: DocumentProcessor,
        job_factory: JobFactory,
        fake_llm: FakeLLMProvider,
        db_session: AsyncSession,
        reload_job: Reload,
    ) -> None:
        job = await job_factory.create(content=INVOICE_TEXT)
        fake_llm.failures = [UnsupportedFileTypeError("permanently broken")]
        await processor.process(delivery(job))
        failed = await reload_job(job)
        assert failed.status is JobStatus.FAILED

        await job_service.reset_for_manual_retry(db_session, failed)
        outcome = await processor.process(delivery(job))

        stored = await reload_job(job)
        assert outcome is ProcessingOutcome.COMPLETED
        assert stored.retry_count == 0
