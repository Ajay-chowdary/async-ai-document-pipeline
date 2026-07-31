"""``GET /metrics-summary`` aggregates job counts and Redis depth."""

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import JobStatus
from app.core.time import utcnow
from tests.conftest import JobFactory

pytestmark = pytest.mark.integration

URL = "/metrics-summary"


class TestMetricsSummary:
    async def test_empty_system(self, api_client: AsyncClient) -> None:
        body = (await api_client.get(URL)).json()

        assert body["total_jobs"] == 0
        assert body["queued"] == 0
        assert body["processing"] == 0
        assert body["completed"] == 0
        assert body["failed"] == 0
        assert body["approximate_success_rate"] is None
        assert body["average_processing_duration_ms"] is None
        assert body["jobs_last_24_hours"] == 0
        assert body["stream_length"] == 0
        assert body["pending"] == 0
        assert body["scheduled_retries"] == 0

    async def test_counts_by_status(self, api_client: AsyncClient, job_factory: JobFactory) -> None:
        await job_factory.create(status=JobStatus.QUEUED)
        await job_factory.create(status=JobStatus.PROCESSING)
        await job_factory.create(status=JobStatus.RETRYING)
        await job_factory.create(status=JobStatus.COMPLETED)
        await job_factory.create(status=JobStatus.FAILED)

        body = (await api_client.get(URL)).json()

        assert body["total_jobs"] == 5
        assert body["queued"] == 1
        assert body["processing"] == 1
        assert body["retrying"] == 1
        assert body["completed"] == 1
        assert body["failed"] == 1
        assert body["approximate_success_rate"] == 0.5
        assert body["jobs_last_24_hours"] == 5

    async def test_average_duration_and_success_rate(
        self, api_client: AsyncClient, job_factory: JobFactory, db_session: AsyncSession
    ) -> None:
        first = await job_factory.create(status=JobStatus.COMPLETED)
        second = await job_factory.create(status=JobStatus.COMPLETED)
        await job_factory.create(status=JobStatus.FAILED)

        first.processing_duration_ms = 100
        second.processing_duration_ms = 300
        await db_session.commit()

        body = (await api_client.get(URL)).json()

        assert body["average_processing_duration_ms"] == 200.0
        assert body["approximate_success_rate"] == pytest.approx(2 / 3, rel=1e-3)

    async def test_jobs_outside_24h_window_excluded_from_recent_count(
        self, api_client: AsyncClient, job_factory: JobFactory, db_session: AsyncSession
    ) -> None:
        recent = await job_factory.create()
        old = await job_factory.create()
        old.created_at = utcnow() - timedelta(hours=25)
        await db_session.commit()

        body = (await api_client.get(URL)).json()

        assert body["total_jobs"] == 2
        assert body["jobs_last_24_hours"] == 1
        assert recent.id is not None
