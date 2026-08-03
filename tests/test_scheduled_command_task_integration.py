"""Cross-layer coverage for scheduled command tasks: CLI create -> executor run.

`tests/test_cli_task_command.py` proves the CLI writes the right definition and
`tests/test_scheduled_tasks.py` proves the executor runs one, but both build their
fixtures with hand-written `add_task` payloads. A disagreement between the two
layers — a field type, or `on_failure` living somewhere the executor does not read —
passes both suites and only fails once a real cron task fires. These tests drive the
real `cmd_task_add` and the real claimed-request path so that seam stays covered.

Hermetic: state, definition store, and the fallback spawn cwd all land in `tmp_path`.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config.paths as paths
from core.caller_context import (
    AVIBE_CALLER_BACKEND_ENV,
    AVIBE_CALLER_CHANNEL_ID_ENV,
    AVIBE_CALLER_MESSAGE_ID_ENV,
    AVIBE_CALLER_PLATFORM_ENV,
    AVIBE_CALLER_SESSION_KEY_ENV,
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_CALLER_USER_ID_ENV,
    AVIBE_CALLER_WORKSPACE_ID_ENV,
    AVIBE_NATIVE_SESSION_ID_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_SESSION_ID_ENV,
)
from core.scheduled_tasks import (
    ScheduledTaskService,
    ScheduledTaskStore,
    TaskExecutionStore,
)
from vibe import cli

_CALLER_ENV_VARS = (
    AVIBE_SESSION_ID_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_CALLER_BACKEND_ENV,
    AVIBE_NATIVE_SESSION_ID_ENV,
    AVIBE_CALLER_PLATFORM_ENV,
    AVIBE_CALLER_USER_ID_ENV,
    AVIBE_CALLER_CHANNEL_ID_ENV,
    AVIBE_CALLER_SESSION_KEY_ENV,
    AVIBE_CALLER_MESSAGE_ID_ENV,
    AVIBE_CALLER_WORKSPACE_ID_ENV,
)


def _isolate(tmp_path: Path, monkeypatch) -> None:
    """Point every write-capable path at ``tmp_path`` and clear caller context.

    The caller vars matter: this suite may itself run inside an Avibe Agent shell,
    where an ambient ``AVIBE_SESSION_ID`` would otherwise reach
    ``_apply_caller_session_default``. A pure command task must be creatable there,
    so leaving them set would test the wrong thing.
    """

    db_path = tmp_path / "state" / "vibe.sqlite"
    monkeypatch.setattr(paths, "get_state_dir", lambda: db_path.parent)
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / "avibe_home")

    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state(db_path=db_path, primary_platform="slack")
    for var in _CALLER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _store(tmp_path: Path) -> ScheduledTaskStore:
    return ScheduledTaskStore(tmp_path / "scheduled_tasks.json")


def _service(tmp_path: Path, calls: list) -> ScheduledTaskService:
    """A service on the SQLite request store, mirroring the executor suite's fixture."""

    async def _handle_scheduled_message(context, message, parsed_session_key=None):
        calls.append(message)
        return None

    settings_manager = SimpleNamespace(
        get_store=lambda: SimpleNamespace(get_user=lambda *_a, **_kw: None)
    )
    controller = SimpleNamespace(
        platform_settings_managers={"slack": settings_manager},
        message_handler=SimpleNamespace(
            handle_scheduled_message=_handle_scheduled_message
        ),
    )
    service = ScheduledTaskService(
        controller=controller,
        store=_store(tmp_path),
        request_store=TaskExecutionStore(),
    )
    service.scheduler = SimpleNamespace(
        get_job=lambda *_a, **_kw: None,
        add_job=lambda *_a, **_kw: None,
        remove_job=lambda *_a, **_kw: None,
        get_jobs=lambda *_a, **_kw: [],
        running=True,
    )
    return service


def _add_via_cli(tmp_path: Path, argv: list[str]) -> None:
    args = cli.build_parser().parse_args(["task", "add", *argv])
    with patch("vibe.cli._task_store", return_value=_store(tmp_path)):
        assert cli.cmd_task_add(args) == 0, "the CLI refused to create the command task"


def _fire(service: ScheduledTaskService, task) -> dict:
    queued = service.request_store.enqueue_task_run(
        task.id, source_kind="scheduler", task=task
    )
    claimed = service.request_store.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))
    run = service.request_store.get_run(queued.id)
    assert run is not None
    return run


def test_cli_created_command_task_runs_with_no_agent_turn(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The headline promise: a cron task that executes and never involves an Agent."""

    _isolate(tmp_path, monkeypatch)
    marker = tmp_path / "ran.txt"
    _add_via_cli(
        tmp_path,
        [
            "--name",
            "nightly-sync",
            "--cron",
            "0 3 * * *",
            "--shell",
            f"echo fired > {marker}; exit 0",
            "--cwd",
            str(tmp_path),
        ],
    )
    capsys.readouterr()

    task = _store(tmp_path).list_tasks()[0]
    assert task.has_command, "the CLI-written definition is not seen as a command task"
    assert task.on_failure == "none", f"on_failure did not survive: {task.on_failure!r}"
    assert not task.session_id and not task.session_key, (
        "a pure command task was given an Agent session; it must have none"
    )

    calls: list = []
    run = _fire(_service(tmp_path, calls), task)

    assert run["status"] == "succeeded", f"the run failed: {run['error']!r}"
    assert run["exit_code"] == 0, f"the run row lost the exit code: {run['exit_code']!r}"
    assert marker.read_text().strip() == "fired", "the command never actually ran"
    assert calls == [], f"a pure command task dispatched an Agent turn: {calls!r}"
    assert _store(tmp_path).get_task(task.id).last_exit_code == 0


def test_cli_created_command_task_records_a_nonzero_exit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A failing command must surface on the run row AND the definition."""

    _isolate(tmp_path, monkeypatch)
    _add_via_cli(
        tmp_path,
        [
            "--cron",
            "0 3 * * *",
            "--shell",
            "echo boom >&2; exit 7",
            "--cwd",
            str(tmp_path),
            "--timeout",
            "30",
        ],
    )
    capsys.readouterr()

    task = _store(tmp_path).list_tasks()[0]
    assert task.timeout_seconds == 30, (
        f"--timeout did not reach the definition: {task.timeout_seconds!r}"
    )

    calls: list = []
    run = _fire(_service(tmp_path, calls), task)

    assert run["status"] == "failed", f"a nonzero exit did not fail the run: {run['status']!r}"
    assert run["exit_code"] == 7, f"the run row lost the exit code: {run['exit_code']!r}"
    assert "boom" in (run["stderr"] or ""), f"stderr was not persisted: {run['stderr']!r}"
    assert calls == [], "a failing pure command task must still not dispatch an Agent turn"
    assert _store(tmp_path).get_task(task.id).last_exit_code == 7
    # SCT-019: the run records WHAT it ran. The definition is editable and deletable,
    # so a reader that goes back to it can be told about a command that never ran.
    assert run["metadata"]["command"] == {"shell": "echo boom >&2; exit 7", "argv": []}, (
        f"the run row carries no command snapshot: {run['metadata']!r}"
    )


def test_a_fire_with_no_exit_code_clears_the_one_the_last_fire_left(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """SCT-017 -- ``last_exit_code`` must describe THIS fire or nothing.

    Some fires never reach a process: a working directory that no longer exists, a
    supervisor that dies during startup. They have no exit code at all, and the notice
    is careful never to invent one. The definition row was not: the stamp only wrote
    ``last_exit_code`` when it had a value, so the code from the LAST fire stayed on
    the row and the Harness pane and ``vibe task list`` showed "exited 7" beside a
    failure that never ran a command -- a fabricated fact, and a misleading one,
    because 7 was a real status the same definition really produced once.

    A message task is the reason the write is not unconditional: it has no exit code
    to report and must not blank a command fire's. So the command fire's own stamp is
    the one authorised to clear it.
    """

    _isolate(tmp_path, monkeypatch)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    _add_via_cli(
        tmp_path,
        [
            "--cron",
            "0 3 * * *",
            "--shell",
            "exit 7",
            "--cwd",
            str(workdir),
            "--timeout",
            "30",
        ],
    )
    capsys.readouterr()

    task = _store(tmp_path).list_tasks()[0]
    first = _fire(_service(tmp_path, []), task)
    assert first["exit_code"] == 7
    assert _store(tmp_path).get_task(task.id).last_exit_code == 7

    # The directory disappears between fires: the next fire cannot spawn, so it has no
    # status of its own to report.
    workdir.rmdir()
    second = _fire(_service(tmp_path, []), task)

    assert second["status"] == "failed"
    assert "working directory does not exist" in (second["error"] or "")
    assert second["exit_code"] is None, (
        f"a fire that never spawned must not carry an exit code: {second['exit_code']!r}"
    )
    stored = _store(tmp_path).get_task(task.id)
    assert stored.last_exit_code is None, (
        "the definition kept the previous fire's exit code beside a failure that "
        f"never ran a command: {stored.last_exit_code!r}"
    )
    assert stored.last_error and "working directory does not exist" in stored.last_error


def test_cli_created_argv_command_task_runs(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The ``-- argv`` form must survive the CLI pre-parse and execute as a list."""

    _isolate(tmp_path, monkeypatch)
    marker = tmp_path / "argv.txt"
    _add_via_cli(
        tmp_path,
        [
            "--cron",
            "0 3 * * *",
            "--cwd",
            str(tmp_path),
            "--",
            "/bin/sh",
            "-c",
            f"echo argv > {marker}",
        ],
    )
    capsys.readouterr()

    task = _store(tmp_path).list_tasks()[0]
    assert task.command == ["/bin/sh", "-c", f"echo argv > {marker}"], (
        f"the argv list did not round-trip: {task.command!r}"
    )
    assert not task.shell_command, "an argv task must not also store a shell command"

    run = _fire(_service(tmp_path, []), task)

    assert run["status"] == "succeeded", f"the argv run failed: {run['error']!r}"
    assert marker.read_text().strip() == "argv"
