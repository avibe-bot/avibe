"""One entry point every run-blind session teardown calls before dismantling anything.

WHY THIS MODULE EXISTS. Avibe reclaims sessions from a lot of places — idle
eviction, the stuck-active backstop, a broken Claude transport, a Codex transport
going idle, controller shutdown, the Running tab's End button. Each of them used to
tear the runtime down and say nothing about the runs executing inside it, so a run
stayed ``running`` forever with no backend left to settle it, and its session lock
stayed held so the next drain could not dispatch either.

The fix is not a flag on each of those paths; it is one ordering, stated once:

    record the cause -> cancel both lanes (awaited) -> reconcile the session's rows
    -> tear the backend down

Settle first, tear down second. The reverse order is the bug: a torn-down backend
can no longer settle its own turn.

WHY A MODULE AND NOT DIRECT CALLS. The teardown entries live in four different
layers — a platform-agnostic handler, a Web-UI service, the controller, and a
backend adapter — and none of them should have to know how to find the settlement
service, or what to do when it is absent. Everything here is defensive by
construction: a controller with no scheduled task service (headless runs, the many
``SimpleNamespace`` doubles in the test suite) is a no-op, never an exception on a
path that is already tearing something down. A teardown that fails because its
settlement bookkeeping raised would be strictly worse than the bug being fixed.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack
from typing import Any, NamedTuple, Optional

from core.runtime_anchor import RuntimeAnchor

# NO MODULE-LEVEL ``storage`` IMPORT, deliberately. This module sits on the
# lightweight import chain the native-session contract pins
# (``core.handlers.session_handler`` must import without ``sqlite3`` —
# ``tests/test_native_session_providers.py``), and ANY ``storage.*`` import runs
# ``storage/__init__`` → importer → backups → ``import sqlite3``.
# ``core.runtime_anchor`` is standard-library-only for exactly this reason.

logger = logging.getLogger(__name__)

__all__ = [
    "SchedulerLaneCancellation",
    "cancel_session_scheduler_lane",
    "hold_session_admission",
    "reconcile_session_runs",
    "resolve_teardown_session_ids",
    "teardown_anchor_session_runs",
    "teardown_session_runs",
]


def _scheduled_task_service(controller: Any) -> Any:
    service = getattr(controller, "scheduled_task_service", None)
    if service is None:
        return None
    if not callable(getattr(service, "teardown_session_runs", None)):
        return None
    return service


def _teardown_session_candidates(
    controller: Any,
    anchor: RuntimeAnchor,
    *,
    agent_backend: Optional[str] = None,
) -> list[str]:
    """Return every live session row this runtime identity could plausibly own.

    The workdir goes down as :attr:`RuntimeAnchor.storage_workdir` because the row it
    has to match was written through the same normalizer; comparing a raw caller
    workdir against a normalized column is a lookup that misses for a reason that has
    nothing to do with ownership.
    """

    if not anchor:
        return []
    sessions = getattr(controller, "sessions", None)
    finder = getattr(sessions, "find_session_ids_for_anchor", None)
    if not callable(finder):
        return []
    try:
        resolved = finder(
            anchor.session_anchor,
            workdir=anchor.storage_workdir,
            agent_backend=str(agent_backend or "").strip() or None,
        )
    except Exception:
        logger.debug(
            "Session teardown: could not resolve session ids for anchor %s",
            anchor.session_anchor,
            exc_info=True,
        )
        return []
    return [str(session_id) for session_id in (resolved or []) if session_id]


def _unambiguous_teardown_session_ids(
    session_anchor: str, candidates: list[str]
) -> list[str]:
    """Keep cancellation safe while retaining candidates for admission holds."""

    if len(candidates) > 1:
        logger.warning(
            "Session teardown: anchor %s still names %d live sessions after backend "
            "and working-directory narrowing; refusing to cancel any of them because "
            "none of them is provably the runtime being reclaimed. Their runs stay "
            "open for restart recovery and the staleness sweep.",
            session_anchor,
            len(candidates),
        )
        return []
    return candidates


def resolve_teardown_session_ids(
    controller: Any,
    anchor: RuntimeAnchor,
    *,
    agent_backend: Optional[str] = None,
    admission_holds: Optional[ExitStack] = None,
    admission_hold_unambiguous: bool = True,
    admission_drain_on_release: bool = True,
    admission_drain_veto: bool = False,
) -> list[str]:
    """Resolve a RUNTIME identity to the Avibe session ids a teardown must settle.

    The runtime knows anchors and working directories; ``agent_runs.session_id`` and
    the scheduler's session lock cache are both keyed by the ``agent_sessions`` row
    id. This is the hop between them (the two-hop resolve §3.3 of the plan calls
    for), and it is deliberately the only place that hop is spelled: eviction, Codex
    transport eviction and End all start from a different runtime identity but need
    the same answer.

    THE IDENTITY ARRIVES AS A PAIR, never as a composite string to be taken apart
    here. Callers hold both halves already — Codex reads them from its session
    manager, Claude from the bound runtime client, End from its row — and a resolve
    that re-derived them from a joined key would be guessing at exactly the moment it
    must not (see :class:`core.runtime_anchor.RuntimeAnchor`).

    Returns a LIST because ``(scope, anchor)`` is the unique key, not the anchor
    alone. Normally one id; empty when the anchor names no live session, which is the
    common and cheap case (an IM session with no harness runs).

    ``agent_backend`` is the runtime's OWN backend, and every caller has it because
    every caller is a specific runtime being reclaimed. It is passed down as a
    predicate rather than used to pick a winner afterwards: an ambiguous list is not
    inspected here, it is CANCELLED, so the narrowing has to happen before the rows
    become ids.

    WHAT REMAINS AMBIGUOUS IS REFUSED, NOT CANCELLED (HFR-323). ``(scope_id,
    session_anchor)`` is the unique key, so several live scopes can still share
    anchor + backend + workdir — shared default working directories make that
    ordinary rather than exotic — and backend/workdir equality is not evidence of
    runtime OWNERSHIP. The callers cancel every id this returns, so handing back two
    candidates means interrupting a healthy turn in a conversation that had nothing
    to do with this teardown. When more than one candidate survives the narrowing,
    this returns NOTHING and says so: an unsettled run is recoverable (restart
    recovery and the staleness sweep both pick it up), a wrongly cancelled live turn
    is not.

    NO IN-PROCESS SIGNAL IS CONSULTED TO BREAK THE TIE, because none is decisive.
    ``SessionTurnManager.in_flight`` and the scheduler's session lock caches are
    keyed by session id and can legitimately be occupied for BOTH candidates at once
    — precisely the case that must not be resolved by guessing — and the activity
    registry's runtime-key map holds one last-writer-wins entry per composite key, so
    it names the most recent scope rather than the owning one. The two facts that
    WOULD be decisive are not available here: ``agent_sessions.native_session_id`` is
    write-once and would identify the runtime exactly, but no lookup by it exists and
    it is empty for a session reserved and not yet dispatched; and the scope key
    makes the row unique by construction, but the runtime teardown paths do not carry
    one (which is why this function exists at all). Plumbing either through is a
    behaviour change for another change than a safety fix.

    Residual, deliberately accepted: runs inside genuinely same-everything scopes are
    not settled at teardown time. Restart recovery and the sweep are the backstop.
    """

    candidates = _teardown_session_candidates(
        controller,
        anchor,
        agent_backend=agent_backend,
    )
    # HFR-369. Ambiguity refuses CANCELLATION because none of these rows is a
    # provable owner, but the caller still dismantles the shared runtime. Retain
    # every plausible row as an admission-only owner for that removal window. The
    # optional stack lets Codex's two-phase multi-session eviction acquire every
    # hold before settling any row, while ordinary read-only resolution is unchanged.
    if admission_hold_unambiguous or len(candidates) != 1:
        for session_id in candidates:
            hold_session_admission(
                controller,
                session_id,
                admission_holds=admission_holds,
                drain_on_release=admission_drain_on_release,
                drain_veto=admission_drain_veto,
            )
    return _unambiguous_teardown_session_ids(anchor.session_anchor, candidates)


def hold_session_admission(
    controller: Any,
    session_id: str,
    *,
    admission_holds: Optional[ExitStack],
    drain_on_release: bool = True,
    drain_veto: bool = False,
) -> None:
    """Extend one session's teardown admission hold onto the CALLER's scope (HFR-330).

    ``SessionTurnManager.release_for_teardown`` closes admission for the duration of
    its own cancel-and-await, but the window it guards does not end there: the caller
    goes on to drop the cached runtime client, and until that happens a session whose
    ``in_flight`` entry was already popped reads IDLE with a live client still
    registered. A message admitted in that gap dispatches onto the dying runtime.

    So the hold is handed OUTWARD rather than resolved twice. The caller owns an
    ``ExitStack``; this registers the manager's reentrant hold on it here, where the
    session id has just been resolved from a runtime identity (and where the HFR-323
    narrowing already refused anything ambiguous), and the stack releases it when the
    caller's teardown block exits — after the runtime is gone, on every path including
    exceptions. Passing ``None`` opts out and leaves ``release_for_teardown``'s own
    narrower hold as the only guard.

    THIS HOLD ALSO OWES THE DRAIN (HFR-332). Refusing admission is only half a
    contract: the message the refusal pushed onto the durable send-while-busy queue
    has to run once the teardown is over, and nothing dequeues it — the turn that
    would have flushed ended inside ``release_for_teardown``'s ``gather``, and the
    callers here drop the runtime and return. THIS is the hold that may ask for the
    drain, precisely because it is the one that spans the runtime removal: when it
    releases, the dying client is gone and a queued row has a replacement to land on.
    ``release_for_teardown``'s own narrower hold asks for nothing, since its exit is
    still inside the window.

    ...BUT NOT EVERY RUNTIME REMOVAL WANTS THE QUEUE BACK (HFR-334). The drain is the
    DEFAULT because the callers that motivated it — Claude cleanup/eviction, Codex
    transport eviction — all reclaim a session that is expected to keep serving its
    conversation, so a queued row has a replacement runtime to land on and running it
    is the user's evident intent. ``drain_on_release=False`` is for the two callers
    where reopening onto a fresh runtime would be WRONG rather than merely early:

    - CONTROLLER SHUTDOWN. A drain would start a brand-new turn inside a process that
      is exiting, against backends ``cleanup_sync`` is about to dismantle. The queue is
      durable, so the honest answer is to leave the row for the next start.
    - THE RUNNING TAB'S END. Stop semantics are explicitly "do not flush" — End is a
      user saying stop, and draining afterwards would start the very work they just
      stopped.

    Both still WANT the hold: refusing admission during their teardown is what keeps a
    racing message off the runtime being dismantled. They only decline the second half
    of the contract, and they can, because for them the durable queue's own backstops
    (the next submission, restart recovery) are the correct owner rather than a
    stopgap.

    Defensive like everything else in this module: a controller with no turn manager
    (headless runs, the test doubles) is a silent no-op, never an exception on a path
    already tearing something down.
    """

    if admission_holds is None:
        return
    resolved = str(session_id or "").strip()
    if not resolved:
        return
    manager = getattr(controller, "session_turns", None)
    hold = getattr(manager, "teardown_admission", None)
    if not callable(hold):
        return
    try:
        # ``drain_veto`` rides only when asserted, so a manager predating HFR-351
        # (test doubles, partial controllers) keeps accepting the ordinary hold.
        if drain_veto:
            admission_holds.enter_context(
                hold(resolved, drain_on_release=drain_on_release, drain_veto=True)
            )
        else:
            admission_holds.enter_context(
                hold(resolved, drain_on_release=drain_on_release)
            )
    except Exception:
        logger.warning(
            "Session teardown: holding admission for session %s failed",
            resolved,
            exc_info=True,
        )


async def teardown_session_runs(
    controller: Any,
    session_id: str,
    *,
    settled_by: str,
    include_manager_lane: bool = True,
) -> int:
    """Cancel and settle everything running in one session. Returns what it touched.

    Thin, forgiving wrapper over ``ScheduledTaskService.teardown_session_runs`` — the
    method owns the cancel/await/reconcile ordering; this owns "find the service, and
    never let its absence or failure break a teardown".
    """

    resolved = str(session_id or "").strip()
    if not resolved:
        return 0
    service = _scheduled_task_service(controller)
    if service is None:
        return 0
    try:
        result = await service.teardown_session_runs(
            resolved,
            settled_by=settled_by,
            include_manager_lane=include_manager_lane,
        )
    except Exception:
        logger.warning(
            "Session teardown: settling runs for session %s as %s failed",
            resolved,
            settled_by,
            exc_info=True,
        )
        return 0
    return int(getattr(result, "cancelled_count", 0)) + int(
        getattr(result, "reconciled_count", 0)
    )


class SchedulerLaneCancellation(NamedTuple):
    """The two ownership halves a scheduler-lane-only cancellation hands its caller.

    ``claimed_run_ids`` is interruption evidence: work this call cancelled, or held and
    could not reach. It is safe to reconcile.

    ``manager_lane_run_ids`` is NOT (HFR-324). Those runs belong to live turns this
    call deliberately did not touch, so reconciling them settles work that is still
    executing. They are reported because this helper's caller excluded the lane in
    order to stop that turn on its own path — and may claim them only on the branch
    where that stop actually ran.
    """

    claimed_run_ids: frozenset[str]
    manager_lane_run_ids: frozenset[str]


async def cancel_session_scheduler_lane(
    controller: Any,
    session_id: str,
    *,
    settled_by: str,
) -> SchedulerLaneCancellation:
    """Cancel only the scheduler lane, and hand back the pre-cancel ownership snapshot.

    For the ONE caller that owns the manager lane itself: the Running tab's End runs
    the canonical user-Stop path for the turn, which must keep recording ``stopped``
    and keep the backend's own Stop behaviour. Cancelling the turn here first would
    leave that path with nothing to stop and degrade a successful End into an error.

    The snapshot has to cross the caller's own stop, which is why it is returned
    rather than consumed here: the reconcile that follows the stop needs the ownership
    that existed BEFORE any of it began, and by then every map that recorded it has
    been cleared.

    THE TURN LANE'S OWNERSHIP COMES BACK ON A SEPARATE CHANNEL (HFR-324). It used to
    ride inside the claim, which reads as "this call interrupted these runs" and is
    false for a lane the call was told to leave alone — End's idle branch, where the
    stop never runs, reconciled a live turn's row into ``canceled`` on the strength of
    it. Whether the caller's own stop earned that half is the caller's fact, not this
    function's, so the two are kept apart and the caller unions them where it knows.
    """

    empty = SchedulerLaneCancellation(frozenset(), frozenset())
    resolved = str(session_id or "").strip()
    if not resolved:
        return empty
    service = getattr(controller, "scheduled_task_service", None)
    canceller = getattr(service, "cancel_session_executions", None)
    if not callable(canceller):
        return empty
    try:
        result = await canceller(
            resolved, settled_by=settled_by, include_manager_lane=False
        )
    except Exception:
        logger.warning(
            "Session teardown: scheduler-lane cancellation for session %s as %s failed",
            resolved,
            settled_by,
            exc_info=True,
        )
        return empty
    return SchedulerLaneCancellation(
        claimed_run_ids=frozenset(getattr(result, "claimed_run_ids", frozenset())),
        manager_lane_run_ids=frozenset(getattr(result, "unclaimed_manager_run_ids", frozenset())),
    )


async def reconcile_session_runs(
    controller: Any,
    session_id: str,
    *,
    settled_by: str,
    claimed_run_ids: "frozenset[str] | set[str]",
) -> int:
    """Run the session-scoped reconcile alone, for a caller that cancelled in pieces.

    Companion to :func:`cancel_session_scheduler_lane`. Both halves exist separately
    only because End interleaves its own stop between them; every other entry uses
    :func:`teardown_session_runs`, which keeps the two adjacent and in order.
    """

    resolved = str(session_id or "").strip()
    if not resolved or not claimed_run_ids:
        return 0
    service = getattr(controller, "scheduled_task_service", None)
    reconciler = getattr(service, "reconcile_session_teardown", None)
    if not callable(reconciler):
        return 0
    try:
        return int(
            reconciler(resolved, settled_by=settled_by, claimed_run_ids=claimed_run_ids)
        )
    except Exception:
        logger.warning(
            "Session teardown: reconciling session %s as %s failed",
            resolved,
            settled_by,
            exc_info=True,
        )
        return 0


async def teardown_anchor_session_runs(
    controller: Any,
    anchor: RuntimeAnchor,
    *,
    settled_by: str,
    agent_backend: Optional[str] = None,
    include_manager_lane: bool = True,
    admission_holds: Optional[ExitStack] = None,
) -> int:
    """:func:`teardown_session_runs` for callers that hold a runtime identity.

    Resolves the anchor (plus its working dir and the backend, when known) to session
    ids and settles each one. In practice that is one id or none: the resolve narrows
    by backend and working directory and then REFUSES anything still ambiguous
    (HFR-323), so this loop never cancels a candidate on the strength of a guess.

    THE RESOLVE MUST BE NARROW BECAUSE THE CANCEL LEG HAS NO OWNERSHIP CHECK. The
    reconciler intersects with the pre-cancel ownership snapshot, so a wrong id is
    inert THERE — it can only fail to find rows. ``cancel_session_executions`` and
    ``release_for_teardown`` have no such guard: they interrupt whatever each
    candidate is running, so a foreign scope's live turn would be cancelled by
    another scope's eviction. ``agent_backend`` — which every runtime caller knows
    about itself — keeps the candidate set to sessions this runtime could plausibly
    own, and the refusal covers what that still cannot separate.

    ``admission_holds`` is the caller's ``ExitStack``, for the callers that go on to
    remove the runtime after this returns — see :func:`hold_session_admission`
    (HFR-330). This is the one place a runtime identity has already become a session
    id, so registering the hold here costs no second resolve.
    """

    if not anchor:
        return 0
    session_ids = resolve_teardown_session_ids(
        controller,
        anchor,
        agent_backend=agent_backend,
        admission_holds=admission_holds,
    )
    touched = 0
    for session_id in session_ids:
        touched += await teardown_session_runs(
            controller,
            session_id,
            settled_by=settled_by,
            include_manager_lane=include_manager_lane,
        )
    return touched
