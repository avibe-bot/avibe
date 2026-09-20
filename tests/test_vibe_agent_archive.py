from __future__ import annotations

import json

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError

from core.scheduled_tasks import TaskExecutionStore
from core.vibe_agents import (
    AGENT_ARCHIVE_METADATA_KEY,
    AgentArchivedEditError,
    AgentArchiveError,
    AgentNameValidationError,
    AgentReferenceRewriteError,
    AgentUnavailableError,
    VibeAgentStore,
    normalize_agent_name,
)
from storage.background import (
    DefinitionWriteExpectation,
    SQLiteBackgroundTaskStore,
    definition_state_unchanged,
)
from storage import message_deliveries as delivery_store
from storage.models import (
    agent_runs,
    agent_sessions,
    agents,
    message_deliveries,
    messages,
    run_definitions,
    scope_settings,
    scopes,
    state_meta,
)
from storage.sessions_service import SQLiteSessionsService


NOW = "2026-07-31T14:00:00+00:00"
SCOPE_ID = "avibe::project::proj_archive"


@pytest.fixture(autouse=True)
def _prepare_behavior_state(tmp_path, sqlite_db_factory, request):
    # Archive transactions and lock races need the current schema, not a fresh
    # migration chain for every private database.
    if not request.node.get_closest_marker("no_sqlite_template"):
        sqlite_db_factory(tmp_path / "state" / "vibe.sqlite")


def _create_archive_fallback(store: VibeAgentStore) -> None:
    store.create(name="archive-fallback", backend="codex")


def _race_archive_at_agent_read(
    primary: VibeAgentStore,
    competitor: VibeAgentStore,
) -> dict[str, object]:
    state: dict[str, object] = {"fired": 0, "refused": [], "committed": 0}

    @event.listens_for(competitor.engine, "checkout")
    def _no_wait(dbapi_connection, *_args) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout = 0")
        cursor.close()

    @event.listens_for(primary.engine, "after_cursor_execute")
    def _archive_on_read(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.split())
        if state["fired"] or "FROM agents WHERE agents.normalized_name" not in normalized:
            return
        state["fired"] = 1
        try:
            competitor.archive("pm")
        except OperationalError as exc:
            state["refused"].append(str(exc))
        else:
            state["committed"] = 1

    return state


def _race_archive_at_identity_read(primary_engine, competitor: VibeAgentStore) -> dict[str, object]:
    state: dict[str, object] = {"fired": 0, "refused": [], "committed": 0}

    @event.listens_for(competitor.engine, "checkout")
    def _no_wait(dbapi_connection, *_args) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout = 0")
        cursor.close()

    @event.listens_for(primary_engine, "after_cursor_execute")
    def _archive_on_identity_read(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        normalized = " ".join(statement.split())
        if state["fired"] or "FROM agents" not in normalized or "agents.id =" not in normalized:
            return
        state["fired"] = 1
        try:
            competitor.archive("pm")
        except OperationalError as exc:
            state["refused"].append(str(exc))
        else:
            state["committed"] = 1

    return state


def _seed_references(store: VibeAgentStore, agent_name: str) -> None:
    with store.engine.begin() as conn:
        conn.execute(
            scopes.insert().values(
                id=SCOPE_ID,
                platform="avibe",
                scope_type="project",
                native_id="proj_archive",
                parent_scope_id=None,
                display_name="Archive test",
                native_type="project",
                is_private=0,
                supports_threads=1,
                metadata_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                updated_at=NOW,
            )
        )
        conn.execute(
            scope_settings.insert().values(
                scope_id=SCOPE_ID,
                enabled=1,
                role=None,
                workdir="/tmp/archive-test",
                agent_name=agent_name,
                agent_backend="claude",
                agent_variant="claude",
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json=json.dumps(
                    {"routing": {"agent_name": agent_name, "agent": agent_name}}
                ),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_archive",
                scope_id=SCOPE_ID,
                agent_id=None,
                agent_name=agent_name,
                agent_backend="claude",
                agent_variant="claude",
                model=None,
                reasoning_effort=None,
                session_anchor="ses_archive",
                workdir="/tmp/archive-test",
                native_session_id="",
                title="Archive test",
                status="active",
                visibility="foreground",
                pinned=0,
                agent_status="idle",
                metadata_json="{}",
                created_at=NOW,
                updated_at=NOW,
                last_active_at=NOW,
            )
        )
        for definition_id, deleted_at in (("task_live", None), ("watch_deleted", NOW)):
            conn.execute(
                run_definitions.insert().values(
                    id=definition_id,
                    definition_type="scheduled" if definition_id == "task_live" else "watch",
                    name=definition_id,
                    agent_name=agent_name,
                    enabled=0,
                    deleted_at=deleted_at,
                    created_at=NOW,
                    updated_at=NOW,
                    metadata_json=json.dumps(
                        {"session_settings_snapshot": {"agent_name": agent_name}}
                    ),
                )
            )
        for status in ("pending", "queued", "processing", "running", "succeeded", "failed", "canceled"):
            conn.execute(
                agent_runs.insert().values(
                    id=f"run_{status}",
                    run_type="agent",
                    status=status,
                    agent_name=agent_name,
                    created_at=NOW,
                    updated_at=NOW,
                    metadata_json="{}",
                )
            )
        delivery_store.insert_delivery(
            conn,
            delivery_id="msg_queued_archive",
            session_id="ses_archive",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=SCOPE_ID,
                session_id="ses_archive",
                platform="avibe",
                author="harness",
                source="harness",
                message_type="harness",
                text="continue",
                metadata={
                    "scheduled_provenance": {
                        "platform_specific": {
                            "vibe_agent_name": agent_name,
                            "scheduled_target_agent_name": agent_name,
                            "agent_session_target": {"agent_name": agent_name},
                        }
                    }
                },
            ),
            dispatch_text="continue",
            now=NOW,
        )


def _seed_dispatchable_delivery_states(store: VibeAgentStore, agent_name: str) -> dict[str, str]:
    states = (
        "reserved",
        "queued",
        "claimed",
        "pending_steer",
        "steering",
        "interrupt_waiting",
        "reconciling_steer",
    )
    seeded: dict[str, str] = {}

    def snapshot(session_id: str) -> dict[str, object]:
        return delivery_store.message_snapshot(
            scope_id=SCOPE_ID,
            session_id=session_id,
            platform="avibe",
            author="harness",
            source="harness",
            message_type="harness",
            text="continue",
            metadata={
                "scheduled_provenance": {
                    "platform_specific": {
                        "vibe_agent_name": agent_name,
                        "scheduled_target_agent_name": agent_name,
                        "agent_session_target": {"agent_name": agent_name},
                    }
                }
            },
        )

    with store.engine.begin() as conn:
        for state in states:
            session_id = f"ses_archive_{state}"
            delivery_id = f"del_archive_{state}"
            seeded[state] = delivery_id
            conn.execute(
                agent_sessions.insert().values(
                    id=session_id,
                    scope_id=SCOPE_ID,
                    agent_id=None,
                    agent_name=None,
                    agent_backend="claude",
                    agent_variant="claude",
                    model=None,
                    reasoning_effort=None,
                    session_anchor=session_id,
                    workdir="/tmp/archive-test",
                    native_session_id="",
                    title=None,
                    status="active",
                    visibility="background",
                    pinned=0,
                    agent_status="idle",
                    metadata_json="{}",
                    created_at=NOW,
                    updated_at=NOW,
                    last_active_at=NOW,
                )
            )
            target = delivery_store.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id=session_id,
                priority="p1",
                state="queued" if state == "queued" else "reserved",
                snapshot=snapshot(session_id),
                dispatch_text="continue",
                now=NOW,
            )

            if state == "claimed":
                delivery_store.claim_start_batch(
                    conn,
                    turn_id=f"turn_archive_{state}",
                    session_id=session_id,
                    backend="claude",
                    deliveries=[target],
                    dispatch_text="continue",
                )
            elif state in {"pending_steer", "steering", "reconciling_steer"}:
                anchor_id = f"anchor_archive_{state}"
                anchor = delivery_store.insert_delivery(
                    conn,
                    delivery_id=anchor_id,
                    session_id=session_id,
                    priority="p3",
                    state="reserved",
                    snapshot=delivery_store.message_snapshot(
                        scope_id=SCOPE_ID,
                        session_id=session_id,
                        platform="avibe",
                        author="user",
                        source="user",
                        message_type="user",
                        text="anchor",
                    ),
                    dispatch_text="anchor",
                    now=NOW,
                )
                turn_id = f"turn_archive_{state}"
                claimed = delivery_store.claim_start_batch(
                    conn,
                    turn_id=turn_id,
                    session_id=session_id,
                    backend="claude",
                    deliveries=[anchor],
                    dispatch_text="anchor",
                )
                attempt_id = f"attempt_archive_{state}"
                if state == "pending_steer":
                    delivery_store.open_pending_steer_batch(
                        conn,
                        deliveries=[target],
                        turn_id=turn_id,
                        attempt_id=attempt_id,
                    )
                else:
                    turn = delivery_store.bind_native_start(
                        conn,
                        turn_id,
                        expected_version=int(claimed["turn"]["version"]),
                        runtime_key=f"runtime-{state}",
                        runtime_turn_id=f"runtime-turn-{state}",
                        native_turn_id=f"native-{state}",
                    )
                    assert turn is not None
                    steering = delivery_store.open_steer_attempt(
                        conn,
                        delivery_id,
                        expected_version=int(target["version"]),
                        turn_id=turn_id,
                        attempt_id=attempt_id,
                        expected_native_turn_id=f"native-{state}",
                    )
                    assert steering is not None
                    if state == "reconciling_steer":
                        assert delivery_store.mark_attempt_unknown(
                            conn,
                            delivery_id,
                            expected_version=int(steering["version"]),
                            receipt={"kind": "lost_receipt"},
                        ) is not None
            elif state == "interrupt_waiting":
                turn_id = f"turn_archive_{state}"
                delivery_store.insert_turn(
                    conn,
                    turn_id=turn_id,
                    session_id=session_id,
                    initial_delivery_id=delivery_id,
                    state="waiting",
                    backend="claude",
                    now=NOW,
                )
                assert delivery_store.cas_delivery(
                    conn,
                    delivery_id,
                    expected_version=int(target["version"]),
                    expected_states=("reserved",),
                    values={
                        "state": "interrupt_waiting",
                        "turn_id": turn_id,
                        "turn_role": "initial",
                        "turn_position": 0,
                    },
                ) is not None

        observed = set(
            conn.execute(
                select(message_deliveries.c.state).where(
                    message_deliveries.c.id.in_(tuple(seeded.values()))
                )
            ).scalars()
        )
        assert observed == set(states)
    return seeded


def test_archive_atomically_moves_live_references_and_hides_agent(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        store.create(name="claude-fallback", backend="claude")
        original = store.create(
            name="pm",
            backend="claude",
            description="Project manager",
            system_prompt="Keep the project moving.",
        )
        store.set_default_agent_name("pm")
        _seed_references(store, "pm")

        result = store.archive("pm")

        assert result is not None
        assert result.original_name == "pm"
        assert result.archived_name.startswith("_pm-")
        assert len(result.archived_name) == len("_pm-") + 4
        assert result.references == {"scopes": 1, "sessions": 1, "definitions": 1}
        assert result.default_agent_name == "claude-fallback"
        assert store.get_default_agent_name() == "claude-fallback"
        assert store.get("pm") is None
        assert all(agent.id != original.id for agent in store.list_agents(include_disabled=True))

        archived = store.require(result.archived_name)
        assert archived.id == original.id
        assert archived.enabled is False
        assert archived.archived_at is not None
        assert archived.to_dict()["display_name"] == "pm"
        assert store.require_reference(result.archived_name).id == original.id
        with pytest.raises(AgentUnavailableError, match="disabled"):
            store.require_enabled(result.archived_name)
        with pytest.raises(AgentArchivedEditError) as edit_error:
            store.set_enabled(result.archived_name, True)
        assert edit_error.value.code == "agent_archived_read_only"
        assert edit_error.value.agent_name == result.archived_name
        with pytest.raises(AgentArchivedEditError):
            store.rename(result.archived_name, "restored-pm")

        listed_with_archives = store.list_agents(include_disabled=True, include_archived=True)
        assert any(agent.id == original.id for agent in listed_with_archives)

        with store.engine.connect() as conn:
            scope = conn.execute(select(scope_settings)).mappings().one()
            session = conn.execute(select(agent_sessions)).mappings().one()
            definitions = conn.execute(select(run_definitions).order_by(run_definitions.c.id)).mappings().all()
            runs = conn.execute(select(agent_runs.c.status, agent_runs.c.agent_name)).mappings().all()
            queued_snapshot = json.loads(
                conn.execute(
                    select(message_deliveries.c.snapshot_json).where(
                        message_deliveries.c.id == "msg_queued_archive"
                    )
                ).scalar_one()
            )
        assert scope["agent_name"] == result.archived_name
        assert json.loads(scope["settings_json"])["routing"] == {
            "agent_name": result.archived_name,
            "agent": result.archived_name,
        }
        assert session["agent_name"] == result.archived_name
        definitions_by_id = {row["id"]: row for row in definitions}
        assert definitions_by_id["task_live"]["agent_name"] == result.archived_name
        assert definitions_by_id["watch_deleted"]["agent_name"] == "pm"
        assert json.loads(definitions_by_id["task_live"]["metadata_json"])[
            "session_settings_snapshot"
        ]["agent_name"] == result.archived_name
        assert json.loads(definitions_by_id["watch_deleted"]["metadata_json"])[
            "session_settings_snapshot"
        ]["agent_name"] == "pm"
        assert {
            row["agent_name"]
            for row in runs
            if row["status"] in {"pending", "queued", "processing", "running"}
        } == {result.archived_name}
        assert {
            row["agent_name"]
            for row in runs
            if row["status"] in {"succeeded", "failed", "canceled"}
        } == {"pm"}
        queued_spec = json.loads(queued_snapshot["metadata_json"])["scheduled_provenance"][
            "platform_specific"
        ]
        assert queued_spec["vibe_agent_name"] == "pm"
        assert queued_spec["scheduled_target_agent_name"] == "pm"
        assert queued_spec["agent_session_target"]["agent_name"] == "pm"

        replacement = store.create(name="pm", backend="claude")
        assert replacement.id != original.id
    finally:
        store.close()


def test_archive_never_rewrites_immutable_delivery_snapshots(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        store.create(name="claude-fallback", backend="claude")
        store.create(name="pm", backend="claude")
        _seed_references(store, "pm")
        seeded = _seed_dispatchable_delivery_states(store, "pm")

        with store.engine.connect() as conn:
            before = {
                str(row["id"]): str(row["snapshot_json"])
                for row in conn.execute(
                    select(
                        message_deliveries.c.id,
                        message_deliveries.c.snapshot_json,
                    ).where(message_deliveries.c.id.in_(tuple(seeded.values())))
                ).mappings()
            }

        result = store.archive("pm")

        assert result is not None
        with store.engine.connect() as conn:
            rows = conn.execute(
                select(
                    message_deliveries.c.id,
                    message_deliveries.c.state,
                    message_deliveries.c.snapshot_json,
                ).where(message_deliveries.c.id.in_(tuple(seeded.values())))
            ).mappings()
            after = {
                str(row["id"]): str(row["snapshot_json"])
                for row in rows
            }
        assert result is not None
        assert after == before
    finally:
        store.close()


def test_update_holds_the_write_lock_before_checking_archived_state(tmp_path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    primary = VibeAgentStore(db_path)
    competitor = VibeAgentStore(db_path)
    try:
        primary.create(name="pm", backend="claude", enabled=False)
        race = _race_archive_at_agent_read(primary, competitor)

        updated = primary.set_enabled("pm", True)

        assert updated.enabled is True
        assert race["fired"] == 1
        assert race["committed"] == 0
        assert len(race["refused"]) == 1
        assert primary.require("pm").archived_at is None
    finally:
        competitor.close()
        primary.close()


def test_default_selection_holds_the_write_lock_before_agent_validation(tmp_path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    primary = VibeAgentStore(db_path)
    competitor = VibeAgentStore(db_path)
    try:
        primary.create(name="fallback", backend="claude")
        primary.create(name="pm", backend="claude")
        primary.set_default_agent_name("fallback")
        race = _race_archive_at_agent_read(primary, competitor)

        primary.set_default_agent_name("pm")

        assert race["fired"] == 1
        assert race["committed"] == 0
        assert len(race["refused"]) == 1
        assert primary.get_default_agent_name() == "pm"
        assert primary.require("pm").archived_at is None
    finally:
        competitor.close()
        primary.close()


@pytest.mark.parametrize("write_kind", ["scheduled", "watch", "run"])
def test_direct_definition_and_run_assignments_validate_under_the_write_lock(
    tmp_path, write_kind: str
) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = VibeAgentStore(db_path)
    competitor = VibeAgentStore(db_path)
    background = SQLiteBackgroundTaskStore(db_path)
    try:
        agent = agent_store.create(name="pm", backend="claude")
        race = _race_archive_at_identity_read(background.engine, competitor)
        common = {
            "id": f"{write_kind}_direct",
            "agent_name": agent.name,
            "created_at": NOW,
            "updated_at": NOW,
        }

        if write_kind == "scheduled":
            background.upsert_scheduled_task(
                {
                    **common,
                    "prompt": "continue",
                    "schedule_type": "at",
                    "run_at": NOW,
                    "timezone": "UTC",
                },
                expected_enabled_agent_id=agent.id,
            )
        elif write_kind == "watch":
            background.upsert_watch(
                {
                    **common,
                    "command": ["true"],
                    "mode": "once",
                },
                expected_enabled_agent_id=agent.id,
            )
        else:
            background.enqueue_run(
                {
                    **common,
                    "agent_id": agent.id,
                    "request_type": "agent_run",
                    "status": "queued",
                    "message": "continue",
                },
                expected_enabled_agent_id=agent.id,
            )

        assert race["fired"] == 1
        assert race["committed"] == 0
        assert len(race["refused"]) == 1
        assert agent_store.require_enabled(agent.name).id == agent.id
    finally:
        background.close()
        competitor.close()
        agent_store.close()


def test_direct_session_assignment_validates_under_the_write_lock(tmp_path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = VibeAgentStore(db_path)
    competitor = VibeAgentStore(db_path)
    sessions = SQLiteSessionsService(db_path)
    try:
        agent = agent_store.create(name="pm", backend="claude")
        race = _race_archive_at_identity_read(sessions.engine, competitor)

        session_id = sessions.reserve_standalone_agent_session(
            agent_backend=agent.backend,
            session_anchor="direct-agent-run",
            agent_id=agent.id,
            agent_name=agent.name,
            require_enabled_agent=True,
        )

        assert session_id
        assert race["fired"] == 1
        assert race["committed"] == 0
        assert len(race["refused"]) == 1
        assert agent_store.require_enabled(agent.name).id == agent.id
    finally:
        sessions.close()
        competitor.close()
        agent_store.close()


def test_direct_write_rejects_a_new_agent_that_reuses_the_selected_name(tmp_path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = VibeAgentStore(db_path)
    background = SQLiteBackgroundTaskStore(db_path)
    try:
        _create_archive_fallback(agent_store)
        original = agent_store.create(name="pm", backend="claude")
        archived = agent_store.archive(original.name)
        assert archived is not None
        replacement = agent_store.create(name="pm", backend="claude")

        with pytest.raises(ValueError, match="replaced before the write"):
            background.enqueue_run(
                {
                    "id": "run_stale_identity",
                    "agent_name": replacement.name,
                    "agent_id": original.id,
                    "request_type": "agent_run",
                    "status": "queued",
                    "message": "continue",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                expected_enabled_agent_id=original.id,
            )
        assert background.get_run("run_stale_identity") is None
    finally:
        background.close()
        agent_store.close()


@pytest.mark.parametrize("write_kind", ["scheduled", "watch", "run", "session"])
def test_existing_reference_writes_canonicalize_by_stable_agent_id(
    tmp_path, write_kind: str
) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = VibeAgentStore(db_path)
    background = SQLiteBackgroundTaskStore(db_path)
    sessions = SQLiteSessionsService(db_path)
    try:
        _create_archive_fallback(agent_store)
        original = agent_store.create(name="pm", backend="claude")
        archived = agent_store.archive(original.name)
        assert archived is not None
        replacement = agent_store.create(name="pm", backend="codex")

        if write_kind == "scheduled":
            background.upsert_scheduled_task(
                {
                    "id": "scheduled_reference",
                    "agent_name": replacement.name,
                    "prompt": "continue",
                    "schedule_type": "at",
                    "run_at": NOW,
                    "timezone": "UTC",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                expected_reference_agent_id=original.id,
            )
            with background.engine.connect() as conn:
                stored_name = conn.execute(
                    select(run_definitions.c.agent_name).where(
                        run_definitions.c.id == "scheduled_reference"
                    )
                ).scalar_one()
            assert stored_name == archived.archived_name
        elif write_kind == "watch":
            background.upsert_watch(
                {
                    "id": "watch_reference",
                    "agent_name": replacement.name,
                    "command": ["true"],
                    "mode": "once",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                expected_reference_agent_id=original.id,
            )
            with background.engine.connect() as conn:
                stored_name = conn.execute(
                    select(run_definitions.c.agent_name).where(
                        run_definitions.c.id == "watch_reference"
                    )
                ).scalar_one()
            assert stored_name == archived.archived_name
        elif write_kind == "run":
            background.enqueue_run(
                {
                    "id": "run_reference",
                    "agent_name": replacement.name,
                    "request_type": "agent_run",
                    "status": "queued",
                    "message": "continue",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
                expected_reference_agent_id=original.id,
            )
            stored = background.get_run("run_reference")
            assert stored is not None
            assert stored["agent_id"] == original.id
            assert stored["agent_name"] == archived.archived_name
        else:
            session_id = sessions.reserve_standalone_agent_session(
                agent_backend=replacement.backend,
                session_anchor="reference",
                agent_id=original.id,
                agent_name=replacement.name,
                expected_reference_agent_id=original.id,
            )
            stored = sessions.get_agent_session_by_id(session_id)
            assert stored is not None
            assert stored["agent_id"] == original.id
            assert stored["agent_name"] == archived.archived_name
            assert stored["agent_backend"] == original.backend
    finally:
        sessions.close()
        background.close()
        agent_store.close()


@pytest.mark.parametrize("write_kind", ["scheduled", "watch", "run", "session"])
def test_existing_reference_writes_reject_an_ordinary_disabled_agent(
    tmp_path, write_kind: str
) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = VibeAgentStore(db_path)
    background = SQLiteBackgroundTaskStore(db_path)
    sessions = SQLiteSessionsService(db_path)
    try:
        disabled = agent_store.create(name="paused", backend="claude", enabled=False)

        with pytest.raises(ValueError, match="agent reference 'paused' is disabled"):
            if write_kind == "scheduled":
                background.upsert_scheduled_task(
                    {
                        "id": "scheduled_disabled_reference",
                        "agent_name": disabled.name,
                        "prompt": "continue",
                        "schedule_type": "at",
                        "run_at": NOW,
                        "timezone": "UTC",
                    },
                    expected_reference_agent_id=disabled.id,
                )
            elif write_kind == "watch":
                background.upsert_watch(
                    {
                        "id": "watch_disabled_reference",
                        "agent_name": disabled.name,
                        "command": ["true"],
                        "mode": "once",
                    },
                    expected_reference_agent_id=disabled.id,
                )
            elif write_kind == "run":
                background.enqueue_run(
                    {
                        "id": "run_disabled_reference",
                        "agent_name": disabled.name,
                        "request_type": "agent_run",
                        "status": "queued",
                        "message": "continue",
                    },
                    expected_reference_agent_id=disabled.id,
                )
            else:
                sessions.reserve_standalone_agent_session(
                    agent_backend=disabled.backend,
                    session_anchor="disabled-reference",
                    agent_id=disabled.id,
                    agent_name=disabled.name,
                    expected_reference_agent_id=disabled.id,
                )
    finally:
        sessions.close()
        background.close()
        agent_store.close()


def test_claim_refresh_normalizes_a_legacy_run_name_before_pinning_identity(tmp_path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = VibeAgentStore(db_path)
    background = SQLiteBackgroundTaskStore(db_path)
    try:
        agent = agent_store.create(name="project-manager", backend="claude")
        background.enqueue_run(
            {
                "id": "run_legacy_spelling",
                "agent_name": "PROJECT-MANAGER",
                "request_type": "scheduled",
                "status": "queued",
                "message": "continue",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )

        pinned = background.refresh_run_agent_reference("run_legacy_spelling")

        assert pinned == {"agent_id": agent.id, "agent_name": agent.name}
        stored = background.get_run("run_legacy_spelling")
        assert stored is not None
        assert stored["agent_id"] == agent.id
        assert stored["agent_name"] == agent.name
    finally:
        background.close()
        agent_store.close()


def test_existing_definition_enqueue_pins_the_post_archive_agent_identity(tmp_path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = VibeAgentStore(db_path)
    background = SQLiteBackgroundTaskStore(db_path)
    requests = TaskExecutionStore(tmp_path / "task_requests")
    requests._sqlite = background
    try:
        _create_archive_fallback(agent_store)
        original = agent_store.create(name="pm", backend="claude")
        with agent_store.engine.begin() as conn:
            conn.execute(
                run_definitions.insert().values(
                    id="task_enqueue_race",
                    definition_type="scheduled",
                    name="enqueue race",
                    agent_name=original.name,
                    enabled=1,
                    created_at=NOW,
                    updated_at=NOW,
                    metadata_json="{}",
                )
            )

        stale_agent_name = original.name
        archived = agent_store.archive(original.name)
        assert archived is not None

        queued = requests.enqueue_definition_run(
            definition_id="task_enqueue_race",
            run_type="scheduled",
            source_kind="scheduler",
            session_key="",
            session_id=None,
            post_to=None,
            deliver_key=None,
            prompt="continue",
            agent_name=stale_agent_name,
            session_policy="create_once",
        )

        assert queued.agent_id == original.id
        assert queued.agent_name == archived.archived_name
        stored = background.get_run(queued.id)
        assert stored is not None
        assert stored["agent_id"] == original.id
        assert stored["agent_name"] == archived.archived_name
    finally:
        background.close()
        agent_store.close()


def test_existing_definition_enqueue_uses_one_current_definition_snapshot(tmp_path) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    agent_store = VibeAgentStore(db_path)
    background = SQLiteBackgroundTaskStore(db_path)
    requests = TaskExecutionStore(tmp_path / "task_requests")
    requests._sqlite = background
    try:
        agent = agent_store.create(name="current-worker", backend="claude")
        with agent_store.engine.begin() as conn:
            conn.execute(
                run_definitions.insert().values(
                    id="task_current_snapshot",
                    definition_type="scheduled",
                    name="current snapshot",
                    agent_name=agent.name,
                    session_policy="create_per_run",
                    session_id="ses-current",
                    legacy_session_key="slack::channel::current",
                    post_to="scope",
                    deliver_key="slack::channel::C2",
                    prompt="current prompt",
                    message="current message",
                    message_payload_json=json.dumps({"text": "current payload"}),
                    enabled=1,
                    created_at=NOW,
                    updated_at=NOW,
                    metadata_json=json.dumps({"version": 2}),
                )
            )

        queued = requests.enqueue_definition_run(
            definition_id="task_current_snapshot",
            run_type="scheduled",
            source_kind="scheduler",
            session_key="slack::channel::stale",
            session_id="ses-stale",
            post_to="none",
            deliver_key="slack::channel::C1",
            prompt="stale prompt",
            agent_name="stale-worker",
            session_policy="existing",
            metadata={"version": 1},
        )

        expected = {
            "agent_name": agent.name,
            "agent_id": agent.id,
            "session_policy": "create_per_run",
            "session_id": "ses-current",
            "session_key": "slack::channel::current",
            "post_to": "scope",
            "deliver_key": "slack::channel::C2",
            "prompt": "current prompt",
            "message": "current message",
            "message_payload": {"text": "current payload"},
            "metadata": {"version": 2},
        }
        assert {key: getattr(queued, key) for key in expected} == expected
        stored = background.get_run(queued.id)
        assert stored is not None
        assert {key: stored[key] for key in expected} == expected
    finally:
        background.close()
        agent_store.close()


def test_archive_does_not_rewrite_reused_name_owned_by_another_agent_id(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        _create_archive_fallback(store)
        first = store.create(name="pm", backend="claude")
        first_archive = store.archive(first.name)
        assert first_archive is not None
        replacement = store.create(name="pm", backend="claude")
        with store.engine.begin() as conn:
            conn.execute(
                agent_sessions.insert().values(
                    id="ses_stale_reused_name",
                    scope_id=None,
                    agent_id=first.id,
                    agent_name=replacement.name,
                    agent_backend="claude",
                    agent_variant="claude",
                    model=None,
                    reasoning_effort=None,
                    session_anchor="ses_stale_reused_name",
                    workdir=None,
                    native_session_id="native-first",
                    title="Stable first Agent",
                    status="active",
                    visibility="foreground",
                    pinned=0,
                    agent_status="idle",
                    metadata_json="{}",
                    created_at=NOW,
                    updated_at=NOW,
                    last_active_at=NOW,
                )
            )
            conn.execute(
                agent_runs.insert().values(
                    id="run_stale_reused_name",
                    run_type="agent_run",
                    status="queued",
                    agent_id=first.id,
                    agent_name=replacement.name,
                    created_at=NOW,
                    updated_at=NOW,
                    metadata_json="{}",
                )
            )

        result = store.archive(replacement.name)

        assert result is not None
        assert result.references["sessions"] == 0
        with store.engine.connect() as conn:
            session_name = conn.execute(
                select(agent_sessions.c.agent_name).where(
                    agent_sessions.c.id == "ses_stale_reused_name"
                )
            ).scalar_one()
            run_name = conn.execute(
                select(agent_runs.c.agent_name).where(agent_runs.c.id == "run_stale_reused_name")
            ).scalar_one()
        assert session_name == "pm"
        assert run_name == "pm"
    finally:
        store.close()


def test_rename_moves_references_and_default_without_changing_agent_identity(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        original = store.create(name="pm", backend="claude")
        store.set_default_agent_name("pm")
        _seed_references(store, "pm")

        renamed = store.rename("pm", "project-manager")

        assert renamed.id == original.id
        assert store.get_default_agent_name() == "project-manager"
        assert store.reference_counts("project-manager") == {
            "scopes": 1,
            "sessions": 1,
            "definitions": 1,
        }
        assert store.get("pm") is None
        with store.engine.connect() as conn:
            runs = conn.execute(select(agent_runs.c.status, agent_runs.c.agent_name)).mappings().all()
        assert {
            row["agent_name"]
            for row in runs
            if row["status"] in {"pending", "queued", "processing", "running"}
        } == {"project-manager"}
        assert {
            row["agent_name"]
            for row in runs
            if row["status"] in {"succeeded", "failed", "canceled"}
        } == {"pm"}
    finally:
        store.close()


def test_rename_updates_normalized_equivalent_legacy_default(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        original = store.create(name="Project Manager", backend="claude")
        with store.engine.begin() as conn:
            conn.execute(
                state_meta.insert().values(
                    key="default_agent_name",
                    value_json=json.dumps("PROJECT-MANAGER"),
                    updated_at=NOW,
                )
            )
        assert store.get_default_agent() is not None
        assert store.get_default_agent().id == original.id

        renamed = store.rename(original.name, "review-lead")

        assert renamed.id == original.id
        assert store.get_default_agent_name() == renamed.name
        assert store.get_default_agent() is not None
        assert store.get_default_agent().id == original.id
    finally:
        store.close()


def test_archive_invalidates_stale_definition_writes_for_direct_and_snapshot_bindings(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        _create_archive_fallback(store)
        store.create(name="pm", backend="claude")
        rows = (
            {"id": "task_direct", "agent_name": "pm", "metadata_json": "{}"},
            {
                "id": "task_snapshot",
                "agent_name": None,
                "metadata_json": json.dumps(
                    {
                        "session_settings_snapshot": {
                            "agent_name": "pm",
                            "captured_at": NOW,
                        }
                    }
                ),
            },
        )
        expectations: dict[str, DefinitionWriteExpectation] = {}
        with store.engine.begin() as conn:
            for row in rows:
                conn.execute(
                    run_definitions.insert().values(
                        id=row["id"],
                        definition_type="scheduled",
                        name=row["id"],
                        agent_name=row["agent_name"],
                        enabled=0,
                        created_at=NOW,
                        updated_at=NOW,
                        metadata_json=row["metadata_json"],
                    )
                )
                expectations[row["id"]] = DefinitionWriteExpectation.from_read(
                    enabled=False,
                    metadata=json.loads(row["metadata_json"]),
                )

        archived = store.archive("pm")
        assert archived is not None

        with store.engine.begin() as conn:
            for row in rows:
                stale_write = conn.execute(
                    update(run_definitions)
                    .where(run_definitions.c.id == row["id"])
                    .where(*definition_state_unchanged(expectations[row["id"]]))
                    .values(agent_name=row["agent_name"], metadata_json=row["metadata_json"])
                )
                assert stale_write.rowcount == 0

            stored = {row["id"]: row for row in conn.execute(select(run_definitions)).mappings().all()}
        assert stored["task_direct"]["agent_name"] == archived.archived_name
        assert (
            json.loads(stored["task_snapshot"]["metadata_json"])["session_settings_snapshot"]["agent_name"]
            == archived.archived_name
        )
    finally:
        store.close()


def test_remove_compacts_legacy_archive_name_and_moves_its_references(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        original = store.create(name="pm", backend="claude")
        _seed_references(store, original.name)
        legacy_name = "_archived_500f78fda90b4b06920bf89f187fb47d"
        archive_metadata = {
            "original_name": original.name,
            "archived_at": NOW,
            "was_enabled": True,
        }
        with store.engine.begin() as conn:
            conn.execute(
                agents.update()
                .where(agents.c.id == original.id)
                .values(
                    name=legacy_name,
                    normalized_name=normalize_agent_name(legacy_name),
                    enabled=0,
                    metadata_json=json.dumps({AGENT_ARCHIVE_METADATA_KEY: archive_metadata}),
                    archived_at=NOW,
                    updated_at=NOW,
                )
            )
            store._rewrite_references(
                conn,
                agent_id=original.id,
                reference_names=frozenset((original.name, original.normalized_name)),
                new_name=legacy_name,
                revision=NOW,
            )

        compacted = store.archive("pm")

        assert compacted is not None
        assert compacted.original_name == "pm"
        assert compacted.archived_name.startswith("_pm-")
        assert len(compacted.archived_name) == len("_pm-") + 4
        assert compacted.references == {"scopes": 1, "sessions": 1, "definitions": 1}
        assert store.get(legacy_name) is None
        assert store.require(compacted.archived_name).to_dict()["display_name"] == "pm"
        with store.engine.connect() as conn:
            assert conn.execute(
                select(agent_sessions.c.id).where(agent_sessions.c.agent_name == legacy_name)
            ).first() is None
            assert conn.execute(
                select(run_definitions.c.id).where(run_definitions.c.agent_name == legacy_name)
            ).first() is None
    finally:
        store.close()


def test_archive_rolls_back_when_default_has_no_replacement(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        original = store.create(name="only-agent", backend="codex")
        store.set_default_agent_name(original.name)

        with pytest.raises(AgentArchiveError) as exc_info:
            store.archive(original.name)
        assert exc_info.value.code == "agent_no_default_replacement"
        assert exc_info.value.agent_name == original.name

        assert store.require(original.name).id == original.id
        assert store.get_default_agent_name() == original.name
    finally:
        store.close()


def test_archive_protects_the_only_effective_default_without_explicit_metadata(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        original = store.create(name="only-agent", backend="codex")
        assert store.get_default_agent_name() is None
        assert store.get_default_agent().id == original.id

        with pytest.raises(AgentArchiveError) as exc_info:
            store.archive(original.name)
        assert exc_info.value.code == "agent_no_default_replacement"
        assert exc_info.value.agent_name == original.name

        assert store.require(original.name).id == original.id
        assert store.get_default_agent_name() is None
    finally:
        store.close()


def test_archive_persists_replacement_for_the_effective_fallback_default(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        original = store.create(name="alpha", backend="codex")
        replacement = store.create(name="beta", backend="claude")
        assert store.get_default_agent_name() is None
        assert store.get_default_agent().id == original.id

        result = store.archive(original.name)

        assert result is not None
        assert result.default_agent_name == replacement.name
        assert store.get_default_agent_name() == replacement.name
        assert store.get_default_agent().id == replacement.id
    finally:
        store.close()


def test_archive_replaces_disabled_explicit_default_pointer(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        original = store.create(name="alpha", backend="codex")
        replacement = store.create(name="beta", backend="claude")
        store.set_default_agent_name(original.name)
        store.set_enabled(original.name, False)
        assert store.get_default_agent().id == replacement.id

        result = store.archive(original.name)

        assert result is not None
        assert result.default_agent_name == replacement.name
        assert store.get_default_agent_name() == replacement.name
        recreated = store.create(name=original.name, backend="opencode")
        assert store.get_default_agent().id == replacement.id
        assert store.get_default_agent().id != recreated.id
    finally:
        store.close()


def test_archive_clears_disabled_explicit_default_without_fallback(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        original = store.create(name="alpha", backend="codex")
        store.set_default_agent_name(original.name)
        store.set_enabled(original.name, False)
        assert store.get_default_agent() is None

        result = store.archive(original.name)

        assert result is not None
        assert result.default_agent_name is None
        assert store.get_default_agent_name() is None
        store.create(name=original.name, backend="opencode")
        assert store.get_default_agent_name() is None
    finally:
        store.close()


@pytest.mark.parametrize(
    "stored_name",
    ("project-manager", "PROJECT-MANAGER", "Project Manager", "Project.Manager"),
)
def test_archive_moves_normalized_equivalent_references(tmp_path, stored_name: str) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        _create_archive_fallback(store)
        original = store.create(name="Project Manager", backend="claude")
        _seed_references(store, stored_name)

        result = store.archive(original.name)

        assert result is not None
        assert result.references == {"scopes": 1, "sessions": 1, "definitions": 1}
        assert store.require_reference_by_id(original.id).name == result.archived_name
        with store.engine.connect() as conn:
            scope = conn.execute(select(scope_settings)).mappings().one()
            session = conn.execute(select(agent_sessions)).mappings().one()
            definitions = conn.execute(select(run_definitions)).mappings().all()
            live_runs = conn.execute(
                select(agent_runs.c.agent_name).where(
                    agent_runs.c.status.in_(("pending", "queued", "processing", "running"))
                )
            ).scalars().all()
            queued_snapshot = conn.execute(
                select(message_deliveries.c.snapshot_json).where(
                    message_deliveries.c.id == "msg_queued_archive"
                )
            ).scalar_one()
        assert scope["agent_name"] == result.archived_name
        assert json.loads(scope["settings_json"])["routing"] == {
            "agent_name": result.archived_name,
            "agent": result.archived_name,
        }
        assert session["agent_name"] == result.archived_name
        definitions_by_id = {row["id"]: row for row in definitions}
        assert definitions_by_id["task_live"]["agent_name"] == result.archived_name
        assert definitions_by_id["watch_deleted"]["agent_name"] == stored_name
        assert json.loads(definitions_by_id["task_live"]["metadata_json"])[
            "session_settings_snapshot"
        ]["agent_name"] == result.archived_name
        assert json.loads(definitions_by_id["watch_deleted"]["metadata_json"])[
            "session_settings_snapshot"
        ]["agent_name"] == stored_name
        assert set(live_runs) == {result.archived_name}
        queued_spec = json.loads(json.loads(queued_snapshot)["metadata_json"])[
            "scheduled_provenance"
        ]["platform_specific"]
        assert queued_spec["vibe_agent_name"] == stored_name
        assert queued_spec["scheduled_target_agent_name"] == stored_name
        assert queued_spec["agent_session_target"]["agent_name"] == stored_name
    finally:
        store.close()


@pytest.mark.parametrize("bind_by_id", (False, True))
def test_late_native_bind_preserves_archived_session_routing_name(tmp_path, bind_by_id: bool) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    service = None
    try:
        _create_archive_fallback(store)
        original = store.create(name="Project Manager", backend="claude")
        _seed_references(store, original.name)
        with store.engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == "ses_archive")
                .values(agent_id=original.id)
            )
        result = store.archive(original.name)
        assert result is not None

        service = SQLiteSessionsService(store.db_path)
        if bind_by_id:
            bound = service.bind_agent_session_by_id(
                session_id="ses_archive",
                native_session_id="native-pm",
                vibe_agent_id=original.id,
                vibe_agent_name=original.name,
                vibe_agent_backend=original.backend,
            )
        else:
            bound = service.bind_agent_session(
                scope_key="avibe::project::proj_archive",
                agent_name=original.backend,
                session_anchor="ses_archive",
                native_session_id="native-pm",
                vibe_agent_id=original.id,
                vibe_agent_name=original.name,
            )

        assert bound == "ses_archive"
        with store.engine.connect() as conn:
            session = conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == "ses_archive")
            ).mappings().one()
        assert session["agent_id"] == original.id
        assert session["agent_name"] == result.archived_name
    finally:
        if service is not None:
            service.close()
        store.close()


def test_archive_refuses_to_overwrite_malformed_definition_metadata(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        _create_archive_fallback(store)
        original = store.create(name="pm", backend="claude")
        with store.engine.begin() as conn:
            conn.execute(
                run_definitions.insert().values(
                    id="task_malformed",
                    definition_type="scheduled",
                    name="Malformed metadata",
                    agent_name=original.name,
                    enabled=0,
                    created_at=NOW,
                    updated_at=NOW,
                    metadata_json="{not-json",
                )
            )

        with pytest.raises(AgentReferenceRewriteError) as exc_info:
            store.archive(original.name)
        assert exc_info.value.code == "agent_reference_metadata_invalid"

        assert store.require_enabled(original.name).id == original.id
        with store.engine.connect() as conn:
            row = conn.execute(
                select(run_definitions.c.agent_name, run_definitions.c.metadata_json).where(
                    run_definitions.c.id == "task_malformed"
                )
            ).one()
        assert row == (original.name, "{not-json")
    finally:
        store.close()


def test_archive_ignores_soft_deleted_definition_metadata(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        _create_archive_fallback(store)
        original = store.create(name="pm", backend="claude")
        with store.engine.begin() as conn:
            conn.execute(
                run_definitions.insert().values(
                    id="task_deleted_malformed",
                    definition_type="scheduled",
                    name="Deleted malformed metadata",
                    agent_name=original.name,
                    enabled=0,
                    deleted_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                    metadata_json="{not-json",
                )
            )

        archived = store.archive(original.name)

        assert archived is not None
        assert archived.references["definitions"] == 0
        with store.engine.connect() as conn:
            row = conn.execute(
                select(
                    run_definitions.c.agent_name,
                    run_definitions.c.metadata_json,
                    run_definitions.c.deleted_at,
                ).where(run_definitions.c.id == "task_deleted_malformed")
            ).one()
        assert row == (original.name, "{not-json", NOW)
    finally:
        store.close()


def test_archive_rolls_back_when_reference_migration_fails(tmp_path, monkeypatch) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        _create_archive_fallback(store)
        original = store.create(name="worker", backend="codex")

        def fail_reference_migration(*_args, **_kwargs):
            raise RuntimeError("injected reference migration failure")

        monkeypatch.setattr(store, "_rewrite_references", fail_reference_migration)
        with pytest.raises(RuntimeError, match="injected reference migration failure"):
            store.archive(original.name)

        restored = store.require(original.name)
        assert restored.id == original.id
        assert restored.archived_at is None
        assert restored.enabled is True
    finally:
        store.close()


def test_user_agent_names_cannot_enter_internal_namespace(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        with pytest.raises(AgentNameValidationError) as create_reserved:
            store.create(name="_hidden", backend="codex")
        assert create_reserved.value.code == "agent_name_reserved"
        store.create(name="worker", backend="codex")
        with pytest.raises(AgentNameValidationError) as rename_reserved:
            store.rename("worker", "_hidden")
        assert rename_reserved.value.code == "agent_name_reserved"
        for invalid_name in ("review/team", r"review\team"):
            with pytest.raises(AgentNameValidationError) as create_path:
                store.create(name=invalid_name, backend="codex")
            assert create_path.value.code == "agent_name_path_separator"
            with pytest.raises(AgentNameValidationError) as rename_path:
                store.rename("worker", invalid_name)
            assert rename_path.value.code == "agent_name_path_separator"
    finally:
        store.close()


def test_ordinary_disabled_agent_is_not_a_resolvable_reference(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
        store.create(name="paused", backend="codex", enabled=False)
        with pytest.raises(AgentUnavailableError, match="disabled"):
            store.require_reference("paused")

        archived = store.archive("paused")
        assert archived is not None
        with pytest.raises(AgentUnavailableError, match="disabled"):
            store.require_reference(archived.archived_name)
    finally:
        store.close()
