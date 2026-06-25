"""Unit tests for the read-only running-agents snapshot aggregator.

Hermetic: the DB enrichment and the on-disk Claude process registry are
monkeypatched out, so the test exercises only the in-memory aggregation logic
against fake controller registries — it never touches real state.
"""

from __future__ import annotations

import asyncio
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
    def __init__(self, active_by_base):
        self._active = active_by_base

    def get_active_turn(self, base):
        return self._active.get(base)


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


def test_private_agent_run_not_labeled_slack():
    # ``reserve_private_agent_session`` stores private agent runs with a
    # placeholder ``platform="slack"`` but ``native_type="private_agent_run"``.
    # Such a row must NOT be shown as a Slack session.
    row = running_agents._make_row(backend="codex", state="idle", base_session_id="b1")
    meta = {
        "id": "ses0000priv",
        "scope_id": "slack::channel::private-agent-run-abc123",
        "scope_platform": "slack",
        "scope_scope_type": "channel",
        "scope_display_name": "Private Agent Run",
        "scope_native_type": "private_agent_run",
        "title": "[Current Task] review",
        "agent_name": "codex",
        "workdir": None,
    }
    running_agents._apply_session_meta(row, meta)

    assert row["platform"] is None  # not "slack"
    assert row["trigger_source"] == "agent"
    assert row["openable_in_chat"] is False
    assert row["scope_display_name"] == "Private Agent Run"


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
    res = asyncio.run(
        running_agents.end_running_agent(_make_controller(opencode=oc), backend="opencode", base_session_id="b1")
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
    manager = types.SimpleNamespace(is_in_flight=lambda sid: sid == "ses-wb", cancel=cancel)
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
        get_active_turn=lambda b: None,
        clear_session=lambda b: cleared.__setitem__("treg", b),
    )
    codex = types.SimpleNamespace(
        _session_mgr=mgr, _turn_registry=treg, _transports=transports, _transport_last_activity={"/w": 0.0}
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


def test_end_unknown_target():
    res = asyncio.run(running_agents.end_running_agent(_make_controller(), backend="mystery"))
    assert res["ok"] is False
    assert res["error"] == "unknown_target"
