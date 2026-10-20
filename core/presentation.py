"""One presentation decision per agent output (docs/plans/reply-and-turn-end.md, PR 3).

What an output *is* comes from the backend (``core.agent_output``); how Avibe *shows*
it is decided here, once, by a ``PresentationPolicy``: whether it leaves a record,
whether that record is a transcript row and renders muted, whether it is a Web Push,
whether it goes out at all, and in what IM form. The dispatcher builds a
``PresentationInput`` for each output, asks the policy, and executes the decision; it
records the decision on the message (``metadata["presentation"]``).

``DefaultPresentationPolicy`` holds the rules Avibe has always applied, unchanged:
the ``level`` grades, background (``suppress_delivery``) visibility, the Web interim
copy of long narration, the catalog's Web Push flags, the target platform's tool-call
delivery, the channel's hidden message types, and the turn's progress style. It is
the only policy; the protocol is the seam a later policy (a decision model, or one
that wraps this one) replaces it through, set as ``presentation_policy`` on the
dispatcher.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol

#: The ``metadata`` key a persisted message records its decision under.
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


@dataclass(frozen=True)
class PresentationDecision:
    """How one output is shown; ``reason`` names why it does not go out, when it does not.

    Every field the dispatcher accepts, it executes in full. A decision outside the
    envelope it can execute raises ``ValueError`` (``validate_decision``) rather than
    being half-executed; a later policy that needs more extends the executor. The
    envelope:

    * not ``persisted`` is nothing at all: no record, not ``outward``, no push, ``im``
      ``none``, no transcript row;
    * not ``outward`` is local history only: ``im`` ``none``, no push, no Web
      publication (no ``message.new``, no Inbox update), the row marked
      ``delivery_suppressed``;
    * an answer or notice (``result``, ``output``, ``error``, ``notify``) that is
      persisted and outward goes out as a message (``im`` ``message``), as a
      transcript row, muted exactly when its type renders muted (``output``);
    * process-log outputs (``assistant``, ``toolcall``, ``system``) go out as
      ``none``, ``status``, or ``log``, never a message; a ``log`` line needs a body,
      and a tool call the target cannot deliver goes out as ``none``;
    * a transcript row of narration is its muted ``interim`` copy (``transcript`` and
      ``muted`` together), which needs the narration body the dispatcher measured;
      tool calls and system lines have none;
    * ``web_push`` may decline a push the catalog allows, never add one.
    """

    policy: str
    persisted: bool
    transcript: bool
    muted: bool
    web_push: bool
    outward: bool
    im: ImDelivery
    reason: str = ""

    def to_metadata(self) -> dict[str, Any]:
        """The record a persisted message keeps of its decision."""

        return {
            "policy": self.policy,
            "persisted": self.persisted,
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
                "persisted": True,
                "transcript": False,
                "muted": False,
                "web_push": False,
                "outward": True,
                "im": ImDelivery.NONE,
                **fields,
            }
            return PresentationDecision(policy=self.id, **values)

        kind = presentation.message_type
        # ``level="silent"``, or nothing left to show: no record, no delivery.
        if presentation.level == "silent" or not presentation.body.strip():
            return decision(persisted=False, outward=False, reason="content_free")
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
    """``decision`` when the dispatcher can execute it in full, else ``ValueError``.

    The envelope is documented on ``PresentationDecision``.
    """

    def reject(why: str) -> PresentationDecision:
        raise ValueError(f"presentation policy {decision.policy!r}: {why} ({decision!r})")

    kind = presentation.message_type
    if not decision.persisted:
        if decision.outward or decision.transcript or decision.muted or decision.web_push:
            return reject("an output that is not persisted is nothing at all")
        if decision.im is not ImDelivery.NONE:
            return reject("an output that is not persisted goes nowhere")
        return decision
    if not decision.outward and (decision.im is not ImDelivery.NONE or decision.web_push):
        return reject("local history goes out nowhere")
    if decision.web_push and not (
        presentation.platform == "avibe" and _web_push(presentation.row_type, presentation.metadata)
    ):
        return reject("a push the catalog does not allow cannot be added")
    if kind in _MESSAGE_TYPES:
        if not decision.transcript or decision.muted is not (presentation.row_type in _MUTED_TYPES):
            return reject("an answer or notice is a transcript row, muted exactly when its type is")
        if decision.outward and decision.im is not ImDelivery.MESSAGE:
            return reject("an answer or notice that goes out goes out as a message")
        return decision
    if decision.im is ImDelivery.MESSAGE:
        return reject("a process-log output never goes out as a message")
    if decision.transcript is not decision.muted:
        return reject("narration's transcript row is its muted interim copy")
    if decision.transcript and (kind != "assistant" or not decision.outward or presentation.narration_body is None):
        return reject("only Web narration the dispatcher measured has an interim copy")
    if decision.im is ImDelivery.LOG and not presentation.log_chunk:
        return reject("a log line needs a body")
    if kind == "toolcall" and not presentation.tools_deliverable and decision.im is not ImDelivery.NONE:
        return reject("a tool call the target cannot deliver goes out nowhere")
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
