"""Shared vocabulary for how an active execution ended without a backend result.

Two layers need to agree on these strings and neither owns the other:

* ``core/services/dispatch.py`` records WHY a streaming turn's waiter was
  released (``TurnDispatchOutcome.settled_by``), which is stamped on the live
  turn sink by the three release sites.
* ``core/scheduled_tasks.py`` turns a waiter settlement or an execution-level
  teardown into an ``agent_runs`` terminal state plus
  ``metadata.interrupt_reason``.

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
#: Spelled ``backend_refresh`` rather than ``restarted`` because it identifies a
#: different infrastructure boundary: a live Agent runtime inside an otherwise
#: healthy service, rather than the service itself shutting down.
SETTLED_BY_BACKEND_REFRESH: Final = "backend_refresh"

#: The scheduled service shut down while its exact claimed request was executing.
#: The interrupted attempt is terminal; a later process must not replay it.
SETTLED_BY_RESTARTED: Final = "restarted"

#: An in-flight claimed request was cancelled without a more specific teardown
#: cause. This is still an infrastructure interruption, never a requeue signal.
SETTLED_BY_INTERRUPTED: Final = "interrupted"

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
        SETTLED_BY_RESTARTED,
        SETTLED_BY_INTERRUPTED,
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
INTERRUPT_REASON_RESTARTED: Final = SETTLED_BY_RESTARTED
INTERRUPT_REASON_INTERRUPTED: Final = SETTLED_BY_INTERRUPTED

#: Reserved for ``docs/plans/harness-run-reliability.md`` (PR2 / PR4 / PR7). Named
#: here now because the classification below has to be closed over them before
#: they are written, or each PR would have to remember to widen it.
INTERRUPT_REASON_EVICTED: Final = "evicted"
INTERRUPT_REASON_LIFETIME_TIMEOUT: Final = "lifetime_timeout"

#: The run could not be dispatched at all because the session it delivers to no
#: longer exists (``UnresolvableSessionTarget`` with ``reason == "missing"``).
#:
#: #1060's field evidence is the whole argument for naming this one. A watch pinned
#: to a session that later ceased to exist failed three deliveries and stopped, and
#: the only cause recorded anywhere was ``last_exit_code = 75`` — the user's own
#: configured ``--retry-exit-code``, i.e. the waiter's HEALTHY "nothing new yet"
#: signal. The reporter's second ask was literally "a cause field distinct from the
#: last exit code": ``delivery_target_missing`` is not ``exited 75``, and anyone
#: debugging from the exit code alone starts by investigating a working waiter.
INTERRUPT_REASON_DELIVERY_TARGET_MISSING: Final = "delivery_target_missing"


#: Per-fire failure classes recorded by the DISPATCH path itself.
#:
#: A third source vocabulary rather than an entry in ``SETTLEMENT_I18N_KEYS`` or
#: ``SWEEP_I18N_KEYS``, because it has a third origin and the honest derivation
#: downstream depends on saying so. Those two describe a run that WAS dispatched:
#: a settlement is how a live turn's waiter was released, and a sweep reason is what
#: a run's vanished owner left behind. This one is recorded BEFORE any turn exists —
#: the executor could not resolve the target, so nothing was ever dispatched to
#: release or to orphan.
#:
#: A frozenset and not a map, deliberately. ``SETTLEMENT_I18N_KEYS`` /
#: ``SWEEP_I18N_KEYS`` exist because their reasons ARE the run's user-visible
#: ``error`` column and therefore need a translated long description. A dispatch
#: failure already has a better one: the exception's own text names the missing
#: session id ("agent session id not found: sesd46nxp3cz5"), which is strictly more
#: informative than any generic sentence this module could supply. Adding a
#: ``harness.run.interrupted.*`` twin would be copy that never renders.
#:
#: What these values DO need is the short parenthetical LABEL for the notice body,
#: which lives with the other labels in
#: ``core.failure_notices.NOTICE_FAILURE_CLASS_I18N_KEYS``.
DISPATCH_FAILURE_REASONS: Final = frozenset({INTERRUPT_REASON_DELIVERY_TARGET_MISSING})


#: ``interrupt_reason`` values that terminate ONE run out of band and say nothing
#: about whether its definition works.
#:
#: This set exists because ``interrupt_reason`` is NOT a synonym for "interrupted".
#: It is the general marker for "terminalized by something other than its own
#: backend result", and most of the values it carries are ordinary per-fire
#: verdicts: a turn that ended without dispatching an agent
#: (``no_terminal_result``), a refused concurrent turn, a run whose transport was
#: never available, a queue hold that expired, a delivery target that no longer
#: exists. Those recur on every fire, are exactly the failures P6 exists to surface,
#: and must count toward a definition's health and share one suppression streak.
#:
#: ``delivery_target_missing`` is the sharpest case of that rule and is deliberately
#: ABSENT below. A definition pinned to a deleted session fails on EVERY fire, so
#: admitting it here would give it an unsuppressed notice per fire, mint an
#: ``interrupt:{run}:{reason}`` identity the live path never uses, and take it out of
#: the health window — reporting a permanently broken watch as healthy while
#: notifying about it forever. It is a per-fire verdict about the definition, which
#: is exactly what this set excludes.
#:
#: The values below are the opposite shape. Each terminates a specific run from
#: outside — a deploy, an eviction, a lifetime cap, a supervisor whose process
#: vanished, a user pressing Stop — at most once per run, and re-firing the
#: definition is expected to work. So they:
#:
#: * stay OUT of the derived health window (they are not evidence about the
#:   definition), and
#: * stay OUT of the consecutive-failure streak, notifying per run instead
#:   (bounded by the number of runs, so there is nothing to suppress).
#:
#: The discriminator has to be membership here rather than ``interrupt_reason IS
#: NOT NULL``: nullness excludes the common failure population, which reports a
#: permanently broken definition as healthy AND gives every one of its failures an
#: unsuppressed notice — P6 and the daily spam it forbids, at the same time.
RUN_INTERRUPTION_REASONS: Final = frozenset(
    {
        SETTLED_BY_STOPPED,
        SETTLED_BY_BACKEND_REFRESH,
        SETTLED_BY_INTERRUPTED,
        INTERRUPT_REASON_EVICTED,
        INTERRUPT_REASON_RESTARTED,
        INTERRUPT_REASON_LIFETIME_TIMEOUT,
        # The sweep's "owner vanished" class: a process restart by another name.
        "orphaned",
    }
)


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
    SETTLED_BY_RESTARTED: "harness.run.interrupted.restarted",
    SETTLED_BY_INTERRUPTED: "harness.run.interrupted.interrupted",
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
    SETTLED_BY_RESTARTED: "failed",
    SETTLED_BY_INTERRUPTED: "failed",
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
