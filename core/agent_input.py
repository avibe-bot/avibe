"""Render execution-only metadata without changing canonical message content."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import unicodedata
from typing import Any, Mapping


# Released wire titles must remain recognizable even if translations change.
_LEGACY_ATTACHMENT_ERROR_TITLES = (
    "Attachment Download Errors",
    "\u9644\u4ef6\u4e0b\u8f7d\u9519\u8bef",
)


def _matches_original_body(text: str, original: str) -> bool:
    if text == original:
        return True
    blocks = tuple(f"[{title}]\n" for title in _LEGACY_ATTACHMENT_ERROR_TITLES)
    if text.startswith(original + "\n\n"):
        return text[len(original) + 2 :].startswith(blocks)
    return not original.strip() and text.startswith(blocks)


def sanitize_identity(value: str) -> str:
    token = (value or "").replace("\n", " ").replace("\r", " ").strip()
    token = token.replace("[", "(").replace("]", ")").replace("<", "(").replace(">", ")")
    return token[:80] or "unknown"


def without_legacy_metadata(text: str, *, original: str, user_id: str) -> str:
    """Remove a released IM prefix only when the immutable body proves its extent."""
    if _matches_original_body(text, original):
        return text
    time_line = r"\[Current Time: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC[+-]\d{2}:\d{2}\]\n"
    identity_line = rf"\[[^\[\]<>\n]{{1,80}}<{re.escape(sanitize_identity(user_id))}>\]\n"
    # A user-authored first line may look like metadata. The immutable body,
    # not a greedy match, decides which released prefix was actually added.
    for prefix in (time_line + identity_line, time_line, identity_line):
        match = re.match(prefix, text)
        if match is not None:
            body = text[match.end() :]
            if _matches_original_body(body, original):
                return body
    return text


def _token(value: Any) -> str:
    """One sanitized line, or empty when there is no value.

    Every line break a reader may honour (``str.splitlines``: VT, FF, NEL, U+2028, U+2029
    among them) and every other control character becomes a space first, so a definition
    name cannot add a line of its own.
    """
    joined = " ".join(str(value or "").splitlines())
    text = "".join(" " if unicodedata.category(char) == "Cc" else char for char in joined).strip()
    return sanitize_identity(text) if text else ""


#: How an input no person typed begins (``AgentInputMetadata.render``).
HARNESS_MARKER = "[Harness:"

#: The dispatch provenance a ``[Harness: ...]`` label is built from: all a merged batch's
#: Message keeps of each later Delivery's provenance (``merged_harness_sources``).
HARNESS_SOURCE_FIELDS = (
    "task_trigger_kind",
    "task_execution_id",
    "task_definition_id",
    "task_definition_name",
    "source_kind",
    "source_actor",
    "source_session_id",
    "parent_run_id",
)


def harness_source(spec: Mapping[str, Any]) -> str:
    """The label of the ``[Harness: ...]`` line for an input no person typed.

    Read from the dispatch provenance the scheduler writes (``task_trigger_kind`` and the
    run's ``source_kind``), never from the text, so a prompt that quotes a user cannot
    pass for one. Every id and name is one sanitized line; a name is quoted.
    """
    trigger = _token(spec.get("task_trigger_kind"))
    run_id = _token(spec.get("task_execution_id"))
    definition = _token(spec.get("task_definition_id"))
    name = _token(spec.get("task_definition_name")).replace('"', "'")
    name = f'"{name}"' if name else ""
    source_kind = _token(spec.get("source_kind"))
    actor = str(spec.get("source_actor") or "").strip()
    # The Agent Session that authored the input: a callback names the delegated run's
    # Session, an agent-authored run its caller (``source_actor``).
    session = _token(spec.get("source_session_id")) or (_token(actor) if source_kind == "agent" else "")
    origin = f"from #{session}" if session else ""
    if trigger == "watch":
        parts = ["Watch", definition, name]
    elif trigger == "scheduled":
        parts = ["Task", definition, name]
    elif trigger == "hook":
        # The one hook a definition still queues is a Task's on-failure Agent turn. A bare
        # one is a ``hook_send`` run an earlier release queued: ``vibe hook send`` now
        # enqueues an Agent run from the CLI, labelled as one.
        parts = ["Task", definition, name, "on-failure"] if definition else ["hook send", run_id]
    elif trigger == "webhook":
        parts = ["webhook", definition, name]
    elif trigger == "agent_run" and source_kind == "callback" and actor.startswith("vault:"):
        parts = ["Vault request", _token(actor[len("vault:") :])]
    elif trigger == "agent_run" and source_kind == "callback":
        # A terminal callback's actor is ``<run>:terminal:<status>``; the run is the parent.
        parent = _token(spec.get("parent_run_id")) or _token(actor.split(":", 1)[0])
        parts = ["callback of run", parent, origin]
    elif trigger == "agent_run":
        parts = ["agent run", run_id, origin or ("from the CLI" if source_kind == "cli" else "")]
    else:
        parts = [trigger or "background input", run_id, origin]
    return " ".join(part for part in parts if part)


def harness_label(spec: Mapping[str, Any]) -> str:
    """The ``[Harness: ...]`` label of a dispatched input, which may merge several Deliveries.

    A merged batch (``harness_sources``, one label per Delivery in FIFO order, as its
    bodies are) is named once when every label is the same, else label by label, so no
    body is attributed to another Delivery's sender.
    """
    merged = [str(label) for label in spec.get("harness_sources") or () if str(label).strip()]
    if not merged:
        return harness_source(spec)
    return merged[0] if len(set(merged)) == 1 else "; ".join(merged)


@dataclass(frozen=True)
class AgentInputMetadata:
    """Stable sender facts; the clock and display switches are evaluated on send.

    ``harness_source`` names what sent an input no person typed, as ``harness_label()``
    labels it. Its line comes first and no display switch hides it: a model must never read
    such an input as the user's own words.
    """

    user_id: str | None = None
    user_name: str | None = None
    harness_source: str | None = None
    #: The block the first turn after a backend switch carries ahead of everything else
    #: (``core/session_handoff.py``); it is context for the model, not part of the input.
    handoff: str | None = None

    def render(self, text: str, config: Any, *, now: datetime | None = None) -> str:
        lines: list[str] = [self.handoff.rstrip() + "\n"] if self.handoff and self.handoff.strip() else []
        if self.harness_source:
            lines.append(f"{HARNESS_MARKER} {self.harness_source}]")
        if getattr(config, "include_time_info", True):
            current = now or datetime.now().astimezone()
            if current.tzinfo is None:
                current = current.astimezone()
            offset = current.strftime("%z")
            if len(offset) == 5:
                offset = f"{offset[:3]}:{offset[3:]}"
            lines.append(f"[Now: {current.strftime('%Y-%m-%d %H:%M:%S')} UTC{offset}]")
        # A person's input that begins like a Harness line names its sender even with the
        # identity switch off, so the marker the system prompt explains cannot be forged.
        forged = not self.harness_source and text.lstrip().startswith(HARNESS_MARKER)
        if self.user_id and (getattr(config, "include_user_info", True) or forged):
            name = sanitize_identity(self.user_name or self.user_id)
            lines.append(f"[{name}<{sanitize_identity(self.user_id)}>]")
        return "\n".join([*lines, text]) if lines else text
