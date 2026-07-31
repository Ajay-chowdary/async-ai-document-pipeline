"""Aggregate job and queue counters for the ops dashboard."""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import JobStatus
from app.core.time import utcnow
from app.db.models import ProcessingJob
from app.schemas.metrics import MetricsSummary
from app.services.queue import RedisQueue


async def build_metrics_summary(session: AsyncSession, queue: RedisQueue) -> MetricsSummary:
    """Return job tallies plus the current Redis backlog.

    Success rate uses only terminal jobs so in-flight work does not drag the
    ratio toward zero during a burst of uploads.
    """
    rows = (
        await session.execute(
            select(ProcessingJob.status, func.count()).group_by(ProcessingJob.status)
        )
    ).all()
    by_status: dict[JobStatus, int] = {status: int(count) for status, count in rows}
    counts = {status: by_status.get(status, 0) for status in JobStatus}

    avg_duration = (
        await session.execute(
            select(func.avg(ProcessingJob.processing_duration_ms)).where(
                ProcessingJob.processing_duration_ms.is_not(None)
            )
        )
    ).scalar_one()

    since = utcnow() - timedelta(hours=24)
    jobs_last_24_hours = (
        await session.execute(
            select(func.count()).select_from(ProcessingJob).where(ProcessingJob.created_at >= since)
        )
    ).scalar_one()

    completed = counts[JobStatus.COMPLETED]
    failed = counts[JobStatus.FAILED]
    terminal = completed + failed
    success_rate = (completed / terminal) if terminal else None
    depth = await queue.depth()

    return MetricsSummary(
        total_jobs=sum(counts.values()),
        queued=counts[JobStatus.QUEUED],
        processing=counts[JobStatus.PROCESSING],
        retrying=counts[JobStatus.RETRYING],
        completed=completed,
        failed=failed,
        average_processing_duration_ms=(
            round(float(avg_duration), 2) if avg_duration is not None else None
        ),
        approximate_success_rate=(round(success_rate, 4) if success_rate is not None else None),
        jobs_last_24_hours=int(jobs_last_24_hours),
        stream_length=depth.stream_length,
        pending=depth.pending,
        scheduled_retries=depth.scheduled_retries,
    )
