from __future__ import annotations

import json

import pytest

from config import paths
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, messages
from storage.settings_service import upsert_scope
from vibe import cli


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    ensure_sqlite_state(primary_platform="avibe")
    return create_sqlite_engine(paths.get_sqlite_state_path())


def _seed(engine, sid, *, platform="avibe", native="proj_a", title="T", backend="claude", status="active", title_source=None, last_active="2026-06-09T10:00:00Z"):
    now = "2026-06-09T09:00:00Z"
    scope_type = "project" if platform == "avibe" else "channel"
    metadata = {"title_source": title_source} if title_source else {}
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform=platform, scope_type=scope_type, native_id=native, now=now)
        conn.execute(
            agent_sessions.insert().values(
                id=sid, scope_id=scope_id, agent_id="agent_internal_" + sid, agent_name=backend,
                agent_backend=backend, agent_variant="default", session_anchor="anc_" + sid,
                native_session_id="nat_" + sid, title=title, status=status, agent_status="idle",
                metadata_json=json.dumps(metadata), created_at=now, updated_at=now, last_active_at=last_active,
            )
        )
    return scope_id


def _run(cmd, argv, capsys):
    args = cli.build_parser().parse_args(argv)
    code = cmd(args)
    captured = capsys.readouterr()
    stream = captured.out if code == 0 else captured.err
    return code, json.loads(stream)


# --------------------------------------------------------------------------- list


def test_list_excludes_archived_and_orders_by_activity(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", platform="avibe", native="proj_a", last_active="2026-06-09T12:00:00Z")
    _seed(engine, "sesbbb", platform="slack", native="C1", last_active="2026-06-09T13:00:00Z")
    _seed(engine, "sesarch", platform="avibe", native="proj_a", status="archived", last_active="2026-06-09T14:00:00Z")

    code, payload = _run(cli.cmd_session_list, ["session", "list"], capsys)
    assert code == 0
    assert payload["kind"] == "agent_sessions"
    ids = [s["id"] for s in payload["sessions"]]
    assert ids == ["sesbbb", "sesaaa"]  # newest activity first; archived excluded
    assert "data query" in payload["message"]


def test_list_row_has_only_lean_fields(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", platform="slack", native="C1")
    _, payload = _run(cli.cmd_session_list, ["session", "list"], capsys)
    row = payload["sessions"][0]
    assert set(row) == {"id", "title", "platform", "project_id", "agent_name", "agent_status", "last_active_at"}
    assert row["platform"] == "slack"
    assert "agent_backend" not in row and "status" not in row


def test_list_type_filter(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesweb", platform="avibe", native="proj_a")
    _seed(engine, "seschat", platform="slack", native="C1")
    _, payload = _run(cli.cmd_session_list, ["session", "list", "--type", "slack"], capsys)
    assert [s["id"] for s in payload["sessions"]] == ["seschat"]


def test_list_avibe_type_includes_foreground_standalone(monkeypatch, tmp_path, capsys):
    from core.services import sessions as sessions_service

    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesweb", platform="avibe", native="proj_a")
    _seed(engine, "seschat", platform="slack", native="C1")
    with engine.begin() as conn:
        standalone = sessions_service.create_session(
            conn,
            scope_id=None,
            agent_backend="codex",
            visibility="foreground",
        )

    _, payload = _run(cli.cmd_session_list, ["session", "list", "--type", "avibe"], capsys)

    assert {session["id"] for session in payload["sessions"]} == {
        "sesweb",
        standalone["id"],
    }


def test_list_pagination_fixed_ten_no_limit_flag(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    for i in range(12):
        _seed(engine, f"ses{i:02d}", platform="slack", native="C1", last_active=f"2026-06-09T10:{i:02d}:00Z")
    _, page1 = _run(cli.cmd_session_list, ["session", "list"], capsys)
    assert len(page1["sessions"]) == 10
    assert page1["pagination"]["has_more"] is True
    assert page1["pagination"]["next_command"] == "vibe session list --page 2"
    assert "--limit" not in page1["pagination"]["next_command"]
    _, page2 = _run(cli.cmd_session_list, ["session", "list", "--page", "2"], capsys)
    assert len(page2["sessions"]) == 2
    assert page2["pagination"]["has_more"] is False


def test_list_on_fresh_home_returns_empty(monkeypatch, tmp_path, capsys):
    # No ensure_sqlite_state(): _open_session_engine must bootstrap the DB itself,
    # so a fresh Avibe home returns a clean empty list, not "no such table" (Codex P2).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    code, payload = _run(cli.cmd_session_list, ["session", "list"], capsys)
    assert code == 0
    assert payload["sessions"] == []


def test_list_invalid_type_errors(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    code, payload = _run(cli.cmd_session_list, ["session", "list", "--type", "bogus"], capsys)
    assert code == 1
    assert payload["ok"] is False
    assert payload["code"] == "invalid_session_type"


# ---------------------------------------------------------------------------- get


def test_get_returns_detail_without_status_agentid_anchor(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", platform="avibe", native="proj_a")
    code, payload = _run(cli.cmd_session_get, ["session", "get", "sesaaa"], capsys)
    assert code == 0
    s = payload["session"]
    assert s["id"] == "sesaaa"
    assert s["platform"] == "avibe"
    assert s["agent_backend"] == "claude"  # backend kept in detail
    for omitted in ("status", "agent_id", "session_anchor"):
        assert omitted not in s
    assert "vibe runs list --session-id sesaaa" in payload["message"]


def test_get_defaults_to_caller_session(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", platform="avibe", native="proj_a")
    monkeypatch.setenv("AVIBE_SESSION_ID", "sesaaa")

    code, payload = _run(cli.cmd_session_get, ["session", "get"], capsys)

    assert code == 0
    assert payload["session"]["id"] == "sesaaa"
    assert payload["session_default_notice"] == {
        "code": "session_defaulted_to_caller",
        "message": "Session defaulted to this Agent Session.",
        "session_id": "sesaaa",
    }


def test_get_requires_session_without_caller(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)

    code, payload = _run(cli.cmd_session_get, ["session", "get"], capsys)

    assert code == 1
    assert payload["code"] == "missing_session_target"
    assert payload["help_command"] == "vibe session get --help"
    assert payload["hint"] == "Run this command from an Avibe Agent shell, or pass the target Session ID positionally."


def test_get_archived_is_not_found(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesarch", status="archived")
    code, payload = _run(cli.cmd_session_get, ["session", "get", "sesarch"], capsys)
    assert code == 1
    assert payload["code"] == "session_not_found"


def test_get_missing_is_not_found(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    code, payload = _run(cli.cmd_session_get, ["session", "get", "nope"], capsys)
    assert code == 1
    assert payload["code"] == "session_not_found"


# ------------------------------------------------------------------------- update


def test_update_sets_title(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", title="Old")
    code, payload = _run(cli.cmd_session_update, ["session", "update", "sesaaa", "--title", "New name"], capsys)
    assert code == 0
    assert payload["updated"] is True
    assert payload["session"]["title"] == "New name"
    assert "status" not in payload["session"]
    # An agent-set title is sourced "agent" (vs "user" for a human Web UI edit).
    assert payload["session"]["metadata"]["title_source"] == "agent"


def test_update_defaults_to_caller_session(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", title="Old")
    monkeypatch.setenv("AVIBE_SESSION_ID", "sesaaa")

    code, payload = _run(cli.cmd_session_update, ["session", "update", "--title", "New name"], capsys)

    assert code == 0
    assert payload["updated"] is True
    assert payload["session"]["id"] == "sesaaa"
    assert payload["session"]["title"] == "New name"
    assert payload["session_default_notice"] == {
        "code": "session_defaulted_to_caller",
        "message": "Session defaulted to this Agent Session.",
        "session_id": "sesaaa",
    }


def test_update_requires_session_without_caller(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)

    code, payload = _run(cli.cmd_session_update, ["session", "update", "--title", "New name"], capsys)

    assert code == 1
    assert payload["code"] == "missing_session_target"
    assert payload["help_command"] == "vibe session update --help"
    assert payload["hint"] == "Run this command from an Avibe Agent shell, or pass the target Session ID positionally."


def test_update_empty_title_clears(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", title="Old")
    _, payload = _run(cli.cmd_session_update, ["session", "update", "sesaaa", "--title", ""], capsys)
    assert payload["session"]["title"] is None


def test_update_help_teaches_visibility_sugar(capsys):
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["session", "update", "--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--visible" in help_text
    assert "--hidden" in help_text
    assert "--visibility {foreground,background}" in help_text


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("--visible", "foreground"), ("--hidden", "background")],
)
def test_update_visibility_sugar_parses(flag, expected):
    args = cli.build_parser().parse_args(["session", "update", "sesaaa", flag])

    assert args.visibility == expected


@pytest.mark.parametrize(
    "visibility_args",
    [
        ["--visible", "--hidden"],
        ["--visible", "--visibility", "background"],
        ["--hidden", "--visibility", "foreground"],
    ],
)
def test_update_visibility_sugar_rejects_conflicts(visibility_args, capsys):
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["session", "update", "sesaaa", *visibility_args])

    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "invalid_arguments"
    assert "not allowed with argument" in payload["error"]


def test_update_visibility_and_make_standalone_keeps_workdir(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    original_scope_id = _seed(engine, "sesaaa", title="Old")
    posted: dict = {}
    monkeypatch.setattr(
        cli,
        "_post_session_activity_to_live_ui",
        lambda session_id, **kwargs: posted.update(session_id=session_id, **kwargs),
    )
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == "sesaaa")
            .values(workdir=str(tmp_path / "original-workdir"))
        )
        original_workdir = conn.execute(
            agent_sessions.select().where(agent_sessions.c.id == "sesaaa")
        ).mappings().one()["workdir"]

    code, payload = _run(
        cli.cmd_session_update,
        [
            "session",
            "update",
            "sesaaa",
            "--visibility",
            "background",
            "--scope-id",
            "none",
        ],
        capsys,
    )

    assert code == 0
    assert payload["session"]["visibility"] == "background"
    assert payload["session"]["scope_id"] is None
    assert payload["session"]["project_id"] is None
    assert payload["session"]["workdir"] == original_workdir
    assert posted == {
        "session_id": "sesaaa",
        "previous_scope_id": original_scope_id,
        "previous_visibility": "foreground",
    }
    with engine.connect() as conn:
        anchor = conn.execute(
            agent_sessions.select().where(agent_sessions.c.id == "sesaaa")
        ).mappings().one()["session_anchor"]
    assert anchor == "sesaaa"


def test_update_archived_is_not_found(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesarch", status="archived", title="Old")
    code, payload = _run(cli.cmd_session_update, ["session", "update", "sesarch", "--title", "x"], capsys)
    assert code == 1
    assert payload["code"] == "session_not_found"
    # title must not have been written to the soft-deleted row
    with engine.connect() as conn:
        from sqlalchemy import select

        title = conn.execute(select(agent_sessions.c.title).where(agent_sessions.c.id == "sesarch")).scalar_one()
    assert title == "Old"


# ------------------------------------------------------------------- IM linkage


def test_im_messages_link_to_session(monkeypatch, tmp_path):
    import core.message_mirror as mm
    from modules.im.base import MessageContext
    from sqlalchemy import select

    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesim", platform="slack", native="C777")

    spec = {"agent_session_id": "sesim", "vibe_agent_name": "claude", "vibe_agent_backend": "claude"}
    # agent reply rides the source session_id; scope stays the delivery channel
    mm.persist_agent_message(
        MessageContext(user_id="U", channel_id="C777", platform="slack", thread_id="t1", platform_specific=spec),
        "result", "answer",
    )
    # human inbound is scope-keyed first, then back-filled
    mm.mirror_inbound(MessageContext(user_id="U", channel_id="C777", platform="slack", thread_id="t1", message_id="m1"), "question")
    mm.link_inbound_message_session(platform="slack", native_message_id="m1", session_id="sesim")
    # harness prompt carrying a source session links too
    mm.mirror_harness_inbound(
        MessageContext(user_id="s", channel_id="C777", platform="slack", message_id="h1",
                       platform_specific={"agent_session_id": "sesim", "task_trigger_kind": "scheduled", "task_definition_id": "d1"}),
        "reminder",
    )

    with engine.connect() as conn:
        rows = list(conn.execute(select(messages.c.author, messages.c.session_id)).mappings())
    assert len(rows) == 3
    assert all(r["session_id"] == "sesim" for r in rows)


def test_link_inbound_is_noop_when_already_linked(monkeypatch, tmp_path):
    import core.message_mirror as mm
    from modules.im.base import MessageContext
    from sqlalchemy import select

    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesim", platform="slack", native="C777")
    _seed(engine, "sesother", platform="slack", native="C777")
    mm.mirror_inbound(MessageContext(user_id="U", channel_id="C777", platform="slack", message_id="m1"), "q")
    mm.link_inbound_message_session(platform="slack", native_message_id="m1", session_id="sesim")
    # a second, different back-fill must not overwrite an already-linked row
    mm.link_inbound_message_session(platform="slack", native_message_id="m1", session_id="sesother")
    with engine.connect() as conn:
        sid = conn.execute(select(messages.c.session_id).where(messages.c.native_message_id == "m1")).scalar_one()
    assert sid == "sesim"


# ------------------------------------------------------------ title prompt


def _injection_for(session_id, *, platform="avibe", platform_specific=None):
    from core.system_prompt_injection import build_system_prompt_injection
    from modules.im.base import MessageContext

    payload = {"agent_session_id": session_id}
    if platform_specific:
        payload.update(platform_specific)

    ctx = MessageContext(
        user_id="u", channel_id="c", platform=platform,
        platform_specific=payload,
    )
    return build_system_prompt_injection(context=ctx)


def test_web_title_prompt_defaults_to_current_session():
    out = _injection_for("sesweb")
    title_prompt = out[out.index("## Session Title") :]
    assert "## Session Title" in out
    assert "`vibe session get`" in out
    assert '`vibe session update --title "<short title>"`' in out
    assert "vibe session get sesweb" not in out
    assert 'vibe session update sesweb --title "<short title>"' not in out
    assert "metadata.title_source" in out
    assert "`user` or `agent`" in out
    assert "leave the title unchanged" in out
    assert "silently set one concise, human-scannable Session title" in out
    assert "without waiting for the user" in out
    assert "do not rename it again" in out
    assert len(title_prompt.strip()) < 450
    assert "not set yet" not in out
    assert "auto-generated" not in out


def test_im_title_prompt_is_not_injected():
    out = _injection_for("sesim", platform="slack")
    assert "## Session Title" not in out
    assert "vibe session update sesim --title" not in out
    assert "Current Session Reminder" not in out


def test_forked_session_prompt_marks_target_session_id_authoritative():
    out = _injection_for(
        "sestarget",
        platform_specific={
            "agent_session_target": {
                "id": "sestarget",
                "native_session_fork": {
                    "source_session_id": "sessource",
                    "source_native_session_id": "native-source",
                    "source_backend": "codex",
                },
            },
        },
    )

    assert "Current session id: `sestarget`" in out
    assert "This Agent Session was forked from `sessource`." in out
    assert "The authoritative Avibe session id for this fork is `sestarget`." in out
    assert "If copied source context mentions another Avibe session id" in out
    assert "treat it as historical source-context only" in out
    assert "for Show Pages, Harness commands, tasks, watches, callbacks, and session updates" not in out


def test_forked_session_prompt_can_use_persisted_metadata():
    out = _injection_for(
        "sestarget",
        platform_specific={
            "agent_session_target": {
                "id": "sestarget",
                "metadata": {
                    "created_via": "session_fork",
                    "fork_source_session_id": "sessource",
                    "fork_source_native_session_id": "native-source",
                    "fork_source_backend": "opencode",
                },
            },
        },
    )

    assert "This Agent Session was forked from `sessource`." in out
    assert "The authoritative Avibe session id for this fork is `sestarget`." in out


# ------------------------------------------------ live session.activity endpoint


def test_cli_activity_endpoint_publishes_with_token(monkeypatch):
    # The publish-only endpoint the CLI pings after vibe session update: with a valid
    # local CLI token it re-reads the row and broadcasts session.activity 'updated'.
    import vibe.sse_broker as sse_broker
    from core.show_pages import SHOW_CLI_EVENT_TOKEN_HEADER, show_cli_event_token
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from vibe.ui_server import app

    ensure_sqlite_state(primary_platform="avibe")  # conftest already isolated the home
    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    _seed(engine, "seslive", title="Renamed", title_source="agent")

    published: list = []
    monkeypatch.setattr(sse_broker.broker, "publish", lambda topic, data: published.append((topic, data)))

    resp = app.test_client().post(
        "/api/sessions/seslive/cli-activity",
        json={},
        headers={"X-Vibe-Show-Client": "cli", SHOW_CLI_EVENT_TOKEN_HEADER: show_cli_event_token()},
    )
    assert resp.status_code == 200
    events = [data for topic, data in published if topic == "session.activity"]
    assert events and events[0]["session_id"] == "seslive"
    assert events[0]["event"] == "updated" and events[0]["title"] == "Renamed"


def test_cli_activity_placement_events_match_patch(monkeypatch):
    import vibe.sse_broker as sse_broker
    from core.services import sessions as sessions_service
    from core.show_pages import SHOW_CLI_EVENT_TOKEN_HEADER, show_cli_event_token
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from tests.ui_server_test_helpers import csrf_headers
    from vibe.ui_server import app

    ensure_sqlite_state(primary_platform="avibe")
    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    scope_id = _seed(engine, "sesplacement", title="Placement")
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        sse_broker.broker,
        "publish",
        lambda topic, data: published.append((topic, data)),
    )

    client = app.test_client()
    response = client.patch(
        "/api/sessions/sesplacement",
        json={"visibility": "background"},
        headers=csrf_headers(client),
    )
    assert response.status_code == 200
    patch_events = [data for topic, data in published if topic == "session.activity"]

    with engine.begin() as conn:
        sessions_service.update_session(
            conn,
            "sesplacement",
            visibility="foreground",
        )
        previous = sessions_service.get_session(conn, "sesplacement")
        sessions_service.update_session(
            conn,
            "sesplacement",
            visibility="background",
        )
    published.clear()

    response = client.post(
        "/api/sessions/sesplacement/cli-activity",
        json={
            "previous_scope_id": previous["scope_id"],
            "previous_visibility": previous["visibility"],
        },
        headers={
            "X-Vibe-Show-Client": "cli",
            SHOW_CLI_EVENT_TOKEN_HEADER: show_cli_event_token(),
        },
    )
    assert response.status_code == 200
    cli_events = [data for topic, data in published if topic == "session.activity"]

    assert previous["scope_id"] == scope_id
    assert cli_events == patch_events


def test_cli_activity_endpoint_rejects_without_token(monkeypatch):
    from tests.ui_server_test_helpers import csrf_headers
    from vibe.ui_server import app

    client = app.test_client()
    # Valid CSRF (passes the mutation guard) but NO local CLI token -> handler 403.
    resp = client.post("/api/sessions/whatever/cli-activity", json={}, headers=csrf_headers(client))
    assert resp.status_code == 403


def test_backfill_does_not_overwrite_agent_title(monkeypatch, tmp_path):
    # Backend auto-fill must never clobber a deliberately agent-set title.
    from core.services import sessions as sessions_service

    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesagent", title="Kept", title_source="agent")
    with engine.begin() as conn:
        result = sessions_service.backfill_session_title(
            conn, "sesagent", title="Auto Title", backend="claude"
        )
    assert result is None  # skipped
    with engine.connect() as conn:
        from sqlalchemy import select

        title = conn.execute(select(agent_sessions.c.title).where(agent_sessions.c.id == "sesagent")).scalar_one()
    assert title == "Kept"
