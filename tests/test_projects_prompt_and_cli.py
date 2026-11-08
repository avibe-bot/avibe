from __future__ import annotations

import pytest

from modules.agents.vibey.prompt_snapshot import Component, component_tier
from core.system_prompt_injection import BotPromptInfo, build_system_prompt_blocks
from modules.im import MessageContext
from storage import projects_service, workbench_sessions_service
from storage.agent_session_rows import create_agent_session_row
from storage.importer import ensure_sqlite_state
from storage.db import create_sqlite_engine
from vibe import cli
from vibe.cli import build_parser


@pytest.fixture
def engine():
    ensure_sqlite_state()
    return create_sqlite_engine()


def _context() -> MessageContext:
    return MessageContext(
        user_id="user",
        channel_id="channel",
        platform="avibe",
        platform_specific={"agent_session_id": "ses-projects"},
    )


def test_project_description_round_trips_and_resolves_by_name_or_id(engine, tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    with engine.begin() as conn:
        first = projects_service.create_project(conn, str(first_dir), display_name="First", description="  Long   work ")
        second = projects_service.create_project(conn, str(second_dir), display_name="Second", description="Short")
        projects_service.update_project(conn, first["id"], description="Updated description")
    with engine.connect() as conn:
        listed = projects_service.list_enabled_projects_for_prompt(conn)
        assert listed == [
            {"name": "First", "description": "Updated description"},
            {"name": "Second", "description": "Short"},
        ]
        resolved = projects_service.resolve_project_target(conn, "First")
        assert resolved["id"] == first["id"]
        assert resolved["scope_id"] == first["scope_id"]
        assert resolved["workdir"] == str(first_dir.resolve())
        assert projects_service.resolve_project_target(conn, second["id"])["id"] == second["id"]


def test_project_resolver_preserves_internal_name_whitespace(engine, tmp_path):
    folder = tmp_path / "spaced"
    collapsed_folder = tmp_path / "collapsed"
    folder.mkdir()
    collapsed_folder.mkdir()
    with engine.begin() as conn:
        spaced = projects_service.create_project(conn, str(folder), display_name="Demo  App")
        collapsed = projects_service.create_project(conn, str(collapsed_folder), display_name="Demo App")
    with engine.connect() as conn:
        assert projects_service.resolve_project_target(conn, "  Demo  App  ")["id"] == spaced["id"]
        assert projects_service.resolve_project_target(conn, "Demo App")["id"] == collapsed["id"]


    folder = tmp_path / "disabled"
    folder.mkdir()
    with engine.begin() as conn:
        project = projects_service.create_project(conn, str(folder), display_name="Disabled")
        projects_service.archive_project(conn, project["id"])
    with engine.connect() as conn:
        with pytest.raises(LookupError, match="Project is disabled: Disabled"):
            projects_service.resolve_project_target(conn, "Disabled")
        assert projects_service.resolve_project_target(conn, "Disabled", active_only=False)["id"] == project["id"]


def test_agent_run_project_resolves_scope_and_workdir_and_reports_unknown(engine, tmp_path, monkeypatch):
    folder = tmp_path / "project"
    folder.mkdir()
    with engine.begin() as conn:
        project = projects_service.create_project(conn, str(folder), display_name="Demo")
    monkeypatch.setattr(cli, "_projects_cli_engine", lambda **_kwargs: engine)
    args = build_parser().parse_args(["agent", "run", "--agent", "default", "--project", "Demo", "--message", "Ship it"])

    assert cli._resolve_agent_run_scope_key(args, caller_context=None, source_session_id=None) == project["scope_id"]
    assert args._resolved_project_workdir == str(folder.resolve())

    unknown = build_parser().parse_args(["agent", "run", "--agent", "default", "--project", "Missing", "--message", "Ship it"])
    with pytest.raises(cli.TaskCliError, match="Project not found: Missing"):
        cli._resolve_agent_run_scope_key(unknown, caller_context=None, source_session_id=None)


def test_agent_run_project_rejects_fork_placement():
    args = build_parser().parse_args(
        [
            "agent",
            "run",
            "--agent",
            "default",
            "--fork-session",
            "ses-source",
            "--project",
            "Demo",
            "--message",
            "Ship it",
        ]
    )
    with pytest.raises(cli.TaskCliError, match="--project cannot be combined with a fork"):
        cli._validate_run_session_policy(args, help_command="vibe agent run --help")


def test_project_show_includes_background_sessions(engine, tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    with engine.begin() as conn:
        project = projects_service.create_project(conn, str(folder), display_name="Demo")
        session_id = create_agent_session_row(
            conn,
            scope_id=project["scope_id"],
            session_anchor="agent-run",
            agent_backend="codex",
            workdir=str(folder),
            visibility="background",
            title="Background work",
        )
        sessions = workbench_sessions_service.list_project_sessions_for_cli(
            conn,
            scope_id=project["scope_id"],
            authorization_context=None,
        )

    assert [session["id"] for session in sessions] == [session_id]
    assert sessions[0]["visibility"] == "background"


def test_projects_prompt_is_bot_only_ordered_and_path_free():
    bot = BotPromptInfo("main", "", "", "ses-projects", builtin=True)
    projects = [
        {"name": "First", "description": "Long work"},
        {"name": "Second", "description": "Short work"},
    ]
    bot_blocks = build_system_prompt_blocks(context=_context(), bot=bot, projects=projects)
    worker_blocks = build_system_prompt_blocks(context=_context(), projects=projects)
    bot_text = "".join(block.text for block in bot_blocks if block.module_id == "projects-prompt")
    assert "Lasting work lives in Projects." in bot_text
    assert bot_text.index("First | Long work") < bot_text.index("Second | Short work")
    assert "/tmp" not in bot_text
    assert not any(block.module_id == "projects-prompt" for block in worker_blocks)


def test_projects_prompt_pages_and_is_tier_two():
    bot = BotPromptInfo("main", "", "", "ses-projects", builtin=True)
    projects = [{"name": f"Project {index}", "description": "x" * 500} for index in range(60)]
    blocks = build_system_prompt_blocks(context=_context(), bot=bot, projects=projects)
    text = next(block.text for block in blocks if block.module_id == "projects-prompt")
    assert "More Projects are available." in text
    assert component_tier(None, Component("projects-prompt", text)) == 2


def test_project_cli_parser_round_trips_project_and_agent_targets():
    parser = build_parser()
    create = parser.parse_args(["project", "create", "Demo", "--description", "Long work"])
    update = parser.parse_args(["project", "update", "Demo", "--description", "Updated"])
    run = parser.parse_args(["agent", "run", "--agent", "default", "--project", "Demo", "--message", "Ship it"])
    assert (create.project_command, create.name, create.description) == ("create", "Demo", "Long work")
    assert (update.project_command, update.name, update.description) == ("update", "Demo", "Updated")
    assert (run.agent_command, run.project, run.message) == ("run", "Demo", "Ship it")
