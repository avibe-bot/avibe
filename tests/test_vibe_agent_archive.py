from __future__ import annotations

import json

import pytest
from sqlalchemy import select, update

from core.vibe_agents import (
    AGENT_ARCHIVE_METADATA_KEY,
    AgentUnavailableError,
    VibeAgentStore,
    normalize_agent_name,
)
from storage.background import DefinitionWriteExpectation, definition_state_unchanged
from storage.models import agent_runs, agent_sessions, agents, run_definitions, scope_settings, scopes


NOW = "2026-07-31T14:00:00+00:00"
SCOPE_ID = "avibe::project::proj_archive"


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
        with pytest.raises(ValueError, match="archived and cannot be edited"):
            store.set_enabled(result.archived_name, True)

        listed_with_archives = store.list_agents(include_disabled=True, include_archived=True)
        assert any(agent.id == original.id for agent in listed_with_archives)

        with store.engine.connect() as conn:
            scope = conn.execute(select(scope_settings)).mappings().one()
            session = conn.execute(select(agent_sessions)).mappings().one()
            definitions = conn.execute(select(run_definitions).order_by(run_definitions.c.id)).mappings().all()
            runs = conn.execute(select(agent_runs.c.status, agent_runs.c.agent_name)).mappings().all()
        assert scope["agent_name"] == result.archived_name
        assert json.loads(scope["settings_json"])["routing"] == {
            "agent_name": result.archived_name,
            "agent": result.archived_name,
        }
        assert session["agent_name"] == result.archived_name
        assert {row["agent_name"] for row in definitions} == {result.archived_name}
        assert {
            json.loads(row["metadata_json"])["session_settings_snapshot"]["agent_name"]
            for row in definitions
        } == {result.archived_name}
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

        replacement = store.create(name="pm", backend="claude")
        assert replacement.id != original.id
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


def test_archive_invalidates_stale_definition_writes_for_direct_and_snapshot_bindings(tmp_path) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
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
                old_name=original.name,
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

        with pytest.raises(ValueError, match="without another enabled Agent"):
            store.archive(original.name)

        assert store.require(original.name).id == original.id
        assert store.get_default_agent_name() == original.name
    finally:
        store.close()


def test_archive_rolls_back_when_reference_migration_fails(tmp_path, monkeypatch) -> None:
    store = VibeAgentStore(tmp_path / "state" / "vibe.sqlite")
    try:
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
        with pytest.raises(ValueError, match="reserved for Avibe"):
            store.create(name="_hidden", backend="codex")
        store.create(name="worker", backend="codex")
        with pytest.raises(ValueError, match="reserved for Avibe"):
            store.rename("worker", "_hidden")
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
