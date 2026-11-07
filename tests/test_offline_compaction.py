"""Offline VACUUM admission and lifetime, through real cross-process locks.

Storage's compaction tests cover disk/checkpoint safety, not a live runtime or a
launch between preflight and VACUUM. These protect that missing CLI/runtime
contract: refusal precedes deletion, starts cannot enter during the rewrite or
cleanup, and deletion without compaction still works online. Child processes
own only the test home; no real service, server or network is started.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from sqlalchemy import event, select

from config import paths
from storage import agent_events_retention, agent_events_service, db
from storage.importer import ensure_sqlite_state
from storage.models import agent_events
from vibe import cli, runtime


class Child:
    def __init__(self, source: str):
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", source],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(line.rstrip())

    def expect(self, expected: str):
        assert self.lines.get(timeout=30) == expected

    def finish(self, *, release=False):
        if release and self.process.poll() is None:
            self.process.stdin.write("finish\n")
            self.process.stdin.flush()
        assert self.process.wait(timeout=30) == 0

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self.reader.join(timeout=10)
        self.process.stdin.close()
        self.process.stdout.close()


@pytest.fixture
def children(monkeypatch):
    owned = []

    def start(source):
        child = Child(source)
        owned.append(child)
        return child

    def owned_processes(attrs):
        # Even liveness discovery reads only this test's processes, never the
        # developer's running Avibe or an unrelated machine's processes.
        for child in owned:
            if child.process.poll() is None:
                proc = psutil.Process(child.process.pid)
                proc.info = proc.as_dict(attrs=attrs)
                yield proc

    monkeypatch.setattr(runtime.psutil, "process_iter", owned_processes)

    def no_stop(*args, **kwargs):
        pytest.fail("retention must never stop a process")

    for name in ("stop_service", "stop_ui", "stop_pid"):
        monkeypatch.setattr(runtime, name, no_stop)
    yield start
    for child in reversed(owned):
        child.close()


@pytest.fixture
def state():
    ensure_sqlite_state()
    engine = db.create_sqlite_engine()
    with engine.begin() as conn:
        for index in range(40):
            agent_events_service.append(
                conn, scope_id=None, session_id=None, platform="web",
                event_type="tool_call", text=f"{index}:" + "x" * 4000,
                visibility="trace",
            )
        conn.execute(agent_events.update().values(created_at="2000-01-01T00:00:00.000000Z"))
    yield engine
    engine.dispose()


def _args(*, compact=True):
    return SimpleNamespace(run=not compact, compact=compact, days=30, json=True)


def _rows(engine):
    with engine.connect() as conn:
        return len(conn.execute(select(agent_events.c.id)).fetchall())


_HOLD_SERVICE = """
import sys
from vibe import runtime
runtime.acquire_service_instance_lock()
print('held')
sys.stdin.readline()
runtime.release_service_instance_lock()
"""

_HOLD_UNKNOWN_SERVICE = """
import sys
from vibe import runtime
runtime.ensure_dirs()
with runtime.get_service_lock_path().open('a+') as handle:
    assert runtime._take_lock_file(handle)
    print('held')
    sys.stdin.readline()
    runtime._unlock_file(handle)
"""

_HOLD_UI = """
import sys
from vibe.ui_server import run_ui_server
print('held')
sys.stdin.readline()
"""


@pytest.mark.parametrize("live", ["service", "unknown-service", "ui", "unrecorded-ui", "ambiguous-ui"])
def test_compact_refuses_before_mutation_but_plain_retention_stays_online(state, children, monkeypatch, capsys, live):
    source = _HOLD_SERVICE if live == "service" else _HOLD_UNKNOWN_SERVICE if live == "unknown-service" else _HOLD_UI
    holder = children(source)
    holder.expect("held")
    if live in {"ui", "ambiguous-ui"}:
        paths.get_runtime_ui_pid_path().parent.mkdir(parents=True, exist_ok=True)
        paths.get_runtime_ui_pid_path().write_text(str(holder.process.pid))
    if live == "ambiguous-ui":
        monkeypatch.setattr(runtime, "get_process_command", lambda pid: None)
    assert cli.cmd_data_retention(_args()) == 1
    error = capsys.readouterr().err
    assert "vibe stop" in error and "omit --compact" in error
    assert _rows(state) == 40
    with state.connect() as conn:
        assert agent_events_retention.get_last_run(conn) is None

    def no_vacuum(*args, **kwargs):
        pytest.fail("online retention must not compact")

    monkeypatch.setattr(agent_events_retention, "maybe_compact", no_vacuum)
    assert cli.cmd_data_retention(_args(compact=False)) == 0
    assert _rows(state) == 0
    assert holder.process.poll() is None
    holder.finish(release=True)


# Reports real acquisition outcomes, not probes mocked to return busy. This also
# tests the same service byte on Windows rather than only POSIX flock behavior.
_PROBE = """
import json
from vibe import runtime
from storage.lock import MigrationLockTimeout
with runtime.get_service_lock_path().open('a+') as handle:
    service = runtime._take_lock_file(handle)
    if service:
        runtime._unlock_file(handle)
try:
    with runtime.ui_start_exclusion(timeout_seconds=0):
        ui = True
except MigrationLockTimeout:
    ui = False
print(json.dumps([service, ui]))
"""


def _probe(children, expected):
    probe = children(_PROBE)
    probe.expect(json.dumps(expected))
    probe.finish()


_SERVICE_ENTRY = """
from vibe import runtime
runtime._SERVICE_LOCK_SETTLE_SECONDS = 0
try:
    runtime.acquire_service_instance_lock()
except runtime.ServiceAlreadyRunningError:
    print('service refused')
else:
    runtime.release_service_instance_lock()
    print('service admitted')
"""

_UI_ENTRY = """
from vibe.ui_server import run_ui_server
from vibe import ui_server
from core.services import settings
from storage import lock
original = lock._try_lock
reported = False
def attempt(handle):
    global reported
    result = original(handle)
    if not result and not reported:
        reported = True
        print('UI blocked')
    return result
lock._try_lock = attempt
class Admitted(Exception):
    pass
def config():
    raise Admitted()
settings.load_config = config
def logging(*args):
    raise Admitted()
ui_server._configure_ui_logging = logging
try:
    run_ui_server('127.0.0.1', 0)
except Admitted:
    print('UI admitted')
"""


def test_compact_excludes_real_entries_through_vacuum_checkpoints_and_disposal(state, children, monkeypatch):
    original_create = db.create_sqlite_engine
    original_compact = agent_events_retention.maybe_compact
    original_checkpoint = agent_events_retention._default_wal_checkpoint
    phases = []
    waiting_ui = []

    def checkpoint(engine):
        _probe(children, [False, False])
        phases.append("checkpoint")
        return original_checkpoint(engine)

    def create(*args, **kwargs):
        engine = original_create(*args, **kwargs)

        @event.listens_for(engine, "before_cursor_execute")
        def before(conn, cursor, statement, parameters, context, executemany):
            if statement == "VACUUM":
                phases.append("vacuum")
                contender = children(_SERVICE_ENTRY)
                contender.expect("service refused")
                contender.finish()
                ui = children(_UI_ENTRY)
                waiting_ui.append(ui)
                # The real OS attempt already failed, so this is not a timing
                # assertion about a child that simply has not been scheduled.
                ui.expect("UI blocked")

        original_dispose = engine.dispose

        def dispose(*args, **kwargs):
            _probe(children, [False, False])
            phases.append("dispose")
            return original_dispose(*args, **kwargs)

        engine.dispose = dispose
        return engine

    monkeypatch.setattr(db, "create_sqlite_engine", create)
    monkeypatch.setattr(agent_events_retention, "_default_wal_checkpoint", checkpoint)
    monkeypatch.setattr(
        agent_events_retention, "maybe_compact",
        lambda engine, **kwargs: original_compact(engine, min_reclaim_bytes=1, free_space_margin_bytes=0, **kwargs),
    )
    assert cli.cmd_data_retention(_args()) == 0
    assert phases == ["checkpoint", "vacuum", "checkpoint", "dispose"]
    assert _rows(state) == 0
    waiting_ui[0].expect("UI admitted")
    waiting_ui[0].finish()
    _probe(children, [True, True])
    contender = children(_SERVICE_ENTRY)
    contender.expect("service admitted")
    contender.finish()


@pytest.mark.parametrize("failure", [RuntimeError("maintenance failed"), KeyboardInterrupt()])
def test_failed_maintenance_releases_both_locks_and_closes_database(state, children, monkeypatch, failure):
    original_create = db.create_sqlite_engine
    disposed = []

    def create(*args, **kwargs):
        engine = original_create(*args, **kwargs)
        original_dispose = engine.dispose

        def dispose():
            _probe(children, [False, False])
            disposed.append(True)
            original_dispose()

        engine.dispose = dispose
        return engine

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(db, "create_sqlite_engine", create)
    monkeypatch.setattr(agent_events_retention, "run_once", fail)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            cli.cmd_data_retention(_args())
    else:
        assert cli.cmd_data_retention(_args()) == 1
    assert disposed == [True]
    _probe(children, [True, True])
    assert _rows(state) == 40


def test_guard_preserves_service_identity_and_refuses_concurrent_maintenance(children):
    paths.get_runtime_dir().mkdir(parents=True, exist_ok=True)
    paths.get_runtime_pid_path().write_text("0")
    runtime.get_service_lock_path().write_text('{"phase":"old"}')
    with runtime.offline_maintenance():
        assert not runtime.service_instance_lock_attached_to_process()
        _probe(children, [False, False])
        other = children("""
from vibe import runtime
try:
    with runtime.offline_maintenance():
        print('incorrectly admitted')
except runtime.OfflineMaintenanceUnavailable:
    print('maintenance refused')
""")
        other.expect("maintenance refused")
        other.finish()
    assert paths.get_runtime_pid_path().read_text() == "0"
    assert runtime.get_service_lock_path().read_text() == '{"phase":"old"}'
    _probe(children, [True, True])


def test_direct_ui_entry_that_wins_admission_refuses_compaction(state, children):
    ui = children(_UI_ENTRY + "\nimport sys\nsys.stdin.readline()\n")
    ui.expect("UI admitted")
    assert paths.get_runtime_ui_pid_path().read_text() == str(ui.process.pid)
    assert cli.cmd_data_retention(_args()) == 1
    assert _rows(state) == 40
    ui.finish(release=True)


@pytest.mark.parametrize("owner_command", ["known", "unreadable"])
def test_duplicate_direct_ui_cannot_replace_live_owner(children, owner_command):
    """A failed duplicate must not lose the serving UI's only lifecycle record.

    Offline admission tests proved exclusion from VACUUM, but did not cover a
    second direct entry taking the PID record before failing to bind.
    """
    owner = children(_UI_ENTRY + "\nimport sys\nsys.stdin.readline()\n")
    owner.expect("UI admitted")
    pid_path = paths.get_runtime_ui_pid_path()
    original = pid_path.read_bytes()
    source = _UI_ENTRY.replace(
        "except Admitted:\n    print('UI admitted')",
        "except RuntimeError as exc:\n"
        "    assert 'already running or starting' in str(exc)\n"
        "    print('UI refused')\n"
        "except Admitted:\n    print('incorrectly admitted')",
    )
    if owner_command == "unreadable":
        source = "from vibe import runtime\nruntime.get_process_command = lambda pid: None\n" + source
    duplicate = children(source)
    duplicate.expect("UI refused")
    duplicate.finish()
    assert pid_path.read_bytes() == original
    assert runtime.ui_pid_file_points_to_running_ui()
    assert owner.process.poll() is None
    owner.finish(release=True)


@pytest.mark.parametrize("record", ["self", "dead"])
def test_direct_ui_accepts_parent_handover_or_dead_owner(children, record):
    """Ordinary spawn/reload prepublishes its child; dead owners are replaceable."""
    if record == "dead":
        previous = children(_UI_ENTRY)
        previous.expect("UI admitted")
        previous.finish()
        prefix = ""
    else:
        prefix = (
            "import os\nfrom config import paths\nfrom vibe import runtime\n"
            "runtime.write_atomic(paths.get_runtime_ui_pid_path(), str(os.getpid()))\n"
        )
    child = children(prefix + _UI_ENTRY)
    child.expect("UI admitted")
    child.finish()
    assert paths.get_runtime_ui_pid_path().read_text() == str(child.process.pid)


@pytest.mark.parametrize("entry", ["start", "reload"])
def test_ui_launch_and_reload_cannot_spawn_inside_maintenance(children, entry):
    source = """
import os
from types import SimpleNamespace
from vibe import runtime, ui_server
from storage import lock
from core.services import settings
from config.v2_config import V2Config
original = lock._try_lock
reported = False
def attempt(handle):
    global reported
    result = original(handle)
    if not result and not reported:
        reported = True
        print('spawn blocked')
    return result
lock._try_lock = attempt
def spawn(*args, hand_over, **kwargs):
    process = SimpleNamespace(pid=os.getpid())
    hand_over(process)
    print('spawn admitted')
    return process
runtime._spawn_owned_process = spawn
runtime.read_status = lambda: {}
runtime.write_status = lambda *args: None
settings.load_config = lambda: V2Config.default()
ui_server.request = SimpleNamespace(json={'host': '127.0.0.1', 'port': 5123})
ui_server.jsonify = lambda value: value
class ImmediateThread:
    def __init__(self, target):
        self.target = target
    def start(self):
        self.target()
ui_server.threading.Thread = ImmediateThread
"""
    source += (
        "runtime.start_ui('127.0.0.1', 5123, wait_for_ready=False)\n"
        if entry == "start" else "ui_server.ui_reload()\n"
    )
    with runtime.offline_maintenance():
        child = children(source)
        child.expect("spawn blocked")
    child.expect("spawn admitted")
    child.finish()


def test_corrupt_recorded_ui_is_not_evidence_of_absence(state, children):
    paths.get_runtime_dir().mkdir(parents=True, exist_ok=True)
    paths.get_runtime_ui_pid_path().write_text("unreadable-pid")
    assert cli.cmd_data_retention(_args()) == 1
    assert _rows(state) == 40
    _probe(children, [True, True])


def test_unavailable_service_lock_fails_closed_before_deletion(state, children, monkeypatch, capsys):
    def unavailable(handle):
        raise OSError("locking unavailable")

    monkeypatch.setattr(runtime, "_take_service_lock", unavailable)
    assert cli.cmd_data_retention(_args()) == 1
    assert "vibe stop" in capsys.readouterr().err
    assert _rows(state) == 40
    _probe(children, [True, True])


def test_storage_retention_defaults_to_no_rewrite(state, monkeypatch):
    def no_vacuum(*args, **kwargs):
        pytest.fail("omitting compact must not rewrite the database")

    monkeypatch.setattr(agent_events_retention, "maybe_compact", no_vacuum)
    assert agent_events_retention.run_once(state, retention_days=30, force=True)["status"] == "ok"
