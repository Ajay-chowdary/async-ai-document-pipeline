"""Async engine, session factory and startup connectivity handling.

The engine is created lazily and cached, so importing this module has no side
effects and tests can point it at a different database before first use.

Startup does not assume PostgreSQL is already accepting connections:
:func:`wait_for_database` retries with a bounded budget. That is deliberate —
Compose ``depends_on`` and fixed ``sleep`` commands both fail the moment
startup ordering shifts, whereas application-level retry is correct whatever
starts first.
"""

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql import text

from app.core.config import Settings, get_settings
from app.core.exceptions import DependencyUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        resolved = settings or get_settings()
        _engine = create_async_engine(
            resolved.database_url.get_secret_value(),
            echo=resolved.db_echo,
            pool_size=resolved.db_pool_size,
            max_overflow=resolved.db_max_overflow,
            pool_pre_ping=True,
            future=True,
        )
        logger.debug("database.engine_created", url=resolved.safe_database_url)
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(settings),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def dispose_engine() -> None:
    """Close all pooled connections and reset the cached engine.

    Called on application shutdown and between test modules.
    """
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session for one request or one queue event.

    The session is never committed here. Commits are issued explicitly by the
    service layer so that transaction boundaries are visible at the call site
    rather than implied by a framework hook.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def wait_for_database(settings: Settings | None = None) -> None:
    """Block until the database answers a trivial query, or give up.

    Raises:
        DependencyUnavailableError: if the retry budget is exhausted.
    """
    resolved = settings or get_settings()
    engine = get_engine(resolved)
    last_error: Exception | None = None

    for attempt in range(1, resolved.db_connect_max_attempts + 1):
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as error:
            last_error = error
            logger.warning(
                "database.connect_retry",
                attempt=attempt,
                max_attempts=resolved.db_connect_max_attempts,
                url=resolved.safe_database_url,
                error=type(error).__name__,
            )
            await asyncio.sleep(resolved.db_connect_retry_seconds)
        else:
            logger.info("database.connected", url=resolved.safe_database_url, attempt=attempt)
            return

    raise DependencyUnavailableError(
        f"PostgreSQL unreachable after {resolved.db_connect_max_attempts} attempts",
        details={"dependency": "postgresql"},
    ) from last_error


async def check_database() -> bool:
    """Return whether the database currently answers a trivial query.

    Used by ``/ready``; never raises, so a readiness probe cannot 500.
    """
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as error:
        logger.warning("database.healthcheck_failed", error=type(error).__name__)
        return False
    return True
