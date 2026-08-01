"""Coverage for ``core.session_teardown``'s runtime-identity resolve.

The module's other half — the cancel/await/reconcile ordering it delegates to
``ScheduledTaskService`` — is exercised through the callers in
``tests/test_scheduled_tasks.py`` and ``tests/test_internal_server.py``. What lives
here is the hop those tests take for granted: turning a RUNTIME identity (anchor +
working directory + backend) into the Avibe session ids a teardown is allowed to
cancel work in.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_sessions import SessionsStore
from core.run_settlement import SETTLED_BY_EVICTED
from core.scheduled_tasks import ScheduledTaskService
from core.runtime_anchor import RuntimeAnchor
from core.session_teardown import (
    hold_session_admission,
    resolve_teardown_session_ids,
    teardown_anchor_session_runs,
)
from core.session_turns import SessionTurnManager, Turn
from modules.im import MessageContext
from modules.sessions_facade import SessionsFacade
from storage.agent_session_rows import create_agent_session_row
from storage.sessions_service import resolve_scope_from_legacy_key

ANCHOR = "slack_171717.123"


def _seed_session(
    store: SessionsStore,
    *,
    legacy_scope_key: str,
    agent_backend: str,
    workdir: str | None,
    session_anchor: str = ANCHOR,
) -> str:
    service = store._service
    assert service is not None
    with service.engine.begin() as conn:
        scope_id = resolve_scope_from_legacy_key(
            conn, legacy_scope_key, now="2026-07-28T00:00:00Z"
        )
        assert scope_id is not None
        return create_agent_session_row(
            conn,
            scope_id=scope_id,
            agent_backend=agent_backend,
            agent_variant=agent_backend,
            session_anchor=session_anchor,
            native_session_id=f"{agent_backend}-{legacy_scope_key}",
            workdir=workdir,
            metadata={"legacy_scope_key": legacy_scope_key},
            require_workdir=False,
        )


def _live_turn(manager: SessionTurnManager, session_id: str, backend: str) -> asyncio.Task:
    async def _busy() -> None:
        await asyncio.sleep(60)

    task = asyncio.get_event_loop().create_task(_busy())
    context = MessageContext(user_id="U", channel_id=session_id, platform="avibe")
    context.platform_specific = {
        "agent_session_id": session_id,
        "agent_session_target": {"agent_backend": backend},
    }
    manager.in_flight[session_id] = Turn(task=task, context=context)
    return task


def test_teardown_does_not_cancel_a_foreign_scope_sharing_the_anchor(
    tmp_path: Path,
) -> None:
    """HFR-128: an ambiguous anchor is not a licence to cancel every candidate.

    ``find_session_ids_for_anchor`` drops the scope predicate because a runtime
    teardown has no scope to offer, so ``(scope, anchor)`` collapses to ``anchor``
    and two unrelated conversations can answer to the same one. That was documented
    as safe on the grounds that the reconciler's ownership intersection makes a wrong
    guess inert — true of the RECONCILE leg, and irrelevant to the one that does the
    damage: ``cancel_session_executions`` and ``release_for_teardown`` interrupt
    whatever each candidate is running, with no ownership check in front of them.

    Two narrowings the runtime genuinely owns close it. ``agent_backend`` is
    ``NOT NULL`` and exact — a Claude eviction cannot own a Codex row. And a
    null-workdir row no longer TIES with a row that names the evicted workdir: the
    leniency exists for rows bound before workdirs were recorded, so it is a
    fallback, not a supplement.
    """

    store = SessionsStore(tmp_path / "sessions.json")
    try:
        owner_id = _seed_session(
            store,
            legacy_scope_key="slack::C_owner",
            agent_backend="claude",
            workdir=str(tmp_path / "owner"),
        )
        # Same anchor, different scope, different backend — and the SAME workdir, so
        # the workdir predicate cannot exclude it and only the backend can.
        foreign_backend_id = _seed_session(
            store,
            legacy_scope_key="slack::C_codex",
            agent_backend="codex",
            workdir=str(tmp_path / "owner"),
        )
        # Same anchor, same backend, but it names no workdir — the row the old
        # "absence is not a mismatch" rule admitted alongside the exact match.
        foreign_workdir_id = _seed_session(
            store,
            legacy_scope_key="slack::C_other",
            agent_backend="claude",
            workdir=None,
        )
        assert len({owner_id, foreign_backend_id, foreign_workdir_id}) == 3

        controller = SimpleNamespace(
            sessions=SessionsFacade(store),
            config=SimpleNamespace(language="en"),
            set_agent_status=lambda *_args, **_kwargs: None,
        )
        manager = SessionTurnManager(controller)
        controller.session_turns = manager
        controller.scheduled_task_service = ScheduledTaskService(controller=controller)

        composite_key = f"{ANCHOR}:{tmp_path / 'owner'}"

        # The resolve alone already refuses both foreigners.
        assert resolve_teardown_session_ids(
            controller,
            RuntimeAnchor(ANCHOR, str(tmp_path / "owner")),
            agent_backend="claude",
        ) == [owner_id]

        async def _exercise() -> tuple[bool, bool, bool, str | None]:
            owner_task = _live_turn(manager, owner_id, "claude")
            foreign_backend_task = _live_turn(manager, foreign_backend_id, "codex")
            foreign_workdir_task = _live_turn(manager, foreign_workdir_id, "claude")
            await asyncio.sleep(0)
            try:
                await teardown_anchor_session_runs(
                    controller,
                    RuntimeAnchor.parse(composite_key),
                    settled_by=SETTLED_BY_EVICTED,
                    agent_backend="claude",
                )
                owner_turn = manager.in_flight.get(owner_id)
                cause = getattr(owner_turn, "cancel_settled_by", None)
                return (
                    owner_task.done(),
                    foreign_backend_task.done(),
                    foreign_workdir_task.done(),
                    cause,
                )
            finally:
                for task in (owner_task, foreign_backend_task, foreign_workdir_task):
                    task.cancel()
                await asyncio.gather(
                    owner_task,
                    foreign_backend_task,
                    foreign_workdir_task,
                    return_exceptions=True,
                )

        owner_done, foreign_backend_done, foreign_workdir_done, owner_cause = asyncio.run(
            _exercise()
        )

        # The evicted runtime's own turn was released, with the eviction recorded.
        assert owner_done is True
        assert owner_cause == SETTLED_BY_EVICTED
        # ...and neither neighbour was touched by somebody else's eviction.
        assert foreign_backend_done is False
        assert foreign_workdir_done is False
        assert manager.in_flight[foreign_backend_id].cancel_settled_by is None
        assert manager.in_flight[foreign_workdir_id].cancel_settled_by is None
    finally:
        store.close()


def test_teardown_refuses_ambiguous_exact_anchor_candidates(
    tmp_path: Path,
    caplog,
) -> None:
    """HFR-323/369: ambiguity refuses cancellation while closing admission.

    HFR-128 narrowed the resolve by backend and by exact workdir, which removes the
    candidates a runtime provably cannot own. It cannot remove the ones it MIGHT: the
    unique key is ``(scope_id, session_anchor)``, so two live conversations can hold
    one anchor with the same backend and the same working directory — shared default
    workdirs make that ordinary. Backend and workdir equality is not evidence of
    ownership, and the teardown callers cancel every id the resolve returns, so a
    two-candidate answer means interrupting a healthy turn belonging to a scope that
    had nothing to do with this eviction.

    Nothing in memory can break the tie: ``in_flight`` and the scheduler's lock caches
    are keyed by session id and can legitimately be occupied for BOTH candidates —
    which is exactly this test's arrangement, and exactly the case a signal that can
    be true for both cannot decide. So the resolve refuses: no ids, a warning naming
    the anchor and the count, and the runs left for restart recovery and the sweep. An
    unsettled run is recoverable; a wrongly cancelled live turn is not.
    """

    store = SessionsStore(tmp_path / "sessions.json")
    try:
        workdir = str(tmp_path / "shared")
        first_id = _seed_session(
            store,
            legacy_scope_key="slack::C_first",
            agent_backend="claude",
            workdir=workdir,
        )
        second_id = _seed_session(
            store,
            legacy_scope_key="slack::C_second",
            agent_backend="claude",
            workdir=workdir,
        )
        assert first_id != second_id

        controller = SimpleNamespace(
            sessions=SessionsFacade(store),
            config=SimpleNamespace(language="en"),
            set_agent_status=lambda *_args, **_kwargs: None,
        )
        manager = SessionTurnManager(controller)
        controller.session_turns = manager
        controller.scheduled_task_service = ScheduledTaskService(controller=controller)

        composite_key = f"{ANCHOR}:{workdir}"

        with caplog.at_level(logging.WARNING, logger="core.session_teardown"):
            assert (
                resolve_teardown_session_ids(
                    controller,
                    RuntimeAnchor(ANCHOR, workdir),
                    agent_backend="claude",
                )
                == []
            )
        refusals = [
            record.getMessage()
            for record in caplog.records
            if "refusing to cancel" in record.getMessage()
        ]
        assert len(refusals) == 1
        assert ANCHOR in refusals[0]
        assert "2 live sessions" in refusals[0]

        async def _exercise() -> tuple[bool, bool, tuple[bool, bool], tuple[bool, bool]]:
            first_task = _live_turn(manager, first_id, "claude")
            second_task = _live_turn(manager, second_id, "claude")
            await asyncio.sleep(0)
            try:
                with ExitStack() as admission_holds:
                    await teardown_anchor_session_runs(
                        controller,
                        RuntimeAnchor.parse(composite_key),
                        settled_by=SETTLED_BY_EVICTED,
                        agent_backend="claude",
                        admission_holds=admission_holds,
                    )
                    held = (
                        manager.is_teardown_admission_closed(first_id),
                        manager.is_teardown_admission_closed(second_id),
                    )
                reopened = (
                    manager.is_teardown_admission_closed(first_id),
                    manager.is_teardown_admission_closed(second_id),
                )
                return first_task.done(), second_task.done(), held, reopened
            finally:
                for task in (first_task, second_task):
                    task.cancel()
                await asyncio.gather(first_task, second_task, return_exceptions=True)

        first_done, second_done, held, reopened = asyncio.run(_exercise())

        # Neither live turn was interrupted, and neither carries a settlement cause.
        assert first_done is False
        assert second_done is False
        assert held == (True, True), (
            "every plausible owner must refuse new turns until the shared runtime is gone"
        )
        assert reopened == (False, False)
        assert manager.in_flight[first_id].cancel_settled_by is None
        assert manager.in_flight[second_id].cancel_settled_by is None
        assert manager.in_flight[first_id].stop_no_flush is False
        assert manager.in_flight[second_id].stop_no_flush is False
    finally:
        store.close()

    # Companion: the refusal is about AMBIGUITY, not about anchors. One exact match
    # still resolves and is still torn down, or the fix would have disabled teardown.
    single_store = SessionsStore(tmp_path / "sessions-single.json")
    try:
        only_id = _seed_session(
            single_store,
            legacy_scope_key="slack::C_only",
            agent_backend="claude",
            workdir=str(tmp_path / "solo"),
        )
        controller = SimpleNamespace(
            sessions=SessionsFacade(single_store),
            config=SimpleNamespace(language="en"),
            set_agent_status=lambda *_args, **_kwargs: None,
        )
        manager = SessionTurnManager(controller)
        controller.session_turns = manager
        controller.scheduled_task_service = ScheduledTaskService(controller=controller)

        assert resolve_teardown_session_ids(
            controller,
            RuntimeAnchor(ANCHOR, str(tmp_path / "solo")),
            agent_backend="claude",
        ) == [only_id]

        async def _exercise_single() -> tuple[bool, str | None]:
            task = _live_turn(manager, only_id, "claude")
            await asyncio.sleep(0)
            try:
                await teardown_anchor_session_runs(
                    controller,
                    RuntimeAnchor.parse(f"{ANCHOR}:{tmp_path / 'solo'}"),
                    settled_by=SETTLED_BY_EVICTED,
                    agent_backend="claude",
                )
                turn = manager.in_flight.get(only_id)
                return task.done(), getattr(turn, "cancel_settled_by", None)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        done, cause = asyncio.run(_exercise_single())
        assert done is True
        assert cause == SETTLED_BY_EVICTED
    finally:
        single_store.close()


def test_anchor_resolve_still_accepts_a_null_workdir_row_when_nothing_matches_exactly(
    tmp_path: Path,
) -> None:
    """HFR-128: the workdir leniency survives where it was actually needed.

    Narrowing to exact matches must not turn into "a row with no workdir is never a
    candidate": a session bound before workdirs were recorded names none, and it is
    the only row there is. It stays resolvable — it just loses to a row that names
    the evicted directory.
    """

    store = SessionsStore(tmp_path / "sessions.json")
    try:
        legacy_id = _seed_session(
            store,
            legacy_scope_key="slack::C_legacy",
            agent_backend="claude",
            workdir=None,
        )
        controller = SimpleNamespace(sessions=SessionsFacade(store))

        assert resolve_teardown_session_ids(
            controller,
            RuntimeAnchor(ANCHOR, str(tmp_path / "owner")),
            agent_backend="claude",
        ) == [legacy_id]
        # And a caller that names no backend at all is unchanged.
        assert resolve_teardown_session_ids(
            controller, RuntimeAnchor(ANCHOR, str(tmp_path / "owner"))
        ) == [legacy_id]
    finally:
        store.close()


def _parsed(composite_key: str | None) -> tuple[str, str]:
    """``RuntimeAnchor.parse`` as the (anchor, workdir) pair the assertions read."""

    anchor = RuntimeAnchor.parse(composite_key)
    return anchor.session_anchor, anchor.workdir


def test_legacy_key_parse_preserves_windows_drive_paths() -> None:
    """HFR-129: the composite separator is the one the KEY added, not the last colon.

    ``f"{base_session_id}:{working_path}"`` is built once, with the working path
    appended last — but the working path is not colon-free. On native Windows it
    starts with a drive letter (``C:\\repo``), and even on POSIX a directory may
    legally contain one (``/tmp/a:b``). ``rpartition`` splits at the LAST colon, which
    for both of those lands INSIDE the path: the anchor grows a path fragment
    (``…:C``) and the workdir shrinks to a tail (``\\repo``), so the row lookup that
    every teardown depends on matches nothing.

    The boundary rule is a property of how the key is built: an anchor never contains
    a filesystem path, and the path is appended exactly once and absolute. So the
    separator is the FIRST colon whose remainder looks like the start of an absolute
    path, and every colon after it belongs to the working directory.

    THIS IS THE LEGACY PATH ONLY. Every live teardown carries a
    :class:`RuntimeAnchor` built where the two halves were still separate, so nothing
    it settles depends on this rule; :meth:`RuntimeAnchor.parse` is for a persisted
    key with no structured origin, and the rule is pinned because that string still
    has to read back the way it was written.
    """

    anchor = "slack_171717.123"

    # Windows drive paths, in both slash conventions.
    assert _parsed(f"{anchor}:C:\\repo") == (anchor, "C:\\repo")
    assert _parsed(f"{anchor}:C:/repo") == (anchor, "C:/repo")
    # A UNC / root-relative Windows path.
    assert _parsed(f"{anchor}:\\\\host\\share") == (
        anchor,
        "\\\\host\\share",
    )
    # A POSIX directory that legally contains a colon.
    assert _parsed("a:/tmp/x:y") == ("a", "/tmp/x:y")
    # A subagent base — the anchor itself carries a colon, and it must survive.
    assert _parsed("slack_T1:reviewer:/work") == (
        "slack_T1:reviewer",
        "/work",
    )
    assert _parsed(f"slack_T1:reviewer:C:\\work") == (
        "slack_T1:reviewer",
        "C:\\work",
    )
    # No separator at all: the whole key is the anchor.
    assert _parsed(anchor) == (anchor, "")
    assert _parsed(None) == ("", "")
    assert _parsed("   ") == ("", "")
    # No absolute-path marker anywhere: the historical last-colon split is kept.
    assert _parsed("a:b") == ("a", "b")
    assert _parsed("slack_T1:reviewer:relative") == (
        "slack_T1:reviewer",
        "relative",
    )


def test_anchor_key_reproduces_the_composite_key_producers_write() -> None:
    """``key`` has to be the cache key byte-for-byte, including its edge spellings.

    Every producer writes ``f"{base_session_id}:{working_path}"`` with no test on the
    workdir, so an empty workdir is spelled ``"anchor:"`` in ``claude_sessions`` and a
    workdir keeps whatever whitespace it was created with. If ``key`` normalized
    either of those it would return a string no cache holds — a silent miss on a
    runtime that is very much still live.

    Normalization is not lost, it is just somewhere else: ``storage_workdir`` is the
    spelling ``agent_sessions.workdir`` is compared with, which is the only place the
    two spellings have to agree.
    """

    anchor = "slack_T1:reviewer"

    # The ordinary case, and the one the caches are overwhelmingly keyed by.
    assert RuntimeAnchor(anchor, "/repo").key == f"{anchor}:/repo"
    # An empty workdir still joins, because the producers join unconditionally.
    assert RuntimeAnchor(anchor, "").key == f"{anchor}:"
    assert RuntimeAnchor(anchor).key == f"{anchor}:"
    # Whitespace in a workdir is data, not formatting.
    assert RuntimeAnchor(anchor, " /repo ").key == f"{anchor}: /repo "
    # ...and it is normalized only where a stored row is compared.
    assert RuntimeAnchor(anchor, " /repo ").storage_workdir == "/repo"


def test_teardown_settles_a_session_whose_workdir_contains_a_colon(
    tmp_path: Path,
) -> None:
    """HFR-129: the split feeds the resolve, so a bad split silently skips teardown.

    The failure is not cosmetic. ``teardown_anchor_session_runs`` resolves the
    anchor/workdir pair to ``agent_sessions`` rows; a key split inside the working
    path resolves to NOTHING, and the caller — Claude eviction, cleanup, shutdown —
    reads that as "no runs to settle" and dismantles the backend anyway, leaving the
    in-flight turn ``running`` with nothing left alive to settle it.

    The workdir here is an absolute POSIX path containing a colon rather than the
    ``C:\\repo`` that motivated the fix, because ``agent_sessions`` stores workdirs
    through ``normalize_workdir`` -> ``os.path.abspath``, which on POSIX rewrites a
    literal drive path into ``<cwd>/C:\\repo`` and would make the row disagree with
    the key for a reason that has nothing to do with the split. Same defect, same
    ``rpartition`` landing inside the path; the drive-letter forms are pinned
    directly on the splitter above.
    """

    workdir = str(tmp_path / "repo:v2")
    store = SessionsStore(tmp_path / "sessions.json")
    try:
        owner_id = _seed_session(
            store,
            legacy_scope_key="slack::C_owner",
            agent_backend="claude",
            workdir=workdir,
        )

        controller = SimpleNamespace(
            sessions=SessionsFacade(store),
            config=SimpleNamespace(language="en"),
            set_agent_status=lambda *_args, **_kwargs: None,
        )
        manager = SessionTurnManager(controller)
        controller.session_turns = manager
        controller.scheduled_task_service = ScheduledTaskService(controller=controller)

        composite_key = f"{ANCHOR}:{workdir}"

        async def _exercise() -> tuple[bool, str | None]:
            owner_task = _live_turn(manager, owner_id, "claude")
            await asyncio.sleep(0)
            try:
                await teardown_anchor_session_runs(
                    controller,
                    RuntimeAnchor.parse(composite_key),
                    settled_by=SETTLED_BY_EVICTED,
                    agent_backend="claude",
                )
                owner_turn = manager.in_flight.get(owner_id)
                return owner_task.done(), getattr(owner_turn, "cancel_settled_by", None)
            finally:
                owner_task.cancel()
                await asyncio.gather(owner_task, return_exceptions=True)

        owner_done, owner_cause = asyncio.run(_exercise())

        # Pre-fix the key split inside the workdir, the anchor matched no row, and the
        # turn was still in flight here while the backend went away underneath it.
        assert owner_done is True
        assert owner_cause == SETTLED_BY_EVICTED

        # ...and the two halves the teardown resolved from are the ones the key names.
        anchor, resolved_workdir = _parsed(composite_key)
        assert (anchor, resolved_workdir) == (ANCHOR, workdir)
        assert resolve_teardown_session_ids(
            controller,
            RuntimeAnchor(anchor, resolved_workdir),
            agent_backend="claude",
        ) == [owner_id]
    finally:
        store.close()


def test_teardown_holds_can_decline_the_drain_they_normally_owe() -> None:
    """HFR-334: the drain is the default, and two audited callers opt out of it.

    ``hold_session_admission`` has always requested ``drain_on_release`` because the
    callers that motivated it — Claude cleanup/eviction, and now Codex transport
    eviction — reclaim a session that keeps serving its conversation, so a message
    refused mid-teardown has a replacement runtime to land on the moment the hold
    lets go (HFR-332).

    The audit of every teardown entry found two where that is the wrong promise:
    controller SHUTDOWN, where reopening would schedule a fresh turn inside a process
    that is exiting against backends about to be dismantled, and the Running tab's
    END, where Stop's contract is explicitly not to flush and a drain would start the
    very work the user just stopped. Both still take the HOLD — refusing admission is
    what keeps a racing message off the dying runtime — and decline only its second
    half, which they can, because the durable queue's own backstops (the next
    submission, restart recovery) are the correct owner there rather than a stopgap.

    Asserted against the real manager state the flag drives, not the call: what
    matters is whether the session is recorded as OWING a drain while held.
    """

    manager = SessionTurnManager(SimpleNamespace())
    controller = SimpleNamespace(session_turns=manager)

    with ExitStack() as holds:
        hold_session_admission(controller, "sess-drains", admission_holds=holds)
        hold_session_admission(
            controller, "sess-quiet", admission_holds=holds, drain_on_release=False
        )

        # Both are CLOSED — the protection is identical, only the reopening differs.
        assert manager.is_teardown_admission_closed("sess-drains") is True
        assert manager.is_teardown_admission_closed("sess-quiet") is True
        assert manager._teardown_drain_owed == {"sess-drains"}

    assert manager.is_teardown_admission_closed("sess-drains") is False
    assert manager.is_teardown_admission_closed("sess-quiet") is False
    # Nothing is left owed on either path; a leaked marker would drain a session at
    # some unrelated later teardown's expense.
    assert manager._teardown_drain_owed == set()


def test_a_stop_semantics_hold_vetoes_an_overlapping_evictions_drain() -> None:
    """HFR-351: a nested no-drain hold with STOP semantics suppresses the owed drain.

    When Running-tab End overlaps an eviction (or cleanup) hold for the SAME session,
    the eviction's ``drain_on_release=True`` records the drain debt — and End's
    ``drain_on_release=False`` used to record NOTHING, so the outermost release
    flushed the queue and immediately restarted the very work the user just stopped,
    on a replacement runtime, against Stop's explicit "do not flush" contract.

    THE VETO IS A THIRD STATE. ``drain_on_release=False`` alone stays NEUTRAL — the
    inner holds (``release_for_teardown``'s own, the settlement phase's) have no
    drain opinion, and reading their silence as a veto would suppress every drain
    HFR-332 exists to schedule. Only ``drain_veto=True`` — Running-tab End and
    controller shutdown, the two Stop-semantics callers from HFR-334's audit —
    forbids it, and the veto wins over the debt regardless of nesting order. Both
    markers clear together at the counter's zero, so no unrelated later teardown
    inherits either.
    """

    scheduled: list[str] = []

    def _build(monkey_target: SessionTurnManager) -> SessionTurnManager:
        monkey_target._schedule_post_teardown_drain = (  # type: ignore[method-assign]
            lambda session_id: scheduled.append(session_id)
        )
        return monkey_target

    # THE FINDING: End (veto) nested inside an eviction (owed) — no drain.
    manager = _build(SessionTurnManager(SimpleNamespace()))
    controller = SimpleNamespace(session_turns=manager)
    with ExitStack() as eviction_holds:
        hold_session_admission(controller, "sess-351", admission_holds=eviction_holds)
        with ExitStack() as end_holds:
            hold_session_admission(
                controller,
                "sess-351",
                admission_holds=end_holds,
                drain_on_release=False,
                drain_veto=True,
            )
        # End released first; the eviction's outermost release decides.
    assert scheduled == [], "End's Stop semantics must veto the eviction's owed drain"
    assert manager._teardown_drain_owed == set()
    assert manager._teardown_drain_vetoed == set()

    # NESTING ORDER DOES NOT MATTER: eviction inside End vetoes the same way.
    scheduled.clear()
    manager = _build(SessionTurnManager(SimpleNamespace()))
    controller = SimpleNamespace(session_turns=manager)
    with ExitStack() as end_holds:
        hold_session_admission(
            controller,
            "sess-351b",
            admission_holds=end_holds,
            drain_on_release=False,
            drain_veto=True,
        )
        with ExitStack() as eviction_holds:
            hold_session_admission(
                controller, "sess-351b", admission_holds=eviction_holds
            )
    assert scheduled == []
    assert manager._teardown_drain_vetoed == set()

    # THE COMPANION (HFR-332 stays true): an eviction alone still drains, and a
    # NEUTRAL no-drain hold (no veto) does not suppress it.
    scheduled.clear()
    manager = _build(SessionTurnManager(SimpleNamespace()))
    controller = SimpleNamespace(session_turns=manager)
    with ExitStack() as eviction_holds:
        hold_session_admission(controller, "sess-351c", admission_holds=eviction_holds)
        with manager.teardown_admission("sess-351c"):
            pass  # the neutral inner hold every teardown chain takes
    assert scheduled == ["sess-351c"], "a neutral inner hold must not veto HFR-332"
    # The opted-out session scheduled no drain at all, so no turn was started against
    # a backend that is going away.
    assert "sess-quiet" not in manager._teardown_drain_tasks


def test_controller_shutdown_settlement_holds_admission_without_draining() -> None:
    """HFR-334: shutdown closes the same window, and refuses the same drain.

    ``_settle_inflight_turns_for_shutdown`` tears down every live turn and then hands
    off to ``cleanup_sync``, which dismantles the backends. Each settlement pops its
    turn's ``in_flight`` entry inside the awaited cancel, so without a hold the
    sessions in between read IDLE with live clients still registered.

    ONE STACK FOR THE WHOLE LOOP is the part worth pinning. Each session's hold is
    taken lazily, immediately before its own settlement — which is when its window
    opens, since until then its live turn makes ``submit`` see it as busy anyway —
    but NONE of them is released until the whole loop is done. A per-session ``with``
    would reopen each session at the top of the next iteration, while the process is
    still exiting and its backend is still to be torn down.
    """

    from core.controller import Controller
    from core.run_settlement import SETTLED_BY_RESTARTED

    manager = SessionTurnManager(SimpleNamespace())
    settled: list[tuple[str, str, bool, bool]] = []

    async def _teardown_session_runs(controller, session_id, *, settled_by, **_kwargs):
        # Observed from INSIDE the settlement: every session is held for the whole
        # loop, not just its own turn.
        settled.append(
            (
                session_id,
                settled_by,
                manager.is_teardown_admission_closed("sess-a"),
                manager.is_teardown_admission_closed("sess-b"),
            )
        )
        return 1

    # The shutdown path enumerates the busy set off the MANAGER, and resolves the
    # hold off the controller — both are this one real manager.
    manager.busy_session_ids = lambda: {"sess-a", "sess-b"}
    fake_controller = SimpleNamespace(session_turns=manager)

    with patch("core.controller.teardown_session_runs", _teardown_session_runs):
        asyncio.run(Controller._settle_inflight_turns_for_shutdown(fake_controller))

    assert [row[0] for row in settled] == ["sess-a", "sess-b"]
    assert {row[1] for row in settled} == {SETTLED_BY_RESTARTED}
    # Each session is held for its OWN settlement...
    assert settled[0][2] is True
    assert settled[1][3] is True
    # ...and the hold taken in the first iteration is STILL held during the second.
    # This is what the shared stack buys: sess-a does not reopen while the shutdown
    # is still working through the rest of the list.
    assert settled[1][2] is True

    # The COUNTED holds are released on the way out, on every path, as they always
    # were — a leaked counter would be a wedged session in any other caller.
    assert manager._teardown_admission == {}
    # ...but admission does NOT reopen, because HFR-339 supersedes what this test
    # used to assert here. Releasing the stack once looked like the end of the
    # window; it is the beginning of the backend teardown, which ``cleanup_sync``
    # performs from another thread while this loop keeps running. The process-wide
    # close now spans it and never lifts. Strictly stronger than the old pin: every
    # session the stack held is still closed, and so is every session it did not.
    assert manager.is_teardown_admission_closed("sess-a") is True
    assert manager.is_teardown_admission_closed("sess-b") is True
    # ...and NOTHING was drained: a shutdown must not start fresh turns against
    # backends ``cleanup_sync`` is about to tear down.
    assert manager._teardown_drain_owed == set()
    assert manager._teardown_drain_tasks == {}


def test_shutdown_admission_stays_closed_after_settlement_returns(
    tmp_path: Path, monkeypatch
) -> None:
    """HFR-339: the shutdown hold must outlive the settlement that took it.

    HFR-334 closed admission for the DURATION of
    ``_settle_inflight_turns_for_shutdown`` and documented the tail as an accepted
    residual "with no awaits in it". That description is false. The helper is
    submitted to the STILL-RUNNING event loop by ``cleanup_sync``, which is a
    synchronous method on another thread; when the helper returns, its ``ExitStack``
    releases and the cleanup thread goes on to BLOCK — once per stop — waiting for
    the watch service, the runtime command watcher, the Model Hub gateway and the
    Codex runtime. The loop is running for the whole of those waits, so it can admit
    and dispatch a brand-new turn onto a backend that is being dismantled. The
    window is not a tail without awaits; it is the entire backend teardown.

    So the shutdown answer is a PROCESS-WIDE, NEVER-CLEARED flag rather than a set of
    per-session holds: the thing that changed is not "these sessions are being torn
    down" but "this process is exiting", and nothing that arrives afterwards should
    ever dispatch. Set on the loop thread at the very top of the helper — before the
    busy set is even enumerated, so no window precedes it and no failure to enumerate
    can skip it.

    Pinned on all three doors the flag has to close, because closing one and leaving
    the others is the HFR-336 lesson: ``is_teardown_admission_closed`` for every
    session id (not just the busy ones), ``submit`` still ENQUEUEING durably rather
    than dispatching, and the scheduler's own single-row door.
    """

    from core.controller import Controller
    from core.scheduled_tasks import ScheduledTaskStore, TaskExecutionStore
    from storage.importer import ensure_sqlite_state

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    manager = SessionTurnManager()
    controller = SimpleNamespace(session_turns=manager, platform_settings_managers={})
    manager.controller = controller
    manager.busy_session_ids = lambda: {"sess-busy"}

    async def _teardown_session_runs(_controller, _session_id, *, settled_by, **_kwargs):
        return 1

    with patch("core.controller.teardown_session_runs", _teardown_session_runs):
        asyncio.run(Controller._settle_inflight_turns_for_shutdown(controller))

    # Pre-fix the stack had unwound by now and every one of these read OPEN.
    assert manager.is_admission_closed_for_shutdown() is True
    assert manager.is_teardown_admission_closed("sess-busy") is True
    # ...including a session that was never busy. The flag is about the dying
    # process, not about who happened to be mid-turn when it started dying.
    assert manager.is_teardown_admission_closed("sess-idle") is True

    dispatched: list[str] = []
    enqueued: list[str] = []

    async def _never_dispatch(_session_id, _context, text, **_kwargs) -> None:
        dispatched.append(text)

    manager._run = _never_dispatch  # type: ignore[assignment]
    context = MessageContext(user_id="U", channel_id="sess-idle", platform="avibe")
    context.platform_specific = {
        "agent_session_id": "sess-idle",
        "agent_session_target": {"agent_backend": "claude"},
    }

    def _enqueue() -> bool:
        enqueued.append("racer")
        return True

    result = asyncio.run(manager.submit("sess-idle", context, "racer", enqueue=_enqueue))

    # The refusal is a DURABLE QUEUE, not a rejection: ``submit`` folds the closed
    # admission into ``busy``, so the message survives to the next start.
    assert result.route == "enqueued"
    assert result.queue_persisted is True
    assert enqueued == ["racer"]
    assert dispatched == [], (
        "a message admitted after the shutdown settlement returned dispatched onto a "
        "backend cleanup_sync was about to tear down"
    )

    # THE SCHEDULER'S DOOR (HFR-336) has to be told the same truth, and cannot learn
    # it from the enumerating half: ``teardown_held_session_ids`` reports the counted
    # per-session holds, and "every session there will ever be" is not a set it can
    # return. So the drain asks the flag first.
    request_store = TaskExecutionStore(tmp_path / "reqs")
    queued = request_store.enqueue_hook_send(
        session_key="slack::channel::C123", prompt="queued while the process exits"
    )
    service = ScheduledTaskService(
        controller=controller,
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    assert manager.teardown_held_session_ids() == set()
    assert service._teardown_holds_request(queued) is True

