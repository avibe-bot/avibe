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
import re
from contextlib import ExitStack
from typing import Any, Iterator, NamedTuple, Optional

# NO MODULE-LEVEL ``storage`` IMPORT, deliberately. This module sits on the
# lightweight import chain the native-session contract pins
# (``core.handlers.session_handler`` must import without ``sqlite3`` —
# ``tests/test_native_session_providers.py``), and ANY ``storage.*`` import runs
# ``storage/__init__`` → importer → backups → ``import sqlite3``. The one storage
# constant this module needs is imported function-scoped at its single use site,
# the house pattern ``core/session_turns.py`` already uses for its deferred
# ``storage.background`` reads.

logger = logging.getLogger(__name__)

# The remainder of a composite key, at the colon that separates anchor from working
# path: a POSIX root (``/x``), a Windows root-relative or UNC path (``\x``,
# ``\\host\share``), or a drive-qualified path (``C:\x`` / ``C:/x``).
_WORKING_PATH_HEAD = re.compile(r"[A-Za-z]:[\\/]|[\\/]")

__all__ = [
    "SchedulerLaneCancellation",
    "cancel_session_scheduler_lane",
    "hold_session_admission",
    "iter_composite_split_candidates",
    "reconcile_session_runs",
    "resolve_composite_teardown_split",
    "resolve_teardown_session_ids",
    "split_composite_session_key",
    "teardown_composite_session_runs",
    "teardown_runtime_session_runs",
    "teardown_session_runs",
]


def split_composite_session_key(composite_key: Optional[str]) -> tuple[str, str]:
    """Split ``f"{session_anchor}:{working_path}"`` back into its two halves.

    THE ONE composite splitter (``core.services.running_agents`` delegates here):
    getting the boundary wrong is not a formatting slip, it is a silent teardown
    skip. The anchor half feeds ``resolve_teardown_session_ids``, so an anchor that
    grew a path fragment matches no ``agent_sessions`` row, and Claude eviction /
    cleanup / shutdown read the empty resolve as "nothing to settle" and dismantle
    the backend with its runs still ``running``.

    THE RULE, which comes from how the key is BUILT rather than from how it looks:
    an anchor never contains a filesystem path (it is ``{platform}_{thread}``,
    optionally suffixed ``:{subagent_or_routing_agent}`` and ``:superseded:{row_id}``),
    and the working path is appended exactly once, absolute. So the separator is the
    FIRST colon whose remainder starts like an absolute path — POSIX ``/``, Windows
    ``\\`` or UNC ``\\\\host``, or a drive letter (``C:\\repo`` / ``C:/repo``) — and
    every colon after it belongs to the working directory.

    FIRST rather than LAST is the whole point. ``rpartition`` was correct only while
    the working path was assumed colon-free, which it is not: on native Windows the
    path OPENS with a colon (``{anchor}:C:\\repo`` split into ``{anchor}:C`` and
    ``\\repo``), and even on POSIX a directory may legally contain one
    (``/tmp/a:b``). Scanning from the left stops at the boundary the key added; every
    later colon is inside the path where it belongs. Anchor colons stay left of it
    because a subagent or routing-agent name is not an absolute path.

    When NO absolute-path marker appears anywhere, the historical ``rpartition``
    split is kept unchanged — a relative or otherwise unrecognizable tail is no
    better identified from the left, and the fallback preserves today's answer for
    non-path keys (``"a:b"`` -> ``("a", "b")``). Returns ``(anchor, "")`` for a key
    with no separator at all.

    THE ONE THING THE RULE CANNOT DO is tell which colon the key added when an ANCHOR
    SUFFIX also looks like a path head — a subagent or routing agent literally named
    ``/review`` makes ``base:/review:/repo`` well-formed under two readings (HFR-335).
    That is not a gap in this function: no rule over the string alone can decide it,
    and this one still has to answer. Settlement paths call
    :func:`resolve_composite_teardown_split` instead, which enumerates the readings
    via :func:`iter_composite_split_candidates` and checks them against storage,
    falling back to the answer here when none resolves. This function's behaviour is
    unchanged and remains correct for every anchor that is not itself path-shaped.
    """

    resolved = str(composite_key or "").strip()
    if not resolved:
        return "", ""
    separator_index = resolved.find(":")
    while separator_index != -1:
        working_path = resolved[separator_index + 1 :]
        if _WORKING_PATH_HEAD.match(working_path):
            return resolved[:separator_index], working_path
        separator_index = resolved.find(":", separator_index + 1)
    anchor, separator, working_path = resolved.rpartition(":")
    if not separator:
        return resolved, ""
    return anchor, working_path


def iter_composite_split_candidates(
    composite_key: Optional[str],
) -> "Iterator[tuple[str, str]]":
    """Every split of a composite key that could plausibly be the real one.

    :func:`split_composite_session_key` has to answer with ONE pair, so it applies
    HFR-129's first-match rule. This yields the whole set the rule chooses from —
    every colon whose remainder starts like an absolute path — for the callers that
    can afford to check a candidate against storage instead of inferring.

    LONGEST ANCHOR FIRST, which is the reverse of the lexical rule and deliberate.
    The ambiguity only exists because an ANCHOR SUFFIX managed to look like the head
    of a path, and a longer anchor is the reading that consumes more of the key as
    what the key literally spells; the short reading wins by default only because
    nothing better was available. Callers walk this in order and stop at the first
    anchor storage actually knows, so the ordering is a preference, not a verdict.

    Yields nothing when no colon is followed by a path head at all — the case
    :func:`split_composite_session_key` answers with its historical ``rpartition``
    fallback, which has no candidates to choose between.
    """

    resolved = str(composite_key or "").strip()
    if not resolved:
        return
    candidates: list[tuple[str, str]] = []
    separator_index = resolved.find(":")
    while separator_index != -1:
        working_path = resolved[separator_index + 1 :]
        if _WORKING_PATH_HEAD.match(working_path):
            candidates.append((resolved[:separator_index], working_path))
        separator_index = resolved.find(":", separator_index + 1)
    yield from reversed(candidates)


def _anchor_names_any_row(
    controller: Any,
    *,
    session_anchor: str,
    workdir: Optional[str],
    agent_backend: Optional[str],
    exact_workdir_only: bool = False,
) -> Optional[bool]:
    """Whether storage knows this anchor at all, WITHOUT the HFR-323 refusal.

    Deliberately not :func:`resolve_teardown_session_ids`, which answers ``[]`` both
    for "no such anchor" and for "several candidates, refusing to pick". Those are
    opposite facts here: the first means this split is wrong, the second means it is
    RIGHT and its scope is unsafe. Collapsing them would make an ambiguous-but-correct
    split look unresolvable and hand the teardown on to a later, wronger candidate.

    ``exact_workdir_only`` ASKS FOR THE TIER, not just the bit (HFR-345). "Names a
    row" is not one fact when a workdir is on the table: ``agent_sessions.workdir`` is
    nullable, so the lookup answers exact rows if it has any and lenient null-workdir
    rows otherwise (HFR-128). Both come back as a bare non-empty list, and the lenient
    tier is exactly what lets a WRONG reading of a composite key look real — an
    unrelated legacy row that names no workdir will answer to any anchor it happens to
    spell. Probing with this flag suppresses that tier so the caller can rank readings
    against each other.

    Returns ``None`` — distinct from ``False`` — when the store cannot answer THIS
    KIND of probe (it predates the keyword). Answering the exact-only question with
    the lenient tier is the one degradation that must not happen silently, so the
    inability is reported and the caller falls back to the untiered walk that shipped
    before HFR-345 rather than to a mis-tiered one.
    """

    anchor = str(session_anchor or "").strip()
    if not anchor:
        return False
    sessions = getattr(controller, "sessions", None)
    finder = getattr(sessions, "find_session_ids_for_anchor", None)
    if not callable(finder):
        return False
    kwargs: dict[str, Any] = {
        "workdir": workdir or None,
        "agent_backend": str(agent_backend or "").strip() or None,
    }
    if exact_workdir_only:
        # Function-scoped on purpose — see the module docstring note: a module-level
        # ``storage`` import here pulls ``sqlite3`` into the lightweight
        # native-session import chain. This branch only runs when a probe is
        # actually being made, i.e. storage is in play anyway.
        from storage.agent_session_rows import WORKDIR_MATCH_EXACT_ONLY

        kwargs["workdir_match"] = WORKDIR_MATCH_EXACT_ONLY
    try:
        resolved = finder(anchor, **kwargs)
    except TypeError:
        if exact_workdir_only:
            logger.debug(
                "Session teardown: store cannot rank workdir matches for anchor %s",
                anchor,
                exc_info=True,
            )
            return None
        logger.debug(
            "Session teardown: could not probe split candidate anchor %s",
            anchor,
            exc_info=True,
        )
        return False
    except Exception:
        logger.debug(
            "Session teardown: could not probe split candidate anchor %s",
            anchor,
            exc_info=True,
        )
        return False
    return any(str(session_id or "").strip() for session_id in (resolved or []))


def resolve_composite_teardown_split(
    controller: Any,
    composite_key: Optional[str],
    *,
    agent_backend: Optional[str] = None,
    preferred_anchor: Optional[str] = None,
) -> tuple[str, str]:
    """Split a composite key, breaking lexical ties against stored anchors (HFR-335).

    WHY THE LEXICAL RULE IS NOT ENOUGH. Subagent and routing-agent names are accepted
    as arbitrary non-empty strings, and the anchor embeds them
    (``{platform}_{thread}:{agent_name}``) before the working path is appended. An
    agent named ``/review`` therefore yields ``base:/review:/repo``, where TWO colons
    are followed by something matching ``_WORKING_PATH_HEAD``. HFR-129's first-match
    rule takes the earlier one and hands the teardown anchor ``base`` with workdir
    ``/review:/repo`` — a pair no ``agent_sessions`` row matches. The resolve comes
    back empty, and the callers read empty as "nothing to settle" and dismantle the
    runtime with its runs still ``running``. That is the exact silent teardown skip
    HFR-129 was written to remove, arrived at from the other direction.

    NO RULE OVER THE STRING ALONE CAN FIX IT. Both readings are well-formed keys; the
    information about which colon the key added is simply not in the key once anchor
    suffixes may look like paths. So this asks the only party that knows: storage.
    Candidates are walked longest-anchor-first, and the first whose anchor names a real
    row wins its TIER.

    TWO TIERS, NOT ONE BIT (HFR-345). "Names a real row" is two different facts once a
    workdir is involved, because ``agent_sessions.workdir`` is nullable: a row that
    names THIS workdir, or a legacy row that names none and is admitted only as
    HFR-128's fallback. Read as a single bit, the fallback tier decides split races it
    has no business deciding — ``base:C:/repo`` whose real session is (``base``,
    ``C:/repo``) is stolen by an unrelated legacy row that happens to be anchored
    ``base:C`` with a null workdir, because longest-first reaches that reading first
    and the lenient tier lets it answer. The teardown then cancels the stranger's turn
    AND leaves its own run unsettled. So HFR-128's rule — exact outranks null-workdir —
    is applied ACROSS candidates too, not only within one: pass one walks every
    candidate for an EXACT match, and only if none exists anywhere does pass two walk
    again accepting the fallback tier. Longest-anchor-first still orders each pass; it
    is the tiebreak WITHIN a tier, never across them.

    THE COMMON CASE PAYS NOTHING. Ordinary anchors contain no path-looking segment, so
    they produce a single candidate and take the fast path with ZERO reads — no probe
    is issued at all — and ``preferred_anchor`` (End's row, which already carries its
    base session id) short-circuits even the multi-candidate walk. Only genuinely
    ambiguous keys touch the DB. Their cost is at most 2N cheap indexed reads rather
    than N, and only when no candidate matches exactly; N is the number of colons in
    the key that are followed by a path head, which is two in every case observed and
    bounded by the key's shape. Ranking correctly is worth the second walk: the reads
    are what stop a teardown from cancelling the wrong conversation.

    A STORE THAT CANNOT RANK gets the pre-HFR-345 answer rather than a wrong one. If
    the probe reports that the tier question is unsupported (a facade or test double
    predating the keyword), pass one is abandoned outright and pass two — the untiered
    first-match walk that shipped with HFR-335 — decides alone.

    AN AMBIGUOUS RESOLUTION STILL COUNTS AS RESOLVING (HFR-323), WITHIN ITS TIER. If
    the winning split's anchor names several live sessions, this still returns it and
    lets :func:`resolve_teardown_session_ids` refuse to cancel any of them. The refusal
    is about SCOPE SAFETY — which of several same-everything sessions is provably this
    runtime — and not about whether the split was read correctly. Skipping on to a
    later candidate would let a worse split slip past a guard that had already
    correctly identified the right anchor, converting a deliberate no-op into a wrong
    teardown. That holds per tier: an exact-tier anchor naming several rows wins pass
    one and the refusal stands on IT — a demotion to pass two would be the same
    skipping-on mistake, arrived at through the ranking instead of the walk.

    FALLS BACK TO THE LEXICAL ANSWER when no candidate resolves, which keeps HFR-129's
    behaviour byte-for-byte for every real anchor and for every caller with no sessions
    facade at all (headless runs, the test doubles). RESIDUAL, and the reason that
    fallback is safe FOR SETTLEMENT: an anchor that resolves nowhere has no rows to
    settle, so choosing the wrong split among unresolvable candidates changes nothing
    that could be settled. It is only a wrong ANSWER, never a wrong ACTION.
    """

    candidates = list(iter_composite_split_candidates(composite_key))
    if len(candidates) < 2:
        # One candidate or none: the lexical splitter already agrees, and there is
        # nothing to disambiguate. No DB read on the overwhelmingly common path.
        return split_composite_session_key(composite_key)

    preferred = str(preferred_anchor or "").strip()
    if preferred:
        for anchor, working_path in candidates:
            if anchor == preferred:
                return anchor, working_path

    # PASS ONE -- the exact tier. A reading whose anchor names a row that also names
    # this working directory is the strongest evidence available, at any anchor length.
    for anchor, working_path in candidates:
        exact_match = _anchor_names_any_row(
            controller,
            session_anchor=anchor,
            workdir=working_path,
            agent_backend=agent_backend,
            exact_workdir_only=True,
        )
        if exact_match is None:
            # The store cannot separate the tiers; ranking on its answers would be
            # guesswork dressed as evidence. Leave the decision to the untiered pass.
            break
        if exact_match:
            return anchor, working_path

    # PASS TWO -- the fallback tier, reached only when no reading matched exactly.
    # HFR-128's leniency still has to be able to name a row bound before workdirs
    # were recorded; it just no longer outranks a reading that matched exactly.
    for anchor, working_path in candidates:
        if _anchor_names_any_row(
            controller,
            session_anchor=anchor,
            workdir=working_path,
            agent_backend=agent_backend,
        ):
            return anchor, working_path
    return split_composite_session_key(composite_key)


def _scheduled_task_service(controller: Any) -> Any:
    service = getattr(controller, "scheduled_task_service", None)
    if service is None:
        return None
    if not callable(getattr(service, "teardown_session_runs", None)):
        return None
    return service


def _teardown_session_candidates(
    controller: Any,
    *,
    session_anchor: str,
    workdir: Optional[str] = None,
    agent_backend: Optional[str] = None,
) -> list[str]:
    """Return every live session row this runtime identity could plausibly own."""

    anchor = str(session_anchor or "").strip()
    if not anchor:
        return []
    sessions = getattr(controller, "sessions", None)
    finder = getattr(sessions, "find_session_ids_for_anchor", None)
    if not callable(finder):
        return []
    try:
        resolved = finder(
            anchor,
            workdir=workdir or None,
            agent_backend=str(agent_backend or "").strip() or None,
        )
    except Exception:
        logger.debug(
            "Session teardown: could not resolve session ids for anchor %s",
            anchor,
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
    *,
    session_anchor: str,
    workdir: Optional[str] = None,
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

    anchor = str(session_anchor or "").strip()
    candidates = _teardown_session_candidates(
        controller,
        session_anchor=anchor,
        workdir=workdir,
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
    return _unambiguous_teardown_session_ids(anchor, candidates)


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


async def teardown_runtime_session_runs(
    controller: Any,
    *,
    session_anchor: str,
    workdir: Optional[str] = None,
    agent_backend: Optional[str] = None,
    settled_by: str,
    include_manager_lane: bool = True,
    admission_holds: Optional[ExitStack] = None,
) -> int:
    """:func:`teardown_session_runs` for callers that hold a runtime identity.

    Resolves the anchor (plus working dir and backend, when known) to session ids and
    settles each one. In practice that is one id or none: the resolve narrows by
    backend and working directory and then REFUSES anything still ambiguous
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

    session_ids = resolve_teardown_session_ids(
        controller,
        session_anchor=session_anchor,
        workdir=workdir,
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


async def teardown_composite_session_runs(
    controller: Any,
    composite_key: Optional[str],
    *,
    settled_by: str,
    agent_backend: Optional[str] = None,
    include_manager_lane: bool = True,
    admission_holds: Optional[ExitStack] = None,
) -> int:
    """:func:`teardown_runtime_session_runs` keyed by a Claude composite session key.

    The split goes through :func:`resolve_composite_teardown_split` rather than the
    lexical splitter (HFR-335): this is a SETTLEMENT path, so a key whose anchor
    embeds a path-looking agent name must not resolve to nothing and be read as
    "no runs to settle". Single-candidate keys — every ordinary anchor — are answered
    without touching the database.
    """

    anchor, working_path = resolve_composite_teardown_split(
        controller,
        composite_key,
        agent_backend=agent_backend,
    )
    if not anchor:
        return 0
    return await teardown_runtime_session_runs(
        controller,
        session_anchor=anchor,
        workdir=working_path,
        agent_backend=agent_backend,
        settled_by=settled_by,
        include_manager_lane=include_manager_lane,
        admission_holds=admission_holds,
    )
