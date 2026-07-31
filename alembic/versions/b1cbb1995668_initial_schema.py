"""initial schema

Creates the three pipeline tables and the two PostgreSQL enum types they share.

The enum types are created and dropped explicitly rather than inline in
``create_table``. ``document_type`` is referenced by two tables, and an inline
declaration makes SQLAlchemy emit ``CREATE TYPE`` once per table, which fails
on the second with "type already exists". Autogenerate does not account for
that, so the generated body was adjusted by hand.

Revision ID: b1cbb1995668
Revises:
Create Date: 2026-07-30 12:50:46.605373
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1cbb1995668"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOCUMENT_TYPE_VALUES = ("invoice", "resume", "support_ticket", "generic")
JOB_STATUS_VALUES = ("queued", "processing", "retrying", "completed", "failed")

document_type = postgresql.ENUM(*DOCUMENT_TYPE_VALUES, name="document_type", create_type=False)
job_status = postgresql.ENUM(*JOB_STATUS_VALUES, name="job_status", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    document_type.create(bind, checkfirst=True)
    job_status.create(bind, checkfirst=True)

    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("stored_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("requested_document_type", document_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("stored_filename", name=op.f("uq_documents_stored_filename")),
    )
    op.create_index(op.f("ix_documents_checksum"), "documents", ["checksum"], unique=False)
    op.create_index(op.f("ix_documents_created_at"), "documents", ["created_at"], unique=False)

    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("status", job_status, server_default="queued", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default="3", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_duration_ms", sa.Integer(), nullable=True),
        sa.Column("consumer_name", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_processing_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_jobs")),
    )
    op.create_index(
        op.f("ix_processing_jobs_created_at"), "processing_jobs", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_processing_jobs_document_id"), "processing_jobs", ["document_id"], unique=False
    )
    # Serves the dashboard's "newest first, optionally filtered by status".
    op.create_index(
        "ix_processing_jobs_status_created_at",
        "processing_jobs",
        ["status", "created_at"],
        unique=False,
    )
    # Serves the stale-job sweep, which looks for long-running active jobs.
    op.create_index(
        "ix_processing_jobs_status_started_at",
        "processing_jobs",
        ["status", "started_at"],
        unique=False,
    )

    op.create_table(
        "extraction_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("detected_document_type", document_type, nullable=False),
        sa.Column("extracted_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("model_provider", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            name=op.f("fk_extraction_results_job_id_processing_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_results")),
        # One result per job, enforced by the database rather than relying only
        # on the worker's idempotency check.
        sa.UniqueConstraint("job_id", name=op.f("uq_extraction_results_job_id")),
    )
    op.create_index(
        op.f("ix_extraction_results_detected_document_type"),
        "extraction_results",
        ["detected_document_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_extraction_results_detected_document_type"), table_name="extraction_results"
    )
    op.drop_table("extraction_results")

    op.drop_index("ix_processing_jobs_status_started_at", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_status_created_at", table_name="processing_jobs")
    op.drop_index(op.f("ix_processing_jobs_document_id"), table_name="processing_jobs")
    op.drop_index(op.f("ix_processing_jobs_created_at"), table_name="processing_jobs")
    op.drop_table("processing_jobs")

    op.drop_index(op.f("ix_documents_created_at"), table_name="documents")
    op.drop_index(op.f("ix_documents_checksum"), table_name="documents")
    op.drop_table("documents")

    bind = op.get_bind()
    job_status.drop(bind, checkfirst=True)
    document_type.drop(bind, checkfirst=True)
