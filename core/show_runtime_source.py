from __future__ import annotations


RETIRED_SHOW_RUNTIME_SOURCES = frozenset({"github", "github-source"})


def retired_show_runtime_source(value: str | None) -> str | None:
    """Return the canonical retired source alias, if value names one."""
    normalized = (value or "").strip().lower()
    return normalized if normalized in RETIRED_SHOW_RUNTIME_SOURCES else None
