"""Liveness and readiness probes.

The two are deliberately different. ``/health`` answers "is this process
alive", touches nothing external, and must never fail because a dependency is
down — otherwise an orchestrator restarts a healthy API during a database
blip. ``/ready`` answers "can this process serve traffic", checks dependencies,
and returns 503 when it cannot.
"""

from typing import Any

from fastapi import APIRouter, Response, status

from app.api.dependencies import QueueDep
from app.db.session import check_database

router = APIRouter(tags=["operations"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Report that the process is running."""
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe")
async def ready(response: Response, queue: QueueDep) -> dict[str, Any]:
    """Report whether every dependency needed to serve requests is reachable.

    Redis is included because an upload that cannot be published is an upload
    that will never be processed; the API is not ready without it.
    """
    checks = {"database": await check_database(), "redis": await queue.health()}
    all_ready = all(checks.values())
    if not all_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if all_ready else "not_ready", "checks": checks}
