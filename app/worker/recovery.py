"""Background sweepers that keep the queue honest.

Two loops run alongside the main consumer:

**Pending recovery** finds entries a consumer accepted but never acknowledged —
the signature of a worker that was killed mid-job — and reprocesses them. This
is the half of at-least-once delivery that the ``XACK``-after-commit rule
depends on: without it, a crash would strand the job forever.

**Retry scheduling** moves events out of the delayed-retry sorted set and back
onto the stream once they come due.

Both are deliberately separate from the consume loop. Folding them in would
mean a slow sweep delays new work, and a blocked ``XREADGROUP`` delays the
sweep.
"""

import asyncio

from app.core.config import Settings
from app.core.exceptions import QueueError
from app.core.logging import get_logger
from app.services.queue import RedisQueue
from app.worker.consumer import StreamConsumer

logger = get_logger(__name__)


async def run_pending_recovery(
    *,
    queue: RedisQueue,
    consumer: StreamConsumer,
    settings: Settings,
    consumer_name: str,
    shutdown: asyncio.Event,
) -> None:
    """Periodically claim and reprocess abandoned entries."""
    logger.info(
        "worker.recovery_started",
        min_idle_ms=settings.pending_min_idle_ms,
        interval_seconds=settings.pending_sweep_interval_seconds,
    )

    while not shutdown.is_set():
        await _sleep_or_stop(shutdown, settings.pending_sweep_interval_seconds)
        if shutdown.is_set():
            break

        try:
            stale = await queue.claim_stale(consumer_name=consumer_name)
        except QueueError:
            logger.exception("worker.recovery_claim_failed")
            continue

        for delivered in stale:
            await consumer.handle(delivered)

    logger.info("worker.recovery_stopped")


async def run_retry_scheduler(
    *, queue: RedisQueue, settings: Settings, shutdown: asyncio.Event
) -> None:
    """Move due retries from the delayed set back onto the stream."""
    logger.info(
        "worker.retry_scheduler_started",
        interval_seconds=settings.retry_sweep_interval_seconds,
    )

    while not shutdown.is_set():
        await _sleep_or_stop(shutdown, settings.retry_sweep_interval_seconds)
        if shutdown.is_set():
            break

        try:
            due = await queue.pop_due_retries()
            for event in due:
                await queue.publish(event)
        except QueueError:
            # The pop is atomic, so a failure here has either taken the events
            # and will publish them, or taken nothing. A publish failure after
            # a successful pop does lose the retry; the job stays in
            # ``retrying`` and needs a manual retry, which is recorded plainly
            # in the README's limitations.
            logger.exception("worker.retry_sweep_failed")

    logger.info("worker.retry_scheduler_stopped")


async def _sleep_or_stop(shutdown: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately if shutdown is requested."""
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except TimeoutError:
        return
