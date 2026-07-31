"""The uploaded file and its immutable metadata."""

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import DocumentType
from app.db.base import Base, CreatedAt, FileSize, UUIDPrimaryKey
from app.db.types import DocumentTypeEnum

if TYPE_CHECKING:
    from app.db.models.processing_job import ProcessingJob


class Document(Base):
    """An uploaded document.

    Rows are append-only: the file on disk and the metadata describing it never
    change after the upload transaction commits. Everything mutable about a
    document lives on its :class:`ProcessingJob`.
    """

    __tablename__ = "documents"

    id: Mapped[UUIDPrimaryKey]

    #: As supplied by the client, sanitized. Shown in the UI, never used as a path.
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Generated name actually used on disk, so client input never reaches a path.
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[FileSize]
    #: Backend-relative key, resolved by the storage implementation.
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    #: SHA-256 of the file bytes; indexed so duplicate uploads are identifiable.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: Set when the client names the type; when null the worker classifies.
    requested_document_type: Mapped[DocumentType | None] = mapped_column(
        DocumentTypeEnum, nullable=True
    )

    created_at: Mapped[CreatedAt]

    # The cascade set is spelled out rather than using "all": that shorthand
    # also includes refresh-expire, which would silently expire every loaded
    # job whenever the parent document is refreshed.
    jobs: Mapped[list["ProcessingJob"]] = relationship(
        back_populates="document",
        cascade="save-update, merge, delete, delete-orphan",
        order_by="ProcessingJob.created_at.desc()",
    )

    def __repr__(self) -> str:
        return f"<Document id={self.id} filename={self.original_filename!r}>"
