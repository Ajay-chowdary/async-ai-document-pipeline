"""FastAPI dependency providers.

Everything the routers need arrives through these, so a test can substitute a
temp-directory storage backend or a throwaway database by overriding one
callable rather than patching module globals.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.services.file_storage import FileStorage, get_file_storage
from app.services.queue import RedisQueue, get_queue


async def provide_session() -> AsyncIterator[AsyncSession]:
    """Yield a database session scoped to the request."""
    async for session in get_session():
        yield session


def provide_settings() -> Settings:
    """Return the application settings."""
    return get_settings()


def provide_storage(settings: Annotated[Settings, Depends(provide_settings)]) -> FileStorage:
    """Return the configured file storage backend."""
    return get_file_storage(settings)


def provide_queue(settings: Annotated[Settings, Depends(provide_settings)]) -> RedisQueue:
    """Return the connected Redis queue."""
    return get_queue(settings)


SessionDep = Annotated[AsyncSession, Depends(provide_session)]
SettingsDep = Annotated[Settings, Depends(provide_settings)]
StorageDep = Annotated[FileStorage, Depends(provide_storage)]
QueueDep = Annotated[RedisQueue, Depends(provide_queue)]
