"""Read-only message-type policy catalog shared with the Web UI."""

from __future__ import annotations

import json
from importlib import resources
from types import MappingProxyType
from typing import Any, Mapping

_BOOL_PROPERTIES = {
    "transcript",
    "searchable",
    "inboxActivity",
    "inboxPreview",
    "inboxSettlesReply",
    "unread",
    "webPush",
    "acceptedReservation",
}
_LIST_PROPERTIES = {
    "inputAuthors",
    "terminalWhenEvents",
    "webPushWhenEvents",
}
_ENUM_PROPERTIES = {
    "activityRole": {"none", "turn_start", "activity", "terminal"},
    "render": {
        "none",
        "user",
        "harness",
        "annotation",
        "agent",
        "status",
        "activity",
    },
}
_PROPERTY_NAMES = _BOOL_PROPERTIES | _LIST_PROPERTIES | set(_ENUM_PROPERTIES)


def _load_catalog() -> tuple[Mapping[str, Any], Mapping[str, Mapping[str, Any]]]:
    raw = json.loads(
        resources.files(__package__).joinpath("message_types.json").read_text(encoding="utf-8")
    )
    if not isinstance(raw, dict) or set(raw) != {"defaults", "types"}:
        raise ValueError("message type catalog must contain defaults and types")
    defaults = raw["defaults"]
    type_specs = raw["types"]
    if not isinstance(defaults, dict) or set(defaults) != _PROPERTY_NAMES:
        raise ValueError("message type catalog defaults do not match the public properties")
    if not isinstance(type_specs, dict):
        raise ValueError("message type catalog types must be an object")

    def freeze_spec(spec: Any, *, partial: bool) -> Mapping[str, Any]:
        if not isinstance(spec, dict):
            raise ValueError("each message type spec must be an object")
        if not set(spec).issubset(_PROPERTY_NAMES):
            raise ValueError("message type spec contains an unknown property")
        if not partial and set(spec) != _PROPERTY_NAMES:
            raise ValueError("message type defaults must define every property")

        frozen: dict[str, Any] = {}
        for name, value in spec.items():
            if name in _BOOL_PROPERTIES:
                if not isinstance(value, bool):
                    raise ValueError(f"{name} must be boolean")
                frozen[name] = value
            elif name in _LIST_PROPERTIES:
                if (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) or not item for item in value)
                    or len(value) != len(set(value))
                ):
                    raise ValueError(f"{name} must be a list of unique non-empty strings")
                frozen[name] = tuple(value)
            else:
                if value not in _ENUM_PROPERTIES[name]:
                    raise ValueError(f"{name} has an unsupported value")
                frozen[name] = value
        return MappingProxyType(frozen)

    frozen_defaults = freeze_spec(defaults, partial=False)
    frozen_types: dict[str, Mapping[str, Any]] = {}
    for message_type, overrides in type_specs.items():
        if not isinstance(message_type, str) or not message_type:
            raise ValueError("message type names must be non-empty strings")
        merged = dict(frozen_defaults)
        merged.update(freeze_spec(overrides, partial=True))
        frozen_types[message_type] = MappingProxyType(merged)
    return frozen_defaults, MappingProxyType(frozen_types)


_DEFAULT_SPEC, _TYPE_SPECS = _load_catalog()


def _property_enabled(property_name: str, value: Any) -> bool:
    if property_name in _BOOL_PROPERTIES:
        return bool(value)
    if property_name in _LIST_PROPERTIES:
        return bool(value)
    return value != "none"


def types_with(property_name: str) -> tuple[str, ...]:
    """Return catalog types for which *property_name* is enabled, in catalog order."""

    if property_name not in _PROPERTY_NAMES:
        raise KeyError(property_name)
    return tuple(
        message_type
        for message_type, spec in _TYPE_SPECS.items()
        if _property_enabled(property_name, spec[property_name])
    )


def types_without(property_name: str) -> tuple[str, ...]:
    """Return catalog types for which *property_name* is disabled, in catalog order."""

    if property_name not in _PROPERTY_NAMES:
        raise KeyError(property_name)
    return tuple(
        message_type
        for message_type, spec in _TYPE_SPECS.items()
        if not _property_enabled(property_name, spec[property_name])
    )


def input_author_type_pairs() -> tuple[tuple[str, str], ...]:
    """Return accepted ``(author, message_type)`` input-turn pairs in catalog order.

    ``inputAuthors`` lists the authors permitted to submit that type as input, so an input turn is
    classified by this ``(author, type)`` pair alone; it never says who wrote a given row.
    ``annotation`` is two-way and its direction lives in ``content.annotation.direction``, never in
    ``author``, which is ``harness`` on a user annotation dispatched as agent input.
    """

    return tuple(
        (author, message_type)
        for message_type, spec in _TYPE_SPECS.items()
        for author in spec["inputAuthors"]
    )


def spec_for(message_type: str) -> Mapping[str, Any]:
    """Return an immutable resolved spec; unknown types receive catalog defaults."""

    return _TYPE_SPECS.get(message_type, _DEFAULT_SPEC)


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_values(values: tuple[str, ...]) -> str:
    if not values:
        raise ValueError("partial-index predicate requires at least one catalog value")
    return ", ".join(_sql_quote(value) for value in values)


def build_partial_index_predicate(index_name: str) -> str:
    """Build one current ``messages`` partial-index predicate byte-for-byte."""

    if index_name == "ix_messages_inbox_activity":
        return (
            "session_id is not null and type not in "
            f"({_sql_values(types_without('inboxActivity'))})"
        )
    if index_name == "ix_messages_inbox_agent_reply":
        return (
            "session_id is not null and type in "
            f"({_sql_values(types_with('inboxPreview'))})"
        )
    if index_name == "ix_messages_inbox_user_send":
        pairs = input_author_type_pairs()
        if not pairs:
            raise ValueError("input-turn partial index requires at least one author/type pair")
        clauses = " or ".join(
            f"(author = {_sql_quote(author)} and type = {_sql_quote(message_type)})"
            for author, message_type in pairs
        )
        return f"session_id is not null and ({clauses})"
    raise KeyError(index_name)


__all__ = [
    "build_partial_index_predicate",
    "input_author_type_pairs",
    "spec_for",
    "types_with",
    "types_without",
]
