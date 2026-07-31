"""Worker entrypoint.

Runs three cooperating loops in one event loop — the consumer, the pending
recovery sweeper and the retry scheduler — and coordinates their shutdown
through a single :class:`asyncio.Event`.

Shutdown is graceful by design. ``SIGTERM`` sets the event; each loop finishes
the event it is holding, drives it to a durable outcome, acknowledges it, and
only then exits. Nothing is abandoned mid-flight, which is what lets a
container be replaced during a deploy without stranding a document.
"""

import asyncio
import contextlib
import signal
import sys

from app import __version__
from app.core.config import Settings, get_settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, get_sessionmaker, wait_for_database
from app.llm.base import LLMProvider
from app.llm.factory import build_provider
from app.services.file_storage import get_file_storage
from app.services.queue import RedisQueue, get_queue
from app.worker.consumer import StreamConsumer, build_consumer_name
from app.worker.processor import DocumentProcessor
from app.worker.recovery import run_pending_recovery, run_retry_scheduler

logger = get_logger(__name__)


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Translate termination signals into a cooperative stop request."""
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_name, shutdown.set)


async def run_worker(settings: Settings, shutdown: asyncio.Event) -> None:
    """Start every dependency, run the loops, then release everything."""
    consumer_name = build_consumer_name()
    logger.info(
        "worker.starting",
        version=__version__,
        consumer=consumer_name,
        llm_provider=settings.llm_provider.value,
        max_retries=settings.max_retries,
    )

    await wait_for_database(settings)
    queue = get_queue(settings)
    await queue.connect()
    await queue.ensure_group()

    provider = build_provider(settings)
    processor = DocumentProcessor(
        sessionmaker=get_sessionmaker(settings),
        storage=get_file_storage(settings),
        queue=queue,
        provider=provider,
        settings=settings,
        consumer_name=consumer_name,
    )
    consumer = StreamConsumer(
        queue=queue, processor=processor, settings=settings, consumer_name=consumer_name
    )

    logger.info("worker.ready", consumer=consumer_name)
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(consumer.run(shutdown), name="consumer")
            tasks.create_task(
                run_pending_recovery(
                    queue=queue,
                    consumer=consumer,
                    settings=settings,
                    consumer_name=consumer_name,
                    shutdown=shutdown,
                ),
                name="pending-recovery",
            )
            tasks.create_task(
                run_retry_scheduler(queue=queue, settings=settings, shutdown=shutdown),
                name="retry-scheduler",
            )
    finally:
        await _shutdown(queue, provider)
        logger.info("worker.stopped", consumer=consumer_name, processed=consumer.processed)


async def _shutdown(queue: RedisQueue, provider: LLMProvider) -> None:
    """Release connections, reporting rather than raising on failure."""
    for name, close in (
        ("llm", provider.aclose),
        ("queue", queue.close),
        ("database", dispose_engine),
    ):
        try:
            await close()
        except Exception:
            logger.exception("worker.shutdown_error", resource=name)


async def main() -> int:
    """Configure logging, run the worker, and map failures to an exit code."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    shutdown = asyncio.Event()
    _install_signal_handlers(shutdown)

    try:
        await run_worker(settings, shutdown)
    except AppError as error:
        # A dependency never came up, or the provider is misconfigured. Exit
        # non-zero so the orchestrator restarts rather than leaving a live
        # container that processes nothing.
        logger.error("worker.startup_failed", code=error.code, message=error.message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
