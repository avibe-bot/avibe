"""A credential's address must not survive in a persisted model selection.

The Model Hub catalog repair renames ids inside ``config.json``. Three SQLite
columns hold a *copy* of one of those ids — the Vibe Agent's model, a channel's
routing override, and a session's pin — and a turn reads them back in that
precedence to decide what it asks for. Repairing only the catalog would leave
them naming a model that no longer exists under that name.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import insert, select

from storage.db import get_cached_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.model_selection_addresses import (
    remove_credential_addresses_from_selections,
)
from storage.models import agent_sessions, agents, scope_settings, scopes

ADDRESS = "avibe-dc0395a1b2c3d4e5f60718"
OTHER_ADDRESS = "avibe-0123456789abcdef01234567"
NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def engine():
    ensure_sqlite_state()
    return get_cached_sqlite_engine()


def _add_agent(connection, agent_id: str, model: str | None) -> None:
    connection.execute(
        insert(agents).values(
            id=agent_id,
            name=agent_id,
            normalized_name=agent_id,
            backend="claude",
            model=model,
            enabled=1,
            source="user",
            metadata_json="{}",
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _add_scope(connection, scope_id: str, *, model: str | None, routing: dict) -> None:
    connection.execute(
        insert(scopes).values(
            id=scope_id,
            platform="slack",
            scope_type="channel",
            native_id=scope_id,
            is_private=0,
            supports_threads=1,
            metadata_json="{}",
            first_seen_at=NOW,
            last_seen_at=NOW,
            updated_at=NOW,
        )
    )
    connection.execute(
        insert(scope_settings).values(
            scope_id=scope_id,
            enabled=1,
            model=model,
            settings_version=1,
            settings_json=json.dumps({"routing": routing}),
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _add_session(connection, session_id: str, model: str | None) -> None:
    connection.execute(
        insert(agent_sessions).values(
            id=session_id,
            agent_backend="claude",
            agent_variant="default",
            model=model,
            session_anchor=session_id,
            native_session_id=session_id,
            status="active",
            visibility="foreground",
            pinned=0,
            agent_status="idle",
            metadata_json="{}",
            created_at=NOW,
            updated_at=NOW,
        )
    )


def test_every_selection_a_turn_resolves_loses_the_address(engine) -> None:
    """The Agent's model, the channel override, and the session pin all move.

    These are the three the turn's precedence reads, in that order. A repair
    that reached only one of them would leave the others deciding the request.
    """

    with engine.begin() as connection:
        _add_agent(connection, "agent-1", f"{ADDRESS}/gpt-5.5")
        _add_scope(
            connection,
            "scope-1",
            model=f"{ADDRESS}/gpt-5.5",
            routing={"agent_name": "claude", "model": f"{ADDRESS}/gpt-5.5"},
        )
        _add_session(connection, "session-1", f"{ADDRESS}/gpt-5.5")

    moved = remove_credential_addresses_from_selections({ADDRESS}, engine=engine)

    assert moved == 4
    with engine.connect() as connection:
        assert connection.execute(select(agents.c.model)).scalar_one() == "gpt-5.5"
        row = connection.execute(
            select(scope_settings.c.model, scope_settings.c.settings_json)
        ).one()
        assert row.model == "gpt-5.5"
        assert json.loads(row.settings_json)["routing"]["model"] == "gpt-5.5"
        assert (
            connection.execute(select(agent_sessions.c.model)).scalar_one() == "gpt-5.5"
        )


def test_the_routing_payload_moves_under_every_key_it_can_use(engine) -> None:
    """A scope spells its selection under whichever key its backend uses.

    ``_routing_from_row`` reads the payload before the column, so a repair that
    stopped at the column would leave the value a turn actually resolves.
    """

    routing = {
        "agent_name": "claude",
        "model_override": f"{ADDRESS}/opus",
        "claude_model": f"{ADDRESS}/sonnet",
        "codex_model": f"{ADDRESS}/gpt-5.5",
        "opencode_model": f"{ADDRESS}/kimi",
        "opencode_agent": "build",
    }
    with engine.begin() as connection:
        _add_scope(connection, "scope-1", model=None, routing=routing)

    moved = remove_credential_addresses_from_selections({ADDRESS}, engine=engine)

    assert moved == 4
    with engine.connect() as connection:
        stored = json.loads(
            connection.execute(select(scope_settings.c.settings_json)).scalar_one()
        )
    assert stored["routing"] == {
        "agent_name": "claude",
        "model_override": "opus",
        "claude_model": "sonnet",
        "codex_model": "gpt-5.5",
        "opencode_model": "kimi",
        "opencode_agent": "build",
    }


def test_an_identity_no_address_proves_is_left_alone(engine) -> None:
    """Only a proven address is an address; a vendor's own name is not ours.

    ``x-ai/grok-4.6-latest`` is a real upstream identity, and one spelled like
    a minted address still belongs to whoever minted it — unless this exact
    installation proved it is one of its own.
    """

    with engine.begin() as connection:
        _add_agent(connection, "agent-1", "x-ai/grok-4.6-latest")
        _add_agent(connection, "agent-2", f"{OTHER_ADDRESS}/gpt-5.5")
        _add_session(connection, "session-1", "anthropic/claude-sonnet-4")

    moved = remove_credential_addresses_from_selections({ADDRESS}, engine=engine)

    assert moved == 0
    with engine.connect() as connection:
        stored = set(connection.execute(select(agents.c.model)).scalars())
        assert stored == {"x-ai/grok-4.6-latest", f"{OTHER_ADDRESS}/gpt-5.5"}
        assert (
            connection.execute(select(agent_sessions.c.model)).scalar_one()
            == "anthropic/claude-sonnet-4"
        )


def test_a_second_pass_finds_nothing_left_to_move(engine) -> None:
    """Idempotent, so the retry an aborted repair schedules is free."""

    with engine.begin() as connection:
        _add_agent(connection, "agent-1", f"{ADDRESS}/gpt-5.5")

    assert remove_credential_addresses_from_selections({ADDRESS}, engine=engine) == 1
    assert remove_credential_addresses_from_selections({ADDRESS}, engine=engine) == 0


def test_no_proven_address_touches_nothing(engine) -> None:
    """With nothing proven there is nothing to remove, and no scan to pay for."""

    with engine.begin() as connection:
        _add_agent(connection, "agent-1", f"{ADDRESS}/gpt-5.5")

    assert remove_credential_addresses_from_selections(frozenset(), engine=engine) == 0
    with engine.connect() as connection:
        assert (
            connection.execute(select(agents.c.model)).scalar_one()
            == f"{ADDRESS}/gpt-5.5"
        )


def test_a_payload_that_cannot_be_read_does_not_stop_the_others(engine) -> None:
    """One unreadable blob is the previous release's state, not a failed repair."""

    with engine.begin() as connection:
        _add_agent(connection, "agent-1", f"{ADDRESS}/gpt-5.5")
        _add_scope(connection, "scope-1", model=None, routing={})
        connection.execute(
            scope_settings.update()
            .where(scope_settings.c.scope_id == "scope-1")
            .values(settings_json="{not json")
        )

    assert remove_credential_addresses_from_selections({ADDRESS}, engine=engine) == 1
    with engine.connect() as connection:
        assert connection.execute(select(agents.c.model)).scalar_one() == "gpt-5.5"
