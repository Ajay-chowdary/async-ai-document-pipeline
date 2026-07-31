"""Job listing, detail and operational endpoints."""

import uuid

import pytest
from httpx import AsyncClient

from app.core.enums import DocumentType, JobStatus
from app.db.models import ExtractionResult
from tests.conftest import JobFactory

pytestmark = pytest.mark.integration

JOBS_URL = "/api/v1/jobs"


class TestListJobs:
    async def test_empty_list(self, api_client: AsyncClient) -> None:
        body = (await api_client.get(JOBS_URL)).json()
        assert body == {"items": [], "total": 0, "limit": 20, "offset": 0}

    async def test_returns_newest_first(
        self, api_client: AsyncClient, job_factory: JobFactory
    ) -> None:
        for index in range(3):
            await job_factory.create(filename=f"doc-{index}.pdf")

        items = (await api_client.get(JOBS_URL)).json()["items"]

        assert len(items) == 3
        assert [item["created_at"] for item in items] == sorted(
            (item["created_at"] for item in items), reverse=True
        )

    async def test_includes_the_joined_filename(
        self, api_client: AsyncClient, job_factory: JobFactory
    ) -> None:
        """The dashboard renders the table without a request per row."""
        await job_factory.create(filename="quarterly-invoice.pdf")

        item = (await api_client.get(JOBS_URL)).json()["items"][0]

        assert item["original_filename"] == "quarterly-invoice.pdf"

    @pytest.mark.parametrize(
        "status", [JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.FAILED, JobStatus.COMPLETED]
    )
    async def test_status_filter(
        self, api_client: AsyncClient, job_factory: JobFactory, status: JobStatus
    ) -> None:
        for value in JobStatus:
            await job_factory.create(status=value)

        body = (await api_client.get(JOBS_URL, params={"status": status.value})).json()

        assert body["total"] == 1
        assert body["items"][0]["status"] == status.value

    async def test_unknown_status_gets_422(self, api_client: AsyncClient) -> None:
        response = await api_client.get(JOBS_URL, params={"status": "exploded"})
        assert response.status_code == 422

    async def test_pagination_reports_the_full_total(
        self, api_client: AsyncClient, job_factory: JobFactory
    ) -> None:
        for index in range(5):
            await job_factory.create(filename=f"doc-{index}.pdf")

        body = (await api_client.get(JOBS_URL, params={"limit": 2, "offset": 0})).json()

        assert len(body["items"]) == 2
        assert body["total"] == 5

    async def test_pages_do_not_overlap(
        self, api_client: AsyncClient, job_factory: JobFactory
    ) -> None:
        for index in range(4):
            await job_factory.create(filename=f"doc-{index}.pdf")

        first = (await api_client.get(JOBS_URL, params={"limit": 2, "offset": 0})).json()
        second = (await api_client.get(JOBS_URL, params={"limit": 2, "offset": 2})).json()

        ids = {item["id"] for item in first["items"]}
        assert ids.isdisjoint({item["id"] for item in second["items"]})

    @pytest.mark.parametrize(("limit", "offset"), [(0, 0), (101, 0), (10, -1)])
    async def test_out_of_range_pagination_rejected(
        self, api_client: AsyncClient, limit: int, offset: int
    ) -> None:
        """A client cannot ask for the whole table in one query."""
        response = await api_client.get(JOBS_URL, params={"limit": limit, "offset": offset})
        assert response.status_code == 422


class TestGetJob:
    async def test_returns_job_state(
        self, api_client: AsyncClient, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(status=JobStatus.QUEUED)

        body = (await api_client.get(f"{JOBS_URL}/{job.id}")).json()

        assert body["id"] == str(job.id)
        assert body["status"] == "queued"
        assert body["result"] is None
        assert body["is_terminal"] is False

    async def test_terminal_flag_tells_the_dashboard_to_stop_polling(
        self, api_client: AsyncClient, job_factory: JobFactory
    ) -> None:
        job = await job_factory.create(status=JobStatus.COMPLETED)
        assert (await api_client.get(f"{JOBS_URL}/{job.id}")).json()["is_terminal"] is True

    async def test_includes_the_extraction_result(
        self, api_client: AsyncClient, job_factory: JobFactory, db_session
    ) -> None:
        job = await job_factory.create(status=JobStatus.COMPLETED)
        db_session.add(
            ExtractionResult(
                job_id=job.id,
                detected_document_type=DocumentType.INVOICE,
                extracted_data={"vendor_name": "ACME", "total": 42.0},
                model_provider="fake",
                model_name="fake-1",
                prompt_version="v1",
                input_tokens=100,
                output_tokens=25,
                confidence_score=0.91,
            )
        )
        await db_session.commit()

        body = (await api_client.get(f"{JOBS_URL}/{job.id}")).json()

        assert body["detected_document_type"] == "invoice"
        assert body["result"]["extracted_data"] == {"vendor_name": "ACME", "total": 42.0}
        assert body["result"]["confidence_score"] == 0.91
        assert body["result"]["input_tokens"] == 100

    async def test_failure_message_is_exposed(
        self, api_client: AsyncClient, job_factory: JobFactory, db_session
    ) -> None:
        job = await job_factory.create(status=JobStatus.FAILED)
        job.error_message = "No text could be extracted from the document."
        await db_session.commit()

        body = (await api_client.get(f"{JOBS_URL}/{job.id}")).json()

        assert body["error_message"] == "No text could be extracted from the document."

    async def test_unknown_id_gets_404(self, api_client: AsyncClient) -> None:
        response = await api_client.get(f"{JOBS_URL}/{uuid.uuid4()}")
        assert response.status_code == 404


class TestOperationalEndpoints:
    async def test_health_is_ok(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_ready_reports_dependencies(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "checks": {"database": True, "redis": True},
        }

    async def test_openapi_is_served(self, api_client: AsyncClient) -> None:
        assert (await api_client.get("/openapi.json")).status_code == 200


class TestErrorContract:
    async def test_unknown_route_uses_the_standard_envelope(self, api_client: AsyncClient) -> None:
        body = (await api_client.get("/api/v1/nope")).json()
        assert set(body) == {"error"}
        assert "code" in body["error"]

    async def test_correlation_id_is_echoed(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/health", headers={"X-Correlation-ID": "trace-123"})
        assert response.headers["X-Correlation-ID"] == "trace-123"

    async def test_correlation_id_is_generated_when_absent(self, api_client: AsyncClient) -> None:
        response = await api_client.get("/health")
        assert uuid.UUID(response.headers["X-Correlation-ID"])

    async def test_errors_carry_the_correlation_id(self, api_client: AsyncClient) -> None:
        """The bridge between what the client sees and what the operator greps."""
        response = await api_client.get(
            f"{JOBS_URL}/{uuid.uuid4()}", headers={"X-Correlation-ID": "trace-456"}
        )
        assert response.json()["error"]["correlation_id"] == "trace-456"
