"""Stateless parser for standard ACP ``session/update`` notification payloads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class AcpDiffBlock:
    """A standard ACP file diff content block attached to a tool call."""

    path: str
    old_text: str | None
    new_text: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class AcpMessageChunkUpdate:
    """An ``agent_message_chunk`` update."""

    session_id: str
    content: dict[str, Any]
    text: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class AcpThoughtChunkUpdate:
    """An ``agent_thought_chunk`` update."""

    session_id: str
    content: dict[str, Any]
    text: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class AcpToolCallUpdate:
    """A ``tool_call`` or incremental ``tool_call_update`` notification."""

    session_id: str
    kind: Literal["tool_call", "tool_call_update"]
    tool_call_id: str
    title: str | None
    tool_kind: str | None
    status: str | None
    raw_input: Any
    raw_output: Any
    content: tuple[dict[str, Any], ...]
    diffs: tuple[AcpDiffBlock, ...]
    locations: tuple[dict[str, Any], ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class AcpUsageUpdate:
    """A ``usage_update`` notification reporting context window and optional cost."""

    session_id: str
    used: int | None
    size: int | None
    cost: dict[str, Any] | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class AcpConfigOptionUpdate:
    """A ``config_option_update`` notification."""

    session_id: str
    config_options: tuple[dict[str, Any], ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class AcpSessionInfoUpdate:
    """A ``session_info_update`` notification (title and timestamp metadata)."""

    session_id: str
    title: str | None
    updated_at: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class AcpUnknownUpdate:
    """Any unrecognized or extension ``sessionUpdate`` kind, preserved verbatim."""

    session_id: str
    update_type: str
    raw: dict[str, Any]


AcpSessionUpdate = (
    AcpMessageChunkUpdate
    | AcpThoughtChunkUpdate
    | AcpToolCallUpdate
    | AcpUsageUpdate
    | AcpConfigOptionUpdate
    | AcpSessionInfoUpdate
    | AcpUnknownUpdate
)


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _extract_chunk_text(content: Mapping[str, Any]) -> str | None:
    if content.get("type") == "text" and isinstance(content.get("text"), str):
        return content["text"]
    return None


def _dict_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def parse_diff_blocks(content: Any) -> tuple[AcpDiffBlock, ...]:
    """Extract standard ACP ``diff`` content blocks from a tool-call content list."""
    if not isinstance(content, (list, tuple)):
        return ()
    diffs: list[AcpDiffBlock] = []
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") != "diff":
            continue
        path = item.get("path")
        new_text = item.get("newText")
        if not isinstance(path, str) or not isinstance(new_text, str):
            continue
        old_text = _opt_str(item.get("oldText"))
        diffs.append(
            AcpDiffBlock(
                path=path,
                old_text=old_text,
                new_text=new_text,
                raw=dict(item),
            )
        )
    return tuple(diffs)


def parse_session_update(params: Mapping[str, Any]) -> AcpSessionUpdate:
    """Parse the ``params`` object of an ACP ``session/update`` notification.

    Raises ``ValueError`` if the envelope does not carry a string ``sessionId``
    and a mapping ``update`` with a string ``sessionUpdate`` discriminator.
    """
    if not isinstance(params, Mapping):
        raise ValueError("session/update params must be a mapping")
    session_id = params.get("sessionId")
    update = params.get("update")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session/update params missing non-empty string 'sessionId'")
    if not isinstance(update, Mapping):
        raise ValueError("session/update params missing mapping 'update'")
    update_type = update.get("sessionUpdate")
    if not isinstance(update_type, str) or not update_type:
        raise ValueError("session/update payload missing non-empty string 'sessionUpdate'")

    raw = dict(update)

    if update_type == "agent_message_chunk":
        raw_content = update.get("content")
        content = dict(raw_content) if isinstance(raw_content, Mapping) else {}
        return AcpMessageChunkUpdate(
            session_id=session_id,
            content=content,
            text=_extract_chunk_text(content),
            raw=raw,
        )

    if update_type == "agent_thought_chunk":
        raw_content = update.get("content")
        content = dict(raw_content) if isinstance(raw_content, Mapping) else {}
        return AcpThoughtChunkUpdate(
            session_id=session_id,
            content=content,
            text=_extract_chunk_text(content),
            raw=raw,
        )

    if update_type in ("tool_call", "tool_call_update"):
        tool_call_id = update.get("toolCallId")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError(f"{update_type} payload missing non-empty string 'toolCallId'")
        content_items = _dict_tuple(update.get("content"))
        kind: Literal["tool_call", "tool_call_update"] = (
            "tool_call" if update_type == "tool_call" else "tool_call_update"
        )
        return AcpToolCallUpdate(
            session_id=session_id,
            kind=kind,
            tool_call_id=tool_call_id,
            title=_opt_str(update.get("title")),
            tool_kind=_opt_str(update.get("kind")),
            status=_opt_str(update.get("status")),
            raw_input=update.get("rawInput"),
            raw_output=update.get("rawOutput"),
            content=content_items,
            diffs=parse_diff_blocks(content_items),
            locations=_dict_tuple(update.get("locations")),
            raw=raw,
        )

    if update_type == "usage_update":
        raw_cost = update.get("cost")
        cost = dict(raw_cost) if isinstance(raw_cost, Mapping) else None
        return AcpUsageUpdate(
            session_id=session_id,
            used=_opt_int(update.get("used")),
            size=_opt_int(update.get("size")),
            cost=cost,
            raw=raw,
        )

    if update_type == "config_option_update":
        return AcpConfigOptionUpdate(
            session_id=session_id,
            config_options=_dict_tuple(update.get("configOptions")),
            raw=raw,
        )

    if update_type == "session_info_update":
        return AcpSessionInfoUpdate(
            session_id=session_id,
            title=_opt_str(update.get("title")),
            updated_at=_opt_str(update.get("updatedAt")),
            raw=raw,
        )

    return AcpUnknownUpdate(
        session_id=session_id,
        update_type=update_type,
        raw=raw,
    )
