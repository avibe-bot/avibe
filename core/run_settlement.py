"""Shared vocabulary for how a turn's dispatch waiter was settled.

Two layers need to agree on these strings and neither owns the other:

* ``core/services/dispatch.py`` records WHY a streaming turn's waiter was
  released (``TurnDispatchOutcome.settled_by``), which is stamped on the live
  turn sink by the three release sites.
* ``core/scheduled_tasks.py`` turns that into an ``agent_runs`` terminal state
  plus ``metadata.interrupt_reason`` for a run whose backend never emitted a
  terminal result.

Keeping the strings here (rather than inline at each site) is what stops the
values in ``docs/plans/agent-run-zombie-settlement.md`` from drifting apart from
the ones in ``docs/plans/harness-run-reliability.md`` (``evicted`` /
``restarted`` / ``lifetime_timeout``), which land on the same column.
"""

from __future__ import annotations

from typing import Final

# ----- how a turn sink's ``done_event`` was released -----------------------

#: The backend emitted the turn's real terminal ``result``. The only honest
#: settlement — the out-of-band writer owns the run's terminal state.
SETTLED_BY_TERMINAL_RESULT: Final = "terminal_result"

#: The turn ended without dispatching an agent at all, so
#: ``Controller.mark_turn_complete`` released the waiter with no result
#: (blank prompt, dedup, inline stop, any future no-dispatch early return).
SETTLED_BY_NO_TERMINAL_RESULT: Final = "no_terminal_result"

#: An external stop (Running-tab "End") settled the sink as a fallback because
#: the backend was interrupted without emitting a terminal result.
SETTLED_BY_STOPPED: Final = "stopped"

#: ``dispatch_turn`` refused a second concurrent streaming turn for the session
#: and returned before any sink existed.
SETTLED_BY_REFUSED_CONCURRENT_TURN: Final = "refused_concurrent_turn"

#: Settlements that mean "no terminal result will ever arrive for this run".
SETTLEMENTS_WITHOUT_RESULT: Final = frozenset(
    {
        SETTLED_BY_NO_TERMINAL_RESULT,
        SETTLED_BY_STOPPED,
        SETTLED_BY_REFUSED_CONCURRENT_TURN,
    }
)

# ----- ``agent_runs.metadata.interrupt_reason`` ---------------------------
#
# A run terminalized by something other than its own backend result carries the
# reason so a later notification / UI badge can say which, and so a failure
# counter can tell an infrastructure fault from a broken definition.

INTERRUPT_REASON_NO_TERMINAL_RESULT: Final = SETTLED_BY_NO_TERMINAL_RESULT
INTERRUPT_REASON_STOPPED: Final = SETTLED_BY_STOPPED
INTERRUPT_REASON_REFUSED_CONCURRENT_TURN: Final = SETTLED_BY_REFUSED_CONCURRENT_TURN


# ----- user-visible reason text -------------------------------------------
#
# The run's ``error`` column is shown to the user (``vibe runs show``, the Runs
# UI, the callback message), so every settlement here needs a translated string.
# Mapping the wire values to explicit keys — instead of interpolating the value
# into a key — keeps the settlement vocabulary free to change without silently
# degrading to a raw key leaking into the UI, and lets one test assert every
# settlement resolves in every language.
SETTLEMENT_I18N_KEYS: Final = {
    SETTLED_BY_NO_TERMINAL_RESULT: "harness.run.interrupted.noTerminalResult",
    SETTLED_BY_STOPPED: "harness.run.interrupted.stopped",
    SETTLED_BY_REFUSED_CONCURRENT_TURN: "harness.run.interrupted.refusedConcurrentTurn",
}
