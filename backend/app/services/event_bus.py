"""In-process publish/subscribe bus carrying job progress events.

Real-time data flow
-------------------

    Orchestrator task --publish()--> JobChannel --fan-out--> one asyncio.Queue per subscriber
                                         |                        |
                                         |                        v
                                         |               WebSocket handler --> browser
                                         v
                              bounded history (replayed on connect / reconnect)

Design choices
--------------
* Fan-out with one bounded queue per subscriber: a slow browser tab can never
  block the orchestrator, nor delay the other tabs watching the same job.
* `publish()` never awaits. If a subscriber's queue is full, that subscriber is
  evicted with a "lagged" marker instead of applying back-pressure to the
  pipeline. Its client reconnects with the last `seq` it rendered and catches up
  from the history buffer.
* The bus (not the caller) assigns `seq`, so ordering is defined in one place.
* No locks: everything runs on a single event loop, and there is no `await`
  between snapshotting the history and registering a subscriber queue, so no
  event can be published "in between" and be lost or duplicated.

Limitation: state lives in process memory, so run a single Uvicorn worker. A
multi-worker deployment swaps this class for a Redis Streams implementation
exposing the same `publish()` / `subscribe(after_seq)` contract.
"""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from app.models.deployment import TERMINAL_EVENTS, EventType, JobEvent, PlanStep


class UnknownJobError(LookupError):
    """No channel exists for this job id (never created, or already purged)."""


class SubscriberLaggedError(Exception):
    """The subscriber fell too far behind and was evicted; it must resubscribe."""


@dataclass(eq=False)  # identity-based hashing: each subscriber is unique
class _Subscriber:
    # A `None` item is the eviction marker ("you lagged, resubscribe").
    queue: asyncio.Queue[JobEvent | None]


@dataclass
class _JobChannel:
    history: deque[JobEvent]
    subscribers: set[_Subscriber] = field(default_factory=set)
    next_seq: int = 1
    closed: bool = False  # True once a terminal event has been published


class JobEventBus:
    def __init__(self, *, history_size: int, queue_size: int) -> None:
        self._history_size = history_size
        self._queue_size = queue_size
        self._channels: dict[UUID, _JobChannel] = {}

    def open_channel(self, job_id: UUID) -> None:
        self._channels[job_id] = _JobChannel(history=deque(maxlen=self._history_size))

    def discard_channel(self, job_id: UUID) -> None:
        self._channels.pop(job_id, None)

    def publish(
        self,
        job_id: UUID,
        event_type: EventType,
        message: str,
        *,
        step_key: str | None = None,
        step_index: int | None = None,
        step_total: int | None = None,
        plan: list[PlanStep] | None = None,
        url: str | None = None,
    ) -> JobEvent:
        channel = self._channels.get(job_id)
        if channel is None:
            raise UnknownJobError(job_id)
        if channel.closed:
            raise RuntimeError(f"job {job_id} already reached a terminal state")

        event = JobEvent(
            job_id=job_id,
            seq=channel.next_seq,
            type=event_type,
            timestamp=datetime.now(UTC),
            message=message,
            step_key=step_key,
            step_index=step_index,
            step_total=step_total,
            plan=plan,
            url=url,
        )
        channel.next_seq += 1
        channel.history.append(event)

        # Fan-out without ever awaiting: the producer's pace is independent of
        # how fast each WebSocket drains its queue.
        for subscriber in list(channel.subscribers):
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Evict the slow consumer: empty its queue (it will replay from
                # history anyway) and leave a single "lagged" marker behind.
                channel.subscribers.discard(subscriber)
                while not subscriber.queue.empty():
                    subscriber.queue.get_nowait()
                subscriber.queue.put_nowait(None)

        if event_type in TERMINAL_EVENTS:
            channel.closed = True
        return event

    async def subscribe(self, job_id: UUID, after_seq: int = 0) -> AsyncGenerator[JobEvent, None]:
        """Yield every event with `seq > after_seq`: first the backlog, then live events.

        The generator ends right after the terminal event. Consumers should wrap
        it in `contextlib.aclosing()` so the `finally` block (unregistering the
        queue) runs deterministically when they stop early, e.g. on disconnect.
        """
        channel = self._channels.get(job_id)
        if channel is None:
            raise UnknownJobError(job_id)

        # --- Atomic section (no `await` until the `try`) ---------------------
        # 1) Snapshot what the client has not seen yet...
        backlog = [event for event in channel.history if event.seq > after_seq]
        # 2) ...and register for live events before yielding control, so every
        #    later event lands in our queue: no gap between replay and live.
        subscriber: _Subscriber | None = None
        if not channel.closed:
            subscriber = _Subscriber(queue=asyncio.Queue(maxsize=self._queue_size))
            channel.subscribers.add(subscriber)
        # ---------------------------------------------------------------------

        last_seq = after_seq
        try:
            for event in backlog:
                last_seq = event.seq
                yield event
            if subscriber is None:  # job already finished: the backlog was everything
                return

            while True:
                item = await subscriber.queue.get()
                if item is None:
                    raise SubscriberLaggedError(job_id)
                if item.seq <= last_seq:  # defensive: never re-send an event
                    continue
                last_seq = item.seq
                yield item
                if item.type in TERMINAL_EVENTS:
                    return
        finally:
            if subscriber is not None:
                channel.subscribers.discard(subscriber)
