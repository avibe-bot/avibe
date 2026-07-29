"""PR6 — make harness failure visible.

Scenarios HFR-060 … HFR-090 (``tests/scenarios/harness_failure_recovery``).

Three groups, in the order the plan builds them:

1. the terminal-writer set — every UPDATE that transitions ``agent_runs.status``
   to a terminal value is guarded and stamps an owed failure notice;
2. the owed-notice drain — receipt/ack/backoff/dead-letter, streak suppression,
   settled-prefix deferral;
3. derived health — the counters the CLI/UI badge reads, and the row classes that
   must not reach them.
"""

from __future__ import annotations

import asyncio
import pytest
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scheduled_tasks import TaskExecutionStore
from storage.background import (
    NOTICE_SENT,
    OWED_FAILURE_NOTICE_KEY,
    OWED_NOTICE_INDEX,
    RUN_INTERRUPTION_REASONS,
    SQLiteBackgroundTaskStore,
)


def _store(tmp_path: Path) -> tuple[SQLiteBackgroundTaskStore, TaskExecutionStore]:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    requests = TaskExecutionStore(tmp_path / "task_requests")
    requests._sqlite = sqlite
    return sqlite, requests


_EPOCH = "2026-07-01T00:00:00+00:00"


def _task(sqlite: SQLiteBackgroundTaskStore, definition_id: str, **overrides) -> None:
    payload = {
        "id": definition_id,
        "name": definition_id,
        "prompt": "go",
        "schedule_type": "cron",
        "cron": "0 * * * *",
        "enabled": True,
        "created_at": _EPOCH,
        "updated_at": _EPOCH,
    }
    payload.update(overrides)
    sqlite.upsert_scheduled_task(payload)


def _watch(sqlite: SQLiteBackgroundTaskStore, definition_id: str, **overrides) -> None:
    payload = {
        "id": definition_id,
        "name": definition_id,
        "prompt": "check",
        "command": ["true"],
        "enabled": True,
        "created_at": _EPOCH,
        "updated_at": _EPOCH,
    }
    payload.update(overrides)
    sqlite.upsert_watch(payload)


# --- group 1: the terminal writer set -------------------------------------


def test_complete_does_not_clobber_an_already_terminal_run(tmp_path: Path) -> None:
    """HFR-060 — ``complete()`` must not rewrite a status another actor settled.

    ``TaskExecutionStore.complete`` terminalizes through the UNGUARDED
    ``update_run_status``, whose UPDATE has no status predicate. A run whose real
    terminal result already landed ``succeeded`` is rewritten to ``failed`` when
    the claimed-request layer completes with an error, so the user is shown a
    failure for a run that worked.
    """

    sqlite, requests = _store(tmp_path)
    request = requests.enqueue_hook_send(session_key="slack::channel::C1", prompt="hi")
    claimed = requests.claim(request.id)
    assert claimed is not None

    # The backend's own terminal result lands first, through a guarded writer.
    sqlite.record_run_output(
        request.id,
        output_id="out-1",
        text="the report",
        terminal_status="succeeded",
    )
    assert sqlite.get_run(request.id)["status"] == "succeeded"

    # The claimed-request layer then completes the same row with an error.
    requests.complete(claimed, ok=False, error="backend blew up")

    saved = sqlite.get_run(request.id)
    assert saved["status"] == "succeeded", "a settled success must not be rewritten to failed"
    assert saved["result_text"] == "the report"


def test_complete_coalesced_does_not_clobber_an_already_succeeded_run(tmp_path: Path) -> None:
    """HFR-061 — the coalesced completer must skip already-terminal rows.

    ``complete_coalesced_agent_runs_for_workbench_in_connection`` honors
    ``cancel_requested`` but has no ``queued|running`` predicate and no
    already-terminal skip, so it rewrites a settled row wholesale.
    """

    sqlite, requests = _store(tmp_path)
    request = requests.enqueue_agent_run(
        session_key="slack::channel::C1",
        message="hi",
        agent_name=None,
    )
    claimed = requests.claim(request.id)
    assert claimed is not None
    sqlite.record_run_output(
        request.id,
        output_id="out-1",
        text="done",
        terminal_status="succeeded",
    )

    requests.complete_coalesced(claimed, [request.id], ok=False, error="turn died")

    saved = sqlite.get_run(request.id)
    assert saved["status"] == "succeeded", "a settled success must not be rewritten to failed"


# --- group 2: the owed-notice drain ---------------------------------------
#
# The policy decisions are tested against ``core.failure_notices.decide`` directly:
# it is a pure function of (notice, streak, earlier-unsettled), so every branch is
# reachable without a controller, an event loop or an IM transport. The delivery
# protocol (receipt/ack/backoff/dead-letter) is tested against the store, since
# what has to survive a crash is the row, not the call.


def _notice(state: str = "pending", **fields) -> dict:
    payload = {"state": state, "attempts": 0, "next_attempt_at": None}
    payload.update(fields)
    return payload


def _streak_row(run_id: str, status: str = "failed", notice: dict | None = None) -> dict:
    return {"id": run_id, "created_at": f"2026-07-27T00:00:{run_id[-2:]}", "status": status, "notice": notice}


def test_second_failure_in_streak_is_skipped_not_notified() -> None:
    """HFR-073 — "notify on the 1st failure (once, not daily)".

    Each execution of a recurring definition is a new run, so without a scope every
    consecutive failure is a distinct unacknowledged notice and the drain notifies on
    every fire — the daily spam the policy forbids.
    """

    from core.failure_notices import ACTION_SKIP, decide

    first = _streak_row("run-01", notice=_notice("sent", ack_evidence="receipt"))
    second = _streak_row("run-02", notice=_notice("pending"))

    decision = decide(
        run_id="run-02",
        definition_id="task-1",
        notice=second["notice"],
        streak=[first, second],
        earlier_unsettled=None,
    )

    assert decision.action == ACTION_SKIP


def test_second_failure_defers_while_first_notice_is_still_pending() -> None:
    """HFR-074 — absence of an acknowledgement is not evidence nothing is in flight.

    Suppressing only once an earlier notice is ACKNOWLEDGED leaves the window that
    matters open: if the streak's first notice fails its transport attempt and a
    second execution fails before the retry lands, nothing is acknowledged, so the
    second row would pass the predicate and notify — while the first is still
    ``pending`` BY DESIGN because it is deliberately still retrying. One streak,
    two notices, produced by the interaction of two fixes rather than either alone.
    """

    from core.failure_notices import ACTION_DEFER, decide

    first = _streak_row("run-01", notice=_notice("pending", attempts=2, error="send: nope"))
    second = _streak_row("run-02", notice=_notice("pending"))

    decision = decide(
        run_id="run-02",
        definition_id="task-1",
        notice=second["notice"],
        streak=[first, second],
        earlier_unsettled=None,
    )

    assert decision.action == ACTION_DEFER
    assert "run-01" in decision.reason


def test_dead_lettered_canonical_notice_promotes_the_next_pending_row() -> None:
    """HFR-065 — a streak's claim on delivery outlives any single row.

    A streak whose canonical notice exhausted its retries still owes the user the
    news that the task is broken, so the earliest remaining ``pending`` row is
    promoted rather than deferring behind a dead letter forever.
    """

    from core.failure_notices import ACTION_DELIVER, decide

    first = _streak_row("run-01", notice=_notice("failed", attempts=5, error="send: nope"))
    second = _streak_row("run-02", notice=_notice("pending"))

    decision = decide(
        run_id="run-02",
        definition_id="task-1",
        notice=second["notice"],
        streak=[first, second],
        earlier_unsettled=None,
    )

    assert decision.action == ACTION_DELIVER


def test_failure_after_a_success_notifies_again() -> None:
    """A ``succeeded`` verdict closes the streak, so recovery re-arms notification."""

    from core.failure_notices import ACTION_DELIVER, decide

    # ``failure_streak`` returns only the streak CONTAINING this run, so a success
    # before it is already excluded — the streak here is this row alone.
    row = _streak_row("run-03", notice=_notice("pending"))

    decision = decide(
        run_id="run-03",
        definition_id="task-1",
        notice=row["notice"],
        streak=[row],
        earlier_unsettled=None,
    )

    assert decision.action == ACTION_DELIVER


def test_run_without_definition_id_is_never_suppressed() -> None:
    """``definition_id`` is nullable; an ad-hoc run has no streak to be absorbed into."""

    from core.failure_notices import ACTION_DELIVER, decide

    decision = decide(
        run_id="run-adhoc",
        definition_id=None,
        notice=_notice("pending"),
        streak=[],
        earlier_unsettled=None,
    )

    assert decision.action == ACTION_DELIVER


def test_classification_defers_while_an_earlier_run_is_still_running() -> None:
    """The streak is only computable over a settled prefix.

    ``create_per_run`` holds no execution lock, so executions overlap and completion
    order need not follow ``created_at``. A later-created run that fails first would
    become canonical and send; the earlier one then fails, becomes the new earliest,
    and the streak emits a second notice for the same outage.
    """

    from core.failure_notices import ACTION_DEFER, decide

    decision = decide(
        run_id="run-02",
        definition_id="task-1",
        notice=_notice("pending"),
        streak=[],
        earlier_unsettled={"id": "run-01", "created_at": "2026-07-27T00:00:00+00:00", "status": "running"},
    )

    assert decision.action == ACTION_DEFER
    assert "run-01" in decision.reason


def test_one_restart_interrupting_overlapping_runs_notifies_for_every_run() -> None:
    """An interruption notice is per-run, always, with no suppression scope.

    A single restart interrupts EVERY in-flight execution of a definition at once,
    and ``create_per_run`` means there can be several. Giving interruptions a
    streak-shaped scope (consecutive interruptions of D sharing one
    ``interrupt_reason``) would derive one notice from one arbitrary run and skip the
    rest — and each skipped run is a distinct turn with its own session, prompt and
    rerun path whose user is told nothing.

    Asserts the COUNT equals the number of interrupted runs, not merely that a
    notice was sent — which is the assertion the broken version would have passed.
    """

    from core.failure_notices import ACTION_DELIVER, decide

    interrupted = ["run-01", "run-02", "run-03"]
    streak = [_streak_row(rid, notice=_notice("pending", interrupt_reason="restarted")) for rid in interrupted]

    delivered = [
        run_id
        for run_id in interrupted
        if decide(
            run_id=run_id,
            definition_id="task-1",
            notice=_notice("pending", interrupt_reason="restarted"),
            streak=streak,
            earlier_unsettled=None,
        ).action
        == ACTION_DELIVER
    ]

    assert delivered == interrupted


def test_an_ordinary_result_less_failure_is_not_treated_as_an_interruption() -> None:
    """The lane split is membership, not ``interrupt_reason`` presence.

    ``no_terminal_result`` is stamped on the commonest harness failure of all. Read
    as an interruption it would get an unsuppressed notice on every fire, which is
    the daily spam the streak exists to prevent — the mirror image of the health bug
    the same wrong predicate causes.
    """

    from core.failure_notices import ACTION_SKIP, decide, is_interruption

    assert is_interruption({"interrupt_reason": "restarted"}) is True
    assert is_interruption({"interrupt_reason": "no_terminal_result"}) is False

    first = _streak_row("run-01", notice=_notice("sent", ack_evidence="receipt"))
    second = _streak_row("run-02", notice=_notice("pending", interrupt_reason="no_terminal_result"))

    decision = decide(
        run_id="run-02",
        definition_id="task-1",
        notice=second["notice"],
        streak=[first, second],
        earlier_unsettled=None,
    )

    assert decision.action == ACTION_SKIP


def test_watch_runtime_heartbeat_does_not_close_a_failure_streak(tmp_path: Path) -> None:
    """The heartbeat shares the watch's ``definition_id`` and goes ``succeeded``.

    ``write_watch_runtime`` flips every prior heartbeat to ``succeeded`` on each
    write, so an unfiltered streak query finds a success between any two watch
    failures. Every watch failure would then read as a first failure and notify —
    trading the permanent silence of the deferral bug for exactly the daily spam
    this sub-step exists to prevent.
    """

    sqlite, _ = _store(tmp_path)
    _watch(sqlite, "watch-streak")
    for index in (1, 2):
        sqlite.enqueue_run(
            {
                "id": f"run-w{index}",
                "request_type": "watch",
                "status": "failed",
                "definition_id": "watch-streak",
                "error": "hook blew up",
                "created_at": f"2026-07-27T0{index}:00:00+00:00",
                "completed_at": f"2026-07-27T0{index}:01:00+00:00",
            }
        )
        # A heartbeat write lands between the two failures, flipping the previous
        # runtime row to ``succeeded``.
        sqlite.write_watch_runtime(
            {"watches": {"watch-streak": {"running": True, "started_at": f"2026-07-27T0{index}:30:00+00:00"}}},
            updated_at=f"2026-07-27T0{index}:30:00+00:00",
        )

    streak = sqlite.failure_streak("watch-streak", "run-w2")

    assert [row["id"] for row in streak] == ["run-w1", "run-w2"], "the heartbeat must not split the streak"


def test_watch_runtime_heartbeat_does_not_defer_a_failed_watch_run(tmp_path: Path) -> None:
    """The heartbeat is earlier-created and permanently nonterminal.

    Unfiltered, every failed watch run defers behind its own supervisor forever and
    a long-running watch never delivers a failure notice — P6 for watches,
    reintroduced by the mechanism written to guarantee notification.
    """

    sqlite, _ = _store(tmp_path)
    _watch(sqlite, "watch-defer")
    sqlite.write_watch_runtime(
        {"watches": {"watch-defer": {"running": True, "started_at": "2026-07-27T00:00:00+00:00"}}},
        updated_at="2026-07-27T00:00:00+00:00",
    )
    sqlite.enqueue_run(
        {
            "id": "run-wd",
            "request_type": "watch",
            "status": "failed",
            "definition_id": "watch-defer",
            "error": "hook blew up",
            "created_at": "2026-07-27T02:00:00+00:00",
            "completed_at": "2026-07-27T02:01:00+00:00",
        }
    )
    # The heartbeat row is still ``running`` and was created two hours earlier.
    assert sqlite.get_run("runtime:watch-defer")["status"] == "running"

    blocker = sqlite.earliest_unsettled_run_before(
        "watch-defer",
        created_at="2026-07-27T02:00:00+00:00",
        run_id="run-wd",
        now="2026-07-27T02:05:00+00:00",
    )

    assert blocker is None, "the supervisor heartbeat is not an execution to wait for"


def test_out_of_order_completion_does_not_resend_for_one_streak(tmp_path: Path) -> None:
    """An earlier-created run that has not settled blocks classification."""

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-overlap", session_policy="create_per_run")
    sqlite.enqueue_run(
        {
            "id": "run-early",
            "request_type": "scheduled",
            "status": "running",
            "definition_id": "task-overlap",
            "created_at": "2026-07-27T01:00:00+00:00",
        }
    )
    sqlite.enqueue_run(
        {
            "id": "run-late",
            "request_type": "scheduled",
            "status": "failed",
            "definition_id": "task-overlap",
            "error": "boom",
            "created_at": "2026-07-27T02:00:00+00:00",
            "completed_at": "2026-07-27T02:01:00+00:00",
        }
    )

    blocker = sqlite.earliest_unsettled_run_before(
        "task-overlap",
        created_at="2026-07-27T02:00:00+00:00",
        run_id="run-late",
        now="2026-07-27T02:05:00+00:00",
    )

    assert blocker is not None and blocker["id"] == "run-early"


def test_a_stale_nonterminal_predecessor_stops_blocking_the_notice(tmp_path: Path) -> None:
    """The deferral is bounded, which the plan's own argument leaves open.

    The plan justifies an unbounded wait with "settling every nonterminal run is
    precisely what PR1/PR2/PR7 guarantee". PR2/PR4/PR7 are NOT landed, so on the
    current tree a queued row for a paused definition sits nonterminal indefinitely
    and the notice would never be delivered — a deferral without a bound, which this
    plan itself calls a deletion. Past the cap the predecessor is treated as
    settled, risking a duplicate notice rather than a lost one, which is the
    direction the plan chooses explicitly.
    """

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-stale-pred", session_policy="create_per_run")
    sqlite.enqueue_run(
        {
            "id": "run-wedged",
            "request_type": "scheduled",
            "status": "running",
            "definition_id": "task-stale-pred",
            "created_at": "2026-07-27T01:00:00+00:00",
        }
    )

    blocker = sqlite.earliest_unsettled_run_before(
        "task-stale-pred",
        created_at="2026-07-27T02:00:00+00:00",
        run_id="run-late",
        stale_after_seconds=3600.0,
        now="2026-07-29T00:00:00+00:00",
    )

    assert blocker is None


# --- group 2b: the delivery protocol --------------------------------------


def test_every_terminal_failure_transition_stamps_an_owed_notice(tmp_path: Path) -> None:
    """All five terminal writers, including the unguarded-until-now pair.

    The stamp is keyed on the PROPERTY "this UPDATE sets status to a terminal
    failure", not on a list of call sites, so a settlement path added later inherits
    it. Guardedness is deliberately not part of the test: the commonest failure of
    all terminalizes through the claimed-request completion.
    """

    sqlite, requests = _store(tmp_path)

    # 1. record_run_output
    first = requests.enqueue_hook_send(session_key="slack::channel::C1", prompt="a")
    requests.claim(first.id)
    sqlite.record_run_output(first.id, output_id="o1", text="bad", terminal_status="failed")

    # 2. settle_run_terminal, via the claimed-request completion
    second = requests.enqueue_hook_send(session_key="slack::channel::C1", prompt="b")
    claimed = requests.claim(second.id)
    requests.complete(claimed, ok=False, error="boom")

    # 3. settle_deferred_run
    third = requests.enqueue_hook_send(session_key="slack::channel::C1", prompt="c")
    requests.claim(third.id)
    sqlite.defer_run_terminal(third.id, terminal_status="failed", error="deferred boom")
    sqlite.settle_deferred_run(third.id)

    # 4. the coalesced completer
    fourth = requests.enqueue_agent_run(session_key="slack::channel::C1", message="d", agent_name=None)
    claimed_fourth = requests.claim(fourth.id)
    requests.complete_coalesced(claimed_fourth, [fourth.id], ok=False, error="coalesced boom")

    for run_id in (first.id, second.id, third.id, fourth.id):
        notice = sqlite.owed_failure_notice(run_id)
        assert notice is not None, f"{run_id} owes no notice"
        assert notice["state"] == "pending"
        # The bare run id, which is exactly what the LIVE path's ``_failure_identity``
        # resolves to. Asserted as the identity rather than as a format so a future
        # spelling change has to keep the dedup working rather than just keep a shape.
        assert notice["failure_id"] == run_id


def test_a_succeeded_or_canceled_transition_owes_no_notice(tmp_path: Path) -> None:
    """Only a failure owes a notice.

    ``canceled`` is reserved for explicit user intent, so telling a user their run
    failed because they stopped it is noise — and a cancellation is the absence of an
    outcome everywhere else in this feature too.
    """

    sqlite, requests = _store(tmp_path)
    ok_run = requests.enqueue_hook_send(session_key="slack::channel::C1", prompt="a")
    claimed = requests.claim(ok_run.id)
    requests.complete(claimed, ok=True)

    stopped = requests.enqueue_hook_send(session_key="slack::channel::C1", prompt="b")
    requests.claim(stopped.id)
    sqlite.cancel_run(stopped.id)
    sqlite.settle_run_terminal(stopped.id, terminal_status="failed", error="stopped")

    assert sqlite.get_run(stopped.id)["status"] == "canceled"
    assert sqlite.owed_failure_notice(ok_run.id) is None
    assert sqlite.owed_failure_notice(stopped.id) is None


def test_a_later_writer_never_resets_an_in_flight_notice(tmp_path: Path) -> None:
    """Re-stamping would reset ``attempts`` and resurrect a dead letter."""

    sqlite, requests = _store(tmp_path)
    run = requests.enqueue_hook_send(session_key="slack::channel::C1", prompt="a")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="boom")
    sqlite.update_owed_failure_notice(run.id, state="failed", attempts=5, error="send: nope")

    # Another writer touches the same already-terminal row.
    sqlite.settle_run_terminal(run.id, terminal_status="failed", error="boom again")

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == "failed"
    assert notice["attempts"] == 5


def test_backoff_spaces_attempts_and_terminates_in_a_dead_letter() -> None:
    """Bounded retry: spaced, then a visible dead letter — not every tick forever.

    There is no attempt counter or backoff anywhere in the scheduler today, so this
    is new machinery and the bound is the part worth pinning.
    """

    from core.failure_notices import MAX_ATTEMPTS, next_attempt

    delays = []
    notice = {"attempts": 0}
    while True:
        attempt, retry_after = next_attempt(notice)
        notice["attempts"] = attempt
        if retry_after is None:
            break
        delays.append(retry_after)
        assert attempt < MAX_ATTEMPTS

    assert delays == sorted(delays) and len(set(delays)) == len(delays), "must back off, not fire every tick"
    assert notice["attempts"] == MAX_ATTEMPTS


def test_a_notice_whose_backoff_has_not_elapsed_is_not_listed(tmp_path: Path) -> None:
    """The drain skips a notice still inside its backoff window."""

    sqlite, requests = _store(tmp_path)
    run = requests.enqueue_hook_send(session_key="slack::channel::C1", prompt="a")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="boom")
    sqlite.update_owed_failure_notice(
        run.id,
        attempts=1,
        next_attempt_at="2026-07-27T12:00:00+00:00",
    )

    assert sqlite.list_owed_failure_notices(now="2026-07-27T11:00:00+00:00") == []
    ready = sqlite.list_owed_failure_notices(now="2026-07-27T13:00:00+00:00")
    assert [item["id"] for item in ready] == [run.id]


def test_a_sent_notice_is_not_listed_again(tmp_path: Path) -> None:
    """A crash between delivery and the ack must not re-send.

    The ack is the durable half: once the row records evidence of delivery, the next
    drain pass simply does not see it. Combined with the run-derived ``failure_id``
    (which dedupes on ``(platform, native_message_id)``), that is at-least-once
    delivery with a bounded duplicate window — NOT exactly-once, because a crash
    between ``send_message`` returning and the row being written leaves nothing to
    deduplicate against.
    """

    sqlite, requests = _store(tmp_path)
    run = requests.enqueue_hook_send(session_key="slack::channel::C1", prompt="a")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="boom")
    assert [item["id"] for item in sqlite.list_owed_failure_notices()] == [run.id]

    sqlite.update_owed_failure_notice(run.id, state="sent", ack_evidence="receipt")

    assert sqlite.list_owed_failure_notices() == []


def test_a_session_less_definition_dead_letters_rather_than_going_silent(tmp_path: Path) -> None:
    """The residual D5 gap, pinned rather than hidden.

    D5 rung (5) is meant always to resolve because it is addressed to the workspace.
    ``maybe_notify_inbox_message``'s ``session_id`` requirement is widened for that,
    but the FIRST blocker is earlier than the plan names: ``persist_agent_message``
    returns before writing anything when an avibe context resolves neither a scope
    nor a session row. A definition that has never had a session therefore still has
    nowhere to put the row.

    What this test pins is that such a notice ends ``failed`` — a VISIBLE dead letter
    carrying the reason — rather than being silently dropped or retried forever. The
    failure also remains visible through ``last_error`` and the health badge, so the
    user is not left with nothing; they are left without a push.
    """

    from core.failure_notices import MAX_ATTEMPTS

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-no-session")
    run = requests.enqueue_task_run("task-no-session")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="unresolvable target", task_id="task-no-session")

    notice = sqlite.owed_failure_notice(run.id)
    assert notice is not None and notice["state"] == "pending"

    # The premise, asserted rather than assumed. Everything below hand-drives the
    # attempt counter, so without this the test would read identically whether the
    # ladder was empty, full, or handing session-less definitions phantom rungs.
    # It stays empty after project-scoped rungs are admitted: rungs (2) and (5) are
    # both keyed on a session id this definition has never had.
    from types import SimpleNamespace

    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)
    assert service._failure_notice_targets(sqlite.get_run(run.id)) == [], (
        "a definition with no session and no provenance has nowhere to deliver"
    )

    # Exhausting the attempts is what a ladder with no usable rung produces.
    for _ in range(MAX_ATTEMPTS):
        sqlite.update_owed_failure_notice(
            run.id,
            attempts=int(sqlite.owed_failure_notice(run.id)["attempts"]) + 1,
        )
    sqlite.update_owed_failure_notice(run.id, state="failed", error="no usable delivery rung")

    settled = sqlite.owed_failure_notice(run.id)
    assert settled["state"] == "failed"
    assert settled["error"] == "no usable delivery rung"
    # And the failure is still visible on the definition itself.
    assert sqlite.definition_health("task-no-session")["health"] == "failing"


def test_the_drain_delivers_an_ordinary_failure_once_and_acks_it(tmp_path: Path, monkeypatch) -> None:
    """The whole path, end to end, through the real wiring.

    Every other test in this group isolates one layer. This one runs the tick's
    actual entry point over a real store so the parts are proven to be connected:
    an ordinary claimed-request failure stamps a notice, the drain walks D5's
    ladder to the definition's delivery key, the notice acks on the receipt, and a
    SECOND pass sends nothing.

    The second pass is the point. It is the crash-between-delivery-and-ack case:
    whatever happens after the row records evidence of delivery, the next drain
    simply does not see it.
    """

    import asyncio
    from types import SimpleNamespace

    import core.scheduled_tasks as scheduled_tasks
    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-e2e", name="daily report", deliver_key="slack::channel::C1")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()

    run = requests.enqueue_task_run("task-e2e")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-e2e")
    assert sqlite.owed_failure_notice(run.id)["state"] == "pending"

    emitted: list[dict] = []

    async def _fake_emit(controller, context, backend, diagnostic, **kwargs):
        emitted.append(
            {
                "platform": context.platform,
                "channel_id": context.channel_id,
                "body": kwargs.get("display_text"),
                "failure_id": kwargs.get("failure_id"),
            }
        )
        evidence = kwargs.get("delivery")
        if evidence is not None:
            evidence.delivered_id = "1717.42"
            evidence.persisted_row = {"id": "msg-1"}
        return False

    monkeypatch.setattr(scheduled_tasks, "emit_replayed_backend_failure", _fake_emit)

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service._drain_dirty = False
    service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
    service._owns_service_instance = lambda: True
    service.validate_platform = lambda platform: None
    service._t = lambda key, **kwargs: key

    asyncio.run(service._drain_failure_notices())

    assert len(emitted) == 1
    # Rung (1): the definition's own delivery key.
    assert (emitted[0]["platform"], emitted[0]["channel_id"]) == ("slack", "C1")
    # Run-derived, so two passes cannot mint two identities — and identical to what
    # the live path would have used, so the drain cannot duplicate a notification the
    # live path already delivered.
    assert emitted[0]["failure_id"] == run.id

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == "sent"
    assert notice["ack_evidence"] == "receipt"
    assert notice["attempts"] == 1

    emitted.clear()
    asyncio.run(service._drain_failure_notices())
    assert emitted == [], "an acknowledged notice must never be delivered twice"


def test_the_drain_body_names_what_failed_and_how_to_re_run(tmp_path: Path) -> None:
    """D5 requires the body to carry its own context.

    A DM is context-free by construction and the workbench rung is attached to no
    conversation, so "your task failed" delivered somewhere the user cannot trace
    is not actionable.
    """

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-body", name="daily report")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
    service._t = lambda key, **kwargs: f"{key}({','.join(f'{k}={v}' for k, v in kwargs.items())})"

    body = service._failure_notice_body(
        {"id": "run-1", "task_id": "task-body", "error": "backend exploded"},
        {"failure_id": "failure:run-1", "interrupt_reason": None},
    )

    assert "name=daily report" in body
    assert "error=backend exploded" in body
    assert "id=task-body" in body
    assert "harness.notice.rerun" in body


def test_a_failed_watch_notice_renders_watch_commands(tmp_path: Path) -> None:
    """HFR-094 — a watch is not a task, and the copy has to know it.

    ``_failure_notice_body`` resolved the definition through
    ``ScheduledTaskStore.get_task`` ALONE and then appended
    ``harness.notice.rerun`` — ``vibe task run {id}`` / ``vibe task show {id}`` — for
    every definition it rendered. A watch definition is not in the task mirror, so a
    failed watch got both halves wrong: named by its raw id, and handed two commands
    that cannot address it. ``vibe task show <watch-id>`` reports "not found", and
    there is no ``vibe task run`` for a definition that is not scheduled at all —
    the only surface telling a user their watch died pointed at the wrong noun.

    Watches have their own verbs (``vibe watch show`` / ``vibe watch resume``), so
    this is a copy and read-path defect, not a missing feature.

    Asserted through the REAL translator: the defect is in the rendered command
    strings a user reads, not in which key was selected.
    """

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    sqlite, requests = _store(tmp_path)
    _watch(sqlite, "watch-ci", name="ci waiter", deliver_key="slack::channel::C1")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()
    assert store.get_task("watch-ci") is None, "a watch is not in the task mirror"

    run = requests.enqueue_hook_send(
        session_key="slack::channel::C1",
        prompt="the waiter finished",
        run_type="watch",
        definition_id="watch-ci",
    )
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="hook delivery failed", task_id="watch-ci")

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    # No ``config`` on the controller, so the real ``_t`` renders English out of the
    # shipped catalog instead of echoing keys.
    service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)

    notice = sqlite.owed_failure_notice(run.id)
    assert notice is not None
    body = service._failure_notice_body(sqlite.get_run(run.id), notice)

    assert "vibe watch" in body, f"a failed watch must be given watch commands: {body}"
    assert "vibe task" not in body, f"a watch cannot be addressed as a task: {body}"
    assert "ci waiter" in body, f"the watch's own name must survive the lookup: {body}"


# --- group 2d: crash/exception ordering in the delivery protocol -----------
#
# All three of these are ordering bugs, not happy-path gaps: the delivery
# succeeds, or the delivery raises, and the protocol draws the wrong conclusion
# because of WHERE in the sequence it looked.


def test_the_duplicate_short_circuit_reports_its_receipt(tmp_path: Path) -> None:
    """HFR-075 — a found row is the strongest receipt there is, and it was dropped.

    Crash after ``persist_agent_message`` commits but before the owed notice is
    acknowledged, and the retry hits the duplicate short-circuit
    (``agent_message_exists`` → ``return native_output_id``) which sits ~112 lines
    ABOVE the notify branch's evidence assignment and touches ``delivery`` nowhere.

    ``_emit_failure_notice`` discards the return value and inspects only
    ``DeliveryEvidence``, so a notice that IS delivered and IS persisted reads as
    unacknowledged — and then either walks on to another ladder rung and sends a
    duplicate, or burns its backoff and dead-letters a notice that already has a
    receipt.
    """

    from unittest.mock import patch

    import core.message_dispatcher as dispatcher_module
    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT, DeliveryEvidence
    from core.message_output import MessageOutput
    from modules.im import MessageContext

    from tests.test_message_dispatcher_scheduled import _StubController

    controller = _StubController()
    dispatcher = dispatcher_module.ConsolidatedMessageDispatcher(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        platform_specific={"task_trigger_kind": "scheduled", "task_execution_id": "run-1"},
    )
    evidence = DeliveryEvidence()

    # The row from the pre-crash attempt is already in ``messages``.
    with patch.object(dispatcher_module, "agent_message_exists", return_value=True):
        returned = asyncio.run(
            dispatcher.emit_agent_message(
                context,
                "notify",
                "your task failed",
                output=MessageOutput(
                    completes_turn=False,
                    completes_run=False,
                    idempotency_key="backend-failure:failure:run-1",
                ),
                delivery=evidence,
            )
        )

    # The short-circuit returns the stable identity and re-sends nothing: that half
    # already worked. The defect is that it says so ONLY through a return value the
    # notice drain does not read.
    assert returned and "backend-failure:failure:run-1" in returned
    assert controller.im_client.sent == []
    # ...and it must SAY so, or the drain cannot tell this from a lost notice.
    assert evidence.delivered is True, "a persisted row is evidence of delivery"
    assert evidence.ack_evidence == ACK_EVIDENCE_RECEIPT


def test_a_raising_delivery_consumes_an_attempt_instead_of_retrying_forever(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-076 — an exception must not escape before ``attempts`` is persisted.

    ``_deliver_one_failure_notice`` computed the attempt number, then awaited
    delivery, then wrote the result. An exception from any ladder rung escaped
    between the second and third step, so nothing was persisted and the next 2 s
    tick recomputed the SAME attempt number and raised again — an unbounded retry
    loop, which is precisely what the backoff exists to prevent.

    The live case is auth recovery (see the bypass test below), but the bound has
    to hold for ANY raising rung, so this test raises directly.
    """

    from types import SimpleNamespace

    import core.scheduled_tasks as scheduled_tasks
    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-raise", deliver_key="slack::channel::C1")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()

    run = requests.enqueue_task_run("task-raise")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="boom", task_id="task-raise")

    async def _raising_emit(controller, context, backend, diagnostic, **kwargs):
        raise RuntimeError("auth prompt exploded")

    monkeypatch.setattr(scheduled_tasks, "emit_replayed_backend_failure", _raising_emit)

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service._drain_dirty = False
    service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
    service._owns_service_instance = lambda: True
    service.validate_platform = lambda platform: None
    service._t = lambda key, **kwargs: key

    seen = []
    for _ in range(8):
        asyncio.run(service._drain_failure_notices())
        current = dict(sqlite.owed_failure_notice(run.id))
        seen.append(current)
        if current["state"] == "pending":
            # Let the backoff elapse without sleeping. Rewinding rather than waiting
            # keeps the test deterministic while still driving the real retry path —
            # and the fact that this is NEEDED is itself the backoff working, since
            # against the old code every tick fired regardless.
            sqlite.update_owed_failure_notice(run.id, next_attempt_at=None)

    attempts = [entry["attempts"] for entry in seen]
    assert attempts[0] == 1, f"a raising delivery must consume an attempt, got {attempts}"
    assert attempts == sorted(attempts), "attempts must advance monotonically"
    assert seen[-1]["state"] == "failed", "a persistently raising rung must dead-letter"
    assert "auth prompt exploded" in (seen[-1]["error"] or ""), "the dead letter must say why"


def test_a_stale_drain_pass_cannot_overwrite_a_newer_acknowledgement(tmp_path: Path) -> None:
    """HFR-076, the concurrency half of the same bound — a handoff must not undo an ack.

    ``_owns_service_instance`` is consulted ONCE at the top of a drain pass and then
    the pass AWAITS delivery. Ownership is a live flock check, so during a
    service-lock handoff the outgoing process's lease can lapse while its coroutine
    is still suspended in that send, and the incoming owner reads the SAME pending
    notice, delivers it and acknowledges. The old pass then resumes and performs its
    own write — and ``update_owed_failure_notice`` was keyed on the run id alone,
    guarded only on "a notice still exists", so the loser's stale ``pending`` retry or
    ``failed`` dead letter overwrote the winner's ``sent``. Both directions produce
    another delivery; the ``failed`` one also permanently buries a receipt the user
    already has, which is the worse half.

    The fix is the ``DefinitionWriteExpectation`` idiom applied to the notice blob: a
    write re-asserts the ``(state, attempts)`` it was decided from, and the losing
    pass silently no-ops rather than raising — it has nothing to repair, and the next
    tick re-reads the winner's state. ``attempts`` is in the predicate so two passes
    that both read attempt N cannot both consume it.
    """

    from types import SimpleNamespace

    from core.failure_notices import MAX_ATTEMPTS

    sqlite, requests = _store(tmp_path)
    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)

    def _owed_run(definition_id: str, *, attempts: int = 0):
        _task(sqlite, definition_id, deliver_key="slack::channel::C1")
        run = requests.enqueue_task_run(definition_id)
        claimed = requests.claim(run.id)
        assert claimed is not None
        requests.complete(claimed, ok=False, error="boom", task_id=definition_id)
        if attempts:
            sqlite.update_owed_failure_notice(run.id, attempts=attempts)
        assert sqlite.owed_failure_notice(run.id)["state"] == "pending"
        return run

    def _overtaken_pass(run_id: str, *, winner_attempts: int) -> dict[str, Any]:
        """One drain pass, overtaken by the new lock owner while its send is in flight."""

        async def _emit_while_the_new_owner_wins(*args, **kwargs):
            # Suspended exactly where the old pass sits when its flock lapses: after
            # its own read of the pending notice, inside the awaited send. The new
            # owner's pass — which read the same pending notice — runs to completion
            # here, ack included.
            await asyncio.sleep(0)
            sqlite.update_owed_failure_notice(
                run_id,
                state=NOTICE_SENT,
                attempts=winner_attempts,
                ack_evidence="receipt",
            )
            # ...and the resumed old pass finds nothing acknowledging ITS send.
            return False

        service._emit_failure_notice = _emit_while_the_new_owner_wins
        asyncio.run(service._drain_failure_notices())
        return dict(sqlite.owed_failure_notice(run_id))

    # Direction 1: the loser still has attempts left, so its stale write is a
    # ``pending`` retry. Self-healing at best — the resend hits the duplicate
    # short-circuit — and a visible duplicate whenever the winner acked on a
    # delivery id with no persisted row to find.
    retry = _owed_run("task-stale-retry")
    after_retry = _overtaken_pass(retry.id, winner_attempts=1)
    assert after_retry["state"] == NOTICE_SENT, (
        "a stale retry must not reopen a notice the new owner already acknowledged, "
        f"got {after_retry['state']}"
    )
    assert after_retry["attempts"] == 1, "the winner's consumed attempt must stand"
    assert after_retry["ack_evidence"] == "receipt", "the winner's receipt must survive"
    assert not after_retry["error"], "the loser's error text must not land on a sent notice"

    # Direction 2: the loser is on its last attempt, so its stale write is the dead
    # letter — the damaging one, because ``failed`` is terminal and hides the receipt
    # for good.
    dead = _owed_run("task-stale-dead-letter", attempts=MAX_ATTEMPTS - 1)
    after_dead = _overtaken_pass(dead.id, winner_attempts=MAX_ATTEMPTS)
    assert after_dead["state"] == NOTICE_SENT, (
        "a stale dead letter must not bury a delivered notice, "
        f"got {after_dead['state']}"
    )
    assert after_dead["attempts"] == MAX_ATTEMPTS
    assert after_dead["ack_evidence"] == "receipt", "the winner's receipt must survive"
    assert not after_dead["error"], "a sent notice must not carry the loser's dead-letter reason"


def test_the_drain_does_not_turn_an_owed_notice_into_a_live_auth_prompt(
    tmp_path: Path,
) -> None:
    """HFR-077 — a drained notice cannot become an interactive auth prompt.

    ``maybe_emit_auth_recovery_message`` cannot fill a ``DeliveryEvidence`` — its
    signature has no such parameter — so a notice delivered through it reads as
    unacknowledged and walks on to the next ladder rung.

    Not plumbing evidence through it is a product answer, not a shortcut: an owed
    notice is a report about a run that failed in the past, possibly hours ago and
    possibly already retried, whereas the auth prompt is an interactive remediation
    affordance about the state of the backend RIGHT NOW. Those are different
    messages.

    Asserted on the drain's OBSERVABLE behaviour against a real controller whose
    auth service refuses to be read at all, rather than on an argument the drain
    passes. The bypass used to be an ``allow_auth_recovery=False`` on the live
    emitter, so this test could only check that the drain remembered to pass it; the
    drain now has its own emitter that cannot reach auth recovery, and the way to
    prove that is to make any access fail.
    """

    _migrated_state_db()
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    controller.agent_auth_service = _ForbiddenAuthService()

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-auth", deliver_key="slack::channel::C123")
    run = requests.enqueue_task_run("task-auth")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="401 unauthorized", task_id="task-auth")

    service = _drain_service(tmp_path, controller, sqlite, requests)

    asyncio.run(service._drain_failure_notices())

    assert sqlite.owed_failure_notice(run.id)["state"] == NOTICE_SENT
    # ...and what the user got is the notice, not an OAuth prompt.
    assert len(controller.im_client.sent) == 1
    assert "harness.notice" in controller.im_client.sent[0][2]


def test_auth_recovery_stays_on_for_every_other_caller() -> None:
    """There is no bypass to forget, because the live path has no switch.

    Auth recovery is where a real 401 gets its reset-OAuth button, and every live
    backend call site reaches it unconditionally. The drain does not decline it by
    argument; it is a different emitter that never had it.
    """

    import inspect
    from types import SimpleNamespace

    from core.backend_failure import emit_backend_failure, emit_replayed_backend_failure
    from modules.im import MessageContext

    live_params = inspect.signature(emit_backend_failure).parameters
    assert "allow_auth_recovery" not in live_params, (
        "a bypass argument on the live path is one a caller can pass by mistake"
    )
    replay_params = inspect.signature(emit_replayed_backend_failure).parameters
    assert "allow_auth_recovery" not in replay_params

    # And the live path really does consult it, for any caller.
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    consulted: list[str] = []

    async def _maybe_recover(context, backend, visible, **kwargs):
        consulted.append(backend)
        return True

    controller.agent_auth_service = SimpleNamespace(
        maybe_emit_auth_recovery_message=_maybe_recover
    )
    context = MessageContext(
        user_id="u1", channel_id="C123", platform="slack", platform_specific={"turn_token": "t"}
    )

    handled = asyncio.run(emit_backend_failure(controller, context, "codex", "401 unauthorized"))

    assert handled is True and consulted == ["codex"]


def test_the_owed_notice_lookup_does_not_scan_settled_history(tmp_path: Path) -> None:
    """HFR-078 — the 2 s lookup must be bounded in SQL, not in Python.

    The query selected every ``failed`` run with no predicate on notice state or
    backoff and no SQL ``LIMIT``; the Python limit applied only once a PENDING
    notice was found. Once historical failures are all ``sent``/``skipped``/
    ``failed`` — the steady state — every tick scanned and JSON-decoded the entire
    failed-run history to return an empty list, at a cost growing without bound
    over the database's lifetime.

    Asserted on the work done, not on wall-clock time: the number of metadata rows
    the store decodes in Python.
    """

    from unittest.mock import patch

    import storage.background as background

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-history")

    settled = 40
    for index in range(settled):
        run_id = f"run-old-{index:03d}"
        sqlite.enqueue_run(
            {
                "id": run_id,
                "request_type": "scheduled",
                "status": "failed",
                "definition_id": "task-history",
                "error": "boom",
                "created_at": f"2026-07-01T00:{index:02d}:00+00:00",
                "completed_at": f"2026-07-01T00:{index:02d}:30+00:00",
                "metadata": {
                    "owed_failure_notice": {
                        "state": "sent",
                        "attempts": 1,
                        "ack_evidence": "receipt",
                        "failure_id": f"failure:{run_id}",
                    }
                },
            }
        )

    decoded: list[int] = []
    real_json_loads = background._json_loads

    def _counting_json_loads(value, default):
        decoded.append(1)
        return real_json_loads(value, default)

    with patch.object(background, "_json_loads", _counting_json_loads):
        assert sqlite.list_owed_failure_notices() == []

    assert len(decoded) < settled, (
        f"decoded {len(decoded)} metadata blobs for {settled} settled rows — "
        "the eligibility predicate must run in SQL, before the limit"
    )

    # And pin the bound on the statement itself, so the behaviour above cannot be
    # satisfied later by some Python-side shortcut that leaves the scan in place.
    from sqlalchemy import event

    statements: list[tuple[str, Any]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "agent_runs" in statement and statement.strip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    event.listen(sqlite.engine, "before_cursor_execute", _capture)
    try:
        sqlite.list_owed_failure_notices(limit=7, now="2026-07-27T12:00:00+00:00")
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _capture)

    statement, parameters = statements[-1]
    rendered = f"{statement} {parameters}"
    assert "LIMIT" in statement.upper(), "the query must be bounded in SQL"
    assert "json_valid" in statement, "one malformed row must not fail the statement"
    assert f"$.{OWED_FAILURE_NOTICE_KEY}.state" in rendered
    assert f"$.{OWED_FAILURE_NOTICE_KEY}.next_attempt_at" in rendered


def test_the_owed_notice_lookup_survives_one_malformed_metadata_row(tmp_path: Path) -> None:
    """The SQL predicate needs the same ``json_valid`` guard as the health window.

    ``json_extract`` raises ``malformed JSON`` and fails the whole STATEMENT, so
    without the guard one bad row stops the drain finding ANY owed notice — every
    failure notification in the system, silenced by a single unparseable blob.
    """

    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-malformed")

    good = requests.enqueue_task_run("task-malformed")
    claimed = requests.claim(good.id)
    requests.complete(claimed, ok=False, error="boom", task_id="task-malformed")

    sqlite.enqueue_run(
        {
            "id": "run-bad-json",
            "request_type": "scheduled",
            "status": "failed",
            "definition_id": "task-malformed",
            "error": "boom",
            "created_at": "2026-07-01T00:00:00+00:00",
        }
    )
    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs).where(agent_runs.c.id == "run-bad-json").values(metadata_json="{not json")
        )

    owed = sqlite.list_owed_failure_notices()

    assert [item["id"] for item in owed] == [good.id]


# --- group 2e: the replay must not disturb the present ---------------------


def _live_turn_dispatcher():
    """A dispatcher whose target channel has a turn in progress.

    ``emit_matches_runtime_turn`` fails OPEN when a context carries no runtime-turn
    key — deliberately, because a scheduled or watch run legitimately has none and
    its own terminal result must still settle. That assumption holds while the
    emitter IS the run's live execution. For a REPLAY of a run that ended hours ago
    it is exactly backwards: the tokenless context is adopted by whatever turn is
    currently live on that channel.
    """

    from types import SimpleNamespace

    import core.message_dispatcher as dispatcher_module

    from tests.test_message_dispatcher_scheduled import _StubController

    controller = _StubController()
    # No runtime-turn key on the replay context, so this returns True (fail-open) —
    # the dispatcher believes the emit belongs to the channel's current turn.
    controller.agent_service = SimpleNamespace(
        emit_matches_runtime_turn=lambda context: True,
        activities=SimpleNamespace(has_blocking_run_activity=lambda run_id: False),
        release_runtime_turn=lambda context: None,
    )
    dispatcher = dispatcher_module.ConsolidatedMessageDispatcher(controller)

    touched: list[str] = []

    async def _collapse(*args, **kwargs):
        touched.append("collapse_status_bubble")

    async def _clear(*args, **kwargs):
        touched.append("clear_consolidated_state")

    def _signal(*args, **kwargs):
        touched.append("signal_turn_complete")

    async def _finish(*args, **kwargs):
        touched.append("finish_processing_indicator_turn")

    def _release(*args, **kwargs):
        touched.append("release_runtime_turn")

    dispatcher._collapse_status_bubble = _collapse
    dispatcher._clear_consolidated_state = _clear
    dispatcher._signal_turn_complete = _signal
    dispatcher._finish_processing_indicator_turn = _finish
    dispatcher._release_runtime_turn = _release

    controller.emit_agent_message = dispatcher.emit_agent_message
    return controller, dispatcher, touched


class _ForbiddenAuthService:
    """Any attribute read at all is a failure.

    The replay may not consult auth recovery, and the check has to be on ACCESS
    rather than on invocation: ``emit_backend_failure`` reads
    ``maybe_emit_auth_recovery_message`` off this service and only then decides
    whether to await it, so a spy that records calls alone would pass for a replay
    that had walked back into the live path.
    """

    def __getattr__(self, name: str):  # pragma: no cover - the assertion is the point
        raise AssertionError(f"the replay path reached auth recovery: {name}")


def _migrated_state_db() -> Path:
    """The isolated home's workbench state DB, with the real schema applied.

    Without the schema ``persist_agent_message`` cannot upsert a scope, swallows the
    "no such table" error and returns ``None``. The notice then acks on the send id
    alone and no ``messages`` row is written — so every assertion about the notice a
    user can actually read back, and about the identity that row is keyed by, needs
    the real schema rather than a stub.

    The one-time JSON → SQLite import is settled here too. Any default-path
    ``SQLiteBackgroundTaskStore()`` runs ``ensure_sqlite_state`` lazily from its
    constructor, and on an unmarked DB that CLEARS every imported table — including
    ``messages``. The live result path builds such a store
    (``_record_agent_run_terminal_result``), so a test that persists a notification
    and then drives a live settlement had its row silently wiped mid-test. Priming
    the marker up front makes the import a fact of setup rather than a hazard of
    whichever path happens to construct a store first.
    """

    from config import paths
    from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
    from storage.migrations import run_migrations

    path = paths.get_sqlite_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    run_migrations(path)
    ensure_sqlite_state(
        primary_platform=resolve_primary_platform_from_config(paths.get_state_dir())
    )
    return path


def _persisted_messages() -> list[dict]:
    from sqlalchemy import text as sa_text

    from storage.db import get_cached_sqlite_engine

    with get_cached_sqlite_engine().begin() as conn:
        rows = conn.execute(sa_text("SELECT * FROM messages ORDER BY created_at, id")).mappings()
        return [dict(row) for row in rows]


def _drain_service(tmp_path: Path, controller, sqlite, requests):
    """A ``ScheduledTaskService`` wired to drain one real store through *controller*."""

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()

    controller.platform_settings_managers = {}
    controller.session_turn_gate = None

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service._drain_dirty = False
    service.controller = controller
    service._owns_service_instance = lambda: True
    service.validate_platform = lambda platform: None
    service._t = lambda key, **kwargs: key
    return service


def _spy_emissions(controller) -> list[dict[str, Any]]:
    """Record every ``emit_agent_message`` the drain makes, then pass it through."""

    seen: list[dict[str, Any]] = []
    inner = controller.emit_agent_message

    async def _spy(context, message_type, text, *args, **kwargs):
        seen.append(
            {
                "type": message_type,
                "text": text,
                "level": kwargs.get("level"),
                "output": kwargs.get("output"),
            }
        )
        return await inner(context, message_type, text, *args, **kwargs)

    controller.emit_agent_message = _spy
    return seen


def test_a_replayed_notice_reaches_the_user_without_touching_the_live_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-079 — the replay is its own emitter, not the live path with switches off.

    The drain used to replay a historical failure through ``emit_backend_failure``
    and then neutralize the live behaviour one argument at a time: a non-settling
    ``output``, an auth-recovery bypass, an explicit identity. The terminal
    ``result`` was still emitted (``settle_terminal_failure`` runs in a ``finally``),
    so the replay stayed inside the live turn lifecycle with a defused payload —
    and four review rounds found a settlement, an auth and an identity defect in
    the gaps between those switches.

    Two halves, and the guard is the load-bearing one:

    * the consuming end — a user actually gets the notice, persisted and acked;
    * the structural guard — the live emitter is UNREACHABLE from the drain, no
      ``result`` is emitted at all, auth recovery is not even looked at, and no
      turn settlement, status-bubble teardown or runtime-turn release happens.

    The run is terminal by construction: a notice is owed only once the row is
    settled, so there is nothing here for a lifecycle emit to settle.
    """

    import core.backend_failure as backend_failure_module
    import core.scheduled_tasks as scheduled_tasks
    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT

    _migrated_state_db()
    controller, _dispatcher, touched = _live_turn_dispatcher()
    controller.agent_auth_service = _ForbiddenAuthService()

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-replay", name="daily report", deliver_key="slack::channel::C123")
    run = requests.enqueue_task_run("task-replay")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-replay")
    assert sqlite.owed_failure_notice(run.id)["state"] == "pending"

    async def _forbidden_live_emit(*args, **kwargs):
        raise AssertionError("the drain called the LIVE backend-failure emitter")

    # Snapshot BEFORE patching: ``raising=False`` below would otherwise CREATE the
    # attribute and make the structural check trivially true.
    drain_imports_live_emitter = hasattr(scheduled_tasks, "emit_backend_failure")

    # Both spellings, because patching only the definition would leave the drain's
    # own module-level reference — the one it actually calls — untouched.
    monkeypatch.setattr(backend_failure_module, "emit_backend_failure", _forbidden_live_emit)
    monkeypatch.setattr(
        scheduled_tasks, "emit_backend_failure", _forbidden_live_emit, raising=False
    )

    service = _drain_service(tmp_path, controller, sqlite, requests)
    emissions = _spy_emissions(controller)

    asyncio.run(service._drain_failure_notices())

    # --- the guard ---------------------------------------------------------
    assert [item["type"] for item in emissions] == ["notify"], (
        f"the replay must emit exactly one visible notify and nothing else: {emissions}"
    )
    assert touched == [], f"the replay mutated live turn state: {touched}"
    output = emissions[0]["output"]
    assert output.completes_turn is False and output.settles_run is False, (
        "a receipt about an already-terminal run may not settle a turn or a run"
    )
    assert not drain_imports_live_emitter, (
        "the drain must not so much as import the live failure emitter"
    )

    # --- the consuming end -------------------------------------------------
    assert controller.im_client.sent, "the notice must still reach the user"
    channel, _thread, sent_text = controller.im_client.sent[0]
    assert channel == "C123"
    assert sent_text == emissions[0]["text"]
    assert "harness.notice.rerun" in sent_text

    rows = _persisted_messages()
    assert [row["type"] for row in rows] == ["notify"], (
        f"exactly one durable row, the visible one: {rows}"
    )
    assert rows[0]["content_text"] == sent_text

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == NOTICE_SENT
    assert notice["ack_evidence"] == ACK_EVIDENCE_RECEIPT
    assert notice["attempts"] == 1


def _workbench_session(
    session_id: str,
    *,
    project: str,
    status: str = "active",
) -> str:
    """One avibe project scope + one session row, in the MIGRATED WORKBENCH DB.

    Two databases are in play in every drain test and they are NOT the same file.
    The run/notice store is its own sqlite under ``tmp_path`` (``_store``), while
    ``resolve_session_id_target`` reads ``paths.get_sqlite_state_path()`` and
    ``persist_agent_message`` writes through ``get_cached_sqlite_engine()``. An
    avibe rung resolves and persists ONLY from the latter, so a session row written
    into the ``_store`` DB leaves rungs (2) and (5) silently unusable — and a test
    built that way passes or fails for the wrong reason.

    Returns the ``scopes.id`` the persisted row must be keyed to.
    """

    from storage.db import get_cached_sqlite_engine
    from storage.models import agent_sessions
    from storage.settings_service import upsert_scope

    _migrated_state_db()
    now = "2026-07-01T00:00:00+00:00"
    with get_cached_sqlite_engine().begin() as conn:
        scope_id = upsert_scope(conn, "avibe", "project", project, now=now)
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=scope_id,
                agent_backend="codex",
                agent_name="codex",
                agent_variant="default",
                session_anchor=f"avibe_{project}:{session_id}",
                native_session_id=f"native-{session_id}",
                status=status,
                visibility="foreground",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    return scope_id


def _no_background_web_push(monkeypatch) -> list[dict]:
    """Keep the Web Push fan-out off the DAEMON THREAD, on the calling thread's DB.

    Persisting an avibe inbox row schedules ``_send_to_enabled_subscriptions`` on a
    daemon thread that sleeps ``WEB_PUSH_NOTIFICATION_DELAY_SECONDS`` and only then
    opens its own connection — long after the test's isolated home is gone, so it
    raises ``no such table: messages`` into an unhandled-thread warning attributable
    to no test. Scheduling is still exercised; only the delayed send is stubbed.

    Returns the payloads that WOULD have been pushed.
    """

    import core.web_push_notifications as web_push_notifications

    pushed: list[dict] = []
    monkeypatch.setattr(
        web_push_notifications, "_send_to_enabled_subscriptions", pushed.append
    )
    return pushed


def test_a_workbench_addressed_notice_lands_as_a_durable_inbox_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-079 — the workbench rungs of D5's ladder were never reachable at all.

    ``_failure_notice_targets._add`` parsed EVERY rung with ``parse_session_key``,
    which rejects any scope type outside ``{channel, user}``. Every avibe rung is
    ``avibe::project::…``: rung (2) for any workbench-bound session (
    ``resolve_session_id_target`` returns a ``project`` scope for one), rung (3) for
    a workbench-created definition, and rung (5) always. ``_add`` swallowed the
    ``ValueError`` and returned, so for an Avibe-only definition the ENTIRE ladder
    was empty and its notice could only ever dead-letter.

    Driven through the real drain against the real workbench DB, because the payoff
    is the durable row: the workbench notice is delivered by being PERSISTED (the
    inbox reads rows, and an SSE fan-out with no browser attached reaches nobody),
    so "one visible avibe rung" and "one ``messages`` row the user can read back
    later" are the same claim.
    """

    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT

    pushed = _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    scope_id = _workbench_session("sesWork", project="proj-notice")

    sqlite, requests = _store(tmp_path)
    # No delivery key and no caller provenance: rungs (1), (3) and (4) are empty by
    # construction, so only the workbench-addressed rungs can carry this notice.
    _task(sqlite, "task-workbench", name="nightly report", session_id="sesWork")
    run = requests.enqueue_task_run("task-workbench")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-workbench")
    assert sqlite.owed_failure_notice(run.id)["state"] == "pending"

    service = _drain_service(tmp_path, controller, sqlite, requests)

    rungs = service._failure_notice_targets(sqlite.get_run(run.id))
    assert [target.to_key() for target, _ in rungs] == [
        "avibe::project::proj-notice",
        "avibe::project::sesWork",
    ], f"a project-scoped rung must survive parsing: {rungs}"

    asyncio.run(service._drain_failure_notices())

    rows = _persisted_messages()
    assert [(row["platform"], row["type"]) for row in rows] == [("avibe", "notify")], (
        f"the workbench notice must exist as one durable row: {rows}"
    )
    assert rows[0]["scope_id"] == scope_id, "the row must land in the session's project scope"
    assert rows[0]["session_id"] == "sesWork"
    assert rows[0]["content_text"], "an empty notice is not a notice"

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == NOTICE_SENT
    assert notice["ack_evidence"] == ACK_EVIDENCE_RECEIPT, (
        "a workbench rung may only ack on the persisted receipt"
    )
    assert notice["attempts"] == 1

    # The row is inbox-visible, so it is also push-notifiable — the notice reaches a
    # user who is not looking at the tab, not just the transcript.
    assert [payload["session_id"] for payload in pushed] == ["sesWork"], (
        f"the workbench notice must be pushed, not only stored: {pushed}"
    )


def test_an_avibe_rung_does_not_ack_on_a_synthetic_send_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-079 — the trap a bare parser swap walks straight into.

    ``AvibeBot.send_message`` mints and returns a synthetic ``msg_<hex>`` id
    unconditionally — with no SSE subscriber, and whether or not anything was
    persisted. The dispatcher records that as ``delivered_id``, and
    ``DeliveryEvidence.delivered`` is true on ``delivered_id`` ALONE
    (``delivery_only``). So merely admitting project rungs into the ladder would
    convert today's visible dead letter into a permanent, FALSE ``sent`` for any
    avibe rung that persisted nothing: ``persist_agent_message``'s avibe branch
    returns before writing when it can resolve neither a scope nor a session row.

    Two rungs, and the ordering is the whole test:

    * rung (3), the caller's project — a workbench provenance whose project has no
      session row here, so nothing durable can be written. It MUST NOT ack.
    * rung (5), through the run's own session — whose row still exists even though
      it is ARCHIVED (``_session_row`` has no status filter, while rung (2)'s
      ``resolve_session_id_target`` refuses an archived session outright). This is
      the real delta between the two session rungs, and it receipts.

    Under a bare swap this test sees ONE send, ZERO durable rows and a
    ``delivery_only`` ack. It also pins the per-rung evidence: one shared
    ``DeliveryEvidence`` latches ``delivered`` true forever once any rung sets an
    id, so rung (3) would both stop the walk and hand the final ack the wrong
    ``ack_evidence``.
    """

    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT

    _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    scope_id = _workbench_session("sesArchived", project="proj-live", status="archived")

    sqlite, requests = _store(tmp_path)
    _task(
        sqlite,
        "task-avibe-ack",
        name="nightly report",
        session_id="sesArchived",
        metadata={"created_by": {"caller": {"scope_id": "avibe::project::proj-gone"}}},
    )
    run = requests.enqueue_task_run("task-avibe-ack")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-avibe-ack")

    service = _drain_service(tmp_path, controller, sqlite, requests)

    rungs = service._failure_notice_targets(sqlite.get_run(run.id))
    assert [target.to_key() for target, _ in rungs] == [
        "avibe::project::proj-gone",
        "avibe::project::sesArchived",
    ], f"an archived session keeps rung (5) and loses rung (2): {rungs}"

    asyncio.run(service._drain_failure_notices())

    channels = [channel for channel, _thread, _text in controller.im_client.sent]
    assert channels == ["proj-gone", "sesArchived"], (
        "a synthetic send id must not end the walk; the next rung has to be tried: "
        f"{channels}"
    )

    rows = _persisted_messages()
    assert [row["session_id"] for row in rows] == ["sesArchived"], (
        f"only the rung that persisted anything counts as delivered: {rows}"
    )
    assert rows[0]["scope_id"] == scope_id

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == NOTICE_SENT
    assert notice["ack_evidence"] == ACK_EVIDENCE_RECEIPT, (
        "the ack must carry the WINNING rung's evidence, not the rejected rung's"
    )


def test_a_replayed_notice_does_not_finalize_a_live_unrelated_turn(tmp_path: Path) -> None:
    """HFR-080 — a receipt about an old run must not mutate the current turn.

    ``emit_backend_failure`` sends the notice and then emits its usual TERMINAL
    result (``terminal_turn_output()`` → ``completes_turn=True``). For a replayed
    notice the context describes a run that ended hours ago and carries no
    runtime-turn token, so the fail-open guard adopts the channel's CURRENT turn:
    delivering the notice collapses a live status bubble, clears consolidated
    state, signals turn-complete and releases the runtime turn — in a conversation
    that has nothing to do with the failed run.

    That is strictly worse than the gap PR6 exists to close: it trades a silent
    failure for a visible one in someone else's turn.

    Driven through the DRAIN rather than through ``emit_backend_failure``, because
    the mechanism to avoid this already exists — ``emit_backend_failure`` honours an
    explicit non-settling ``output``. The defect is that the drain never supplied
    one, so that is what the test exercises.
    """

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    controller, _dispatcher, touched = _live_turn_dispatcher()

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-live", deliver_key="slack::channel::C123")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()
    run = requests.enqueue_task_run("task-live")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-live")

    controller.platform_settings_managers = {}
    controller.session_turn_gate = None

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service._drain_dirty = False
    service.controller = controller
    service._owns_service_instance = lambda: True
    service.validate_platform = lambda platform: None
    service._t = lambda key, **kwargs: key

    asyncio.run(service._drain_failure_notices())

    assert touched == [], f"a replayed notice mutated the live turn: {touched}"
    # The notice itself must still have been delivered — this is not a fix by silence.
    assert controller.im_client.sent, "the notice must still reach the user"


def test_a_live_failure_still_settles_its_own_turn() -> None:
    """The non-settling output is opt-in, so the live failure path is unchanged.

    Only the replay declines to settle. A backend failure reported as it happens
    still owns its turn, or every real 401 would leave a spinner running forever.
    """

    from core.backend_failure import emit_backend_failure
    from modules.im import MessageContext

    controller, _dispatcher, touched = _live_turn_dispatcher()
    live_context = MessageContext(
        user_id="u1",
        channel_id="C123",
        platform="slack",
        platform_specific={"turn_token": "tok-1"},
    )

    asyncio.run(
        emit_backend_failure(controller, live_context, "codex", "boom", display_text="it broke")
    )

    assert "signal_turn_complete" in touched, "the live path must still settle its turn"


def test_the_drain_reuses_the_identity_the_live_path_would_have_used(tmp_path: Path) -> None:
    """HFR-081 — a stamped notice must dedupe against the live notification.

    The live path keys its failure notification through ``_failure_identity``, which
    prefers ``task_execution_id`` — and ``_build_context`` sets that to the run id.
    Stamping ``failure:{run_id}`` instead produced a DIFFERENT
    ``native_message_id``, so the drain's ``agent_message_exists`` lookup could not
    see the message the live path had already persisted and sent a second
    notification for the same failure.

    This is the mirror of the duplicate-short-circuit defect: there the drain could
    not see its OWN receipt, here it cannot see the live path's.
    """

    from core.backend_failure import backend_failure_notification_output
    from modules.im import MessageContext

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-dedup", deliver_key="slack::channel::C1")
    run = requests.enqueue_task_run("task-dedup")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="boom", task_id="task-dedup")

    # What the LIVE path produces, from a context built the way ``_build_context``
    # builds one for this run.
    live_context = MessageContext(
        user_id="scheduled",
        channel_id="C1",
        platform="slack",
        platform_specific={"task_execution_id": run.id, "task_trigger_kind": "scheduled"},
    )
    live_identity = backend_failure_notification_output(live_context, "harness").idempotency_key

    # What the DRAIN stamped on the durable row.
    notice = sqlite.owed_failure_notice(run.id)
    drained_context = MessageContext(user_id="scheduled", channel_id="C1", platform="slack")
    drained_identity = backend_failure_notification_output(
        drained_context, "harness", failure_id=notice["failure_id"]
    ).idempotency_key

    assert drained_identity == live_identity, (
        "the drain would post a second notification for a failure already delivered"
    )


def test_an_interruption_keeps_an_identity_distinct_from_an_ordinary_failure(
    tmp_path: Path,
) -> None:
    """HFR-082 — the interruption notice must NOT collide with the ordinary one.

    Reusing the live identity is right for an ordinary failure, and wrong for an
    interruption: a run terminalized out of band may already have an ordinary
    backend-failure notice against ``task_execution_id``, and a colliding identity
    would let the dedup silently swallow the D1 notice that a deploy killed the run.
    """

    from core.backend_failure import backend_failure_notification_output
    from modules.im import MessageContext

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-interrupt")
    run = requests.enqueue_task_run("task-interrupt")
    requests.claim(run.id)
    sqlite.settle_run_terminal(
        run.id,
        terminal_status="failed",
        error="interrupted by a restart",
        metadata={"interrupt_reason": "restarted"},
    )

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["interrupt_reason"] == "restarted"

    context = MessageContext(user_id="scheduled", channel_id="C1", platform="slack")
    interrupt_identity = backend_failure_notification_output(
        context, "harness", failure_id=notice["failure_id"]
    ).idempotency_key
    ordinary_identity = backend_failure_notification_output(
        context, "harness", failure_id=run.id
    ).idempotency_key

    assert interrupt_identity != ordinary_identity
    assert run.id in notice["failure_id"] and "restarted" in notice["failure_id"], (
        "the identity must stay derivable from the durable run row"
    )


def test_the_owed_notice_lookup_seeks_rather_than_scans(tmp_path: Path) -> None:
    """HFR-083 — the SQL bound must actually bound the work.

    ``LIMIT`` plus in-SQL predicates moved the JSON decoding from Python into SQLite
    without bounding it: the planner still had to seek only on ``status='failed'``
    and then evaluate the unindexed ``json_valid``/``json_extract`` terms on every
    failed row before concluding that no eligible rows exist. Measured on the
    previous revision, the plan was::

        SEARCH agent_runs USING INDEX ix_agent_runs_status_created (status=?)
        USE TEMP B-TREE FOR LAST TERM OF ORDER BY

    — and the temp sort is the second half of the problem, because it defeats the
    ``LIMIT``'s early exit: SQLite must order the whole failed set before returning
    ten rows.

    Asserted on the query plan, which is what "bounded" actually means here, rather
    than on wall-clock time.
    """

    import sqlite3

    from sqlalchemy import event

    sqlite_store, requests = _store(tmp_path)
    _task(sqlite_store, "task-plan")
    run = requests.enqueue_task_run("task-plan")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="boom", task_id="task-plan")

    captured: list[tuple[str, Any]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "agent_runs" in statement and statement.strip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    event.listen(sqlite_store.engine, "before_cursor_execute", _capture)
    try:
        sqlite_store.list_owed_failure_notices(limit=10, now="2026-07-27T12:00:00+00:00")
    finally:
        event.remove(sqlite_store.engine, "before_cursor_execute", _capture)

    statement, parameters = captured[-1]
    raw = sqlite3.connect(str(tmp_path / "state" / "vibe.sqlite"))
    try:
        plan = [row[-1] for row in raw.execute("EXPLAIN QUERY PLAN " + statement, parameters)]
    finally:
        raw.close()

    rendered = "\n".join(plan)
    assert OWED_NOTICE_INDEX in rendered, (
        f"the eligibility lookup must seek on {OWED_NOTICE_INDEX}; plan was:\n{rendered}"
    )
    assert "SCAN" not in rendered, f"the lookup must not scan agent_runs; plan was:\n{rendered}"
    # The index also supplies (created_at, id) order, so the LIMIT can short-circuit
    # instead of sorting the whole failed history first.
    assert "TEMP B-TREE" not in rendered, (
        f"the ORDER BY must be served by the index, not a temp sort; plan was:\n{rendered}"
    )


def test_a_malformed_metadata_row_is_still_writable_under_the_expression_index(
    tmp_path: Path,
) -> None:
    """HFR-084 — the index expression must not reject rows at write time.

    A bare ``json_extract`` in an index expression is evaluated on every INSERT and
    UPDATE, so one malformed blob would make the row UNWRITABLE — converting a
    read-side degradation into a write-side outage. The ``CASE json_valid`` guard
    short-circuits, so the expression is never evaluated on invalid JSON.
    """

    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    sqlite_store, requests = _store(tmp_path)
    _task(sqlite_store, "task-badjson")
    run = requests.enqueue_task_run("task-badjson")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="boom", task_id="task-badjson")

    # Must not raise.
    with sqlite_store.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs).where(agent_runs.c.id == run.id).values(metadata_json="{not json")
        )

    assert sqlite_store.get_run(run.id)["status"] == "failed"
    assert sqlite_store.list_owed_failure_notices() == []


# --- group 2f: fairness and boundedness of the drain batch -----------------


def _pending_failure(sqlite_store, run_id: str, definition_id: str, *, created_at: str, notice: dict) -> None:
    """A settled failure row carrying a hand-built owed notice."""

    sqlite_store.enqueue_run(
        {
            "id": run_id,
            "request_type": "scheduled",
            "status": "failed",
            "definition_id": definition_id,
            "error": "boom",
            "created_at": created_at,
            "completed_at": created_at,
            "metadata": {"owed_failure_notice": notice},
        }
    )


def test_one_backed_off_definition_does_not_starve_every_other_notice(tmp_path: Path) -> None:
    """HFR-085 — a deferred streak must not occupy the global batch forever.

    When one definition has more than ten pending failures and its canonical notice
    is in backoff, the canonical is excluded by the ``next_attempt_at`` predicate
    while its LATER streak rows fill the limit of ten. Those rows are deferred
    without being changed, so every two-second tick selects the same ten rows again
    and never reaches another definition's notice — or an interruption's.

    A definition whose delivery ladder never works repeats this through each
    promoted canonical, starving unrelated notices indefinitely. This is the worst
    shape of defect for this PR: it makes an arbitrary subset of failures
    permanently invisible, with no error, no log, and a drain that looks healthy
    because it is busy.
    """

    from types import SimpleNamespace

    import core.scheduled_tasks as scheduled_tasks
    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-noisy", deliver_key="slack::channel::C1")
    _task(sqlite, "task-quiet", deliver_key="slack::channel::C2")

    # The noisy definition's canonical is mid-backoff and therefore not eligible.
    _pending_failure(
        sqlite,
        "run-noisy-canonical",
        "task-noisy",
        created_at="2026-07-27T00:00:00+00:00",
        notice={
            "state": "pending",
            "attempts": 2,
            "next_attempt_at": "2099-01-01T00:00:00+00:00",
            "failure_id": "run-noisy-canonical",
        },
    )
    # ...and it is followed by more than one batch worth of streak rows.
    for index in range(15):
        _pending_failure(
            sqlite,
            f"run-noisy-{index:02d}",
            "task-noisy",
            created_at=f"2026-07-27T01:{index:02d}:00+00:00",
            notice={
                "state": "pending",
                "attempts": 0,
                "next_attempt_at": None,
                "failure_id": f"run-noisy-{index:02d}",
            },
        )
    # A completely unrelated definition owes exactly one notice.
    _pending_failure(
        sqlite,
        "run-quiet",
        "task-quiet",
        created_at="2026-07-27T02:00:00+00:00",
        notice={
            "state": "pending",
            "attempts": 0,
            "next_attempt_at": None,
            "failure_id": "run-quiet",
        },
    )

    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()

    delivered: list[str] = []

    async def _spy_emit(controller, context, backend, diagnostic, **kwargs):
        delivered.append(str(kwargs.get("failure_id")))
        evidence = kwargs.get("delivery")
        if evidence is not None:
            evidence.delivered_id = "m1"
            evidence.persisted_row = {"id": "m1"}
        return False

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", _spy_emit)
        service = ScheduledTaskService.__new__(ScheduledTaskService)
        service.store = store
        service.request_store = requests
        service._drain_dirty = False
        service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
        service._owns_service_instance = lambda: True
        service.validate_platform = lambda platform: None
        service._t = lambda key, **kwargs: key
        # A handful of ticks — far more than the one the quiet notice should need.
        for _ in range(4):
            asyncio.run(service._drain_failure_notices())

    assert sqlite.owed_failure_notice("run-quiet")["state"] == "sent", (
        "an unrelated definition's notice was starved by a deferred streak; "
        f"delivered={delivered}"
    )


def test_the_eligibility_lookup_is_bounded_when_everything_is_backed_off(
    tmp_path: Path,
) -> None:
    """HFR-086 — the backoff term must be CONSTRAINED by the index, not filtered.

    The previous index was the state expression followed by ``(created_at, id)``, so
    SQLite reported the index as used and avoided the temp sort — satisfying the
    plan test exactly — while still walking every pending-state entry to evaluate
    the unindexed future-backoff condition. With many notices waiting on retry and
    none eligible, the tick was again unbounded in the number of pending notices.

    The plan test is therefore strengthened to assert the backoff term is
    constrained, not merely that an index is named. ``EXPLAIN QUERY PLAN`` reports
    constrained terms explicitly — ``(<expr>=? AND <expr><?)`` — so this is
    assertable directly rather than through a proxy.
    """

    import sqlite3

    from sqlalchemy import event

    sqlite_store, _requests = _store(tmp_path)
    _task(sqlite_store, "task-backoff")
    for index in range(40):
        _pending_failure(
            sqlite_store,
            f"run-backoff-{index:03d}",
            "task-backoff",
            created_at=f"2026-07-27T00:{index:02d}:00+00:00",
            notice={
                "state": "pending",
                "attempts": 3,
                "next_attempt_at": "2099-01-01T00:00:00+00:00",
                "failure_id": f"run-backoff-{index:03d}",
            },
        )

    captured: list[tuple[str, Any]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "agent_runs" in statement and statement.strip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    event.listen(sqlite_store.engine, "before_cursor_execute", _capture)
    try:
        assert sqlite_store.list_owed_failure_notices(now="2026-07-27T12:00:00+00:00") == []
    finally:
        event.remove(sqlite_store.engine, "before_cursor_execute", _capture)

    statement, parameters = captured[-1]
    raw = sqlite3.connect(str(tmp_path / "state" / "vibe.sqlite"))
    try:
        plan = [row[-1] for row in raw.execute("EXPLAIN QUERY PLAN " + statement, parameters)]
    finally:
        raw.close()
    rendered = "\n".join(plan)

    assert OWED_NOTICE_INDEX in rendered, f"plan was:\n{rendered}"
    # TWO constrained terms. The previous index produced ``(<expr>=?)`` — state
    # only — which is what let the backoff walk stay unbounded while every other
    # assertion in the old test still held.
    assert "AND" in rendered.split(OWED_NOTICE_INDEX, 1)[1].split("\n")[0], (
        f"the backoff term must be constrained by the index, not filtered per row; plan was:\n{rendered}"
    )


def test_the_drain_and_a_backend_supplied_identity_agree_for_harness_runs(
    tmp_path: Path,
) -> None:
    """HFR-087 — the ordinary/interruption split still collided on a third path.

    Codex and OpenCode pass an explicit ``failure_id`` (the Codex ``turn_id``, an
    OpenCode message/session id) at five call sites, and ``_failure_identity``
    prioritises an explicit value over ``task_execution_id``. For a scheduled run on
    those backends the live notification is keyed by the backend's id while the
    drain stamps the run id, so the drain cannot find the already-persisted live
    notification and the user gets a duplicate.

    Aligning them cannot simply mean "explicit always wins" either, because the
    drain's own ``interrupt:{run}:{reason}`` is an explicit id that MUST stay
    distinct from the ordinary one.
    """

    from core.backend_failure import backend_failure_notification_output
    from modules.im import MessageContext

    harness_context = MessageContext(
        user_id="scheduled",
        channel_id="C1",
        platform="slack",
        platform_specific={"task_execution_id": "run-abc", "task_trigger_kind": "scheduled"},
    )

    # What Codex produces live for a SCHEDULED run: an explicit backend turn id.
    live = backend_failure_notification_output(
        harness_context, "codex", failure_id="codex-turn-77"
    ).idempotency_key
    # What the drain stamps and replays.
    replay = backend_failure_notification_output(
        harness_context, "codex", failure_id="run-abc", failure_id_authoritative=True
    ).idempotency_key

    assert live == replay, "the drain would duplicate a notification Codex already sent"

    # A non-harness context has no run to align to, so the backend's id still wins.
    plain_context = MessageContext(user_id="u", channel_id="C1", platform="slack")
    plain = backend_failure_notification_output(
        plain_context, "codex", failure_id="codex-turn-77"
    ).idempotency_key
    assert "codex-turn-77" in plain

    # And an interruption stays distinct from the ordinary identity for the same run.
    interrupt = backend_failure_notification_output(
        harness_context,
        "codex",
        failure_id="interrupt:run-abc:restarted",
        failure_id_authoritative=True,
    ).idempotency_key
    assert interrupt != live


def test_the_drain_preserves_an_authoritative_interruption_identity(tmp_path: Path) -> None:
    """HFR-091 — HFR-087's alignment must not eat the interruption identity.

    ``_failure_identity`` honours an explicit id outright only when the caller says
    it is AUTHORITATIVE; otherwise the harness run-id override wins, which is what
    aligns a backend's own turn id with the drain's replay (HFR-087). The drain
    passed its explicit ``failure_id`` without that flag, so ``interrupt:{run}:{reason}``
    — stamped precisely so a D1 interruption cannot be swallowed by the dedup for an
    ordinary failure on the same execution — collapsed back to the bare run id at
    the one call site that reads it off a durable row.

    HFR-082 asserts the two identities differ when the flag is set, and HFR-087 that
    the ordinary lane still aligns. Neither could see this, because both call
    ``backend_failure_notification_output`` directly. This one drives the REAL drain
    and reads the identity off the row a user keeps, which is why the authority is
    now a property of ``emit_replayed_backend_failure`` rather than an argument any
    call site can forget.
    """

    _migrated_state_db()
    controller, _dispatcher, touched = _live_turn_dispatcher()

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-interrupt-drain", deliver_key="slack::channel::C123")
    run = requests.enqueue_task_run("task-interrupt-drain")
    requests.claim(run.id)
    assert "restarted" in RUN_INTERRUPTION_REASONS
    sqlite.settle_run_terminal(
        run.id,
        terminal_status="failed",
        error="interrupted by a restart",
        metadata={"interrupt_reason": "restarted"},
    )

    expected_failure_id = f"interrupt:{run.id}:restarted"
    notice = sqlite.owed_failure_notice(run.id)
    assert notice["failure_id"] == expected_failure_id

    service = _drain_service(tmp_path, controller, sqlite, requests)
    emissions = _spy_emissions(controller)

    asyncio.run(service._drain_failure_notices())

    assert sqlite.owed_failure_notice(run.id)["state"] == NOTICE_SENT
    visible = [item for item in emissions if item["type"] == "notify"]
    assert len(visible) == 1
    assert visible[0]["output"].idempotency_key == f"backend-failure:{expected_failure_id}", (
        "the drain's interruption identity collapsed into the ordinary failure's"
    )

    # And on the durable row, which is what the dedup lookup and the D1 notice's
    # survival actually depend on.
    rows = _persisted_messages()
    assert len(rows) == 1
    assert rows[0]["native_message_id"].endswith(f"backend-failure:{expected_failure_id}")
    assert f'"failure_id": "{expected_failure_id}"' in rows[0]["metadata_json"]
    assert touched == []


def test_a_suppressed_lane_reason_never_takes_the_interruption_identity_or_copy(
    tmp_path: Path,
) -> None:
    """HFR-092 — ``interrupt_reason`` presence is not the interruption lane.

    ``interrupt_reason`` is the general marker for "terminalized by something other
    than its own backend result", and most values it carries are ordinary per-fire
    verdicts: ``no_terminal_result``, ``refused_concurrent_turn``,
    ``transport_unavailable``, ``queue_hold_expired``. Those recur on every fire and
    belong in the SUPPRESSED failure lane — which is exactly what
    ``failure_notices.is_interruption`` says, by membership in
    ``RUN_INTERRUPTION_REASONS``.

    Two other places asked the question by PRESENCE instead:

    * the stamp (``_owed_failure_notice_for_transition``) minted
      ``interrupt:{run}:{reason}`` for any non-empty reason, and
    * the copy (``_failure_notice_body``) rendered the "was interrupted, nothing is
      wrong with the definition itself" headline for any non-empty reason.

    Both are wrong for the commonest failures, and the identity half is now
    load-bearing: the drain's ``failure_id`` is AUTHORITATIVE (HFR-091), so an id
    the live path never used is an id the dedup cannot match — one duplicate
    notification per suppressed-lane failure. The copy half tells the user their
    definition is fine when it is the definition that is broken.
    """

    from types import SimpleNamespace

    from core import failure_notices
    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-suppressed", name="daily report", deliver_key="slack::channel::C1")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()

    run = requests.enqueue_task_run("task-suppressed")
    requests.claim(run.id)
    assert "no_terminal_result" not in RUN_INTERRUPTION_REASONS
    sqlite.settle_run_terminal(
        run.id,
        terminal_status="failed",
        error="the turn ended without dispatching an agent",
        metadata={"interrupt_reason": "no_terminal_result"},
    )

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["interrupt_reason"] == "no_terminal_result", (
        "the reason itself is still recorded — it is a copy selector, not a lane"
    )
    # The lane, from the one predicate that decides it.
    assert failure_notices.is_interruption(notice) is False
    assert notice["failure_id"] == run.id, (
        "a suppressed-lane failure must carry the identity the live path uses, or the "
        "authoritative replay duplicates a notification already delivered"
    )

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
    service._t = lambda key, **kwargs: key

    body = service._failure_notice_body(sqlite.get_run(run.id), notice)
    assert "harness.notice.failed" in body
    assert "harness.notice.interrupted" not in body, (
        "a recurring per-fire verdict must not be reported as an out-of-band interruption"
    )


def test_the_drain_and_the_live_path_agree_for_a_suppressed_lane_reason(
    tmp_path: Path,
) -> None:
    """HFR-093 — the F1 × F3 interaction, pinned where it actually bites.

    HFR-091 made the drain's stamped ``failure_id`` authoritative, which is right and
    turns the presence-based stamp into a user-visible duplicate: a
    ``no_terminal_result`` failure is stamped ``interrupt:{run}:no_terminal_result``,
    the live path keys the same failure by the run id, and the drain's
    ``agent_message_exists`` lookup can no longer see the notification the live path
    already persisted.

    So this drives BOTH paths over one real failure and counts the durable rows the
    user would see. Neither HFR-081 (identity equality, computed directly) nor
    HFR-091 (interruption identity survives) can see it: one never runs the drain,
    the other uses a reason that IS in the interruption set.
    """

    from core.backend_failure import emit_backend_failure
    from core.scheduled_tasks import parse_session_key

    _migrated_state_db()
    controller, _dispatcher, _touched = _live_turn_dispatcher()

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-suppressed-dedup", deliver_key="slack::channel::C123")
    run = requests.enqueue_task_run("task-suppressed-dedup")
    requests.claim(run.id)
    sqlite.settle_run_terminal(
        run.id,
        terminal_status="failed",
        error="the turn ended without dispatching an agent",
        metadata={"interrupt_reason": "no_terminal_result"},
    )
    assert sqlite.owed_failure_notice(run.id)["state"] == "pending"

    service = _drain_service(tmp_path, controller, sqlite, requests)

    # The LIVE notification, through the context the harness itself builds for this
    # run — the same builder the drain's replay goes through, so nothing about the
    # delivery target or the persisted scope differs between the two paths. What is
    # being compared is the IDENTITY each one keys its notification by.
    target = parse_session_key("slack::channel::C123")
    live_context = asyncio.run(
        service._build_context(
            target,
            delivery_target=target,
            execution_id=run.id,
            task_id="task-suppressed-dedup",
            trigger_kind="scheduled",
        )
    )
    asyncio.run(
        emit_backend_failure(
            controller,
            live_context,
            "harness",
            "the turn ended without dispatching an agent",
            display_text="the live notice",
        )
    )
    live_rows = [
        row for row in _persisted_messages() if "backend-failure:" in (row["native_message_id"] or "")
    ]
    assert len(live_rows) == 1, f"the live path must have told the user once: {live_rows}"

    # …and then the drain replays the same failure off the durable row.
    asyncio.run(service._drain_failure_notices())

    rows = [
        row for row in _persisted_messages() if "backend-failure:" in (row["native_message_id"] or "")
    ]
    assert len(rows) == 1, (
        "the drain sent a SECOND notification for a failure the live path already "
        f"delivered: {[row['native_message_id'] for row in rows]}"
    )
    assert rows[0]["native_message_id"].endswith(f"backend-failure:{run.id}")


def test_the_index_migration_and_the_query_use_the_same_expressions(tmp_path: Path) -> None:
    """HFR-088 — index/query drift is silent, so it needs its own assertion.

    The planner matches an index expression to a query expression by structure. A
    one-character difference makes it decline, and nothing reports that: the index
    is built, ignored, and the query keeps returning correct results at scan cost.
    That happened twice while fixing this — once from bound parameters, once from
    editing only one of the two copies.

    The migration cannot import the store (migrations must stay stable against a
    moving codebase), so the strings are duplicated on purpose and pinned here —
    the same mirror discipline ``SWEEP_I18N_KEYS`` already uses.
    """

    from importlib import import_module

    from storage.background import OWED_NOTICE_NEXT_ATTEMPT_SQL, OWED_NOTICE_STATE_SQL

    migration = import_module(
        "storage.alembic.versions.20260728_0041_agent_runs_owed_notice_backoff_index"
    )

    assert migration._STATE_EXPR == OWED_NOTICE_STATE_SQL
    assert migration._NEXT_ATTEMPT_EXPR == OWED_NOTICE_NEXT_ATTEMPT_SQL


def test_a_notice_stamped_without_a_backoff_field_is_still_reachable(tmp_path: Path) -> None:
    """HFR-089 — a null backoff must read as "eligible now", not as unreachable.

    Making the backoff a pure ``<= now`` range term is what lets the index constrain
    it, but a NULL never satisfies ``<=``. Any notice stamped before that field
    existed — or by any writer that leaves it unset — would silently drop out of the
    drain forever. ``coalesce(..., '')`` inside the indexed expression keeps it a
    range term AND keeps those rows visible, because the empty string sorts before
    every ISO instant.
    """

    sqlite_store, _requests = _store(tmp_path)
    _task(sqlite_store, "task-legacy")
    _pending_failure(
        sqlite_store,
        "run-legacy",
        "task-legacy",
        created_at="2026-07-27T00:00:00+00:00",
        notice={"state": "pending", "attempts": 0, "next_attempt_at": None, "failure_id": "run-legacy"},
    )

    owed = sqlite_store.list_owed_failure_notices(now="2026-07-27T12:00:00+00:00")

    assert [item["id"] for item in owed] == ["run-legacy"]


# --- group 2g: the streak read itself --------------------------------------
#
# ``failure_streak`` runs once per pending notice on the same two-second tick as
# the eligibility lookup, so it is under the same bound — and it was the last read
# in this path that was not. The oracle below is the algorithm it used to be:
# ``_definition_verdict_rows`` materialised a definition's ENTIRE settled lifetime
# and sliced the streak out of it in Python. Kept here rather than deleted so the
# SQL replacement has something to be equal to.


def _materialized_streak(
    sqlite_store: SQLiteBackgroundTaskStore, definition_id: str, run_id: str
) -> list[dict]:
    """The pre-SQL streak, computed by materialising the whole definition.

    Byte-for-byte the production algorithm as of ``4558fc35`` — the lifetime
    ``SELECT`` ordered by ``(created_at, id)``, the per-row ``interrupt_reason``
    decode, and the two Python scans outward from the run. It is the specification
    the SQL has to reproduce, including its edge cases.
    """

    import storage.background as background
    from sqlalchemy import or_ as sa_or, select as sa_select
    from storage.background import (
        OWED_FAILURE_NOTICE_KEY as NOTICE_KEY,
        _status_query_values,
        normalize_run_status,
    )
    from storage.models import agent_runs

    verdicts = _status_query_values("succeeded") + _status_query_values("failed")
    stmt = (
        sa_select(agent_runs)
        .where(agent_runs.c.definition_id == definition_id)
        .where(sa_or(agent_runs.c.run_type.is_(None), agent_runs.c.run_type != "watch_runtime"))
        .where(agent_runs.c.status.in_(verdicts))
        .order_by(agent_runs.c.created_at, agent_runs.c.id)
    )
    rows: list[dict] = []
    with sqlite_store.engine.connect() as conn:
        for row in conn.execute(stmt).mappings():
            metadata = background._json_loads(row["metadata_json"], {})
            metadata = metadata if isinstance(metadata, dict) else {}
            reason = str(metadata.get("interrupt_reason") or "").strip()
            if reason in RUN_INTERRUPTION_REASONS:
                continue
            rows.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "status": normalize_run_status(row["status"]),
                    "notice": metadata.get(NOTICE_KEY)
                    if isinstance(metadata.get(NOTICE_KEY), dict)
                    else None,
                }
            )
    index = next((position for position, row in enumerate(rows) if row["id"] == run_id), None)
    if index is None:
        return []
    start = index
    while start > 0 and rows[start - 1]["status"] == "failed":
        start -= 1
    end = index
    while end + 1 < len(rows) and rows[end + 1]["status"] == "failed":
        end += 1
    return rows[start : end + 1]


def _agent_run_query_plans(sqlite_store, db_path: Path, call) -> list[tuple[str, list[str]]]:
    """Every ``agent_runs`` SELECT one call issues, with its query plan."""

    import sqlite3

    from sqlalchemy import event

    captured: list[tuple[str, Any]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "agent_runs" in statement and statement.strip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    event.listen(sqlite_store.engine, "before_cursor_execute", _capture)
    try:
        call()
    finally:
        event.remove(sqlite_store.engine, "before_cursor_execute", _capture)

    plans: list[tuple[str, list[str]]] = []
    raw = sqlite3.connect(str(db_path))
    try:
        for statement, parameters in captured:
            plans.append(
                (statement, [str(row[-1]) for row in raw.execute("EXPLAIN QUERY PLAN " + statement, parameters)])
            )
    finally:
        raw.close()
    return plans


def _seed_streak_history(sqlite_store, definition_id: str, total: int) -> None:
    """A long settled history whose failures sit in short, closed streaks."""

    _task(sqlite_store, definition_id)
    for index in range(total):
        # Successes every seventh run, so the streak containing any given failure is
        # at most six rows long while the lifetime is ``total``.
        status = "succeeded" if index % 7 == 0 else "failed"
        instant = f"2026-07-01T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}+00:00"
        sqlite_store.enqueue_run(
            {
                "id": f"run-{index:05d}",
                "request_type": "scheduled",
                "status": status,
                "definition_id": definition_id,
                "error": "boom" if status == "failed" else None,
                "created_at": instant,
                "completed_at": instant,
                "metadata": {"owed_failure_notice": {"state": "pending", "attempts": 0}},
            }
        )


def test_the_streak_read_is_bounded_and_seeks_rather_than_scans(tmp_path: Path) -> None:
    """HFR-095 — the streak read runs on the 2 s tick and was unbounded.

    ``failure_streak`` is asked once per pending notice, every tick. It called
    ``_definition_verdict_rows``, which selected the definition's ENTIRE settled
    lifetime ordered by ``(created_at, id)``, materialised every row into Python and
    JSON-decoded each one to drop interruptions — then sliced out a streak that is
    almost always two or three rows. Measured on this 5000-row history, the plan
    was::

        SEARCH agent_runs USING INDEX ix_agent_runs_definition_created (definition_id=?)
        USE TEMP B-TREE FOR LAST TERM OF ORDER BY

    — one constrained term, an unindexed sort of the whole definition, and 5000
    metadata blobs decoded to return one row.

    Asserted on the CONSTRAINED TERMS of the plan, not on the index name: naming an
    index proves nothing (HFR-086 is the same lesson — the plan named the index
    while the term stayed a per-row filter). The bound the streak needs is the
    ``(created_at, id)`` position range, so that is what the plan has to say. The
    Python-side decode count is asserted alongside it, because that is the work the
    tick actually pays.
    """

    sqlite_store, _requests = _store(tmp_path)
    lifetime = 5000
    _seed_streak_history(sqlite_store, "task-streak-plan", lifetime)
    # A failure late in the history whose streak is closed by a success on BOTH
    # sides, so both boundary seeks have something to find.
    target = "run-04997"
    expected = [f"run-{index:05d}" for index in range(4992, 4998)]

    import storage.background as background
    from unittest.mock import patch

    decoded: list[int] = []
    real_json_loads = background._json_loads

    def _counting_json_loads(value, default):
        decoded.append(1)
        return real_json_loads(value, default)

    streak: list[dict] = []
    with patch.object(background, "_json_loads", _counting_json_loads):
        streak = sqlite_store.failure_streak("task-streak-plan", target)

    assert [row["id"] for row in streak] == expected
    assert len(decoded) <= 2 * len(streak) + 2, (
        f"decoded {len(decoded)} metadata blobs to return a {len(streak)}-run streak out of "
        f"{lifetime} rows — the streak read must be O(streak), not O(lifetime)"
    )

    plans = _agent_run_query_plans(
        sqlite_store,
        tmp_path / "state" / "vibe.sqlite",
        lambda: sqlite_store.failure_streak("task-streak-plan", target),
    )
    assert plans, "the streak read issued no agent_runs query"
    rendered = "\n".join(line for _statement, plan in plans for line in plan)
    compact = rendered.replace(" ", "")

    assert "SCAN" not in rendered, (
        f"the streak read must never scan a definition's history; plans were:\n{rendered}"
    )
    assert "TEMP B-TREE" not in rendered, (
        f"the (created_at, id) order must come from an index, not a sort; plans were:\n{rendered}"
    )
    # The two boundary seeks, by their constrained terms. A plan that constrains only
    # ``definition_id`` is the unbounded read this scenario exists to remove, and it
    # would satisfy every assertion above once the sort is indexed away.
    assert "(created_at,id)<(?,?)" in compact, (
        f"the preceding success must be found by an indexed seek; plans were:\n{rendered}"
    )
    assert "(created_at,id)>(?,?)" in compact, (
        f"the following success must be found by an indexed seek; plans were:\n{rendered}"
    )


def test_the_sql_streak_matches_the_materialized_streak(tmp_path: Path) -> None:
    """HFR-096 — the bounded read has to be the SAME streak, edge cases included.

    Parity against ``_materialized_streak``, the algorithm this replaces, over
    randomised histories containing every row class that decides the answer:
    successes and failures, ``canceled``/``queued`` rows that are not verdicts at
    all, interruptions whose reason IS in ``RUN_INTERRUPTION_REASONS`` (transparent:
    skipped, and neither joining nor closing a streak), interruptions whose reason is
    NOT (``no_terminal_result`` and friends — ordinary failures that DO join),
    ``watch_runtime`` heartbeats, and runs sharing one ``created_at`` so the ``id``
    tie-break decides ordering.

    Every run in the history is asked for, not just failures: the streak is defined
    relative to the run it is given, and a caller that hands it a succeeded or
    excluded row must get the same answer it got before.
    """

    import random

    outside_the_lane = ["no_terminal_result", "refused_concurrent_turn", "queue_hold_expired"]
    inside_the_lane = sorted(RUN_INTERRUPTION_REASONS)

    sqlite_store, _requests = _store(tmp_path)
    for seed in range(6):
        rng = random.Random(seed)
        definition_id = f"task-parity-{seed}"
        _task(sqlite_store, definition_id)
        ids: list[str] = []
        for index in range(40):
            run_id = f"run-{seed}-{index:03d}"
            ids.append(run_id)
            # A small pool of instants, so identical timestamps are common and the
            # ``id`` tie-break decides the sequence.
            instant = f"2026-07-27T00:{rng.randrange(6):02d}:00+00:00"
            status = rng.choice(["failed", "failed", "failed", "succeeded", "canceled", "queued"])
            metadata: dict[str, Any] = {}
            roll = rng.random()
            if roll < 0.2:
                metadata["interrupt_reason"] = rng.choice(inside_the_lane)
            elif roll < 0.4:
                metadata["interrupt_reason"] = rng.choice(outside_the_lane)
            if rng.random() < 0.2:
                metadata[OWED_FAILURE_NOTICE_KEY] = {
                    "state": rng.choice(["pending", "sent", "skipped", "failed"]),
                    "attempts": 1,
                }
            request_type = "watch_runtime" if rng.random() < 0.1 else "scheduled"
            sqlite_store.enqueue_run(
                {
                    "id": run_id,
                    "request_type": request_type,
                    "status": status,
                    "definition_id": definition_id,
                    "error": "boom" if status == "failed" else None,
                    "created_at": instant,
                    "completed_at": instant,
                    "metadata": metadata,
                }
            )

        for run_id in [*ids, "run-does-not-exist"]:
            expected = _materialized_streak(sqlite_store, definition_id, run_id)
            actual = sqlite_store.failure_streak(definition_id, run_id)
            assert actual == expected, (
                f"seed {seed}, run {run_id}: the SQL streak disagrees with the materialized one\n"
                f"  expected {[(row['id'], row['status']) for row in expected]}\n"
                f"  actual   {[(row['id'], row['status']) for row in actual]}"
            )


# --- group 2c: delivery evidence ------------------------------------------


def test_delivered_but_unpersisted_acks_on_the_delivery_id() -> None:
    """A returned message id is positive evidence the user was told.

    Re-sending because the DB write failed would spam a notice that already arrived.
    The ack records ``delivery_only`` rather than pretending a receipt exists.
    """

    from core.delivery_evidence import ACK_EVIDENCE_DELIVERY_ONLY, STAGE_PERSIST, DeliveryEvidence

    evidence = DeliveryEvidence(
        delivered_id="1717.42",
        persisted_row=None,
        error=RuntimeError("disk is full"),
        error_stage=STAGE_PERSIST,
    )

    assert evidence.delivered is True
    assert evidence.ack_evidence == ACK_EVIDENCE_DELIVERY_ONLY
    # The persistence exception's OWN message, not a generic string.
    assert "disk is full" in evidence.error_text


def test_a_send_that_returned_no_id_is_not_evidence_of_delivery() -> None:
    """The notice must stay pending and be retried, never marked sent."""

    from core.delivery_evidence import STAGE_SEND, DeliveryEvidence

    evidence = DeliveryEvidence(error=ConnectionError("slack is down"), error_stage=STAGE_SEND)

    assert evidence.delivered is False
    assert evidence.ack_evidence is None
    assert "slack is down" in evidence.error_text


def test_a_post_delivery_error_is_not_a_delivery_failure() -> None:
    """Send and persist succeeded; only the SSE fan-out raised.

    Under the old single ``try`` this returned ``None`` — indistinguishable from a
    failed send — so the drain would have re-sent a notice the user already had.
    """

    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT, STAGE_STREAM, DeliveryEvidence

    evidence = DeliveryEvidence(
        delivered_id="1717.42",
        persisted_row={"id": "msg-1"},
        error=RuntimeError("sink closed"),
        error_stage=STAGE_STREAM,
    )

    assert evidence.delivered is True
    assert evidence.ack_evidence == ACK_EVIDENCE_RECEIPT


def test_an_avibe_send_returning_none_still_acks_on_its_receipt() -> None:
    """avibe delivers over SSE, so the happy path returns ``None``.

    A drain acking on "the function returned an id" would never acknowledge a
    Workbench-targeted notice at all, and would re-send it every tick.
    """

    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT, DeliveryEvidence

    evidence = DeliveryEvidence(delivered_id=None, persisted_row={"id": "msg-1"}, send_returned=True)

    assert evidence.delivered is True
    assert evidence.ack_evidence == ACK_EVIDENCE_RECEIPT


def test_persist_agent_message_surfaces_its_caught_exception() -> None:
    """The error channel exists, and does NOT raise through the notify branch.

    Letting it raise would be caught by the branch's blanket ``except`` and discard
    the message id assigned on the line above, turning delivered-but-unpersisted
    into looks-like-never-delivered.
    """

    from unittest.mock import patch

    from core import message_mirror
    from modules.im import MessageContext

    context = MessageContext(user_id="u", channel_id="C1", platform="slack", platform_specific={"platform": "slack"})
    sink: list = []
    with patch.object(message_mirror, "get_cached_sqlite_engine", side_effect=RuntimeError("db is gone")):
        result = message_mirror.persist_agent_message(context, "notify", "boom", error_sink=sink)

    assert result is None
    assert len(sink) == 1 and "db is gone" in str(sink[0])


# --- group 3: derived health ----------------------------------------------


def test_definition_health_reaches_the_cli_list_after_a_success(capsys) -> None:
    """HFR-062 — one success must not erase days of failure on the CLI surface.

    ``last_run_at``/``last_error`` are both overwritten on every fire, so a task
    that failed three days running and then succeeded once reported a clean
    ``succeeded`` and nothing else.

    Asserted on ``health``/``recent_failures`` rather than on ``last_status``.
    #1061 demoted ``last_status`` to a compatibility field that never determines
    lifecycle, so an earlier revision of this test — which asserted
    ``last_status == "degraded"`` — was pinning new semantics onto a field the
    canonical projection is not supposed to read. Health is its own axis now.
    """

    import json

    from storage.background import SQLiteBackgroundTaskStore
    from storage.pagination import PageRequest
    from vibe import cli

    store = SQLiteBackgroundTaskStore()
    try:
        _task(store, "task-cli")
        for index in range(3):
            store.enqueue_run(
                {
                    "id": f"run-cli-fail-{index}",
                    "request_type": "scheduled",
                    "status": "failed",
                    "definition_id": "task-cli",
                    "error": "boom",
                    "created_at": f"2026-07-27T0{index}:00:00+00:00",
                    "completed_at": f"2026-07-27T0{index}:30:00+00:00",
                }
            )
        store.enqueue_run(
            {
                "id": "run-cli-ok",
                "request_type": "scheduled",
                "status": "succeeded",
                "definition_id": "task-cli",
                "created_at": "2026-07-27T04:00:00+00:00",
                "completed_at": "2026-07-27T04:30:00+00:00",
            }
        )
        # What ``mark_task_result`` leaves behind after that final success.
        store.upsert_scheduled_task(
            {
                "id": "task-cli",
                "name": "task-cli",
                "prompt": "go",
                "schedule_type": "cron",
                "cron": "0 * * * *",
                "enabled": True,
                "created_at": _EPOCH,
                "updated_at": _EPOCH,
                "last_run_at": "2026-07-27T04:30:00+00:00",
                "last_error": None,
            }
        )

        assert cli.cmd_task_list(page_request=PageRequest(limit=20)) == 0
    finally:
        store.close()

    entry = json.loads(capsys.readouterr().out)["definitions"][0]
    assert entry["recent_failures"] == 3, "a success downgrades, it does not erase"
    assert entry["health"] == "degraded"
    # And the demoted compatibility field keeps #1061's three-valued vocabulary.
    assert entry["last_status"] in {"failed", "succeeded", "never_run"}


def test_brief_task_payload_carries_last_error(capsys) -> None:
    """HFR-063 — ``vibe task list`` dropped ``last_error`` entirely.

    The brief payload is an explicit allowlist, so the one field that says WHY a
    task is failing never reached the list a user actually runs. It is forwarded
    with the health fields now, since it answers the question the badge raises.
    """

    import json

    from storage.background import SQLiteBackgroundTaskStore
    from storage.pagination import PageRequest
    from vibe import cli

    store = SQLiteBackgroundTaskStore()
    try:
        _task(store, "task-broken", last_error="unresolvable session binding", last_run_at=_EPOCH)
        assert cli.cmd_task_list(page_request=PageRequest(limit=20)) == 0
    finally:
        store.close()

    entry = json.loads(capsys.readouterr().out)["definitions"][0]
    assert entry["last_error"] == "unresolvable session binding"


def test_a_watch_reports_health_on_the_same_terms_as_a_task(capsys) -> None:
    """HFR-090 — a watch whose hook fails nightly is as invisible as a task.

    ``_enrich_definitions`` computes health for both definition types, so the only
    thing that decided whether it reached a surface was the projection allowlist.
    """

    import json

    from storage.background import SQLiteBackgroundTaskStore
    from storage.pagination import PageRequest
    from vibe import cli

    store = SQLiteBackgroundTaskStore()
    try:
        _watch(store, "watch-health")
        store.enqueue_run(
            {
                "id": "run-watch-health",
                "request_type": "watch",
                "status": "failed",
                "definition_id": "watch-health",
                "error": "hook blew up",
                "created_at": "2026-07-27T01:00:00+00:00",
                "completed_at": "2026-07-27T01:01:00+00:00",
            }
        )
        assert cli.cmd_watch_list(page_request=PageRequest(limit=20)) == 0
    finally:
        store.close()

    entry = json.loads(capsys.readouterr().out)["definitions"][0]
    assert entry["health"] == "failing"
    assert entry["consecutive_failures"] == 1


def test_storage_interruption_reasons_mirror_the_settlement_vocabulary() -> None:
    """HFR-064 — the two spellings of the interruption class may not drift.

    ``storage`` cannot import ``core``, so the set is declared in
    ``core.run_settlement`` and mirrored as literals in ``storage.background``,
    exactly as ``SWEEP_I18N_KEYS`` mirrors the sweep reasons. This is the test that
    keeps the mirror honest.
    """

    from core.run_settlement import RUN_INTERRUPTION_REASONS as core_reasons
    from storage.background import RUN_INTERRUPTION_REASONS as storage_reasons

    assert set(core_reasons) == set(storage_reasons)


def test_one_success_does_not_erase_recent_failure_history(tmp_path: Path) -> None:
    """HFR-066 — P6 itself: ``last_run_at``/``last_error`` are overwritten every fire.

    Three failed fires then one success leaves the definition indistinguishable
    from one that has never failed, because both source fields are single-valued.
    Health has to be derived from ``agent_runs``, which still holds all four rows.
    """

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-health")

    for index in range(3):
        sqlite.enqueue_run(
            {
                "id": f"run-fail-{index}",
                "request_type": "scheduled",
                "status": "failed",
                "definition_id": "task-health",
                "error": "boom",
                "created_at": f"2026-07-27T0{index}:00:00+00:00",
                "completed_at": f"2026-07-27T0{index}:30:00+00:00",
            }
        )
    sqlite.enqueue_run(
        {
            "id": "run-ok",
            "request_type": "scheduled",
            "status": "succeeded",
            "definition_id": "task-health",
            "created_at": "2026-07-27T04:00:00+00:00",
            "completed_at": "2026-07-27T04:30:00+00:00",
        }
    )

    health = sqlite.definition_health("task-health", now="2026-07-27T05:00:00+00:00")

    assert health["recent_failures"] == 3
    assert health["consecutive_failures"] == 0
    assert health["health"] == "degraded", "one success must downgrade, not erase"


def test_result_less_settlement_still_counts_toward_health(tmp_path: Path) -> None:
    """HFR-067 — an ``interrupt_reason`` is not by itself a D1 interruption.

    ``metadata.interrupt_reason`` is master's general marker for "terminalized by
    something other than its own backend result": ``_settle_agent_run_without_result``
    writes ``no_terminal_result`` / ``refused_concurrent_turn`` / ``backend_refresh``
    and ``sweep_stale_runs`` writes ``orphaned`` / ``transport_unavailable`` /
    ``queue_hold_expired``. Only the last group is out-of-band interruption; the
    rest are ordinary per-fire verdicts and are the COMMON P6 failure. A health
    predicate keyed on ``interrupt_reason IS NULL`` excludes all of them and
    reports a permanently broken definition as healthy.
    """

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-resultless")
    sqlite.enqueue_run(
        {
            "id": "run-resultless",
            "request_type": "scheduled",
            "status": "failed",
            "definition_id": "task-resultless",
            "error": "the turn ended without a terminal result",
            "created_at": "2026-07-27T03:00:00+00:00",
            "completed_at": "2026-07-27T03:01:00+00:00",
            "metadata": {"interrupt_reason": "no_terminal_result"},
        }
    )

    health = sqlite.definition_health("task-resultless", now="2026-07-27T04:00:00+00:00")

    assert health["consecutive_failures"] == 1
    assert health["health"] == "failing"


def test_cancellations_do_not_bury_a_failure_in_the_bounded_window(tmp_path: Path) -> None:
    """HFR-068 — ``canceled`` is transparent, and by predicate rather than classifier.

    A cancellation is the absence of an outcome. Skipping it while classifying
    cannot make it transparent, because ``LIMIT N`` is applied first: N cancelled
    retries after one failure fill the whole window and displace the failure, so
    the definition reads healthy although nothing ever succeeded.
    """

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-cancel")
    sqlite.enqueue_run(
        {
            "id": "run-failed",
            "request_type": "scheduled",
            "status": "failed",
            "definition_id": "task-cancel",
            "error": "boom",
            "created_at": "2026-07-27T01:00:00+00:00",
            "completed_at": "2026-07-27T01:01:00+00:00",
        }
    )
    for index in range(12):
        sqlite.enqueue_run(
            {
                "id": f"run-cancel-{index}",
                "request_type": "scheduled",
                "status": "canceled",
                "definition_id": "task-cancel",
                "created_at": f"2026-07-27T02:{index:02d}:00+00:00",
                "completed_at": f"2026-07-27T02:{index:02d}:30+00:00",
            }
        )

    health = sqlite.definition_health("task-cancel", now="2026-07-27T03:00:00+00:00")

    assert health["consecutive_failures"] == 1, "cancellations must not displace the failure"
    assert health["health"] == "failing"


def test_watch_runtime_heartbeat_does_not_reset_consecutive_failures(tmp_path: Path) -> None:
    """HFR-069 — the supervisor heartbeat shares the watch's ``definition_id``.

    ``write_watch_runtime`` stores ``runtime:<watch_id>`` with
    ``definition_id = watch_id`` and flips the previous heartbeat to
    ``succeeded`` on every write, so an unfiltered history has a success between
    any two real failures and the watch reads healthy.
    """

    sqlite, _ = _store(tmp_path)
    _watch(sqlite, "watch-1")
    sqlite.enqueue_run(
        {
            "id": "run-watch-fail",
            "request_type": "watch",
            "status": "failed",
            "definition_id": "watch-1",
            "error": "hook blew up",
            "created_at": "2026-07-27T01:00:00+00:00",
            "completed_at": "2026-07-27T01:01:00+00:00",
        }
    )
    # The supervisor heartbeat, written after the failure and therefore newest.
    sqlite.write_watch_runtime(
        {"watches": {"watch-1": {"running": True, "started_at": "2026-07-27T02:00:00+00:00"}}},
        updated_at="2026-07-27T02:00:00+00:00",
    )

    health = sqlite.definition_health("watch-1", now="2026-07-27T03:00:00+00:00")

    assert health["consecutive_failures"] == 1, "the heartbeat is not an execution"
    assert health["health"] == "failing"


def test_nonterminal_execution_does_not_displace_the_newest_verdict(tmp_path: Path) -> None:
    """HFR-070 — health is a function of settled outcomes only.

    A failing recurring definition fires again on schedule; the fresh
    ``queued``/``running`` row is the newest entry for the definition. It is not
    an outcome, so reading "the latest run failed" off it reports a definition
    that is actively failing as healthy for the whole duration of its next
    attempt — and every fire re-arms that.
    """

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-inflight")
    sqlite.enqueue_run(
        {
            "id": "run-old-fail",
            "request_type": "scheduled",
            "status": "failed",
            "definition_id": "task-inflight",
            "error": "boom",
            "created_at": "2026-07-27T01:00:00+00:00",
            "completed_at": "2026-07-27T01:01:00+00:00",
        }
    )
    sqlite.enqueue_run(
        {
            "id": "run-inflight",
            "request_type": "scheduled",
            "status": "running",
            "definition_id": "task-inflight",
            "created_at": "2026-07-27T02:00:00+00:00",
        }
    )

    health = sqlite.definition_health("task-inflight", now="2026-07-27T02:30:00+00:00")

    assert health["consecutive_failures"] == 1
    assert health["health"] == "failing"


def test_health_window_ages_out_after_the_time_bound(tmp_path: Path) -> None:
    """HFR-071 — the window is count AND time bounded, in the WHERE clause.

    Bounded only by ``LIMIT N``, a definition that fails once and then stops
    firing keeps that failure as its newest verdict forever and reads ``failing``
    indefinitely, with no user action able to clear it.
    """

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-stale", schedule_type="at", run_at="2026-07-01T00:00:00+00:00", cron=None, enabled=False)
    sqlite.enqueue_run(
        {
            "id": "run-ancient",
            "request_type": "scheduled",
            "status": "failed",
            "definition_id": "task-stale",
            "error": "boom",
            "created_at": "2026-07-01T00:00:00+00:00",
            "completed_at": "2026-07-01T00:01:00+00:00",
        }
    )

    health = sqlite.definition_health("task-stale", now="2026-07-27T00:00:00+00:00")

    assert health["recent_failures"] == 0, "a failure older than the window is aged out"
    assert health["health"] == "healthy"


def test_one_malformed_metadata_row_does_not_blank_health_for_every_definition(
    tmp_path: Path,
) -> None:
    """HFR-072 — a ``json_extract`` predicate aborts the statement on one bad row.

    Master filters ``metadata_json`` in Python through the defensive
    ``_json_loads``, so a single unparseable row costs one misclassified run.
    SQLite raises ``malformed JSON`` and fails the whole statement, which would
    take the health badge down for every definition in the list rather than for
    the row that is broken.
    """

    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    sqlite, _ = _store(tmp_path)
    for definition_id in ("task-a", "task-b"):
        _task(sqlite, definition_id)
        sqlite.enqueue_run(
            {
                "id": f"run-{definition_id}",
                "request_type": "scheduled",
                "status": "failed",
                "definition_id": definition_id,
                "error": "boom",
                "created_at": "2026-07-27T01:00:00+00:00",
                "completed_at": "2026-07-27T01:01:00+00:00",
            }
        )

    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs)
            .where(agent_runs.c.id == "run-task-a")
            .values(metadata_json="{not json")
        )

    healths = sqlite.definition_health_batch(
        ["task-a", "task-b"],
        now="2026-07-27T02:00:00+00:00",
    )

    # task-b is unaffected by task-a's bad row.
    assert healths["task-b"]["health"] == "failing"
    # task-a degrades to unknown rather than taking the list down.
    assert healths["task-a"]["health"] in {"failing", "unknown"}


# --- group 4: a binding change is a notice, not just a log line ------------
#
# The rebind lane is the one transition where the run SUCCEEDS and the user still
# has to be told: ``create_once`` re-reserves a session, the retry works, and the
# only trace is a ``logger.warning``. Everything below rides the same owed-notice
# protocol as a failure, because "the user was told" needs a receipt whichever lane
# produced the message.


def _rebound_run(tmp_path: Path, monkeypatch):
    """Drive one real ``create_once`` fire whose pinned session is gone.

    One SQLite file serves all three halves of HFR-099: ``_binding_env`` and
    ``_store`` both resolve to ``tmp_path/state/vibe.sqlite``, so the execution that
    rebinds, the drain that delivers, and the ``messages`` row the receipt is read
    back from are the same database rather than three that agree by construction.
    """

    from core.scheduled_tasks import ScheduledTaskStore

    from tests.test_scheduled_tasks import _binding_env, _binding_service

    db_path = _binding_env(tmp_path, monkeypatch)
    _migrated_state_db()

    sqlite, requests = _store(tmp_path)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()
    task = store.add_task(
        name="daily digest",
        session_key="",
        session_id="sesdoesnotexist",
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )

    calls: list = []
    service = _binding_service(tmp_path, store, calls)
    # The SQLite-backed request store, so the fire produces a real ``agent_runs``
    # row for the notice to be stamped on and the drain to find.
    service.request_store = requests

    queued = requests.enqueue_task_run(task.id, source_kind="scheduler", task=task)
    claimed = requests.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))

    return db_path, sqlite, requests, store, task, queued.id, calls


def test_a_successful_rebind_is_delivered_through_the_retried_notice_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-099 — a silently replaced session binding owes the user a notice.

    ``create_once`` whose pinned session was hard-deleted re-reserves one and
    retries the fire. When the retry SUCCEEDS the run settles ``succeeded`` with
    ``error=None``, so ``_owed_failure_notice_for_transition`` stamps nothing — it
    stamps only on ``failed`` — and ``_notify_binding_change`` is a log-only seam
    whose docstring claimed "the run row's own owed notice carries the user-visible
    half". False in exactly this case: the user's pinned session was replaced,
    possibly with different settings, and nothing ever said so.

    Two halves, and the second is the point of the finding:

    * the notice EXISTS, is keyed by the binding transition rather than by the run,
      and names old → new session;
    * it is DURABLE and RETRIED — killing the first delivery consumes an attempt,
      arms the backoff, and the next eligible tick delivers it with a receipt. A
      one-shot ``emit`` next to the log line would satisfy "a message appeared" and
      fail this half, which is why the notice rides the owed-notice protocol
      instead of a parallel path.
    """

    import core.failure_notices as failure_notices
    import core.scheduled_tasks as scheduled_tasks
    import storage.background as background
    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT
    from storage.background import NOTICE_PENDING
    from vibe.i18n import t as i18n_t

    _db_path, sqlite, requests, store, task, run_id, calls = _rebound_run(
        tmp_path, monkeypatch
    )

    # The fire itself worked: rebound, retried, and settled as a success.
    updated = store.get_task(task.id)
    assert updated is not None and updated.enabled is True
    assert updated.session_id and updated.session_id != "sesdoesnotexist"
    assert calls == ["send digest"], "the rebound run never executed"
    assert sqlite.get_run(run_id)["status"] == "succeeded"

    # --- half 1: the stamp -------------------------------------------------
    notice = sqlite.owed_failure_notice(run_id)
    assert isinstance(notice, dict), (
        "a successful rebind left NO durable notice: the pinned session was "
        "replaced and only a log line said so"
    )
    assert notice["state"] == NOTICE_PENDING
    assert notice["kind"] == failure_notices.NOTICE_KIND_BINDING_CHANGE
    # The storage mirror and the core vocabulary must agree, for the same reason
    # ``RUN_INTERRUPTION_REASONS`` is mirrored: ``core`` imports ``storage``, never
    # the reverse, so the constant is spelled twice and pinned once.
    assert background.NOTICE_KIND_BINDING_CHANGE == failure_notices.NOTICE_KIND_BINDING_CHANGE
    assert background.NOTICE_KIND_FAILURE == failure_notices.NOTICE_KIND_FAILURE
    recorded = (store.get_task(task.id).metadata or {}).get("binding_recovery") or {}
    assert notice["failure_id"] == f"binding:{task.id}:{recorded['signature']}", (
        "the identity must be the binding transition's own dedup key, so one broken "
        "binding is one notification however many times it fires"
    )
    binding = notice["binding"]
    assert binding["action"] == "rebound"
    assert binding["previous_session_id"] == "sesdoesnotexist"
    assert binding["new_session_id"] == updated.session_id
    assert binding["settings_preserved"] is False

    # --- half 2: delivery is retried, not fire-and-forget ------------------
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    service = _drain_service(tmp_path, controller, sqlite, requests)
    service._t = lambda key, **kwargs: i18n_t(key, "en", **kwargs)
    emissions = _spy_emissions(controller)

    real_emit = scheduled_tasks.emit_replayed_backend_failure

    async def _raising_emit(*args, **kwargs):
        raise RuntimeError("transport down")

    monkeypatch.setattr(scheduled_tasks, "emit_replayed_backend_failure", _raising_emit)
    asyncio.run(service._drain_failure_notices())

    after_failure = sqlite.owed_failure_notice(run_id)
    assert after_failure["state"] == NOTICE_PENDING, "a killed delivery must stay owed"
    assert after_failure["attempts"] == 1, "the failed attempt must be persisted"
    assert str(after_failure["next_attempt_at"] or "") > str(after_failure["stamped_at"]), (
        "the backoff must be armed rather than re-firing on the next 2 s tick"
    )
    assert not controller.im_client.sent

    # The armed backoff really holds: an immediate second tick is a no-op.
    monkeypatch.setattr(scheduled_tasks, "emit_replayed_backend_failure", real_emit)
    asyncio.run(service._drain_failure_notices())
    assert sqlite.owed_failure_notice(run_id)["attempts"] == 1
    assert not controller.im_client.sent

    # Let the backoff elapse without sleeping, exactly as HFR-076 does.
    sqlite.update_owed_failure_notice(run_id, next_attempt_at=None)
    asyncio.run(service._drain_failure_notices())

    settled = sqlite.owed_failure_notice(run_id)
    assert settled["state"] == NOTICE_SENT, "the retry must deliver the notice"
    assert settled["attempts"] == 2, "the retry is the SECOND attempt, not the first"
    assert settled["ack_evidence"] == ACK_EVIDENCE_RECEIPT

    assert [item["type"] for item in emissions] == ["notify"]
    channel, _thread, sent_text = controller.im_client.sent[0]
    assert channel == "C123"
    assert "sesdoesnotexist" in sent_text and updated.session_id in sent_text, (
        f"the notice must name old -> new session: {sent_text!r}"
    )
    assert "daily digest" in sent_text
    assert i18n_t("harness.notice.unknownError", "en") not in sent_text, (
        "a run that SUCCEEDED must not be reported with an error line"
    )

    rows = _persisted_messages()
    assert [row["type"] for row in rows] == ["notify"]
    assert rows[0]["content_text"] == sent_text


#: Every ``vibe`` command the binding-change copy is allowed to print, with the
#: argv the CLI must accept for it. Placeholders are rendered as uppercase tokens so
#: the scan below cannot mistake an id for a subcommand.
_BINDING_NOTICE_COMMANDS = {
    "vibe task update ID --session-id <session-id>": [
        "task",
        "update",
        "ID",
        "--session-id",
        "<session-id>",
    ],
    "vibe task show ID": ["task", "show", "ID"],
}

#: The keys the binding-change body is built from, in both languages.
_BINDING_NOTICE_KEYS = (
    "harness.notice.rebound",
    "harness.notice.reboundSessions",
    "harness.notice.reboundSettingsPreserved",
    "harness.notice.reboundSettingsReset",
    "harness.notice.reboundRepin",
    "harness.notice.show",
)


def test_the_binding_notice_copy_only_names_commands_the_cli_has() -> None:
    """HFR-099 — every command the binding copy prints must really parse.

    The same trap WI-2 hit with ``vibe watch run``: copy is the one place a command
    can be invented with nothing failing, and a user handed a command that does not
    parse is worse off than one handed none. Two directions, because either alone
    passes trivially: the allowed commands are checked against the REAL parser, and
    the rendered copy is checked to mention no ``vibe`` command outside that set.
    """

    import re

    from vibe.cli import build_parser
    from vibe.i18n import t as i18n_t

    parser = build_parser()
    for spelling, argv in _BINDING_NOTICE_COMMANDS.items():
        try:
            parser.parse_args(argv)
        except SystemExit:  # pragma: no cover - the assertion is the point
            raise AssertionError(f"the binding copy prints {spelling!r}, which the CLI cannot parse")

    for lang in ("en", "zh"):
        for key in _BINDING_NOTICE_KEYS:
            rendered = i18n_t(
                key,
                lang,
                name="NAME",
                id="ID",
                previous="PREVIOUS",
                new="NEW",
            )
            assert rendered != key, f"{key} is missing from {lang}.json"
            # A left word boundary, so the ``[Avibe Harness]`` brand prefix is
            # not read as an invocation of a ``vibe Harness]`` subcommand.
            for match in re.finditer(r"(?<![A-Za-z])vibe ", rendered):
                tail = rendered[match.start():]
                assert any(tail.startswith(spelling) for spelling in _BINDING_NOTICE_COMMANDS), (
                    f"{lang}/{key} prints an unvetted command: {tail!r}"
                )
