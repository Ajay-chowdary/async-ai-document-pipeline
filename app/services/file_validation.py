"""Upload validation: filename sanitization, type checks and checksums.

Three independent checks run on every upload, because each one alone is
bypassable:

1. **Extension** — cheap, and drives which parser is selected later.
2. **Declared MIME type** — client-supplied and therefore untrusted, but it
   catches honest mistakes.
3. **Magic bytes** — the only check the client cannot lie about, so a ``.exe``
   renamed to ``.pdf`` is rejected before it is ever written to disk.
"""

import hashlib
import re
import unicodedata
from pathlib import PurePosixPath, PureWindowsPath

from app.core.config import Settings
from app.core.exceptions import FileTooLargeError, UnsupportedFileTypeError

#: Content types accepted per extension. Browsers and CLI clients disagree on
#: these, so several spellings are tolerated for the same format.
EXTENSION_CONTENT_TYPES: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf", "application/x-pdf"}),
    ".txt": frozenset({"text/plain", "text/x-log", "text/markdown"}),
    ".docx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/zip",
        }
    ),
}

#: A generic type sent by ``curl --data-binary`` and several upload widgets.
#: Permitted because the magic-byte check below is the real gate.
GENERIC_CONTENT_TYPES = frozenset({"application/octet-stream", ""})

#: Leading bytes every well-formed file of a given type must have. TXT has no
#: signature, so it is validated by attempting a UTF-8 decode instead.
MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
}

MAX_FILENAME_LENGTH = 200
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]")
_REPEATED_SEPARATORS = re.compile(r"[-_ ]{2,}")
FALLBACK_FILENAME = "document"


def sanitize_filename(filename: str | None) -> str:
    """Reduce a client-supplied filename to something safe to store and display.

    Directory components are discarded rather than escaped: the result is never
    used to build a path — see :mod:`app.services.file_storage` — but a name
    containing ``../`` should not survive into the database or the UI either.
    """
    if not filename:
        return FALLBACK_FILENAME

    # Strip directory parts under both path flavours, so "..\\..\\evil.pdf"
    # from a Windows client is handled the same as its POSIX equivalent.
    name = PureWindowsPath(PurePosixPath(filename).name).name

    # Normalise unicode, then drop anything that is not plain ASCII, so
    # right-to-left overrides and homoglyph tricks cannot reach the UI.
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")

    name = _UNSAFE_CHARS.sub("_", name).strip(" ._")
    name = _REPEATED_SEPARATORS.sub("_", name)

    if not name:
        return FALLBACK_FILENAME

    if len(name) > MAX_FILENAME_LENGTH:
        stem, _, extension = name.rpartition(".")
        if stem and len(extension) <= 10:
            name = f"{stem[: MAX_FILENAME_LENGTH - len(extension) - 1]}.{extension}"
        else:
            name = name[:MAX_FILENAME_LENGTH]

    return name


def extract_extension(filename: str) -> str:
    """Return the lower-cased extension of a filename, including the dot."""
    suffix = PurePosixPath(filename).suffix.lower()
    return suffix


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 digest of the given bytes."""
    return hashlib.sha256(data).hexdigest()


def validate_extension(filename: str, settings: Settings) -> str:
    """Return the validated extension, or reject the upload.

    Raises:
        UnsupportedFileTypeError: the extension is missing or not allowed.
    """
    extension = extract_extension(filename)
    if not extension:
        raise UnsupportedFileTypeError(
            "The uploaded file has no extension.",
            details={"allowed": sorted(settings.allowed_extensions)},
        )
    if extension not in settings.allowed_extensions:
        raise UnsupportedFileTypeError(
            f"Files of type {extension} are not supported.",
            details={"extension": extension, "allowed": sorted(settings.allowed_extensions)},
        )
    return extension


def validate_content_type(extension: str, content_type: str | None) -> None:
    """Reject a declared MIME type that contradicts the extension.

    Raises:
        UnsupportedFileTypeError: the declared type cannot describe this extension.
    """
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared in GENERIC_CONTENT_TYPES:
        return
    permitted = EXTENSION_CONTENT_TYPES.get(extension, frozenset())
    if declared not in permitted:
        raise UnsupportedFileTypeError(
            f"Content type {declared!r} does not match the {extension} extension.",
            details={"content_type": declared, "expected": sorted(permitted)},
        )


def validate_magic_bytes(extension: str, data: bytes) -> None:
    """Reject content whose leading bytes contradict its claimed format.

    Raises:
        UnsupportedFileTypeError: the payload is not the format it claims to be.
    """
    if extension == ".txt":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UnsupportedFileTypeError(
                "The .txt file is not valid UTF-8 text.",
                details={"extension": extension},
            ) from error
        return

    prefixes = MAGIC_PREFIXES.get(extension)
    if prefixes and not data.startswith(prefixes):
        raise UnsupportedFileTypeError(
            f"The file content does not look like a valid {extension} file.",
            details={"extension": extension},
        )


def validate_size(size: int, settings: Settings) -> None:
    """Reject an empty or oversized upload.

    Raises:
        UnsupportedFileTypeError: the upload is empty.
        FileTooLargeError: the upload exceeds the configured limit.
    """
    if size <= 0:
        raise UnsupportedFileTypeError("The uploaded file is empty.")
    if size > settings.max_upload_bytes:
        raise FileTooLargeError(
            f"The file exceeds the {settings.max_upload_mb} MB limit.",
            details={"size_bytes": size, "max_bytes": settings.max_upload_bytes},
        )


def validate_upload(
    *, filename: str | None, content_type: str | None, data: bytes, settings: Settings
) -> tuple[str, str]:
    """Run every upload check and return ``(sanitized_filename, extension)``.

    Ordering is intentional: the cheap checks run before the expensive ones, and
    nothing touches the filesystem until all of them have passed.
    """
    safe_name = sanitize_filename(filename)
    extension = validate_extension(safe_name, settings)
    validate_content_type(extension, content_type)
    validate_size(len(data), settings)
    validate_magic_bytes(extension, data)
    return safe_name, extension
