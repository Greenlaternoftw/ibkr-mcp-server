"""Tests for the in-process ThreadEventBus.

Covers the contract the SSE endpoint + mutating endpoints rely on:
  * Multiple subscribers receive every event (fan-out works).
  * Unsubscribing on context-manager exit removes the queue
    (so disconnected SSE clients don't leak forever).
  * A slow subscriber whose queue fills doesn't block the publisher
    or stop other subscribers from getting their events.
  * Events round-trip with payload intact (no field reshaping).

The bus is intentionally simple, so the test surface is small -- this
file is the safety net to keep it that way under future edits.
"""

from __future__ import annotations

import asyncio

import pytest

from ibkr_mcp_server.chat.pubsub import ThreadEventBus


@pytest.fixture
def bus() -> ThreadEventBus:
    return ThreadEventBus()


# --- fan-out --------------------------------------------------------------


class TestFanOut:
    @pytest.mark.asyncio
    async def test_single_subscriber_receives_published_event(self, bus):
        async with bus.subscribe() as q:
            await bus.publish({"type": "thread_updated", "thread_id": "thr_a"})
            event = await asyncio.wait_for(q.get(), timeout=0.5)
        assert event == {"type": "thread_updated", "thread_id": "thr_a"}

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_get_the_event(self, bus):
        """Two tabs open -- both should see the same notification."""
        async with bus.subscribe() as q1, bus.subscribe() as q2, bus.subscribe() as q3:
            await bus.publish({"type": "thread_created", "thread_id": "thr_x"})
            e1 = await asyncio.wait_for(q1.get(), timeout=0.5)
            e2 = await asyncio.wait_for(q2.get(), timeout=0.5)
            e3 = await asyncio.wait_for(q3.get(), timeout=0.5)
        assert e1 == e2 == e3
        assert e1["thread_id"] == "thr_x"

    @pytest.mark.asyncio
    async def test_publish_with_no_subscribers_is_noop(self, bus):
        # Should not raise, should not log loudly.
        await bus.publish({"type": "thread_updated", "thread_id": "thr_x"})
        assert bus.subscriber_count == 0


# --- subscription lifecycle -----------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_subscribe_increments_count(self, bus):
        assert bus.subscriber_count == 0
        async with bus.subscribe():
            assert bus.subscriber_count == 1
            async with bus.subscribe():
                assert bus.subscriber_count == 2
            assert bus.subscriber_count == 1
        assert bus.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_context_exit_removes_subscriber_even_on_exception(self, bus):
        """A handler raising mid-stream must not leak its queue."""
        with pytest.raises(RuntimeError):
            async with bus.subscribe():
                assert bus.subscriber_count == 1
                raise RuntimeError("simulated handler crash")
        assert bus.subscriber_count == 0


# --- slow-subscriber isolation --------------------------------------------


class TestSlowSubscriber:
    @pytest.mark.asyncio
    async def test_full_queue_does_not_block_publisher(self, bus, monkeypatch):
        """If one subscriber stops draining, publish() must still return
        immediately AND other subscribers must still get every event.

        We interleave drains on the fast subscriber so its queue stays
        empty while the slow subscriber's fills. Confirms isolation:
        the slow subscriber loses an event (dropped), the fast one
        loses nothing.
        """
        # Tiny queue size so we don't have to publish 100+ events.
        import ibkr_mcp_server.chat.pubsub as pubsub_mod
        monkeypatch.setattr(pubsub_mod, "_QUEUE_MAX", 2)

        slow_bus = ThreadEventBus()

        async with slow_bus.subscribe() as slow_q, slow_bus.subscribe() as fast_q:
            # Publish + drain fast, twice. After this: slow has 2 events
            # buffered (full); fast has zero.
            await slow_bus.publish({"n": 1})
            assert (await asyncio.wait_for(fast_q.get(), timeout=0.5))["n"] == 1
            await slow_bus.publish({"n": 2})
            assert (await asyncio.wait_for(fast_q.get(), timeout=0.5))["n"] == 2
            assert slow_q.qsize() == 2
            assert fast_q.empty()

            # Third publish: slow is full so the event must be dropped
            # for slow; publish() must NOT block; fast must still get it.
            await asyncio.wait_for(slow_bus.publish({"n": 3}), timeout=0.5)
            assert (await asyncio.wait_for(fast_q.get(), timeout=0.5))["n"] == 3

            # Slow still has exactly its first 2 events.
            slow_drained = [slow_q.get_nowait()["n"], slow_q.get_nowait()["n"]]
            assert slow_drained == [1, 2]
            assert slow_q.empty()


# --- payload integrity ----------------------------------------------------


class TestPayload:
    @pytest.mark.asyncio
    async def test_arbitrary_fields_pass_through(self, bus):
        """The bus must not reshape events -- mutating endpoints rely on
        ``originating_client_id``, ``thread_id``, ``title`` etc. arriving
        at the subscriber exactly as published."""
        payload = {
            "type": "thread_renamed",
            "thread_id": "thr_abc",
            "title": "Investment thoughts",
            "originating_client_id": "cid-xyz",
            "nested": {"a": 1, "b": [2, 3]},
        }
        async with bus.subscribe() as q:
            await bus.publish(payload)
            received = await asyncio.wait_for(q.get(), timeout=0.5)
        # Same object semantically; dict equality covers nested.
        assert received == payload
