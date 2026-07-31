"""Fake provider output must validate against the real extraction schemas."""

import pytest

from app.core.enums import DocumentType
from app.core.exceptions import LLMRateLimitError
from app.llm.base import FakeLLMProvider
from app.schemas.extraction import SCHEMA_REGISTRY


@pytest.mark.parametrize("document_type", list(DocumentType))
async def test_fake_extract_validates_against_real_schema(document_type: DocumentType) -> None:
    provider = FakeLLMProvider()
    output = await provider.extract("Sample document text for extraction.", document_type)

    SCHEMA_REGISTRY[document_type].model_validate(output.data)
    assert output.document_type is document_type
    assert output.model_provider == "fake"
    assert output.prompt_version == "fake-v1"
    assert output.confidence_score == 0.75


async def test_fake_classify_is_deterministic() -> None:
    provider = FakeLLMProvider()
    first = await provider.classify("INVOICE\nVendor: ACME\nSubtotal: 10")
    second = await provider.classify("INVOICE\nVendor: ACME\nSubtotal: 10")
    assert first.document_type is DocumentType.INVOICE
    assert first.document_type is second.document_type


async def test_fake_failures_are_popped_per_call() -> None:
    provider = FakeLLMProvider(failures=[LLMRateLimitError("once")])
    with pytest.raises(LLMRateLimitError):
        await provider.extract("text", DocumentType.GENERIC)
    output = await provider.extract("text", DocumentType.GENERIC)
    assert output.data["confidence_score"] == 0.75
