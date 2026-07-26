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

#: A REAL terminal output released the turn while deliberately keeping the run's
#: ownership somewhere else, so no run settlement is owed here. The Claude
#: Activity delivery-failure path is the live case: when a completion cannot be
#: persisted or delivered it emits a silent ``result`` with
#: ``MessageOutput(completes_turn=True, completes_run=False)`` to close the origin
#: turn while the REQUEUED Activity keeps the run and retries. Reading that as
#: "no terminal result" would settle an Activity-owned run ``failed`` — and fire
#: its callback — before the retry ever ran.
#:
#: This value never reaches a row: it is not in ``SETTLEMENTS_WITHOUT_RESULT``, so
#: it has no terminal status and no user-visible text, exactly like
#: ``SETTLED_BY_TERMINAL_RESULT``.
SETTLED_BY_TURN_ONLY_RESULT: Final = "turn_only_result"

#: An agent-runtime refresh retired the turn: ``release_for_backend_refresh``
#: cancels in-flight turns of a backend whose cached process state is about to
#: disappear (an ``agents.*`` save's rolling reconciliation, a Codex runtime
#: reload). This is NOT the same event as a user stop even though both arrive as
#: a cancelled task — nobody asked for this run to end, so it must not be
#: reported as ``canceled`` with the user-stop explanation.
#:
#: Spelled ``backend_refresh`` rather than reusing ``harness-run-reliability``'s
#: ``restarted``: that value is reserved for the *service* restarting around a
#: run, which recovery retries, while this one interrupts a live turn inside a
#: healthy process and does not.
SETTLED_BY_BACKEND_REFRESH: Final = "backend_refresh"

#: Settlements that mean "no terminal result will ever arrive for this run" — the
#: ONLY ones a caller may terminalize a row from. Both settlement lanes test
#: membership here rather than excluding ``SETTLED_BY_TERMINAL_RESULT``, so a new
#: "the turn ended but the run lives on" value can never be mistaken for a zombie.
SETTLEMENTS_WITHOUT_RESULT: Final = frozenset(
    {
        SETTLED_BY_NO_TERMINAL_RESULT,
        SETTLED_BY_STOPPED,
        SETTLED_BY_REFUSED_CONCURRENT_TURN,
        SETTLED_BY_BACKEND_REFRESH,
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
INTERRUPT_REASON_BACKEND_REFRESH: Final = SETTLED_BY_BACKEND_REFRESH


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
    SETTLED_BY_BACKEND_REFRESH: "harness.run.interrupted.backendRefresh",
}


# ----- which terminal status each settlement writes -----------------------
#
# ``stopped`` is the one settlement that carries explicit user intent: someone
# pressed End/Stop on a running agent. ``canceled`` — already in the closed
# vocabulary for exactly that — is the honest terminal, not ``failed``: the run
# did not break, it was called off. This also makes the narrow race in
# ``settle_bound_turn_sink`` benign in both directions. If the backend's terminal
# result lands first, the honest ``succeeded`` write wins and this settlement is a
# no-op; if it lands after the stop was acknowledged, the row reads ``canceled``,
# which is still true of a run the user stopped.
#
# The other settlements are infrastructure faults with no user intent behind
# them, so they stay ``failed`` and remain visible to a failure counter.
SETTLEMENT_TERMINAL_STATUS: Final = {
    SETTLED_BY_NO_TERMINAL_RESULT: "failed",
    SETTLED_BY_STOPPED: "canceled",
    SETTLED_BY_REFUSED_CONCURRENT_TURN: "failed",
    SETTLED_BY_BACKEND_REFRESH: "failed",
}


# ----- staleness-sweep reason text ----------------------------------------
#
# The settlements above are all reported by the turn that owned the run. A run
# whose owner vanished (process restart, lost turn, a queue gate that never
# reopened) has nobody left to report it, so ``sweep_stale_runs`` terminalizes it
# out of band and stamps one of these reasons instead. Same contract as
# ``SETTLEMENT_I18N_KEYS``: the text lands in the user-visible ``error`` column.
#
# The keys are the ``SWEEP_REASON_*`` values from ``storage.background``, spelled
# as literals here on purpose — this module stays dependency-free so the dispatch
# layer can import it without pulling in SQLAlchemy, and importing core from
# storage would invert the layering. ``tests/test_i18n_backend_keys.py`` asserts
# this map's key set equals the store's constants, so the two cannot drift.
SWEEP_I18N_KEYS: Final = {
    "orphaned": "harness.run.interrupted.orphaned",
    "transport_unavailable": "harness.run.interrupted.transportUnavailable",
    "queue_hold_expired": "harness.run.interrupted.queueHoldExpired",
}
