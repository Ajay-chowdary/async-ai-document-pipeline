"""The message contract carried on the Redis stream.

Producers and consumers share this module, so the wire format has exactly one
definition. Events are serialised as a single JSON string in one stream field
rather than flattened into Redis key/value pairs: flattening loses the
distinction between ``null`` and the string ``"None"``, and makes optional
fields awkward to evolve.

``event_version`` is present from the first release. A consumer that meets a
version it does not understand logs and acknowledges rather than crash-looping
on a message it can never process.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import DocumentType, EventType
from app.core.time import utcnow

#: Bump when a change to the payload is not backwards compatible.
EVENT_VERSION = 1

#: The stream field the JSON payload is stored under.
PAYLOAD_FIELD = "data"


class DocumentProcessingEvent(BaseModel):
    """A request to process one document.

    Note what is *not* here: the file's bytes, and any extracted content. The
    event carries identifiers only, so the queue stays small and a message
    lingering in Redis never becomes a copy of a customer's document.
    """

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: EventType = EventType.DOCUMENT_PROCESSING_REQUESTED
    event_version: int = EVENT_VERSION

    job_id: uuid.UUID
    document_id: uuid.UUID
    storage_path: str

    #: Set when the client named the type; null means the worker classifies.
    requested_document_type: DocumentType | None = None

    #: Zero on first publish, incremented by each scheduled retry. The job row
    #: remains the authority on attempts; this is for logging and for the
    #: backoff calculation.
    attempt: int = 0

    created_at: datetime = Field(default_factory=utcnow)
    #: Ties this event's log lines back to the upload request that caused it.
    correlation_id: str | None = None

    def next_attempt(self) -> "DocumentProcessingEvent":
        """Return a copy for the next delivery, with a fresh event ID.

        A retry is a genuinely new message: reusing ``event_id`` would make the
        two indistinguishable in the logs. ``job_id`` stays the same and is the
        real idempotency key.
        """
        return self.model_copy(
            update={
                "event_id": uuid.uuid4(),
                "attempt": self.attempt + 1,
                "created_at": utcnow(),
            }
        )

    def to_stream_fields(self) -> dict[str, str]:
        """Render the event as the field mapping passed to ``XADD``."""
        return {PAYLOAD_FIELD: self.model_dump_json()}

    def log_context(self) -> dict[str, str | int | None]:
        """The identifiers every log line for this event should carry."""
        return {
            "event_id": str(self.event_id),
            "job_id": str(self.job_id),
            "document_id": str(self.document_id),
            "attempt": self.attempt,
            "correlation_id": self.correlation_id,
        }
