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

from sqlalchemy import select

from core.scheduled_tasks import TaskExecutionStore
from storage.background import (
    NOTICE_KIND_FAILURE,
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
# it is a pure function of (notice, streak facts, earlier-unsettled), so every branch
# is reachable without a controller, an event loop or an IM transport. The delivery
# protocol (receipt/ack/backoff/dead-letter) is tested against the store, since
# what has to survive a crash is the row, not the call.


def _notice(state: str = "pending", **fields) -> dict:
    payload = {"state": state, "attempts": 0, "next_attempt_at": None}
    payload.update(fields)
    return payload


def _streak_row(run_id: str, status: str = "failed", notice: dict | None = None) -> dict:
    return {"id": run_id, "created_at": f"2026-07-27T00:00:{run_id[-2:]}", "status": status, "notice": notice}


def _facts(streak: list[dict], run_id: str) -> dict:
    """``failure_streak_decision``'s three facts, derived from a streak's ROWS.

    This is byte-for-byte the derivation ``decide`` used to perform inline on the
    streak it was handed, and it stays in the test module for two jobs:

    * these policy scenarios keep reading as streaks — "a sent row and a pending row"
      is what the policy is about, and stating it as ``has_sent_elsewhere=True``
      instead would assert the answer as the question;
    * it is the ORACLE. ``test_the_sql_streak_facts_match_the_materialized_streak``
      composes it with ``_materialized_streak`` to check the SQL facts, and
      ``test_the_facts_decide_exactly_what_the_streak_rows_decided`` uses it to check
      that moving the derivation into SQL did not move any outcome.
    """

    others = [row for row in streak if row["id"] != run_id]
    return {
        "in_streak": any(row["id"] == run_id for row in streak),
        "has_sent_elsewhere": any(
            (row.get("notice") or {}).get("state") == "sent" for row in others
        ),
        "earliest_pending_id": next(
            (row["id"] for row in streak if (row.get("notice") or {}).get("state") == "pending"),
            None,
        ),
    }


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
        streak_facts=_facts([first, second], "run-02"),
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
        streak_facts=_facts([first, second], "run-02"),
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
        streak_facts=_facts([first, second], "run-02"),
        earlier_unsettled=None,
    )

    assert decision.action == ACTION_DELIVER


def test_failure_after_a_success_notifies_again() -> None:
    """A ``succeeded`` verdict closes the streak, so recovery re-arms notification."""

    from core.failure_notices import ACTION_DELIVER, decide

    # ``failure_streak_decision`` answers only about the streak CONTAINING this run,
    # so a success before it is already excluded — the streak here is this row alone.
    row = _streak_row("run-03", notice=_notice("pending"))

    decision = decide(
        run_id="run-03",
        definition_id="task-1",
        notice=row["notice"],
        streak_facts=_facts([row], "run-03"),
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
        streak_facts=None,
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
        streak_facts=None,
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
            streak_facts=_facts(streak, run_id),
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
        streak_facts=_facts([first, second], "run-02"),
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

    Asserted through the CONSEQUENCE rather than through a list of ids: the first
    failure carries a ``sent`` notice, so if the heartbeat splits the streak the
    second failure no longer sees it, ``has_sent_elsewhere`` goes false and the drain
    notifies again. That is the spam, stated as the thing that produces it.
    """

    from core.failure_notices import ACTION_SKIP, decide

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
                # The first failure already told the user; the second is the one whose
                # fate the heartbeat must not change.
                "metadata": {
                    OWED_FAILURE_NOTICE_KEY: {
                        "state": "sent" if index == 1 else "pending",
                        "attempts": 1,
                    }
                },
            }
        )
        # A heartbeat write lands between the two failures, flipping the previous
        # runtime row to ``succeeded``.
        sqlite.write_watch_runtime(
            {"watches": {"watch-streak": {"running": True, "started_at": f"2026-07-27T0{index}:30:00+00:00"}}},
            updated_at=f"2026-07-27T0{index}:30:00+00:00",
        )

    facts = sqlite.failure_streak_decision("watch-streak", "run-w2")

    assert facts["in_streak"] is True
    assert facts["has_sent_elsewhere"] is True, (
        "the heartbeat must not split the streak; the earlier failure's sent notice "
        f"has to stay visible to run-w2, got {facts}"
    )
    assert (
        decide(
            run_id="run-w2",
            definition_id="watch-streak",
            notice=_notice("pending"),
            streak_facts=facts,
            earlier_unsettled=None,
        ).action
        == ACTION_SKIP
    )


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


def _python_earliest_unsettled_before(
    sqlite_store: SQLiteBackgroundTaskStore,
    definition_id: str,
    *,
    created_at: str,
    run_id: str,
    stale_after_seconds: float | None,
    now: str,
) -> dict | None:
    """The pre-SQL predecessor read, computed by filtering in Python.

    Byte-for-byte the production algorithm as of ``3578f2b6`` — the whole
    queued/running population for the definition ordered by ``(created_at, id)``,
    then a Python loop that ``continue``s past rows at or after the anchor position
    and past rows older than the staleness cap. It is the specification the SQL has
    to reproduce, including its edge cases.
    """

    from datetime import datetime, timezone

    from sqlalchemy import or_ as sa_or, select as sa_select

    from storage.background import (
        _WATCH_RUNTIME_RUN_TYPE,
        _parse_iso_instant,
        _status_query_values,
    )
    from storage.models import agent_runs

    instant = _parse_iso_instant(now) or datetime.now(timezone.utc)
    stmt = (
        sa_select(agent_runs.c.id, agent_runs.c.created_at, agent_runs.c.status)
        .where(agent_runs.c.definition_id == definition_id)
        .where(
            sa_or(
                agent_runs.c.run_type.is_(None),
                agent_runs.c.run_type != _WATCH_RUNTIME_RUN_TYPE,
            )
        )
        .where(
            agent_runs.c.status.in_(
                _status_query_values("queued") + _status_query_values("running")
            )
        )
        .order_by(agent_runs.c.created_at, agent_runs.c.id)
    )
    with sqlite_store.engine.connect() as conn:
        for row in conn.execute(stmt).mappings():
            if (str(row["created_at"]), str(row["id"])) >= (str(created_at), str(run_id)):
                continue
            if stale_after_seconds is not None:
                started = _parse_iso_instant(row["created_at"])
                if started is not None and (instant - started).total_seconds() > stale_after_seconds:
                    continue
            return {"id": row["id"], "created_at": row["created_at"], "status": row["status"]}
    return None


def test_the_predecessor_read_is_bounded_and_seeks_rather_than_scans(tmp_path: Path) -> None:
    """Subordinate to HFR-078 — the predecessor read runs on the same 2 s tick.

    ``earliest_unsettled_run_before`` is asked once per pending owed notice, exactly
    like the eligibility lookup HFR-078 bounded and the streak read HFR-095 bounded —
    and it was the last read in that path that was not. It selected EVERY
    queued/running row for the definition and then decided in Python which ones were
    before the anchor and which had gone stale, so a definition holding a large
    nonterminal backlog paid the whole backlog per notice per tick to answer a
    question whose answer is at most one row.

    Asserted on two things, because either alone is satisfiable by an unbounded read:
    the CONSTRAINED TERMS of the plan (naming an index proves nothing — HFR-086 is
    that lesson) and the SIZE of the result set the statement hands back, which is
    the work the tick actually pays.
    """

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-pred-plan", session_policy="create_per_run")
    backlog = 1200
    for index in range(backlog):
        sqlite.enqueue_run(
            {
                "id": f"run-backlog-{index:05d}",
                "request_type": "scheduled",
                "status": "queued" if index % 2 else "running",
                "definition_id": "task-pred-plan",
                # Well before the staleness cap, so every one of them is treated as
                # settled and the honest answer is ``None`` on both sides.
                "created_at": f"2026-07-01T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}+00:00",
            }
        )

    anchor_created = "2026-07-29T00:00:00+00:00"
    now = "2026-07-29T00:05:00+00:00"

    def _read():
        return sqlite.earliest_unsettled_run_before(
            "task-pred-plan",
            created_at=anchor_created,
            run_id="run-anchor",
            stale_after_seconds=3600.0,
            now=now,
        )

    assert _read() is None, "every predecessor is past the staleness cap"

    db_path = tmp_path / "state" / "vibe.sqlite"
    sizes = _agent_run_query_result_sizes(sqlite, db_path, _read)
    assert sizes, "the predecessor read issued no agent_runs query"
    assert max(sizes) <= 1, (
        f"the predecessor read handed Python {max(sizes)} rows out of a {backlog}-row "
        "nonterminal backlog to answer a question whose answer is at most one row"
    )

    plans = _agent_run_query_plans(sqlite, db_path, _read)
    rendered = "\n".join(line for _statement, plan in plans for line in plan)
    compact = rendered.replace(" ", "")
    assert "SCAN" not in rendered, (
        f"the predecessor read must never scan a definition's backlog; plans were:\n{rendered}"
    )
    assert "TEMPB-TREE" not in compact, (
        f"the (created_at, id) order must come from an index, not a sort; plans were:\n{rendered}"
    )
    assert "(created_at,id)<(?,?)" in compact, (
        "the anchor position must bound the seek in SQL, not in a Python ``continue``; "
        f"plans were:\n{rendered}"
    )

    # ...and a predecessor inside the cap is still found, so the bound above is not a
    # read that answers ``None`` to everything.
    sqlite.enqueue_run(
        {
            "id": "run-fresh",
            "request_type": "scheduled",
            "status": "running",
            "definition_id": "task-pred-plan",
            "created_at": "2026-07-28T23:59:00+00:00",
        }
    )
    blocker = _read()
    assert blocker is not None and blocker["id"] == "run-fresh"


def test_the_predecessor_read_breaks_created_at_ties_by_id_and_stays_bounded_on_no_match(
    tmp_path: Path,
) -> None:
    """Subordinate to HFR-078 — the two cases the randomised parity fixture only samples.

    The parity test above reaches ties and empty answers through a seeded RNG, which
    makes them reproducible but not NAMED: nothing in it fails if the tie-break or the
    empty case regresses for a reason the fixture's row pool happens to stop
    generating. Both are pinned here as fixed, hand-built histories.

    TIES, because ``created_at`` is not a position. Several writers stamp a whole
    batch with ONE value, so "the earliest unsettled predecessor" is only well defined
    with ``id`` inside the comparison. Two things follow and both are asserted: among
    predecessors sharing an instant the SMALLEST id wins, and a row sharing the
    ANCHOR's instant is a predecessor only when its id sorts below the anchor's — a
    scalar ``created_at <`` would drop the whole tied group (losing a real blocker,
    the duplicate-notice direction) and a scalar ``<=`` would admit the anchor itself
    and defer every notice behind its own run forever.

    THE NO-MATCH EDGE, because ``None`` is the answer the drain acts on: it is what
    releases the notice to send. It has to stay bounded too — an unbounded read that
    materialises the backlog and then finds nothing pays the same cost per tick as one
    that finds something, and ``None`` is the common case on a healthy definition.
    """

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-pred-tie", session_policy="create_per_run")

    # Well inside the staleness cap, so the cap plays no part in either case: this
    # test is about position, and a tied group that answered ``None`` because it had
    # aged out would pass the tie assertions for the wrong reason.
    tied = "2026-07-28T12:59:00+00:00"
    anchor_created = "2026-07-28T13:00:00+00:00"
    now = "2026-07-28T13:00:30+00:00"

    def _enqueue(run_id: str, status: str, created_at: str) -> None:
        sqlite.enqueue_run(
            {
                "id": run_id,
                "request_type": "scheduled",
                "status": status,
                "definition_id": "task-pred-tie",
                "created_at": created_at,
            }
        )

    def _read(created_at: str, run_id: str):
        return sqlite.earliest_unsettled_run_before(
            "task-pred-tie",
            created_at=created_at,
            run_id=run_id,
            stale_after_seconds=3600.0,
            now=now,
        )

    db_path = tmp_path / "state" / "vibe.sqlite"

    # THE NO-MATCH EDGE FIRST, while the only nonterminal rows sit at or after the
    # anchor position: there is no predecessor, and finding that out must still cost
    # at most one row.
    _enqueue("run-later", "queued", "2026-07-28T14:00:00+00:00")
    _enqueue("run-anchor", "running", anchor_created)
    assert _read(anchor_created, "run-anchor") is None
    sizes = _agent_run_query_result_sizes(sqlite, db_path, lambda: _read(anchor_created, "run-anchor"))
    assert sizes and max(sizes) <= 1, (
        f"the no-match answer must be bounded too; the read handed Python {sizes} rows"
    )

    # TIES AMONG PREDECESSORS: three rows on one instant, inserted so that neither
    # insertion order nor rowid order matches id order.
    for run_id in ("run-tie-c", "run-tie-a", "run-tie-b"):
        _enqueue(run_id, "queued", tied)
    blocker = _read(anchor_created, "run-anchor")
    assert blocker is not None and blocker["id"] == "run-tie-a", (
        f"the smallest id in a tied group is the earliest predecessor; got {blocker}"
    )

    # A TIE WITH THE ANCHOR ITSELF: asked from an anchor on the tied instant, the
    # answer is the tied row whose id sorts BELOW it, and never the anchor.
    blocker = _read(tied, "run-tie-b")
    assert blocker is not None and blocker["id"] == "run-tie-a", (
        f"a row tied with the anchor is a predecessor when its id sorts below; got {blocker}"
    )
    # ...and from the lowest id in the group there is no predecessor at all — the
    # anchor must not find itself.
    assert _read(tied, "run-tie-a") is None, "the anchor must never be its own blocker"


def test_the_sql_predecessor_read_matches_the_python_filtered_one(tmp_path: Path) -> None:
    """Subordinate to HFR-078 — the bounded read has to be the SAME predecessor.

    Parity against ``_python_earliest_unsettled_before``, the algorithm this
    replaces, over randomised WELL-FORMED histories containing every row class that
    decides the answer: queued and running rows on both sides of the anchor
    position, terminal rows that are not predecessors at all, ``watch_runtime``
    heartbeats (excluded, or every failed watch run would defer behind its own
    supervisor forever), rows sharing one ``created_at`` so the ``id`` tie-break
    decides which is earliest, and rows straddling the staleness cap. Asked with and
    without the cap, and from several anchors per history.

    WELL-FORMED is the caveat, and it is the one documented divergence. Python read
    an UNPARSEABLE ``created_at`` as fresh — ``_parse_iso_instant`` returned ``None``
    and the staleness test was skipped — while the SQL cutoff is a lexicographic
    string comparison that may class the same value as stale. That direction risks a
    duplicated notice rather than a lost one, which is the direction the 1 h cap
    itself already chose; it is asserted explicitly below rather than left to a
    fixture that never produces such a row.
    """

    import random

    sqlite, _ = _store(tmp_path)
    for seed in range(6):
        rng = random.Random(seed)
        definition_id = f"task-pred-parity-{seed}"
        _task(sqlite, definition_id, session_policy="create_per_run")
        ids: list[str] = []
        for index in range(30):
            run_id = f"run-{seed}-{index:03d}"
            ids.append(run_id)
            # A small pool of instants, some inside the cap and some well outside it,
            # with ties so the ``id`` tie-break decides the sequence.
            day = rng.choice([27, 27, 28, 29, 29])
            instant = f"2026-07-{day:02d}T{rng.randrange(4):02d}:00:00+00:00"
            status = rng.choice(["queued", "running", "running", "failed", "succeeded", "canceled"])
            request_type = "watch_runtime" if rng.random() < 0.15 else "scheduled"
            sqlite.enqueue_run(
                {
                    "id": run_id,
                    "request_type": request_type,
                    "status": status,
                    "definition_id": definition_id,
                    "created_at": instant,
                    "completed_at": instant if status in {"failed", "succeeded", "canceled"} else None,
                }
            )

        now = "2026-07-29T04:00:00+00:00"
        anchors = [
            ("2026-07-29T03:00:00+00:00", "run-anchor"),
            ("2026-07-27T00:00:00+00:00", "run-anchor"),
            ("2026-07-28T02:00:00+00:00", f"run-{seed}-015"),
            *[
                (f"2026-07-{rng.choice([27, 28, 29]):02d}T{rng.randrange(4):02d}:00:00+00:00", rng.choice(ids))
                for _ in range(6)
            ],
        ]
        for created_at, run_id in anchors:
            for cap in (None, 3600.0, 86400.0, 0.0):
                expected = _python_earliest_unsettled_before(
                    sqlite,
                    definition_id,
                    created_at=created_at,
                    run_id=run_id,
                    stale_after_seconds=cap,
                    now=now,
                )
                actual = sqlite.earliest_unsettled_run_before(
                    definition_id,
                    created_at=created_at,
                    run_id=run_id,
                    stale_after_seconds=cap,
                    now=now,
                )
                assert actual == expected, (
                    f"seed {seed}, anchor ({created_at}, {run_id}), cap {cap}: the SQL "
                    f"predecessor disagrees with the Python-filtered one\n"
                    f"  expected {expected}\n  actual   {actual}"
                )


def test_an_unparseable_created_at_reads_as_stale_rather_than_as_a_blocker(
    tmp_path: Path,
) -> None:
    """Subordinate to HFR-078 — the one divergence the SQL cutoff introduces.

    The Python filter asked ``_parse_iso_instant`` and skipped the staleness test
    when it answered ``None``, so a row whose ``created_at`` cannot be parsed BLOCKED
    the notice for as long as it stayed nonterminal — which, for an unparseable
    timestamp, is a wait nothing can bound. A lexicographic cutoff classes the same
    row as stale and treats it as settled.

    That is the duplicate-not-lost direction the cap already chose ("a duplicated
    notice is a papercut, a lost one is the D1 violation"), so it is pinned here as
    the intended behaviour rather than tolerated as an accident.
    """

    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-pred-garbage", session_policy="create_per_run")
    sqlite.enqueue_run(
        {
            "id": "run-garbage",
            "request_type": "scheduled",
            "status": "running",
            "definition_id": "task-pred-garbage",
            "created_at": "2026-07-27T01:00:00+00:00",
        }
    )
    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs)
            .where(agent_runs.c.id == "run-garbage")
            .values(created_at="0000-13-45T99:99:99+00:00")
        )

    blocker = sqlite.earliest_unsettled_run_before(
        "task-pred-garbage",
        created_at="2026-07-29T00:00:00+00:00",
        run_id="run-late",
        stale_after_seconds=3600.0,
        now="2026-07-29T00:05:00+00:00",
    )

    assert blocker is None, (
        "a row whose created_at cannot be read must not block a notice forever"
    )
    # Without a cap there is no cutoff to compare against, so the row is still a
    # predecessor: the divergence is confined to the staleness test, and the anchor
    # position comparison — a string comparison on both sides all along — is
    # unchanged.
    assert (
        sqlite.earliest_unsettled_run_before(
            "task-pred-garbage",
            created_at="2026-07-29T00:00:00+00:00",
            run_id="run-late",
            now="2026-07-29T00:05:00+00:00",
        )
        or {}
    ).get("id") == "run-garbage"


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


def _raw_metadata_json(sqlite: SQLiteBackgroundTaskStore, run_id: str) -> Any:
    """The ``metadata_json`` COLUMN, undecoded — what a clobber would rewrite."""

    from storage.models import agent_runs

    with sqlite.engine.connect() as conn:
        return conn.execute(
            select(agent_runs.c.metadata_json).where(agent_runs.c.id == run_id)
        ).scalar_one()


def _write_raw_metadata_json(sqlite: SQLiteBackgroundTaskStore, run_id: str, blob: str) -> None:
    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs).where(agent_runs.c.id == run_id).values(metadata_json=blob)
        )
    assert _raw_metadata_json(sqlite, run_id) == blob, "the fixture blob must survive the write"


def test_a_terminal_writer_never_rewrites_unparseable_metadata(tmp_path: Path) -> None:
    """Subordinate to HFR-084/HFR-072 — malformed metadata is READ-ONLY to the stamp.

    ``_merge_owed_failure_notice`` decoded the column with ``_json_loads(..., {})``
    and then fell back to ``{}`` for anything that was not a dict, so settling a row
    whose ``metadata_json`` is unparseable — or valid JSON that is not an object —
    REPLACED the raw column with ``{"owed_failure_notice": …}``. Whatever the blob
    held (a truncated write, a value another component owns, the bytes an operator
    would need to diagnose it) was destroyed by the notification feature, on all four
    terminal writers.

    Inconsistent with all three accepted precedents on this head, which is what makes
    it a defect rather than a policy: the binding stamp refuses a malformed row
    through ``json_valid`` (HFR-084), ``update_owed_failure_notice`` returns ``None``
    when the metadata is not a dict, and ``list_owed_failure_notices`` excludes
    malformed rows outright — "a row whose metadata will not parse cannot hold a
    readable notice anyway".

    So the choke point refuses the METADATA write while the terminal transition still
    commits: the run settles ``failed`` with its ``error`` recorded and the column is
    byte-identical. The residual is stated rather than hidden — a malformed row
    settles failed but never owes a notice, which was already true at the read side.
    """

    blobs = ("{broken", "[1]", '"5"', "not json at all")
    for blob in blobs:
        for writer in ("record_run_output", "settle_run_terminal", "settle_deferred_run", "coalesced"):
            # ``[1]`` and ``"5"`` — valid JSON that is not an object — are only
            # exercised on one writer; the truncated blob is exercised on all four.
            if blob != "{broken" and writer != "record_run_output":
                continue
            sqlite, requests = _store(tmp_path / f"{writer}-{blobs.index(blob)}")
            if writer == "coalesced":
                run = requests.enqueue_agent_run(
                    session_key="slack::channel::C1", message="d", agent_name=None
                )
            else:
                run = requests.enqueue_hook_send(session_key="slack::channel::C1", prompt="a")
            claimed = requests.claim(run.id)
            if writer == "settle_deferred_run":
                sqlite.defer_run_terminal(run.id, terminal_status="failed", error="boom")

            _write_raw_metadata_json(sqlite, run.id, blob)

            if writer == "record_run_output":
                sqlite.record_run_output(
                    run.id, output_id="o1", text="bad", terminal_status="failed", error="boom"
                )
            elif writer == "settle_run_terminal":
                # ``extra_metadata`` too: a caller-supplied sibling field must not be
                # the lever that rewrites the column either.
                sqlite.settle_run_terminal(
                    run.id,
                    terminal_status="failed",
                    error="boom",
                    metadata={"interrupt_reason": "evicted"},
                )
            elif writer == "settle_deferred_run":
                sqlite.settle_deferred_run(run.id)
            else:
                requests.complete_coalesced(claimed, [run.id], ok=False, error="boom")

            saved = sqlite.get_run(run.id)
            assert _raw_metadata_json(sqlite, run.id) == blob, (
                f"{writer} rewrote an unparseable metadata column instead of leaving it "
                f"alone: {_raw_metadata_json(sqlite, run.id)!r}"
            )
            assert saved["status"] == "failed", (
                f"{writer} must still commit the terminal transition, got {saved['status']!r}"
            )
            assert "boom" in str(saved["error"] or ""), (
                f"{writer} must still record the error, got {saved['error']!r}"
            )
            # The stated residual, pinned so it is a decision and not a surprise.
            assert sqlite.owed_failure_notice(run.id) is None
            assert sqlite.list_owed_failure_notices() == []


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

    Subordinate to HFR-076 — and the SHAPE is not enough, which is why the value
    assertions below were added. This test used to check only that the delays rose,
    were unique, and ran out at ``MAX_ATTEMPTS``. All three held while
    ``next_attempt`` indexed ``BACKOFF_SECONDS[attempts]`` after incrementing, so a
    freshly stamped notice's first failure armed 8 s: the declared 2 s interval at
    index 0 was unreachable, the ladder was one rung shorter than it declared, and
    ``CLAIM_LEASE_SECONDS``' "must exceed the retry cap" argument was reasoning
    about an interval the code could not arm. The mapping is therefore pinned by
    VALUE: the Nth failed attempt arms ``BACKOFF_SECONDS[N - 1]``, and the attempt
    that finds no interval left dead-letters instead.
    """

    from core.failure_notices import BACKOFF_SECONDS, MAX_ATTEMPTS, next_attempt

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

    # The full declared sequence, in order, each interval used exactly once —
    # starting at the first one.
    assert delays == list(BACKOFF_SECONDS), (
        f"every declared interval must be armed exactly once, in order: {delays}"
    )
    assert delays[0] == 2.0, (
        f"the first failed attempt must wait the declared first interval, not {delays[0]}"
    )

    # The exhaustion edge, from both sides: one attempt per declared interval, plus
    # the one that finds none left. The terminal bound is a consequence of the
    # sequence's length rather than an independently chosen number.
    assert MAX_ATTEMPTS == len(BACKOFF_SECONDS) + 1
    assert next_attempt({"attempts": len(BACKOFF_SECONDS) - 1}) == (
        len(BACKOFF_SECONDS),
        BACKOFF_SECONDS[-1],
    ), "the last declared interval must be reachable, or the cap is decoration"
    assert next_attempt({"attempts": len(BACKOFF_SECONDS)}) == (MAX_ATTEMPTS, None), (
        "the attempt after the last interval has nowhere to go and must dead-letter"
    )


def test_the_notice_timing_constants_bound_each_other_as_documented() -> None:
    """Subordinate to HFR-076 — the ordering the docstrings ARGUE, asserted.

    Three constants have to be ordered for the claim/retry state machine to hold, and
    each argument currently lives only in prose that nothing checks:

    * ``NOTICE_DELIVERY_TIMEOUT_SECONDS < CLAIM_LEASE_SECONDS`` — a claimant whose
      delivery is timed out must still hold its own lease when it writes the retry,
      or its ``expect``-guarded write races a replacement claimant that has already
      taken the row;
    * ``CLAIM_LEASE_SECONDS > BACKOFF_SECONDS[-1]`` — a lease may never expire sooner
      than the backoff a failed attempt would have armed, or expiry-recovery quietly
      becomes the faster retry path and replaces the backoff it exists to respect;
    * ``MAX_ATTEMPTS == len(BACKOFF_SECONDS) + 1`` — the terminal bound is one
      attempt per declared interval plus the attempt that finds none left, so N
      attempts are separated by exactly N-1 intervals.

    Cheap, and it fails the moment someone retunes one number without the others.
    """

    from core import failure_notices

    assert failure_notices.NOTICE_DELIVERY_TIMEOUT_SECONDS < failure_notices.CLAIM_LEASE_SECONDS, (
        "a timed-out claimant must be cancelled while its own lease still holds"
    )
    assert failure_notices.CLAIM_LEASE_SECONDS > failure_notices.BACKOFF_SECONDS[-1], (
        "lease expiry must never undercut the longest backoff it can be racing"
    )
    assert failure_notices.MAX_ATTEMPTS == len(failure_notices.BACKOFF_SECONDS) + 1


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

    D5 rung (5) was described as always resolving because it is addressed to the
    workspace. It is not: the rung is a SYNTHETIC project candidate built from the
    run's session id, and it resolves to a REAL PERSISTED project scope only through
    that session's ``agent_sessions`` row. ``maybe_notify_inbox_message``'s
    ``session_id`` requirement is widened for the sessionless case, but the FIRST
    blocker is earlier than the plan names: ``persist_agent_message`` returns before
    writing anything when an avibe context resolves neither a scope nor a session
    row. A definition that has never had a session therefore has no candidate at all,
    and one whose row was deleted has a candidate that resolves to nothing — a truly
    sessionless workspace surface is a declared Known-By-Design limitation under
    #1044's still-open plan contract, not something the ladder fakes.

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


def test_the_duplicate_short_circuit_receipt_acks_a_workbench_rung() -> None:
    """HFR-075 — the dedup receipt has to satisfy the STRICTEST ack source there is.

    The receipt above is the general claim; this is the one consumer for which it is
    load-bearing rather than convenient. A workbench rung may acknowledge on a
    durable persisted receipt and on nothing else (``LADDER_ACK_SOURCES``), so if the
    duplicate short-circuit reported its found row as anything weaker than
    ``receipt`` — a bare ``delivered_id``, say — then the exact case the idempotency
    key exists for would be unserviceable on avibe: a crash between persisting the
    message and acknowledging the notice would find the row, decline to re-send,
    report nothing the policy accepts, and then re-send forever on other rungs or
    dead-letter a notice the user demonstrably already has.

    Driven through the real dispatcher against an avibe context, then handed to the
    real predicate, because the claim spans the two modules: what the short-circuit
    fills in, and what the ladder is willing to ack on.
    """

    from unittest.mock import patch

    import core.message_dispatcher as dispatcher_module
    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT, DeliveryEvidence
    from core.message_output import MessageOutput
    from core.scheduled_tasks import (
        ACK_SOURCE_PERSISTED_RECEIPT,
        ScheduledTaskService,
        failure_notice_ack_source,
        parse_scope_id,
    )
    from modules.im import MessageContext

    from tests.test_message_dispatcher_scheduled import _StubController

    controller = _StubController()
    dispatcher = dispatcher_module.ConsolidatedMessageDispatcher(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="proj-notice",
        platform="avibe",
        platform_specific={
            "platform": "avibe",
            "agent_session_id": "sesDup",
            "task_trigger_kind": "scheduled",
            "task_execution_id": "run-dup",
        },
    )
    evidence = DeliveryEvidence()

    with patch.object(dispatcher_module, "agent_message_exists", return_value=True):
        returned = asyncio.run(
            dispatcher.emit_agent_message(
                context,
                "notify",
                "your task failed",
                output=MessageOutput(
                    completes_turn=False,
                    completes_run=False,
                    idempotency_key="backend-failure:failure:run-dup",
                ),
                delivery=evidence,
            )
        )

    assert returned and "backend-failure:failure:run-dup" in returned
    assert controller.im_client.sent == [], "the row already exists; nothing may be re-sent"

    target = parse_scope_id("avibe::project::proj-notice")
    assert failure_notice_ack_source(target) == ACK_SOURCE_PERSISTED_RECEIPT, (
        "the premise: this target class accepts nothing but a receipt"
    )
    assert evidence.ack_evidence == ACK_EVIDENCE_RECEIPT
    assert ScheduledTaskService._rung_acknowledges(target, evidence) is True, (
        "a found row must acknowledge a workbench rung, or a crash-then-retry there "
        "can never settle"
    )
    # And the rejection annotation stays off a rung that was accepted: an ack that
    # also carried a "no persisted receipt" error would report a lie on the notice.
    assert evidence.error is None


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

    Scope, stated because the obvious reading of the title is wider than the proof:
    this is about stale WRITE rejection only. The winner here is substituted with a
    direct store update, so what is demonstrated is that the loser's ``pending`` retry
    and its ``failed`` dead letter both land on nothing. It says nothing about whether
    the loser DELIVERED — its send is stubbed out entirely, and in the real drain the
    send happens before any of these writes. The no-redelivery consuming outcome is
    ``test_two_owners_reading_one_pending_notice_deliver_it_exactly_once``, which
    counts outbound sends at the IM adapter with two real connections; the durable
    single-flight claim it pins is what makes the two owners here mutually exclusive
    in the first place.

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


def test_a_notice_write_refuses_a_race_settled_after_its_own_read(tmp_path: Path) -> None:
    """Subordinate to HFR-076 — the expectation must be evaluated by SQLITE, not Python.

    The test above proves the guard catches a winner that landed BEFORE the losing
    write was issued. That is the wide window (a whole awaited send) but not the only
    one: the guard itself was a check-then-act. ``update_owed_failure_notice`` read
    the row, compared ``(state, attempts)`` in Python, and only then issued its
    UPDATE — and under pysqlite a bare SELECT emits no ``BEGIN`` (the reason
    ``upsert_definition_in_connection`` puts its own predicate in the WHERE clause),
    so no lock is held across that gap and the write lock is first taken by the
    UPDATE. Two passes could both read ``(pending, N)``, both pass the comparison,
    and both write.

    So the winner is committed HERE, from another connection, in exactly that gap:
    after the loser's SELECT, before the loser's UPDATE reaches the driver. Only a
    predicate SQLite evaluates in the writing statement survives it; a Python
    comparison has already been made against a snapshot the winner invalidated.

    The window is sub-millisecond, which is why this is a property test rather than a
    reported defect — but the losing write is the ``failed`` dead letter, which is
    terminal and buries a receipt the user already has, so "narrow" is not the same
    as "closed".
    """

    from sqlalchemy import event

    from storage.background import NOTICE_FAILED, notice_write_expectation

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-cas", deliver_key="slack::channel::C1")
    run = requests.enqueue_task_run("task-cas")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="boom", task_id="task-cas")

    # What the losing pass decided from, read exactly as the drain reads it. The
    # expectation is the TRIPLE, so the freshly stamped ``next_attempt_at`` is part of
    # it — asserted on the first two elements plus "the third is the stamped instant"
    # rather than on a literal, which would pin a wall clock.
    expect = notice_write_expectation(sqlite.owed_failure_notice(run.id))
    assert expect[:2] == ("pending", 0)
    assert expect[2] == sqlite.owed_failure_notice(run.id)["next_attempt_at"]

    interleaved: list[str] = []

    def _win_the_race_inside_the_gap(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if interleaved or not statement.lstrip().upper().startswith("UPDATE AGENT_RUNS"):
            return
        interleaved.append(statement)
        # The new lock owner's pass — which read the same pending notice — delivers
        # and acknowledges, and COMMITS, on its own connection. The loser's read is
        # already stale; its UPDATE has not been sent yet.
        sqlite.update_owed_failure_notice(
            run.id,
            expect=expect,
            state=NOTICE_SENT,
            attempts=1,
            ack_evidence="receipt",
        )

    event.listen(sqlite.engine, "before_cursor_execute", _win_the_race_inside_the_gap)
    try:
        lost = sqlite.update_owed_failure_notice(
            run.id,
            expect=expect,
            state=NOTICE_FAILED,
            attempts=5,
            error="stale dead letter",
        )
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _win_the_race_inside_the_gap)

    assert interleaved, "the winner never got into the gap — this test proves nothing"
    assert lost is None, (
        "a write whose expectation was invalidated between its read and its UPDATE "
        f"must report that it wrote nothing, got {lost}"
    )

    settled = sqlite.owed_failure_notice(run.id)
    assert settled["state"] == NOTICE_SENT, (
        "the ack committed inside the loser's read/write gap must stand, "
        f"got {settled['state']}"
    )
    assert settled["attempts"] == 1, "the winner's consumed attempt must stand"
    assert settled["ack_evidence"] == "receipt", "the winner's receipt must survive"
    assert not settled["error"], "the loser's dead-letter reason must not land at all"


def _owed_notice_run(sqlite, requests, definition_id: str):
    """A settled failure of ``definition_id`` owing a freshly stamped notice."""

    _task(sqlite, definition_id, deliver_key="slack::channel::C1")
    run = requests.enqueue_task_run(definition_id)
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="boom", task_id=definition_id)
    assert sqlite.owed_failure_notice(run.id)["state"] == "pending"
    return run


def test_a_stale_claim_cannot_overwrite_a_concurrent_deferral(tmp_path: Path) -> None:
    """HFR-076 — the value-CAS has to cover every field eligibility is decided from.

    Round 8 put ``(state, attempts)`` in the claim's WHERE clause. Eligibility is
    decided from THREE fields, not two: ``owed_notice_eligible`` reads ``state`` and
    ``next_attempt_at``, and ``next_attempt`` reads ``attempts``. The one the claim did
    not re-assert is the one a DEFERRAL writes — a deferral moves ``next_attempt_at``
    and ``defer_reason`` and leaves ``(state, attempts)`` exactly as they were — so a
    claimant that read before a concurrent owner's deferral still matched its own
    expectation, won the CAS, and overwrote the deferral.

    Two consequences, and neither is theoretical. In the stale-cutoff lane the
    predecessor deferral is what stops a second notice going out for one outage, so
    erasing it sends two. And ``DEFERRAL_RECHECK_SECONDS`` becomes advisory rather
    than durable: any stale claimant can pull a deferred row straight back into the
    batch it was deferred out of, which is the starvation the durable deferral exists
    to prevent.

    Round 8's reason for keeping ``updated_at`` OUT of the predicate does not extend
    to this field, which is why this is a completion rather than a reversal: a
    row-version guard refuses benign writes and a freshly stamped notice carries no
    such marker at all, whereas ``next_attempt_at`` is stamped unconditionally by
    every stamper and a legacy notice that lacks it reads ``""`` identically on both
    sides (the same ``coalesce(..., '')`` that keeps the eligibility index a range
    term).
    """

    from storage.background import notice_write_expectation

    sqlite, requests = _store(tmp_path)
    run = _owed_notice_run(sqlite, requests, "task-defer-race")

    # Owner A reads the notice and decides to CLAIM it.
    expect_a = notice_write_expectation(sqlite.owed_failure_notice(run.id))

    # Owner B is a second process, so a second engine over the same file. It reads the
    # same notice and DEFERS it behind the streak's canonical row.
    other = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    deferred_until = "2026-07-29T00:00:30+00:00"
    assert (
        other.update_owed_failure_notice(
            run.id,
            expect=notice_write_expectation(other.owed_failure_notice(run.id)),
            next_attempt_at=deferred_until,
            defer_reason="canonical_pending:run-canonical",
        )
        is not None
    ), "the deferral itself must land"

    # A's claim was decided from a world that no longer exists.
    lost = sqlite.update_owed_failure_notice(
        run.id,
        expect=expect_a,
        attempts=1,
        next_attempt_at="2026-07-29T00:10:00+00:00",
    )

    assert lost is None, (
        "a claim decided before a concurrent deferral must report that it wrote "
        f"nothing, got {lost}"
    )
    settled = sqlite.owed_failure_notice(run.id)
    assert settled["next_attempt_at"] == deferred_until, (
        "the durable deferral must stand, or DEFERRAL_RECHECK_SECONDS is advisory; "
        f"got {settled['next_attempt_at']!r}"
    )
    assert settled["attempts"] == 0, "no attempt may be consumed by a losing claim"
    assert settled["defer_reason"] == "canonical_pending:run-canonical"


def test_a_second_identical_deferral_loses_the_cas_without_changing_the_outcome(
    tmp_path: Path,
) -> None:
    """HFR-076 — and the Known-By-Design sentence round 8 wrote has to be updated.

    Round 8 recorded, as a deliberate non-guarantee, that two owners deferring one
    notice from the same read BOTH land: the deferral touches neither ``state`` nor
    ``attempts``, so neither write lost the ``(state, attempts)`` CAS. Adding
    ``next_attempt_at`` to the expectation changes that — the first deferral MOVES the
    field the second one's expectation carries, so the second now loses.

    That is the correct outcome under the new contract, not a benign-refusal
    regression, and the distinction is the OBSERVABLE state: the refused write was
    refused because the world had already moved to the state it was trying to
    establish. The notice is deferred either way, no attempt is consumed either way,
    and the recheck instant differs only by the microseconds between two owners
    computing ``now`` — so nothing a user or a later drain pass can observe differs.
    Asserted here, rather than argued, because "the loser's write was redundant" is
    exactly the claim a reviewer should not have to take on trust.
    """

    from storage.background import NOTICE_PENDING, notice_write_expectation

    sqlite, requests = _store(tmp_path)
    run = _owed_notice_run(sqlite, requests, "task-double-defer")

    read_together = notice_write_expectation(sqlite.owed_failure_notice(run.id))
    other = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")

    first = other.update_owed_failure_notice(
        run.id,
        expect=read_together,
        next_attempt_at="2026-07-29T00:00:30+00:00",
        defer_reason="canonical_pending:run-canonical",
    )
    second = sqlite.update_owed_failure_notice(
        run.id,
        expect=read_together,
        next_attempt_at="2026-07-29T00:00:30.000001+00:00",
        defer_reason="canonical_pending:run-canonical",
    )

    assert first is not None, "the first deferral lands"
    assert second is None, (
        "the second deferral is decided from a superseded read and must report that it "
        "wrote nothing"
    )

    # And the OUTCOME is the one both owners were trying to establish.
    settled = sqlite.owed_failure_notice(run.id)
    assert settled["state"] == NOTICE_PENDING, "still owed, and still not tried"
    assert settled["attempts"] == 0, "a deferral consumes no attempt, from either owner"
    assert settled["defer_reason"] == "canonical_pending:run-canonical"
    assert settled["next_attempt_at"] == "2026-07-29T00:00:30+00:00"
    # Deferred out of the immediately-eligible batch, which is the property the
    # durable deferral exists for — the starvation bound, not the exact instant.
    assert sqlite.list_owed_failure_notices(now="2026-07-29T00:00:00+00:00") == []
    assert [
        item["id"]
        for item in sqlite.list_owed_failure_notices(now="2026-07-29T00:01:00+00:00")
    ] == [run.id]


def test_the_notice_expectation_reads_the_same_in_python_and_in_sql(tmp_path: Path) -> None:
    """Subordinate to HFR-076 — the guard's two normalizations may not drift.

    Moving the predicate into the WHERE clause buys atomicity at the cost of a twin:
    ``notice_write_expectation`` normalizes the value the CALLER decided from, and
    ``owed_notice_state_unchanged`` has to normalize the stored blob identically or
    the guard refuses writes nobody raced. Same shape, and same hazard, as
    ``reclaim_snapshot_marker`` and its ``_RECLAIM_SNAPSHOT_MARKER_SQL`` twin, so it
    gets the same treatment: drive both sides over the values a notice can actually
    hold and require the same answer.

    The interesting rows are the ones where Python is lenient — a missing
    ``attempts``, a null, a JSON string, a non-integer, an ABSENT
    ``next_attempt_at`` — because each is a way for ``expect`` to say 0 or ``""``
    while SQL says NULL, which would fail every guarded write on a freshly stamped
    notice.

    The expectation is the TRIPLE ``(state, attempts, next_attempt_at)``: eligibility
    is decided from all three (``owed_notice_eligible`` reads state and
    ``next_attempt_at``, ``next_attempt`` reads ``attempts``), so all three have to be
    re-asserted or a write can land on a world that moved in the one field it did not
    check — a deferral written by a concurrent owner, overwritten by a stale claim.
    """

    from sqlalchemy import update as sa_update

    from storage.background import notice_write_expectation, owed_notice_state_unchanged
    from storage.models import agent_runs

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-twin")
    run = requests.enqueue_task_run("task-twin")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="boom", task_id="task-twin")

    notices: list[dict[str, Any]] = [
        {"state": "pending", "attempts": 0},
        {"state": "pending"},
        {"state": "pending", "attempts": None},
        {"state": "pending", "attempts": 3},
        {"state": "pending", "attempts": "3"},
        {"state": "pending", "attempts": "not-a-number"},
        {"state": "pending", "attempts": 3.7},
        {"state": "sent", "attempts": 1},
        {"state": "failed", "attempts": 5},
        {"state": "skipped", "attempts": 0},
        {"state": None, "attempts": 2},
        {"attempts": 2},
        # ``next_attempt_at`` in every shape a notice can hold it: absent (a notice
        # stamped before the backoff field existed), explicitly null, empty, a real
        # instant, and a non-string.
        {"state": "pending", "attempts": 1, "next_attempt_at": None},
        {"state": "pending", "attempts": 1, "next_attempt_at": ""},
        {"state": "pending", "attempts": 1, "next_attempt_at": "2026-07-29T00:00:30+00:00"},
        {"state": "pending", "attempts": "not-a-number", "next_attempt_at": "2026-07-29T00:00:30+00:00"},
        {"state": "sent", "attempts": 2, "next_attempt_at": "2026-07-29T00:10:00+00:00"},
        {"state": "pending", "attempts": 1, "next_attempt_at": 5},
    ]

    import json as _json

    for stored in notices:
        with sqlite.engine.begin() as conn:
            conn.execute(
                sa_update(agent_runs)
                .where(agent_runs.c.id == run.id)
                .values(metadata_json=_json.dumps({OWED_FAILURE_NOTICE_KEY: stored}))
            )
        expect = notice_write_expectation(stored)
        with sqlite.engine.connect() as conn:
            matched = conn.execute(
                sa_update(agent_runs)
                .where(agent_runs.c.id == run.id)
                .where(*owed_notice_state_unchanged(expect))
                .values(updated_at="2026-07-01T00:00:00+00:00")
            ).rowcount
        assert matched == 1, (
            f"SQL disagreed with notice_write_expectation({stored!r}) == {expect!r}; "
            "a guarded write would be refused with no race at all"
        )
        # ...and the same predicate rejects the neighbouring expectations, so the
        # agreement above is not a predicate that matches everything. One neighbour per
        # field, INCLUDING ``next_attempt_at`` — a predicate that silently ignored the
        # third element would pass every other assertion in this test.
        for wrong in (
            (expect[0], expect[1] + 1, expect[2]),
            (expect[0] + "x", expect[1], expect[2]),
            (expect[0], expect[1], expect[2] + "x"),
        ):
            with sqlite.engine.connect() as conn:
                assert (
                    conn.execute(
                        sa_update(agent_runs)
                        .where(agent_runs.c.id == run.id)
                        .where(*owed_notice_state_unchanged(wrong))
                        .values(updated_at="2026-07-01T00:00:00+00:00")
                    ).rowcount
                    == 0
                ), f"{wrong!r} must not match a notice whose expectation is {expect!r}"


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
    # The notice drain is dispatched from the watch rather than awaited by it, so a
    # service built without ``__init__`` still needs the handle the dispatcher keys its
    # single-flight check on.
    service._notice_drain_task = None
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


# --- group 2f: single-flight BEFORE the external side effect ----------------


def _second_owner_store(tmp_path: Path) -> tuple[SQLiteBackgroundTaskStore, TaskExecutionStore]:
    """A genuinely separate store pair over the SAME database file.

    Not ``_store``'s objects passed twice: the race these tests exist for is between
    two OS processes, so the two owners must not be able to coordinate through shared
    Python state — no shared engine, no shared connection, no shared identity map. Two
    ``SQLiteBackgroundTaskStore`` instances over one file is the closest single-process
    approximation, and it is faithful where it matters: the guarded UPDATE is evaluated
    by SQLite under its single-writer lock, which is the same primitive across
    connections and across processes.
    """

    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    requests = TaskExecutionStore(tmp_path / "task_requests")
    requests._sqlite = sqlite
    return sqlite, requests


def _owed_slack_notice(sqlite, requests, definition_id: str) -> str:
    """One failed run owing one pending notice, addressed at a Slack channel rung."""

    from storage.background import NOTICE_PENDING

    _task(sqlite, definition_id, name="daily report", deliver_key="slack::channel::C123")
    run = requests.enqueue_task_run(definition_id)
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id=definition_id)
    assert sqlite.owed_failure_notice(run.id)["state"] == NOTICE_PENDING
    return run.id


def test_two_owners_reading_one_pending_notice_deliver_it_exactly_once(
    tmp_path: Path,
) -> None:
    """HFR-076, subordinate — the consuming outcome: single-flight before the external send.

    The ``(state, attempts)`` expectation on the notice write stops a stale pass from
    OVERWRITING a newer acknowledgement. It does not stop that pass from DELIVERING,
    because it is only reached after the send has already happened: both owners read
    ``pending``, both walk the ladder, both await ``_emit_failure_notice``, and only
    then does either attempt its CAS. The loser's write is rejected — and the user has
    two identical messages, which no database predicate can recall.

    Nothing downstream closes it either. ``MessageDispatcher.emit_agent_message``
    checks ``agent_message_exists`` BEFORE ``send_message`` and persists AFTER it, so
    two owners both pass the pre-send lookup while neither receipt exists yet. The
    unique ``(platform, native_message_id)`` row resolves the later database race
    only: it keeps the transcript honest, one row for two sends, which is precisely
    why the count has to be taken further out.

    So the count is taken at the IM adapter's ``send_message`` — the last hop before
    the platform. Not at the drain's intent (a pass that stood down never had one) and
    not at the persisted rows (they dedupe, and would report success for a duplicate
    the user can see). Everything between the drain and that hop runs for real: the
    ladder, the replay emitter, ``agent_message_exists``, ``persist_agent_message`` and
    the guarded write. Only the transport is stubbed, as it is throughout this file.

    Handing owner B a run dict listed BEFORE anything was written is load-bearing: an
    implementation that only tightened the SQL listing would otherwise pass, while the
    real drain reads its batch and then awaits deliveries one at a time.

    The fix has to be a durable claim taken BEFORE the side effect — the attempt
    consumed and a lease armed in one guarded UPDATE — so the second owner either
    loses that CAS or sees the lease and stands down. A process-local lock cannot do
    it (two processes), and neither can a post-send CAS.
    """

    _migrated_state_db()

    sqlite_a, requests_a = _store(tmp_path)
    sqlite_b, requests_b = _second_owner_store(tmp_path)
    assert sqlite_a.engine is not sqlite_b.engine, "two owners, two connections"

    run_id = _owed_slack_notice(sqlite_a, requests_a, "task-race")

    sends: list[tuple[str, str, str]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    from tests.test_message_dispatcher_scheduled import _StubIMClient

    class _GatedIMClient(_StubIMClient):
        """Owner A, suspended with its request already on the wire."""

        async def send_message(self, context, text, parse_mode=None, reply_to=None):
            sends.append(("A", context.channel_id, text))
            entered.set()
            await release.wait()
            return "slack-msg-A"

    class _SecondOwnerIMClient(_StubIMClient):
        async def send_message(self, context, text, parse_mode=None, reply_to=None):
            sends.append(("B", context.channel_id, text))
            return "slack-msg-B"

    controller_a, _dispatcher_a, _touched_a = _live_turn_dispatcher()
    controller_a.im_client = _GatedIMClient()
    controller_b, _dispatcher_b, _touched_b = _live_turn_dispatcher()
    controller_b.im_client = _SecondOwnerIMClient()

    service_a = _drain_service(tmp_path, controller_a, sqlite_a, requests_a)
    service_b = _drain_service(tmp_path, controller_b, sqlite_b, requests_b)

    # Both owners load the same pending notice, each on its own connection, BEFORE
    # either has written anything at all.
    run_a = sqlite_a.list_owed_failure_notices()[0]
    run_b = sqlite_b.list_owed_failure_notices()[0]
    assert run_a["id"] == run_b["id"] == run_id

    async def scenario() -> None:
        task_a = asyncio.create_task(service_a._deliver_one_failure_notice(sqlite_a, run_a))
        await asyncio.wait_for(entered.wait(), timeout=30)
        # The second owner takes over and runs to completion while A is still in
        # flight — the handoff the ownership check cannot see, because it was
        # consulted once, before the await.
        await service_b._deliver_one_failure_notice(sqlite_b, run_b)
        release.set()
        await task_a

    asyncio.run(scenario())

    assert len(sends) == 1, (
        f"exactly one outbound IM send may leave for one owed notice, got {len(sends)}: {sends}"
    )
    rows = _persisted_messages()
    assert [row["type"] for row in rows] == ["notify"]
    notice = sqlite_a.owed_failure_notice(run_a["id"])
    assert notice["state"] == NOTICE_SENT
    assert notice["attempts"] == 1
    assert not notice["error"]


def test_a_dead_claimants_lease_expires_into_exactly_one_recovered_delivery(
    tmp_path: Path,
) -> None:
    """HFR-076, subordinate — the bounded-recovery half of the same claim.

    A claim taken before the send buys single-flight at the price of a new way to
    lose a notice: the claimant can die holding it. A process killed inside the send
    leaves ``pending`` with an armed lease and no owner, and if that lease never
    expired the notice would be owed forever with nothing reporting it — a worse
    outcome than the duplicate the claim exists to prevent.

    So the lease is a BOUND, not a lock: once it elapses the row is eligible again,
    any owner may re-claim it, and the recovered delivery consumes its OWN attempt
    rather than riding the dead one. That is what keeps the retry ladder finite —
    a claim that could be inherited for free would let a repeatedly-dying claimant
    retry without limit and never dead-letter.

    The claimant dies with ``Task.cancel``, which is faithful twice over: the drain's
    ``except Exception`` deliberately does not swallow ``CancelledError``, so nothing
    is written on the way out, and the send is abandoned BEFORE the adapter records
    it — the transport never accepted the request, so no user has this notice yet.
    The lease is elapsed by rewinding it rather than by sleeping, exactly as the
    raising-rung test elapses the backoff.
    """

    _migrated_state_db()

    sqlite_a, requests_a = _store(tmp_path)
    sqlite_b, requests_b = _second_owner_store(tmp_path)

    run_id = _owed_slack_notice(sqlite_a, requests_a, "task-lease")

    sends: list[tuple[str, str, str]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    from tests.test_message_dispatcher_scheduled import _StubIMClient

    class _DyingIMClient(_StubIMClient):
        """Owner A, killed before the transport ever accepted the request."""

        async def send_message(self, context, text, parse_mode=None, reply_to=None):
            entered.set()
            await release.wait()
            sends.append(("A", context.channel_id, text))  # pragma: no cover
            return "slack-msg-A"  # pragma: no cover

    class _RecoveringIMClient(_StubIMClient):
        async def send_message(self, context, text, parse_mode=None, reply_to=None):
            sends.append(("B", context.channel_id, text))
            return "slack-msg-B"

    controller_a, _dispatcher_a, _touched_a = _live_turn_dispatcher()
    controller_a.im_client = _DyingIMClient()
    controller_b, _dispatcher_b, _touched_b = _live_turn_dispatcher()
    controller_b.im_client = _RecoveringIMClient()

    service_a = _drain_service(tmp_path, controller_a, sqlite_a, requests_a)
    service_b = _drain_service(tmp_path, controller_b, sqlite_b, requests_b)

    run_a = sqlite_a.list_owed_failure_notices()[0]

    async def scenario() -> None:
        task_a = asyncio.create_task(service_a._deliver_one_failure_notice(sqlite_a, run_a))
        await asyncio.wait_for(entered.wait(), timeout=30)
        task_a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task_a

        # The lease elapses, with no claimant left to renew or release it.
        sqlite_b.update_owed_failure_notice(run_id, next_attempt_at=None)

        owed = sqlite_b.list_owed_failure_notices()
        assert [item["id"] for item in owed] == [run_id], (
            "an expired claim must make the row eligible again, or a claimant that "
            f"died mid-send owes the user a notice forever: {owed}"
        )
        await service_b._deliver_one_failure_notice(sqlite_b, owed[0])

    asyncio.run(scenario())

    assert [entry[0] for entry in sends] == ["B"], (
        f"recovery must deliver exactly once, and only the survivor: {sends}"
    )
    rows = _persisted_messages()
    assert [row["type"] for row in rows] == ["notify"]
    notice = sqlite_a.owed_failure_notice(run_id)
    assert notice["state"] == NOTICE_SENT
    assert notice["attempts"] == 2, (
        "the recovered delivery must consume its OWN attempt rather than riding the "
        f"dead claimant's, or a dying claimant retries without bound: {notice}"
    )


def test_a_wedged_delivery_times_out_into_one_durably_retryable_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-076, subordinate — the claim must be bounded in TIME, not just in owners.

    The claim taken before the send makes a second owner stand down for
    ``CLAIM_LEASE_SECONDS``. That is the right trade for a claimant that DIES, and the
    wrong one for a claimant that never returns: a transport that accepted the request
    and hung (no timeout, a half-open socket, a platform that stopped answering) leaves
    the pass suspended inside ``_emit_failure_notice`` forever. Nothing recovers it —
    the row is not eligible while the lease holds, and the coroutine holding it is not
    going to fail — so the notice is owed indefinitely with no error anywhere.

    So the ladder walk gets a deadline of its own, and the interesting part is what the
    deadline does to the CLAIM. It CONSUMES it, it does not release it: the attempt was
    already made durable by the claim before the send, so the timeout writes the
    ordinary retry — ``(pending, attempts=1)`` with the declared first backoff armed —
    under the expectation the claim left behind. Releasing it instead (rewinding
    ``attempts``) would let a transport that hangs every time retry without bound and
    never dead-letter.

    Two properties beyond "it returned", both of which a naive ``asyncio.wait_for``
    around the wrong scope would break:

    * the hung transport is CANCELLED and observed to be cancelled, so no detached
      coroutine survives to send behind a replacement claimant's back;
    * the row's armed retry is the declared FIRST interval, which is the item-7
      indexing fix showing up where a user would feel it.

    The send is recorded only when the stub COMPLETES, mirroring the wedge being
    modelled: an HTTP call still sitting on the wire never reached the platform, so the
    one message the user eventually gets is the recovered owner's.

    Honest residual, stated because the timeout cannot remove it: a transport that
    accepted the request and is cancelled after that acceptance leaves a delivered
    message and a retryable row — the same at-least-once window
    ``CLAIM_LEASE_SECONDS`` documents for a claimant that dies there. And the bound
    itself assumes the transport honours cancellation; one that swallowed
    ``CancelledError`` would hang the deadline too.
    """

    from datetime import datetime, timezone

    from core import failure_notices
    from storage.background import NOTICE_PENDING

    _migrated_state_db()

    sqlite_a, requests_a = _store(tmp_path)
    sqlite_b, requests_b = _second_owner_store(tmp_path)

    run_id = _owed_slack_notice(sqlite_a, requests_a, "task-wedge")

    sends: list[tuple[str, str, str]] = []
    entered = asyncio.Event()
    never_returns = asyncio.Event()
    cancelled: list[str] = []

    from tests.test_message_dispatcher_scheduled import _StubIMClient

    class _WedgedIMClient(_StubIMClient):
        """A transport that took the call and never came back."""

        async def send_message(self, context, text, parse_mode=None, reply_to=None):
            entered.set()
            try:
                await never_returns.wait()
            except asyncio.CancelledError:
                cancelled.append("A")
                raise
            sends.append(("A", context.channel_id, text))  # pragma: no cover
            return "slack-msg-A"  # pragma: no cover

    class _RecoveringIMClient(_StubIMClient):
        async def send_message(self, context, text, parse_mode=None, reply_to=None):
            sends.append(("B", context.channel_id, text))
            return "slack-msg-B"

    controller_a, _dispatcher_a, _touched_a = _live_turn_dispatcher()
    controller_a.im_client = _WedgedIMClient()
    controller_b, _dispatcher_b, _touched_b = _live_turn_dispatcher()
    controller_b.im_client = _RecoveringIMClient()

    service_a = _drain_service(tmp_path, controller_a, sqlite_a, requests_a)
    service_b = _drain_service(tmp_path, controller_b, sqlite_b, requests_b)

    # ``raising=False`` is load-bearing rather than defensive: this test has to be
    # runnable against the tree where no such bound exists, and the missing constant
    # IS the defect. With ``raising=True`` the red would be a fixture error.
    monkeypatch.setattr(
        failure_notices, "NOTICE_DELIVERY_TIMEOUT_SECONDS", 0.05, raising=False
    )

    run_a = sqlite_a.list_owed_failure_notices()[0]
    assert run_a["id"] == run_id

    async def scenario() -> None:
        armed_from = datetime.now(timezone.utc)
        # The outer bound is what makes the red a clean failure instead of a hung
        # pytest: against an unbounded await this pass never returns at all.
        await asyncio.wait_for(service_a._deliver_one_failure_notice(sqlite_a, run_a), 5.0)

        assert entered.is_set(), "the wedge must have reached the transport"
        assert cancelled == ["A"], (
            "a timed-out delivery must CANCEL its transport, not detach it to send "
            f"later behind a replacement claimant: {cancelled}"
        )

        timed_out = dict(sqlite_a.owed_failure_notice(run_id))
        assert timed_out["state"] == NOTICE_PENDING, (
            f"a timed-out delivery must stay retryable, got {timed_out['state']}"
        )
        assert timed_out["attempts"] == 1, (
            "the timeout must consume the claim's attempt rather than release it, or "
            f"a permanently hanging transport never dead-letters: {timed_out}"
        )
        assert "timed out" in (timed_out["error"] or "").lower(), (
            f"the stamped error must say what happened: {timed_out['error']!r}"
        )
        armed = datetime.fromisoformat(timed_out["next_attempt_at"])
        delay = (armed - armed_from).total_seconds()
        assert 2.0 <= delay <= 3.5, (
            "a first failed attempt must arm the declared FIRST backoff interval "
            f"(2 s), got {delay:.2f}s"
        )

        # Durably retryable by a DIFFERENT owner on its own connection. The armed
        # backoff is rewound rather than slept through, exactly as the raising-rung
        # and dead-claimant tests elapse theirs.
        sqlite_b.update_owed_failure_notice(run_id, next_attempt_at=None)
        owed = sqlite_b.list_owed_failure_notices()
        assert [item["id"] for item in owed] == [run_id], (
            f"a timed-out claim must return the row to the eligible set: {owed}"
        )
        await service_b._deliver_one_failure_notice(sqlite_b, owed[0])

    asyncio.run(scenario())

    assert [entry[0] for entry in sends] == ["B"], (
        f"one owed notice may produce one completed send, by the survivor: {sends}"
    )
    rows = _persisted_messages()
    assert [row["type"] for row in rows] == ["notify"]
    notice = sqlite_a.owed_failure_notice(run_id)
    assert notice["state"] == NOTICE_SENT
    assert notice["attempts"] == 2, (
        "the recovered delivery must consume its own attempt, not the wedged one's: "
        f"{notice}"
    )


def test_a_wedged_notice_delivery_does_not_stall_the_store_watch(tmp_path: Path) -> None:
    """HFR-076, subordinate — the drain may not be on the serial watch's critical path.

    ``_watch_store`` is ONE coroutine doing every periodic pass in sequence, and it
    awaited ``_drain_failure_notices`` inline. A single notice whose delivery does not
    return therefore stops the entire tick: request draining, callbacks, vault
    callbacks, the stale-run sweep, and — the recursive part — every LATER notice,
    including the ones that would have reported the failures this wedge is a symptom
    of. The service stays "healthy" the whole time, because the loop is not crashed,
    it is suspended.

    A per-notice deadline alone does not fix this: it bounds the stall at the deadline
    instead of removing it, and a deadline long enough to be safe for a legitimate
    ladder walk is far too long to hold the tick. So the drain is DISPATCHED from the
    watch rather than awaited by it, and the deadline bounds the dispatched work.

    Single-flight is asserted alongside liveness, because the cheap version of this
    fix — spawn a task every tick — trades a stalled loop for a notice delivered once
    per 2 s tick. One entry into the transport across several ticks is the property.

    The watch is driven for real; only the periodic passes that are not the subject
    are stubbed, and the sweep and vault passes are counted because they sit AFTER the
    notice drain in the tick and so are exactly what a wedge starves.
    """

    _migrated_state_db()

    sqlite, requests = _store(tmp_path)
    _owed_slack_notice(sqlite, requests, "task-watch-wedge")

    entries: list[str] = []
    entered = asyncio.Event()
    never_returns = asyncio.Event()

    from tests.test_message_dispatcher_scheduled import _StubIMClient

    class _WedgedIMClient(_StubIMClient):
        async def send_message(self, context, text, parse_mode=None, reply_to=None):
            entries.append(context.channel_id)
            entered.set()
            await never_returns.wait()
            return "slack-msg"  # pragma: no cover

    controller, _dispatcher, _touched = _live_turn_dispatcher()
    controller.im_client = _WedgedIMClient()
    service = _drain_service(tmp_path, controller, sqlite, requests)

    counts = {"sweeps": 0, "vault": 0}
    ticked = asyncio.Event()

    def _sweep() -> None:
        counts["sweeps"] += 1
        if counts["sweeps"] >= 2 and counts["vault"] >= 2:
            ticked.set()

    async def _vault() -> None:
        counts["vault"] += 1

    async def _recovered_outputs() -> None:
        return None

    service._running = True
    service._notice_drain_task = None
    service._drain_recovered_activity_outputs = _recovered_outputs
    # Held steady so the tick's conditional half (reconcile, request/callback drains)
    # stays out of the way: the subject is the UNCONDITIONAL passes after the drain.
    service.store.maybe_reload = lambda: False
    service.request_store.maybe_reload = lambda: False
    service._drain_vault_callbacks = _vault
    service._sweep_stale_runs = _sweep

    async def scenario() -> None:
        watch = asyncio.create_task(service._watch_store())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5.0)
            # Two ticks is one real 2 s sleep. The generous bound is for a loaded
            # machine, not for the behaviour: against an inline await this never
            # advances at all.
            await asyncio.wait_for(ticked.wait(), timeout=15.0)
            assert not never_returns.is_set(), (
                "the point is that the watch advanced while the delivery was STILL wedged"
            )
            assert entries == ["C123"], (
                "the wedged notice must be delivered once, not re-dispatched every "
                f"tick: {entries}"
            )
        finally:
            service._running = False
            watch.cancel()
            drain = service._notice_drain_task
            if drain is not None:
                drain.cancel()
            for task in (watch, drain):
                if task is None:
                    continue
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    asyncio.run(scenario())

    assert counts["sweeps"] >= 2 and counts["vault"] >= 2, (
        "a wedged notice delivery must not stop the store watch's later passes: "
        f"{counts}"
    )


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

    The REPLAY guard is asserted here too, not only in
    ``test_a_replayed_notice_reaches_the_user_without_touching_the_live_lifecycle``.
    That test pins it for rung (1), a Slack channel; a project rung reaches the
    dispatcher on the one platform where a terminal ``result`` also drives the
    workbench sidebar dot and the SSE turn stream, so "the drain never enters the
    live ``emit_backend_failure`` lifecycle" is a distinct claim on this path rather
    than a restatement of that one.
    """

    import core.backend_failure as backend_failure_module
    import core.scheduled_tasks as scheduled_tasks
    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT

    pushed = _no_background_web_push(monkeypatch)
    controller, _dispatcher, touched = _live_turn_dispatcher()
    controller.agent_auth_service = _ForbiddenAuthService()
    scope_id = _workbench_session("sesWork", project="proj-notice")

    async def _forbidden_live_emit(*args, **kwargs):
        raise AssertionError("the drain called the LIVE backend-failure emitter")

    # Snapshot BEFORE patching: ``raising=False`` below CREATES the attribute, which
    # would make the structural check below trivially true forever after.
    drain_imports_live_emitter = hasattr(scheduled_tasks, "emit_backend_failure")

    # Both spellings — patching only the definition leaves the drain's own
    # module-level reference, the one it would actually call, untouched.
    monkeypatch.setattr(backend_failure_module, "emit_backend_failure", _forbidden_live_emit)
    monkeypatch.setattr(
        scheduled_tasks, "emit_backend_failure", _forbidden_live_emit, raising=False
    )

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
    emissions = _spy_emissions(controller)

    rungs = service._failure_notice_targets(sqlite.get_run(run.id))
    assert [target.to_key() for target, _ in rungs] == [
        "avibe::project::proj-notice",
        "avibe::project::sesWork",
    ], f"a project-scoped rung must survive parsing: {rungs}"

    asyncio.run(service._drain_failure_notices())

    # --- the replay guard, on the workbench path -----------------------------
    assert [item["type"] for item in emissions] == ["notify"], (
        f"a workbench notice must be exactly one visible notify and nothing else: {emissions}"
    )
    output = emissions[0]["output"]
    assert output.completes_turn is False and output.settles_run is False, (
        "a receipt about an already-terminal run may not settle a turn or a run"
    )
    assert touched == [], f"the workbench replay mutated live turn state: {touched}"
    assert not drain_imports_live_emitter, (
        "the drain must not so much as import the live failure emitter"
    )

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

    The worst shape of that is a definition whose bound Session row has been
    DELETED, which is what this drives. Rung (5) is still BUILT — it is keyed on the
    run's session id, not on the row's existence — so it addresses a project
    candidate that resolves to nothing: ``AvibeBot.send_message`` still returns an
    id, ``persist_agent_message`` still returns before writing, and an ack on that
    id would mark the notice ``sent`` forever with no row, no push and no dead
    letter. That is strictly worse than the gap the drain exists to close.

    Two phases, because "does not ack" is only half a contract. A rung that must not
    acknowledge must also not CONSUME the notice:

    * pass 1 — a stale project candidate on every rung. Both rungs send, neither
      persists, so nothing acks: the notice stays ``pending``, records no
      ``ack_evidence``, and carries the reason a receipt was missing.
    * pass 2 — the same notice, still owed, after the Session row exists again
      (ARCHIVED, which is the real delta rung (5) buys: ``_session_row`` has no
      status filter where rung (2)'s ``resolve_session_id_target`` refuses an
      archived session outright). It persists, and NOW it acks — on the receipt, with
      the attempt count carried forward from the pass that walked on.

    Pass 1 also pins the per-rung evidence: one shared ``DeliveryEvidence`` latches
    ``delivered`` true forever once any rung sets an id, so the first rejected rung
    would both stop the walk and hand the eventual ack/dead letter another rung's
    ``ack_evidence``.
    """

    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT
    from storage.background import NOTICE_PENDING

    _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    # The schema, but deliberately NO session row for ``sesGone`` yet: the binding
    # points at a session that has been deleted.
    _migrated_state_db()

    sqlite, requests = _store(tmp_path)
    _task(
        sqlite,
        "task-avibe-ack",
        name="nightly report",
        session_id="sesGone",
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
        "avibe::project::sesGone",
    ], f"a deleted session loses rung (2) and still BUILDS rung (5): {rungs}"

    # --- pass 1: nothing durable, so nothing acknowledges -------------------
    asyncio.run(service._drain_failure_notices())

    channels = [channel for channel, _thread, _text in controller.im_client.sent]
    assert channels == ["proj-gone", "sesGone"], (
        "a synthetic send id must not end the walk; the next rung has to be tried: "
        f"{channels}"
    )
    assert _persisted_messages() == [], (
        "neither candidate resolves a scope or a session row, so nothing can persist"
    )

    notice = dict(sqlite.owed_failure_notice(run.id))
    assert notice["state"] == NOTICE_PENDING, (
        "a stale project candidate may not mark the notice sent: "
        f"{notice['state']} / {notice.get('ack_evidence')}"
    )
    assert not notice.get("ack_evidence"), (
        f"a synthetic send id is not an acknowledgement: {notice.get('ack_evidence')}"
    )
    assert notice["attempts"] == 1, "the pass consumed exactly one attempt"
    assert "without a persisted receipt" in (notice["error"] or ""), (
        f"the retry must say why the rung was refused, not 'no evidence': {notice['error']}"
    )
    # Still RETRYABLE rather than dead-lettered: a backoff instant, not a terminal state.
    assert notice["next_attempt_at"], "a refused rung must leave the notice schedulable"

    # --- pass 2: the same owed notice, delivered by a rung that now receipts --
    scope_id = _workbench_session("sesGone", project="proj-live", status="archived")
    # Let the backoff elapse without sleeping (the same rewind the retry tests use).
    sqlite.update_owed_failure_notice(run.id, next_attempt_at=None)
    assert [row["id"] for row in sqlite.list_owed_failure_notices(limit=10)] == [run.id], (
        "the notice has to still be OWED for a later rung to be able to deliver it"
    )

    asyncio.run(service._drain_failure_notices())

    rows = _persisted_messages()
    assert [row["session_id"] for row in rows] == ["sesGone"], (
        f"only the rung that persisted anything counts as delivered: {rows}"
    )
    assert rows[0]["scope_id"] == scope_id

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == NOTICE_SENT
    assert notice["ack_evidence"] == ACK_EVIDENCE_RECEIPT, (
        "the ack must carry the WINNING rung's evidence, not the rejected rung's"
    )
    assert notice["attempts"] == 2, (
        "the attempt the refused pass consumed is carried forward, not reset"
    )


def test_every_ladder_target_class_declares_its_acknowledgement_source() -> None:
    """HFR-079 — a ladder target may not inherit an acknowledgement by accident.

    The three findings in this class were all the same shape: a target whose
    acknowledgement source nobody had decided, silently answered by whichever
    branch the predicate happened to fall through to. Fixing them one platform at a
    time leaves the NEXT target — a new platform kind, a new scope type, a new rung
    — to rediscover the trap, and the failure mode is invisible: an undeclared
    target that acks on a send id turns a visible dead letter into a permanent false
    ``sent``.

    So the policy is a TABLE (``LADDER_ACK_SOURCES``) keyed by target class, and
    this test asserts the table is total over what the ladder can produce. Both axes
    are read out of the code that produces them and never listed here — that is the
    whole point:

    * the platform-kind axis from ``PLATFORM_REGISTRY``, whose ``kind`` field is the
      structural distinction between a real IM transport and the workbench, plus the
      one kind the registry cannot supply — an unregistered platform, which a
      free-string ``deliver_key`` makes reachable;
    * the scope-type axis from the two parsers ``_add`` builds every rung with,
      driven here so the constants are proven to be what the parsers enforce rather
      than a second copy of it.

    Add a platform kind or a scope type without declaring its acknowledgement
    source and this fails. At RUNTIME the same omission is safe rather than
    permissive — the lookup falls back to the receipt — but silent, which is why the
    enumeration is checked here.
    """

    from config.platform_registry import PLATFORM_REGISTRY
    from core.scheduled_tasks import (
        ACK_EVIDENCE_BY_ACK_SOURCE,
        ACK_SOURCE_NATIVE_DELIVERY_ID,
        ACK_SOURCE_PERSISTED_RECEIPT,
        LADDER_ACK_SOURCES,
        LADDER_PLATFORM_KIND_UNREGISTERED,
        LADDER_SCOPE_TYPES,
        SCOPE_ID_SCOPE_TYPES,
        SESSION_KEY_SCOPE_TYPES,
        UNDECLARED_LADDER_ACK_SOURCE,
        ParsedSessionKey,
        failure_notice_ack_source,
        failure_notice_target_class,
        parse_scope_id,
        parse_session_key,
    )

    # --- axis 1: the scope types, proven against the parsers themselves -----
    for scope_type in SESSION_KEY_SCOPE_TYPES:
        assert parse_session_key(f"slack::{scope_type}::X").scope_type == scope_type
    for scope_type in SCOPE_ID_SCOPE_TYPES:
        assert parse_scope_id(f"slack::{scope_type}::X").scope_type == scope_type
    scope_types = SESSION_KEY_SCOPE_TYPES | SCOPE_ID_SCOPE_TYPES
    assert scope_types == LADDER_SCOPE_TYPES

    # A scope type outside that vocabulary cannot reach the ladder at all: both
    # parsers refuse it, so ``_add`` drops the rung rather than handing the policy a
    # class it has never heard of. This is what bounds the axis to the union above.
    for parser in (parse_session_key, parse_scope_id):
        with pytest.raises(ValueError):
            parser("slack::not-a-scope-type::W1")

    # --- axis 2: the platform kinds, from the registry ----------------------
    registered_kinds = {descriptor.kind for descriptor in PLATFORM_REGISTRY.values()}
    assert registered_kinds, "the registry is the platform-kind axis; an empty one proves nothing"
    platform_kinds = registered_kinds | {LADDER_PLATFORM_KIND_UNREGISTERED}
    assert LADDER_PLATFORM_KIND_UNREGISTERED not in registered_kinds, (
        "the unregistered kind must stay distinct from every registered one"
    )

    # --- the table is total over the product of the two ---------------------
    declared = set(LADDER_ACK_SOURCES)
    expected = {(kind, scope_type) for kind in platform_kinds for scope_type in scope_types}
    assert declared == expected, (
        "every (platform kind, scope type) the ladder can produce must declare its "
        f"acknowledgement source: missing {sorted(expected - declared)}, "
        f"stale {sorted(declared - expected)}"
    )

    # ...and the classifier's whole RANGE is inside that domain — every registered
    # platform AND an unregistered one — so no target the ladder can build today
    # resolves by fallback. The fallback stays load-bearing for the case this test
    # is here to catch: a scope type added to a parser without a row.
    for platform in list(PLATFORM_REGISTRY) + ["brandnew"]:
        for scope_type in scope_types:
            target = ParsedSessionKey(platform=platform, scope_type=scope_type, scope_id="X")
            assert failure_notice_target_class(target) in LADDER_ACK_SOURCES, (
                f"{target.to_key()} resolves by fallback rather than by declaration"
            )

    # --- every source is interpretable, and the undeclared one is the SAFE one ---
    for source in set(LADDER_ACK_SOURCES.values()) | {UNDECLARED_LADDER_ACK_SOURCE}:
        assert source in ACK_EVIDENCE_BY_ACK_SOURCE, (
            f"acknowledgement source {source!r} admits no stated evidence"
        )
    assert UNDECLARED_LADDER_ACK_SOURCE == ACK_SOURCE_PERSISTED_RECEIPT, (
        "an undeclared target must default to the STRICTER source, never the permissive one"
    )
    # An unregistered platform takes the same strict answer, through its declared
    # row: a deliver key can name any platform string, and ``parse_session_key`` does
    # not check it against the registry.
    assert (
        failure_notice_ack_source(parse_session_key("brandnew::channel::C1"))
        == ACK_SOURCE_PERSISTED_RECEIPT
    )
    # The two named classes, spot-checked at the call site's own granularity so the
    # table cannot be reshuffled without one of these moving.
    assert (
        failure_notice_ack_source(parse_session_key("slack::channel::C1"))
        == ACK_SOURCE_NATIVE_DELIVERY_ID
    )
    assert (
        failure_notice_ack_source(parse_scope_id("avibe::project::proj-1"))
        == ACK_SOURCE_PERSISTED_RECEIPT
    )


def test_every_registry_platform_declares_its_kind_explicitly() -> None:
    """Subordinate to HFR-075/079 — the ack policy's permissive row rests on a default.

    The test above proves ``LADDER_ACK_SOURCES`` is TOTAL over the axes; it cannot
    prove the axis value is right, and for ``kind`` that value is the whole trust
    boundary. ``("im", channel|user)`` are the permissive rows — a notice sent there
    acks on the id the send returned — and the premise is that such an id was minted
    by a platform that reached a person. ``PlatformDescriptor.kind`` DEFAULTS to
    ``"im"``, so a transport added to the registry without stating its kind inherits
    the permissive answer, and the totality test above stays green because no new
    kind appeared. If that transport mints its own id the way ``AvibeBot.send_message``
    does, every defect in this class reopens with nothing failing.

    Checked in the SOURCE rather than at runtime because at runtime the two cases are
    indistinguishable: a defaulted ``kind`` and an explicit ``kind="im"`` produce the
    same object. The registry is a module-level dict literal of direct constructor
    calls, so the AST answers the question exactly — and the assertion that the
    parsed ids equal ``PLATFORM_REGISTRY``'s own keys is what keeps this honest if
    that ever stops being true: a registry the AST can no longer read fails here
    instead of silently passing over nothing.
    """

    import ast
    import dataclasses
    import inspect

    from config import platform_registry
    from config.platform_registry import PLATFORM_REGISTRY, PlatformDescriptor

    # The default is real — this test is not asserting a hypothetical.
    kind_field = next(
        field for field in dataclasses.fields(PlatformDescriptor) if field.name == "kind"
    )
    assert kind_field.default == "im", (
        "if kind is mandatory now, this test is obsolete rather than merely passing"
    )

    tree = ast.parse(inspect.getsource(platform_registry))
    registry_literal = None
    for node in ast.walk(tree):
        # The registry is an annotated assignment today; accept both spellings so a
        # dropped annotation does not read as a missing registry.
        targets = (
            [node.target]
            if isinstance(node, ast.AnnAssign)
            else node.targets
            if isinstance(node, ast.Assign)
            else []
        )
        if any(
            isinstance(target, ast.Name) and target.id == "PLATFORM_REGISTRY"
            for target in targets
        ):
            registry_literal = node.value
    assert isinstance(registry_literal, ast.Dict), (
        "PLATFORM_REGISTRY is no longer a dict literal; this guard must be rewritten "
        "rather than deleted — the kind axis is still a trust boundary"
    )

    undeclared: list[str] = []
    parsed_ids: list[str] = []
    for key, value in zip(registry_literal.keys, registry_literal.values):
        assert isinstance(key, ast.Constant), "every registry key must be a literal id"
        parsed_ids.append(str(key.value))
        assert isinstance(value, ast.Call) and getattr(value.func, "id", "") == (
            "PlatformDescriptor"
        ), f"{key.value} is not a direct PlatformDescriptor(...) call; rewrite this guard"
        if not any(keyword.arg == "kind" for keyword in value.keywords):
            undeclared.append(str(key.value))

    assert set(parsed_ids) == set(PLATFORM_REGISTRY), (
        "the AST did not read the live registry, so it proves nothing: parsed "
        f"{sorted(set(parsed_ids))}, registered {sorted(PLATFORM_REGISTRY)}"
    )
    assert not undeclared, (
        f"{undeclared} inherit PlatformDescriptor.kind's default of 'im', which grants "
        "them LADDER_ACK_SOURCES' permissive rows — a failure notice may be marked "
        "delivered on the id their send returns. State kind= explicitly, and if the "
        "transport mints that id itself, it is not 'im'."
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


def _agent_run_statements(sqlite_store, call) -> list[tuple[str, Any]]:
    """Every ``agent_runs`` SELECT one call issues, with its bound parameters."""

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
    return captured


def _agent_run_query_result_sizes(sqlite_store, db_path: Path, call) -> list[int]:
    """How many rows each ``agent_runs`` SELECT one call hands back.

    The statement count says how many round trips a read costs; this says how much
    of the table each round trip drags into Python. A read that filters in Python
    passes both a statement-count budget and a plan that names an index while still
    materialising an unbounded population, so the size of the result set is asserted
    separately.
    """

    import sqlite3

    raw = sqlite3.connect(str(db_path))
    try:
        return [
            len(raw.execute(statement, parameters).fetchall())
            for statement, parameters in _agent_run_statements(sqlite_store, call)
        ]
    finally:
        raw.close()


def _seed_streak_history(
    sqlite_store, definition_id: str, total: int, *, ever_succeeded: bool = True
) -> None:
    """A long settled history whose failures sit in short, closed streaks.

    ``ever_succeeded=False`` is the OPPOSITE case and it is the one that matters most:
    a definition with no success anywhere has ONE streak as long as its lifetime, so
    every bound expressed in terms of "the streak" is vacuous there. That is the shape
    the old ``2 * len(streak) + 2`` decode budget could not constrain.
    """

    _task(sqlite_store, definition_id)
    for index in range(total):
        # Successes every seventh run, so the streak containing any given failure is
        # at most six rows long while the lifetime is ``total``.
        status = "succeeded" if ever_succeeded and index % 7 == 0 else "failed"
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
    """HFR-095 — the streak decision read runs on the 2 s tick and must be bounded.

    Repointed at ``failure_streak_decision``, which is what the drain asks now. The
    original scenario removed a lifetime ``SELECT`` ordered by ``(created_at, id)``
    whose plan was::

        SEARCH agent_runs USING INDEX ix_agent_runs_definition_created (definition_id=?)
        USE TEMP B-TREE FOR LAST TERM OF ORDER BY

    — one constrained term, an unindexed sort of the whole definition, and 5000
    metadata blobs decoded to return one row. The seek-based replacement fixed the
    plan but kept HANDING THE STREAK'S ROWS BACK, and its decode budget was written
    as ``2 * len(streak) + 2``: a bound on the ANSWER, which is only a bound when the
    answer is small. On a definition that has never succeeded the streak IS the
    lifetime, so that budget permitted decoding the whole history — the same cost,
    per pending notice, per tick.

    Three properties, and the test would be incomplete without any of them:

    * ONE STATEMENT. Also the correctness property, not just a cost one: one
      statement is one SQLite read snapshot, which is what stops a success settling
      mid-read from merging two streaks (see
      ``test_a_success_settling_mid_read_cannot_merge_two_streaks``).
    * ZERO metadata blobs decoded in Python, on BOTH shapes. Not "few" — the notice
      states are compared inside SQLite through ``OWED_NOTICE_STATE_SQL``, so the
      honest number is zero and any nonzero number means rows are crossing back into
      Python again.
    * The plan's CONSTRAINED TERMS, not the index name (HFR-086's lesson: a plan can
      name an index while the term that matters stays a per-row filter). Both boundary
      seeks, and the window seek constrained on BOTH ends.
    """

    import storage.background as background
    from unittest.mock import patch

    sqlite_store, _requests = _store(tmp_path)
    lifetime = 5000
    _seed_streak_history(sqlite_store, "task-streak-plan", lifetime)
    # A failure late in the history whose streak is closed by a success on BOTH
    # sides, so both boundary seeks have something to find.
    target = "run-04997"
    # The never-succeeded definition: one streak, `lifetime` rows long.
    unbroken = 1500
    _seed_streak_history(sqlite_store, "task-never-succeeded", unbroken, ever_succeeded=False)

    def _decode_count(call) -> tuple[int, Any]:
        decoded: list[int] = []
        real_json_loads = background._json_loads

        def _counting_json_loads(value, default):
            decoded.append(1)
            return real_json_loads(value, default)

        with patch.object(background, "_json_loads", _counting_json_loads):
            answer = call()
        return len(decoded), answer

    # 1. THE BRACKETED STREAK. run-04992 is the earliest pending row between the
    #    successes at 4991 and 4998, so it is the canonical notice.
    decoded, facts = _decode_count(
        lambda: sqlite_store.failure_streak_decision("task-streak-plan", target)
    )
    assert facts == {
        "in_streak": True,
        "has_sent_elsewhere": False,
        "earliest_pending_id": "run-04992",
    }, facts
    assert decoded == 0, (
        f"decoded {decoded} metadata blobs out of {lifetime} rows — the notice states are "
        "compared in SQL, so nothing should be decoded in Python at all"
    )

    # 2. THE NEVER-SUCCEEDED DEFINITION, where "bounded by the streak" is no bound.
    decoded, facts = _decode_count(
        lambda: sqlite_store.failure_streak_decision("task-never-succeeded", "run-01499")
    )
    assert facts == {
        "in_streak": True,
        "has_sent_elsewhere": False,
        "earliest_pending_id": "run-00000",
    }, facts
    assert decoded == 0, (
        f"decoded {decoded} metadata blobs for a definition whose streak is its whole "
        f"{unbroken}-run lifetime — this is the case the old O(streak) budget could not "
        "constrain"
    )

    # 3. ONE STATEMENT, and one row back from it.
    db_path = tmp_path / "state" / "vibe.sqlite"
    statements = _agent_run_statements(
        sqlite_store, lambda: sqlite_store.failure_streak_decision("task-streak-plan", target)
    )
    assert len(statements) == 1, (
        f"the streak decision must be ONE statement — one read snapshot — but issued "
        f"{len(statements)}"
    )
    sizes = _agent_run_query_result_sizes(
        sqlite_store, db_path, lambda: sqlite_store.failure_streak_decision("task-streak-plan", target)
    )
    assert sizes == [1], f"the decision is three scalars on one row; the read returned {sizes}"

    # 4. THE PLAN.
    plans = _agent_run_query_plans(
        sqlite_store,
        db_path,
        lambda: sqlite_store.failure_streak_decision("task-streak-plan", target),
    )
    assert plans, "the streak decision read issued no agent_runs query"
    lines = [line for _statement, plan in plans for line in plan]
    rendered = "\n".join(lines)
    compact = rendered.replace(" ", "")

    # ``SCAN agent_runs`` is the property, and it is asserted as such rather than as
    # ``"SCAN" not in ...``: the outer projection has no FROM at all, so SQLite reports
    # a ``SCAN CONSTANT ROW`` for it. That is the single zero-table row the three
    # scalars are projected onto, and pinning the allowed set keeps the assertion from
    # being weakened into "no scan of agent_runs, probably".
    assert {line for line in lines if "SCAN" in line} <= {"SCAN CONSTANT ROW"}, (
        f"the streak decision must never scan a definition's history; plans were:\n{rendered}"
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
    # ...and the window itself, constrained on BOTH ends by the boundaries above. This
    # is the term the sentinel ``coalesce`` exists to preserve: spelled as
    # ``(boundary IS NULL OR position > boundary)`` it would be a disjunction, and
    # SQLite cannot use a disjunction as an index constraint.
    assert "(definition_id=?AND(created_at,id)>(?,?)AND(created_at,id)<(?,?))" in compact, (
        f"the streak window must be an indexed range on both ends; plans were:\n{rendered}"
    )


def test_the_sql_streak_facts_match_the_materialized_streak(tmp_path: Path) -> None:
    """HFR-096 — the one-statement decision must decide what the streak's rows decided.

    Repointed at ``failure_streak_decision``, and the oracle is COMPOSED rather than
    replaced: ``_materialized_streak`` still computes the streak the pre-SQL algorithm
    computed, and ``_facts`` still derives from those rows exactly what ``decide`` used
    to derive inline. Their composition is therefore the full old behaviour end to end,
    and it is what the new statement is checked against — a parity test against a
    reimplementation of the new code would prove nothing.

    Randomised histories carry every row class that decides the answer: successes and
    failures, ``canceled``/``queued`` rows that are not verdicts at all, interruptions
    whose reason IS in ``RUN_INTERRUPTION_REASONS`` (transparent: skipped, and neither
    joining nor closing a streak), interruptions whose reason is NOT
    (``no_terminal_result`` and friends — ordinary failures that DO join),
    ``watch_runtime`` heartbeats, notices in all four states, and runs sharing one
    ``created_at`` so the ``id`` tie-break decides ordering.

    Every run in the history is asked for, not just failures: the streak is defined
    relative to the run it is given, and a caller that hands it a succeeded or
    excluded row must get the same answer it got before. ``run-does-not-exist`` is
    asked too, because that is the case where a missing anchor could turn the window
    into the definition's whole history.
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
            streak = _materialized_streak(sqlite_store, definition_id, run_id)
            expected = _facts(streak, run_id)
            actual = sqlite_store.failure_streak_decision(definition_id, run_id)
            assert actual == expected, (
                f"seed {seed}, run {run_id}: the SQL streak facts disagree with the ones "
                f"derived from the materialized streak\n"
                f"  streak   {[(row['id'], row['status'], (row.get('notice') or {}).get('state')) for row in streak]}\n"
                f"  expected {expected}\n"
                f"  actual   {actual}"
            )


def _legacy_decide(
    *,
    run_id: str,
    definition_id: str | None,
    notice: dict | None,
    streak: list[dict],
    earlier_unsettled: dict | None,
):
    """``core.failure_notices.decide`` as of ``467b7c80``, taking the streak's ROWS.

    Byte-for-byte the branch order and reason strings of the version that derived
    "anyone sent?" and "who is canonical?" in Python. Kept as the oracle for
    ``test_the_facts_decide_exactly_what_the_streak_rows_decided``: moving a
    derivation into SQL is only safe if no outcome moved with it, and that is a claim
    about the OLD code, so the old code has to be present to check it against.
    """

    from core.failure_notices import (
        ACTION_DEFER,
        ACTION_DELIVER,
        ACTION_SKIP,
        NoticeDecision,
        is_binding_change,
        is_interruption,
    )

    if is_interruption(notice):
        return NoticeDecision(ACTION_DELIVER, "interruption")
    if is_binding_change(notice):
        return NoticeDecision(ACTION_DELIVER, "binding_change")
    if not definition_id:
        return NoticeDecision(ACTION_DELIVER, "no_definition")
    if earlier_unsettled is not None:
        return NoticeDecision(ACTION_DEFER, f"earlier_run_unsettled:{earlier_unsettled['id']}")
    others = [row for row in streak if row["id"] != run_id]
    if any((row.get("notice") or {}).get("state") == "sent" for row in others):
        return NoticeDecision(ACTION_SKIP, "streak_already_notified")
    canonical = next(
        (row for row in streak if (row.get("notice") or {}).get("state") == "pending"),
        None,
    )
    if canonical is None or canonical["id"] == run_id:
        return NoticeDecision(ACTION_DELIVER, "canonical")
    return NoticeDecision(ACTION_DEFER, f"canonical_pending:{canonical['id']}")


def test_the_facts_decide_exactly_what_the_streak_rows_decided() -> None:
    """Subordinate to HFR-096 — the rewrite must not move a single outcome.

    ``decide`` stopped receiving the streak and started receiving three facts about
    it. Parity for the SQL that produces those facts is
    ``test_the_sql_streak_facts_match_the_materialized_streak``; this is parity for the
    CONSUMER, against ``_legacy_decide`` — the version that took the rows.

    The matrix is exhaustive over what the branches read, not sampled: every notice
    lane (ordinary failure, each interruption reason in and out of the lane, binding
    change), present and absent ``definition_id``, present and absent
    ``earlier_unsettled``, and every streak shape that can change the answer —
    empty, this row alone, this row with earlier/later rows in each of the four
    notice states, this row not pending itself, and a streak this row is not in.

    ``reason`` is compared, not just ``action``: the reasons are written into the row
    as ``skip_reason``/``defer_reason`` and read back by operators, and
    ``canonical_pending:<id>`` names a specific run.
    """

    from core.failure_notices import decide

    states = [None, "pending", "sent", "skipped", "failed"]

    def _row(run_id: str, state: str | None) -> dict:
        return _streak_row(run_id, notice=None if state is None else _notice(state))

    run_id = "run-02"
    streaks: list[list[dict]] = [
        [],
        [_row("run-02", "pending")],
        [_row("run-02", "sent")],
        [_row("run-02", None)],
        # A streak this row is not a member of at all — the shape a non-verdict anchor
        # produced as ``[]`` before, and which must still deliver.
        [_row("run-01", "pending"), _row("run-03", "sent")],
    ]
    for earlier in states:
        for later in states:
            streaks.append(
                [_row("run-01", earlier), _row("run-02", "pending"), _row("run-03", later)]
            )
            # ...and the same with THIS row not pending, which decides whether the
            # canonical can be a row other than the one being asked about.
            streaks.append(
                [_row("run-01", earlier), _row("run-02", "failed"), _row("run-03", later)]
            )

    notices: list[dict] = [
        _notice("pending"),
        _notice("pending", kind="binding_change"),
        _notice("pending", interrupt_reason="restarted"),
        _notice("pending", interrupt_reason="no_terminal_result"),
        _notice("pending", interrupt_reason=""),
    ]
    unsettled = [None, {"id": "run-00", "created_at": "2026-07-27T00:00:00+00:00", "status": "queued"}]

    checked = 0
    for streak in streaks:
        for notice in notices:
            for definition_id in ("task-1", None):
                for earlier_unsettled in unsettled:
                    expected = _legacy_decide(
                        run_id=run_id,
                        definition_id=definition_id,
                        notice=notice,
                        streak=streak,
                        earlier_unsettled=earlier_unsettled,
                    )
                    actual = decide(
                        run_id=run_id,
                        definition_id=definition_id,
                        notice=notice,
                        streak_facts=_facts(streak, run_id),
                        earlier_unsettled=earlier_unsettled,
                    )
                    assert (actual.action, actual.reason) == (expected.action, expected.reason), (
                        f"streak {[(row['id'], (row.get('notice') or {}).get('state')) for row in streak]}, "
                        f"notice {notice}, definition {definition_id}, unsettled {earlier_unsettled}: "
                        f"expected {expected}, got {actual}"
                    )
                    checked += 1

    # The matrix is worth stating: a silently-shrinking parity sweep is how a rewrite
    # gets declared equivalent against three cases.
    assert checked == len(streaks) * len(notices) * 2 * 2 == 1100


def test_a_success_settling_mid_read_cannot_merge_two_streaks(tmp_path: Path) -> None:
    """Subordinate to HFR-095 — the decision must describe ONE state of the database.

    pysqlite opens no transaction for reads, so a multi-statement read is several
    snapshots. The streak's boundaries were seeked by one statement and its rows read
    by a later one, and a success settling in that gap made the read describe a
    history that had already stopped existing: the boundary said "no success before
    this run", so the row read swept in the PREVIOUS outage's rows, and that outage's
    ``sent`` notice made ``decide`` answer SKIP for a live one.

    That answer is written DURABLY (``state='skipped'``), which is what makes it a
    lost notice rather than a late one: nothing revisits a skipped row, so the outage
    is never reported at all. D1 direction.

    The invariant asserted is the one that matters and it is stated without reference
    to statement counts: the facts the drain is about to write must match the database
    it is writing about, computed independently by ``_materialized_streak`` AFTER the
    read returns. A concurrent writer is scheduled at the point where a multi-statement
    read is exposed — partway through. A one-statement read has no partway, which is
    the property, and it is why the writer never fires against it.
    """

    import sqlite3

    from sqlalchemy import event

    sqlite_store, _requests = _store(tmp_path)
    _task(sqlite_store, "task-torn")

    def _run(run_id: str, status: str, instant: str, state: str | None) -> None:
        sqlite_store.enqueue_run(
            {
                "id": run_id,
                "request_type": "scheduled",
                "status": status,
                "definition_id": "task-torn",
                "error": "boom" if status == "failed" else None,
                "created_at": instant,
                "completed_at": instant if status != "running" else None,
                "metadata": {} if state is None else {OWED_FAILURE_NOTICE_KEY: {"state": state, "attempts": 1}},
            }
        )

    # The PREVIOUS outage, already reported.
    _run("run-a", "failed", "2026-07-27T01:00:00+00:00", "sent")
    # The recovery that closes it — still RUNNING when the read starts, which is why
    # the boundary seek cannot see it yet.
    _run("run-s", "running", "2026-07-27T02:00:00+00:00", None)
    # The LIVE outage. Alone in its own streak once run-s settles, so the user must be
    # told about it.
    _run("run-b", "failed", "2026-07-27T03:00:00+00:00", "pending")

    db_path = tmp_path / "state" / "vibe.sqlite"
    seen: list[int] = []

    def _settle_the_recovery(conn, cursor, statement, parameters, context, executemany):
        if "agent_runs" not in statement or not statement.strip().upper().startswith(("SELECT", "WITH")):
            return
        seen.append(1)
        # From the THIRD statement onward: a multi-statement read has taken its
        # boundaries by then and has not yet read its rows, which is exactly the gap.
        # A one-statement read never reaches this point.
        if len(seen) < 3:
            return
        other = sqlite3.connect(str(db_path))
        try:
            other.execute(
                "update agent_runs set status = 'succeeded', completed_at = ? where id = 'run-s'",
                ("2026-07-27T02:30:00+00:00",),
            )
            other.commit()
        finally:
            other.close()

    event.listen(sqlite_store.engine, "before_cursor_execute", _settle_the_recovery)
    try:
        facts = sqlite_store.failure_streak_decision("task-torn", "run-b")
    finally:
        event.remove(sqlite_store.engine, "before_cursor_execute", _settle_the_recovery)

    # The database AS IT NOW STANDS, read independently of the decision.
    truth = _facts(_materialized_streak(sqlite_store, "task-torn", "run-b"), "run-b")
    assert facts == truth, (
        "the streak decision describes a history that no longer exists: it was written "
        f"from {facts} while the database says {truth}"
    )

    # ...and the consequence, so a future reader can see what the mismatch costs.
    from core.failure_notices import ACTION_SKIP, decide

    action = decide(
        run_id="run-b",
        definition_id="task-torn",
        notice=_notice("pending"),
        streak_facts=facts,
        earlier_unsettled=None,
    ).action
    expected_action = decide(
        run_id="run-b",
        definition_id="task-torn",
        notice=_notice("pending"),
        streak_facts=truth,
        earlier_unsettled=None,
    ).action
    assert action == expected_action, (
        f"the live outage on run-b is decided {action} from a stale window while the "
        f"database says {expected_action}"
    )
    if action == ACTION_SKIP:
        assert truth["has_sent_elsewhere"], (
            "run-b was skipped as a duplicate, so a sent notice really has to be in its "
            "streak — a skip is durable and nothing revisits it"
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
    # task-a degrades to unknown rather than taking the list down. Asserted exactly,
    # not as a disjunction: ``HEALTH_UNKNOWN`` is what its own docstring promises for
    # this row ("a health signal that cannot be computed must not read as a clean bill
    # of health"), and a read that answers ``failing`` here is answering from a window
    # it could not classify.
    assert healths["task-a"]["health"] == "unknown"


def test_a_malformed_history_row_reports_unknown_rather_than_a_verdict(
    tmp_path: Path,
) -> None:
    """HFR-072 — an unreadable row in the window is not evidence of anything.

    ``_health_rows``' predicates let a malformed row through as an ORDINARY verdict:
    ``INTERRUPT_REASON_SQL``'s ``CASE json_valid`` guard degrades the extract to NULL,
    which passes ``reason IS NULL``, so the row is counted as whatever ``status``
    happens to say. That guard exists to stop one bad blob failing the whole statement
    — it was never a claim that the row is classifiable — and the result contradicts
    ``HEALTH_UNKNOWN``'s own docstring, which promises unknown for exactly this row.

    The consequence is directional, which is why it is worth a scenario rather than a
    note: the badge reads ``healthy`` or ``failing`` with equal confidence off a
    history it could not read, and a wrongly-clean bill is the failure mode this whole
    feature exists to remove.

    The negative control is the third definition: a malformed row OUTSIDE the window
    must not make health unknown, or one ancient bad blob would blank a definition's
    badge forever. Unknown is scoped to the window the verdict is read from, which is
    the same scope the counters use.
    """

    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    sqlite, _ = _store(tmp_path)

    def _run(definition_id: str, index: int, status: str, hour: int) -> str:
        run_id = f"run-{definition_id}-{index:03d}"
        instant = f"2026-07-28T{hour:02d}:00:00+00:00"
        sqlite.enqueue_run(
            {
                "id": run_id,
                "request_type": "scheduled",
                "status": status,
                "definition_id": definition_id,
                "error": "boom" if status == "failed" else None,
                "created_at": instant,
                "completed_at": instant,
            }
        )
        return run_id

    def _break(run_id: str) -> None:
        with sqlite.engine.begin() as conn:
            conn.execute(
                sa_update(agent_runs).where(agent_runs.c.id == run_id).values(metadata_json="{broken")
            )

    # 1. the NEWEST verdict is unreadable, so "the latest run failed" is unknowable.
    _task(sqlite, "task-newest-bad")
    _run("task-newest-bad", 0, "succeeded", 1)
    _break(_run("task-newest-bad", 1, "failed", 2))

    # 2. an unreadable row INSIDE the window but not newest: the failure count over the
    #    window is unknowable too, so ``degraded`` is not answerable either.
    _task(sqlite, "task-inner-bad")
    _break(_run("task-inner-bad", 0, "failed", 1))
    _run("task-inner-bad", 1, "succeeded", 2)

    # 3. the negative control: unreadable, but displaced out of the window by ten
    #    later verdicts.
    _task(sqlite, "task-aged-bad")
    _break(_run("task-aged-bad", 0, "failed", 1))
    for index in range(1, 12):
        _run("task-aged-bad", index, "succeeded", 1 + index)

    healths = sqlite.definition_health_batch(
        ["task-newest-bad", "task-inner-bad", "task-aged-bad"],
        now="2026-07-29T00:00:00+00:00",
    )

    for definition_id in ("task-newest-bad", "task-inner-bad"):
        assert healths[definition_id]["health"] == "unknown", (
            f"{definition_id} has an unreadable row in its window and must report "
            f"unknown, not {healths[definition_id]['health']!r}"
        )
        # The same counter shape the batch-degrade path reports, so a caller rendering
        # "N consecutive failures" beside an unknown badge cannot read a number that
        # was never computed.
        assert healths[definition_id]["consecutive_failures"] == 0
        assert healths[definition_id]["recent_failures"] == 0

    assert healths["task-aged-bad"]["health"] == "healthy", (
        "an unreadable row outside the window must not blank the badge forever; got "
        f"{healths['task-aged-bad']}"
    )


def _ranked_health_rows(sqlite_store, definition_ids, *, now: str) -> dict[str, list[str]]:
    """The WINDOW-FUNCTION health read, kept as the oracle for the bounded one.

    Byte-for-byte the implementation ``_health_rows`` had before the correlated seek
    replaced it: ``row_number()`` over the whole 72 h window per definition, then
    ``position <= HEALTH_WINDOW_RUNS`` on the outside. It is the SEMANTICS that are
    being preserved — the cost is what changed — so the old shape stays here as an
    executable specification rather than as prose in a docstring (HFR-096's pattern:
    ``_materialized_streak`` does the same job for the streak read).
    """

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, or_

    from storage.background import (
        HEALTH_WINDOW_HOURS,
        HEALTH_WINDOW_RUNS,
        _id_batches,
        _not_an_out_of_band_interruption,
        _parse_iso_instant,
        _status_query_values,
        _WATCH_RUNTIME_RUN_TYPE,
        normalize_run_status,
    )
    from storage.models import agent_runs

    ids = [str(value or "").strip() for value in definition_ids]
    ids = [value for value in dict.fromkeys(ids) if value]
    if not ids:
        return {}
    instant = _parse_iso_instant(now) or datetime.now(timezone.utc)
    cutoff = (instant - timedelta(hours=HEALTH_WINDOW_HOURS)).isoformat()
    settled_at = func.coalesce(agent_runs.c.completed_at, agent_runs.c.created_at)
    verdicts = _status_query_values("succeeded") + _status_query_values("failed")

    def _statement(batch):
        ranked = (
            select(
                agent_runs.c.definition_id.label("definition_id"),
                agent_runs.c.status.label("status"),
                func.row_number()
                .over(
                    partition_by=agent_runs.c.definition_id,
                    order_by=[settled_at.desc(), agent_runs.c.id.desc()],
                )
                .label("position"),
            )
            .where(agent_runs.c.definition_id.in_(batch))
            .where(
                or_(
                    agent_runs.c.run_type.is_(None),
                    agent_runs.c.run_type != _WATCH_RUNTIME_RUN_TYPE,
                )
            )
            .where(agent_runs.c.status.in_(verdicts))
            .where(settled_at >= cutoff)
            .where(_not_an_out_of_band_interruption())
            .subquery()
        )
        return (
            select(ranked.c.definition_id, ranked.c.status)
            .where(ranked.c.position <= HEALTH_WINDOW_RUNS)
            .order_by(ranked.c.definition_id, ranked.c.position)
        )

    out: dict[str, list[str]] = {}
    with sqlite_store.engine.connect() as conn:
        for batch in _id_batches(ids):
            for row in conn.execute(_statement(batch)):
                out.setdefault(str(row[0]), []).append(normalize_run_status(row[1]))
    return out


def test_the_health_read_is_bounded_before_it_is_ranked(tmp_path: Path) -> None:
    """Subordinate to HFR-068 — the advertised 10-run bound must bound the DB work.

    ``HEALTH_WINDOW_RUNS`` says "the last ten verdicts", and the windowed form made
    SQLite assign ``row_number()`` to EVERY verdict in the 72 h window before the outer
    ``position <= 10`` could discard any of them. The settled-time index narrows by
    time and cannot stop after ten entries, so a high-frequency definition made every
    Harness list, show and mutation read walk its entire recent history to return ten
    rows. Measured on this history, the plan was::

        CO-ROUTINE anon_1
        CO-ROUTINE (subquery-3)
        SEARCH agent_runs USING INDEX ix_agent_runs_definition_settled (definition_id=? AND <expr>>?)
        SCAN (subquery-3)
        SCAN anon_1
        USE TEMP B-TREE FOR ORDER BY

    — the seek is there, and then the whole result is scanned twice and sorted, with no
    ``LIMIT`` anywhere to stop it.

    TWO properties, and the test would be wrong without either. The bound has to be in
    the DATABASE (``LIMIT`` inside the per-definition seek, an indexed order so it can
    early-exit), and it has to be reached in ONE statement, because
    ``test_a_page_costs_a_fixed_number_of_queries`` in
    ``tests/test_harness_definition_lifecycle.py`` pins a page's statement count at a
    fixed budget that the health read holds exactly one slot in. A per-definition
    statement loop satisfies the bound and reintroduces the per-row query that #1033
    removed; a batched window function satisfies the budget and walks the window. Only
    a batched statement whose per-definition work is a bounded correlated seek does
    both.

    Asserted on the CONSTRAINED TERMS, never on an index name: HFR-095 and HFR-086 are
    the same lesson — a plan can name the index while the term stays a per-row filter.
    """

    import re

    sqlite, _ = _store(tmp_path)
    # Far more than ten verdicts inside the window, which is the only case where the
    # ranking cost differs from the bound.
    for definition_id in ("task-hot-a", "task-hot-b", "task-hot-c"):
        _task(sqlite, definition_id)
        for index in range(400):
            instant = f"2026-07-27T{index // 60:02d}:{index % 60:02d}:00+00:00"
            sqlite.enqueue_run(
                {
                    "id": f"run-{definition_id}-{index:04d}",
                    "request_type": "scheduled",
                    "status": "failed" if index % 4 else "succeeded",
                    "definition_id": definition_id,
                    "error": "boom" if index % 4 else None,
                    "created_at": instant,
                    "completed_at": instant,
                }
            )

    plans = _agent_run_query_plans(
        sqlite,
        tmp_path / "state" / "vibe.sqlite",
        lambda: sqlite._health_rows(
            ["task-hot-a", "task-hot-b", "task-hot-c"], now="2026-07-27T12:00:00+00:00"
        ),
    )
    assert len(plans) == 1, (
        "three definitions must cost ONE statement: the page-budget invariant in "
        "test_a_page_costs_a_fixed_number_of_queries gives this read a single slot, so "
        f"a statement per definition breaks it; issued {len(plans)}"
    )
    statement, plan = plans[0]
    rendered = "\n".join(plan)
    compact = rendered.replace(" ", "")

    assert "LIMIT" in statement.upper(), (
        "without a LIMIT inside the per-definition seek the bound is applied after the "
        f"read, and the whole window is still walked: {statement}"
    )
    # And the LIMIT is the PER-DEFINITION one, asserted on statement STRUCTURE. The
    # direct measurement — rows examined per statement — is not available: pysqlite
    # binds no ``sqlite3_stmt_status`` accessor, so there is no row counter to read, and
    # the plan text says a seek is used without saying where the bound sits. Structure
    # plus the plan shape is therefore the proportionate evidence, and it is what rules
    # out the one rewrite the assertions above would wave through: a statement-level
    # ``LIMIT 10`` over the whole batched result satisfies "a LIMIT exists" and answers
    # a different question — ten rows TOTAL, so every definition after the first loses
    # its verdicts. The LIMIT must therefore close INSIDE the correlated scalar
    # subquery, i.e. before its ``) AS verdicts`` and before the outer ``FROM
    # json_each`` over the id list.
    assert len(re.findall(r"\bLIMIT\b", statement, re.IGNORECASE)) == 1, (
        "exactly one LIMIT is expected, and it has to be the per-definition one: "
        f"{statement}"
    )
    assert re.search(
        r"\bLIMIT\b[^)]*\)\s*AS\s+recent\s*\)\s*AS\s+verdicts",
        statement,
        re.IGNORECASE | re.DOTALL,
    ), (
        "the LIMIT must belong lexically to the correlated per-definition subquery, "
        f"closing before the scalar subquery does: {statement}"
    )
    assert re.search(r"\bLIMIT\b.*\bFROM\s+json_each\b", statement, re.IGNORECASE | re.DOTALL), (
        "an outer LIMIT would render AFTER the FROM over the id list, so a bound that "
        f"appears before it is the statement's own rather than the seek's: {statement}"
    )
    assert "TEMP B-TREE" not in rendered, (
        f"the newest-first order must come from an index, not a sort; plan was:\n{rendered}"
    )
    # ``SCAN agent_runs``, not bare ``SCAN``. Two residual SCANs are correct and
    # unavoidable in this shape: ``SCAN d VIRTUAL TABLE INDEX 1`` walks the ``json_each``
    # id list, which is bounded by the page, and ``SCAN (subquery-1)`` walks the
    # already-LIMITed <=10-row co-routine. Neither touches the table. A ``SCAN
    # agent_runs`` would be the unbounded history read this scenario exists to remove.
    assert "SCAN agent_runs" not in rendered, (
        f"the health read must never scan a definition's history; plan was:\n{rendered}"
    )
    assert "(definition_id=?AND<expr>>?)" in compact, (
        "the per-definition window must be an indexed seek on (definition_id, settled) "
        f"— both terms constrained, not one plus a filter; plan was:\n{rendered}"
    )


def test_the_bounded_health_read_matches_the_ranked_health_read(tmp_path: Path) -> None:
    """Subordinate to HFR-068 — the bounded read has to be the SAME ten verdicts.

    Parity against ``_ranked_health_rows``, the window-function form this replaces,
    over randomised histories containing every row class that decides the answer:
    verdicts and non-verdicts (``canceled``, ``queued``, ``running``),
    interruptions whose reason IS in ``RUN_INTERRUPTION_REASONS`` (excluded) and whose
    reason is not (an ordinary per-fire failure, counted), ``watch_runtime`` heartbeats
    sharing the definition id, rows sharing one ``created_at`` so the ``id`` tie-break
    decides the order, more than ten verdicts inside the window, rows straddling the
    72 h cutoff on both sides, a definition with NO rows at all — the ranked form
    omits that key entirely rather than mapping it to an empty list, so the bounded
    form has to skip its empty result instead of returning one — and the LEGACY
    ``completed`` spelling that a literal ``('succeeded','failed')`` would drop.

    That last one has to be written PAST ``enqueue_run``, which normalizes ``status``
    on the way in (``_run_values`` -> ``normalize_run_status``): a fixture row asking
    for ``completed`` lands as ``succeeded``, so going through the front door would
    leave ``_status_query_values``' legacy lane — the reason neither reader may use a
    literal status list — entirely unexercised while appearing to cover it.

    Green on BOTH sides of the change by construction — that is the point. The plan
    test above is the one that goes red; this one exists so the plan test cannot be
    satisfied by a query that answers a different question, in particular by a LIMIT
    applied to a population the interruption predicate had not yet filtered, or by a
    result whose order comes from an aggregate that does not promise one.
    """

    import random

    sqlite, _ = _store(tmp_path)
    rng = random.Random(20260729)
    now = "2026-07-29T00:00:00+00:00"
    # No ``completed`` here on purpose: ``enqueue_run`` would normalize it to
    # ``succeeded`` on write, so it would add a status the fixture already has while
    # looking like legacy coverage. The real legacy row is written raw further down.
    statuses = ["succeeded", "failed", "canceled", "queued", "running"]
    reasons = [None, None, *sorted(RUN_INTERRUPTION_REASONS)[:3], "transport_unavailable"]
    definition_ids = [f"task-parity-{index}" for index in range(6)]

    for definition_id in definition_ids:
        _task(sqlite, definition_id)
        # Enough rows that at least one definition reaches the ten-verdict bound after
        # the non-verdict, interruption and cutoff filters have taken their share —
        # asserted below, because a fixture that never fills the window never tests the
        # LIMIT at all.
        for index in range(rng.randint(0, 60)):
            # Hours spread across and beyond the 72 h window, so rows land on both
            # sides of the cutoff. NOT on it: the arithmetic below floors the day and
            # subtracts the remainder from hour 23, so no ``hours`` value can produce
            # the cutoff INSTANT (``now - 72 h`` is midnight, and every row here lands
            # at hour 23 - hours % 24). The boundary itself is covered absolutely by
            # ``test_the_health_window_includes_a_verdict_settled_on_the_cutoff``, for
            # the reason spelled out there: both readers share the ``>=`` predicate, so
            # parity can never see a boundary regression.
            hours = rng.choice([0, 1, 12, 12, 40, 40, 71, 71, 72, 73, 100])
            day = 29 - (hours // 24)
            hour = 23 - (hours % 24)
            # Some rows deliberately SHARE a timestamp, which is when ``id`` decides.
            minute = rng.choice([0, 0, 0, index % 60])
            instant = f"2026-07-{day:02d}T{hour:02d}:{minute:02d}:00+00:00"
            reason = rng.choice(reasons)
            sqlite.enqueue_run(
                {
                    "id": f"run-{definition_id}-{index:03d}",
                    "request_type": "scheduled",
                    "status": rng.choice(statuses),
                    "definition_id": definition_id,
                    "error": "boom",
                    "created_at": instant,
                    # Sometimes unset, so ``coalesce`` has to fall back.
                    "completed_at": instant if rng.random() < 0.8 else None,
                    "metadata": {"interrupt_reason": reason} if reason else {},
                }
            )
        # A supervisor heartbeat under the same definition id. The ``watches`` wrapper
        # is load-bearing: ``write_watch_runtime`` reads ``payload["watches"]``, so a
        # bare ``{definition_id: …}`` writes NOTHING and this coverage claim would be
        # false.
        if rng.random() < 0.5:
            sqlite.write_watch_runtime(
                {
                    "watches": {
                        definition_id: {
                            "running": True,
                            "started_at": "2026-07-28T23:59:00+00:00",
                        }
                    }
                },
                updated_at="2026-07-28T23:59:00+00:00",
            )

    # A definition that exists and has never run, asked for alongside the rest.
    _task(sqlite, "task-parity-empty")

    # And one whose ONLY verdict carries the legacy ``completed`` spelling, written
    # straight to the column because ``enqueue_run`` would normalize it away. One row,
    # inside the window, no interruption — so if either reader dropped the legacy
    # spelling this definition would vanish from the mapping entirely.
    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    _task(sqlite, "task-parity-legacy")
    sqlite.enqueue_run(
        {
            "id": "run-parity-legacy",
            "request_type": "scheduled",
            "status": "succeeded",
            "definition_id": "task-parity-legacy",
            "created_at": "2026-07-28T12:00:00+00:00",
            "completed_at": "2026-07-28T12:00:00+00:00",
        }
    )
    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs)
            .where(agent_runs.c.id == "run-parity-legacy")
            .values(status="completed")
        )
        assert (
            conn.execute(
                select(agent_runs.c.status).where(agent_runs.c.id == "run-parity-legacy")
            ).scalar_one()
            == "completed"
        ), "the legacy spelling must survive in the column, or this row proves nothing"

    asked = [*definition_ids, "task-parity-empty", "task-parity-legacy"]

    live = sqlite._health_rows(asked, now=now)
    oracle = _ranked_health_rows(sqlite, asked, now=now)

    assert live == oracle, (
        "the bounded correlated seek must return the same verdicts, in the same order, "
        f"as the ranked read:\nlive   {live}\noracle {oracle}"
    )
    assert "task-parity-empty" not in live, (
        "a definition with no verdicts must be ABSENT from the mapping, not present "
        f"with an empty list: {live}"
    )
    assert live.get("task-parity-legacy") == ["succeeded"], (
        "the legacy ``completed`` row must be SELECTED as a verdict and normalized on "
        f"the way out; got {live.get('task-parity-legacy')!r}"
    )
    # And the fixture actually exercised the interesting case.
    assert any(len(values) >= 5 for values in oracle.values()), (
        f"the randomised history produced almost no verdicts: {oracle}"
    )
    assert any(len(values) == 10 for values in oracle.values()), (
        f"no definition reached the 10-verdict bound, so the LIMIT was never tested: {oracle}"
    )


def test_the_health_window_includes_a_verdict_settled_on_the_cutoff(tmp_path: Path) -> None:
    """Subordinate to HFR-068 — the 72 h boundary is INCLUSIVE, pinned absolutely.

    ``_health_rows`` admits a row with ``settled >= cutoff``, where ``cutoff`` is
    ``now - HEALTH_WINDOW_HOURS``. The test above cannot defend that ``>=``: it is
    PARITY against ``_ranked_health_rows``, and both readers spell the boundary the
    same way, so flipping ``>=`` to ``>`` moves both lanes together and parity stays
    green while a definition whose only verdict settled exactly on the boundary
    silently reports as never-run. Parity is the wrong instrument for a predicate the
    oracle shares — the assertion here is therefore ABSOLUTE, against literal expected
    verdicts, and the parity check is kept only as a second, weaker witness.

    Two definitions, one row each, so the expected mapping is a literal and either
    direction of the bug is visible in it:

    * one settled at EXACTLY the cutoff instant, computed from the same ``now`` the
      read is given rather than hand-written, so it cannot drift from
      ``HEALTH_WINDOW_HOURS``. It must be PRESENT.
    * one settled one second earlier — the smallest step outside — which must be
      ABSENT, so an over-wide ``>`` -> ``>=`` on the wrong side of the comparison, or
      a cutoff computed with the wrong sign, does not pass either.

    The comparison happens in SQLite against the stored ISO TEXT, so the boundary row
    only proves anything if the string it was written with is the string the cutoff is
    rendered as. That is asserted on the raw column below rather than assumed:
    ``_run_values`` passes ``completed_at`` through verbatim today, and a normalizing
    writer added later would turn this test into a tautology without failing.
    """

    import re
    from datetime import timedelta

    from storage.background import HEALTH_WINDOW_HOURS, _parse_iso_instant
    from storage.models import agent_runs

    sqlite, _ = _store(tmp_path)
    now = "2026-07-29T00:00:00+00:00"
    # The cutoff exactly as ``_health_rows`` computes it, from the same ``now``.
    instant = _parse_iso_instant(now)
    assert instant is not None
    cutoff = (instant - timedelta(hours=HEALTH_WINDOW_HOURS)).isoformat()
    just_outside = (instant - timedelta(hours=HEALTH_WINDOW_HOURS, seconds=1)).isoformat()
    # Not the assertion, a guard on the fixture: a lexicographic comparison is only
    # an instant comparison while both strings are the same fixed-width ISO shape.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", cutoff), cutoff
    assert just_outside < cutoff

    # Distinct statuses, so the literal expected list below says WHICH row survived
    # the predicate rather than only how many did.
    _task(sqlite, "task-on-the-cutoff")
    sqlite.enqueue_run(
        {
            "id": "run-on-the-cutoff",
            "request_type": "scheduled",
            "status": "succeeded",
            "definition_id": "task-on-the-cutoff",
            "created_at": cutoff,
            "completed_at": cutoff,
        }
    )
    _task(sqlite, "task-outside-the-cutoff")
    sqlite.enqueue_run(
        {
            "id": "run-outside-the-cutoff",
            "request_type": "scheduled",
            "status": "failed",
            "definition_id": "task-outside-the-cutoff",
            "error": "boom",
            "created_at": just_outside,
            "completed_at": just_outside,
        }
    )

    with sqlite.engine.connect() as conn:
        stored = dict(
            conn.execute(
                select(agent_runs.c.id, agent_runs.c.completed_at).where(
                    agent_runs.c.definition_id.in_(
                        ["task-on-the-cutoff", "task-outside-the-cutoff"]
                    )
                )
            ).all()
        )
    assert stored == {"run-on-the-cutoff": cutoff, "run-outside-the-cutoff": just_outside}, (
        "the fixture's instants must survive the write byte-for-byte, or the boundary "
        f"row is not on the boundary the read compares against: {stored}"
    )

    asked = ["task-on-the-cutoff", "task-outside-the-cutoff"]
    live = sqlite._health_rows(asked, now=now)

    assert live == {"task-on-the-cutoff": ["succeeded"]}, (
        "the 72 h window is inclusive of its own edge and exclusive one second past it: "
        "a verdict settled exactly at now - 72 h must still count toward health, and a "
        f"definition whose only verdict is older must be absent; got {live}"
    )
    # Kept, but explicitly the weaker witness: it would pass with the predicate broken
    # in both lanes at once.
    assert live == _ranked_health_rows(sqlite, asked, now=now), (
        "the bounded read and the ranked oracle must also agree on the boundary"
    )
    # And the boundary really is decided by the window rather than by the row being
    # unreadable for some other reason: one hour later, both rows are inside.
    wider = sqlite._health_rows(asked, now="2026-07-28T23:00:00+00:00")
    assert wider == {
        "task-on-the-cutoff": ["succeeded"],
        "task-outside-the-cutoff": ["failed"],
    }, f"the excluded row must be a WINDOW exclusion, not an unreadable row: {wider}"


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
    # The one command a definition that no longer exists can still offer. The run row
    # outlives its definition, and ``runs show`` takes an optional positional run id.
    "vibe runs show ID": ["runs", "show", "ID"],
}

#: The keys the binding-change body is built from, in both languages — including the
#: deleted-definition pair, which replaces the repin/show commands rather than adding
#: to them, and so has to clear the same bar.
_BINDING_NOTICE_KEYS = (
    "harness.notice.rebound",
    "harness.notice.reboundSessions",
    "harness.notice.reboundSettingsPreserved",
    "harness.notice.reboundSettingsReset",
    "harness.notice.reboundRepin",
    "harness.notice.show",
    "harness.notice.definitionDeleted",
    "harness.notice.runInspect",
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


# --- round 9, gate item 3: the binding stamp's terminal CAS ------------------


def _binding_stamp(sqlite, run_id: str, *, task_id: str, signature: str = "sig-1"):
    """``stamp_binding_change_notice`` with the one transition shape that stamps."""

    return sqlite.stamp_binding_change_notice(
        run_id,
        task_id=task_id,
        signature=signature,
        action="rebound",
        reason="session_missing",
        previous_session_id="ses-gone",
        new_session_id="ses-fresh",
        settings_preserved=True,
    )


def test_a_binding_stamp_refuses_a_failure_terminalized_inside_its_gap(tmp_path: Path) -> None:
    """Subordinate to HFR-099 — the binding stamp must re-assert what it read.

    ``stamp_binding_change_notice`` is the one owed-notice writer that does not ride a
    terminal transition: it runs on a LIVE run, before ``complete()``. Round 7 audited
    it and excused it — "a pre-read stamp inside its own guarded read/modify/write" —
    on the assumption that being gated on "no existing notice" was enough. It is not,
    for the reason round 8 established one function over:
    pysqlite emits no ``BEGIN`` for a bare SELECT, so ``engine.begin()`` holds no lock
    across the read, and the write lock is first taken by the UPDATE.

    So the terminal writer is committed HERE, from another connection, in exactly that
    gap. It stamps its OWN failure notice as part of the same UPDATE that moves the row
    to ``failed`` (``_merge_owed_failure_notice``), and the binding stamp then wrote
    the whole ``metadata_json`` blob it had composed from the pre-transition snapshot
    over the top: the failure the user needs to hear about replaced by the news that a
    session was swapped, with the run reporting ``failed`` and no failure notice
    anywhere.
    """

    from sqlalchemy import event

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-binding-cas", deliver_key="slack::channel::C1")
    run = requests.enqueue_task_run("task-binding-cas")
    claimed = requests.claim(run.id)
    assert claimed is not None

    interleaved: list[str] = []

    def _terminalize_inside_the_gap(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if interleaved or not statement.lstrip().upper().startswith("UPDATE AGENT_RUNS"):
            return
        interleaved.append(statement)
        # The backend result lands and the run settles ``failed``, stamping the
        # failure notice, on its own connection and committed. The stamp's snapshot
        # is now stale; its UPDATE has not reached the driver yet.
        requests.complete(claimed, ok=False, error="backend exploded", task_id="task-binding-cas")

    event.listen(sqlite.engine, "before_cursor_execute", _terminalize_inside_the_gap)
    try:
        lost = _binding_stamp(sqlite, run.id, task_id="task-binding-cas")
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _terminalize_inside_the_gap)

    assert interleaved, "the terminal writer never got into the gap — this test proves nothing"
    assert lost is None, (
        "a binding stamp whose run terminalized between its read and its UPDATE must "
        f"report that it wrote nothing, got {lost}"
    )

    assert sqlite.get_run(run.id)["status"] == "failed"
    settled = sqlite.owed_failure_notice(run.id)
    assert settled is not None, "the terminal verdict's own notice must still be there"
    assert settled["failure_id"] == run.id, (
        "the FAILURE notice must survive byte-intact; the binding blob keys it "
        f"``binding:…`` and this one reads {settled['failure_id']!r}"
    )
    assert settled.get("kind") in (None, NOTICE_KIND_FAILURE), (
        f"a binding notice replaced the failure notice: {settled!r}"
    )
    assert settled.get("binding") is None, f"the binding payload must not be here: {settled!r}"
    assert settled["state"] == "pending", "the failure is still owed to the user"
    # And it is deliverable, which is the whole point of it surviving.
    assert [row["id"] for row in sqlite.list_owed_failure_notices()] == [run.id]


def test_a_binding_stamp_never_lands_on_a_run_canceled_inside_its_gap(tmp_path: Path) -> None:
    """Subordinate to HFR-099 — ``canceled`` is reserved for explicit stop semantics.

    The other damage direction, and the durable one. ``canceled`` owes no notice by
    design (``_owed_failure_notice_for_transition``: telling a user their run failed
    because they stopped it is noise), and ``list_owed_failure_notices`` selects only
    ``failed``/``succeeded``. So a binding notice stamped onto a row that another
    actor canceled in the read/write gap is written, is ``pending``, and is
    PERMANENTLY unreachable: excluded from every drain batch, retried never,
    dead-lettered never.

    The user's explicit Stop also outranks the rebind news on its own terms, so the
    correct outcome is not "deliver it later" but "never stamp it".
    """

    from sqlalchemy import event

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-binding-cancel", deliver_key="slack::channel::C1")
    run = requests.enqueue_task_run("task-binding-cancel")
    claimed = requests.claim(run.id)
    assert claimed is not None
    # The user pressed Stop while the fire was running; the settlement below maps the
    # ``cancel_requested`` row to ``canceled`` rather than ``failed``.
    assert sqlite.cancel_run(run.id)

    interleaved: list[str] = []

    def _cancel_inside_the_gap(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if interleaved or not statement.lstrip().upper().startswith("UPDATE AGENT_RUNS"):
            return
        interleaved.append(statement)
        requests.complete(claimed, ok=False, error="stopped", task_id="task-binding-cancel")

    event.listen(sqlite.engine, "before_cursor_execute", _cancel_inside_the_gap)
    try:
        lost = _binding_stamp(sqlite, run.id, task_id="task-binding-cancel")
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _cancel_inside_the_gap)

    assert interleaved, "the canceller never got into the gap — this test proves nothing"
    assert sqlite.get_run(run.id)["status"] == "canceled", "the stop must have won"
    assert lost is None, (
        f"a binding stamp must report that it wrote nothing onto a canceled run, got {lost}"
    )
    assert sqlite.owed_failure_notice(run.id) is None, (
        "a canceled run must carry NO owed notice: the drain excludes ``canceled``, so "
        "one written here is durable and undeliverable forever"
    )
    assert sqlite.list_owed_failure_notices() == []


def test_a_binding_stamp_on_a_malformed_metadata_row_is_refused_not_raised(
    tmp_path: Path,
) -> None:
    """Subordinate to HFR-084 — the stamp degrades, it does not take the fire down.

    Moving the write from a whole-blob overwrite to ``json_set`` means SQLite now
    parses ``metadata_json`` at WRITE time, and ``json_set`` over an unparseable blob
    raises ``malformed JSON`` — which would turn one bad row into an exception on the
    rebind path. The same ``CASE json_valid`` discipline the eligibility expressions
    document applies here: the row is refused, reported as "wrote nothing", and left
    exactly as it was.
    """

    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-binding-badjson")
    run = requests.enqueue_task_run("task-binding-badjson")
    assert requests.claim(run.id) is not None
    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs).where(agent_runs.c.id == run.id).values(metadata_json="{not json")
        )

    # Must not raise, and must not write.
    assert _binding_stamp(sqlite, run.id, task_id="task-binding-badjson") is None
    with sqlite.engine.connect() as conn:
        stored = conn.execute(
            select(agent_runs.c.metadata_json).where(agent_runs.c.id == run.id)
        ).scalar_one()
    assert stored == "{not json", "the malformed blob must be left exactly as it was"


def test_a_binding_stamp_writes_only_its_own_metadata_key(tmp_path: Path) -> None:
    """Subordinate to HFR-099 — a status CAS alone would not have been enough.

    The run this stamps on is LIVE, and live rows take concurrent metadata writes: the
    sweep records ``interrupt_reason``, the settler merges its ``ok`` marker. A
    compare-and-swap on ``(status, no notice)`` makes the notice slot safe and says
    nothing about the rest of the blob — a whole-``metadata_json`` write composed from
    the pre-read snapshot would still erase whichever sibling key landed in the gap,
    and erase it with the guard SATISFIED, which is the worst kind of pass.

    ``json_set`` addresses one path, so this asserts on the one thing that makes the
    guard total: a sibling written after the stamp's read survives it.
    """

    from sqlalchemy import event
    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-binding-siblings")
    run = requests.enqueue_task_run("task-binding-siblings")
    assert requests.claim(run.id) is not None

    interleaved: list[str] = []

    def _write_a_sibling_inside_the_gap(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if interleaved or not statement.lstrip().upper().startswith("UPDATE AGENT_RUNS"):
            return
        interleaved.append(statement)
        # Not a terminal transition, so the status CAS still passes — exactly the
        # interleaving a status-only guard would wave through.
        with sqlite.engine.begin() as other:
            other.execute(
                sa_update(agent_runs)
                .where(agent_runs.c.id == run.id)
                .values(metadata_json='{"interrupt_reason": "transport_unavailable"}')
            )

    event.listen(sqlite.engine, "before_cursor_execute", _write_a_sibling_inside_the_gap)
    try:
        stamped = _binding_stamp(sqlite, run.id, task_id="task-binding-siblings")
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _write_a_sibling_inside_the_gap)

    assert interleaved, "the sibling write never got into the gap — this test proves nothing"
    assert stamped is not None, "a non-terminal sibling write must not refuse the stamp"

    metadata = sqlite.get_run(run.id)["metadata"]
    assert metadata["interrupt_reason"] == "transport_unavailable", (
        f"the stamp must write only its own key, not the whole blob: {metadata!r}"
    )
    assert metadata[OWED_FAILURE_NOTICE_KEY]["failure_id"] == "binding:task-binding-siblings:sig-1"


def test_a_binding_stamp_survives_a_success_settled_inside_its_gap(tmp_path: Path) -> None:
    """Subordinate to HFR-099 — the third damage direction: a LOST notice.

    The round-9 CAS closed two directions and opened one. ``failed`` and ``canceled``
    both have a legitimate reason to refuse — the failure notice outranks the binding
    news, and a Stop outranks it too — but ``succeeded`` has none. A successful
    settlement writes NO notice at all, so refusing leaves the slot empty; and the
    refusal is PERMANENT, not deferred, because ``_emit_binding_change`` persists the
    ``binding_recovery`` dedup marker BEFORE it calls the stamp, so every later fire
    short-circuits on that marker and never retries. The user's pinned session was
    replaced and nothing ever tells them.

    ``list_owed_failure_notices`` was widened to failed+succeeded for exactly this
    row — "the one notice a succeeded run can owe" — so the slot is not merely empty,
    it is legitimately owed and deliverable. The store therefore re-reads the row once
    after a lost CAS and stamps when the winner is ``succeeded`` and the slot is still
    empty.

    The interleaving is the failed-race test's, with a SUCCESSFUL settlement as the
    racing write.
    """

    from sqlalchemy import event

    from storage.background import NOTICE_KIND_BINDING_CHANGE

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-binding-success", deliver_key="slack::channel::C1")
    run = requests.enqueue_task_run("task-binding-success")
    claimed = requests.claim(run.id)
    assert claimed is not None

    interleaved: list[str] = []

    def _settle_successfully_inside_the_gap(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if interleaved or not statement.lstrip().upper().startswith("UPDATE AGENT_RUNS"):
            return
        interleaved.append(statement)
        # The rebound retry's result lands and the run settles ``succeeded`` — no
        # error, so no failure notice, so the notice slot stays empty. The stamp's
        # status snapshot (``running``) is now stale; its UPDATE has not reached the
        # driver yet.
        requests.complete(claimed, ok=True, task_id="task-binding-success")

    event.listen(sqlite.engine, "before_cursor_execute", _settle_successfully_inside_the_gap)
    try:
        stamped = _binding_stamp(sqlite, run.id, task_id="task-binding-success")
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _settle_successfully_inside_the_gap)

    assert interleaved, "the successful settler never got into the gap — this test proves nothing"
    assert stamped is not None, (
        "a successful settlement writes no notice, so refusing the binding stamp loses "
        "it forever: the dedup marker is already durable and no later fire retries"
    )

    assert sqlite.get_run(run.id)["status"] == "succeeded"
    notice = sqlite.owed_failure_notice(run.id)
    assert notice is not None, "the binding notice must be on the row, not just returned"
    assert notice["failure_id"] == "binding:task-binding-success:sig-1"
    assert notice["kind"] == NOTICE_KIND_BINDING_CHANGE
    assert notice["state"] == "pending"
    # And it is DELIVERABLE — the reason ``list_owed_failure_notices`` selects
    # ``succeeded`` alongside ``failed`` at all.
    assert [row["id"] for row in sqlite.list_owed_failure_notices()] == [run.id]


def test_one_rebind_that_wins_its_race_still_notifies_exactly_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Subordinate to HFR-099 — the race fix must not turn one rebind into two notices.

    A GREEN-FROM-BIRTH PIN, said plainly: nothing here is a red-first fix. Both dedup
    layers already exist and are each exercised elsewhere — the ``binding_recovery``
    signature short-circuit at the top of ``_emit_binding_change``
    (``tests/test_scheduled_tasks.py`` covers "one broken binding, one notification")
    and the slot-occupied refusal inside ``stamp_binding_change_notice`` (the CAS tests
    just above). What no test did was put them TOGETHER on the path the round-10 change
    touched, and that omission was load-bearing: the success-race test above stops at
    store level and never calls ``_emit_binding_change`` at all, so the marker that
    gates every later fire does not exist in its world. It therefore proves the notice
    is not LOST and says nothing about it being DUPLICATED — and the retry the round-10
    change added is precisely a second stamp attempt against a row whose transition
    marker is already durable.

    The three steps are the whole claim, end to end on one store:

    1. the race, driven THROUGH ``_emit_binding_change`` so the marker is persisted
       before the stamp exactly as in production, resolves with one pending notice;
    2. a LATER FIRE of the same definition against the same binding — a real second
       ``agent_runs`` row, because reusing the first would be dedup'd by the occupied
       slot and would prove the wrong layer — reaches the stamp NOT AT ALL, and leaves
       exactly one owed row;
    3. the drain, through the real dispatcher, lands exactly ONE durable message, and a
       second pass lands nothing.

    Red-by-mutation evidence, since the test cannot go red against HEAD: neutering the
    signature short-circuit in ``_emit_binding_change`` makes step 2 stamp a second
    notice onto the later run, and both of its assertions fail.
    """

    from sqlalchemy import event

    from core.scheduled_tasks import SessionBindingChange
    from storage.background import NOTICE_KIND_BINDING_CHANGE, NOTICE_PENDING
    from vibe.i18n import t as i18n_t

    from tests.test_scheduled_tasks import _binding_env

    # One DB for the execution, the drain and the ``messages`` receipt, exactly as
    # ``_rebound_run`` arranges it for the HFR-099 delivery test.
    _binding_env(tmp_path, monkeypatch)
    _migrated_state_db()
    sqlite, requests = _store(tmp_path)

    controller, _dispatcher, _touched = _live_turn_dispatcher()
    service = _drain_service(tmp_path, controller, sqlite, requests)
    service._t = lambda key, **kwargs: i18n_t(key, "en", **kwargs)

    task = service.store.add_task(
        name="daily digest",
        session_key="",
        session_id="ses-fresh",
        session_policy="create_once",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        deliver_key="slack::channel::C123",
        metadata={"session_scope_id": "slack::channel::C123"},
    )
    change = SessionBindingChange(
        action="rebound",
        task_id=task.id,
        reason="session_missing",
        previous_session_id="sesdoesnotexist",
        detail="the pinned session was replaced",
        new_session_id="ses-fresh",
        settings_preserved=False,
    )

    # Every stamp ATTEMPT, not just every stamp that landed: the claim in step 2 is that
    # the later fire short-circuits before the store is asked, which a check on the
    # store's contents alone cannot distinguish from a refused attempt.
    attempts: list[tuple[str, str]] = []
    real_stamp = sqlite.stamp_binding_change_notice

    def _recording_stamp(run_id, **kwargs):
        attempts.append((run_id, str(kwargs.get("signature"))))
        return real_stamp(run_id, **kwargs)

    monkeypatch.setattr(sqlite, "stamp_binding_change_notice", _recording_stamp)

    # --- step 1: the race, through the real emitter -------------------------
    first = requests.enqueue_task_run(task.id, source_kind="scheduler", task=task)
    claimed = requests.claim(first.id)
    assert claimed is not None

    interleaved: list[str] = []

    def _settle_successfully_inside_the_gap(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if interleaved or not statement.lstrip().upper().startswith("UPDATE AGENT_RUNS"):
            return
        interleaved.append(statement)
        requests.complete(claimed, ok=True, task_id=task.id)

    event.listen(sqlite.engine, "before_cursor_execute", _settle_successfully_inside_the_gap)
    try:
        asyncio.run(service._emit_binding_change(change, run_id=first.id, run_error=None))
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _settle_successfully_inside_the_gap)

    assert interleaved, "the successful settler never got into the gap — this test proves nothing"
    # The marker really is durable BEFORE the stamp, which is what makes step 2's
    # short-circuit the only thing standing between one notice and one per fire.
    recorded = (service.store.get_task(task.id).metadata or {}).get("binding_recovery") or {}
    assert recorded.get("signature") == change.signature, (
        f"the dedup marker must be persisted by the emitter: {recorded!r}"
    )
    assert sqlite.get_run(first.id)["status"] == "succeeded"
    assert attempts == [(first.id, change.signature)]

    notice = sqlite.owed_failure_notice(first.id)
    assert notice is not None, "the rebind notice was lost to the race"
    assert notice["kind"] == NOTICE_KIND_BINDING_CHANGE
    assert notice["state"] == NOTICE_PENDING
    assert notice["failure_id"] == f"binding:{task.id}:{change.signature}"
    assert [row["id"] for row in sqlite.list_owed_failure_notices()] == [first.id]

    # --- step 2: a later fire against the same binding ----------------------
    # A fresh run row, settled ``succeeded`` like the first: a duplicate stamped here
    # would be selected by ``list_owed_failure_notices`` and delivered, which is the
    # damage this step exists to exclude. Reusing ``first`` would instead be refused by
    # the occupied notice slot and would prove a different guard.
    later = requests.enqueue_task_run(task.id, source_kind="scheduler", task=task)
    claimed_later = requests.claim(later.id)
    assert claimed_later is not None
    asyncio.run(service._emit_binding_change(change, run_id=later.id, run_error=None))
    requests.complete(claimed_later, ok=True, task_id=task.id)
    assert sqlite.get_run(later.id)["status"] == "succeeded"

    assert attempts == [(first.id, change.signature)], (
        "a later fire against the SAME binding must not reach the store at all: the "
        f"transition marker is the dedup key, not the run; attempts were {attempts}"
    )
    assert sqlite.owed_failure_notice(later.id) is None, (
        "the later fire must owe nothing — one broken binding is one notification"
    )
    assert [row["id"] for row in sqlite.list_owed_failure_notices()] == [first.id], (
        "exactly one row may be owed for this transition, however often it fires"
    )

    # --- step 3: exactly one durable message reaches the user ---------------
    emissions = _spy_emissions(controller)
    asyncio.run(service._drain_failure_notices())

    settled = sqlite.owed_failure_notice(first.id)
    assert settled["state"] == NOTICE_SENT
    assert [item["type"] for item in emissions] == ["notify"]
    assert len(controller.im_client.sent) == 1, (
        f"one transition must produce ONE message: {controller.im_client.sent}"
    )
    channel, _thread, sent_text = controller.im_client.sent[0]
    assert channel == "C123"
    assert "sesdoesnotexist" in sent_text and "ses-fresh" in sent_text, (
        f"the one message must name old -> new session: {sent_text!r}"
    )
    delivered = _persisted_messages()
    assert [row["type"] for row in delivered] == ["notify"]
    assert delivered[0]["content_text"] == sent_text

    # A second pass delivers NOTHING: the notice is sent and acked, so it is no longer
    # owed — the drain is idempotent rather than merely first-wins.
    asyncio.run(service._drain_failure_notices())
    assert not sqlite.list_owed_failure_notices()
    assert len(controller.im_client.sent) == 1
    assert [item["type"] for item in emissions] == ["notify"]
    assert _persisted_messages() == delivered


# --- round 9, gate item 5: copy for a definition that no longer exists -------


def _copy_service(tmp_path: Path, sqlite, requests):
    """A service wired for body rendering only, with the REAL translator."""

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()
    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)
    return service


def _every_vibe_command(body: str) -> list[list[str]]:
    """Every ``vibe …`` invocation the copy prints, as argv.

    A left word boundary so the ``[Avibe Harness]`` brand prefix is not read as an
    invocation, and one command per line because that is how the body is built.
    """

    import re
    import shlex

    commands: list[list[str]] = []
    for line in body.splitlines():
        for match in re.finditer(r"(?<![A-Za-z])vibe ", line):
            commands.append(shlex.split(line[match.end():].strip()))
    return commands


def test_a_deleted_definition_notice_names_only_run_level_recovery(tmp_path: Path) -> None:
    """Subordinate to HFR-094 — parser-validated copy, for a definition that is gone.

    ``get_task`` returns ``None`` once the definition row is deleted while the run row
    keeps its ``definition_id`` forever, and the failure body still appended
    ``harness.notice.rerun`` unconditionally: "Re-run it now with: vibe task run
    <id>". That command parses and then fails at runtime with "not found", which is
    the exact defect HFR-094 exists for one noun over — copy that names an action the
    user cannot take.

    A deleted definition has no rerun, no resume and no ``show``. What it does have is
    the run record, so the copy offers ``vibe runs show`` and says plainly that the
    definition is gone.
    """

    from vibe.cli import build_parser

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-deleted", deliver_key="slack::channel::C1")
    run = requests.enqueue_task_run("task-deleted")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-deleted")

    service = _copy_service(tmp_path, sqlite, requests)
    # The definition is removed AFTER the run settled, which is the ordinary case: a
    # user archives a task whose last fire failed and has not been told yet.
    assert service.store.remove_task("task-deleted")
    assert service.store.get_task("task-deleted") is None
    assert service.store.get_watch_definition("task-deleted") is None

    notice = sqlite.owed_failure_notice(run.id)
    assert notice is not None
    body = service._failure_notice_body(sqlite.get_run(run.id), notice)

    assert "vibe task" not in body, f"a deleted task cannot be addressed as a task: {body}"
    assert "vibe watch" not in body, f"a deleted definition is not a watch either: {body}"
    assert run.id in body, f"the run is the only thing left to inspect: {body}"

    parser = build_parser()
    printed = _every_vibe_command(body)
    assert printed, f"the copy must still offer the user something: {body}"
    for argv in printed:
        try:
            parser.parse_args(argv)
        except SystemExit:  # pragma: no cover - the assertion is the point
            raise AssertionError(f"the copy prints {argv!r}, which the CLI cannot parse: {body}")


def test_a_deleted_definition_binding_notice_drops_the_repin_command(tmp_path: Path) -> None:
    """Subordinate to HFR-094 — the same hole on the binding-change body.

    ``_binding_notice_body`` renders ``vibe task update <id> --session-id …`` and
    ``vibe task show <id>`` against a definition it never checked the existence of.
    A rebind notice can outlive its definition by the whole retry/backoff window, so
    "pin it back" is offered for a row that cannot be pinned.
    """

    from vibe.cli import build_parser
    from storage.background import NOTICE_KIND_BINDING_CHANGE

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-rebound-deleted", deliver_key="slack::channel::C1")
    run = requests.enqueue_task_run("task-rebound-deleted")
    claimed = requests.claim(run.id)
    assert claimed is not None
    stamped = _binding_stamp(sqlite, run.id, task_id="task-rebound-deleted")
    assert stamped is not None and stamped["kind"] == NOTICE_KIND_BINDING_CHANGE
    requests.complete(claimed, ok=True, task_id="task-rebound-deleted")

    service = _copy_service(tmp_path, sqlite, requests)
    assert service.store.remove_task("task-rebound-deleted")
    assert service.store.get_task("task-rebound-deleted") is None

    body = service._failure_notice_body(
        sqlite.get_run(run.id), sqlite.owed_failure_notice(run.id)
    )

    assert "vibe task" not in body, f"a deleted task cannot be re-pinned or shown: {body}"
    assert "vibe watch" not in body, f"a rebound task is not a watch: {body}"
    assert run.id in body, f"the run is the only thing left to inspect: {body}"
    # The news itself must survive: this is still "your session was replaced".
    assert "ses-fresh" in body and "ses-gone" in body, f"the rebind must still be reported: {body}"

    parser = build_parser()
    for argv in _every_vibe_command(body):
        try:
            parser.parse_args(argv)
        except SystemExit:  # pragma: no cover - the assertion is the point
            raise AssertionError(f"the copy prints {argv!r}, which the CLI cannot parse: {body}")
