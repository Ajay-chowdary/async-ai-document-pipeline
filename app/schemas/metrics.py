"""Operational metrics returned by ``GET /metrics-summary``."""

from pydantic import BaseModel, Field


class MetricsSummary(BaseModel):
    """Job counts, latency hint and Redis backlog in one payload."""

    total_jobs: int
    queued: int
    processing: int
    retrying: int
    completed: int
    failed: int
    average_processing_duration_ms: float | None = Field(
        description="Mean of recorded processing_duration_ms values, or null when none."
    )
    approximate_success_rate: float | None = Field(
        description=(
            "completed / (completed + failed). Null when no terminal jobs exist. "
            "Ignores jobs still in flight."
        ),
    )
    jobs_last_24_hours: int
    stream_length: int = Field(description="Redis stream XLEN.")
    pending: int = Field(description="Entries in the consumer-group pending list.")
    scheduled_retries: int = Field(description="Events waiting in the delayed-retry sorted set.")
