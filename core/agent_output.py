"""One typed output event set for every agent backend (docs/plans/reply-and-turn-end.md).

A backend says what an output is, never how the dispatcher should treat it:

* ``Reply``: a complete answer that does not end the current Turn, or the answer of
  detached work (an Activity) that settles its own Runs;
* ``Narration``: text beside the work, Activity-only (long narration also gets the
  Web's muted ``interim`` copy);
* ``ToolActivity``: one tool call;
* ``TurnEnd``: the Turn's end, with its ``TurnOutcome`` and, when the final reply
  coincides with the end, that reply's text;
* ``AgentError``: a backend failure the user is told about;
* ``Notice``: any other visible notice from a backend (``notify``).

``emit_output`` is the single entry. ``dispatch_call`` is the one place an event
becomes the dispatcher's message type, lifecycle and options: a ``Reply`` never ends
the Turn, a ``TurnEnd`` always does and its outcome supplies the stop, teardown and
failure semantics. A backend passes a ``MessageOutput`` only as provenance and Run
lineage (identity, committed row, Activity or Run routing). Every emit answers with a
``DeliveryReceipt``, so delivery evidence is a type rather than a convention.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Optional, Union

from core.citations import CitationBundle
from core.delivery_evidence import DeliveryEvidence
from core.message_output import MessageOutput, terminal_turn_output
from core.run_settlement import SETTLED_BY_BACKEND_REFRESH, SETTLED_BY_STOPPED


class TurnOutcome(str, Enum):
    """How a Turn ended."""

    #: It finished; its reply, if any, is its answer.
    DONE = "done"
    #: The backend failed it.
    FAILED = "failed"
    #: The user stopped it: it releases the Turn and leaves its Runs to the stop's owner.
    STOPPED = "stopped"
    #: The service retired the backend runtime under it (``backend_refresh``).
    TEARDOWN = "teardown"


@dataclass(frozen=True)
class Reply:
    """A complete answer that does not end the current Turn.

    It is a ``result`` (an ``error`` row when ``failed``) whatever follows it. Empty
    text is the content-free end of detached work whose ``output`` settles its own
    Runs (an Activity with nothing to show).
    """

    text: str
    parse_mode: Optional[str] = "markdown"
    citations: Optional[CitationBundle] = None
    footer: Optional[str] = None
    failed: bool = False
    output: Optional[MessageOutput] = None


@dataclass(frozen=True)
class Narration:
    """Text beside the work; ``process_only`` keeps it out of the Web's ``interim`` copy."""

    text: str
    parse_mode: Optional[str] = "markdown"
    process_only: bool = False
    citations: Optional[CitationBundle] = None
    status_label: Optional[str] = None
    output: Optional[MessageOutput] = None


@dataclass(frozen=True)
class ToolActivity:
    """One tool call: its line, its concise status label, its hosted-tool card."""

    text: str
    parse_mode: Optional[str] = "markdown"
    status_label: Optional[str] = None
    hosted: Optional[dict] = None
    citations: Optional[CitationBundle] = None


@dataclass(frozen=True)
class TurnEnd:
    """The Turn's end.

    ``text`` is the final reply when it coincides with the end, empty for a
    content-free end; ``silent`` settles without a bubble even when there is text.
    ``output`` is the Turn's output policy and Run lineage (``request.output``).
    """

    outcome: TurnOutcome = TurnOutcome.DONE
    text: str = ""
    parse_mode: Optional[str] = "markdown"
    citations: Optional[CitationBundle] = None
    footer: Optional[str] = None
    silent: bool = False
    terminal_error: Optional[str] = None
    output: Optional[MessageOutput] = None


@dataclass(frozen=True)
class AgentError:
    """A backend failure the user is told about; ``delivery`` records what the send proved."""

    text: str
    parse_mode: Optional[str] = "markdown"
    output: Optional[MessageOutput] = None
    delivery: Optional[DeliveryEvidence] = None


@dataclass(frozen=True)
class Notice:
    """Any other visible notice from a backend: neither a reply nor a failure."""

    text: str
    parse_mode: Optional[str] = "markdown"


AgentOutputEvent = Union[Reply, Narration, ToolActivity, TurnEnd, AgentError, Notice]


@dataclass(frozen=True)
class DeliveryReceipt:
    """What an emit proved: the delivered message's id, None when nothing was delivered.

    On the Web and in a background Session the receipt is the persisted row; on IM the
    message the platform accepted.
    """

    message_id: Optional[Any] = None

    @property
    def delivered(self) -> bool:
        return self.message_id is not None


def _turn_end_output(event: TurnEnd) -> MessageOutput:
    base = event.output or terminal_turn_output()
    if event.outcome is TurnOutcome.STOPPED:
        return replace(base, completes_turn=True, completes_run=False, settled_by=SETTLED_BY_STOPPED)
    if event.outcome is TurnOutcome.TEARDOWN:
        return replace(base, completes_turn=True, completes_run=False, settled_by=SETTLED_BY_BACKEND_REFRESH)
    if not base.completes_turn:
        raise ValueError("a TurnEnd's output must end the Turn; detached work answers with a Reply")
    return base


def dispatch_call(event: AgentOutputEvent) -> tuple[str, str, dict[str, Any]]:
    """The dispatcher's ``(message_type, text, options)`` for one event.

    Options a dispatcher stand-in may not accept (citations, status labels, hosted
    cards, footers, failure details, delivery evidence) are passed only when set, as
    every caller passed them before.
    """

    def optional(**values: Any) -> dict[str, Any]:
        return {key: value for key, value in values.items() if value}

    if isinstance(event, Reply):
        output = event.output or MessageOutput(completes_turn=False, completes_run=False)
        if output.completes_turn:
            raise ValueError("a Reply never ends the Turn; its end is a TurnEnd")
        return "result", event.text, {
            "parse_mode": event.parse_mode,
            "output": output,
            **optional(is_error=event.failed, result_footer=event.footer, citations=event.citations),
        }
    if isinstance(event, Narration):
        return "assistant", event.text, {
            "parse_mode": event.parse_mode,
            **optional(
                level="process" if event.process_only else None,
                status_label=event.status_label,
                output=event.output,
                citations=event.citations,
            ),
        }
    if isinstance(event, ToolActivity):
        return "toolcall", event.text, {
            "parse_mode": event.parse_mode,
            **optional(status_label=event.status_label, hosted=event.hosted, citations=event.citations),
        }
    if isinstance(event, TurnEnd):
        return "result", event.text, {
            "parse_mode": event.parse_mode,
            "output": _turn_end_output(event),
            **optional(
                is_error=event.outcome in (TurnOutcome.FAILED, TurnOutcome.TEARDOWN),
                level="silent" if event.silent else None,
                result_footer=event.footer,
                terminal_error=event.terminal_error,
                citations=event.citations,
            ),
        }
    if isinstance(event, AgentError):
        return "notify", event.text, {
            "parse_mode": event.parse_mode,
            **optional(output=event.output, delivery=event.delivery),
        }
    if isinstance(event, Notice):
        return "notify", event.text, {"parse_mode": event.parse_mode}
    raise TypeError(f"not an agent output event: {event!r}")


async def emit_output(controller: Any, context: Any, event: AgentOutputEvent) -> DeliveryReceipt:
    """Emit one backend output through the dispatcher; the single entry every backend uses."""

    message_type, text, options = dispatch_call(event)
    message_id = await controller.emit_agent_message(context, message_type, text, **options)
    return DeliveryReceipt(message_id)
