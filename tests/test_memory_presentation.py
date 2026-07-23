from core.memory.presentation import MemoryStatusBuckets, memory_status_buckets


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
