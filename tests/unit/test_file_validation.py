"""Filename sanitization and upload validation."""

import hashlib

import pytest

from app.core.config import Settings
from app.core.exceptions import FileTooLargeError, UnsupportedFileTypeError
from app.services.file_validation import (
    FALLBACK_FILENAME,
    MAX_FILENAME_LENGTH,
    extract_extension,
    sanitize_filename,
    sha256_hex,
    validate_content_type,
    validate_extension,
    validate_magic_bytes,
    validate_size,
    validate_upload,
)


@pytest.fixture
def upload_settings() -> Settings:
    return Settings(_env_file=None, max_upload_bytes=1024)


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("invoice.pdf", "invoice.pdf"),
            ("My Invoice 2026.pdf", "My Invoice 2026.pdf"),
            ("report_final-v2.docx", "report_final-v2.docx"),
        ],
    )
    def test_ordinary_names_preserved(self, raw: str, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "../../../etc/passwd.txt",
            "/etc/passwd.txt",
            "..\\..\\windows\\system32\\evil.txt",
            "C:\\Users\\admin\\secret.txt",
            "./../.././passwd.txt",
        ],
    )
    def test_directory_traversal_stripped(self, raw: str) -> None:
        """No separator survives, from either path flavour."""
        result = sanitize_filename(raw)
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result

    def test_null_byte_removed(self) -> None:
        assert "\x00" not in sanitize_filename("evil\x00.pdf")

    def test_newline_removed(self) -> None:
        """A newline in a filename would forge an extra line in the log stream."""
        assert "\n" not in sanitize_filename("in\nvoice.pdf")

    def test_unicode_normalised_to_ascii(self) -> None:
        result = sanitize_filename("réçu-café.pdf")
        assert result.isascii()
        assert result.endswith(".pdf")

    def test_rtl_override_removed(self) -> None:
        """U+202E can make 'evil_txt.exe' render as 'evil_exe.txt' in the UI."""
        assert "\u202e" not in sanitize_filename("invoice\u202egpj.pdf")

    @pytest.mark.parametrize("raw", ["", None, "   ", "...", "___"])
    def test_empty_results_fall_back(self, raw: str | None) -> None:
        assert sanitize_filename(raw) == FALLBACK_FILENAME

    def test_long_name_truncated_keeping_extension(self) -> None:
        result = sanitize_filename(f"{'a' * 500}.pdf")
        assert len(result) <= MAX_FILENAME_LENGTH
        assert result.endswith(".pdf")

    def test_shell_metacharacters_replaced(self) -> None:
        result = sanitize_filename("in;rm -rf $(pwd)`voice.pdf")
        assert not set(result) & set(";$()`")


class TestExtractExtension:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("a.pdf", ".pdf"),
            ("a.PDF", ".pdf"),
            ("archive.tar.gz", ".gz"),
            ("noext", ""),
            (".hidden", ""),
        ],
    )
    def test_extension(self, filename: str, expected: str) -> None:
        assert extract_extension(filename) == expected


class TestValidateExtension:
    @pytest.mark.parametrize("filename", ["a.pdf", "a.txt", "a.docx", "a.PDF"])
    def test_allowed(self, filename: str, upload_settings: Settings) -> None:
        assert validate_extension(filename, upload_settings) == extract_extension(filename)

    @pytest.mark.parametrize("filename", ["a.exe", "a.sh", "a.zip", "a.jpg", "noext"])
    def test_rejected(self, filename: str, upload_settings: Settings) -> None:
        with pytest.raises(UnsupportedFileTypeError) as error:
            validate_extension(filename, upload_settings)
        assert error.value.http_status == 415


class TestValidateContentType:
    @pytest.mark.parametrize(
        ("extension", "content_type"),
        [
            (".pdf", "application/pdf"),
            (".txt", "text/plain"),
            (".txt", "text/plain; charset=utf-8"),
            (
                ".docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (".pdf", "application/octet-stream"),
            (".pdf", None),
        ],
    )
    def test_accepted(self, extension: str, content_type: str | None) -> None:
        validate_content_type(extension, content_type)

    @pytest.mark.parametrize(
        ("extension", "content_type"),
        [(".pdf", "text/plain"), (".txt", "application/pdf"), (".pdf", "image/png")],
    )
    def test_mismatch_rejected(self, extension: str, content_type: str) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            validate_content_type(extension, content_type)


class TestValidateMagicBytes:
    def test_pdf_signature_accepted(self, pdf_bytes: bytes) -> None:
        validate_magic_bytes(".pdf", pdf_bytes)

    def test_docx_signature_accepted(self, docx_bytes: bytes) -> None:
        validate_magic_bytes(".docx", docx_bytes)

    def test_utf8_text_accepted(self, txt_bytes: bytes) -> None:
        validate_magic_bytes(".txt", txt_bytes)

    def test_executable_renamed_to_pdf_rejected(self) -> None:
        """The check a client cannot lie its way past."""
        with pytest.raises(UnsupportedFileTypeError):
            validate_magic_bytes(".pdf", b"MZ\x90\x00\x03\x00\x00\x00")

    def test_zip_renamed_to_pdf_rejected(self) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            validate_magic_bytes(".pdf", b"PK\x03\x04")

    def test_binary_renamed_to_txt_rejected(self) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            validate_magic_bytes(".txt", b"\xff\xfe\x00\x01\x80")


class TestValidateSize:
    def test_within_limit(self, upload_settings: Settings) -> None:
        validate_size(1024, upload_settings)

    def test_over_limit(self, upload_settings: Settings) -> None:
        with pytest.raises(FileTooLargeError) as error:
            validate_size(1025, upload_settings)
        assert error.value.http_status == 413

    @pytest.mark.parametrize("size", [0, -1])
    def test_empty_rejected(self, size: int, upload_settings: Settings) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            validate_size(size, upload_settings)


class TestValidateUpload:
    def test_happy_path(self, upload_settings: Settings, pdf_bytes: bytes) -> None:
        name, extension = validate_upload(
            filename="../invoice 2026.pdf",
            content_type="application/pdf",
            data=pdf_bytes,
            settings=upload_settings,
        )
        assert name == "invoice 2026.pdf"
        assert extension == ".pdf"

    def test_traversal_with_disallowed_extension_rejected(self, upload_settings: Settings) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            validate_upload(
                filename="../../etc/passwd",
                content_type="text/plain",
                data=b"root:x:0:0",
                settings=upload_settings,
            )

    def test_size_checked_before_magic_bytes(self, upload_settings: Settings) -> None:
        """An oversized payload is rejected on size, not on its content."""
        with pytest.raises(FileTooLargeError):
            validate_upload(
                filename="big.pdf",
                content_type="application/pdf",
                data=b"not-a-pdf" * 1000,
                settings=upload_settings,
            )


def test_sha256_matches_hashlib(pdf_bytes: bytes) -> None:
    assert sha256_hex(pdf_bytes) == hashlib.sha256(pdf_bytes).hexdigest()
