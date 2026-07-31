"""File storage behind a narrow interface.

The pipeline only ever needs four operations, so the protocol stays small
enough that an S3 implementation is a genuinely drop-in replacement rather than
a rewrite. Everything above this module deals in opaque ``storage_path`` keys
and never constructs a filesystem path itself.

Blocking file I/O is pushed to a worker thread with :func:`asyncio.to_thread`.
Under the current volumes this is comfortably cheap, and it keeps the event
loop free without dragging in an async filesystem dependency.
"""

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.core.config import Settings
from app.core.exceptions import StorageError, StoragePathError
from app.core.logging import get_logger
from app.core.time import utcnow
from app.services.file_validation import sha256_hex

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StoredFile:
    """The outcome of persisting an upload."""

    stored_filename: str
    storage_path: str
    size: int
    checksum: str


@runtime_checkable
class FileStorage(Protocol):
    """The storage operations the pipeline depends on."""

    async def save(self, *, data: bytes, extension: str) -> StoredFile:
        """Persist bytes under a generated name and return its metadata."""
        ...

    async def read(self, storage_path: str) -> bytes:
        """Return the bytes previously stored at ``storage_path``."""
        ...

    async def delete(self, storage_path: str) -> None:
        """Remove the object at ``storage_path``; missing objects are not an error."""
        ...

    async def exists(self, storage_path: str) -> bool:
        """Return whether an object exists at ``storage_path``."""
        ...


class LocalFileStorage:
    """Stores files on a local directory, typically a mounted Docker volume.

    Names are generated (``{uuid4}{extension}``) and laid out in ``YYYY/MM/DD``
    subdirectories, which keeps any single directory small enough to list.
    Client-supplied filenames are never part of the path, so path traversal is
    prevented by construction rather than by escaping; :meth:`_resolve` is the
    second line of defence against a corrupted or hand-edited database row.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        """The absolute directory all objects live under."""
        return self._root

    def _resolve(self, storage_path: str) -> Path:
        """Map a storage key to an absolute path, refusing to escape the root."""
        candidate = (self._root / storage_path).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise StoragePathError(
                "Resolved storage path lies outside the storage root.",
                details={"storage_path": storage_path},
            )
        return candidate

    def ensure_root(self) -> None:
        """Create the storage root if it does not exist."""
        self._root.mkdir(parents=True, exist_ok=True)

    async def save(self, *, data: bytes, extension: str) -> StoredFile:
        stored_filename = f"{uuid.uuid4()}{extension}"
        today = utcnow()
        relative = Path(f"{today:%Y/%m/%d}") / stored_filename
        destination = self._resolve(str(relative))

        try:
            await asyncio.to_thread(_write_bytes, destination, data)
        except OSError as error:
            raise StorageError(
                "Failed to write the uploaded file to storage.",
                details={"storage_path": str(relative)},
            ) from error

        stored = StoredFile(
            stored_filename=stored_filename,
            storage_path=relative.as_posix(),
            size=len(data),
            checksum=sha256_hex(data),
        )
        logger.info(
            "storage.saved",
            storage_path=stored.storage_path,
            size_bytes=stored.size,
            checksum=stored.checksum,
        )
        return stored

    async def read(self, storage_path: str) -> bytes:
        target = self._resolve(storage_path)
        try:
            return await asyncio.to_thread(target.read_bytes)
        except FileNotFoundError as error:
            # Permanent: the row points at a file that is gone, and retrying
            # the same read will fail identically.
            raise StoragePathError(
                "The stored file no longer exists.",
                details={"storage_path": storage_path},
            ) from error
        except OSError as error:
            raise StorageError(
                "Failed to read the stored file.",
                details={"storage_path": storage_path},
            ) from error

    async def delete(self, storage_path: str) -> None:
        target = self._resolve(storage_path)
        try:
            await asyncio.to_thread(target.unlink, True)
        except OSError as error:
            raise StorageError(
                "Failed to delete the stored file.",
                details={"storage_path": storage_path},
            ) from error
        logger.info("storage.deleted", storage_path=storage_path)

    async def exists(self, storage_path: str) -> bool:
        try:
            target = self._resolve(storage_path)
        except StoragePathError:
            return False
        return await asyncio.to_thread(target.is_file)


def _write_bytes(destination: Path, data: bytes) -> None:
    """Write ``data`` to ``destination``, creating parent directories."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


_storage: FileStorage | None = None


def get_file_storage(settings: Settings) -> FileStorage:
    """Return the configured storage backend, created once per process."""
    global _storage
    if _storage is None:
        storage = LocalFileStorage(settings.storage_local_path)
        storage.ensure_root()
        _storage = storage
        logger.debug(
            "storage.initialised",
            backend=settings.storage_backend.value,
            root=str(storage.root),
        )
    return _storage


def reset_file_storage() -> None:
    """Drop the cached backend, so tests can point storage at a temp directory."""
    global _storage
    _storage = None
