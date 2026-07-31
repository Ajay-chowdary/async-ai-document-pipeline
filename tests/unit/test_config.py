"""Settings parsing, derived values and secret handling."""

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings, get_settings
from app.core.enums import LLMProviderName, StorageBackend


def test_defaults_are_sane(default_settings: Settings) -> None:
    assert default_settings.environment == "local"
    assert default_settings.redis_stream_name == "document-processing"
    assert default_settings.redis_consumer_group == "document-workers"
    assert default_settings.max_retries == 3
    assert default_settings.max_upload_bytes == 10 * 1024 * 1024
    assert default_settings.storage_backend is StorageBackend.LOCAL
    assert default_settings.llm_provider is LLMProviderName.OPENAI
    assert default_settings.openai_api_key is None


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_RETRIES", "7")
    monkeypatch.setenv("REDIS_STREAM_NAME", "custom-stream")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.max_retries == 7
    assert settings.redis_stream_name == "custom-stream"


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_retry_zset_is_derived_from_stream_name(default_settings: Settings) -> None:
    assert default_settings.redis_retry_zset == "document-processing:retry"


def test_cors_origins_parsed_from_comma_list() -> None:
    settings = Settings(
        _env_file=None,
        cors_allow_origins="http://a.test, http://b.test ,",
    )
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_cors_origins_empty_when_unset() -> None:
    assert Settings(_env_file=None, cors_allow_origins="").cors_origins == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".pdf,.txt", {".pdf", ".txt"}),
        ("pdf, TXT ,docx", {".pdf", ".txt", ".docx"}),
        (".PDF", {".pdf"}),
    ],
)
def test_allowed_extensions_normalised(raw: str, expected: set[str]) -> None:
    settings = Settings(_env_file=None, allowed_upload_extensions=raw)
    assert settings.allowed_extensions == frozenset(expected)


def test_max_upload_mb_derived() -> None:
    assert Settings(_env_file=None, max_upload_bytes=5 * 1024 * 1024).max_upload_mb == 5.0


def test_llm_is_mocked_only_for_fake_provider() -> None:
    assert Settings(_env_file=None, llm_provider=LLMProviderName.FAKE).llm_is_mocked
    assert not Settings(_env_file=None, llm_provider=LLMProviderName.OPENAI).llm_is_mocked


def test_log_level_is_upper_cased() -> None:
    assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="chatty")


@pytest.mark.parametrize("ratio", [-0.1, 1.5])
def test_invalid_jitter_ratio_rejected(ratio: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, retry_jitter_ratio=ratio)


def test_invalid_environment_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="staging")


class TestSecretHandling:
    """Credentials must never appear in a repr, a log line or an error page."""

    def test_database_password_masked(self) -> None:
        settings = Settings(
            _env_file=None,
            database_url=SecretStr("postgresql+asyncpg://app:hunter2@db:5432/pipeline"),
        )
        assert settings.safe_database_url == "postgresql+asyncpg://app:***@db:5432/pipeline"
        assert "hunter2" not in settings.safe_database_url

    def test_redis_password_masked(self) -> None:
        settings = Settings(_env_file=None, redis_url=SecretStr("redis://:s3cret@cache:6379/0"))
        assert "s3cret" not in settings.safe_redis_url

    def test_url_without_credentials_unchanged(self) -> None:
        settings = Settings(_env_file=None, redis_url=SecretStr("redis://localhost:6379/0"))
        assert settings.safe_redis_url == "redis://localhost:6379/0"

    def test_secrets_absent_from_repr(self) -> None:
        settings = Settings(
            _env_file=None,
            openai_api_key=SecretStr("sk-should-never-appear"),
            database_url=SecretStr("postgresql+asyncpg://app:hunter2@db:5432/pipeline"),
        )
        rendered = repr(settings)
        assert "sk-should-never-appear" not in rendered
        assert "hunter2" not in rendered
