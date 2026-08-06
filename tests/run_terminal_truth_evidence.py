"""PR7R evidence metadata: current-master run terminal truth.

``docs/plans/harness-run-reliability.md`` §7 (PR7R) requires ONE executable
matrix over backends x execution lanes x Harness triggers x terminal outcomes,
plus consuming-test answers to five enumerated questions. This module is that
matrix. It is evidence metadata only -- no lifecycle writer imports it, and
PR7R adds no status, timeout field, terminal writer, health cursor, or
cancellation path.

It deliberately mirrors ``core.run_settlement.TEARDOWN_SETTLEMENT_MATRIX``:
explicit cells, a small proof vocabulary, and a parametrized guard
(``tests/test_run_terminal_truth_matrix.py``) that fails when a dimension grows
without every new cell naming a consuming test or a precise ownership reason.
It lives under ``tests/`` rather than in ``core/`` because PR7R's boundary is
"tests and the plan only"; ``TEARDOWN_SETTLEMENT_MATRIX`` predates that
boundary and stays where it is.

Two things separate this matrix from its predecessor.

**Factoring.** The full product is 3 x 2 x 4 x 7 = 168 cells, but the durable
owner chain a Run walks -- Run row, request / Delivery reservation, Turn start,
terminal-result latch, Turn terminal evidence, Activity output batch, accepted
Message receipt, Run settlement, definition health projection -- is chosen by
the LANE, not by the backend. So each cell is written once per
(lane, trigger, outcome) with a ``shared`` proof that must justify why the
backend cannot vary it, and a ``per_backend`` override wherever it demonstrably
can. The guard still expands to all 168 cells and parametrizes over them, so a
missing backend is a failure, not an omission nobody notices.

**An honest gap vocabulary.** ``TEARDOWN_SETTLEMENT_MATRIX`` describes finished
work, so every cell there is ``covered`` or ``N/A``. PR7R describes work in
progress: its whole job is to say where current master is NOT proven. A cell
with no evidence is ``unproven`` with the exact probe that would settle it, and
``UNPROVEN_BUDGET`` pins how many such cells exist. The count can only fall by
someone writing the probe and editing the number in the same commit -- silently
dropping coverage fails the guard. Reading a high budget as "PR7R is
incomplete" is correct; that is what it is for.
"""

from typing import Final

# ----- dimensions ----------------------------------------------------------

#: Every agent backend that can own a Harness Run's Turn.
BACKENDS: Final = ("claude", "codex", "opencode")

#: The two execution lanes from the plan. ``direct_im`` is a Run whose Session
#: is IM-scoped: the agent answers through the IM client and the Run settles
#: from the backend's terminal emit. ``durable_workbench`` is a Run owned by
#: ``SessionTurnManager`` with a durable Delivery and a persisted Message.
LANES: Final = ("direct_im", "durable_workbench")

#: The four ways a Harness Run enters the system. A plain user message is NOT
#: here on purpose: it has no Run row, so it has no terminal Run truth to
#: trace. ``manual_cli`` is ``vibe task run`` / the equivalent API call.
TRIGGERS: Final = ("scheduler_cron", "scheduler_at", "manual_cli", "watch")

#: The seven terminal outcomes the plan enumerates.
OUTCOMES: Final = (
    "success",
    "failure",
    "resultless_termination",
    "user_stop",
    "terminal_persistence_failure",
    "pending_output_delivery",
    "post_delivery_local_settlement_failure",
)


# ----- proof vocabulary ----------------------------------------------------


def covered(test_node: str) -> tuple[str, str]:
    """A consuming test proves this cell's durable facts for this backend."""

    return "covered", test_node


def shared_owner(reason_and_node: str) -> tuple[str, str]:
    """One backend-independent owner decides the cell; the test proves it.

    The string must name BOTH why the backend cannot vary the outcome and the
    consuming test, separated by ``" -- "``. The guard splits on that marker
    and resolves the right-hand side as a node id, so a ``shared`` claim
    without a real test is as loud as a missing cell.
    """

    return "shared", reason_and_node


def not_applicable(reason: str) -> tuple[str, str]:
    """This (lane, trigger, outcome) combination cannot occur on master."""

    return "N/A", reason


def defect(finding_and_node: str) -> tuple[str, str]:
    """Current master is wrong here; a characterization test pins what it does.

    Same ``" -- "`` shape as :func:`shared_owner`: finding id on the left, the
    reproducing node id on the right. These tests assert CURRENT behavior, so
    the implementation PR that fixes the finding must flip them.
    """

    return "defect", finding_and_node


def unproven(reason: str) -> tuple[str, str]:
    """No evidence yet. The reason must name the probe that would settle it."""

    return "unproven", reason


# ----- consuming tests -----------------------------------------------------

_TERMINAL_SUCCESS = (
    "tests/test_scheduled_tasks.py::"
    "test_base_agent_terminal_markdown_example_persists_complete_run_and_callback"
)
_TERMINAL_FAILURE = (
    "tests/test_harness_failure_visibility.py::"
    "test_every_terminal_failure_transition_stamps_an_owed_notice"
)
_NO_RESULT = (
    "tests/test_scheduled_tasks.py::"
    "test_workbench_turn_settles_its_agent_run_when_no_result_arrives"
)
_STOP_DEFAULT = (
    "tests/test_agent_stop_settlement.py::AgentStopSettlementTests::"
    "test_no_backend_stop_uses_the_terminal_turn_default"
)
_END_TURN = (
    "tests/test_running_agents_service.py::test_end_active_workbench_turn_settles_via_manager"
)
_END_IM = (
    "tests/test_running_agents_service.py::test_end_active_im_turn_uses_canonical_stop_path"
)
_PREWRITE = (
    "tests/test_session_delivery_fsm.py::"
    "test_definite_handler_prewrite_exception_requeues_through_terminal_boundary"
)
_PENDING_OUTPUT = (
    "tests/test_scheduled_tasks.py::"
    "test_restart_delivers_persisted_activity_summary_and_settles_run_once"
)
_LOCAL_SETTLEMENT_DURABLE = (
    "tests/test_message_dispatcher_scheduled.py::MessageDispatcherScheduledTests::"
    "test_recovered_durable_message_retries_only_local_settlement_after_restart"
)
_LOCAL_SETTLEMENT_IM = (
    "tests/test_claude_agent_initiated_turn.py::ReceiverOpensAgentInitiatedTurnTests::"
    "test_nondurable_delivery_retries_only_local_settlement_in_process"
)
_LOCAL_SETTLEMENT_EVIDENCE = (
    "tests/test_session_activities.py::test_terminal_evidence_requeues_only_for_local_settlement_retry"
)
_NONTERMINAL_UNTIL_SETTLED = (
    "tests/test_scheduled_tasks.py::"
    "test_claimed_watch_stays_nonterminal_after_delivery_ownership_transfer"
)
_DEFINITION_CAS = (
    "tests/test_scheduled_tasks.py::"
    "test_task_definition_projection_follows_the_exact_terminal_cas_winner"
)
_ONE_SHOT_RETIREMENT = (
    "tests/test_scheduled_tasks.py::"
    "test_canceled_task_execution_projects_every_result_and_only_retires_scheduler_one_shots"
)
_IM_WATCH_DELIVERY = (
    "tests/test_scheduled_tasks.py::test_execute_request_im_watch_steers_through_delivery_owner"
)
_HEALTH_PROJECTION = (
    "tests/test_harness_health_projection.py::"
    "test_the_cli_is_a_projection_consumer_not_a_second_health_read_model"
)
_NOT_A_SUCCESS = (
    "tests/test_harness_failure_visibility.py::"
    "test_an_ordinary_result_less_failure_is_not_treated_as_an_interruption"
)

# PR7R's own reproducers.
_F1 = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_end_live_state_race_settles_an_unmarked_claude_turn_as_a_refresh"
)
_F2 = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_codex_end_reports_ended_when_the_canonical_stop_never_interrupted"
)
_Q2_BLOCKER = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_no_backend_exposes_a_per_turn_progress_signal_on_either_lane"
)
_Q3_PROBE = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_the_turn_merge_key_admits_runs_with_different_source_semantics"
)


# ----- the matrix ----------------------------------------------------------

#: One cell per (lane, trigger, outcome). ``shared`` applies to every backend
#: unless ``per_backend`` names one; the guard expands both into the full
#: backend x lane x trigger x outcome product.
#:
#: Reading order is the durable chain from the plan: a ``covered``/``shared``
#: cell means that chain is traced end to end by the named test, NOT merely
#: that the outcome is reachable.

_SHARED_TERMINAL_LATCH = (
    "the terminal-result latch and Run settlement are owned by "
    "SessionTurnManager / the run settlement writers, which never branch on "
    "backend -- " + _NONTERMINAL_UNTIL_SETTLED
)
_SHARED_DEFINITION_PROJECTION = (
    "definition health/last_run_at/last_error is one CAS-guarded write in the "
    "same terminal transition, identical for every backend -- " + _DEFINITION_CAS
)

_DURABLE_BY_OUTCOME: Final = {
    "success": {"shared": covered(_TERMINAL_SUCCESS)},
    "failure": {"shared": covered(_TERMINAL_FAILURE)},
    "resultless_termination": {"shared": covered(_NO_RESULT)},
    "user_stop": {
        "shared": covered(_STOP_DEFAULT),
        "per_backend": {
            # End on a Workbench row routes through SessionTurnManager.cancel
            # for every backend; the per-backend runtime teardown after it does
            # not choose the settlement.
            "claude": covered(_END_TURN),
            "codex": covered(_END_TURN),
            "opencode": covered(_END_TURN),
        },
    },
    "terminal_persistence_failure": {"shared": covered(_PREWRITE)},
    "pending_output_delivery": {"shared": covered(_PENDING_OUTPUT)},
    "post_delivery_local_settlement_failure": {
        "shared": covered(_LOCAL_SETTLEMENT_DURABLE)
    },
}

_IM_BY_OUTCOME: Final = {
    "success": {"shared": shared_owner(_SHARED_TERMINAL_LATCH)},
    "failure": {"shared": covered(_TERMINAL_FAILURE)},
    "resultless_termination": {
        "shared": unproven(
            "the IM lane has no durable Turn row to settle from, so the Run "
            "depends on the backend's own terminal emit. Probe: drive each "
            "backend's receiver to EOF with an IM-scoped Harness Run bound to "
            "the turn and assert the Run row reaches a terminal status."
        ),
    },
    "user_stop": {
        "shared": covered(_END_IM),
        "per_backend": {
            "claude": defect("PR7R-F1 -- " + _F1),
            "codex": defect("PR7R-F2 -- " + _F2),
            "opencode": unproven(
                "``_resolve_live_state`` reads ``agent._active_requests[base]`` "
                "and calls a missing entry ``idle``. Probe: End an opencode row "
                "whose polling task has not been registered yet and record the "
                "settlement the Run receives."
            ),
        },
    },
    "terminal_persistence_failure": {
        "shared": unproven(
            "the IM lane's terminal write is the Run settlement itself, with no "
            "Delivery prewrite boundary in front of it. Probe: fail "
            "``settle_run_terminal`` under an IM-scoped Harness Run and record "
            "whether the Run is retried, swept, or left nonterminal."
        ),
    },
    "pending_output_delivery": {"shared": covered(_LOCAL_SETTLEMENT_EVIDENCE)},
    "post_delivery_local_settlement_failure": {
        "shared": covered(_LOCAL_SETTLEMENT_IM)
    },
}


def _lane_rows(by_outcome: dict) -> dict:
    return {outcome: dict(by_outcome[outcome]) for outcome in OUTCOMES}


#: Trigger-specific overrides layered on top of the lane defaults. Only the
#: facts a trigger actually owns are respelled: which Run row is enqueued, and
#: what the definition projection retires when the Run goes terminal.
_TRIGGER_OVERRIDES: Final = {
    ("durable_workbench", "scheduler_at"): {
        "success": {"shared": covered(_ONE_SHOT_RETIREMENT)},
    },
    ("durable_workbench", "watch"): {
        "success": {"shared": covered(_NONTERMINAL_UNTIL_SETTLED)},
    },
    ("direct_im", "watch"): {
        "success": {"shared": covered(_IM_WATCH_DELIVERY)},
    },
    ("direct_im", "scheduler_at"): {
        "success": {"shared": shared_owner(_SHARED_DEFINITION_PROJECTION)},
    },
    ("direct_im", "scheduler_cron"): {
        "success": {"shared": shared_owner(_SHARED_DEFINITION_PROJECTION)},
    },
    ("direct_im", "manual_cli"): {
        "success": {"shared": shared_owner(_SHARED_DEFINITION_PROJECTION)},
    },
}


def _build_matrix() -> dict:
    matrix: dict = {}
    for lane in LANES:
        base = _DURABLE_BY_OUTCOME if lane == "durable_workbench" else _IM_BY_OUTCOME
        for trigger in TRIGGERS:
            rows = _lane_rows(base)
            rows.update(_TRIGGER_OVERRIDES.get((lane, trigger), {}))
            matrix[(lane, trigger)] = rows
    return matrix


RUN_TERMINAL_TRUTH_MATRIX: Final = _build_matrix()

#: How many of the 168 expanded cells currently have no evidence. This is the
#: honest gap in PR7R, not a tolerance: it may only be lowered by a commit that
#: adds the probe named in the cell.
UNPROVEN_BUDGET: Final = 28


# ----- Q2: exact-Turn progress attribution ---------------------------------

#: The plan singles out one question that must be answered SEPARATELY for every
#: backend and both lanes: which observable event can be attributed to the exact
#: Turn and its participating Runs. A backend/lane with no exact signal blocks a
#: generic inactivity timeout, and session-wide activity is never a substitute.
EXACT_TURN_PROGRESS_SIGNALS: Final = {
    ("claude", "durable_workbench"): unproven(
        "``mark_session_turn_started`` stamps ``session_turn_started`` and "
        "``session_last_activity`` per COMPOSITE KEY, not per Turn, and the SDK "
        "exposes no query/result correlation id (see the comment above "
        "``_wait_for_activity_output``). Probe: run two overlapping Runs in one "
        "composite key and assert whether any emitted event names the exact Turn."
    ),
    ("claude", "direct_im"): unproven(
        "same session-wide stamp as the Workbench lane; the IM lane additionally "
        "has no durable Turn row to attribute against. Same probe."
    ),
    ("codex", "durable_workbench"): unproven(
        "``_turn_registry.get_active_turn(base)`` holds ONE turn id per base "
        "session, which is an exact signal only while a single Turn is active. "
        "Probe: assert whether a second accepted Run in the same base session "
        "can be distinguished from the first by any registry read."
    ),
    ("codex", "direct_im"): unproven("same registry as above -- same probe."),
    ("opencode", "durable_workbench"): unproven(
        "``_active_requests[base]`` is one asyncio task per base session with no "
        "per-Turn identity. Probe: assert whether the poller reports progress "
        "bound to a Turn or only to the session."
    ),
    ("opencode", "direct_im"): unproven("same task map as above -- same probe."),
}


# ----- the five questions --------------------------------------------------

#: Verdicts must be one of ``answered`` (the evidence settles it),
#: ``open`` (the probe exists and disagrees or is partial), or ``blocked``
#: (no evidence yet). Every entry names at least one consuming test.
PR7R_QUESTIONS: Final = {
    "Q1": {
        "question": (
            "Does the Run remain nonterminal until its actual terminal "
            "Turn/result or Activity output batch settles it?"
        ),
        "verdict": "answered",
        "answer": (
            "Yes on both lanes. Delivery reservation, ownership transfer and "
            "backend dispatch acceptance all leave the Run nonterminal; only a "
            "terminal Turn/result, an Activity output batch, or an explicit "
            "settlement writes a terminal status. The old premature-success "
            "claim should be CLOSED with this regression evidence rather than "
            "answered with another terminal writer."
        ),
        "evidence": (_NONTERMINAL_UNTIL_SETTLED, _NOT_A_SUCCESS),
    },
    "Q2": {
        "question": (
            "Which observable assistant/tool events can be attributed to the "
            "exact Turn and participating Runs, per backend and per lane?"
        ),
        "verdict": "blocked",
        "answer": (
            "No backend exposes a per-Turn progress signal on either lane; all "
            "three stamp progress per session/base-session. See "
            "``EXACT_TURN_PROGRESS_SIGNALS`` for the six cells and their probes. "
            "Until at least one exact signal exists for every backend and lane, "
            "the plan's own rule forbids a generic inactivity timeout -- so no "
            "implementation PR may be opened for one."
        ),
        "evidence": (_Q2_BLOCKER,),
    },
    "Q3": {
        "question": (
            "Can scheduler and manual Runs with different source semantics or "
            "effective deadlines enter the same Turn?"
        ),
        "verdict": "open",
        "answer": (
            "The merge key is the session anchor, not the Run source, so Runs "
            "with different ``source_kind`` can coalesce into one Turn. "
            "Cancellation is Turn-level, so no per-Run timeout policy may be "
            "specified until this cardinality is made explicit -- see the probe "
            "for the exact key and the recorded batch."
        ),
        "evidence": (_Q3_PROBE, _ONE_SHOT_RETIREMENT),
    },
    "Q4": {
        "question": (
            "Which evidence exists before the Turn becomes terminal, proving "
            "natural completion has started?"
        ),
        "verdict": "answered",
        "answer": (
            "Four durable facts, all of which must outrank a later inactivity "
            "decision: the terminal-result latch, a durable pending-output fact, "
            "an accepted Message receipt, and the "
            "``activity_local_settlement_only`` marker that survives a failed "
            "local settlement without re-delivering."
        ),
        "evidence": (
            _PENDING_OUTPUT,
            _LOCAL_SETTLEMENT_EVIDENCE,
            _LOCAL_SETTLEMENT_DURABLE,
        ),
    },
    "Q5": {
        "question": (
            "Are health, consecutive_failures, recent_failures, last_run_at and "
            "last_error already monotonic projections of bounded terminal Run "
            "history?"
        ),
        "verdict": "answered",
        "answer": (
            "Yes. ``health``/``consecutive_failures``/``recent_failures`` are "
            "derived per read by ``_health_from_verdicts`` over a bounded window "
            "of terminal Run verdicts -- they are not stored counters, so they "
            "cannot drift. ``last_run_at``/``last_error`` are written in the same "
            "CAS-guarded terminal transition that settles the Run. No health "
            "cursor is needed, and dispatch or Delivery acceptance never touches "
            "any of them."
        ),
        "evidence": (_HEALTH_PROJECTION, _DEFINITION_CAS),
    },
}


# ----- findings ------------------------------------------------------------

#: Defects this evidence unit reproduces on current master. Each names the
#: durable owner that must fix it; PR7R itself fixes nothing.
PR7R_FINDINGS: Final = {
    "PR7R-F1": {
        "title": (
            "End settles a Claude turn as a backend refresh when the live-state "
            "probe cannot see it yet"
        ),
        "owner": "core/services/running_agents.py::_resolve_live_state",
        "detail": (
            "``handle_message`` calls ``mark_session_active`` only AFTER "
            "``get_or_create_claude_session`` returns, so a turn that is "
            "accepted while the CLI is still starting is absent from "
            "``claude_active_sessions``. ``_resolve_live_state`` reports "
            "``idle`` for it, End takes the idle branch into ``_end_claude``, "
            "and the intentional-teardown path settles the Run as "
            "``backend_refresh`` -> ``failed`` instead of ``stopped`` -> "
            "``canceled``. Invariant 2 says a user Stop is ``canceled``."
        ),
        "reproducer": _F1,
    },
    "PR7R-F2": {
        "title": (
            "Codex End reports ok/ended when the canonical stop never "
            "interrupted the turn"
        ),
        "owner": "core/services/running_agents.py::end_running_agent",
        "detail": (
            "On the active branch, a codex teardown that succeeds after a FAILED "
            "``_stop_active_agent`` synthesizes ``{ok: True, action: 'ended'}``. "
            "Clearing the stale row is deliberate (see "
            "``test_end_active_codex_clears_stale_row_even_when_stop_fails``), "
            "but the response is byte-identical to a stop that really settled "
            "the turn: the un-interrupted turn's mappings are cleared, its Run "
            "is never settled, and no caller can tell. The teardown may stay; "
            "the result must carry the failed stop."
        ),
        "reproducer": _F2,
    },
}
