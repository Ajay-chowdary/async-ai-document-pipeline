"""Event serialisation and the wire contract."""

import json
import uuid

import pytest
from pydantic import ValidationError

from app.core.enums import DocumentType, EventType
from app.schemas.events import EVENT_VERSION, PAYLOAD_FIELD, DocumentProcessingEvent


@pytest.fixture
def event() -> DocumentProcessingEvent:
    return DocumentProcessingEvent(
        job_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        storage_path="2026/07/30/abc.pdf",
        requested_document_type=DocumentType.INVOICE,
        correlation_id="corr-1",
    )


class TestContract:
    def test_defaults(self, event: DocumentProcessingEvent) -> None:
        assert event.event_type is EventType.DOCUMENT_PROCESSING_REQUESTED
        assert event.event_version == EVENT_VERSION
        assert event.attempt == 0
        assert event.created_at.tzinfo is not None

    def test_every_documented_field_is_present(self, event: DocumentProcessingEvent) -> None:
        payload = json.loads(event.model_dump_json())
        assert set(payload) == {
            "event_id",
            "event_type",
            "event_version",
            "job_id",
            "document_id",
            "storage_path",
            "requested_document_type",
            "attempt",
            "created_at",
            "correlation_id",
        }

    def test_document_type_is_optional(self) -> None:
        event = DocumentProcessingEvent(
            job_id=uuid.uuid4(), document_id=uuid.uuid4(), storage_path="a.pdf"
        )
        assert event.requested_document_type is None

    def test_required_fields_enforced(self) -> None:
        with pytest.raises(ValidationError):
            DocumentProcessingEvent(job_id=uuid.uuid4())  # type: ignore[call-arg]

    def test_carries_no_document_content(self, event: DocumentProcessingEvent) -> None:
        """The queue holds identifiers only, never the document itself."""
        payload = json.loads(event.model_dump_json())
        assert "raw_text" not in payload
        assert "content" not in payload
        assert "data" not in payload

    def test_is_immutable(self, event: DocumentProcessingEvent) -> None:
        with pytest.raises(ValidationError):
            event.attempt = 5  # type: ignore[misc]


class TestRoundTrip:
    def test_json_round_trip_preserves_everything(self, event: DocumentProcessingEvent) -> None:
        restored = DocumentProcessingEvent.model_validate_json(event.model_dump_json())
        assert restored == event

    def test_stream_fields_are_a_single_json_payload(self, event: DocumentProcessingEvent) -> None:
        fields = event.to_stream_fields()
        assert set(fields) == {PAYLOAD_FIELD}
        assert DocumentProcessingEvent.model_validate_json(fields[PAYLOAD_FIELD]) == event

    def test_null_survives_the_round_trip(self) -> None:
        """Flattened key/value fields would turn this into the string 'None'."""
        event = DocumentProcessingEvent(
            job_id=uuid.uuid4(), document_id=uuid.uuid4(), storage_path="a.pdf"
        )
        restored = DocumentProcessingEvent.model_validate_json(event.model_dump_json())
        assert restored.requested_document_type is None


class TestNextAttempt:
    def test_increments_the_attempt(self, event: DocumentProcessingEvent) -> None:
        assert event.next_attempt().attempt == 1
        assert event.next_attempt().next_attempt().attempt == 2

    def test_gets_a_new_event_id(self, event: DocumentProcessingEvent) -> None:
        """A retry is a new message; sharing an ID makes the logs ambiguous."""
        assert event.next_attempt().event_id != event.event_id

    def test_job_id_is_stable(self, event: DocumentProcessingEvent) -> None:
        """job_id is the idempotency key and must survive every retry."""
        assert event.next_attempt().job_id == event.job_id

    def test_original_is_unchanged(self, event: DocumentProcessingEvent) -> None:
        event.next_attempt()
        assert event.attempt == 0

    def test_correlation_id_is_carried_forward(self, event: DocumentProcessingEvent) -> None:
        assert event.next_attempt().correlation_id == "corr-1"


def test_log_context_has_the_identifiers_for_tracing(event: DocumentProcessingEvent) -> None:
    context = event.log_context()
    assert context["job_id"] == str(event.job_id)
    assert context["document_id"] == str(event.document_id)
    assert context["correlation_id"] == "corr-1"
    assert context["attempt"] == 0
