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
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_sessions import SessionsStore
from core.run_settlement import SETTLED_BY_EVICTED
from core.scheduled_tasks import ScheduledTaskService
from core.session_teardown import (
    resolve_teardown_session_ids,
    split_composite_session_key,
    teardown_composite_session_runs,
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
            session_anchor=ANCHOR,
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
            session_anchor=ANCHOR,
            workdir=str(tmp_path / "owner"),
            agent_backend="claude",
        ) == [owner_id]

        async def _exercise() -> tuple[bool, bool, bool, str | None]:
            owner_task = _live_turn(manager, owner_id, "claude")
            foreign_backend_task = _live_turn(manager, foreign_backend_id, "codex")
            foreign_workdir_task = _live_turn(manager, foreign_workdir_id, "claude")
            await asyncio.sleep(0)
            try:
                await teardown_composite_session_runs(
                    controller,
                    composite_key,
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
    """HFR-323: same anchor, same backend, same workdir is not the same runtime.

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
                    session_anchor=ANCHOR,
                    workdir=workdir,
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

        async def _exercise() -> tuple[bool, bool]:
            first_task = _live_turn(manager, first_id, "claude")
            second_task = _live_turn(manager, second_id, "claude")
            await asyncio.sleep(0)
            try:
                await teardown_composite_session_runs(
                    controller,
                    composite_key,
                    settled_by=SETTLED_BY_EVICTED,
                    agent_backend="claude",
                )
                return first_task.done(), second_task.done()
            finally:
                for task in (first_task, second_task):
                    task.cancel()
                await asyncio.gather(first_task, second_task, return_exceptions=True)

        first_done, second_done = asyncio.run(_exercise())

        # Neither live turn was interrupted, and neither carries a settlement cause.
        assert first_done is False
        assert second_done is False
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
            session_anchor=ANCHOR,
            workdir=str(tmp_path / "solo"),
            agent_backend="claude",
        ) == [only_id]

        async def _exercise_single() -> tuple[bool, str | None]:
            task = _live_turn(manager, only_id, "claude")
            await asyncio.sleep(0)
            try:
                await teardown_composite_session_runs(
                    controller,
                    f"{ANCHOR}:{tmp_path / 'solo'}",
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
            session_anchor=ANCHOR,
            workdir=str(tmp_path / "owner"),
            agent_backend="claude",
        ) == [legacy_id]
        # And a caller that names no backend at all is unchanged.
        assert resolve_teardown_session_ids(
            controller, session_anchor=ANCHOR, workdir=str(tmp_path / "owner")
        ) == [legacy_id]
    finally:
        store.close()


def test_composite_key_split_preserves_windows_drive_paths() -> None:
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
    """

    anchor = "slack_171717.123"

    # Windows drive paths, in both slash conventions.
    assert split_composite_session_key(f"{anchor}:C:\\repo") == (anchor, "C:\\repo")
    assert split_composite_session_key(f"{anchor}:C:/repo") == (anchor, "C:/repo")
    # A UNC / root-relative Windows path.
    assert split_composite_session_key(f"{anchor}:\\\\host\\share") == (
        anchor,
        "\\\\host\\share",
    )
    # A POSIX directory that legally contains a colon.
    assert split_composite_session_key("a:/tmp/x:y") == ("a", "/tmp/x:y")
    # A subagent base — the anchor itself carries a colon, and it must survive.
    assert split_composite_session_key("slack_T1:reviewer:/work") == (
        "slack_T1:reviewer",
        "/work",
    )
    assert split_composite_session_key(f"slack_T1:reviewer:C:\\work") == (
        "slack_T1:reviewer",
        "C:\\work",
    )
    # No separator at all: the whole key is the anchor.
    assert split_composite_session_key(anchor) == (anchor, "")
    assert split_composite_session_key(None) == ("", "")
    assert split_composite_session_key("   ") == ("", "")
    # No absolute-path marker anywhere: the historical last-colon split is kept.
    assert split_composite_session_key("a:b") == ("a", "b")
    assert split_composite_session_key("slack_T1:reviewer:relative") == (
        "slack_T1:reviewer",
        "relative",
    )


def test_teardown_settles_a_session_whose_workdir_contains_a_colon(
    tmp_path: Path,
) -> None:
    """HFR-129: the split feeds the resolve, so a bad split silently skips teardown.

    The failure is not cosmetic. ``teardown_composite_session_runs`` resolves the
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
                await teardown_composite_session_runs(
                    controller,
                    composite_key,
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
        anchor, resolved_workdir = split_composite_session_key(composite_key)
        assert (anchor, resolved_workdir) == (ANCHOR, workdir)
        assert resolve_teardown_session_ids(
            controller,
            session_anchor=anchor,
            workdir=resolved_workdir,
            agent_backend="claude",
        ) == [owner_id]
    finally:
        store.close()
