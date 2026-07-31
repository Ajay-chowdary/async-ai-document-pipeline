"""Application exception hierarchy.

Two orthogonal facts are attached to every error:

``http_status``
    How the API renders it, used by the global exception handler.
``retryable``
    Whether the worker should schedule another attempt. This is a property of
    the failure, not of the call site, which keeps the retry decision in one
    place instead of scattering ``except`` clauses through the pipeline.
"""

from typing import Any


class AppError(Exception):
    """Base class for every error the application raises deliberately."""

    code: str = "internal_error"
    http_status: int = 500
    retryable: bool = False
    default_message: str = "An unexpected error occurred."

    def __init__(
        self, message: str | None = None, *, details: dict[str, Any] | None = None
    ) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self, correlation_id: str | None = None) -> dict[str, Any]:
        """Render the error as the API's standard response envelope.

        ``details`` is included only when it was set explicitly, so internal
        context is never leaked to clients by accident.
        """
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        if correlation_id:
            payload["correlation_id"] = correlation_id
        return {"error": payload}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class RetryableError(AppError):
    """A transient failure; the worker should try the job again later."""

    code = "transient_error"
    http_status = 503
    retryable = True
    default_message = "A transient error occurred; the operation will be retried."


class NonRetryableError(AppError):
    """A permanent failure; retrying cannot change the outcome."""

    code = "permanent_error"
    retryable = False
    default_message = "The operation cannot succeed and will not be retried."


# ---------------------------------------------------------------------------
# API-facing errors
# ---------------------------------------------------------------------------


class NotFoundError(NonRetryableError):
    """A requested resource does not exist."""

    code = "not_found"
    http_status = 404
    default_message = "Resource not found."


class ConflictError(NonRetryableError):
    """The request is valid but conflicts with the resource's current state.

    Raised, for example, when retrying a job that is not in ``failed``.
    """

    code = "conflict"
    http_status = 409
    default_message = "The request conflicts with the current state of the resource."


class ValidationFailedError(NonRetryableError):
    """The request was structurally valid but semantically rejected."""

    code = "validation_failed"
    http_status = 400
    default_message = "The request could not be validated."


class FileTooLargeError(NonRetryableError):
    """The upload exceeds the configured size limit."""

    code = "file_too_large"
    http_status = 413
    default_message = "The uploaded file exceeds the maximum allowed size."


class UnsupportedFileTypeError(NonRetryableError):
    """The upload is not one of the supported document formats."""

    code = "unsupported_file_type"
    http_status = 415
    default_message = "The uploaded file type is not supported."


class DependencyUnavailableError(RetryableError):
    """A backing service (database, Redis, storage) is unreachable."""

    code = "dependency_unavailable"
    http_status = 503
    default_message = "A required dependency is unavailable."


# ---------------------------------------------------------------------------
# Storage and text extraction
# ---------------------------------------------------------------------------


class StorageError(RetryableError):
    """The file could not be read from or written to the storage backend."""

    code = "storage_error"
    default_message = "The file could not be read from storage."


class StoragePathError(NonRetryableError):
    """A storage key resolved outside the storage root.

    Signals a path-traversal attempt or a corrupt record; never retried.
    """

    code = "invalid_storage_path"
    http_status = 400
    default_message = "The requested storage path is not permitted."


class TextExtractionError(NonRetryableError):
    """The document could not be parsed into text."""

    code = "text_extraction_failed"
    http_status = 422
    default_message = "The document could not be parsed."


class EmptyDocumentError(NonRetryableError):
    """Parsing succeeded but produced no usable text.

    Typically a scanned or image-only PDF, which needs OCR the pipeline does
    not have. Retrying would produce the same empty result.
    """

    code = "empty_document"
    http_status = 422
    default_message = "No text could be extracted from the document."


# ---------------------------------------------------------------------------
# LLM provider
# ---------------------------------------------------------------------------


class LLMError(AppError):
    """Base class for LLM provider failures."""

    code = "llm_error"
    http_status = 502
    default_message = "The language model request failed."


class LLMRateLimitError(LLMError):
    """The provider rate-limited the request."""

    code = "llm_rate_limited"
    http_status = 429
    retryable = True
    default_message = "The language model provider rate-limited the request."


class LLMTimeoutError(LLMError):
    """The provider did not respond within the configured timeout."""

    code = "llm_timeout"
    http_status = 504
    retryable = True
    default_message = "The language model request timed out."


class LLMProviderError(LLMError):
    """The provider returned a server-side error."""

    code = "llm_provider_error"
    retryable = True
    default_message = "The language model provider returned an error."


class LLMAuthenticationError(LLMError):
    """The provider rejected the credentials.

    Not retryable: the key will still be wrong on the next attempt.
    """

    code = "llm_authentication_failed"
    http_status = 502
    retryable = False
    default_message = "The language model provider rejected the API credentials."


class LLMResponseValidationError(LLMError):
    """The provider's response did not satisfy the extraction schema.

    Not retryable at the job level; the provider implementation makes one
    in-call repair attempt before giving up.
    """

    code = "llm_invalid_response"
    http_status = 422
    retryable = False
    default_message = "The language model returned a response that failed validation."


class LLMConfigurationError(NonRetryableError):
    """The selected provider is not usable, e.g. a missing API key."""

    code = "llm_not_configured"
    http_status = 503
    default_message = "The language model provider is not configured."


# ---------------------------------------------------------------------------
# Queue and job lifecycle
# ---------------------------------------------------------------------------


class QueueError(RetryableError):
    """Publishing to or consuming from Redis failed."""

    code = "queue_error"
    default_message = "The message queue is unavailable."


class MalformedEventError(NonRetryableError):
    """A stream message could not be decoded into a known event.

    Acknowledged and dropped rather than redelivered, so one poison message
    cannot stall the consumer group.
    """

    code = "malformed_event"
    http_status = 400
    default_message = "The queue message could not be decoded."


class InvalidJobTransitionError(ConflictError):
    """A job state change was rejected by the state machine."""

    code = "invalid_job_transition"
    default_message = "The requested job state transition is not allowed."
