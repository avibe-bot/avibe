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

    NO CELL USES THIS, and that is itself a result. Both former users argued
    "the owner is backend-independent, so one test covers all three" while
    citing a test that never drove a backend at all -- the argument was doing
    the work the evidence was supposed to do. The vocabulary stays because a
    genuine shared-owner proof is possible; none exists on master today.
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
_HEALTH_WINDOW_AGES_OUT = (
    "tests/test_harness_failure_visibility.py::"
    "test_health_window_ages_out_after_the_time_bound"
)
_HEALTH_DERIVED_NOT_STORED = (
    "tests/test_harness_failure_visibility.py::"
    "test_one_success_does_not_erase_recent_failure_history"
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
    "test_end_tears_down_a_live_claude_turn_and_reclassifies_it_as_intentional"
)
_F2 = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_codex_end_reports_ended_when_the_canonical_stop_never_interrupted"
)
_Q2_SIGNALS = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_which_backends_attribute_a_progress_event_to_an_exact_turn"
)
_Q2_ADMISSION = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_the_shared_admission_layer_stamps_a_turn_token_on_direct_im"
)
_Q2_SERIALIZED = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_one_runtime_key_admits_one_live_turn_at_a_time"
)
_Q2_KEY_SPLIT = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_a_cwd_change_splits_the_gate_key_but_not_the_codex_turn_slot"
)
_Q2_RUNS = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_participating_run_attribution_is_resolved_per_turn_not_per_session"
)
_Q1_RESERVATION = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_no_step_of_the_durable_reservation_path_settles_the_run"
)
_Q3_PROBE = (
    "tests/test_run_terminal_truth_evidence_probes.py::"
    "test_the_accepted_run_batch_records_no_per_run_source_or_deadline"
)


# ----- the matrix ----------------------------------------------------------

#: One cell per (lane, trigger, outcome). ``shared`` applies to every backend
#: unless ``per_backend`` names one; the guard expands both into the full
#: backend x lane x trigger x outcome product.
#:
#: Reading order is the durable chain from the plan: a ``covered``/``shared``
#: cell means that chain is traced end to end by the named test, NOT merely
#: that the outcome is reachable.

#: The Activity output batch is not a backend-independent mechanism. Every test
#: that proves a pending-output or post-delivery local-settlement fact starts a
#: ``SessionActivity`` with ``backend="claude"``, and ``claude_agent.py`` is the
#: only production module that PRODUCES one -- ``modules/agents/service.py``
#: merely owns the registry. So those cells are evidence for Claude and nothing
#: else. They are ``unproven`` rather than ``N/A`` for the other two backends
#: because a pending-output fact is still reachable for them by another route:
#: ``defer_run_terminal`` is called from ``core/message_dispatcher.py`` and
#: ``core/scheduled_tasks.py``, neither of which branches on backend. Whether
#: that route actually fires on a codex/opencode terminal path is the open
#: question, and calling it ``N/A`` would answer it by assertion.
def _activity_is_claude_only(outcome: str) -> tuple[str, str]:
    return unproven(
        f"the cited {outcome} evidence starts a SessionActivity with "
        "backend='claude', and claude_agent.py is the only production producer "
        "of one. Probe: drive this backend's terminal path with a pending "
        "output owed and record which durable fact (an Activity batch, a "
        "``defer_run_terminal`` deferral, or none) the Run actually carries."
    )


_NON_CLAUDE_ACTIVITY: Final = {
    backend: _activity_is_claude_only("pending-output")
    for backend in ("codex", "opencode")
}
_NON_CLAUDE_LOCAL_SETTLEMENT: Final = {
    backend: _activity_is_claude_only("local-settlement-retry")
    for backend in ("codex", "opencode")
}

#: Claude's half of the same two outcomes. Round 3 separated two failures that
#: the old ``covered`` collapsed into one. ``_PENDING_OUTPUT`` really does trace
#: a Run: a real ``TaskExecutionStore`` row, a real ``defer_run_terminal``, a
#: real recovery drain, and a terminal ``succeeded`` at the end -- but it is
#: admitted by ``enqueue_agent_run``, so it says nothing about the trigger the
#: cell is indexed by. The three local-settlement citations are weaker still:
#: every one of them passes a bare ``run_id`` string to a MOCK run store, so no
#: ``agent_runs`` row exists in any of them and the retry they prove belongs to
#: the Activity registry, not to a Run.
_CLAUDE_ACTIVITY_PENDING: Final = unproven(
    "the cited test traces a real Run to a terminal ``succeeded`` through the "
    "recovery drain, but admits it with ``enqueue_agent_run`` -- not one of "
    "the four TRIGGERS -- so the trigger this cell is indexed by is untraced. "
    "Probe: owe an output on a Run admitted through THIS trigger and assert "
    "the deferred terminal still settles once after restart -- "
    + _PENDING_OUTPUT
    + " covers the agent-run admission only."
)
_CLAUDE_ACTIVITY_LOCAL_SETTLEMENT: Final = unproven(
    "the cited retry evidence carries no Harness Run: it hands a bare run-id "
    "string to a stubbed run store, so what it proves is the Activity "
    "registry's requeue policy. Probe: bind a real Run admitted through THIS "
    "trigger to the owed output, fail local settlement once, and assert the "
    "Run settles exactly once without re-delivering."
)

_DURABLE_BY_OUTCOME: Final = {
    # Same defect as ``resultless_termination`` below, in the cell round 2
    # walked straight past: ``_TERMINAL_SUCCESS`` enqueues with
    # ``request_store.enqueue_agent_run(...)``. It is a thorough test of the
    # callback/persistence chain and it settles a real Run -- but through
    # ``vibe agent run``, which is deliberately NOT one of ``TRIGGERS``. A cron,
    # ``at``, CLI-task or watch fire that never attached its run id to the Turn
    # it submits would leave it green.
    "success": {
        "shared": unproven(
            "the cited test admits its Run through ``enqueue_agent_run`` "
            "(``vibe agent run``), which is not one of the four TRIGGERS. "
            "Probe: fire THIS trigger, let the backend return a real terminal "
            "result, and assert the run id reached the Turn context and the "
            "Run row reached ``succeeded`` -- "
            + _TERMINAL_SUCCESS
            + " covers the agent-run admission only."
        ),
    },
    # ``_TERMINAL_FAILURE`` is a STORAGE test and nothing more. It drives all
    # five terminal writers directly -- ``record_run_output``,
    # ``settle_run_terminal`` via a claimed completion, ``settle_deferred_run``,
    # the coalesced completer -- with rows enqueued by ``enqueue_hook_send`` and
    # ``enqueue_agent_run``. The property it pins is real and valuable ("any
    # UPDATE that sets a terminal failure status stamps an owed notice, so a
    # writer added later inherits it"), and it is the wrong property for this
    # cell: no backend runs, no trigger admits, and nothing shows a failing turn
    # ever REACHES one of those writers.
    "failure": {
        "shared": unproven(
            "the cited test invokes the five terminal storage writers directly "
            "with hook-send/agent-run rows; no backend fails and no trigger "
            "admits. Probe: fail a turn on this backend under a Run admitted "
            "through THIS trigger and assert the Run reaches a terminal "
            "failure status with the owed notice attached -- "
            + _TERMINAL_FAILURE
            + " covers the writer property only."
        ),
    },
    # The settlement half IS traced end to end: the cited test drives a real
    # ``SessionTurnManager``, a real ``TaskExecutionStore``, and asserts the Run
    # reaches ``canceled`` with ``interrupt_reason='stopped'``. What it does not
    # cover is the ADMISSION half, and admission is exactly what a trigger owns.
    # Its context carries ``task_trigger_kind='agent_run'`` -- ``vibe agent run``,
    # which is deliberately not one of ``TRIGGERS`` -- so a scheduler or watch
    # fire that never attaches ``accepted_agent_run_ids`` to the Turn context it
    # submits would leave the test green and its own Run unsettled forever.
    "resultless_termination": {
        "shared": unproven(
            "the cited test admits the Run through ``enqueue_agent_run`` with "
            "``task_trigger_kind='agent_run'``, which is not one of the four "
            "TRIGGERS; only the settlement half is traced. Probe: admit a Run "
            "through THIS trigger's real enqueue path, end its turn with no "
            "terminal result, and assert the run id reached the Turn context "
            "and the Run row reached a terminal status -- "
            + _NO_RESULT
            + " covers the agent-run admission only."
        ),
    },
    # Neither cited test observes a Run. ``_STOP_DEFAULT`` is a SOURCE-STRUCTURE
    # test: it parses each backend's ``handle_stop`` and asserts the terminal
    # emit uses ``stop_output_for``, which pins the emit's semantics and nothing
    # about settlement. ``_END_TURN`` replaces the manager with a stub that
    # returns a prebuilt ``{ok: True}`` dict, so it proves the Running-tab
    # service delegates to ``manager.cancel`` and stops there. A broken real
    # ``SessionTurnManager.cancel`` settlement path leaves both green.
    "user_stop": {
        "shared": unproven(
            "no cited test binds a Run to a durable Stop: "
            + _STOP_DEFAULT
            + " reads the backend stop emit out of the AST, and "
            + _END_TURN
            + " stubs the manager out entirely. Probe: End a Workbench row "
            "whose Turn owns a real Harness Run through the real "
            "``SessionTurnManager.cancel`` and assert the Run settles "
            "``canceled`` with the user-stop cause."
        ),
    },
    # The cited test inserts a bare Delivery through ``delivery_store`` and
    # asserts the Delivery requeues and the Turn records ``not_written``. No
    # ``agent_runs`` row is ever created, so it says nothing about what the Run
    # does when the terminal write fails -- which is the whole cell.
    "terminal_persistence_failure": {
        "shared": unproven(
            "the cited test carries no Harness Run: it inserts a bare Delivery "
            "and asserts only the requeue and the ``not_written`` Turn "
            "outcomes. Probe: attach a real Run to that Delivery, fail the "
            "prewrite, and assert whether the Run is retried, left nonterminal, "
            "or swept -- "
            + _PREWRITE
            + " covers the Delivery/Turn half only."
        ),
    },
    "pending_output_delivery": {
        "shared": _CLAUDE_ACTIVITY_PENDING,
        "per_backend": dict(_NON_CLAUDE_ACTIVITY),
    },
    "post_delivery_local_settlement_failure": {
        "shared": _CLAUDE_ACTIVITY_LOCAL_SETTLEMENT,
        "per_backend": dict(_NON_CLAUDE_LOCAL_SETTLEMENT),
    },
}

_IM_BY_OUTCOME: Final = {
    # ``_DEFINITION_CAS`` used to carry this as a shared-owner argument
    # ("definition health is one CAS-guarded write, identical for every
    # backend"). The argument is sound and the citation does not support it:
    # the test creates only an ``at`` definition, its scheduled handler blocks
    # forever so no backend ever returns, and its natural-terminal branch calls
    # ``store.mark_task_result`` and ``request_store.complete`` by hand. It is
    # excellent evidence for Q5's projection question and no evidence at all
    # that a backend terminal success reaches a Run on this lane.
    "success": {
        "shared": unproven(
            "no cited test drives a backend to a terminal success with an "
            "IM-scoped Harness Run bound to it; the projection evidence hand-"
            "calls ``mark_task_result``/``complete`` behind a handler that "
            "never returns. Probe: fire THIS trigger on this backend, let the "
            "receiver emit a real terminal result, and assert the Run row "
            "reaches ``succeeded`` and the definition projection follows it."
        ),
    },
    "failure": {
        # ``_TERMINAL_FAILURE`` is a STORAGE test: it drives the five terminal
        # writers directly and proves the owed-notice stamp is keyed on the
        # property "this UPDATE sets a terminal failure status", so no future
        # writer can escape it. That is a real backend-independent guarantee,
        # and on the durable lane the Run genuinely reaches it as a claimed
        # request completion. On the IM lane nothing connects a backend failure
        # to any of those writers, so citing it here would be borrowing a
        # storage proof to cover a dispatch path.
        "shared": unproven(
            "the owed-notice guarantee is proven at the storage writers, not "
            "from an IM dispatch. Probe: fail a turn on each backend with an "
            "IM-scoped Harness Run bound to it and assert the Run reaches a "
            "terminal failure status with an owed notice attached."
        ),
    },
    "resultless_termination": {
        "shared": unproven(
            "the IM lane has no durable Turn row to settle from, so the Run "
            "depends on the backend's own terminal emit. Probe: drive each "
            "backend's receiver to EOF with an IM-scoped Harness Run bound to "
            "the turn and assert the Run row reaches a terminal status."
        ),
    },
    # These four cells were ``defect`` until round 3, and the classification was
    # not survivable. ``defect`` means "current master is wrong HERE and a
    # characterization test pins what it does" -- here being a Run's terminal
    # truth. But round 2 narrowed both reproducers precisely because neither
    # builds a Run: F1 stops at the skipped canonical stop, F2 at the
    # synthesized ``ended`` payload, and both now SAY so in their docstrings.
    # A cell cannot claim the reproducer characterizes what the reproducer
    # explicitly disclaims. ``defect`` is also excluded from ``UNPROVEN_BUDGET``,
    # so the misclassification was hiding eight cells from the gap count -- the
    # same accounting failure as a wrong ``covered``, one vocabulary word over.
    # The findings stay; they are service findings against
    # ``running_agents.py``, and they are named here so the guard still ties
    # each one to the matrix.
    "user_stop": {
        "shared": unproven(
            "the cited End test asserts which BRANCH End takes, not what any "
            "Run receives; no IM-scoped Run is bound in it. Probe: End an "
            "IM-scoped turn that owns a real Harness Run and assert the "
            "settlement the Run row actually reaches -- "
            + _END_IM
            + " covers the canonical-stop dispatch only."
        ),
        "per_backend": {
            "claude": unproven(
                "PR7R-F1 characterizes the skipped canonical stop and the "
                "intentional-teardown reclassification, and claims nothing "
                "about the Run -- the reproducer builds no ``agent_runs`` row. "
                "Probe: bind an IM-scoped Run to the racing turn and record "
                "the status it settles to, if any -- "
                + _F1
                + " covers End's branch and the misclassification only."
            ),
            "codex": unproven(
                "PR7R-F2 characterizes the ``ok/ended`` payload synthesized "
                "after a FAILED stop, and claims nothing about the Run -- the "
                "reproducer builds only the codex session and turn registries. "
                "Probe: bind an IM-scoped Run to that turn and record whether "
                "anything settles it after the interrupt fails -- "
                + _F2
                + " covers the response payload only."
            ),
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
    "pending_output_delivery": {
        "shared": unproven(
            "the cited evidence is a ``SessionActivityRegistry`` unit built on "
            "a ``mock.Mock`` store with a bare ``run_id`` string; it proves the "
            "requeue-and-retry policy, not that any Run owes an output. Probe: "
            "bind a real IM-scoped Run admitted through THIS trigger to an owed "
            "output and assert the Run stays nonterminal until it lands -- "
            + _LOCAL_SETTLEMENT_EVIDENCE
            + " covers the registry policy only."
        ),
        "per_backend": dict(_NON_CLAUDE_ACTIVITY),
    },
    "post_delivery_local_settlement_failure": {
        "shared": unproven(
            "the cited test patches ``SQLiteBackgroundTaskStore`` with a stub "
            "whose ``record_run_output`` always raises, against a bare "
            "``run_id`` string; no ``agent_runs`` row exists to be settled. "
            "Probe: bind a real IM-scoped Run admitted through THIS trigger, "
            "fail local settlement once, and assert the Run settles exactly "
            "once with no re-delivery -- "
            + _LOCAL_SETTLEMENT_IM
            + " covers the in-process retry policy only."
        ),
        "per_backend": dict(_NON_CLAUDE_LOCAL_SETTLEMENT),
    },
}


def _lane_rows(by_outcome: dict) -> dict:
    return {outcome: dict(by_outcome[outcome]) for outcome in OUTCOMES}


#: Trigger-specific overrides layered on top of the lane defaults. Only the
#: facts a trigger actually owns are respelled: which Run row is enqueued, and
#: what the definition projection retires when the Run goes terminal.
_TRIGGER_OVERRIDES: Final = {
    # The one-shot retirement test is a CANCELLATION test: it cancels the
    # execution and asserts the Run settles ``failed`` with ``last_error``. It
    # proves the projection follows a terminal Run and that only scheduler
    # one-shots are retired -- but never on a successful firing, which is what
    # this cell is about.
    ("durable_workbench", "scheduler_at"): {
        "success": {
            "shared": unproven(
                "the cited test cancels its execution and asserts the Run is "
                "``failed``; no successful ``at`` firing is driven. Probe: fire "
                "a one-shot to a real terminal success and assert both the "
                "Run's terminal status and the definition's retirement -- "
                + _ONE_SHOT_RETIREMENT
                + " covers the canceled path only."
            ),
        },
    },
    # Both watch tests deliberately stop at the ownership boundary, which is
    # what makes them good tests of the boundary and useless as success
    # evidence: the durable one ASSERTS the Run is still ``running`` after the
    # transfer, and the IM one has no Run row at all -- it asserts the steer
    # reached the Delivery owner. A watch's terminal half is a different fact
    # (does the cycle settle its Run and re-arm?) and nothing proves it yet.
    ("durable_workbench", "watch"): {
        "success": {
            "shared": unproven(
                "the cited transfer test asserts the Run is still ``running``; "
                "the terminal half of the watch cycle is unproven. Probe: run "
                "one watch firing through to its terminal result and assert "
                "both the Run's terminal status and the next cycle's re-arm -- "
                + _NONTERMINAL_UNTIL_SETTLED
                + " covers only the pre-terminal half."
            ),
        },
    },
    ("direct_im", "watch"): {
        "success": {
            "shared": unproven(
                "the cited test proves admission only -- the steer reaches the "
                "Delivery owner and no Run row is involved. Probe: bind an "
                "IM-scoped Harness Run to a watch firing and assert it reaches "
                "a terminal success -- "
                + _IM_WATCH_DELIVERY
                + " covers only the steer."
            ),
        },
    },
    # The one direct-IM cell whose citation at least has the right trigger:
    # ``_DEFINITION_CAS`` really does admit through ``enqueue_task_run`` with
    # ``source_kind='scheduler'`` on an ``at`` definition and really does drive
    # ``_drain_requests``. Kept separate from the lane default because what is
    # missing here is narrower -- only the backend terminal result -- and so is
    # the probe that would close it. The cron and manual-CLI cells that used to
    # cite the same test have no such excuse and now fall through to the lane
    # default: an ``at`` definition is not a cron definition, and
    # ``source_kind='scheduler'`` is not ``vibe task run``.
    ("direct_im", "scheduler_at"): {
        "success": {
            "shared": unproven(
                "the cited test admits a real ``at`` Run through "
                "``enqueue_task_run`` and drains it, then hand-settles the "
                "natural-terminal branch with ``mark_task_result`` and "
                "``complete`` behind a handler that never returns -- the "
                "admission is this trigger's, the terminal result is nobody's. "
                "Probe: let the backend emit the terminal result instead of "
                "hand-calling it -- "
                + _DEFINITION_CAS
                + " covers admission and projection only."
            ),
        },
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
#:
#: It went 28 -> 78 under review. Every one of those 50 cells was previously
#: marked ``covered`` by a test that proves a NEARBY fact: a storage-writer
#: property standing in for an IM dispatch, a Claude-only Activity fixture
#: standing in for three backends, an ownership-transfer test that asserts
#: ``running`` standing in for a terminal success. That is the exact failure
#: mode this budget exists to expose, and it took an adversarial read to find
#: it -- which is worth recording, because it means a cell citing a real,
#: passing, relevant-looking test is still not evidence until someone checks
#: that the test's subject is this cell's subject.
#:
#: A second adversarial round moved it 78 -> 117, and every one of the 39 was
#: the same defect again, in cells the first round had walked past: a
#: cancellation test standing in for a successful one-shot firing (3), a bare
#: Delivery with no Run row standing in for the Run's persistence-failure
#: behaviour (12), an AST read of ``handle_stop`` plus a stubbed-out manager
#: standing in for a durable Stop settling a Run (12), and a settlement test
#: admitted through ``vibe agent run`` standing in for all four real triggers
#: (12). Two rounds of this is the finding: the guard checks that a citation
#: RESOLVES, and nothing mechanical can check that it is ABOUT the cell. The
#: number going up twice is the unit working, not the unit failing.
#:
#: A third round took it 117 -> 168, i.e. to every cell, and the reason it went
#: all the way is worth stating plainly rather than softening. Round 2 fixed
#: four cells one at a time. Round 3 asked the same question of the REMAINING
#: cells as a class and got one answer for all of them: **no test on master
#: traces a Run from a trigger's admission through to that Run's terminal
#: settlement.** Every surviving citation proved a segment -- storage writers in
#: isolation (12), a projection whose backend never returns (9), an Activity
#: registry holding a bare run-id string against a mock store (16), a terminal
#: chain admitted by ``vibe agent run`` (10), and two End reproducers filed as
#: ``defect`` for a Run half they explicitly disclaim (8, and ``defect`` is
#: excluded from this budget, so that spelling was hiding them).
#:
#: An all-unproven matrix is an uncomfortable result and it is the correct one.
#: What PR7R was asked to produce is a true statement about current master, and
#: the true statement is that run-terminal-truth has segment coverage, not
#: end-to-end coverage, on every backend/lane/trigger/outcome. The unit's value
#: is not the count: it is 168 named probes, each saying which segment exists
#: and which is missing, plus two findings and five question verdicts. A matrix
#: that had reported 34 green cells would have been more comfortable and wrong.
UNPROVEN_BUDGET: Final = 168


# ----- Q2: exact-Turn progress attribution ---------------------------------

#: The plan singles out one question that must be answered SEPARATELY for every
#: backend and both lanes: which observable event can be attributed to the exact
#: Turn and its participating Runs. A backend/lane with no exact signal blocks a
#: generic inactivity timeout, and session-wide activity is never a substitute.
EXACT_TURN_PROGRESS_SIGNALS: Final = {
    # Claude was read the same wrong way as codex and opencode, one round later
    # again -- three for three. ``mark_session_turn_started`` really is stamped
    # per composite key, and the SDK really has no query/result correlation id,
    # but neither is the event stream. The receiver is long-lived and its
    # captured context carries an OLDER turn's tokens, so before any
    # assistant/tool emit ``_adopt_pending_turn_token`` copies the FIFO-matched
    # pending ``AgentRequest``'s ``turn_token`` (and the runtime turn key/token,
    # and the Run attribution keys) onto that context. The emit therefore names
    # the Turn it belongs to. Avibe supplies the correlation the SDK does not.
    #
    # "Exact" here is a FIFO position, not an id the event carries: the head of
    # ``_pending_requests[composite_key]`` is taken to be the turn producing the
    # event. That is a weaker mechanism than codex's ``turnId`` and it is still
    # an exact-Turn attribution under per-key serialization -- and it is what
    # the remediation has to build on, which is why the distinction is recorded
    # rather than smoothed over.
    ("claude", "durable_workbench"): covered(_Q2_SIGNALS),
    # ...and it needs a ``turn_token`` to copy, which the previous round said it
    # never gets on IM. That was FALSE, and it is the fourth distinct mechanism
    # of the same recurring error: the search was a literal grep for
    # ``"turn_token"``, and the write that matters is CONSTANT-KEYED --
    # ``platform_specific[AGENT_TURN_TOKEN]`` in ``_stamp_runtime_turn``
    # (modules/agents/service.py:1015), called unconditionally by
    # ``AgentService.handle_message`` BEFORE the backend is invoked. That is the
    # shared admission layer every backend and BOTH lanes pass through, so a
    # plain IM request carries a freshly generated per-Turn token by the time
    # the adopt path looks for one. Driven, not read: the probe runs the real
    # ``handle_message`` twice and asserts the two tokens differ.
    ("claude", "direct_im"): covered(_Q2_ADMISSION),
    # Codex is the exception, and the first draft of this table got it wrong by
    # reading only the base-session projection. The app-server's ``item/*`` and
    # ``turn/*`` notifications carry a ``turnId`` in their params, and
    # ``_find_request_for_notification`` resolves the participating Run's
    # request from THAT id through ``get_request_for_turn`` -- a per-turn map,
    # not a per-session one. The exact-Turn signal therefore exists. An earlier
    # round said the signal is then DISCARDED by ``should_emit_progress``, which
    # gates on the single ``_active_turns[base]`` slot. That claim has now been
    # argued three times -- over-claim (round 6), reinstated (round 8),
    # RETRACTED (round 9) -- and only round 9 drove it. The key-space split is
    # real: the gate keys on the composite ``<base>:<working_path>`` and
    # ``_active_turns`` keys on the base alone, so a working-path change does
    # get two requests past the shared gate at once. What rounds 6 and 8 both
    # missed is the code BETWEEN those two facts.
    # ``CodexAgent.handle_message`` holds ``_session_locks[base]`` -- the
    # registry's key space -- across its whole body, and inside it sends
    # ``turn/interrupt`` for any active turn before ``turn/start``. So the two
    # requests are SERIALIZED before they reach the backend, and the muted turn
    # has been interrupted. That is correct filtering. The real consequence of
    # the split is narrower: a cwd change turns "queue and run after" into
    # "interrupt and replace". All of it is driven in
    # ``test_a_cwd_change_splits_the_gate_key_but_not_the_codex_turn_slot``.
    # Round 10 narrows it once more, from the same projection habit: "the turns
    # never overlap" was read off the registry, and the protocol note
    # (docs/plans/codex-app-server-refactor.md, step 2) says an insertion must
    # WAIT for ``turn/completed(interrupted)`` before ``turn/start``. Production
    # does not wait, so turn-1 is still executing on the backend while turn-2 is
    # registered -- the window exists. It is harmless because both of its
    # arrivals are handled, and the same probe now drives them through the real
    # ``CodexEventHandler``: the interrupted turn's late tail is dropped by the
    # named guard in ``_on_item_completed`` while the live turn's lands, and its
    # late ``turn/completed`` is popped and released with nothing emitted rather
    # than mistaken for the new turn's result. Serialization at the lock is a
    # weaker property than non-overlap, and it is the one that is true.
    # The cell is ``covered`` because the question is whether the exact-Turn
    # signal EXISTS, and it does: the ``turnId`` is on the notification and
    # ``_find_request_for_notification`` resolves it before any filtering. One
    # consumer still throws attribution away -- the activity timestamp stamped
    # per SESSION rather than per Turn -- and that one is unretracted.
    ("codex", "durable_workbench"): covered(_Q2_SIGNALS),
    ("codex", "direct_im"): covered(_Q2_SIGNALS),
    # OpenCode was read the same wrong way as codex, one round later, and for
    # the same reason: ``_active_requests[base]`` is a LIVENESS map -- one
    # asyncio task per base session -- and reading a lossy projection told us
    # nothing about the event stream. ``OpenCodePollLoop.run_prompt_poll``
    # receives the exact ``AgentRequest`` and emits every tool call and
    # assistant message with ``request.context``, and ``_process_message``
    # reads that same context's ``turn_token`` as ``logical_turn_id`` before
    # polling starts. So each progress emit carries its own Turn's token.
    ("opencode", "durable_workbench"): covered(_Q2_SIGNALS),
    # ...and the same admission stamp settles this lane too. The previous round
    # named ``SessionTurnManager`` and ``core/services/dispatch.py`` as the only
    # writers; both are Workbench owners, which is what made the lane split look
    # real. ``_stamp_runtime_turn`` is a third writer and it is upstream of
    # both, so ``logical_turn_id`` is NOT empty on IM -- ``_process_message``
    # reads a token that the admission layer put there.
    ("opencode", "direct_im"): covered(_Q2_ADMISSION),
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
        "verdict": "open",
        "answer": (
            "Yes on the durable Workbench lane, driven against real rows: "
            "reservation, ownership transfer, claim, native bind and "
            "materialized acceptance each leave the Run nonterminal, and so "
            "does terminalizing the TURN -- only the explicit participant "
            "settlement moves it. A result-less failure is also not laundered "
            "into an interruption. "
            "Round 10 corrected the basis of that first clause rather than the "
            "clause itself. It used to rest on a scheduler test that stubs "
            "``submit_scheduled`` and merely REPORTS ``queue_persisted`` / "
            "``delivery_owner_transferred``, which establishes the caller's "
            "reaction to a reported reservation and not the reservation; a "
            "store that settled the Run on reserve would have left it green. "
            "The stubbed test is still cited, for the scheduler decision it "
            "does prove. "
            "The direct-IM lane is NOT established. None of the cited tests "
            "touch the IM reservation or the backend-acceptance boundary, so a "
            "premature terminal transition there would leave them green. "
            "Probe: bind an IM-scoped Harness Run to an accepted backend "
            "dispatch and assert the Run is still nonterminal at the "
            "acceptance boundary. The old premature-success claim may only be "
            "closed once that probe exists -- on the durable lane alone this "
            "answer does not carry it."
        ),
        "evidence": (_NONTERMINAL_UNTIL_SETTLED, _NOT_A_SUCCESS, _Q1_RESERVATION),
    },
    "Q2": {
        "question": (
            "Which observable assistant/tool events can be attributed to the "
            "exact Turn and participating Runs, per backend and per lane?"
        ),
        "verdict": "answered",
        "answer": (
            "ALL SIX cells can, and it took four corrections to get here "
            "because the same reading error was made four times in four "
            "different disguises. The first three: each "
            "time, a backend's base-session PROJECTION was read -- "
            "``_active_turns[base]`` for codex, ``_active_requests[base]`` for "
            "opencode, ``session_turn_started[composite_key]`` for claude -- "
            "and a conclusion about the EVENT STREAM was drawn from a lossy "
            "liveness map. All three event streams carry a Turn. Codex's "
            "``item/*`` notifications carry a ``turnId`` and "
            "``_find_request_for_notification`` resolves the participating "
            "Run's request from it. OpenCode's poll loop is handed the exact "
            "``AgentRequest`` and emits with ``request.context``, whose "
            "``turn_token`` ``_process_message`` has already read as "
            "``logical_turn_id``. Claude's long-lived receiver adopts the "
            "FIFO-matched pending request's ``turn_token`` onto the emit "
            "context before any assistant/tool output, so Avibe supplies the "
            "correlation the SDK lacks. The FOURTH error was this table's own "
            "previous verdict: it held claude and opencode ``direct_im`` open "
            "on the ground that ``turn_token`` is stamped only by "
            "``core/session_turns.py`` and the streaming turn dispatch, both "
            "Workbench owners. That was arrived at by grepping the LITERAL "
            "string ``\"turn_token\"``, and the write that matters is "
            "CONSTANT-KEYED: ``platform_specific[AGENT_TURN_TOKEN]``, written "
            "by ``AgentService._stamp_runtime_turn`` "
            "(modules/agents/service.py:1015) which "
            "``AgentService.handle_message`` calls unconditionally, before the "
            "backend runs, on every request from every lane. So the claimed "
            "lane split does not exist: a plain IM request carries a "
            "freshly-generated per-Turn token by the time claude's adopt path "
            "looks for one and by the time opencode reads ``logical_turn_id``. "
            "This is now DRIVEN rather than read -- the probe calls the real "
            "``handle_message`` twice on one runtime key and asserts the two "
            "tokens differ, i.e. per-Turn and not per-session, and that a "
            "pre-existing Workbench token is preserved rather than overwritten. "
            "What remains is NOT attribution but its downstream use, and round "
            "8 made that residual bigger and round 9 made it smaller again. "
            "The FIFTH error was the SHAPE of the answer rather than a fact in "
            "it: this question asks about the exact Turn AND PARTICIPATING "
            "RUNS, every probe asserted Turn tokens, and the verdict was "
            "written as though the conjunction had been checked. It had not "
            "been. It holds, and the mechanism is a derivation: "
            "``_owned_agent_run_ids`` reads ``accepted_agent_run_ids`` off the "
            "emit context, and ``_durable_accepted_agent_run_ids`` looks Runs "
            "up per ``turn_token`` from that SAME payload, so Run attribution "
            "is exactly as exact as the Turn -- driven on all three backends, "
            "including the case that would smear Runs across Turns if claude's "
            "reused receiver context merged rather than replaced. "
            "The SIXTH correction is a retraction of round 8's. Codex does NOT "
            "discard its signal at ``should_emit_progress``. The key spaces do "
            "differ -- the gate keys on ``BaseAgent.runtime_turn_key``, the "
            "composite ``<base>:<working_path>``, and ``_active_turns`` keys on "
            "the base session alone -- so a working-path change does get two "
            "requests past the shared gate. But ``CodexAgent.handle_message`` "
            "holds ``_session_locks[base]``, the REGISTRY's key space, across "
            "its whole body and sends ``turn/interrupt`` for any active turn "
            "before ``turn/start``. The two requests are SERIALIZED before they "
            "reach the backend and the muted turn has been interrupted, which "
            "is correct filtering. Rounds 6, 8 and "
            "9 all argued this claim from the two ENDS of a mechanism without "
            "running the code between them; a claim that flips three times is "
            "one whose subject was never driven end to end. "
            "The SEVENTH correction, round 10, is the same projection habit at "
            "its last hiding place. Round 9 wrote \"there is no window in which "
            "two codex turns are live\", and read that off the REGISTRY's one "
            "slot. Codex's own protocol note "
            "(docs/plans/codex-app-server-refactor.md, insertion step 2) "
            "requires waiting for ``turn/completed`` with interrupted status "
            "before ``turn/start``; production sends interrupt and start with "
            "no wait between them, so turn-1 is still executing on the backend "
            "while turn-2 is registered. The window EXISTS, and it is harmless "
            "for a reason the registry cannot show: both of its arrivals are "
            "handled. The probe now drives them through the real "
            "``CodexEventHandler`` -- the interrupted turn's late "
            "``item/completed`` is dropped by the named guard in "
            "``_on_item_completed`` while the live turn's is kept, and its late "
            "``turn/completed`` is popped, ack-removed and stream-released with "
            "NOTHING emitted, so it is never mistaken for the new turn's "
            "result. Q2 asks whether the exact-Turn signal EXISTS, and the "
            "window does not touch that; what it corrects is the basis, from a "
            "projection's one slot to the handlers that actually meet the "
            "overlap. One byproduct is pinned in the probe: the ack removal for "
            "an interrupted turn happens TWICE, eagerly at "
            "``clear_pending(active_turn)`` and again when the completion "
            "lands. What the key split "
            "really costs is narrower: a cwd change turns queue-and-run-after "
            "into interrupt-and-replace, and "
            "``runtime_turn_keys_for_session_key`` cannot address the replaced "
            "turn. A generic inactivity timeout is therefore UNBLOCKED on the "
            "attribution question -- an exact signal exists on all three "
            "backends and both lanes, and the Runs come with it -- and "
            "remediation is ONE item: a per-Turn activity timestamp instead of "
            "a per-session one. Note claude's attribution "
            "is a FIFO POSITION rather than an id the event carries: exact "
            "under per-key serialization, weaker than codex's ``turnId``, and "
            "the remediation has to build on it as it is."
        ),
        "evidence": (
            _Q2_SIGNALS,
            _Q2_ADMISSION,
            _Q2_SERIALIZED,
            _Q2_KEY_SPLIT,
            _Q2_RUNS,
        ),
    },
    "Q3": {
        "question": (
            "Can scheduler and manual Runs with different source semantics or "
            "effective deadlines enter the same Turn?"
        ),
        "verdict": "open",
        "answer": (
            "Partly. What IS established, and NARROWED in round 3 to what the "
            "probe actually reaches: once several Runs are attributed to one "
            "Turn, the Turn's own CONTEXT PROJECTION records only a flat "
            "``accepted_agent_run_ids`` list plus one Turn-level "
            "``source_kind`` stamped by whichever participant arrived first -- "
            "no per-Run source, no per-Run deadline. An earlier draft went on "
            "to conclude that a per-Run timeout policy is therefore "
            "unspecifiable, and that was an over-claim: each accepted id is the "
            "primary key of an ``agent_runs`` row carrying ``source_kind``, "
            "``source_actor`` and ``definition_id``, and the definition it "
            "points at carries the timeout fields. The inputs exist; they are "
            "one join away and absent from the projection. What that makes "
            "unproven is a different and narrower thing -- whether the "
            "cancellation site can perform that join, and whether the values "
            "are stable enough to decide on -- and neither is tested. Probe: "
            "drive a Turn-level cancellation over a mixed batch and record "
            "whether it reads ``agent_runs`` at all. What is also NOT "
            "established is the admission half: whether a scheduler Run and "
            "a manual CLI Run actually coalesce. The probe drives the "
            "accumulator, and the accumulator is downstream of the decision -- "
            "it appends ids already attributed to its Turn. The owner that "
            "decides is ``SessionTurnManager._hydrate_delivery_batch_context``, "
            "which folds a Delivery batch into ONE context and calls "
            "``_append_accepted_agent_run_ids`` once. Probe: enqueue a cron "
            "Delivery and a ``vibe task run`` Delivery on one Session and drive "
            "that batch hydration, asserting whether both run ids land in a "
            "single Turn context."
        ),
        "evidence": (_Q3_PROBE, _ONE_SHOT_RETIREMENT),
    },
    "Q4": {
        "question": (
            "Which evidence exists before the Turn becomes terminal, proving "
            "natural completion has started?"
        ),
        "verdict": "open",
        "answer": (
            "Two facts are established, FOR CLAUDE ONLY, and a third and fourth "
            "are named but unproven. Established: a durable pending-output fact "
            "(an Activity output batch that survives restart and settles the Run "
            "once) and the ``activity_local_settlement_only`` marker that "
            "survives a failed local settlement without re-delivering. Both rest "
            "on the Activity output batch, and ``modules/agents/claude_agent.py`` "
            "is its only production producer -- every cited test starts its "
            "activity with ``backend='claude'``. NOT established, and previously "
            "asserted here without evidence: the terminal-result latch "
            "``SessionTurnManager.on_terminal_result`` writes, and the accepted "
            "Message receipt. No cited node invokes ``on_terminal_result`` or "
            "reads ``_avibe_terminal_result_latch``; removing the latch would "
            "leave every test named here green. The latch is also in-process "
            "context state, not a durable row -- it is popped by "
            "``on_terminal_delivery_complete``, which is where the durable "
            "commit happens -- so calling it a durable pre-terminal fact was "
            "wrong on two counts. A codex or opencode Turn has no proven "
            "pre-terminal evidence at all, which is the stronger form of the "
            "same gap as Q2: an inactivity decision on those backends would have "
            "nothing to outrank it. Probes: (1) the per-backend one named in the "
            "``pending_output_delivery`` cells; (2) latch a terminal result on a "
            "Turn that owns a Run, then drive an inactivity decision against it "
            "and assert the latch outranks it. "
            "One apparent contradiction is deliberate and worth spelling out: "
            "round 3 marked every ``pending_output_delivery`` and "
            "``post_delivery_local_settlement_failure`` matrix cell "
            "``unproven`` while this answer still calls two of those facts "
            "established. Both are true because they are different questions. "
            "Q4 asks what pre-terminal evidence a TURN carries, and "
            "``_PENDING_OUTPUT`` answers it on a real Run. The matrix cell asks "
            "whether THIS TRIGGER's Run reaches that evidence, and the same "
            "test cannot answer that, because it admits through "
            "``enqueue_agent_run``. A test may settle one and not the other."
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
        "verdict": "open",
        "answer": (
            "SPLIT, and the previous ``answered`` verdict was carried by the "
            "half that holds. ``health``/``consecutive_failures``/"
            "``recent_failures`` ARE monotonic projections: they are derived "
            "per read by ``SQLiteBackgroundTaskStore._classify_health`` over "
            "the bounded verdict window ``_health_rows`` collects, so they are "
            "not stored counters, cannot drift, a failure that ages out of the "
            "window stops counting on its own, and a success downgrades "
            "``failing`` to ``degraded`` instead of erasing the history. No "
            "health cursor is needed for those three, and dispatch or Delivery "
            "acceptance never touches them. "
            "``last_run_at``/``last_error`` are NOT the same thing, and the "
            "earlier claim that they are 'written in the same CAS-guarded "
            "terminal transition that settles the Run' is false. They are a "
            "definition-level stamp written by ``store.mark_task_result`` "
            "inside ``_execute_task`` (core/scheduled_tasks.py:~8394), which "
            "COMMITS and returns; the Run's own terminal CAS is "
            "``request_store.complete`` in ``_execute_claimed_request``'s "
            "``finally`` (~7815-7829), a SECOND write that happens afterwards. "
            "Two writes, not one transition, with a window between them. "
            "HFR-261 reconciles the case where the terminal CAS REFUSES the "
            "transition -- nothing reconciles process loss in the gap, which "
            "leaves a definition advertising a ``last_run_at``/``last_error`` "
            "for a Run that never reached a terminal status. So these two "
            "fields are a projection of an ATTEMPT, not of settled Run "
            "history, and unlike the health trio they can drift because they "
            "are stored rather than derived. Probe: kill between "
            "``mark_task_result`` and ``request_store.complete`` and assert "
            "whether any reconciliation restores agreement."
        ),
        "evidence": (
            _HEALTH_WINDOW_AGES_OUT,
            _HEALTH_DERIVED_NOT_STORED,
            _HEALTH_PROJECTION,
            _DEFINITION_CAS,
        ),
    },
}


# ----- findings ------------------------------------------------------------

#: Defects this evidence unit reproduces on current master. Each names the
#: durable owner that must fix it; PR7R itself fixes nothing.
PR7R_FINDINGS: Final = {
    "PR7R-F1": {
        "title": (
            "End tears down a live Claude turn without the canonical stop when "
            "the live-state probe cannot see it yet"
        ),
        "owner": "core/services/running_agents.py::_resolve_live_state",
        "detail": (
            "``handle_message`` calls ``mark_session_active`` only AFTER "
            "``get_or_create_claude_session`` returns, so a turn that is "
            "accepted while the CLI is still starting is absent from "
            "``claude_active_sessions``. ``_resolve_live_state`` reports "
            "``idle`` for it, and End takes the idle branch into "
            "``_end_claude``. Two things follow, and only the second was "
            "originally stated correctly. (1) ``handle_stop`` -- the ONLY path "
            "that emits ``stopped`` -> ``canceled`` -- never runs, so a user "
            "Stop cannot produce the status Invariant 2 requires for one. "
            "(2) ``cleanup_session`` marks the key an INTENTIONAL teardown, so "
            "when the still-live turn dies of the resulting SIGTERM/SIGKILL, "
            "``claude_teardown_is_intentional`` classifies it as service "
            "cleanup rather than a fault -- the user's Stop is erased from the "
            "record twice over. WHICH terminal status the IM Run then receives "
            "is deliberately NOT claimed here: ``SETTLED_BY_BACKEND_REFRESH`` "
            "is emitted by ``SessionTurnManager.release_for_backend_refresh``, "
            "a Workbench-lane path, and no evidence connects it to an IM Run. "
            "The IM lane's ``user_stop`` and ``resultless_termination`` cells "
            "carry the probe that would settle it."
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
            "the turn: the un-interrupted turn's mappings are cleared and no "
            "caller can tell that the interrupt never happened. LOCATED "
            "PRECISELY IN ROUND 4: the signal is not missing from the system. "
            "``_end_codex`` computes and returns ``interrupted``; the "
            "failed-stop branch of ``end_running_agent`` writes a fresh literal "
            "and copies only ``process_killed`` out of the teardown result, so "
            "``interrupted`` is produced and then dropped by the frame that "
            "reports. The reproducer drives a LIVE transport twice -- interrupt "
            "raising and interrupt succeeding -- and gets byte-identical "
            "payloads; an earlier draft left ``_transports`` empty, which "
            "staged the deliberate stale-row case instead of the defect. The "
            "teardown may stay; the result must carry the failed stop. WHAT THE RUN "
            "RECEIVES IS DELIBERATELY NOT CLAIMED HERE -- the reproducer builds "
            "only the codex session and turn registries, so no Run row exists "
            "for it to observe. An earlier draft asserted the Run 'is never "
            "settled'; that was inference from the missing interrupt, not "
            "evidence. The IM lane's ``user_stop`` cells carry the probe that "
            "would settle it."
        ),
        "reproducer": _F2,
    },
}
