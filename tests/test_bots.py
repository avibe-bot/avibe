"""Bots: Agent metadata plus a reserved Session and a directory (``docs/plans/bots.md`` D1, D2, D7, D9)."""

from __future__ import annotations

import subprocess

import pytest

from storage.importer import ensure_sqlite_state


@pytest.fixture()
def home(monkeypatch, tmp_path):
    from core.vibe_agents import VibeAgentStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agents([])
    finally:
        store.close()
    yield tmp_path


@pytest.fixture()
def service(home):
    from core.bots import BotService

    with BotService() as bots:
        yield bots


def _store():
    from core.vibe_agents import VibeAgentStore

    return VibeAgentStore()


def _session(session_id: str):
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import get_session

    with create_sqlite_engine().connect() as conn:
        return get_session(conn, session_id)


def _git(directory, *args) -> str:
    return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True, text=True).stdout


# --- the main Bot -------------------------------------------------------------------------------------------------


def test_the_main_bot_is_a_dedicated_builtin_agent_beside_vibey(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = _store()
    try:
        vibey = store.ensure_builtin_default_agent(backend="vibey")
        store.update(vibey.name, model="vibey-model", reasoning_effort="low")
        # A user Agent already holds the name: the main Bot's Agent takes the next free one.
        store.create(name="main", backend="vibey")
        store.ensure_builtin_default_agents([])
        main = store.get_main_bot_agent()
        assert (main.name, main.backend, main.source, main.enabled) == ("main-2", "vibey", "builtin", True)
        assert (main.model, main.reasoning_effort) == ("vibey-model", "low")
        assert main.description == "The user's main Bot and first point of contact."
        # Vibey keeps its built-in identity (and its preamble); the main Bot never becomes the default for new chats.
        assert store.get_builtin_default_agent_for_backend("vibey").id == vibey.id
        assert store.get_default_agent().id == vibey.id
        assert store.ensure_main_bot_agent().id == main.id
    finally:
        store.close()


def test_the_main_bot_is_always_enabled_and_cannot_leave(service):
    from core.bots import BotError
    from core.vibe_agents import AgentAlwaysEnabledError, AgentArchiveError, AgentBotLockedError

    bot = service.ensure_main_bot()
    assert (bot.handle, bot.session_id, bot.builtin, bot.display_name) == ("main", "ses_main", True, "")
    assert bot.to_payload()["avatar"] == {"kind": "mascot"}
    store = service.store
    with pytest.raises(AgentAlwaysEnabledError):
        store.update(bot.agent.name, enabled=False)
    with pytest.raises(AgentBotLockedError):
        store.rename(bot.agent.name, "other")
    with pytest.raises(AgentArchiveError) as archived:
        store.archive(bot.agent.name)
    assert archived.value.code == "agent_is_bot"
    with pytest.raises(BotError) as removed:
        service.remove_bot("main")
    assert removed.value.code == "bot_main_not_removable"
    renamed = service.update_bot("main", display_name="歪比")
    assert (renamed.display_name, _session("ses_main")["title"]) == ("歪比", "歪比")
    assert service.update_bot("main", display_name="").display_name == ""
    assert _session("ses_main")["title"] is None


# --- Bot fields are the catalog's ---------------------------------------------------------------------------------


def test_ordinary_agent_writes_cannot_forge_or_strip_bot_fields(service):
    store = service.store
    forged = store.create(
        name="forger", backend="vibey", metadata={"bot": {"handle": "forger"}, "builtin_bot": "main", "note": 1}
    )
    assert forged.metadata == {"note": 1}
    assert service.list_bots()[-1].handle == "main"

    bot = service.create_bot(display_name="Researcher")
    kept = store.update(bot.agent.name, metadata={"note": 2})
    assert kept.metadata["bot"]["handle"] == "researcher"
    assert kept.metadata["note"] == 2
    overwritten = store.update(bot.agent.name, metadata={"bot": {"handle": "other", "removed_at": "now"}})
    assert overwritten.metadata["bot"]["handle"] == "researcher"
    assert "removed_at" not in overwritten.metadata["bot"]


def test_a_bot_agent_keeps_its_name_row_and_enabled_state_until_removed(service):
    from core.vibe_agents import AgentArchiveError, AgentBotLockedError

    bot = service.create_bot(display_name="Researcher")
    store = service.store
    with pytest.raises(AgentBotLockedError):
        store.update(bot.agent.name, enabled=False)
    with pytest.raises(AgentBotLockedError):
        store.rename(bot.agent.name, "scholar")
    with pytest.raises(AgentArchiveError):
        store.archive(bot.agent.name)
    with pytest.raises(AgentBotLockedError):
        store.remove(bot.agent.name)

    service.remove_bot(bot.handle)
    assert store.rename(bot.agent.name, "scholar").name == "scholar"
    assert store.update("scholar", enabled=False).enabled is False


# --- custom Bots --------------------------------------------------------------------------------------------------


def test_a_new_bot_gets_an_agent_a_session_and_a_directory_with_only_git(service, home):
    main = service.ensure_main_bot()
    service.store.update(main.agent.name, model="main-model", reasoning_effort="medium")
    bot = service.create_bot(display_name="首席安全官", role="Reviews security risks.", avatar={"emoji": "🛡️", "hue": 210})

    assert bot.handle == "shouxianquanguan"
    agent = bot.agent
    assert (agent.name, agent.backend, agent.description) == (bot.handle, "vibey", "Reviews security risks.")
    assert (agent.model, agent.reasoning_effort) == ("main-model", "medium")
    assert agent.system_prompt is None
    session = _session(bot.session_id)
    assert session["id"] == f"ses_{bot.handle}"
    assert session["scope_id"] is None
    assert session["workdir"] == str(home / "bots" / bot.handle)
    assert session["title"] == "首席安全官"
    assert session["metadata"]["kind"] == "bot"
    assert sorted(path.name for path in (home / "bots" / bot.handle).iterdir()) == [".git"]
    assert _git(home / "bots" / bot.handle, "rev-list", "--all") == ""
    assert bot.to_payload()["avatar"] == {"kind": "emoji", "emoji": "🛡️", "hue": 210}
    assert [item.handle for item in service.list_bots()] == ["main", bot.handle]


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [("Researcher", "researcher"), ("小爱", "xiaoai"), ("Main", "main-2"), ("Ops", "ops-2"), ("42", "bot")],
)
def test_generated_handles_normalize_transliterate_and_skip_reserved(service, display_name, expected):
    assert service.create_bot(display_name=display_name).handle == expected


def test_generated_handles_add_a_suffix_on_collision(service):
    service.store.create(name="xiaoai", backend="claude")
    assert service.create_bot(display_name="小爱").handle == "xiaoai-2"
    assert service.create_bot(display_name="小爱").handle == "xiaoai-3"


@pytest.mark.parametrize(
    ("handle", "code"),
    [("Bad Handle", "bot_handle_invalid"), ("a", "bot_handle_invalid"), ("memory", "bot_handle_reserved")],
)
def test_a_chosen_handle_is_checked_never_rewritten(service, handle, code):
    from core.bots import BotError

    with pytest.raises(BotError) as refused:
        service.create_bot(display_name="Anything", handle=handle)
    assert refused.value.code == code


def test_a_chosen_handle_that_is_taken_is_refused(service):
    from core.bots import BotError

    service.create_bot(display_name="Security", handle="cso")
    with pytest.raises(BotError) as refused:
        service.create_bot(display_name="Another", handle="cso")
    assert (refused.value.code, refused.value.status) == ("bot_handle_taken", 409)


def test_removing_hides_and_setting_again_reconnects(service, home):
    bot = service.create_bot(display_name="Researcher")
    service.remove_bot(bot.handle)
    assert [item.handle for item in service.list_bots()] == ["main"]
    assert _session(bot.session_id)["status"] == "active"
    assert (home / "bots" / bot.handle / ".git").is_dir()

    # Its Agent can be renamed once it is no longer a Bot; setting it again still reconnects the old handle.
    service.store.rename(bot.agent.name, "scholar")
    again = service.set_bot("scholar", display_name="Scholar")
    assert (again.handle, again.session_id) == ("researcher", bot.session_id)
    session = _session(bot.session_id)
    assert (session["agent_name"], session["title"]) == ("scholar", "Scholar")


def test_only_enabled_vibey_agents_can_be_set(service):
    from core.bots import BotError

    service.store.create(name="coder", backend="claude")
    service.store.create(name="idle", backend="vibey", enabled=False)
    for name, code in (("coder", "bot_agent_backend"), ("idle", "bot_agent_disabled"), ("ghost", "bot_agent_not_found")):
        with pytest.raises(BotError) as refused:
            service.set_bot(name, display_name="X")
        assert refused.value.code == code
    service.store.create(name="writer_1", backend="vibey")
    bot = service.set_bot("writer_1", display_name="Writer")
    assert bot.handle == "writer-1"
    with pytest.raises(BotError) as twice:
        service.set_bot("writer_1", display_name="Writer")
    assert twice.value.code == "bot_agent_is_bot"


def test_positions_order_bots_after_the_main_bot(service):
    from core.bots import BotError

    for name in ("Alpha", "Beta", "Gamma"):
        service.create_bot(display_name=name)
    service.update_bot("gamma", position=1)
    assert [bot.handle for bot in service.list_bots()] == ["main", "gamma", "alpha", "beta"]
    service.remove_bot("alpha")
    assert [bot.position for bot in service.list_bots()] == [0, 1, 2]
    with pytest.raises(BotError):
        service.update_bot("main", position=2)


def test_bot_operations_are_the_owners(service):
    from vibe.authorization import AuthorizationContext, InstanceAuthorizationError

    member = AuthorizationContext(instance_role="member", is_remote=True, subject="m1")
    with pytest.raises(InstanceAuthorizationError):
        service.list_bots(authorization_context=member)
    with pytest.raises(InstanceAuthorizationError):
        service.create_bot(display_name="X", authorization_context=member)


# --- Bot Sessions -------------------------------------------------------------------------------------------------


def test_bot_sessions_are_reserved_and_owner_only(service):
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import (
        ReservedSessionError,
        archive_session,
        list_sessions,
        update_session,
    )
    from vibe.authorization import AuthorizationContext

    bot = service.create_bot(display_name="Researcher")
    member = AuthorizationContext(instance_role="member", is_remote=True, subject="m1")
    with create_sqlite_engine().begin() as conn:
        assert update_session(conn, bot.session_id, model="other-model")["model"] == "other-model"
        for fields in ({"title": "x"}, {"agent_name": "vibey"}, {"visibility": "background"}, {"scope_id": None}):
            with pytest.raises(ReservedSessionError):
                update_session(conn, bot.session_id, **fields)
        with pytest.raises(ReservedSessionError):
            archive_session(conn, bot.session_id)
        listed = {row["id"] for row in list_sessions(conn, status="all")["sessions"]}
        searched = {row["id"] for row in list_sessions(conn, title_query="Research")["sessions"]}
        assert bot.session_id not in listed | searched
        from storage import project_access_service

        assert project_access_service.get_effective_session_role(conn, member, bot.session_id) is None


def test_the_inbox_includes_bot_sessions_for_the_owner_only(service):
    from core.bots import hidden_bot_session_ids
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from vibe.authorization import AuthorizationContext, instance_owner_context

    shown = service.create_bot(display_name="Shown")
    removed = service.create_bot(display_name="Removed")
    with create_sqlite_engine().begin() as conn:
        for bot in (shown, removed):
            messages_service.append(
                conn, scope_id=None, session_id=bot.session_id, platform="avibe", author="agent",
                message_type="result", text="done",
            )
    service.remove_bot(removed.handle)
    member = AuthorizationContext(instance_role="member", is_remote=True, subject="m1")
    with create_sqlite_engine().connect() as conn:
        owner_hidden = hidden_bot_session_ids(conn, instance_owner_context())
        feed = messages_service.list_inbox_sessions(conn, hidden_session_ids=owner_hidden)
        assert [row["session_id"] for row in feed["sessions"]] == [shown.session_id]
        assert messages_service.unread_counts_by_session(conn, hidden_session_ids=owner_hidden) == {shown.session_id: 1}
        member_hidden = hidden_bot_session_ids(conn, member)
        assert messages_service.list_inbox_sessions(conn, hidden_session_ids=member_hidden)["sessions"] == []
        assert messages_service.total_unread(conn, hidden_session_ids=member_hidden) == 0


def test_agents_list_exposes_bot_info(service):
    from vibe import api

    bot = service.create_bot(display_name="Researcher")
    agents = {row["name"]: row for row in api.get_vibe_agents()["agents"]}
    assert agents[bot.agent.name]["bot"] == {"handle": "researcher", "display_name": "Researcher", "builtin": False}
    assert agents["main"]["bot"]["builtin"] is True
    assert agents["vibey"]["bot"] is None


def test_an_uploaded_avatar_is_a_256px_png_in_the_bot_directory(service, home):
    import io

    from PIL import Image

    bot = service.create_bot(display_name="Researcher")
    source = io.BytesIO()
    Image.new("RGB", (800, 400), "red").save(source, format="JPEG")
    updated = service.set_avatar_image(bot.handle, source.getvalue())
    stored = home / "bots" / bot.handle / "avatar.png"
    with Image.open(stored) as image:
        assert (image.format, image.size) == ("PNG", (256, 256))
    assert updated.to_payload()["avatar"]["url"].startswith(f"/api/bots/{bot.handle}/avatar?v=")
    assert service.avatar_path(bot.handle) == stored


def test_two_starts_racing_create_one_main_bot(home, monkeypatch):
    """The main Bot is decided under the writer lock, not by an Agent-name collision."""
    from sqlalchemy import func, select

    from storage.models import agents

    store = _store()
    try:
        existing = store.ensure_main_bot_agent()
        # The other process read "no main Bot" before this one committed it.
        real, stale = store.get_main_bot_agent, [True]
        monkeypatch.setattr(store, "get_main_bot_agent", lambda: None if stale and stale.pop() else real())
        assert store.ensure_main_bot_agent().id == existing.id
        with store.engine.connect() as conn:
            marked = conn.execute(
                select(func.count()).select_from(agents).where(
                    func.json_extract(agents.c.metadata_json, "$.builtin_bot") == "main"
                )
            ).scalar_one()
        assert marked == 1
    finally:
        store.close()


def test_the_cli_session_list_hides_removed_bots_and_other_members_bots(service):
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import list_sessions_page
    from vibe.authorization import AuthorizationContext, instance_owner_context

    shown = service.create_bot(display_name="Shown")
    removed = service.create_bot(display_name="Removed")
    service.remove_bot(removed.handle)
    member = AuthorizationContext(instance_role="member", is_remote=True, subject="m1")
    with create_sqlite_engine().connect() as conn:
        owner_ids = {row["id"] for row in list_sessions_page(conn, limit=50, authorization_context=instance_owner_context()).items}
        member_ids = {row["id"] for row in list_sessions_page(conn, limit=50, authorization_context=member).items}
    assert shown.session_id in owner_ids and removed.session_id not in owner_ids
    assert not {shown.session_id, removed.session_id} & member_ids
