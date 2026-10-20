"""One presentation decision per agent output (docs/plans/reply-and-turn-end.md, PR 3).

What an output *is* comes from the backend (``core.agent_output``); how Avibe *shows*
it is decided here, once, by a ``PresentationPolicy``: whether it is a transcript row
and renders muted, whether it is a Web Push, whether it goes out at all, and in what
IM form. The dispatcher builds a ``PresentationInput`` for each output with content,
asks the policy, and executes the decision; the output's record, where its type has
one, stores the decision (``metadata["presentation"]``).

``DefaultPresentationPolicy`` holds the rules Avibe has always applied, unchanged:
the ``level`` grades, background (``suppress_delivery``) visibility, the Web interim
copy of long narration, the catalog's Web Push flags, the target platform's tool-call
delivery, the channel's hidden message types, and the turn's progress style. It is
the only policy; the protocol is the seam a later policy (a decision model, or one
that wraps this one) replaces it through, set as ``presentation_policy`` on the
dispatcher. A replacement may differ from the default only by the allowed deltas
documented on ``PresentationDecision``.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol

#: The ``metadata`` key an output's record stores its decision under.
PRESENTATION_METADATA_KEY = "presentation"

#: A Web narration bubble is kept as a transcript-visible ``interim`` copy once it
#: has this many non-blank lines or this display width (wide characters count twice).
INTERIM_MIN_LINES = 5
INTERIM_MIN_WIDTH = 500

#: The answer-like types: a transcript row, always delivered as a message.
_MESSAGE_TYPES = frozenset({"result", "output", "error", "notify"})
#: The process-log types: an Activity or trace record, delivered as the turn's log.
_LOG_TYPES = frozenset({"system", "assistant", "toolcall"})
#: A transcript row of these types renders muted (the released ``output``, the
#: ``interim`` copy of narration).
_MUTED_TYPES = frozenset({"output", "interim"})


class ImDelivery(str, Enum):
    """How an output goes out to its channel."""

    #: It does not.
    NONE = "none"
    #: As the turn's concise status line.
    STATUS = "status"
    #: As the turn's process log (one consolidated message, or one per output).
    LOG = "log"
    #: As a message of its own.
    MESSAGE = "message"


@dataclass(frozen=True)
class PresentationInput:
    """What a policy may decide from: one output and its context.

    ``message_type`` is the output's dispatched type (``result``, ``error``,
    ``output``, ``notify``, ``assistant``, ``toolcall``, ``system``), ``row_type`` the
    type its transcript row would carry. ``body`` is the visible text with silent
    directives removed; ``log_chunk`` what a process log would show;
    ``narration_body`` the bubble body a Web narration copy would carry, None where
    no copy can be written (not a Web narration, ``level="process"``, or a background
    Session). The
    channel's hidden types and the turn's progress style are read only when a policy
    asks, as the dispatcher has always read them. ``log_editable`` says whether the
    channel can edit a message, which a consolidated log and a status line need.

    ``content_free`` is a fact of the output, not a decision: an output with
    ``level="silent"``, or with nothing left once silent directives are removed, is
    never shown and never asked about. The dispatcher runs only its lifecycle (the
    Turn end and its IM boundary row, Run settlement, releasing the stream).
    """

    message_type: str
    row_type: str
    level: str
    platform: str
    background: bool
    body: str
    log_chunk: str = ""
    narration_body: Optional[str] = None
    metadata: Optional[Mapping[str, Any]] = None
    tools_deliverable: bool = True
    log_editable: bool = True
    hidden: Callable[[], bool] = lambda: False
    progress_style: Callable[[], str] = lambda: "verbose"

    @property
    def content_free(self) -> bool:
        return self.level == "silent" or not self.body.strip()


@dataclass(frozen=True)
class PresentationDecision:
    """How one output with content is shown; ``reason`` names why it does not go out,
    when it does not.

    Whether an output has a record is not a decision but a fixed fact of its type,
    kept best effort (none when its Session or scope row is not visible): an answer,
    notice or narration (``result``, ``output``, ``error``, ``notify``, ``assistant``)
    a ``messages`` row, a tool call an ``agent_events`` trace event, a ``system`` line
    none. The record stores the decision under ``metadata["presentation"]``.

    The dispatcher accepts the default policy's decision for the same output plus only
    these deltas, each executed in full; any other difference raises ``ValueError``
    (``validate_decision``) rather than being half-executed:

    * narration's transcript copy on or off: for Web narration the dispatcher measured
      (``assistant`` with a ``narration_body``), ``transcript`` and ``muted`` together
      keep or drop its muted ``interim`` bubble;
    * ``web_push`` declined where the default pushes.

    Everything else (``outward``, ``im``, ``reason``, and the transcript of any other
    output) equals the default. A later delta is added on its own, with its executor
    support and tests, in the change that needs it.
    """

    policy: str
    transcript: bool
    muted: bool
    web_push: bool
    outward: bool
    im: ImDelivery
    reason: str = ""

    def to_metadata(self) -> dict[str, Any]:
        """What an output's record stores of its decision."""

        return {
            "policy": self.policy,
            "outward": self.outward,
            "transcript": self.transcript,
            "muted": self.muted,
            "web_push": self.web_push,
            "im": self.im.value,
            **({"reason": self.reason} if self.reason else {}),
        }


class PresentationPolicy(Protocol):
    """Decides how one output is shown. ``id`` is recorded with every decision."""

    id: str

    def decide(self, presentation: PresentationInput) -> PresentationDecision: ...


def interim_worthy(text: Optional[str]) -> bool:
    """Whether narration is long enough to keep a transcript-visible copy."""

    body = (text or "").strip()
    width = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in body)
    if width >= INTERIM_MIN_WIDTH:
        return True
    return sum(1 for line in body.splitlines() if line.strip()) >= INTERIM_MIN_LINES


def _web_push(row_type: str, metadata: Optional[Mapping[str, Any]]) -> bool:
    from core.web_push_notifications import _is_notifiable_message

    return _is_notifiable_message(row_type, dict(metadata) if metadata else None)


class DefaultPresentationPolicy:
    """The rules Avibe has always applied, in the order it has always applied them."""

    id = "default/1"

    def decide(self, presentation: PresentationInput) -> PresentationDecision:
        def decision(**fields: Any) -> PresentationDecision:
            values = {
                "transcript": False,
                "muted": False,
                "web_push": False,
                "outward": True,
                "im": ImDelivery.NONE,
                **fields,
            }
            return PresentationDecision(policy=self.id, **values)

        kind = presentation.message_type
        if kind in _MESSAGE_TYPES:
            transcript = {"transcript": True, "muted": presentation.row_type in _MUTED_TYPES}
            if presentation.background:
                # A background Session keeps local history and delivers nothing outward.
                return decision(**transcript, outward=False, reason="background")
            return decision(
                **transcript,
                web_push=presentation.platform == "avibe"
                and _web_push(presentation.row_type, presentation.metadata),
                im=ImDelivery.MESSAGE,
            )
        if presentation.background:
            return decision(outward=False, reason="background")
        # Long Web narration also keeps a muted, transcript-visible ``interim`` copy;
        # ``level="process"`` keeps it log-only.
        copy = presentation.narration_body is not None and interim_worthy(presentation.narration_body)
        shown = {"transcript": copy, "muted": copy}
        if kind == "toolcall" and not presentation.tools_deliverable:
            return decision(**shown, reason="tools_unsupported")
        # The concise status line is the turn's single process line, not the verbose
        # log the hidden types govern: narration and tool calls go to it regardless.
        if kind in {"assistant", "toolcall"} and presentation.progress_style() == "concise":
            return decision(**shown, im=ImDelivery.STATUS)
        if presentation.hidden():
            return decision(**shown, reason="hidden")
        if not presentation.log_chunk:
            return decision(**shown, reason="empty")
        # A platform that cannot edit a message has no consolidated log or status
        # line to keep: each process message is sent on its own, whatever the style.
        if not presentation.log_editable:
            return decision(**shown, im=ImDelivery.LOG)
        style = presentation.progress_style()
        if style == "off":
            return decision(**shown, reason="style_off")
        if style == "concise":
            return decision(**shown, im=ImDelivery.STATUS)
        return decision(**shown, im=ImDelivery.LOG)


def validate_decision(presentation: PresentationInput, decision: PresentationDecision) -> PresentationDecision:
    """``decision`` when it is the default decision plus allowed deltas, else ``ValueError``.

    The allowed deltas are documented on ``PresentationDecision``.
    """

    default = DefaultPresentationPolicy().decide(presentation)
    delta = {
        field
        for field in ("transcript", "muted", "web_push", "outward", "im", "reason")
        if getattr(decision, field) != getattr(default, field)
    }
    allowed = set()
    if presentation.message_type == "assistant" and presentation.narration_body is not None:
        if decision.transcript is decision.muted:
            allowed |= {"transcript", "muted"}
    if default.web_push and not decision.web_push:
        allowed.add("web_push")
    if delta - allowed:
        raise ValueError(
            f"presentation policy {decision.policy!r} changes {sorted(delta - allowed)} of the default "
            f"decision, which is not an allowed delta ({decision!r} vs {default!r})"
        )
    return decision


def presentation_metadata(
    decision: Optional[PresentationDecision], metadata: Optional[Mapping[str, Any]] = None
) -> Optional[dict[str, Any]]:
    """``metadata`` with the decision recorded, for the message it decided."""

    if decision is None:
        return dict(metadata) if metadata is not None else None
    return {**(metadata or {}), PRESENTATION_METADATA_KEY: decision.to_metadata()}


__all__ = [
    "INTERIM_MIN_LINES",
    "INTERIM_MIN_WIDTH",
    "PRESENTATION_METADATA_KEY",
    "DefaultPresentationPolicy",
    "ImDelivery",
    "PresentationDecision",
    "PresentationInput",
    "PresentationPolicy",
    "interim_worthy",
    "presentation_metadata",
    "validate_decision",
]
