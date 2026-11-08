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
#: The line a dictated input carries, after its time and sender lines (``AgentInputMetadata.render``).
VOICE_INPUT_LINE = "[Input: voice dictation; may contain speech-recognition errors]"
#: A Bot's permanent Session id, ``ses_<handle>`` (``core.bots.BOT_HANDLE_PATTERN``).
_BOT_SESSION = re.compile(r"ses_([a-z][a-z0-9-]{1,23})")

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
    "source_session_title",
    "parent_run_id",
    "callback_session_id",
    "callback_mode",
    "suppress_report_hint",
)
#: ``callback_mode`` of a run that asked for no callback: only a failure is reported (``core.scheduled_tasks``).
_CALLBACK_FAILURE_ONLY = "failure"
#: ``source_actor`` of a failure notice: ``<run>:notice:<status>`` (``core.scheduled_tasks``).
_NOTICE_ACTOR_MARKER = ":notice:"


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
    # Session, an agent-authored run its caller (``source_actor``). A Bot's Session is
    # ``ses_<handle>``, a namespace generated ids never enter (``docs/plans/bots.md`` D2),
    # so it is named as the Bot.
    session = _token(spec.get("source_session_id")) or (_token(actor) if source_kind == "agent" else "")
    bot = _BOT_SESSION.fullmatch(session)
    origin = f"from @<bot:{bot.group(1)}>" if bot else f"from #{session}" if session else ""
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
    elif trigger == "agent_run" and source_kind == "callback" and actor.startswith("delegation-idle:"):
        title = _token(spec.get("source_session_title")).replace('"', "'")
        parts = ["Session", f'"{title}"' if title else "", f"#{session}", "went idle without reporting"]
    elif trigger == "agent_run" and source_kind == "callback" and actor.startswith("vault:"):
        parts = ["Vault request", _token(actor[len("vault:") :])]
    elif trigger == "agent_run" and source_kind == "callback" and _NOTICE_ACTOR_MARKER in actor:
        # A run that asked for no callback still tells its sender when it did not complete.
        parent = _token(spec.get("parent_run_id")) or _token(actor.split(":", 1)[0])
        parts = ["agent run", parent, "did not complete", origin.replace("from ", "in ", 1)]
    elif trigger == "agent_run" and source_kind == "callback":
        # A terminal callback's actor is ``<run>:terminal:<status>``; the run is the parent.
        parent = _token(spec.get("parent_run_id")) or _token(actor.split(":", 1)[0])
        parts = ["callback of run", parent, origin]
    elif trigger == "agent_run":
        parts = ["agent run", run_id, origin or ("from the CLI" if source_kind == "cli" else "")]
    else:
        parts = [trigger or "background input", run_id, origin]
    return " ".join(part for part in parts if part)


def _address(session: str) -> tuple[str, str]:
    """How a Session is named in a hint, and the command that messages it."""
    bot = _BOT_SESSION.fullmatch(session)
    if bot:
        return f"@<bot:{bot.group(1)}>", f"vibe agent run --bot {bot.group(1)} --message ..."
    return f"#{session}", f"vibe agent run --session-id {session} --message ..."


def is_voice_input(metadata: Any) -> bool:
    """Whether a person's input was dictated: its message metadata says ``input: 'voice'``.

    The hold-to-talk surfaces set it (``ui/src/lib/voiceMessage.ts``); anything else,
    including no metadata at all, is typed.
    """
    return isinstance(metadata, Mapping) and metadata.get("input") == "voice"


def harness_hint(spec: Mapping[str, Any], *, receiver: Any = None) -> str:
    """Where an Agent run's result goes, for the line after its ``[Harness: ...]`` label.

    Progressive disclosure instead of system prompt guidance (``docs/plans/bots.md`` D4): a run with a
    callback returns its final reply to that Session by itself; a run without one is reported by an explicit
    message, so the input names the sender and the exact command. Empty for every other input.
    """
    if _token(spec.get("task_trigger_kind")) != "agent_run":
        return ""
    source_kind = _token(spec.get("source_kind"))
    if source_kind not in {"agent", "cli"}:
        return ""
    receiver = _token(receiver)
    callback = _token(spec.get("callback_session_id"))
    if callback and str(spec.get("callback_mode") or "") != _CALLBACK_FAILURE_ONLY:
        if callback == receiver:
            return ""
        return f"Callback: your final reply returns to {_address(callback)[0]} automatically."
    sender = _token(spec.get("source_actor")) if source_kind == "agent" else ""
    if not sender or sender == receiver:
        return ""
    if spec.get("suppress_report_hint") is True:
        return ""
    name, command = _address(sender)
    return f"No callback. When the work is done or you need a decision, you can report to {name} with `{command}`."


def harness_hints(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """The hint lines of a dispatched input: one per distinct hint of a merged batch, in its order."""
    merged = spec.get("harness_hints")
    hints = (
        [str(hint) for hint in merged]
        if isinstance(merged, (list, tuple))
        else [harness_hint(spec, receiver=spec.get("agent_session_id"))]
    )
    return tuple(dict.fromkeys(hint for hint in hints if hint.strip()))


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
    #: Lines after the ``[Harness: ...]`` one: where the result of an Agent run goes (``harness_hints``).
    harness_hints: tuple[str, ...] = ()
    #: The block the first turn after a backend switch carries ahead of everything else
    #: (``core/session_handoff.py``); it is context for the model, not part of the input.
    handoff: str | None = None
    #: The person dictated this input (``is_voice_input``). A batch never mixes dictated and
    #: typed inputs (``storage.message_deliveries.message_merge_identity``), so one flag covers it.
    voice: bool = False

    def render(self, text: str, config: Any, *, now: datetime | None = None) -> str:
        lines: list[str] = [self.handoff.rstrip() + "\n"] if self.handoff and self.handoff.strip() else []
        if self.harness_source:
            lines.append(f"{HARNESS_MARKER} {self.harness_source}]")
            lines.extend(f"[{hint}]" for hint in self.harness_hints)
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
        # How to read the words, so no display switch hides it.
        if self.voice:
            lines.append(VOICE_INPUT_LINE)
        return "\n".join([*lines, text]) if lines else text
