"""Local file storage: naming, isolation and path-traversal defence."""

import re
from pathlib import Path

import pytest

from app.core.exceptions import StoragePathError
from app.services.file_storage import FileStorage, LocalFileStorage, StoredFile
from app.services.file_validation import sha256_hex

DATE_SHARDED = re.compile(r"^\d{4}/\d{2}/\d{2}/[0-9a-f-]{36}\.pdf$")


class TestSave:
    async def test_returns_metadata(self, storage: LocalFileStorage, pdf_bytes: bytes) -> None:
        stored = await storage.save(data=pdf_bytes, extension=".pdf")

        assert isinstance(stored, StoredFile)
        assert stored.size == len(pdf_bytes)
        assert stored.checksum == sha256_hex(pdf_bytes)

    async def test_path_is_date_sharded_with_a_generated_name(
        self, storage: LocalFileStorage, pdf_bytes: bytes
    ) -> None:
        """The client's filename never appears in the path."""
        stored = await storage.save(data=pdf_bytes, extension=".pdf")
        assert DATE_SHARDED.match(stored.storage_path), stored.storage_path

    async def test_file_lands_inside_the_root(
        self, storage: LocalFileStorage, pdf_bytes: bytes
    ) -> None:
        stored = await storage.save(data=pdf_bytes, extension=".pdf")
        written = storage.root / stored.storage_path
        assert written.is_file()
        assert written.read_bytes() == pdf_bytes

    async def test_identical_content_gets_distinct_names(
        self, storage: LocalFileStorage, pdf_bytes: bytes
    ) -> None:
        """Two uploads of the same document must not overwrite each other."""
        first = await storage.save(data=pdf_bytes, extension=".pdf")
        second = await storage.save(data=pdf_bytes, extension=".pdf")

        assert first.stored_filename != second.stored_filename
        assert first.checksum == second.checksum


class TestRead:
    async def test_round_trip(self, storage: LocalFileStorage, pdf_bytes: bytes) -> None:
        stored = await storage.save(data=pdf_bytes, extension=".pdf")
        assert await storage.read(stored.storage_path) == pdf_bytes

    async def test_missing_file_is_permanent(self, storage: LocalFileStorage) -> None:
        """A dangling row must not be retried forever."""
        with pytest.raises(StoragePathError) as error:
            await storage.read("2026/01/01/does-not-exist.pdf")
        assert error.value.retryable is False


class TestPathTraversalDefence:
    @pytest.mark.parametrize(
        "malicious",
        [
            "../../../etc/passwd",
            "../outside.pdf",
            "a/../../../../etc/shadow",
        ],
    )
    async def test_escaping_keys_rejected(self, storage: LocalFileStorage, malicious: str) -> None:
        """Second line of defence: keys are generated, but a tampered database
        row must not be able to read arbitrary files."""
        with pytest.raises(StoragePathError):
            await storage.read(malicious)

    async def test_absolute_key_rejected(self, storage: LocalFileStorage) -> None:
        with pytest.raises(StoragePathError):
            await storage.read("/etc/passwd")

    async def test_exists_is_false_rather_than_raising(self, storage: LocalFileStorage) -> None:
        assert await storage.exists("../../../etc/passwd") is False

    async def test_sibling_directory_prefix_rejected(self, tmp_path: Path) -> None:
        """``/tmp/uploads-evil`` must not pass a check that only compares prefixes."""
        backend = LocalFileStorage(tmp_path / "uploads")
        backend.ensure_root()
        (tmp_path / "uploads-evil").mkdir()
        (tmp_path / "uploads-evil" / "secret.txt").write_text("secret")

        with pytest.raises(StoragePathError):
            await backend.read("../uploads-evil/secret.txt")


class TestDeleteAndExists:
    async def test_exists_reflects_state(self, storage: LocalFileStorage, pdf_bytes: bytes) -> None:
        stored = await storage.save(data=pdf_bytes, extension=".pdf")
        assert await storage.exists(stored.storage_path) is True

        await storage.delete(stored.storage_path)
        assert await storage.exists(stored.storage_path) is False

    async def test_deleting_a_missing_file_is_not_an_error(self, storage: LocalFileStorage) -> None:
        await storage.delete("2026/01/01/never-existed.pdf")


def test_local_storage_satisfies_the_protocol(storage: LocalFileStorage) -> None:
    """The interface an S3 backend would have to implement, verified structurally."""
    assert isinstance(storage, FileStorage)
