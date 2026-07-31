"""Exception metadata: HTTP mapping, retry classification, response envelope."""

import pytest

from app.core import exceptions as exc


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (exc.NotFoundError(), 404),
        (exc.ConflictError(), 409),
        (exc.ValidationFailedError(), 400),
        (exc.FileTooLargeError(), 413),
        (exc.UnsupportedFileTypeError(), 415),
        (exc.TextExtractionError(), 422),
        (exc.EmptyDocumentError(), 422),
        (exc.DependencyUnavailableError(), 503),
        (exc.LLMRateLimitError(), 429),
        (exc.LLMTimeoutError(), 504),
        (exc.AppError(), 500),
    ],
)
def test_http_status_mapping(error: exc.AppError, status: int) -> None:
    assert error.http_status == status


@pytest.mark.parametrize(
    "error",
    [
        exc.LLMRateLimitError(),
        exc.LLMTimeoutError(),
        exc.LLMProviderError(),
        exc.DependencyUnavailableError(),
        exc.StorageError(),
        exc.QueueError(),
    ],
)
def test_transient_failures_are_retryable(error: exc.AppError) -> None:
    assert error.retryable is True


@pytest.mark.parametrize(
    "error",
    [
        exc.UnsupportedFileTypeError(),
        exc.EmptyDocumentError(),
        exc.TextExtractionError(),
        exc.LLMResponseValidationError(),
        exc.LLMAuthenticationError(),
        exc.LLMConfigurationError(),
        exc.MalformedEventError(),
        exc.StoragePathError(),
        exc.InvalidJobTransitionError(),
    ],
)
def test_permanent_failures_are_not_retryable(error: exc.AppError) -> None:
    assert error.retryable is False


def test_base_error_is_not_retryable_by_default() -> None:
    """An unclassified failure must not be retried blindly."""
    assert exc.AppError().retryable is False


def test_default_message_used_when_none_given() -> None:
    assert exc.NotFoundError().message == "Resource not found."


def test_custom_message_overrides_default() -> None:
    assert exc.NotFoundError("No such job").message == "No such job"


def test_error_codes_are_unique() -> None:
    subclasses: set[type[exc.AppError]] = set()
    pending = [exc.AppError]
    while pending:
        current = pending.pop()
        subclasses.add(current)
        pending.extend(current.__subclasses__())

    codes = [cls.code for cls in subclasses]
    duplicates = {code for code in codes if codes.count(code) > 1}
    assert not duplicates, f"duplicate error codes: {sorted(duplicates)}"


class TestResponseEnvelope:
    def test_minimal_envelope(self) -> None:
        assert exc.NotFoundError("missing").to_dict() == {
            "error": {"code": "not_found", "message": "missing"}
        }

    def test_correlation_id_included_when_supplied(self) -> None:
        payload = exc.ConflictError().to_dict(correlation_id="abc-123")
        assert payload["error"]["correlation_id"] == "abc-123"

    def test_details_omitted_unless_set(self) -> None:
        assert "details" not in exc.NotFoundError().to_dict()["error"]

    def test_details_included_when_set(self) -> None:
        error = exc.UnsupportedFileTypeError(details={"extension": ".exe"})
        assert error.to_dict()["error"]["details"] == {"extension": ".exe"}

    def test_repr_does_not_expose_details(self) -> None:
        error = exc.StorageError(details={"path": "/srv/uploads/secret.pdf"})
        assert "secret.pdf" not in repr(error)


def test_invalid_transition_is_a_conflict() -> None:
    """Retrying a non-failed job must surface as HTTP 409, not 500."""
    error = exc.InvalidJobTransitionError()
    assert isinstance(error, exc.ConflictError)
    assert error.http_status == 409
