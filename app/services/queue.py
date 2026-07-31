"""Redis Streams queue: publishing, consuming, acknowledging and retry scheduling.

Why Streams and not Pub/Sub. Pub/Sub delivers to whoever is connected at that
instant and then forgets. Restart a worker and every message published during
the gap is gone, with no acknowledgement, no redelivery and no way to see a
backlog. A Stream is a durable log: a consumer group hands each entry to
exactly one member, tracks it in the pending entries list until it is
acknowledged, and lets another consumer claim it if the first one dies.
``XLEN`` and ``XPENDING`` then give real queue-depth metrics for free.

Delayed retries live in a sorted set scored by their due time, swept back onto
the stream when they come due. The alternative — sleeping inside the message
handler — occupies a worker slot for the whole backoff and loses the retry
entirely if the process dies mid-sleep.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, cast

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError, ResponseError

from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError, MalformedEventError, QueueError
from app.core.logging import get_logger
from app.core.time import utcnow
from app.schemas.events import EVENT_VERSION, PAYLOAD_FIELD, DocumentProcessingEvent

logger = get_logger(__name__)

#: Raised by ``XGROUP CREATE`` when the group is already there. Every worker
#: attempts creation on startup, so this is the normal case, not an error.
_GROUP_EXISTS = "BUSYGROUP"

#: Headroom added to the socket read timeout on top of the blocking-read
#: duration, so the server's own block expiry always wins the race.
SOCKET_TIMEOUT_MARGIN_SECONDS = 10.0

SOCKET_CONNECT_TIMEOUT_SECONDS = 5.0

#: Pops every retry whose due time has passed, in one round trip. Doing this as
#: separate ZRANGEBYSCORE and ZREM calls would let two workers claim the same
#: entry and publish it twice.
_POP_DUE_RETRIES = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
if #due > 0 then
    redis.call('ZREM', KEYS[1], unpack(due))
end
return due
"""


@dataclass(frozen=True, slots=True)
class DeliveredEvent:
    """A stream entry handed to this consumer, with its redelivery history."""

    message_id: str
    event: DocumentProcessingEvent
    #: How many times Redis has delivered this entry. Greater than one means a
    #: previous consumer took it and never acknowledged it.
    delivery_count: int = 1


@dataclass(frozen=True, slots=True)
class QueueDepth:
    """A point-in-time view of the backlog, for ``/metrics-summary``."""

    stream_length: int
    pending: int
    scheduled_retries: int


class RedisQueue:
    """All Redis access for the pipeline.

    One class rather than loose functions because the connection, the stream
    name, the group name and the Lua script all have the same lifetime, and
    tests can then point a worker at an isolated database by constructing
    another instance.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Redis | None = None
        self._pop_due_retries: Any = None

    @property
    def settings(self) -> Settings:
        """The configuration this queue was built with."""
        return self._settings

    @property
    def stream(self) -> str:
        return self._settings.redis_stream_name

    @property
    def group(self) -> str:
        return self._settings.redis_consumer_group

    @property
    def retry_key(self) -> str:
        return self._settings.redis_retry_zset

    @property
    def client(self) -> Redis:
        """The connected client.

        Raises:
            QueueError: :meth:`connect` has not been called.
        """
        if self._client is None:
            raise QueueError("The Redis queue is not connected.")
        return self._client

    # -- Lifecycle --------------------------------------------------------

    @property
    def _socket_timeout_seconds(self) -> float:
        """Socket read timeout, kept safely above the ``XREADGROUP`` block time.

        ``XREADGROUP ... BLOCK n`` parks the connection for up to ``n``
        milliseconds. If the client's own read timeout is not comfortably
        longer, it fires first and an ordinary idle poll surfaces as a
        connection timeout — an error every block interval on a healthy,
        simply idle worker.
        """
        return (self._settings.redis_block_ms / 1000) + SOCKET_TIMEOUT_MARGIN_SECONDS

    async def connect(self) -> None:
        """Open the connection, retrying until Redis answers or the budget runs out.

        Like the database, Redis is waited for rather than assumed: under
        Compose the worker regularly wins the race against it.

        Raises:
            DependencyUnavailableError: Redis never became reachable.
        """
        client: Redis = Redis.from_url(
            self._settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_timeout=self._socket_timeout_seconds,
            socket_connect_timeout=SOCKET_CONNECT_TIMEOUT_SECONDS,
        )
        last_error: Exception | None = None

        for attempt in range(1, self._settings.redis_connect_max_attempts + 1):
            try:
                await client.ping()
            except (RedisConnectionError, OSError) as error:
                last_error = error
                logger.warning(
                    "queue.connect_retry",
                    attempt=attempt,
                    max_attempts=self._settings.redis_connect_max_attempts,
                    url=self._settings.safe_redis_url,
                    error=type(error).__name__,
                )
                await asyncio.sleep(self._settings.redis_connect_retry_seconds)
            else:
                self._client = client
                self._pop_due_retries = client.register_script(_POP_DUE_RETRIES)
                logger.info("queue.connected", url=self._settings.safe_redis_url, attempt=attempt)
                return

        await client.aclose()
        raise DependencyUnavailableError(
            f"Redis unreachable after {self._settings.redis_connect_max_attempts} attempts",
            details={"dependency": "redis"},
        ) from last_error

    async def close(self) -> None:
        """Release the connection."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ensure_group(self) -> None:
        """Create the stream and consumer group if they do not exist.

        ``MKSTREAM`` means the very first worker can start before any producer
        has published, which is the normal order under Compose.
        """
        try:
            await self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as error:
            if _GROUP_EXISTS not in str(error):
                raise QueueError("Failed to create the consumer group.") from error
            logger.debug("queue.group_exists", stream=self.stream, group=self.group)
        else:
            logger.info("queue.group_created", stream=self.stream, group=self.group)

    async def health(self) -> bool:
        """Return whether Redis answers a ping; never raises."""
        try:
            await self.client.ping()
        except (RedisError, OSError, QueueError) as error:
            logger.warning("queue.healthcheck_failed", error=type(error).__name__)
            return False
        return True

    # -- Producing --------------------------------------------------------

    async def publish(self, event: DocumentProcessingEvent) -> str:
        """Append an event to the stream and return its message ID.

        Raises:
            QueueError: the append failed; retryable by the caller.
        """
        # redis-py types the field mapping with an invariant dict of its own
        # union alias. The cast keeps that detail here, in the only module that
        # knows about Redis, rather than leaking it into the event schema.
        fields = cast(dict[Any, Any], event.to_stream_fields())
        try:
            message_id = await self.client.xadd(self.stream, fields)
        except (RedisError, OSError) as error:
            raise QueueError("Failed to publish the processing event.") from error

        logger.info("queue.published", message_id=message_id, **event.log_context())
        return str(message_id)

    async def schedule_retry(self, event: DocumentProcessingEvent, delay_seconds: float) -> None:
        """Park an event in the retry set, due ``delay_seconds`` from now.

        Raises:
            QueueError: the write failed.
        """
        due_at = utcnow().timestamp() + delay_seconds
        try:
            await self.client.zadd(self.retry_key, {event.model_dump_json(): due_at})
        except (RedisError, OSError) as error:
            raise QueueError("Failed to schedule the retry.") from error

        logger.info(
            "queue.retry_scheduled",
            delay_seconds=round(delay_seconds, 2),
            **event.log_context(),
        )

    async def pop_due_retries(self, limit: int = 50) -> list[DocumentProcessingEvent]:
        """Atomically remove and return every retry whose due time has passed."""
        try:
            raw = await self._pop_due_retries(
                keys=[self.retry_key], args=[utcnow().timestamp(), limit]
            )
        except (RedisError, OSError) as error:
            raise QueueError("Failed to read the retry schedule.") from error

        events: list[DocumentProcessingEvent] = []
        for payload in cast(list[str], raw or []):
            try:
                events.append(DocumentProcessingEvent.model_validate_json(payload))
            except ValidationError:
                # A scheduled entry we can no longer parse is dropped rather
                # than re-scheduled forever.
                logger.error("queue.retry_undecodable")
        return events

    # -- Consuming --------------------------------------------------------

    async def read(self, *, consumer_name: str) -> list[DeliveredEvent]:
        """Block for new entries addressed to this consumer.

        Returns an empty list when the block timeout expires, which is what
        gives the run loop a regular chance to notice a shutdown request.
        """
        try:
            response = await self.client.xreadgroup(
                groupname=self.group,
                consumername=consumer_name,
                streams={self.stream: ">"},
                count=self._settings.redis_read_count,
                block=self._settings.redis_block_ms,
            )
        except (RedisError, OSError) as error:
            raise QueueError("Failed to read from the stream.") from error

        return await self._decode_entries(response)

    async def claim_stale(self, *, consumer_name: str) -> list[DeliveredEvent]:
        """Take over entries a previous consumer accepted but never acknowledged.

        ``XPENDING`` first, then ``XCLAIM``, rather than the single-call
        ``XAUTOCLAIM``: only ``XPENDING`` reports how many times an entry has
        been delivered, and that count is what identifies a poison message that
        keeps killing whichever worker picks it up.
        """
        try:
            pending = await self.client.xpending_range(
                name=self.stream,
                groupname=self.group,
                min="-",
                max="+",
                count=self._settings.redis_read_count,
                idle=self._settings.pending_min_idle_ms,
            )
            if not pending:
                return []

            # XPENDING reports deliveries *so far*; the XCLAIM below adds one
            # more. Adding it here keeps the count consistent with a normal
            # XREADGROUP delivery, where the first read reports 1.
            delivery_counts = {
                str(entry["message_id"]): int(entry["times_delivered"]) + 1 for entry in pending
            }
            claimed = await self.client.xclaim(
                name=self.stream,
                groupname=self.group,
                consumername=consumer_name,
                min_idle_time=self._settings.pending_min_idle_ms,
                message_ids=list(delivery_counts),
            )
        except (RedisError, OSError) as error:
            raise QueueError("Failed to claim pending messages.") from error

        delivered = await self._decode_messages(claimed, delivery_counts)
        if delivered:
            logger.warning(
                "queue.claimed_stale",
                consumer=consumer_name,
                count=len(delivered),
                message_ids=[item.message_id for item in delivered],
            )
        return delivered

    async def ack(self, message_id: str) -> None:
        """Acknowledge an entry, removing it from the pending list.

        Called only after the outcome is durable in PostgreSQL. Acknowledging
        earlier would mean a crash between the ack and the commit loses the job
        with no trace in the pending list.
        """
        try:
            await self.client.xack(self.stream, self.group, message_id)
        except (RedisError, OSError) as error:
            raise QueueError("Failed to acknowledge the message.") from error

    async def depth(self) -> QueueDepth:
        """Return current backlog counters."""
        try:
            stream_length = await self.client.xlen(self.stream)
            pending = await self.client.xpending(self.stream, self.group)
            scheduled = await self.client.zcard(self.retry_key)
        except (RedisError, OSError) as error:
            raise QueueError("Failed to read queue depth.") from error

        return QueueDepth(
            stream_length=int(stream_length),
            pending=int(pending["pending"]) if pending else 0,
            scheduled_retries=int(scheduled),
        )

    # -- Decoding ---------------------------------------------------------

    async def _decode_entries(self, response: Any) -> list[DeliveredEvent]:
        """Flatten an ``XREADGROUP`` response into events."""
        delivered: list[DeliveredEvent] = []
        for _stream_name, messages in response or []:
            delivered.extend(await self._decode_messages(messages, {}))
        return delivered

    async def _decode_messages(
        self, messages: Any, delivery_counts: dict[str, int]
    ) -> list[DeliveredEvent]:
        delivered: list[DeliveredEvent] = []
        for message_id, fields in messages or []:
            identifier = str(message_id)
            try:
                event = self._decode_one(fields)
            except MalformedEventError as error:
                # Poison message: acknowledge and drop. Leaving it pending
                # means it is redelivered forever, and no amount of retrying
                # will make an unparseable payload parse.
                logger.error("queue.event_undecodable", message_id=identifier, reason=error.message)
                await self.ack(identifier)
                continue
            delivered.append(
                DeliveredEvent(
                    message_id=identifier,
                    event=event,
                    delivery_count=delivery_counts.get(identifier, 1),
                )
            )
        return delivered

    def _decode_one(self, fields: dict[str, str]) -> DocumentProcessingEvent:
        payload = fields.get(PAYLOAD_FIELD)
        if payload is None:
            raise MalformedEventError("Stream entry has no payload field.")
        try:
            event = DocumentProcessingEvent.model_validate_json(payload)
        except ValidationError as error:
            raise MalformedEventError("Stream entry failed schema validation.") from error

        if event.event_version != EVENT_VERSION:
            raise MalformedEventError(
                f"Unsupported event version {event.event_version}.",
                details={"expected": EVENT_VERSION},
            )
        return event


_queue: RedisQueue | None = None


def get_queue(settings: Settings) -> RedisQueue:
    """Return the process-wide queue instance, created once."""
    global _queue
    if _queue is None:
        _queue = RedisQueue(settings)
    return _queue


def reset_queue() -> None:
    """Drop the cached queue, so tests can point it at another database."""
    global _queue
    _queue = None
