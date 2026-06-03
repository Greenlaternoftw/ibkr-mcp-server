"""In-memory pub/sub bus for live multi-tab/multi-device chat sync.

When a thread is mutated (message added, renamed, deleted, etc.), the
mutating endpoint publishes an event to this bus. All currently
connected ``/chat/api/events/stream`` SSE subscribers receive it and
their browser tabs decide what to do (re-fetch the active thread,
refresh the thread list, etc.).

Design choices:

  * **In-process only.** No Redis, no external broker. One daemon =
    one bus; if there are ever multiple daemon processes serving the
    same SQLite store they wouldn't see each other's events. For a
    single-user, single-process trading daemon this is fine and avoids
    a whole ops surface.

  * **Bounded queue per subscriber.** A slow subscriber (e.g. an
    iPhone tab that's been backgrounded with a flaky connection) gets
    a fixed-size queue; if it fills, new events are silently dropped
    for that subscriber. The browser still has REST endpoints to
    resync on the next user action, so a missed push is recoverable
    -- worse than perfect but better than blocking the publisher.

  * **No retention / no replay.** Subscribers receive events only
    while connected; reconnecting doesn't get you a backlog. The
    initial REST fetch is the snapshot, SSE is the delta tail.

  * **Self-echo dedup is client-side.** Each browser tab generates a
    random ``client_id`` on page load and sends it with every POST.
    The server includes ``originating_client_id`` in published events.
    Tabs ignore events from their own client_id. This is cheaper than
    server-side per-subscriber filtering and keeps the publish path O(1).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict

logger = logging.getLogger(__name__)


# Maximum events buffered per subscriber. Higher = more memory if a
# subscriber stalls; lower = more events dropped. 100 is comfortably
# above any realistic burst (the user typing fast is ~1 event/sec).
_QUEUE_MAX = 100


class ThreadEventBus:
    """Asyncio-safe fan-out bus for thread events.

    Use via the async-context-manager ``subscribe()`` so cleanup runs
    even when the SSE handler is cancelled by client disconnect.
    """

    def __init__(self) -> None:
        # subscriber_id -> queue. Integer keys are easier to log than
        # opaque object IDs; we don't care about cross-process uniqueness.
        self._subscribers: Dict[int, asyncio.Queue] = {}
        self._next_id: int = 0
        # Lock protects _subscribers / _next_id during mutation. publish()
        # snapshots under the lock then drops it before fanout -- a slow
        # subscriber can't block other publishers.
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        """Subscribe and yield a queue that receives all published events.

        The queue is removed from the bus automatically on context exit
        (i.e. when the SSE handler returns, raises, or is cancelled).
        """
        async with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
            self._subscribers[sub_id] = queue
            logger.debug("event bus: subscriber %d connected (now %d total)",
                         sub_id, len(self._subscribers))
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.pop(sub_id, None)
                logger.debug(
                    "event bus: subscriber %d disconnected (now %d total)",
                    sub_id, len(self._subscribers),
                )

    async def publish(self, event: dict) -> None:
        """Fan ``event`` out to every current subscriber.

        Drops the event for any subscriber whose queue is full -- they'll
        resync from REST on their next user action. The publisher itself
        always returns immediately (no head-of-line blocking on a slow
        subscriber).
        """
        async with self._lock:
            subs = list(self._subscribers.items())
        dropped = 0
        for sub_id, queue in subs:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dropped += 1
        if dropped:
            logger.warning(
                "event bus: dropped %s/%s deliveries (slow subscriber)",
                dropped, len(subs),
            )

    @property
    def subscriber_count(self) -> int:
        """For debugging / smoke-testing. Not used by the bus itself."""
        return len(self._subscribers)
