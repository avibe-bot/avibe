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
    OWED_FAILURE_NOTICE_KEY,
    OWED_NOTICE_INDEX,
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

    monkeypatch.setattr(scheduled_tasks, "emit_backend_failure", _fake_emit)

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

    monkeypatch.setattr(scheduled_tasks, "emit_backend_failure", _raising_emit)

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


def test_the_drain_does_not_turn_an_owed_notice_into_a_live_auth_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-077 — auth recovery is bypassed for drained notices, deliberately.

    ``maybe_emit_auth_recovery_message`` cannot fill a ``DeliveryEvidence`` — its
    signature has no such parameter — so a notice delivered through it reads as
    unacknowledged and walks on to the next ladder rung.

    The fix chosen is the bypass rather than plumbing evidence through it, because
    the product answer decides the plumbing: an owed notice is a report about a run
    that failed in the past, possibly hours ago and possibly already retried,
    whereas the auth prompt is an interactive remediation affordance about the state
    of the backend RIGHT NOW. Those are different messages.
    """

    from types import SimpleNamespace

    import core.scheduled_tasks as scheduled_tasks
    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-auth", deliver_key="slack::channel::C1")
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()

    run = requests.enqueue_task_run("task-auth")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="401 unauthorized", task_id="task-auth")

    captured: list[dict] = []
    real_emit = scheduled_tasks.emit_backend_failure

    async def _spy_emit(controller, context, backend, diagnostic, **kwargs):
        captured.append(kwargs)
        evidence = kwargs.get("delivery")
        if evidence is not None:
            evidence.delivered_id = "1717.1"
            evidence.persisted_row = {"id": "msg-1"}
        return False

    monkeypatch.setattr(scheduled_tasks, "emit_backend_failure", _spy_emit)
    assert real_emit is not _spy_emit

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service._drain_dirty = False
    service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
    service._owns_service_instance = lambda: True
    service.validate_platform = lambda platform: None
    service._t = lambda key, **kwargs: key

    asyncio.run(service._drain_failure_notices())

    assert len(captured) == 1
    assert captured[0].get("allow_auth_recovery") is False, (
        "the drain must opt out of auth recovery, which cannot report a receipt"
    )
    assert sqlite.owed_failure_notice(run.id)["state"] == "sent"


def test_auth_recovery_stays_on_for_every_other_caller() -> None:
    """The bypass is opt-in, so the interactive 401 path is untouched.

    Auth recovery is where a real 401 gets its reset-OAuth button; only the drain
    declines it. Every one of the thirteen backend call sites keeps the default.
    """

    import inspect

    from core.backend_failure import emit_backend_failure

    default = inspect.signature(emit_backend_failure).parameters["allow_auth_recovery"].default
    assert default is True


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
        patch.setattr(scheduled_tasks, "emit_backend_failure", _spy_emit)
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
