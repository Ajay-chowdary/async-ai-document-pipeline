"""ORM models.

Importing this package registers every model on ``Base.metadata``, which is
what Alembic's autogenerate compares against the live database. Adding a model
without importing it here would silently produce empty migrations.
"""

from app.db.models.document import Document
from app.db.models.extraction_result import ExtractionResult
from app.db.models.processing_job import ProcessingJob

__all__ = ["Document", "ExtractionResult", "ProcessingJob"]
