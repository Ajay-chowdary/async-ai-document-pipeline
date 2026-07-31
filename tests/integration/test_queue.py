"""Redis Streams behaviour: publishing, consuming, acking, recovery, retries."""

import asyncio
import uuid

import pytest

from app.core.enums import DocumentType
from app.core.time import utcnow
from app.schemas.events import PAYLOAD_FIELD, DocumentProcessingEvent
from app.services.queue import RedisQueue

pytestmark = pytest.mark.integration

CONSUMER = "worker-test-1"


def make_everything_claimable(queue: RedisQueue) -> None:
    """Drop the idle threshold so the recovery path is testable without waiting."""
    queue._settings = queue.settings.model_copy(update={"pending_min_idle_ms": 0})


def make_event(**overrides: object) -> DocumentProcessingEvent:
    defaults: dict[str, object] = {
        "job_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "storage_path": "2026/07/30/abc.pdf",
        "correlation_id": "corr-1",
    }
    return DocumentProcessingEvent(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestGroupSetup:
    async def test_ensure_group_is_idempotent(self, queue: RedisQueue) -> None:
        """Every worker calls this on startup; only the first one creates it."""
        await queue.ensure_group()
        await queue.ensure_group()

        groups = await queue.client.xinfo_groups(queue.stream)
        assert [group["name"] for group in groups] == [queue.group]

    async def test_stream_is_created_before_any_producer(self, queue: RedisQueue) -> None:
        """A worker can start first; MKSTREAM means it does not have to wait."""
        assert await queue.client.exists(queue.stream) == 1


class TestPublishAndConsume:
    async def test_round_trip(self, queue: RedisQueue) -> None:
        event = make_event(requested_document_type=DocumentType.INVOICE)
        await queue.publish(event)

        delivered = await queue.read(consumer_name=CONSUMER)

        assert len(delivered) == 1
        assert delivered[0].event == event
        assert delivered[0].delivery_count == 1

    async def test_read_returns_empty_when_idle(self, queue: RedisQueue) -> None:
        """The block timeout expiring is how the run loop notices shutdown."""
        assert await queue.read(consumer_name=CONSUMER) == []

    async def test_idle_block_expiry_is_not_an_error(self, queue: RedisQueue) -> None:
        """An idle worker must not log an error every block interval.

        Regression test. The socket read timeout has to exceed the
        ``XREADGROUP`` block duration; when it did not, a healthy idle worker
        raised a connection timeout on every poll. A short block time hid the
        bug, so this uses one long enough for the two timeouts to race.
        """
        slow = RedisQueue(queue.settings.model_copy(update={"redis_block_ms": 1_500}))
        await slow.connect()
        try:
            started = utcnow()
            assert await slow.read(consumer_name=CONSUMER) == []
            waited = (utcnow() - started).total_seconds()
        finally:
            await slow.close()

        # It really blocked rather than returning instantly by another route.
        assert waited >= 1.0

    async def test_socket_timeout_exceeds_the_block_duration(self, queue: RedisQueue) -> None:
        """The invariant behind the test above, asserted directly."""
        assert queue._socket_timeout_seconds > queue.settings.redis_block_ms / 1000

    async def test_two_consumers_split_the_work(self, queue: RedisQueue) -> None:
        """A consumer group load-balances; it does not broadcast."""
        for _ in range(2):
            await queue.publish(make_event())

        first = await queue.read(consumer_name="worker-a")
        second = await queue.read(consumer_name="worker-b")

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].message_id != second[0].message_id

    async def test_unacked_messages_stay_pending(self, queue: RedisQueue) -> None:
        await queue.publish(make_event())
        await queue.read(consumer_name=CONSUMER)

        assert (await queue.depth()).pending == 1

    async def test_ack_clears_the_pending_entry(self, queue: RedisQueue) -> None:
        await queue.publish(make_event())
        delivered = await queue.read(consumer_name=CONSUMER)

        await queue.ack(delivered[0].message_id)

        assert (await queue.depth()).pending == 0

    async def test_acked_messages_are_not_redelivered(self, queue: RedisQueue) -> None:
        await queue.publish(make_event())
        delivered = await queue.read(consumer_name=CONSUMER)
        await queue.ack(delivered[0].message_id)

        assert await queue.read(consumer_name=CONSUMER) == []

    async def test_messages_survive_with_no_consumer_connected(self, queue: RedisQueue) -> None:
        """The property Pub/Sub cannot provide: nothing is lost while the
        worker fleet is restarting."""
        for _ in range(3):
            await queue.publish(make_event())

        await asyncio.sleep(0.05)
        delivered = []
        for _ in range(3):
            delivered.extend(await queue.read(consumer_name=CONSUMER))

        assert len(delivered) == 3


class TestPoisonMessages:
    async def test_unparseable_payload_is_acked_and_dropped(self, queue: RedisQueue) -> None:
        """Redelivering a payload that can never parse wastes the whole fleet."""
        await queue.client.xadd(queue.stream, {PAYLOAD_FIELD: "{not valid json"})

        assert await queue.read(consumer_name=CONSUMER) == []
        assert (await queue.depth()).pending == 0

    async def test_missing_payload_field_is_dropped(self, queue: RedisQueue) -> None:
        await queue.client.xadd(queue.stream, {"unexpected": "shape"})

        assert await queue.read(consumer_name=CONSUMER) == []
        assert (await queue.depth()).pending == 0

    async def test_unknown_event_version_is_dropped(self, queue: RedisQueue) -> None:
        """Forward compatibility: a newer producer must not crash-loop an old
        consumer."""
        future = make_event().model_dump_json()
        await queue.client.xadd(
            queue.stream, {PAYLOAD_FIELD: future.replace('"event_version":1', '"event_version":99')}
        )

        assert await queue.read(consumer_name=CONSUMER) == []
        assert (await queue.depth()).pending == 0

    async def test_a_poison_message_does_not_block_good_ones(self, queue: RedisQueue) -> None:
        await queue.client.xadd(queue.stream, {PAYLOAD_FIELD: "garbage"})
        event = make_event()
        await queue.publish(event)

        # The first read consumes and discards the poison entry, returning
        # nothing; the valid message is still waiting behind it.
        assert await queue.read(consumer_name=CONSUMER) == []
        delivered = await queue.read(consumer_name=CONSUMER)

        assert [item.event.job_id for item in delivered] == [event.job_id]


class TestPendingRecovery:
    async def test_idle_messages_are_claimable(self, queue: RedisQueue) -> None:
        """The crashed-worker path: entry accepted, never acknowledged."""
        await queue.publish(make_event())
        await queue.read(consumer_name="worker-that-died")

        await asyncio.sleep(0.05)
        make_everything_claimable(queue)
        claimed = await queue.claim_stale(consumer_name="worker-alive")

        assert len(claimed) == 1
        assert claimed[0].delivery_count == 2

    async def test_fresh_messages_are_not_stolen(self, queue: RedisQueue) -> None:
        """A worker that is still alive keeps its in-flight job."""
        await queue.publish(make_event())
        await queue.read(consumer_name="worker-busy")

        assert await queue.claim_stale(consumer_name="worker-other") == []

    async def test_nothing_to_claim_is_not_an_error(self, queue: RedisQueue) -> None:
        assert await queue.claim_stale(consumer_name=CONSUMER) == []

    async def test_delivery_count_grows_with_each_claim(self, queue: RedisQueue) -> None:
        """The signal that identifies a message that keeps killing workers."""
        await queue.publish(make_event())
        await queue.read(consumer_name="worker-1")
        make_everything_claimable(queue)

        first = await queue.claim_stale(consumer_name="worker-2")
        second = await queue.claim_stale(consumer_name="worker-3")

        assert first[0].delivery_count == 2
        assert second[0].delivery_count == 3


class TestDelayedRetries:
    async def test_scheduled_retry_is_not_due_immediately(self, queue: RedisQueue) -> None:
        await queue.schedule_retry(make_event(), delay_seconds=60)

        assert await queue.pop_due_retries() == []
        assert (await queue.depth()).scheduled_retries == 1

    async def test_due_retry_is_popped(self, queue: RedisQueue) -> None:
        event = make_event()
        await queue.schedule_retry(event, delay_seconds=0)

        due = await queue.pop_due_retries()

        assert [item.job_id for item in due] == [event.job_id]

    async def test_popping_removes_the_entry(self, queue: RedisQueue) -> None:
        """Atomic pop: two sweepers must not both publish the same retry."""
        await queue.schedule_retry(make_event(), delay_seconds=0)

        first = await queue.pop_due_retries()
        second = await queue.pop_due_retries()

        assert len(first) == 1
        assert second == []
        assert (await queue.depth()).scheduled_retries == 0

    async def test_only_due_entries_are_popped(self, queue: RedisQueue) -> None:
        soon = make_event()
        await queue.schedule_retry(soon, delay_seconds=0)
        await queue.schedule_retry(make_event(), delay_seconds=300)

        due = await queue.pop_due_retries()

        assert [item.job_id for item in due] == [soon.job_id]
        assert (await queue.depth()).scheduled_retries == 1

    async def test_limit_is_respected(self, queue: RedisQueue) -> None:
        for _ in range(5):
            await queue.schedule_retry(make_event(), delay_seconds=0)

        assert len(await queue.pop_due_retries(limit=2)) == 2

    async def test_retry_round_trip_back_onto_the_stream(self, queue: RedisQueue) -> None:
        event = make_event()
        await queue.schedule_retry(event.next_attempt(), delay_seconds=0)

        for due in await queue.pop_due_retries():
            await queue.publish(due)

        delivered = await queue.read(consumer_name=CONSUMER)
        assert delivered[0].event.job_id == event.job_id
        assert delivered[0].event.attempt == 1


class TestDepthAndHealth:
    async def test_depth_counts_each_dimension(self, queue: RedisQueue) -> None:
        await queue.publish(make_event())
        await queue.publish(make_event())
        await queue.read(consumer_name=CONSUMER)
        await queue.schedule_retry(make_event(), delay_seconds=300)

        depth = await queue.depth()

        assert depth.stream_length == 2
        assert depth.pending == 1
        assert depth.scheduled_retries == 1

    async def test_health_is_true_when_connected(self, queue: RedisQueue) -> None:
        assert await queue.health() is True

    async def test_health_is_false_rather_than_raising(self, settings) -> None:
        """A readiness probe must never 500."""
        assert await RedisQueue(settings).health() is False
