"""Shared column types.

PostgreSQL enums are database-level objects, so the same ``document_type`` type
is referenced by two tables. Defining one instance here — rather than
constructing ``sa.Enum`` at each use site — means ``create_all`` and Alembic
both see a single type and neither tries to create it twice.

``values_callable`` makes the database store the enum *values* (``"support_ticket"``)
rather than the Python member names (``"SUPPORT_TICKET"``), keeping the column
readable in psql and identical to what the API emits.
"""

from sqlalchemy import Enum as SAEnum

from app.core.enums import DocumentType, JobStatus

DocumentTypeEnum: SAEnum = SAEnum(
    DocumentType,
    name="document_type",
    values_callable=lambda enum: [member.value for member in enum],
)

JobStatusEnum: SAEnum = SAEnum(
    JobStatus,
    name="job_status",
    values_callable=lambda enum: [member.value for member in enum],
)
