"""Threading invariants for ``vibe.sse_broker.SSEBroker``.

Reviewers flagged that ``publish`` runs from arbitrary threads (sync REST
routes, IM threads) while ``subscribe`` / ``unsubscribe`` mutate the
subscriber map on the event loop thread. A bare ``list(dict.values())``
can raise ``RuntimeError: dictionary changed size during iteration`` and
take down whichever caller was publishing the event. The lock added to
``SSEBroker`` removes that race; this test exercises it under load.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe.sse_broker import SSEBroker


def test_publish_preserves_repeated_payloads_and_omits_ts():
    broker = SSEBroker()

    async def scenario():
        _sub_id, queue = broker.subscribe()
        broker.publish("queue.updated", {"session_id": "ses1"})
        broker.publish("queue.updated", {"session_id": "ses1"})
        broker.publish("queue.updated", {"session_id": "ses2"})

        first_event, first_payload = await asyncio.wait_for(queue.get(), timeout=1)
        second_event, second_payload = await asyncio.wait_for(queue.get(), timeout=1)
        third_event, third_payload = await asyncio.wait_for(queue.get(), timeout=1)
        return (
            first_event,
            json.loads(first_payload),
            second_event,
            json.loads(second_payload),
            third_event,
            json.loads(third_payload),
        )

    first_event, first_payload, second_event, second_payload, third_event, third_payload = asyncio.run(scenario())

    assert first_event == "queue.updated"
    assert first_payload == {"type": "queue.updated", "data": {"session_id": "ses1"}}
    assert "ts" not in first_payload
    assert second_event == "queue.updated"
    assert second_payload == {"type": "queue.updated", "data": {"session_id": "ses1"}}
    assert third_event == "queue.updated"
    assert third_payload == {"type": "queue.updated", "data": {"session_id": "ses2"}}


def test_publish_survives_concurrent_subscribe_churn():
    broker = SSEBroker()

    # Seed an event loop the broker can reuse — publish() captures it
    # lazily, normally on the first subscribe() call.
    loop = asyncio.new_event_loop()

    def _runner() -> None:
        try:
            loop.run_forever()
        except Exception:
            pass

    runner = threading.Thread(target=_runner, daemon=True)
    runner.start()

    async def _seed_loop():
        return broker.subscribe()

    stable_sub_id, stable_queue = asyncio.run_coroutine_threadsafe(_seed_loop(), loop).result(timeout=5)

    async def _drain_stable_subscriber() -> None:
        while True:
            await stable_queue.get()

    drain = asyncio.run_coroutine_threadsafe(_drain_stable_subscriber(), loop)

    stop = threading.Event()
    errors: list[BaseException] = []

    def _publisher() -> None:
        try:
            while not stop.is_set():
                broker.publish("ping", {"n": 1})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    publisher = threading.Thread(target=_publisher, daemon=True)
    publisher.start()

    # Hammer subscribe/unsubscribe on the loop thread while the publisher
    # is iterating. Without the lock this reliably trips
    # ``RuntimeError: dictionary changed size during iteration`` within
    # a few hundred churn cycles.
    async def _churn(iterations: int = 800) -> None:
        for _ in range(iterations):
            sub_id, _ = broker.subscribe()
            broker.unsubscribe(sub_id)
            await asyncio.sleep(0)

    async def _unsubscribe(sub_id: int) -> None:
        broker.unsubscribe(sub_id)

    try:
        asyncio.run_coroutine_threadsafe(_churn(), loop).result(timeout=15)
    finally:
        stop.set()
        publisher.join(timeout=5)

        drain.cancel()
        try:
            drain.result(timeout=5)
        except concurrent.futures.CancelledError:
            pass

        asyncio.run_coroutine_threadsafe(_unsubscribe(stable_sub_id), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)
        loop.close()

    assert not errors, f"publish() raised under concurrent churn: {errors!r}"
