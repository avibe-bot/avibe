"""Cache-write decompositions shared by the wire adapters, independent of Model Hub."""

from typing import Any, Mapping


def write_ttls(
    value: Any, fields: Mapping[str, str], total: int | None, *, current: Mapping[str, int] | None = None,
) -> dict[str, int] | None:
    """Merge reported counters; a missing breakdown stays unknown, not inferred from requested TTL.

    Contradictory counters cannot be priced honestly. Keep the aggregate and drop
    the decomposition rather than changing billed totals or double counting it.
    """
    result = dict(current) if current is not None else None
    if isinstance(value, Mapping):
        for field, ttl in fields.items():
            if field in value and value[field] is not None:
                if type(value[field]) is not int or value[field] < 0:
                    return None
                if result is None:
                    result = {}
                result[ttl] = value[field]
    return result if result is None or total is None or sum(result.values()) <= total else None
