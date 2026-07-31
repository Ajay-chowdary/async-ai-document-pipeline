"""Selecting the LLM backend from configuration.

Provider choice happens here and nowhere else, so the worker never imports a
vendor SDK directly and tests get the deterministic fake by setting one
environment variable.
"""

from app.core.config import Settings
from app.core.enums import LLMProviderName
from app.core.exceptions import LLMConfigurationError
from app.core.logging import get_logger
from app.llm.base import FakeLLMProvider, LLMProvider
from app.llm.openai_provider import OpenAIProvider

logger = get_logger(__name__)


def build_provider(settings: Settings) -> LLMProvider:
    """Return the configured provider.

    Failures surface at startup rather than on the first document: a worker
    that cannot extract anything should not sit in the consumer group claiming
    jobs it will only fail.

    Raises:
        LLMConfigurationError: the selected provider cannot be constructed.
    """
    if settings.llm_provider is LLMProviderName.FAKE:
        logger.warning(
            "llm.using_fake_provider",
            reason="LLM_PROVIDER=fake; extraction output is synthetic, not model-generated",
        )
        return FakeLLMProvider()

    if settings.llm_provider is LLMProviderName.OPENAI:
        if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
            raise LLMConfigurationError(
                "LLM_PROVIDER is 'openai' but OPENAI_API_KEY is not set. "
                "Set the key, or set LLM_PROVIDER=fake to run without one.",
                details={"provider": settings.llm_provider.value},
            )
        logger.info("llm.using_openai_provider", model=settings.openai_model)
        return OpenAIProvider(settings)

    raise LLMConfigurationError(
        f"Unsupported LLM provider: {settings.llm_provider!r}.",
        details={"provider": str(settings.llm_provider)},
    )
