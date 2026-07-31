"""Shared pytest fixtures.

Two guarantees are enforced here for the whole suite:

* No test can pick up a developer's real ``.env`` — in particular a real
  ``OPENAI_API_KEY`` — so the suite never makes a paid API call.
* Integration tests skip cleanly when PostgreSQL is not reachable, so
  ``make test`` still works on a machine with nothing running.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.enums import DocumentType, JobStatus
from app.db.models import Document, ProcessingJob
from app.llm.base import FakeLLMProvider
from app.services import job_service
from app.services.file_storage import LocalFileStorage, reset_file_storage
from app.services.queue import RedisQueue, reset_queue
from app.worker.consumer import StreamConsumer
from app.worker.processor import DocumentProcessor
from tests.fixtures.pdf_builder import build_pdf

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Override when your local PostgreSQL uses different credentials, e.g.
#: TEST_DATABASE_URL=postgresql+asyncpg://me@localhost:5432/docpipeline_test
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/docpipeline_test",
)

#: Database 15 is used so a test run cannot disturb a local dev Redis on 0.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

TEST_ENV = {
    "ENVIRONMENT": "test",
    "LLM_PROVIDER": "fake",
    "OPENAI_API_KEY": "",
    "LOG_JSON": "true",
    "LOG_LEVEL": "WARNING",
    "DATABASE_URL": TEST_DATABASE_URL,
    "REDIS_URL": TEST_REDIS_URL,
    "DB_CONNECT_MAX_ATTEMPTS": "1",
    "DB_CONNECT_RETRY_SECONDS": "0.1",
    "REDIS_CONNECT_MAX_ATTEMPTS": "1",
    "REDIS_CONNECT_RETRY_SECONDS": "0.1",
}

TABLES = ("extraction_results", "processing_jobs", "documents")


@pytest.fixture(autouse=True)
def _test_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin configuration to safe test values for every test."""
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_file_storage()
    reset_queue()
    yield
    get_settings.cache_clear()
    reset_file_storage()
    reset_queue()


@pytest.fixture
def settings() -> Settings:
    """Settings built from the pinned test environment."""
    return get_settings()


@pytest.fixture
def default_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings built from code defaults only, ignoring the environment and .env.

    Lets tests assert the shipped defaults without depending on whatever the
    developer happens to have configured locally.
    """
    known_fields = set(Settings.model_fields)
    for name in list(os.environ):
        if name.lower() in known_fields:
            monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _database_reachable() -> bool:
    async def probe() -> bool:
        engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=False)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:  # any failure at all means "skip the integration tests"
            return False
        finally:
            await engine.dispose()
        return True

    return asyncio.run(probe())


def _redis_reachable() -> bool:
    async def probe() -> bool:
        client = Redis.from_url(TEST_REDIS_URL)
        try:
            await client.ping()
        except Exception:  # any failure at all means "skip the Redis tests"
            return False
        finally:
            await client.aclose()
        return True

    return asyncio.run(probe())


@pytest.fixture(scope="session")
def redis_available() -> bool:
    """Probe Redis once per session rather than once per test."""
    return _redis_reachable()


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Bring the test database to head, or skip the tests that need it.

    Migrations are applied rather than ``metadata.create_all``: the schema
    under test is then the same one that ships, so a broken migration fails the
    suite instead of only failing in production.
    """
    if not _database_reachable():
        pytest.skip(f"PostgreSQL not reachable at {TEST_DATABASE_URL}")

    result = subprocess.run(  # fixed argv, no shell
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")
    return TEST_DATABASE_URL


@pytest.fixture
async def db_session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """Yield a session against the migrated test database, cleaned afterwards.

    Tables are truncated rather than each test running inside a rolled-back
    transaction, because the code under test commits deliberately and those
    commits are part of what is being verified.
    """
    engine = create_async_engine(migrated_database, pool_pre_ping=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
            await session.commit()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Storage and API
# ---------------------------------------------------------------------------


@pytest.fixture
async def queue(settings: Settings, redis_available: bool) -> AsyncIterator[RedisQueue]:
    """A connected queue on an isolated stream, or skip if Redis is absent.

    Each test gets its own stream and retry-set names so parallel or
    interleaved tests cannot see each other's messages.
    """
    if not redis_available:
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")

    suffix = uuid.uuid4().hex[:8]
    scoped = settings.model_copy(
        update={
            "redis_url": SecretStr(TEST_REDIS_URL),
            "redis_stream_name": f"test-stream-{suffix}",
            "redis_consumer_group": f"test-group-{suffix}",
            "redis_block_ms": 50,
            "redis_connect_max_attempts": 1,
        }
    )

    backend = RedisQueue(scoped)
    await backend.connect()
    await backend.ensure_group()
    try:
        yield backend
    finally:
        await backend.client.delete(backend.stream, backend.retry_key)
        await backend.close()


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    """Local storage rooted in a per-test temporary directory."""
    root = tmp_path / "uploads"
    backend = LocalFileStorage(root)
    backend.ensure_root()
    return backend


@pytest.fixture
async def api_client(
    db_session: AsyncSession, storage: LocalFileStorage, queue: RedisQueue
) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired to the app, the test session, temp storage and an
    isolated Redis stream.

    The ASGI transport does not run the lifespan, so the app under test never
    performs its startup dependency wait; the fixtures supply those directly.
    """
    from app.api.dependencies import provide_queue, provide_session, provide_storage
    from app.api.main import create_app

    app = create_app(get_settings())
    app.dependency_overrides[provide_session] = lambda: db_session
    app.dependency_overrides[provide_storage] = lambda: storage
    app.dependency_overrides[provide_queue] = lambda: queue

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@pytest.fixture
def reload_job(db_session: AsyncSession):
    """Re-read a job from the database, discarding any in-memory state.

    The processor commits and rolls back on the shared test session, which
    expires the objects a test is holding. Re-reading also makes assertions
    about what is actually persisted rather than about a stale ORM instance.
    """

    async def _reload(job: ProcessingJob) -> ProcessingJob:
        # The identity key is read from the instance state, so it works even
        # when every attribute has been expired by a rollback.
        job_id = inspect(job).identity[0]
        db_session.expire_all()
        return await job_service.get_job(db_session, job_id)

    return _reload


@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    """The deterministic provider every test extracts with."""
    return FakeLLMProvider()


@pytest.fixture
def processor(
    db_session: AsyncSession,
    storage: LocalFileStorage,
    queue: RedisQueue,
    fake_llm: FakeLLMProvider,
    settings: Settings,
) -> DocumentProcessor:
    """A processor sharing the test session, so assertions see its writes.

    The real worker opens a session per event; here a factory returning the
    one test session keeps the visibility simple without changing the code
    under test.
    """

    @asynccontextmanager
    async def session_scope() -> AsyncIterator[AsyncSession]:
        yield db_session

    return DocumentProcessor(
        sessionmaker=cast(async_sessionmaker[AsyncSession], session_scope),
        storage=storage,
        queue=queue,
        provider=fake_llm,
        settings=settings,
        consumer_name="worker-test-1",
    )


@pytest.fixture
def consumer(processor: DocumentProcessor, queue: RedisQueue, settings: Settings) -> StreamConsumer:
    """A consumer bound to the test queue and processor."""
    return StreamConsumer(
        queue=queue, processor=processor, settings=settings, consumer_name="worker-test-1"
    )


DEFAULT_DOCUMENT_TEXT = b"Sample document content used by the test suite.\n"


@pytest.fixture
def job_factory(db_session: AsyncSession, storage: LocalFileStorage) -> "JobFactory":
    """Build persisted document/job pairs without going through the API."""
    return JobFactory(db_session, storage)


class JobFactory:
    """Creates realistic job rows for tests that start mid-pipeline.

    The file is genuinely written to the test storage backend, so a job built
    here can be handed straight to the worker and processed for real.
    """

    def __init__(self, session: AsyncSession, storage: LocalFileStorage) -> None:
        self._session = session
        self._storage = storage

    async def create(
        self,
        *,
        status: "JobStatus | None" = None,
        retry_count: int = 0,
        max_retries: int = 3,
        filename: str = "invoice.txt",
        extension: str = ".txt",
        content: bytes | None = None,
        requested_document_type: "DocumentType | None" = None,
        started_at: "datetime | None" = None,
        write_file: bool = True,
    ) -> "ProcessingJob":
        if write_file:
            stored = await self._storage.save(
                data=content if content is not None else DEFAULT_DOCUMENT_TEXT,
                extension=extension,
            )
            stored_filename, storage_path = stored.stored_filename, stored.storage_path
            size, checksum = stored.size, stored.checksum
        else:
            # A row pointing at a file that is not there, for the
            # missing-file failure path.
            stored_filename = f"{uuid.uuid4()}{extension}"
            storage_path = f"2026/07/30/{stored_filename}"
            size, checksum = 1024, uuid.uuid4().hex * 2

        document = Document(
            original_filename=filename,
            stored_filename=stored_filename,
            content_type="text/plain",
            file_size=size,
            storage_path=storage_path,
            checksum=checksum,
            requested_document_type=requested_document_type,
        )
        job = ProcessingJob(
            document=document,
            status=status or JobStatus.QUEUED,
            retry_count=retry_count,
            max_retries=max_retries,
            started_at=started_at,
        )
        self._session.add(job)
        await self._session.commit()
        await self._session.refresh(job)
        return job


# ---------------------------------------------------------------------------
# Sample file payloads
# ---------------------------------------------------------------------------


@pytest.fixture
def txt_bytes() -> bytes:
    """A minimal valid TXT payload."""
    return b"Invoice 123 for ACME Corp. Total due: 42.00 USD.\n"


@pytest.fixture
def pdf_bytes() -> bytes:
    """A valid single-page PDF whose text layer says "Sample invoice"."""
    return build_pdf(["Sample invoice", "Vendor: Northwind Ltd", "Total: 1284.50 EUR"])


@pytest.fixture
def docx_bytes() -> bytes:
    """Bytes with a ZIP signature, enough to pass the .docx magic-byte check."""
    return b"PK\x03\x04" + b"\x00" * 60
