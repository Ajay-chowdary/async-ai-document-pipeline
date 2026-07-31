"""Upload and document endpoints, end to end over HTTP."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import JobStatus
from app.db.models import Document, ProcessingJob

pytestmark = pytest.mark.integration

UPLOAD_URL = "/api/v1/documents"


class TestUploadSuccess:
    async def test_returns_202_with_job_details(
        self, api_client: AsyncClient, pdf_bytes: bytes
    ) -> None:
        response = await api_client.post(
            UPLOAD_URL, files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")}
        )

        assert response.status_code == 202
        body = response.json()
        assert uuid.UUID(body["document_id"])
        assert uuid.UUID(body["job_id"])
        assert body["status"] == JobStatus.QUEUED.value
        assert body["job_id"] in body["status_url"]

    async def test_creates_a_queued_job(
        self, api_client: AsyncClient, db_session: AsyncSession, pdf_bytes: bytes
    ) -> None:
        response = await api_client.post(
            UPLOAD_URL, files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")}
        )

        job = await db_session.get(ProcessingJob, uuid.UUID(response.json()["job_id"]))
        assert job is not None
        assert job.status is JobStatus.QUEUED
        assert job.retry_count == 0
        assert job.started_at is None

    async def test_persists_file_metadata_and_checksum(
        self, api_client: AsyncClient, db_session: AsyncSession, pdf_bytes: bytes
    ) -> None:
        response = await api_client.post(
            UPLOAD_URL, files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")}
        )

        document = await db_session.get(Document, uuid.UUID(response.json()["document_id"]))
        assert document is not None
        assert document.original_filename == "invoice.pdf"
        assert document.file_size == len(pdf_bytes)
        assert len(document.checksum) == 64

    async def test_file_is_written_to_storage(
        self, api_client: AsyncClient, db_session: AsyncSession, storage, pdf_bytes: bytes
    ) -> None:
        response = await api_client.post(
            UPLOAD_URL, files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")}
        )

        document = await db_session.get(Document, uuid.UUID(response.json()["document_id"]))
        assert document is not None
        assert await storage.read(document.storage_path) == pdf_bytes

    async def test_explicit_document_type_is_recorded(
        self, api_client: AsyncClient, db_session: AsyncSession, pdf_bytes: bytes
    ) -> None:
        """When the client names the type, the worker skips classification."""
        response = await api_client.post(
            UPLOAD_URL,
            files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")},
            data={"document_type": "invoice"},
        )

        document = await db_session.get(Document, uuid.UUID(response.json()["document_id"]))
        assert document is not None
        assert document.requested_document_type is not None
        assert document.requested_document_type.value == "invoice"

    async def test_type_is_null_when_omitted(
        self, api_client: AsyncClient, db_session: AsyncSession, txt_bytes: bytes
    ) -> None:
        response = await api_client.post(
            UPLOAD_URL, files={"file": ("notes.txt", txt_bytes, "text/plain")}
        )

        document = await db_session.get(Document, uuid.UUID(response.json()["document_id"]))
        assert document is not None
        assert document.requested_document_type is None

    @pytest.mark.parametrize(
        ("filename", "content_type", "payload_fixture"),
        [
            ("a.pdf", "application/pdf", "pdf_bytes"),
            ("a.txt", "text/plain", "txt_bytes"),
            ("a.docx", "application/octet-stream", "docx_bytes"),
        ],
    )
    async def test_every_supported_format_accepted(
        self,
        api_client: AsyncClient,
        request: pytest.FixtureRequest,
        filename: str,
        content_type: str,
        payload_fixture: str,
    ) -> None:
        payload = request.getfixturevalue(payload_fixture)
        response = await api_client.post(
            UPLOAD_URL, files={"file": (filename, payload, content_type)}
        )
        assert response.status_code == 202

    async def test_filename_is_sanitized_before_storage(
        self, api_client: AsyncClient, db_session: AsyncSession, pdf_bytes: bytes
    ) -> None:
        response = await api_client.post(
            UPLOAD_URL,
            files={"file": ("../../../etc/passwd.pdf", pdf_bytes, "application/pdf")},
        )

        document = await db_session.get(Document, uuid.UUID(response.json()["document_id"]))
        assert document is not None
        assert document.original_filename == "passwd.pdf"
        assert ".." not in document.storage_path


class TestUploadRejection:
    async def _count_rows(self, session: AsyncSession) -> int:
        return (await session.execute(select(func.count()).select_from(Document))).scalar_one()

    async def test_disallowed_extension_gets_415(self, api_client: AsyncClient) -> None:
        response = await api_client.post(
            UPLOAD_URL, files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")}
        )

        assert response.status_code == 415
        assert response.json()["error"]["code"] == "unsupported_file_type"

    async def test_disguised_executable_gets_415(self, api_client: AsyncClient) -> None:
        """Correct extension, correct MIME, wrong content."""
        response = await api_client.post(
            UPLOAD_URL, files={"file": ("invoice.pdf", b"MZ\x90\x00\x03", "application/pdf")}
        )
        assert response.status_code == 415

    async def test_mismatched_content_type_gets_415(
        self, api_client: AsyncClient, pdf_bytes: bytes
    ) -> None:
        response = await api_client.post(
            UPLOAD_URL, files={"file": ("invoice.pdf", pdf_bytes, "image/png")}
        )
        assert response.status_code == 415

    async def test_oversized_upload_gets_413(
        self, api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import get_settings

        monkeypatch.setenv("MAX_UPLOAD_BYTES", "512")
        get_settings.cache_clear()

        response = await api_client.post(
            UPLOAD_URL,
            files={"file": ("big.pdf", b"%PDF-" + b"0" * 4096, "application/pdf")},
        )

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"

    async def test_empty_file_rejected(self, api_client: AsyncClient) -> None:
        response = await api_client.post(
            UPLOAD_URL, files={"file": ("empty.pdf", b"", "application/pdf")}
        )
        assert response.status_code == 415

    async def test_unknown_document_type_gets_422(
        self, api_client: AsyncClient, pdf_bytes: bytes
    ) -> None:
        response = await api_client.post(
            UPLOAD_URL,
            files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")},
            data={"document_type": "not_a_real_type"},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "request_validation_failed"

    async def test_missing_file_gets_422(self, api_client: AsyncClient) -> None:
        assert (await api_client.post(UPLOAD_URL, data={})).status_code == 422

    async def test_rejected_upload_writes_nothing(
        self, api_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Validation runs before any row or file is created."""
        before = await self._count_rows(db_session)

        await api_client.post(
            UPLOAD_URL, files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")}
        )

        assert await self._count_rows(db_session) == before


class TestGetDocument:
    async def test_returns_metadata(self, api_client: AsyncClient, pdf_bytes: bytes) -> None:
        upload = await api_client.post(
            UPLOAD_URL, files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")}
        )
        document_id = upload.json()["document_id"]

        response = await api_client.get(f"{UPLOAD_URL}/{document_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == document_id
        assert body["original_filename"] == "invoice.pdf"
        assert body["file_size"] == len(pdf_bytes)

    async def test_does_not_leak_internal_placement(
        self, api_client: AsyncClient, pdf_bytes: bytes
    ) -> None:
        """Clients get metadata, never anything resembling a path."""
        upload = await api_client.post(
            UPLOAD_URL, files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")}
        )
        body = (await api_client.get(f"{UPLOAD_URL}/{upload.json()['document_id']}")).json()

        assert "storage_path" not in body
        assert "stored_filename" not in body

    async def test_unknown_id_gets_404(self, api_client: AsyncClient) -> None:
        response = await api_client.get(f"{UPLOAD_URL}/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_malformed_id_gets_422(self, api_client: AsyncClient) -> None:
        assert (await api_client.get(f"{UPLOAD_URL}/not-a-uuid")).status_code == 422
