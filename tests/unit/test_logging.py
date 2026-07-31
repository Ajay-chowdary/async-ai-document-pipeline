"""Logging redaction, truncation and correlation-ID context."""

import json

import pytest
import structlog

from app.core.logging import (
    MAX_VALUE_CHARS,
    bind_context,
    bind_correlation_id,
    clear_context,
    configure_logging,
    get_correlation_id,
    get_logger,
    log_context,
    redact_sensitive,
    truncate_long_values,
)


@pytest.fixture(autouse=True)
def _reset_context() -> None:
    clear_context()


class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "openai_api_key",
            "OPENAI_API_KEY",
            "password",
            "db_password",
            "authorization",
            "secret",
            "client_secret",
            "access_token",
            "database_url",
            "redis_url",
        ],
    )
    def test_sensitive_keys_masked(self, key: str) -> None:
        result = redact_sensitive(None, "info", {key: "sk-live-value"})
        assert result[key] == "***redacted***"

    @pytest.mark.parametrize("key", ["job_id", "input_tokens", "output_tokens", "duration_ms"])
    def test_ordinary_keys_untouched(self, key: str) -> None:
        result = redact_sensitive(None, "info", {key: 42})
        assert result[key] == 42

    def test_token_counts_are_not_mistaken_for_credentials(self) -> None:
        """``input_tokens`` is telemetry we want; ``access_token`` is not."""
        event = {"input_tokens": 1200, "output_tokens": 340, "access_token": "abc"}
        result = redact_sensitive(None, "info", event)
        assert result["input_tokens"] == 1200
        assert result["output_tokens"] == 340
        assert result["access_token"] == "***redacted***"

    def test_nested_sensitive_keys_masked(self) -> None:
        event = {"config": {"openai_api_key": "sk-live", "model": "gpt-4o-mini"}}
        result = redact_sensitive(None, "info", event)
        assert result["config"]["openai_api_key"] == "***redacted***"
        assert result["config"]["model"] == "gpt-4o-mini"


class TestTruncation:
    def test_long_values_truncated(self) -> None:
        document_text = "x" * 50_000
        result = truncate_long_values(None, "info", {"raw_text": document_text})
        assert len(result["raw_text"]) < MAX_VALUE_CHARS + 60
        assert "truncated 50000 chars" in result["raw_text"]

    def test_short_values_unchanged(self) -> None:
        result = truncate_long_values(None, "info", {"message": "all good"})
        assert result["message"] == "all good"

    def test_non_string_values_unchanged(self) -> None:
        result = truncate_long_values(None, "info", {"count": 12345})
        assert result["count"] == 12345


class TestCorrelationId:
    def test_generated_when_absent(self) -> None:
        generated = bind_correlation_id()
        assert generated
        assert get_correlation_id() == generated

    def test_supplied_value_preserved(self) -> None:
        assert bind_correlation_id("req-42") == "req-42"
        assert get_correlation_id() == "req-42"

    def test_none_before_binding(self) -> None:
        assert get_correlation_id() is None

    def test_cleared_context_drops_the_id(self) -> None:
        bind_correlation_id("req-42")
        clear_context()
        assert get_correlation_id() is None

    def test_log_context_restores_previous_values(self) -> None:
        bind_context(job_id="job-1")
        with log_context(job_id="job-2"):
            assert structlog.contextvars.get_contextvars()["job_id"] == "job-2"
        assert structlog.contextvars.get_contextvars()["job_id"] == "job-1"


class TestEndToEndRendering:
    def test_json_output_carries_context_and_redacts(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", json_logs=True)
        bind_correlation_id("corr-1")
        bind_context(job_id="job-9")

        get_logger("test").info("job.claimed", openai_api_key="sk-live", attempt=1)

        line = capsys.readouterr().out.strip().splitlines()[-1]
        payload = json.loads(line)

        assert payload["event"] == "job.claimed"
        assert payload["correlation_id"] == "corr-1"
        assert payload["job_id"] == "job-9"
        assert payload["attempt"] == 1
        assert payload["openai_api_key"] == "***redacted***"
        assert payload["level"] == "info"
        assert "timestamp" in payload

    def test_level_filtering(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="WARNING", json_logs=True)
        get_logger("test").info("should.not.appear")
        assert "should.not.appear" not in capsys.readouterr().out
