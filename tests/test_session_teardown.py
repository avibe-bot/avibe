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
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_sessions import SessionsStore
from core.run_settlement import SETTLED_BY_EVICTED
from core.scheduled_tasks import ScheduledTaskService
from core.session_teardown import (
    resolve_teardown_session_ids,
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
