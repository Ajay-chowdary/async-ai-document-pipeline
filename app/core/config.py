"""Typed application configuration.

Every tunable value in the system is declared here and read from the
environment, so neither the API nor the worker ever reaches for ``os.environ``
directly. Secrets are held as :class:`~pydantic.SecretStr` so that an accidental
``repr`` or log call cannot leak them.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.enums import LLMProviderName, StorageBackend

_MB = 1024 * 1024


class Settings(BaseSettings):
    """Runtime configuration, populated from environment variables and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application ------------------------------------------------------
    app_name: str = "async-ai-document-pipeline"
    environment: Literal["local", "test", "production"] = "local"
    debug: bool = False

    # -- HTTP -------------------------------------------------------------
    api_host: str = "0.0.0.0"  # noqa: S104 - containers must bind all interfaces
    api_port: int = 8000
    #: Comma-separated origin list; parsed by :attr:`cors_origins`.
    cors_allow_origins: str = "http://localhost:8000"

    # -- Database ---------------------------------------------------------
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/docpipeline"
    )
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False
    #: Applies to the startup connectivity check, not to per-request queries.
    db_connect_max_attempts: int = 30
    db_connect_retry_seconds: float = 2.0

    # -- Redis ------------------------------------------------------------
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    redis_stream_name: str = "document-processing"
    redis_consumer_group: str = "document-workers"
    #: How long ``XREADGROUP`` blocks before looping, which bounds shutdown latency.
    redis_block_ms: int = 5_000
    redis_read_count: int = 1
    redis_connect_max_attempts: int = 30
    redis_connect_retry_seconds: float = 2.0

    # -- Worker reliability ------------------------------------------------
    max_retries: int = 3
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 60.0
    #: Fraction of the computed delay applied as random jitter, to avoid
    #: retry storms when many jobs fail at once.
    retry_jitter_ratio: float = 0.2
    #: Idle time after which a pending message may be claimed by another worker.
    pending_min_idle_ms: int = 60_000
    pending_sweep_interval_seconds: float = 30.0
    retry_sweep_interval_seconds: float = 1.0
    #: Redelivery count after which a message is dead-lettered instead of retried.
    max_delivery_count: int = 5
    #: A job stuck in ``processing`` for longer than this may be re-claimed.
    stale_processing_seconds: int = 600

    # -- File handling -----------------------------------------------------
    storage_backend: StorageBackend = StorageBackend.LOCAL
    storage_local_path: Path = Path("uploads")
    max_upload_bytes: int = 10 * _MB
    #: Comma-separated extensions; parsed by :attr:`allowed_extensions`.
    allowed_upload_extensions: str = ".pdf,.txt,.docx"

    # -- LLM ---------------------------------------------------------------
    llm_provider: LLMProviderName = LLMProviderName.OPENAI
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0
    llm_max_attempts: int = 1
    llm_temperature: float = 0.0
    #: Documents longer than this are truncated before being sent to the model.
    llm_max_input_chars: int = 40_000
    #: Only this much text is sent to the cheaper classification call.
    classification_max_chars: int = 2_000
    #: Truncation applied to ``extraction_results.raw_text`` before persisting.
    raw_text_store_max_chars: int = 20_000

    # -- Logging -----------------------------------------------------------
    log_level: str = "INFO"
    #: JSON in containers, human-readable colour output for local terminals.
    log_json: bool = True

    # -- Dashboard ---------------------------------------------------------
    dashboard_poll_interval_ms: int = 3_000
    dashboard_recent_jobs_limit: int = 25

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if level not in allowed:
            msg = f"log_level must be one of {sorted(allowed)}, got {value!r}"
            raise ValueError(msg)
        return level

    @field_validator("retry_jitter_ratio")
    @classmethod
    def _check_jitter(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            msg = "retry_jitter_ratio must be between 0 and 1"
            raise ValueError(msg)
        return value

    @property
    def cors_origins(self) -> list[str]:
        """CORS origins as a list, empty when unset."""
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]

    @property
    def allowed_extensions(self) -> frozenset[str]:
        """Lower-cased, dot-prefixed upload extensions."""
        return frozenset(
            ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
            for ext in self.allowed_upload_extensions.split(",")
            if ext.strip()
        )

    @property
    def redis_retry_zset(self) -> str:
        """Sorted set holding events scheduled for a delayed retry."""
        return f"{self.redis_stream_name}:retry"

    @property
    def max_upload_mb(self) -> float:
        """Upload limit in megabytes, for error messages and templates."""
        return round(self.max_upload_bytes / _MB, 2)

    @property
    def llm_is_mocked(self) -> bool:
        """Whether extraction runs against the deterministic fake provider."""
        return self.llm_provider is LLMProviderName.FAKE

    @property
    def safe_database_url(self) -> str:
        """Database URL with the password masked, safe to log."""
        return _mask_url_password(self.database_url.get_secret_value())

    @property
    def safe_redis_url(self) -> str:
        """Redis URL with the password masked, safe to log."""
        return _mask_url_password(self.redis_url.get_secret_value())


def _mask_url_password(url: str) -> str:
    """Replace the password component of a connection URL with ``***``."""
    scheme, separator, remainder = url.partition("://")
    if not separator or "@" not in remainder:
        return url
    credentials, _, host = remainder.rpartition("@")
    user, has_password, _ = credentials.partition(":")
    if not has_password:
        return url
    return f"{scheme}://{user}:***@{host}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Cached so that configuration is parsed and validated exactly once. Tests
    that manipulate the environment should call ``get_settings.cache_clear()``.
    """
    return Settings()
