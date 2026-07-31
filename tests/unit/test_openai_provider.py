"""OpenAI provider: exception mapping and validation, without network calls."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import openai
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.enums import DocumentType
from app.core.exceptions import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponseValidationError,
    LLMTimeoutError,
)
from app.llm.factory import build_provider
from app.llm.openai_provider import OpenAIProvider, _map_openai_error
from app.llm.prompts import PROMPT_VERSION
from app.schemas.extraction import InvoiceExtraction


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "llm_provider": "openai",
        "openai_api_key": SecretStr("sk-test-key"),
        "openai_model": "gpt-4o-mini",
        "llm_temperature": 0.0,
        "llm_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return Settings(**values)


def _completion(parsed: Any, *, usage: Any = None, content: str | None = None) -> MagicMock:
    message = SimpleNamespace(parsed=parsed, content=content, refusal=None)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(
        choices=[choice],
        usage=usage or SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )


@pytest.mark.parametrize(
    ("sdk_error", "expected"),
    [
        (
            openai.RateLimitError("slow", response=MagicMock(status_code=429), body=None),
            LLMRateLimitError,
        ),
        (openai.APITimeoutError(request=MagicMock()), LLMTimeoutError),
        (
            openai.AuthenticationError("bad key", response=MagicMock(status_code=401), body=None),
            LLMAuthenticationError,
        ),
        (openai.APIConnectionError(request=MagicMock()), LLMProviderError),
        (
            openai.InternalServerError("boom", response=MagicMock(status_code=500), body=None),
            LLMProviderError,
        ),
    ],
)
def test_exception_mapping(sdk_error: Exception, expected: type[Exception]) -> None:
    mapped = _map_openai_error(sdk_error)  # type: ignore[arg-type]
    assert isinstance(mapped, expected)


def test_authentication_error_is_not_retryable() -> None:
    mapped = _map_openai_error(
        openai.AuthenticationError("nope", response=MagicMock(status_code=401), body=None)
    )
    assert isinstance(mapped, LLMAuthenticationError)
    assert mapped.retryable is False


async def test_extract_returns_validated_payload() -> None:
    provider = OpenAIProvider(_settings())
    invoice = InvoiceExtraction(
        vendor_name="ACME",
        invoice_number="1",
        invoice_date=None,
        due_date=None,
        currency="USD",
        subtotal=10.0,
        tax=0.0,
        total=10.0,
        line_items=[],
        confidence_score=0.8,
    )
    provider._client.chat.completions.parse = AsyncMock(return_value=_completion(invoice))

    output = await provider.extract("Invoice for ACME", DocumentType.INVOICE)

    assert output.model_provider == "openai"
    assert output.prompt_version == PROMPT_VERSION
    assert output.model_name == "gpt-4o-mini"
    assert output.data["vendor_name"] == "ACME"
    assert output.confidence_score == 0.8
    assert output.input_tokens == 11
    assert output.output_tokens == 7
    provider._client.chat.completions.parse.assert_awaited_once()
    await provider.aclose()


async def test_malformed_response_repairs_then_succeeds() -> None:
    provider = OpenAIProvider(_settings())
    invoice = InvoiceExtraction(
        vendor_name="ACME",
        invoice_number=None,
        invoice_date=None,
        due_date=None,
        currency=None,
        subtotal=None,
        tax=None,
        total=None,
        line_items=[],
        confidence_score=0.4,
    )
    provider._client.chat.completions.parse = AsyncMock(
        side_effect=[_completion(None, content="{not-json"), _completion(invoice)]
    )

    output = await provider.extract("Invoice text", DocumentType.INVOICE)

    assert output.data["vendor_name"] == "ACME"
    assert provider._client.chat.completions.parse.await_count == 2
    await provider.aclose()


async def test_malformed_response_raises_after_repair_fails() -> None:
    provider = OpenAIProvider(_settings())
    provider._client.chat.completions.parse = AsyncMock(
        return_value=_completion(None, content="{still-bad")
    )

    with pytest.raises(LLMResponseValidationError):
        await provider.extract("Invoice text", DocumentType.INVOICE)

    assert provider._client.chat.completions.parse.await_count == 2
    await provider.aclose()


async def test_rate_limit_is_translated() -> None:
    provider = OpenAIProvider(_settings())
    provider._client.chat.completions.parse = AsyncMock(
        side_effect=openai.RateLimitError("slow", response=MagicMock(status_code=429), body=None)
    )

    with pytest.raises(LLMRateLimitError):
        await provider.classify("some text")
    await provider.aclose()


async def test_document_text_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    provider = OpenAIProvider(_settings())
    invoice = InvoiceExtraction(
        vendor_name=None,
        invoice_number=None,
        invoice_date=None,
        due_date=None,
        currency=None,
        subtotal=None,
        tax=None,
        total=None,
        line_items=[],
        confidence_score=0.1,
    )
    provider._client.chat.completions.parse = AsyncMock(return_value=_completion(invoice))
    secret = "UNIQUE_SECRET_DOCUMENT_BODY_XYZ"

    with caplog.at_level("INFO"):
        await provider.extract(secret, DocumentType.INVOICE)

    assert secret not in caplog.text
    await provider.aclose()


def test_factory_builds_openai_provider() -> None:
    provider = build_provider(_settings())
    assert isinstance(provider, OpenAIProvider)


def test_factory_requires_api_key_for_openai() -> None:
    with pytest.raises(LLMConfigurationError, match="OPENAI_API_KEY"):
        build_provider(_settings(openai_api_key=None))


def test_factory_builds_fake_provider() -> None:
    from app.llm.base import FakeLLMProvider

    provider = build_provider(_settings(llm_provider="fake", openai_api_key=None))
    assert isinstance(provider, FakeLLMProvider)
