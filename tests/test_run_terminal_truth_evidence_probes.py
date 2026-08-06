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
from modules.agents.codex.agent import CodexAgent
from modules.agents.codex.event_handler import CodexEventHandler
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
    """Does ``func``'s body reach ``method_name``?

    Used to join two halves of a production chain that a unit probe drives
    separately. Without it the join is a comment, and a refactor that moves the
    call would leave the probe green while the claim it supports stopped being
    true. Shares ``_first_reference_line``'s resolution so the two helpers
    cannot disagree about what "calls" means.
    """
    try:
        _first_reference_line(func, method_name)
    except AssertionError:
        return False
    return True


def _first_reference_line(func, method_name: str) -> int:
    """Source line where ``func`` first reaches ``method_name``.

    Ordering, not mere presence. A claim of the form "A happens only AFTER B"
    is a claim about sequence, and a probe that asserts both calls exist would
    stay green the day production fixes the sequence -- still reporting the
    defect as reproduced.

    "Reaches" covers two spellings, because this file has now been bitten by
    the second one twice. A direct ``x.name(...)`` is the obvious form; the
    other is ``getattr(obj, "name", None)`` followed by a call on the bound
    result, which claude_agent uses for BOTH sites this probe cares about --
    the teardown dispatch and this very marking call. An attribute-call scan
    reports "does not call" for those, which is a false negative about
    production rather than a fact about it.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    lines = [
        node.lineno
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method_name
        )
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == method_name
        )
    ]
    assert lines, f"{func.__qualname__} never reaches {method_name}"
    return min(lines)


# ----- HFR-180: PR7R-F1 ----------------------------------------------------


class _RealTeardownMarking:
    """The production teardown/classification methods, on a bare object.

    Bound off ``SessionHandler`` rather than reimplemented: the classification
    this probe turns on is the real one, TTL and return-code set included.
    Round 3 added ``_cleanup_session_locked`` to the list. Stamping the marker
    by hand -- which the probe used to do -- skipped the production guard right
    above it (``if client is not None or receiver_task is not None``), so the
    probe was asserting a mark that production makes only conditionally, and
    would have kept passing if that condition stopped holding for End.
    """

    _cleanup_session_locked = SessionHandler._cleanup_session_locked
    _mark_claude_teardown_intentional = (
        SessionHandler._mark_claude_teardown_intentional
    )
    _is_intentional_teardown_signal = SessionHandler._is_intentional_teardown_signal
    claude_teardown_is_intentional = SessionHandler.claude_teardown_is_intentional

    def __init__(self) -> None:
        self.claude_sessions: dict = {}
        self.claude_intentional_teardowns: dict = {}
        self.receiver_tasks: dict = {}
        self.tracking_cleared: list[str] = []

    # Collaborators of ``_cleanup_session_locked`` that are not this probe's
    # subject. Each keeps the production signature so a change to it fails here
    # rather than silently skipping a step.
    def _retire_claude_runtime_activation(self, composite_key, client, still_owns):
        return True

    def _retire_model_hub_process_scope(self, composite_key):
        return None

    def clear_session_tracking(self, composite_key):
        self.tracking_cleared.append(composite_key)

    async def _disconnect_client(self, client, composite_key):
        return None

    def _disconnect_client_after_receiver(self, client, composite_key, task):
        return None

    async def _stop_receiver_task(self, receiver_task, composite_key=None):
        return None

    async def _reap_duplicate_resume_processes(self, *args, **kwargs):
        return None


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

    Round 3 closed two ways this probe could have outlived its own defect. The
    race used to be POSTULATED -- an empty ``claude_active_sessions`` handed in
    by the fixture -- so the day ``handle_message`` starts stamping the live set
    before it creates the session, the window closes and this test still passes,
    still reporting F1 as reproduced. The ordering is now read out of
    production. And the teardown marker used to be stamped by the probe itself
    after a structural check of the LAST hop only; the four hops in between,
    one of which dispatches by a getattr on a string name, were a comment.
    """
    # The race, read off production instead of assumed. ``mark_session_active``
    # is what fills ``claude_active_sessions``; it is called only after
    # ``get_or_create_claude_session`` returns, so a turn accepted while that
    # call is in flight is invisible to the live-state probe. Reorder those two
    # in production and this assertion -- and the finding -- fall together.
    from modules.agents.claude_agent import ClaudeAgent

    assert _first_reference_line(
        ClaudeAgent.handle_message, "get_or_create_claude_session"
    ) < _first_reference_line(ClaudeAgent.handle_message, "mark_session_active"), (
        "the startup race PR7R-F1 depends on has been closed; the finding and "
        "this characterization both need rereading"
    )
    # And the earlier call is AWAITED, which is what makes the window a window
    # rather than two adjacent statements: the session creation yields, so a
    # turn can be accepted while the live set is still unstamped.
    assert any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "get_or_create_claude_session"
        for node in ast.walk(
            ast.parse(textwrap.dedent(inspect.getsource(ClaudeAgent.handle_message)))
        )
    )

    session_handler = _RealTeardownMarking()
    client = types.SimpleNamespace(
        # The turn is live; the CLI has not exited yet.
        _transport=types.SimpleNamespace(_process=types.SimpleNamespace(returncode=None))
    )
    session_handler.claude_sessions = {"slack_a:/w": client}

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

    # Consequence 2, in two parts: the chain End takes to the marking code, and
    # then the marking code itself, run for real.
    #
    # Part one -- every hop, because the probe cannot execute the SDK teardown
    # and a partial join is how a chain quietly stops being a chain. The third
    # hop is the load-bearing one and the one no call-graph check would catch:
    # ``_cleanup_runtime_session_state`` does not call ``cleanup_session``, it
    # resolves the NAME off the session handler and calls whatever comes back.
    assert _calls_method_named(ClaudeAgent.end_runtime_session, "_cleanup_runtime_session")
    assert _calls_method_named(
        ClaudeAgent._cleanup_runtime_session, "_cleanup_runtime_session_state"
    )
    dispatch = inspect.getsource(ClaudeAgent._cleanup_runtime_session_state)
    assert 'cleanup_name = "_cleanup_session_locked" if runtime_lock_held else "cleanup_session"' in dispatch
    assert "cleanup = getattr(self.session_handler, cleanup_name, None)" in dispatch
    assert _calls_method_named(SessionHandler.cleanup_session, "_cleanup_session_locked")
    assert _calls_method_named(
        SessionHandler._cleanup_session_locked, "_mark_claude_teardown_intentional"
    )

    # Part two -- the real locked body, not a hand-stamped marker. It decides
    # for itself whether this key earned a teardown record; only a key that
    # actually held a generation does.
    assert session_handler.claude_intentional_teardowns == {}
    asyncio.run(session_handler._cleanup_session_locked("slack_a:/w"))
    assert "slack_a:/w" in session_handler.claude_intentional_teardowns
    assert session_handler.claude_sessions == {}, "the live generation was dropped"

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
    and turn-registry mappings are cleared and the Web/API caller is told
    ``ok: True, action: "ended"``.

    The teardown is not the defect. The missing signal is.

    Scope, narrowed after review: this builds only the codex session and turn
    registries, so there is no Run row here and the probe claims nothing about
    the Run's terminal state. An earlier draft said the Run "is never settled";
    that was inferred from the missing interrupt, not observed, and inference
    dressed as evidence is the one thing this unit exists to stop. The IM
    lane's ``user_stop`` cells carry the probe that would settle it.
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

    # Current master's answer. The turn was NOT interrupted, yet nothing in the
    # payload says so -- no ``stop_failed``, no ``interrupted: False``, no
    # ``settled``. A fix must add that signal here. What happened to any Run
    # bound to this turn is outside what this probe can see.
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
    is about.

    Scope, narrowed again in round 3. An earlier draft ended "no per-Run timeout
    policy can be specified against that record", and quietly promoted "the
    PROJECTION lacks the inputs" into "the inputs do not exist". They do: every
    accepted id is the primary key of an ``agent_runs`` row that carries
    ``source_kind``, ``source_actor`` and ``definition_id``, and the definition
    carries the timeout fields. The gap is a join, not an absence -- so the
    probe now asserts BOTH halves, and the open question becomes the narrower
    and more useful one of whether the cancellation site performs that join.
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

    # The other half of the narrowed claim: the durable rows those ids key DO
    # carry the provenance the projection drops, so "the Turn cannot
    # discriminate" is a statement about the projection and must not be read as
    # a statement about the system. Asserted against the real schema, so if a
    # per-Run field is ever folded into the Turn context the two assertions
    # disagree and Q3 has to be rewritten rather than drifting.
    from storage.models import agent_runs, run_definitions

    assert {"source_kind", "source_actor", "definition_id"} <= set(agent_runs.c.keys())
    assert {"timeout_seconds", "lifetime_timeout_seconds"} <= set(
        run_definitions.c.keys()
    ), "the definition a Run names is where its deadline already lives"


# ----- HFR-183: Q2 exact-Turn signals --------------------------------------


def test_which_backends_attribute_a_progress_event_to_an_exact_turn():
    """HFR-183 / Q2: codex and workbench opencode have the signal; claude does not.

    The plan forbids a generic inactivity timeout unless every backend and lane
    has an exact-Turn progress signal, and states that session-wide activity is
    never an acceptable substitute.

    This probe has now corrected itself twice, both times the same way, and the
    pattern is the finding. Two drafts ago it read
    ``EXACT_TURN_PROGRESS_SIGNALS`` back to itself. One draft ago it exercised
    three real structures but looked only at each backend's base-session
    PROJECTION -- ``_active_turns[base]`` for codex -- and concluded from a
    lossy liveness map that no exact signal existed anywhere; codex's did, one
    level up, in the notification. Round 3 caught the identical mistake still
    standing for opencode: ``_active_requests[base]`` is also a liveness map,
    and the poll loop it indexes is handed the exact ``AgentRequest`` and emits
    every tool call and assistant message with ``request.context`` -- whose
    ``turn_token`` ``_process_message`` has already read as
    ``logical_turn_id``. Reading a projection to decide what an event stream
    carries is a mistake that survived being named once, so it is asserted
    against here rather than only described.

    "The attribution is discarded" and "the attribution does not exist" call
    for different fixes, which is what makes the distinction load-bearing.

    Lanes matter for exactly one cell. Claude's key is a composite key and
    codex's ``turnId`` rides the notification, so neither varies. OpenCode's
    token, though, is stamped into the context by ``SessionTurnManager`` and by
    the streaming turn dispatch -- both Workbench owners. A plain IM context
    gets none, so ``logical_turn_id`` is empty there and the emit has no Turn
    to name.
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

    # Codex. Two distinct Runs accepted into one base session.
    registry = CodexTurnRegistry()
    requests = {
        turn_id: types.SimpleNamespace(base_session_id="base-1", turn=turn_id)
        for turn_id in ("turn-1", "turn-2")
    }
    for turn_id, request in requests.items():
        registry.register_turn(turn_id, request)

    # The signal EXISTS. A notification naming ``turn-1`` resolves to turn-1's
    # own request even though turn-2 is the base session's active turn, because
    # ``_find_request_for_notification`` reads the params' ``turnId`` first and
    # only falls back to the thread when there is none. This is the exact-Turn
    # attribution the plan asks for, on the real production resolver.
    agent = object.__new__(CodexAgent)
    agent._turn_registry = registry
    agent._session_mgr = types.SimpleNamespace(
        find_base_session_id_for_thread=lambda _thread: "base-1"
    )
    for turn_id, request in requests.items():
        resolved = agent._find_request_for_notification(
            "item/completed", {"turnId": turn_id, "threadId": "thread-1"}
        )
        assert resolved is request, turn_id
    # Without the turnId the same notification collapses to the base session's
    # latest request -- so the id is doing the work, not the thread.
    assert (
        agent._find_request_for_notification("item/completed", {"threadId": "thread-1"})
        is requests["turn-2"]
    )
    # ``_on_item_completed`` reads the same key, so the attribution is present
    # on the progress path specifically and not only on turn lifecycle events.
    assert 'params.get("turnId", "")' in inspect.getsource(
        CodexEventHandler._on_item_completed
    )

    # And it is DISCARDED one step later. ``should_emit_progress`` gates on
    # ``is_active_turn``, which reads the single ``_active_turns[base]`` slot,
    # so turn-1's progress -- correctly attributed a line earlier -- is dropped
    # while turn-1 is still live.
    assert registry.get_active_turn("base-1") == "turn-2"
    assert registry.get_turn("turn-1") is not None
    assert registry.should_emit_progress("turn-2") is True
    assert registry.should_emit_progress("turn-1") is False, (
        "the notification named turn-1 exactly; the emit gate throws that away"
    )

    # OpenCode. ``_active_requests`` really is one asyncio task slot per base
    # session id -- by annotation and by every read/write site -- and that is
    # the projection whose lossiness proves nothing about the event stream.
    from modules.agents.opencode.agent import OpenCodeAgent
    from modules.agents.opencode.poll_loop import OpenCodePollLoop

    agent_source = inspect.getsource(OpenCodeAgent)
    assert "self._active_requests: Dict[str, asyncio.Task] = {}" in agent_source
    assert "self._active_requests[request.base_session_id] = task" in agent_source
    assert "self._active_requests[turn" not in agent_source

    # The event stream is a different object, and this is a claim about the
    # production function's own text, so it is read out of the production
    # function rather than restaged. Restaging it -- building a fake loop and
    # calling a recording emitter by hand -- would prove only that the probe
    # can pass a context to a callback, which is the fabrication this file's
    # rule forbids and which review caught in HFR-180 the same round.
    #
    # ``run_prompt_poll`` receives the exact ``AgentRequest``, and EVERY
    # progress emit in its body passes ``request.context`` as the context. Not
    # "at least one" -- all of them, so a single emit switched to a
    # session-derived context fails here.
    assert "request" in inspect.signature(OpenCodePollLoop.run_prompt_poll).parameters
    poll_tree = ast.parse(
        textwrap.dedent(inspect.getsource(OpenCodePollLoop.run_prompt_poll))
    )
    emit_contexts = [
        node.args[0]
        for node in ast.walk(poll_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "emit_agent_message"
        and node.args
    ]
    assert emit_contexts, "the poll loop emits no progress at all"
    assert all(
        isinstance(arg, ast.Attribute)
        and arg.attr == "context"
        and isinstance(arg.value, ast.Name)
        and arg.value.id == "request"
        for arg in emit_contexts
    ), ast.unparse(poll_tree)

    # ...and the token that context carries is the one production reads as the
    # logical turn id -- which is what makes it a Turn identifier rather than
    # an opaque string the poller happens to forward.
    assert (
        'logical_turn_id = str(platform_payload.get("turn_token") or "").strip()'
        in inspect.getsource(OpenCodeAgent._process_message)
    )

    # Consistency tie: the matrix must still say the same thing these probes
    # just showed, for all six cells.
    from tests.run_terminal_truth_evidence import (
        BACKENDS,
        EXACT_TURN_PROGRESS_SIGNALS,
        LANES,
    )

    assert set(EXACT_TURN_PROGRESS_SIGNALS) == {
        (backend, lane) for backend in BACKENDS for lane in LANES
    }
    attributed = {("codex", "direct_im"), ("codex", "durable_workbench"),
                  ("opencode", "durable_workbench")}
    for cell, (kind, reason) in EXACT_TURN_PROGRESS_SIGNALS.items():
        if cell in attributed:
            assert kind == "covered", cell
            assert reason.endswith(
                "test_which_backends_attribute_a_progress_event_to_an_exact_turn"
            ), cell
        else:
            assert kind == "unproven", cell
            assert "probe" in reason.lower(), cell

