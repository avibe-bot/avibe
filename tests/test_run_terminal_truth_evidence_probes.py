"""PR7R probes: what current master actually does at the terminal boundary.

These are EVIDENCE tests for
``docs/plans/harness-run-reliability.md`` §7. Two of them are characterization
tests: they assert current, wrong behavior so the defect is executable rather
than asserted in prose. The implementation PR that fixes ``PR7R-F1`` /
``PR7R-F2`` must flip them -- that is the point, not an accident.

Scenario ids: HFR-180 .. HFR-183.
"""

import asyncio
import types

import pytest

from core.run_settlement import (
    SETTLED_BY_BACKEND_REFRESH,
    SETTLED_BY_STOPPED,
    SETTLEMENT_TERMINAL_STATUS,
)
from core.services import running_agents
from core.session_turns import SessionTurnManager


class _AsyncFlag:
    """Awaitable spy: records that it ran and with what."""

    def __init__(self, result=None):
        self.called = False
        self.calls: list[tuple] = []
        self._result = result

    async def __call__(self, *args, **kwargs):
        self.called = True
        self.calls.append((args, kwargs))
        return self._result


@pytest.fixture(autouse=True)
def _no_process_probing(monkeypatch):
    # Keep the probes hermetic: never touch the real process table.
    monkeypatch.setattr(
        "modules.agents.claude_process_reaper.get_claude_client_pid",
        lambda client: None,
    )


# ----- HFR-180: PR7R-F1 ----------------------------------------------------


def test_end_live_state_race_settles_an_unmarked_claude_turn_as_a_refresh():
    """HFR-180 / PR7R-F1: End skips the canonical stop when the probe is blind.

    ``ClaudeAgent.handle_message`` calls ``mark_session_active`` only after
    ``get_or_create_claude_session`` returns, so a Run whose turn was accepted
    while that call is still in flight is NOT in ``claude_active_sessions``. On
    the direct-IM / agent-run lane there is also no ``session_turns`` entry to
    fall back on -- that projection only exists for the Workbench lane -- so
    ``_resolve_live_state`` has nothing left to read and answers ``idle``.

    End then takes the idle branch straight into ``_end_claude``. The canonical
    stop never runs, so nothing emits ``stopped``; the runtime cleanup marks an
    intentional teardown and the Run settles as ``backend_refresh``. Invariant
    2 says a user Stop is ``canceled``, and this path makes it ``failed``.
    """
    session_handler = types.SimpleNamespace(
        claude_sessions={"slack_a:/w": types.SimpleNamespace()},
        cleanup_session=_AsyncFlag(),
    )
    end_runtime_session = _AsyncFlag(result=True)
    controller = types.SimpleNamespace(
        agent_service=types.SimpleNamespace(
            agents={"claude": types.SimpleNamespace(end_runtime_session=end_runtime_session)}
        ),
        session_handler=session_handler,
        claude_sessions=session_handler.claude_sessions,
        # The live turn set has NOT been stamped yet -- this is the race window.
        claude_active_sessions=set(),
        session_last_activity={},
        # Direct-IM lane: no Workbench turn projection to rescue the probe.
        session_turns=types.SimpleNamespace(in_flight={}),
        command_handler=types.SimpleNamespace(handle_stop=_AsyncFlag(result=True)),
    )

    live_state = running_agents._resolve_live_state(
        controller,
        backend="claude",
        session_id="ses-im",
        composite_key="slack_a:/w",
        base_session_id="b1",
    )
    # The turn IS running; the probe cannot see it.
    assert live_state == "idle"

    result = asyncio.run(
        running_agents.end_running_agent(
            controller,
            backend="claude",
            # The browser polled the row while it was genuinely active.
            state="active",
            session_id="ses-im",
            composite_key="slack_a:/w",
            base_session_id="b1",
        )
    )

    assert result["ok"] is True and result["action"] == "ended"
    # The canonical stop -- the ONLY path that emits a ``stopped`` settlement --
    # was skipped entirely; the runtime was torn down underneath the live turn.
    assert controller.command_handler.handle_stop.called is False
    assert end_runtime_session.called is True

    # And the two settlements this chooses between are not interchangeable.
    assert SETTLEMENT_TERMINAL_STATUS[SETTLED_BY_STOPPED] == "canceled"
    assert SETTLEMENT_TERMINAL_STATUS[SETTLED_BY_BACKEND_REFRESH] == "failed"


# ----- HFR-181: PR7R-F2 ----------------------------------------------------


def test_codex_end_reports_ended_when_the_canonical_stop_never_interrupted():
    """HFR-181 / PR7R-F2: a failed codex stop is reported as a clean end.

    Clearing a stale-active codex row whose app-server died is deliberate --
    ``test_end_active_codex_clears_stale_row_even_when_stop_fails`` owns that
    behavior and it should stay. What this probe records is the part nobody
    owns: the value returned to the caller is synthesized fresh and is
    byte-identical to a stop that really settled the turn. The turn's session
    and turn-registry mappings are cleared, its Run is never settled by anyone,
    and the Web/API caller is told ``ok: True, action: "ended"``.

    The teardown is not the defect. The missing signal is.
    """
    cleared = {}
    session_mgr = types.SimpleNamespace(
        get_cwd=lambda b: "/w",
        get_thread_id=lambda b: None,
        clear=lambda b: cleared.__setitem__("session_mgr", b),
        sessions_for_cwd=lambda cwd: [],
    )
    turn_registry = types.SimpleNamespace(
        # The registry still holds the turn: it was never interrupted.
        get_active_turn=lambda b: "turn-that-outlived-its-transport",
        clear_session=lambda b: cleared.__setitem__("turn_registry", b),
    )
    codex = types.SimpleNamespace(
        _session_mgr=session_mgr,
        _turn_registry=turn_registry,
        _transports={},
        _transport_last_activity={},
        _runtime_turn_key_for_base_session=lambda b: f"{b}:/w",
    )
    handle_stop = _AsyncFlag(result=False)  # the canonical stop FAILS
    controller = types.SimpleNamespace(
        agent_service=types.SimpleNamespace(agents={"codex": codex}),
        claude_sessions={},
        claude_active_sessions=set(),
        session_last_activity={},
        session_turns=types.SimpleNamespace(is_in_flight=lambda sid: False, cancel=_AsyncFlag()),
        command_handler=types.SimpleNamespace(handle_stop=handle_stop),
    )

    result = asyncio.run(
        running_agents.end_running_agent(
            controller,
            backend="codex",
            state="active",
            session_id="ses-im",
            base_session_id="b1",
        )
    )

    assert handle_stop.called is True  # it ran, and it returned False
    assert cleared == {"session_mgr": "b1", "turn_registry": "b1"}

    # Current master's answer. The turn was NOT interrupted and its Run was NOT
    # settled, yet nothing in the payload says so -- no ``stop_failed``, no
    # ``interrupted: False``, no ``settled``. A fix must add that signal here.
    assert result == {"ok": True, "action": "ended", "backend": "codex"}
    assert "stop_failed" not in result
    assert "interrupted" not in result


# ----- HFR-182: Q3 merge cardinality ---------------------------------------


def test_the_turn_merge_key_admits_runs_with_different_source_semantics():
    """HFR-182 / Q3: the merge key is the Session, not the Run's source.

    ``_attach_accepted_agent_runs`` matches on ``session_id`` plus the logical
    turn id and nothing else, so a scheduler cron Run and a manual CLI Run --
    different ``source_kind``, different retirement rules, potentially
    different effective deadlines -- accumulate into ONE turn's
    ``accepted_agent_run_ids``.

    Cancellation is Turn-level. Until this cardinality is made explicit no
    per-Run timeout policy can be specified, because the Turn that would be
    cancelled owns Runs that never agreed on one.
    """
    manager = object.__new__(SessionTurnManager)
    projected = types.SimpleNamespace(
        logical_turn_id="turn-1",
        context=types.SimpleNamespace(platform_specific={"source_kind": "scheduler"}),
    )
    manager.in_flight = {"ses-1": projected}
    manager.active_turn_sinks = {}

    manager._attach_accepted_agent_runs(
        session_id="ses-1",
        turn_id="turn-1",
        run_ids=["run-cron"],
        context=None,
    )
    manager._attach_accepted_agent_runs(
        session_id="ses-1",
        turn_id="turn-1",
        # A manual `vibe task run` of a different definition, same Session.
        run_ids=["run-manual-cli"],
        context=None,
    )

    accepted = projected.context.platform_specific["accepted_agent_run_ids"]
    assert accepted == ["run-cron", "run-manual-cli"]
    # The turn recorded no source or deadline per participant, so there is
    # nothing a cancellation could consult to treat them differently.
    assert projected.context.platform_specific["source_kind"] == "scheduler"


# ----- HFR-183: Q2 blocker -------------------------------------------------


def test_no_backend_exposes_a_per_turn_progress_signal_on_either_lane():
    """HFR-183 / Q2: every backend/lane cell is still ``unproven``.

    The plan forbids a generic inactivity timeout unless every backend and lane
    has an exact-Turn progress signal, and states that session-wide activity is
    never an acceptable substitute. This asserts the current, blocking answer
    so an implementation PR cannot be opened on the assumption it was resolved
    quietly: closing a cell means editing
    ``EXACT_TURN_PROGRESS_SIGNALS`` and this assertion in the same commit.
    """
    from tests.run_terminal_truth_evidence import (
        BACKENDS,
        EXACT_TURN_PROGRESS_SIGNALS,
        LANES,
    )

    assert set(EXACT_TURN_PROGRESS_SIGNALS) == {
        (backend, lane) for backend in BACKENDS for lane in LANES
    }
    for cell, (kind, reason) in EXACT_TURN_PROGRESS_SIGNALS.items():
        assert kind == "unproven", cell
        assert "probe" in reason.lower(), cell
