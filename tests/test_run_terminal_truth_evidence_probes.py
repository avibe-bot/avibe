"""PR7R probes: what current master actually does at the terminal boundary.

These are EVIDENCE tests for
``docs/plans/harness-run-reliability.md`` §7. Three of them are characterization
tests: they assert current, wrong behavior so the defect is executable rather
than asserted in prose. Two are the reproducers for ``PR7R-F1`` / ``PR7R-F2``,
and the implementation PR that fixes those must flip them -- that is the point,
not an accident. The third, ``HFR-205``, reproduces a Q2 defect found in round
17 that belongs to no top-level finding and adds no scope to this unit: it
documents that OpenCode's restart path emits without its Turn identity.

Every probe here is held to one rule, because review found the first draft
breaking it in four places: **the subject of the test must be the subject of
the claim.** A probe may not stand on a nearby passing assertion -- a constant
lookup, a metadata label, a helper downstream of the decision it is supposed to
be about. Where the real subject is out of reach in this unit, the claim is
narrowed to what is reached and the rest becomes a named probe in the matrix,
never a green test that reads like coverage.

Scenario ids: HFR-180 .. HFR-183, HFR-188, HFR-191, HFR-195, HFR-197, HFR-199,
HFR-205.
"""

import ast
import asyncio
import contextlib
import inspect
import re
import textwrap
import types
from pathlib import Path

import pytest

from core.handlers.session_handler import SessionHandler
from core.services import running_agents
from core.session_turns import SessionTurnManager
from modules.agents.codex.agent import CodexAgent
from modules.agents.codex.event_handler import CodexEventHandler
from modules.agents.codex.turn_state import CodexTurnRegistry
from modules.agents.service import AgentService


_STAMP = "2026-08-01T00:00:00+00:00"


def _durable_engine(tmp_path, session_id: str = "ses-1"):
    """A real state DB with the real schema and one seeded Agent Session.

    Round 10. Two probes here had been standing on stubbed stores -- the shape
    this unit keeps catching, one layer lower: a fabricated reader answers
    whatever the test wants, so a store that could not tell two Turns apart, or
    that settled a Run on reservation, would leave both of them green. The rows
    below are cheap; the substitution was never worth it.
    """
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions, metadata as storage_metadata

    engine = create_sqlite_engine(tmp_path / "state.sqlite")
    storage_metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id=session_id, scope_id=None, agent_id=None, agent_name="codex",
                agent_backend="codex", agent_variant="codex", model=None,
                reasoning_effort=None, session_anchor=session_id, workdir="/w",
                native_session_id="", title=None, status="active",
                visibility="foreground", pinned=0, agent_status="idle",
                composer_draft_text=None, composer_draft_updated_at=None,
                metadata_json="{}", created_at=_STAMP, updated_at=_STAMP,
                last_active_at=_STAMP,
            )
        )
    return engine


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

    Round 20 retracted "the yield is unconditional and unbounded", which round
    4 wrote and which was a claim about suspension standing on a match against
    source text. An ``await`` is not a suspension point, and an uncontended
    ``asyncio.Lock`` acquires without ever yielding, so a quiet runtime runs
    the whole resolver in one scheduler step and the window does not exist
    there. What makes it a window is CONTENTION on the generation lock, which
    is the warm second turn this fixture already describes -- so the real
    resolver is now run twice under a live event loop, once with the lock free
    and once with it held, and the two outcomes are asserted. The uncontended
    case is asserted as FINISHING, not as a wart to be tolerated: it is the
    half that shows why reading the source could not carry the claim.

    Round 21 changes who HOLDS the lock, which is the difference between "the
    resolver can be blocked" and "production blocks it". Round 20 took it by
    hand, and a lock held by the test says nothing about whether an accepted
    turn ever meets contention. Production has three owners of this key's
    generation lock -- session resolution itself, ``cleanup_session``, and the
    idle-reclamation sweep -- and the middle one is where End's chain
    terminates, hop for hop, as asserted below. So the holder is now End's own
    teardown and the parked caller is the real resolver: both sides acquire and
    release through production, and only the two innermost bodies are stubbed.

    Round 22 puts the halves in ONE interleaving, which is what round 21 still
    owed. Fixing the holder left the two facts staged apart -- contention on a
    throwaway handler calling ``get_or_create_claude_session`` directly, End
    afterwards on a different fixture reading a live set written empty by hand
    -- and two separately green facts do not add up to "an accepted turn and a
    blind End coexist". So there is one handler now, the parked caller is
    ``ClaudeAgent.handle_message`` with nothing stubbed between it and the
    generation lock, End runs inside the same event loop while that turn is
    suspended, and the set ``_resolve_live_state`` reads is the handler's own
    ``active_sessions`` -- so ``idle`` is a consequence of the parked turn,
    not a literal. The lesson is the one round 21 wrote down, aimed at round
    21: a fix inherits the blind spot of the thing it fixes, and replacing a
    fixture's WHO does not answer a question about its WHEN.
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
    # Round 20 retracts the sentence that used to stand here, "the yield is
    # unconditional and unbounded", and review was right about why: it is a
    # claim about SUSPENSION resting on an assertion about source text. An
    # uncontended ``asyncio.Lock`` acquires on a fast path that never returns
    # to the event loop, so on a quiet runtime the awaited call completes in
    # one step and nothing can interleave with it. The window is the CONTENDED
    # lock -- which is precisely the warm second-turn state staged above -- and
    # both halves are now DRIVEN against the real resolver instead of argued.
    resolve_source = inspect.getsource(SessionHandler.get_or_create_claude_session)
    assert "async with self._claude_runtime_generation_lock(composite_key):" in resolve_source
    assert "await self._wait_for_claude_receiver_cleanup(retry.composite_key)" in resolve_source

    # ONE handler, and everything below happens on it. Round 21 made End's own
    # ``cleanup_session`` the lock holder instead of the test, and review was
    # right that the halves were still staged apart: the contention ran on a
    # throwaway handler against a bare ``get_or_create_claude_session`` call,
    # and End ran afterwards, on a different fixture, reading a live set the
    # test had written empty by hand. Two green halves are not the claim. The
    # claim is that an ACCEPTED TURN and a blind End coexist, so round 22 puts
    # them in one interleaving: ``ClaudeAgent.handle_message`` is the parked
    # caller, ``cleanup_session`` is the holder, and the set
    # ``_resolve_live_state`` reads IS the handler's own ``active_sessions``.
    # "Idle" is then a consequence of the parked turn not having stamped
    # itself, which is the whole of PR7R-F1 -- not a literal this test chose.
    handler = object.__new__(SessionHandler)
    handler.claude_runtime_generation_locks = {}
    handler.active_sessions = set()
    handler.session_turn_started = {}
    handler.session_last_activity = {}
    client = types.SimpleNamespace(
        # The turn is live; the CLI has not exited yet.
        _transport=types.SimpleNamespace(_process=types.SimpleNamespace(returncode=None))
    )
    handler.claude_sessions = {"slack_a:/w": client}
    handler._claude_runtime_generation_key = lambda context, subagent: "slack_a:/w"

    async def _resolve_locked(*args, **kwargs):
        return client

    handler._get_or_create_claude_session_locked = _resolve_locked

    # REACHABILITY, which an early draft left to the reader and review was
    # right to challenge. The state this probe needs -- a client registered in
    # ``claude_sessions`` while its key is absent from ``claude_active_sessions``
    # -- is not a fresh-startup state: ``_get_or_create_claude_session_locked``
    # registers the client only once connection completes, so during a COLD
    # start there is nothing for ``_end_claude`` to tear down and the defect
    # cannot fire. It is the ordinary WARM-IDLE state. ``mark_session_idle``
    # discards the key from the live set and deliberately keeps the client --
    # it even touches activity on the strength of the client still being there.
    # Driven on the very handler the interleaving then runs on, so the state
    # under test is the state production put there.
    handler.mark_session_active("slack_a:/w")
    assert "slack_a:/w" in handler.active_sessions
    handler.mark_session_idle("slack_a:/w")
    assert "slack_a:/w" not in handler.active_sessions
    assert "slack_a:/w" in handler.claude_sessions, (
        "warm-idle: the live set forgot the key and the client is still there"
    )

    # The real backend object. Only the cancel path is stubbed, and only
    # because this probe ends the turn rather than completing it: the turn
    # never gets past session resolution, which is the point.
    agent = object.__new__(ClaudeAgent)
    agent.session_handler = handler
    agent._remove_specific_pending_reaction = _AsyncFlag()
    agent._delete_ack = _AsyncFlag()
    agent._remove_pending_request = lambda *a, **kw: None
    agent._mark_session_idle_if_no_pending_requests = lambda *a, **kw: None
    agent._release_service_runtime_turn = lambda *a, **kw: None
    # A backstop one statement PAST the subject, so that a run in which the
    # window has closed reports the closure instead of crashing somewhere
    # downstream. ``_wait_for_activity_output`` is the first await after
    # ``mark_session_active``: if session resolution ever returns during the
    # staged window, the turn stamps itself and stops here, and it is
    # ``unstamped`` below that fails -- with a sentence about the race, which
    # is the assertion that should speak.
    _never = asyncio.Event()

    async def _hold(*args, **kwargs):
        await _never.wait()

    agent._wait_for_activity_output = _hold
    request = types.SimpleNamespace(
        context=None,
        base_session_id="b1",
        composite_session_id="slack_a:/w",
        subagent_name=None,
        subagent_model=None,
        subagent_reasoning_effort=None,
    )

    # End's fixture, sharing the handler's own registries rather than copies.
    session_handler = _RealTeardownMarking()
    session_handler.claude_sessions = handler.claude_sessions
    end_runtime_session = _AsyncFlag(result=True)
    controller = types.SimpleNamespace(
        agent_service=types.SimpleNamespace(
            agents={"claude": types.SimpleNamespace(end_runtime_session=end_runtime_session)}
        ),
        session_handler=session_handler,
        claude_sessions=handler.claude_sessions,
        # Not a literal: this is the set ``mark_session_active`` writes to.
        claude_active_sessions=handler.active_sessions,
        session_last_activity=handler.session_last_activity,
        # Direct-IM lane: no Workbench turn projection to rescue the probe.
        session_turns=types.SimpleNamespace(in_flight={}),
        command_handler=types.SimpleNamespace(handle_stop=_AsyncFlag(result=True)),
    )

    async def _drive_the_interleave():
        # Uncontended first. One scheduler step is enough for the whole call,
        # which is the half that makes reading the source insufficient: the
        # ``await`` is there and it does not suspend.
        free = asyncio.create_task(handler.get_or_create_claude_session(None))
        await asyncio.sleep(0)
        uncontended = free.done()
        assert await free is client

        # Now the contention, and it is production's: ``cleanup_session``
        # takes the generation lock for this key and holds it across its
        # locked body. That method is not an arbitrary choice of holder -- it
        # is the exact one End's own chain terminates on, hop by hop, as
        # asserted further down.
        inside_cleanup = asyncio.Event()
        finish_cleanup = asyncio.Event()

        async def _cleanup_locked(*args, **kwargs):
            inside_cleanup.set()
            await finish_cleanup.wait()

        handler._cleanup_session_locked = _cleanup_locked
        teardown = asyncio.create_task(handler.cleanup_session("slack_a:/w"))
        await inside_cleanup.wait()

        # A real accepted turn arrives on the warm-idle session. Nothing
        # between ``handle_message`` and the generation lock is stubbed; it
        # parks inside session resolution, before anything stamps it active.
        # Eight scheduler steps, so "not done" is a park and not a slow start.
        turn = asyncio.create_task(agent.handle_message(request))
        for _ in range(8):
            await asyncio.sleep(0)
        parked = not turn.done()
        unstamped = "slack_a:/w" not in handler.active_sessions

        # End arrives HERE -- with that turn still in flight, in the same
        # event loop, reading the same registries.
        live_state = running_agents._resolve_live_state(
            controller,
            backend="claude",
            session_id="ses-im",
            composite_key="slack_a:/w",
            base_session_id="b1",
        )
        result = await running_agents.end_running_agent(
            controller,
            backend="claude",
            # The browser polled the row while it was genuinely active.
            state="active",
            session_id="ses-im",
            composite_key="slack_a:/w",
            base_session_id="b1",
        )

        # Retire the parked turn BEFORE the lock is released. Letting it
        # resume would run it through the rest of ``handle_message`` -- the
        # activity wait, the query, the receiver task -- none of which this
        # probe is about, and all of which would need stubbing to reach a
        # conclusion that is already reached.
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        finish_cleanup.set()
        await teardown
        return uncontended, parked, unstamped, live_state, result

    uncontended, parked, unstamped, live_state, result = asyncio.run(
        _drive_the_interleave()
    )
    assert uncontended, (
        "an uncontended acquire suspended after all; if that changes, the "
        "retraction above is what needs rereading, not this assertion"
    )
    assert parked, (
        "``handle_message`` did not park inside session resolution while End's "
        "own teardown held the generation lock, so the window PR7R-F1 depends "
        "on is not a window and the finding needs rereading. This fails if "
        "``cleanup_session`` stops taking the lock, which is the right way for "
        "it to fail -- the reachability argument would be gone with it."
    )
    assert unstamped, (
        "the accepted turn was already stamped active while End looked, so "
        "the window PR7R-F1 depends on was not open. Either session resolution "
        "returned inside the staged contention -- ``cleanup_session`` stopped "
        "taking the generation lock -- or ``handle_message`` now stamps before "
        "it resolves. Both close the race; both make this finding wrong, which "
        "is the right way for this probe to fail."
    )
    # The turn IS running -- a real ``handle_message`` is suspended mid-call
    # right now -- and the probe cannot see it.
    assert live_state == "idle"

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


#: A dict key that names a Run, whatever the value under it turns out to be.
_RUN_ID_KEY = re.compile(r"(?:^|_)runs?_?ids?$|^run_?ids?$", re.IGNORECASE)

#: The trailing id-word to strip off the flat list's key to get its stem, so
#: ``accepted_agent_run_ids`` yields ``accepted_agent_run`` and any sibling
#: filed under that stem is recognised as a companion field.
_ID_SUFFIX = re.compile(r"_?ids?$", re.IGNORECASE)

#: Values a positionally-keyed vector can hold. A vector of dicts is already
#: caught by the mention and shape rules; this is for the scalar case, which is
#: the one that carries no id of its own and therefore nothing to match on.
_SCALAR = (str, int, float, bool, type(None))


def _flat_list_stem(flat_list_key: str) -> str:
    """``accepted_agent_run_ids`` -> ``accepted_agent_run_``, or ``""``."""

    leaf = flat_list_key.rsplit(".", 1)[-1]
    stem = _ID_SUFFIX.sub("", leaf)
    return f"{stem}_" if len(stem) >= 3 and stem != leaf else ""


def _per_run_provenance_sites(
    recorded: dict, accepted: list[str], *, flat_list_key: str
) -> list[str]:
    """Every place in a Turn projection that records something PER Run.

    Round 17's third finding. The previous detector was a one-line
    comprehension over the projection's TOP level looking for a dict whose keys
    intersect the accepted ids, and the comment above it claimed a future field
    carrying per-Run provenance "would fail here". It would not: a list of
    ``{"run_id": ..., "source_kind": ...}`` records, a nested map one level
    down, or a scalar naming a single Run all slip past it. That is the
    degenerate-assertion shape this unit keeps finding -- a check that reads
    like the rule and enforces one special case of it -- and it was load-bearing
    for Q3's verdict, since the verdict rests on this search coming back empty.

    Round 17 wrote "two rules, because per-Run can be spelled two ways" and
    round 19 retracted the count: both of its rules need the data to CARRY a
    run id, and the cheapest way to add per-Run provenance to a flat list of
    ids carries none. A sibling vector --
    ``{"accepted_agent_run_ids": ["run-a", "run-b"],
    "accepted_agent_run_sources": ["scheduler", "manual_cli"]}`` -- is keyed by
    POSITION. No id appears in it and its key is not run-shaped, so a detector
    built on mention and shape reports the projection clean and Q3 keeps its
    verdict while the thing the verdict denies sits one key away. It is the
    third round running that this helper has been found blind to a shape, and
    the pattern in all three is the same: each rule was written from the
    example in front of it.

    Four rules now, and the last two exist to cover data that identifies a Run
    without naming one:

    * MENTION -- an accepted run id appears anywhere below the projection as a
      dict key or as a string leaf, outside the flat list that is supposed to
      be the only place it appears. That catches maps keyed by run id at any
      depth, record lists, and a scalar pointing at one Run.
    * SHAPE -- a dict key that names a Run at all (``run_id``, ``runIds``,
      ``source_run_id``). That catches a record whose ids are not ours, which a
      mention rule alone would call clean on this fixture and let through on
      the next.
    * STEM -- a key filed under the flat list's own stem, so
      ``accepted_agent_run_sources`` and ``accepted_agent_run_deadlines`` are
      companions of ``accepted_agent_run_ids`` by name, whatever they hold and
      however long they are.
    * POSITION -- a scalar sequence, at any depth, as long as the accepted-id
      list and not that list. Deliberately over-inclusive: this detector's
      EMPTINESS is what carries Q3's verdict, so a false positive costs a human
      one look and a false negative costs the verdict. Its stated limit is that
      it is inert when there is a single accepted Run, because "aligned with a
      one-element list" and "a one-element list" are the same shape; the STEM
      rule is what covers that case, and the controls below pin both halves.

    Paths are returned rather than keys, because a hit two levels down that
    reports only its leaf name is a failure message nobody can act on.
    """
    wanted = set(accepted)
    stem = _flat_list_stem(flat_list_key)
    aligned = len(accepted) if len(accepted) >= 2 else None
    sites: list[str] = []

    def positional(node: object) -> bool:
        return (
            aligned is not None
            and isinstance(node, (list, tuple))
            and len(node) == aligned
            and all(isinstance(item, _SCALAR) for item in node)
        )

    def walk(node: object, path: str, *, skip_mentions: bool) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                where = f"{path}.{key}" if path else str(key)
                if not skip_mentions and str(key) in wanted:
                    sites.append(f"{where} (key names a Run)")
                if _RUN_ID_KEY.search(str(key)) and where != flat_list_key:
                    sites.append(f"{where} (key shape names a Run)")
                companion = (
                    bool(stem) and str(key).startswith(stem) and where != flat_list_key
                )
                if companion:
                    sites.append(f"{where} (key parallels the flat run-id list)")
                elif where != flat_list_key and positional(value):
                    sites.append(f"{where} (positionally aligned with the run ids)")
                walk(value, where, skip_mentions=where == flat_list_key)
            return
        if isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]", skip_mentions=skip_mentions)
            return
        if not skip_mentions and isinstance(node, str) and node in wanted:
            sites.append(f"{path} (value names a Run)")

    walk(recorded, "", skip_mentions=False)
    return sorted(sites)


def test_the_accepted_run_batch_records_no_per_run_source_or_deadline():
    """HFR-182 / Q3: a Turn's accepted-run record cannot discriminate.

    Scoped deliberately to the half this reaches. ``_attach_accepted_agent_runs``
    is DOWNSTREAM of the decision it would be tempting to test here: it appends
    ids already attributed to a Turn, so driving it twice proves nothing about
    whether a cron Run and a manual CLI Run coalesce. That question belongs to
    ``SessionTurnManager._hydrate_delivery_batch_context``, which folds a
    Delivery batch into one context, and the matrix carries it as an open probe.

    What IS provable here is the consequence, and it holds however the merge
    happens: the Turn keeps a flat list of run ids and nothing per-Run. There is
    one Turn-level ``source_kind``, a later participant does not restamp it, and
    a cancellation consulting it would therefore answer for a Run that may not
    be the one it is about.

    Round 10 corrected the wording of exactly that sentence. It used to say the
    label is "whatever the FIRST participant stamped" -- which this fixture
    PRELOADS and cannot possibly establish, so the assertion's own message was
    claiming the conclusion the docstring two paragraphs down concedes is not
    reached here. The driven fact is the weaker and still useful one: the append
    path does not write the label, so whoever set it keeps it. Who that is stays
    with ``_hydrate_delivery_batch_context`` and stays an open probe.

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
        # A manual `vibe task run` of a different definition, same Session,
        # arriving with its OWN provenance -- which is the only way to ask
        # whether a second participant restamps the Turn. Round 9 passed
        # ``None`` here and asserted about first-participant ownership anyway.
        run_ids=["run-manual-cli"],
        context=types.SimpleNamespace(
            platform_specific={"turn_token": "turn-1", "source_kind": "manual_cli"}
        ),
    )

    recorded = projected.context.platform_specific
    accepted = recorded["accepted_agent_run_ids"]
    assert accepted == ["run-cron", "run-manual-cli"]

    # The load-bearing assertion: for every accepted run id, the Turn holds no
    # keyed record of that run's source or deadline. Written as a search over
    # what was actually recorded rather than a check of two known keys, so a
    # future field that DID carry per-Run provenance fails here and forces this
    # verdict to be revisited.
    #
    # Positive controls first, because until round 17 this search was a
    # top-level intersection that could only see ONE of these four shapes, and
    # a detector whose emptiness carries a verdict has to be shown firing. The
    # last fixture is the one the old form was blindest to: records whose ids
    # are not even ours.
    flat = "accepted_agent_run_ids"
    probe_ids = ["run-a", "run-b"]
    assert _per_run_provenance_sites(
        {flat: probe_ids, "source_kind": "scheduler", "turn_token": "turn-1"},
        probe_ids,
        flat_list_key=flat,
    ) == []
    assert _per_run_provenance_sites(
        {flat: probe_ids, "run_deadlines": {"run-a": 30}},
        probe_ids,
        flat_list_key=flat,
    ) == ["run_deadlines.run-a (key names a Run)"]
    assert _per_run_provenance_sites(
        {flat: probe_ids, "batch": {"provenance": {"run-b": {"source": "cron"}}}},
        probe_ids,
        flat_list_key=flat,
    ) == ["batch.provenance.run-b (key names a Run)"]
    assert _per_run_provenance_sites(
        {flat: probe_ids, "primary_run": "run-a"},
        probe_ids,
        flat_list_key=flat,
    ) == ["primary_run (value names a Run)"]
    assert _per_run_provenance_sites(
        {flat: probe_ids, "runs": [{"run_id": "run-z", "source_kind": "manual_cli"}]},
        probe_ids,
        flat_list_key=flat,
    ) == ["runs[0].run_id (key shape names a Run)"]

    # Round 19's shape, and the cheapest way anyone would actually add per-Run
    # provenance to a flat list of ids: a second list of the same length, keyed
    # by POSITION. It holds no id and its key is not run-shaped, so the two
    # rules round 17 shipped both report it clean. Caught twice over here --
    # by name, because it is filed under the flat list's own stem, and by
    # length -- and the two are separated below so that neither can be the only
    # thing holding it.
    assert _per_run_provenance_sites(
        {flat: probe_ids, "accepted_agent_run_sources": ["scheduler", "manual_cli"]},
        probe_ids,
        flat_list_key=flat,
    ) == ["accepted_agent_run_sources (key parallels the flat run-id list)"]
    assert _per_run_provenance_sites(
        {flat: probe_ids, "batch": {"deadline_seconds": [30, 60]}},
        probe_ids,
        flat_list_key=flat,
    ) == ["batch.deadline_seconds (positionally aligned with the run ids)"]

    # The positional rule is a length match, not "any list": a vector that is
    # not aligned is not a hit, or the rule would fire on every collection in
    # the projection and stop meaning anything.
    assert (
        _per_run_provenance_sites(
            {flat: probe_ids, "tags": ["a", "b", "c"]},
            probe_ids,
            flat_list_key=flat,
        )
        == []
    )

    # The stated limit, asserted instead of described. With ONE accepted Run,
    # "aligned with the id list" and "a one-element list" are the same shape,
    # so the positional rule is switched off and the stem rule is what covers
    # the case -- both halves pinned, so a later edit cannot quietly drop the
    # cover and leave the limit written down as if it were still true.
    one = ["run-a"]
    assert (
        _per_run_provenance_sites(
            {flat: one, "unrelated_pair": ["x"]}, one, flat_list_key=flat
        )
        == []
    )
    assert _per_run_provenance_sites(
        {flat: one, "accepted_agent_run_sources": ["scheduler"]},
        one,
        flat_list_key=flat,
    ) == ["accepted_agent_run_sources (key parallels the flat run-id list)"]

    assert _per_run_provenance_sites(recorded, accepted, flat_list_key=flat) == []
    # One Turn-level label, and the append path is not what sets it: the second
    # participant carried ``manual_cli`` and the Turn still reads ``scheduler``.
    # This says the label cannot be corrected by a later Run, NOT that the first
    # Run is what stamped it -- that decision is upstream and still unproven.
    assert recorded["source_kind"] == "scheduler", recorded
    assert "source_kind" not in inspect.getsource(
        SessionTurnManager._attach_accepted_agent_runs
    ), "the append path started writing provenance; Q3 has to be re-derived"

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
    """HFR-183 / Q2: every backend and lane carries an exact-Turn signal LIVE.

    The plan forbids a generic inactivity timeout unless every backend and lane
    has an exact-Turn progress signal, and states that session-wide activity is
    never an acceptable substitute.

    Scope, and round 17 narrowed it: everything below walks a LIVE dispatch
    path. OpenCode has a second entry point after a daemon restart, which this
    probe never reached and which discards the identity; that is
    ``test_a_restored_opencode_poll_loop_emits_without_its_turn_identity`` and
    the two opencode cells are ``defect`` because of it. The closing section
    here reads the table, so it enforces that split rather than restating this
    paragraph.

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
    passes through. There is no lane split on the live path, and round 17
    narrows the wording draft 5 landed on -- "all six cells" carry an exact-Turn
    signal was true of what this probe walks and is retracted as a statement
    about the unit, since the restart path is neither. Driven in
    ``test_the_shared_admission_layer_stamps_a_turn_token_on_direct_im``.

    What is NOT settled by that is what the consumer does with the signal.
    Round 8 said, and round 9 RETRACTED, "for codex it drops it". The settled
    reading is that ``should_emit_progress`` returning False for the older turn
    is correct filtering of a turn ``handle_message`` has already interrupted --
    HFR-195 drives both halves, the serialization at the lock and what becomes
    of that turn's late events, and the closing section here shows the slot the
    drop reads.

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
    # against the conclusion: a working-path change splits the gate key, driven
    # in ``test_a_cwd_change_splits_the_gate_key_but_not_the_codex_turn_slot``,
    # and round eight concluded from it, in a sentence round nine RETRACTED,
    # that "the drop above is a discarded live signal, not correct filtering".
    # The gate is not the only serializer. ``CodexAgent.handle_message`` holds
    # ``_session_locks[base]`` -- the REGISTRY's key space, not the gate's --
    # across its whole body and sends ``turn/interrupt`` before ``turn/start``,
    # so turn-1 has already been interrupted by the time turn-2 is the active
    # turn and the drop above is correct filtering. HFR-195 drives that, and it
    # drives turn-1's late events too, which is what makes the residual window
    # harmless rather than closed.

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
    # just showed, cell by cell.
    from tests.run_terminal_truth_evidence import (
        BACKENDS,
        EXACT_TURN_PROGRESS_SIGNALS,
        LANES,
    )

    assert set(EXACT_TURN_PROGRESS_SIGNALS) == {
        (backend, lane) for backend in BACKENDS for lane in LANES
    }
    # Every cell went covered after round 6: no lane split and no backend
    # split, the two IM cells having been open only because the shared
    # admission layer had been missed. Round 17 splits the table again, on a
    # boundary neither the backend nor the lane predicts -- the restart path,
    # which only opencode's probe reaches. The expectations are spelled out per
    # cell rather than computed from ``cell[0]``/``cell[1]``, because the
    # previous form encoded "the answer is a function of backend and lane" and
    # that is precisely the belief round 17 falsified.
    expectations = {
        ("claude", "durable_workbench"): (
            "covered",
            "test_which_backends_attribute_a_progress_event_to_an_exact_turn",
        ),
        ("claude", "direct_im"): (
            "covered",
            "test_the_shared_admission_layer_stamps_a_turn_token_on_direct_im",
        ),
        ("codex", "durable_workbench"): (
            "covered",
            "test_which_backends_attribute_a_progress_event_to_an_exact_turn",
        ),
        ("codex", "direct_im"): (
            "covered",
            "test_which_backends_attribute_a_progress_event_to_an_exact_turn",
        ),
        ("opencode", "durable_workbench"): (
            "defect",
            "test_a_restored_opencode_poll_loop_emits_without_its_turn_identity",
        ),
        ("opencode", "direct_im"): (
            "defect",
            "test_a_restored_opencode_poll_loop_emits_without_its_turn_identity",
        ),
    }
    assert set(expectations) == set(EXACT_TURN_PROGRESS_SIGNALS)
    for cell, (kind, reason) in EXACT_TURN_PROGRESS_SIGNALS.items():
        expected_kind, expected_node = expectations[cell]
        assert kind == expected_kind, (cell, kind)
        assert reason.endswith(expected_node), cell
    # The live half this probe drives is still true of opencode, and the cells
    # above no longer say so, so it is asserted here instead of inferred: the
    # defect detail names the restart and not the poll loop walked above.
    for lane in LANES:
        assert "restart" in EXACT_TURN_PROGRESS_SIGNALS[("opencode", lane)][1]



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
    # which shows the two key spaces disagreeing -- and then shows codex's own
    # inner lock supplying the exclusion the gate stopped supplying.


def test_a_cwd_change_splits_the_gate_key_but_not_the_codex_turn_slot():
    """HFR-195 / Q2: a cwd change splits the gate, and codex's own lock catches it.

    This closes the probe the codex cell has been carrying since round four --
    "whether any state can put two live turns on one base session" -- and the
    answer is no, but not for the reason the shared gate suggested.

    Two identifiers that look like the same thing are not. The admission gate
    keys on ``BaseAgent.runtime_turn_key``, the COMPOSITE identity
    ``<base>:<working_path>``; codex's ``_active_turns`` keys on
    ``request.base_session_id`` ALONE. Same session, two working paths, and the
    gate hands out two permits where the registry has one slot. That much is
    real and is driven in parts (1)-(3) below.

    What round eight then inferred from it -- that the second turn silently
    mutes a first turn which is still running -- is RETRACTED here, and part (4)
    is what retracts it. ``CodexAgent.handle_message`` wraps its whole body in
    ``self._session_locks[request.base_session_id]``, which is the registry's
    key space, not the gate's; and inside that lock it sends ``turn/interrupt``
    for any active turn before ``turn/start``. So the second request cannot even
    reach the backend until the first has registered, and when it does reach it
    the first turn is interrupted first. ``should_emit_progress`` returning
    False for turn-1 is therefore correct filtering of an interrupted turn, not
    a discarded live signal.

    The real consequence of the key split is smaller and different: a cwd change
    converts "queue behind the gate and run after" into "interrupt the running
    turn and replace it". That is a behavioural difference worth recording, and
    it is what part (4) actually observes.

    This claim has now flipped three times -- correct, defective, correct --
    across rounds six, eight and nine, and every flip was argued from the two
    ends of a mechanism rather than from the mechanism. Rounds six and eight
    both reasoned about ``_active_turns`` and about the gate without ever
    running ``handle_message``, which is the code that sits between them. A
    claim that keeps flipping is not an unlucky claim; it is a claim whose
    subject was never driven end to end. So part (4) drives it, on the real
    method, with only the collaborators it needs to reach the network stubbed.

    Round ten narrows the answer once more, and the narrowing is the same shape
    as every earlier flip. "There is no window in which two codex turns are
    live" was asserted from the REGISTRY -- one slot, therefore one turn -- and
    that reading is too strong, because the registry is an in-process projection
    of a backend that has its own opinion. ``docs/plans/codex-app-server-refactor.md`` specifies three steps
    for insertion: interrupt, WAIT for the interrupted completion, then start.
    Production does the first and third. So between ``turn/interrupt`` and the
    ``turn/completed(interrupted)`` that answers it, turn-1 IS still executing
    on the backend while turn-2 is registered. The window exists.

    What makes it harmless is not the registry but the two handlers that meet
    the window's arrivals, and part (4b) drives both through the real
    ``CodexEventHandler``: turn-1's late tail is dropped by the named guard in
    ``_on_item_completed`` while turn-2's lands, and turn-1's late
    ``turn/completed`` is handled as an interruption -- popped, ack removed,
    stream released, nothing emitted -- rather than mistaken for turn-2's
    result. Q2 asks whether attribution EXISTS, and it does; the window changes
    where it comes from, not whether it holds. The stronger sentence is
    retracted because it was true of the projection and not of the system.
    """
    import inspect as _inspect

    from modules.agents.base import BaseAgent
    from modules.agents.codex.session import CodexSessionManager

    def _codex_request(working_path: str):
        return types.SimpleNamespace(
            context=types.SimpleNamespace(platform_specific={}),
            message="m",
            base_session_id="base-1",
            session_key="sk",
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

    # (3) THE GATE admits both at once. Two distinct gate keys means two gates,
    # so the shared admission layer lets the second request through while the
    # first is still inside the backend. Note the scope: this is a fact about
    # ``AgentService``, and it is the whole of what the shared layer decides.
    # Whether two codex TURNS then coexist is decided further in, by part (4) --
    # rounds six and eight both stopped here and guessed at the rest.
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

    # (4) ...and codex's own lock closes what the gate opened. Driven on the
    # REAL ``CodexAgent.handle_message``: only the collaborators that would
    # reach a subprocess or a chat surface are stubbed, and the control flow
    # under test -- the per-base-session lock, the active-turn lookup, the
    # interrupt, the ordering of the two -- is production code.
    #
    # The stub for ``_start_turn`` is the one substitution that could beg the
    # question, so it is pinned to the real method: it sends ``turn/start`` and
    # then registers the turn from the response, which is what the real body
    # does, asserted below rather than assumed.
    real_start = _inspect.getsource(CodexAgent._start_turn)
    assert 'await transport.send_request("turn/start"' in real_start
    assert "self._turn_registry.finalize_turn_start_response(turn_id, request)" in real_start
    assert real_start.index('send_request("turn/start"') < real_start.index(
        "finalize_turn_start_response"
    ), "the stub below registers after sending; the real one must too"

    acks: list = []
    emitted: list = []
    released: list = []

    async def _drive_handle_message():
        calls: list[tuple[str, str]] = []
        first_start_reached = asyncio.Event()
        release_first_start = asyncio.Event()

        class _Transport:
            async def send_request(self, method, params):
                turn = params.get("turnId", "")
                calls.append((method, turn))
                if method == "turn/start":
                    if not first_start_reached.is_set():
                        first_start_reached.set()
                        await release_first_start.wait()
                        return {"id": "turn-1"}
                    return {"id": "turn-2"}
                return {}

        transport = _Transport()
        live = object.__new__(CodexAgent)
        live.controller = types.SimpleNamespace(
            model_hub_runtime=None,
            mark_turn_complete=lambda ctx: released.append(("mark", ctx)),
            agent_service=types.SimpleNamespace(
                release_runtime_turn=lambda ctx: released.append(("release", ctx))
            ),
        )
        live._session_locks = {}
        live._session_mgr = CodexSessionManager()
        live._session_mgr.set_thread_id("base-1", "thread-1")
        live._turn_registry = CodexTurnRegistry()
        # The REAL event handler, because part (4b) below drives the window
        # through it and a stub there would decide the answer.
        live._event_handler = CodexEventHandler(live)

        async def _noop_async(*a, **kw):
            return None

        async def _get_transport(*a, **kw):
            return transport

        live._get_or_create_transport = _get_transport
        live._touch_transport_activity = lambda *a, **kw: None
        live._delete_ack = _noop_async
        live._refresh_thread_developer_instructions_if_needed = _noop_async
        live._bind_runtime_agent_session_id = lambda *a, **kw: None

        async def _remove_ack(request):
            acks.append(request)

        live._remove_ack_reaction = _remove_ack

        async def _emit_result(*a, **kw):
            emitted.append((a, kw))

        live.emit_result_message = _emit_result

        async def _fake_start_turn(_transport, request, thread_id):
            live._turn_registry.begin_turn_start(request, thread_id)
            resp = await _transport.send_request(
                "turn/start", {"threadId": thread_id, "input": request.message}
            )
            live._turn_registry.finalize_turn_start_response(resp["id"], request)
            return thread_id

        live._start_turn = _fake_start_turn

        first = asyncio.create_task(live.handle_message(first_request))
        await asyncio.wait_for(first_start_reached.wait(), timeout=2)

        second = asyncio.create_task(live.handle_message(second_request))
        await asyncio.sleep(0.05)
        # THE RETRACTION. The gate let this second request through (part 3), but
        # codex's own lock is keyed by base session -- the registry's key space,
        # not the gate's -- so it has made no backend call at all. The two
        # requests are serialized before they reach the backend.
        blocked = list(calls)

        release_first_start.set()
        await asyncio.gather(
            *(asyncio.wait_for(t, timeout=2) for t in (first, second)),
            return_exceptions=True,
        )
        return blocked, calls, live

    blocked, calls, live = asyncio.run(_drive_handle_message())
    live_registry = live._turn_registry
    assert blocked == [("turn/start", "")], blocked
    # And when it is finally admitted, it interrupts turn-1 BEFORE starting
    # turn-2. So the slot overwrite lands on a turn that has been told to stop.
    assert calls == [
        ("turn/start", ""),
        ("turn/interrupt", "turn-1"),
        ("turn/start", ""),
    ], calls
    assert live_registry.get_active_turn("base-1") == "turn-2"
    assert live_registry.should_emit_progress("turn-1") is False
    assert live_registry.should_emit_progress("turn-2") is True

    # (4b) The window, which round 9 asserted away instead of driving. Codex's
    # protocol note (``docs/plans/codex-app-server-refactor.md``, "Message
    # Insertion via turn/interrupt + turn/start") specifies THREE steps --
    # interrupt, WAIT for the interrupted completion, then start. Production
    # skips the wait: the call sequence in ``calls`` above goes straight from
    # ``turn/interrupt`` to ``turn/start``. So turn-1 is still executing on the
    # backend while turn-2 is registered, and round 9's "there is no window in
    # which two codex turns are live" was too strong. The design divergence is
    # asserted, not narrated, so this stops being true silently.
    _refactor_plan = (
        Path(__file__).resolve().parents[1]
        / "docs" / "plans" / "codex-app-server-refactor.md"
    ).read_text(encoding="utf-8")
    assert "Wait for `turn/completed` (with interrupted status)" in _refactor_plan
    handle_source = _inspect.getsource(CodexAgent.handle_message)
    assert '"turn/interrupt"' in handle_source
    assert "turn/completed" not in handle_source, (
        "handle_message started awaiting the interrupted completion; the window "
        "below no longer exists and this probe has to be re-derived"
    )

    # What makes the window harmless is not that it is closed but that both of
    # its arrivals are handled, and each is driven here through the real
    # ``handle_notification`` entry point on the real event handler.
    turn_1 = live_registry.get_turn("turn-1")
    turn_2 = live_registry.get_turn("turn-2")
    assert turn_1 is not None and turn_2 is not None, "the window needs both turns"
    assert turn_1.pending_assistant is None and turn_2.pending_assistant is None

    async def _drive_window():
        for turn_id in ("turn-1", "turn-2"):
            await live._event_handler.handle_notification(
                "item/completed",
                {
                    "turnId": turn_id,
                    "threadId": "thread-1",
                    "item": {"type": "agentMessage", "text": f"tail of {turn_id}"},
                },
                live_registry.get_turn(turn_id).request,
            )
        # ...and then the late completion the protocol says should have been
        # awaited before turn-2 ever started.
        await live._event_handler.handle_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "interrupted"}},
            turn_1.request,
        )

    # Part (4) already removed turn-1's ack: ``handle_message`` calls
    # ``clear_pending(active_turn)`` right after ``turn/interrupt`` and removes
    # the ack of whatever request it hid, without waiting for the completion.
    # So the ack removal is EAGER, and the late completion below removes it a
    # second time -- idempotent for every current surface, but a fact the
    # window makes visible and worth pinning rather than papering over.
    assert acks == [turn_1.request], acks
    assert released == [], released
    acks.clear()
    released.clear()

    asyncio.run(_drive_window())

    # The interrupted turn's tail is dropped -- deliberately, by the named guard
    # in ``_on_item_completed`` -- and the live turn's is not. The filter is
    # therefore selective, which is the property round 9 needed and asserted
    # from the registry alone.
    assert turn_1.pending_assistant is None, turn_1.pending_assistant
    assert turn_2.pending_assistant == ("tail of turn-2", "markdown")
    assert "Ignoring stale/interrupted item" in _inspect.getsource(
        CodexEventHandler._on_item_completed
    )

    # And the late ``turn/completed`` is handled as an interruption rather than
    # as turn-2's result: turn-1 is popped, its ack removed, its stream
    # released, and NOTHING is emitted to the user. Turn-2 is untouched.
    assert live_registry.get_turn("turn-1") is None
    assert acks == [turn_1.request]
    assert released == [("mark", turn_1.request.context), ("release", turn_1.request.context)]
    assert emitted == []
    assert live_registry.get_active_turn("base-1") == "turn-2"
    assert live_registry.should_emit_progress("turn-2") is True

    # The same eviction on the bare registry, for contrast: identical end state,
    # and it is ONLY the interrupt above that makes it correct rather than lossy.
    # Round eight asserted this half and called the difference a defect.
    registry.register_turn("turn-2", second_request)
    assert registry.get_active_turn("base-1") == "turn-2"
    assert registry.get_turn("turn-1") is not None  # still present, just mute
    assert registry.should_emit_progress("turn-1") is False

    # (5) And the registry's own derived key cannot name the REPLACED turn
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
    # timeout sweep) cannot even address the turn that was just interrupted,
    # which matters because an interrupt is a request, not a completion.
    assert codex.runtime_turn_keys_for_session_key("sk") == {"base-1:/w2"}
    assert gate_keys[0] not in codex.runtime_turn_keys_for_session_key("sk")

    # ...and that move is what ``handle_message`` does per request, which is the
    # production text making the collision reachable rather than hypothetical.
    assert (
        "self._session_mgr.set_cwd(request.base_session_id, request.working_path)"
        in inspect.getsource(CodexAgent.handle_message)
    )


def test_participating_run_attribution_is_resolved_per_turn_not_per_session(tmp_path):
    """HFR-197 / Q2: the "and participating Runs" half of the question.

    Q2 asks which events can be attributed to the exact Turn AND PARTICIPATING
    RUNS. Every probe before this one answered the first half and none answered
    the second, and the verdict was written as though the two were one claim.
    They are not: a Turn token is a token, and a Run id is a row.

    They are, however, joined by a mechanism, and the mechanism is what makes
    the second half cheap once the first is settled. Emission-time Run
    attribution has exactly two sources, both on the emit context that all three
    backends already carry:

      * ``_owned_agent_run_ids`` reads ``accepted_agent_run_ids`` straight off
        ``context.platform_specific``; and
      * ``_durable_accepted_agent_run_ids`` reads the ``turn_token`` off the
        SAME payload and looks the Runs up per Turn in the delivery store.

    So Run attribution is derived from Turn attribution, per emit. That is the
    claim, and the point of driving it is that a derivation can be exact at one
    end and lossy at the other -- if the durable read were keyed by session, or
    if the in-context list survived a Turn change, the Runs would smear across
    Turns even though the tokens did not.

    Round 10 supplied the half round 9 faked. Parts (1) through (5) drive the
    dispatcher against a ``_Turns`` stub, which shows that the READER asks per
    Turn but says nothing about whether the STORE can answer per Turn -- a stub
    that returns fabricated ids for fabricated tokens is green whether or not a
    Run is ever bound to a Turn at all. Part (6) therefore builds the rows: real
    schema, real claim/bind/materialize path, real ``attach_agent_run_delivery``,
    and the dispatcher reading through a real ``SessionTurnManager``. Both are
    kept, because they fail differently -- (1) catches a reader that stops
    passing the token, (6) catches a store that cannot keep two Turns apart.
    """
    from core.message_dispatcher import ConsolidatedMessageDispatcher, _owned_agent_run_ids
    from modules.agents.base import AGENT_TURN_TOKEN
    from modules.agents.claude_agent import ClaudeAgent

    # (1) The durable read is keyed by the TURN token, and by nothing else.
    looked_up: list[str] = []

    class _Turns:
        def accepted_agent_run_ids_for_turn(self, turn_id):
            looked_up.append(turn_id)
            return {"tok-a": ["run-a1", "run-a2"], "tok-b": ["run-b1"]}.get(turn_id, [])

    dispatcher = object.__new__(ConsolidatedMessageDispatcher)
    dispatcher.controller = types.SimpleNamespace(session_turns=_Turns())

    def _ctx(payload):
        return types.SimpleNamespace(platform_specific=payload)

    read = ConsolidatedMessageDispatcher._durable_accepted_agent_run_ids
    assert read(dispatcher, _ctx({"turn_token": "tok-a"})) == ["run-a1", "run-a2"]
    assert read(dispatcher, _ctx({"turn_token": "tok-b"})) == ["run-b1"]
    assert looked_up == ["tok-a", "tok-b"]

    # Two emits from ONE session but different Turns therefore resolve to
    # different Run sets -- which is the whole of "participating Runs, exactly".
    # And with no Turn token there is no Run attribution at all: the derivation
    # is load-bearing, not a decoration on an answer that holds without it.
    assert read(dispatcher, _ctx({"turn_token": ""})) == []
    assert read(dispatcher, _ctx({})) == []
    assert looked_up == ["tok-a", "tok-b"]  # not even attempted

    # (2) The in-context carrier is replaced per Turn, not merged. Driven on
    # claude, because claude is the backend whose emit context is REUSED across
    # turns -- the one place a stale Run id could survive a Turn change.
    adopt = ClaudeAgent._adopt_pending_turn_token
    reused = types.SimpleNamespace(
        platform_specific={AGENT_TURN_TOKEN: "tok-a", "accepted_agent_run_ids": ["run-a1"]}
    )
    pending_b = types.SimpleNamespace(
        context=types.SimpleNamespace(
            platform_specific={AGENT_TURN_TOKEN: "tok-b", "accepted_agent_run_ids": ["run-b1"]}
        )
    )
    adopt(reused, pending_b)
    assert reused.platform_specific[AGENT_TURN_TOKEN] == "tok-b"
    assert reused.platform_specific["accepted_agent_run_ids"] == ["run-b1"]

    # ...and a Turn with NO Runs clears the previous Turn's, rather than
    # inheriting them. A merge here would attribute turn B's output to turn A's
    # Run, which is the exact failure the question is asking about.
    pending_c = types.SimpleNamespace(
        context=types.SimpleNamespace(platform_specific={AGENT_TURN_TOKEN: "tok-c"})
    )
    adopt(reused, pending_c)
    assert reused.platform_specific[AGENT_TURN_TOKEN] == "tok-c"
    assert "accepted_agent_run_ids" not in reused.platform_specific

    # The copy is defensive, so mutating the emit context cannot write back into
    # the pending request's own attribution.
    adopt(reused, pending_b)
    reused.platform_specific["accepted_agent_run_ids"].append("run-x")
    assert pending_b.context.platform_specific["accepted_agent_run_ids"] == ["run-b1"]

    # (3) And the consumer reads that same payload key, so the carrier and the
    # reader are the same field rather than two fields with one name.
    assert _owned_agent_run_ids(reused.platform_specific) == ["run-b1", "run-x"]
    assert _owned_agent_run_ids({}) == []

    # (4) Codex, joined end to end rather than argued. HFR-183 established that
    # a notification resolves to its own turn's REQUEST; the step that was never
    # taken is reading the Run ids off what came back. Two turns on one base
    # session, each carrying its own Runs, resolved by ``turnId``:
    registry = CodexTurnRegistry()
    codex_requests = {
        "turn-1": types.SimpleNamespace(
            base_session_id="base-1",
            context=_ctx({AGENT_TURN_TOKEN: "tok-a", "accepted_agent_run_ids": ["run-a1"]}),
        ),
        "turn-2": types.SimpleNamespace(
            base_session_id="base-1",
            context=_ctx({AGENT_TURN_TOKEN: "tok-b", "accepted_agent_run_ids": ["run-b1"]}),
        ),
    }
    for turn_id, request in codex_requests.items():
        registry.register_turn(turn_id, request)
    codex_agent = object.__new__(CodexAgent)
    codex_agent._turn_registry = registry
    codex_agent._session_mgr = types.SimpleNamespace(
        find_base_session_id_for_thread=lambda _thread: "base-1"
    )
    resolved_runs = {
        turn_id: _owned_agent_run_ids(
            codex_agent._find_request_for_notification(
                "item/completed", {"turnId": turn_id, "threadId": "thread-1"}
            ).context.platform_specific
        )
        for turn_id in codex_requests
    }
    assert resolved_runs == {"turn-1": ["run-a1"], "turn-2": ["run-b1"]}, resolved_runs
    # ...and the durable side lands the same way, from the token on that same
    # resolved context rather than from the session.
    assert [
        read(dispatcher, codex_requests[t].context) for t in ("turn-1", "turn-2")
    ] == [["run-a1", "run-a2"], ["run-b1"]]

    # (5) Scope, stated rather than implied. OpenCode is NOT re-driven here: its
    # emit context is ``request.context`` (HFR-183), so it is the same object
    # this test already read, and the pointer below is a pointer, not evidence.
    from modules.agents.opencode.poll_loop import OpenCodePollLoop

    assert "request.context" in inspect.getsource(OpenCodePollLoop.run_prompt_poll)

    # (6) The rows themselves, which is where round 9 stopped and asserted
    # anyway. One Session, three Turns taken in sequence -- the schema permits
    # only one live Turn per Session, so "sequential" is the real shape and the
    # smear this is looking for is a LATER Turn inheriting an earlier one's
    # Runs. Turn A merges two Deliveries into one native start and so has two
    # participating Runs; ``agent_runs.delivery_id`` is unique, which is why
    # plural participation must come from a merged batch and not from two Runs
    # on one Delivery. Turn C is claimed and never materialized, so its Run is
    # bound to a real Delivery of a real Turn that was never ACCEPTED.
    from storage import message_deliveries as durable_deliveries
    from storage.background import attach_agent_run_delivery_in_connection
    from storage.models import agent_runs

    engine = _durable_engine(tmp_path)
    with engine.begin() as conn:
        for turn_id, delivery_ids, accepted_turn in (
            ("turn-a", ("del-a1", "del-a2"), True),
            ("turn-b", ("del-b1",), True),
            ("turn-c", ("del-c1",), False),
        ):
            batch = [
                durable_deliveries.insert_delivery(
                    conn,
                    delivery_id=delivery_id,
                    session_id="ses-1",
                    priority="p3",
                    state="queued",
                    snapshot=durable_deliveries.message_snapshot(
                        scope_id=None, session_id="ses-1", platform="avibe",
                        author="harness", source="harness", message_type="user",
                        text=f"prompt {delivery_id}", metadata={},
                    ),
                    dispatch_text=f"prompt {delivery_id}",
                )
                for delivery_id in delivery_ids
            ]
            claimed = durable_deliveries.claim_start_batch(
                conn, turn_id=turn_id, session_id="ses-1", backend="codex",
                deliveries=batch, dispatch_text=f"prompt {turn_id}",
            )
            if accepted_turn:
                durable_deliveries.bind_native_start(
                    conn, turn_id,
                    expected_version=int(claimed["turn"]["version"]),
                    runtime_key="ses-1:/w", runtime_turn_id=turn_id,
                    native_turn_id=turn_id,
                )
                durable_deliveries.materialize_start_acceptance(
                    conn, turn_id=turn_id, evidence={"kind": "probe"},
                )
            durable_deliveries.terminalize_turn(
                conn, turn_id, outcome="completed", settled_by="probe",
                evidence_kind="probe",
            )
        for run_id, delivery_id in (
            ("run-a1", "del-a1"), ("run-a2", "del-a2"),
            ("run-b1", "del-b1"), ("run-c1", "del-c1"),
        ):
            conn.execute(
                agent_runs.insert().values(
                    id=run_id, definition_id=None, run_type="agent_run",
                    status="running", cancel_requested=0, session_id="ses-1",
                    created_at=_STAMP, updated_at=_STAMP, metadata_json="{}",
                )
            )
            assert attach_agent_run_delivery_in_connection(
                conn, run_id, session_id="ses-1", delivery_id=delivery_id
            ), run_id

    durable = object.__new__(SessionTurnManager)
    durable._engine = engine
    assert durable._durable_schema_available()

    # The Runs split by Turn, out of the store, with no stub anywhere in the
    # path -- which is the claim Q2's Run half rests on.
    assert durable.accepted_agent_run_ids_for_turn("turn-a") == ["run-a1", "run-a2"]
    assert durable.accepted_agent_run_ids_for_turn("turn-b") == ["run-b1"]
    # A Turn that never reached acceptance contributes nothing, even though its
    # Run is bound and running: the read is over ACCEPTED participation, so an
    # abandoned start cannot lend its Run to the Session's next Turn.
    assert durable.accepted_agent_run_ids_for_turn("turn-c") == []
    assert durable.accepted_agent_run_ids_for_turn("turn-z") == []

    # ...and the dispatcher reads exactly that, through the real manager. Same
    # method as part (1), same emit-context shape, real rows underneath.
    live_dispatcher = object.__new__(ConsolidatedMessageDispatcher)
    live_dispatcher.controller = types.SimpleNamespace(session_turns=durable)
    assert read(live_dispatcher, _ctx({"turn_token": "turn-a"})) == ["run-a1", "run-a2"]
    assert read(live_dispatcher, _ctx({"turn_token": "turn-b"})) == ["run-b1"]

    # (7) The same rows on the OTHER lane, which round 12's finding is right
    # that nothing here built. Everything above stamps ``platform="avibe"`` --
    # the Workbench -- so "Run attribution holds on direct_im too" was carried
    # by the durable-lane rows plus the belief that the path is platform-blind.
    # It is platform-blind, and that is now driven rather than believed: an
    # IM-scoped Session, a telegram snapshot, and a Harness Run bound through
    # the same ``attach_agent_run_delivery`` resolve per Turn identically. The
    # belief was cheap to check, and cheap to check is not checked -- the same
    # sentence round 10 wrote about the stub it replaced.
    # A fourth and fifth schema lesson, and both appear only on this lane:
    # acceptance MATERIALIZES the Delivery into a ``messages`` row, whose
    # ``scope_id`` is a plain foreign key and whose ``session_id`` is a DEFERRED
    # one (so it fails at COMMIT, not at insert). Part (6) passes
    # ``scope_id=None`` and never persists a Message at all, so it meets
    # neither. An IM Delivery carries the scope it arrived in -- which is what
    # makes a Workbench-only fixture unable to discover this, and what makes
    # "the path is platform-blind" worth driving instead of asserting.
    from storage.models import agent_sessions, scopes

    with engine.begin() as conn:
        conn.execute(
            scopes.insert().values(
                id="scp-im", platform="telegram", scope_type="dm",
                native_id="tg-1", is_private=1, supports_threads=0,
                metadata_json="{}", first_seen_at=_STAMP, last_seen_at=_STAMP,
                updated_at=_STAMP,
            )
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses-im", agent_backend="claude", agent_variant="default",
                session_anchor="scp-im", native_session_id="native-im",
                status="active", visibility="foreground", pinned=0,
                agent_status="idle", metadata_json="{}",
                created_at=_STAMP, updated_at=_STAMP,
            )
        )
        im_batch = [
            durable_deliveries.insert_delivery(
                conn,
                delivery_id="del-im1",
                session_id="ses-im",
                priority="p3",
                state="queued",
                snapshot=durable_deliveries.message_snapshot(
                    scope_id="scp-im", session_id="ses-im", platform="telegram",
                    author="harness", source="harness", message_type="user",
                    text="scheduled prompt", metadata={},
                ),
                dispatch_text="scheduled prompt",
            )
        ]
        im_claimed = durable_deliveries.claim_start_batch(
            conn, turn_id="turn-im", session_id="ses-im", backend="claude",
            deliveries=im_batch, dispatch_text="scheduled prompt",
        )
        durable_deliveries.bind_native_start(
            conn, "turn-im",
            expected_version=int(im_claimed["turn"]["version"]),
            runtime_key="ses-im:/w", runtime_turn_id="turn-im",
            native_turn_id="turn-im",
        )
        durable_deliveries.materialize_start_acceptance(
            conn, turn_id="turn-im", evidence={"kind": "probe"},
        )
        conn.execute(
            agent_runs.insert().values(
                id="run-im1", definition_id=None, run_type="agent_run",
                status="running", cancel_requested=0, session_id="ses-im",
                created_at=_STAMP, updated_at=_STAMP, metadata_json="{}",
            )
        )
        assert attach_agent_run_delivery_in_connection(
            conn, "run-im1", session_id="ses-im", delivery_id="del-im1"
        )

    # The row really is on the IM lane -- otherwise this part re-runs part (6)
    # under a different Turn id and proves nothing about platforms. Read off
    # the MATERIALIZED Message rather than the snapshot, because acceptance
    # consumes the snapshot (``snapshot_json`` is NULL by now) and the Message
    # is what actually reached telegram.
    from sqlalchemy import select

    from storage.models import messages as messages_table

    with engine.begin() as conn:
        landed = conn.execute(
            select(messages_table.c.platform, messages_table.c.scope_id).where(
                messages_table.c.session_id == "ses-im"
            )
        ).all()
        stored = durable_deliveries.deliveries_for_turn(conn, "turn-im")
    assert landed == [("telegram", "scp-im")], landed
    assert [row["snapshot_json"] for row in stored] == [None]

    assert durable.accepted_agent_run_ids_for_turn("turn-im") == ["run-im1"]
    assert read(live_dispatcher, _ctx({"turn_token": "turn-im"})) == ["run-im1"]
    # ...and the two lanes do not leak into each other, which is the failure a
    # session-keyed read would produce and a Turn-keyed one cannot.
    assert durable.accepted_agent_run_ids_for_turn("turn-a") == ["run-a1", "run-a2"]

    # Why one lane's rows carry the other's conclusion, stated rather than
    # assumed: the write path takes no branch on platform. ``_submit_scheduled_turn``
    # inserts the Delivery, binds the Run and calls ``deliver`` gated on the
    # SESSION id, not the scope's surface (core/internal_server.py), and the only
    # platform comparison anywhere near the Turn write chooses which string
    # becomes ``context.message_id``. The one real bypass is sessionless,
    # CLI-style dispatch, which writes no Delivery and no Turn at all -- so its
    # attribution is empty rather than exact. That is outside both lanes here,
    # since both are defined by having a Session, and it is recorded so the next
    # unit does not read the Q2 signal table as covering it.
    assert durable.accepted_agent_run_ids_for_turn("") == []

    # Scope that survives round 10: whether every Run a Turn really executed is
    # bound to one of that Turn's Deliveries in the first place is a question
    # about the WRITE side, on Q5's boundary, and this probe assumes it.


# ----- HFR-199: Q1 durable reservation -------------------------------------


def test_no_step_of_the_durable_reservation_path_settles_the_run(tmp_path):
    """HFR-199 / Q1: the reservation half, driven against real rows.

    Round 10's first-priority finding, and it is the stubbed-store lesson in
    the other half of the unit. Q1's answer said, and this test SUPERSEDED,
    "a Delivery reservation and an
    ownership transfer both leave the Run ``running``" -- it cited a scheduler
    test that replaces ``submit_scheduled`` with a ``SimpleNamespace`` returning
    ``queue_persisted=True, delivery_owner_transferred=True``. No Delivery is
    reserved there and no Run row is read: what that test establishes is the
    SCHEDULER'S REACTION to a reported reservation, which is a fact about the
    caller, not about the boundary Q1 asks after. A store that settled the Run
    the moment its Delivery was reserved would have left it green.

    So the reservation is taken for real here -- real schema, real
    ``enqueue_queued``, real ``attach_agent_run_delivery_in_connection``, real
    claim/bind/materialize -- and the Run row is re-read after every single
    step. The trace is the assertion, rather than one check at the end, because
    "still nonterminal afterwards" and "never terminal in between" are different
    claims and only the second one is what a terminal-truth matrix can use.

    One fact fell out that the answer did not previously contain, and it is the
    sharper half: terminalizing the TURN does not settle the Run either. The
    settlement is a separate write, so a path that ends a Turn without calling
    it leaves a live Run with no owner -- which is Q4's subject, recorded here
    because this is where it became visible rather than argued.
    """
    from sqlalchemy import select

    from storage import message_deliveries as durable_deliveries
    from storage.background import (
        attach_agent_run_delivery_in_connection,
        settle_agent_runs_for_turn_in_connection,
    )
    from storage.models import agent_runs

    engine = _durable_engine(tmp_path)
    trace: list[tuple[str, str, bool]] = []

    def _observe(conn, step: str) -> None:
        row = (
            conn.execute(select(agent_runs).where(agent_runs.c.id == "run-1"))
            .mappings()
            .first()
        )
        trace.append((step, str(row["status"]), row["completed_at"] is not None))

    with engine.begin() as conn:
        conn.execute(
            agent_runs.insert().values(
                id="run-1", definition_id=None, run_type="agent_run",
                status="queued", cancel_requested=0, session_id="ses-1",
                source_kind="scheduler", created_at=_STAMP, updated_at=_STAMP,
                metadata_json="{}",
            )
        )
        _observe(conn, "run_enqueued")

        # (1) The reservation itself: a durable P3 Delivery, persisted.
        reserved = durable_deliveries.enqueue_queued(
            conn, scope_id=None, session_id="ses-1", text="cron prompt",
            source="harness", author="harness",
        )
        _observe(conn, "delivery_reserved")

        # (2) The ownership transfer: the Run hands its input to the Delivery
        # owner, which is the exact transition the stubbed test only reported.
        assert attach_agent_run_delivery_in_connection(
            conn, "run-1", session_id="ses-1", delivery_id=reserved["id"]
        )
        _observe(conn, "delivery_owner_transferred")

        # (3) ...and on through admission, so the claim covers the whole path
        # rather than stopping where the old citation did.
        claimed = durable_deliveries.claim_start_batch(
            conn, turn_id="turn-1", session_id="ses-1", backend="codex",
            deliveries=[durable_deliveries.get_delivery(conn, reserved["id"])],
            dispatch_text="cron prompt",
        )
        _observe(conn, "turn_claimed")
        durable_deliveries.bind_native_start(
            conn, "turn-1", expected_version=int(claimed["turn"]["version"]),
            runtime_key="ses-1:/w", runtime_turn_id="turn-1",
            native_turn_id="turn-1",
        )
        durable_deliveries.materialize_start_acceptance(
            conn, turn_id="turn-1", evidence={"kind": "probe"},
        )
        _observe(conn, "start_accepted")

        # (4) The Turn reaches its terminal state.
        assert durable_deliveries.terminalize_turn(
            conn, "turn-1", outcome="completed", settled_by="probe",
            evidence_kind="probe",
        )["changed"]
        _observe(conn, "turn_terminal")

        # (5) And only now, on the explicit participant settlement.
        assert settle_agent_runs_for_turn_in_connection(
            conn, ["run-1"], ok=True
        ) == ["run-1"]
        _observe(conn, "run_settled")

    nonterminal = [step for step, status, done in trace if not done]
    assert nonterminal == [
        "run_enqueued",
        "delivery_reserved",
        "delivery_owner_transferred",
        "turn_claimed",
        "start_accepted",
        "turn_terminal",
    ], trace
    assert {status for _s, status, done in trace if not done} == {"queued"}, trace
    assert trace[-1] == ("run_settled", "succeeded", True), trace[-1]


# ----- HFR-205: Q2, the OpenCode path that survives a restart --------------


def test_a_restored_opencode_poll_loop_emits_without_its_turn_identity():
    """HFR-205 / Q2: restart strips the Turn off OpenCode's emit context.

    Round 17's first finding, and it is the fifth instance of this unit's
    oldest reading error in a new place. HFR-183 established that opencode's
    progress emits carry a Turn -- and established it by walking exactly one
    function, ``run_prompt_poll``. There is a second emitting entry point.
    ``run_restored_poll_loop`` is what continues a poll that a restart
    interrupted, it emits tool calls and assistant output like the live loop
    does, and its context is not the live ``AgentRequest``'s: it is rebuilt by
    ``ProcessingIndicatorHandle.from_snapshot``, which reads ``platform``,
    ``is_dm`` and ``context_token`` out of ``platform_specific`` and drops
    everything else -- ``turn_token`` and ``accepted_agent_run_ids`` included.

    So a whole production path emits progress that cannot name its Turn or its
    Runs, and the previous rounds' answer to Q2 -- "all six cells carry an
    exact-Turn signal" -- is narrowed by this probe: it was true of the live
    path and asserted of the cell.

    The sharper half is what makes this a DEFECT rather than a gap. The turn id
    is not lost in persistence: ``OpenCodeAgent`` writes it into the SAME
    snapshot dict, under ``_STEERING_SNAPSHOT_KEY``, as ``logical_turn_id``,
    and the restore path reads that key back for steering while handing the
    emit context nothing. Production says so itself -- restored
    ``additional_steer_targets`` are built with ``context=None``. The identity
    survives the restart and is discarded at the rebuild.

    Round 18 retracted this docstring's next sentence, "which is a one-line
    remediation and a different one from persist more", because it generalised
    from the Turn to the cell. The assertions below say why: the Turn is in the
    snapshot, and ``run-1`` is not in it in any form, so the Run half has
    nothing to read back. It has a durable source instead --
    ``accepted_agent_run_ids_for_turn`` over the Deliveries accepted against
    the recovered Turn -- which is a second remediation, and one that reaches a
    participant only if it has such a Delivery.

    This probe is a CHARACTERIZATION test in the sense this file's header
    means: it asserts the current, wrong behaviour so the gap is executable.
    It is NOT one of the two PR7R-F1/F2 probes and does not widen this unit's
    scope -- PR7R adds no writer, and nothing here restamps the context.
    """
    from core.processing_indicator import ProcessingIndicatorHandle
    from modules.im import MessageContext
    from modules.agents.opencode.agent import _STEERING_SNAPSHOT_KEY
    from modules.agents.opencode.poll_loop import OpenCodePollLoop

    # The live turn, as the admission layer leaves it: a Turn token and the
    # Runs that Turn accepted, both on ``platform_specific``. Driven through
    # the real handle rather than a dict literal, so the round trip under test
    # is production's own.
    live = ProcessingIndicatorHandle(
        context=MessageContext(
            user_id="u-1",
            channel_id="c-1",
            platform="telegram",
            thread_id="t-1",
            message_id="m-1",
            platform_specific={
                "platform": "telegram",
                "is_dm": True,
                "turn_token": "wb-turn-1",
                "agent_runtime_turn_token": "rt-1",
                "accepted_agent_run_ids": ["run-1", "run-2"],
            },
        ),
        ack_reaction_message_id="m-1",
        ack_reaction_emoji="eyes",
    )
    snapshot = live.to_snapshot()

    # What the snapshot keeps, and what it does not. Asserted as a search over
    # the whole payload rather than two key lookups, so a future field that DID
    # carry the Turn would fail here and force this verdict to be revisited.
    assert snapshot["platform"] == "telegram"
    assert "wb-turn-1" not in repr(snapshot), snapshot
    assert "run-1" not in repr(snapshot), snapshot

    # ...and the steering write puts the same Turn id back into that very dict,
    # keyed for a different consumer. This is the line that makes the loss a
    # discard: the restore is handed the identity and does not use it.
    snapshot[_STEERING_SNAPSHOT_KEY] = {
        "target_session_id": "ses-1",
        "logical_turn_id": "wb-turn-1",
    }
    assert (
        'logical_turn_id = str(platform_payload.get("turn_token") or "").strip()'
        in inspect.getsource(
            __import__(
                "modules.agents.opencode.agent", fromlist=["OpenCodeAgent"]
            ).OpenCodeAgent._process_message
        )
    )

    restored = ProcessingIndicatorHandle.from_snapshot(snapshot).context
    payload = restored.platform_specific or {}
    assert payload.get("platform") == "telegram"
    assert "turn_token" not in payload, payload
    assert "agent_runtime_turn_token" not in payload, payload
    assert "accepted_agent_run_ids" not in payload, payload
    # The steering key is in the input and not in the output either, so the
    # rebuild is not merely ignoring the two identity fields -- it rebuilds
    # ``platform_specific`` from a fixed three-key allowlist.
    assert _STEERING_SNAPSHOT_KEY not in payload, payload

    # And that context is what the restored loop emits with. Walked the same
    # way HFR-183 walks the live loop, so the two paths are compared on one
    # criterion rather than one being read and the other described.
    restored_tree = ast.parse(
        textwrap.dedent(inspect.getsource(OpenCodePollLoop.run_restored_poll_loop))
    )
    emit_contexts = [
        node.args[0]
        for node in ast.walk(restored_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "emit_agent_message"
        and node.args
    ]
    assert emit_contexts, "the restored loop emits no progress at all"
    assert all(
        isinstance(arg, ast.Name) and arg.id == "context" for arg in emit_contexts
    ), ast.unparse(restored_tree)
    # ``context`` there is the rebuilt one, not a live request's -- the two
    # lines that make the walk above mean what it says.
    restored_source = inspect.getsource(OpenCodePollLoop.run_restored_poll_loop)
    assert "restored_request = self._build_restored_ack_request(poll_info)" in restored_source
    assert "context = restored_request.context" in restored_source
    assert "self._agent.controller.processing_indicator.handle_from_snapshot(snapshot)" in (
        inspect.getsource(OpenCodePollLoop._build_restored_handle)
    )
    # Nothing puts a Turn back on it: the whole module never names either field.
    poll_module = inspect.getsource(
        __import__("modules.agents.opencode.poll_loop", fromlist=["OpenCodePollLoop"])
    )
    assert "turn_token" not in poll_module
    assert "logical_turn_id" not in poll_module

    # Production's own record of the same fact, on a different consumer: a
    # restored steer target carries the persisted ``logical_turn_id`` and an
    # explicitly absent context. Read out of the real method rather than
    # restaged, because the claim is about what that method constructs.
    steer_source = inspect.getsource(
        __import__(
            "modules.agents.opencode.agent", fromlist=["OpenCodeAgent"]
        ).OpenCodeAgent.additional_steer_targets
    )
    assert "logical_turn_id=state.logical_turn_id" in steer_source
    assert "context=None" in steer_source

    # Round 18. The Run half's remediation is named as a real durable read
    # rather than described, so the prose above cannot drift from what exists:
    # the participants are keyed on the Turn this rebuild would have recovered,
    # and the source is the accepted Deliveries, which is also the limit -- a
    # participant with no accepted Delivery is not in this answer.
    from core.session_turns import SessionTurnManager
    from storage import message_deliveries

    assert set(
        inspect.signature(SessionTurnManager.accepted_agent_run_ids_for_turn).parameters
    ) == {"self", "turn_id"}
    durable_read = inspect.getsource(message_deliveries.accepted_agent_run_ids_for_turn)
    assert "deliveries_for_turn(conn, turn_id)" in durable_read
    assert 'delivery.get("state") != "accepted"' in durable_read

    # Consistency tie: the matrix must say what this probe just showed. Both
    # opencode cells, because the restore path is reached from either lane --
    # ``poll_info`` is rehydrated from durable state and carries the platform
    # rather than branching on it.
    from tests.run_terminal_truth_evidence import EXACT_TURN_PROGRESS_SIGNALS

    for lane in ("durable_workbench", "direct_im"):
        kind, detail = EXACT_TURN_PROGRESS_SIGNALS[("opencode", lane)]
        assert kind == "defect", (lane, kind)
        assert detail.endswith(
            "test_a_restored_opencode_poll_loop_emits_without_its_turn_identity"
        ), (lane, detail)
    # The other four are untouched by this: claude and codex attribute from the
    # event stream and the notification, neither of which is rebuilt from a
    # processing-indicator snapshot.
    for cell in (("claude", "durable_workbench"), ("codex", "durable_workbench")):
        assert EXACT_TURN_PROGRESS_SIGNALS[cell][0] == "covered", cell
