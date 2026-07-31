"""Retry classification and backoff."""

import random

import pytest
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.exceptions import (
    EmptyDocumentError,
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMResponseValidationError,
    LLMTimeoutError,
    QueueError,
    StorageError,
    TextExtractionError,
    UnsupportedFileTypeError,
)
from app.worker.retry_policy import compute_delay, describe, is_retryable, should_retry


@pytest.fixture
def retry_settings() -> Settings:
    return Settings(
        _env_file=None,
        retry_base_delay_seconds=2.0,
        retry_max_delay_seconds=60.0,
        retry_jitter_ratio=0.0,
    )


class TestClassification:
    @pytest.mark.parametrize(
        "error",
        [
            LLMRateLimitError(),
            LLMTimeoutError(),
            QueueError(),
            StorageError(),
            OperationalError("stmt", {}, Exception("connection lost")),
            ConnectionError("refused"),
            TimeoutError(),
        ],
    )
    def test_transient_failures_retryable(self, error: BaseException) -> None:
        assert is_retryable(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            UnsupportedFileTypeError(),
            EmptyDocumentError(),
            TextExtractionError(),
            LLMResponseValidationError(),
            LLMAuthenticationError(),
        ],
    )
    def test_permanent_failures_not_retryable(self, error: BaseException) -> None:
        assert is_retryable(error) is False

    def test_unknown_exceptions_are_not_retried(self) -> None:
        """Retrying an unrecognised bug turns one failure into several."""
        assert is_retryable(ValueError("something unexpected")) is False
        assert is_retryable(KeyError("missing")) is False

    def test_auth_failure_is_not_retried(self) -> None:
        """A rejected key will still be rejected in two seconds."""
        assert is_retryable(LLMAuthenticationError()) is False


class TestShouldRetry:
    def test_budget_remaining(self) -> None:
        assert should_retry(LLMTimeoutError(), retry_count=0, max_retries=3)
        assert should_retry(LLMTimeoutError(), retry_count=2, max_retries=3)

    def test_budget_exhausted(self) -> None:
        assert not should_retry(LLMTimeoutError(), retry_count=3, max_retries=3)

    def test_permanent_failure_ignores_remaining_budget(self) -> None:
        assert not should_retry(EmptyDocumentError(), retry_count=0, max_retries=3)

    def test_zero_budget_disables_retries(self) -> None:
        assert not should_retry(LLMTimeoutError(), retry_count=0, max_retries=0)


class TestBackoff:
    @pytest.mark.parametrize(
        ("attempt", "expected"),
        [(0, 2.0), (1, 4.0), (2, 8.0), (3, 16.0), (4, 32.0)],
    )
    def test_doubles_each_attempt(
        self, attempt: int, expected: float, retry_settings: Settings
    ) -> None:
        assert compute_delay(attempt, retry_settings) == expected

    def test_capped(self, retry_settings: Settings) -> None:
        assert compute_delay(20, retry_settings) == 60.0

    def test_negative_attempt_treated_as_first(self, retry_settings: Settings) -> None:
        assert compute_delay(-1, retry_settings) == 2.0

    def test_jitter_stays_within_the_configured_band(self) -> None:
        settings = Settings(
            _env_file=None,
            retry_base_delay_seconds=10.0,
            retry_max_delay_seconds=100.0,
            retry_jitter_ratio=0.2,
        )
        delays = [compute_delay(0, settings, rng=random.Random(seed)) for seed in range(200)]

        assert all(8.0 <= delay <= 12.0 for delay in delays)

    def test_jitter_actually_varies(self) -> None:
        """Without variation, a rate-limited batch retries in lockstep."""
        settings = Settings(_env_file=None, retry_jitter_ratio=0.5)
        delays = {compute_delay(1, settings, rng=random.Random(seed)) for seed in range(50)}
        assert len(delays) > 1

    def test_delay_is_never_negative(self) -> None:
        settings = Settings(_env_file=None, retry_jitter_ratio=1.0)
        assert all(
            compute_delay(0, settings, rng=random.Random(seed)) >= 0.0 for seed in range(200)
        )


class TestDescribe:
    def test_app_error_includes_its_code(self) -> None:
        assert describe(LLMRateLimitError("slow down")) == "llm_rate_limited: slow down"

    def test_plain_exception_includes_its_type(self) -> None:
        assert describe(ValueError("bad input")) == "ValueError: bad input"

    def test_message_is_short_enough_to_display(self) -> None:
        assert len(describe(EmptyDocumentError())) < 500
