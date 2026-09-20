"""Closed Memory project-id vocabulary.

Wire values must already be lowercase. Mixed case is invalid.
"""

from __future__ import annotations

import re

DEFAULT_MEMORY_PROJECT_ID = "default"
MEMORY_SEARCH_ALL_PROJECTS = "all"
RESERVED_MEMORY_PROJECT_IDS = frozenset(
    {DEFAULT_MEMORY_PROJECT_ID, MEMORY_SEARCH_ALL_PROJECTS, "personal"}
)
MAX_NAMED_MEMORY_PROJECTS = 16

_NAMED_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,62}\Z")
_LEGACY_HASH = re.compile(r"^p-[0-9a-f]{32}\Z")


def is_legacy_memory_project_id(value: object) -> bool:
    """Return whether *value* is an exact leftover workdir hash."""

    return isinstance(value, str) and _LEGACY_HASH.fullmatch(value) is not None


def is_named_memory_project_id(value: object) -> bool:
    """Return whether *value* is an opt-in named slug."""

    return (
        isinstance(value, str)
        and value not in RESERVED_MEMORY_PROJECT_IDS
        and not value.startswith("p-")
        and not value.startswith("u-")
        and _NAMED_SLUG.fullmatch(value) is not None
    )


def is_new_stored_memory_project_id(value: object) -> bool:
    """Return whether *value* may appear in the product catalog."""

    return value == DEFAULT_MEMORY_PROJECT_ID or is_named_memory_project_id(value)


def is_writable_memory_project_id(value: object) -> bool:
    """Return whether a new capture or remember may target *value*."""

    return is_new_stored_memory_project_id(value)


def is_persisted_memory_project_id(value: object) -> bool:
    """Return whether *value* may exist on disk for recovery or drain."""

    return is_legacy_memory_project_id(value) or is_new_stored_memory_project_id(value)


def is_project_id(value: object) -> bool:
    """Validate any released or current Memory project identifier."""

    return is_persisted_memory_project_id(value)


def parse_writable_memory_project(value: object) -> str:
    """Parse a remember/capture project. Missing/null is not handled here."""

    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError("invalid Memory project")
    if not is_writable_memory_project_id(value):
        raise ValueError("invalid Memory project")
    return value


def parse_agent_search_project(value: object) -> str:
    """Parse an Agent/CLI search project. ``all`` is rejected."""

    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError("invalid Memory project")
    if not is_new_stored_memory_project_id(value):
        raise ValueError("invalid Memory project")
    return value


def parse_ui_search_project(value: object) -> str:
    """Parse a Settings search project. ``all`` is allowed."""

    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError("invalid Memory project")
    if value == MEMORY_SEARCH_ALL_PROJECTS or is_new_stored_memory_project_id(value):
        return value
    raise ValueError("invalid Memory project")


def omitted_project_to_default(value: object) -> object:
    """Map omitted/null project fields to default; leave present values alone."""

    return DEFAULT_MEMORY_PROJECT_ID if value is None else value
