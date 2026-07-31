"""Unit tests for the read-only running-agents snapshot aggregator.

Hermetic: the DB enrichment and the on-disk Claude process registry are
monkeypatched out, so the test exercises only the in-memory aggregation logic
against fake controller registries — it never touches real state.
"""

from __future__ import annotations

import asyncio
import contextlib
import types

import pytest

from core.services import running_agents


class _AsyncFlag:
    """Awaitable mock that records it was called and returns a fixed value."""

    def __init__(self, ret=None):
        self.called = False
        self.ret = ret

    async def __call__(self, *args, **kwargs):
        self.called = True
        return self.ret


class _FakeClaudeClient:
    def __init__(self, base, native, model):
        self._vibe_runtime_base_session_id = base
        self._vibe_native_session_id = native
        self._vibe_current_model = model


class _FakeSessionMgr:
    def __init__(self, cwd_by_base):
        self._cwd_by_base = cwd_by_base

    def all_base_sessions(self):
        return list(self._cwd_by_base.keys())

    def get_cwd(self, base):
        return self._cwd_by_base.get(base)

    def get_sessions_by_session_key(self, _session_key):
        return list(self._cwd_by_base.keys())


class _FakeTurnRegistry:
    def __init__(self, active_by_base, pending=None):
        self._active = active_by_base
        self._pending = set(pending or ())

    def get_active_turn(self, base):
        return self._active.get(base)

    def has_pending_turn_start(self, base):
        return base in self._pending


class _FakeTransport:
    def __init__(self, pid, is_alive=True):
        self.pid = pid
        self.is_alive = is_alive


class _FakeTask:
    def __init__(self, done):
        self._done = done

    def done(self):
        return self._done


def _make_controller(*, claude=None, codex=None, opencode=None):
    agents = {}
    if codex is not None:
        agents["codex"] = codex
    if opencode is not None:
        agents["opencode"] = opencode
    controller = types.SimpleNamespace()
    controller.agent_service = types.SimpleNamespace(agents=agents)
    controller.claude_sessions = (claude or {}).get("sessions", {})
    controller.claude_active_sessions = (claude or {}).get("active", set())
    controller.session_last_activity = (claude or {}).get("last_activity", {})
    return controller


@pytest.fixture(autouse=True)
def _no_db_no_registry(monkeypatch):
    # Keep the aggregator hermetic: never read the real DB or process registry.
    monkeypatch.setattr(running_agents, "_enrich_from_db", lambda rows: None)
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper._load_owned_process_registry",
        lambda *a, **k: [],
    )
    # Claude pid resolution reads client._transport._process.pid; force a stable value.
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper.get_claude_client_pid",
        lambda client: getattr(client, "_fake_pid", None),
    )
    # Orphan liveness: by default every probed pid is "alive" (batched ages) with
    # a start time that matches the record (so identity passes). Tests exercising
    # the dead/reused-pid filter override these per-pid.
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper._process_ages",
        lambda pids: {p: 1.0 for p in pids},
    )
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper._process_start_time",
        lambda pid: 1000.0,
    )


def test_safe_items_tolerates_concurrent_mutation():
    # Normal dict round-trips.
    assert dict(running_agents._safe_items({"a": 1, "b": 2})) == {"a": 1, "b": 2}

    # A mapping whose first ``.items()`` raises (dict-changed-size) then succeeds
    # must be retried, not propagated.
    class _FlakyMapping:
        def __init__(self):
            self._calls = 0
            self._data = {"x": 1}

        def items(self):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("dictionary changed size during iteration")
            return self._data.items()

    assert dict(running_agents._safe_items(_FlakyMapping())) == {"x": 1}
    # Non-mapping input is handled gracefully.
    assert running_agents._safe_items(None) == []


def test_safe_call_retries_runtime_error_then_falls_back():
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("changed size during iteration")
        return ["a", "b"]

    assert running_agents._safe_call(_flaky, []) == ["a", "b"]

    # Always-raising callable falls back to the default rather than propagating.
    def _always():
        raise RuntimeError("boom")

    assert running_agents._safe_call(_always, []) == []


def test_claude_active_and_idle_rows():
    c_active = _FakeClaudeClient("slack_111", "nat-a", "opus")
    c_active._fake_pid = 4242
    c_idle = _FakeClaudeClient("slack_222", "nat-b", None)
    controller = _make_controller(
        claude={
            "sessions": {
                "slack_111:/home/u/proj": c_active,
                "slack_222:/home/u/other": c_idle,
            },
            "active": {"slack_111:/home/u/proj"},
            "last_activity": {"slack_111:/home/u/proj": 0.0, "slack_222:/home/u/other": 0.0},
        }
    )
    snap = running_agents.snapshot_running_agents(controller)
    rows = {r["base_session_id"]: r for r in snap["agents"]}

    assert rows["slack_111"]["state"] == "active"
    assert rows["slack_111"]["pid"] == 4242
    assert rows["slack_111"]["workdir"] == "/home/u/proj"
    assert rows["slack_111"]["model"] == "opus"
    assert rows["slack_222"]["state"] == "idle"
    assert rows["slack_222"]["pid"] is None
    assert snap["counts"]["active"] == 1
    assert snap["counts"]["idle"] == 1
    assert snap["counts"]["by_backend"]["claude"] == 2


def test_subagent_composite_key_base_parsing():
    # Subagent composite keys are `{platform}_{thread}:{agent}:{workdir}` — the
    # base must be everything before the LAST colon (the abs workdir).
    client = _FakeClaudeClient("slack_999:reviewer", "nat-x", None)
    controller = _make_controller(
        claude={
            "sessions": {"slack_999:reviewer:/srv/app": client},
            "active": set(),
            "last_activity": {},
        }
    )
    snap = running_agents.snapshot_running_agents(controller)
    row = snap["agents"][0]
    assert row["base_session_id"] == "slack_999:reviewer"
    assert row["workdir"] == "/srv/app"


def test_codex_shared_pid_one_row_per_session():
    mgr = _FakeSessionMgr({"base-1": "/work/x", "base-2": "/work/x", "base-3": "/work/y"})
    turns = _FakeTurnRegistry({"base-1": "turn-1"})  # base-1 active, others idle
    codex = types.SimpleNamespace(
        _session_mgr=mgr,
        _turn_registry=turns,
        _transports={"/work/x": _FakeTransport(7001), "/work/y": _FakeTransport(7002)},
        _transport_last_activity={"/work/x": 0.0, "/work/y": 0.0},
    )
    controller = _make_controller(codex=codex)
    snap = running_agents.snapshot_running_agents(controller)
    by_base = {r["base_session_id"]: r for r in snap["agents"]}

    assert by_base["base-1"]["pid"] == 7001 and by_base["base-1"]["pid_shared"] is True
    assert by_base["base-2"]["pid"] == 7001 and by_base["base-2"]["pid_shared"] is True
    assert by_base["base-3"]["pid"] == 7002 and by_base["base-3"]["pid_shared"] is False
    assert by_base["base-1"]["state"] == "active"
    assert by_base["base-2"]["state"] == "idle"
    assert snap["counts"]["by_backend"]["codex"] == 3


def test_codex_skips_evicted_idle_base_without_transport():
    mgr = _FakeSessionMgr({"live": "/work/live", "evicted": "/work/gone", "active": "/work/active"})
    turns = _FakeTurnRegistry({"active": "turn-1"})
    codex = types.SimpleNamespace(
        _session_mgr=mgr,
        _turn_registry=turns,
        _transports={"/work/live": _FakeTransport(7001)},
    )
    controller = _make_controller(codex=codex)
    snap = running_agents.snapshot_running_agents(controller)
    by_base = {r["base_session_id"]: r for r in snap["agents"]}

    assert set(by_base) == {"live", "active"}
    assert by_base["live"]["state"] == "idle"
    assert by_base["active"]["state"] == "active"
    assert by_base["active"]["pid"] is None


def test_codex_skips_dead_transport_object():
    # A transport whose app-server already exited (is_alive False) can linger in
    # _transports; such a base must not surface as a phantom idle row, nor count
    # toward pid_shared for a sibling on the same cwd.
    mgr = _FakeSessionMgr({"dead": "/work/x", "alive": "/work/y"})
    turns = _FakeTurnRegistry({})
    codex = types.SimpleNamespace(
        _session_mgr=mgr,
        _turn_registry=turns,
        _transports={"/work/x": _FakeTransport(8001, is_alive=False), "/work/y": _FakeTransport(8002)},
    )
    controller = _make_controller(codex=codex)
    snap = running_agents.snapshot_running_agents(controller)
    by_base = {r["base_session_id"]: r for r in snap["agents"]}

    assert set(by_base) == {"alive"}  # the dead-transport base is dropped
    assert by_base["alive"]["pid"] == 8002


def test_claude_active_elapsed_uses_turn_start_baseline():
    # Active "busy for" must be measured from the turn-start baseline, NOT
    # session_last_activity (which is bumped on every streamed chunk and would
    # read as seconds-since-last-chunk). Idle rows still use last activity.
    now = 1_000.0
    c_active = _FakeClaudeClient("slack_a", "nat-a", "opus")
    c_idle = _FakeClaudeClient("slack_b", "nat-b", None)
    controller = _make_controller(
        claude={
            "sessions": {"slack_a:/w1": c_active, "slack_b:/w2": c_idle},
            "active": {"slack_a:/w1"},
            "last_activity": {"slack_a:/w1": now - 2.0, "slack_b:/w2": now - 90.0},
        }
    )
    # Turn started long before the last chunk: busy time must reflect the turn
    # baseline (120s), not the 2s since the last streamed event.
    controller.session_turn_started = {"slack_a:/w1": now - 120.0}

    import time as _time

    orig = _time.monotonic
    _time.monotonic = lambda: now
    try:
        snap = running_agents.snapshot_running_agents(controller)
    finally:
        _time.monotonic = orig

    rows = {r["base_session_id"]: r for r in snap["agents"]}
    assert rows["slack_a"]["state"] == "active"
    assert rows["slack_a"]["elapsed_seconds"] == 120.0  # turn baseline, not 2s
    assert rows["slack_b"]["elapsed_seconds"] == 90.0  # idle uses last activity


def test_claude_active_elapsed_falls_back_to_last_activity_without_baseline():
    # When no turn baseline is recorded (e.g. activated before this build), the
    # active row degrades gracefully to last-activity rather than dropping elapsed.
    now = 500.0
    client = _FakeClaudeClient("slack_c", "nat-c", None)
    controller = _make_controller(
        claude={
            "sessions": {"slack_c:/w": client},
            "active": {"slack_c:/w"},
            "last_activity": {"slack_c:/w": now - 5.0},
        }
    )
    controller.session_turn_started = {}  # no baseline

    import time as _time

    orig = _time.monotonic
    _time.monotonic = lambda: now
    try:
        snap = running_agents.snapshot_running_agents(controller)
    finally:
        _time.monotonic = orig
    row = snap["agents"][0]
    assert row["state"] == "active"
    assert row["elapsed_seconds"] == 5.0


def test_codex_pending_turn_start_counts_as_active():
    # While turn/start is in flight, get_active_turn is still empty but the request
    # already holds the runtime turn. Such a base must report active (not idle), so
    # the UI offers Stop and End takes the canonical active path.
    mgr = _FakeSessionMgr({"starting": "/work/p"})
    turns = _FakeTurnRegistry({}, pending={"starting"})  # no active turn yet, pending start
    codex = types.SimpleNamespace(
        _session_mgr=mgr,
        _turn_registry=turns,
        _transports={"/work/p": _FakeTransport(9100)},
    )
    controller = _make_controller(codex=codex)
    snap = running_agents.snapshot_running_agents(controller)
    by_base = {r["base_session_id"]: r for r in snap["agents"]}
    assert by_base["starting"]["state"] == "active"


def test_opencode_active_requests_have_no_pid():
    oc = types.SimpleNamespace(_active_requests={"base-oc": _FakeTask(done=False)})
    controller = _make_controller(opencode=oc)
    snap = running_agents.snapshot_running_agents(controller)
    row = snap["agents"][0]
    assert row["backend"] == "opencode"
    assert row["state"] == "active"
    assert row["pid"] is None


def test_orphan_only_when_native_not_owned(monkeypatch):
    from modules.agents.claude_process_reaper import AVIBE_CLAUDE_SESSION_OWNER

    live = _FakeClaudeClient("slack_live", "nat-live", None)
    owned = types.SimpleNamespace(pid=100, native_session_id="nat-live", owner=AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0)
    leaked = types.SimpleNamespace(pid=200, native_session_id="nat-gone", owner=AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0)
    auth_proc = types.SimpleNamespace(pid=300, native_session_id="nat-auth", owner="auth", started_at=1000.0)
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper._load_owned_process_registry",
        lambda *a, **k: [owned, leaked, auth_proc],
    )
    controller = _make_controller(
        claude={"sessions": {"slack_live:/w": live}, "active": set(), "last_activity": {}}
    )
    snap = running_agents.snapshot_running_agents(controller)
    orphans = [r for r in snap["agents"] if r["state"] == "orphan"]

    # Only the leaked session-owned process becomes an orphan: the owned one is
    # still backed by a live client (native matches), the auth process is excluded.
    assert len(orphans) == 1
    assert orphans[0]["pid"] == 200
    assert orphans[0]["native_session_id"] == "nat-gone"
    assert snap["counts"]["orphan"] == 1


def test_orphan_dedup_by_pid_when_live_client_lacks_native(monkeypatch):
    from modules.agents.claude_process_reaper import AVIBE_CLAUDE_SESSION_OWNER

    # Live client with NO native id but a resolvable pid (e.g. SDK build that
    # exposes the pid but Avibe hasn't captured the native session id yet).
    live = _FakeClaudeClient("slack_live", None, None)
    live._fake_pid = 555
    same_pid = types.SimpleNamespace(pid=555, native_session_id="nat-x", owner=AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0)
    leaked = types.SimpleNamespace(pid=999, native_session_id="nat-y", owner=AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0)
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper._load_owned_process_registry",
        lambda *a, **k: [same_pid, leaked],
    )
    controller = _make_controller(
        claude={"sessions": {"slack_live:/w": live}, "active": set(), "last_activity": {}}
    )
    snap = running_agents.snapshot_running_agents(controller)
    orphans = [r for r in snap["agents"] if r["state"] == "orphan"]

    # ``same_pid`` is NOT an orphan: its pid still backs the live client even
    # though native ids don't match. Only the genuinely leaked pid is an orphan.
    assert {o["pid"] for o in orphans} == {999}


def test_standalone_background_session_is_openable():
    row = running_agents._make_row(backend="codex", state="idle", base_session_id="b1")
    meta = {
        "id": "ses0000priv",
        "scope_id": None,
        "scope_platform": None,
        "scope_scope_type": None,
        "scope_display_name": None,
        "visibility": "background",
        "title": "[Current Task] review",
        "agent_name": "codex",
        "workdir": None,
    }
    running_agents._apply_session_meta(row, meta)

    assert row["platform"] is None
    assert row["visibility"] == "background"
    assert row["openable_in_chat"] is True
    assert row["scope_display_name"] is None


def test_real_slack_session_labeled_and_openable():
    row = running_agents._make_row(backend="claude", state="idle", base_session_id="slack_x")
    meta = {
        "id": "ses0000slack",
        "scope_id": "slack::user::U123",
        "scope_platform": "slack",
        "scope_scope_type": "user",
        "scope_display_name": "qiqi",
        "scope_native_type": "im",
        "title": None,
        "agent_name": "claude",
        "workdir": "/home/u/cc/slack",
    }
    running_agents._apply_session_meta(row, meta)

    assert row["platform"] == "slack"
    assert row["trigger_source"] == "human"
    assert row["openable_in_chat"] is True  # real IM session is openable


def test_session_meta_prefers_matching_backend_before_recent_fallback():
    candidates = [
        {"id": "ses-claude", "agent_backend": "claude", "last_active_at": "2026-01-01T00:00:00"},
        {"id": "ses-codex", "agent_backend": "codex", "last_active_at": "2026-01-02T00:00:00"},
    ]
    claude_row = running_agents._make_row(backend="claude", state="idle", base_session_id="same-anchor")
    unknown_row = running_agents._make_row(backend="opencode", state="idle", base_session_id="same-anchor")

    assert running_agents._choose_session_meta(claude_row, candidates)["id"] == "ses-claude"
    assert running_agents._choose_session_meta(unknown_row, candidates)["id"] == "ses-codex"


def test_orphan_surfaces_duplicate_native_with_different_pid(monkeypatch):
    from modules.agents.claude_process_reaper import AVIBE_CLAUDE_SESSION_OWNER

    # A live client reconnected as pid 100 (native nat-dup); an OLDER process with
    # the SAME native id but pid 200 leaked. The native match must NOT hide it.
    live = _FakeClaudeClient("slack_dup", "nat-dup", None)
    live._fake_pid = 100
    leaked_old = types.SimpleNamespace(
        pid=200, native_session_id="nat-dup", owner=AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0
    )
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper._load_owned_process_registry",
        lambda *a, **k: [leaked_old],
    )
    controller = _make_controller(
        claude={"sessions": {"slack_dup:/w": live}, "active": set(), "last_activity": {}}
    )
    snap = running_agents.snapshot_running_agents(controller)
    orphans = [r for r in snap["agents"] if r["state"] == "orphan"]

    # The leaked old process surfaces as a killable orphan; the live client's own
    # pid 100 is still excluded (by seen_pids).
    assert {o["pid"] for o in orphans} == {200}


def test_orphan_skips_dead_and_reused_pids(monkeypatch):
    from modules.agents.claude_process_reaper import AVIBE_CLAUDE_SESSION_OWNER

    alive = types.SimpleNamespace(pid=10, native_session_id="nat-alive", owner=AVIBE_CLAUDE_SESSION_OWNER, started_at=5000.0)
    dead = types.SimpleNamespace(pid=20, native_session_id="nat-dead", owner=AVIBE_CLAUDE_SESSION_OWNER, started_at=5000.0)
    reused = types.SimpleNamespace(pid=30, native_session_id="nat-reused", owner=AVIBE_CLAUDE_SESSION_OWNER, started_at=5000.0)
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper._load_owned_process_registry",
        lambda *a, **k: [alive, dead, reused],
    )

    # pid 20 is not alive (absent from batched ages → stale); pid 30 is alive but
    # started far from the recorded time (reused by an unrelated process); pid 10
    # is a real leak (alive + start matches the record within 1s).
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper._process_ages",
        lambda pids: {p: 1.0 for p in pids if p in {10, 30}},
    )

    def _start(pid):
        return {10: 5000.4, 30: 9999.0}.get(pid)

    monkeypatch.setattr("modules.agents.claude_process_reaper._process_start_time", _start)

    snap = running_agents.snapshot_running_agents(_make_controller())
    orphans = [r for r in snap["agents"] if r["state"] == "orphan"]

    # Only the genuinely-alive, identity-matching pid 10 is shown; the dead pid
    # 20 and the reused pid 30 are filtered out (no stale/false orphans).
    assert {o["pid"] for o in orphans} == {10}


# ---------------------------------------------------------------------------
# end_running_agent dispatch (unified End)
# ---------------------------------------------------------------------------


def test_end_orphan_kills_verified_owned_pid(monkeypatch):
    from modules.agents import claude_process_reaper as reaper

    rec = types.SimpleNamespace(pid=10, owner=reaper.AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0)
    monkeypatch.setattr(reaper, "_load_owned_process_registry", lambda *a, **k: [rec])
    monkeypatch.setattr(reaper, "_process_start_time", lambda pid: 1000.0)
    reap = _AsyncFlag(ret=1)
    monkeypatch.setattr(reaper, "_reap_pid_set", reap)

    res = asyncio.run(running_agents.end_running_agent(_make_controller(), state="orphan", pid=10))
    assert res["ok"] is True
    assert res["action"] == "killed_process"
    assert reap.called


def test_end_orphan_reaps_root_plus_descendants(monkeypatch):
    # Killing a leaked Claude orphan must also reap its descendants (node helpers),
    # mirroring the background sweep — otherwise the row disappears while children
    # leak with no registry root a later kill could target.
    from modules.agents import claude_process_reaper as reaper

    rec = types.SimpleNamespace(pid=100, owner=reaper.AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0)
    monkeypatch.setattr(reaper, "_load_owned_process_registry", lambda *a, **k: [rec])
    monkeypatch.setattr(reaper, "_process_start_time", lambda pid: 1000.0)
    # Tree: 100(root) -> 200(child) -> 300(grandchild); 999 is unrelated.
    ps_rows = [
        reaper.ClaudeProcessRow(pid=100, ppid=1, command="claude"),
        reaper.ClaudeProcessRow(pid=200, ppid=100, command="node helper"),
        reaper.ClaudeProcessRow(pid=300, ppid=200, command="node helper2"),
        reaper.ClaudeProcessRow(pid=999, ppid=1, command="unrelated"),
    ]
    monkeypatch.setattr(reaper, "_run_ps", lambda: "ignored")
    monkeypatch.setattr(reaper, "_parse_ps_rows", lambda _out: ps_rows)
    captured = {}

    async def _reap(target_pids, *, terminate_timeout, logger):
        captured["pids"] = set(target_pids)
        return len(target_pids)

    monkeypatch.setattr(reaper, "_reap_pid_set", _reap)

    res = asyncio.run(running_agents.end_running_agent(_make_controller(), state="orphan", pid=100))
    assert res["ok"] is True
    assert captured["pids"] == {100, 200, 300}  # root + descendants, NOT the unrelated 999


def test_end_orphan_falls_back_to_root_when_ps_unreadable(monkeypatch):
    # If the ps read fails during descendant expansion, still reap the verified
    # root pid (better than reaping nothing).
    from modules.agents import claude_process_reaper as reaper

    rec = types.SimpleNamespace(pid=55, owner=reaper.AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0)
    monkeypatch.setattr(reaper, "_load_owned_process_registry", lambda *a, **k: [rec])
    monkeypatch.setattr(reaper, "_process_start_time", lambda pid: 1000.0)

    def _boom():
        raise RuntimeError("ps unavailable")

    monkeypatch.setattr(reaper, "_run_ps", _boom)
    captured = {}

    async def _reap(target_pids, *, terminate_timeout, logger):
        captured["pids"] = set(target_pids)
        return len(target_pids)

    monkeypatch.setattr(reaper, "_reap_pid_set", _reap)

    res = asyncio.run(running_agents.end_running_agent(_make_controller(), state="orphan", pid=55))
    assert res["ok"] is True
    assert captured["pids"] == {55}


def test_end_orphan_excludes_other_registered_session_pids(monkeypatch):
    # A descendant that belongs to a DIFFERENT registered session (or its own
    # children) must NOT be reaped when ending one orphan — mirror the sweep's
    # owned-pid subtraction so we don't kill another live session's process.
    from modules.agents import claude_process_reaper as reaper

    root = types.SimpleNamespace(pid=100, owner=reaper.AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0)
    other = types.SimpleNamespace(pid=200, owner=reaper.AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0)
    monkeypatch.setattr(reaper, "_load_owned_process_registry", lambda *a, **k: [root, other])
    monkeypatch.setattr(reaper, "_process_start_time", lambda pid: 1000.0)
    # 100(root) -> 200(other owned) -> 300(other's child)
    ps_rows = [
        reaper.ClaudeProcessRow(pid=100, ppid=1, command="claude"),
        reaper.ClaudeProcessRow(pid=200, ppid=100, command="claude"),
        reaper.ClaudeProcessRow(pid=300, ppid=200, command="node helper"),
    ]
    monkeypatch.setattr(reaper, "_run_ps", lambda: "ignored")
    monkeypatch.setattr(reaper, "_parse_ps_rows", lambda _out: ps_rows)
    captured = {}

    async def _reap(target_pids, *, terminate_timeout, logger):
        captured["pids"] = set(target_pids)
        return len(target_pids)

    monkeypatch.setattr(reaper, "_reap_pid_set", _reap)

    res = asyncio.run(running_agents.end_running_agent(_make_controller(), state="orphan", pid=100))
    assert res["ok"] is True
    # 200 (other owned) and its child 300 are excluded; only the verified root remains.
    assert captured["pids"] == {100}


def test_end_orphan_never_reaps_the_service_tree(monkeypatch):
    # If the avibe service pid (or its descendants) somehow appears under the
    # orphan root, it must never be signalled.
    from modules.agents import claude_process_reaper as reaper

    root = types.SimpleNamespace(pid=100, owner=reaper.AVIBE_CLAUDE_SESSION_OWNER, started_at=1000.0)
    monkeypatch.setattr(reaper, "_load_owned_process_registry", lambda *a, **k: [root])
    monkeypatch.setattr(reaper, "_process_start_time", lambda pid: 1000.0)
    # 100(root) -> 200(== service pid) -> 300(service child)
    ps_rows = [
        reaper.ClaudeProcessRow(pid=100, ppid=1, command="claude"),
        reaper.ClaudeProcessRow(pid=200, ppid=100, command="python avibe"),
        reaper.ClaudeProcessRow(pid=300, ppid=200, command="python worker"),
    ]
    monkeypatch.setattr(reaper, "_run_ps", lambda: "ignored")
    monkeypatch.setattr(reaper, "_parse_ps_rows", lambda _out: ps_rows)
    monkeypatch.setattr(running_agents.os, "getpid", lambda: 200)  # avibe service is pid 200
    captured = {}

    async def _reap(target_pids, *, terminate_timeout, logger):
        captured["pids"] = set(target_pids)
        return len(target_pids)

    monkeypatch.setattr(reaper, "_reap_pid_set", _reap)

    res = asyncio.run(running_agents.end_running_agent(_make_controller(), state="orphan", pid=100))
    assert res["ok"] is True
    assert 200 not in captured["pids"] and 300 not in captured["pids"]
    assert captured["pids"] == {100}


def test_end_orphan_refuses_unowned_pid(monkeypatch):
    from modules.agents import claude_process_reaper as reaper

    monkeypatch.setattr(reaper, "_load_owned_process_registry", lambda *a, **k: [])
    reap = _AsyncFlag(ret=1)
    monkeypatch.setattr(reaper, "_reap_pid_set", reap)

    res = asyncio.run(running_agents.end_running_agent(_make_controller(), state="orphan", pid=999))
    assert res["ok"] is False
    assert reap.called is False  # never kills a pid avibe doesn't own


def test_end_orphan_refuses_when_identity_unprovable(monkeypatch):
    # An avibe-owned record with NO recorded start time cannot be distinguished
    # from a reused pid → fail closed (never kill), matching the read path.
    from modules.agents import claude_process_reaper as reaper

    rec = types.SimpleNamespace(pid=10, owner=reaper.AVIBE_CLAUDE_SESSION_OWNER, started_at=None)
    monkeypatch.setattr(reaper, "_load_owned_process_registry", lambda *a, **k: [rec])
    monkeypatch.setattr(reaper, "_process_start_time", lambda pid: 1000.0)
    reap = _AsyncFlag(ret=1)
    monkeypatch.setattr(reaper, "_reap_pid_set", reap)

    res = asyncio.run(running_agents.end_running_agent(_make_controller(), state="orphan", pid=10))
    assert res["ok"] is False
    assert res["error"] == "identity_unprovable"
    assert reap.called is False


def test_end_claude_interrupts_disconnects_and_reaps_subprocess(monkeypatch):
    interrupt = _AsyncFlag()
    cleanup = _AsyncFlag()
    client = types.SimpleNamespace(interrupt=interrupt, _fake_pid=4321)
    session_handler = types.SimpleNamespace(claude_sessions={"slack_1:/w": client}, cleanup_session=cleanup)
    controller = _make_controller()
    controller.session_handler = session_handler
    reap = _AsyncFlag(ret=1)
    monkeypatch.setattr("modules.agents.claude_process_reaper._reap_pid_set", reap)

    res = asyncio.run(
        running_agents.end_running_agent(controller, backend="claude", composite_key="slack_1:/w")
    )
    assert res["ok"] is True
    assert interrupt.called and cleanup.called
    # The subprocess is reaped promptly (not left as an orphan for the sweeper).
    assert reap.called and res["process_killed"] is True and res["pid"] == 4321


def test_end_claude_routes_registered_adapter_teardown(monkeypatch):
    interrupt = _AsyncFlag()
    cleanup = _AsyncFlag()
    end_runtime = _AsyncFlag(ret=True)
    client = types.SimpleNamespace(interrupt=interrupt, _fake_pid=4321)
    session_handler = types.SimpleNamespace(
        claude_sessions={"slack_1:/w": client},
        cleanup_session=cleanup,
    )
    controller = _make_controller()
    controller.session_handler = session_handler
    controller.agent_service.agents["claude"] = types.SimpleNamespace(
        end_runtime_session=end_runtime
    )
    reap = _AsyncFlag(ret=1)
    monkeypatch.setattr("modules.agents.claude_process_reaper._reap_pid_set", reap)

    res = asyncio.run(
        running_agents.end_running_agent(
            controller,
            backend="claude",
            composite_key="slack_1:/w",
        )
    )

    assert res["ok"] is True
    assert end_runtime.called
    assert not interrupt.called and not cleanup.called
    assert reap.called and res["process_killed"] is True


def test_end_claude_session_not_live():
    session_handler = types.SimpleNamespace(claude_sessions={}, cleanup_session=_AsyncFlag())
    controller = _make_controller()
    controller.session_handler = session_handler
    res = asyncio.run(running_agents.end_running_agent(controller, backend="claude", composite_key="missing:/w"))
    assert res["ok"] is False
    assert res["error"] == "session_not_live"


def test_end_codex_interrupts_clears_and_stops_last_transport():
    send = _AsyncFlag()
    stop = _AsyncFlag()
    transport = types.SimpleNamespace(send_request=send, stop=stop)
    cleared = {}
    transports = {"/w": transport}
    mgr = types.SimpleNamespace(
        get_cwd=lambda b: "/w",
        get_thread_id=lambda b: "th1",
        clear=lambda b: cleared.__setitem__("inv", b),
        sessions_for_cwd=lambda cwd: [],  # this was the last session on the cwd
    )
    treg = types.SimpleNamespace(
        get_active_turn=lambda b: "turn1",
        clear_session=lambda b: cleared.__setitem__("clr", b),
    )
    codex = types.SimpleNamespace(
        _session_mgr=mgr, _turn_registry=treg, _transports=transports, _transport_last_activity={"/w": 0.0}
    )
    res = asyncio.run(
        running_agents.end_running_agent(_make_controller(codex=codex), backend="codex", base_session_id="b1")
    )
    assert res["ok"] is True
    assert send.called  # turn/interrupt RPC sent
    assert cleared.get("inv") == "b1" and cleared.get("clr") == "b1"
    # Last session on the cwd → the shared app-server transport is stopped + dropped.
    assert stop.called and res["process_killed"] is True and "/w" not in transports


def test_end_codex_keeps_transport_when_other_sessions_share_cwd():
    transport = types.SimpleNamespace(send_request=_AsyncFlag(), stop=_AsyncFlag())
    transports = {"/w": transport}
    mgr = types.SimpleNamespace(
        get_cwd=lambda b: "/w",
        get_thread_id=lambda b: "th1",
        clear=lambda b: None,
        sessions_for_cwd=lambda cwd: ["other-base"],  # another session still uses it
    )
    treg = types.SimpleNamespace(get_active_turn=lambda b: None, clear_session=lambda b: None)
    codex = types.SimpleNamespace(_session_mgr=mgr, _turn_registry=treg, _transports=transports)
    res = asyncio.run(
        running_agents.end_running_agent(_make_controller(codex=codex), backend="codex", base_session_id="b1")
    )
    assert res["ok"] is True
    # Shared transport stays up; not stopped, still registered.
    assert res["process_killed"] is False and "/w" in transports


def test_end_opencode_cancels_active_task():
    class _Task:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    task = _Task()
    oc = types.SimpleNamespace(
        _active_requests={"b1": task},
        _session_manager=types.SimpleNamespace(get_request_session=lambda b: None),
    )
    # OpenCode rows are inherently in-flight (an active request task), so the live
    # recheck classifies this as active: End runs the canonical stop, then
    # _end_opencode cancels the local polling task.
    async def _handle_stop(_context):
        return True

    controller = _make_controller(opencode=oc)
    controller.session_turns = types.SimpleNamespace(is_in_flight=lambda sid: False)
    controller.command_handler = types.SimpleNamespace(handle_stop=_handle_stop)
    res = asyncio.run(
        running_agents.end_running_agent(
            controller, backend="opencode", state="active", session_id="oc-s", base_session_id="b1"
        )
    )
    assert res["ok"] is True
    assert task.cancelled


def test_end_active_workbench_turn_settles_via_manager(monkeypatch):
    # An active turn owned by the Workbench FSM must be stopped through
    # SessionTurnManager.cancel. The real Claude stop path pops the SDK client
    # during cancel; End must still return success and must not run a duplicate
    # backend teardown that would now report session_not_live.
    sessions = {"slack_x:/w": types.SimpleNamespace(interrupt=_AsyncFlag())}

    class _WorkbenchCancel:
        def __init__(self):
            self.called = False

        async def __call__(self, _session_id):
            self.called = True
            sessions.pop("slack_x:/w", None)
            return {"ok": True, "status": "cancel_requested", "backend": "claude"}

    cancel = _WorkbenchCancel()
    # The live-state recheck verifies the in-flight turn belongs to THIS row before
    # promoting to active, so expose an in_flight entry whose context identifies the
    # same (claude) backend.
    wb_entry = types.SimpleNamespace(
        task=types.SimpleNamespace(done=lambda: False),
        context=types.SimpleNamespace(
            platform_specific={"agent_session_target": {"agent_backend": "claude"}}
        ),
    )
    manager = types.SimpleNamespace(
        is_in_flight=lambda sid: sid == "ses-wb",
        cancel=cancel,
        in_flight={"ses-wb": wb_entry},
    )
    cleanup = _AsyncFlag()
    controller = _make_controller()
    controller.session_turns = manager
    controller.session_handler = types.SimpleNamespace(claude_sessions=sessions, cleanup_session=cleanup)
    monkeypatch.setattr("modules.agents.claude_process_reaper._reap_pid_set", _AsyncFlag(ret=0))

    res = asyncio.run(
        running_agents.end_running_agent(
            controller, backend="claude", state="active", session_id="ses-wb", composite_key="slack_x:/w"
        )
    )
    assert res["ok"] is True
    assert cancel.called and res.get("turn_settled") is True
    assert cleanup.called is False
    assert sessions == {}


def test_end_active_im_turn_uses_canonical_stop_path(monkeypatch):
    # IM turns never enter Workbench in_flight, but active End must still use the
    # canonical /stop path so backend adapters release pending requests, runtime
    # gates, and terminal silent results before their registries change.
    cancel = _AsyncFlag()
    manager = types.SimpleNamespace(is_in_flight=lambda sid: False, cancel=cancel)
    seen = {}

    async def _handle_stop(context):
        seen["context"] = context
        return True

    command_handler = types.SimpleNamespace(handle_stop=_handle_stop)
    cleanup = _AsyncFlag()
    controller = _make_controller()
    # A genuinely-active IM turn is registered in claude_active_sessions; the
    # server-side live-state recheck reads this to confirm the active path.
    controller.claude_active_sessions = {"slack_y:/w"}
    controller.session_turns = manager
    controller.command_handler = command_handler
    controller.session_handler = types.SimpleNamespace(claude_sessions={"slack_y:/w": object()}, cleanup_session=cleanup)
    monkeypatch.setattr("modules.agents.claude_process_reaper._reap_pid_set", _AsyncFlag(ret=0))

    res = asyncio.run(
        running_agents.end_running_agent(
            controller, backend="claude", state="active", session_id="slack-im", composite_key="slack_y:/w"
        )
    )
    assert res["ok"] is True
    assert cancel.called is False
    assert res["action"] == "stopped"
    assert res["turn_settled"] is False
    assert cleanup.called is False
    payload = seen["context"].platform_specific
    assert payload["backend_base_session_id"] == "slack_y"
    assert payload["backend_composite_session_id"] == "slack_y:/w"
    assert payload["agent_session_target"]["agent_backend"] == "claude"
    assert payload["suppress_stop_no_active_notice"] is True


def test_end_active_agent_run_binds_stop_context_to_matching_turn_sink(monkeypatch):
    from core.scheduled_tasks import ParsedSessionKey, ResolvedSessionIdTarget
    from core.session_turns import SessionTurnManager
    from modules.im import MessageContext

    session_id = "ses-run"
    base_session_id = "slack_private-agent-abc"
    sink_done = asyncio.Event()
    seen = {}

    target = ResolvedSessionIdTarget(
        session_id=session_id,
        session_key=ParsedSessionKey(
            platform="slack",
            scope_type="channel",
            scope_id="private-agent-run-scope",
            thread_id="private-agent-abc",
        ),
        agent_backend="codex",
        agent_variant="codex",
        native_session_id="",
        agent_name="codex",
        workdir="/w",
        session_anchor=base_session_id,
        metadata={"no_delivery": True},
        suppress_delivery=True,
    )
    monkeypatch.setattr(
        "core.scheduled_tasks.resolve_session_id_target",
        lambda sid: target if sid == session_id else None,
    )

    async def _handle_stop(context):
        seen["context"] = context
        return True

    class _Controller:
        platform_settings_managers = {}

        def __init__(self):
            self.command_handler = types.SimpleNamespace(handle_stop=_handle_stop)
            self.session_turns = SessionTurnManager(self)

        def _get_session_key(self, context):
            return f"{context.platform}::{context.channel_id}"

        def bind_context_to_turn_sink(self, context, *, agent_session_id=None, backend_base_session_id=None):
            return self.session_turns.bind_context_to_turn_sink(
                context,
                agent_session_id=agent_session_id,
                backend_base_session_id=backend_base_session_id,
            )

        def settle_bound_turn_sink(self, binding):
            return self.session_turns.settle_bound_turn_sink(binding)

    controller = _Controller()
    dispatch_context = MessageContext(
        user_id="scheduled",
        channel_id="private-agent-run-scope",
        platform="slack",
        thread_id="private-agent-abc",
        platform_specific={
            "agent_session_id": session_id,
            "task_trigger_kind": "agent_run",
            "task_execution_id": "run-primary",
            "coalesced_queue": {"execution_ids": ["run-coalesced"]},
            "agent_session_target": {
                "id": session_id,
                "agent_backend": "codex",
                "session_anchor": base_session_id,
            },
        },
    )
    controller.session_turns.register_turn_sink(
        "slack::private-agent-run-scope",
        on_chunk=lambda _envelope: None,
        done_event=sink_done,
        turn_token="sink-token",
        context=dispatch_context,
    )

    res = asyncio.run(
        running_agents._stop_active_agent(
            controller,
            backend="codex",
            session_id=session_id,
            composite_key=None,
            base_session_id=base_session_id,
        )
    )

    assert res["ok"] is True
    assert res["turn_settled"] is True
    assert sink_done.is_set()
    stopped_context = seen["context"]
    assert controller._get_session_key(stopped_context) == "slack::private-agent-run-scope"
    assert stopped_context.platform_specific["turn_token"] == "sink-token"
    assert stopped_context.platform_specific["task_trigger_kind"] == "agent_run"
    assert stopped_context.platform_specific["task_execution_id"] == "run-primary"
    assert stopped_context.platform_specific["coalesced_queue"] == {"execution_ids": ["run-coalesced"]}
    assert stopped_context.platform_specific["agent_session_target"]["session_anchor"] == base_session_id


def test_end_active_agent_run_does_not_settle_mismatched_turn_sink(monkeypatch):
    from core.scheduled_tasks import ParsedSessionKey, ResolvedSessionIdTarget
    from core.session_turns import SessionTurnManager
    from modules.im import MessageContext

    session_id = "ses-clicked"
    sink_done = asyncio.Event()
    target = ResolvedSessionIdTarget(
        session_id=session_id,
        session_key=ParsedSessionKey(
            platform="slack",
            scope_type="channel",
            scope_id="private-agent-run-scope",
            thread_id="private-agent-abc",
        ),
        agent_backend="codex",
        agent_variant="codex",
        native_session_id="",
        session_anchor="slack_private-agent-clicked",
    )
    monkeypatch.setattr(
        "core.scheduled_tasks.resolve_session_id_target",
        lambda sid: target if sid == session_id else None,
    )

    seen = {}

    async def _handle_stop(context):
        seen["context"] = context
        return True

    class _Controller:
        platform_settings_managers = {}

        def __init__(self):
            self.command_handler = types.SimpleNamespace(handle_stop=_handle_stop)
            self.session_turns = SessionTurnManager(self)

        def _get_session_key(self, context):
            return f"{context.platform}::{context.channel_id}"

        def bind_context_to_turn_sink(self, context, *, agent_session_id=None, backend_base_session_id=None):
            return self.session_turns.bind_context_to_turn_sink(
                context,
                agent_session_id=agent_session_id,
                backend_base_session_id=backend_base_session_id,
            )

        def settle_bound_turn_sink(self, binding):
            return self.session_turns.settle_bound_turn_sink(binding)

    controller = _Controller()
    newer_context = MessageContext(
        user_id="scheduled",
        channel_id="private-agent-run-scope",
        platform="slack",
        platform_specific={
            "agent_session_id": "ses-newer",
            "task_trigger_kind": "agent_run",
            "task_execution_id": "run-newer",
            "agent_session_target": {
                "id": "ses-newer",
                "agent_backend": "codex",
                "session_anchor": "slack_private-agent-newer",
            },
        },
    )
    controller.session_turns.register_turn_sink(
        "slack::private-agent-run-scope",
        on_chunk=lambda _envelope: None,
        done_event=sink_done,
        turn_token="new-token",
        context=newer_context,
    )

    res = asyncio.run(
        running_agents._stop_active_agent(
            controller,
            backend="codex",
            session_id=session_id,
            composite_key=None,
            base_session_id="slack_private-agent-clicked",
        )
    )

    assert res["ok"] is True
    assert res["turn_settled"] is False
    assert not sink_done.is_set()
    stopped_context = seen["context"]
    assert stopped_context.platform_specific.get("turn_token") is None
    assert stopped_context.platform_specific.get("task_trigger_kind") is None
    assert stopped_context.platform_specific.get("task_execution_id") is None


def test_end_active_codex_frees_runtime_after_stop():
    # Active Codex End must FREE the runtime after the canonical stop (which only
    # interrupts the turn): clear the session mappings + stop the now-unused shared
    # transport so the row disappears instead of forcing a second Disconnect.
    async def _handle_stop(context):
        return True

    cleared = {}
    transport = types.SimpleNamespace(send_request=_AsyncFlag(), stop=_AsyncFlag())
    transports = {"/w": transport}
    mgr = types.SimpleNamespace(
        get_cwd=lambda b: "/w",
        get_thread_id=lambda b: "th1",
        clear=lambda b: cleared.__setitem__("clr", b),
        sessions_for_cwd=lambda cwd: [],  # this was the last session on the cwd
    )
    treg = types.SimpleNamespace(
        get_active_turn=lambda b: "turn1",  # genuinely active per the live registry
        clear_session=lambda b: cleared.__setitem__("treg", b),
    )
    codex = types.SimpleNamespace(
        _session_mgr=mgr,
        _turn_registry=treg,
        _transports=transports,
        _transport_last_activity={"/w": 0.0},
        _runtime_turn_key_for_base_session=lambda b: f"{b}:/w",
    )
    controller = _make_controller(codex=codex)
    controller.session_turns = types.SimpleNamespace(is_in_flight=lambda sid: False, cancel=_AsyncFlag())
    controller.command_handler = types.SimpleNamespace(handle_stop=_handle_stop)

    res = asyncio.run(
        running_agents.end_running_agent(
            controller, backend="codex", state="active", session_id="ses-im", base_session_id="b1"
        )
    )
    assert res["ok"] is True
    # Canonical stop ran, THEN teardown cleared mappings + stopped the shared transport.
    assert cleared.get("clr") == "b1" and cleared.get("treg") == "b1"
    assert transport.stop.called and "/w" not in transports
    assert res["process_killed"] is True


def test_end_active_codex_clears_stale_row_even_when_stop_fails():
    # A stale-active codex row (turn still in the registry but the app-server died)
    # makes the canonical stop fail; End must still tear it down + release the gate
    # so the row clears instead of sticking forever.
    async def _handle_stop(context):
        return False

    cleared = {}
    mgr = types.SimpleNamespace(
        get_cwd=lambda b: "/w",
        get_thread_id=lambda b: None,
        clear=lambda b: cleared.__setitem__("clr", b),
        sessions_for_cwd=lambda cwd: [],
    )
    treg = types.SimpleNamespace(
        get_active_turn=lambda b: "stale-turn",
        clear_session=lambda b: cleared.__setitem__("treg", b),
    )
    codex = types.SimpleNamespace(
        _session_mgr=mgr,
        _turn_registry=treg,
        _transports={},  # app-server already gone
        _transport_last_activity={},
        _runtime_turn_key_for_base_session=lambda b: f"{b}:/w",
    )
    controller = _make_controller(codex=codex)
    controller.session_turns = types.SimpleNamespace(is_in_flight=lambda sid: False, cancel=_AsyncFlag())
    controller.command_handler = types.SimpleNamespace(handle_stop=_handle_stop)

    res = asyncio.run(
        running_agents.end_running_agent(
            controller, backend="codex", state="active", session_id="s", base_session_id="b1"
        )
    )
    assert res["ok"] is True  # teardown cleared the stale row despite the failed stop
    assert cleared.get("clr") == "b1" and cleared.get("treg") == "b1"


def test_end_unknown_target():
    res = asyncio.run(running_agents.end_running_agent(_make_controller(), backend="mystery"))
    assert res["ok"] is False
    assert res["error"] == "unknown_target"


def _controller_with_inflight(session_id, *, agent_backend=None, session_anchor=None, task_done=False):
    target = {}
    if agent_backend is not None:
        target["agent_backend"] = agent_backend
    if session_anchor is not None:
        target["session_anchor"] = session_anchor
    ctx = types.SimpleNamespace(
        platform_specific=({"agent_session_target": target} if target else {})
    )
    entry = types.SimpleNamespace(task=types.SimpleNamespace(done=lambda: task_done), context=ctx)
    controller = _make_controller()
    controller.session_turns = types.SimpleNamespace(in_flight={session_id: entry})
    return controller


def test_inflight_match_anchor_only():
    # The dominant production path: backend empty on the live context, but the
    # reliably-populated session_anchor matches the row → promote.
    c = _controller_with_inflight("s1", session_anchor="base-1")
    assert running_agents._inflight_turn_matches_row(
        c, session_id="s1", backend="codex", base_session_id="base-1"
    ) is True


def test_inflight_match_backend_only():
    # No anchor on the context, but backend matches → promote.
    c = _controller_with_inflight("s1", agent_backend="claude")
    assert running_agents._inflight_turn_matches_row(
        c, session_id="s1", backend="claude", base_session_id="base-1"
    ) is True


def test_inflight_no_match_same_backend_different_anchor():
    # Same backend but a different anchor → a different turn is running; do NOT
    # promote (anchor conflict wins over backend match).
    c = _controller_with_inflight("s1", agent_backend="claude", session_anchor="other-base")
    assert running_agents._inflight_turn_matches_row(
        c, session_id="s1", backend="claude", base_session_id="base-1"
    ) is False


def test_inflight_no_match_when_target_missing_or_task_done():
    # No agent_session_target at all → no positive evidence → not this row.
    c_empty = _controller_with_inflight("s1")
    assert running_agents._inflight_turn_matches_row(
        c_empty, session_id="s1", backend="claude", base_session_id="base-1"
    ) is False
    # A completed task is not in flight, even if identity would match.
    c_done = _controller_with_inflight("s1", session_anchor="base-1", task_done=True)
    assert running_agents._inflight_turn_matches_row(
        c_done, session_id="s1", backend="claude", base_session_id="base-1"
    ) is False


def test_end_does_not_cancel_unrelated_inflight_turn_of_other_backend():
    # Stale idle Codex row, but the SAME chat has since started a new Claude turn
    # (in flight under the same session_id). Ending the codex row must NOT cancel
    # the unrelated Claude turn — the live recheck sees a backend conflict, treats
    # the codex row as idle, and runs the idle codex teardown instead.
    cancel_called = {"v": False}

    async def _cancel(_sid):
        cancel_called["v"] = True
        return {"ok": True, "status": "cancel_requested", "backend": "claude"}

    inflight_ctx = types.SimpleNamespace(
        platform_specific={"agent_session_target": {"agent_backend": "claude", "session_anchor": "claude-base"}}
    )
    inflight_entry = types.SimpleNamespace(task=types.SimpleNamespace(done=lambda: False), context=inflight_ctx)
    manager = types.SimpleNamespace(
        is_in_flight=lambda sid: True,
        cancel=_cancel,
        in_flight={"chat-1": inflight_entry},
    )

    cleared = {}
    transport = types.SimpleNamespace(send_request=_AsyncFlag(), stop=_AsyncFlag())
    mgr = types.SimpleNamespace(
        get_cwd=lambda b: "/w",
        get_thread_id=lambda b: None,
        clear=lambda b: cleared.__setitem__("clr", b),
        sessions_for_cwd=lambda cwd: [],
    )
    treg = _FakeTurnRegistry({}, pending=set())  # the codex base is genuinely idle
    treg.clear_session = lambda b: cleared.__setitem__("treg", b)
    codex = types.SimpleNamespace(
        _session_mgr=mgr, _turn_registry=treg, _transports={"/w": transport}, _transport_last_activity={"/w": 0.0}
    )
    controller = _make_controller(codex=codex)
    controller.session_turns = manager

    res = asyncio.run(
        running_agents.end_running_agent(
            controller, backend="codex", state="active", session_id="chat-1", base_session_id="codex-base"
        )
    )
    assert res["ok"] is True
    assert cancel_called["v"] is False  # the unrelated in-flight Claude turn was NOT canceled
    assert cleared.get("clr") == "codex-base" and cleared.get("treg") == "codex-base"  # idle codex teardown ran


def test_end_rechecks_live_state_idle_to_active(monkeypatch):
    # The browser polled this Claude row as idle, but it went active before the
    # user clicked Disconnect. End must re-derive the live state server-side and
    # route through the canonical stop path (NOT the idle _end_claude teardown,
    # which would skip the runtime-gate / terminal-result release).
    seen = {}

    async def _handle_stop(context):
        seen["stopped"] = True
        return True

    cleanup = _AsyncFlag()
    controller = _make_controller()
    controller.claude_active_sessions = {"slack_z:/w"}  # live registry says ACTIVE now
    controller.session_turns = types.SimpleNamespace(is_in_flight=lambda sid: False, cancel=_AsyncFlag())
    controller.command_handler = types.SimpleNamespace(handle_stop=_handle_stop)
    controller.session_handler = types.SimpleNamespace(
        claude_sessions={"slack_z:/w": object()}, cleanup_session=cleanup
    )
    monkeypatch.setattr("modules.agents.claude_process_reaper._reap_pid_set", _AsyncFlag(ret=0))

    res = asyncio.run(
        running_agents.end_running_agent(
            controller,
            backend="claude",
            state="idle",  # stale client state
            session_id="slack-im",
            composite_key="slack_z:/w",
        )
    )
    assert res["ok"] is True
    assert seen.get("stopped") is True  # canonical active stop path was taken
    assert res["action"] == "stopped"
    assert cleanup.called is False  # idle _end_claude teardown was NOT used


def test_end_rechecks_live_state_active_to_idle():
    # The browser polled this Codex row as active, but the turn finished before
    # End. With no live active/pending turn, End re-derives idle and runs the idle
    # teardown directly (no canonical stop needed).
    cleared = {}
    transport = types.SimpleNamespace(send_request=_AsyncFlag(), stop=_AsyncFlag())
    mgr = types.SimpleNamespace(
        get_cwd=lambda b: "/w",
        get_thread_id=lambda b: None,
        clear=lambda b: cleared.__setitem__("clr", b),
        sessions_for_cwd=lambda cwd: [],
    )
    treg = _FakeTurnRegistry({}, pending=set())  # no active turn, no pending start
    treg.clear_session = lambda b: cleared.__setitem__("treg", b)
    codex = types.SimpleNamespace(
        _session_mgr=mgr, _turn_registry=treg, _transports={"/w": transport}, _transport_last_activity={"/w": 0.0}
    )
    controller = _make_controller(codex=codex)
    controller.session_turns = types.SimpleNamespace(is_in_flight=lambda sid: False)

    res = asyncio.run(
        running_agents.end_running_agent(
            controller, backend="codex", state="active", session_id="s", base_session_id="b1"
        )
    )
    assert res["ok"] is True
    assert cleared.get("clr") == "b1" and cleared.get("treg") == "b1"


def _settlement_service(tmp_path):
    """A REAL settlement service over a real (temp-home) store.

    End's settlement claims are about rows: which status a run lands in and whether
    it carries an interruption the user gets told about. A recording double would
    only prove a call was made, which is the assertion both owed tests were written
    to be stronger than.
    """

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore, TaskExecutionStore

    request_store = TaskExecutionStore()
    service = ScheduledTaskService(
        controller=types.SimpleNamespace(),
        store=ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )
    return service, request_store


def _running_harness_run(
    request_store,
    *,
    session_id: str,
    message: str,
    agent_backend: str | None = None,
    agent_name: str = "codex",
) -> str:
    """A running ``agent_runs`` row on ``session_id``.

    ``agent_backend`` is the column the run carries from its DISPATCH target, so it
    names the backend the run actually went to. Left unset by default because most
    callers here predate HFR-327 and exercise the unresolvable-identity path.

    ``agent_name`` matters since HFR-328: a blank ``agent_backend`` is now resolved
    from the name at enqueue, so a row is only genuinely unresolvable when NO Agents
    row claims its name. These temp-home tests seed no Agents rows at all, so every
    name here resolves to nothing; the HFR-328 caller passes an explicitly
    unclaimable one rather than relying on that.
    """

    from sqlalchemy import update

    from storage.db import create_sqlite_engine
    from storage.models import agent_runs

    request = request_store.enqueue_agent_run(
        session_key="slack::channel::C123",
        message=message,
        agent_name=agent_name,
        agent_backend=agent_backend,
    )
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == request.id)
            .values(session_id=session_id, status="running")
        )
    return request.id


def test_ending_an_active_row_settles_the_run_canceled_with_no_interruption_notice(
    tmp_path, monkeypatch
):
    """HFR-107: End is a user's decision, and the run must be recorded as one.

    The status and the ABSENCE of a notice are asserted together because either
    alone is satisfied by the wrong outcome: a terminality-only check passes against
    ``failed`` + ``interrupt_reason=stopped``, which would tell the user their run was
    interrupted by infrastructure moments after they pressed the button, and count
    against the definition's health. ``canceled`` with no interrupt reason and no
    owed notice is the whole contract (HFR-012 / HFR-037, from the other direction).
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    service, request_store = _settlement_service(tmp_path)
    run_id = _running_harness_run(
        request_store, session_id="ses-end-107", message="a turn the user stopped"
    )

    sessions = {"slack_x:/w": types.SimpleNamespace(interrupt=_AsyncFlag())}

    async def _cancel(_session_id):
        # The real manager pops the SDK client during cancel; End must still succeed.
        sessions.pop("slack_x:/w", None)
        return {"ok": True, "status": "cancel_requested", "backend": "claude"}

    wb_entry = types.SimpleNamespace(
        task=types.SimpleNamespace(done=lambda: False),
        context=types.SimpleNamespace(
            platform_specific={"agent_session_target": {"agent_backend": "claude"}}
        ),
    )
    controller = _make_controller()
    controller.scheduled_task_service = service
    controller.session_turns = types.SimpleNamespace(
        is_in_flight=lambda sid: sid == "ses-end-107",
        cancel=_cancel,
        in_flight={"ses-end-107": wb_entry},
        # The turn owns the run, so the pre-cancel snapshot is where its ownership
        # is recorded -- the manager's own maps are empty again by reconcile time.
        owned_agent_run_ids=lambda: {run_id},
    )
    controller.session_handler = types.SimpleNamespace(
        claude_sessions=sessions, cleanup_session=_AsyncFlag()
    )
    # The service reads the turn lane through ITS controller, which is the same
    # controller in production.
    service.controller = controller
    monkeypatch.setattr("modules.agents.claude_process_reaper._reap_pid_set", _AsyncFlag(ret=0))

    res = asyncio.run(
        running_agents.end_running_agent(
            controller,
            backend="claude",
            state="active",
            session_id="ses-end-107",
            composite_key="slack_x:/w",
        )
    )

    assert res["ok"] is True
    settled = request_store.get_run(run_id)
    assert settled is not None
    assert settled["status"] == "canceled"
    assert settled["completed_at"] is not None
    metadata = settled["metadata"] or {}
    assert "interrupt_reason" not in metadata
    assert "owed_failure_notice" not in metadata


def test_ending_an_idle_row_is_silent_and_settles_nothing(tmp_path, monkeypatch):
    """HFR-108: the unconditional settlement must be SAFE, not merely correct.

    End calls the cancellation whatever the live-state read returned, which is what
    removes the race where a scheduler execution acquires the session just after the
    read. That is only defensible if the idle case costs nothing: no run settled, no
    message, no error. A run sitting on the same session that this process never
    claimed is the sharpest version of the question, and it must survive untouched.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    service, request_store = _settlement_service(tmp_path)
    run_id = _running_harness_run(
        request_store, session_id="ses-end-108", message="somebody else's live run"
    )

    client = types.SimpleNamespace(interrupt=_AsyncFlag())
    sessions = {"slack_y:/w": client}
    cleanup = _AsyncFlag()
    stop_calls: list = []

    async def _handle_stop(context):
        stop_calls.append(context)
        return False

    controller = _make_controller()
    controller.scheduled_task_service = service
    controller.session_turns = types.SimpleNamespace(
        is_in_flight=lambda sid: False,
        in_flight={},
        owned_agent_run_ids=lambda: set(),
    )
    controller.session_handler = types.SimpleNamespace(
        claude_sessions=sessions, cleanup_session=cleanup
    )
    controller.command_handler = types.SimpleNamespace(handle_stop=_handle_stop)
    service.controller = controller

    res = asyncio.run(
        running_agents.end_running_agent(
            controller,
            backend="claude",
            state="idle",
            session_id="ses-end-108",
            composite_key="slack_y:/w",
        )
    )

    assert res["ok"] is True
    # Silent: the idle branch never reaches the stop path at all, so there is no
    # "nothing was running" message for it to suppress.
    assert stop_calls == []
    # ...and nothing was settled underneath it.
    survivor = request_store.get_run(run_id)
    assert survivor is not None
    assert survivor["status"] == "running"
    assert survivor["completed_at"] is None
    assert not (survivor["metadata"] or {}).get("interrupt_reason")


def test_end_awaits_the_stopped_turn_before_reconciling_and_teardown(tmp_path, monkeypatch):
    """HFR-333: End's step 3 says AWAIT both settlements, and the manager leg did not.

    ``SessionTurnManager.cancel``'s success path is ``turn.task.cancel()`` followed
    immediately by ``return {"status": "cancel_requested"}`` — fire and forget. It
    awaits internally only on the ``stale_released`` branch, where it cancelled a turn
    the backend could no longer stop. So ``_settle_workbench_turn`` used to return
    while the cancelled turn was still unwinding, and ``end_running_agent`` went
    straight on to the reconcile (whose active branch unions the manager lane's ids
    per HFR-324) and then to the BACKEND TEARDOWN.

    That inverts the ordering the whole teardown module exists to state: settle first,
    tear down second, because a dismantled backend can no longer settle its own turn.
    The turn's ``finally`` still owes the Model Hub provenance settle, the sink
    release, the settlement of the ``agent_runs`` rows it owned, and the status stamp
    — all of it racing a teardown that has already started.

    (The bot's framing was that the turn would "park through the Activity-aware
    settlement path". It would not: HFR-325 deliberately preserves master's immediate
    settle for ``stopped``. The defect is purely the un-awaited race.)

    The fix is local to End's manager leg — ``cancel``'s semantics are unchanged for
    every other caller. The task reference is captured BEFORE the cancel (which pops
    ``in_flight`` on some branches) and awaited unbounded, exactly as
    ``release_for_teardown`` does for the same reason.
    """

    from core import session_turns

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    service, request_store = _settlement_service(tmp_path)
    run_id = _running_harness_run(
        request_store, session_id="ses-end-333", message="a turn the user stopped"
    )

    events: list[str] = []

    async def _reap(*_args, **_kwargs):
        events.append("teardown")
        return 0

    monkeypatch.setattr("modules.agents.claude_process_reaper._reap_pid_set", _reap)
    monkeypatch.setattr(running_agents, "_claude_pid_for", lambda *a, **k: 4242)

    controller = _make_controller()
    controller.scheduled_task_service = service
    controller.command_handler = types.SimpleNamespace(handle_stop=_AsyncFlag(ret=True))
    controller.session_handler = types.SimpleNamespace(
        claude_sessions={}, cleanup_session=_AsyncFlag()
    )
    manager = session_turns.SessionTurnManager(controller)
    controller.session_turns = manager
    service.controller = controller

    holder: dict = {}

    async def _go():
        started = asyncio.Event()

        async def _turn_body():
            try:
                started.set()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # Stands in for ``_run``'s finally, which is not instantaneous: it
                # still owes the Model Hub settle, the sink release, the owned-run
                # settlement and the status stamp, each of which can yield.
                for _ in range(5):
                    await asyncio.sleep(0)
                events.append("turn_settled")
                manager.in_flight.pop("ses-end-333", None)
                raise

        task = asyncio.create_task(_turn_body())
        ctx = types.SimpleNamespace(
            platform_specific={
                "agent_session_id": "ses-end-333",
                "agent_session_target": {
                    "agent_backend": "claude",
                    "session_anchor": "slack_x",
                },
                # The turn owns the harness run, so the pre-stop snapshot is where
                # that ownership is recorded.
                "task_execution_id": run_id,
            }
        )
        manager.in_flight["ses-end-333"] = session_turns.Turn(task=task, context=ctx)
        await started.wait()

        result = await running_agents.end_running_agent(
            controller,
            backend="claude",
            state="active",
            session_id="ses-end-333",
            composite_key="slack_x:/w",
        )
        holder["turn_done_at_return"] = task.done()
        await asyncio.gather(task, return_exceptions=True)
        return result

    res = asyncio.run(_go())

    assert res["ok"] is True
    # (1) End did not return while its own stopped turn was still unwinding.
    assert holder["turn_done_at_return"] is True, (
        "End returned while the cancelled turn was still settling"
    )
    # (2) ...and the turn's settlement observably preceded the backend teardown.
    assert "turn_settled" in events and "teardown" in events
    assert events.index("turn_settled") < events.index("teardown"), (
        f"the backend was torn down under a mid-unwind turn: {events}"
    )
    # (3) The settlement recorded is the canonical stop's: a user decision.
    settled = request_store.get_run(run_id)
    assert settled is not None
    assert settled["status"] == "canceled"
    assert settled["completed_at"] is not None
    assert "interrupt_reason" not in (settled["metadata"] or {})


def test_shutdown_converges_a_slow_settlement_before_teardown(tmp_path, monkeypatch):
    """HFR-340: a settlement slower than its wait must still finish before teardown.

    ``cleanup_sync`` submits every stop with ``run_coroutine_threadsafe`` and blocks on
    ``future.result(timeout=...)`` inside a bare ``except Exception``. So a settlement
    that outlasts the wait — a turn whose unwind is slow, which is HFR-333's whole
    subject — raised ``TimeoutError``, was swallowed as "cleanup skipped", and the
    shutdown walked straight on to Codex ``shutdown_runtime`` while the settlement
    coroutine and its terminal writers were still running on the loop. Exactly the
    inversion the teardown module exists to forbid: a dismantled backend cannot settle
    its own turn, and the rows stayed ``running`` until restart recovery.

    Driven through the REAL ``cleanup_sync``, on the real two-thread shape, because the
    bug is entirely in that shape: a synchronous method on a non-loop thread, a loop
    that keeps running beside it. Only the wait is shortened, so the test does not
    spend the production one.

    The convergence is not a second implementation of the settlement — it is
    ``reconcile_session_teardown``, called synchronously from the cleanup thread over
    plain SQLite, so the selection and every exclusion are provably the ones the
    awaited path would have applied.
    """

    import threading

    from core import controller as controller_module
    from core import session_turns
    from core.controller import Controller

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    service, request_store = _settlement_service(tmp_path)
    run_id = _running_harness_run(
        request_store,
        session_id="ses-shutdown-340",
        message="a turn whose unwind outlasts the shutdown wait",
    )

    # Parameterized so the real path can be driven without a real 8s wait.
    monkeypatch.setattr(controller_module, "SHUTDOWN_SETTLEMENT_TIMEOUT_SECONDS", 0.1)

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    unwind_started = threading.Event()
    observed: dict = {}
    events: list[str] = []

    async def _turn_body():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # ``_run``'s finally, made deliberately slower than the wait: the Model Hub
            # settle, the sink release, the owned-run settlement, the status stamp.
            unwind_started.set()
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                events.append("turn_unwind_aborted")
                raise
            events.append("turn_unwound")
            raise

    async def _codex_shutdown():
        # THE INSTRUMENTED BACKEND TEARDOWN. Whatever the row says here is what the
        # shutdown was willing to dismantle a backend on top of.
        events.append("codex_teardown")
        row = request_store.get_run(run_id)
        observed["status_at_teardown"] = row["status"] if row else None
        observed["metadata_at_teardown"] = (row or {}).get("metadata") or {}
        observed["open_rows_at_teardown"] = [
            item["id"]
            for item in request_store.list_open_runs_for_session("ses-shutdown-340")
            if item.get("status") in {"queued", "running"}
        ]

    async def _noop_stop():
        return None

    async def _arm_turn():
        task = asyncio.ensure_future(_turn_body())
        ctx = types.SimpleNamespace(
            platform_specific={
                "agent_session_id": "ses-shutdown-340",
                "agent_session_target": {
                    "agent_backend": "codex",
                    "session_anchor": "slack_x",
                },
                # The turn owns the harness run; this is where the pre-settlement
                # ownership snapshot reads it from.
                "task_execution_id": run_id,
            }
        )
        manager.in_flight["ses-shutdown-340"] = session_turns.Turn(task=task, context=ctx)
        return task

    controller = types.SimpleNamespace()
    manager = session_turns.SessionTurnManager(controller)
    controller.session_turns = manager
    controller._loop = loop
    controller.cleanup_task = None
    controller.update_checker = types.SimpleNamespace(stop=lambda: None)
    controller.scheduled_task_service = service
    controller.watch_service = types.SimpleNamespace(stop=_noop_stop)
    controller.runtime_command_watcher = types.SimpleNamespace(stop=_noop_stop)
    controller.model_hub_turn_gateway = None
    controller.show_git_checkpoint_service = None
    controller.agent_service = types.SimpleNamespace(
        agents={"codex": types.SimpleNamespace(shutdown_runtime=_codex_shutdown)}
    )
    controller.receiver_tasks = {}
    controller.im_client = types.SimpleNamespace()
    controller._im_thread = None
    # Reached by ``release_for_teardown`` on the branch where the unwind wins the race
    # with the cancel; present so the test proves the settlement path rather than an
    # AttributeError inside it.
    controller.set_agent_status = lambda *_args, **_kwargs: None
    service.controller = controller
    for name in (
        "_settle_inflight_turns_for_shutdown",
        "_shutdown_settlement_debt",
        "_converge_abandoned_shutdown_settlement",
    ):
        setattr(controller, name, types.MethodType(getattr(Controller, name), controller))

    turn_task = asyncio.run_coroutine_threadsafe(_arm_turn(), loop).result(timeout=5)
    try:
        # The cleanup thread IS this thread — the production shape exactly: a
        # synchronous caller blocking while the loop keeps running beside it.
        Controller.cleanup_sync(controller)
        assert unwind_started.is_set(), "the settlement never reached the turn's unwind"
    finally:
        asyncio.run_coroutine_threadsafe(
            asyncio.wait([turn_task], timeout=5), loop
        ).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        loop.close()

    # (1) THE INVARIANT. The row was ALREADY terminal when the backend teardown ran —
    # pre-fix it was still ``running`` and the settlement was abandoned mid-flight.
    assert observed["status_at_teardown"] == "failed", (
        "Codex was torn down while the settlement still owed this run a terminal row"
    )
    assert observed["metadata_at_teardown"].get("interrupt_reason") == "restarted"
    # (2) Nothing was left behind for the busy session.
    assert observed["open_rows_at_teardown"] == []
    # (3) The user is owed the notice, stamped by the same guarded UPDATE that
    # terminalized the row rather than by a second write.
    notice = observed["metadata_at_teardown"]["owed_failure_notice"]
    assert notice["state"] == "pending"
    assert notice["failure_id"] == f"interrupt:{run_id}:restarted"
    # (4) WHAT THE CANCEL ACTUALLY COSTS, pinned rather than assumed.
    # ``release_for_teardown`` awaits ``asyncio.gather(turn.task)``, and cancelling the
    # settlement future cancels that gather — which cancels its CHILD. So the turn's
    # own unwind is TRUNCATED partway through: it never reaches its own settlement, and
    # the terminal row therefore cannot have come from it. That is not a regression the
    # convergence causes, it is the reason the convergence is required — the alternative
    # (let the coroutine run on) is the invariant violation itself, backends coming
    # apart under a writer nobody is waiting for.
    assert "turn_unwind_aborted" in events
    assert "turn_unwound" not in events
    assert "codex_teardown" in events

    # (5) DOUBLE-SETTLEMENT SAFETY, observed rather than asserted from the docs: the
    # cancelled settlement and the turn's own finally both drained afterwards through
    # the same ``queued|running``-scoped writer, and neither took the row back.
    final = request_store.get_run(run_id)
    assert final["status"] == "failed"
    assert final["metadata"]["interrupt_reason"] == "restarted"
    # (6) The HFR-330 holds a cancelled teardown leaves behind are counted and
    # released from ``finally``, so nothing leaked — and HFR-339's flag keeps
    # admission shut regardless.
    assert manager._teardown_admission == {}
    assert manager.is_admission_closed_for_shutdown() is True


def test_end_idle_branch_does_not_settle_a_turn_owned_by_a_newer_backend(
    tmp_path, monkeypatch
):
    """HFR-324: End's idle branch must not terminalize a turn it deliberately spared.

    The backend-switch shape that
    ``test_end_does_not_cancel_unrelated_inflight_turn_of_other_backend`` already
    pins at the CANCEL leg, carried one step further to the ROW. A stale codex row
    shares its session id with a Workbench turn that has since switched to claude;
    the live-state recheck correctly refuses to treat that turn as this row's, so
    End reclassifies the row idle and the canonical stop — the path that owns the
    manager lane and settles what it stops — never runs at all.

    What is left is ``cancel_session_scheduler_lane`` (``include_manager_lane=False``)
    plus the idle branch's reconcile, and the snapshot handed between them used to
    carry the manager lane's owned run ids: ids nothing in this call cancelled and
    nothing self-skipped. The reconcile then selected the live claude turn's row
    (ours INTERSECT this session INTERSECT still running) and settled it ``canceled``
    while the turn was mid-prompt — and ``settle_run_terminal`` is scoped to
    queued|running, so the turn's real outcome could never take the row back.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    service, request_store = _settlement_service(tmp_path)
    run_id = _running_harness_run(
        request_store, session_id="chat-1", message="a newer claude turn, still running"
    )

    # The same fixture shape as the cancel-leg test: an in-flight turn on this
    # session whose target names a DIFFERENT backend than the row being ended.
    inflight_ctx = types.SimpleNamespace(
        platform_specific={
            "agent_session_target": {"agent_backend": "claude", "session_anchor": "claude-base"}
        }
    )
    inflight_entry = types.SimpleNamespace(
        task=types.SimpleNamespace(done=lambda: False), context=inflight_ctx
    )
    manager = types.SimpleNamespace(
        is_in_flight=lambda sid: True,
        cancel=_AsyncFlag(),
        in_flight={"chat-1": inflight_entry},
        # The live turn owns the run: this is the only place that ownership is
        # recorded by the time a reconcile looks.
        owned_agent_run_ids=lambda: {run_id},
    )

    transport = types.SimpleNamespace(send_request=_AsyncFlag(), stop=_AsyncFlag())
    mgr = types.SimpleNamespace(
        get_cwd=lambda b: "/w",
        get_thread_id=lambda b: None,
        clear=lambda b: None,
        sessions_for_cwd=lambda cwd: [],
    )
    treg = _FakeTurnRegistry({}, pending=set())  # the codex base is genuinely idle
    treg.clear_session = lambda b: None
    codex = types.SimpleNamespace(
        _session_mgr=mgr,
        _turn_registry=treg,
        _transports={"/w": transport},
        _transport_last_activity={"/w": 0.0},
    )
    controller = _make_controller(codex=codex)
    controller.session_turns = manager
    controller.scheduled_task_service = service
    service.controller = controller

    res = asyncio.run(
        running_agents.end_running_agent(
            controller, backend="codex", state="active", session_id="chat-1", base_session_id="codex-base"
        )
    )

    assert res["ok"] is True
    # The newer backend's turn is still executing, so its row must still be open for
    # its own outcome — not carrying a terminal status End invented for it.
    survivor = request_store.get_run(run_id)
    assert survivor is not None
    assert survivor["status"] == "running"
    assert survivor["completed_at"] is None
    assert not (survivor["metadata"] or {}).get("interrupt_reason")
    # ...and the guarded writer still accepts the turn's real result afterwards,
    # which is precisely what a wrongly settled row makes impossible.
    assert request_store.settle_without_result(run_id, terminal_status="succeeded") == "succeeded"


def _seed_agent_session_row(session_id: str, *, agent_backend: str) -> None:
    """An ``agent_sessions`` row for ``session_id`` naming ``agent_backend``.

    The durable statement of WHICH backend a session-bound run executes on. Written
    by ``_reserve_runtime_session`` / the anchor claim in production; inserted
    directly here because these tests drive the lane maps rather than a real
    dispatch.
    """

    from sqlalchemy import insert

    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(agent_sessions).values(
                id=session_id,
                scope_id=None,
                agent_backend=agent_backend,
                agent_variant=agent_backend,
                session_anchor=session_id,
                native_session_id="",
                status="active",
                visibility="foreground",
                metadata_json="{}",
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
        )


def _end_with_scheduler_lane_owner(
    tmp_path,
    *,
    lane_backend,
    clicked_backend,
    lane_agent_name="codex",
    lane_session_backend=None,
):
    """End a codex row on a session whose scheduler lane is held by ``lane_backend``.

    Returns ``(result, task_cancelled, run_status, cleared)``. The lane owner is a
    REAL running row plus the two service maps ``cancel_session_executions`` Path 1
    reads (``_session_lock_cache`` -> lock key, ``_session_lock_owners`` -> run id)
    and the ``_inflight_executions`` task it would actually cancel.

    ``lane_session_backend`` seeds the SESSION row the lane owner is bound to
    (HFR-337). A default-routed run carries neither a stamped backend nor an
    ``agent_name``, so the session it executes in is the only thing left that names
    its backend.
    """

    service, request_store = _settlement_service(tmp_path)
    run_id = _running_harness_run(
        request_store,
        session_id="chat-1",
        message="a live execution holding this session's scheduler lane",
        agent_backend=lane_backend,
        agent_name=lane_agent_name,
    )
    if lane_session_backend:
        _seed_agent_session_row("chat-1", agent_backend=lane_session_backend)

    cleared = {}
    mgr = types.SimpleNamespace(
        get_cwd=lambda b: "/w",
        get_thread_id=lambda b: None,
        clear=lambda b: cleared.__setitem__("clr", b),
        sessions_for_cwd=lambda cwd: [],
    )
    treg = _FakeTurnRegistry({}, pending=set())
    treg.clear_session = lambda b: cleared.__setitem__("treg", b)
    codex = types.SimpleNamespace(
        _session_mgr=mgr,
        _turn_registry=treg,
        _transports={"/w": types.SimpleNamespace(send_request=_AsyncFlag(), stop=_AsyncFlag())},
        _transport_last_activity={"/w": 0.0},
    )
    controller = _make_controller(codex=codex)
    controller.session_turns = types.SimpleNamespace(
        is_in_flight=lambda sid: False,
        cancel=_AsyncFlag(),
        in_flight={},
        owned_agent_run_ids=lambda: set(),
    )
    controller.scheduled_task_service = service
    service.controller = controller

    async def _go():
        async def _busy():
            await asyncio.sleep(60)

        task = asyncio.create_task(_busy())
        await asyncio.sleep(0)
        service._session_lock_cache["chat-1"] = "sid:chat-1"
        service._session_lock_owners["sid:chat-1"] = run_id
        service._inflight_executions[run_id] = task

        res = await running_agents.end_running_agent(
            controller,
            backend=clicked_backend,
            state="idle",
            session_id="chat-1",
            base_session_id="codex-base",
        )
        cancelled = task.cancelled() or task.cancelling() > 0
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return res, cancelled

    res, cancelled = asyncio.run(_go())
    return res, cancelled, request_store.get_run(run_id)["status"], cleared


def test_end_of_stale_backend_row_does_not_cancel_the_sessions_new_backend_execution(
    tmp_path, monkeypatch
):
    """HFR-327: End's scheduler-lane cancel must be scoped to the CLICKED backend.

    ``end_running_agent`` calls ``cancel_session_scheduler_lane`` unconditionally,
    before the state branch, with no backend check at all. HFR-324 only stopped
    un-cancelled MANAGER ids from riding into the reconcile snapshot; scheduler-owned
    tasks are still both claimed AND actually cancelled, and the cancel reaches
    whatever the session lock owner names.

    So ending a stale Codex row after the session has switched to Claude cancelled
    the healthy Claude execution: the lock owner for ``chat-1`` is the live Claude
    run, ``_cancel(owner)`` interrupts its task, and the user's working turn dies
    because they cleaned up a dead row in the Running tab.

    The decisive identity is the LANE OWNER'S RUN ROW, not the session row.
    ``agent_sessions.agent_backend`` is write-once after the first native bind (a
    real switch supersedes the anchor and creates a NEW row, or is refused outright),
    so it cannot tell you who owns the lane NOW. ``agent_runs.agent_backend`` is
    stamped at enqueue from the resolved dispatch target, so it names the backend the
    in-flight run actually went to.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    res, cancelled, status, cleared = _end_with_scheduler_lane_owner(
        tmp_path, lane_backend="claude", clicked_backend="codex"
    )

    assert res["ok"] is True
    # The newer backend's execution was left alone...
    assert cancelled is False
    assert status == "running"
    # ...and End still did its own job: the stale codex runtime is torn down.
    assert cleared.get("clr") == "codex-base"
    assert cleared.get("treg") == "codex-base"


def test_end_still_cancels_the_scheduler_lane_when_the_backend_matches(tmp_path, monkeypatch):
    """HFR-327 companion: the same-backend path stays unconditional.

    The scoping must not reopen the race the unconditional cancel closes. ``live_state``
    is a READ and a scheduler-lane execution can take the session right after it, so a
    matching backend still cancels without consulting any state.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    res, cancelled, _status, cleared = _end_with_scheduler_lane_owner(
        tmp_path, lane_backend="codex", clicked_backend="codex"
    )

    assert res["ok"] is True
    assert cancelled is True
    assert cleared.get("clr") == "codex-base"


def test_end_refuses_to_cancel_an_unprovable_scheduler_lane(tmp_path, monkeypatch):
    """HFR-328, inverting the HFR-327 companion: unprovable ownership REFUSES.

    THIS TEST PINNED THE OPPOSITE IN ROUND 6. HFR-327 shipped its scoping fail-OPEN:
    a lane whose runs carried no backend "proves nothing", so End cancelled it
    anyway, and this test asserted that as the intended behaviour. Round 7 refuted
    the premise it rested on. A blank ``agent_runs.agent_backend`` was not a rare
    nullable-column edge case — it was the NORM for the entire scheduler lane.
    ``enqueue_task_run`` / ``enqueue_definition_run`` / ``enqueue_hook_send`` all
    passed ``agent_name`` and never a backend, so every ordinary scheduled,
    definition, watch and hook run left the column NULL. The guard therefore
    fail-opened on precisely the runs it was written to protect, and HFR-327's fix
    was a no-op for the common case: End of a stale row still cancelled and
    terminalized the session's healthy newer-backend execution.

    So the fix has two halves and this test pins the second. HFR-328 stamps the
    backend at enqueue (``test_definition_run_enqueue_stamps_the_agent_backend``),
    which makes a resolvable lane the normal case; what remains unresolvable is a
    legacy row or a name no Agents row claims — genuinely rare. For those the guard
    now REFUSES, because the two failures are not symmetric: an unsettled run is
    recovered by the staleness sweep and restart recovery, while a healthy foreign
    turn killed mid-flight is unrecoverable and settled ``canceled`` by a write its
    real result can never take back. The idle-state race stays open only for rows
    whose ownership cannot be proven at all.

    A lane with NO runs is a different answer and keeps the unconditional call: there
    is no foreign work to protect, and the call is still how End's active branch
    learns the manager-lane ownership it must reconcile (HFR-107).
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    res, cancelled, status, cleared = _end_with_scheduler_lane_owner(
        tmp_path,
        lane_backend=None,
        clicked_backend="codex",
        # No Agents row claims this name, so the enqueue stamp resolves nothing and
        # the row reaches End with a genuinely blank backend.
        lane_agent_name="a-name-no-agents-row-claims",
    )

    assert res["ok"] is True
    # The unprovable execution was left alone...
    assert cancelled is False
    assert status == "running"
    # ...and End still completed and tore down the clicked row's own runtime.
    assert cleared.get("clr") == "codex-base"
    assert cleared.get("treg") == "codex-base"


def test_default_routed_definition_lane_is_identifiable_to_end(tmp_path, monkeypatch):
    """HFR-337: HFR-328's residual — the run that pins no Agent at all.

    HFR-328 stamps ``agent_runs.agent_backend`` at enqueue by resolving the request's
    ``agent_name``. That closes the lane for a definition that PINS an Agent. It does
    nothing for the far more common shape: a definition that names no Agent and
    follows the routing ladder (session Agent -> per-channel scope override -> the
    global default in ``state_meta.default_agent_name``). Those enqueue with a blank
    name, so the resolver has nothing to resolve, the column stays NULL, and the lane
    reads as unidentifiable — which sends End down HFR-328's fail-closed branch. The
    cancel is SKIPPED and End proceeds to tear the runtime down anyway, leaving the
    execution and its ``running`` row live until the staleness sweep finds them.

    Fail-closed is the right backstop for a lane nothing can name; it is the wrong
    answer for a lane that is perfectly nameable through a fact nobody was reading.
    A session-bound run executes on its SESSION's backend — that is what
    ``_execute_request`` hands the dispatch (``agent_session_target.agent_backend``)
    and what ``_reserve_runtime_session`` writes when it mints the session. So the
    ownership is PROVABLE, and both directions must follow from the proof rather than
    from the absence of one: a matching backend cancels, a mismatched one skips.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    res, cancelled, status, cleared = _end_with_scheduler_lane_owner(
        tmp_path,
        # Default routing: no pinned Agent name, so nothing for the enqueue stamp to
        # resolve and a genuinely blank ``agent_runs.agent_backend``.
        lane_backend=None,
        lane_agent_name="",
        lane_session_backend="codex",
        clicked_backend="codex",
    )

    assert res["ok"] is True
    # Provably OURS: the same-backend cancel is unconditional again, instead of being
    # refused by a guard that could not tell whose lane it was.
    assert cancelled is True
    assert cleared.get("clr") == "codex-base"


def test_default_routed_lane_of_another_backend_is_still_spared_by_end(tmp_path, monkeypatch):
    """HFR-337 companion: proving ownership must not turn into cancelling everything.

    The mismatch direction of the same proof. A default-routed lane whose session
    runs on Claude is now identifiable, so End of a stale Codex row skips it for the
    HFR-327 reason — the mismatch — rather than for the HFR-328 reason (nobody could
    say). Both tests are needed: a fix that resolved the backend but ignored it would
    pass the matching case alone.
    """

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    res, cancelled, status, cleared = _end_with_scheduler_lane_owner(
        tmp_path,
        lane_backend=None,
        lane_agent_name="",
        lane_session_backend="claude",
        clicked_backend="codex",
    )

    assert res["ok"] is True
    assert cancelled is False
    assert status == "running"
    assert cleared.get("clr") == "codex-base"
    assert cleared.get("treg") == "codex-base"


def test_end_holds_admission_through_the_backend_teardown_without_draining(monkeypatch):
    """HFR-334: End closes the same reopen window, and deliberately does not drain.

    The audit that found the Codex eviction's missing hold found this leg too. End's
    stop cancels the manager turn and — since HFR-333 — awaits it, and that await runs
    the turn's ``finally``, whose first act is popping ``in_flight``. The runtime is
    not actually removed until the backend teardown below, so between the two the
    session reads IDLE with a live client still registered: a message arriving there
    dispatches onto the runtime being ended.

    THE DRAIN IS DECLINED, which is the difference from every eviction/cleanup caller.
    End is a user Stop, and Stop's contract is explicitly not to flush ("不清空"), so
    reopening onto a fresh runtime and immediately running the queued row would start
    the very work the user just stopped. The row stays durably queued for whatever
    they send next.

    Asserted where it matters — INSIDE the backend teardown, the last thing End does
    to the runtime — rather than on the call shape alone.
    """
    from core.session_teardown import hold_session_admission
    from core.session_turns import SessionTurnManager

    manager = SessionTurnManager(types.SimpleNamespace())
    controller = _make_controller()
    controller.session_turns = manager
    controller.sessions = types.SimpleNamespace(
        find_session_ids_for_anchor=lambda anchor, **_kw: ["sess-end"]
    )

    observed = {}

    async def _cleanup(*_args, **_kwargs):
        # The runtime is being dropped right now: admission must still be shut.
        observed["closed_during_teardown"] = manager.is_teardown_admission_closed(
            "sess-end"
        )

    client = types.SimpleNamespace(interrupt=_AsyncFlag(), _fake_pid=4321)
    controller.session_handler = types.SimpleNamespace(
        claude_sessions={"slack_1:/w": client}, cleanup_session=_cleanup
    )
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper._reap_pid_set", _AsyncFlag(ret=0)
    )

    recorded = []
    real_hold = hold_session_admission

    def _spy(controller_arg, session_id, *, admission_holds, drain_on_release=True):
        recorded.append((session_id, drain_on_release))
        return real_hold(
            controller_arg,
            session_id,
            admission_holds=admission_holds,
            drain_on_release=drain_on_release,
        )

    monkeypatch.setattr(running_agents, "hold_session_admission", _spy)

    res = asyncio.run(
        running_agents.end_running_agent(
            controller, backend="claude", composite_key="slack_1:/w"
        )
    )

    assert res["ok"] is True
    # The hold is taken against the session End resolved, and it opts out of the drain.
    assert recorded == [("sess-end", False)]
    assert observed["closed_during_teardown"] is True, (
        "admission reopened before End had removed the runtime it was ending"
    )
    # Released on the way out — a leaked hold is a permanently wedged session.
    assert manager.is_teardown_admission_closed("sess-end") is False
    # ...and nothing was drained: Stop does not flush the queue it left behind.
    assert manager._teardown_drain_owed == set()
    assert manager._teardown_drain_tasks == {}


def test_end_fallback_resolve_splits_a_path_shaped_agent_anchor_against_storage():
    """HFR-335: End's fallback resolve is a settlement path, so its split is verified.

    ``_teardown_session_id`` falls back to the runtime identity whenever the
    Running-tab row carries no ``session_id`` — a row built purely from live state. A
    composite key whose anchor embeds a path-looking routing-agent name
    (``base:/review:/repo``) splits lexically into an anchor that matches no row, so
    the fallback would resolve NOTHING and End would tear the runtime down with its
    runs still ``running``.

    Both halves matter: the anchor must be the stored one, and the workdir must be its
    complement — a teardown that resolves the right anchor against ``/review:/repo``
    matches nothing either.
    """
    anchor = "slack_T1:/review"
    workdir = "/repo"
    probes = []

    def _find(probe_anchor, *, workdir=None, agent_backend=None):
        probes.append((probe_anchor, workdir, agent_backend))
        if probe_anchor == anchor and workdir == "/repo":
            return ["sess-review"]
        return []

    controller = _make_controller()
    controller.sessions = types.SimpleNamespace(find_session_ids_for_anchor=_find)

    resolved = running_agents._teardown_session_id(
        controller,
        session_id=None,
        composite_key=f"{anchor}:{workdir}",
        base_session_id=None,
        backend="claude",
    )

    assert resolved == "sess-review"
    # Longest anchor first, so the stored one is the FIRST thing tried — the lexical
    # reading (``slack_T1`` + ``/review:/repo``) is never used.
    assert probes[0][:2] == (anchor, "/repo")
    assert all(probe[1] != "/review:/repo" for probe in probes)


def test_end_fallback_prefers_the_rows_own_base_session_id_without_extra_reads():
    """HFR-335: a row that already knows its anchor short-circuits the candidate walk.

    ``preferred_anchor`` exists so the common End case pays nothing for the
    disambiguation: the Running-tab row carries ``base_session_id``, which IS the
    anchor, so the matching candidate is picked outright. It also fixes the workdir
    half — pairing the row's anchor with a lexically-split workdir would resolve
    nothing even though the anchor was right.
    """
    anchor = "slack_T1:/review"
    probes = []

    def _find(probe_anchor, *, workdir=None, agent_backend=None):
        probes.append((probe_anchor, workdir, agent_backend))
        return ["sess-review"] if workdir == "/repo" else []

    controller = _make_controller()
    controller.sessions = types.SimpleNamespace(find_session_ids_for_anchor=_find)

    resolved = running_agents._teardown_session_id(
        controller,
        session_id=None,
        composite_key=f"{anchor}:/repo",
        base_session_id=anchor,
        backend="claude",
    )

    assert resolved == "sess-review"
    # Exactly one read: the real resolve. The candidate probe never ran.
    assert probes == [(anchor, "/repo", "claude")]
