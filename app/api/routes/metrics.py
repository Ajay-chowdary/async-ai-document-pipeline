"""Operational metrics endpoint."""

from fastapi import APIRouter

from app.api.dependencies import QueueDep, SessionDep
from app.schemas.metrics import MetricsSummary
from app.services import metrics as metrics_service

router = APIRouter(tags=["metrics"])


@router.get(
    "/metrics-summary",
    response_model=MetricsSummary,
    summary="Job counts, latency and Redis backlog",
)
async def metrics_summary(session: SessionDep, queue: QueueDep) -> MetricsSummary:
    """Return a single payload for the dashboard header and ops probes."""
    return await metrics_service.build_metrics_summary(session, queue)
