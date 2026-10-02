"""Watch target kind ``job`` (plan C-7 section 5.3, ``recovery.md`` J3 and J6).

Every job here is a real ``LocalJobHost`` job in a temporary directory; the Watch
service builds its own host over the same directory, as it does after a restart.
Stores default to SQLite under the per-test ``AVIBE_HOME`` (``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from sqlalchemy import insert

from config import paths
from core import watches as watches_module
from core.agent_core.tools.jobs import LocalJobHost
from core.scheduled_tasks import TaskExecutionStore
from core.watches import (
    JobWatchUpdateRefused,
    ManagedWatchService,
    ManagedWatchStore,
    WatchRuntimeStateStore,
    hand_over_job,
    stop_all_jobs,
)
from storage.background import SQLiteBackgroundTaskStore
from storage.models import agent_sessions
from storage.session_reclaim import reclaim_bound_definitions
from vibe import cli
from vibe.i18n import t as i18n_t
from vibe.ui_server import app as ui_app

from tests.ui_server_test_helpers import csrf_headers

SESSION_ID = "ses_job_watch"


@pytest.fixture(autouse=True)
def _fast_watch_loop(monkeypatch):
    monkeypatch.setattr(watches_module, "WATCH_RECONCILE_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(watches_module, "WATCH_JOB_POLL_SECONDS", 0.05)


@pytest.fixture(autouse=True)
def _job_session():
    """The live Session the jobs run in: a hand-over binds its Watch to it."""

    now = datetime.now(timezone.utc).isoformat()
    with SQLiteBackgroundTaskStore().engine.begin() as conn:
        conn.execute(
            insert(agent_sessions).values(
                id=SESSION_ID,
                agent_backend="avibe",
                agent_variant="default",
                session_anchor=SESSION_ID,
                native_session_id=SESSION_ID,
                status="active",
                visibility="foreground",
                pinned=0,
                agent_status="idle",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
            )
        )


def _jobs_dir(tmp_path: Path) -> str:
    return str(tmp_path / "jobs")


def _host(tmp_path: Path, store: ManagedWatchStore) -> LocalJobHost:
    """The host the adapter runs: its hand-over is Watch's adopt-or-create."""

    return LocalJobHost(_jobs_dir(tmp_path), on_hand_over=partial(hand_over_job, store=store))


async def _start_job(host: LocalJobHost, cwd: Path, command: str, *, timeout_s=None, tool_call_id="toolu_1") -> str:
    return await host.start(
        command,
        cwd=str(cwd),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        timeout_s=timeout_s,
        session_id=SESSION_ID,
        tool_call_id=tool_call_id,
    )


def _service(store: ManagedWatchStore) -> ManagedWatchService:
    return ManagedWatchService(
        controller=SimpleNamespace(),
        store=store,
        request_store=TaskExecutionStore(),
        runtime_store=WatchRuntimeStateStore(),
    )


async def _start_service(service: ManagedWatchService) -> None:
    service.start()
    if service._startup_task is not None:
        await service._startup_task


async def _until(predicate, message: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


def _follow_ups(watch_id: str) -> list:
    return [request for request in TaskExecutionStore().list_pending() if request.task_id == watch_id]


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _sql(statement: str, *params) -> list[tuple]:
    with closing(sqlite3.connect(paths.get_sqlite_state_path())) as conn, conn:
        return conn.execute(statement, params).fetchall()


def _settled_row(watch_id: str) -> dict:
    """The Watch row as the Harness shows it once its follow-up turn is done."""

    _sql(
        "UPDATE agent_runs SET status = 'succeeded', completed_at = updated_at "
        "WHERE definition_id = ? AND run_type = 'watch'",
        watch_id,
    )
    return SQLiteBackgroundTaskStore().get_watch(watch_id)


def _released(watch_id: str) -> bool:
    return all(row["id"] != watch_id for row in SQLiteBackgroundTaskStore().list_job_watch_targets())


def test_restart_while_a_job_watch_waits_reattaches_and_reports_once(tmp_path: Path) -> None:
    release = tmp_path / "release"
    command = f"while [ ! -e {release} ]; do sleep 0.05; done; echo finished; exit 3"

    async def run() -> tuple[str, dict]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, command)
        watch_id = await host.hand_over(job_id)

        first = _service(store)
        await _start_service(first)
        await _until(lambda: first.store.get_watch(watch_id).last_started_at, "the job Watch never started waiting")
        assert not store.job_watch_settled(job_id)
        # What a crash leaves behind for the next service's stale-worker reaping.
        crashed_runtime = first._runtime_state_payload()
        await first.stop()
        WatchRuntimeStateStore().write(crashed_runtime)
        assert host.status(job_id).state == "running"

        second_store = ManagedWatchStore()
        second = _service(second_store)
        await _start_service(second)
        await asyncio.sleep(0.2)
        assert host.status(job_id).state == "running", "restart recovery touched the job"
        release.touch()
        await _until(lambda: not second_store.get_watch(watch_id).enabled, "the re-attached Watch never reported")
        await asyncio.sleep(0.2)  # a second report would land here
        await second.stop()
        # J5: the job's files may go once its tool call has settled too.
        assert second_store.job_watch_settled(job_id)
        return watch_id, crashed_runtime

    watch_id, crashed_runtime = asyncio.run(run())

    # The job Watch's runtime entry names no process, so reaping has nothing to kill.
    assert crashed_runtime["watches"][watch_id]["pid"] is None
    follow_ups = _follow_ups(watch_id)
    assert len(follow_ups) == 1
    prompt = follow_ups[0].prompt
    assert f"Command: {command}" in prompt
    assert "\nfinished\n" in prompt
    assert prompt.endswith("Command exited with code 3")
    assert follow_ups[0].session_id == SESSION_ID


def _end_by_remove(store: ManagedWatchStore, watch_id: str) -> None:
    assert store.remove_watch(watch_id)


def _end_by_pause(store: ManagedWatchStore, watch_id: str) -> None:
    store.set_enabled(watch_id, False)


def _archive_session() -> None:
    """The archive dialog's teardown: the Session is archived and its Watches soft-deleted."""

    with SQLiteBackgroundTaskStore().engine.begin() as conn:
        conn.execute(agent_sessions.update().where(agent_sessions.c.id == SESSION_ID).values(status="archived"))
        reclaim_bound_definitions(conn, SESSION_ID, mode="delete")
    watches_module._publish_watch_definitions_updated()


def _end_by_archive(store: ManagedWatchStore, watch_id: str) -> None:
    _archive_session()


def _end_by_new(store: ManagedWatchStore, watch_id: str) -> None:
    # ``/new``'s hard delete: the Session's Watches are paused and its row removed, in one transaction.
    with SQLiteBackgroundTaskStore().engine.begin() as conn:
        reclaim_bound_definitions(conn, SESSION_ID, mode="pause")
        conn.execute(agent_sessions.delete().where(agent_sessions.c.id == SESSION_ID))
    watches_module._publish_watch_definitions_updated()


@pytest.mark.parametrize(
    ("end", "reason"),
    [
        (_end_by_remove, "watch_removed"),
        (_end_by_pause, "watch_disabled"),
        (_end_by_archive, "session_archived"),
        (_end_by_new, "watch_disabled"),
    ],
    ids=["remove", "pause", "archive", "new"],
)
def test_ending_ownership_kills_the_job_process_tree(tmp_path: Path, end, reason: str) -> None:
    child_pid_file = tmp_path / "child.pid"
    command = f"sleep 60 & echo $! > {child_pid_file}; wait"

    async def run() -> tuple[LocalJobHost, str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, command)
        watch_id = await host.hand_over(job_id)
        service = _service(store)
        await _start_service(service)
        await _until(lambda: child_pid_file.exists() and child_pid_file.read_text().strip(), "no background child")
        await _until(lambda: watch_id in service._active_tasks, "the job Watch never started")
        end(store, watch_id)
        await _until(lambda: host.status(job_id).state != "running", "the job outlived its Watch")
        await _until(lambda: _released(watch_id), "the Watch was never released")
        await service.stop()
        return host, job_id, watch_id

    host, job_id, watch_id = asyncio.run(run())

    assert host.stop_reason(job_id) == reason
    assert _gone(int(child_pid_file.read_text()))
    assert _follow_ups(watch_id) == []


def _resume_in_the_cli(watch_id: str, capsys) -> tuple[str, str]:
    assert cli.cmd_watch_set_enabled(watch_id, True) == 1
    error = json.loads(capsys.readouterr().err)
    return error["code"], error["error"]


def _resume_in_the_web_ui(watch_id: str, capsys) -> tuple[str, str]:
    client = ui_app.test_client()
    response = client.patch(
        f"/api/harness/watches/{watch_id}", json={"enabled": True}, headers=csrf_headers(client)
    )
    assert response.status_code == 409
    error = response.get_json()["error"]
    return error["code"], error["message"]


@pytest.mark.parametrize("state", ["paused", "released"])
@pytest.mark.parametrize("resume", [_resume_in_the_cli, _resume_in_the_web_ui], ids=["cli", "web-ui"])
def test_a_job_watch_that_no_longer_owns_its_job_is_never_resumed(
    tmp_path: Path, capsys, monkeypatch, state: str, resume
) -> None:
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: "zh")

    async def run() -> tuple[LocalJobHost, str, str, tuple[str, str]]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "echo done" if state == "released" else "sleep 60")
        watch_id = await host.hand_over(job_id)
        if state == "released":
            first = _service(store)
            await _start_service(first)
            await _until(lambda: not store.get_watch(watch_id).enabled, "the job never reported")
            await first.stop()
        else:
            # Paused, and resumed before any service has seen the pause.
            store.set_enabled(watch_id, False)
        refusal = resume(watch_id, capsys)
        # A repeated hand-over adopts the same Watch and does not re-arm it either.
        assert await hand_over_job(host.meta(job_id), store=ManagedWatchStore()) == watch_id
        reloaded = ManagedWatchStore()
        service = _service(reloaded)
        await _start_service(service)
        await _until(lambda: host.status(job_id).state != "running", "the paused job kept running")
        await _until(lambda: _released(watch_id), "the Watch was never released")
        await asyncio.sleep(0.2)  # a replayed report would land here
        await service.stop()
        return host, job_id, watch_id, refusal

    host, job_id, watch_id, (code, message) = asyncio.run(run())

    assert code == "job_watch_not_resumable"
    language = "zh" if resume is _resume_in_the_cli else "en"
    assert message == i18n_t("error.jobWatchResumeRefused.message", language)
    assert not ManagedWatchStore().get_watch(watch_id).enabled
    if state == "released":
        assert len(_follow_ups(watch_id)) == 1
        assert host.stop_reason(job_id) is None
    else:
        assert _follow_ups(watch_id) == []
        assert host.stop_reason(job_id) == "watch_disabled"


def test_changing_a_job_watch_waiter_is_refused_in_the_user_language(tmp_path: Path, capsys, monkeypatch) -> None:
    async def hand_over() -> tuple[LocalJobHost, str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "sleep 60")
        return host, job_id, await host.hand_over(job_id)

    host, job_id, watch_id = asyncio.run(hand_over())
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: "zh")
    monkeypatch.setattr(sys, "argv", ["vibe", "watch", "update", watch_id, "--shell", "pytest -q"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == "job_watch_update_refused"
    assert error["error"] == i18n_t("error.jobWatchUpdateRefused.message", "zh")
    assert ManagedWatchStore().get_watch(watch_id).job_target["command"] == "sleep 60"

    # What it does take goes through the same doorway.
    monkeypatch.setattr(sys, "argv", ["vibe", "watch", "update", watch_id, "--name", "slow tests"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0, capsys.readouterr().err
    assert ManagedWatchStore().get_watch(watch_id).name == "slow tests"
    asyncio.run(host.kill(job_id))


def _session_gone() -> None:
    with SQLiteBackgroundTaskStore().engine.begin() as conn:
        conn.execute(agent_sessions.delete().where(agent_sessions.c.id == SESSION_ID))


def _update_fields(watch) -> dict:
    """Every field ``update_watch`` takes, as the Watch has them now."""

    return {
        "name": watch.name,
        "session_key": watch.session_key,
        "session_id": watch.session_id,
        "command": list(watch.command),
        "shell_command": watch.shell_command,
        "prefix": watch.prefix,
        "cwd": watch.cwd,
        "mode": watch.mode,
        "timeout_seconds": watch.timeout_seconds,
        "lifetime_timeout_seconds": watch.lifetime_timeout_seconds,
        "retry_exit_codes": list(watch.retry_exit_codes),
        "retry_delay_seconds": watch.retry_delay_seconds,
        "post_to": watch.post_to,
        "deliver_key": watch.deliver_key,
        "agent_name": watch.agent_name,
        "session_policy": watch.session_policy,
        "message": watch.message,
        "metadata": dict(watch.metadata),
    }


_FIXED_JOB_WATCH_FIELDS = {
    "session_id": "ses_other",
    "session_key": "slack::channel::C999",
    "session_policy": "create_once",
    "agent_name": "claude",
    "post_to": "channel",
    "deliver_key": "slack::channel::C999",
    "metadata": {"binding_follows_session": True},
    "command": ["pytest", "-q"],
    "shell_command": "pytest -q",
    "cwd": "/tmp",
    "mode": "forever",
    "timeout_seconds": 60.0,
    "retry_exit_codes": [75],
    "retry_delay_seconds": 5.0,
}


@pytest.mark.parametrize(("field", "value"), sorted(_FIXED_JOB_WATCH_FIELDS.items()))
def test_a_job_watch_takes_no_update_beyond_its_name_message_and_lifetime(tmp_path: Path, field: str, value) -> None:
    async def hand_over() -> tuple[LocalJobHost, str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "sleep 60")
        return host, job_id, await host.hand_over(job_id)

    host, job_id, watch_id = asyncio.run(hand_over())
    store = ManagedWatchStore()
    before = store.get_watch(watch_id).to_dict()
    fields = _update_fields(store.get_watch(watch_id))
    if field == "metadata":
        value = {**fields["metadata"], **value}

    with pytest.raises(JobWatchUpdateRefused):
        store.update_watch(watch_id, **{**fields, field: value})
    assert ManagedWatchStore().get_watch(watch_id).to_dict() == before

    # The three it does take still land.
    allowed = {**fields, "name": "tests", "message": "Tests finished.", "lifetime_timeout_seconds": 900.0}
    updated = store.update_watch(watch_id, **allowed)
    assert (updated.name, updated.message, updated.lifetime_timeout_seconds) == ("tests", "Tests finished.", 900.0)
    assert ManagedWatchStore().get_watch(watch_id).session_id == SESSION_ID
    asyncio.run(host.kill(job_id))


def _corrupt_deadline(meta: dict) -> dict:
    return {**meta, "deadline_at": "not a time"}


def test_an_unreadable_job_state_stops_the_job_before_the_watch_lets_go(tmp_path: Path) -> None:
    async def run() -> tuple[LocalJobHost, str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "echo started; sleep 60")
        watch_id = await host.hand_over(job_id)
        meta_path = Path(host.job_dir(job_id)) / "meta.json"
        meta_path.write_text(json.dumps(_corrupt_deadline(json.loads(meta_path.read_text()))))
        service = _service(store)
        await _start_service(service)
        await _until(lambda: not store.get_watch(watch_id).enabled, "the Watch never settled the unreadable job")
        await service.stop()
        return host, job_id, watch_id

    host, job_id, watch_id = asyncio.run(run())

    assert host.status(job_id).state == "gone"
    assert host.stop_reason(job_id) == "watch_unreadable_state"
    (follow_up,) = _follow_ups(watch_id)
    assert "started" in follow_up.prompt
    assert follow_up.prompt.endswith("Command stopped: its job state could not be read")
    assert SQLiteBackgroundTaskStore().get_watch(watch_id)["last_error"] == i18n_t(
        "harness.watch.jobStoppedUnreadable", "en"
    )
    assert _released(watch_id)


@pytest.mark.parametrize("owned", [True, False], ids=["owning-watch", "removed-watch"])
def test_a_job_that_cannot_be_stopped_is_never_released(tmp_path: Path, owned: bool) -> None:
    async def run() -> tuple[LocalJobHost, str, str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "sleep 60")
        watch_id = await host.hand_over(job_id)
        meta_path = Path(host.job_dir(job_id)) / "meta.json"
        intact = meta_path.read_text()
        # Unreadable: neither its deadline nor the process identity a kill needs can be read.
        meta_path.write_text("{ not json")
        if not owned:
            store.remove_watch(watch_id)
        service = _service(store)
        await _start_service(service)
        if owned:
            await _until(lambda: store.get_watch(watch_id).last_error, "the Watch never said it was stuck")
        else:
            await asyncio.sleep(0.5)  # several sweeps
        await service.stop()
        meta_path.write_text(intact)
        return host, job_id, watch_id, intact

    host, job_id, watch_id, _intact = asyncio.run(run())

    assert host.status(job_id).state == "running"
    assert not _released(watch_id)
    assert _follow_ups(watch_id) == []
    if owned:
        row = SQLiteBackgroundTaskStore().get_watch(watch_id)
        assert row["enabled"]
        assert row["last_error"] == i18n_t("harness.watch.jobUnmanageable", "en")
    asyncio.run(host.kill(job_id))


@pytest.mark.parametrize(
    ("teardown", "reason"),
    [(_archive_session, "session_archived"), (_session_gone, "watch_removed")],
    ids=["archived", "deleted"],
)
def test_a_hand_over_after_its_session_teardown_still_ends_the_job(tmp_path: Path, teardown, reason: str) -> None:
    async def run() -> tuple[LocalJobHost, str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "sleep 60")
        # The teardown's reclaim commits before the hand-over, so it cannot see this Watch.
        teardown()
        watch_id = await host.hand_over(job_id)
        service = _service(store)
        await _start_service(service)
        await _until(lambda: host.status(job_id).state != "running", "the job outlived its Session")
        await _until(lambda: _released(watch_id), "the Watch was never released")
        await service.stop()
        return host, job_id, watch_id

    host, job_id, watch_id = asyncio.run(run())

    assert host.stop_reason(job_id) == reason
    assert _follow_ups(watch_id) == []


def test_watch_lifetime_kills_the_job_and_reports_it(tmp_path: Path) -> None:
    async def run() -> tuple[LocalJobHost, str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "echo started; sleep 60")
        watch_id = await host.hand_over(job_id)
        watch = store.get_watch(watch_id)
        watch.lifetime_timeout_seconds = 0.5
        store.upsert_watch(watch)
        service = _service(store)
        await _start_service(service)
        await _until(lambda: not store.get_watch(watch_id).enabled, "the lifetime never ended the Watch")
        await service.stop()
        return host, job_id, watch_id

    host, job_id, watch_id = asyncio.run(run())

    assert host.status(job_id).state == "gone"
    assert host.stop_reason(job_id) == "watch_lifetime"
    (follow_up,) = _follow_ups(watch_id)
    assert "started" in follow_up.prompt
    assert follow_up.prompt.endswith(f"Command stopped: Watch {watch_id} reached its lifetime of 0.5 seconds")
    assert _released(watch_id)


def test_job_deadline_holds_while_the_watch_owns_it(tmp_path: Path) -> None:
    async def run() -> tuple[LocalJobHost, str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "sleep 60", timeout_s=60)
        # Move the recorded deadline in, past the one the wrapper enforces from its argv,
        # so only the job's owner can enforce it in time.
        meta_path = Path(host.job_dir(job_id)) / "meta.json"
        meta = json.loads(meta_path.read_text())
        deadline = datetime.now(timezone.utc) + timedelta(seconds=0.5)
        meta["deadline_at"] = deadline.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        meta["timeout_s"] = 0.5
        meta_path.write_text(json.dumps(meta))
        watch_id = await host.hand_over(job_id)
        service = _service(store)
        await _start_service(service)
        await _until(lambda: not store.get_watch(watch_id).enabled, "the deadline never ended the job", timeout=10)
        await service.stop()
        return host, job_id, watch_id

    host, job_id, watch_id = asyncio.run(run())

    assert host.stop_reason(job_id) == "timeout"
    (follow_up,) = _follow_ups(watch_id)
    assert follow_up.prompt.endswith("Command timed out after 0.5 seconds")
    row = _settled_row(watch_id)
    assert row["lifecycle_detail"] == "timeout"
    assert row["metadata"]["last_command_timed_out"] is True


def test_hand_over_returns_the_watch_that_already_owns_the_job(tmp_path: Path) -> None:
    async def run() -> None:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "sleep 60")
        watch_id = await host.hand_over(job_id)

        # A crash before meta.json recorded the hand-over: the job host asks again.
        meta_path = Path(host.job_dir(job_id)) / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["watch_id"] = None
        meta_path.write_text(json.dumps(meta))
        assert await host.hand_over(job_id) == watch_id
        # A removed Watch still owned the job: no second Watch replaces it.
        store.remove_watch(watch_id)
        assert await hand_over_job(host.meta(job_id), store=ManagedWatchStore()) == watch_id

        # Two hand-overs of one job at once, from separate stores, converge on one Watch.
        other_job = await _start_job(host, tmp_path, "sleep 60", tool_call_id="toolu_2")
        other_meta = host.meta(other_job)
        ids = await asyncio.gather(
            hand_over_job(other_meta, store=ManagedWatchStore()),
            hand_over_job(other_meta, store=ManagedWatchStore()),
        )
        assert ids[0] == ids[1]
        for job in (job_id, other_job):
            await host.kill(job)

    asyncio.run(run())

    rows = _sql(
        "SELECT json_extract(metadata_json, '$.watch_target.job_id'), count(*) FROM run_definitions "
        "WHERE definition_type = 'watch' GROUP BY 1"
    )
    assert sorted(count for _job, count in rows) == [1, 1]


@pytest.mark.parametrize(
    "retry",
    [
        {"user_context": {}},  # the caller has lost Editor access since the first hand-over
        {"agent_name": "no-such-agent", "user_context": {}},
        {"drop": "command"},  # a job record that could no longer create a Watch
    ],
    ids=["access-revoked", "agent-gone", "record-incomplete"],
)
def test_a_repeated_hand_over_returns_the_owner_whatever_creation_would_now_refuse(tmp_path: Path, retry: dict) -> None:
    async def run() -> tuple[LocalJobHost, str, str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "sleep 60")
        # The first hand-over committed its Watch; a crash lost meta.json's record of it.
        watch_id = await hand_over_job(host.meta(job_id), store=store)
        meta = host.meta(job_id)
        if "drop" in retry:
            meta.pop(retry["drop"])
        route = {key: value for key, value in retry.items() if key != "drop"}
        again = await hand_over_job(meta, store=ManagedWatchStore(), **route)
        await host.kill(job_id)
        return host, job_id, watch_id, again

    _host_, _job, watch_id, again = asyncio.run(run())

    assert again == watch_id


def test_a_hand_over_that_loses_its_creation_check_to_a_concurrent_owner_returns_that_owner(
    tmp_path: Path, monkeypatch
) -> None:
    from storage import resource_access_service

    async def run() -> tuple[str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "sleep 60")
        meta = host.meta(job_id)
        concurrent: list[str] = []
        original = resource_access_service.ensure_harness_definition_write

        def lose_the_race(user_context=None):
            # Another hand-over of this job commits its Watch after this one looked, and
            # this one's access check then fails.
            monkeypatch.setattr(resource_access_service, "ensure_harness_definition_write", original)
            concurrent.append(ManagedWatchStore().adopt_or_create_job_watch(meta))
            raise resource_access_service.ResourceAccessError(resource_access_service.HARNESS_ACCESS_FORBIDDEN_CODE)

        monkeypatch.setattr(resource_access_service, "ensure_harness_definition_write", lose_the_race)
        watch_id = await hand_over_job(meta, store=store)
        await host.kill(job_id)
        return watch_id, concurrent[0]

    watch_id, concurrent_id = asyncio.run(run())

    assert watch_id == concurrent_id


@pytest.mark.parametrize(
    ("exit_code", "detail"),
    [(0, "normal"), (64, "error"), (75, "error"), (124, "error")],
)
def test_job_exit_code_is_reported_never_read_as_a_watch_control_code(
    tmp_path: Path, exit_code: int, detail: str
) -> None:
    async def run() -> str:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, f"echo 'avibe-watch: no-event'; exit {exit_code}")
        await host.wait(job_id, deadline_s=10)
        watch_id = await host.hand_over(job_id)
        service = _service(store)
        await _start_service(service)
        await _until(lambda: not store.get_watch(watch_id).enabled, "the Watch never reported the exit")
        await asyncio.sleep(0.2)  # a retry would start another cycle here
        await service.stop()
        return watch_id

    watch_id = asyncio.run(run())

    # 64 with the no-event marker, 75, and 124 mean something to a waiter; here they are the command's.
    (follow_up,) = _follow_ups(watch_id)
    assert follow_up.prompt.endswith(f"Command exited with code {exit_code}")
    row = _settled_row(watch_id)
    assert row["lifecycle_state"] == "finished"
    assert row["lifecycle_detail"] == detail
    assert row["last_exit_code"] is None
    assert row["metadata"]["watch_target"]["exit_code"] == exit_code


@pytest.mark.parametrize(
    ("command", "kept", "dropped", "notice"),
    [
        ("seq 1 3000", "\n1001\n", "\n1000\n", "[Showing lines 1001-3000 of 3000. "),
        ("head -c 4000000 /dev/zero | tr '\\0' 'x'; echo", "xxxx\n", None, "tail.log were omitted"),
    ],
    ids=["long-output", "log-bounded-on-disk"],
)
def test_follow_up_states_the_tail_under_the_bash_rules(
    tmp_path: Path, command: str, kept: str, dropped, notice: str
) -> None:
    async def run() -> tuple[str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, command)
        watch_id = await host.hand_over(job_id)
        service = _service(store)
        await _start_service(service)
        await _until(lambda: not store.get_watch(watch_id).enabled, "the Watch never reported")
        await service.stop()
        return watch_id, host.output_path(job_id)

    watch_id, output_path = asyncio.run(run())

    (follow_up,) = _follow_ups(watch_id)
    prompt = follow_up.prompt
    assert prompt.startswith(f"Watch {watch_id} finished after ")
    assert f"Command: {command}\n" in prompt
    assert kept in prompt and (dropped is None or dropped not in prompt)
    assert notice in prompt and output_path in prompt
    # J4: a log bounded on disk is never promised as the full output.
    assert dropped is not None or "Full output:" not in prompt
    assert prompt.endswith("Command exited with code 0")


def test_a_job_watch_reads_as_its_command_and_stores_no_waiter(tmp_path: Path) -> None:
    command = "pytest -q tests/test_slow.py"

    async def run() -> str:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, f"sleep 60 # {command}")
        watch_id = await hand_over_job({**host.meta(job_id), "command": command}, store=store)
        await host.kill(job_id)
        return watch_id

    watch_id = asyncio.run(run())

    sqlite = SQLiteBackgroundTaskStore()
    shown = cli._watch_projection_payload(sqlite.get_watch(watch_id))
    listed = sqlite.list_watches_page(page_request=None, query="test_slow").items
    assert shown["command_preview"] == command
    assert shown["display_name"] == command
    assert shown["shell_command"] == command
    assert [row["id"] for row in listed] == [watch_id]
    # Nothing a reader without job targets could run.
    assert _sql("SELECT command_json, shell_command FROM run_definitions WHERE id = ?", watch_id) == [("[]", None)]


_RELEASED_COMMAND_WATCHES = [
    pytest.param(
        {
            "id": "a1b2c3d4e5f6",
            "name": "Wait for CI",
            "session_key": "slack::channel::C123",
            "session_id": "sesk8m4q2p7x",
            "agent_name": "claude",
            "session_policy": "existing",
            "command": ["python3", "wait_pr.py", "--forever"],
            "shell_command": None,
            "prefix": "CI finished.",
            "message": "CI finished.",
            "cwd": "/work/repo",
            "mode": "forever",
            "timeout_seconds": 0.0,
            "lifetime_timeout_seconds": 0.0,
            "retry_exit_codes": [75],
            "retry_delay_seconds": 30.0,
            "post_to": None,
            "deliver_key": None,
            "enabled": True,
            "metadata": {"watch_lifetime_started_at": "2026-05-15T00:00:00+00:00", "delivered_reports": 2},
        },
        id="argv-forever",
    ),
    pytest.param(
        {
            "id": "f6e5d4c3b2a1",
            "name": None,
            "session_key": "",
            "session_id": "sesk8m4q2p7x",
            "command": [],
            "shell_command": "gh run watch 123 --exit-status",
            "mode": "once",
            "timeout_seconds": 600.0,
            "retry_exit_codes": [75, 124],
            "enabled": False,
            "last_exit_code": 64,
            "metadata": {},
        },
        id="shell-once",
    ),
    pytest.param(
        {"id": "0a1b2c3d4e5f", "session_key": "slack::channel::C9", "command": ["true"]},
        id="legacy-minimal",
    ),
]


@pytest.mark.parametrize("released", _RELEASED_COMMAND_WATCHES)
def test_released_command_watch_shapes_load_unchanged(tmp_path: Path, released: dict) -> None:
    """Rows and ``watches.json`` entries written before job targets keep their waiter."""

    legacy = ManagedWatchStore(tmp_path / "watches.json")
    (tmp_path / "watches.json").write_text(json.dumps({"watches": [released]}))
    legacy.load()
    from_file = legacy.get_watch(released["id"])

    sqlite = SQLiteBackgroundTaskStore()
    stamps = {"created_at": "2026-05-15T00:00:00+00:00", "updated_at": "2026-05-15T00:00:00+00:00"}
    assert sqlite.upsert_watch({**stamps, **released})
    from_row = ManagedWatchStore().get_watch(released["id"])

    for watch in (from_file, from_row):
        assert watch.job_target is None
        assert watch.command == released["command"]
        assert watch.shell_command == released.get("shell_command")
        assert watch.retry_exit_codes == released.get("retry_exit_codes", [75])
        assert watch.mode == released.get("mode", "once")
    # Writing the loaded Watch back stores the same waiter it was loaded with.
    assert sqlite.upsert_watch(from_row.to_dict())
    stored = _sql(
        "SELECT command_json, shell_command, retry_exit_codes_json FROM run_definitions WHERE id = ?",
        released["id"],
    )
    assert stored == [
        (
            json.dumps(released["command"], separators=(",", ":")),
            released.get("shell_command"),
            json.dumps(released.get("retry_exit_codes", [75]), separators=(",", ":")),
        )
    ]


def test_vibe_stop_ends_owned_jobs_and_the_watch_reports_after_the_next_start(tmp_path: Path, monkeypatch) -> None:
    async def hand_over() -> tuple[LocalJobHost, str, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "sleep 60")
        return host, job_id, await host.hand_over(job_id)

    host, job_id, watch_id = asyncio.run(hand_over())
    monkeypatch.setattr(cli, "_pid_file_points_to_live_process", lambda path: False)
    monkeypatch.setattr(cli.runtime, "stop_service", lambda **kwargs: False)
    monkeypatch.setattr(cli.runtime, "stop_ui", lambda **kwargs: False)
    monkeypatch.setattr(cli, "_stop_opencode_server", lambda *args: False)
    monkeypatch.setattr(cli, "_write_status", lambda state, detail=None: None)

    assert cli.cmd_stop() == 0

    assert host.status(job_id).state == "gone"
    assert host.stop_reason(job_id) == "vibe_stop"
    assert ManagedWatchStore().get_watch(watch_id).enabled

    async def start_again() -> None:
        store = ManagedWatchStore()
        service = _service(store)
        await _start_service(service)
        await _until(lambda: not store.get_watch(watch_id).enabled, "the Watch never reported the stop")
        await service.stop()

    asyncio.run(start_again())
    (follow_up,) = _follow_ups(watch_id)
    assert follow_up.prompt.endswith("Command stopped by `vibe stop`")
    # Called again, there is nothing left to stop and nothing to report twice.
    stop_all_jobs(ManagedWatchStore())
    assert len(_follow_ups(watch_id)) == 1


def test_vibe_stop_ends_a_foreground_job_that_no_watch_owns(tmp_path: Path, monkeypatch) -> None:
    # The adapter's host: the bash tool's jobs live in the state's jobs directory.
    host = LocalJobHost(str(watches_module.agent_jobs_dir()))
    job_id = asyncio.run(_start_job(host, tmp_path, "sleep 60 & wait"))
    assert host.meta(job_id)["watch_id"] is None  # still in the foreground, never handed over
    monkeypatch.setattr(cli, "_pid_file_points_to_live_process", lambda path: False)
    monkeypatch.setattr(cli.runtime, "stop_service", lambda **kwargs: False)
    monkeypatch.setattr(cli.runtime, "stop_ui", lambda **kwargs: False)
    monkeypatch.setattr(cli, "_stop_opencode_server", lambda *args: False)
    monkeypatch.setattr(cli, "_write_status", lambda state, detail=None: None)

    assert cli.cmd_stop() == 0

    assert host.status(job_id).state == "gone"
    assert host.stop_reason(job_id) == "vibe_stop"


def test_vibe_stop_leaves_the_jobs_of_a_service_that_holds_the_lock(tmp_path: Path, monkeypatch) -> None:
    async def hand_over() -> tuple[LocalJobHost, str]:
        store = ManagedWatchStore()
        host = _host(tmp_path, store)
        job_id = await _start_job(host, tmp_path, "sleep 60")
        await host.hand_over(job_id)
        return host, job_id

    host, job_id = asyncio.run(hand_over())
    monkeypatch.setattr(cli, "_pid_file_points_to_live_process", lambda path: False)
    monkeypatch.setattr(cli.runtime, "stop_service", lambda **kwargs: False)
    monkeypatch.setattr(cli.runtime, "stop_ui", lambda **kwargs: False)
    monkeypatch.setattr(cli, "_stop_opencode_server", lambda *args: False)
    monkeypatch.setattr(cli, "_write_status", lambda state, detail=None: None)
    lock_path = cli.runtime.get_service_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # A service that started after this stop's own stop of the service owns the jobs now.
    with lock_path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cli.cmd_stop() == 0

    assert host.status(job_id).state == "running"
    asyncio.run(host.kill(job_id))
