"""PR7R probes: what current master actually does at the terminal boundary.

These are EVIDENCE tests for
``docs/plans/harness-run-reliability.md`` §7. Two of them are characterization
tests: they assert current, wrong behavior so the defect is executable rather
than asserted in prose. The implementation PR that fixes ``PR7R-F1`` /
``PR7R-F2`` must flip them -- that is the point, not an accident.

Every probe here is held to one rule, because review found the first draft
breaking it in four places: **the subject of the test must be the subject of
the claim.** A probe may not stand on a nearby passing assertion -- a constant
lookup, a metadata label, a helper downstream of the decision it is supposed to
be about. Where the real subject is out of reach in this unit, the claim is
narrowed to what is reached and the rest becomes a named probe in the matrix,
never a green test that reads like coverage.

Scenario ids: HFR-180 .. HFR-183.
"""

import ast
import asyncio
import inspect
import textwrap
import types

import pytest

from core.handlers.session_handler import SessionHandler
from core.services import running_agents
from core.session_turns import SessionTurnManager
from modules.agents.codex.turn_state import CodexTurnRegistry


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


def _calls_method_named(func, method_name: str) -> bool:
    """Does ``func``'s body contain a ``self.<method_name>(...)`` call?

    Used to join two halves of a production chain that a unit probe drives
    separately. Without it the join is a comment, and a refactor that moves the
    call would leave the probe green while the claim it supports stopped being
    true.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method_name
        for node in ast.walk(tree)
    )


# ----- HFR-180: PR7R-F1 ----------------------------------------------------


class _RealTeardownMarking:
    """The production intentional-teardown methods, on a bare object.

    Bound off ``SessionHandler`` rather than reimplemented: the classification
    this probe turns on is the real one, TTL and return-code set included.
    """

    _mark_claude_teardown_intentional = (
        SessionHandler._mark_claude_teardown_intentional
    )
    _is_intentional_teardown_signal = SessionHandler._is_intentional_teardown_signal
    claude_teardown_is_intentional = SessionHandler.claude_teardown_is_intentional


def test_end_tears_down_a_live_claude_turn_and_reclassifies_it_as_intentional():
    """HFR-180 / PR7R-F1: End skips the canonical stop when the probe is blind.

    ``ClaudeAgent.handle_message`` calls ``mark_session_active`` only after
    ``get_or_create_claude_session`` returns, so a Run whose turn was accepted
    while that call is still in flight is NOT in ``claude_active_sessions``. On
    the direct-IM / agent-run lane there is also no ``session_turns`` entry to
    fall back on -- that projection only exists for the Workbench lane -- so
    ``_resolve_live_state`` has nothing left to read and answers ``idle``.

    End then takes the idle branch straight into ``_end_claude``, and the probe
    follows the consequence two steps, no further:

    1. ``handle_stop`` never runs. It is the only path that emits ``stopped``
       -> ``canceled``, so the status Invariant 2 requires for a user Stop is
       now unreachable for this Run.
    2. The teardown marks the key intentional, so when the still-live turn dies
       of the resulting kill signal the REAL classifier calls it service
       cleanup rather than a backend fault. The Stop is erased from the record
       a second time.

    What this does NOT claim is the Run's final status. Reaching it needs an
    IM-scoped Harness Run driven to settlement, which is the probe named in the
    IM ``user_stop`` / ``resultless_termination`` cells. An earlier draft
    asserted ``backend_refresh`` here on the strength of two constant lookups
    from ``SETTLEMENT_TERMINAL_STATUS`` -- those pass no matter what End does,
    and the claim was wrong besides: ``SETTLED_BY_BACKEND_REFRESH`` is emitted
    by ``SessionTurnManager.release_for_backend_refresh``, on the other lane.
    """
    session_handler = _RealTeardownMarking()
    client = types.SimpleNamespace(
        # The turn is live; the CLI has not exited yet.
        _transport=types.SimpleNamespace(_process=types.SimpleNamespace(returncode=None))
    )
    session_handler.claude_sessions = {"slack_a:/w": client}
    session_handler.claude_intentional_teardowns = {}

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
    # Consequence 1. The canonical stop -- the ONLY path that emits a
    # ``stopped`` settlement -- was skipped entirely.
    assert controller.command_handler.handle_stop.called is False
    assert end_runtime_session.called is True

    # Consequence 2. ``end_runtime_session`` reaches the runtime teardown via
    # ``ClaudeAgent._cleanup_runtime_session`` -> ``cleanup_session``, whose
    # locked body marks the key. Asserted structurally so the two halves this
    # probe drives separately stay joined in production.
    assert _calls_method_named(
        SessionHandler._cleanup_session_locked, "_mark_claude_teardown_intentional"
    )
    session_handler._mark_claude_teardown_intentional("slack_a:/w", client)

    # Now the live turn dies of the teardown's own SIGTERM, and the real
    # classifier reads it. -15 is in ``CLAUDE_TEARDOWN_RETURNCODES``.
    client._transport._process.returncode = -15
    assert (
        session_handler.claude_teardown_is_intentional(
            "slack_a:/w",
            RuntimeError("Claude Code process exited with exit code: -15"),
            client=client,
        )
        is True
    ), "the user's Stop is now indistinguishable from routine service cleanup"


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


def test_the_accepted_run_batch_records_no_per_run_source_or_deadline():
    """HFR-182 / Q3: a Turn's accepted-run record cannot discriminate.

    Scoped deliberately to the half this reaches. ``_attach_accepted_agent_runs``
    is DOWNSTREAM of the decision it would be tempting to test here: it appends
    ids already attributed to a Turn, so driving it twice proves nothing about
    whether a cron Run and a manual CLI Run coalesce. That question belongs to
    ``SessionTurnManager._hydrate_delivery_batch_context``, which folds a
    Delivery batch into one context, and the matrix carries it as an open probe.

    What IS provable here is the consequence, and it holds however the merge
    happens: the Turn keeps a flat list of run ids and nothing per-Run. The
    Turn-level ``source_kind`` is whatever the FIRST participant stamped, so a
    cancellation consulting it would answer for a Run that may not be the one it
    is about. No per-Run timeout policy can be specified against that record.
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

    recorded = projected.context.platform_specific
    accepted = recorded["accepted_agent_run_ids"]
    assert accepted == ["run-cron", "run-manual-cli"]

    # The load-bearing assertion: for every accepted run id, the Turn holds no
    # keyed record of that run's source or deadline. Written as a search over
    # what was actually recorded rather than a check of two known keys, so a
    # future field that DID carry per-Run provenance would fail here and force
    # this verdict to be revisited.
    per_run_keys = [
        key
        for key, value in recorded.items()
        if isinstance(value, dict) and set(value) & set(accepted)
    ]
    assert per_run_keys == [], per_run_keys
    assert recorded["source_kind"] == "scheduler", (
        "one Turn-level label, stamped by the first participant"
    )


# ----- HFR-183: Q2 blocker -------------------------------------------------


def test_no_backend_keys_a_progress_signal_by_turn_on_either_lane():
    """HFR-183 / Q2: every backend stamps progress per session, not per Turn.

    The plan forbids a generic inactivity timeout unless every backend and lane
    has an exact-Turn progress signal, and states that session-wide activity is
    never an acceptable substitute.

    This exercises the three real progress-bearing structures rather than
    reading the evidence file's own labels back. An earlier draft did the
    latter: it asserted that ``EXACT_TURN_PROGRESS_SIGNALS`` says ``unproven``
    everywhere, which is true by construction and would stay green after a
    backend gained Turn correlation. The metadata cross-check is still made at
    the end, but only as a consistency tie -- the finding is established by the
    three probes above it.

    Neither lane changes any of it: all three structures are keyed by composite
    key or base session id, and a lane does not change a key.
    """
    # Claude. The progress baseline is stamped per COMPOSITE KEY, and the
    # method has no parameter that could carry a Turn.
    params = list(
        inspect.signature(SessionHandler.mark_session_turn_started).parameters
    )
    assert params == ["self", "composite_key"], params
    handler = object.__new__(SessionHandler)
    handler.active_sessions = set()
    handler.session_turn_started = {}
    handler.session_last_activity = {}
    handler.mark_session_turn_started("slack_a:/w")
    first_baseline = handler.session_turn_started["slack_a:/w"]
    # A second Turn accepted in the same composite key overwrites the first
    # one's baseline; afterwards nothing can report on the older Turn.
    handler.mark_session_turn_started("slack_a:/w")
    assert list(handler.session_turn_started) == ["slack_a:/w"]
    assert handler.session_turn_started["slack_a:/w"] >= first_baseline

    # Codex. The registry holds ONE active turn per base session, so a second
    # accepted turn displaces the first as the answer to "what is running".
    registry = CodexTurnRegistry()
    for turn_id in ("turn-1", "turn-2"):
        # Two distinct Runs, one base session -- the registry keys on the
        # latter, so the second registration overwrites the first.
        registry.register_turn(
            turn_id, types.SimpleNamespace(base_session_id="base-1")
        )
    assert registry.get_active_turn("base-1") == "turn-2"
    # Both turn states are still held, so the loss is specifically in the
    # base-session -> active-turn projection every progress read goes through.
    assert registry.get_turn("turn-1") is not None
    assert not registry.is_active_turn("turn-1"), (
        "the first turn is still live but no base-session read names it"
    )

    # OpenCode. One asyncio task slot per base session id, by annotation and by
    # every read/write site: ``_active_requests[request.base_session_id]``.
    from modules.agents.opencode.agent import OpenCodeAgent

    source = inspect.getsource(OpenCodeAgent)
    assert "self._active_requests: Dict[str, asyncio.Task] = {}" in source
    assert "self._active_requests[request.base_session_id] = task" in source
    assert "self._active_requests[turn" not in source

    # Consistency tie: the matrix must still say the same thing these three
    # probes just showed, for all six cells.
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

