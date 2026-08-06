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

Scenario ids: HFR-180 .. HFR-183, HFR-188, HFR-191, HFR-195.
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
from modules.agents.service import AgentService


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

    Round 4 answered the reachability challenge the ordering check left open:
    an ordering is not a window unless the fixture's combination of registries
    can actually occur. Review argued it cannot, on the grounds that
    ``claude_sessions`` is populated only at the end of session creation, so a
    cold start has no client for End to tear down. That is right about a COLD
    start and it is not this state. The state staged here is warm-idle --
    ``mark_session_idle`` drops the key from the live set and keeps the client
    -- and the second turn on a warm session waits on the per-generation async
    lock inside ``get_or_create_claude_session`` before anything stamps it
    active. Both halves are now driven or read off production below.
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
    # The yield is unconditional and unbounded: EVERY path through
    # ``get_or_create_claude_session`` -- warm reuse included -- first acquires
    # the per-generation async lock, and the retry path additionally waits on
    # receiver cleanup. So the window is as wide as whatever else holds that
    # lock, not a few instructions.
    resolve_source = inspect.getsource(SessionHandler.get_or_create_claude_session)
    assert "async with self._claude_runtime_generation_lock(composite_key):" in resolve_source
    assert "await self._wait_for_claude_receiver_cleanup(retry.composite_key)" in resolve_source

    # REACHABILITY, which the previous draft left to the reader and review was
    # right to challenge. The state below -- a client registered in
    # ``claude_sessions`` while its key is absent from ``claude_active_sessions``
    # -- is not a fresh-startup state: ``_get_or_create_claude_session_locked``
    # registers the client only once connection completes, so during a COLD
    # start there is nothing for ``_end_claude`` to tear down and the defect
    # cannot fire. It is the ordinary WARM-IDLE state. ``mark_session_idle``
    # discards the key from the live set and deliberately keeps the client --
    # it even touches activity on the strength of the client still being there.
    # A second turn arriving on that warm session is admitted, waits on the
    # generation lock, and is invisible to ``_resolve_live_state`` the whole
    # time. Driven, not asserted in prose.
    warm = object.__new__(SessionHandler)
    warm.active_sessions = set()
    warm.session_turn_started = {}
    warm.session_last_activity = {}
    warm.claude_sessions = {"slack_a:/w": object()}
    warm.mark_session_active("slack_a:/w")
    assert "slack_a:/w" in warm.active_sessions
    warm.mark_session_idle("slack_a:/w")
    assert "slack_a:/w" not in warm.active_sessions
    assert "slack_a:/w" in warm.claude_sessions, (
        "the warm-idle state the fixture below stages is exactly this one"
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


def _codex_end_controller(
    *, interrupt_raises: bool, co_tenant: bool, cleared: dict, sent: list
):
    """A codex End fixture with a LIVE transport and a live thread + turn.

    Two corrections live in this fixture, one per review round, and the second
    reverses part of the first.

    Round 4: the original left ``_transports`` empty and ``get_thread_id``
    returning ``None``, which is precisely the stale-row case ``_end_codex`` is
    DESIGNED to clean up -- an app-server that already died, with nothing left
    to interrupt. Staging that and then complaining the payload reports no
    interrupt was staging the absence of the thing being measured.

    Round 5: a live transport was necessary and not sufficient. With
    ``sessions_for_cwd`` returning ``[]`` this session is the cwd's last, so
    ``_end_codex`` stops the shared app-server -- and a killed app-server ends
    the turn just as surely as an interrupt does. Both runs therefore terminated
    the turn, by different means, which makes ``ended`` a TRUE report in both
    and establishes nothing. ``co_tenant`` is the fix: another session on the
    same cwd keeps the transport up, so a failed interrupt leaves the backend
    turn genuinely running. The lesson is the same one this unit keeps
    relearning at a new depth -- a fixture has to stage the state the claim is
    about, and "live transport" was not yet that state.
    """

    class _Transport:
        async def send_request(self, method, params):
            sent.append((method, params))
            if interrupt_raises:
                raise RuntimeError("app-server refused turn/interrupt")
            return {"ok": True}

        async def stop(self):
            cleared["transport_stopped"] = True

    session_mgr = types.SimpleNamespace(
        get_cwd=lambda b: "/w",
        get_thread_id=lambda b: "thread-1",  # live thread
        clear=lambda b: cleared.__setitem__("session_mgr", b),
        # Production calls this AFTER ``clear``, so it lists the sessions that
        # remain on the cwd. A co-tenant means the shared app-server stays up.
        sessions_for_cwd=lambda cwd: (["b2"] if co_tenant else []),
    )
    turn_registry = types.SimpleNamespace(
        get_active_turn=lambda b: "turn-1",  # live turn
        clear_session=lambda b: cleared.__setitem__("turn_registry", b),
    )
    codex = types.SimpleNamespace(
        _session_mgr=session_mgr,
        _turn_registry=turn_registry,
        _transports={"/w": _Transport()},
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
    return controller, handle_stop, codex


def test_codex_end_reports_ended_when_the_canonical_stop_never_interrupted():
    """HFR-181 / PR7R-F2: End discards the interrupt outcome it already has.

    Clearing a stale-active codex row whose app-server died is deliberate --
    ``test_end_active_codex_clears_stale_row_even_when_stop_fails`` owns that
    behavior and it should stay. This probe is about a live turn, and round 4
    located the defect a level more precisely than the first filing did.

    ``_end_codex`` DOES compute and return ``interrupted``. The loss happens one
    frame up: on the failed-stop branch ``end_running_agent`` writes
    ``{"ok": True, "action": "ended", "backend": "codex"}`` as a fresh literal
    and copies only ``process_killed`` out of the teardown result. So the
    signal is not missing from the system -- it is produced, and then dropped
    by the caller that is supposed to report it.

    The demonstration is two runs against the SAME fixture whose only difference
    is whether ``turn/interrupt`` succeeds. One leaves the backend turn genuinely
    running, the other genuinely interrupts it, and both return a byte-identical
    payload. A caller cannot distinguish them, which is what makes this a
    reporting defect rather than a teardown one.

    Round 5 fixed what "genuinely running" required. The fixture used to make
    this session the cwd's last, which means ``_end_codex`` stops the shared
    app-server -- so the failed-interrupt run's turn died anyway, by process
    kill, and ``ended`` was a true report in both runs. The turn only survives a
    failed interrupt when a co-tenant session keeps the transport up, so that is
    what the primary pair stages, and the survival is asserted rather than
    assumed: the transport is never stopped and is still registered afterwards.

    Scope: this builds only the codex session/turn registries and a transport,
    so there is no Run row and the probe claims nothing about the Run's terminal
    state. An earlier draft said the Run "is never settled"; that was inferred
    from the missing interrupt, not observed. The IM lane's ``user_stop`` cells
    carry the probe that would settle it.
    """
    def _run(*, interrupt_raises: bool, co_tenant: bool):
        cleared: dict = {}
        sent: list = []
        controller, handle_stop, codex = _codex_end_controller(
            interrupt_raises=interrupt_raises,
            co_tenant=co_tenant,
            cleared=cleared,
            sent=sent,
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
        # The live transport WAS asked to interrupt the live turn, so the two
        # runs differ in the one fact the caller cares about.
        assert sent == [
            ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"})
        ], sent
        assert cleared["session_mgr"] == "b1"
        assert cleared["turn_registry"] == "b1"
        return result, cleared, codex

    # --- the pair that establishes the finding: a co-tenant keeps the shared
    # app-server up, so the two runs really do differ in whether the backend
    # turn is still executing.
    not_interrupted, cleared_alive, codex_alive = _run(
        interrupt_raises=True, co_tenant=True
    )
    interrupted, _, _ = _run(interrupt_raises=False, co_tenant=True)

    # The turn survives -- asserted, not assumed. Nothing stopped the transport
    # and it is still registered, so the only thing that could have ended that
    # turn was the interrupt, and the interrupt raised.
    assert "transport_stopped" not in cleared_alive
    assert "/w" in codex_alive._transports

    # Current master's answer: identical. No ``stop_failed``, no ``interrupted``,
    # no ``settled`` -- and, critically, no difference between a turn still
    # running on the backend and one that was actually stopped.
    assert not_interrupted == {"ok": True, "action": "ended", "backend": "codex"}
    assert interrupted == not_interrupted, (
        "a real interrupt and a failed one are byte-identical to the caller"
    )
    assert "stop_failed" not in not_interrupted
    assert "interrupted" not in not_interrupted
    assert "process_killed" not in not_interrupted

    # --- the last-session variant, kept for one narrow purpose. Here the turn
    # DOES die even when the interrupt fails, because stopping the cwd's last
    # transport kills the app-server running it, so ``ended`` is a true report
    # and this pair proves nothing about misreporting. What it does show is that
    # the failed-stop branch is perfectly capable of copying a field out of the
    # teardown result -- it copies ``process_killed`` and leaves ``interrupted``
    # behind. That makes the omission a field the caller forgot rather than
    # information it never had, which is why the fix is a one-line forward.
    last_session, cleared_killed, codex_killed = _run(
        interrupt_raises=True, co_tenant=False
    )
    assert cleared_killed["transport_stopped"] is True
    assert "/w" not in codex_killed._transports
    assert last_session == {
        "ok": True,
        "action": "ended",
        "backend": "codex",
        "process_killed": True,
    }
    assert "interrupted" not in last_session

    # The signal exists one frame down and is dropped by the frame above. Both
    # halves asserted, because a fix belongs in the second one and a probe that
    # only showed the absence would not say where.
    end_codex_source = inspect.getsource(running_agents._end_codex)
    assert '"interrupted": interrupted,' in end_codex_source
    assert (
        'result = dict(stop_result) if stop_ok else {"ok": True, "action": "ended", "backend": "codex"}'
        in inspect.getsource(running_agents.end_running_agent)
    ), "the discard site moved; PR7R-F2 needs relocating"


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
    """HFR-183 / Q2: all six backend/lane cells carry an exact-Turn signal.

    The plan forbids a generic inactivity timeout unless every backend and lane
    has an exact-Turn progress signal, and states that session-wide activity is
    never an acceptable substitute.

    This probe has now corrected itself three times, ALWAYS THE SAME WAY, and
    that pattern is worth more than the verdict. Draft 1 read
    ``EXACT_TURN_PROGRESS_SIGNALS`` back to itself. Draft 2 exercised three real
    structures but looked only at each backend's base-session PROJECTION and
    concluded from a lossy liveness map that no exact signal existed anywhere;
    codex's did, one level up, in the notification. Draft 3 fixed codex and left
    the identical mistake standing for opencode -- ``_active_requests[base]`` is
    also a liveness map, and the poll loop it indexes emits with
    ``request.context``. Draft 4 fixed opencode and left it standing for claude:
    ``session_turn_started[composite_key]`` is a projection too, and claude's
    long-lived receiver adopts the FIFO-matched pending request's ``turn_token``
    onto the emit context before any assistant/tool output. Three backends,
    three identical misreadings, each caught only after the previous one was
    named. So the probe now checks the projection AND the event stream for every
    backend, and says which is which.

    "The attribution is discarded" and "the attribution does not exist" call
    for different fixes, which is what makes the distinction load-bearing.

    Draft 5 was a fourth instance of the same shape and is the reason this
    summary was stale for two rounds: it concluded the answer splits by LANE,
    on the ground that ``turn_token`` is stamped only by ``SessionTurnManager``
    and the streaming turn dispatch, both Workbench owners. That came from
    grepping the LITERAL string and missing the constant-keyed write in
    ``AgentService._stamp_runtime_turn``, which every request on every lane
    passes through. There is no lane split: ALL SIX cells carry an exact-Turn
    signal, driven in
    ``test_the_shared_admission_layer_stamps_a_turn_token_on_direct_im``.

    What is NOT settled by that is what the consumer does with the signal, and
    for codex it drops it -- see the closing section here and HFR-195.

    Claude's attribution is a FIFO POSITION, not an id the event carries: the
    head of ``_pending_requests[composite_key]`` is taken to be the turn
    producing the event. Exact under per-key serialization, weaker than codex's,
    and asserted as such rather than smoothed into "claude has a turn id too".
    """
    # Claude, part one -- the PROJECTION, which is all the first three drafts of
    # this probe looked at. The progress baseline is stamped per COMPOSITE KEY,
    # and the method has no parameter that could carry a Turn.
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

    # Claude, part two -- the EVENT STREAM, which is a different object, and
    # which says the opposite. Claude's receiver is long-lived, so the context
    # it captured belongs to an OLDER turn; before any assistant/tool emit,
    # ``_adopt_pending_turn_token`` copies the FIFO-matched pending request's
    # Turn identity onto it. Driven on the real static method with a stale
    # receiver context and a pending request from a different Turn.
    from modules.agents.claude_agent import ClaudeAgent

    stale_receiver_context = types.SimpleNamespace(
        platform_specific={
            "turn_token": "wb-turn-1",
            "agent_runtime_turn_token": "rt-1",
            "accepted_agent_run_ids": ["run-1"],
        }
    )
    live_turn_request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            platform_specific={
                "turn_token": "wb-turn-2",
                "agent_runtime_turn_token": "rt-2",
                "accepted_agent_run_ids": ["run-2"],
            }
        )
    )
    ClaudeAgent._adopt_pending_turn_token(stale_receiver_context, live_turn_request)
    assert stale_receiver_context.platform_specific["turn_token"] == "wb-turn-2"
    assert stale_receiver_context.platform_specific["agent_runtime_turn_token"] == "rt-2"
    assert stale_receiver_context.platform_specific["accepted_agent_run_ids"] == ["run-2"]

    # ...and it is reached from the progress path, not only from the terminal
    # one. The toolcall/assistant branch of ``_receive_messages`` reads the FIFO
    # head and adopts from it -- ``tests/test_claude_agent_sessions.py``'s
    # ``test_toolcall_emit_adopts_current_pending_turn_token`` drives that whole
    # branch end to end and asserts the emitted toolcall carries the pending
    # turn's token, so this probe pins the read rather than restaging it.
    receive_source = inspect.getsource(ClaudeAgent._receive_messages)
    assert "pending_requests = self._pending_requests.get(composite_key) or []" in receive_source
    assert "self._adopt_pending_turn_token(context, pending_request)" in receive_source

    # The mechanism is a FIFO POSITION, not an id the event carries. Recorded
    # because it is weaker than codex's ``turnId`` and the remediation differs:
    # the head of the pending list is TAKEN to be the turn producing the event.
    assert (
        "requests = self._pending_requests.get(composite_key)"
        in inspect.getsource(ClaudeAgent._pop_pending_request)
    )

    # The adopt copies whatever the pending request carries, so what it does on
    # direct IM depends entirely on whether anything stamped that request. This
    # probe used to answer "nothing does" by handing the adopt two empty
    # contexts and observing a no-op -- which showed only that copying nothing
    # copies nothing. The real answer is one layer up, in the shared admission
    # path, and it is driven in
    # ``test_the_shared_admission_layer_stamps_a_turn_token_on_direct_im``.

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

    # And it is narrowed one step later. ``should_emit_progress`` gates on
    # ``is_active_turn``, which reads the single ``_active_turns[base]`` slot,
    # so a notification naming turn-1 is dropped once turn-2 is the active turn.
    assert registry.get_active_turn("base-1") == "turn-2"
    assert registry.get_turn("turn-1") is not None
    assert registry.should_emit_progress("turn-2") is True
    assert registry.should_emit_progress("turn-1") is False

    # For two rounds this was only a fact about the REGISTRY. The fixture
    # registers both turns by hand, so it postulates the state it needs -- and
    # round four rightly refused to call that a defect, then round six went
    # further and called the drop CORRECT, on the ground that the runtime gate
    # admits one turn per key at a time. That last step was wrong: it is true of
    # the gate's key and says nothing about this slot's key, which is the base
    # session alone. The remaining state -- "can anything put two LIVE turns on
    # one base session" -- was carried as a probe, and round eight closed it
    # against the conclusion. A working-path change does exactly that, driven in
    # ``test_a_cwd_change_splits_the_gate_key_but_not_the_codex_turn_slot``. So
    # the drop above is a discarded live signal, not correct filtering.

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
    # All six, after round 6. There is no lane split and no backend split: the
    # two IM cells were open only because the shared admission layer had been
    # missed, and the citation for those two is the probe that drives it.
    for cell, (kind, reason) in EXACT_TURN_PROGRESS_SIGNALS.items():
        assert kind == "covered", cell
        expected = (
            "test_the_shared_admission_layer_stamps_a_turn_token_on_direct_im"
            if cell[1] == "direct_im" and cell[0] != "codex"
            else "test_which_backends_attribute_a_progress_event_to_an_exact_turn"
        )
        assert reason.endswith(expected), cell



# ----- HFR-188 / HFR-191: Q2, the shared admission layer -------------------


class _AdmissionAgent:
    """The smallest agent ``AgentService`` will drive, recording what it is handed.

    It records ``platform_specific`` AT THE MOMENT the backend is invoked, which
    is the only moment that matters: a token stamped after the backend starts
    emitting is not available to the emit.
    """

    name = "claude"

    def __init__(self, release=None):
        self.seen: list[dict] = []
        self.release = release

    def runtime_turn_key(self, request):
        return request.composite_session_id

    async def handle_message(self, request):
        self.seen.append(dict(request.context.platform_specific or {}))
        if self.release is not None:
            await self.release.wait()

    async def clear_sessions(self, _session_key):
        return 0

    async def handle_stop(self, _request):
        return False


def _im_request(message: str, runtime_key: str = "slack_c1:/w"):
    """A direct-IM request: no Workbench turn token anywhere on its context."""
    return types.SimpleNamespace(
        context=types.SimpleNamespace(platform_specific={}),
        message=message,
        composite_session_id=runtime_key,
    )


def _admission_service():
    service = AgentService(controller=types.SimpleNamespace(session_turns=None))
    agent = _AdmissionAgent()
    service.register(agent)
    return service, agent


def test_the_shared_admission_layer_stamps_a_turn_token_on_direct_im():
    """HFR-188 / Q2: direct IM gets an exact Turn token from AgentService.

    This probe exists because the previous answer to Q2 was wrong, and wrong in
    a fourth distinct way. Rounds 2-4 each read a lossy base-session PROJECTION
    and concluded the event stream carried nothing. This was a different mistake
    with the same shape: the writers of ``turn_token`` were found by grepping the
    LITERAL string, which turns up ``core/session_turns.py`` and
    ``core/services/dispatch.py`` -- both Workbench owners -- and misses the
    single most central writer, because ``AgentService`` writes
    ``platform_specific[AGENT_TURN_TOKEN]`` through the constant. The layer the
    grep skipped is the one EVERY request passes through, on every lane.

    So "nothing stamps a ``turn_token`` into an IM context" was false. It is
    replaced here by driving the real ``AgentService.handle_message`` with an
    IM-scoped request rather than by reading source. Three properties, because a
    token is only useful if it is present, exact, and non-destructive:

    1. it is on the context BEFORE the backend is invoked;
    2. a second turn on the SAME runtime key gets a DIFFERENT token -- otherwise
       it would be a session id in disguise and would attribute nothing;
    3. an existing Workbench ``turn_token`` is preserved, so the durable lane's
       own identifier is not clobbered by the admission stamp.
    """
    from modules.agents.base import (
        AGENT_RUNTIME_TURN_KEY,
        AGENT_RUNTIME_TURN_TOKEN,
        AGENT_TURN_TOKEN,
    )

    assert AGENT_TURN_TOKEN == "turn_token"  # the key the backends read

    async def _drive():
        service, agent = _admission_service()
        first = _im_request("one")
        second = _im_request("two")
        # The gate is held past the backend call and released by the outbound
        # dispatcher at terminal delivery, so the release is explicit here --
        # which is itself the fact the serialization probe below turns on.
        await asyncio.wait_for(service.handle_message("claude", first), timeout=5)
        service.release_runtime_turn(first.context)
        await asyncio.wait_for(service.handle_message("claude", second), timeout=5)
        service.release_runtime_turn(second.context)
        return agent, first, second

    agent, first, second = asyncio.run(_drive())

    # (1) present at backend-invocation time, not merely at the end of the turn.
    assert len(agent.seen) == 2
    tokens = [seen.get(AGENT_TURN_TOKEN) for seen in agent.seen]
    assert all(tokens), agent.seen
    # ...and it is the runtime gate's token, which is what makes it a TURN id
    # rather than an unrelated correlation value.
    for seen in agent.seen:
        assert seen[AGENT_TURN_TOKEN] == seen[AGENT_RUNTIME_TURN_TOKEN]
        assert seen[AGENT_RUNTIME_TURN_KEY] == "slack_c1:/w"

    # (2) exact per Turn. Same session, same runtime key, different token.
    assert tokens[0] != tokens[1], tokens
    assert first.context.platform_specific[AGENT_TURN_TOKEN] == tokens[0]
    assert second.context.platform_specific[AGENT_TURN_TOKEN] == tokens[1]

    # (3) a context that already carries a Workbench turn token keeps it: the
    # stamp fills a gap, it does not overwrite the durable lane's identifier.
    async def _drive_workbench():
        service, agent = _admission_service()
        request = _im_request("wb")
        request.context.platform_specific[AGENT_TURN_TOKEN] = "wb-turn-1"
        await asyncio.wait_for(service.handle_message("claude", request), timeout=5)
        service.release_runtime_turn(request.context)
        return agent

    wb_agent = asyncio.run(_drive_workbench())
    assert wb_agent.seen[0][AGENT_TURN_TOKEN] == "wb-turn-1"
    assert wb_agent.seen[0][AGENT_RUNTIME_TURN_TOKEN] != "wb-turn-1"


def test_one_runtime_key_admits_one_live_turn_at_a_time():
    """HFR-191 / Q2: the admission gate serializes turns per runtime key.

    Review challenged the codex half of the Q2 probe on reachability, and the
    challenge holds. That probe registers two turns for one base session and
    shows ``should_emit_progress`` dropping the older one -- but if production
    cannot have two LIVE turns on one key, the older id is stale by the time the
    newer one is active, and dropping it is correct filtering rather than a
    discarded signal. Same standard this unit applied to PR7R-F1 in round 4: an
    ordering is not a defect until the state is shown to occur.

    It does not occur ON ONE RUNTIME KEY, and that qualifier is the whole of
    what this test proves. The gate is acquired in ``handle_message`` before the
    backend is invoked and is NOT released when the backend returns -- the
    outbound dispatcher releases it at terminal delivery -- so a second turn on
    the same key cannot enter the backend while the first is live. Driven here
    rather than read off the production comment that says so.

    Round eight had to add the qualifier because round six dropped it and
    concluded codex's ``should_emit_progress`` is therefore lossless. It is not:
    the gate key and the codex turn slot are DIFFERENT KEY SPACES, so "one live
    turn per gate key" says nothing about "one live turn per slot". That is
    ``test_a_cwd_change_splits_the_gate_key_but_not_the_codex_turn_slot``, and
    this test deliberately stops at its own key.

    The second half is the half that carries the claim, and the first draft of
    this probe was missing it. Holding a lock ACROSS a call is what any mutex
    does; what Q2 turns on is the gate outliving the call, because a backend
    returning from turn submission is not a turn ending -- codex and claude both
    return while the turn is still executing, and it is that window in which a
    second live turn would have to be excluded. A probe that only blocks inside
    the backend stays green under the exact regression it should catch: add a
    release when the backend returns and native turns can overlap while the
    assertions still pass. So the release is driven in two steps -- let the first
    backend call RETURN, then assert the second is still shut out, and only then
    release explicitly.
    """
    from modules.agents.base import AGENT_TURN_TOKEN

    async def _drive():
        service = AgentService(controller=types.SimpleNamespace(session_turns=None))
        release = asyncio.Event()
        agent = _AdmissionAgent(release=release)
        service.register(agent)

        first_request = _im_request("first")
        first = asyncio.create_task(service.handle_message("claude", first_request))
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(agent.seen) == 1  # first turn is inside the backend

        second = asyncio.create_task(service.handle_message("claude", _im_request("second")))
        await asyncio.sleep(0.05)
        # The second turn is queued on the gate: its backend has NOT been
        # entered, so there is no second live turn for this runtime key.
        assert len(agent.seen) == 1, agent.seen

        release.set()
        await asyncio.wait_for(first, timeout=5)
        # The first backend call has RETURNED and its turn is unreleased. This
        # is the state a backend is in while its native turn keeps running, and
        # it is the one the Q2 conclusion depends on: the gate must still be
        # held, so the queued turn must still be outside the backend.
        await asyncio.sleep(0.05)
        assert len(agent.seen) == 1, agent.seen
        assert not second.done()

        service.release_runtime_turn(first_request.context)
        await asyncio.wait_for(second, timeout=5)
        return agent

    agent = asyncio.run(_drive())
    assert len(agent.seen) == 2
    assert agent.seen[0][AGENT_TURN_TOKEN] != agent.seen[1][AGENT_TURN_TOKEN]

    # What this does NOT establish is the codex conclusion an earlier round
    # hung on it. "One runtime key admits one live turn" is a statement about a
    # KEY, and codex's ``_active_turns`` slot is keyed by something else, so the
    # two do not compose. The first draft of that step was a substring read of
    # ``_runtime_turn_key_for_base_session`` -- exactly the nearby-passing-
    # assertion this file forbids, and it was reading the wrong function on top
    # of that: the gate never calls it. Driven properly in
    # ``test_a_cwd_change_splits_the_gate_key_but_not_the_codex_turn_slot``,
    # which shows the two key spaces disagreeing and two live turns landing in
    # one slot.


def test_a_cwd_change_splits_the_gate_key_but_not_the_codex_turn_slot():
    """HFR-195 / Q2: two live codex turns share one slot, and the older goes silent.

    This closes the probe the codex cell has been carrying since round four --
    "whether any state can put two live turns on one base session" -- and it
    closes it the way that costs the previous round's conclusion. A key
    collision can.

    The mechanism is that two identifiers which look like the same thing are
    not. The admission gate keys on ``BaseAgent.runtime_turn_key``, which is the
    COMPOSITE session identity ``<base>:<working_path>``; codex's
    ``_active_turns`` keys on ``request.base_session_id`` ALONE. Same session,
    two working paths, and the gate sees two keys where the registry sees one
    slot. Both turns are admitted, the second's ``register_turn`` overwrites the
    slot, and ``should_emit_progress`` then reports False for a turn that is
    still running -- the exact-Turn attribution reaches the filter and is
    discarded there.

    Round six's error is worth naming because it is a new one rather than a
    fifth repeat. It was not a lossy projection read as an event stream; it was
    a correct fact about one key ("the gate holds it for the whole turn")
    carried across to a DIFFERENT key without checking that the keys are the
    same. And the assertion that was supposed to check exactly that read a
    substring out of ``_runtime_turn_key_for_base_session`` -- a function the
    gate never calls, which computes ``<base>:<cwd>`` for the registry's own
    bookkeeping. It passed, it looked like a key-space tie, and it was neither.

    So every step below is driven on production objects: the two key spaces are
    compared by calling them, the concurrency is shown by running the real
    ``AgentService.handle_message``, and the silencing is shown on the real
    ``CodexTurnRegistry``.
    """
    from modules.agents.base import BaseAgent
    from modules.agents.codex.session import CodexSessionManager

    def _codex_request(working_path: str):
        return types.SimpleNamespace(
            context=types.SimpleNamespace(platform_specific={}),
            message="m",
            base_session_id="base-1",
            working_path=working_path,
            composite_session_id=f"base-1:{working_path}",
        )

    first_request = _codex_request("/w1")
    second_request = _codex_request("/w2")

    # (1) The gate key. Codex does not override it -- asserted by identity, not
    # by grepping for an absent ``def`` -- so the gate uses the composite key,
    # which the two requests do NOT share even though the base session is one.
    assert CodexAgent.runtime_turn_key is BaseAgent.runtime_turn_key
    codex = object.__new__(CodexAgent)
    gate_keys = [codex.runtime_turn_key(req) for req in (first_request, second_request)]
    assert gate_keys == ["base-1:/w1", "base-1:/w2"], gate_keys
    assert first_request.base_session_id == second_request.base_session_id

    # ...and ``AgentService`` really routes through that method rather than
    # falling back to its own composite-id default, which would make the point
    # accidental rather than structural.
    assert AgentService._runtime_turn_key(codex, first_request) == gate_keys[0]

    # (2) The slot key. ``_active_turns`` is written by base session id, so the
    # registry cannot tell the two requests apart at all.
    registry = CodexTurnRegistry()
    registry.register_turn("turn-1", first_request)
    assert registry.get_active_turn("base-1") == "turn-1"
    assert registry.should_emit_progress("turn-1") is True

    # (3) Both turns are LIVE at once. Two distinct gate keys means two gates,
    # so the second request enters the backend while the first is still inside
    # it -- the state the round-four narrowing said could not occur. Driven on
    # the real service with the real key function.
    class _CodexKeyedAgent(_AdmissionAgent):
        name = "codex"

        def runtime_turn_key(self, request):
            return BaseAgent.runtime_turn_key(self, request)

    async def _drive():
        service = AgentService(controller=types.SimpleNamespace(session_turns=None))
        release = asyncio.Event()
        agent = _CodexKeyedAgent(release=release)
        service.register(agent)

        first = asyncio.create_task(service.handle_message("codex", first_request))
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(agent.seen) == 1

        second = asyncio.create_task(service.handle_message("codex", second_request))
        await asyncio.sleep(0.05)
        # THE FINDING. On one runtime key this second call is still queued
        # outside the backend (HFR-191). A working-path change is enough to make
        # it a different key, and then both turns are inside the backend.
        overlapped = len(agent.seen)

        # Drained with the exceptions swallowed on purpose: under a mutation
        # that gives the two requests ONE gate the second call never enters the
        # backend, and the failure this probe should report is the count above,
        # not a hang in the teardown.
        release.set()
        await asyncio.gather(
            *(asyncio.wait_for(task, timeout=2) for task in (first, second)),
            return_exceptions=True,
        )
        service.release_runtime_turn(first_request.context)
        service.release_runtime_turn(second_request.context)
        return overlapped

    assert asyncio.run(_drive()) == 2

    # (4) The consequence. The second live turn registers into the same slot and
    # evicts the first, which is still running -- so its progress is filtered
    # out. ``should_emit_progress`` is therefore lossy for LIVE turns, and the
    # round-six claim that it is "correct as written" is retracted.
    registry.register_turn("turn-2", second_request)
    assert registry.get_active_turn("base-1") == "turn-2"
    assert registry.get_turn("turn-1") is not None  # turn-1 is not gone, just mute
    assert registry.should_emit_progress("turn-1") is False
    assert registry.should_emit_progress("turn-2") is True

    # (5) And the registry's own derived key cannot name the silenced turn
    # either, because ``handle_message`` moves the tracked cwd to whatever the
    # latest request carried. Driven on the real session manager, replacing the
    # substring read this probe was written to retire.
    session_mgr = CodexSessionManager()
    codex._session_mgr = session_mgr
    session_mgr.set_session_key("base-1", "sk")
    session_mgr.set_cwd("base-1", first_request.working_path)
    assert codex._runtime_turn_key_for_base_session("base-1") == "base-1:/w1"
    session_mgr.set_cwd("base-1", second_request.working_path)
    assert codex._runtime_turn_key_for_base_session("base-1") == "base-1:/w2"
    # One base session, so ONE derived key -- and it is the newer turn's. Any
    # consumer that enumerates runtime keys to find live turns (a stop, a
    # timeout sweep) cannot even address the turn that is being silenced.
    assert codex.runtime_turn_keys_for_session_key("sk") == {"base-1:/w2"}
    assert gate_keys[0] not in codex.runtime_turn_keys_for_session_key("sk")

    # ...and that move is what ``handle_message`` does per request, which is the
    # production text making the collision reachable rather than hypothetical.
    assert (
        "self._session_mgr.set_cwd(request.base_session_id, request.working_path)"
        in inspect.getsource(CodexAgent.handle_message)
    )
