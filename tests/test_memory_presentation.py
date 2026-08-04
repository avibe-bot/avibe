from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from config import paths
from core.memory.everos import FakeMemoryProvider
from core.memory.module import MIN_FREE_DISK_BYTES, MemoryModule
from core.memory.presentation import MemoryStatusBuckets, memory_status_buckets
from core.memory.store import MemoryStore
from core.memory.types import CaptureAccepted, CaptureRequest, CaptureSkipped


# The counters the bucket rule reads. A rename on the emitting side that misses
# `memory_status_buckets` silently zeroes a bucket on every surface, so pin them.
COUNTER_NAMES = frozenset(
    {
        "pending",
        "processing",
        "awaiting_receipt",
        "succeeded",
        "receipt_unknown",
        "distill_failed",
        "dead",
        "missed",
    }
)


def _request(source: str, *, text: str = "remember this") -> CaptureRequest:
    return CaptureRequest(
        source_message_id=source,
        session_id="conversation-1",
        principal_id="u-11111111111111111111111111111111",
        provenance="user_input",
        text=text,
        occurred_at_ms=1_000,
    )


def test_memory_status_buckets_are_shared_by_backend_presentations() -> None:
    assert memory_status_buckets(
        {
            "pending": 1,
            "processing": 2,
            "awaiting_receipt": 3,
            "succeeded": 4,
            "receipt_unknown": 5,
            "distill_failed": 6,
            "dead": 7,
            "missed": 8,
        }
    ) == MemoryStatusBuckets(
        syncing=6,
        succeeded=4,
        unknown=5,
        failed=6,
        dead=7,
        missed=8,
    )


async def test_status_payload_publishes_the_buckets_every_surface_renders(tmp_path: Path) -> None:
    """The status payload carries the six buckets, agreeing with its own counters.

    The UI reads `buckets` and `vibe memory status` derives them from the raw
    counters; this is the one place that holds those two views together.
    """

    store = MemoryStore(paths.get_state_dir() / "memory-presentation" / tmp_path.name / "memory.sqlite")
    module = MemoryModule(
        store,
        FakeMemoryProvider(),
        enabled=True,
        disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
    )

    assert await module.capture(_request("delivered")) == CaptureAccepted()
    assert await module._worker.drain_once() == 1
    assert await module.capture(_request("queued")) == CaptureAccepted()
    assert await module.capture(_request("blank", text="\r\n  \r")) == CaptureSkipped(
        reason="memory_invalid_input"
    )

    payload = asdict(await module.status())

    assert COUNTER_NAMES <= set(payload)
    assert payload["buckets"] == asdict(memory_status_buckets(payload))
    assert payload["buckets"] == asdict(
        MemoryStatusBuckets(syncing=1, succeeded=1, unknown=0, failed=0, dead=0, missed=1)
    )
