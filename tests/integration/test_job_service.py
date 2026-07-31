"""Job persistence and the claim/transition logic, against a real database."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DocumentType, JobStatus
from app.core.exceptions import InvalidJobTransitionError, NotFoundError
from app.core.time import utcnow
from app.db.models import ExtractionResult, ProcessingJob
from app.services import job_service
from app.services.file_storage import StoredFile
from tests.conftest import JobFactory

pytestmark = pytest.mark.integration

STALE_AFTER = 600


@pytest.fixture
def stored_file() -> StoredFile:
    return StoredFile(
        stored_filename="abc.pdf",
        storage_path="2026/07/30/abc.pdf",
        size=2048,
        checksum="f" * 64,
    )


class TestCreateDocumentAndJob:
    async def test_both_rows_persist(
        self, db_session: AsyncSession, stored_file: StoredFile
    ) -> None:
        document, job = await job_service.create_document_and_job(
            db_session,
            stored=stored_file,
            original_filename="invoice.pdf",
            content_type="application/pdf",
            requested_document_type=DocumentType.INVOICE,
            max_retries=3,
        )

        assert document.id is not None
        assert job.document_id == document.id
        assert job.status is JobStatus.QUEUED
        assert job.retry_count == 0
        assert document.checksum == stored_file.checksum

    async def test_max_retries_snapshotted_per_job(
        self, db_session: AsyncSession, stored_file: StoredFile
    ) -> None:
        """Changing the global default must not alter in-flight work."""
        _, job = await job_service.create_document_and_job(
            db_session,
            stored=stored_file,
            original_filename="a.pdf",
            content_type="application/pdf",
            requested_document_type=None,
            max_retries=7,
        )
        assert job.max_retries == 7


class TestClaimJob:
    """The idempotency barrier: exactly which rows a worker may take."""

    @pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RETRYING])
    async def test_available_job_is_claimed(
        self, db_session: AsyncSession, job_factory: JobFactory, status: JobStatus
    ) -> None:
        job = await job_factory.create(status=status)

        claimed = await job_service.claim_job(
            db_session, job.id, consumer_name="worker-1", stale_after_seconds=STALE_AFTER
        )

        assert claimed is not None
        assert claimed.status is JobStatus.PROCESSING
        assert claimed.consumer_name == "worker-1"
        assert claimed.started_at is not None

    async def test_completed_job_is_never_reclaimed(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        """A redelivered message for finished work must do nothing."""
        job = await job_factory.create(status=JobStatus.COMPLETED)

        claimed = await job_service.claim_job(
            db_session, job.id, consumer_name="worker-1", stale_after_seconds=STALE_AFTER
        )

        assert claimed is None

    async def test_failed_job_is_not_claimed(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(status=JobStatus.FAILED)
        assert (
            await job_service.claim_job(
                db_session, job.id, consumer_name="w", stale_after_seconds=STALE_AFTER
            )
            is None
        )

    async def test_freshly_processing_job_is_not_stolen(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        """A live worker keeps its job; only presumed-dead owners lose it."""
        job = await job_factory.create(status=JobStatus.PROCESSING, started_at=utcnow())

        claimed = await job_service.claim_job(
            db_session, job.id, consumer_name="worker-2", stale_after_seconds=STALE_AFTER
        )

        assert claimed is None

    async def test_stale_processing_job_is_recovered(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        """How work orphaned by a killed worker gets picked up again."""
        job = await job_factory.create(
            status=JobStatus.PROCESSING,
            started_at=utcnow() - timedelta(seconds=STALE_AFTER + 60),
        )

        claimed = await job_service.claim_job(
            db_session, job.id, consumer_name="worker-2", stale_after_seconds=STALE_AFTER
        )

        assert claimed is not None
        assert claimed.consumer_name == "worker-2"

    async def test_second_claim_of_the_same_job_fails(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        """Duplicate delivery: the first claim wins, the second is a no-op."""
        job = await job_factory.create(status=JobStatus.QUEUED)

        first = await job_service.claim_job(
            db_session, job.id, consumer_name="worker-1", stale_after_seconds=STALE_AFTER
        )
        second = await job_service.claim_job(
            db_session, job.id, consumer_name="worker-2", stale_after_seconds=STALE_AFTER
        )

        assert first is not None
        assert second is None

    async def test_unknown_job_returns_none(self, db_session: AsyncSession) -> None:
        assert (
            await job_service.claim_job(
                db_session, uuid.uuid4(), consumer_name="w", stale_after_seconds=STALE_AFTER
            )
            is None
        )


class TestTerminalTransitions:
    async def test_mark_completed_records_duration(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(
            status=JobStatus.PROCESSING, started_at=utcnow() - timedelta(milliseconds=250)
        )

        await job_service.mark_completed(db_session, job)

        assert job.status is JobStatus.COMPLETED
        assert job.completed_at is not None
        assert job.processing_duration_ms is not None
        assert job.processing_duration_ms >= 250

    async def test_mark_completed_clears_a_previous_error(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        """A job that succeeded on retry must not still show the old failure."""
        job = await job_factory.create(status=JobStatus.PROCESSING, started_at=utcnow())
        job.error_message = "rate limited on attempt 1"

        await job_service.mark_completed(db_session, job)

        assert job.error_message is None

    async def test_mark_retrying_counts_the_attempt(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(status=JobStatus.PROCESSING, started_at=utcnow())

        await job_service.mark_retrying(db_session, job, error_message="LLM timed out")

        assert job.status is JobStatus.RETRYING
        assert job.retry_count == 1
        assert job.attempts_remaining == 2
        assert job.error_message == "LLM timed out"

    async def test_mark_failed_is_terminal(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(status=JobStatus.PROCESSING, started_at=utcnow())

        await job_service.mark_failed(db_session, job, error_message="unsupported file")

        assert job.status is JobStatus.FAILED
        assert job.completed_at is not None

    async def test_completed_job_cannot_be_failed(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(status=JobStatus.COMPLETED)
        with pytest.raises(InvalidJobTransitionError):
            await job_service.mark_failed(db_session, job, error_message="too late")

    async def test_changes_survive_a_fresh_read(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        """Verifies the commit actually happened, not just the in-memory object."""
        job = await job_factory.create(status=JobStatus.PROCESSING, started_at=utcnow())
        await job_service.mark_completed(db_session, job)
        db_session.expunge_all()

        reloaded = await job_service.get_job(db_session, job.id)
        assert reloaded.status is JobStatus.COMPLETED


class TestManualRetry:
    async def test_failed_job_is_requeued_with_a_reset_budget(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(status=JobStatus.FAILED, retry_count=3)
        job.error_message = "gave up"

        await job_service.reset_for_manual_retry(db_session, job)

        assert job.status is JobStatus.QUEUED
        assert job.retry_count == 0
        assert job.error_message is None
        assert job.started_at is None
        assert job.completed_at is None
        assert job.consumer_name is None

    @pytest.mark.parametrize(
        "status", [JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.RETRYING, JobStatus.COMPLETED]
    )
    async def test_only_failed_jobs_are_eligible(
        self, db_session: AsyncSession, job_factory: JobFactory, status: JobStatus
    ) -> None:
        """Prevents a manual retry racing a worker into two live attempts."""
        job = await job_factory.create(status=status)
        with pytest.raises(InvalidJobTransitionError) as error:
            await job_service.reset_for_manual_retry(db_session, job)
        assert error.value.http_status == 409


class TestQueries:
    async def test_get_job_raises_for_unknown_id(self, db_session: AsyncSession) -> None:
        with pytest.raises(NotFoundError):
            await job_service.get_job(db_session, uuid.uuid4())

    async def test_get_document_raises_for_unknown_id(self, db_session: AsyncSession) -> None:
        with pytest.raises(NotFoundError):
            await job_service.get_document(db_session, uuid.uuid4())

    async def test_list_returns_newest_first(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        for index in range(3):
            await job_factory.create(filename=f"doc-{index}.pdf")

        jobs, total = await job_service.list_jobs(db_session, limit=10, offset=0)

        assert total == 3
        assert [job.created_at for job in jobs] == sorted(
            (job.created_at for job in jobs), reverse=True
        )

    async def test_status_filter(self, db_session: AsyncSession, job_factory: JobFactory) -> None:
        await job_factory.create(status=JobStatus.QUEUED)
        await job_factory.create(status=JobStatus.FAILED)
        await job_factory.create(status=JobStatus.FAILED)

        jobs, total = await job_service.list_jobs(
            db_session, status=JobStatus.FAILED, limit=10, offset=0
        )

        assert total == 2
        assert all(job.status is JobStatus.FAILED for job in jobs)

    async def test_pagination_does_not_overlap(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        for index in range(5):
            await job_factory.create(filename=f"doc-{index}.pdf")

        first, total = await job_service.list_jobs(db_session, limit=2, offset=0)
        second, _ = await job_service.list_jobs(db_session, limit=2, offset=2)

        assert total == 5
        assert len(first) == len(second) == 2
        assert {job.id for job in first}.isdisjoint({job.id for job in second})


class TestSchemaGuarantees:
    async def test_a_job_cannot_have_two_results(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        """The database backstop behind the worker's idempotency check."""
        job = await job_factory.create(status=JobStatus.PROCESSING)

        def build() -> ExtractionResult:
            return ExtractionResult(
                job_id=job.id,
                detected_document_type=DocumentType.INVOICE,
                extracted_data={"total": 42},
                model_provider="fake",
                model_name="fake-1",
                prompt_version="v1",
            )

        db_session.add(build())
        await db_session.commit()

        db_session.add(build())
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_deleting_a_document_cascades(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create()
        document = await job_service.get_document(db_session, job.document_id)

        await db_session.delete(document)
        await db_session.commit()

        remaining = await db_session.execute(
            select(ProcessingJob).where(ProcessingJob.id == job.id)
        )
        assert remaining.scalar_one_or_none() is None

    async def test_enums_stored_as_their_values(
        self, db_session: AsyncSession, job_factory: JobFactory
    ) -> None:
        """The columns must read as 'support_ticket', not 'SUPPORT_TICKET'.

        Bypasses the ORM so the assertion is about what is actually on disk;
        a psql user and the API should see the same spelling.
        """
        await job_factory.create(
            status=JobStatus.RETRYING, requested_document_type=DocumentType.SUPPORT_TICKET
        )

        rows = await db_session.execute(
            text(
                "SELECT d.requested_document_type::text AS doc_type, j.status::text AS status "
                "FROM documents d JOIN processing_jobs j ON j.document_id = d.id"
            )
        )
        assert rows.mappings().one() == {"doc_type": "support_ticket", "status": "retrying"}
