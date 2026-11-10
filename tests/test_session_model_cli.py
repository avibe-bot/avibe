"""Seeing and switching a Session's model, reasoning effort, and speed from the CLI.

The menu is the Web route picker's (``hubCatalogModels`` in ``ui/src/lib/backendModels.ts``), projected from Model
Hub's Agent payload; a change is the same ``update_session`` write the Web PATCH makes, applied from the next Turn.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import paths
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions
from storage.settings_service import upsert_scope
from tests.cli_control_support import lent
from vibe import api, cli

pytestmark = pytest.mark.usefixtures("in_process_cli_control")


def _hub_row(model_id, *, efforts=("low", "medium", "high"), omitted=(), fast=False, confirmed=None, **extra):
    return {
        "id": model_id,
        "display_name": extra.pop("display_name", None),
        "routeable": extra.pop("routeable", True),
        "supports_reasoning": extra.pop("supports_reasoning", True),
        "reasoning_efforts": list(efforts),
        "effort_menu": {"omitted": list(omitted), "not_on": {}},
        "fast_mode": fast,
        "fast_mode_confirmed": confirmed,
        **extra,
    }


HUB = {
    "vibey": {
        "mode": "hub",
        "catalog_models": [
            _hub_row("claude-opus-5-5", display_name="Claude Opus 5.5", efforts=("low", "high", "max"), fast=True),
            _hub_row("gpt-6.1-sol", efforts=("low", "medium", "high", "xhigh"), omitted=("xhigh",), fast=True,
                     confirmed=False),
            _hub_row("grok/grok-4.6", efforts=("low", "high")),
            _hub_row("plain-model", supports_reasoning=False),
            _hub_row("hidden-model", routeable=False),
            _hub_row("claude-opus-5-5", efforts=("low",)),
        ],
    },
    "claude": {
        "mode": "hub",
        "catalog_models": [_hub_row("claude-opus-5-5", fast=True), _hub_row("claude-sonnet-5-5")],
    },
}


def _hub_reader(backend):
    return HUB[backend]


def test_route_menu_mirrors_the_web_picker_projection():
    menu = api.route_menu_options("vibey", hub_agent_reader=_hub_reader)

    assert menu["authoritative"] is True and menu["mode"] == "hub"
    assert menu["models"] == [
        {"value": "claude-opus-5-5", "label": "Claude Opus 5.5", "reasoning_efforts": ["low", "high", "max"],
         "fast": "offered"},
        # An effort every hop refuses is left out; a fast response that did not confirm the tier marks it.
        {"value": "gpt-6.1-sol", "label": "gpt-6.1-sol", "reasoning_efforts": ["low", "medium", "high"],
         "fast": "unconfirmed"},
        {"value": "grok/grok-4.6", "label": "grok/grok-4.6", "reasoning_efforts": ["low", "high"]},
        # A model that does not reason takes no effort; unrouteable and repeated ids are not menu entries.
        {"value": "plain-model", "label": "plain-model", "reasoning_efforts": []},
    ]


@pytest.mark.parametrize("reader", [lambda backend: {"mode": "direct", "catalog_models": []}, None])
def test_route_menu_without_a_hub_answer_uses_the_backend_catalog_without_fast(monkeypatch, reader):
    def unreachable(backend):
        raise RuntimeError("engine_down")

    monkeypatch.setattr(
        api,
        "agent_model_options",
        lambda backend, **kwargs: {"ok": True, "backend": backend, "models": [
            {"value": "claude-opus-5-5", "reasoning_efforts": ["high"]}]},
    )
    menu = api.route_menu_options("claude", hub_agent_reader=reader or unreachable)

    assert menu["authoritative"] is False
    assert [entry.get("fast") for entry in menu["models"]] == [None]


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)
    paths.ensure_data_dirs()
    ensure_sqlite_state(primary_platform="avibe")
    monkeypatch.setattr(cli, "_post_session_activity_to_live_ui", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_remote_hub_agent_reader", lambda: _hub_reader)
    return create_sqlite_engine(paths.get_sqlite_state_path())


def _seed(engine, sid, *, backend="vibey", model="claude-opus-5-5", effort="high", speed=None, running=False,
          agent_name=None, metadata=None):
    now = "2026-11-10T09:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj", now=now)
        conn.execute(
            agent_sessions.insert().values(
                id=sid, scope_id=scope_id, agent_id="agent_" + sid, agent_name=agent_name or backend, agent_backend=backend,
                agent_variant=backend, session_anchor="anc_" + sid, native_session_id="nat_" + sid, status="active",
                visibility="foreground", agent_status="running" if running else "idle", model=model,
                reasoning_effort=effort,
                metadata_json=json.dumps({**({"speed": speed} if speed else {}), **(metadata or {})}),
                created_at=now, updated_at=now, last_active_at=now,
            )
        )


def _row(engine, sid):
    with engine.connect() as conn:
        row = conn.execute(agent_sessions.select().where(agent_sessions.c.id == sid)).mappings().one()
    return row["model"], row["reasoning_effort"], json.loads(row["metadata_json"] or "{}").get("speed")


def _run(cmd, argv, capsys):
    code = cmd(cli.build_parser().parse_args(argv))
    captured = capsys.readouterr()
    return code, json.loads(captured.out if code == 0 else captured.err)


async def _read_agent_sources(backend):
    return _hub_reader(backend)


def _update(capsys, sid, *flags):
    # The service reads the menu with ``read_agent_sources`` on its loop, which owns Model Hub's recovery state.
    with patch.object(lent, "model_hub_service", SimpleNamespace(read_agent_sources=_read_agent_sources)):
        return _run(cli.cmd_session_update, ["session", "update", "--session-id", sid, *flags], capsys)


@pytest.mark.parametrize("running", [False, True])
def test_update_switches_model_effort_and_speed_from_the_next_turn(monkeypatch, tmp_path, capsys, running):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", running=running)

    code, payload = _update(
        capsys, "sesaaa", "--model", "gpt-6.1-sol", "--reasoning-effort", "medium", "--speed", "fast"
    )

    assert code == 0, payload
    assert payload["applies_from"] == "next_turn"
    assert payload["turn_running"] is running
    assert payload["session"]["model"] == "gpt-6.1-sol" and payload["session"]["speed"] == "fast"
    assert _row(engine, "sesaaa") == ("gpt-6.1-sol", "medium", "fast")
    assert any("did not confirm" in note for note in payload["notes"])

    code, payload = _update(capsys, "sesaaa", "--speed", "standard")
    assert code == 0 and _row(engine, "sesaaa") == ("gpt-6.1-sol", "medium", None)


def test_model_change_clears_an_effort_the_new_model_does_not_offer(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", effort="max", speed="fast")

    code, payload = _update(capsys, "sesaaa", "--model", "grok/grok-4.6")

    assert code == 0, payload
    assert _row(engine, "sesaaa") == ("grok/grok-4.6", None, "fast")
    assert any("'max' is not offered" in note for note in payload["notes"])
    assert any("standard tier" in note for note in payload["notes"])


@pytest.mark.parametrize(
    ("backend", "flags", "code"),
    [
        ("vibey", ["--model", "not-on-menu"], "session_model_unavailable"),
        ("vibey", ["--reasoning-effort", "xhigh"], "session_effort_unavailable"),
        ("vibey", ["--model", "grok/grok-4.6", "--speed", "fast"], "fast_unavailable"),
        ("claude", ["--model", "gpt-6.1-sol"], "model_family_unsupported"),
    ],
)
def test_update_refuses_what_the_menu_does_not_offer(monkeypatch, tmp_path, capsys, backend, flags, code):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", backend=backend)

    status, payload = _update(capsys, "sesaaa", *flags)

    assert status == 1 and payload["code"] == code
    assert payload["hint"]
    assert _row(engine, "sesaaa") == ("claude-opus-5-5", "high", None)


@pytest.mark.parametrize(
    ("caller", "allowed"),
    [(None, True), ("ses_main", True), ("sesother", False)],
)
def test_a_bot_session_changes_only_for_its_user_or_itself(monkeypatch, tmp_path, capsys, caller, allowed):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "ses_main")
    _seed(engine, "sesother")
    if caller:
        monkeypatch.setenv("AVIBE_SESSION_ID", caller)

    status, payload = _update(capsys, "ses_main", "--reasoning-effort", "low")

    if allowed:
        assert status == 0, payload
        assert _row(engine, "ses_main")[1] == "low"
    else:
        assert status == 1 and payload["code"] == "bot_model_is_users"
        assert _row(engine, "ses_main")[1] == "high"
        status, payload = _run(cli.cmd_session_models, ["session", "models", "--session-id", "ses_main"], capsys)
        assert status == 0 and payload["changeable"] is False and payload["reason"] == "bot_model_is_users"


def test_session_models_shows_current_settings_and_the_menu(monkeypatch, tmp_path, capsys):
    engine = _setup(monkeypatch, tmp_path)
    _seed(engine, "sesaaa", backend="claude", effort=None, speed="fast", running=True)

    code, payload = _run(cli.cmd_session_models, ["session", "models", "--session-id", "sesaaa"], capsys)

    assert code == 0, payload
    assert payload["kind"] == "session_models" and payload["backend"] == "claude"
    assert payload["changeable"] is True
    current = payload["current"]
    assert (current["model"], current["speed"], current["turn_running"]) == ("claude-opus-5-5", "fast", True)
    assert current["from"] == {"model": "session", "reasoning_effort": "agent"}
    assert [(entry["value"], entry.get("fast")) for entry in payload["models"]] == [
        ("claude-opus-5-5", "offered"), ("claude-sonnet-5-5", None),
    ]
    assert any("only Claude models" in note for note in payload["notes"])
    assert payload["pagination"]["returned"] == 2


def test_agent_run_speed_applies_only_to_a_new_session(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)

    args = cli.build_parser().parse_args(
        ["agent", "run", "--session-id", "sesaaa", "--speed", "fast", "--message", "hi"]
    )
    with pytest.raises(cli.TaskCliError) as refused:
        cli._validate_run_session_policy(args, help_command="vibe agent run --help")
    assert refused.value.code == "model_override_with_existing_session"

    with pytest.raises(cli.TaskCliError) as refused:
        cli._session_model_override_fields(
            backend="vibey", agent_label="vibey", model="grok/grok-4.6", reasoning_effort="low",
            speed="fast", hub_agent_reader=_hub_reader, help_command="vibe agent run --help",
        )
    assert refused.value.code == "fast_unavailable"
    assert cli._session_model_override_fields(
        backend="vibey", agent_label="vibey", model="claude-opus-5-5", reasoning_effort="high",
        speed="fast", hub_agent_reader=_hub_reader, help_command="vibe agent run --help",
    ) == {}


def test_session_models_reports_an_explicitly_pinned_null_as_the_sessions_own(monkeypatch, tmp_path, capsys):
    from storage.session_reclaim import SESSION_SETTINGS_OVERRIDE_KEY

    engine = _setup(monkeypatch, tmp_path)
    cli._agent_store().create(name="tuned", backend="vibey", model="gpt-6.1-sol", reasoning_effort="high")
    _seed(engine, "sespin", agent_name="tuned", model="claude-opus-5-5", effort=None,
          metadata={SESSION_SETTINGS_OVERRIDE_KEY: ["reasoning_effort"]})
    _seed(engine, "sesinh", agent_name="tuned", model=None, effort=None)

    _, pinned = _run(cli.cmd_session_models, ["session", "models", "--session-id", "sespin"], capsys)
    _, inherited = _run(cli.cmd_session_models, ["session", "models", "--session-id", "sesinh"], capsys)

    assert pinned["current"]["reasoning_effort"] is None
    assert pinned["current"]["from"] == {"model": "session", "reasoning_effort": "session"}
    assert (inherited["current"]["model"], inherited["current"]["reasoning_effort"]) == ("gpt-6.1-sol", "high")
    assert inherited["current"]["from"] == {"model": "agent", "reasoning_effort": "agent"}
