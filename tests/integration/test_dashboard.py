"""Dashboard HTML pages and static assets."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DocumentType, JobStatus
from app.db.models import ExtractionResult
from tests.conftest import JobFactory

pytestmark = pytest.mark.integration


class TestDashboardPages:
    async def test_home_renders(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "DocPipeline" in response.text
        assert 'id="upload-form"' in response.text
        assert 'id="jobs-table"' in response.text

    async def test_failed_job_detail_shows_retry(
        self, api_client: AsyncClient, job_factory: JobFactory, db_session: AsyncSession
    ) -> None:
        job = await job_factory.create(status=JobStatus.FAILED, filename="broken.txt")
        job.error_message = "synthetic failure"
        await db_session.commit()

        response = await api_client.get(f"/jobs/{job.id}")

        assert response.status_code == 200
        assert str(job.id) in response.text
        assert "broken.txt" in response.text
        assert "synthetic failure" in response.text
        assert 'id="retry-button"' in response.text

    async def test_completed_job_detail_pretty_prints_json(
        self, api_client: AsyncClient, job_factory: JobFactory, db_session: AsyncSession
    ) -> None:
        job = await job_factory.create(status=JobStatus.COMPLETED, filename="done.txt")
        db_session.add(
            ExtractionResult(
                job_id=job.id,
                detected_document_type=DocumentType.INVOICE,
                extracted_data={"vendor_name": "Northwind", "total": 12.0},
                model_provider="fake",
                model_name="fake-extractor-1",
                prompt_version="fake-v1",
                confidence_score=0.75,
            )
        )
        await db_session.commit()

        response = await api_client.get(f"/jobs/{job.id}")

        assert response.status_code == 200
        assert "Northwind" in response.text
        assert "extracted_data" in response.text

    async def test_static_css_is_served(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/static/dashboard.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]
        assert "--accent" in response.text

    async def test_static_js_is_served(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/static/dashboard.js")
        assert response.status_code == 200
        assert "refreshMetrics" in response.text
