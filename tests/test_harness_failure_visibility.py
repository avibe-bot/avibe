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
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import event, select

from core.scheduled_tasks import TaskExecutionRequest, TaskExecutionStore
from storage.background import (
    NOTICE_KIND_BINDING_CHANGE,
    NOTICE_KIND_FAILURE,
    NOTICE_PENDING,
    NOTICE_SENT,
    OWED_FAILURE_NOTICE_KEY,
    OWED_NOTICE_INDEX,
    RUN_INTERRUPTION_REASONS,
    SQLiteBackgroundTaskStore,
    TASK_RETIREMENT_SCHEDULE_CONSUMED,
    enqueue_run_in_connection,
    owed_notice_eligible,
    run_update_event_transaction,
)


@pytest.fixture(autouse=True)
def _prepare_behavior_state(tmp_path, sqlite_db_factory, request):
    # These cases exercise Harness behavior against the real current schema,
    # not the migration path that produces it.
    if not request.node.get_closest_marker("no_sqlite_template"):
        sqlite_db_factory(tmp_path / "state" / "vibe.sqlite")


def _store(tmp_path: Path) -> tuple[SQLiteBackgroundTaskStore, TaskExecutionStore]:
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    requests = TaskExecutionStore(tmp_path / "task_requests")
    requests._sqlite = sqlite
    return sqlite, requests


def _seed_query_history(sqlite: SQLiteBackgroundTaskStore, payloads: list[dict]) -> None:
    # Static query fixtures need the same durable rows, not one commit per row.
    with run_update_event_transaction(sqlite.engine) as connection:
        for payload in payloads:
            enqueue_run_in_connection(connection, sqlite._run_values(payload))


def test_query_history_batch_matches_individual_writes(tmp_path, sqlite_db_factory):
    from storage.models import agent_runs

    sqlite_db_factory(tmp_path / "reference" / "state" / "vibe.sqlite")
    reference, _ = _store(tmp_path / "reference")
    batched, _ = _store(tmp_path)
    payloads = [
        {
            "id": f"run-{index % 2}", "definition_id": "fixture",
            "request_type": "scheduled", "status": status,
            "created_at": _EPOCH, "completed_at": _EPOCH,
            "agent_backend": "codex" if index == 0 else None,
            "metadata": {"owed_failure_notice": {"state": "pending", "attempts": index}},
        }
        for index, status in enumerate(("failed", "succeeded", "running"))
    ]
    commits = []

    def committed(connection):
        commits.append(True)

    try:
        for store in (reference, batched):
            _task(store, "fixture")
        for payload in payloads:
            reference.enqueue_run(payload)
        event.listen(batched.engine, "commit", committed)
        try:
            _seed_query_history(batched, payloads)
        finally:
            event.remove(batched.engine, "commit", committed)
        assert commits == [True]
        with reference.engine.connect() as fresh, batched.engine.connect() as clone:
            statement = select(agent_runs).order_by(agent_runs.c.id)
            assert fresh.execute(statement).fetchall() == clone.execute(statement).fetchall()
    finally:
        reference.close()
        batched.close()


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


def test_turn_participant_settlement_does_not_clobber_an_already_succeeded_run(tmp_path: Path) -> None:
    """HFR-061 — Turn settlement must skip already-terminal rows.

    ``settle_agent_runs_for_turn_in_connection`` honors
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

    requests.settle_turn_participants(claimed, [request.id], ok=False, error="turn died")

    saved = sqlite.get_run(request.id)
    assert saved["status"] == "succeeded", "a settled success must not be rewritten to failed"


def _cancel_mid_write(sqlite_store, run_id: str, *, fire_on: str):
    """A ``cancel_run`` from a SECOND connection, fired between snapshot and UPDATE.

    Returns a listener to register on ``before_cursor_execute``. It fires once, on the
    first statement whose SQL contains ``fire_on`` — for a terminal writer that is its
    own UPDATE, so the cancel is committed after the writer has read the row and
    decided what to write, and before the write lands. That gap is not hypothetical:
    pysqlite starts no transaction for a SELECT, so a guarded writer's snapshot really
    is older than its own UPDATE, and ``cancel_run`` really is a separate transaction.

    The cancel goes through the production ``cancel_run`` on a second store over the
    same file, not through a hand-written UPDATE, so what the test races against is
    what the Stop button does.
    """

    fired: list[int] = []

    def _listener(conn, cursor, statement, parameters, context, executemany):
        if fired or fire_on not in statement:
            return
        fired.append(1)
        other = SQLiteBackgroundTaskStore(sqlite_store.db_path)
        try:
            other.cancel_run(run_id)
        finally:
            other.close()

    return _listener, fired


def test_a_cancel_landing_under_the_coalesced_completer_is_not_overwritten(
    tmp_path: Path,
) -> None:
    """Subordinate to HFR-060/061 — Stop must outrank a stale terminal snapshot.

    HFR-061 stopped the coalesced completer clobbering an already-SETTLED row by
    skipping terminal statuses, but it deliberately let ``canceled`` fall through so a
    cancel-requested row keeps being normalized. That fall-through is also a hole: the
    row's ``cancel_requested`` is read from a batch snapshot and never re-asserted, so
    a ``cancel_run`` landing between the snapshot and the UPDATE is overwritten by a
    ``failed`` the writer decided on before the user pressed Stop.

    Two consequences, and the second is the one that makes this a P6 scenario rather
    than a cosmetic status bug:

    * the run reads ``failed`` when the user cancelled it, so the Stop silently did
      nothing;
    * a ``failed`` transition STAMPS AN OWED FAILURE NOTICE, so the drain then tells
      the user their task is broken — because they cancelled it. A notice nobody can
      act on, in the lane this whole feature exists to make trustworthy.
    """

    sqlite, requests = _store(tmp_path)
    request = requests.enqueue_agent_run(
        session_key="slack::channel::C1",
        message="hi",
        agent_name=None,
    )
    claimed = requests.claim(request.id)
    assert claimed is not None
    assert sqlite.get_run(request.id)["status"] == "running"

    listener, fired = _cancel_mid_write(sqlite, request.id, fire_on="UPDATE agent_runs")
    event.listen(sqlite.engine, "before_cursor_execute", listener)
    try:
        requests.settle_turn_participants(claimed, [request.id], ok=False, error="turn died")
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", listener)

    assert fired, "the interleaved cancel never fired; the race was not exercised"

    saved = sqlite.get_run(request.id)
    assert saved["status"] == "canceled", (
        f"the user's Stop was overwritten by a stale terminal snapshot; row is {saved['status']!r}"
    )
    assert (saved.get("metadata") or {}).get(OWED_FAILURE_NOTICE_KEY) is None, (
        "a cancelled run owes no failure notice — the drain would tell the user their "
        "task broke because they stopped it"
    )


def test_a_cancel_landing_under_settle_run_terminal_is_not_overwritten(
    tmp_path: Path,
) -> None:
    """Subordinate to HFR-060/061 — the same hole in the zombie-settlement writer.

    ``settle_run_terminal`` scopes its UPDATE to ``queued|running``, which catches a
    cancel of a QUEUED row because ``cancel_run`` flips that one straight to
    ``canceled``. On a RUNNING row ``cancel_run`` sets only ``cancel_requested``, the
    status predicate still matches, and the write lands ``failed`` over the Stop with
    an owed notice attached. A running turn is precisely what a user presses Stop on,
    so this is the reachable half of the same defect.

    The refusal alone would not be a fix: a run left ``running`` with nothing to settle
    it is the zombie this writer exists to prevent. So the write is re-decided ONCE
    from the row as it then stands, and lands ``canceled``.
    """

    sqlite, requests = _store(tmp_path)
    request = requests.enqueue_agent_run(
        session_key="slack::channel::C2",
        message="hi",
        agent_name=None,
    )
    claimed = requests.claim(request.id)
    assert claimed is not None
    assert sqlite.get_run(request.id)["status"] == "running"

    listener, fired = _cancel_mid_write(sqlite, request.id, fire_on="UPDATE agent_runs")
    event.listen(sqlite.engine, "before_cursor_execute", listener)
    try:
        written = sqlite.settle_run_terminal(
            request.id, terminal_status="failed", error="no terminal result"
        )
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", listener)

    assert fired, "the interleaved cancel never fired; the race was not exercised"

    saved = sqlite.get_run(request.id)
    assert saved["status"] == "canceled", (
        f"the user's Stop was overwritten by a stale snapshot; row is {saved['status']!r}"
    )
    assert written == "canceled", (
        f"the writer must report what it actually wrote, not what it first decided; got {written!r}"
    )
    assert (saved.get("metadata") or {}).get(OWED_FAILURE_NOTICE_KEY) is None, (
        "a cancelled run owes no failure notice"
    )


def test_a_cancel_landing_under_record_run_output_is_not_overwritten(
    tmp_path: Path,
) -> None:
    """The backend output writer must re-decide a failed turn after Stop wins."""

    sqlite, requests = _store(tmp_path)
    request = requests.enqueue_agent_run(
        session_key="slack::channel::C6",
        message="hi",
        agent_name=None,
    )
    assert requests.claim(request.id) is not None

    listener, fired = _cancel_mid_write(sqlite, request.id, fire_on="UPDATE agent_runs")
    event.listen(sqlite.engine, "before_cursor_execute", listener)
    try:
        result = sqlite.record_run_output(
            request.id,
            output_id="failed-output",
            text="backend failed",
            terminal_status="failed",
            error="backend failed",
        )
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", listener)

    assert fired, "the interleaved cancel never fired; the race was not exercised"
    saved = sqlite.get_run(request.id)
    assert result["terminal_transition"] is True
    assert saved["status"] == "canceled"
    assert (saved.get("metadata") or {}).get(OWED_FAILURE_NOTICE_KEY) is None


def test_a_cancel_landing_before_deferred_owner_reservation_is_not_overwritten(
    tmp_path: Path,
) -> None:
    """A Stop that wins the writer reservation remains authoritative."""

    sqlite, requests = _store(tmp_path)
    request = requests.enqueue_agent_run(
        session_key="slack::channel::C7",
        message="hi",
        agent_name=None,
    )
    assert requests.claim(request.id) is not None
    assert sqlite.defer_run_terminal(
        request.id,
        terminal_status="failed",
        error="deferred failure",
        result_text="backend failed",
    )

    listener, fired = _cancel_mid_write(sqlite, request.id, fire_on="BEGIN IMMEDIATE")
    event.listen(sqlite.engine, "before_cursor_execute", listener)
    try:
        transitioned = sqlite.settle_deferred_run(request.id)
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", listener)

    assert fired, "the cancel never won the writer reservation"
    saved = sqlite.get_run(request.id)
    assert transitioned is True
    assert saved["status"] == "canceled"
    assert (saved.get("metadata") or {}).get(OWED_FAILURE_NOTICE_KEY) is None
    assert "deferred_terminal_status" not in (saved.get("result_payload") or {})


def test_the_cancel_cas_does_not_leave_an_uncancelled_run_unsettled(tmp_path: Path) -> None:
    """Subordinate to HFR-060/061 — the guard must not cost an ordinary settlement.

    The negative control for the two races above. A ``cancel_requested`` predicate is
    the kind of guard that quietly refuses writes nobody raced — the failure mode
    ``owed_notice_state_unchanged`` calls out — and here that would mean a failed turn
    never settling and never owing its notice, which is a D1 loss dressed as a safety
    fix. So: no interleaved cancel, and both writers must settle ``failed`` and stamp
    the notice exactly as before.
    """

    sqlite, requests = _store(tmp_path)

    coalesced = requests.enqueue_agent_run(
        session_key="slack::channel::C3", message="hi", agent_name=None
    )
    claimed = requests.claim(coalesced.id)
    assert claimed is not None
    requests.settle_turn_participants(claimed, [coalesced.id], ok=False, error="turn died")
    saved = sqlite.get_run(coalesced.id)
    assert saved["status"] == "failed"
    assert (saved.get("metadata") or {}).get(OWED_FAILURE_NOTICE_KEY) is not None

    settled = requests.enqueue_agent_run(
        session_key="slack::channel::C4", message="hi", agent_name=None
    )
    assert requests.claim(settled.id) is not None
    assert sqlite.settle_run_terminal(settled.id, terminal_status="failed", error="boom") == "failed"
    saved = sqlite.get_run(settled.id)
    assert saved["status"] == "failed"
    assert (saved.get("metadata") or {}).get(OWED_FAILURE_NOTICE_KEY) is not None

    # ...and a cancel the writer's OWN snapshot saw still normalizes to ``canceled``
    # through the fall-through branch, which the CAS must not have closed.
    requested = requests.enqueue_agent_run(
        session_key="slack::channel::C5", message="hi", agent_name=None
    )
    claimed = requests.claim(requested.id)
    assert claimed is not None
    sqlite.cancel_run(requested.id)
    requests.settle_turn_participants(
        claimed,
        [requested.id],
        ok=False,
        error="turn died",
    )
    saved = sqlite.get_run(requested.id)
    assert saved["status"] == "canceled"
    assert (saved.get("metadata") or {}).get(OWED_FAILURE_NOTICE_KEY) is None


def test_turn_output_batch_locks_before_re_electing_a_canceled_fallback_owner(
    tmp_path: Path,
) -> None:
    """One locked snapshot owns election and every participant's terminal write."""

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "watch-owner-a")
    _task(sqlite, "watch-owner-b")
    runs = [
        requests.enqueue_task_run(definition_id)
        for definition_id in ("watch-owner-a", "watch-owner-b")
    ]
    for run in runs:
        assert requests.claim(run.id) is not None
    assert sqlite.cancel_run(runs[0].id)

    statements: list[str] = []

    def _capture_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().upper())

    event.listen(sqlite.engine, "before_cursor_execute", _capture_statement)
    try:
        sqlite.record_turn_run_outputs(
            [run.id for run in runs],
            output_id="terminal",
            text="",
            provenance={
                "turn_id": "turn-shared",
                "turn_failure_notification": {
                    "failure_id": "turn:turn-shared",
                    "delivered": False,
                    "fallback_run_id": runs[0].id,
                },
            },
            terminal_status="failed",
            error="stream disconnected",
        )
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _capture_statement)

    lock_index = statements.index("BEGIN IMMEDIATE")
    participant_read_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("SELECT") and "FROM AGENT_RUNS" in statement
    )
    assert lock_index < participant_read_index
    assert sqlite.get_run(runs[0].id)["status"] == "canceled"
    assert sqlite.get_run(runs[1].id)["status"] == "failed"
    notice = sqlite.owed_failure_notice(runs[1].id)
    assert notice["turn_fallback_run_id"] == runs[1].id


def test_turn_output_batch_prefers_an_immediately_settled_fallback_owner(
    tmp_path: Path,
) -> None:
    """A running Activity cannot own notices that its terminal sibling owes now."""

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "watch-immediate-owner-a")
    _task(sqlite, "watch-immediate-owner-b")
    runs = [
        requests.enqueue_task_run(definition_id)
        for definition_id in ("watch-immediate-owner-a", "watch-immediate-owner-b")
    ]
    for run in runs:
        assert requests.claim(run.id) is not None
    deferred, immediate = sorted(runs, key=lambda run: run.id)

    sqlite.record_turn_run_outputs(
        [run.id for run in runs],
        output_id="terminal",
        text="",
        provenance={
            "turn_id": "turn-immediate-owner",
            "turn_failure_notification": {
                "failure_id": "turn:turn-immediate-owner",
                "delivered": False,
                "fallback_run_id": deferred.id,
            },
        },
        terminal_status="failed",
        error="stream disconnected",
        deferred_run_ids=[deferred.id],
    )

    assert sqlite.get_run(deferred.id)["status"] == "running"
    assert sqlite.get_run(immediate.id)["status"] == "failed"
    assert sqlite.owed_failure_notice(immediate.id)["turn_fallback_run_id"] == immediate.id
    deferred_metadata = sqlite.get_run(deferred.id)["result_payload"][
        "deferred_terminal_metadata"
    ]
    assert (
        deferred_metadata["turn_failure_notification"]["fallback_run_id"]
        == immediate.id
    )


def test_all_deferred_turn_runs_elect_and_propagate_the_first_stable_owner(
    tmp_path: Path,
) -> None:
    """A canceled deferred candidate cannot strand the Turn's only fallback."""

    sqlite, requests = _store(tmp_path)
    definitions = [f"watch-deferred-owner-{index}" for index in range(3)]
    for definition_id in definitions:
        _task(sqlite, definition_id)
    runs = [requests.enqueue_task_run(definition_id) for definition_id in definitions]
    for run in runs:
        assert requests.claim(run.id) is not None

    sqlite.record_turn_run_outputs(
        [run.id for run in runs],
        output_id="terminal",
        text="",
        provenance={
            "turn_id": "turn-all-deferred",
            "turn_failure_notification": {
                "failure_id": "turn:turn-all-deferred",
                "delivered": False,
                "fallback_run_id": runs[0].id,
            },
        },
        terminal_status="failed",
        error="stream disconnected",
        deferred_run_ids=[run.id for run in runs],
    )
    for run in runs:
        metadata = sqlite.get_run(run.id)["result_payload"][
            "deferred_terminal_metadata"
        ]
        assert "fallback_run_id" not in metadata["turn_failure_notification"]

    assert sqlite.cancel_run(runs[0].id)
    assert sqlite.settle_deferred_run(runs[0].id)
    assert sqlite.get_run(runs[0].id)["status"] == "canceled"

    assert sqlite.settle_deferred_run(runs[1].id)
    first_notice = sqlite.owed_failure_notice(runs[1].id)
    assert first_notice["turn_fallback_run_id"] == runs[1].id
    propagated = sqlite.get_run(runs[2].id)["result_payload"][
        "deferred_terminal_metadata"
    ]
    assert (
        propagated["turn_failure_notification"]["fallback_run_id"] == runs[1].id
    )

    assert sqlite.settle_deferred_run(runs[2].id)
    second_notice = sqlite.owed_failure_notice(runs[2].id)
    assert second_notice["turn_fallback_run_id"] == runs[1].id


def test_hfr_459_deferred_owner_election_reserves_writer_before_reads(
    tmp_path: Path,
) -> None:
    """Independent Activity releases serialize before choosing a Turn owner."""

    sqlite, requests = _store(tmp_path)
    definitions = ["watch-deferred-lock-a", "watch-deferred-lock-b"]
    for definition_id in definitions:
        _task(sqlite, definition_id)
    runs = [requests.enqueue_task_run(definition_id) for definition_id in definitions]
    for run in runs:
        assert requests.claim(run.id) is not None
    turn_id = "turn-deferred-owner-lock"
    sqlite.record_turn_run_outputs(
        [run.id for run in runs],
        output_id="terminal",
        text="",
        provenance={
            "turn_id": turn_id,
            "turn_failure_notification": {
                "failure_id": f"turn:{turn_id}",
                "delivered": False,
            },
        },
        terminal_status="failed",
        error="stream disconnected",
        deferred_run_ids=[run.id for run in runs],
    )

    statements: list[str] = []

    def _capture_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().upper())

    event.listen(sqlite.engine, "before_cursor_execute", _capture_statement)
    try:
        assert sqlite.settle_deferred_run(runs[0].id)
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _capture_statement)

    lock_index = statements.index("BEGIN IMMEDIATE")
    first_run_read = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("SELECT") and "FROM AGENT_RUNS" in statement
    )
    assert lock_index < first_run_read
    first_notice = sqlite.owed_failure_notice(runs[0].id)
    assert first_notice["turn_fallback_run_id"] == runs[0].id
    assert sqlite.settle_deferred_run(runs[1].id)
    second_notice = sqlite.owed_failure_notice(runs[1].id)
    assert second_notice["turn_fallback_run_id"] == runs[0].id


def test_hfr_447_fallback_owner_requires_writable_notice_metadata(
    tmp_path: Path,
) -> None:
    """Owner election and notice persistence share one eligibility predicate."""

    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    sqlite, requests = _store(tmp_path)
    definitions = ["watch-bad-owner", "watch-valid-owner"]
    for definition_id in definitions:
        _task(sqlite, definition_id)
    runs = sorted(
        (requests.enqueue_task_run(definition_id) for definition_id in definitions),
        key=lambda run: run.id,
    )
    for run in runs:
        assert requests.claim(run.id) is not None
    malformed, valid = runs
    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs)
            .where(agent_runs.c.id == malformed.id)
            .values(metadata_json="{not-json")
        )

    turn_id = "turn-writable-owner"
    sqlite.record_turn_run_outputs(
        [run.id for run in runs],
        output_id="terminal",
        text="",
        provenance={
            "turn_id": turn_id,
            "turn_failure_notification": {
                "failure_id": f"turn:{turn_id}",
                "delivered": False,
                "fallback_run_id": malformed.id,
            },
        },
        terminal_status="failed",
        error="stream disconnected",
    )

    assert sqlite.get_run(malformed.id)["status"] == "failed"
    assert sqlite.owed_failure_notice(malformed.id) is None
    assert sqlite.owed_failure_notice(valid.id)["turn_fallback_run_id"] == valid.id

    deferred_definitions = ["watch-bad-deferred-owner", "watch-valid-deferred-owner"]
    for definition_id in deferred_definitions:
        _task(sqlite, definition_id)
    deferred_runs = sorted(
        (
            requests.enqueue_task_run(definition_id)
            for definition_id in deferred_definitions
        ),
        key=lambda run: run.id,
    )
    for run in deferred_runs:
        assert requests.claim(run.id) is not None
    malformed_deferred, valid_deferred = deferred_runs
    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs)
            .where(agent_runs.c.id == malformed_deferred.id)
            .values(metadata_json="[]")
        )

    deferred_turn_id = "turn-writable-deferred-owner"
    sqlite.record_turn_run_outputs(
        [run.id for run in deferred_runs],
        output_id="terminal",
        text="",
        provenance={
            "turn_id": deferred_turn_id,
            "turn_failure_notification": {
                "failure_id": f"turn:{deferred_turn_id}",
                "delivered": False,
                "fallback_run_id": malformed_deferred.id,
            },
        },
        terminal_status="failed",
        error="stream disconnected",
        deferred_run_ids=[run.id for run in deferred_runs],
    )

    assert sqlite.settle_deferred_run(malformed_deferred.id)
    assert sqlite.owed_failure_notice(malformed_deferred.id) is None
    pending_metadata = sqlite.get_run(valid_deferred.id)["result_payload"][
        "deferred_terminal_metadata"
    ]
    assert "fallback_run_id" not in pending_metadata["turn_failure_notification"]
    assert sqlite.settle_deferred_run(valid_deferred.id)
    assert (
        sqlite.owed_failure_notice(valid_deferred.id)["turn_fallback_run_id"]
        == valid_deferred.id
    )


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
    history = []
    for index in range(backlog):
        history.append(
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

    _seed_query_history(sqlite, history)

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
    requests.settle_turn_participants(
        claimed_fourth,
        [fourth.id],
        ok=False,
        error="turn participant boom",
    )

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


def test_a_terminal_writer_never_rewrites_unparseable_metadata(tmp_path: Path, sqlite_schema_db_factory) -> None:
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
            sqlite_schema_db_factory(tmp_path / f"{writer}-{blobs.index(blob)}" / "state" / "vibe.sqlite")
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
                requests.settle_turn_participants(
                    claimed,
                    [run.id],
                    ok=False,
                    error="boom",
                )

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
    assert (
        sqlite.next_owed_failure_notice_at(now="2026-07-27T11:00:00+00:00")
        == "2026-07-27T12:00:00+00:00"
    )
    ready = sqlite.list_owed_failure_notices(now="2026-07-27T13:00:00+00:00")
    assert [item["id"] for item in ready] == [run.id]
    assert sqlite.next_owed_failure_notice_at(now="2026-07-27T13:00:00+00:00") is None


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


def test_a_session_less_definition_now_reaches_the_workspace_inbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The INVERSION of a pin this branch used to carry, kept so the reversal is legible.

    It read ``test_a_session_less_definition_dead_letters_rather_than_going_silent``,
    and it asserted ``_failure_notice_targets(run) == []`` for a definition with no
    session and no provenance, then hand-drove the attempt counter to a ``failed`` dead
    letter, on the argument that a truly session-less workspace surface was a declared
    Known-By-Design limitation under #1044's still-open plan contract.

    THE PLAN REFUTED THAT WITH TWO DATED CORRECTIONS (``docs/plans/harness-run-reliability.md``
    :3170-3174 and :3196-3222). The mechanism it names is a reserved
    workspace-notifications session created lazily on first need — not a widened
    session-less writer, which really is unreachable — and it puts that session in
    PR6's scope explicitly, "because without it rung (5) is as empty as the four above
    it". A dead letter is visible to somebody who goes looking; D1's subject is the
    runs nobody is watching.

    So the same setup now DELIVERS, and the half of the old pin that was never wrong is
    kept: the failure is still visible on the definition itself.
    """

    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID

    controller, _dispatcher, _touched = _live_turn_dispatcher()
    _no_background_web_push(monkeypatch)
    _migrated_state_db()

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-no-session")
    run = requests.enqueue_task_run("task-no-session")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="unresolvable target", task_id="task-no-session")

    notice = sqlite.owed_failure_notice(run.id)
    assert notice is not None and notice["state"] == "pending"

    service = _drain_service(tmp_path, controller, sqlite, requests)
    rungs = service._failure_notice_targets(sqlite.get_run(run.id))
    assert [target.to_key() for target, _ in rungs] == [
        f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}"
    ], f"the ladder's last rung must resolve where the four above it cannot: {rungs}"

    asyncio.run(service._drain_failure_notices())

    settled = sqlite.owed_failure_notice(run.id)
    assert settled["state"] == NOTICE_SENT, (
        f"a session-less definition must no longer dead-letter: {settled}"
    )
    assert settled["attempts"] == 1, "and it must not spend the backoff getting there"
    assert [row["session_id"] for row in _persisted_messages()] == [
        WORKSPACE_NOTICE_SESSION_ID
    ], "the notice has to be durable, not merely attempted"
    # The half of the old pin that was never wrong: the definition still reports the
    # failure, so the push and the badge agree rather than substituting for each other.
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


@pytest.mark.parametrize(
    ("outcome", "definition_enabled", "headline_key"),
    [
        ("event", True, "harness.notice.watchProcessingFailed"),
        ("waiter_failure", False, "harness.notice.watchFailureReportFailed"),
        ("circuit_repair", False, "harness.notice.watchCircuitRepairFailed"),
    ],
)
def test_watch_notice_copy_preserves_the_waiter_outcome(
    tmp_path: Path,
    outcome: str,
    definition_enabled: bool,
    headline_key: str,
) -> None:
    """A failed reporting Turn cannot turn a waiter failure into an event."""

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore
    from storage.background import WATCH_HOOK_OUTCOME_METADATA_KEY
    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    _watch(
        sqlite,
        "watch-copy",
        name="CI waiter",
        enabled=definition_enabled,
    )
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()
    run = requests.enqueue_hook_send(
        session_key="slack::channel::C1",
        prompt="report the Watch outcome",
        run_type="watch",
        definition_id="watch-copy",
        metadata={WATCH_HOOK_OUTCOME_METADATA_KEY: outcome},
    )
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(
        claimed,
        ok=False,
        error="Agent report failed",
        task_id="watch-copy",
    )
    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service.controller = SimpleNamespace(
        platform_settings_managers={},
        session_turn_gate=None,
    )
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)

    body = service._failure_notice_body(
        sqlite.get_run(run.id),
        sqlite.owed_failure_notice(run.id),
    )

    assert i18n_t(headline_key, "en").split("{")[0] in body
    if outcome == "waiter_failure":
        assert "detected an event" not in body


@pytest.mark.parametrize("definition_state", ["resumed", "deleted"])
def test_circuit_repair_failure_copy_does_not_claim_a_stale_pause_state(
    tmp_path: Path,
    definition_state: str,
) -> None:
    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore
    from storage.background import WATCH_HOOK_OUTCOME_METADATA_KEY
    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    _watch(sqlite, "watch-repair-state", name="Disk waiter", enabled=True)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()
    run = requests.enqueue_hook_send(
        session_key="slack::channel::C1",
        prompt="repair the Watch",
        run_type="watch",
        definition_id="watch-repair-state",
        metadata={WATCH_HOOK_OUTCOME_METADATA_KEY: "circuit_repair"},
    )
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(
        claimed,
        ok=False,
        error="repair Agent failed",
        task_id="watch-repair-state",
    )
    if definition_state == "deleted":
        assert sqlite.remove_task("watch-repair-state")

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service.controller = SimpleNamespace(
        platform_settings_managers={},
        session_turn_gate=None,
    )
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)

    body = service._failure_notice_body(
        sqlite.get_run(run.id),
        sqlite.owed_failure_notice(run.id),
    )

    generic = i18n_t("harness.notice.watchFollowUpFailed", "en").format(
        name="Disk waiter" if definition_state == "resumed" else "watch-repair-state"
    )
    assert generic in body
    assert "remains paused" not in body


def test_a_retired_one_shot_notice_says_finished_not_paused(tmp_path: Path) -> None:
    """The task-side twin of the retired-watch copy fix — FINISHED IS NOT PAUSED.

    A consumed ``at`` task is disabled and retired before its notice renders, so
    the generic disabled branch told the user the task
    was PAUSED and offered ``vibe task resume`` — while the canonical lifecycle
    projection (``definition_lifecycle_expression``) classifies the persisted retirement as
    FINISHED and the CLI reads the same combination as a failed one-shot. One
    surface's copy contradicted every other surface and named a lifecycle action
    that re-arms nothing.

    The distinction is read through the projection's own persisted marker, so
    the copy and the badge cannot disagree. The explicit re-run
    affordance is retained: ``vibe task run`` is real and is the honest next step
    for a failed one-shot.

    The control is a paused CRON task: a cron task cannot retire itself, so its
    disabled copy must keep naming resume.
    """

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore
    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    # The scheduler consumed this instant and persisted that transition.
    _task(
        sqlite,
        "task-once",
        name="one shot",
        schedule_type="at",
        cron=None,
        run_at="2026-07-20T00:00:00+00:00",
        enabled=False,
        retired_at=_EPOCH,
        retirement_reason=TASK_RETIREMENT_SCHEDULE_CONSUMED,
    )
    _task(sqlite, "task-paused", name="paused cron", enabled=False)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    # No ``config`` on the controller, so the real ``_t`` renders English out of
    # the shipped catalog: the defect is in the command strings a user reads.
    service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)

    body = service._failure_notice_body(
        {"id": "run-once", "task_id": "task-once", "error": "boom"},
        {"failure_id": "failure:run-once", "interrupt_reason": None},
    )
    assert i18n_t("harness.notice.taskFinished", "en") in body, (
        f"a finished one-shot's copy has to say it finished: {body!r}"
    )
    assert "vibe task resume" not in body, (
        "and must NOT offer resume for a definition the lifecycle projection reads "
        f"as FINISHED: {body!r}"
    )
    assert "vibe task run task-once" in body, (
        f"the explicit re-run affordance is retained for a failed one-shot: {body!r}"
    )

    control = service._failure_notice_body(
        {"id": "run-paused", "task_id": "task-paused", "error": "boom"},
        {"failure_id": "failure:run-paused", "interrupt_reason": None},
    )
    assert "vibe task resume task-paused" in control, (
        f"a genuinely paused cron task keeps the resume copy: {control!r}"
    )

    # A naive ``run_at`` is resolved in the task's own timezone for the next-fire
    # projection. Lifecycle itself reads only the persisted retirement fact.
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    naive_past_shanghai = (
        (
            datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
            - timedelta(hours=1)
        )
        .replace(tzinfo=None)
        .isoformat()
    )
    _task(
        sqlite,
        "task-tz",
        name="tz one shot",
        schedule_type="at",
        cron=None,
        run_at=naive_past_shanghai,
        timezone="Asia/Shanghai",
        enabled=False,
        retired_at=_EPOCH,
        retirement_reason=TASK_RETIREMENT_SCHEDULE_CONSUMED,
    )
    store.load()
    assert sqlite.definition_lifecycle_state("task-tz", definition_type="task") == "finished"
    from storage.background import compute_next_run_at as _next_run

    assert (
        _next_run(
            enabled=True,
            schedule_type="at",
            cron=None,
            run_at=naive_past_shanghai,
            timezone_name="Asia/Shanghai",
        )
        is None
    )

    tz_body = service._failure_notice_body(
        {"id": "run-tz", "task_id": "task-tz", "error": "boom"},
        {"failure_id": "failure:run-tz", "interrupt_reason": None},
    )
    assert i18n_t("harness.notice.taskFinished", "en") in tz_body, (
        f"the copy and badge must both read persisted retirement as FINISHED: {tz_body!r}"
    )
    assert "vibe task resume task-tz" not in tz_body, f"a finished task must not offer resume: {tz_body!r}"

    # THE IN-FLIGHT SPLIT: ``vibe task run`` accepts a disabled one-shot, and an
    # in-flight execution outranks the ended predicate in the canonical CASE — the
    # badge says RUNNING. Flattening every non-finished state to "paused" printed
    # resume copy beside that badge. With a rerun in flight the notice prints NO
    # lifecycle line: either claim would contradict the badge, and the re-run/show
    # affordance stands on its own.
    _task(
        sqlite,
        "task-running",
        name="rerun in flight",
        schedule_type="at",
        cron=None,
        run_at="2026-07-20T00:00:00+00:00",
        enabled=False,
    )
    sqlite.enqueue_run(
        {
            "id": "run-manual-rerun",
            "request_type": "scheduled",
            "status": "queued",
            "definition_id": "task-running",
            "created_at": "2026-07-29T00:00:00+00:00",
        }
    )
    store.load()
    assert (
        sqlite.definition_lifecycle_state("task-running", definition_type="task")
        == "running"
    ), "the premise: an in-flight execution outranks the ended predicate"

    running_body = service._failure_notice_body(
        {"id": "run-earlier", "task_id": "task-running", "error": "boom"},
        {"failure_id": "failure:run-earlier", "interrupt_reason": None},
    )
    assert "vibe task resume task-running" not in running_body, (
        f"the copy must not say PAUSED while the badge says RUNNING: {running_body!r}"
    )
    assert i18n_t("harness.notice.taskFinished", "en") not in running_body, (
        f"nor FINISHED while an execution is in flight: {running_body!r}"
    )
    assert "vibe task run task-running" in running_body, (
        f"the re-run affordance stands on its own: {running_body!r}"
    )


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


def test_suppressed_duplicate_short_circuit_is_local_history_not_a_receipt() -> None:
    """A background Session's persisted row proves no outward delivery."""

    from unittest.mock import patch

    import core.message_dispatcher as dispatcher_module
    from core.delivery_evidence import DeliveryEvidence
    from core.message_output import MessageOutput
    from modules.im import MessageContext

    from tests.test_message_dispatcher_scheduled import _StubController

    controller = _StubController()
    dispatcher = dispatcher_module.ConsolidatedMessageDispatcher(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        platform_specific={
            "task_trigger_kind": "scheduled",
            "task_execution_id": "run-background",
            "suppress_delivery": True,
        },
    )
    evidence = DeliveryEvidence()

    with patch.object(dispatcher_module, "agent_message_exists", return_value=True):
        returned = asyncio.run(
            dispatcher.emit_agent_message(
                context,
                "notify",
                "your background task failed",
                output=MessageOutput(
                    completes_turn=False,
                    completes_run=False,
                    idempotency_key="backend-failure:failure:run-background",
                ),
                delivery=evidence,
            )
        )

    assert returned and "backend-failure:failure:run-background" in returned
    assert controller.im_client.sent == []
    assert evidence.ack_evidence is None


def test_suppressed_history_cannot_shadow_a_later_visible_receipt() -> None:
    """HFR-441 — local history cannot satisfy the stable outward identity."""

    from unittest.mock import patch

    import core.message_dispatcher as dispatcher_module
    from core.delivery_evidence import DeliveryEvidence
    from core.message_output import MessageOutput
    from modules.im import MessageContext

    from tests.test_message_dispatcher_scheduled import _StubController

    controller = _StubController()
    dispatcher = dispatcher_module.ConsolidatedMessageDispatcher(controller)
    persisted: dict[str, dict[str, Any]] = {}
    persisted_versions: list[dict[str, Any]] = []

    def _lookup(_context, native_message_id):
        return persisted.get(str(native_message_id))

    def _persist(
        _context,
        canonical_type,
        text,
        *,
        metadata=None,
        native_message_id=None,
        **_kwargs,
    ):
        row = {
            "id": f"row-{len(persisted) + 1}",
            "type": canonical_type,
            "text": text,
            "metadata": dict(metadata or {}),
            "native_message_id": native_message_id,
        }
        persisted[str(native_message_id)] = row
        persisted_versions.append(row)
        return row

    output = MessageOutput(
        completes_turn=False,
        completes_run=False,
        idempotency_key="backend-failure:failure:shared",
    )
    suppressed = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        platform_specific={"suppress_delivery": True},
    )
    visible = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
    )
    evidence = DeliveryEvidence()

    with (
        patch.object(dispatcher_module, "agent_message_exists", side_effect=_lookup),
        patch.object(dispatcher_module, "persist_agent_message", side_effect=_persist),
    ):
        asyncio.run(
            dispatcher.emit_agent_message(
                suppressed,
                "notify",
                "local background failure",
                output=output,
            )
        )
        asyncio.run(
            dispatcher.emit_agent_message(
                visible,
                "notify",
                "visible durable fallback",
                output=output,
                delivery=evidence,
            )
        )

    assert len(controller.im_client.sent) == 1
    assert evidence.delivered is True
    assert len(persisted) == 1, "local history preserves the stable output identity"
    assert persisted_versions[0]["metadata"]["delivery_suppressed"] is True
    assert "delivery_suppressed" not in persisted_versions[1]["metadata"]


def test_visible_send_promotes_the_stable_suppressed_history_row(tmp_path: Path) -> None:
    """HFR-445 — promotion keeps identity and adopts the visible target Session."""

    from storage import messages_service
    from storage.models import agent_sessions

    sqlite, _requests = _store(tmp_path)
    _callback_session(sqlite)
    now = "2026-07-27T00:00:00+00:00"
    with sqlite.engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id="ses-suppressed-source",
                scope_id=None,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="suppressed-source",
                native_session_id="native-suppressed-source",
                status="active",
                visibility="background",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        local = messages_service.append(
            conn,
            scope_id=None,
            session_id="ses-suppressed-source",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="notify",
            text="local history",
            metadata={"delivery_suppressed": True, "run_id": "run-promote"},
            native_message_id="agent-output:codex:run-promote:failure",
        )
        promoted = messages_service.promote_suppressed_native_message(
            conn,
            platform="avibe",
            scope_id=None,
            session_id="ses-callback-target",
            native_message_id="agent-output:codex:run-promote:failure",
            message_type="notify",
            text="visible fallback",
            metadata={"run_id": "run-promote"},
        )

    assert promoted is not None
    assert promoted["id"] == local["id"]
    assert promoted["session_id"] == "ses-callback-target"
    assert promoted["text"] == "visible fallback"
    assert promoted["metadata"] == {"run_id": "run-promote"}


def test_hfr_446_promoted_receipt_uses_visible_delivery_order(tmp_path: Path) -> None:
    """Promotion orders the stable row by its visible send, not hidden creation."""

    from storage import messages_service
    from storage.models import agent_sessions

    sqlite, _requests = _store(tmp_path)
    _callback_session(sqlite)
    now = "2026-07-27T00:00:00+00:00"
    instants = iter(
        [
            "2026-07-27T00:00:01.000000Z",
            "2026-07-27T00:00:02.000000Z",
            "2026-07-27T00:00:03.000000Z",
        ]
    )
    with sqlite.engine.begin() as conn, pytest.MonkeyPatch.context() as patch:
        patch.setattr(messages_service, "_utc_now_iso", lambda: next(instants))
        conn.execute(
            agent_sessions.insert().values(
                id="ses-order-source",
                scope_id=None,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="order-source",
                native_session_id="native-order-source",
                status="active",
                visibility="background",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
        hidden = messages_service.append(
            conn,
            scope_id=None,
            session_id="ses-order-source",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="notify",
            text="hidden failure",
            metadata={"delivery_suppressed": True, "run_id": "run-order"},
            native_message_id="agent-output:codex:run-order:failure",
        )
        newer = messages_service.append(
            conn,
            scope_id=None,
            session_id="ses-callback-target",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="notify",
            text="already visible",
        )
        promoted = messages_service.promote_suppressed_native_message(
            conn,
            platform="avibe",
            scope_id=None,
            session_id="ses-callback-target",
            native_message_id="agent-output:codex:run-order:failure",
            message_type="notify",
            text="visible failure",
            metadata={"run_id": "run-order"},
        )
        transcript = messages_service.list_session_messages(
            conn,
            session_id="ses-callback-target",
            types={"notify"},
            tail=True,
        )["messages"]

    assert promoted is not None
    assert promoted["id"] == hidden["id"]
    assert promoted["delivered_at"] == "2026-07-27T00:00:03.000000Z"
    assert [message["id"] for message in transcript] == [newer["id"], hidden["id"]]


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
        {"state": "pending", "attempts": 1, "next_attempt_at": " 9999-01-01T00:00:00+00:00"},
        # ...and every JSON type it cannot legally hold. These are not curiosities: the
        # eligibility predicate ADMITS all of them (SQLite sorts every number before
        # every ISO instant), so an expectation SQL cannot match is a row the drain
        # reads and can never write — a batch slot held for good. ``0`` and ``false``
        # are the ones an ``or ""`` read got wrong, and the reals are the ones whose
        # SQLite text cannot be reproduced in Python at all.
        {"state": "pending", "attempts": 1, "next_attempt_at": 5},
        {"state": "pending", "attempts": 1, "next_attempt_at": 0},
        {"state": "pending", "attempts": 1, "next_attempt_at": True},
        {"state": "pending", "attempts": 1, "next_attempt_at": False},
        {"state": "pending", "attempts": 1, "next_attempt_at": 3.5},
        {"state": "pending", "attempts": 1, "next_attempt_at": 1e25},
        {"state": "pending", "attempts": 1, "next_attempt_at": -1.5063173670565552e-212},
        {"state": "pending", "attempts": 1, "next_attempt_at": {"a": 1}},
        {"state": "pending", "attempts": 1, "next_attempt_at": [1]},
        # ``attempts`` in the shapes where SQLite's CAST diverges from ``int()`` — the
        # round-19 finding. CAST parses a numeric PREFIX and saturates at the i64
        # bounds, so each of these is a row the listing admits whose claim could never
        # match under an ``int()``-based expectation: eligible, unchanged, and holding
        # a batch slot on every pass.
        {"state": "pending", "attempts": "3x"},
        {"state": "pending", "attempts": "1e100"},
        {"state": "pending", "attempts": " 5 "},
        {"state": "pending", "attempts": "  +7x"},
        {"state": "pending", "attempts": "-3q"},
        {"state": "pending", "attempts": "3.9"},
        {"state": "pending", "attempts": 9223372036854775808},
        {"state": "pending", "attempts": 1e100},
        {"state": "pending", "attempts": True},
        {"state": "pending", "attempts": [2]},
        {"state": "pending", "attempts": {"n": 2}},
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
        # The wrong-attempts neighbour steps DOWN at the saturation bound: +1 there
        # cannot bind as an SQLite integer parameter at all (OverflowError), and a
        # CAST-saturated expectation is exactly the shape that sits on the bound.
        wrong_attempts = expect[1] + 1 if expect[1] < 2**63 - 1 else expect[1] - 1
        for wrong in (
            (expect[0], wrong_attempts, expect[2]),
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

    # The non-text branch asserts a TYPE, so it needs its own negative control: an
    # expectation read from a numeric instant must not match a row that now holds a
    # real one. That transition is exactly what the third element exists to catch — a
    # concurrent DEFERRAL writes an ISO string — so a guard that ignored it would let a
    # stale claim erase the deferral it never saw.
    numeric = {"state": "pending", "attempts": 1, "next_attempt_at": 5}
    from_numeric = notice_write_expectation(numeric)
    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs)
            .where(agent_runs.c.id == run.id)
            .values(
                metadata_json=_json.dumps(
                    {
                        OWED_FAILURE_NOTICE_KEY: {
                            "state": "pending",
                            "attempts": 1,
                            "next_attempt_at": "2026-07-29T00:00:30+00:00",
                        }
                    }
                )
            )
        )
    with sqlite.engine.connect() as conn:
        assert (
            conn.execute(
                sa_update(agent_runs)
                .where(agent_runs.c.id == run.id)
                .where(*owed_notice_state_unchanged(from_numeric))
                .values(updated_at="2026-07-01T00:00:00+00:00")
            ).rowcount
            == 0
        ), (
            f"{from_numeric!r} matched a row whose retry instant became TEXT; a "
            "concurrent deferral would be erased by a claim that never saw it"
        )


def test_the_eligibility_predicate_reads_the_same_in_python_and_in_sql(tmp_path: Path) -> None:
    """Subordinate to HFR-076 — the eligibility twins may not drift over ANY JSON shape.

    ``owed_notice_eligible`` is the Python twin of the two expressions
    ``list_owed_failure_notices`` seeks on, and the seek applies ``LIMIT`` BEFORE the
    Python re-check runs. So a shape SQL admits and Python rejects is not a harmless
    difference of opinion: the row consumes one of the ten slots, is dropped with no
    state transition, and is selected again on the next tick forever. Ten of them
    starve every valid notice behind them — silently, in a drain that looks busy and
    healthy. Same defect class as HFR-085 and as the malformed ``attempts`` that
    ``notice_write_expectation`` degrades instead of wedging.

    ``next_attempt_at`` is stamped only by this module's own writers, always as an ISO
    string, so the shapes below need a foreign writer or a hand-edited row to arrive.
    That is exactly the argument that made the earlier drift invisible for two rounds,
    so the predicate is pinned over every JSON type instead: null, text, integer, real,
    true/false, object and array, and the paddings that make a string sort against
    ``now`` differently before and after a ``strip()``.

    The comparison SQLite makes is a STORAGE CLASS comparison, and this test exists
    because that is not the comparison Python makes:  every INTEGER and REAL sorts
    before every TEXT in SQLite, so a numeric ``next_attempt_at`` is ``<= now`` for
    any instant, while ``str(9999) <= "2026-..."`` is false. And SQLite does not strip,
    so ``" 9999-01-01..."`` sorts before ``now`` on the leading space while a stripped
    copy sorts after it.
    """

    from sqlalchemy import and_, literal_column
    from sqlalchemy import select as sa_select
    from sqlalchemy import update as sa_update

    from storage.background import (
        NOTICE_PENDING,
        OWED_NOTICE_NEXT_ATTEMPT_SQL,
        OWED_NOTICE_STATE_SQL,
    )
    from storage.models import agent_runs

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-eligible-twin")
    run = requests.enqueue_task_run("task-eligible-twin")
    claimed = requests.claim(run.id)
    requests.complete(claimed, ok=False, error="boom", task_id="task-eligible-twin")

    now = "2026-07-29T00:00:00+00:00"
    # The two expressions VERBATIM, combined exactly as the listing query combines
    # them, so this compares against the predicate that actually applies the limit.
    in_sql = and_(
        literal_column(OWED_NOTICE_STATE_SQL) == NOTICE_PENDING,
        literal_column(OWED_NOTICE_NEXT_ATTEMPT_SQL) <= now,
    )

    shapes: list[Any] = [
        # ``next_attempt_at`` as TEXT, which is the only shape a stamper writes.
        {"state": "pending"},
        {"state": "pending", "next_attempt_at": None},
        {"state": "pending", "next_attempt_at": ""},
        {"state": "pending", "next_attempt_at": "2026-07-28T23:59:59+00:00"},
        {"state": "pending", "next_attempt_at": "2099-01-01T00:00:00+00:00"},
        {"state": "pending", "next_attempt_at": " 2020-01-01T00:00:00+00:00"},
        {"state": "pending", "next_attempt_at": " 9999-01-01T00:00:00+00:00"},
        {"state": "pending", "next_attempt_at": "2026-07-28T23:59:59+00:00 "},
        # ...and a numeric-looking STRING, which is still text and still compares as
        # text on both sides — the negative control for the numeric rows below.
        {"state": "pending", "next_attempt_at": "9999"},
        # Every other JSON type.
        {"state": "pending", "next_attempt_at": 0},
        {"state": "pending", "next_attempt_at": 1},
        {"state": "pending", "next_attempt_at": 3},
        {"state": "pending", "next_attempt_at": 9999},
        {"state": "pending", "next_attempt_at": -5},
        {"state": "pending", "next_attempt_at": 1.5},
        {"state": "pending", "next_attempt_at": 3.5},
        {"state": "pending", "next_attempt_at": 1e25},
        {"state": "pending", "next_attempt_at": True},
        {"state": "pending", "next_attempt_at": False},
        {"state": "pending", "next_attempt_at": {"a": 1}},
        {"state": "pending", "next_attempt_at": [1]},
        # The state half of the predicate, including the shapes that make the notice
        # unreadable rather than merely ineligible.
        {"state": "sent", "next_attempt_at": None},
        {"state": "skipped"},
        {"state": "failed"},
        {"state": "PENDING"},
        {"state": None},
        {},
        None,
        "pending",
        5,
    ]

    import json as _json

    answers: list[bool] = []
    drift: list[str] = []
    for stored in shapes:
        with sqlite.engine.begin() as conn:
            conn.execute(
                sa_update(agent_runs)
                .where(agent_runs.c.id == run.id)
                .values(metadata_json=_json.dumps({OWED_FAILURE_NOTICE_KEY: stored}))
            )
        with sqlite.engine.connect() as conn:
            listed = bool(
                conn.execute(
                    sa_select(in_sql).select_from(agent_runs).where(agent_runs.c.id == run.id)
                ).scalar()
            )
        answers.append(listed)
        if owed_notice_eligible(stored, now) is not listed:
            drift.append(f"{stored!r}: SQL={listed} python={not listed}")
    # Reported as one list rather than one failure: the shapes that drift are the
    # inventory of ways a row can hold a batch slot forever, and seeing them together
    # is what makes the normalization rule obvious.
    assert not drift, (
        "owed_notice_eligible disagreed with the SQL the LIMIT is applied to for "
        + str(len(drift))
        + " shape(s); each is a row SQL admits and Python drops, which holds one of "
        "the ten batch slots forever with no state transition:\n  "
        + "\n  ".join(drift)
    )

    # A predicate that answered the same thing to everything would satisfy the loop
    # above without pinning anything.
    assert any(answers) and not all(answers), "the shapes must exercise both answers"


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
    """Interactive callers keep auth recovery; Harness delivery stays durable.

    Auth recovery is where a real interactive 401 gets its reset-OAuth button. A
    Harness run instead settles into its owed-notice row and lets that drain own the
    single visible delivery, including recurring-streak suppression.
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

    # The interactive live path really does consult it.
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

    harness_context = MessageContext(
        user_id="u1",
        channel_id="C123",
        platform="slack",
        platform_specific={
            "task_execution_id": "run-harness-auth",
            "task_trigger_kind": "scheduled",
        },
    )
    handled = asyncio.run(
        emit_backend_failure(controller, harness_context, "codex", "401 unauthorized")
    )
    assert handled is False
    assert consulted == ["codex"], "Harness failures must not bypass the durable notice policy"


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
    # The tick also observes cancellations requested against work THIS process runs, and
    # that pass reads the in-flight registry first. Empty here: this service never claims
    # a request, so the pass is a no-op — but the attribute has to exist, because an
    # AttributeError inside the tick aborts every LATER pass in it.
    service._inflight_executions = {}
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
    visibility: str = "foreground",
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
                visibility=visibility,
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
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID

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
        # The mandatory workspace rung, appended after every person/context target
        # (round-14 gate). It is present on every ladder and is NOT what delivers here:
        # rung (2) resolves a live scope and acks first, which the single persisted row
        # below is the proof of.
        f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}",
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
    * pass 2 — the same notice, still owed, after the Session row exists again as an
      ARCHIVED row. It now delivers and acks on a receipt, with the attempt count
      carried forward from the pass that walked on — but the receipt is in the
      WORKSPACE-NOTIFICATIONS session, not in the archived row.

    THE MANDATORY WORKSPACE RUNG IS SUPPRESSED IN PASS 1, and that is deliberate
    isolation rather than a workaround. Since the round-14 gate the ladder ends with a
    workspace rung on EVERY attempt, so a pass 1 with it enabled would DELIVER — which
    is correct product behaviour and would leave nothing to say about the synthetic send
    id, the subject of this test. ``_workspace_notice_session_id`` returning ``None``
    (the unwritable-workbench-DB path, pinned in its own right by
    ``test_an_unwritable_workspace_inbox_still_dead_letters_visibly``) makes the walk
    skip that rung, so pass 1 sees exactly the two stale project candidates and nothing
    else. Pass 2 restores it, and what it then pins is the archived REROUTE.

    DOCTRINE REVERSAL IN PASS 2 (round-13 P1, review thread 3676292667). This test
    was written in round 9 to assert the opposite: that rung (5) delivers INTO the
    archived row, on the premise that "an archived row is a valid delivery surface"
    because ``_session_row`` has no status filter where rung (2)'s
    ``resolve_session_id_target`` refuses an archived session outright. The mechanism
    is real and still is; the premise was wrong. An archived row is WRITABLE but not
    VISIBLE: the write happens, the receipt exists, so the workbench class's
    receipt-only ack source marks the notice ``sent`` and it is never retried — while
    ``list_inbox_sessions`` / ``get_inbox_session`` exclude archived sessions, so there
    is no inbox card, no ``inbox.session.updated`` and no Web Push. That is the ack
    source's blind spot, and it is blind precisely because the write is genuine, which
    is why the check had to move UPSTREAM into the address (``_rung_five_session_id``).

    Not healed, unlike the reserved workspace session's own archive
    (``test_an_archived_workspace_notice_session_heals_instead_of_swallowing_the_notice``):
    the user archived THIS session on purpose. So the notice is rerouted instead, and
    what pass 2 now pins is that the surviving delta rung (5) buys over rung (2) is a
    DELIVERY at all, not a delivery into that particular row.

    Pass 1 also pins the per-rung evidence: one shared ``DeliveryEvidence`` latches
    ``delivered`` true forever once any rung sets an id, so the first rejected rung
    would both stop the walk and hand the eventual ack/dead letter another rung's
    ``ack_evidence``.
    """

    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.background import NOTICE_PENDING
    from storage.db import get_cached_sqlite_engine
    from storage.messages_service import get_inbox_session

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
        f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}",
    ], f"a deleted session loses rung (2) and still BUILDS rung (5): {rungs}"

    # --- pass 1: nothing durable, so nothing acknowledges -------------------
    #
    # With the mandatory workspace rung SKIPPED (see the docstring), so the only rungs
    # the walk can act on are the two stale project candidates this pass is about.
    workspace_enabled = service._workspace_notice_session_id
    service._workspace_notice_session_id = lambda: None
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

    # --- pass 2: the same owed notice, REROUTED off the archived row ----------
    service._workspace_notice_session_id = workspace_enabled
    archived_scope_id = _workbench_session("sesGone", project="proj-live", status="archived")
    # Let the backoff elapse without sleeping (the same rewind the retry tests use).
    sqlite.update_owed_failure_notice(run.id, next_attempt_at=None)
    assert [row["id"] for row in sqlite.list_owed_failure_notices(limit=10)] == [run.id], (
        "the notice has to still be OWED for a later rung to be able to deliver it"
    )

    rungs = service._failure_notice_targets(sqlite.get_run(run.id))
    assert [target.to_key() for target, _ in rungs] == [
        "avibe::project::proj-gone",
        f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}",
    ], (
        "rung (5) must address a surface the user can SEE. The archived row is writable "
        f"and invisible, so the candidate is the workspace inbox instead: {rungs}"
    )
    assert [target.to_key() for target, _ in rungs].count(
        f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}"
    ) == 1, (
        "and it collapses with the MANDATORY appended workspace rung rather than "
        f"appearing twice — the reroute returns the same reserved id: {rungs}"
    )

    asyncio.run(service._drain_failure_notices())

    rows = _persisted_messages()
    assert [row["session_id"] for row in rows] == [WORKSPACE_NOTICE_SESSION_ID], (
        "only the rung that persisted anything counts as delivered — and the row it "
        f"persisted has to be one the inbox displays: {rows}"
    )
    assert rows[0]["scope_id"] != archived_scope_id, (
        f"nothing may be written into the archived session's scope: {rows}"
    )

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == NOTICE_SENT
    assert notice["ack_evidence"] == ACK_EVIDENCE_RECEIPT, (
        "the ack must carry the WINNING rung's evidence, not the rejected rung's"
    )
    assert notice["attempts"] == 2, (
        "the attempt the refused pass consumed is carried forward, not reset"
    )

    # THE REVERSAL, asserted on the SURFACE rather than on the notice state, because the
    # notice state is exactly where the old doctrine and the new one look identical: an
    # ack into the archived row reads ``sent`` just the same.
    with get_cached_sqlite_engine().begin() as conn:
        assert get_inbox_session(conn, "sesGone") is None, (
            "the premise of the reversal: an archived session shows no inbox card, so a "
            "notice acked into it was never seen by anybody"
        )
        assert get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is not None, (
            "and the session it was rerouted to does show one"
        )


def test_a_background_session_notice_is_rerouted_to_the_workspace_inbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A hidden bound session cannot acknowledge a user-visible failure notice."""

    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.background import NOTICE_SENT
    from storage.db import get_cached_sqlite_engine
    from storage.messages_service import get_inbox_session

    _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    _workbench_session(
        "sesBackgroundNotice",
        project="proj-background-notice",
        visibility="background",
    )

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-background-notice", session_id="sesBackgroundNotice")
    run = requests.enqueue_task_run("task-background-notice")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(
        claimed,
        ok=False,
        error="backend exploded",
        task_id="task-background-notice",
    )

    service = _drain_service(tmp_path, controller, sqlite, requests)
    rungs = service._failure_notice_targets(sqlite.get_run(run.id))
    assert [(target.to_key(), session_id) for target, session_id in rungs] == [
        (
            f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}",
            WORKSPACE_NOTICE_SESSION_ID,
        )
    ], f"a background session must be omitted from both session-derived rungs: {rungs}"

    asyncio.run(service._drain_failure_notices())

    rows = _persisted_messages()
    assert [row["session_id"] for row in rows] == [WORKSPACE_NOTICE_SESSION_ID], rows
    assert sqlite.owed_failure_notice(run.id)["state"] == NOTICE_SENT
    with get_cached_sqlite_engine().begin() as conn:
        assert get_inbox_session(conn, "sesBackgroundNotice") is None
        assert get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is not None


def _workspace_notice_session_rows() -> list[dict]:
    """Every ``agent_sessions`` row holding the reserved workspace-notice identity."""

    from sqlalchemy import select as sa_select

    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.db import get_cached_sqlite_engine
    from storage.models import agent_sessions

    with get_cached_sqlite_engine().begin() as conn:
        rows = conn.execute(
            sa_select(agent_sessions).where(
                agent_sessions.c.id == WORKSPACE_NOTICE_SESSION_ID
            )
        ).mappings()
        return [dict(row) for row in rows]


def test_a_caller_less_cli_definition_still_delivers_its_failure_notice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """D5 rung (5), owed by name at plan ``docs/plans/harness-run-reliability.md:3216``.

    Subordinate coverage under §D5's rung-(5) requirement (plan :3193, :3215-3222);
    no new scenario id this round — the §10.7 HFR-280…319 assignment is offered to
    the maintainer as a follow-up.

    THE EMPTINESS IS STRUCTURAL, not mocked. ``vibe task add`` typed at a terminal
    reaches ``_definition_creation_metadata_from_caller(None)``, whose first line is
    ``if caller_context is None: return {}`` — so there is no ``created_by``, hence no
    rung (3) and no rung (4); no ``deliver_key``, hence no rung (1); and no session
    binding, hence neither rung (2) nor the session-derived rung (5) candidate. The
    test builds the definition's metadata by CALLING that helper rather than by
    asserting a hand-written ``{}``, so the premise cannot drift away from the
    product path it stands for.

    Before this change every rung was empty and the notice could only dead-letter
    (``_failure_notice_targets(run) == []``): a failure notice with nowhere to go is a
    failure notice that is never written, which is D1 unmet for exactly the runs
    nobody is watching. Rung (5) is now unconditional — a reserved
    workspace-notifications session, resolved-or-created lazily, addressed as
    ``avibe::project::<reserved session id>``.

    Asserted at the CONSUMING end, because "the ladder was walked" is not delivery:
    the notice has to exist as one durable ``messages`` row in that session, be
    inbox-readable, be push-notifiable, and ack on the persisted receipt (the
    workbench target class may not ack on ``AvibeBot``'s synthetic send id — see
    ``test_an_avibe_rung_does_not_ack_on_a_synthetic_send_id``).
    """

    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from vibe.cli import _definition_creation_metadata_from_caller

    pushed = _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    # The real workbench schema, and deliberately NO session row and NO project
    # scope of any kind: the workspace session must be created by the notice path.
    _migrated_state_db()
    assert _workspace_notice_session_rows() == [], (
        "the reserved session must not pre-exist; the drain is what creates it"
    )

    sqlite, requests = _store(tmp_path)
    caller_metadata = _definition_creation_metadata_from_caller(None)
    assert caller_metadata == {}, (
        "the premise: a plain CLI invocation records no caller provenance at all"
    )
    _task(sqlite, "task-cli-only", name="nightly report", metadata=caller_metadata)
    run = requests.enqueue_task_run("task-cli-only")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-cli-only")
    assert sqlite.owed_failure_notice(run.id)["state"] == "pending"

    service = _drain_service(tmp_path, controller, sqlite, requests)
    # The REAL translator: the claim is about copy a user reads back out of the inbox,
    # so a ``_t`` that echoes keys would assert the wrong thing on both the notice body
    # and the workspace session's name.
    from core.scheduled_tasks import ScheduledTaskService

    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)

    rungs = service._failure_notice_targets(sqlite.get_run(run.id))
    assert [target.to_key() for target, _ in rungs] == [
        f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}"
    ], f"rung (5) must resolve for a definition with no caller and no session: {rungs}"
    assert [session_id for _target, session_id in rungs] == [WORKSPACE_NOTICE_SESSION_ID], (
        "the rung has to carry the session id, or ``persist_agent_message`` has no row "
        "to resolve the message against"
    )

    asyncio.run(service._drain_failure_notices())

    rows = _persisted_messages()
    assert [(row["platform"], row["type"], row["session_id"]) for row in rows] == [
        ("avibe", "notify", WORKSPACE_NOTICE_SESSION_ID)
    ], f"the notice must land as exactly one durable row in the workspace session: {rows}"
    assert rows[0]["content_text"], "an empty notice is not a notice"
    assert "nightly report" in rows[0]["content_text"], (
        f"and it must name what failed: {rows[0]['content_text']!r}"
    )

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == NOTICE_SENT
    assert notice["ack_evidence"] == ACK_EVIDENCE_RECEIPT, (
        "the workbench class acks on the persisted receipt, never on a send id"
    )
    assert notice["attempts"] == 1, "one rung, one attempt — no retry, no dead letter"

    # READABLE, not merely stored: the inbox feed is the surface D5 promises, and it
    # filters on ``visibility = 'foreground'`` — so a session hidden by that flag
    # would store the row and show the user nothing.
    from storage.db import get_cached_sqlite_engine
    from storage.messages_service import get_inbox_session

    with get_cached_sqlite_engine().begin() as conn:
        card = get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID)
    assert card is not None, "the workspace notice must be an inbox-readable card"
    assert "nightly report" in str(card["preview_text"]), (
        f"the card must preview the notice a user has to read: {card}"
    )
    # A CLEAR name rather than a bare reserved id: the row is a ``system`` surface
    # hidden from ordinary session lists, but its inbox card still leads with the
    # title, so an unnamed card would read as a bug.
    assert str(card["title"] or "").strip(), f"the workspace session must be named: {card}"

    # And push-notifiable, so it reaches a user who is not looking at the tab.
    assert [payload["session_id"] for payload in pushed] == [WORKSPACE_NOTICE_SESSION_ID], (
        f"the workspace notice must be pushed, not only stored: {pushed}"
    )


def test_workspace_notification_session_is_created_once_and_reused(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Repeated workspace notices reuse one retained Session history owner."""

    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID

    _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    _migrated_state_db()

    sqlite, requests = _store(tmp_path)
    service = _drain_service(tmp_path, controller, sqlite, requests)

    def _fail(definition_id: str) -> str:
        _task(sqlite, definition_id, name=definition_id)
        run = requests.enqueue_task_run(definition_id)
        claimed = requests.claim(run.id)
        assert claimed is not None
        requests.complete(claimed, ok=False, error="boom", task_id=definition_id)
        return run.id

    first = _fail("task-cli-a")
    asyncio.run(service._drain_failure_notices())
    created_at = _workspace_notice_session_rows()[0]["created_at"]

    second = _fail("task-cli-b")
    asyncio.run(service._drain_failure_notices())

    rows = _workspace_notice_session_rows()
    assert len(rows) == 1, f"the reserved session must be created once, not per notice: {rows}"
    assert rows[0]["created_at"] == created_at, (
        "the second notice must REUSE the row, not replace it"
    )

    messages = _persisted_messages()
    assert [row["session_id"] for row in messages] == [
        WORKSPACE_NOTICE_SESSION_ID,
        WORKSPACE_NOTICE_SESSION_ID,
    ], f"both notices must land in the one workspace session: {messages}"
    for run_id in (first, second):
        assert sqlite.owed_failure_notice(run_id)["state"] == NOTICE_SENT

    third = _fail("task-cli-c")
    asyncio.run(service._drain_failure_notices())

    assert len(_workspace_notice_session_rows()) == 1, (
        "later notices must keep using the retained workspace Session"
    )
    assert len(_persisted_messages()) == 3
    assert sqlite.owed_failure_notice(third)["state"] == NOTICE_SENT, (
        "retaining accepted history cannot make a later notice unsendable"
    )


def test_an_archived_workspace_notice_session_heals_instead_of_swallowing_the_notice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The one state "lazy recreation" does NOT cover, and it fails SILENTLY.

    Recreation is keyed on the row being GONE. An archive leaves the reserved primary
    key in place, so nothing recreates — and every downstream half of the delivery
    disagrees about what archived means:

    * ``persist_agent_message`` resolves the session through ``_session_row``, which has
      NO status filter, so the message still persists;
    * a persisted row is the workbench class's ack source, so the rung ACKS and the
      notice is stamped ``sent``;
    * but ``list_inbox_sessions`` / ``get_inbox_session`` exclude archived sessions, so
      there is no card, no ``inbox.session.updated`` and no Web Push.

    So every later caller-less failure is recorded as DELIVERED into a surface nothing
    displays — strictly worse than the dead letter this branch replaced, because a dead
    letter at least says so. Asserted on the INBOX SURFACE rather than on the notice
    state, which is exactly where the two disagree.

    The heal runs under the same ``reserve_write_lock`` re-read as the create, so it
    also repairs a row archived before the ``archive_session`` guard existed. The
    archive here is driven by DIRECT SQL on purpose: the guard
    (``tests/test_session_archive.py::test_the_reserved_workspace_notice_session_cannot_be_archived``)
    now refuses the ordinary path, and a test that could only reach this state through a
    door that is closed would prove nothing about a database that is already in it.
    """

    from sqlalchemy import update as sa_update

    from storage.agent_session_rows import (
        WORKSPACE_NOTICE_SESSION_ANCHOR,
        WORKSPACE_NOTICE_SESSION_ID,
    )
    from storage.db import get_cached_sqlite_engine
    from storage.messages_service import get_inbox_session
    from storage.models import agent_sessions

    _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    _migrated_state_db()

    sqlite, requests = _store(tmp_path)
    service = _drain_service(tmp_path, controller, sqlite, requests)

    def _fail(definition_id: str) -> str:
        _task(sqlite, definition_id, name=definition_id)
        run = requests.enqueue_task_run(definition_id)
        claimed = requests.claim(run.id)
        assert claimed is not None
        requests.complete(claimed, ok=False, error="boom", task_id=definition_id)
        return run.id

    _fail("task-heal-a")
    asyncio.run(service._drain_failure_notices())
    with get_cached_sqlite_engine().begin() as conn:
        assert get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is not None

    # --- the state the guard now forbids, reached out of band -----------------
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(
            sa_update(agent_sessions)
            .where(agent_sessions.c.id == WORKSPACE_NOTICE_SESSION_ID)
            .values(
                status="archived",
                # ``archive_session`` vacates the thread anchor too, so the heal has to
                # restore that as well or the row keeps an anchor nothing resolves.
                session_anchor=f"archived:{WORKSPACE_NOTICE_SESSION_ID}",
            )
        )
    with get_cached_sqlite_engine().begin() as conn:
        assert get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID) is None, (
            "the premise: an archived session shows no inbox card"
        )

    second = _fail("task-heal-b")
    asyncio.run(service._drain_failure_notices())

    with get_cached_sqlite_engine().begin() as conn:
        row = dict(
            conn.execute(
                select(agent_sessions).where(agent_sessions.c.id == WORKSPACE_NOTICE_SESSION_ID)
            ).mappings().first()
        )
        card = get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID)

    # THE DEFECT, asserted in the order it happens. The notice acks either way — that
    # is what makes the failure silent — so the ack is checked FIRST and then the
    # surface it claims to have reached. On the unhealed code the ack passes and the
    # card is ``None``.
    assert sqlite.owed_failure_notice(second)["state"] == NOTICE_SENT, (
        "the premise: this rung acks on the persisted row, archived or not"
    )
    assert card is not None, (
        "an acked notice with no inbox card is worse than the dead letter this "
        "branch replaced — a dead letter at least says so"
    )
    assert row["status"] == "active", f"the notice must heal the row it needs: {row}"
    assert row["session_anchor"] == WORKSPACE_NOTICE_SESSION_ANCHOR, (
        f"including the reserved anchor the archive vacated: {row}"
    )
    assert row["visibility"] == "system", (
        "and the system visibility the inbox feed admits while ordinary session "
        f"lists exclude: {row}"
    )
    assert [row["session_id"] for row in _persisted_messages()] == [
        WORKSPACE_NOTICE_SESSION_ID,
        WORKSPACE_NOTICE_SESSION_ID,
    ]
    assert len(_workspace_notice_session_rows()) == 1, (
        "healing repairs the reserved row; it never mints a second one"
    )


def test_an_archived_ordinary_session_is_rerouted_instead_of_acked_into(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-079, subordinate — the SAME hole for an ordinary session, opposite remedy.

    Round-13 P1, review thread 3676292667: "an archived-session receipt is acked as
    delivery but is invisible". The heal above closes this for the RESERVED workspace
    session; a run pinned to an ordinary session that its owner later archived reaches
    the identical disagreement by a different door — ``persist_agent_message`` resolves
    through ``_session_row`` (no status filter) and writes, the receipt is the workbench
    class's ack source so the notice is stamped ``sent`` and never retried, and
    ``list_inbox_sessions`` / ``get_inbox_session`` exclude archived sessions so no card,
    no realtime patch and no push is produced.

    WHY THIS ONE IS NOT HEALED. The reserved row may not be archived at all
    (``archive_session`` refuses its id), so an archived one is corruption and repairing
    it restores an invariant. An ordinary session was archived by its owner ON PURPOSE.
    Un-archiving it to deliver a failure notice would overrule that decision and
    resurface the whole session, not the notice. The session is not broken; the ADDRESS
    is. So the fix is routing (``_rung_five_session_id``), and this test asserts routing
    — the candidate itself, before any send — rather than a repaired row.

    Nor is the ack policy the place to fix it. The receipt-only source is the right
    measurement for a MISSING row (nothing persists, so nothing acks — see
    ``test_an_avibe_rung_does_not_ack_on_a_synthetic_send_id`` pass 1) and it is blind
    here precisely because the write is genuine.

    RED ON THE PRE-FIX HEAD, in the order the failure happens: the notice acks ``sent``
    with its receipt inside the archived session and the inbox shows nothing. The ack is
    therefore asserted FIRST — it passes either way, which is what makes the bug silent
    — and then the surface it claims to have reached.

    Deliberately a SESSION-ONLY definition: no ``deliver_key`` and no ``created_by``, so
    rungs (1), (3) and (4) are empty and rung (5) is the only rung. A definition with a
    live preferred rung would let that rung win the walk and prove nothing about which
    session rung (5) named.

    WHAT THE MANDATORY WORKSPACE RUNG DOES AND DOES NOT ACCOUNT FOR HERE. Since the
    round-14 gate every ladder ends with a workspace rung, so "the notice reached the
    workspace inbox" no longer distinguishes routing from fallback on its own. The claim
    this test carries is therefore the NEGATIVE one, asserted on the ladder before any
    send: ``avibe::project::sesArchived`` is ABSENT. Without the reroute it would be
    there and would be walked FIRST, persist into the archived row, and ack — so the
    appended rung would never be reached and the notice would be invisible. The ladder
    assertion below is what separates the two mechanisms; ``…_holds_exactly_one_workspace_rung``
    pins that they collapse into one rung rather than two.
    """

    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.db import get_cached_sqlite_engine
    from storage.messages_service import get_inbox_session
    from storage.models import agent_sessions

    pushed = _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    archived_scope_id = _workbench_session(
        "sesArchived", project="proj-archived", status="archived"
    )

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-archived", name="nightly report", session_id="sesArchived")
    run = requests.enqueue_task_run("task-archived")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-archived")

    service = _drain_service(tmp_path, controller, sqlite, requests)

    # --- the routing decision, asserted before anything is sent --------------
    rungs = service._failure_notice_targets(sqlite.get_run(run.id))
    assert [target.to_key() for target, _ in rungs] == [
        f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}"
    ], (
        "rung (2) refuses an archived session, and rung (5) must not quietly deliver "
        f"into it either — the only visible surface left is the workspace inbox: {rungs}"
    )
    assert [session_id for _target, session_id in rungs] == [WORKSPACE_NOTICE_SESSION_ID], (
        f"the session the context is built with has to be rerouted too: {rungs}"
    )

    asyncio.run(service._drain_failure_notices())

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == NOTICE_SENT, (
        f"the premise: this rung acks on a persisted receipt either way: {notice}"
    )
    assert notice["ack_evidence"] == ACK_EVIDENCE_RECEIPT
    assert notice["attempts"] == 1, "one visible delivery, on the first attempt"

    rows = _persisted_messages()
    assert [(row["platform"], row["type"], row["session_id"]) for row in rows] == [
        ("avibe", "notify", WORKSPACE_NOTICE_SESSION_ID)
    ], f"one notice, in the session the inbox displays: {rows}"
    assert rows[0]["scope_id"] != archived_scope_id, (
        f"and nothing at all in the archived session's scope: {rows}"
    )
    assert str(rows[0]["content_text"] or "").strip(), f"an empty notice is not a notice: {rows}"
    assert f'"failure_id": "{run.id}"' in str(rows[0]["metadata_json"] or ""), (
        "and it is still THIS run's notice — rerouting changes the address, never the "
        f"identity the live path's dedup looks up: {rows}"
    )

    with get_cached_sqlite_engine().begin() as conn:
        assert get_inbox_session(conn, "sesArchived") is None, (
            "the premise of the finding: an archived session has no inbox card, so an "
            "acked notice inside it is invisible and — being acked — never retried"
        )
        card = get_inbox_session(conn, WORKSPACE_NOTICE_SESSION_ID)
    assert card is not None, "the rerouted notice must produce a card a user can see"

    # And the push fan-out, which is the surface for a user who is not looking at the
    # tab — the third thing the archived row silently withheld.
    assert [payload["session_id"] for payload in pushed] == [WORKSPACE_NOTICE_SESSION_ID], (
        f"the rerouted notice must be pushed, not only stored: {pushed}"
    )

    # The archived session is left exactly as its owner left it. This is the line that
    # separates this remedy from the reserved session's heal.
    with get_cached_sqlite_engine().begin() as conn:
        status = conn.execute(
            select(agent_sessions.c.status).where(agent_sessions.c.id == "sesArchived")
        ).scalar_one_or_none()
    assert status == "archived", (
        "delivering a failure notice may not un-archive a session the user archived on "
        f"purpose: {status}"
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


def test_ten_unstampable_retry_instants_do_not_starve_the_notice_behind_them(
    tmp_path: Path,
) -> None:
    """HFR-085 — a row the SQL admits and Python drops must not hold a batch slot.

    The same starvation as the test above, reached through the OTHER half of the
    eligibility contract. ``list_owed_failure_notices`` seeks and LIMITS in SQL and
    then re-checks the decoded blob in Python, so a ``next_attempt_at`` the two sides
    read differently is a row that is selected, dropped, and never written — the one
    thing that guarantees it is selected again next tick.

    A JSON number is the sharpest shape: SQLite compares storage classes, so every
    INTEGER sorts before every TEXT and the row is both ELIGIBLE and FIRST in
    ``ORDER BY next_attempt_at``. Ten of them therefore fill the batch of ten ahead of
    every legitimately eligible notice, on every tick, forever.

    The fix is not to make the drain skip them harder: it is that a row admitted by
    the listing must be advanced by the pass that reads it. Degrade and advance, as
    ``notice_write_expectation`` already does for a malformed ``attempts`` — the claim
    stamps a real ISO instant over the unreadable one, so the poisoned row leaves the
    front of the queue by being HANDLED rather than by being avoided.

    MORE POISONED ROWS THAN THE BATCH LIMIT, and every off-domain shape among them:
    twelve numerics (which sort ahead of the valid notice), padded instants (text, so
    they sort behind it), and containers (never eligible on either side, so they are
    never listed at all — the domain table in ``owed_notice_eligible`` states that
    treatment, and this test pins that it starves nothing).
    """

    from types import SimpleNamespace

    import core.scheduled_tasks as scheduled_tasks
    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-poisoned", deliver_key="slack::channel::C1")
    _task(sqlite, "task-behind", deliver_key="slack::channel::C2")

    # MORE than one batch worth of rows whose retry instant is a JSON number. Every one
    # is ``<= now`` in SQL and sorts ahead of every ISO instant and ahead of the empty
    # string a freshly stamped notice reads as, so all twelve are ahead of the valid
    # notice below.
    for index in range(12):
        _pending_failure(
            sqlite,
            f"run-poisoned-{index:02d}",
            "task-poisoned",
            created_at=f"2026-07-27T00:{index:02d}:00+00:00",
            notice={
                "state": "pending",
                "attempts": 0,
                "next_attempt_at": 30000 + index,
                "failure_id": f"run-poisoned-{index:02d}",
            },
        )
    # Padded instants: still TEXT, so they sort behind the valid notice, and eligible on
    # both sides only because neither side strips.
    for index in range(3):
        _pending_failure(
            sqlite,
            f"run-padded-{index:02d}",
            "task-poisoned",
            created_at=f"2026-07-27T01:{index:02d}:00+00:00",
            notice={
                "state": "pending",
                "attempts": 0,
                "next_attempt_at": " 9999-01-01T00:00:00+00:00",
                "failure_id": f"run-padded-{index:02d}",
            },
        )
    # Containers: ineligible on BOTH sides, so the seek never returns them. They occupy
    # no slot and starve nothing — the other half of the domain's explicit treatment.
    for index, value in enumerate(({"at": "2020-01-01T00:00:00+00:00"}, [1], [])):
        _pending_failure(
            sqlite,
            f"run-container-{index:02d}",
            "task-poisoned",
            created_at=f"2026-07-27T00:00:0{index}+00:00",
            notice={
                "state": "pending",
                "attempts": 0,
                "next_attempt_at": value,
                "failure_id": f"run-container-{index:02d}",
            },
        )
    # ...and one ordinary notice behind them, eligible now.
    _pending_failure(
        sqlite,
        "run-behind",
        "task-behind",
        created_at="2026-07-27T02:00:00+00:00",
        notice={
            "state": "pending",
            "attempts": 0,
            "next_attempt_at": None,
            "failure_id": "run-behind",
        },
    )

    # The batch the SQL LIMIT filled must not arrive HOLLOW. Ten rows were admitted
    # before the limit; if the Python re-check drops them afterwards the drain gets an
    # empty pass while nineteen notices are owed, and gets the same empty pass forever.
    listed = [item["id"] for item in sqlite.list_owed_failure_notices(limit=10)]
    assert len(listed) == 10, (
        "the limited batch came back short while nineteen notices were owed: the rows SQL "
        "admitted were dropped by the Python re-check AFTER the limit, so the batch is a "
        f"hole rather than a queue (listed={listed})"
    )
    assert not [run_id for run_id in listed if run_id.startswith("run-container-")], (
        "a container-valued retry instant was listed; it is ineligible on both sides "
        f"and must never consume a slot (listed={listed})"
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
        for _ in range(4):
            asyncio.run(service._drain_failure_notices())

    assert sqlite.owed_failure_notice("run-behind")["state"] == "sent", (
        "a notice was starved by ten rows the listing admitted and the drain dropped; "
        f"delivered={delivered}"
    )
    # ...and every poisoned row was ADVANCED rather than skipped over: settled, or
    # deferred to a real instant. None of them is still immediately eligible, so none
    # can re-occupy the front of the queue on the next tick. That — not the field's
    # final value — is the anti-starvation property, and it is asserted through the
    # very predicate the listing seeks on.
    from datetime import datetime, timezone

    later = datetime.now(timezone.utc).isoformat()
    admitted = [f"run-poisoned-{index:02d}" for index in range(12)]
    admitted += [f"run-padded-{index:02d}" for index in range(3)]
    stuck = {run_id: sqlite.owed_failure_notice(run_id) for run_id in admitted}
    stuck = {
        run_id: notice for run_id, notice in stuck.items() if owed_notice_eligible(notice, later)
    }
    assert not stuck, (
        "rows the listing admitted are still immediately eligible after being read by "
        f"four passes; each holds a batch slot for good: {stuck}"
    )
    # The containers made no progress, which is the stated treatment rather than an
    # oversight: they are ineligible on both sides, so no pass ever reads them. What
    # they must not do is hold a slot — proved by the valid notice above having been
    # delivered while they sat there.
    for index in range(3):
        container = sqlite.owed_failure_notice(f"run-container-{index:02d}")
        assert container["state"] == "pending" and not owed_notice_eligible(container, later), (
            "a container-valued retry instant changed the domain's answer: it is "
            f"ineligible and undelivered by design ({container})"
        )


@pytest.mark.parametrize(
    "attempts",
    ["-3q", -3, -(2**63), -1e100],
    ids=["negative-prefix-string", "negative-int", "int64-min", "saturated-real"],
)
def test_a_negative_attempt_counter_starts_the_ladder_instead_of_counting_up(
    attempts,
) -> None:
    """Subordinate to HFR-076 — the round-20 finding: clamp the POLICY, not the CAS.

    CAST semantics can read a negative counter from an out-of-band value ("-3q"
    reads -3; a saturated number reads INT64_MIN), and a policy that increments
    FROM it grants the notice more than ``MAX_ATTEMPTS`` attempts — roughly 9.2
    quintillion for the saturated case, each armed with the shortest backoff.
    The split is deliberate and directional: ``notice_write_expectation`` keeps
    asserting the RAW stored value (the CAS must match the row as it is, or the
    claim never lands at all), while ``next_attempt`` clamps the policy read to
    the start of the ladder — so the first claim stamps attempt 1 over the poison
    and the row is a normal notice again in one pass.
    """

    from core.failure_notices import BACKOFF_SECONDS, next_attempt

    assert next_attempt({"state": "pending", "attempts": attempts}) == (
        1,
        BACKOFF_SECONDS[0],
    ), "a negative counter must start the retry ladder, not extend it downward"


def test_twelve_cast_divergent_attempt_counters_do_not_starve_the_notice_behind_them(
    tmp_path: Path,
) -> None:
    """Subordinate to HFR-076 — the round-19 finding: the OTHER field's CAST split.

    Same starvation as the retry-instant test above, reached through ``attempts``.
    The listing admits a pending notice regardless of its counter, but the claim
    asserts the counter through SQLite's ``CAST(... AS INTEGER)`` — which parses a
    numeric PREFIX (``"3x"`` reads 3, ``"1e100"`` reads 1) and saturates at the
    signed-64-bit bounds, where Python's ``int()`` raises and the old reader
    degraded to 0. An expectation of 0 against a stored ``"3x"`` is a claim that
    can never match: the row is selected, refused, and unchanged on every pass,
    holding a batch slot forever, with every notice behind it starved.

    Both readers (``core.failure_notices._attempts_read`` and
    ``storage.background.notice_write_expectation``) now consume ONE model of the
    CAST (``storage.sqlite_semantics.sqlite_cast_integer``) for the DECODE, so the
    claim's expectation matches the stored value and the write lands. The two final
    numbers are deliberately NOT identical for every shape: for a NEGATIVE decode
    the CAS asserts the raw stored value while the policy clamps to the ladder's
    start before incrementing (round 20 — an unclamped increment from INT64_MIN
    would grant ~9.2 quintillion attempts). For every nonnegative shape they are
    the same number. Either way the claim lands, stamps a real integer over the
    malformed counter, and the row advances through the ordinary backoff toward
    delivery or the visible dead letter. Bounded progress, not a broad retry and
    not a widened batch.
    """

    from types import SimpleNamespace

    import core.scheduled_tasks as scheduled_tasks
    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-cast", deliver_key="slack::channel::C1")
    _task(sqlite, "task-cast-behind", deliver_key="slack::channel::C2")

    # MORE divergent counters than the batch limit, in every shape the finding
    # names: numeric-prefix strings, scientific-notation prefixes, padded digits,
    # signed prefixes, reals-as-text, and out-of-range numbers. Each reads as a
    # different integer under CAST than under the old int()-or-0 read.
    divergent = ["3x", "1e100", " 5 ", "  +7x", "-3q", "3.9", "2x", "4y",
                 9223372036854775808, 1e100, "1e19", "8 8"]
    for index, counter in enumerate(divergent):
        _pending_failure(
            sqlite,
            f"run-cast-{index:02d}",
            "task-cast",
            created_at=f"2026-07-27T00:{index:02d}:00+00:00",
            notice={
                "state": "pending",
                "attempts": counter,
                "next_attempt_at": None,
                "failure_id": f"run-cast-{index:02d}",
            },
        )
    _pending_failure(
        sqlite,
        "run-cast-behind",
        "task-cast-behind",
        created_at="2026-07-27T02:00:00+00:00",
        notice={
            "state": "pending",
            "attempts": 0,
            "next_attempt_at": None,
            "failure_id": "run-cast-behind",
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
        for _ in range(4):
            asyncio.run(service._drain_failure_notices())

    assert sqlite.owed_failure_notice("run-cast-behind")["state"] == "sent", (
        "a valid notice was starved by rows whose attempt counter the claim could "
        f"never re-assert; delivered={delivered}"
    )
    # And the divergent rows made bounded progress under the guarded claim: none is
    # still holding a malformed counter AND immediately eligible. Each was either
    # delivered, dead-lettered on running off the ladder (counters that CAST past
    # MAX_ATTEMPTS dead-letter on their first read — the '1e100'-as-number and
    # out-of-range shapes), or advanced onto a real integer with a real backoff.
    from datetime import datetime, timezone

    later = datetime.now(timezone.utc).isoformat()
    stuck = {}
    for index in range(len(divergent)):
        notice = sqlite.owed_failure_notice(f"run-cast-{index:02d}")
        # A REAL nonnegative integer — ``-2`` from an unclamped negative increment
        # would have satisfied a bare isinstance check (the round-20 gate's point).
        attempts_now = notice.get("attempts")
        if notice["state"] == "pending" and not (
            isinstance(attempts_now, int) and not isinstance(attempts_now, bool) and attempts_now >= 1
        ):
            stuck[f"run-cast-{index:02d}"] = notice
    assert not stuck, (
        "rows with CAST-divergent counters were read by four passes and never "
        f"advanced onto a real nonnegative attempt — each holds a batch slot for good: {stuck}"
    )


def test_negative_attempt_counters_claim_at_the_raw_value_and_dead_letter_on_schedule(
    tmp_path: Path,
) -> None:
    """Subordinate to HFR-076 — the round-20 clamp, proven at the consuming end.

    The direct policy test pins ``next_attempt``'s answer; this one proves the
    machine: for a NEGATIVE stored counter (a ``"-3q"`` hand-edit, a negative JSON
    scalar, the i64 floor a saturated number decodes to) the real guarded claim
    matches the RAW negative value — that is what the CAS asserts, deliberately
    unclamped — and persists attempt exactly 1 with the first declared backoff
    armed. The same rows then follow the ordinary retry schedule to the ordinary
    dead letter in the normal bounded attempt count.

    MORE negative rows than the drain's batch limit sit ahead of a valid due
    notice, so the batch BOUNDARY is exercised, not just batch ordering: pass 1
    claims exactly the first batch and leaves the remainder untouched (unclaimed
    — no attempt consumed, no backoff armed); pass 2, inside the first batch's
    backoff, must advance PAST those backed-off rows to the remaining negatives
    and the valid notice rather than reselecting the first batch forever.

    Red on the pre-clamp head: the first claim persisted the negative increment
    (``-2`` from ``"-3q"``; ``INT64_MIN + 1`` from the floor), never attempt 1,
    and the floor row could not dead-letter in any bounded number of passes.
    """

    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import core.scheduled_tasks as scheduled_tasks
    from core.failure_notices import BACKOFF_SECONDS, MAX_ATTEMPTS
    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore
    from storage.background import notice_write_expectation

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-negative-behind", deliver_key="slack::channel::C2")

    # One DEFINITION per negative row: rows of one definition share a streak, so
    # only the canonical would be claimed per pass and the rest would defer —
    # correct suppression, but it would hide the per-row claim this test exists
    # to prove. Separate definitions make every row its own canonical.
    #
    # THIRTEEN rows — more than the drain's batch limit of 10 — with all three
    # required shapes (negative-prefix text, negative scalar, i64 floor) present
    # in the first batch AND in the remainder past the boundary. The listing
    # orders by ``(created_at, id)``, so indexes 0-9 are the first batch.
    negatives: list[Any] = [
        "-3q", -3, -(2**63), "-1x", -1, -1e100, "-7q", -5, -(2**63), " -2x",
        # Past the batch boundary:
        "-9z", -4, -(2**63),
    ]
    for index, counter in enumerate(negatives):
        _task(sqlite, f"task-neg-{index:02d}", deliver_key="slack::channel::C1")
        _pending_failure(
            sqlite,
            f"run-neg-{index:02d}",
            f"task-neg-{index:02d}",
            created_at=f"2026-07-27T00:{index:02d}:00+00:00",
            notice={
                "state": "pending",
                "attempts": counter,
                "next_attempt_at": None,
                "failure_id": f"run-neg-{index:02d}",
            },
        )
        # The premise, asserted before any pass runs: the CAS expectation reads the
        # RAW negative — the clamp is policy-side only. If the expectation clamped
        # too, the claim would assert a value the row does not hold and never land.
        expectation = notice_write_expectation(sqlite.owed_failure_notice(f"run-neg-{index:02d}"))
        assert expectation[1] < 0, (
            f"the CAS must assert the stored negative as-is, got {expectation!r} for {counter!r}"
        )
    _pending_failure(
        sqlite,
        "run-neg-behind",
        "task-negative-behind",
        created_at="2026-07-27T02:00:00+00:00",
        notice={
            "state": "pending",
            "attempts": 0,
            "next_attempt_at": None,
            "failure_id": "run-neg-behind",
        },
    )

    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()

    async def _spy_emit(controller, context, backend, diagnostic, **kwargs):
        # The valid notice behind the negatives delivers; the negative rows'
        # deliveries FAIL every time, so they must walk the declared retry
        # schedule to the ordinary dead letter.
        failure_id = str(kwargs.get("failure_id"))
        evidence = kwargs.get("delivery")
        if failure_id == "run-neg-behind" and evidence is not None:
            evidence.delivered_id = "m1"
            evidence.persisted_row = {"id": "m1"}
        return False

    import pytest as _pytest

    import storage.background as storage_background

    # ONE FROZEN INSTANT for every clock the consuming path reads, so pass-two
    # eligibility is deterministic BY CONSTRUCTION: the listing's default ``now``
    # (``storage.background._utc_now_iso``) and every eligibility/claim/retry
    # calculation in ``core.scheduled_tasks`` (its module ``datetime``). The
    # production 2-second first rung stays UNCHANGED — pass 1 stamps
    # ``frozen + BACKOFF_SECONDS[0]`` and pass 2 lists at ``frozen``, which is
    # provably inside the backoff no matter how long the process is suspended
    # between the passes; there is no wall-clock read left for preemption to
    # advance. Stored deadlines are then asserted EXACTLY from the frozen
    # instant. (The drain's pass budget uses ``time.monotonic`` and is not
    # frozen; with the spy emitter it never truncates.)
    frozen = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - datetime API shape
            return frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", _spy_emit)
        patch.setattr(scheduled_tasks, "datetime", _FrozenDatetime)
        patch.setattr(storage_background, "_utc_now_iso", lambda: frozen.isoformat())
        service = ScheduledTaskService.__new__(ScheduledTaskService)
        service.store = store
        service.request_store = requests
        service._drain_dirty = False
        service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
        service._owns_service_instance = lambda: True
        service.validate_platform = lambda platform: None
        service._t = lambda key, **kwargs: key

        # PASS 1 claims exactly the first batch. Snapshot the boundary between the
        # passes — pass 2 runs inside the (widened) first backoff to prove the
        # batch does not get reselected.
        # PASS 1 claims exactly the first batch; the boundary is snapshotted
        # between the passes.
        first_batch = list(range(10))
        remainder = list(range(10, len(negatives)))
        asyncio.run(service._drain_failure_notices())
        remainder_after_pass_one = {
            index: sqlite.owed_failure_notice(f"run-neg-{index:02d}") for index in remainder
        }
        valid_after_pass_one = sqlite.owed_failure_notice("run-neg-behind")["state"]

        # PASS 2, at the SAME frozen instant: the first batch is backed off by
        # construction, so the bounded drain must advance PAST it to the
        # remaining negatives and the valid notice.
        asyncio.run(service._drain_failure_notices())

        # The boundary, as pass 1 left it: rows past the limit were simply not
        # pulled — unclaimed, so no attempt consumed and no backoff armed, and the
        # CAS expectation still reads their RAW negative. The valid notice, last in
        # ``(created_at, id)`` order, was outside the first batch too.
        for index, snapshot in remainder_after_pass_one.items():
            assert snapshot["state"] == "pending" and snapshot.get("next_attempt_at") is None, (
                f"pass 1 must leave the row past the batch boundary unclaimed, got {snapshot!r}"
            )
            assert notice_write_expectation(snapshot)[1] < 0, (
                f"an unclaimed remainder row must still hold its raw negative, got {snapshot!r}"
            )
        assert valid_after_pass_one == "pending", (
            "the valid notice sorts after thirteen negatives — pass 1's batch of 10 "
            "must not have reached it"
        )

        # PASS 1's claims: the guarded claim matched the raw negative and persisted
        # exactly attempt 1, with the FIRST declared interval armed — the clamp
        # starting the ladder rather than extending it downward. Still attempt 1
        # after pass 2: the backoff excluded the first batch from reselection.
        for index in first_batch:
            notice = sqlite.owed_failure_notice(f"run-neg-{index:02d}")
            assert notice["attempts"] == 1, (
                f"the first claim over {negatives[index]!r} must persist attempt exactly 1 "
                f"and pass 2 must not reselect the backed-off batch, "
                f"got {notice['attempts']!r} (state={notice['state']!r})"
            )
            assert notice["state"] == "pending"
            # EXACT, from the frozen instant: the retry stamp is
            # ``frozen + BACKOFF_SECONDS[0]`` to the microsecond — rung 1, not
            # rung 2 and not the claim lease, with no timing tolerance at all.
            armed = datetime.fromisoformat(notice["next_attempt_at"])
            assert armed == frozen + timedelta(seconds=BACKOFF_SECONDS[0]), (
                f"the first backoff must be exactly the ladder's FIRST rung from the "
                f"frozen instant, got {notice['next_attempt_at']!r}"
            )
        # PASS 2's claims: the remainder crossed the boundary on the very next
        # bounded pass — attempt exactly 1, first interval armed — and the valid
        # due notice behind every negative row delivered instead of starving.
        for index in remainder:
            notice = sqlite.owed_failure_notice(f"run-neg-{index:02d}")
            assert notice["attempts"] == 1, (
                f"pass 2 must claim the remainder row {negatives[index]!r} at attempt 1, "
                f"got {notice['attempts']!r} (state={notice['state']!r})"
            )
            assert notice["state"] == "pending"
            armed = datetime.fromisoformat(notice["next_attempt_at"])
            assert armed == frozen + timedelta(seconds=BACKOFF_SECONDS[0]), (
                f"the remainder's first backoff must be exactly the ladder's FIRST rung "
                f"from the frozen instant, got {notice['next_attempt_at']!r}"
            )
        assert sqlite.owed_failure_notice("run-neg-behind")["state"] == "sent", (
            "thirteen negative rows ahead of it must not starve the valid due notice "
            "past the second pass"
        )

        # PASSES 3..N: rewind the backoff between passes and let the schedule run
        # out. Every negative row must reach the ordinary dead letter at exactly
        # the declared bound — MAX_ATTEMPTS — never more. The bound: 13 rows need
        # 5 more claims each and a pass claims at most 10, with ``(created_at,
        # id)`` order letting the first batch monopolize passes until it dead-
        # letters — 5 passes for the first ten, then 5 for the remainder, so
        # 2 * (MAX_ATTEMPTS + 1) passes is enough with slack.
        for _ in range(2 * (MAX_ATTEMPTS + 1)):
            pending = [
                index
                for index in range(len(negatives))
                if sqlite.owed_failure_notice(f"run-neg-{index:02d}")["state"] == "pending"
            ]
            if not pending:
                break
            for index in pending:
                sqlite.update_owed_failure_notice(f"run-neg-{index:02d}", next_attempt_at=None)
            asyncio.run(service._drain_failure_notices())

    for index in range(len(negatives)):
        notice = sqlite.owed_failure_notice(f"run-neg-{index:02d}")
        assert notice["state"] == "failed", (
            f"a negative counter must reach the ordinary dead letter, got {notice['state']!r} "
            f"for {negatives[index]!r} at attempts={notice['attempts']!r}"
        )
        assert notice["attempts"] == MAX_ATTEMPTS, (
            "the dead letter must arrive at the declared bound, not before or after: "
            f"{notice['attempts']!r} != {MAX_ATTEMPTS} for {negatives[index]!r}"
        )


def _callback_run(sqlite_store, run_id: str, definition_id: str, *, status: str) -> None:
    """A settled failure owing a notice, whose run also carries a callback."""

    from sqlalchemy import update as sa_update

    from storage.models import agent_runs

    _pending_failure(
        sqlite_store,
        run_id,
        definition_id,
        created_at="2026-07-27T00:00:00+00:00",
        notice={
            "state": "pending",
            "attempts": 0,
            "next_attempt_at": None,
            "failure_id": run_id,
        },
    )
    with sqlite_store.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(callback_session_id="ses-callback-target", callback_status=status)
        )


def _callback_session(
    sqlite_store,
    *,
    visibility: str = "foreground",
    status: str = "active",
) -> None:
    """The callback target row whose visibility decides whether a receipt is visible."""

    from storage.models import agent_sessions

    now = "2026-07-27T00:00:00+00:00"
    with sqlite_store.engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id="ses-callback-target",
                scope_id=None,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="callback-target",
                native_session_id="native-callback-target",
                status=status,
                visibility=visibility,
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )


def _persist_callback_receipt(
    sqlite_store,
    run_id: str,
    *,
    text: str,
    message_type: str = "result",
    delivery_suppressed: bool = False,
) -> None:
    """Materialize the durable message receipt that a callback success requires."""

    from storage import messages_service

    with sqlite_store.engine.begin() as conn:
        messages_service.append(
            conn,
            scope_id=None,
            session_id="ses-callback-target",
            platform="avibe",
            author="agent",
            source="agent",
            message_type=message_type,
            text=text,
            metadata={
                "run_id": run_id,
                **({"delivery_suppressed": True} if delivery_suppressed else {}),
            },
        )


def _notice_drain_service(tmp_path: Path, sqlite_store, requests) -> tuple[Any, list[str]]:
    """A detached drain service whose emitter records and acks every delivery."""

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite_store
    store.load()
    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.store = store
    service.request_store = requests
    service._drain_dirty = False
    service.controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
    service._owns_service_instance = lambda: True
    service.validate_platform = lambda platform: None
    service._t = lambda key, **kwargs: key
    delivered: list[str] = []

    async def _spy_emit(controller, context, backend, diagnostic, **kwargs):
        delivered.append(str(kwargs.get("failure_id")))
        evidence = kwargs.get("delivery")
        if evidence is not None:
            evidence.delivered_id = "m1"
            evidence.persisted_row = {"id": "m1"}
        return False

    service._spy_emit = _spy_emit  # kept referenced for the caller's MonkeyPatch
    return service, delivered


@pytest.mark.parametrize("receipt_type", ["result", "output"])
def test_failed_run_with_callback_delivers_exactly_one_message(
    tmp_path: Path,
    receipt_type: str,
) -> None:
    """Owed by the plan (corrected 2026-07-29): a pending callback IS the delivery.

    A failed run carrying ``callback_session_id`` gets a user-visible result turn
    from ``_drain_callbacks`` — the path the plan treats as sufficient notification
    — while terminalization stamps ``owed_failure_notice`` unconditionally for
    durability. Without coordination the two drains both fire and one failure
    produces two independently keyed messages.

    The notice drain must resolve on the callback's outcome: defer while it is
    ``pending`` (no attempt consumed — the row has not been tried), and acknowledge
    the notice as delivered-by-callback (``skipped``) once it is ``sent``. The
    fallback is proven live by the companion test
    ``test_owed_notice_takes_over_when_the_callback_dead_letters``.
    """

    import core.scheduled_tasks as scheduled_tasks
    from core.failure_notices import DEFERRAL_RECHECK_SECONDS

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-cb", deliver_key="slack::channel::C1")
    _callback_session(sqlite)
    _callback_run(sqlite, "run-cb", "task-cb", status="pending")
    callback = requests.enqueue_agent_run(
        message="deliver the callback",
        source_kind="callback",
        parent_run_id="run-cb",
        session_id="ses-callback-target",
    )
    sqlite.update_callback_status("run-cb", status="sent", callback_run_id=callback.id)

    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)

        # PASS 1: the parent has enqueued the callback child, but that child is still
        # queued. Enqueue is not delivery, so the notice steps aside — no
        # message, no attempt consumed, and the deferral is durable (a real backoff
        # instant, so the row does not occupy the batch on every tick).
        asyncio.run(service._drain_failure_notices())
        assert delivered == [], "the notice lane must not fire beside a pending callback"
        notice = sqlite.owed_failure_notice("run-cb")
        assert notice["state"] == "pending"
        assert not notice.get("attempts"), (
            f"a deferral consumes no attempt, got {notice.get('attempts')!r}"
        )
        assert notice.get("defer_reason") == "callback_pending", f"got {notice!r}"
        from datetime import datetime

        armed = datetime.fromisoformat(notice["next_attempt_at"])
        assert armed is not None  # a durable step-aside, not a Python-side continue
        assert DEFERRAL_RECHECK_SECONDS > 0

        # The callback child succeeds. Only now is the notice a duplicate of a
        # message the user has, and it must settle terminally.
        claimed_callback = requests.claim(callback.id)
        assert claimed_callback is not None
        delivered_callback = sqlite.record_run_output(
            callback.id,
            output_id="terminal",
            text="callback delivered",
            message_id="callback-message-1",
            terminal_status="succeeded",
        )
        assert delivered_callback["terminal_transition"]
        _persist_callback_receipt(
            sqlite,
            callback.id,
            text="callback delivered",
            message_type=receipt_type,
        )
        sqlite.update_owed_failure_notice("run-cb", next_attempt_at=None)
        asyncio.run(service._drain_failure_notices())
        notice = sqlite.owed_failure_notice("run-cb")
        assert notice["state"] == "skipped", f"got {notice!r}"
        assert notice.get("skip_reason") == "delivered_by_callback", f"got {notice!r}"
        assert delivered == [], (
            "exactly one message for the transition — the callback's; the notice "
            f"lane emitted {delivered!r}"
        )

        # And the skip is terminal: a later pass does not resurrect it.
        asyncio.run(service._drain_failure_notices())
        assert delivered == []
        assert sqlite.owed_failure_notice("run-cb")["state"] == "skipped"


def test_owed_notice_takes_over_when_the_callback_dead_letters(tmp_path: Path) -> None:
    """Owed by the plan (corrected 2026-07-29): the fallback exists the moment the
    primary path dies.

    Same transition as the companion test, but the callback delivery FAILED. The
    parent's unconditional fallback must become deliverable and walk the ordinary
    retry protocol, while the callback child never stamps a second notice for the
    delivery mechanism itself.

    The control is a binding-change notice riding a run whose callback is still
    ``pending``: it reports a fact (the pinned session was replaced) the callback's
    result turn never carries, so the shield must not silence it.
    """

    import core.scheduled_tasks as scheduled_tasks

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-cb-dead", deliver_key="slack::channel::C1")
    _callback_run(sqlite, "run-cb-dead", "task-cb-dead", status="pending")
    dead_callback = requests.enqueue_agent_run(
        message="deliver the callback",
        source_kind="callback",
        parent_run_id="run-cb-dead",
        session_id="ses-callback-target",
    )
    sqlite.update_callback_status(
        "run-cb-dead", status="sent", callback_run_id=dead_callback.id
    )
    claimed_callback = requests.claim(dead_callback.id)
    assert claimed_callback is not None
    failed_callback = sqlite.record_run_output(
        dead_callback.id,
        output_id="terminal",
        text="callback delivery failed",
        terminal_status="failed",
        updated_at="2026-07-27T00:00:01+00:00",
        error="callback delivery failed",
    )
    assert failed_callback["terminal_transition"]
    assert sqlite.owed_failure_notice(dead_callback.id) is None

    _task(sqlite, "task-cb-binding", deliver_key="slack::channel::C2")
    _callback_run(sqlite, "run-cb-binding", "task-cb-binding", status="pending")
    sqlite.update_owed_failure_notice("run-cb-binding", kind="binding_change")

    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)
        asyncio.run(service._drain_failure_notices())

    assert sorted(delivered) == ["run-cb-binding", "run-cb-dead"], (
        "a dead-lettered callback hands the transition back to the notice lane, and "
        "a binding-change notice is never shielded by an unrelated callback; got "
        f"{delivered!r}"
    )
    assert sqlite.owed_failure_notice("run-cb-dead")["state"] == "sent"
    assert sqlite.owed_failure_notice("run-cb-binding")["state"] == "sent"


def test_failed_callback_owns_notice_when_successful_parent_has_no_fallback(
    tmp_path: Path,
) -> None:
    """A callback delivery failure stays visible when its parent did not fail."""

    import core.scheduled_tasks as scheduled_tasks

    sqlite, requests = _store(tmp_path)
    _callback_session(sqlite)
    parent = requests.enqueue_agent_run(
        message="produce a successful result",
        session_id="ses-callback-target",
    )
    claimed_parent = requests.claim(parent.id)
    assert claimed_parent is not None
    parent_result = sqlite.record_run_output(
        parent.id,
        output_id="terminal",
        text="result ready",
        terminal_status="succeeded",
    )
    assert parent_result["terminal_transition"]
    assert sqlite.owed_failure_notice(parent.id) is None

    callback = requests.enqueue_agent_run(
        message="deliver the successful result",
        source_kind="callback",
        parent_run_id=parent.id,
        session_id="ses-callback-target",
    )
    sqlite.update_callback_status(parent.id, status="sent", callback_run_id=callback.id)
    claimed_callback = requests.claim(callback.id)
    assert claimed_callback is not None
    callback_result = sqlite.record_run_output(
        callback.id,
        output_id="terminal",
        text="callback delivery failed",
        terminal_status="failed",
        error="callback delivery failed",
    )
    assert callback_result["terminal_transition"]
    assert sqlite.owed_failure_notice(callback.id)["state"] == "pending"

    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)
        asyncio.run(service._drain_failure_notices())

    assert delivered == [callback.id]
    assert sqlite.owed_failure_notice(callback.id)["state"] == "sent"


def test_owed_notice_takes_over_when_callback_success_has_no_delivery_receipt(
    tmp_path: Path,
) -> None:
    """A child outcome is not proof that the callback reached its user.

    Ordinary result settlement is intentionally allowed after every IM send failed,
    and Avibe creates a synthetic send id before persistence, so neither
    ``status='succeeded'`` nor a Workbench ``message_ids_json`` entry can suppress
    the durable fallback. Only a real ``messages`` row carrying this child run's
    provenance is a Workbench receipt.
    """

    import core.scheduled_tasks as scheduled_tasks

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-cb-no-receipt", deliver_key="slack::channel::C1")
    _callback_session(sqlite)
    _callback_run(sqlite, "run-cb-no-receipt", "task-cb-no-receipt", status="pending")
    callback = requests.enqueue_agent_run(
        message="try the callback",
        source_kind="callback",
        parent_run_id="run-cb-no-receipt",
        session_id="ses-callback-target",
        session_key="avibe::project::proj_callback",
    )
    sqlite.update_callback_status(
        "run-cb-no-receipt", status="sent", callback_run_id=callback.id
    )
    claimed = requests.claim(callback.id)
    assert claimed is not None
    recorded = sqlite.record_run_output(
        callback.id,
        output_id="terminal",
        text="callback looked delivered",
        message_id="msg_synthetic_only",
        terminal_status="succeeded",
    )
    assert recorded["terminal_transition"]
    assert sqlite.get_run(callback.id)["message_ids"] == ["msg_synthetic_only"]

    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)
        asyncio.run(service._drain_failure_notices())

    assert delivered == ["run-cb-no-receipt"]
    assert sqlite.owed_failure_notice("run-cb-no-receipt")["state"] == "sent"


@pytest.mark.parametrize("session_teardown", ["none", "archived", "deleted"])
def test_callback_native_im_delivery_id_prevents_duplicate_failure_notice(
    tmp_path: Path,
    session_teardown: str,
) -> None:
    """A real IM send stays delivered after transcript or local-session loss."""

    import core.scheduled_tasks as scheduled_tasks

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-cb-native-receipt", deliver_key="slack::channel::C1")
    _callback_session(sqlite)
    _callback_run(
        sqlite,
        "run-cb-native-receipt",
        "task-cb-native-receipt",
        status="pending",
    )
    callback = requests.enqueue_agent_run(
        message="deliver through Slack",
        source_kind="callback",
        parent_run_id="run-cb-native-receipt",
        session_id="ses-callback-target",
        session_key="slack::channel::C1",
    )
    sqlite.update_callback_status(
        "run-cb-native-receipt", status="sent", callback_run_id=callback.id
    )
    claimed = requests.claim(callback.id)
    assert claimed is not None
    recorded = sqlite.record_run_output(
        callback.id,
        output_id="terminal",
        text="callback reached Slack",
        message_id="native-slack-message-id",
        terminal_status="succeeded",
    )
    assert recorded["terminal_transition"]

    from storage.models import agent_sessions

    with sqlite.engine.begin() as conn:
        if session_teardown == "archived":
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == "ses-callback-target")
                .values(status="archived")
            )
        elif session_teardown == "deleted":
            conn.execute(
                agent_sessions.delete().where(
                    agent_sessions.c.id == "ses-callback-target"
                )
            )
    assert sqlite.run_callback_state("run-cb-native-receipt") == "sent"

    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)
        asyncio.run(service._drain_failure_notices())

    assert delivered == []
    notice = sqlite.owed_failure_notice("run-cb-native-receipt")
    assert notice["state"] == "skipped"
    assert notice["skip_reason"] == "delivered_by_callback"


def test_owed_notice_takes_over_when_callback_receipt_is_in_a_hidden_session(
    tmp_path: Path,
) -> None:
    """A persisted background-history row is not a user-visible callback receipt."""

    import core.scheduled_tasks as scheduled_tasks

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-cb-hidden", deliver_key="slack::channel::C1")
    _callback_session(sqlite, visibility="background")
    _callback_run(sqlite, "run-cb-hidden", "task-cb-hidden", status="pending")
    callback = requests.enqueue_agent_run(
        message="persist only in hidden history",
        source_kind="callback",
        parent_run_id="run-cb-hidden",
        session_id="ses-callback-target",
    )
    sqlite.update_callback_status(
        "run-cb-hidden", status="sent", callback_run_id=callback.id
    )
    claimed = requests.claim(callback.id)
    assert claimed is not None
    recorded = sqlite.record_run_output(
        callback.id,
        output_id="terminal",
        text="hidden callback body",
        message_id="hidden-history-row",
        terminal_status="succeeded",
    )
    assert recorded["terminal_transition"]
    _persist_callback_receipt(sqlite, callback.id, text="hidden callback body")
    assert sqlite.run_callback_state("run-cb-hidden") == "failed"

    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)
        asyncio.run(service._drain_failure_notices())

    assert delivered == ["run-cb-hidden"]
    assert sqlite.owed_failure_notice("run-cb-hidden")["state"] == "sent"


def test_suppressed_callback_history_is_not_visible_delivery_evidence(
    tmp_path: Path,
) -> None:
    """A foreground Session does not make a suppress_delivery row visible."""

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-cb-suppressed", deliver_key="slack::channel::C1")
    _callback_session(sqlite, visibility="foreground")
    _callback_run(sqlite, "run-cb-suppressed", "task-cb-suppressed", status="pending")
    callback = requests.enqueue_agent_run(
        message="persist without outward delivery",
        source_kind="callback",
        parent_run_id="run-cb-suppressed",
        session_id="ses-callback-target",
    )
    sqlite.update_callback_status(
        "run-cb-suppressed",
        status="sent",
        callback_run_id=callback.id,
    )
    assert requests.claim(callback.id) is not None
    sqlite.record_run_output(
        callback.id,
        output_id="terminal",
        text="local callback history",
        terminal_status="succeeded",
    )
    _persist_callback_receipt(
        sqlite,
        callback.id,
        text="local callback history",
        delivery_suppressed=True,
    )

    assert sqlite.run_callback_state("run-cb-suppressed") == "failed"


def test_skip_reason_writer_cannot_erase_a_terminal_notice_from_its_write_gap(
    tmp_path: Path,
) -> None:
    """A queued diagnostic must not overwrite a terminal owner's metadata."""

    from sqlalchemy import event

    sqlite, requests = _store(tmp_path)
    run = requests.enqueue_agent_run(message="run later", source_kind="agent")
    interleaved: list[str] = []

    def _terminalize_before_skip_update(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if interleaved or not statement.lstrip().upper().startswith("UPDATE AGENT_RUNS"):
            return
        interleaved.append(statement)
        assert sqlite.settle_run_terminal(
            run.id,
            terminal_status="failed",
            error="backend failed before dispatch",
        ) == "failed"

    event.listen(sqlite.engine, "before_cursor_execute", _terminalize_before_skip_update)
    try:
        written = sqlite.record_run_skip_reason(
            run.id,
            reason="transport_unavailable",
            at="2026-07-31T00:00:00+00:00",
        )
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _terminalize_before_skip_update)

    assert interleaved, "the terminal owner never entered the write gap"
    assert written is False, "the queued-only writer must lose after terminal settlement"
    assert sqlite.get_run(run.id)["status"] == "failed"
    notice = sqlite.owed_failure_notice(run.id)
    assert notice is not None and notice["failure_id"] == run.id
    assert notice["state"] == "pending"


def test_skip_recovery_clear_cannot_erase_a_terminal_notice_from_its_write_gap(
    tmp_path: Path,
) -> None:
    """Recovery clears only its JSON keys and only while the row stays queued."""

    from sqlalchemy import event

    sqlite, requests = _store(tmp_path)
    run = requests.enqueue_agent_run(message="run later", source_kind="agent")
    assert sqlite.record_run_skip_reason(
        run.id,
        reason="transport_unavailable",
        at="2026-07-31T00:00:00+00:00",
    )
    interleaved: list[str] = []

    def _terminalize_before_clear_update(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if interleaved or not statement.lstrip().upper().startswith("UPDATE AGENT_RUNS"):
            return
        interleaved.append(statement)
        assert sqlite.settle_run_terminal(
            run.id,
            terminal_status="failed",
            error="backend failed during recovery",
        ) == "failed"

    event.listen(sqlite.engine, "before_cursor_execute", _terminalize_before_clear_update)
    try:
        cleared = sqlite._clear_transport_skip_evidence({run.id})
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _terminalize_before_clear_update)

    assert interleaved, "the terminal owner never entered the write gap"
    assert cleared == 0, "the queued-only clear must lose after terminal settlement"
    assert sqlite.get_run(run.id)["status"] == "failed"
    notice = sqlite.owed_failure_notice(run.id)
    assert notice is not None and notice["failure_id"] == run.id
    assert notice["state"] == "pending"


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


def test_the_eligibility_seek_still_constrains_both_terms_with_off_domain_rows(
    tmp_path: Path,
) -> None:
    """HFR-086 — the plan must survive the values the domain normalization admits.

    A PIN, not a repair: HFR-086 above proves the seek constrains both terms over
    text-only rows, and the eligibility domain now admits values that are not text at
    all. Whether an index over a JSON expression still yields a two-term range
    constraint when the indexed values span several STORAGE CLASSES is a property of
    SQLite, not of this code — so it is asserted rather than argued.

    The reasoning it replaces: a numeric key sorts below every text key, so the
    ``<= now`` upper bound still bounds it and the range stays a range; a container key
    sorts above every ISO instant, so it falls outside the bound and is never visited.
    Both are conclusions about the b-tree, and the way to keep a conclusion true is to
    fail the build when it stops being.

    Stronger than HFR-086 on the two failure modes that make the tick unbounded again:
    no ``TEMP B-TREE`` (the ``ORDER BY`` must still be served by the index) and no
    ``SCAN`` of ``agent_runs`` (one off-domain row must not cost a full walk).
    """

    import sqlite3

    from sqlalchemy import event

    sqlite_store, _requests = _store(tmp_path)
    _task(sqlite_store, "task-mixed")
    # Text rows, all in backoff: the population HFR-086 pins.
    for index in range(40):
        _pending_failure(
            sqlite_store,
            f"run-text-{index:03d}",
            "task-mixed",
            created_at=f"2026-07-27T00:{index:02d}:00+00:00",
            notice={
                "state": "pending",
                "attempts": 3,
                "next_attempt_at": "2099-01-01T00:00:00+00:00",
                "failure_id": f"run-text-{index:03d}",
            },
        )
    # ...and every off-domain shape, interleaved through the same index: numerics and
    # booleans (below the bound, so INSIDE the range), padded instants (text, early),
    # and containers (above every instant, so outside the range).
    off_domain: list[Any] = [
        30000,
        30001,
        -5,
        0,
        3.5,
        1e25,
        True,
        False,
        " 9999-01-01T00:00:00+00:00",
        " 2020-01-01T00:00:00+00:00",
        {"a": 1},
        [1],
    ]
    for index, value in enumerate(off_domain):
        _pending_failure(
            sqlite_store,
            f"run-offdomain-{index:03d}",
            "task-mixed",
            created_at=f"2026-07-26T00:{index:02d}:00+00:00",
            notice={
                "state": "pending",
                "attempts": 0,
                "next_attempt_at": value,
                "failure_id": f"run-offdomain-{index:03d}",
            },
        )

    captured: list[tuple[str, Any]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "agent_runs" in statement and statement.strip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    event.listen(sqlite_store.engine, "before_cursor_execute", _capture)
    try:
        listed = sqlite_store.list_owed_failure_notices(now="2026-07-27T12:00:00+00:00")
    finally:
        event.remove(sqlite_store.engine, "before_cursor_execute", _capture)

    # The rows the domain admits came back, and nothing the domain excludes did — the
    # plan below is a plan for the query that actually answers this population.
    ids = {str(item["id"]) for item in listed}
    assert ids and all(run_id.startswith("run-offdomain-") for run_id in ids), (
        f"the seek returned rows outside the admitted domain: {sorted(ids)}"
    )

    statement, parameters = captured[-1]
    raw = sqlite3.connect(str(tmp_path / "state" / "vibe.sqlite"))
    try:
        plan = [row[-1] for row in raw.execute("EXPLAIN QUERY PLAN " + statement, parameters)]
    finally:
        raw.close()
    rendered = "\n".join(plan)

    assert OWED_NOTICE_INDEX in rendered, f"plan was:\n{rendered}"
    assert "AND" in rendered.split(OWED_NOTICE_INDEX, 1)[1].split("\n")[0], (
        "both the state and the next-attempt terms must stay CONSTRAINED with "
        f"off-domain values in the index; plan was:\n{rendered}"
    )
    assert "TEMP B-TREE" not in rendered.upper(), (
        f"the ORDER BY fell back to a temp sort; plan was:\n{rendered}"
    )
    assert "SCAN" not in rendered.upper(), (
        f"one off-domain row cost a full table walk; plan was:\n{rendered}"
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
    """HFR-093 — live delivery is primary and the durable drain remains a fallback."""

    _migrated_state_db()
    controller, _dispatcher, _touched = _live_turn_dispatcher()

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-suppressed-dedup", deliver_key="slack::channel::C123")
    runs = []
    for index in range(2):
        run = requests.enqueue_task_run("task-suppressed-dedup")
        requests.claim(run.id)
        sqlite.record_run_output(
            run.id,
            output_id="terminal",
            text="",
            terminal_status="failed",
            error="backend failed",
            provenance={
                "turn_id": f"turn-{index}",
                "turn_failure_notification": {
                    "failure_id": f"turn:turn-{index}",
                    "delivered": True,
                    "ack_evidence": "receipt",
                    "fallback_run_id": run.id,
                },
            },
        )
        assert sqlite.owed_failure_notice(run.id)["state"] == "pending"
        runs.append(run)

    service = _drain_service(tmp_path, controller, sqlite, requests)

    asyncio.run(service._drain_failure_notices())

    assert [sqlite.owed_failure_notice(run.id)["state"] for run in runs] == [
        "skipped",
        "skipped",
    ]
    assert controller.im_client.sent == [], "the fallback drain repeated a live Turn notification"


def test_legacy_harness_delivery_evidence_suppresses_the_durable_fallback(
    tmp_path: Path,
) -> None:
    """A pre-turn-token Harness receipt remains authoritative delivery evidence."""

    _migrated_state_db()
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    sqlite, requests = _store(tmp_path)
    _task(sqlite, "watch-legacy-delivery", deliver_key="slack::channel::C123")
    run = requests.enqueue_task_run("watch-legacy-delivery")
    assert requests.claim(run.id) is not None
    sqlite.record_run_output(
        run.id,
        output_id="terminal",
        text="",
        terminal_status="failed",
        error="backend failed",
        provenance={
            "turn_failure_notification": {
                "failure_id": run.id,
                "delivered": True,
                "ack_evidence": "delivery_only",
            }
        },
    )

    notice = sqlite.owed_failure_notice(run.id)
    assert notice["turn_id"] is None
    assert notice["turn_notification_delivered"] is True

    service = _drain_service(tmp_path, controller, sqlite, requests)
    asyncio.run(service._drain_failure_notices())

    assert sqlite.owed_failure_notice(run.id)["state"] == "skipped"
    assert controller.im_client.sent == []


def _settle_linked_turn_failures(sqlite, runs, *, delivered: bool) -> None:
    fallback_run_id = min(run.id for run in runs)
    for run in runs:
        sqlite.record_run_output(
            run.id,
            output_id="terminal",
            text="",
            terminal_status="failed",
            error="stream disconnected",
            provenance={
                "turn_id": "turn-shared-failure",
                "turn_failure_notification": {
                    "failure_id": "turn:turn-shared-failure",
                    "delivered": delivered,
                    "ack_evidence": "receipt" if delivered else None,
                    "fallback_run_id": fallback_run_id,
                },
            },
        )


def test_hfr_437_visible_turn_failure_suppresses_all_linked_run_notices(
    tmp_path: Path,
) -> None:
    """One acknowledged backend error is enough, regardless of Run provenance."""

    _migrated_state_db()
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    sqlite, requests = _store(tmp_path)
    _task(sqlite, "watch-source-a", deliver_key="slack::channel::C123")
    _task(sqlite, "watch-source-b", deliver_key="slack::channel::C123")
    runs = [requests.enqueue_task_run(definition) for definition in ("watch-source-a", "watch-source-b")]
    for run in runs:
        assert requests.claim(run.id) is not None
    _settle_linked_turn_failures(sqlite, runs, delivered=True)

    service = _drain_service(tmp_path, controller, sqlite, requests)
    asyncio.run(service._drain_failure_notices())

    assert controller.im_client.sent == []
    assert {sqlite.owed_failure_notice(run.id)["state"] for run in runs} == {"skipped"}
    assert {sqlite.get_run(run.id)["status"] for run in runs} == {"failed"}


def test_hfr_438_missing_turn_notification_delivers_one_fallback(tmp_path: Path) -> None:
    """A lost primary elects one Run fallback, not one fallback per definition."""

    _migrated_state_db()
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    sqlite, requests = _store(tmp_path)
    _task(sqlite, "watch-source-a", deliver_key="slack::channel::C123")
    _task(sqlite, "watch-source-b", deliver_key="slack::channel::C123")
    runs = [requests.enqueue_task_run(definition) for definition in ("watch-source-a", "watch-source-b")]
    for run in runs:
        assert requests.claim(run.id) is not None
    _settle_linked_turn_failures(sqlite, runs, delivered=False)

    service = _drain_service(tmp_path, controller, sqlite, requests)
    asyncio.run(service._drain_failure_notices())

    assert len(controller.im_client.sent) == 1
    notices = {run.id: sqlite.owed_failure_notice(run.id) for run in runs}
    assert notices[min(notices)]["state"] == "sent"
    assert {notice["state"] for run_id, notice in notices.items() if run_id != min(notices)} == {
        "skipped"
    }


def test_hfr_440_one_sibling_callback_suppresses_the_whole_turn_fallback(
    tmp_path: Path,
) -> None:
    """A callback reports the shared Turn result, not only its parent Run."""

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "watch-callback-a", deliver_key="slack::channel::C123")
    _task(sqlite, "watch-callback-b", deliver_key="slack::channel::C123")
    _callback_session(sqlite)
    owner_id = "run-turn-owner"
    callback_parent_id = "run-turn-callback"
    notice = {
        "state": "pending",
        "attempts": 0,
        "next_attempt_at": None,
        "failure_id": "turn:turn-with-callback",
        "turn_id": "turn-with-callback",
        "turn_fallback_run_id": owner_id,
        "turn_participant_run_ids": [owner_id, callback_parent_id],
    }
    _pending_failure(
        sqlite,
        owner_id,
        "watch-callback-a",
        created_at="2026-07-27T00:00:00+00:00",
        notice=notice,
    )
    _callback_run(
        sqlite,
        callback_parent_id,
        "watch-callback-b",
        status="pending",
    )
    sqlite.update_owed_failure_notice(
        callback_parent_id,
        failure_id="turn:turn-with-callback",
        turn_id="turn-with-callback",
        turn_fallback_run_id=owner_id,
        turn_participant_run_ids=[owner_id, callback_parent_id],
    )
    callback = requests.enqueue_agent_run(
        message="deliver the shared Turn result",
        source_kind="callback",
        parent_run_id=callback_parent_id,
        session_id="ses-callback-target",
    )
    sqlite.update_callback_status(
        callback_parent_id,
        status="sent",
        callback_run_id=callback.id,
    )
    assert requests.claim(callback.id) is not None
    sqlite.record_run_output(
        callback.id,
        output_id="terminal",
        text="shared Turn callback delivered",
        terminal_status="succeeded",
    )
    _persist_callback_receipt(sqlite, callback.id, text="shared Turn callback delivered")

    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    import core.scheduled_tasks as scheduled_tasks

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)
        asyncio.run(service._drain_failure_notices())

    assert delivered == []
    for run_id in (owner_id, callback_parent_id):
        stored = sqlite.owed_failure_notice(run_id)
        assert stored["state"] == "skipped"
        assert stored["skip_reason"] == "delivered_by_callback"


def test_hfr_443_deferred_sibling_callback_blocks_the_turn_fallback(
    tmp_path: Path,
) -> None:
    """A deferred participant remains part of the Turn callback snapshot."""

    from sqlalchemy import update as sa_update

    import core.scheduled_tasks as scheduled_tasks
    from storage.models import agent_runs

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "watch-immediate-notice", deliver_key="slack::channel::C123")
    _task(sqlite, "watch-deferred-callback", deliver_key="slack::channel::C123")
    _callback_session(sqlite)
    immediate = requests.enqueue_task_run("watch-immediate-notice")
    deferred = requests.enqueue_task_run("watch-deferred-callback")
    for run in (immediate, deferred):
        assert requests.claim(run.id) is not None
    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs)
            .where(agent_runs.c.id == deferred.id)
            .values(
                callback_session_id="ses-callback-target",
                callback_status="pending",
            )
        )

    turn_id = "turn-deferred-callback"
    sqlite.record_turn_run_outputs(
        [immediate.id, deferred.id],
        output_id="terminal",
        text="",
        provenance={
            "turn_id": turn_id,
            "turn_failure_notification": {
                "failure_id": f"turn:{turn_id}",
                "delivered": False,
                "fallback_run_id": deferred.id,
            },
        },
        terminal_status="failed",
        error="stream disconnected",
        deferred_run_ids=[deferred.id],
    )

    assert sqlite.get_run(deferred.id)["status"] == "running"
    assert (
        sqlite.turn_callback_state(
            turn_id,
            participant_run_ids=[immediate.id, deferred.id],
        )
        == "pending"
    )
    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)
        asyncio.run(service._drain_failure_notices())

    assert delivered == []
    assert sqlite.owed_failure_notice(immediate.id)["state"] == "pending"


def test_hfr_448_cancel_requested_callback_does_not_block_turn_fallback(
    tmp_path: Path,
) -> None:
    """A stopped deferred participant no longer owns callback membership."""

    from sqlalchemy import update as sa_update

    import core.scheduled_tasks as scheduled_tasks
    from storage.models import agent_runs

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "watch-cancel-callback-owner", deliver_key="slack::channel::C123")
    _task(sqlite, "watch-cancel-callback-deferred", deliver_key="slack::channel::C123")
    _callback_session(sqlite)
    immediate = requests.enqueue_task_run("watch-cancel-callback-owner")
    deferred = requests.enqueue_task_run("watch-cancel-callback-deferred")
    for run in (immediate, deferred):
        assert requests.claim(run.id) is not None
    with sqlite.engine.begin() as conn:
        conn.execute(
            sa_update(agent_runs)
            .where(agent_runs.c.id == deferred.id)
            .values(
                callback_session_id="ses-callback-target",
                callback_status="pending",
            )
        )

    turn_id = "turn-cancel-requested-callback"
    sqlite.record_turn_run_outputs(
        [immediate.id, deferred.id],
        output_id="terminal",
        text="",
        provenance={
            "turn_id": turn_id,
            "turn_failure_notification": {
                "failure_id": f"turn:{turn_id}",
                "delivered": False,
                "fallback_run_id": immediate.id,
            },
        },
        terminal_status="failed",
        error="stream disconnected",
        deferred_run_ids=[deferred.id],
    )

    assert sqlite.cancel_run(deferred.id)
    assert sqlite.get_run(deferred.id)["cancel_requested"] is True
    assert (
        sqlite.turn_callback_state(
            turn_id,
            participant_run_ids=[immediate.id, deferred.id],
        )
        is None
    )
    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)
        asyncio.run(service._drain_failure_notices())

    assert delivered == [f"turn:{turn_id}"]
    assert sqlite.owed_failure_notice(immediate.id)["state"] == "sent"


def test_hfr_449_canceled_parent_keeps_armed_callback_evidence(
    tmp_path: Path,
) -> None:
    """Cancellation removes unarmed parents, not callback work already accepted."""

    sqlite, requests = _store(tmp_path)
    _callback_session(sqlite)

    def _parent_with_callback(turn_id: str, *, delivered: bool) -> tuple[Any, Any]:
        from sqlalchemy import update as sa_update

        from storage.models import agent_runs

        definition_id = f"watch-{turn_id}"
        _task(sqlite, definition_id)
        parent = requests.enqueue_task_run(definition_id)
        assert requests.claim(parent.id) is not None
        sqlite.record_turn_run_outputs(
            [parent.id],
            output_id="terminal",
            text="",
            provenance={
                "turn_id": turn_id,
                "turn_failure_notification": {
                    "failure_id": f"turn:{turn_id}",
                    "delivered": False,
                    "fallback_run_id": parent.id,
                },
            },
            terminal_status="failed",
            error="stream disconnected",
        )
        callback = requests.enqueue_agent_run(
            message="deliver the Turn result",
            source_kind="callback",
            parent_run_id=parent.id,
            session_id="ses-callback-target",
        )
        with sqlite.engine.begin() as conn:
            conn.execute(
                sa_update(agent_runs)
                .where(agent_runs.c.id == parent.id)
                .values(callback_session_id="ses-callback-target")
            )
        sqlite.update_callback_status(
            parent.id,
            status="sent",
            callback_run_id=callback.id,
        )
        if delivered:
            assert requests.claim(callback.id) is not None
            sqlite.record_run_output(
                callback.id,
                output_id="terminal",
                text="callback delivered",
                terminal_status="succeeded",
            )
            _persist_callback_receipt(sqlite, callback.id, text="callback delivered")
        assert sqlite.cancel_run(parent.id)
        return parent, callback

    pending_parent, _pending_callback = _parent_with_callback(
        "turn-canceled-pending-callback", delivered=False
    )
    delivered_parent, _delivered_callback = _parent_with_callback(
        "turn-canceled-delivered-callback", delivered=True
    )

    assert (
        sqlite.turn_callback_state(
            "turn-canceled-pending-callback",
            participant_run_ids=[pending_parent.id],
        )
        == "pending"
    )
    assert (
        sqlite.turn_callback_state(
            "turn-canceled-delivered-callback",
            participant_run_ids=[delivered_parent.id],
        )
        == "sent"
    )


def test_hfr_450_terminal_replay_promotes_new_turn_delivery_evidence(
    tmp_path: Path,
) -> None:
    """A same-Turn replay upgrades its notice without resetting drain progress."""

    import core.scheduled_tasks as scheduled_tasks

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "watch-delivery-replay")
    run = requests.enqueue_task_run("watch-delivery-replay")
    assert requests.claim(run.id) is not None
    turn_id = "turn-delivery-replay"
    provenance = {
        "turn_id": turn_id,
        "turn_failure_notification": {
            "failure_id": f"turn:{turn_id}",
            "delivered": False,
            "fallback_run_id": run.id,
        },
    }
    sqlite.record_turn_run_outputs(
        [run.id],
        output_id="terminal",
        text="",
        provenance=provenance,
        terminal_status="failed",
        error="stream disconnected",
    )
    sqlite.update_owed_failure_notice(run.id, attempts=2)

    replay = sqlite.record_run_output(
        run.id,
        output_id="terminal",
        text="",
        provenance={
            "turn_id": turn_id,
            "turn_failure_notification": {
                "failure_id": f"turn:{turn_id}",
                "delivered": True,
                "ack_evidence": "delivery_only",
                "fallback_run_id": run.id,
            },
        },
        terminal_status="failed",
        error="stream disconnected",
    )

    notice = sqlite.owed_failure_notice(run.id)
    assert replay["recorded"] is False
    assert replay["terminal_transition"] is False
    assert replay["delivery_evidence_merged"] is True
    assert notice["attempts"] == 2
    assert notice["turn_notification_delivered"] is True
    assert notice["turn_notification_ack_evidence"] == "delivery_only"

    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)
        asyncio.run(service._drain_failure_notices())
    assert delivered == []
    assert sqlite.owed_failure_notice(run.id)["state"] == "skipped"


def test_hfr_451_turn_callback_lookup_uses_bounded_ownership(
    tmp_path: Path,
) -> None:
    """Turn callback reads use Run ids and indexed Delivery ownership, never JSON scans."""

    sqlite, _requests = _store(tmp_path)
    _pending_failure(
        sqlite,
        "run-bounded-callback",
        "watch-bounded-callback",
        created_at="2026-07-27T00:00:00+00:00",
        notice={
            "state": "pending",
            "attempts": 0,
            "next_attempt_at": None,
            "failure_id": "turn:turn-bounded-callback",
            "turn_id": "turn-bounded-callback",
            "turn_fallback_run_id": "run-bounded-callback",
            "turn_participant_run_ids": ["run-bounded-callback"],
        },
    )
    statements: list[tuple[str, Any]] = []

    def _capture_statement(conn, cursor, statement, parameters, context, executemany):
        if "callback_parent" in statement:
            statements.append((statement, parameters))

    event.listen(sqlite.engine, "before_cursor_execute", _capture_statement)
    try:
        sqlite.turn_callback_state(
            "turn-bounded-callback",
            participant_run_ids=["run-bounded-callback"],
        )
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _capture_statement)

    assert len(statements) == 1
    statement, parameters = statements[0]
    normalized_statement = statement.upper()
    assert "CALLBACK_PARENT.METADATA_JSON" not in normalized_statement
    assert "CALLBACK_PARENT.RESULT_PAYLOAD_JSON" not in normalized_statement
    assert "MESSAGE_DELIVERIES.TURN_ID" in normalized_statement
    assert "CALLBACK_PARENT.ID IN" in normalized_statement
    with sqlite.engine.connect() as conn:
        plan = conn.exec_driver_sql(
            f"EXPLAIN QUERY PLAN {statement}",
            parameters,
        ).all()
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "message_deliveries_turn" in plan_text
    assert "sqlite_autoindex_agent_runs_1" in plan_text


def test_hfr_452_callback_children_cannot_own_a_turn_fallback(
    tmp_path: Path,
) -> None:
    """Owner election applies every suppression used by the notice writer."""

    sqlite, requests = _store(tmp_path)
    parent = requests.enqueue(
        TaskExecutionRequest(
            id="run-callback-parent",
            request_type="agent_run",
            message="original callback parent",
        )
    )
    assert requests.claim(parent.id) is not None
    sqlite.record_run_output(
        parent.id,
        output_id="parent-terminal",
        text="",
        terminal_status="failed",
        error="parent failed",
    )
    assert sqlite.owed_failure_notice(parent.id) is not None

    callback_child = requests.enqueue(
        TaskExecutionRequest(
            id="run-a-callback-child",
            request_type="agent_run",
            message="callback child",
            source_kind="callback",
            parent_run_id=parent.id,
        )
    )
    sibling = requests.enqueue(
        TaskExecutionRequest(
            id="run-b-fallback-owner",
            request_type="agent_run",
            message="ordinary sibling",
        )
    )
    assert requests.claim(callback_child.id) is not None
    assert requests.claim(sibling.id) is not None
    turn_id = "turn-callback-child-owner"

    sqlite.record_turn_run_outputs(
        [callback_child.id, sibling.id],
        output_id="turn-terminal",
        text="",
        provenance={
            "turn_id": turn_id,
            "turn_failure_notification": {
                "failure_id": f"turn:{turn_id}",
                "delivered": False,
                "fallback_run_id": callback_child.id,
            },
        },
        terminal_status="failed",
        error="stream disconnected",
    )

    assert sqlite.owed_failure_notice(callback_child.id) is None
    assert (
        sqlite.owed_failure_notice(sibling.id)["turn_fallback_run_id"]
        == sibling.id
    )


def test_hfr_453_callback_enqueue_arms_the_parent_in_one_transaction(
    tmp_path: Path,
) -> None:
    """A durable callback child is never visible without its parent marker."""

    sqlite, requests = _store(tmp_path)
    parent = requests.enqueue(
        TaskExecutionRequest(
            id="run-atomic-callback-parent",
            request_type="agent_run",
            message="parent",
            callback_session_id="ses-callback-target",
            callback_status="pending",
        )
    )
    statements: list[str] = []
    commits: list[int] = []

    def _capture_statement(conn, cursor, statement, parameters, context, executemany):
        if "agent_runs" in statement:
            statements.append(statement.upper())

    def _capture_commit(conn):
        commits.append(1)

    event.listen(sqlite.engine, "before_cursor_execute", _capture_statement)
    event.listen(sqlite.engine, "commit", _capture_commit)
    try:
        callback = requests.enqueue_agent_run(
            message="deliver callback",
            source_kind="callback",
            source_actor="run-atomic-callback-parent:terminal:failed",
            parent_run_id=parent.id,
            callback_parent_to_arm=parent.id,
        )
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _capture_statement)
        event.remove(sqlite.engine, "commit", _capture_commit)

    stored_parent = sqlite.get_run(parent.id)
    assert stored_parent["callback_status"] == "sent"
    assert stored_parent["callback_run_id"] == callback.id
    assert sqlite.get_run(callback.id)["parent_run_id"] == parent.id
    assert commits == [1]
    insert_position = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("INSERT INTO AGENT_RUNS")
    )
    parent_update_position = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("UPDATE AGENT_RUNS")
        and "CALLBACK_RUN_ID" in statement
    )
    assert insert_position < parent_update_position


def test_hfr_455_turn_owner_excludes_same_batch_parent_suppression(
    tmp_path: Path,
) -> None:
    """A child cannot own when its earlier parent acquires a notice in the batch."""

    sqlite, requests = _store(tmp_path)
    parent = requests.enqueue(
        TaskExecutionRequest(
            id="run-b-callback-parent",
            request_type="agent_run",
            message="parent",
        )
    )
    callback_child = requests.enqueue(
        TaskExecutionRequest(
            id="run-a-callback-child",
            request_type="agent_run",
            message="callback child",
            source_kind="callback",
            parent_run_id=parent.id,
        )
    )
    assert requests.claim(parent.id) is not None
    assert requests.claim(callback_child.id) is not None
    turn_id = "turn-parent-before-callback-child"

    sqlite.record_turn_run_outputs(
        [parent.id, callback_child.id],
        output_id="turn-terminal",
        text="",
        provenance={
            "turn_id": turn_id,
            "turn_failure_notification": {
                "failure_id": f"turn:{turn_id}",
                "delivered": False,
                "fallback_run_id": callback_child.id,
            },
        },
        terminal_status="failed",
        error="stream disconnected",
    )

    parent_notice = sqlite.owed_failure_notice(parent.id)
    assert parent_notice["turn_fallback_run_id"] == parent.id
    assert sqlite.owed_failure_notice(callback_child.id) is None


def test_hfr_456_late_canceled_failed_parent_rejects_only_new_callbacks(
    tmp_path: Path,
) -> None:
    """Late cancellation blocks new failure callbacks but preserves accepted evidence."""

    sqlite, requests = _store(tmp_path)

    def failed_parent(run_id: str) -> TaskExecutionRequest:
        parent = requests.enqueue(
            TaskExecutionRequest(
                id=run_id,
                request_type="agent_run",
                message="parent",
                callback_session_id="ses-callback-target",
                callback_status="pending",
            )
        )
        assert requests.claim(parent.id) is not None
        sqlite.record_run_output(
            parent.id,
            output_id=f"{run_id}-terminal",
            text="",
            terminal_status="failed",
            error="parent failed",
        )
        assert sqlite.cancel_run(parent.id)
        return parent

    rejected_parent = failed_parent("run-canceled-callback-parent")
    rejected_actor = f"{rejected_parent.id}:terminal:failed"
    rejected = requests.enqueue_agent_run(
        message="new callback",
        source_kind="callback",
        source_actor=rejected_actor,
        parent_run_id=rejected_parent.id,
        callback_parent_to_arm=rejected_parent.id,
    )
    assert rejected is None
    assert (
        requests.find_callback_run(
            parent_run_id=rejected_parent.id,
            source_actor=rejected_actor,
        )
        is None
    )

    armed_parent = failed_parent("run-armed-callback-parent")
    armed_actor = f"{armed_parent.id}:terminal:failed"
    existing = requests.enqueue(
        TaskExecutionRequest(
            id="run-existing-callback-child",
            request_type="agent_run",
            message="accepted callback",
            source_kind="callback",
            source_actor=armed_actor,
            parent_run_id=armed_parent.id,
        )
    )
    recovered = requests.enqueue_agent_run(
        message="accepted callback",
        source_kind="callback",
        source_actor=armed_actor,
        parent_run_id=armed_parent.id,
        callback_parent_to_arm=armed_parent.id,
    )
    assert recovered is not None
    assert recovered.id == existing.id
    stored_parent = sqlite.get_run(armed_parent.id)
    assert stored_parent["callback_status"] == "sent"
    assert stored_parent["callback_run_id"] == existing.id


def test_hfr_457_missing_turn_participant_does_not_rollback_valid_runs(
    tmp_path: Path,
) -> None:
    """A stale attribution id is excluded before ownership and settlement."""

    sqlite, requests = _store(tmp_path)
    run = requests.enqueue(
        TaskExecutionRequest(
            id="run-valid-turn-participant",
            request_type="agent_run",
            message="valid participant",
        )
    )
    assert requests.claim(run.id) is not None
    turn_id = "turn-with-stale-participant"

    results = sqlite.record_turn_run_outputs(
        ["run-missing-turn-participant", run.id],
        output_id="turn-terminal",
        text="",
        provenance={
            "turn_id": turn_id,
            "turn_failure_notification": {
                "failure_id": f"turn:{turn_id}",
                "delivered": False,
                "fallback_run_id": "run-missing-turn-participant",
            },
        },
        terminal_status="failed",
        error="stream disconnected",
    )

    assert list(results) == [run.id]
    assert sqlite.get_run(run.id)["status"] == "failed"
    notice = sqlite.owed_failure_notice(run.id)
    assert notice["turn_fallback_run_id"] == run.id
    assert notice["turn_participant_run_ids"] == [run.id]


def test_hfr_458_steered_callback_uses_its_shared_turn_output_receipt(
    tmp_path: Path,
) -> None:
    """A callback need not be the primary Run named by its persisted result."""

    from storage import messages_service

    sqlite, requests = _store(tmp_path)
    _callback_session(sqlite)
    parent = requests.enqueue(
        TaskExecutionRequest(
            id="run-shared-receipt-parent",
            request_type="agent_run",
            message="parent",
            callback_session_id="ses-callback-target",
        )
    )
    callback = requests.enqueue(
        TaskExecutionRequest(
            id="run-shared-receipt-callback",
            request_type="agent_run",
            message="callback",
            source_kind="callback",
            parent_run_id=parent.id,
            session_id="ses-callback-target",
        )
    )
    for run in (parent, callback):
        assert requests.claim(run.id) is not None
    sqlite.update_callback_status(
        parent.id,
        status="sent",
        callback_run_id=callback.id,
    )
    turn_id = "turn-steered-callback-receipt"
    output_id = "steered-terminal-output"
    sqlite.record_turn_run_outputs(
        [callback.id],
        output_id=output_id,
        text="callback delivered",
        provenance={"turn_id": turn_id, "run_id": "run-primary-human-turn"},
        terminal_status="succeeded",
    )
    with sqlite.engine.begin() as conn:
        messages_service.append(
            conn,
            scope_id=None,
            session_id="ses-callback-target",
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="callback delivered",
            metadata={
                "turn_id": turn_id,
                "run_id": "run-primary-human-turn",
                "output_id": output_id,
            },
        )

    assert sqlite.run_callback_state(parent.id) == "sent"


@pytest.mark.parametrize("turn_notification", [None, {}], ids=["absent", "empty"])
def test_hfr_442_bare_turn_provenance_does_not_enter_the_fallback_lane(
    tmp_path: Path,
    turn_notification: dict[str, Any] | None,
) -> None:
    """A direct error result has a Turn id but no notification ownership contract."""

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-direct-error", deliver_key="slack::channel::C123")
    runs = [requests.enqueue_task_run("task-direct-error") for _ in range(2)]
    for run in runs:
        assert requests.claim(run.id) is not None
    provenance: dict[str, Any] = {"turn_id": "turn-direct-error"}
    if turn_notification is not None:
        provenance["turn_failure_notification"] = turn_notification
    sqlite.record_turn_run_outputs(
        [run.id for run in runs],
        output_id="terminal",
        text="question tool is disabled",
        terminal_status="failed",
        error="question tool is disabled",
        provenance=provenance,
    )

    notices = [sqlite.owed_failure_notice(run.id) for run in runs]
    assert all(notice["turn_id"] is None for notice in notices)
    assert all(notice["turn_fallback_run_id"] is None for notice in notices)


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
    history = []
    for index in range(total):
        # Successes every seventh run, so the streak containing any given failure is
        # at most six rows long while the lifetime is ``total``.
        status = "succeeded" if ever_succeeded and index % 7 == 0 else "failed"
        instant = f"2026-07-01T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}+00:00"
        history.append(
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
    _seed_query_history(sqlite_store, history)


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
    from datetime import datetime, timedelta, timezone

    from storage.background import SQLiteBackgroundTaskStore
    from storage.pagination import PageRequest
    from vibe import cli

    # Seeded RELATIVE to the wall clock, because the CLI path computes health
    # from ``datetime.now`` — a fixed calendar date here was a time bomb that
    # started aging runs out of the 72 h window three days after it was written.
    def _instant(hours_ago: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()

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
                    "created_at": _instant(8 - index),
                    "completed_at": _instant(7.5 - index),
                }
            )
        store.enqueue_run(
            {
                "id": "run-cli-ok",
                "request_type": "scheduled",
                "status": "succeeded",
                "definition_id": "task-cli",
                "created_at": _instant(4),
                "completed_at": _instant(3.5),
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
                "last_run_at": _instant(3.5),
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


def test_a_watch_reports_processing_health_separately_from_waiter_health(capsys) -> None:
    """HFR-090 — a failed hook stays visible without blaming the waiter.

    ``_enrich_definitions`` computes health for both definition types, so the only
    thing that decided whether it reached a surface was the projection allowlist.
    """

    import json
    from datetime import datetime, timedelta, timezone

    from storage.background import SQLiteBackgroundTaskStore
    from storage.pagination import PageRequest
    from vibe import cli

    # Seeded RELATIVE to the wall clock, for the same reason HFR-062's sibling was
    # (``c57ce4b8``): the CLI path computes health from ``datetime.now``, so a fixed
    # calendar date here was a time bomb that started aging the run out of the 72 h
    # window three days after it was written. This one was missed by that commit and
    # went red on 2026-07-30 — the health it asserts is ``failing`` read ``healthy``
    # once the seeded failure fell outside the window.
    def _instant(hours_ago: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()

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
                "created_at": _instant(2),
                "completed_at": _instant(1.98),
            }
        )
        assert cli.cmd_watch_list(page_request=PageRequest(limit=20)) == 0
    finally:
        store.close()

    entry = json.loads(capsys.readouterr().out)["definitions"][0]
    assert entry["health"] == "unknown", "a never-run waiter has no success evidence"
    assert entry["consecutive_failures"] == 0
    assert entry["processing_health"] == "failing"
    assert entry["processing_consecutive_failures"] == 1


def test_hfr_444_first_in_flight_waiter_health_remains_unknown(tmp_path: Path) -> None:
    """Starting a cycle is not evidence that its waiter succeeded."""

    sqlite, _requests = _store(tmp_path)
    _watch(
        sqlite,
        "watch-first-cycle",
        last_started_at="2026-08-08T05:00:00+00:00",
        last_finished_at=None,
        last_exit_code=None,
        last_error=None,
    )

    projected = sqlite.get_watch("watch-first-cycle")

    assert projected is not None
    assert projected["health"] == "unknown"
    assert projected["consecutive_failures"] == 0
    assert projected["recent_failures"] == 0


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

    def _break(run_id: str, payload: str = "{broken") -> None:
        with sqlite.engine.begin() as conn:
            conn.execute(
                sa_update(agent_runs).where(agent_runs.c.id == run_id).values(metadata_json=payload)
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

    # 4. VALID JSON that is not an OBJECT is just as unreadable as malformed JSON:
    #    ``json_valid('[]')`` is 1, so a validity-only readability signal admits the
    #    row, ``INTERRUPT_REASON_SQL``'s extract degrades to NULL (passing
    #    ``reason IS NULL``), and the badge answers confidently off a metadata
    #    schema that cannot be read. Every non-object type SQLite's ``json_type``
    #    can report at the top level is covered.
    for suffix, payload in (
        ("array", "[]"),
        ("text", '"value"'),
        ("number", "3"),
        ("null", "null"),
        ("boolean", "true"),
    ):
        _task(sqlite, f"task-{suffix}-bad")
        _run(f"task-{suffix}-bad", 0, "succeeded", 1)
        _break(_run(f"task-{suffix}-bad", 1, "failed", 2), payload)

    non_object = tuple(
        f"task-{suffix}-bad" for suffix in ("array", "text", "number", "null", "boolean")
    )
    healths = sqlite.definition_health_batch(
        ["task-newest-bad", "task-inner-bad", "task-aged-bad", *non_object],
        now="2026-07-29T00:00:00+00:00",
    )

    for definition_id in ("task-newest-bad", "task-inner-bad", *non_object):
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


def test_binding_marker_rolls_back_when_its_notice_write_faults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A transient notice fault must leave the transition retryable."""

    from core.scheduled_tasks import BINDING_RECOVERY_METADATA_KEY, SessionBindingChange
    from tests.test_scheduled_tasks import _binding_env

    _binding_env(tmp_path, monkeypatch)
    sqlite, requests = _store(tmp_path)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    service = _drain_service(tmp_path, controller, sqlite, requests)
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
    )
    run = requests.enqueue_task_run(task.id, source_kind="scheduler", task=task)
    assert requests.claim(run.id) is not None
    change = SessionBindingChange(
        action="rebound",
        task_id=task.id,
        reason="session_missing",
        previous_session_id="ses-gone",
        detail="the pinned session was replaced",
        new_session_id="ses-fresh",
        settings_preserved=True,
    )

    real_stamp = sqlite.stamp_binding_change_notice

    def _fault(*args, **kwargs):
        raise OSError("transient sqlite write fault")

    monkeypatch.setattr(sqlite, "stamp_binding_change_notice", _fault)
    asyncio.run(service._emit_binding_change(change, run_id=run.id, run_error=None))

    after_fault = service.store.get_task(task.id)
    assert after_fault is not None
    assert BINDING_RECOVERY_METADATA_KEY not in (after_fault.metadata or {})
    assert sqlite.owed_failure_notice(run.id) is None

    monkeypatch.setattr(sqlite, "stamp_binding_change_notice", real_stamp)
    asyncio.run(service._emit_binding_change(change, run_id=run.id, run_error=None))

    recovered = service.store.get_task(task.id)
    marker = (recovered.metadata or {}).get(BINDING_RECOVERY_METADATA_KEY)
    assert isinstance(marker, dict) and marker["signature"] == change.signature
    notice = sqlite.owed_failure_notice(run.id)
    assert notice is not None
    assert notice["failure_id"] == f"binding:{task.id}:{change.signature}"


def test_binding_notice_rolls_back_when_the_definition_marker_cas_loses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A refused marker must not commit the notice written earlier in its transaction."""

    from sqlalchemy import update as sa_update

    from core.scheduled_tasks import BINDING_RECOVERY_METADATA_KEY, SessionBindingChange
    from storage.models import run_definitions
    from tests.test_scheduled_tasks import _binding_env

    _binding_env(tmp_path, monkeypatch)
    sqlite, requests = _store(tmp_path)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    service = _drain_service(tmp_path, controller, sqlite, requests)
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
    )
    run = requests.enqueue_task_run(task.id, source_kind="scheduler", task=task)
    assert requests.claim(run.id) is not None
    change = SessionBindingChange(
        action="rebound",
        task_id=task.id,
        reason="session_missing",
        previous_session_id="ses-gone",
        detail="the pinned session was replaced",
        new_session_id="ses-fresh",
        settings_preserved=True,
    )

    real_atomic_write = sqlite.upsert_scheduled_task_with_binding_notice

    def _repoint_before_atomic_write(payload, *, expect, notice):
        with sqlite.engine.begin() as conn:
            conn.execute(
                sa_update(run_definitions)
                .where(run_definitions.c.id == task.id)
                .values(session_id="ses-concurrently-repointed")
            )
        return real_atomic_write(payload, expect=expect, notice=notice)

    monkeypatch.setattr(
        sqlite,
        "upsert_scheduled_task_with_binding_notice",
        _repoint_before_atomic_write,
    )
    asyncio.run(service._emit_binding_change(change, run_id=run.id, run_error=None))

    stored = service.store.get_task(task.id)
    assert stored is not None and stored.session_id == "ses-concurrently-repointed"
    assert BINDING_RECOVERY_METADATA_KEY not in (stored.metadata or {})
    assert sqlite.owed_failure_notice(run.id) is None


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
    interleaved: list[str] = []

    def _cancel_inside_the_gap(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if interleaved or not statement.lstrip().upper().startswith("UPDATE AGENT_RUNS"):
            return
        interleaved.append(statement)
        # Stop lands after the stamp's snapshot. It first sets cancel_requested, then
        # the terminal owner maps the failed result to canceled before the stale stamp
        # reaches its UPDATE.
        assert sqlite.cancel_run(run.id)
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


def test_a_binding_notice_committed_before_stop_remains_deliverable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A later Stop cannot strand news about a rebind that already committed."""

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-binding-cancel-after-stamp", deliver_key="slack::channel::C1")
    run = requests.enqueue_task_run("task-binding-cancel-after-stamp")
    claimed = requests.claim(run.id)
    assert claimed is not None

    stamped = _binding_stamp(sqlite, run.id, task_id="task-binding-cancel-after-stamp")
    assert stamped is not None
    assert sqlite.cancel_run(run.id)
    requests.complete(
        claimed,
        ok=False,
        error="stopped",
        task_id="task-binding-cancel-after-stamp",
    )

    assert sqlite.get_run(run.id)["status"] == "canceled"
    notice = sqlite.owed_failure_notice(run.id)
    assert notice is not None and notice["kind"] == NOTICE_KIND_BINDING_CHANGE
    assert notice["state"] == NOTICE_PENDING
    assert [row["id"] for row in sqlite.list_owed_failure_notices()] == [run.id]

    import core.scheduled_tasks as scheduled_tasks

    service, delivered = _notice_drain_service(tmp_path, sqlite, requests)
    monkeypatch.setattr(scheduled_tasks, "emit_replayed_backend_failure", service._spy_emit)
    asyncio.run(service._drain_failure_notices())

    assert delivered == [notice["failure_id"]]
    assert sqlite.owed_failure_notice(run.id)["state"] == NOTICE_SENT
    assert sqlite.list_owed_failure_notices() == []


def test_a_binding_stamp_refuses_a_stop_already_visible_in_its_snapshot(tmp_path: Path) -> None:
    """Both Stop shapes outrank binding news before the first CAS.

    A queued Stop writes ``status='canceled'`` immediately; a running Stop leaves the
    status running and writes ``cancel_requested`` for the terminal owner. Either row
    would make a binding notice permanently unreachable after cancellation settles.
    """

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-binding-stopped")

    queued = requests.enqueue_task_run("task-binding-stopped")
    assert sqlite.cancel_run(queued.id)
    assert sqlite.get_run(queued.id)["status"] == "canceled"

    running = requests.enqueue_task_run("task-binding-stopped")
    assert requests.claim(running.id) is not None
    assert sqlite.cancel_run(running.id)
    running_row = sqlite.get_run(running.id)
    assert running_row["status"] == "running"
    assert running_row["cancel_requested"] is True

    for run in (queued, running):
        assert _binding_stamp(sqlite, run.id, task_id="task-binding-stopped") is None
        assert sqlite.owed_failure_notice(run.id) is None


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


# =============================================================================
# round 12: drain semantics and interruption copy
# =============================================================================
#
# Four defects and one consuming-path pin, all in the owed-notice drain's own
# control flow rather than in the store:
#
# * T-1 (G) a rung that RAISES must not kill the ladder walk;
# * T-2 (L) one pass may not hold the whole backlog for batch x timeout;
# * T-3 (E) an interruption reason is a wire value, not user-visible copy;
# * T-4 (F) a nonnumeric ``attempts`` must degrade, not wedge the batch;
# * T-5 (B) the consuming-path pin for the adapter bookkeeping guard.
#
# Appended as one block on purpose: several rounds run in parallel worktrees and
# a single trailing section is the cheapest thing to cherry-pick.


def _ladder_task(
    sqlite,
    definition_id: str,
    *,
    first: str,
    second: str,
) -> None:
    """A definition whose ladder has exactly TWO rungs, in a known order.

    Rung (1) is the delivery key and rung (3) is caller provenance; the caller
    carries a ``session_key`` and NOTHING else, because a ``platform`` +
    ``user_id`` pair would add rung (4) and make "the walk continued" ambiguous
    about which later rung it reached.
    """

    _task(
        sqlite,
        definition_id,
        name="daily report",
        deliver_key=first,
        metadata={"created_by": {"caller": {"session_key": second}}},
    )


def test_a_raising_ladder_rung_does_not_abandon_the_rest_of_the_walk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-079, subordinate — a rung that raises is an UNUSABLE rung, not a dead walk.

    The per-rung ``try/except`` covered ``_build_context`` alone. The
    ``emit_replayed_backend_failure`` call one line below it was unwrapped, so any
    raise from a rung's delivery — a platform whose settings manager is gone, a
    client lookup that fails, an adapter that throws before the transport — escaped
    the ``for`` body entirely, unwound through the walk's ``finally`` and landed in
    ``_deliver_one_failure_notice``'s handler, which consumed the attempt and armed
    the backoff. The next pass then started the SAME ladder from rung (1) and raised
    again, so the notice burned every attempt on one broken rung and dead-lettered
    without ever trying rung (2) — even though the walk already CONTINUES for a rung
    whose send is un-acked (``_rung_acknowledges`` returning False), which is the
    weaker version of the same condition.

    Round 7's accepted contract is that an unresolvable or stale candidate continues
    the walk. A raising one is the same class of unusable and must be treated the
    same way.

    The cost, stated rather than hidden: a rung that raises AFTER its transport
    accepted the send, without leaving evidence, now duplicates onto the next rung.
    That is exactly the class finding B's adapter guard (0ebf4c55, shipped in this
    same round) closes at the source — an already-delivered id is no longer destroyed
    by post-send bookkeeping — and what remains is covered by the duplicate
    short-circuit on the retry plus the at-least-once residual documented on
    ``CLAIM_LEASE_SECONDS``.
    """

    import core.scheduled_tasks as scheduled_tasks
    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT

    sqlite, requests = _store(tmp_path)
    _ladder_task(
        sqlite,
        "task-raising-rung",
        first="slack::channel::C-FIRST",
        second="slack::channel::C-SECOND",
    )
    run = requests.enqueue_task_run("task-raising-rung")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-raising-rung")

    attempted: list[str] = []

    async def _first_rung_raises(controller, context, backend, diagnostic, **kwargs):
        attempted.append(context.channel_id)
        if context.channel_id == "C-FIRST":
            raise RuntimeError("no settings manager for slack")
        evidence = kwargs.get("delivery")
        if evidence is not None:
            evidence.send_returned = True
            evidence.delivered_id = "ts-second"
            evidence.persisted_row = {"id": "msg-second"}
        return False

    monkeypatch.setattr(scheduled_tasks, "emit_replayed_backend_failure", _first_rung_raises)

    from types import SimpleNamespace

    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)
    asyncio.run(service._drain_failure_notices())

    assert attempted == ["C-FIRST", "C-SECOND"], (
        "a raising rung must be skipped, not end the walk; attempted=" f"{attempted}"
    )
    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == NOTICE_SENT, f"the second rung delivered it: {notice}"
    assert notice["ack_evidence"] == ACK_EVIDENCE_RECEIPT
    assert notice["attempts"] == 1, "one pass, one attempt"
    # The skipped rung is recorded rather than swallowed: the aggregate evidence
    # carries the first raise so an operator can see WHY rung (1) was passed over,
    # on a row that is nonetheless acknowledged and will never be resent.
    assert "no settings manager for slack" in (notice["error"] or ""), (
        f"the skipped rung's own error must survive for diagnosis: {notice}"
    )


def test_the_walk_deadline_still_cancels_through_a_rung(tmp_path: Path, monkeypatch) -> None:
    """HFR-079, subordinate — the per-rung handler may not eat ``CancelledError``.

    The fix above catches ``Exception``, deliberately and not ``BaseException``: the
    walk-level deadline (``NOTICE_DELIVERY_TIMEOUT_SECONDS``) cancels the delivery
    task, and cancellation has to unwind the whole walk rather than being converted
    into "this rung was unusable, try the next one". A handler one letter wider would
    turn one wedged rung into a walk that keeps going past its own deadline, holding
    the claim beyond the lease it was cancelled to respect.

    Rung (2) never being attempted is the assertion: it is what distinguishes
    cancellation from the raise above.
    """

    import core.failure_notices as failure_notices
    import core.scheduled_tasks as scheduled_tasks

    sqlite, requests = _store(tmp_path)
    _ladder_task(
        sqlite,
        "task-cancel-rung",
        first="slack::channel::C-HANG",
        second="slack::channel::C-NEVER",
    )
    run = requests.enqueue_task_run("task-cancel-rung")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-cancel-rung")

    attempted: list[str] = []

    async def _first_rung_hangs(controller, context, backend, diagnostic, **kwargs):
        attempted.append(context.channel_id)
        await asyncio.Event().wait()
        return False  # pragma: no cover - the wait never returns

    monkeypatch.setattr(scheduled_tasks, "emit_replayed_backend_failure", _first_rung_hangs)
    monkeypatch.setattr(failure_notices, "NOTICE_DELIVERY_TIMEOUT_SECONDS", 0.05)

    from types import SimpleNamespace

    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)
    asyncio.run(service._drain_failure_notices())

    assert attempted == ["C-HANG"], (
        "the deadline must unwind the whole walk, not advance it to the next rung: "
        f"{attempted}"
    )
    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == "pending", f"a timed-out delivery stays retryable: {notice}"
    assert notice["attempts"] == 1, "the claim's attempt is consumed by the timeout"
    assert "timed out" in (notice["error"] or ""), f"the timeout must say so: {notice}"


def test_one_pass_stops_at_its_fairness_budget_instead_of_holding_the_backlog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-076, subordinate — a pass is bounded by a budget, not by batch x timeout.

    What is NOT the defect, refuted first because the obvious reading is wrong:
    a wedged batch does not re-select itself forever. ``list_owed_failure_notices``
    orders by ``next_attempt_at ASC`` (least-recently-deferred first), so a notice
    stamped while ten rows were being delivered sorts AHEAD of the fresh backoff
    instants those ten just armed and is picked up on the next pass.

    What IS real is the SERIAL cost. One pass delivers its batch one notice at a
    time — single-flight, accepted in round 9, because row #2 only sees row #1's
    ``sent`` when the passes are ordered — and each delivery may legitimately sit on
    ``NOTICE_DELIVERY_TIMEOUT_SECONDS``. Ten wedged rows is ten times that before the
    pass even returns, and a freshly stamped notice waits behind all of it. The cure
    is NOT concurrency: parallel deliveries reopen the same-streak duplicate window
    that serialization closes.

    So the pass takes a fairness budget instead, checked BETWEEN notices and never
    against a delivery already in flight: once it is spent the pass logs what is left
    and stops pulling work. Untouched rows consumed no attempt, kept their backoff,
    and stay eligible — and the ASC ordering already puts the oldest of them first
    next pass.
    """

    import core.failure_notices as failure_notices
    import core.scheduled_tasks as scheduled_tasks

    sqlite, requests = _store(tmp_path)
    for index in range(3):
        definition_id = f"task-budget-{index}"
        _task(sqlite, definition_id, deliver_key=f"slack::channel::C{index}")
        _pending_failure(
            sqlite,
            f"run-budget-{index}",
            definition_id,
            created_at=f"2026-07-27T0{index}:00:00+00:00",
            notice={
                "state": "pending",
                "attempts": 0,
                "next_attempt_at": None,
                "failure_id": f"run-budget-{index}",
            },
        )

    delivered: list[str] = []

    async def _slow_emit(controller, context, backend, diagnostic, **kwargs):
        # Slower than the budget, so the pass is over the line after the FIRST one.
        await asyncio.sleep(0.05)
        delivered.append(str(kwargs.get("failure_id")))
        evidence = kwargs.get("delivery")
        if evidence is not None:
            evidence.send_returned = True
            evidence.delivered_id = "m1"
            evidence.persisted_row = {"id": "m1"}
        return False

    monkeypatch.setattr(scheduled_tasks, "emit_replayed_backend_failure", _slow_emit)
    # ``raising=False``: the constant does not exist before this fix, and the test has
    # to be runnable at that head to be red evidence rather than an import error.
    monkeypatch.setattr(
        failure_notices, "NOTICE_DRAIN_PASS_BUDGET_SECONDS", 0.01, raising=False
    )

    from types import SimpleNamespace

    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)
    asyncio.run(service._drain_failure_notices())

    assert len(delivered) == 1, (
        "the pass must stop pulling work once its budget is spent, and must still "
        f"make progress on one notice: {delivered}"
    )
    # The in-flight delivery was never cancelled: the one notice the pass started is
    # acknowledged, not left half-done.
    started = delivered[0]
    assert sqlite.owed_failure_notice(started)["state"] == NOTICE_SENT

    now = "2099-01-01T00:00:00+00:00"
    for index in range(3):
        run_id = f"run-budget-{index}"
        if run_id == started:
            continue
        untouched = sqlite.owed_failure_notice(run_id)
        assert untouched["attempts"] == 0, (
            f"a row the pass never reached must not consume an attempt: {untouched}"
        )
        assert owed_notice_eligible(untouched, now), (
            f"a row the pass never reached must stay eligible: {untouched}"
        )
    listed = {str(row["id"]) for row in sqlite.list_owed_failure_notices(limit=10)}
    assert listed == {f"run-budget-{i}" for i in range(3)} - {started}, (
        f"the deferred remainder is exactly what the next pass picks up: {listed}"
    )


def test_the_pass_budget_admits_one_full_worst_case_delivery() -> None:
    """The lower side of the budget's two-sided argument, as an assertion.

    A budget below ``NOTICE_DELIVERY_TIMEOUT_SECONDS`` would be spent before the
    notice the pass STARTED could finish, which reads as "bound the send" — a job the
    walk deadline already owns and this constant cannot do, since it is only ever
    checked between notices. At or above one deadline, a pass always makes progress on
    at least one notice no matter how slow that notice is.
    """

    from core.failure_notices import (
        NOTICE_DELIVERY_TIMEOUT_SECONDS,
        NOTICE_DRAIN_PASS_BUDGET_SECONDS,
    )

    assert NOTICE_DRAIN_PASS_BUDGET_SECONDS >= NOTICE_DELIVERY_TIMEOUT_SECONDS, (
        "a pass must be able to contain one full worst-case delivery"
    )


def _zh_body_service(tmp_path: Path, sqlite, requests):
    """A service whose ``_t`` is the REAL translator, in Chinese.

    Copy defects are only visible through the real catalog: a ``_t`` that echoes keys
    reports which key was chosen, not what the user reads, and the whole point here is
    that an untranslated fragment survives translation.
    """

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService

    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)
    service.controller.config = SimpleNamespace(language="zh", platform="slack")
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)
    return service


def test_an_interruption_notice_never_prints_the_raw_wire_reason(tmp_path: Path) -> None:
    """HFR-094's family — an ``interrupt_reason`` is a wire value, not product copy.

    ``harness.notice.interrupted`` takes ``reason=`` and renders it INSIDE the
    sentence, and the drain passed the stored value straight through. So a Chinese
    user was told 后台任务「daily report」被中断(backend_refresh) — an internal
    identifier, untranslated, in the middle of a translated sentence, for every one of
    the six reasons in ``RUN_INTERRUPTION_REASONS``.

    Same shape and same fix as ``SWEEP_I18N_KEYS``: a closed map from wire value to
    label key, drift-pinned against the lane's own frozenset in
    ``tests/test_i18n_backend_keys.py`` so a new reason cannot ship unlabelled.
    """

    from core.run_settlement import (
        RUN_INTERRUPTION_REASONS as REASONS,
        SETTLED_BY_RESTARTED,
    )

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-zh-reason", name="daily report")
    service = _zh_body_service(tmp_path, sqlite, requests)

    for reason in sorted(REASONS):
        body = service._failure_notice_body(
            {"id": "run-zh", "task_id": "task-zh-reason", "error": "boom"},
            {"failure_id": "run-zh", "interrupt_reason": reason},
        )
        assert reason not in body, (
            f"the raw wire reason {reason!r} leaked into user-visible copy: {body}"
        )
        expected_copy = "重启停止" if reason == SETTLED_BY_RESTARTED else "被中断"
        assert expected_copy in body, f"the interruption must still render: {body}"


def test_an_unmapped_interruption_reason_renders_a_localized_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The closed map needs a closed FALLBACK, or the leak comes back by another door.

    ``KEYS.get(reason, reason)`` would be the obvious spelling and it is the defect
    again: a reason present in ``RUN_INTERRUPTION_REASONS`` but missing from the label
    map — exactly what the drift test in ``tests/test_i18n_backend_keys.py`` exists to
    catch, which means exactly what could reach a user between someone adding a reason
    and CI failing — would print the identifier. The fallback is therefore a
    TRANSLATED generic label, never the wire value and never the dotted key path.

    Driven by emptying the map rather than by inventing a reason, because
    ``is_interruption`` gates the branch on lane membership: an unmapped reason is
    reachable only as a map/lane disagreement, so that is what the test creates.
    """

    import core.failure_notices as failure_notices
    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-zh-unmapped", name="daily report")
    service = _zh_body_service(tmp_path, sqlite, requests)

    monkeypatch.setattr(failure_notices, "NOTICE_REASON_I18N_KEYS", {})

    body = service._failure_notice_body(
        {"id": "run-zh", "task_id": "task-zh-unmapped", "error": "boom"},
        {"failure_id": "run-zh", "interrupt_reason": "backend_refresh"},
    )

    fallback = i18n_t(failure_notices.NOTICE_REASON_UNKNOWN_I18N_KEY, "zh")
    assert fallback in body, f"an unmapped reason must render the localized label: {body}"
    assert "backend_refresh" not in body, f"and never the wire value: {body}"
    assert failure_notices.NOTICE_REASON_UNKNOWN_I18N_KEY not in body, (
        f"nor the dotted key path: {body}"
    )


@pytest.mark.parametrize("language", ["en", "zh"])
def test_hfr_475_restart_notice_is_one_calm_action_without_internal_details(
    tmp_path: Path,
    language: str,
) -> None:
    """A restart notice names readable work but never exposes an orphaned id."""

    from types import SimpleNamespace

    from core.run_settlement import SETTLED_BY_RESTARTED
    from core.scheduled_tasks import ScheduledTaskService
    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    _watch(sqlite, "watch-release", name="Watch release", mode="once")
    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)
    service.controller.config = SimpleNamespace(language=language, platform="avibe")
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)

    def _body(definition_id: str, run_id: str) -> str:
        _settled_run(
            sqlite,
            definition_id,
            run_id,
            status="failed",
            at="2026-08-11T04:47:17+00:00",
            metadata={"interrupt_reason": SETTLED_BY_RESTARTED},
        )
        return service._failure_notice_body(
            sqlite.get_run(run_id),
            {
                "failure_id": run_id,
                "interrupt_reason": SETTLED_BY_RESTARTED,
            },
        )

    named = _body("watch-release", "run-restart-named")
    assert named == i18n_t(
        "harness.notice.restartStopped",
        language,
        name="Watch release",
    )
    assert "\n" not in named

    opaque_definition_id = "b366664bf5db"
    unnamed = _body(opaque_definition_id, "run-restart-unnamed")
    assert unnamed == i18n_t("harness.notice.restartStoppedUnnamed", language)
    assert opaque_definition_id not in unnamed
    assert "run-restart-unnamed" not in unnamed
    assert "\n" not in unnamed


def _settled_run(
    sqlite,
    definition_id: str,
    run_id: str,
    *,
    status: str,
    at: str,
    metadata: dict | None = None,
) -> None:
    """One already-settled run of *definition_id*, stamped at *at*."""

    sqlite.enqueue_run(
        {
            "id": run_id,
            "request_type": "scheduled",
            "status": status,
            "definition_id": definition_id,
            "error": "boom" if status == "failed" else None,
            "created_at": at,
            "completed_at": at,
            "metadata": metadata or {},
        }
    )


@pytest.mark.parametrize("language", ["en", "zh"])
def test_the_failed_lane_names_its_class_and_when_it_last_succeeded(
    tmp_path: Path,
    language: str,
) -> None:
    """D5's body contract, two of whose named items the failed lane never printed.

    Subordinate coverage under §D5's body requirement (plan
    ``docs/plans/harness-run-reliability.md``:3167-3169 — "the error and its class …
    when it last succeeded"); no new scenario id this round, the §10.7 HFR-280…319
    assignment is offered to the maintainer as a follow-up.

    TWO GAPS, both in the per-fire FAILED lane specifically:

    * **The class.** ``interrupt_reason`` is not a synonym for "interrupted": master
      stamps it for ``no_terminal_result`` / ``refused_concurrent_turn`` /
      ``transport_unavailable`` / ``queue_hold_expired`` too, and those recur on every
      fire, so ``is_interruption`` correctly keeps them in the failure lane. But the
      interrupted HEADLINE was the only place any reason was rendered, so on the failed
      lane the class was dropped entirely and the user saw only whatever text the
      ``error`` column happened to hold. ``NOTICE_REASON_I18N_KEYS`` cannot supply the
      label: its key set is drift-pinned to ``RUN_INTERRUPTION_REASONS``, which is
      exactly the set this lane excludes.
    * **The last success.** No read for it existed at all, on any lane.

    Asserted through the REAL translator in BOTH languages, because the defect is in
    rendered copy: a ``_t`` that echoes keys would report which key was chosen and say
    nothing about whether the sentence a user reads is translated. The wire value must
    never appear, which is HFR-094's lesson applied to a second call site.
    """

    from types import SimpleNamespace

    import core.failure_notices as failure_notices
    from core.scheduled_tasks import ScheduledTaskService
    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-class", name="daily report")
    # A success, then a later success, then the failure being reported: the line has to
    # name the LATEST success, not the first one found.
    _settled_run(sqlite, "task-class", "run-ok-1", status="succeeded", at="2026-07-20T03:00:00+00:00")
    _settled_run(sqlite, "task-class", "run-ok-2", status="succeeded", at="2026-07-26T03:00:00+00:00")
    _settled_run(sqlite, "task-class", "run-bad", status="failed", at="2026-07-27T03:00:00+00:00")

    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)
    service.controller.config = SimpleNamespace(language=language, platform="slack")
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)

    reason = "no_terminal_result"
    assert reason not in failure_notices.RUN_INTERRUPTION_REASONS, (
        "the premise: this reason belongs to the FAILED lane, not the interrupted one"
    )
    notice = {"failure_id": "run-bad", "interrupt_reason": reason}
    assert not failure_notices.is_interruption(notice)

    body = service._failure_notice_body(sqlite.get_run("run-bad"), notice)

    # --- the class line ------------------------------------------------------
    label = i18n_t(failure_notices.notice_failure_class_i18n_key(reason), language)
    assert label in body, f"the failed lane must name its class: {body}"
    assert reason not in body, f"and never the raw wire reason: {body}"
    assert "harness.notice." not in body, f"nor a dotted key path: {body}"

    # --- the last-success line ----------------------------------------------
    assert "2026-07-26T03:00:00+00:00" in body, (
        f"the body must say when the definition last succeeded: {body}"
    )
    assert "2026-07-20T03:00:00+00:00" not in body, (
        f"and it must be the LATEST success, not the earliest: {body}"
    )
    assert i18n_t("harness.notice.lastSucceeded", language).split("{")[0].strip() in body, (
        f"rendered through the localized sentence, not bare: {body}"
    )


def test_a_failed_lane_body_omits_both_lines_when_neither_is_real(tmp_path: Path) -> None:
    """The other half of the contract: render only what exists.

    A definition with no prior success has no last-success line, and a failure with no
    ``interrupt_reason`` has no class line. Rendering "Last succeeded: never" or
    "Class: unknown" would be the ``harness.notice.unknownError`` mistake again — copy
    about nothing, on the lane that already carries the most lines.
    """

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-fresh", name="daily report")
    _settled_run(sqlite, "task-fresh", "run-bad", status="failed", at="2026-07-27T03:00:00+00:00")

    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)
    service.controller.config = SimpleNamespace(language="en", platform="slack")
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)

    body = service._failure_notice_body(
        sqlite.get_run("run-bad"), {"failure_id": "run-bad", "interrupt_reason": None}
    )

    from vibe.i18n import t as i18n_t

    assert i18n_t("harness.notice.lastSucceeded", "en").split("{")[0].strip() not in body, (
        f"a definition that has never succeeded gets no last-success line: {body}"
    )
    assert i18n_t("harness.notice.failureClass", "en").split("{")[0].strip() not in body, (
        f"a failure with no recorded class gets no class line: {body}"
    )


def test_the_last_success_read_is_one_bounded_indexed_seek(tmp_path: Path) -> None:
    """Subordinate to HFR-068's family — this read runs per notice on the 2s tick.

    The body renders one instant, so the STORE must return one row: an indexed seek on
    ``ix_agent_runs_definition_settled`` with ``LIMIT 1`` and the filters as SQL terms,
    not a definition's whole success history filtered in Python. Asserted on the
    CONSTRAINED TERMS rather than only on the index name, which is the HFR-095 /
    HFR-086 lesson: a plan can name an index while the term stays a per-row filter.

    The expression and tie-break are shared BY NAME with ``_health_rows`` rather than
    retyped — two copies of ``coalesce(completed_at, created_at)`` drift silently, and
    the copy that drifts is the one the planner stops matching.
    """

    import re

    sqlite, _ = _store(tmp_path)
    _task(sqlite, "task-hot")
    for index in range(400):
        instant = f"2026-07-27T{index // 60:02d}:{index % 60:02d}:00+00:00"
        _settled_run(
            sqlite,
            "task-hot",
            f"run-{index:04d}",
            status="failed" if index % 4 else "succeeded",
            at=instant,
        )

    plans = _agent_run_query_plans(
        sqlite,
        tmp_path / "state" / "vibe.sqlite",
        lambda: sqlite.last_success_settled_at("task-hot"),
    )
    assert len(plans) == 1, f"one instant must cost one statement: {plans}"
    statement, plan = plans[0]
    flat = " ".join(plan)
    assert "ix_agent_runs_definition_settled" in flat, (
        f"the seek must ride the settled-time index: {plan}"
    )
    assert "SCAN agent_runs" not in flat, f"and must not scan the table: {plan}"
    assert "TEMP B-TREE" not in flat, (
        f"the index supplies the order, so nothing may be sorted: {plan}"
    )
    normalized = re.sub(r"\s+", " ", statement).upper()
    # The bound travels as a PARAMETER, exactly as ``_health_rows``' does, so the
    # value is asserted from the parameters rather than from the SQL text.
    assert "LIMIT ?" in normalized, f"the bound has to be in the DATABASE: {statement}"
    statements = _agent_run_statements(sqlite, lambda: sqlite.last_success_settled_at("task-hot"))
    assert len(statements) == 1
    assert 1 in tuple(statements[0][1]), (
        f"and the bound has to be ONE row, not a page: {statements[0][1]}"
    )
    assert "DEFINITION_ID = ?" in normalized, f"the definition is an equality term: {statement}"
    assert "STATUS IN" in normalized, f"the verdict filter is a SQL term: {statement}"
    assert "RUN_TYPE NOT IN" in normalized, (
        "the non-verdict exclusion (watch supervisor, escalation turn) is a SQL term too: "
        f"{statement}"
    )

    # And the answer is the real one: the newest succeeded row in the history above.
    assert sqlite.last_success_settled_at("task-hot") == "2026-07-27T06:36:00+00:00"
    # A definition with no success at all answers None rather than an empty string, so
    # the body can decide by presence.
    assert sqlite.last_success_settled_at("task-absent") is None


@pytest.mark.parametrize(
    "attempts,expected_attempt",
    [("x", 1), ("", 1), ("3.5", 4), (["1"], 1), ({"n": 1}, 1), (None, 1)],
    ids=["nonnumeric", "blank", "float-string", "list", "dict", "null"],
)
def test_a_malformed_attempts_value_degrades_instead_of_raising(
    attempts, expected_attempt
) -> None:
    """HFR-076, subordinate — ``attempts`` is JSON, so it can be anything.

    ``int(notice.get("attempts") or 0)`` raises ``ValueError`` on a nonnumeric string
    and ``TypeError`` on a container. Nothing crashes — the drain's per-row handler
    catches it — and that is precisely what makes it bad: the exception escapes BEFORE
    the claim, so no state is written at all. The row keeps its malformed value, stays
    eligible, is re-selected by the next 2 s tick, raises again, and occupies a slot in
    the batch of ten forever. Starvation of every notice behind it, with a log line per
    tick and a drain that looks busy rather than broken.

    CORRECTED IN ROUND 19: the round-12 version degraded EVERYTHING unreadable to 0,
    which agreed with SQLite for these shapes except one — ``"3.5"`` reads 3 under
    ``CAST(... AS INTEGER)``'s numeric-prefix parse, not 0 — so the expected pair here
    was pinning a Python-only answer the CAS would refuse forever. Both readers now
    consume one model of the CAST (``storage.sqlite_semantics.sqlite_cast_integer``),
    so the float-string row expects the CAST's answer: attempt 4, not attempt 1. The
    truly nonnumeric shapes still read 0 on both sides.
    """

    from core.failure_notices import BACKOFF_SECONDS, next_attempt

    assert next_attempt({"state": "pending", "attempts": attempts}) == (
        expected_attempt,
        BACKOFF_SECONDS[expected_attempt - 1],
    )


def test_the_python_and_sql_readings_of_a_malformed_attempts_agree(tmp_path: Path) -> None:
    """The agreement itself, asserted rather than asserted-about.

    ``next_attempt`` picks the backoff, ``notice_write_expectation`` builds the CAS
    predicate, and ``OWED_NOTICE_ATTEMPTS_SQL`` evaluates it inside SQLite. If any one
    of the three read a malformed ``attempts`` differently the claim would be refused
    every pass — the same wedge as raising, reached by a guarded write that can never
    match.
    """

    from storage.background import notice_write_expectation

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-bad-attempts", deliver_key="slack::channel::C1")
    _pending_failure(
        sqlite,
        "run-bad-attempts",
        "task-bad-attempts",
        created_at="2026-07-27T00:00:00+00:00",
        notice={
            "state": "pending",
            "attempts": "not-a-number",
            "next_attempt_at": None,
            "failure_id": "run-bad-attempts",
        },
    )

    notice = sqlite.owed_failure_notice("run-bad-attempts")
    assert notice_write_expectation(notice)[1] == 0, "the CAS side reads 0"
    claimed = sqlite.update_owed_failure_notice(
        "run-bad-attempts",
        expect=notice_write_expectation(notice),
        attempts=1,
    )
    assert claimed is not None, "so SQL must agree and let the claim through"
    assert claimed["attempts"] == 1


def test_a_malformed_attempts_row_advances_instead_of_occupying_the_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The consuming outcome: the batch slot is released, not re-occupied forever."""

    import core.scheduled_tasks as scheduled_tasks

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-bad-attempts", deliver_key="slack::channel::C1")
    _pending_failure(
        sqlite,
        "run-bad-attempts",
        "task-bad-attempts",
        created_at="2026-07-27T00:00:00+00:00",
        notice={
            "state": "pending",
            "attempts": ["1"],
            "next_attempt_at": None,
            "failure_id": "run-bad-attempts",
        },
    )

    delivered: list[str] = []

    async def _emit(controller, context, backend, diagnostic, **kwargs):
        delivered.append(str(kwargs.get("failure_id")))
        evidence = kwargs.get("delivery")
        if evidence is not None:
            evidence.send_returned = True
            evidence.delivered_id = "m1"
            evidence.persisted_row = {"id": "m1"}
        return False

    monkeypatch.setattr(scheduled_tasks, "emit_replayed_backend_failure", _emit)

    from types import SimpleNamespace

    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)
    asyncio.run(service._drain_failure_notices())

    notice = sqlite.owed_failure_notice("run-bad-attempts")
    assert notice["attempts"] == 1, f"the claim must consume an attempt: {notice}"
    assert notice["state"] == NOTICE_SENT, f"and the delivery must be reached: {notice}"
    assert delivered == ["run-bad-attempts"]
    assert sqlite.list_owed_failure_notices(limit=10) == [], (
        "a settled row must free its slot in the batch"
    )


# =============================================================================
# round 13: #1060's field case — the watch that died with its delivery target
# =============================================================================
#
# The maintainer's acceptance note (PR #1072 comment 5120451508) asks this round to
# absorb the field evidence filed on #1060 (comment 5097759698) rather than argue
# from the plan: a real watch (``lane-b-pr5``) was created with ``--session-id``, the
# session later ceased to exist, three follow-up deliveries failed four minutes
# apart, the watch flipped to ``enabled=0``, and NOBODY WAS TOLD FOR 3.5 HOURS. A P1
# review finding sat unattended in that window, because the watch was the only thing
# that would have surfaced it.
#
# Five outcomes are owed, and the two tests below consume them end to end rather
# than one layer at a time — the note's ask is field realism, so nothing here stubs
# ``resolve_session_id_target``, the waiter subprocess, or settlement:
#
#   (a) the terminal row is NOT indistinguishable from a pause or a retirement;
#   (b) the RECORDED CAUSE is the delivery failure, never the waiter's retry
#       sentinel (#1060's first complaint: the row blamed exit 75, the configured
#       ``--retry-exit-code``, i.e. the "nothing new yet" signal);
#   (c) the terminal timestamps are COHERENT (its second: ``last_started_at`` after
#       ``last_event_at`` with a null ``last_finished_at`` on a terminal row);
#   (d) exactly one actionable notice reaches the workspace inbox — via the mandatory
#       workspace rung, since every preferred rung here names a session row that no
#       longer exists (its third complaint and the whole cost: "a monitor that fails
#       silently is worse than no monitor");
#   (e) a restart or replay can neither duplicate nor erase that notice.
#
# What this round found ALREADY COHERENT, recorded because the plan predicted
# otherwise: ``mark_cycle_result`` writes ``retired_at``, ``last_finished_at`` and
# ``last_event_at`` in ONE guarded stamp on the cycle that disables the watch, so
# (c) and the ``enabled=0``/``retired_at IS NULL`` half of (a) need no change in
# ``core/watches.py`` at all — #1060's row predates that stamp. They are asserted
# here anyway: they are the preconditions the rest of the case rests on, and a
# regression in them reopens the field bug by a different road.
#
# No new scenario id. The §10.7 HFR-280… block is unassigned and cyhhao declined to
# consume one for this round; both cases are subordinate coverage under HFR-094.


def _default_home_store_pair() -> tuple[Any, Any]:
    """A store pair over the ISOLATED HOME's database, as a fresh owner would open it.

    Not ``_store`` / ``_second_owner_store``: those point at ``tmp_path/state``, while a
    real watch supervisor, the claimed-request executor and the notice drain all reach
    the default ``paths.get_sqlite_state_path()`` — the same file
    ``resolve_session_id_target`` and ``persist_agent_message`` read. A test that split
    those two databases would exercise a shape no install has.

    Each call builds NEW objects with their own engines, which is what makes the
    second/third owner in (d) and (e) a genuine second reader rather than the same
    identity map answering twice.
    """

    from core.scheduled_tasks import TaskExecutionStore as _Requests

    requests = _Requests()
    sqlite = requests.sqlite_backend
    assert sqlite is not None, "the default-path store must be SQLite-backed"
    return sqlite, requests


def _delete_agent_session_row(session_id: str) -> None:
    """Hard-delete one ``agent_sessions`` row, the way #1060's session ceased to exist.

    Not an archive: ``resolve_session_id_target`` refuses an archived session with
    ``reason="archived"``, and an archived row still gives rung (5) a real scope to
    persist into. The field case is the harder one — the row is GONE, so the notice's
    own delivery target is missing too, which is why (d) has to land somewhere else.
    """

    from sqlalchemy import delete as sa_delete

    from storage.db import get_cached_sqlite_engine
    from storage.models import agent_sessions

    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(sa_delete(agent_sessions).where(agent_sessions.c.id == session_id))


def _watch_hook_runs(requests) -> list[Any]:
    return [row for row in requests.list_pending() if row.request_type == "watch"]


async def _drive_watch_service(service, watch_id: str, *, until, limit: int = 400) -> None:
    """Run the REAL supervisor until *until* holds, then stop it.

    The waiter is a real subprocess and the stamps are real guarded writes, so the
    loop polls rather than counting cycles: the point of using the live service is
    that the ordering between the cycle stamp, the queued hook and the disable is the
    thing under test.
    """

    from tests.test_watches import _start_watch_service

    await _start_watch_service(service)
    try:
        for _ in range(limit):
            if until():
                break
            if watch_id not in service._active_tasks:
                break
            await asyncio.sleep(0.05)
    finally:
        await service.stop()


def _execution_service(tmp_path: Path, controller, sqlite, requests):
    """``_drain_service``, plus the real translator — the bodies here are read copy."""

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService

    service = _drain_service(tmp_path, controller, sqlite, requests)
    service.controller.config = SimpleNamespace(language="en", platform="slack")
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)
    return service


def test_a_watch_that_outlives_its_delivery_target_dies_visibly(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """HFR-094, subordinate — #1060's four-step terminal case, driven end to end.

    Maintainer acceptance note: PR #1072 comment 5120451508 ("absorb the field
    evidence"). Field evidence: #1060 comment 5097759698 — a ``once`` watch pinned to
    a session that ceased to exist, whose follow-up delivery failed and which then
    stopped with no notification of any kind for 3.5 hours.

    Everything real, deliberately, because the note asks for field realism and each
    stub would remove one of the four steps: a migrated workbench DB, a real
    ``agent_sessions`` row that is then hard-deleted, the real ``ManagedWatchService``
    with a real waiter subprocess, the real claimed-request executor (so
    ``resolve_session_id_target`` raises for the real reason), the real settlement
    writer, and the real notice drain.

    The four steps, asserted in the order the failure happens:

    (c) the terminal timestamps agree with each other and survive a fresh read;
    (a) the row reads FINISHED, never paused, its waiter stays healthy, and its event
        processing says failing on both the projection and the CLI payload;
    (b) the run settled ``failed`` naming the delivery failure, the notice carries the
        structured class ``delivery_target_missing``, and the DEFINITION's
        ``last_error``/``last_exit_code`` still describe the healthy waiter — the
        retry sentinel is never blamed;
    (d) exactly one actionable notice reaches the workspace inbox, through the mandatory
        workspace rung;
    (e) a third owner replaying the same row neither duplicates nor erases it.
    """

    import json
    import sys as _sys
    from types import SimpleNamespace

    import core.failure_notices as failure_notices
    from core.watches import ManagedWatchService, ManagedWatchStore, WatchRuntimeStateStore
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.background import NOTICE_PENDING
    from vibe import cli
    from vibe.i18n import t as i18n_t

    _no_background_web_push(monkeypatch)
    _migrated_state_db()
    _workbench_session("sesd46nxp3cz5", project="proj-lane-b")

    watch_store = ManagedWatchStore()
    runtime_store = WatchRuntimeStateStore()
    sqlite, requests = _default_home_store_pair()

    watch = watch_store.add_watch(
        name="lane-b-pr5",
        session_key="",
        command=[_sys.executable, "-c", "print('event')"],
        shell_command=None,
        prefix="The waiter finished.",
        cwd=None,
        mode="once",
        timeout_seconds=30,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
        session_id="sesd46nxp3cz5",
        session_policy="existing",
    )

    # "That session later ceased to exist." Between creating the watch and its first
    # event, which is exactly the order the field case ran in.
    _delete_agent_session_row("sesd46nxp3cz5")

    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=watch_store,
        request_store=requests,
        runtime_store=runtime_store,
    )
    asyncio.run(
        _drive_watch_service(
            service,
            watch.id,
            until=lambda: watch_store.get_watch(watch.id) is not None
            and not watch_store.get_watch(watch.id).enabled,
        )
    )

    saved = watch_store.get_watch(watch.id)
    assert saved is not None and saved.enabled is False, (
        f"the premise: a ``once`` watch retires itself after its event: {saved}"
    )
    hooks = _watch_hook_runs(requests)
    assert len(hooks) == 1, f"the retiring cycle owes exactly one completion hook: {hooks}"

    # --- (c) coherent terminal history --------------------------------------
    assert saved.retired_at, f"a retired watch must record WHEN it retired: {saved}"
    assert saved.last_finished_at, (
        f"and a terminal row may not claim a cycle that never finished: {saved}"
    )
    assert saved.last_started_at and saved.last_started_at <= saved.last_finished_at, (
        "#1060's row started a cycle three hours after the watch stopped: "
        f"{saved.last_started_at} > {saved.last_finished_at}"
    )
    assert saved.last_event_at and saved.last_event_at <= saved.last_finished_at, (
        f"the event it caught cannot postdate the cycle that caught it: {saved}"
    )
    reread = ManagedWatchStore().get_watch(watch.id)
    assert (reread.retired_at, reread.last_finished_at, reread.last_event_at) == (
        saved.retired_at,
        saved.last_finished_at,
        saved.last_event_at,
    ), "the stamp has to be durable, not an artifact of the writer's in-memory row"

    # --- the delivery that fails, exactly as it did in the field -------------
    claimed = requests.claim(hooks[0].id)
    assert claimed is not None
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    executor = _execution_service(tmp_path, controller, sqlite, requests)
    asyncio.run(executor._execute_claimed_request(claimed))

    # --- (a) not indistinguishable from a pause or a retirement -------------
    projected = sqlite.get_watch(watch.id)
    assert projected["lifecycle_state"] == "finished", (
        "#1060 predicted this row would surface as PAUSED, which is what a user does "
        f"deliberately: {projected}"
    )
    assert not (projected["enabled"] is False and projected.get("retired_at") in (None, "")), (
        f"``enabled=0`` with no retirement marker is the ambiguous state: {projected}"
    )
    assert projected["health"] == "healthy", (
        f"the waiter exited zero, so downstream delivery cannot poison it: {projected}"
    )
    assert projected["processing_health"] == "failing", (
        f"the failed follow-up must remain visible on its own axis: {projected}"
    )
    # The CLI payload too, not only the store projection: a coding agent driving this
    # runtime reads ``vibe watch show``, and #1060 was discovered by a human running
    # ``vibe watch list`` for an unrelated reason. The two must agree.
    assert cli.cmd_watch_show(watch.id) == 0
    shown = json.loads(capsys.readouterr().out)["definition"]
    assert (shown["health"], shown["processing_health"], shown["lifecycle_state"]) == (
        "healthy",
        "failing",
        "finished",
    ), (
        f"the CLI payload must carry the same verdict as the projection: {shown}"
    )

    # --- (b) the recorded cause is the delivery failure ---------------------
    run = sqlite.get_run(claimed.id)
    assert run["status"] == "failed", f"the failed delivery must settle the run: {run}"
    assert "agent session id not found" in str(run.get("error") or ""), (
        f"and its error must name what actually broke: {run}"
    )
    notice = sqlite.owed_failure_notice(claimed.id)
    assert notice is not None, "a failed run owes a notice"
    assert notice["interrupt_reason"] == "delivery_target_missing", (
        "#1060's second ask — 'a cause field distinct from the last exit code'. "
        f"``delivery_target_missing`` is not ``exited 75``: {notice}"
    )
    assert not failure_notices.is_interruption(notice), (
        "the class must NOT join the interruption lane: that would change the notice's "
        f"headline and its identity, and defeat streak collapse: {notice}"
    )
    assert notice["failure_id"] == claimed.id, (
        f"so the identity stays the bare run id, not ``interrupt:…``: {notice}"
    )
    after = ManagedWatchStore().get_watch(watch.id)
    assert after.last_error is None and after.last_exit_code == 0, (
        "the WAITER was healthy — it detected its event and exited 0. #1060's row "
        f"blamed the retry sentinel instead, which sends the operator upstream: {after}"
    )

    # --- (d) exactly one actionable notice, in the workspace inbox ----------
    #
    # This half consumes the MANDATORY workspace rung (the other half of this round).
    # Without it the ladder here holds one unusable rung: rung (5) manufactures
    # ``avibe::project::sesd46nxp3cz5`` for a row that no longer exists, so
    # ``persist_agent_message`` writes nothing, no receipt appears, and the receipt-only
    # ack source correctly refuses the synthetic send id. Under round 12's
    # only-when-nothing-resolved gate the fallback never fired (``rungs`` is not empty),
    # all six attempts burned, and the notice dead-lettered with nothing written
    # anywhere. That silent dead letter is #1060's 3.5 hours, reached through the notice
    # layer instead of the definition row.
    #
    # The loop below is deliberately written to the WEAKER claim — drain until the notice
    # leaves ``pending``, up to the full attempt budget — so it holds under both the
    # retired final-attempt design and the round-14 rung, and it is not the assertion.
    # Which attempt actually delivers is pinned by
    # ``test_a_walk_whose_preferred_rungs_all_fail_lands_in_the_workspace_inbox``
    # (the first one); what THIS case owns is that the real four-step field failure ends
    # with one readable card rather than silence.
    #
    # AT LEAST ONCE, not exactly once: the contract is that no scripted run of this
    # ladder may produce two messages, which is what "exactly one" below asserts. The
    # residual duplicate window (a claimant that dies after the transport accepted) is
    # documented on ``CLAIM_LEASE_SECONDS`` and is out of this case's reach.
    sqlite_b, requests_b = _default_home_store_pair()
    drain = _execution_service(tmp_path, _live_turn_dispatcher()[0], sqlite_b, requests_b)
    for _ in range(failure_notices.MAX_ATTEMPTS + 2):
        state = str((sqlite_b.owed_failure_notice(claimed.id) or {}).get("state") or "")
        if state not in {"", NOTICE_PENDING}:
            break
        # Rewind the backoff rather than sleeping through it, as every other retry
        # test in this file does.
        sqlite_b.update_owed_failure_notice(claimed.id, next_attempt_at=None)
        asyncio.run(drain._drain_failure_notices())

    rows = _persisted_messages()
    assert [(row["platform"], row["type"], row["session_id"]) for row in rows] == [
        ("avibe", "notify", WORKSPACE_NOTICE_SESSION_ID)
    ], f"exactly one actionable notice, in the workspace inbox: {rows}"
    body = str(rows[0]["content_text"])
    assert "lane-b-pr5" in body, f"the notice must name the watch that died: {body!r}"
    assert i18n_t("harness.notice.class.deliveryTargetMissing", "en") in body, (
        f"and name the class, not only the raw error line: {body!r}"
    )
    assert i18n_t("harness.notice.watchProcessingFailed", "en").split("{")[0] in body, (
        f"the fallback must identify event processing rather than waiter failure: {body!r}"
    )
    assert i18n_t("harness.notice.watchRetired", "en") not in body, (
        f"normal one-shot retirement is unrelated to the processing failure: {body!r}"
    )
    assert i18n_t("harness.notice.watchPaused", "en").split("{")[0].strip() not in body, (
        "and must NOT offer ``vibe watch resume`` for a watch that retired — that is "
        f"the same contradiction (a) closes, restated as copy: {body!r}"
    )
    settled = sqlite_b.owed_failure_notice(claimed.id)
    assert settled["state"] == NOTICE_SENT, f"the notice must be marked delivered: {settled}"

    # --- (e) a replay may neither duplicate nor erase it -------------------
    sqlite_c, requests_c = _default_home_store_pair()
    replay = _execution_service(tmp_path, _live_turn_dispatcher()[0], sqlite_c, requests_c)
    sqlite_c.update_owed_failure_notice(claimed.id, next_attempt_at=None)
    asyncio.run(replay._drain_failure_notices())
    asyncio.run(replay._drain_failure_notices())

    assert _persisted_messages() == rows, (
        "a restart must not re-send a notice the user already has, and must not "
        "rewrite the row that proves they have it"
    )
    replayed = sqlite_c.owed_failure_notice(claimed.id)
    assert replayed["state"] == NOTICE_SENT
    assert replayed["attempts"] == settled["attempts"], (
        f"a replay consumes no attempt against a settled notice: {replayed}"
    )
    assert replayed["interrupt_reason"] == "delivery_target_missing", (
        f"and the structured cause survives the replay: {replayed}"
    )


def test_watch_cycle_outcome_pair_is_published_atomically(
    tmp_path: Path,
) -> None:
    """A held reader cannot straddle publication of the next cycle outcome."""

    from core.watches import ManagedWatchStore

    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="atomic outcome",
        session_key="",
        command=[],
        shell_command="exit 75",
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=0,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    assert store.mark_cycle_result(watch.id, exit_code=0, error=None)
    previous_outcome = (0, None)
    next_outcome = (75, "watch command exited with status 75")

    reader_has_old_exit_code = threading.Event()
    writer_finished = threading.Event()
    observed_outcomes: list[tuple[int | None, str | None]] = []
    reader_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []

    class _BlockingNamespace(dict):
        def __getitem__(self, key):
            value = super().__getitem__(key)
            if key == "last_exit_code":
                reader_has_old_exit_code.set()
                if not writer_finished.wait(timeout=5):
                    raise AssertionError("writer did not publish while the reader was paused")
            return value

    # ``watch`` is the mutation result retained by its caller and therefore the cached
    # object whose namespace publication updates. Pause its outcome reader after the
    # first old value has been fetched, then let the writer publish the complete next
    # namespace before the reader fetches the second value.
    watch.__dict__ = _BlockingNamespace(watch.__dict__)

    def _read_result() -> None:
        try:
            observed_outcomes.append(watch.last_cycle_outcome)
        except BaseException as exc:
            reader_errors.append(exc)

    def _write_result() -> None:
        try:
            assert store.mark_cycle_result(
                watch.id,
                exit_code=next_outcome[0],
                error=next_outcome[1],
            )
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    reader = threading.Thread(target=_read_result)
    reader.start()
    assert reader_has_old_exit_code.wait(timeout=5)
    writer = threading.Thread(target=_write_result)
    writer.start()
    writer.join(timeout=5)
    reader.join(timeout=5)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert not writer_errors
    assert not reader_errors
    assert observed_outcomes == [previous_outcome]

    visible_after = store.get_watch(watch.id)
    assert visible_after is not None
    assert visible_after is watch
    assert visible_after.last_cycle_outcome == next_outcome
    persisted_after = ManagedWatchStore(tmp_path / "watches.json").get_watch(watch.id)
    assert persisted_after is not None
    assert persisted_after.last_cycle_outcome == next_outcome


def test_watch_write_and_mirror_publication_exclude_same_store_reload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A newer durable edit cannot be replaced by an older pending publication."""

    from core.watches import ManagedWatchStore

    db_path = tmp_path / "state" / "vibe.sqlite"

    def _sqlite_watch_store() -> ManagedWatchStore:
        store = ManagedWatchStore(tmp_path / "unused-watches.json")
        store._sqlite = SQLiteBackgroundTaskStore(db_path)
        store.load()
        return store

    store = _sqlite_watch_store()
    other = _sqlite_watch_store()
    watch = store.add_watch(
        name="atomic publication",
        session_key="",
        command=[],
        shell_command="exit 75",
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=0,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    other.load()

    candidate_committed = threading.Event()
    allow_publication = threading.Event()
    newer_edit_committed = threading.Event()
    reload_started = threading.Event()
    reload_finished = threading.Event()
    writer_errors: list[BaseException] = []
    reloader_errors: list[BaseException] = []
    sqlite = store.sqlite_backend
    assert sqlite is not None
    original_upsert = sqlite.upsert_watch

    def _pause_after_commit(*args, **kwargs):
        landed = original_upsert(*args, **kwargs)
        candidate_committed.set()
        if not allow_publication.wait(timeout=5):
            raise AssertionError("test did not release the pending mirror publication")
        return landed

    monkeypatch.setattr(sqlite, "upsert_watch", _pause_after_commit)

    def _write_cycle_result() -> None:
        try:
            assert store.mark_cycle_result(
                watch.id,
                exit_code=75,
                error="watch command exited with status 75",
            )
        except BaseException as exc:
            writer_errors.append(exc)

    def _write_newer_edit_and_reload() -> None:
        try:
            assert candidate_committed.wait(timeout=5)
            other.set_enabled(watch.id, False)
            newer_edit_committed.set()
            reload_started.set()
            store.load()
        except BaseException as exc:
            reloader_errors.append(exc)
        finally:
            reload_finished.set()

    writer = threading.Thread(target=_write_cycle_result)
    reloader = threading.Thread(target=_write_newer_edit_and_reload)
    writer.start()
    reloader.start()
    assert candidate_committed.wait(timeout=5)
    assert newer_edit_committed.wait(timeout=5)
    assert reload_started.wait(timeout=5)
    assert not reload_finished.wait(timeout=0.1), (
        "same-store reload must wait for durable write plus mirror publication"
    )
    allow_publication.set()
    writer.join(timeout=5)
    reloader.join(timeout=5)

    assert not writer.is_alive()
    assert not reloader.is_alive()
    assert not writer_errors
    assert not reloader_errors
    visible = store.get_watch(watch.id)
    other_sqlite = other.sqlite_backend
    assert other_sqlite is not None
    persisted = other_sqlite.get_watch(watch.id)
    assert visible is not None
    assert visible.enabled is False
    assert persisted is not None
    assert persisted["enabled"] is False


def test_a_forever_watch_repeating_the_field_failure_notifies_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-094, subordinate — #1060's REPETITION pattern, which is the other half.

    Maintainer acceptance note: PR #1072 comment 5120451508. Field evidence: #1060
    comment 5097759698, whose log is three failures four minutes apart, not one.

    The ``once`` case above proves a terminal watch is visible. A ``forever`` watch is
    where the two lanes of #1060's report pull against each other, and both directions
    have to hold at the same time:

    * the retry sentinel (exit 75, "nothing new yet") must NOT disable the watch or
      look like a failure — it is the configured healthy signal;
    * the real delivery failures repeat on every event, so they must collapse to ONE
      canonical notice rather than one message per event — the streak lane, which is
      only reachable because ``delivery_target_missing`` stays OUT of
      ``RUN_INTERRUPTION_REASONS``;
    * and the definition's ``last_error`` legitimately holds the sentinel string for
      its last cycle, so the notice must not present that as the failure's cause.
      This is #1060's first complaint in its exact shape: the sentinel is a true fact
      about the waiter and a false explanation of the death.

    The waiter is a real subprocess whose exit code walks a marker file: 75 first,
    then 0 twice (two events, two follow-up deliveries, two failures), then 75 again
    so the LAST recorded cycle is the sentinel — the state #1060's row was actually
    read in.
    """

    import sys as _sys
    from types import SimpleNamespace

    import core.failure_notices as failure_notices
    from core.watches import ManagedWatchService, ManagedWatchStore, WatchRuntimeStateStore

    _no_background_web_push(monkeypatch)
    _migrated_state_db()
    _workbench_session("sesGoneForever", project="proj-forever")

    marker = tmp_path / "waiter-cycles.txt"
    waiter = tmp_path / "waiter.py"
    waiter.write_text(
        "import pathlib, sys\n"
        f"marker = pathlib.Path(r{str(marker)!r})\n"
        "n = (int(marker.read_text() or 0) if marker.exists() else 0) + 1\n"
        "marker.write_text(str(n))\n"
        "if n in (2, 3):\n"
        "    print('event %d' % n)\n"
        "    sys.exit(0)\n"
        "sys.exit(75)\n",
        encoding="utf-8",
    )

    watch_store = ManagedWatchStore()
    runtime_store = WatchRuntimeStateStore()
    sqlite, requests = _default_home_store_pair()

    watch = watch_store.add_watch(
        name="lane-b-forever",
        session_key="",
        command=[_sys.executable, str(waiter)],
        shell_command=None,
        prefix="The waiter finished.",
        cwd=None,
        mode="forever",
        timeout_seconds=30,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
        session_id="sesGoneForever",
        session_policy="existing",
    )
    _delete_agent_session_row("sesGoneForever")

    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=watch_store,
        request_store=requests,
        runtime_store=runtime_store,
    )

    # HFR-464 serializes Watch follow-ups. This scenario is about failure-notice
    # deduplication, so settle each event Run before allowing the next event and
    # remove the unrelated five-second production cooldown from the test clock.
    monkeypatch.setattr("core.watches.WATCH_MIN_REARM_SECONDS", 0)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    executor = _execution_service(tmp_path, controller, sqlite, requests)

    asyncio.run(
        _drive_watch_service(
            service,
            watch.id,
            until=lambda: len(_watch_hook_runs(requests)) == 1,
        )
    )
    first_hook = _watch_hook_runs(requests)[0]
    first_claim = requests.claim(first_hook.id)
    assert first_claim is not None
    asyncio.run(executor._execute_claimed_request(first_claim))

    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=watch_store,
        request_store=requests,
        runtime_store=runtime_store,
    )
    asyncio.run(
        _drive_watch_service(
            service,
            watch.id,
            until=lambda: len(_watch_hook_runs(requests)) == 1,
        )
    )
    second_hook = _watch_hook_runs(requests)[0]
    second_claim = requests.claim(second_hook.id)
    assert second_claim is not None
    asyncio.run(executor._execute_claimed_request(second_claim))

    sentinel = "watch command exited with status 75"

    def _last_completed_cycle_is_retry() -> bool:
        row = watch_store.get_watch(watch.id)
        return row is not None and row.last_cycle_outcome == (75, sentinel)

    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=watch_store,
        request_store=requests,
        runtime_store=runtime_store,
    )
    asyncio.run(
        _drive_watch_service(
            service,
            watch.id,
            until=_last_completed_cycle_is_retry,
            limit=800,
        )
    )

    hooks = [first_hook, second_hook]
    assert len(hooks) == 2, f"two events must queue two follow-up deliveries: {hooks}"

    saved = watch_store.get_watch(watch.id)
    assert saved.enabled is True, (
        "a configured retry exit code is the HEALTHY 'nothing new yet' signal; it may "
        f"never retire the watch: {saved}"
    )
    assert saved.retired_at is None and saved.last_finished_at is None, (
        f"nor stamp a retirement on a watch that is still running: {saved}"
    )
    assert saved.last_error == sentinel and saved.last_exit_code == 75, (
        f"the premise for the last assertion below — the last cycle was a retry: {saved}"
    )

    for hook in hooks:
        run = sqlite.get_run(hook.id)
        assert run["status"] == "failed", f"both deliveries fail the same way: {run}"
        assert "agent session id not found" in str(run.get("error") or "")

    projected = sqlite.get_watch(watch.id)
    assert projected["lifecycle_state"] == "waiting", (
        f"the watch is still armed, so it is not finished: {projected}"
    )
    assert projected["health"] == "healthy", (
        f"retry exit 75 is a healthy waiter outcome, not a failure: {projected}"
    )
    assert projected["processing_health"] == "failing", (
        "the repeating delivery failure belongs to processing history instead: "
        f"{projected}"
    )

    # ONE canonical notice for the streak, keyed to the FIRST failed run.
    first = sqlite.owed_failure_notice(hooks[0].id)
    second = sqlite.owed_failure_notice(hooks[1].id)
    for notice in (first, second):
        assert notice["interrupt_reason"] == "delivery_target_missing", notice
        assert not failure_notices.is_interruption(notice), (
            "in the interruption lane each run would notify separately — one message "
            f"per event, which is the spam the streak exists to prevent: {notice}"
        )
    assert first["failure_id"] == hooks[0].id and second["failure_id"] == hooks[1].id, (
        "the per-fire lane keeps the bare run id as its identity, which is what lets "
        "the live path's dedup see a notification it already delivered"
    )
    decision = failure_notices.decide(
        run_id=hooks[1].id,
        definition_id=watch.id,
        notice=second,
        streak_facts=sqlite.failure_streak_decision(watch.id, hooks[1].id),
        earlier_unsettled=None,
    )
    assert decision.action in {failure_notices.ACTION_DEFER, failure_notices.ACTION_SKIP}, (
        f"the second failure of one streak must not notify on its own: {decision}"
    )
    canonical = failure_notices.decide(
        run_id=hooks[0].id,
        definition_id=watch.id,
        notice=first,
        streak_facts=sqlite.failure_streak_decision(watch.id, hooks[0].id),
        earlier_unsettled=None,
    )
    assert canonical.action == failure_notices.ACTION_DELIVER, (
        f"and the canonical one is the earliest failed run of the streak: {canonical}"
    )

    body = executor._failure_notice_body(sqlite.get_run(hooks[0].id), first)
    assert sentinel not in body, (
        "the definition's ``last_error`` is a true fact about the waiter's LAST cycle "
        "and a false explanation of this failure. #1060: 'anyone debugging this starts "
        f"by investigating a healthy waiter.': {body!r}"
    )
    assert "agent session id not found" in body, f"the real cause has to be there: {body!r}"


# --- the reserved session is not a run target either -------------------------
#
# Round-16 review thread 3678900318, and the same class as the two cases above one
# session row over: a definition whose ``--session-id`` names a row that CANNOT take a
# turn must fail VISIBLY instead of dispatching one.
#
# The previous round closed the composer (``POST /api/sessions/<id>/messages``). That is
# the door a human finds; the backend entry points come through
# ``resolve_session_id_target`` instead, and it refused only ARCHIVED rows while the
# reserved workspace-notifications row is deliberately kept ACTIVE. The two tests below
# consume the resolver's new ``reason="reserved"`` from the two lanes that dispatch
# differently, because their handlers are different code:
#
#   * ``agent_run`` — the ``vibe agent run --session-id`` lane named in the finding —
#     lands in ``_execute_claimed_request``'s ``UnresolvableSessionTarget`` handler;
#   * ``task_run`` — a definition pinned with ``vibe task add --session-id`` — is caught
#     one level lower by ``_execute_task``, which runs the BINDING RECOVERY. That lane is
#     the one that could do real damage on a new reason (a rebind would silently
#     re-point a user's definition), so it is pinned rather than assumed.
#
# No new scenario id: both are subordinate coverage under HFR-094, like the #1060 cases
# above them.


def _reserved_notice_session() -> str:
    """The reserved row in the DEFAULT-HOME workbench DB, with one notice in it.

    The notice matters: it makes the transcript NON-EMPTY, so "no turn was dispatched"
    is asserted as "the transcript is unchanged and still notices-only" rather than as
    "there is nothing there", which an empty table would satisfy for the wrong reason.
    """

    from storage import messages_service
    from storage.agent_session_rows import (
        WORKSPACE_NOTICE_SESSION_ID,
        resolve_workspace_notice_session,
    )
    from storage.db import get_cached_sqlite_engine

    with get_cached_sqlite_engine().begin() as conn:
        session_id = resolve_workspace_notice_session(conn, title="Workspace notifications")
        messages_service.append(
            conn,
            scope_id=None,
            session_id=session_id,
            platform="avibe",
            author="agent",
            message_type="notify",
            text="Scheduled task 'nightly' failed: backend exploded",
        )
    assert session_id == WORKSPACE_NOTICE_SESSION_ID
    return session_id


def _session_transcript(session_id: str) -> list[tuple[str, str]]:
    """``(author, type)`` for every persisted row in one session, in order."""

    return [
        (str(row["author"]), str(row["type"]))
        for row in _persisted_messages()
        if str(row["session_id"] or "") == session_id
    ]


def test_an_agent_run_pinned_to_the_reserved_session_dispatches_no_turn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-094, subordinate — the CLI lane the composer guard could not reach.

    ``vibe agent run --session-id ses-workspace-notices --agent <enabled> --message …``
    resolves the pin through ``resolve_session_id_target`` and enqueues an ``agent_run``.
    Before the resolver owned the ownership check, that resolve SUCCEEDED — the row is
    active, so only the archived branch could have stopped it — and the run went on to
    dispatch a real turn into a machine-owned row with an empty ``agent_backend``,
    mixing conversation into the failure-notice transcript.

    Everything real: the migrated workbench DB the resolver actually reads, the real
    request store, the real claimed-request executor and the real settlement writer.

    Three outcomes:

    * the run settles ``failed`` NAMING the reserved session — the visible result, and
      the reason a run failure is the right disposal rather than a silent skip;
    * the notice is NOT classified ``delivery_target_missing``. The row exists and is
      healthy; this is a configuration error, and that label would send the reader
      hunting for a session sitting in their own inbox;
    * the reserved transcript is UNCHANGED and still notices-only — no ``user`` turn,
      no assistant reply.
    """

    _no_background_web_push(monkeypatch)
    _migrated_state_db()
    reserved = _reserved_notice_session()
    before = _session_transcript(reserved)
    assert before == [("agent", "notify")], f"the premise: one notice, no turns: {before}"

    sqlite, requests = _default_home_store_pair()
    queued = requests.enqueue_agent_run(
        message="summarise today's failures",
        session_id=reserved,
        session_policy="existing",
        source_kind="cli",
        source_actor="cli",
    )
    claimed = requests.claim(queued.id)
    assert claimed is not None
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    executor = _execution_service(tmp_path, controller, sqlite, requests)
    asyncio.run(executor._execute_claimed_request(claimed))

    run = sqlite.get_run(claimed.id)
    assert run["status"] == "failed", f"a turn that may not be dispatched is a failure: {run}"
    error = str(run.get("error") or "")
    assert reserved in error and "reserved" in error, (
        f"and the recorded cause has to name the reserved session: {error!r}"
    )

    notice = sqlite.owed_failure_notice(claimed.id)
    assert notice is not None, "a failed run owes a notice"
    assert notice["interrupt_reason"] != "delivery_target_missing", (
        "``delivery_target_missing`` means the destination CEASED TO EXIST. This one is "
        f"alive and in the inbox — a wrong class is worse than no class: {notice}"
    )

    assert _session_transcript(reserved) == before, (
        "NO turn may be dispatched into the runtime's own row: its transcript has to be "
        f"byte-identical to the notices it held before the run: {_session_transcript(reserved)}"
    )
    assert requests.list_pending() == [], (
        "and the refused run leaves NOTHING queued — not a requeue, not a retry. A "
        "settled-failed row that is still pending would fire again on the next drain, "
        f"which is the same hole one tick later: {requests.list_pending()}"
    )


def test_a_task_pinned_to_the_reserved_session_is_paused_never_rebound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-094, subordinate — the binding-recovery lane must not "repair" this one.

    ``task_run`` does not reach ``_execute_claimed_request``'s handler: ``_execute_task``
    catches ``UnresolvableSessionTarget`` first and runs ``_recover_pinned_session_binding``,
    which for some definitions RESERVES A REPLACEMENT SESSION and re-points the
    definition at it. Auditing the new reason meant proving that cannot happen here,
    because a silent rebind would answer "you pointed this at a row that takes no turns"
    by pointing it somewhere it does — and then dispatching the turn.

    It cannot, and the reason is structural rather than a reason check: only
    ``create_once`` is ever rebound, and a ``create_once`` definition reserved its own
    session from ``SESSION_ID_ALPHABET``, which contains no ``-`` and therefore can
    never mint ``ses-workspace-notices``. Reaching the reserved id at all takes a
    user-pinned ``existing`` binding, and ``existing`` is never rebound. Its first
    classified failure is visible and the third consecutive classified failure pauses
    it; transient failures carry no such code and never advance this policy.

    Pinned here so the structural argument has a test behind it: still enabled for the
    first two failures, paused on the third, no replacement session, and no turn handed
    to the message handler.
    """

    from core.scheduled_tasks import ScheduledTaskStore
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID

    from tests.test_scheduled_tasks import _binding_env, _binding_service

    _no_background_web_push(monkeypatch)
    _binding_env(tmp_path, monkeypatch)
    _migrated_state_db()
    reserved = _reserved_notice_session()
    before = _session_transcript(reserved)
    assert before == [("agent", "notify")], f"the premise: one notice, no turns: {before}"

    sqlite, requests = _store(tmp_path)
    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()
    task = store.add_task(
        name="daily digest",
        session_key="",
        session_id=reserved,
        session_policy="existing",
        prompt="send digest",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
    )

    dispatched: list = []
    service = _binding_service(tmp_path, store, dispatched)
    service.request_store = requests

    claimed_ids: list[str] = []
    for attempt in range(1, 4):
        current = store.get_task(task.id)
        assert current is not None
        queued = requests.enqueue_task_run(task.id, source_kind="scheduler", task=current)
        claimed = requests.claim(queued.id)
        assert claimed is not None
        claimed_ids.append(claimed.id)
        asyncio.run(service._execute_claimed_request(claimed))
        saved = store.get_task(task.id)
        assert saved is not None
        assert saved.enabled is (attempt < 3)

    saved = store.get_task(task.id)
    assert saved is not None
    assert saved.enabled is False, (
        f"a definition that can never fire again must not keep firing: {saved}"
    )
    assert saved.session_id == WORKSPACE_NOTICE_SESSION_ID, (
        "and the user's own pin must still be there to re-point — a rebind here would "
        f"silently answer a configuration error with a different session: {saved}"
    )
    assert dispatched == [], (
        f"no prompt may reach the message handler on this fire: {dispatched}"
    )

    run = sqlite.get_run(claimed_ids[-1])
    assert run["status"] == "failed", f"the fire that could not run is a failure: {run}"
    assert "paused" in str(run.get("error") or ""), (
        f"and its recorded detail is the recovery's own verdict: {run}"
    )
    assert reserved in str(run.get("error") or ""), (
        f"which still names the session the user pinned: {run}"
    )
    assert {
        (sqlite.get_run(run_id).get("metadata") or {}).get("failure_code")
        for run_id in claimed_ids
    } == {"unresolvable_target"}
    assert sqlite.owed_failure_notice(claimed_ids[0]) is not None, (
        "the first classified failure must be visible before the third failure pauses"
    )
    assert _session_transcript(reserved) == before, (
        f"the runtime's own row stays notices-only: {_session_transcript(reserved)}"
    )


@pytest.mark.parametrize("language", ["en", "zh"])
def test_a_disabled_watchs_notice_copy_distinguishes_retired_from_paused(
    tmp_path: Path,
    language: str,
) -> None:
    """The copy half of outcome (a), asserted on the rendered sentence in both languages.

    Maintainer acceptance note: PR #1072 comment 5120451508. Field evidence: #1060
    comment 5097759698 — "``enabled=0`` with ``retired_at=null`` reads as *paused*, not
    *broken*… ``enabled=0`` is carrying three meanings."

    The body used to render the resume copy for EVERY disabled watch. For a retired one
    that is a self-contradiction the round otherwise closes: the lifecycle projection
    calls the row FINISHED while its notice offers ``vibe watch resume``, an action that
    would arm a watch nobody paused. So the two states get two sentences, chosen from
    the same ``retired_at`` column the projection's ``ended`` predicate reads.

    Through the REAL translator, because the defect is in copy: a ``_t`` that echoes
    keys would report which branch ran and nothing about what the user reads. And in
    BOTH languages, because ``harness.notice.watchPaused`` was translated and its
    replacement has to be too — a missing zh string degrades to English mid-sentence.
    """

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService
    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    service = _drain_service(tmp_path, SimpleNamespace(), sqlite, requests)
    service.controller.config = SimpleNamespace(language=language, platform="slack")
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)

    retired_copy = i18n_t("harness.notice.watchRetired", language)
    processing_copy = i18n_t("harness.notice.watchProcessingFailed", language).split("{")[0]
    # The command, not the whole sentence: that is the part a user could act on, and
    # the part that must not appear for a watch nobody paused.
    resume_command = "vibe watch resume"

    def _body(definition_id: str, **watch_fields) -> str:
        from storage.background import (
            WATCH_HOOK_OUTCOME_EVENT,
            WATCH_HOOK_OUTCOME_METADATA_KEY,
        )

        _watch(sqlite, definition_id, enabled=False, **watch_fields)
        _settled_run(
            sqlite,
            definition_id,
            f"run-{definition_id}",
            status="failed",
            at="2026-07-27T03:00:00+00:00",
            metadata={WATCH_HOOK_OUTCOME_METADATA_KEY: WATCH_HOOK_OUTCOME_EVENT},
        )
        return service._failure_notice_body(
            sqlite.get_run(f"run-{definition_id}"),
            {"failure_id": f"run-{definition_id}", "interrupt_reason": None},
        )

    retired = _body("watch-retired", retired_at="2026-07-27T03:00:00+00:00", mode="once")
    assert processing_copy in retired
    assert retired_copy not in retired, (
        f"normal one-shot retirement is unrelated to event processing failure: {retired!r}"
    )
    assert resume_command not in retired, (
        "and must not offer to resume something that was never paused — the copy has to "
        f"agree with the FINISHED lifecycle state: {retired!r}"
    )

    paused = _body("watch-paused", mode="forever")
    assert resume_command in paused, (
        f"a genuinely paused watch still needs its resume affordance: {paused!r}"
    )
    assert retired_copy not in paused, (
        f"and must not be told it has finished when it has not: {paused!r}"
    )


# --- round 12: the consuming-path pin for the adapter bookkeeping guard -------
#
# GREEN FROM BIRTH. The fix these three cases consume landed as 0ebf4c55 in this
# same round (``modules/im/slack.py`` / ``modules/im/feishu.py``, matching the guard
# ``modules/im/discord.py`` already had); the adapter-level and dispatcher-level
# coverage landed with it. What was missing was the NOTICE-level consumer: proof
# that the drain's ack/retry decision comes out right for an adapter whose transport
# succeeded and whose post-send bookkeeping blew up.
#
# Red-by-mutation, since there is no head at which the production code is wrong any
# more: ``_PreGuardSlackBot`` below reintroduces the pre-0ebf4c55 shape — the same
# transport call, the same bookkeeping call, unguarded — and is asserted to produce
# the old outcome. That is the red half, run in the same file as the green one.


def _threaded_slack_notice(sqlite, requests, definition_id: str) -> str:
    """One owed notice whose only rung is a THREADED Slack channel.

    The thread segment is load-bearing: every adapter's post-send bookkeeping is
    ``if settings_manager and (thread_id or reply_to): sessions.mark_thread_active(...)``,
    so an unthreaded target never reaches the line under test.
    """

    from storage.background import NOTICE_PENDING

    _task(
        sqlite,
        definition_id,
        name="daily report",
        deliver_key="slack::channel::C123::thread::1710000000.000100",
    )
    run = requests.enqueue_task_run(definition_id)
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id=definition_id)
    assert sqlite.owed_failure_notice(run.id)["state"] == NOTICE_PENDING
    return run.id


class _RaisingSessions:
    """``sessions.mark_thread_active`` as a locked SQLite / dead session store."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def mark_thread_active(self, *args) -> None:
        self.calls.append(args)
        raise RuntimeError("session store unavailable")


def _slack_bot_with_broken_bookkeeping(*, transport_fails: bool = False):
    """A REAL ``SlackBot``, transport stubbed, bookkeeping guaranteed to raise.

    Real adapter and not a shaped stub, because the subject IS the adapter's own
    ordering: transport, bookkeeping, return. Only the HTTP call is replaced.
    """

    from config.v2_config import SlackConfig
    from modules.im.slack import SlackBot

    bot = SlackBot(SlackConfig(bot_token="xoxb-test"))
    bot._ensure_clients = lambda: None  # type: ignore[method-assign]
    bot.settings_manager = object()
    bot.sessions = _RaisingSessions()
    accepted: list[str] = []

    async def _prepared(context, text, parse_mode=None, reply_to=None):
        if transport_fails:
            # BEFORE the point of no return: the platform never took the payload.
            raise RuntimeError("slack rejected the payload")
        accepted.append(text)
        return {"ts": f"ts-{len(accepted)}"}

    bot._send_prepared_text_message = _prepared  # type: ignore[method-assign]
    return bot, accepted


class _PreGuardSlackBot:
    """The pre-0ebf4c55 send path, reproduced to make the mutation red.

    Delegates to a real ``SlackBot`` for the transport and then runs the bookkeeping
    call UNGUARDED, exactly as ``send_message`` did before the fix — so the delivered
    ``ts`` is destroyed by the exception on its way out.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.accepted: list[str] = []

    def should_use_thread_for_reply(self) -> bool:
        return self._inner.should_use_thread_for_reply()

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        response = await self._inner._send_prepared_text_message(
            context, text, parse_mode=parse_mode, reply_to=reply_to
        )
        self.accepted.append(text)
        thread_ts = context.thread_id or reply_to
        if self._inner.settings_manager and thread_ts:
            self._inner.sessions.mark_thread_active(context.user_id, context.channel_id, thread_ts)
        return response["ts"]

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_notice_acks_when_the_adapters_post_send_bookkeeping_raises(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """HFR-079, subordinate — delivered-but-unbookkept must ack, not resend.

    The consuming end of finding B, through the real drain: claim, ladder walk, replay
    emitter, dispatcher, and a real ``SlackBot`` whose transport accepted the message
    and whose ``sessions.mark_thread_active`` then raised. The user HAS the notice, so
    the only correct outcome is one send and an acknowledged row.

    Green from birth — see the section header. The red half is
    ``test_the_pre_guard_adapter_shape_resends_an_already_delivered_notice`` below,
    which runs the pre-fix adapter body against this same wiring.

    Feishu's threaded reply paths have the identical shape and the identical guard;
    they are pinned at adapter level in ``tests/test_im_post_send_bookkeeping.py``
    rather than duplicated here, because the notice-level consumer is
    platform-agnostic once the adapter returns its id.
    """

    from core.delivery_evidence import ACK_EVIDENCE_DELIVERY_ONLY, ACK_EVIDENCE_RECEIPT

    _migrated_state_db()
    sqlite, requests = _store(tmp_path)
    run_id = _threaded_slack_notice(sqlite, requests, "task-bookkeeping")

    controller, _dispatcher, _touched = _live_turn_dispatcher()
    bot, accepted = _slack_bot_with_broken_bookkeeping()
    controller.im_client = bot
    service = _drain_service(tmp_path, controller, sqlite, requests)

    asyncio.run(service._drain_failure_notices())

    assert len(accepted) == 1, f"exactly one send reached the transport: {accepted}"
    assert bot.sessions.calls, "the bookkeeping call must actually have been attempted"
    notice = sqlite.owed_failure_notice(run_id)
    assert notice["state"] == NOTICE_SENT, (
        f"a delivered message whose bookkeeping failed is still delivered: {notice}"
    )
    assert notice["ack_evidence"] in {ACK_EVIDENCE_RECEIPT, ACK_EVIDENCE_DELIVERY_ONLY}
    assert notice["attempts"] == 1

    # And the retry that a lost id would have caused does not happen.
    asyncio.run(service._drain_failure_notices())
    assert len(accepted) == 1, f"an acknowledged notice must never be resent: {accepted}"


def test_the_pre_guard_adapter_shape_resends_an_already_delivered_notice(
    tmp_path: Path,
) -> None:
    """The red half of the pin: the pre-0ebf4c55 body, same wiring, old outcome.

    With the bookkeeping call unguarded the exception replaces the returned ``ts``, so
    the drain sees a rung that raised. Both consequences are asserted, because either
    alone would understate the defect: the notice is NOT acknowledged (it retries and
    will eventually dead-letter a message the user already has), and — now that a
    raising rung continues the walk rather than ending it — the SAME body is pushed
    again onto the next rung. That second half is why finding B's adapter fix and
    finding G's ladder fix ship in one round.

    THE MANDATORY WORKSPACE RUNG IS SUPPRESSED, for isolation. Since the round-14 gate
    every ladder ends with it, and here it would DELIVER — the two Slack rungs raise, the
    walk reaches the workspace rung, ``persist_agent_message`` writes a real row and the
    notice acks on that receipt. Which is the gate's intent working exactly as designed,
    and it would make this test say "the notice was rescued" instead of "the adapter
    destroyed a delivered id". The subject here is the adapter's ordering, so the rescue
    is switched off the same way ``test_an_avibe_rung_does_not_ack_on_a_synthetic_send_id``
    switches it off for its pass 1. What the rescue itself buys is pinned by
    ``test_a_walk_whose_preferred_rungs_all_fail_lands_in_the_workspace_inbox``.
    """

    _migrated_state_db()
    sqlite, requests = _store(tmp_path)
    _task(
        sqlite,
        "task-preguard",
        name="daily report",
        deliver_key="slack::channel::C123::thread::1710000000.000100",
        metadata={
            "created_by": {"caller": {"session_key": "slack::channel::C999::thread::1710000000.000200"}}
        },
    )
    run = requests.enqueue_task_run("task-preguard")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-preguard")

    controller, _dispatcher, _touched = _live_turn_dispatcher()
    inner, _accepted = _slack_bot_with_broken_bookkeeping()
    bot = _PreGuardSlackBot(inner)
    controller.im_client = bot
    service = _drain_service(tmp_path, controller, sqlite, requests)
    service._workspace_notice_session_id = lambda: None

    asyncio.run(service._drain_failure_notices())

    assert len(bot.accepted) == 2, (
        "the pre-guard shape destroys the delivered id, so the walk pushes the same "
        f"notice onto the next rung: {bot.accepted}"
    )
    notice = sqlite.owed_failure_notice(run.id)
    assert notice["state"] == "pending", (
        f"and the row is left owing a notice the user already received: {notice}"
    )
    assert notice["attempts"] == 1
    assert "session store unavailable" in (notice["error"] or "")


def test_a_real_send_failure_is_still_undelivered(tmp_path: Path) -> None:
    """The control, so the guard above cannot be read as "ack on anything".

    The bookkeeping guard only ever runs AFTER the transport returned. A transport
    that raises BEFORE it returns an id never delivered, and must still leave the
    notice unacknowledged with its retry armed — otherwise the guard would have
    converted every failed send into a silent success, which is the failure mode the
    whole durable notice exists to prevent.
    """

    _migrated_state_db()
    sqlite, requests = _store(tmp_path)
    run_id = _threaded_slack_notice(sqlite, requests, "task-send-failure")

    controller, _dispatcher, _touched = _live_turn_dispatcher()
    bot, accepted = _slack_bot_with_broken_bookkeeping(transport_fails=True)
    controller.im_client = bot
    service = _drain_service(tmp_path, controller, sqlite, requests)

    asyncio.run(service._drain_failure_notices())

    assert accepted == [], "nothing reached the platform"
    notice = sqlite.owed_failure_notice(run_id)
    assert notice["state"] == "pending", f"an undelivered notice stays owed: {notice}"
    assert notice["ack_evidence"] in {None, ""}, f"and acks on nothing: {notice}"
    assert notice["attempts"] == 1, "one attempt consumed by the claim"
    assert str(notice["next_attempt_at"] or ""), "with a retry armed"


# =============================================================================
# --- round 14: the MANDATORY workspace rung, on every walk -------------------
# =============================================================================
#
# Subordinate to HFR-079's ladder family (``test_a_workbench_addressed_notice_lands_as_a_durable_inbox_row``,
# ``test_an_avibe_rung_does_not_ack_on_a_synthetic_send_id``,
# ``test_a_caller_less_cli_definition_still_delivers_its_failure_notice``). NO new
# scenario id: the subject is the same ladder those three already own.
#
# WHAT ROUND 12 LEFT OPEN, and it is the #1060 field evidence rather than a
# hypothetical (maintainer note 5120451508). Round 12 gave the ladder a last rung —
# the reserved workspace-notifications session — but gated it on ``if not rungs``,
# "ONLY WHEN NOTHING ELSE RESOLVED", to keep a definition that already has an address
# from collecting a duplicate card. That gate answers the duplicate question and
# leaves the ORTHOGONAL one open: a ladder can be non-empty and yet contain no rung
# that can deliver anything. A watch or task bound to a HARD-DELETED session is
# exactly that shape — rung (5) is manufactured unconditionally from the run's session
# id and nothing checks that the row it names still exists — so every attempt sends to
# a candidate that resolves to nothing, no rung can persist a receipt, and the notice
# burns all six attempts into a silent dead letter. That silence is the 3.5 hours in
# #1060.
#
# ROUND 13 CLOSED THAT WITH A FINAL-ATTEMPT FALLBACK (commit ``ce695b42``): appended
# last, but only on attempt ``MAX_ATTEMPTS``, on the argument that an always-present
# workspace rung converts a TRANSIENT preferred-rung failure into a permanently
# workspace-routed notice. THE ROUND-14 GATE OVERRULED THAT (review comment
# 5121007240): "Workspace fallback is mandatory rung 5. Append one distinct
# workspace-notifications target after every person/context target, even when earlier
# candidates exist but are stale, unavailable, or fail delivery. It cannot be
# conditional on rungs being empty." The transient-misroute trade is accepted
# explicitly by that ruling ("…or fail delivery").
#
# THE RULE THE TESTS BELOW PIN, then, is three claims and not one:
#
# * UNCONDITIONAL — the rung is on every ladder on every attempt, so a walk whose
#   preferred rungs all fail delivers on THAT walk rather than on the sixth
#   (``test_a_walk_whose_preferred_rungs_all_fail_lands_in_the_workspace_inbox``);
# * LAST — a healthy preferred rung still wins every walk and produces no workspace
#   card at all, which is what keeps the unconditional rung from being noise
#   (``test_a_healthy_preferred_rung_never_reaches_the_workspace_inbox``);
# * ONCE — the archived-session reroute names the same reserved id, so the two
#   mechanisms collapse into a single rung
#   (``test_an_archived_session_ladder_holds_exactly_one_workspace_rung``).


def _dead_binding_notice(sqlite, requests, definition_id: str, *, session_id: str = "sesGone"):
    """One owed notice whose ONLY preferred rung names a session row that does not exist.

    No ``deliver_key`` (rung 1 empty), no ``created_by`` provenance (rungs 3 and 4
    empty), and a ``session_id`` whose ``agent_sessions`` row was never written — so
    ``resolve_session_id_target`` refuses it (rung 2 empty) while rung (5) is still
    MANUFACTURED from the same id. A non-empty ladder with zero DELIVERABLE preferred
    rungs: the precondition round 12's ``if not rungs`` fallback could not see.
    """

    from storage.background import NOTICE_PENDING

    _task(sqlite, definition_id, name="nightly report", session_id=session_id)
    run = requests.enqueue_task_run(definition_id)
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id=definition_id)
    assert sqlite.owed_failure_notice(run.id)["state"] == NOTICE_PENDING
    return run.id


def _rewind_notice_backoff(sqlite, run_id: str) -> None:
    """Let the armed backoff elapse without sleeping (the rewind the retry tests use)."""

    sqlite.update_owed_failure_notice(run_id, next_attempt_at=None)
    assert [row["id"] for row in sqlite.list_owed_failure_notices(limit=10)] == [run_id], (
        "the notice has to still be OWED for the next attempt to be able to run"
    )


def _avibe_sends(controller) -> list[str]:
    return [channel for channel, _thread, _text in controller.im_client.sent]


def test_a_walk_whose_preferred_rungs_all_fail_lands_in_the_workspace_inbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Subordinate to HFR-079 — a non-empty ladder of undeliverable rungs must not
    dead-letter in silence, and must not wait six attempts to say so.

    THE DEFECT, traced end to end (maintainer note 5120451508, #1060's field
    evidence). The definition is bound to a session whose row is gone, so the preferred
    ladder is ``["avibe::project::sesGone"]`` — non-empty, because rung (5) is built from
    the run's session id and "nothing here checks that the row it names still exists".
    The rung sends (``AvibeBot.send_message`` mints a synthetic id unconditionally),
    persists nothing (``persist_agent_message`` returns before writing with neither a
    scope nor a session row), and is therefore refused by the workbench class's
    receipt-only ack source. Correct so far, and correctly retryable — but under round
    12's ``if not rungs`` gate the ladder contained nothing else, so all six attempts
    refused and the sixth dead-lettered a notice nobody will ever read.

    THE ATTEMPT THIS LANDS ON IS THE POINT, and it is attempt ONE. Round 13 appended the
    workspace rung on the final attempt only; the round-14 gate (review comment
    5121007240) made it unconditional — "it cannot be conditional on rungs being empty"
    — so the FIRST walk whose preferred rungs all fail delivers and acks. One drain
    pass, ``attempts == 1``, one durable row. The five backoff attempts an undeliverable
    ladder used to burn in silence are gone, not merely made visible at the end.

    THE PREFERRED RUNG IS STILL WALKED FIRST, which is the ordering half: the sends are
    ``[sesGone, workspace]`` in that order, so the workspace rung is a LAST resort inside
    one walk rather than a replacement for the definition's own addressing. That it does
    not fire at all when the preferred rung is healthy is
    ``test_a_healthy_preferred_rung_never_reaches_the_workspace_inbox``.

    Exactly ONE row for this scripted run — not a claim of exactly-once delivery. The
    contract remains at-least-once (see ``CLAIM_LEASE_SECONDS``); what is asserted here
    is that one uninterrupted walk produces one card.
    """

    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID

    _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    # The real schema, and deliberately NO row for ``sesGone`` and no reserved
    # workspace session: both absences are the premise.
    _migrated_state_db()
    assert _workspace_notice_session_rows() == [], (
        "the reserved session must not pre-exist; the walk is what creates it"
    )

    sqlite, requests = _store(tmp_path)
    run_id = _dead_binding_notice(sqlite, requests, "task-dead-binding")

    service = _drain_service(tmp_path, controller, sqlite, requests)
    run_row = sqlite.get_run(run_id)
    # The ladder shape, asserted before any send, so the behaviour below is attributable
    # to the rung ORDER rather than to a lucky walk: the undeliverable preferred rung
    # first, the mandatory workspace rung appended strictly after it.
    assert [
        (target.to_key(), session_id)
        for target, session_id in service._failure_notice_targets(run_row)
    ] == [
        ("avibe::project::sesGone", "sesGone"),
        (
            f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}",
            WORKSPACE_NOTICE_SESSION_ID,
        ),
    ], "the premise: an undeliverable preferred rung, then the mandatory workspace rung"

    # --- ONE drain pass, and the notice is delivered -------------------------
    asyncio.run(service._drain_failure_notices())

    assert _avibe_sends(controller) == ["sesGone", WORKSPACE_NOTICE_SESSION_ID], (
        "the preferred rung must still be tried FIRST; the workspace rung is the last "
        f"resort within the same walk, not a replacement: {_avibe_sends(controller)}"
    )

    rows = _persisted_messages()
    assert [(row["platform"], row["type"], row["session_id"]) for row in rows] == [
        ("avibe", "notify", WORKSPACE_NOTICE_SESSION_ID)
    ], f"the workspace rung must receive exactly one actionable notice: {rows}"
    assert rows[0]["content_text"], "an empty notice is not a notice"
    assert len(_workspace_notice_session_rows()) == 1, (
        "and the reserved row is created lazily, by the walk that reached the rung"
    )

    notice = sqlite.owed_failure_notice(run_id)
    assert notice["state"] == NOTICE_SENT, (
        f"an undeliverable ladder may not be silent: {notice['state']} / {notice.get('error')}"
    )
    assert notice["ack_evidence"] == ACK_EVIDENCE_RECEIPT, (
        "the workbench class acks on the durable receipt and on nothing weaker"
    )
    assert notice["attempts"] == 1, (
        "THE ROUND-14 DELTA: the notice lands on the FIRST walk, not after five silent "
        f"backoff attempts: {notice}"
    )
    # And it is genuinely settled, not merely stamped: a second pass sends nothing.
    asyncio.run(service._drain_failure_notices())
    assert _avibe_sends(controller) == ["sesGone", WORKSPACE_NOTICE_SESSION_ID], (
        f"an acknowledged notice must never be walked again: {_avibe_sends(controller)}"
    )
    assert len(_persisted_messages()) == 1, "and must not grow a second card"


def test_a_healthy_preferred_rung_never_reaches_the_workspace_inbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The ORDERING property, which is what makes the mandatory rung safe to make
    mandatory.

    The round-14 gate appends a workspace rung to EVERY ladder, and round 12's objection
    to that was duplicate cards: a delivered notice's silent per-rung failures turning
    into a second copy in the workspace inbox, plus a reserved row minted on
    installations that never need one. Both are answered by POSITION rather than by a
    condition — the rung is appended strictly LAST and the walk returns on the first rung
    that acknowledges — and this test is the pin for that answer, because nothing else in
    the file asserts the NEGATIVE.

    Two claims, and the second is why the ``_emit_failure_notice`` half of the design is
    lazy:

    * no workspace SEND and no workspace ``messages`` row. The definition's own delivery
      key acks first, so the appended rung is never walked;
    * no reserved ``agent_sessions`` ROW AT ALL. ``_failure_notice_targets`` appends the
      reserved id as a constant and performs no database access; the
      resolve-or-create-or-heal lives in the walk. A build-time resolve would create the
      row on the first failure of every install even though nothing here ever addresses
      it.

    The ladder is asserted too, so this cannot be mistaken for evidence that the rung is
    conditional: it IS on the ladder — the gate says it must be — and it simply loses the
    walk.
    """

    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID

    _no_background_web_push(monkeypatch)
    _migrated_state_db()
    assert _workspace_notice_session_rows() == [], "the premise: no reserved row yet"

    sqlite, requests = _store(tmp_path)
    run_id = _threaded_slack_notice(sqlite, requests, "task-healthy-rung")

    controller, _dispatcher, _touched = _live_turn_dispatcher()
    service = _drain_service(tmp_path, controller, sqlite, requests)

    rungs = service._failure_notice_targets(sqlite.get_run(run_id))
    assert [target.to_key() for target, _ in rungs] == [
        "slack::channel::C123::thread::1710000000.000100",
        f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}",
    ], f"the mandatory rung is present, and it is LAST: {rungs}"

    asyncio.run(service._drain_failure_notices())

    notice = sqlite.owed_failure_notice(run_id)
    assert notice["state"] == NOTICE_SENT, f"the premise: rung (1) delivered: {notice}"

    # --- the negative, which is the whole point ------------------------------
    assert _avibe_sends(controller) == ["C123"], (
        "the walk must return on the acknowledged rung; a healthy delivery may not "
        f"also push a workspace card: {_avibe_sends(controller)}"
    )
    assert WORKSPACE_NOTICE_SESSION_ID not in [
        row["session_id"] for row in _persisted_messages()
    ], (
        "and nothing may be written into the workspace inbox: "
        f"{_persisted_messages()}"
    )
    assert _workspace_notice_session_rows() == [], (
        "nor may the reserved row be MINTED for an install whose rung (1) always "
        "delivers — the ladder names it as a constant and only the WALK resolves it"
    )


def test_an_archived_session_ladder_holds_exactly_one_workspace_rung(
    tmp_path: Path,
) -> None:
    """The COMPOSITION of round 14's two halves, asserted on the address alone.

    Two independent mechanisms now name the reserved workspace session:

    * rung (5)'s archived-session REROUTE (``_rung_five_session_id``), because an
      archived row is writable but invisible and must not be addressed;
    * the MANDATORY appended rung, which is on every ladder unconditionally.

    Both return the same reserved id, so ``_add``'s seen-set — keyed on
    ``(parsed key, session id)`` — collapses them. Without that the ladder would hold TWO
    identical workspace rungs, the walk would deliver on the first and the second would
    be dead weight the ack policy still had to reason about; if the reroute had instead
    returned a resolved-but-different id the ladder would be internally inconsistent.

    Driven with a LIVE preferred rung present as well, so the assertion covers POSITION
    and not only the count: the surviving workspace rung has to sit after the definition's
    own delivery key, not in rung (5)'s slot ahead of nothing.

    Ladder-only, deliberately. Delivery through the rerouted rung is
    ``test_an_archived_ordinary_session_is_rerouted_instead_of_acked_into``; what is
    unasserted anywhere else is the SHAPE, and a drain pass would settle on rung (1) here
    and never inspect it.
    """

    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID

    controller, _dispatcher, _touched = _live_turn_dispatcher()
    _workbench_session("sesArchivedDup", project="proj-archived-dup", status="archived")

    sqlite, requests = _store(tmp_path)
    _task(
        sqlite,
        "task-archived-dup",
        name="nightly report",
        deliver_key="slack::channel::C123::thread::1710000000.000100",
        session_id="sesArchivedDup",
    )
    run = requests.enqueue_task_run("task-archived-dup")
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id="task-archived-dup")

    service = _drain_service(tmp_path, controller, sqlite, requests)
    rungs = service._failure_notice_targets(sqlite.get_run(run.id))

    assert [(target.to_key(), session_id) for target, session_id in rungs] == [
        ("slack::channel::C123::thread::1710000000.000100", None),
        (
            f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}",
            WORKSPACE_NOTICE_SESSION_ID,
        ),
    ], (
        "the reroute and the mandatory append must collapse into ONE rung, positioned "
        f"last: {rungs}"
    )
    assert "avibe::project::sesArchivedDup" not in [
        target.to_key() for target, _ in rungs
    ], f"and the archived row may not be addressed at all: {rungs}"


def test_replaying_the_workspace_walk_reuses_the_persisted_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Subordinate to HFR-079/HFR-075 — restart cannot duplicate the workspace card.

    The maintainer's "restart/replay cannot duplicate that notice", applied to the
    mandatory rung: crash between ``persist_agent_message`` committing the workspace row
    and the owed notice being acknowledged, then re-drive the walk on a fresh service
    instance. The dispatcher's duplicate short-circuit finds the row by its run-derived
    identity, declines to re-send, and reports the found row as a ``receipt``
    (HFR-075) — which is precisely what the workbench class's ack source accepts, so the
    replay settles instead of writing a second card.

    ATTEMPT 1 THROUGHOUT, since round 14. The rung is on every ladder, so the walk that
    persists the row is the FIRST one and the crash rewind puts the notice back to zero
    consumed attempts rather than to ``MAX_ATTEMPTS - 1``. Under the retired
    final-attempt design that rewind had to reconstruct the sixth attempt, because it was
    the only interleaving in which the rung existed at all.

    A PIN, not a red-first case: HFR-075's machinery already makes this green, and the
    rung inherits it because it is an ordinary ``avibe::project::…`` rung with an
    ordinary run-derived ``failure_id``. The red half is the mutation at the end —
    delete the persisted row and the same replay delivers again, which is what the
    short-circuit is being credited with preventing.
    """

    from core.delivery_evidence import ACK_EVIDENCE_RECEIPT
    from sqlalchemy import text as sa_text
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.background import NOTICE_PENDING
    from storage.db import get_cached_sqlite_engine

    _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    _migrated_state_db()

    sqlite, requests = _store(tmp_path)
    run_id = _dead_binding_notice(sqlite, requests, "task-dead-binding-replay")

    service = _drain_service(tmp_path, controller, sqlite, requests)
    asyncio.run(service._drain_failure_notices())
    rows = _persisted_messages()
    assert [row["session_id"] for row in rows] == [WORKSPACE_NOTICE_SESSION_ID], (
        f"the premise: the first walk already persisted the workspace row: {rows}"
    )
    persisted_identity = rows[0]["native_message_id"]

    def _crash_between_persist_and_ack() -> None:
        """The row is committed; the notice never learned about it.

        ``attempts=0`` so the recovered pass claims attempt 1 — the same attempt the
        crashed pass was walking, which is now every attempt's shape.
        """

        sqlite.update_owed_failure_notice(
            run_id,
            state=NOTICE_PENDING,
            attempts=0,
            next_attempt_at=None,
            ack_evidence=None,
            error=None,
        )

    _crash_between_persist_and_ack()

    # A FRESH service and dispatcher: the recovery is a different process, so nothing
    # in-memory from the crashed pass may be what settles it.
    replay_controller, _replay_dispatcher, _replay_touched = _live_turn_dispatcher()
    replay_service = _drain_service(tmp_path, replay_controller, sqlite, requests)
    asyncio.run(replay_service._drain_failure_notices())

    assert [row["native_message_id"] for row in _persisted_messages()] == [
        persisted_identity
    ], "the replay must reuse the committed row, not write a second one"
    assert WORKSPACE_NOTICE_SESSION_ID not in _avibe_sends(replay_controller), (
        "the duplicate short-circuit must run BEFORE the send: "
        f"{_avibe_sends(replay_controller)}"
    )
    notice = sqlite.owed_failure_notice(run_id)
    assert notice["state"] == NOTICE_SENT, f"a found row settles the notice: {notice}"
    assert notice["ack_evidence"] == ACK_EVIDENCE_RECEIPT, (
        "the found row is the receipt the workbench class requires"
    )
    assert notice["attempts"] == 1

    # --- red by mutation: without the committed row, the replay DOES deliver --
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(
            sa_text("DELETE FROM messages WHERE native_message_id = :identity"),
            {"identity": persisted_identity},
        )
    _crash_between_persist_and_ack()
    mutation_controller, _mutation_dispatcher, _mutation_touched = _live_turn_dispatcher()
    mutation_service = _drain_service(tmp_path, mutation_controller, sqlite, requests)
    asyncio.run(mutation_service._drain_failure_notices())

    assert [row["session_id"] for row in _persisted_messages()] == [
        WORKSPACE_NOTICE_SESSION_ID
    ], "with the row gone there is nothing to short-circuit on, so it is written again"
    assert WORKSPACE_NOTICE_SESSION_ID in _avibe_sends(mutation_controller), (
        "and the send happens too — which is exactly what the receipt above prevented"
    )


def test_an_unwritable_workspace_inbox_still_dead_letters_visibly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Subordinate to HFR-079 — the residual, asserted rather than described.

    ``_workspace_notice_session_id`` returns ``None`` when the workbench database cannot
    be read or written. The rung is still APPENDED — the round-14 gate makes that
    unconditional and the ladder never touches the database — so the residual now lives in
    the WALK: ``_emit_failure_notice`` skips the rung on every attempt, the notice spends
    its full backoff on the undeliverable preferred rung, and the sixth attempt
    dead-letters exactly as it did before this change: ``failed``, carrying the reason the
    last usable rung was refused ("without a persisted receipt", not the skipped rung's
    own complaint).

    That path has to stay reachable — a fallback that swallowed the dead letter when it
    could not itself deliver would replace one silence with a worse one. It is also the
    reason ``MAX_ATTEMPTS`` and the backoff survive round 14 untouched: making the rung
    mandatory removes the retry ladder from the cases where it delivers, and leaves it
    exactly where it is still the only thing standing between an unusable workbench DB
    and an unbounded retry loop.
    """

    from core import failure_notices
    from storage.background import NOTICE_FAILED, NOTICE_PENDING

    _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    _migrated_state_db()

    sqlite, requests = _store(tmp_path)
    run_id = _dead_binding_notice(sqlite, requests, "task-dead-binding-unwritable")

    service = _drain_service(tmp_path, controller, sqlite, requests)
    service._workspace_notice_session_id = lambda: None

    for attempt in range(1, failure_notices.MAX_ATTEMPTS):
        asyncio.run(service._drain_failure_notices())
        assert sqlite.owed_failure_notice(run_id)["state"] == NOTICE_PENDING
        _rewind_notice_backoff(sqlite, run_id)

    asyncio.run(service._drain_failure_notices())

    assert _persisted_messages() == [], "there was nowhere to write it"
    notice = sqlite.owed_failure_notice(run_id)
    assert notice["state"] == NOTICE_FAILED, (
        f"an unwritable workbench DB must still dead-letter VISIBLY: {notice}"
    )
    assert notice["attempts"] == failure_notices.MAX_ATTEMPTS
    assert "without a persisted receipt" in (notice["error"] or ""), (
        f"and it must say why the last rung was refused: {notice['error']}"
    )
    # The definition-level surface is unchanged either way, so the dead letter is
    # still discoverable by somebody who goes looking.
    assert sqlite.definition_health("task-dead-binding-unwritable")["health"] == "failing"


# --- group N: the creation origin the notice names -------------------------
#
# Round 14, gate item 3 (review comment 5121007240): "capture the stable creation origin
# needed to render the channel/thread and deep link, then include origin/deep link … in
# owner-DM and workspace notices". Subordinate to HFR-094's notice-body family for the
# copy and to HFR-079's ladder family for the rungs the capture lights up; no new
# scenario id.


_SLACK_ORIGIN_CALLER = {
    "session_id": "sesAgent",
    "run_id": "runAgent",
    "source": "agent_turn",
    "platform": "slack",
    "user_id": "U0AUTHOR",
    "channel_id": "C0123",
    "session_key": "slack::channel::C0123::thread::1710000000.000100",
    "scope_id": "slack::channel::C0123",
    "message_id": "1710000000.000200",
    "workspace_id": "T0999",
}

_SLACK_ORIGIN_PERMALINK = (
    "https://slack.com/archives/C0123/p1710000000000200"
    "?thread_ts=1710000000.000100&cid=C0123"
)


def _StubShell():
    """A bare controller stand-in for the body-only tests (no drain, no delivery)."""

    from types import SimpleNamespace

    return SimpleNamespace()


def _origin_metadata(caller: dict) -> dict:
    """The ``created_by`` envelope ``vibe task add`` writes, verbatim."""

    return {"created_by": {"kind": "caller_context", "caller": dict(caller)}}


def _failed_run(sqlite, requests, definition_id: str) -> str:
    run = requests.enqueue_task_run(definition_id)
    claimed = requests.claim(run.id)
    assert claimed is not None
    requests.complete(claimed, ok=False, error="backend exploded", task_id=definition_id)
    return run.id


def _real_translator(service, language: str):
    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService

    service.controller.config = SimpleNamespace(language=language, platform="slack")
    service._t = ScheduledTaskService._t.__get__(service, ScheduledTaskService)
    return service


@pytest.mark.parametrize("language", ["en", "zh"])
def test_the_notice_names_where_the_definition_was_created_and_links_to_it(
    tmp_path: Path,
    language: str,
) -> None:
    """The last item on D5's body list, and the one a DM needs most.

    A failure notice is CONTEXT-FREE by construction: it may be delivered to an owner DM
    or to the workspace inbox hours after the fire, neither of which is attached to the
    conversation that asked for the definition. So "which Slack thread did I create this
    in" was unanswerable from the notice — the ids were never captured, and no permalink
    builder existed anywhere in the codebase.

    RED at 3578f2b6 (round 11), overlaid with its own local ``_failed_run`` helper so it
    imports nothing newer than that commit: the body was

        ``[Avibe Harness] Scheduled work "nightly report" failed.``
        ``Error: backend exploded``
        ``Definition: task-origin``
        ``Re-run it now with: vibe task run task-origin …``

    — verbatim, in both languages, with no line naming ``C0123``, no thread ts, and no
    URL. (The overlay spells the origin metadata as a literal dict, which round 11
    accepts and ignores: ``run_definitions.metadata_json`` is free-form JSON.)

    Asserted through the REAL translator in BOTH languages because the defect is in
    rendered copy — and the raw platform value must never appear, which is HFR-094's
    lesson applied to a third call site: ``slack`` goes through a closed label map and
    comes out as ``Slack``.
    """

    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    _task(
        sqlite,
        "task-origin",
        name="nightly report",
        metadata=_origin_metadata(_SLACK_ORIGIN_CALLER),
    )
    run_id = _failed_run(sqlite, requests, "task-origin")

    service = _real_translator(_drain_service(tmp_path, _StubShell(), sqlite, requests), language)
    body = service._failure_notice_body(sqlite.get_run(run_id), {"failure_id": run_id})

    # --- the origin line ----------------------------------------------------
    assert i18n_t("harness.notice.origin", language).split("{")[0].strip() in body, (
        f"the body must say where the definition was created: {body}"
    )
    assert i18n_t("harness.notice.platform.slack", language) in body
    assert "C0123" in body, f"named by the captured channel id: {body}"
    assert "1710000000.000100" in body, f"and by the captured thread: {body}"

    # --- the deep link ------------------------------------------------------
    assert _SLACK_ORIGIN_PERMALINK in body, f"with a followable permalink: {body}"
    assert i18n_t("harness.notice.originLink", language).split("{")[0].strip() in body

    # --- and nothing leaked -------------------------------------------------
    assert "harness.notice." not in body, f"no dotted key path: {body}"
    for wire_value in ("slack", "session_key", "workspace_id"):
        assert wire_value not in body.replace(_SLACK_ORIGIN_PERMALINK, ""), (
            f"{wire_value!r} is a wire value, not product copy: {body}"
        )


def test_a_definition_with_no_captured_origin_gets_no_origin_line(tmp_path: Path) -> None:
    """The backfill answer is OMIT, and this is what that costs and what it buys.

    EVERY definition created before the capture landed has no origin — the ids were never
    recorded, and there is no migration that could invent them. A definition created from
    the CLI by hand has none either, legitimately and forever: there is no conversation
    behind it. Both take NO line, which is the same call
    ``notice_failure_class_i18n_key`` makes for a class it cannot name and the same call
    the last-success line makes for a definition that has never succeeded. "Created in:
    unknown" would be copy about nothing on the lane that already carries the most lines.
    """

    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    # A pre-origin definition: metadata exists (round 12 writes a ``created_by`` for CLI
    # callers) but carries no origin fields at all.
    _task(
        sqlite,
        "task-legacy",
        name="nightly report",
        metadata=_origin_metadata({"session_id": "sesAgent", "source": "agent_turn"}),
    )
    run_id = _failed_run(sqlite, requests, "task-legacy")

    service = _real_translator(_drain_service(tmp_path, _StubShell(), sqlite, requests), "en")
    body = service._failure_notice_body(sqlite.get_run(run_id), {"failure_id": run_id})

    assert i18n_t("harness.notice.origin", "en").split("{")[0].strip() not in body, (
        f"no captured origin means no origin line: {body}"
    )
    assert i18n_t("harness.notice.originLink", "en").split("{")[0].strip() not in body

    # And a definition with NO metadata whatsoever behaves identically.
    _task(sqlite, "task-bare", name="nightly report")
    bare_run = _failed_run(sqlite, requests, "task-bare")
    bare_body = service._failure_notice_body(sqlite.get_run(bare_run), {"failure_id": bare_run})
    assert i18n_t("harness.notice.origin", "en").split("{")[0].strip() not in bare_body


@pytest.mark.parametrize(
    "caller,expects_link,why",
    [
        (
            {
                "platform": "lark",
                "user_id": "ou_1",
                "channel_id": "oc_abc",
                "session_key": "lark::channel::oc_abc",
                "message_id": "om_abc",
            },
            False,
            "Feishu/Lark has no public message permalink, so the text stands alone",
        ),
        (
            {
                "platform": "avibe",
                "session_key": "avibe::project::proj-1",
                "channel_id": "proj-1",
                "message_id": "msg-1",
            },
            False,
            "a Workbench origin is addressable only as a localhost URL, which is not "
            "reachable from wherever this notice is read",
        ),
        (
            {
                "platform": "discord",
                "user_id": "111",
                "channel_id": "555",
                "session_key": "discord::user::111",
                "message_id": "777",
            },
            False,
            "a Discord DM has no guild, and ``@me`` would resolve for one user only",
        ),
        (
            {
                "platform": "telegram",
                "user_id": "42",
                "channel_id": "-1001234567890",
                "session_key": "telegram::channel::-1001234567890::thread::7",
                "message_id": "99",
            },
            True,
            "a Telegram supergroup does have a t.me/c link",
        ),
    ],
)
def test_an_origin_that_cannot_be_linked_is_still_named(
    tmp_path: Path,
    caller: dict,
    expects_link: bool,
    why: str,
) -> None:
    """Two independent decisions, and the second failing must not suppress the first.

    ``origin_link`` refuses for three different reasons (no permalink grammar exists; the
    only URL is not reachable from where the notice is read; a required id was not
    captured), and in all three the origin TEXT is still true and still narrows the
    search. Collapsing them — dropping the whole origin because there is no link — would
    make Feishu, WeChat and Workbench definitions permanently anonymous.
    """

    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-origin-nolink", name="nightly report", metadata=_origin_metadata(caller))
    run_id = _failed_run(sqlite, requests, "task-origin-nolink")

    service = _real_translator(_drain_service(tmp_path, _StubShell(), sqlite, requests), "en")
    body = service._failure_notice_body(sqlite.get_run(run_id), {"failure_id": run_id})

    assert i18n_t("harness.notice.origin", "en").split("{")[0].strip() in body, (
        f"the origin is named regardless of linkability ({why}): {body}"
    )
    assert i18n_t(f"harness.notice.platform.{caller['platform']}", "en") in body
    link_line = i18n_t("harness.notice.originLink", "en").split("{")[0].strip()
    assert (link_line in body) is expects_link, f"{why}: {body}"
    if not expects_link:
        assert "http" not in body, f"a refused link must not leak a partial URL: {body}"


def test_an_unmappable_origin_platform_takes_no_line_rather_than_leaking_itself(
    tmp_path: Path,
) -> None:
    """A wire value from a platform this build has never heard of.

    The label is rendered INSIDE a translated sentence, so the only two options are a
    closed map or interpolation — and "Created in: mystery_platform channel X" puts an
    identifier into product copy for no benefit. Same choice, same reasoning, as
    ``notice_failure_class_i18n_key`` returning ``None``.
    """

    from vibe.i18n import t as i18n_t

    sqlite, requests = _store(tmp_path)
    _task(
        sqlite,
        "task-origin-unknown",
        name="nightly report",
        metadata=_origin_metadata(
            {
                "platform": "mystery_platform",
                "user_id": "U1",
                "channel_id": "C1",
                "session_key": "mystery_platform::channel::C1",
                "message_id": "m1",
            }
        ),
    )
    run_id = _failed_run(sqlite, requests, "task-origin-unknown")

    service = _real_translator(_drain_service(tmp_path, _StubShell(), sqlite, requests), "en")
    body = service._failure_notice_body(sqlite.get_run(run_id), {"failure_id": run_id})

    assert "mystery_platform" not in body, f"the wire value must not reach copy: {body}"
    assert i18n_t("harness.notice.origin", "en").split("{")[0].strip() not in body, (
        f"and an unnameable platform takes no line at all: {body}"
    )


def test_a_captured_origin_lights_up_the_scope_and_owner_dm_rungs(tmp_path: Path) -> None:
    """D5's designed ladder shape, finally REACHABLE.

    Rungs (3) and (4) have always read ``caller["session_key"]`` / ``caller["scope_id"]``
    and ``caller["platform"]`` / ``caller["user_id"]`` — fields NOTHING in the codebase
    had ever written. They were dead code: every ladder skipped straight from the bound
    session to the workbench rung, and the owner DM the plan specifies had never once
    fired. The origin capture is what lights them up, and this is the consequence to own
    loudly rather than discover in the field: for newly created definitions an owner DM
    is now a real delivery attempt.

    NO RED OF ITS OWN, stated plainly because the honest answer is more useful than a
    manufactured one. Overlaid at 3578f2b6 (round 11) with the same hand-written
    ``created_by`` metadata, the ladder came back as

        ``[('slack::channel::C999', None),
           ('slack::channel::C0123::thread::1710000000.000100', None),
           ('slack::user::U0AUTHOR', None)]``

    — rungs (3) and (4) already resolved correctly. The reading logic was never the
    defect; nothing in the product ever WROTE those fields, which is what
    ``tests/test_caller_context.py``'s capture test is red on at that same commit. The
    only assertion below that would fail at 3578f2b6 is the trailing workspace rung, and
    that belongs to round 14's always-append change, not to this one. So this is a
    consequence test: it pins that the capture makes D5's designed shape REACHABLE, and
    it is the test that will fail if a future change to the origin key set silently
    unlights the owner DM again.

    ORDER is asserted, not just membership: the definition's own delivery key first, then
    the creating conversation, then the owner DM, then the workbench rung, and the
    reserved workspace rung last on every ladder (round-14 always-append). A DM that
    preceded the definition's own channel would move a routine failure notice out of the
    shared conversation the team watches and into one person's inbox.
    """

    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID

    sqlite, requests = _store(tmp_path)
    _task(
        sqlite,
        "task-origin-ladder",
        name="nightly report",
        deliver_key="slack::channel::C999",
        metadata=_origin_metadata(_SLACK_ORIGIN_CALLER),
    )
    run_id = _failed_run(sqlite, requests, "task-origin-ladder")

    service = _drain_service(tmp_path, _StubShell(), sqlite, requests)
    rungs = [
        (target.to_key(), session_id)
        for target, session_id in service._failure_notice_targets(sqlite.get_run(run_id))
    ]

    assert rungs == [
        ("slack::channel::C999", None),
        ("slack::channel::C0123::thread::1710000000.000100", None),
        ("slack::user::U0AUTHOR", None),
        (f"avibe::project::{WORKSPACE_NOTICE_SESSION_ID}", WORKSPACE_NOTICE_SESSION_ID),
    ], f"the captured origin must produce rungs (3) and (4), in that order: {rungs}"

    # The five-part origin key is delivered to as a THREAD, not downgraded to its parent
    # channel — ``scope_id`` is present in the same caller and is deliberately NOT the
    # one rung (3) prefers, because retargeting a thread notice at its channel would
    # broadcast it to everyone in the channel instead.
    assert "slack::channel::C0123" not in [key for key, _ in rungs]


def test_the_workspace_card_carries_the_creation_origin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The half of the gate that is about the WORKSPACE notice, not the owner DM.

    RED at the CURRENT HEAD (d2c3bf31), pre-change — not at 3578f2b6, because the
    workspace-notifications rung, its reserved session and its receipt-only ack policy are
    all round-12-and-later symbols this test imports. At HEAD the card was persisted with
    a body that named no origin and carried no URL; the red is the absence of the origin
    line in ``content_text``, and it is the same absence the ``_failure_notice_body``
    tests above prove against the older baseline.

    THE SCENARIO IS THE POINT, and it is the one where the origin matters most: a
    definition created in a Slack thread, in an install whose Slack transport is no longer
    configured. ``validate_platform`` refuses, so ``_build_context`` raises and BOTH origin
    rungs are unusable — the disposal every unusable rung gets. The workspace card is then
    the only place the failure surfaces, and it is the only place the user can learn which
    Slack thread to go look at. Without the origin the card names a definition id and a
    dead end.
    """

    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
    from storage.background import NOTICE_SENT
    from vibe.i18n import t as i18n_t

    _no_background_web_push(monkeypatch)
    controller, _dispatcher, _touched = _live_turn_dispatcher()
    _migrated_state_db()

    sqlite, requests = _store(tmp_path)
    _task(
        sqlite,
        "task-origin-workspace",
        name="nightly report",
        deliver_key="slack::channel::C0123::thread::1710000000.000100",
        metadata=_origin_metadata(_SLACK_ORIGIN_CALLER),
    )
    run_id = _failed_run(sqlite, requests, "task-origin-workspace")

    service = _real_translator(_drain_service(tmp_path, controller, sqlite, requests), "en")

    def _no_slack_transport(platform: str) -> None:
        if platform == "slack":
            raise ValueError("unsupported task platform: slack")

    service.validate_platform = _no_slack_transport

    asyncio.run(service._drain_failure_notices())

    rows = _persisted_messages()
    assert [(row["type"], row["session_id"]) for row in rows] == [
        ("notify", WORKSPACE_NOTICE_SESSION_ID)
    ], f"the premise: only the workspace rung could be delivered to: {rows}"
    assert sqlite.owed_failure_notice(run_id)["state"] == NOTICE_SENT

    card = rows[0]["content_text"]
    assert i18n_t("harness.notice.origin", "en").split("{")[0].strip() in card, (
        f"the workspace card must name the conversation it came from: {card}"
    )
    assert "C0123" in card and "1710000000.000100" in card
    assert _SLACK_ORIGIN_PERMALINK in card, (
        f"and give the user a way back to it: {card}"
    )


# --- command-task notice copy (tests/scenarios/harness_command_task) -------
#
# A command definition runs a subprocess and never prompts an Agent, so its failure
# notice was the only place the user could learn a scheduled command broke — and it
# named the definition and the error while never naming the COMMAND. These tests pin
# the failed-lane copy (command line before the error, exit code after it, and only
# when the row carries one) and the two closed-loop scenarios SCT-001/SCT-002.


def _command_task(sqlite, definition_id: str, **overrides) -> None:
    """One command definition: no prompt, a command, and a rung-(1) delivery key."""

    payload = {
        "name": definition_id,
        "prompt": "",
        "deliver_key": "slack::channel::C1",
        "shell_command": "echo boom >&2; exit 7",
    }
    payload.update(overrides)
    _task(sqlite, definition_id, **payload)


def _command_run(definition_id: str, run_id: str = "run-cmd", **overrides) -> dict:
    """The run row shape a settled command fire leaves behind."""

    run = {
        "id": run_id,
        "task_id": definition_id,
        "error": "command exited with status 7: boom",
        "exit_code": 7,
        "stdout": "",
        "stderr": "boom\n",
    }
    run.update(overrides)
    return run


def _notice_prefix(key: str, language: str = "en") -> str:
    from vibe.i18n import t as i18n_t

    return i18n_t(key, language).split("{")[0].strip()


@pytest.mark.parametrize("language", ["en", "zh"])
def test_a_failed_command_notice_names_the_command_then_the_error_then_the_exit_code(
    tmp_path: Path,
    language: str,
) -> None:
    """The failed lane's command copy, in the order a reader needs it.

    WHAT it ran, WHY it failed, and the exit status it failed with. Order is asserted
    rather than mere membership: the command has to arrive before the error it explains,
    and the exit code after it, or the three lines read as unrelated facts.

    Both languages, through the REAL translator, for the same reason the failure-class
    test does it: a ``_t`` that echoes keys proves which key was chosen and says nothing
    about whether the sentence a user reads is translated.
    """

    from types import SimpleNamespace

    sqlite, requests = _store(tmp_path)
    _command_task(sqlite, "task-cmd")

    service = _real_translator(
        _drain_service(tmp_path, SimpleNamespace(), sqlite, requests), language
    )
    body = service._failure_notice_body(
        _command_run("task-cmd"), {"failure_id": "run-cmd", "interrupt_reason": None}
    )

    command_line = _notice_prefix("harness.notice.commandLine", language)
    error_line = _notice_prefix("harness.notice.error", language)
    exit_line = _notice_prefix("harness.notice.commandExit", language)

    assert "echo boom >&2; exit 7" in body, f"the notice must name the command: {body}"
    assert command_line in body and exit_line in body, (
        f"both command lines must be rendered through localized copy: {body}"
    )
    assert "7" in body.split(exit_line)[1].splitlines()[0], (
        f"the exit line must carry the exit code: {body}"
    )
    assert body.index(command_line) < body.index(error_line) < body.index(exit_line), (
        f"command, then error, then exit code: {body}"
    )
    assert "harness.notice." not in body, f"never a dotted key path: {body}"


def test_an_argv_command_notice_renders_the_shell_joined_argv(tmp_path: Path) -> None:
    """The ``-- argv`` form has no shell string, so the preview joins the list.

    ``str(list)`` would print Python repr quoting at a user; ``shlex.join`` prints
    something the user can paste back into a shell, which is the same choice
    ``vibe task list`` already made.
    """

    from types import SimpleNamespace

    sqlite, requests = _store(tmp_path)
    _command_task(
        sqlite,
        "task-argv",
        shell_command=None,
        command=["/bin/sh", "-c", "echo hello world"],
    )

    service = _real_translator(
        _drain_service(tmp_path, SimpleNamespace(), sqlite, requests), "en"
    )
    body = service._failure_notice_body(
        _command_run("task-argv"), {"failure_id": "run-cmd", "interrupt_reason": None}
    )

    assert "/bin/sh -c 'echo hello world'" in body, (
        f"the argv preview must be shell-joined, not a Python list: {body}"
    )
    assert "['/bin/sh'" not in body, f"and never a repr: {body}"


def test_a_notice_names_the_command_the_run_actually_executed(tmp_path: Path) -> None:
    """SCT-019 -- the command copy comes from the RUN, not from the live definition.

    The notice drain is asynchronous and the definition is editable: between the fire
    settling and the notice going out, the user can rewrite the command or delete the
    task. Composing the line from ``get_task`` therefore reported the REPLACEMENT
    command as the thing that failed -- and after a delete, dropped the line entirely --
    while the run row itself kept only output and an exit code and could not say what
    had run. Either way the user debugs the wrong command, or none.

    So the fire snapshots its own command onto the run, and every later reader uses the
    snapshot. The live definition stays the fallback for rows written before the
    snapshot existed.
    """

    from types import SimpleNamespace

    sqlite, requests = _store(tmp_path)
    # The definition as it is NOW: already rewritten since the fire.
    _command_task(sqlite, "task-edited", shell_command="./scripts/backup-v2.sh --fast")

    service = _real_translator(
        _drain_service(tmp_path, SimpleNamespace(), sqlite, requests), "en"
    )
    body = service._failure_notice_body(
        _command_run(
            "task-edited",
            metadata={"command": {"shell": "./scripts/backup.sh", "argv": []}},
        ),
        {"failure_id": "run-cmd", "interrupt_reason": None},
    )

    assert "./scripts/backup.sh" in body, (
        f"the notice must name the command the run actually executed: {body}"
    )
    assert "backup-v2" not in body, (
        f"and never the replacement the user has since configured: {body}"
    )

    # And the harder half of the same defect: once the definition is DELETED,
    # ``get_task`` has no answer at all, so the command line vanished from exactly the
    # notice that needs it most -- a task the user can no longer inspect.
    deleted = service._failure_notice_body(
        _command_run(
            "task-gone",
            metadata={"command": {"shell": None, "argv": ["/bin/sh", "-c", "echo hi there"]}},
        ),
        {"failure_id": "run-cmd", "interrupt_reason": None},
    )

    assert "/bin/sh -c 'echo hi there'" in deleted, (
        f"a removed definition's run still names its command, shell-joined: {deleted}"
    )


def test_a_command_that_never_spawned_carries_no_exit_code_line(tmp_path: Path) -> None:
    """No exit code exists, so no exit line — the row is the only source of truth.

    A missing working directory is refused BEFORE the spawn, so the run settles with
    ``exit_code`` NULL. "Exit code: None", or a fabricated 0, would be a lie on the one
    surface that has to be trusted. The command line still renders: the user still needs
    to know which definition's command could not be run.
    """

    from types import SimpleNamespace

    sqlite, requests = _store(tmp_path)
    _command_task(sqlite, "task-nospawn")

    service = _real_translator(
        _drain_service(tmp_path, SimpleNamespace(), sqlite, requests), "en"
    )
    body = service._failure_notice_body(
        _command_run(
            "task-nospawn",
            error="working directory does not exist: /nope/gone",
            exit_code=None,
        ),
        {"failure_id": "run-cmd", "interrupt_reason": None},
    )

    assert _notice_prefix("harness.notice.commandLine") in body, (
        f"a no-spawn failure still names its command: {body}"
    )
    assert "/nope/gone" in body, f"and still carries the error: {body}"
    assert _notice_prefix("harness.notice.commandExit") not in body, (
        f"a run with no exit code must get no exit line: {body}"
    )
    assert "None" not in body, f"and must never render the missing value: {body}"


def test_a_long_command_is_truncated_in_the_notice(tmp_path: Path) -> None:
    """A notice is a chat message: an uncapped pipeline would bury the error line.

    Same 120-char cap and trailing ellipsis as the CLI preview, so the notice and
    ``vibe task list`` show the same string for the same definition.
    """

    from types import SimpleNamespace

    sqlite, requests = _store(tmp_path)
    long_command = "echo " + ("x" * 200)
    _command_task(sqlite, "task-long", shell_command=long_command)

    service = _real_translator(
        _drain_service(tmp_path, SimpleNamespace(), sqlite, requests), "en"
    )
    body = service._failure_notice_body(
        _command_run("task-long"), {"failure_id": "run-cmd", "interrupt_reason": None}
    )

    rendered = [line for line in body.splitlines() if line.startswith(_notice_prefix("harness.notice.commandLine"))]
    assert len(rendered) == 1, f"exactly one command line: {body}"
    assert long_command not in body, f"the full command must not be rendered: {body}"
    assert "…" in rendered[0], f"a truncated preview must say so: {rendered[0]}"
    preview = rendered[0].split(":", 1)[1].strip()
    assert len(preview) == 120, f"the cap is 120 characters: {len(preview)}"


def test_a_message_task_notice_carries_no_command_copy(tmp_path: Path) -> None:
    """The regression guard: an Agent-prompting task's notice is unchanged.

    A message task has no command, so neither line may appear — not with an empty
    value, and not at all.
    """

    from types import SimpleNamespace

    sqlite, requests = _store(tmp_path)
    _task(sqlite, "task-message", name="daily report", deliver_key="slack::channel::C1")

    service = _real_translator(
        _drain_service(tmp_path, SimpleNamespace(), sqlite, requests), "en"
    )
    body = service._failure_notice_body(
        {"id": "run-msg", "task_id": "task-message", "error": "backend exploded"},
        {"failure_id": "run-msg", "interrupt_reason": None},
    )

    assert _notice_prefix("harness.notice.commandLine") not in body, (
        f"a message task's notice must carry no command line: {body}"
    )
    assert _notice_prefix("harness.notice.commandExit") not in body, (
        f"nor an exit line: {body}"
    )
    assert _notice_prefix("harness.notice.error") in body, f"the error line stays: {body}"


def test_an_interrupted_command_run_keeps_the_generic_interruption_copy(
    tmp_path: Path,
) -> None:
    """An interrupted command run is a fact about the SERVICE, not about the command.

    The interrupted headline already says nothing is wrong with the definition itself;
    printing the command and an exit code beside it would invite the user to go and
    debug a command that was never given the chance to finish. So the command copy is
    gated on the FAILED lane by the same predicate the rest of the body uses.
    """

    from types import SimpleNamespace

    import core.failure_notices as failure_notices

    sqlite, requests = _store(tmp_path)
    _command_task(sqlite, "task-cmd-interrupted")

    notice = {"failure_id": "run-cmd", "interrupt_reason": "restarted"}
    assert failure_notices.is_interruption(notice), (
        "the premise: this reason belongs to the interrupted lane"
    )

    service = _real_translator(
        _drain_service(tmp_path, SimpleNamespace(), sqlite, requests), "en"
    )
    body = service._failure_notice_body(
        _command_run(
            "task-cmd-interrupted",
            error="the service restarted",
            exit_code=None,
        ),
        notice,
    )

    assert _notice_prefix("harness.notice.commandLine") not in body, (
        f"the interrupted lane keeps generic copy: {body}"
    )
    assert _notice_prefix("harness.notice.commandExit") not in body, (
        f"including no exit line: {body}"
    )


def _command_fire_service(tmp_path: Path, sqlite, requests):
    """A REAL ``ScheduledTaskService`` over tmp-path stores that can fire a command.

    Built through ``__init__`` rather than ``__new__``, because these two scenarios are
    about the executor and the drain being CONNECTED: a fake settle would prove the copy
    and nothing about whether a real command fire reaches it. Only the scheduler and the
    two ownership guards are stubbed — the parts that would otherwise need a live
    APScheduler and a service lease.
    """

    from types import SimpleNamespace

    from core.scheduled_tasks import ScheduledTaskService, ScheduledTaskStore

    store = ScheduledTaskStore(tmp_path / "scheduled_tasks.json")
    store._sqlite = sqlite
    store.load()

    controller = SimpleNamespace(platform_settings_managers={}, session_turn_gate=None)
    service = ScheduledTaskService(
        controller=controller, store=store, request_store=requests
    )
    service.scheduler = SimpleNamespace(
        get_job=lambda *_a, **_kw: None,
        add_job=lambda *_a, **_kw: None,
        remove_job=lambda *_a, **_kw: None,
        get_jobs=lambda *_a, **_kw: [],
        running=True,
    )
    service._owns_service_instance = lambda: True
    service.validate_platform = lambda platform: None
    return service


def _fire_command_task(service, definition_id: str) -> dict:
    """Run one real fire of *definition_id* through the claimed-request path."""

    task = service.store.get_task(definition_id)
    assert task is not None and task.has_command, "the premise: a command definition"
    queued = service.request_store.enqueue_task_run(
        definition_id, source_kind="scheduler", task=task
    )
    claimed = service.request_store.claim(queued.id)
    assert claimed is not None
    asyncio.run(service._execute_claimed_request(claimed))
    run = service.request_store.get_run(queued.id)
    assert run is not None
    return run


def _capture_notice_emissions(monkeypatch) -> list[dict]:
    """Stub rung (1) delivery and record every notice body it is handed."""

    import core.scheduled_tasks as scheduled_tasks

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
    return emitted


def test_a_failed_command_fire_delivers_exactly_one_command_aware_notice(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-001 — closed loop: a real failing command fire, one command-aware notice.

    Everything real except the outbound send: the subprocess actually runs and exits
    nonzero, ``_execute_claimed_request`` settles the row through the executor's own
    path (stamping ``exit_code``/``stderr`` and the owed notice), and
    ``_drain_failure_notices`` walks the ladder to the definition's delivery key. ONE
    notice, because the streak machinery is keyed on the definition and a second pass
    must see the acknowledgement.
    """

    sqlite, requests = _store(tmp_path)
    _command_task(
        sqlite,
        "task-sct-001",
        name="nightly sync",
        shell_command="echo boom >&2; exit 7",
        cwd=str(tmp_path),
        timeout_seconds=30,
    )

    emitted = _capture_notice_emissions(monkeypatch)
    service = _real_translator(_command_fire_service(tmp_path, sqlite, requests), "en")

    run = _fire_command_task(service, "task-sct-001")
    assert run["status"] == "failed", f"the premise: the fire failed: {run['status']!r}"
    assert run["exit_code"] == 7, f"the row must carry the exit code: {run['exit_code']!r}"

    asyncio.run(service._drain_failure_notices())

    assert len(emitted) == 1, f"exactly one notice for one failed fire: {emitted}"
    assert (emitted[0]["platform"], emitted[0]["channel_id"]) == ("slack", "C1")

    body = emitted[0]["body"]
    assert "echo boom >&2; exit 7" in body, f"the delivered notice must name the command: {body}"
    assert _notice_prefix("harness.notice.commandLine") in body, body
    assert "7" in body.split(_notice_prefix("harness.notice.commandExit"))[1].splitlines()[0], (
        f"and the exit code the fire actually produced: {body}"
    )

    emitted.clear()
    asyncio.run(service._drain_failure_notices())
    assert emitted == [], "an acknowledged notice must never be delivered twice"


def test_a_successful_command_fire_notifies_nowhere(tmp_path: Path, monkeypatch) -> None:
    """SCT-002 — silent success is the contract, not an omission.

    ``--on-failure none`` is cron parity: a command task that succeeds is visible in
    ``vibe task show`` (``last_exit_code``) and ``vibe runs show`` (stdout) and NOWHERE
    else. So the guard is two-sided — nothing is owed, and the drain sends nothing —
    because a success notification would turn every minute-ly command task into a
    notification firehose.
    """

    sqlite, requests = _store(tmp_path)
    _command_task(
        sqlite,
        "task-sct-002",
        name="nightly sync",
        shell_command="echo fine",
        cwd=str(tmp_path),
    )

    emitted = _capture_notice_emissions(monkeypatch)
    service = _real_translator(_command_fire_service(tmp_path, sqlite, requests), "en")

    run = _fire_command_task(service, "task-sct-002")
    assert run["status"] == "succeeded", f"the premise: the fire succeeded: {run['error']!r}"
    assert run["exit_code"] == 0

    assert sqlite.owed_failure_notice(run["id"]) is None, (
        "a succeeded command run must owe no notice"
    )
    assert sqlite.list_owed_failure_notices() == [], (
        "and must not appear in the drain's work list"
    )

    asyncio.run(service._drain_failure_notices())
    assert emitted == [], f"a successful command fire must notify nobody: {emitted}"


def test_an_escalated_command_failure_is_never_delivered_a_notice(
    tmp_path: Path, monkeypatch
) -> None:
    """SCT-003, drain half — the escalation IS the report, so the notice is suppressed.

    A ``--on-failure agent`` fire queues one Agent turn carrying the failure report, in
    the same transaction that stamps its definition. A notice as well would be the same
    failure told twice — once as a turn the Agent acts on, once as an alert the user has
    to. Asserted at the DRAIN, not just at the stamp: nothing must be owed, the row must
    not appear in the drain's work list, and a real drain pass must send nothing.

    The regression half is the second run: the identical failure WITHOUT the marker
    still owes and still delivers, so the suppression cannot silence anything else.
    """

    sqlite, requests = _store(tmp_path)
    _command_task(sqlite, "task-sct-003", name="nightly sync")

    escalated = requests.enqueue_task_run("task-sct-003", source_kind="scheduler")
    claimed = requests.claim(escalated.id)
    assert claimed is not None
    requests.complete(
        claimed,
        ok=False,
        error="command exited with status 7: boom",
        task_id="task-sct-003",
        exit_code=7,
        stderr="boom\n",
        escalation_run_id="esc-sct-003",
    )

    row = sqlite.get_run(escalated.id)
    assert row is not None and row["status"] == "failed"
    assert row["metadata"].get("escalation_run_id") == "esc-sct-003"
    assert sqlite.owed_failure_notice(escalated.id) is None, (
        "an escalated failure owes a notice as well; the report would arrive twice"
    )
    assert [item["id"] for item in sqlite.list_owed_failure_notices()] == [], (
        "and it reached the drain's work list anyway"
    )

    emitted = _capture_notice_emissions(monkeypatch)
    from types import SimpleNamespace

    service = _real_translator(
        _drain_service(tmp_path, SimpleNamespace(), sqlite, requests), "en"
    )
    asyncio.run(service._drain_failure_notices())
    assert emitted == [], f"an escalated failure was delivered a notice: {emitted}"

    plain = requests.enqueue_task_run("task-sct-003", source_kind="scheduler")
    plain_claimed = requests.claim(plain.id)
    assert plain_claimed is not None
    requests.complete(
        plain_claimed,
        ok=False,
        error="command exited with status 7: boom",
        task_id="task-sct-003",
        exit_code=7,
        stderr="boom\n",
    )
    notice = sqlite.owed_failure_notice(plain.id)
    assert notice is not None and notice.get("state"), (
        "the suppression leaked to un-escalated failures; every failed command fire "
        "would go silent"
    )
