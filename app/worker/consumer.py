"""The main consume loop.

``XREADGROUP`` blocks for ``REDIS_BLOCK_MS`` rather than polling, so an idle
worker costs nothing. That timeout is also the upper bound on how long a
shutdown takes: the loop cannot notice the stop signal while it is parked in
Redis, so the block duration and the container's grace period are related
numbers.
"""

import asyncio
import os
import socket
import uuid

from app.core.config import Settings
from app.core.exceptions import QueueError
from app.core.logging import clear_context, get_logger
from app.services.queue import DeliveredEvent, RedisQueue
from app.worker.processor import DocumentProcessor, ProcessingOutcome

logger = get_logger(__name__)

#: Pause after a queue-level error, so a Redis outage produces a slow retry
#: loop rather than a hot one.
ERROR_BACKOFF_SECONDS = 2.0


def build_consumer_name() -> str:
    """Return a name unique to this worker process.

    Redis tracks pending entries per consumer name. Two processes sharing a
    name would appear as one consumer and could claim each other's in-flight
    work, so the hostname, PID and a random suffix are all included — the
    suffix because a restarted container can reuse both of the others.
    """
    return f"worker-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class StreamConsumer:
    """Reads events for one worker process and hands them to the processor."""

    def __init__(
        self,
        *,
        queue: RedisQueue,
        processor: DocumentProcessor,
        settings: Settings,
        consumer_name: str,
    ) -> None:
        self._queue = queue
        self._processor = processor
        self._settings = settings
        self._consumer_name = consumer_name
        self._processed = 0

    @property
    def processed(self) -> int:
        """Events handled since startup."""
        return self._processed

    async def run(self, shutdown: asyncio.Event) -> None:
        """Consume until ``shutdown`` is set.

        The stop is checked between events, never mid-event: a job that has
        started is always driven to a durable outcome before the worker exits,
        so shutting down never orphans work.
        """
        logger.info("worker.consumer_started", consumer=self._consumer_name)

        while not shutdown.is_set():
            try:
                delivered = await self._queue.read(consumer_name=self._consumer_name)
            except QueueError:
                logger.exception("worker.read_failed")
                await _sleep_or_stop(shutdown, ERROR_BACKOFF_SECONDS)
                continue

            for item in delivered:
                await self.handle(item)

        logger.info(
            "worker.consumer_stopped", consumer=self._consumer_name, processed=self._processed
        )

    async def handle(self, delivered: DeliveredEvent) -> ProcessingOutcome | None:
        """Process one event and acknowledge it.

        Returns ``None`` when processing raised, in which case the entry is
        deliberately left unacknowledged so the recovery sweeper retries it.
        """
        try:
            outcome = await self._processor.process(delivered)
        except Exception:
            logger.exception("worker.process_crashed", message_id=delivered.message_id)
            return None
        finally:
            clear_context()

        # Every outcome is durable in PostgreSQL by this point, so the entry
        # can safely leave the pending list.
        await self._queue.ack(delivered.message_id)
        self._processed += 1
        logger.info("worker.event_handled", message_id=delivered.message_id, outcome=outcome.value)
        return outcome


async def _sleep_or_stop(shutdown: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately if shutdown is requested."""
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except TimeoutError:
        return
