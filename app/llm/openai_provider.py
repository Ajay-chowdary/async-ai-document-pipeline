"""OpenAI-backed classification and structured extraction.

The worker never imports the OpenAI SDK directly; failures are translated into
the typed hierarchy in :mod:`app.core.exceptions` so retry decisions stay in
one place. Document text is never written to the log — only length, type and
token counts.
"""

from typing import Any, cast

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.enums import DocumentType
from app.core.exceptions import (
    LLMAuthenticationError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponseValidationError,
    LLMTimeoutError,
)
from app.core.logging import get_logger
from app.llm.base import ClassificationOutput, ExtractionOutput, LLMProvider
from app.llm.prompts import (
    PROMPT_VERSION,
    classification_messages,
    extraction_messages,
    repair_user_message,
)
from app.schemas.extraction import SCHEMA_REGISTRY, DocumentClassification

logger = get_logger(__name__)


class OpenAIProvider(LLMProvider):
    """Structured-output extraction against the configured OpenAI model."""

    name: str = "openai"

    def __init__(self, settings: Settings) -> None:
        if settings.openai_api_key is None:
            raise LLMAuthenticationError(
                "OPENAI_API_KEY is not set.",
                details={"provider": self.name},
            )
        # Retries belong to the worker: a rate-limit here would otherwise be
        # retried twice, once by the SDK and once by the delayed-retry path.
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
        )
        self._model = settings.openai_model
        self._temperature = settings.llm_temperature

    @property
    def model_name(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._client.close()

    async def classify(self, text: str) -> ClassificationOutput:
        """Decide which of the supported types a document is."""
        parsed, usage = await self._parse(
            messages=classification_messages(text),
            response_format=DocumentClassification,
            operation="classify",
            document_chars=len(text),
        )
        result = cast(DocumentClassification, parsed)
        return ClassificationOutput(
            document_type=result.document_type,
            input_tokens=_usage_input(usage),
            output_tokens=_usage_output(usage),
            confidence_score=result.confidence_score,
        )

    async def extract(self, text: str, document_type: DocumentType) -> ExtractionOutput:
        """Extract the structured record for a known document type."""
        schema = SCHEMA_REGISTRY[document_type]
        parsed, usage = await self._parse(
            messages=extraction_messages(text, document_type),
            response_format=schema,
            operation="extract",
            document_chars=len(text),
            document_type=document_type.value,
        )
        payload = parsed.model_dump(mode="json")
        confidence = payload.get("confidence_score")
        score = float(confidence) if isinstance(confidence, (int, float)) else None
        return ExtractionOutput(
            document_type=document_type,
            data=payload,
            model_provider=self.name,
            model_name=self.model_name,
            prompt_version=PROMPT_VERSION,
            input_tokens=_usage_input(usage),
            output_tokens=_usage_output(usage),
            confidence_score=score,
        )

    async def _parse(
        self,
        *,
        messages: list[dict[str, Any]],
        response_format: type[BaseModel],
        operation: str,
        document_chars: int,
        document_type: str | None = None,
    ) -> tuple[BaseModel, Any]:
        """Call the model once, then once more if the first response is unusable.

        The second attempt is a schema-repair nudge, not a retry of a transport
        failure — those surface as typed exceptions and the worker schedules
        them.
        """
        typed_messages = cast(list[ChatCompletionMessageParam], messages)
        completion = await self._complete(typed_messages, response_format, operation=operation)
        parsed = _parsed_model(completion, response_format)
        if parsed is not None:
            logger.info(
                "llm.openai_success",
                operation=operation,
                model=self._model,
                document_chars=document_chars,
                document_type=document_type,
                input_tokens=_usage_input(completion.usage),
                output_tokens=_usage_output(completion.usage),
                repaired=False,
            )
            return parsed, completion.usage

        repair_messages: list[ChatCompletionMessageParam] = [
            *typed_messages,
            {
                "role": "assistant",
                "content": _assistant_text(completion),
            },
            {"role": "user", "content": repair_user_message()},
        ]
        logger.warning(
            "llm.openai_repair_attempt",
            operation=operation,
            model=self._model,
            document_chars=document_chars,
            document_type=document_type,
        )
        repaired = await self._complete(repair_messages, response_format, operation=operation)
        parsed = _parsed_model(repaired, response_format)
        if parsed is not None:
            logger.info(
                "llm.openai_success",
                operation=operation,
                model=self._model,
                document_chars=document_chars,
                document_type=document_type,
                input_tokens=_usage_input(repaired.usage),
                output_tokens=_usage_output(repaired.usage),
                repaired=True,
            )
            return parsed, repaired.usage

        raise LLMResponseValidationError(
            "The language model returned a response that failed schema validation "
            "after one repair attempt.",
            details={"operation": operation, "model": self._model},
        )

    async def _complete(
        self,
        messages: list[ChatCompletionMessageParam],
        response_format: type[BaseModel],
        *,
        operation: str,
    ) -> Any:
        try:
            return await self._client.chat.completions.parse(
                model=self._model,
                messages=messages,
                response_format=response_format,
                temperature=self._temperature,
            )
        except (
            openai.LengthFinishReasonError,
            openai.ContentFilterFinishReasonError,
        ) as error:
            raise LLMResponseValidationError(
                "The language model stopped before producing a valid response.",
                details={"operation": operation, "reason": type(error).__name__},
            ) from error
        except openai.APIError as error:
            raise _map_openai_error(error) from error


def _parsed_model(completion: Any, response_format: type[BaseModel]) -> BaseModel | None:
    """Return a validated model instance, or ``None`` when repair is needed."""
    if not completion.choices:
        return None
    message = completion.choices[0].message
    if getattr(message, "refusal", None):
        return None
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        if isinstance(parsed, response_format):
            return parsed
        try:
            return response_format.model_validate(parsed)
        except ValidationError:
            return None
    content = getattr(message, "content", None)
    if not content:
        return None
    try:
        return response_format.model_validate_json(content)
    except ValidationError:
        return None


def _assistant_text(completion: Any) -> str:
    if not completion.choices:
        return ""
    message = completion.choices[0].message
    content = message.content
    if isinstance(content, str) and content:
        return content
    refusal = getattr(message, "refusal", None)
    return refusal if isinstance(refusal, str) else ""


def _usage_input(usage: Any) -> int | None:
    if usage is None:
        return None
    value = getattr(usage, "prompt_tokens", None)
    return int(value) if value is not None else None


def _usage_output(usage: Any) -> int | None:
    if usage is None:
        return None
    value = getattr(usage, "completion_tokens", None)
    return int(value) if value is not None else None


def _map_openai_error(error: openai.APIError) -> Exception:
    """Translate an SDK failure into the application hierarchy."""
    if isinstance(error, openai.RateLimitError):
        return LLMRateLimitError(
            "The language model provider rate-limited the request.",
            details={"provider": "openai"},
        )
    if isinstance(error, openai.APITimeoutError):
        return LLMTimeoutError(
            "The language model request timed out.",
            details={"provider": "openai"},
        )
    if isinstance(error, openai.AuthenticationError):
        return LLMAuthenticationError(
            "The language model provider rejected the API credentials.",
            details={"provider": "openai"},
        )
    if isinstance(error, openai.APIConnectionError):
        return LLMProviderError(
            "Could not reach the language model provider.",
            details={"provider": "openai"},
        )
    status = getattr(error, "status_code", None)
    if status == 429:
        return LLMRateLimitError(
            "The language model provider rate-limited the request.",
            details={"provider": "openai", "status_code": status},
        )
    if status in {401, 403}:
        return LLMAuthenticationError(
            "The language model provider rejected the API credentials.",
            details={"provider": "openai", "status_code": status},
        )
    return LLMProviderError(
        "The language model provider returned an error.",
        details={"provider": "openai", "status_code": status},
    )
