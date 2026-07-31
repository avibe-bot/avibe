from __future__ import annotations

import json
import logging
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence, TypeVar
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import (
    Integer,
    Text,
    and_,
    case,
    cast,
    exists,
    func,
    insert,
    literal,
    literal_column,
    or_,
    select,
    tuple_,
    update,
)

from config import paths
from config.platform_registry import PLATFORM_REGISTRY
from storage.agent_session_rows import (
    INBOX_SESSION_VISIBILITIES,
    reserve_write_lock,
    session_openable_in_chat,
    unchanged_text,
)
from storage.db import SqliteInvalidationProbe, create_sqlite_engine
from storage.migrations import (
    background_tables_ready,
    ensure_background_indexes,
    guard_source_checkout_default_state_migration,
    initialize_background_tables,
)
from storage.models import agent_runs, agent_sessions, messages, run_definitions, scopes
from storage.pagination import PageRequest, PageResult, page_result_from_limit_plus_one
from storage.sqlite_semantics import sqlite_cast_integer
from storage.session_reclaim import SESSION_SETTINGS_SNAPSHOT_KEY

logger = logging.getLogger(__name__)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_run_at(run_at: str, timezone_name: Optional[str]) -> datetime:
    """The instant a one-shot ``run_at`` names, in the task's own timezone.

    A ``run_at`` with no UTC offset is not an instant — it is a wall-clock
    reading, and something has to say which zone to read it in. The task
    carries a ``timezone`` for exactly that, so that is the answer; resolving
    it any other way makes when a task fires depend on the machine.

    ``datetime.astimezone()`` is the other way, and it is the wrong one: on a
    naive value it silently assumes the *host* zone first. The scheduler used
    it while the payload used this rule, so the two disagreed by the offset
    between host and task zone and the UI promised a fire time the scheduler
    would not honour. One resolver, imported by both, is why that cannot come
    back — the scheduler at ``core/scheduled_tasks.py::_build_trigger`` and
    ``compute_next_run_at`` below are its two callers.
    """

    tz = ZoneInfo(timezone_name or "UTC")
    instant = datetime.fromisoformat(run_at)
    return instant.replace(tzinfo=tz) if instant.tzinfo is None else instant.astimezone(tz)


def compute_next_run_at(
    *,
    enabled: bool,
    schedule_type: Optional[str],
    cron: Optional[str],
    run_at: Optional[str],
    timezone_name: Optional[str],
) -> Optional[str]:
    """Next fire time (tz-aware ISO) for a scheduled task, or None.

    Shared by the harness API payload and the CLI so the two never drift. A
    disabled task, an unparseable schedule, or an ``at`` task whose time has
    already passed all yield ``None``.
    """
    if not enabled:
        return None
    try:
        tz = ZoneInfo(timezone_name or "UTC")
        now = datetime.now(tz)
        if schedule_type == "cron":
            if not cron:
                return None
            trigger = CronTrigger.from_crontab(cron, timezone=tz)
        elif schedule_type == "at":
            if not run_at:
                return None
            instant = resolve_run_at(run_at, timezone_name)
            if instant <= now:
                # A one-shot whose time has already passed has no next run.
                return None
            trigger = DateTrigger(run_date=instant)
        else:
            return None
        next_fire = trigger.get_next_fire_time(None, now)
        return next_fire.isoformat() if next_fire else None
    except Exception:
        return None


RUN_STATUS_ALIASES: dict[str, str] = {
    "pending": "queued",
    "queued": "queued",
    "processing": "running",
    "running": "running",
    "completed": "succeeded",
    "succeeded": "succeeded",
    "failed": "failed",
    "canceled": "canceled",
}

#: Raw ``agent_runs.status`` values that are NOT yet terminal — the rows something
#: may still write, so a fork, an archive or a session teardown has to account for
#: them rather than treat them as history.
#:
#: DERIVED from ``RUN_STATUS_ALIASES`` instead of retyped, because the raw column
#: carries two spellings of each live state (``pending``/``queued``,
#: ``processing``/``running``) and every hand-written copy of this tuple had to
#: remember both. There were three copies; a live status added to the alias map now
#: reaches all of them at once instead of leaving one caller silently reading a
#: running run as finished.
NON_TERMINAL_RUN_STATUSES: tuple[str, ...] = tuple(
    status for status, canonical in RUN_STATUS_ALIASES.items() if canonical in {"queued", "running"}
)
_LIKE_ESCAPE = "\\"
# What a task/watch is *doing*, which is not what ``enabled`` records.
#
# ``enabled`` is a switch: it says whether the scheduler may fire the row, and
# nothing else. Reading it as a state made two different things look identical —
# a one-shot watch that finished on its own and a watch the user paused both
# store ``enabled = 0`` — and left the states users actually ask about
# ("what is running right now?", "what is still waiting?") unnameable. These
# four are derived per row from columns that already exist; no migration.
DEFINITION_LIFECYCLE_STATES = ("running", "waiting", "paused", "finished")
DEFINITION_STATUS_COUNTS = ("total",) + DEFINITION_LIFECYCLE_STATES
# The status filters the API accepts, and which states each one selects. An
# empty tuple means "no restriction". ``active`` is the default view: waiting and
# running are one question ("is this thing still live?"), and the row itself says
# which of the two it is, so it is a filter value without being a count key.
DEFINITION_STATUS_FILTERS: dict[str, tuple[str, ...]] = {
    "all": (),
    "active": ("waiting", "running"),
    "running": ("running",),
    "waiting": ("waiting",),
    "paused": ("paused",),
    "finished": ("finished",),
}
RUN_STATUS_COUNTS = ("all", "queued", "running", "succeeded", "failed", "canceled")
# run_definitions.definition_type -> the user-facing kind the UI routes on.
# The column says "scheduled"; every surface calls that thing a task.
_DEFINITION_KINDS = {"scheduled": "task", "watch": "watch"}
_BLANK_DEFINITION_SUMMARY: dict[str, Any] = {
    "definition_name": None,
    "definition_kind": None,
    "definition_deleted": False,
}
# Where a definition's session binding hides when it has no ``session_id``: a
# legacy IM binding, then a ``create_per_run`` delivery target. Precedence order.
_DEFINITION_SESSION_KEY_FIELDS = ("session_key", "deliver_key")
# The exit code a waiter that ran out of lifetime carries. Written by
# ``core/watches.py`` (the ``timeout`` convention), read here to tell an ending
# that ran out of time from one that failed.
_TIMEOUT_EXIT_CODE = 124
# The runs a definition's own executions are recorded as. A watch's supervisor
# heartbeat is *also* an ``agent_runs`` row and is ``running`` for as long as the
# waiter lives, so counting it as an execution would make every healthy waiter
# read as "running" and leave "waiting" unreachable. Waiter liveness is a
# separate field (``process_alive``), not a state.
_WATCH_RUNTIME_RUN_TYPE = "watch_runtime"
# SQLite caps how many parameters one statement may bind (999 on builds before
# 3.32). Paged lookups stay far under it, but the unpaged harness lists resolve
# every row in the store at once, so batch resolvers chunk their id lists: a few
# queries on a large store instead of one that fails.
#
# The cap counts *bound parameters*, not values. A resolver that binds one
# parameter per value can take 400 of them; one that matches on a three-column
# tuple binds three, so 400 values would be 1200 parameters and the same "too
# many SQL variables" error the batching exists to prevent. Callers declare
# their cost via ``params_per_value`` and the chunk size follows from it.
_MAX_BOUND_PARAMS = 400
# Ids here are usually strings, but a resolver keyed on a composite (platform,
# scope_type, native_id) batches tuples through the same helper.
_BatchValue = TypeVar("_BatchValue")


def _id_batches(values: Iterable[_BatchValue], *, params_per_value: int = 1) -> list[list[_BatchValue]]:
    """De-duplicated, non-empty ids in chunks small enough to bind."""

    size = max(1, _MAX_BOUND_PARAMS // max(1, params_per_value))
    ids = [value for value in dict.fromkeys(values) if value]
    return [ids[start : start + size] for start in range(0, len(ids), size)]


class DefinitionWriteConflict(RuntimeError):
    """A full-row definition write lost to a concurrent lifecycle/binding change.

    Raised by the callers that OWN a user action (``vibe task update``,
    ``vibe watch update``, pause/resume), never swallowed: the write did not
    happen, so reporting the mutated in-memory task back to the user would claim
    an edit the database refused.
    """

    def __init__(self, definition_id: str, *, definition_type: str = "definition") -> None:
        super().__init__(
            f"{definition_type} {definition_id} changed underneath this update "
            "(its Session binding, enabled state, deletion or reclaim snapshot moved); "
            "nothing was written"
        )
        self.definition_id = str(definition_id)
        self.definition_type = str(definition_type)


@dataclass(frozen=True)
class DefinitionWriteExpectation:
    """The definition state a FULL-ROW payload was derived from.

    ``upsert_scheduled_task`` / ``upsert_watch`` write EVERY column of
    ``run_definitions``, from a payload a caller built out of a read that happened
    somewhere else entirely -- ``vibe task update`` reads the definition, resolves
    Agents and Sessions, prompts for nothing, and only then writes the whole row
    back. Between that read and this write, ``reclaim_bound_definitions`` (``/new``
    or the archive dialog) can pause or soft-delete the very same row and stamp its
    ``session_settings_snapshot`` on it. A write keyed on ``id`` alone then RESTORES
    the pre-teardown ``session_id`` / ``enabled`` / ``deleted_at`` / metadata: the
    reclaim's compare-and-set succeeded, the counters and the teardown ledger told
    the user "1 task paused", and the row is enabled again and pointing at a session
    that no longer exists.

    So the write must re-assert the state it was decided from -- the same idiom the
    session writers use (``storage.agent_session_rows.unchanged_text``), applied to
    a payload whose read is one layer up instead of one statement up.

    THE PREDICATE SET IS THE STATE TEARDOWN OWNS, and nothing else:

    * ``session_id`` -- the binding the payload's fields were resolved against.
    * ``enabled`` -- the pause half of a ``pause``-mode reclaim.
    * ``deleted_at`` -- the soft-delete half of a ``delete``-mode reclaim, and the
      one that lets a full-row write RESURRECT a removed task, since no in-memory
      definition even carries the column (it is always written back as ``NULL``).
    * the reclaim snapshot's ``captured_at`` -- the third reclaim shape: for an
      ALREADY-paused definition the reclaim changes neither ``enabled`` nor
      ``deleted_at``, it only refreshes ``session_settings_snapshot``. That
      snapshot is what a later ``create_once`` rebind reads to carry the old
      workdir / agent / model forward, so restoring the pre-teardown metadata sends
      the task back on the wrong route (D3) with every other guard satisfied.

    DELIBERATELY NOT ``updated_at``, and not the whole row. A row-version guard
    would refuse every benign concurrent write -- a run result landing while the
    user renames a task -- and turn a working edit into an error. What is guarded is
    the lifecycle and binding state a teardown decides, which is exactly what a
    stale full-row payload must not be allowed to undo.
    """

    session_id: Optional[str] = None
    enabled: bool = True
    deleted_at: Optional[str] = None
    #: ``session_settings_snapshot.captured_at`` as read, ``None``/"" when the
    #: definition carried no reclaim snapshot.
    snapshot_captured_at: Optional[str] = None

    @classmethod
    def from_read(
        cls,
        *,
        session_id: Any = None,
        enabled: Any = True,
        deleted_at: Any = None,
        metadata: Any = None,
    ) -> "DefinitionWriteExpectation":
        """Build the expectation from the definition row/dataclass just read."""

        return cls(
            session_id=str(session_id) if session_id else None,
            enabled=bool(enabled),
            deleted_at=str(deleted_at) if deleted_at else None,
            snapshot_captured_at=reclaim_snapshot_marker(metadata),
        )


def reclaim_snapshot_marker(metadata: Any) -> Optional[str]:
    """``session_settings_snapshot.captured_at`` from definition metadata, if any.

    Never raises: the marker is JSON on rows that predate it, so anything
    unparseable reads as "this definition carries no snapshot", which is what the
    SQL side of the guard also computes for a malformed blob.
    """

    if not isinstance(metadata, dict):
        return None
    snapshot = metadata.get(SESSION_SETTINGS_SNAPSHOT_KEY)
    if not isinstance(snapshot, dict):
        return None
    captured_at = snapshot.get("captured_at")
    return str(captured_at) if captured_at else None


#: ``captured_at`` of the reclaim snapshot, read in SQL. Guarded by ``json_valid``
#: because ``metadata_json`` is user-visible text on legacy rows: a bare
#: ``json_extract`` over a malformed blob raises, which would turn "this row has no
#: snapshot" into a failed write.
_RECLAIM_SNAPSHOT_MARKER_SQL = case(
    (
        func.json_valid(run_definitions.c.metadata_json) == 1,
        func.json_extract(
            run_definitions.c.metadata_json,
            f"$.{SESSION_SETTINGS_SNAPSHOT_KEY}.captured_at",
        ),
    ),
    else_=None,
)


def definition_state_unchanged(expect: DefinitionWriteExpectation) -> list[Any]:
    """Predicates re-asserting the state a full-row payload was derived from.

    Shares ``unchanged_text`` with the session writers rather than restating its
    NULL handling: these are nullable TEXT columns and a bare ``col == value`` over
    a NULL evaluates to NULL, not false, so without ``COALESCE`` the guard stops
    guarding exactly the rows most likely to be raced on (an unbound definition, a
    row with no snapshot). ``enabled`` is ``NOT NULL INTEGER`` and needs none.
    """

    return [
        unchanged_text(run_definitions.c.session_id, expect.session_id),
        run_definitions.c.enabled == (1 if expect.enabled else 0),
        unchanged_text(run_definitions.c.deleted_at, expect.deleted_at),
        unchanged_text(_RECLAIM_SNAPSHOT_MARKER_SQL, expect.snapshot_captured_at),
    ]


def definition_lifecycle_expression(definition_type: str):
    """The single declaration of a task/watch lifecycle state, as SQL.

    The row select and the filter counts both read this expression, so a row can
    never land in a bucket its own chip did not count. That is why it is SQL and
    not Python: counts are a ``GROUP BY`` over the whole table while rows are one
    page of it, and a Python twin of this rule would have to be kept in step by
    hand — the same shape of drift ``_RunProjection`` exists to prevent.

    Branch order is the priority: an execution in flight outranks everything,
    then a definition that can never fire again, then the switch. ``finished``
    has to outrank ``waiting`` because ``enabled`` is not a promise of a future
    fire — re-enabling a one-shot that already fired flips the switch back on
    without giving it anything left to do, and reading the switch first parked
    such a row in the default Active view forever.

    Both ``ended`` branches read a fact written by whatever ends the definition,
    never a proxy for one: a watch retires when its supervisor says so, and a
    one-shot when the clock passes the instant it names. A history column —
    "it has run at least once" — looks like either and is neither.
    """

    in_flight = (
        select(agent_runs.c.id)
        .where(agent_runs.c.definition_id == run_definitions.c.id)
        .where(
            or_(
                agent_runs.c.run_type.is_(None),
                agent_runs.c.run_type != _WATCH_RUNTIME_RUN_TYPE,
            )
        )
        .where(agent_runs.c.status.in_([*_status_query_values("queued"), *_status_query_values("running")]))
        .exists()
    )
    if definition_type == "watch":
        # Retirement is persisted only by the supervisor branch that switches
        # the watch off. Legacy rows have no marker and therefore read paused:
        # their history cannot prove whether the old writer retired them or a
        # user paused them after a cycle.
        ended = and_(
            run_definitions.c.enabled == 0,
            run_definitions.c.retired_at.is_not(None),
        )
    else:
        # A cron task cannot retire itself, so a disabled one is always someone
        # having paused it. A one-shot is over when the instant it names has
        # passed — not when it last ran. ``vibe task run`` executes an armed
        # task early and records ``last_run_at`` without consuming the schedule,
        # so reading history here retired a task that was still going to fire.
        #
        # This is the same question ``compute_next_run_at`` answers — it returns
        # ``None`` exactly when the instant is behind us — so the state and the
        # time printed beside it cannot contradict each other.
        #
        # SQLite cannot resolve an IANA timezone and treats a naive timestamp as
        # UTC. The connection UDF delegates that resolution to the same stdlib
        # rule the scheduler uses, then compares two epoch values in UTC.
        ended = and_(
            run_definitions.c.schedule_type == "at",
            run_definitions.c.run_at.is_not(None),
            func.avibe_run_at_epoch(
                run_definitions.c.run_at,
                run_definitions.c.timezone,
            )
            <= (func.julianday("now") - literal(2440587.5)) * literal(86400.0),
        )
    return case(
        (in_flight, "running"),
        (ended, "finished"),
        (run_definitions.c.enabled != 0, "waiting"),
        else_="paused",
    )


def _successful_finished_definition_expression(definition_type: str, lifecycle: Any):
    """Successful one-shot history hidden by the compact CLI lists.

    This predicate is deliberately anchored to the canonical lifecycle
    expression first. A queued execution therefore keeps a disabled definition
    visible as ``running`` instead of letting historical completion fields hide
    live work.
    """

    no_error = func.trim(func.coalesce(run_definitions.c.last_error, "")) == ""
    if definition_type == "watch":
        successful_one_shot = and_(
            run_definitions.c.mode == "once",
            run_definitions.c.last_finished_at.is_not(None),
            or_(
                run_definitions.c.last_exit_code.is_(None),
                run_definitions.c.last_exit_code == 0,
            ),
            no_error,
        )
    else:
        successful_one_shot = and_(
            run_definitions.c.schedule_type == "at",
            run_definitions.c.enabled == 0,
            run_definitions.c.last_run_at.is_not(None),
            no_error,
        )
    return and_(lifecycle == "finished", successful_one_shot)


# One completed cycle's worth of state: when the row last ran, how that ending
# went, and what it caught.
DEFINITION_RETIREMENT_COLUMNS = (
    "retired_at",
    "last_finished_at",
    "last_exit_code",
    "last_error",
)
DEFINITION_CYCLE_COLUMNS = (
    "retired_at",
    "last_started_at",
    "last_finished_at",
    "last_event_at",
    "last_exit_code",
    "last_error",
)


def definition_resume_clear_columns(
    definition_type: Optional[str], mode: Optional[str]
) -> tuple[str, ...]:
    """Which lifecycle fields switching this definition back on clears.

    Retirement is state, not history: every resumed watch clears the finish,
    exit, and error that described its previous retirement. A one-shot also
    clears its prior start/event history because it begins a distinct cycle.

    A ``forever`` watch keeps continuous history: "last fired 2h ago" is
    precisely what its row exists to show, and a pause does not make it untrue.
    Scheduled tasks keep all history because a fired one-shot has no future fire
    to protect, and a cron task still needs to report its last run.

    Lives here, next to the single UPDATE, because two doorways must agree on
    it: the Harness UI writes through ``set_definition_enabled`` while the CLI
    and supervisor write through ``core/watches.py``.
    """

    if definition_type != "watch":
        return ()
    return DEFINITION_RETIREMENT_COLUMNS if mode == "forever" else DEFINITION_CYCLE_COLUMNS


def _row_lifecycle_state(row: Any) -> Optional[str]:
    """The state the query resolved for this row, if the query selected it.

    Every harness read path goes through ``_definitions_query`` and so carries
    the column. Importers and other direct ``select(run_definitions)`` readers do
    not, and get ``None`` — an absent state, which the UI renders as unknown
    rather than as a wrong one.
    """

    try:
        return row["lifecycle_state"]
    except (KeyError, IndexError):
        return None


def definition_lifecycle_detail(
    *,
    lifecycle_state: Optional[str],
    definition_type: Optional[str] = None,
    last_run_at: Any = None,
    last_exit_code: Any = None,
    last_error: Any = None,
) -> Optional[str]:
    """How a finished task/watch ended: ``normal``, ``timeout``, or ``error``.

    Non-null only for ``finished``; the other three states are still in play and
    have no ending to report yet. Python rather than SQL because it has exactly
    one consumer — the row — and never a ``GROUP BY``: the filter groups by
    state, and the row alone says which of the three endings it was.
    """

    if lifecycle_state != "finished":
        return None
    if definition_type == "scheduled" and last_run_at is None:
        return None
    if last_exit_code == _TIMEOUT_EXIT_CODE:
        return "timeout"
    if last_exit_code not in (None, 0):
        return "error"
    # Scheduled tasks never write an exit code, so the code alone would report
    # every failed one as a normal ending; ``last_error`` is where their failure
    # lands. Same pair ``vibe/cli.py`` already reads to call a watch clean.
    if str(last_error or "").strip():
        return "error"
    return "normal"


def definition_status_total(counts: dict[str, int], status: Optional[str]) -> int:
    """How many rows a status filter selects, from the per-state counts.

    The API's ``total`` used to be ``counts[status]``, which only worked while
    every filter was also a count key. ``active`` spans two states, so the sum
    is declared here — beside the filter table it sums — instead of being
    re-derived by each caller.
    """

    states = DEFINITION_STATUS_FILTERS.get(status or "all")
    if not states:
        return int(counts.get("total", 0))
    return sum(int(counts.get(state, 0)) for state in states)


@dataclass(frozen=True)
class _RunProjection:
    """One place ``_enrich_runs`` writes resolved, user-visible text onto a run.

    Two consumers have to agree on this list, and kept not agreeing: the
    enrichment that *fills* a projected field, and the search predicate that has
    to *find* what it filled in. Review caught the mismatch one field at a time
    — first the definition name, then the session label — because the list only
    existed as parallel code in two functions, so each fix closed one field and
    left the next one open.

    It exists once now. ``_enrich_runs`` and ``_run_search_predicates`` both
    read it, so a projection added here is searchable by construction rather
    than by remembering, and one added *without* coming through here fails
    ``test_every_projected_label_is_searchable`` instead of costing a review
    round.
    """

    source: str
    """Which batch resolver fills it. Sites sharing a source resolve together in
    one query — that is what keeps a page at a fixed number of round trips
    however many sites there are."""

    payload_key: Optional[str]
    """Where the resolved summary lands. ``None`` merges it into the run row
    itself; a key nests it under that name, and nests ``None`` when the run has
    nothing to resolve there."""

    id_field: str
    id_column: Any
    """The run column naming the row to resolve — payload name and SQL column,
    which differ for the legacy key below."""

    key_fields: tuple[str, ...] = ()
    key_columns: tuple[str, ...] = ()
    """Scope-key fallbacks in precedence order, consulted only when ``id_field``
    resolves to nothing. Column names rather than columns: ``session_key`` is
    stored as ``legacy_session_key``."""


_RUN_PROJECTIONS: tuple[_RunProjection, ...] = (
    _RunProjection(
        source="session",
        payload_key=None,
        id_field="session_id",
        id_column="session_id",
        key_fields=("session_key", "deliver_key"),
        key_columns=("legacy_session_key", "deliver_key"),
    ),
    _RunProjection(
        source="session",
        payload_key="callback_session",
        id_field="callback_session_id",
        id_column="callback_session_id",
    ),
    # The run's 来源. Its payload field reads a *derived* id (agent-sourced runs
    # only) while its SQL column is the raw ``source_actor`` — the projection's
    # two-name design exists for exactly this.
    _RunProjection(
        source="session",
        payload_key="source_session",
        id_field="source_session_id",
        id_column="source_actor",
    ),
    _RunProjection(
        source="definition",
        payload_key=None,
        id_field="definition_id",
        id_column="definition_id",
    ),
)
_DEFERRED_RUN_EVENT_ROWS_KEY = "avibe.deferred_run_event_rows"
_TERMINAL_STATUS_PRIORITY = {
    "succeeded": 0,
    "canceled": 1,
    "failed": 2,
}
# The closed set of terminal run statuses. Shared so guarded writers and reconcile
# paths cannot drift apart (this file previously spelled the set inline).
TERMINAL_RUN_STATUSES = frozenset(_TERMINAL_STATUS_PRIORITY)


def _parse_iso_instant(value: Any) -> Optional[datetime]:
    """Parse a stored ISO timestamp, or ``None`` if it is absent/unusable.

    Callers deciding whether a row is stale must treat ``None`` as "not old enough".
    A row we cannot date is a row we must not sweep.
    """

    text = str(value or "").strip()
    if not text:
        return None
    try:
        instant = datetime.fromisoformat(text)
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant


@dataclass(frozen=True)
class SweptRun:
    """One run the staleness sweep terminalized, plus who it belonged to.

    The identity fields are the sweep's report, not its repair: an honest DB row is
    not enough if the session stays undispatchable, but the in-memory wedge is
    released from the recorded lock owner (``ScheduledTaskService.
    _release_leaked_session_locks``) rather than reconstructed from these fields —
    a lock key is per-conversation, so freeing it from a swept run's identity could
    free one a different live execution still holds. These fields exist so the
    caller can log, notify, and test what was swept.
    """

    run_id: str
    status: str
    interrupt_reason: str
    run_type: Optional[str] = None
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    session_key: Optional[str] = None


#: ``metadata.last_skip_reason`` values written by the drain when it defers a queued
#: run. The sweep requires this recorded evidence rather than re-deriving readiness, so
#: a run deferred for a reason that still represents progress is never swept.
#:
#: Only ``transport_unavailable`` makes a row sweepable. ``session_busy`` is recorded
#: precisely so it can OVERWRITE a stale ``transport_unavailable``: without it, a run
#: that was once blocked on a dead transport and is now merely queued behind its own
#: session's active turn would still look sweepable. Capacity skips are deliberately
#: not recorded — the drain ``break``s at capacity without examining the remaining
#: rows, and an unstamped row is never swept, which is the safe direction.
SKIP_REASON_TRANSPORT_UNAVAILABLE = "transport_unavailable"
SKIP_REASON_SESSION_BUSY = "session_busy"

#: ``metadata.interrupt_reason`` values the sweep writes. Kept beside the sweep so
#: the query and the reason cannot drift.
SWEEP_REASON_ORPHANED = "orphaned"
SWEEP_REASON_TRANSPORT_UNAVAILABLE = "transport_unavailable"
SWEEP_REASON_QUEUE_HOLD_EXPIRED = "queue_hold_expired"

#: ``agent_runs.metadata.owed_failure_notice`` — the durable record that a user
#: still has to be told about one terminal failure.
#:
#: Stamped by whichever UPDATE actually transitions ``status`` to ``failed``, not
#: by a list of call sites: that property is what makes a settlement path added
#: later inherit the notice instead of having to remember it. The one exception is
#: ``stamp_binding_change_notice``, and it is an exception precisely because there
#: is no transition to ride — a rebind whose retry succeeds settles ``succeeded``
#: and still owes the user the news. Guardedness is
#: deliberately NOT part of the test — an ordinary synchronous failure
#: terminalizes through the claimed-request completion, and excluding it would
#: leave the most common failure of all with no notice to deliver.
OWED_FAILURE_NOTICE_KEY = "owed_failure_notice"

#: Expression index serving the drain's eligibility seek. Named here so the query,
#: the migration and the query-plan test cannot drift apart.
OWED_NOTICE_INDEX = "ix_agent_runs_owed_notice"

#: The eligibility expressions, as literal SQL.
#:
#: Literal rather than composed with ``case()``/``func`` because SQLAlchemy renders
#: the ``1`` and the JSON path as bound parameters, and SQLite will not match an
#: index expression against a query expression containing binds — the index gets
#: built and silently ignored.
#:
#: The ``CASE json_valid`` guard does two jobs. In the QUERY it stops one malformed
#: blob from raising ``malformed JSON`` and failing the whole statement, which would
#: silence every failure notification at once. In the INDEX it stops the same thing
#: at WRITE time: an index expression is evaluated on every INSERT/UPDATE, so a bare
#: ``json_extract`` would make a row with an unparseable blob unwritable.
#:
#: Columns are unqualified because SQLite rejects the "." operator inside an index
#: expression. ``20260728_0040`` must keep these byte-identical.
OWED_NOTICE_STATE_SQL = (
    "CASE WHEN (json_valid(metadata_json) = 1) "
    "THEN json_extract(metadata_json, '$.owed_failure_notice.state') END"
)
#: The notice kind is not part of the eligibility index: it only distinguishes the
#: exceptional canceled binding-change row after the indexed pending/backoff seek.
#: Keep the same malformed-JSON guard as the indexed expressions so one damaged row
#: cannot stop every notification from draining.
OWED_NOTICE_KIND_SQL = (
    "CASE WHEN (json_valid(metadata_json) = 1) "
    "THEN json_extract(metadata_json, '$.owed_failure_notice.kind') END"
)
#: ``coalesce(..., '')`` is what keeps this a RANGE term. A missing or null
#: ``next_attempt_at`` means "eligible now", and expressing that as
#: ``(x IS NULL OR x <= now)`` is a disjunction, which SQLite cannot use as an index
#: constraint — the index would be named in the plan while the backoff was still
#: filtered per row. The empty string sorts before every ISO instant, so a null
#: reads as eligible through the same ``<=`` comparison.
#:
#: This also keeps notices stamped before the backoff column existed visible
#: instead of silently unreachable forever.
OWED_NOTICE_NEXT_ATTEMPT_SQL = (
    "CASE WHEN (json_valid(metadata_json) = 1) "
    "THEN coalesce(json_extract(metadata_json, '$.owed_failure_notice.next_attempt_at'), '') END"
)

#: The notice lifecycle, mirroring ``callback_status`` rather than inventing a
#: second vocabulary: ``pending`` owes delivery, ``sent`` has evidence of it,
#: ``skipped`` is a row the drain decided needs no user-visible notice (streak
#: suppression), ``failed`` is a dead letter that exhausted its retries and stays
#: visible instead of retrying forever.
NOTICE_PENDING = "pending"
NOTICE_SENT = "sent"
NOTICE_SKIPPED = "skipped"
NOTICE_FAILED = "failed"
#: Terminal notice states: never delivered again, and no longer blocking a streak.
NOTICE_TERMINAL_STATES = frozenset({NOTICE_SENT, NOTICE_SKIPPED, NOTICE_FAILED})


class _UnstampableInstant(str):
    """A ``next_attempt_at`` that is not JSON text, carried through an expectation.

    A ``str`` subclass so the expectation stays a ``tuple[str, int, str]`` and every
    caller that only passes it through, prints it or compares it keeps working, while
    ``owed_notice_state_unchanged`` can recognize it by TYPE and re-assert the stored
    value the only way SQLite makes reproducible: by its JSON type.

    Why a JSON number cannot be re-asserted by its text. The guard compares
    ``cast(json_extract(...) AS TEXT)`` against a Python string, and SQLite renders a
    REAL with its own 15-significant-digit formatter: ``1e25`` reads back as
    ``'1.0e+25'`` where Python's ``str`` gives ``'1e+25'``, and at the 15th digit the
    two round differently (``-1.5063173670565552e-212`` -> ``...655`` in SQLite,
    ``...656`` in Python). Guessing that text would refuse the write, and a refused
    write on a row the listing ADMITS is the starvation this whole pair exists to
    prevent: the row is selected first every tick — every numeric sorts before every
    ISO instant — consumes a batch slot, and never transitions.

    Asserting the TYPE is not a loosening of what the third element is FOR. It exists
    to catch a concurrent DEFERRAL, which writes an ISO string
    (``notice_write_expectation``'s docstring records why), so "still not text" catches
    every deferral there can be. ``state`` and ``attempts`` are unchanged and still
    carry single-flight: a competing claim consumes the attempt, so only one owner can
    win regardless of how this element compares.
    """

    __slots__ = ()


#: The expectation's ``next_attempt_at`` when the stored value is not JSON text — a
#: shape no stamper writes, so it can only arrive from a hand-edited row or a foreign
#: writer. Interned as one value so the guard has a single spelling to recognize.
_NEXT_ATTEMPT_NOT_TEXT = _UnstampableInstant("<next_attempt_at:not-text>")


def notice_write_expectation(notice: Optional[dict[str, Any]]) -> tuple[str, int, str]:
    """The ``(state, attempts, next_attempt_at)`` an owed-notice write was decided from.

    Pass the result as ``update_owed_failure_notice(..., expect=...)``: one function so
    the reading side and the predicate side normalize identically and cannot drift.
    Never raises — ``attempts`` is JSON and a malformed value must read the same for
    both sides rather than turning a guarded write into an error.

    ALL THREE FIELDS THE DECISION READ, not just the two that identify a delivery
    attempt. Eligibility is a function of ``state`` and ``next_attempt_at``
    (``owed_notice_eligible``) and of ``attempts`` (``core.failure_notices.next_attempt``),
    so a predicate over two of them leaves one way for a write to land on a world that
    moved: a DEFERRAL writes only ``next_attempt_at`` and ``defer_reason``, leaving
    ``(state, attempts)`` untouched, so a claimant that read before a concurrent
    owner's deferral still matched its own expectation, won, and erased the deferral —
    a second notice for one outage in the stale-cutoff lane, and a
    ``DEFERRAL_RECHECK_SECONDS`` that any stale claimant could cancel.

    Why this field and not ``updated_at``, which ``DefinitionWriteExpectation``
    deliberately refuses: a row-version marker refuses benign writes and a freshly
    stamped notice does not carry one at all, whereas ``next_attempt_at`` is stamped
    unconditionally by every stamper (``_owed_failure_notice_for_transition`` and
    ``stamp_binding_change_notice``) and a legacy notice that predates the field reads
    ``""`` identically on both sides — the same ``coalesce(..., '')`` that keeps
    ``OWED_NOTICE_NEXT_ATTEMPT_SQL`` a range term.

    One consequence is recorded rather than hidden: two owners deferring the same
    notice from one read no longer BOTH land — the first moves the field the second
    re-asserts. The refusal is correct, and observationally empty: the notice is
    deferred either way, no attempt is consumed either way, and the two recheck
    instants differ by the microseconds between two owners reading the clock. Pinned by
    ``test_a_second_identical_deferral_loses_the_cas_without_changing_the_outcome``.
    """

    source = notice if isinstance(notice, dict) else {}
    # CAST semantics, not int(): SQLite parses a numeric PREFIX ('3x' → 3,
    # '1e100' → 1) and saturates at the i64 bounds, where int() raises or
    # overflows past what the CAS will read back. A divergent read here is a
    # claim that can never match — the row stays eligible and unchanged on every
    # drain pass, occupying one of the ten batch slots forever.
    attempts = sqlite_cast_integer(source.get("attempts"))
    return (str(source.get("state") or ""), attempts, _expected_next_attempt_at(source))


def _expected_next_attempt_at(source: dict[str, Any]) -> str:
    """``next_attempt_at`` as SQLite will read it back for the guard's comparison.

    Text is taken VERBATIM — not stripped, not re-formatted — because the guard
    compares it against the stored blob through SQLite, which does neither; a stripped
    copy would refuse a write over a padded instant that nobody raced. Missing and
    null both read as ``""``, mirroring the ``coalesce(..., '')`` in
    ``OWED_NOTICE_NEXT_ATTEMPT_SQL``.

    Anything else — a JSON number, ``true``/``false``, an object, an array — is a shape
    no stamper writes and whose SQLite text is not reproducible in Python, so it is
    carried as ``_NEXT_ATTEMPT_NOT_TEXT`` and re-asserted by JSON type instead. Note
    the falsiness traps this avoids by testing the raw value rather than ``or ""``:
    a stored ``0`` is ``'0'`` to SQLite, not ``''``, and a stored ``true`` is ``'1'``,
    not ``'True'`` — both of which read as an unraceable guard on the eligible row
    they belong to.
    """

    raw = source.get("next_attempt_at")
    if raw is None or raw == "":
        return ""
    if isinstance(raw, str):
        return raw
    return _NEXT_ATTEMPT_NOT_TEXT


def owed_notice_eligible(notice: Optional[dict[str, Any]], now: str) -> bool:
    """Whether this notice may be acted on at *now*: ``pending`` and out of its wait.

    The Python twin of ``OWED_NOTICE_STATE_SQL`` / ``OWED_NOTICE_NEXT_ATTEMPT_SQL``,
    and it has to agree with them value for value — same relationship, and same
    hazard, as ``notice_write_expectation`` and ``owed_notice_state_unchanged``. The
    listing query seeks on those two index expressions and then re-checks the decoded
    blob (the index is over a JSON path, so a row whose blob changed between the seek
    and the read is worth re-reading), and ``_deliver_one_failure_notice`` re-reads
    the row again before claiming it. Three copies of "is this eligible" is three
    chances to disagree; one function is none.

    ``next_attempt_at`` missing, null or empty means ELIGIBLE NOW, mirroring the
    ``coalesce(..., '')`` in ``OWED_NOTICE_NEXT_ATTEMPT_SQL`` — the empty string sorts
    before every ISO instant, so both sides read an absent wait through the same
    ``<=``. String comparison, for the same reason every other timestamp comparison
    here is: ISO-8601 in UTC sorts lexicographically.

    Agreement means agreeing with the comparison SQLITE makes, which for a value that
    is not text is not the comparison Python makes. The indexed expression is compared
    RAW, so SQLite compares STORAGE CLASSES: every INTEGER and REAL sorts before every
    TEXT, making a numeric ``next_attempt_at`` eligible at every instant, and SQLite
    never strips, so ``" 9999-01-01..."`` sorts before ``now`` on its leading space.
    Reading those through ``str(...).strip() <= now`` said the opposite, and the
    disagreement is not a difference of opinion the drain absorbs: the seek applies
    ``LIMIT`` BEFORE this re-check, so such a row is selected, dropped without any
    state transition, and selected again on the next tick forever — and because it
    sorts FIRST, ten of them starve every valid notice behind them, silently, in a
    drain that looks busy. So the shapes SQL admits are admitted here too, and the
    drain ADVANCES them: the claim stamps a real instant over the unreadable one.
    Degrade and advance, as ``notice_write_expectation`` does for ``attempts``.

    THE WHOLE DOMAIN, decided explicitly rather than left to whichever of the two
    languages happens to answer first. Only the first two rows are reachable through
    this module's writers; the rest need a hand-edited row or a foreign writer, and are
    given a stated treatment anyway because the failure mode of an unstated one is
    silence:

    ==================== ============ =============================================
    stored value         eligible?    treatment
    ==================== ============ =============================================
    absent / null / ``""`` yes        no wait ever armed — ``coalesce(..., '')``
    text                 ``value <= now``  UNSTRIPPED lexicographic compare, both
                                      sides, because SQLite does not strip: a padded
                                      instant is EARLY (``" 9999-…"`` sorts on its
                                      leading space), never late
    integer / real /     yes          numeric storage class sorts below every text
    ``true`` / ``false``              bound, so SQL admits it at every instant;
                                      admitted here too and NORMALIZED BY THE CLAIM,
                                      which stamps a real instant over it. Bounded
                                      progress: one pass and the row is a normal
                                      notice again
    object / array       NO           reads back as JSON text beginning ``{``/``[``,
                                      which sorts above every ISO instant, so the
                                      SEEK never returns it: it occupies no batch
                                      slot and starves nothing. It also never
                                      delivers. Chosen over admitting it in Python
                                      only, which is the divergence this function
                                      exists to remove; the underlying failure stays
                                      visible through the definition's ``last_error``
                                      and ``definition_health`` (the run is still
                                      ``failed``), so what is lost is the push, not
                                      the record. Pinned by
                                      ``test_a_container_retry_instant_is_ineligible_
                                      on_both_sides_and_starves_nothing``
    ==================== ============ =============================================

    The field carries two things that are one thing to this predicate: the retry
    BACKOFF after a failed attempt, and the LEASE a claimant arms before it performs
    the external send (see ``core.failure_notices.CLAIM_LEASE_SECONDS``). Both mean
    "not this owner, not yet", so both are expressed as a future instant rather than
    as a second column — which is also what makes the claim expire on its own if the
    claimant dies.
    """

    if not isinstance(notice, dict) or notice.get("state") != NOTICE_PENDING:
        return False
    raw = notice.get("next_attempt_at")
    if raw is None or raw == "":
        return True
    if isinstance(raw, str):
        return raw <= now
    if isinstance(raw, (int, float)):
        # Numeric storage class, so SQLite sorts it before every TEXT: eligible at any
        # ``now``. ``bool`` is an ``int`` here and is one in SQLite too — ``json_extract``
        # reads JSON ``true``/``false`` back as 1/0.
        return True
    # An object or an array reads back as its JSON TEXT, which begins with a brace or a
    # bracket and therefore sorts above every ISO instant — ineligible on both sides,
    # and spelled out as a comparison rather than hardcoded as ``False`` so the two
    # sides stay one rule rather than two agreeing accidents.
    return _json_dumps(raw) <= now


#: ``attempts``, read in SQL. Unlike ``OWED_NOTICE_STATE_SQL`` this one is NOT pinned
#: to an index expression and does not need to be: the guarded write is located by
#: primary key, so this pair is a FILTER on one already-identified row rather than a
#: seek term, and no plan depends on its text.
#:
#: It keeps the ``CASE json_valid`` shape for the WRITE-time reason that constant
#: documents: a bare ``json_extract`` over a malformed blob raises ``malformed JSON``
#: and fails the whole statement, which here would turn "this write lost a race" into
#: an exception the drain logs on every pass.
_OWED_NOTICE_ATTEMPTS_SQL = (
    "CASE WHEN (json_valid(metadata_json) = 1) "
    "THEN json_extract(metadata_json, '$.owed_failure_notice.attempts') END"
)

#: ``next_attempt_at``'s JSON TYPE, read in SQL. The only reproducible way to
#: re-assert a stored value whose TEXT rendering is SQLite's own — see
#: ``_UnstampableInstant``. Not index-pinned, for the reason
#: ``_OWED_NOTICE_ATTEMPTS_SQL`` gives: this is a filter on a row already located by
#: primary key, so no query plan depends on its text.
#:
#: Same ``CASE json_valid`` guard, same reason: ``json_type`` raises ``malformed JSON``
#: on an unparseable blob and would fail the whole statement.
_OWED_NOTICE_NEXT_ATTEMPT_TYPE_SQL = (
    "CASE WHEN (json_valid(metadata_json) = 1) "
    "THEN json_type(metadata_json, '$.owed_failure_notice.next_attempt_at') END"
)


def owed_notice_state_unchanged(expect: tuple[str, int, str]) -> list[Any]:
    """Predicates re-asserting the ``(state, attempts, next_attempt_at)`` of a write.

    The SQL twin of ``notice_write_expectation``, and normalized to agree with it
    value for value — same relationship as ``reclaim_snapshot_marker`` and
    ``_RECLAIM_SNAPSHOT_MARKER_SQL``, for the same reason: a predicate that read the
    stored blob more strictly than the caller read it would refuse writes nobody
    raced. ``coalesce`` because a missing/null key is 0 or ``""`` on the Python side
    and would otherwise be NULL here, and NULL is not false — it would fail the guard
    on every freshly stamped notice. ``CAST`` because JSON is untyped: ``"3"`` and
    ``3.7`` are what ``int(...)`` makes of them.

    Values a notice cannot legally hold (a state stored as a JSON number) may still
    resolve differently on the two sides; when they do, SQL is the STRICTER one, so
    the residue is a refused write and a retried notice, never a lost one.

    THAT ARGUMENT DOES NOT EXTEND TO ``next_attempt_at``, and the exception is why the
    third predicate has two forms. A refused write is harmless only for a row that is
    also ineligible; a row this guard can never match while ``owed_notice_eligible``
    keeps admitting it is selected first by every tick — every JSON number sorts before
    every ISO instant — occupies a batch slot, and never transitions. So for a stored
    value that is not JSON text, whose SQLite TEXT rendering is not reproducible in
    Python, the guard re-asserts the JSON TYPE instead of the text. See
    ``_UnstampableInstant`` for the rendering evidence and for why the type is a
    faithful substitute here.

    The text form reuses ``OWED_NOTICE_NEXT_ATTEMPT_SQL`` VERBATIM under the
    same outer ``coalesce``/``CAST`` shape as the other two, for the reason
    ``owed_notice_absent`` gives: this is a filter on a row located by primary key so
    no plan depends on its text, but a divergent copy of an indexed expression is
    exactly how the eligibility index was built and silently ignored twice.
    """

    state, attempts, next_attempt_at = expect
    if isinstance(next_attempt_at, _UnstampableInstant):
        # The stored value is not JSON text, so its TEXT rendering is SQLite's own and
        # not reproducible here (see ``_UnstampableInstant``): re-assert the type
        # instead. ``coalesce`` to ``'null'`` because an absent key and a malformed blob
        # both read as NULL, and NULL is not false — either would pass a bare
        # ``NOT IN`` and let this guard match a row the caller never read.
        third: Any = cast(
            func.coalesce(literal_column(_OWED_NOTICE_NEXT_ATTEMPT_TYPE_SQL), "null"), Text
        ).notin_(["text", "null"])
    else:
        third = (
            cast(func.coalesce(literal_column(OWED_NOTICE_NEXT_ATTEMPT_SQL), ""), Text)
            == next_attempt_at
        )
    return [
        cast(func.coalesce(literal_column(OWED_NOTICE_STATE_SQL), ""), Text) == state,
        cast(func.coalesce(literal_column(_OWED_NOTICE_ATTEMPTS_SQL), 0), Integer) == attempts,
        third,
    ]


def owed_notice_absent() -> list[Any]:
    """Predicate for "this row owes no notice yet", evaluated by SQLite.

    The SQL twin of the Python pre-check every STAMPING writer makes
    (``_owed_failure_notice_for_transition`` and ``stamp_binding_change_notice``:
    "an existing notice is never overwritten"), and normalized to agree with it —
    ``coalesce`` because an absent key, a null state and a notice stored as
    something other than an object all read as "no notice" on the Python side and
    would be NULL here, and NULL is not false.

    It exists for the writer that has no terminal transition to ride. A stamp folded
    into the UPDATE that moves ``status`` to ``failed`` is already atomic with the
    thing it depends on; ``stamp_binding_change_notice`` runs on a LIVE run, before
    ``complete()``, so its "no existing notice" read and its write are two statements
    and something else can terminalize in between. Same reason
    ``owed_notice_state_unchanged`` exists, same fix.

    Reuses ``OWED_NOTICE_STATE_SQL`` verbatim rather than spelling the JSON path
    again: this is a FILTER on a row located by primary key, so no plan depends on
    its text, but a divergent copy of that expression is exactly how the eligibility
    index was built and silently ignored twice.
    """

    return [cast(func.coalesce(literal_column(OWED_NOTICE_STATE_SQL), ""), Text) == ""]


#: Mirror of ``core.failure_notices.NOTICE_KIND_*``, spelled as literals for the
#: same reason ``RUN_INTERRUPTION_REASONS`` is below: ``core`` imports ``storage``,
#: not the other way round. ``tests/test_harness_failure_visibility.py`` asserts the
#: two agree so they cannot drift.
#:
#: The field is ADDITIVE. A notice stamped before it existed carries no ``kind`` and
#: reads as ``failure``, and neither eligibility expression mentions it, so no index
#: and no migration is involved.
NOTICE_KIND_FAILURE = "failure"
NOTICE_KIND_BINDING_CHANGE = "binding_change"

#: How the drain proved delivery. ``receipt`` is a persisted ``messages`` row;
#: ``delivery_only`` is a transport that returned an id whose row write failed —
#: positive evidence the user was told, recorded explicitly rather than pretending
#: the receipt exists.
ACK_EVIDENCE_RECEIPT = "receipt"
ACK_EVIDENCE_DELIVERY_ONLY = "delivery_only"

#: Mirror of ``core.run_settlement.RUN_INTERRUPTION_REASONS``, spelled as literals
#: for the same reason ``SWEEP_I18N_KEYS`` mirrors this module's sweep reasons:
#: ``core`` imports ``storage``, not the other way round, and this module must stay
#: importable without pulling ``core`` in. ``tests/test_harness_failure_visibility.py``
#: asserts the two sets are equal so they cannot drift.
RUN_INTERRUPTION_REASONS = frozenset(
    {
        "stopped",
        "backend_refresh",
        "evicted",
        "restarted",
        "lifetime_timeout",
        # The teardown lane's generic default: an execution cancelled with no cause
        # recorded. Still one run ended from outside, so it belongs here and not in
        # the per-fire failure population.
        "interrupted",
        SWEEP_REASON_ORPHANED,
    }
)

#: ``metadata.interrupt_reason``, as literal SQL. ONE spelling, shared by every
#: query that has to keep interruptions out of a definition's history — the health
#: window and the failure streak — because a divergent copy is silent: the results
#: stay correct and the planner just declines to match, which is precisely how the
#: eligibility index was built and ignored twice (see ``OWED_NOTICE_STATE_SQL``).
#:
#: Literal, and unqualified, for the same two reasons as the eligibility
#: expressions: SQLAlchemy renders a composed ``case()`` with BOUND PARAMETERS,
#: which no index expression can match, and SQLite rejects the "." operator inside
#: one. Nothing indexes this expression today — the streak's bound comes from
#: ``(definition_id, created_at, id)`` and the interruption filter is evaluated on
#: the handful of rows that seek touches — but keeping it index-shaped is what
#: makes indexing it later a migration rather than a rewrite.
#:
#: The ``CASE json_valid`` guard is not optional: ``json_extract`` raises
#: ``malformed JSON`` and fails the whole STATEMENT, so one unparseable blob would
#: take out health for every definition in a batch, or the streak read for the
#: whole drain. CASE evaluates lazily, so a malformed row degrades to "no interrupt
#: reason" — the same way this module's Python ``_json_loads`` idiom degrades.
INTERRUPT_REASON_SQL = (
    "CASE WHEN (json_valid(metadata_json) = 1) "
    "THEN json_extract(metadata_json, '$.interrupt_reason') END"
)


def _not_an_out_of_band_interruption() -> Any:
    """SQL for "this row is not an out-of-band interruption".

    MEMBERSHIP in ``RUN_INTERRUPTION_REASONS``, never ``interrupt_reason IS NOT
    NULL``. Nullness would also exclude ``no_terminal_result`` /
    ``refused_concurrent_turn`` / ``transport_unavailable`` / ``queue_hold_expired``,
    which are the ordinary per-fire failures this whole feature exists to surface.
    """

    reason = literal_column(INTERRUPT_REASON_SQL)
    return or_(reason.is_(None), reason.notin_(sorted(RUN_INTERRUPTION_REASONS)))

#: Derived health, per definition. No new state: both counters come from one
#: indexed query over ``agent_runs``, so nothing has to be kept in sync or
#: backfilled.
HEALTH_FAILING = "failing"
HEALTH_DEGRADED = "degraded"
HEALTH_HEALTHY = "healthy"
#: What a definition reads when its own history cannot be classified — a malformed
#: ``metadata_json`` row, in practice. Distinct from ``healthy`` on purpose: a
#: health signal that cannot be computed must not read as a clean bill.
HEALTH_UNKNOWN = "unknown"

#: The health window: the last N verdicts OR the last T hours, whichever is
#: shorter. Both bounds live in the ``WHERE``/``LIMIT`` of one query rather than
#: one of them in prose — bounded only by count, a definition that failed once and
#: then stopped firing would read ``failing`` forever with no user action able to
#: clear it.
HEALTH_WINDOW_RUNS = 10
HEALTH_WINDOW_HOURS = 72


def _owed_failure_notice_for_transition(
    run_id: str,
    *,
    status: Any,
    metadata: dict[str, Any],
    now: str,
) -> Optional[dict[str, Any]]:
    """The notice a terminal transition owes, or ``None`` when it owes nothing.

    Only a ``failed`` transition owes one. ``succeeded`` has nothing to report, and
    ``canceled`` is reserved for explicit user intent (``SETTLEMENT_TERMINAL_STATUS``
    maps only ``stopped`` there, and the guarded writers map a ``cancel_requested``
    row there) — telling a user their run failed because they stopped it is noise.

    An existing notice is never overwritten. Re-stamping would reset ``attempts``
    and resurrect a dead letter, so a row that already carries a notice keeps the
    one it has whatever later writer touches it.
    """

    if normalize_run_status(status) != "failed":
        return None
    existing = metadata.get(OWED_FAILURE_NOTICE_KEY)
    if isinstance(existing, dict) and str(existing.get("state") or "").strip():
        return None
    reason = str(metadata.get("interrupt_reason") or "").strip() or None
    return {
        "state": NOTICE_PENDING,
        "attempts": 0,
        # ALWAYS an instant, never ``None``. A nullable column forces the query into
        # ``(x IS NULL OR x <= now)``, and a disjunction cannot be an index range
        # term — which is how the backoff stayed unindexed while the plan still
        # named the index. ``now`` means "eligible immediately".
        "next_attempt_at": now,
        # Run-derived so two drain passes over the same row produce ONE identity,
        # and so the identity is available at drain time at all: deriving it from
        # whatever context a pass happens to build would key it off a per-pass
        # ``task_execution_id`` — or off ``uuid4`` when the rebuild supplies none —
        # and re-send the notice every tick.
        #
        # WHICH run-derived form matters, and the two lanes need different ones.
        #
        # An ordinary failure reuses the bare run id, because that is exactly what
        # the LIVE path's ``_failure_identity`` resolves to (it prefers
        # ``task_execution_id``, which ``_build_context`` sets to the run id). A
        # different spelling here would produce a different ``native_message_id``,
        # so the drain's ``agent_message_exists`` lookup could not see a
        # notification the live path had already delivered — and would send a
        # second one for the same failure, defeating the dedup the whole receipt
        # protocol rests on.
        #
        # An interruption must NOT collide with that. A run terminalized out of band
        # may already carry an ordinary backend-failure notice against the same
        # execution, and a shared identity would let the dedup silently swallow the
        # D1 notice telling the user a deploy killed their run — the notices that
        # matter most, lost to the mechanism meant to prevent duplicates.
        #
        # Which lane this is comes from MEMBERSHIP in ``RUN_INTERRUPTION_REASONS``,
        # never from the presence of a reason. ``interrupt_reason`` is the general
        # marker for "terminalized by something other than its own backend result",
        # and its commonest values — ``no_terminal_result``,
        # ``refused_concurrent_turn``, ``transport_unavailable``,
        # ``queue_hold_expired`` — are ordinary per-fire verdicts that belong in the
        # suppressed failure lane. Minting ``interrupt:{run}:{reason}`` for those gave
        # them an identity the live path never uses, and since the drain's id is
        # AUTHORITATIVE that is one duplicate notification per failure.
        "failure_id": (
            f"interrupt:{run_id}:{reason}"
            if reason in RUN_INTERRUPTION_REASONS
            else run_id
        ),
        # Optional, and only ever a copy selector. The lane a notice belongs to is
        # decided from this value's membership in ``RUN_INTERRUPTION_REASONS``, not
        # from its presence.
        "interrupt_reason": reason,
        "error": None,
        "ack_evidence": None,
        "stamped_at": now,
    }


def _callback_parent_owns_failure_notice(
    conn: Any,
    *,
    source_kind: Any,
    parent_run_id: Any,
) -> bool:
    """Whether a callback child's parent already owns the user-visible notice."""

    if str(source_kind or "").strip() != "callback":
        return False
    parent_id = str(parent_run_id or "").strip()
    if not parent_id:
        return False
    raw_metadata = conn.execute(
        select(agent_runs.c.metadata_json).where(agent_runs.c.id == parent_id).limit(1)
    ).scalar_one_or_none()
    metadata = _json_loads(raw_metadata, {})
    notice = metadata.get(OWED_FAILURE_NOTICE_KEY) if isinstance(metadata, dict) else None
    return isinstance(notice, dict) and bool(str(notice.get("state") or "").strip())


def _merge_owed_failure_notice(
    values: dict[str, Any],
    *,
    conn: Any,
    run_id: str,
    status: Any,
    source_kind: Any,
    parent_run_id: Any,
    row_metadata_json: Any,
    extra_metadata: Optional[dict[str, Any]] = None,
    now: str,
) -> None:
    """Fold the owed notice into the ``metadata_json`` an UPDATE is about to write.

    In-place on ``values`` so the stamp rides the SAME statement that transitions
    the status. A stamp written by a second UPDATE could be lost to a crash between
    the two, which is the whole failure mode the durable notice exists to close.

    Takes the RAW COLUMN, not a decoded dict, because the decode is a decision this
    choke point has to make rather than inherit. Every caller used to hand it
    ``_json_loads(row["metadata_json"], {})`` and the fallback here turned anything
    that was not a dict into ``{}``, so settling a row whose blob is unparseable — or
    valid JSON that is not an object — REPLACED the column with just the notice.
    Whatever those bytes were is not this feature's to destroy.

    So an unreadable blob is READ-ONLY here: empty or NULL is a fresh ``{}`` base as
    before, and anything non-empty that will not decode to an object skips the
    metadata write ENTIRELY while the caller's terminal status/error/completed_at
    transition still commits. The same answer the three precedents on this path
    already give — the binding stamp refuses malformed rows through ``json_valid``,
    ``update_owed_failure_notice`` returns ``None`` when the metadata is not a dict,
    and ``list_owed_failure_notices`` excludes them ("a row whose metadata will not
    parse cannot hold a readable notice anyway").

    RESIDUAL, stated rather than hidden: such a row settles ``failed`` and never owes
    a notice, so its failure is visible in the run list and in derived health but is
    never delivered as a message. That was ALREADY true — the eligibility query
    excludes malformed rows, so a notice written here would have been durable and
    unreachable — and it is the direction every other reader on this path chose.
    """

    if isinstance(row_metadata_json, (bytes, bytearray)):
        row_metadata_json = bytes(row_metadata_json).decode("utf-8", "replace")
    if row_metadata_json is None or (
        isinstance(row_metadata_json, str) and not row_metadata_json.strip()
    ):
        merged: dict[str, Any] = {}
    else:
        decoded = _json_loads(row_metadata_json if isinstance(row_metadata_json, str) else None, None)
        if not isinstance(decoded, dict):
            logger.warning(
                "run %s has unreadable metadata_json; settling it without touching the "
                "column, so it records no owed failure notice",
                run_id,
            )
            return
        merged = decoded
    if extra_metadata:
        merged.update(extra_metadata)
    notice = None
    if not _callback_parent_owns_failure_notice(
        conn,
        source_kind=source_kind,
        parent_run_id=parent_run_id,
    ):
        notice = _owed_failure_notice_for_transition(
            run_id,
            status=status,
            metadata=merged,
            now=now,
        )
    if notice is None and not extra_metadata:
        return
    if notice is not None:
        merged[OWED_FAILURE_NOTICE_KEY] = notice
    values["metadata_json"] = _json_dumps(merged)


def normalize_run_status(status: Any) -> str:
    return RUN_STATUS_ALIASES.get(str(status or "").strip(), str(status or "").strip() or "queued")


def _stronger_terminal_status(current: Any, incoming: Any) -> str:
    current_status = normalize_run_status(current)
    incoming_status = normalize_run_status(incoming)
    if _TERMINAL_STATUS_PRIORITY.get(current_status, -1) >= _TERMINAL_STATUS_PRIORITY.get(
        incoming_status,
        -1,
    ):
        return current_status
    return incoming_status


#: When a run SETTLED, as one expression shared by every read that orders a
#: definition's history by it — the health window and the last-success seek.
#:
#: ``COALESCE`` rather than bare ``completed_at``: master does not treat terminal and
#: ``completed_at IS NOT NULL`` as the same condition (``list_pending_callbacks`` tests
#: them separately), and ordering must not silently reorder a row on the day one
#: terminal writer stops stamping it.
#:
#: ONE OBJECT, not two spellings, for the reason ``INTERRUPT_REASON_SQL`` is also
#: shared by name: this is the second key of ``ix_agent_runs_definition_settled``
#: (migration ``20260728_0039``), and a retyped copy that drifts is one the planner
#: silently stops matching while the results stay correct.
_SETTLED_AT = func.coalesce(agent_runs.c.completed_at, agent_runs.c.created_at)


def _status_query_values(status: str) -> list[str]:
    normalized = normalize_run_status(status)
    values = [raw for raw, public in RUN_STATUS_ALIASES.items() if public == normalized]
    return values or [normalized]


def _publish_run_rows_updated(rows: list[Any]) -> None:
    if not rows:
        return
    try:
        from core.inbox_events import RUNS_UPDATED_EVENT, bus, is_controller_process, run_updated_payload
    except Exception:
        logger.debug("failed to import run event publisher", exc_info=True)
        return
    for raw_row in rows:
        if raw_row is None:
            continue
        row = dict(raw_row)
        run_id = str(row.get("id") or "").strip()
        if not run_id:
            continue
        try:
            payload = run_updated_payload(
                run_id=run_id,
                status=normalize_run_status(row.get("status")),
                run_type=row.get("run_type"),
                session_id=row.get("session_id"),
                definition_id=row.get("definition_id"),
                updated_at=row.get("updated_at"),
                cancel_requested=bool(row.get("cancel_requested")),
            )
            bus.publish(RUNS_UPDATED_EVENT, payload)
            if bus.subscriber_count() == 0 and not is_controller_process():
                try:
                    from vibe import internal_client

                    internal_client.publish_event_sync(RUNS_UPDATED_EVENT, payload, timeout=1.5)
                except Exception:
                    logger.debug("failed to bridge runs.updated for %s", run_id, exc_info=True)
        except Exception:
            logger.debug("failed to publish runs.updated for %s", run_id, exc_info=True)


def _publish_queue_updated(session_id: str) -> None:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        return
    try:
        from core.inbox_events import (
            QUEUE_UPDATED_EVENT,
            bus,
            is_controller_process,
        )
    except Exception:
        logger.debug("failed to import queue event publisher", exc_info=True)
        return
    payload = {"session_id": normalized_session_id}
    try:
        bus.publish(QUEUE_UPDATED_EVENT, payload)
        if bus.subscriber_count() == 0 and not is_controller_process():
            try:
                from vibe import internal_client

                internal_client.publish_event_sync(
                    QUEUE_UPDATED_EVENT,
                    payload,
                    timeout=1.5,
                )
            except Exception:
                logger.debug(
                    "failed to bridge queue.updated for %s",
                    normalized_session_id,
                    exc_info=True,
                )
    except Exception:
        logger.debug(
            "failed to publish queue.updated for %s",
            normalized_session_id,
            exc_info=True,
        )


def _defer_run_rows_updated_from_connection(conn: Any, rows: list[Any]) -> None:
    if not rows:
        return
    pending = conn.info.setdefault(_DEFERRED_RUN_EVENT_ROWS_KEY, {})
    for raw_row in rows:
        if raw_row is None:
            continue
        row = dict(raw_row)
        run_id = str(row.get("id") or "").strip()
        if run_id:
            pending[run_id] = row


def pop_deferred_run_event_rows_from_connection(conn: Any) -> list[dict[str, Any]]:
    pending = conn.info.pop(_DEFERRED_RUN_EVENT_ROWS_KEY, {})
    if not isinstance(pending, dict):
        return []
    return [dict(row) for row in pending.values() if row is not None]


@contextmanager
def run_update_event_transaction(engine: Any):
    """Commit DB writes before publishing deferred ``runs.updated`` snapshots."""

    pending_rows: list[dict[str, Any]] = []
    with engine.begin() as conn:
        try:
            yield conn
            pending_rows = pop_deferred_run_event_rows_from_connection(conn)
        except Exception:
            conn.info.pop(_DEFERRED_RUN_EVENT_ROWS_KEY, None)
            raise
    _publish_run_rows_updated(pending_rows)


def _run_rows_for_ids(conn: Any, run_ids: list[str]) -> list[Any]:
    normalized_ids: list[str] = []
    seen: set[str] = set()
    for raw_run_id in run_ids:
        run_id = str(raw_run_id or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        normalized_ids.append(run_id)
    if not normalized_ids:
        return []
    return list(conn.execute(select(agent_runs).where(agent_runs.c.id.in_(normalized_ids))).mappings())


def _defer_run_ids_updated_from_connection(conn: Any, run_ids: list[str]) -> None:
    _defer_run_rows_updated_from_connection(conn, _run_rows_for_ids(conn, run_ids))


def _like_contains_pattern(value: str) -> str:
    """A contains-match pattern that tolerates whatever whitespace the row shows.

    Rows do not display stored text verbatim: a run title is the message's first
    non-empty line with whitespace runs collapsed, and HTML collapses the rest
    anyway. So the phrase a user reads — and types, or pastes — can differ from
    the column by exactly its spacing, and a literal LIKE finds nothing.

    Each whitespace run in the term becomes a wildcard, which makes the search
    match what is on screen. A single-token term is unchanged.
    """
    def escape(part: str) -> str:
        return (
            part.replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE)
            .replace("%", _LIKE_ESCAPE + "%")
            .replace("_", _LIKE_ESCAPE + "_")
        )

    parts = value.split() or [value]
    return "%" + "%".join(escape(part) for part in parts) + "%"


def _coalesced_agent_run_metadata(rows: dict[str, Any], run_ids: list[str]) -> dict[str, Any]:
    messages: list[dict[str, str]] = []
    prompt_parts: list[str] = []
    for run_id in run_ids:
        row = rows[run_id]
        message = str(row["message"] or row["prompt"] or "")
        messages.append({"execution_id": run_id, "message": message})
        if message:
            prompt_parts.append(message)
    metadata: dict[str, Any] = {
        "execution_ids": run_ids,
        "messages": messages,
    }
    if prompt_parts:
        metadata["prompt"] = "\n\n---\n\n".join(prompt_parts)
    return metadata


def cancel_not_requested() -> Any:
    """SQL for "still nobody has asked to cancel this run", evaluated by SQLite.

    The SQL twin of the Python read ``bool(row["cancel_requested"])`` that every
    guarded terminal writer branches on, and normalized to agree with it —
    ``coalesce`` because a NULL column reads as "not requested" on the Python side and
    NULL is not false here, ``CAST`` because JSON-free but untyped storage means a
    text ``'0'`` must not read as truthy. Same relationship, and the same reason, as
    ``notice_write_expectation`` and ``owed_notice_state_unchanged``.

    WHY IT BELONGS IN THE TERMINAL ``WHERE``. ``cancel_run`` is a separate
    transaction. It can land between a guarded writer's snapshot SELECT and that
    writer's UPDATE — pysqlite starts no transaction for the read, so the snapshot is
    genuinely older than the write — and ``cancel_requested`` is the ONLY signal
    distinguishing "this run failed" from "the user pressed Stop". A writer that reads
    it in Python and then does not re-assert it overwrites the Stop with ``failed``
    and, worse, stamps an owed failure notice: the user is told their task broke
    because they cancelled it.

    A FILTER on a row already located by primary key, so no query plan depends on its
    text.
    """

    return cast(func.coalesce(agent_runs.c.cancel_requested, 0), Integer) == 0


def _cancel_aware_terminal_status(
    row: Any,
    requested_status: Any,
) -> tuple[str, list[Any]]:
    """Decide one terminal status and return the CAS guards for that snapshot."""

    status = normalize_run_status(requested_status)
    cancel_requested = bool(row["cancel_requested"])
    if cancel_requested and status == "failed":
        status = "canceled"
    guards = [
        agent_runs.c.status.in_(
            _status_query_values("queued") + _status_query_values("running")
        )
    ]
    if not cancel_requested:
        guards.append(cancel_not_requested())
    return status, guards


def _deferred_metadata_for_settlement(
    parked: Any,
    *,
    run_id: str,
    settling_status: Any,
    deferred_status: Any,
) -> Optional[dict[str, Any]]:
    """THE SETTLEMENT EQUALITY RULE for a parked terminal cause. ONE copy, TWO callers.

    Used by BOTH settling consumers of the ``deferred_terminal_*`` family —
    :meth:`SQLiteBackgroundTaskStore.record_run_output` and
    :meth:`SQLiteBackgroundTaskStore.settle_deferred_run`. **A third consumer must call
    this, not re-derive it**: the rule was written once for HFR-329, left inline, and
    the other consumer settled contradictory rows for a whole review round (HFR-331).

    The parked metadata rides along IFF the status that ACTUALLY settles equals the
    deferred one — i.e. the parked cause won or tied the arbitration. ``settling_status``
    must therefore be the value the caller is about to WRITE, read from inside its own
    bounded re-read, never a pre-loop request: ``_cancel_aware_terminal_status`` turns a
    ``failed`` into ``canceled`` when a user's Stop lands under the first CAS, and that
    is a different outcome from the parked one.

    STRICTER THAN THE SIBLING ``deferred_terminal_error``, deliberately, which still
    overrides unconditionally in both callers (§10.3 covers contradictory TEXT through
    the equality-guarded backfill instead). ``interrupt_reason`` is not text: it selects
    the notice IDENTITY (``interrupt:{run}:{reason}`` vs the bare run id the live path's
    dedup key resolves to) and it removes the row from derived health by membership in
    ``RUN_INTERRUPTION_REASONS``. Merging a cause into an outcome it did not win is
    therefore wrong in two user-visible systems at once — and the case that matters most
    is the inversion: a user's Stop settling ``canceled`` while carrying
    ``interrupt_reason=evicted``, infrastructure metadata describing user intent, which
    is HFR-012/037's inversion in metadata form.

    Returns the metadata to merge, or ``None`` when the cause was superseded. THE KEY IS
    POPPED BY THE CALLER EITHER WAY: the call consumes the deferred intent, and a key
    left in ``result_payload_json`` is a parked cause with no settlement left to reach —
    stale forever and replayable by the next reader of the family.
    """

    if not isinstance(parked, dict) or not parked:
        return None
    settling = normalize_run_status(settling_status)
    if settling == normalize_run_status(deferred_status):
        return dict(parked)
    logger.debug(
        "run %s: parked terminal cause (%s) superseded by the settling outcome (%s); "
        "settling without it",
        run_id,
        normalize_run_status(deferred_status),
        settling,
    )
    return None


def _coalesced_terminal_write(
    conn: Any, row: Any, *, run_id: str, ok: bool, error: Optional[str], now: str
) -> Optional[tuple[dict[str, Any], list[Any]]]:
    """The values and the CAS predicates one coalesced run's settlement needs.

    ``None`` means "write nothing": the row was already settled by another actor.
    Without that skip the UPDATE rewrites a row wholesale — a ``record_run_output``
    success that landed first becomes ``failed`` — because the write carries no status
    predicate of its own.

    The two branches guard DIFFERENTLY, and that asymmetry is the point:

    * the ``canceled`` branch is reached because the snapshot ALREADY saw the cancel,
      so it keeps ``canceled`` in its status list and falls through — a
      cancel-requested row goes on being normalized to ``canceled`` exactly as before,
      and a second cancel landing under it changes nothing it would write;
    * the ``succeeded``/``failed`` branch is reached because the snapshot saw NO
      cancel, so it re-asserts that (``cancel_not_requested``) and DROPS ``canceled``
      from its status list. Both halves are needed: the status predicate stops the
      write landing on a row already flipped to ``canceled`` by ``cancel_run`` (which
      does that for a queued row), and the ``cancel_requested`` predicate stops it
      landing on a RUNNING row where ``cancel_run`` set only the flag.
    """

    status = normalize_run_status(row["status"])
    if status in TERMINAL_RUN_STATUSES and status != "canceled":
        return None
    values: dict[str, Any] = {"updated_at": now}
    nonterminal = _status_query_values("queued") + _status_query_values("running")
    if bool(row["cancel_requested"]) or status == "canceled":
        values["status"] = "canceled"
        values["completed_at"] = now
        predicates = [agent_runs.c.status.in_(nonterminal + _status_query_values("canceled"))]
    else:
        values["status"] = "succeeded" if ok else "failed"
        values["completed_at"] = now
        if error is not None:
            values["error"] = error
        predicates = [agent_runs.c.status.in_(nonterminal), cancel_not_requested()]
    _merge_owed_failure_notice(
        values,
        conn=conn,
        run_id=run_id,
        status=values["status"],
        source_kind=row["source_kind"],
        parent_run_id=row["parent_run_id"],
        row_metadata_json=row["metadata_json"],
        now=now,
    )
    return values, predicates


def complete_coalesced_agent_runs_for_workbench_in_connection(
    conn: Any,
    run_ids: list[str],
    *,
    ok: bool,
    error: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> list[str]:
    normalized_run_ids: list[str] = []
    seen: set[str] = set()
    for raw_run_id in run_ids:
        run_id = str(raw_run_id or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        normalized_run_ids.append(run_id)
    if not normalized_run_ids:
        return []
    rows = {
        row["id"]: row
        for row in conn.execute(select(agent_runs).where(agent_runs.c.id.in_(normalized_run_ids))).mappings()
    }
    now = completed_at or _utc_now_iso()
    completed_ids: list[str] = []
    for run_id in normalized_run_ids:
        row = rows.get(run_id)
        # ONE re-read on a refused write, never a loop. A refusal means the row moved
        # under the batch snapshot, so the decision has to be made again from what the
        # row NOW says — otherwise a run the user cancelled mid-flight is simply left
        # ``running`` and nothing ever settles it, which trades a wrong status for a
        # zombie. The retry is bounded at one because its own snapshot already sees the
        # cancel: a second racing writer would have to land inside the retry itself,
        # and this runs on the Workbench completion path where an unbounded retry is
        # its own outage.
        for final_attempt in (False, True):
            if row is None:
                break
            plan = _coalesced_terminal_write(
                conn, row, run_id=run_id, ok=ok, error=error, now=now
            )
            if plan is None:
                break
            values, predicates = plan
            result = conn.execute(
                update(agent_runs).where(agent_runs.c.id == run_id).where(*predicates).values(**values)
            )
            if result.rowcount:
                completed_ids.append(run_id)
                break
            if final_attempt:
                break
            row = (
                conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1))
                .mappings()
                .first()
            )
    _defer_run_ids_updated_from_connection(conn, completed_ids)
    return completed_ids


def claim_queued_runs_for_workbench_in_connection(
    conn: Any,
    run_ids: list[str],
    *,
    started_at: Optional[str] = None,
) -> list[str]:
    normalized_run_ids: list[str] = []
    seen: set[str] = set()
    for raw_run_id in run_ids:
        run_id = str(raw_run_id or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        normalized_run_ids.append(run_id)
    queued_run_ids, stale_run_ids = inspect_queued_runs_for_workbench_in_connection(conn, normalized_run_ids)
    if stale_run_ids or queued_run_ids != normalized_run_ids:
        return []
    primary_run_id = normalized_run_ids[0] if normalized_run_ids else ""
    if not primary_run_id:
        return []
    now = started_at or _utc_now_iso()
    rows = {
        row["id"]: row
        for row in conn.execute(select(agent_runs).where(agent_runs.c.id.in_(normalized_run_ids))).mappings()
    }
    for run_id in normalized_run_ids:
        row = rows[run_id]
        metadata = _json_loads(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["workbench_queue_holds_run"] = run_id != primary_run_id
        metadata["effective_run_id"] = primary_run_id
        if run_id == primary_run_id and len(normalized_run_ids) > 1:
            metadata["coalesced_queue"] = _coalesced_agent_run_metadata(rows, normalized_run_ids)
        if run_id != primary_run_id:
            metadata["coalesced_into_run_id"] = primary_run_id
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .where(agent_runs.c.status.in_(_status_query_values("queued")))
                .values(
                    updated_at=now,
                    metadata_json=_json_dumps(metadata),
                )
            )
            if not result.rowcount:
                raise RuntimeError(f"failed to claim queued agent run {run_id}")
            continue
        result = conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .where(agent_runs.c.status.in_(_status_query_values("queued")))
            .values(
                status="running",
                started_at=now,
                updated_at=now,
                metadata_json=_json_dumps(metadata),
            )
        )
        if not result.rowcount:
            raise RuntimeError(f"failed to claim queued agent run {run_id}")
    _defer_run_ids_updated_from_connection(conn, normalized_run_ids)
    return normalized_run_ids


def hold_running_agent_run_for_workbench_in_connection(
    conn: Any,
    run_id: str,
    *,
    delivery_outcome: Optional[dict[str, Any]] = None,
) -> bool:
    """Transfer a claimed Agent Run to the durable Workbench queue.

    The caller persists the matching queued message in the same write
    transaction. The queue row therefore cannot become flushable while the
    scheduler still owns the Run as ``running``.
    """

    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return False
    row = conn.execute(
        select(agent_runs)
        .where(agent_runs.c.id == normalized_run_id)
        .limit(1)
    ).mappings().first()
    if row is None:
        return False
    metadata = _json_loads(row["metadata_json"], {})
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["workbench_queue_holds_run"] = True
    if delivery_outcome is not None:
        metadata["delivery_outcome"] = dict(delivery_outcome)
    now = _utc_now_iso()
    result = conn.execute(
        update(agent_runs)
        .where(agent_runs.c.id == normalized_run_id)
        .where(agent_runs.c.status.in_(_status_query_values("running")))
        .where(agent_runs.c.cancel_requested == 0)
        .values(
            status="queued",
            started_at=None,
            updated_at=now,
            metadata_json=_json_dumps(metadata),
        )
    )
    if not result.rowcount:
        return False
    _defer_run_ids_updated_from_connection(conn, [normalized_run_id])
    return True


def agent_run_cancellation_won_in_connection(conn: Any, run_id: str) -> bool:
    """Whether cancellation already owns a refused queue handoff."""

    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return False
    row = conn.execute(
        select(agent_runs.c.status, agent_runs.c.cancel_requested)
        .where(agent_runs.c.id == normalized_run_id)
        .limit(1)
    ).mappings().first()
    if row is None:
        return False
    return bool(row["cancel_requested"]) or normalize_run_status(
        row["status"]
    ) == "canceled"


def cancel_workbench_queued_agent_run_in_connection(
    conn: Any,
    run_id: str,
    *,
    session_id: str,
) -> bool:
    """Cancel a Run only while the named Workbench queue still owns it.

    Queue-row deletion and this transition share the caller's transaction. A
    concurrent claim/settlement therefore either wins before this guard (and the
    row is not removed) or loses after both cancellation and deletion commit.
    Missing Run rows are stale queue input and may be removed.
    """

    normalized_run_id = str(run_id or "").strip()
    normalized_session_id = str(session_id or "").strip()
    if not normalized_run_id or not normalized_session_id:
        return False
    row = conn.execute(
        select(agent_runs)
        .where(agent_runs.c.id == normalized_run_id)
        .limit(1)
    ).mappings().first()
    if row is None:
        return True
    metadata = _json_loads(row["metadata_json"], {})
    if (
        normalize_run_status(row["status"]) != "queued"
        or str(row["session_id"] or "").strip() != normalized_session_id
        or not isinstance(metadata, dict)
        or metadata.get("workbench_queue_holds_run") is not True
    ):
        return False
    now = _utc_now_iso()
    transition = conn.execute(
        update(agent_runs)
        .where(agent_runs.c.id == normalized_run_id)
        .where(agent_runs.c.session_id == normalized_session_id)
        .where(agent_runs.c.status.in_(_status_query_values("queued")))
        .where(agent_runs.c.metadata_json == row["metadata_json"])
        .values(
            status="canceled",
            cancel_requested=1,
            cancel_requested_at=now,
            completed_at=now,
            updated_at=now,
        )
    )
    if not transition.rowcount:
        return False
    updated = conn.execute(
        select(agent_runs)
        .where(agent_runs.c.id == normalized_run_id)
        .limit(1)
    ).mappings().one()
    _defer_run_rows_updated_from_connection(conn, [updated])
    return True


def record_agent_run_delivery_outcome_in_connection(
    conn: Any,
    run_id: str,
    outcome: dict[str, Any],
) -> bool:
    """Merge the observed delivery transition without changing Run ownership."""

    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return False
    result = conn.execute(
        update(agent_runs)
        .where(agent_runs.c.id == normalized_run_id)
        .where(func.json_valid(agent_runs.c.metadata_json) == 1)
        .values(
            updated_at=_utc_now_iso(),
            metadata_json=func.json_set(
                agent_runs.c.metadata_json,
                "$.delivery_outcome",
                func.json(_json_dumps(dict(outcome))),
            ),
        )
    )
    if not result.rowcount:
        return False
    _defer_run_ids_updated_from_connection(conn, [normalized_run_id])
    return True


def _refresh_recovered_coalesced_workbench_runs_in_connection(conn: Any, *, now: str) -> None:
    rows = list(
        conn.execute(
            select(agent_runs)
            .where(agent_runs.c.run_type == "agent_run")
            .where(agent_runs.c.status.in_(_status_query_values("queued")))
        ).mappings()
    )
    rows_by_id = {row["id"]: row for row in rows}
    processed: set[str] = set()
    for row in rows:
        run_id = str(row["id"] or "")
        if run_id in processed:
            continue
        metadata = _json_loads(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        coalesced = metadata.get("coalesced_queue") if isinstance(metadata, dict) else None
        raw_ids = coalesced.get("execution_ids") if isinstance(coalesced, dict) else None
        if not isinstance(raw_ids, list):
            continue
        run_ids: list[str] = []
        for value in raw_ids:
            coalesced_id = str(value or "").strip()
            if coalesced_id and coalesced_id not in run_ids:
                run_ids.append(coalesced_id)
        if run_id not in run_ids:
            run_ids.insert(0, run_id)
        live_ids = [
            candidate
            for candidate in run_ids
            if candidate in rows_by_id
            and not bool(rows_by_id[candidate]["cancel_requested"])
            and normalize_run_status(rows_by_id[candidate]["status"]) == "queued"
        ]
        if not live_ids:
            processed.update(run_ids)
            continue
        primary_id = live_ids[0]
        live_rows = {candidate: rows_by_id[candidate] for candidate in live_ids}
        primary_metadata = _json_loads(rows_by_id[primary_id]["metadata_json"], {})
        if not isinstance(primary_metadata, dict):
            primary_metadata = {}
        primary_metadata["workbench_queue_holds_run"] = False
        primary_metadata["effective_run_id"] = primary_id
        primary_metadata.pop("coalesced_into_run_id", None)
        if len(live_ids) > 1:
            primary_metadata["coalesced_queue"] = _coalesced_agent_run_metadata(live_rows, live_ids)
        else:
            primary_metadata.pop("coalesced_queue", None)
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == primary_id)
            .values(metadata_json=_json_dumps(primary_metadata), updated_at=now)
        )
        for child_id in live_ids[1:]:
            child_metadata = _json_loads(rows_by_id[child_id]["metadata_json"], {})
            if not isinstance(child_metadata, dict):
                child_metadata = {}
            child_metadata["workbench_queue_holds_run"] = True
            child_metadata["effective_run_id"] = primary_id
            child_metadata["coalesced_into_run_id"] = primary_id
            child_metadata.pop("coalesced_queue", None)
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == child_id)
                .values(metadata_json=_json_dumps(child_metadata), updated_at=now)
            )
        processed.update(run_ids)


def inspect_queued_runs_for_workbench_in_connection(conn: Any, run_ids: list[str]) -> tuple[list[str], list[str]]:
    normalized_run_ids: list[str] = []
    seen: set[str] = set()
    for raw_run_id in run_ids:
        run_id = str(raw_run_id or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        normalized_run_ids.append(run_id)
    if not normalized_run_ids:
        return [], []
    rows = {
        row["id"]: row
        for row in conn.execute(select(agent_runs).where(agent_runs.c.id.in_(normalized_run_ids))).mappings()
    }
    queued_run_ids: list[str] = []
    stale_run_ids: list[str] = []
    cancel_requested_run_ids: list[str] = []
    for run_id in normalized_run_ids:
        row = rows.get(run_id)
        if row is None:
            stale_run_ids.append(run_id)
            continue
        if bool(row["cancel_requested"]):
            if normalize_run_status(row["status"]) == "queued":
                cancel_requested_run_ids.append(run_id)
            stale_run_ids.append(run_id)
            continue
        if normalize_run_status(row["status"]) != "queued":
            stale_run_ids.append(run_id)
            continue
        queued_run_ids.append(run_id)
    if cancel_requested_run_ids:
        now = _utc_now_iso()
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id.in_(cancel_requested_run_ids))
            .where(agent_runs.c.status.in_(_status_query_values("queued")))
            .values(status="canceled", completed_at=now, updated_at=now)
        )
        _defer_run_ids_updated_from_connection(conn, cancel_requested_run_ids)
    return queued_run_ids, stale_run_ids


def reset_workbench_claimed_runs_in_connection(conn: Any, run_ids: list[str]) -> None:
    now = _utc_now_iso()
    seen: set[str] = set()
    changed_ids: list[str] = []
    for raw_run_id in run_ids:
        run_id = str(raw_run_id or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        row = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
        if not row:
            continue
        metadata = _json_loads(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["workbench_queue_holds_run"] = True
        metadata.pop("effective_run_id", None)
        metadata.pop("coalesced_into_run_id", None)
        values = {
            "updated_at": now,
            "metadata_json": _json_dumps(metadata),
        }
        if normalize_run_status(row["status"]) == "running":
            values["status"] = "queued"
            values["started_at"] = None
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(**values)
        )
        changed_ids.append(run_id)
    _defer_run_ids_updated_from_connection(conn, changed_ids)


def upsert_definition_in_connection(
    conn: Any,
    values: dict[str, Any],
    *,
    expect: DefinitionWriteExpectation | None,
    definition_type: str,
) -> bool:
    """The one full-row ``run_definitions`` write, guarded, in a CALLER'S transaction.

    Separated from ``_upsert_definition`` so a guarded stamp and the durable effect it
    authorises can be committed together (HFR-269). ``False`` means the write was
    refused and the caller's transaction must not persist anything that depended on
    it.
    """

    existing = conn.execute(
        select(run_definitions.c.id).where(run_definitions.c.id == values["id"]).limit(1)
    ).scalar_one_or_none()
    if not existing:
        conn.execute(insert(run_definitions).values(**values))
        return True
    stmt = update(run_definitions).where(run_definitions.c.id == values["id"])
    if expect is not None:
        # RE-ASSERT what the payload was decided from. The read that produced it
        # reserves nothing -- it happened in the caller, one layer and possibly many
        # statements ago, and even the ``existing`` SELECT above takes no write lock
        # (pysqlite emits no ``BEGIN`` for a bare SELECT), so the lock is first taken
        # here.
        stmt = stmt.where(*definition_state_unchanged(expect))
    result = conn.execute(stmt.values(**values))
    if result.rowcount:
        return True
    # LOST. Nothing was written, so nothing may be reported as written: the counters
    # and the ledger a reclaim credited stay true, and the caller decides what to tell
    # the user (``DefinitionWriteConflict`` for a user action, a ``False`` return for a
    # best-effort runtime stamp).
    logger.warning(
        "Refused a stale full-row write for %s %s: its Session binding, enabled "
        "state, deletion or reclaim snapshot changed after the payload was read",
        definition_type,
        values["id"],
    )
    return False


def enqueue_run_in_connection(conn: Any, values: dict[str, Any]) -> None:
    """Write one ``agent_runs`` outbox row in a CALLER'S transaction.

    The event snapshot is deferred to the transaction, so subscribers are told about
    a run only once the row they would read is committed.
    """

    existing = conn.execute(
        select(agent_runs.c.id).where(agent_runs.c.id == values["id"]).limit(1)
    ).scalar_one_or_none()
    if existing:
        conn.execute(update(agent_runs).where(agent_runs.c.id == values["id"]).values(**values))
    else:
        conn.execute(insert(agent_runs).values(**values))
    _defer_run_ids_updated_from_connection(conn, [values["id"]])


class SQLiteBackgroundTaskStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or paths.get_sqlite_state_path()
        guard_source_checkout_default_state_migration(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path is None:
            from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config

            ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
        if not background_tables_ready(self.db_path):
            initialize_background_tables(self.db_path)
        ensure_background_indexes(self.db_path)
        self.engine = create_sqlite_engine(self.db_path)
        self._probe = SqliteInvalidationProbe(self.engine)

    def close(self) -> None:
        self._probe.close()
        self.engine.dispose()

    def maybe_reload(self) -> bool:
        return self._probe.has_external_write()

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        stmt = self._definitions_query("scheduled").order_by(
            run_definitions.c.created_at, run_definitions.c.id
        )
        with self.engine.connect() as conn:
            rows = [self._scheduled_task_from_row(row) for row in conn.execute(stmt).mappings()]
            return self._enrich_definitions(rows, conn, definition_type="scheduled")

    def list_scheduled_tasks_page(
        self,
        *,
        status: Optional[str] = None,
        query: Optional[str] = None,
        session_id: Optional[str] = None,
        page_request: PageRequest | None,
        newest_first: bool = True,
        include_successful_finished: bool = True,
        enabled_first: bool = False,
    ) -> PageResult[dict[str, Any]]:
        stmt = self._definitions_query(
            "scheduled",
            status=status,
            query=query,
            session_id=session_id,
            include_successful_finished=include_successful_finished,
        )
        activity = func.coalesce(
            run_definitions.c.last_run_at,
            run_definitions.c.updated_at,
            run_definitions.c.created_at,
            "",
        )
        if enabled_first:
            # Offset pagination needs a persisted ordering key. Lifecycle can
            # change when the clock crosses run_at between page requests.
            enabled_rank = case((run_definitions.c.enabled != 0, 0), else_=1)
            stmt = stmt.order_by(enabled_rank, run_definitions.c.created_at, run_definitions.c.id)
        elif newest_first:
            stmt = stmt.order_by(activity.desc(), run_definitions.c.id.desc())
        else:
            stmt = stmt.order_by(activity, run_definitions.c.id)
        if page_request is not None:
            stmt = stmt.offset(page_request.offset).limit(page_request.limit + 1)
        with self.engine.connect() as conn:
            rows = [self._scheduled_task_from_row(row) for row in conn.execute(stmt).mappings()]
            self._enrich_definitions(rows, conn, definition_type="scheduled")
        return page_result_from_limit_plus_one(rows, page_request)

    def count_scheduled_tasks(
        self, *, query: Optional[str] = None, session_id: Optional[str] = None
    ) -> dict[str, int]:
        return self._definition_counts("scheduled", query=query, session_id=session_id)

    def get_scheduled_task(self, definition_id: str) -> Optional[dict[str, Any]]:
        stmt = self._definitions_query("scheduled").where(run_definitions.c.id == definition_id).limit(1)
        with self.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            if row is None:
                return None
            return self._enrich_definitions(
                [self._scheduled_task_from_row(row)], conn, definition_type="scheduled"
            )[0]

    def upsert_scheduled_task(
        self, payload: dict[str, Any], *, expect: DefinitionWriteExpectation | None = None
    ) -> bool:
        """Write a whole scheduled-task row. ``False`` means the write was REFUSED.

        ``expect`` is the state the payload was derived from (see
        ``DefinitionWriteExpectation``); pass it from every caller that read the
        definition before building the payload, so a teardown committed in between
        cannot be silently reverted.
        """

        return self._upsert_definition(
            self._scheduled_task_values(payload), expect=expect, definition_type="scheduled task"
        )

    def upsert_scheduled_task_with_binding_notice(
        self,
        payload: dict[str, Any],
        *,
        expect: DefinitionWriteExpectation,
        notice: dict[str, Any],
    ) -> bool:
        """Commit a recovery marker and its owed notice as one durable effect."""

        with self.engine.begin() as conn:
            self.stamp_binding_change_notice(_conn=conn, **notice)
            landed = upsert_definition_in_connection(
                conn,
                self._scheduled_task_values(payload),
                expect=expect,
                definition_type="scheduled task",
            )
            if not landed:
                # A normal return from ``engine.begin()`` COMMITs. The notice was
                # written earlier in this transaction, so an explicit rollback is
                # what keeps a refused definition CAS from publishing stale news.
                conn.rollback()
                return False
        return True

    def remove_task(self, definition_id: str, *, deleted_at: Optional[str] = None) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                update(run_definitions)
                .where(run_definitions.c.id == definition_id)
                .where(run_definitions.c.deleted_at.is_(None))
                .values(deleted_at=deleted_at or _utc_now_iso())
            )
            return bool(result.rowcount)

    def set_definition_enabled(
        self,
        definition_id: str,
        enabled: bool,
        *,
        definition_type: Optional[str] = None,
    ) -> bool:
        with self.engine.begin() as conn:
            values: dict[str, Any] = {"enabled": 1 if enabled else 0, "updated_at": _utc_now_iso()}
            if enabled:
                # Resuming may start a new lifecycle, and the old one must stop
                # deciding the row's state when it does. The rule needs to know
                # what the row *is*, so read before writing — and do it here, at
                # the single UPDATE every caller reaches, rather than asking each
                # caller to remember. The Harness UI toggle skipping this is what
                # made a resumed-then-paused watch vanish from its own list.
                current = (
                    conn.execute(
                        select(
                            run_definitions.c.definition_type,
                            run_definitions.c.mode,
                            run_definitions.c.enabled,
                        )
                        .where(run_definitions.c.id == definition_id)
                        .where(run_definitions.c.deleted_at.is_(None))
                    )
                    .mappings()
                    .first()
                )
                if current is not None and not current["enabled"]:
                    clear_columns = definition_resume_clear_columns(
                        current["definition_type"], current["mode"]
                    )
                    values.update(dict.fromkeys(clear_columns, None))
            stmt = (
                update(run_definitions)
                .where(run_definitions.c.id == definition_id)
                .where(run_definitions.c.deleted_at.is_(None))
                .values(**values)
            )
            if definition_type is not None:
                stmt = stmt.where(run_definitions.c.definition_type == definition_type)
            result = conn.execute(stmt)
            return bool(result.rowcount)

    def list_watches(self) -> list[dict[str, Any]]:
        stmt = self._definitions_query("watch").order_by(
            run_definitions.c.created_at, run_definitions.c.id
        )
        with self.engine.connect() as conn:
            rows = [self._watch_from_row(row) for row in conn.execute(stmt).mappings()]
            return self._enrich_definitions(rows, conn, definition_type="watch")

    def list_watches_page(
        self,
        *,
        status: Optional[str] = None,
        query: Optional[str] = None,
        session_id: Optional[str] = None,
        page_request: PageRequest | None,
        newest_first: bool = True,
        include_successful_finished: bool = True,
        enabled_first: bool = False,
    ) -> PageResult[dict[str, Any]]:
        stmt = self._definitions_query(
            "watch",
            status=status,
            query=query,
            session_id=session_id,
            include_successful_finished=include_successful_finished,
        )
        activity = func.coalesce(
            run_definitions.c.last_event_at,
            run_definitions.c.last_started_at,
            run_definitions.c.updated_at,
            run_definitions.c.created_at,
            "",
        )
        if enabled_first:
            # Runtime and execution state may change between pages; the stored
            # switch keeps the list order stable while those facts are enriched.
            enabled_rank = case((run_definitions.c.enabled != 0, 0), else_=1)
            stmt = stmt.order_by(enabled_rank, run_definitions.c.created_at, run_definitions.c.id)
        elif newest_first:
            stmt = stmt.order_by(activity.desc(), run_definitions.c.id.desc())
        else:
            stmt = stmt.order_by(activity, run_definitions.c.id)
        if page_request is not None:
            stmt = stmt.offset(page_request.offset).limit(page_request.limit + 1)
        with self.engine.connect() as conn:
            rows = [self._watch_from_row(row) for row in conn.execute(stmt).mappings()]
            self._enrich_definitions(rows, conn, definition_type="watch")
        return page_result_from_limit_plus_one(rows, page_request)

    def count_watches(
        self, *, query: Optional[str] = None, session_id: Optional[str] = None
    ) -> dict[str, int]:
        return self._definition_counts("watch", query=query, session_id=session_id)

    def get_watch(self, watch_id: str) -> Optional[dict[str, Any]]:
        stmt = self._definitions_query("watch").where(run_definitions.c.id == watch_id).limit(1)
        with self.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            if row is None:
                return None
            return self._enrich_definitions([self._watch_from_row(row)], conn, definition_type="watch")[0]

    def upsert_watch(
        self, payload: dict[str, Any], *, expect: DefinitionWriteExpectation | None = None
    ) -> bool:
        """Write a whole watch row. ``False`` means the write was REFUSED.

        The twin of ``upsert_scheduled_task``: same table, same full-row shape, same
        guard. Watches are reclaimed by the same ``reclaim_bound_definitions`` call,
        so guarding only the task side would leave the identical hole open.
        """

        return self._upsert_definition(
            self._watch_values(payload), expect=expect, definition_type="watch"
        )

    def _upsert_definition(
        self,
        values: dict[str, Any],
        *,
        expect: DefinitionWriteExpectation | None,
        definition_type: str,
    ) -> bool:
        """The one full-row ``run_definitions`` write, guarded once for both types."""

        with self.engine.begin() as conn:
            return upsert_definition_in_connection(
                conn, values, expect=expect, definition_type=definition_type
            )

    def upsert_watch_with_queued_run(
        self,
        payload: dict[str, Any],
        *,
        expect: DefinitionWriteExpectation | None,
        run_payload: dict[str, Any],
    ) -> bool:
        """A guarded watch stamp and the outbox row it authorises, ONE transaction.

        HFR-269 -- the bug this method exists to remove. HFR-267 put the guarded stamp
        BEFORE the enqueue, which is necessary and was not sufficient, because two
        commits are not one decision:

            self.store.mark_cycle_result(...)           # transaction 1, COMMITS
            self.request_store.enqueue_hook_send(...)    # transaction 2, COMMITS

        A ``/new`` reclaim or an archive from another connection can commit in the gap
        between those two commits. The stamp is then accepted -- it won its
        compare-and-set fairly, before the teardown -- and the hook is queued
        afterwards anyway, against a definition the database has since paused or
        soft-deleted. The guard refuses nothing because there is nothing left to
        refuse: the ordering change moved the race window, it did not close it.

        The inverse was the other half: an exception between the two commits left a
        ``once``/terminal watch durably disabled with its completion hook LOST, so the
        user is never told the watch finished and the definition cannot say why.

        Both are the same defect -- the stamp and the effect it authorises were
        separate transactions. Here they are one: the outbox row is written on the same
        connection, after the guard, and a refusal or an exception rolls BOTH back.
        ``False`` means nothing was written; the watch is untouched and no hook exists.
        """

        values = self._watch_values(payload)
        run_values = self._run_values(run_payload)
        with run_update_event_transaction(self.engine) as conn:
            if not upsert_definition_in_connection(
                conn, values, expect=expect, definition_type="watch"
            ):
                return False
            enqueue_run_in_connection(conn, run_values)
        return True

    def enqueue_run(self, payload: dict[str, Any]) -> None:
        values = self._run_values(payload)
        with run_update_event_transaction(self.engine) as conn:
            enqueue_run_in_connection(conn, values)

    def list_runs(self, *, status: Optional[str] = None) -> list[dict[str, Any]]:
        stmt = self._runs_query(status=status).order_by(agent_runs.c.created_at, agent_runs.c.id)
        with self.engine.connect() as conn:
            return [self._run_from_row(row) for row in conn.execute(stmt).mappings()]

    def list_runs_page(
        self,
        *,
        status: Optional[str] = None,
        run_type: Optional[str] = None,
        exclude_run_type: Optional[Sequence[str]] = None,
        agent_name: Optional[str] = None,
        agent_backend: Optional[str] = None,
        session_id: Optional[str] = None,
        definition_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        query: Optional[str] = None,
        page_request: PageRequest | None,
        newest_first: bool = True,
    ) -> PageResult[dict[str, Any]]:
        stmt = self._runs_query(
            status=status,
            run_type=run_type,
            exclude_run_type=exclude_run_type,
            agent_name=agent_name,
            agent_backend=agent_backend,
            session_id=session_id,
            definition_id=definition_id,
            created_after=created_after,
            created_before=created_before,
            query=query,
        )
        if newest_first:
            stmt = stmt.order_by(agent_runs.c.created_at.desc(), agent_runs.c.id.desc())
        else:
            stmt = stmt.order_by(agent_runs.c.created_at, agent_runs.c.id)
        if page_request is not None:
            stmt = stmt.offset(page_request.offset).limit(page_request.limit + 1)
        with self.engine.connect() as conn:
            rows = self._enrich_runs(
                [self._run_from_row(row) for row in conn.execute(stmt).mappings()], conn
            )
        return page_result_from_limit_plus_one(rows, page_request)

    def count_runs(
        self,
        *,
        status: Optional[str] = None,
        run_type: Optional[str] = None,
        exclude_run_type: Optional[Sequence[str]] = None,
        agent_name: Optional[str] = None,
        agent_backend: Optional[str] = None,
        session_id: Optional[str] = None,
        definition_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        query: Optional[str] = None,
    ) -> int:
        stmt = self._runs_query(
            status=status,
            run_type=run_type,
            exclude_run_type=exclude_run_type,
            agent_name=agent_name,
            agent_backend=agent_backend,
            session_id=session_id,
            definition_id=definition_id,
            created_after=created_after,
            created_before=created_before,
            query=query,
            count=True,
        )
        with self.engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one() or 0)

    def count_runs_by_status(
        self,
        *,
        run_type: Optional[str] = None,
        exclude_run_type: Optional[Sequence[str]] = None,
        agent_name: Optional[str] = None,
        agent_backend: Optional[str] = None,
        session_id: Optional[str] = None,
        definition_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        query: Optional[str] = None,
    ) -> dict[str, int]:
        stmt = self._runs_query(
            run_type=run_type,
            exclude_run_type=exclude_run_type,
            agent_name=agent_name,
            agent_backend=agent_backend,
            session_id=session_id,
            definition_id=definition_id,
            created_after=created_after,
            created_before=created_before,
            query=query,
            columns=(agent_runs.c.status, func.count()),
        ).group_by(agent_runs.c.status)
        counts = {key: 0 for key in RUN_STATUS_COUNTS}
        with self.engine.connect() as conn:
            for raw_status, count in conn.execute(stmt).all():
                public_status = normalize_run_status(raw_status)
                if public_status not in counts:
                    counts[public_status] = 0
                value = int(count or 0)
                counts[public_status] += value
                counts["all"] += value
        return counts

    def list_run_types(self) -> list[str]:
        """The run types actually present in the ledger, for the type selector.

        The UI knows the types it has words for, but not the ones it does not:
        ``webhook`` is written by the scheduler and preserved by the
        compatibility importer, yet a hardcoded option list omits it, and search
        deliberately skips ``run_type`` because it is a translated chip. Between
        them a row was visible under All and unreachable by any filter.

        Reading the distinct values closes that by construction. Unfiltered on
        purpose — a facet that narrows to the current filter would delete the
        option the user needs to switch to — and index-only over
        ``ix_agent_runs_type_status_created``.
        """
        stmt = (
            select(agent_runs.c.run_type)
            .where(agent_runs.c.run_type.is_not(None))
            .distinct()
            .order_by(agent_runs.c.run_type)
        )
        with self.engine.connect() as conn:
            return [value for (value,) in conn.execute(stmt).all() if value]

    def _runs_query(
        self,
        *,
        status: Optional[str] = None,
        run_type: Optional[str] = None,
        exclude_run_type: Optional[Sequence[str]] = None,
        agent_name: Optional[str] = None,
        agent_backend: Optional[str] = None,
        session_id: Optional[str] = None,
        definition_id: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        query: Optional[str] = None,
        count: bool = False,
        columns: Any = None,
    ):
        if columns is not None:
            stmt = select(*columns) if isinstance(columns, tuple) else select(columns)
        elif count:
            stmt = select(func.count()).select_from(agent_runs)
        else:
            stmt = select(agent_runs)
        if status:
            stmt = stmt.where(agent_runs.c.status.in_(_status_query_values(status)))
        if run_type:
            stmt = stmt.where(agent_runs.c.run_type == run_type)
        # Exclusion, not an include-list: the Runs tab hides watcher heartbeats by
        # default, and a run type added later must still show up by default rather
        # than silently vanish. Every count path takes the same argument so the
        # status badges never disagree with the rows on screen.
        excluded = [value for value in (exclude_run_type or []) if value]
        if excluded:
            stmt = stmt.where(
                or_(agent_runs.c.run_type.is_(None), agent_runs.c.run_type.notin_(excluded))
            )
        if agent_name:
            stmt = stmt.where(agent_runs.c.agent_name == agent_name)
        if agent_backend:
            stmt = stmt.where(agent_runs.c.agent_backend == agent_backend)
        if session_id:
            stmt = stmt.where(agent_runs.c.session_id == session_id)
        if definition_id:
            stmt = stmt.where(agent_runs.c.definition_id == definition_id)
        if created_after:
            stmt = stmt.where(agent_runs.c.created_at >= created_after)
        if created_before:
            stmt = stmt.where(agent_runs.c.created_at <= created_before)
        if query:
            stmt = stmt.where(or_(*self._run_search_predicates(_like_contains_pattern(query))))
        return stmt

    @staticmethod
    def _run_search_predicates(pattern: str) -> list[Any]:
        """Everything the Runs search can match, in one place.

        The rule: **a run is findable by every value its row displays.** The
        list projects more than it stores (plan §3) — the originating task/watch
        name and the resolved session labels are joins, not columns — and a list
        that cannot find what it shows on screen is worse than no search at all.

        The projected half is generated from ``_RUN_PROJECTIONS`` rather than
        listed here, so it cannot fall behind the enrichment that produces it:
        a new projection site is matched the moment it is declared. All of it is
        semi-joins/EXISTS inside the one statement — no extra round trip, no
        N+1 — and the count paths inherit it because every caller goes through
        ``_runs_query``.

        Deliberately *not* matched: values that are translated UI labels rather
        than data — the run-type chip ("Agent run"), the status chip, and the
        platform/scope-kind of a session. Each has its own selector, and a
        search box that matched English label text would find nothing for a user
        reading the UI in Chinese.
        """
        def like(column: Any) -> Any:
            return column.like(pattern, escape=_LIKE_ESCAPE)

        # The scope key an IM binding is stored under, rebuilt from the scope row
        # so a resolved channel name can be matched back to the key naming it.
        scope_key = scopes.c.platform + "::" + scopes.c.scope_type + "::" + scopes.c.native_id

        def bound_to_named_scope(key_column: Any) -> Any:
            """Runs whose scope key belongs to a scope whose display name matches.

            Not equality: a threaded key appends "::thread::<id>", which is the
            common shape, not the exception. Not a bare prefix either — Telegram
            "-100123" is a prefix of "-1001234", so the "::" boundary has to be
            part of the comparison or one channel's name finds another's runs.
            """
            return (
                select(1)
                .select_from(scopes)
                .where(like(scopes.c.display_name))
                .where(
                    or_(
                        key_column == scope_key,
                        func.substr(key_column, 1, func.length(scope_key) + 2) == scope_key + "::",
                    )
                )
                .correlate(agent_runs)
                .exists()
            )

        # Ids of rows whose own user-visible text matches, per projection source.
        # A workbench session shows its title; an IM one shows the channel's
        # display name, falling back to the native id. Soft-deleted definitions
        # match for the same reason _definition_summaries returns them — the run
        # still displays the name.
        matching_ids = {
            "session": select(agent_sessions.c.id)
            .select_from(SQLiteBackgroundTaskStore._session_scope_join())
            .where(
                or_(
                    like(agent_sessions.c.title),
                    like(scopes.c.display_name),
                    like(scopes.c.native_id),
                )
            ),
            "definition": select(run_definitions.c.id).where(like(run_definitions.c.name)),
        }

        predicates = [
            # Text stored on the run itself.
            like(agent_runs.c.id),
            like(agent_runs.c.agent_name),
            like(agent_runs.c.prompt),
            like(agent_runs.c.message),
            like(agent_runs.c.result_text),
            like(agent_runs.c.error),
            like(agent_runs.c.stdout),
            like(agent_runs.c.stderr),
        ]
        for site in _RUN_PROJECTIONS:
            # The raw id, so pasting one still works, and the projected text it
            # resolves to. Both read ``id_column``: ``id_field`` is the payload
            # name, which for a derived site is not a column at all.
            predicates.append(like(agent_runs.c[site.id_column]))
            projected_text = agent_runs.c[site.id_column].in_(matching_ids[site.source])
            if site.payload_key == "source_session":
                projected_text = and_(agent_runs.c.source_kind == "agent", projected_text)
            predicates.append(projected_text)
            for column in site.key_columns:
                # An IM binding stores "<platform>::<kind>::<native_id>", so the
                # raw match covers typing the platform or channel id, and the
                # scope match covers the display name the row actually shows.
                predicates.append(like(agent_runs.c[column]))
                predicates.append(bound_to_named_scope(agent_runs.c[column]))
        return predicates

    def _definitions_query(
        self,
        definition_type: str,
        *,
        status: Optional[str] = None,
        query: Optional[str] = None,
        session_id: Optional[str] = None,
        include_successful_finished: bool = True,
        columns: Any = None,
    ):
        lifecycle = definition_lifecycle_expression(definition_type)
        if columns is not None:
            stmt = select(*columns) if isinstance(columns, tuple) else select(columns)
        else:
            # Every row carries its state, resolved by the same expression the
            # counts group by.
            stmt = select(run_definitions, lifecycle.label("lifecycle_state"))
        stmt = (
            stmt.where(run_definitions.c.definition_type == definition_type)
            .where(run_definitions.c.deleted_at.is_(None))
        )
        # Precise bound-session filter (ix_run_definitions_session) — powers the
        # Harness "只看本会话" chip that background-work banner rows navigate into.
        if session_id:
            stmt = stmt.where(run_definitions.c.session_id == session_id)
        if status and status != "all":
            states = DEFINITION_STATUS_FILTERS.get(status)
            if not states:
                raise ValueError("status must be one of: " + ", ".join(DEFINITION_STATUS_FILTERS))
            stmt = stmt.where(lifecycle.in_(states))
        if not include_successful_finished:
            stmt = stmt.where(
                ~_successful_finished_definition_expression(definition_type, lifecycle)
            )
        if query:
            pattern = _like_contains_pattern(query)
            fields = [
                run_definitions.c.id,
                run_definitions.c.name,
                run_definitions.c.agent_name,
                run_definitions.c.session_id,
                run_definitions.c.legacy_session_key,
                run_definitions.c.message,
            ]
            if definition_type == "scheduled":
                fields.extend(
                    [
                        run_definitions.c.prompt,
                        run_definitions.c.schedule_type,
                        run_definitions.c.cron,
                        run_definitions.c.run_at,
                    ]
                )
            elif definition_type == "watch":
                fields.extend(
                    [
                        run_definitions.c.command_json,
                        run_definitions.c.shell_command,
                        run_definitions.c.prefix,
                        run_definitions.c.cwd,
                    ]
                )
            stmt = stmt.where(or_(*(field.like(pattern, escape=_LIKE_ESCAPE) for field in fields)))
        return stmt

    def _definition_counts(
        self,
        definition_type: str,
        *,
        query: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict[str, int]:
        lifecycle = definition_lifecycle_expression(definition_type)
        stmt = self._definitions_query(
            definition_type,
            query=query,
            session_id=session_id,
            columns=(lifecycle.label("lifecycle_state"), func.count()),
        ).group_by(lifecycle)
        counts = {key: 0 for key in DEFINITION_STATUS_COUNTS}
        with self.engine.connect() as conn:
            for state, count in conn.execute(stmt).all():
                value = int(count or 0)
                if state in counts:
                    counts[state] += value
                counts["total"] += value
        return counts

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
            if row is None:
                return None
            return self._enrich_runs([self._run_from_row(row)], conn)[0]

    def list_deferred_runs(self) -> list[dict[str, Any]]:
        """Return non-terminal Runs carrying a durable terminal intent."""

        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(agent_runs)
                    .where(
                        agent_runs.c.status.in_(
                            _status_query_values("queued")
                            + _status_query_values("running")
                        )
                    )
                    .order_by(agent_runs.c.created_at, agent_runs.c.id)
                ).mappings()
            )
        deferred: list[dict[str, Any]] = []
        for row in rows:
            result_payload = _json_loads(row["result_payload_json"], {})
            if isinstance(result_payload, dict) and result_payload.get("deferred_terminal_status"):
                deferred.append(self._run_from_row(row))
        return deferred

    def list_open_runs_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """Every still-open Run associated with one session, newest last.

        The DB half of a session teardown. A teardown knows which session it is
        reclaiming and needs the rows that session may still owe a terminal write —
        including the ones no in-memory map can name, which is the whole reason this
        read exists rather than a walk over ``_inflight_executions``: a
        ``create_per_run`` execution has no session lock key by design, so its only
        durable association is the ``session_id`` stamped onto the row at reservation
        (:meth:`stamp_run_session_id`).

        Deliberately UNFILTERED beyond "this session, not terminal". It returns
        ``queued`` rows, gate-parked holders and ``watch_runtime`` heartbeats too,
        because the decision about which of those a teardown may settle is a policy
        the caller owns and must state in one visible place — a read that quietly
        pre-narrowed would hide half the predicate in SQL where no reviewer of the
        settlement rule would look for it.

        Read-only: no row is written, so a caller that only wants to look costs
        nothing.
        """

        resolved = str(session_id or "").strip()
        if not resolved:
            return []
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(agent_runs)
                    .where(agent_runs.c.session_id == resolved)
                    .where(agent_runs.c.status.in_(NON_TERMINAL_RUN_STATUSES))
                    .order_by(agent_runs.c.created_at, agent_runs.c.id)
                ).mappings()
            )
        return [self._run_from_row(row) for row in rows]

    def list_pending_callbacks(self, *, limit: int = 20) -> list[dict[str, Any]]:
        terminal_statuses = _status_query_values("succeeded") + _status_query_values("failed") + _status_query_values("canceled")
        with self.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(agent_runs)
                    .where(agent_runs.c.callback_session_id.is_not(None))
                    .where(agent_runs.c.callback_session_id != "")
                    .where(agent_runs.c.callback_status == "pending")
                    .where(agent_runs.c.completed_at.is_not(None))
                    .where(agent_runs.c.status.in_(terminal_statuses))
                    .order_by(agent_runs.c.completed_at, agent_runs.c.id)
                    .limit(limit)
                ).mappings()
            )
            return [self._run_from_row(row) for row in rows]

    def cancel_run(self, run_id: str, *, requested_at: Optional[str] = None) -> bool:
        now = requested_at or _utc_now_iso()
        row_to_publish = None
        queue_session_id = ""
        with self.engine.begin() as conn:
            # Cancellation and Workbench queue retirement are one ownership
            # transition. Reserve the writer before reading so a concurrent
            # claim cannot move the Run between the decision and its row delete.
            reserve_write_lock(conn)
            row = conn.execute(
                select(agent_runs)
                .where(agent_runs.c.id == run_id)
                .limit(1)
            ).mappings().first()
            if not row:
                return False
            status = normalize_run_status(row["status"])
            metadata = _json_loads(row["metadata_json"], {})
            values: dict[str, Any] = {
                "cancel_requested": 1,
                "cancel_requested_at": now,
                "updated_at": now,
            }
            if status == "queued":
                values["status"] = "canceled"
                values["completed_at"] = now
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .values(**values)
            )
            if result.rowcount:
                row_to_publish = dict(
                    conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().one()
                )
                if (
                    status == "queued"
                    and isinstance(metadata, dict)
                    and metadata.get("workbench_queue_holds_run") is True
                ):
                    from storage import messages_service

                    session_id = str(row["session_id"] or "").strip()
                    if messages_service.delete_queued_agent_run(
                        conn,
                        session_id=session_id,
                        run_id=run_id,
                    ):
                        queue_session_id = session_id
        _publish_run_rows_updated([row_to_publish])
        _publish_queue_updated(queue_session_id)
        return row_to_publish is not None

    def claim_pending_run(self, run_id: str, *, started_at: str) -> Optional[dict[str, Any]]:
        row_to_publish = None
        payload = None
        with self.engine.begin() as conn:
            row = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
            if not row:
                return None
            if bool(row["cancel_requested"]) or normalize_run_status(row["status"]) == "canceled":
                conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.id == run_id)
                    .values(status="canceled", completed_at=started_at, updated_at=started_at)
                )
                row_to_publish = dict(
                    conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().one()
                )
                payload = None
            else:
                result = conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.id == run_id)
                    .where(agent_runs.c.status.in_(_status_query_values("queued")))
                    .values(status="running", started_at=started_at, updated_at=started_at)
                )
                if not result.rowcount:
                    return None
                row = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
                if row:
                    row_to_publish = dict(row)
                    payload = self._run_from_row(row)
        _publish_run_rows_updated([row_to_publish])
        return payload

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        updated_at: str,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        pid: Optional[int] = None,
        definition_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
        result_text: Optional[str] = None,
        result_payload: Optional[dict[str, Any]] = None,
        message_ids: Optional[list[str]] = None,
        cancel_requested: Optional[bool] = None,
        cancel_requested_at: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        callback_status: Optional[str] = None,
        callback_error: Optional[str] = None,
        callback_run_id: Optional[str] = None,
        callback_completed_at: Optional[str] = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "updated_at": updated_at,
        }
        if started_at is not None:
            values["started_at"] = started_at
        if completed_at is not None:
            values["completed_at"] = completed_at
        if exit_code is not None:
            values["exit_code"] = exit_code
        if error is not None:
            values["error"] = error
        if stdout is not None:
            values["stdout"] = stdout
        if stderr is not None:
            values["stderr"] = stderr
        if pid is not None:
            values["pid"] = pid
        resolved_definition_id = definition_id or task_id
        if resolved_definition_id is not None:
            values["definition_id"] = resolved_definition_id
        if session_key is not None:
            values["legacy_session_key"] = session_key
        if session_id is not None:
            values["session_id"] = session_id
        if result_text is not None:
            values["result_text"] = result_text
        if result_payload is not None:
            values["result_payload_json"] = _json_dumps(result_payload)
        if message_ids is not None:
            values["message_ids_json"] = _json_dumps(message_ids)
        if cancel_requested is not None:
            values["cancel_requested"] = 1 if cancel_requested else 0
        if cancel_requested_at is not None:
            values["cancel_requested_at"] = cancel_requested_at
        if metadata is not None:
            existing = self.get_run(run_id) or {}
            merged = dict(existing.get("metadata") or {})
            merged.update(metadata)
            values["metadata_json"] = _json_dumps(merged)
        if callback_status is not None:
            values["callback_status"] = callback_status
        if callback_error is not None:
            values["callback_error"] = callback_error
        if callback_run_id is not None:
            values["callback_run_id"] = callback_run_id
        if callback_completed_at is not None:
            values["callback_completed_at"] = callback_completed_at
        row_to_publish = None
        with self.engine.begin() as conn:
            result = conn.execute(update(agent_runs).where(agent_runs.c.id == run_id).values(**values))
            if result.rowcount:
                row_to_publish = dict(
                    conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().one()
                )
        _publish_run_rows_updated([row_to_publish])

    def definition_lifecycle_state(
        self, definition_id: str, *, definition_type: str = "task"
    ) -> Optional[str]:
        """One definition's canonical lifecycle state, from the shared expression.

        ``definition_lifecycle_expression`` evaluated for a single row — the same
        CASE every list and count surface reads, so a consumer rendering copy
        beside the badge cannot reach a different answer by re-deriving the state
        in Python. The expression resolves an offset-free ``run_at`` in the stored
        IANA timezone through the same rule as ``compute_next_run_at``; the state
        and the displayed next fire therefore cannot disagree during the zone's
        UTC-offset interval. ``None`` when the definition row does not exist.
        """

        resolved = str(definition_id or "").strip()
        if not resolved:
            return None
        statement = (
            select(definition_lifecycle_expression(definition_type))
            .select_from(run_definitions)
            .where(run_definitions.c.id == resolved)
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(statement).first()
        return None if row is None else str(row[0])

    def run_callback_state(self, run_id: str) -> Optional[str]:
        """This run's effective callback delivery state, or ``None`` without one.

        Read FRESH, at decision time, rather than trusted from the drain's listing:
        the owed-notice batch is listed once per pass and each row is then decided
        one at a time, so by the time a row is reached ``_drain_callbacks`` may
        already have moved its callback from ``pending`` to ``sent`` — and the
        notice decision keyed on the stale copy would defer a row whose blocker is
        already resolved, or worse, deliver beside a callback that just landed.
        A parent marked ``sent`` only proves that ``_drain_callbacks`` enqueued the
        callback child. The user has the callback only after that child succeeds and
        records delivery evidence. A persisted result row is evidence only while its
        target Session is admitted by the Inbox; a recorded native send id for a real
        IM conversation is transport evidence in its own right because it is returned
        only after delivery. Archiving or deleting the local Session afterwards cannot
        revoke that completed send. Workbench ids are synthetic and never qualify on
        their own, while a receipt in suppressed background history proves persistence,
        not visibility. This read joins the child and target Session and projects the
        state the notice lane actually needs: queued/running is pending, succeeded with
        effective delivery evidence is sent, and every other terminal outcome releases
        the notice as failed. All three lookups are primary-key probes in one statement.

        ``None`` means "no callback exists for this run" (no target session), which
        is different from a callback whose status column is empty — a target with
        no recorded status has never been armed, and the caller treats both as
        no-shield.
        """

        parent = agent_runs.alias("callback_parent")
        child = agent_runs.alias("callback_child")
        callback_session = agent_sessions.alias("callback_session")
        persisted_receipt = (
            select(literal(1))
            .select_from(messages)
            .where(messages.c.session_id == child.c.session_id)
            .where(messages.c.type == "result")
            .where(func.json_valid(messages.c.metadata_json) == 1)
            .where(
                cast(func.json_extract(messages.c.metadata_json, "$.run_id"), Text)
                == child.c.id
            )
            .correlate(child)
            .exists()
        )
        with self.engine.connect() as conn:
            row = conn.execute(
                select(
                    parent.c.callback_session_id,
                    parent.c.callback_status,
                    parent.c.callback_run_id,
                    child.c.status,
                    persisted_receipt,
                    callback_session.c.status,
                    callback_session.c.visibility,
                    child.c.legacy_session_key,
                    child.c.message_ids_json,
                )
                .select_from(
                    parent.outerjoin(child, child.c.id == parent.c.callback_run_id).outerjoin(
                        callback_session,
                        callback_session.c.id == child.c.session_id,
                    )
                )
                .where(parent.c.id == str(run_id))
                .limit(1)
            ).first()
        if row is None or not str(row[0] or "").strip():
            return None
        callback_status = str(row[1] or "").strip() or None
        callback_run_id = str(row[2] or "").strip()
        if callback_status != "sent" or not callback_run_id:
            return callback_status
        child_status = normalize_run_status(row[3]) if row[3] is not None else ""
        if child_status in {"queued", "running"}:
            return "pending"
        target_key_parts = self._parse_session_key(row[7])
        message_ids = _json_loads(row[8], [])
        descriptor = (
            PLATFORM_REGISTRY.get(target_key_parts[0])
            if target_key_parts is not None
            else None
        )
        has_native_im_receipt = (
            descriptor is not None
            and descriptor.kind == "im"
            and target_key_parts is not None
            and target_key_parts[1] in {"channel", "user"}
            and isinstance(message_ids, list)
            and any(str(message_id or "").strip() for message_id in message_ids)
        )
        target_is_visible = (
            str(row[5] or "").strip() != "archived"
            and str(row[6] or "").strip() in INBOX_SESSION_VISIBILITIES
        )
        has_visible_persisted_receipt = bool(row[4]) and target_is_visible
        if child_status == "succeeded" and (
            has_native_im_receipt or has_visible_persisted_receipt
        ):
            return "sent"
        return "failed"

    def update_callback_status(
        self,
        run_id: str,
        *,
        status: str,
        error: Optional[str] = None,
        callback_run_id: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> None:
        now = completed_at or _utc_now_iso()
        values: dict[str, Any] = {
            "callback_status": status,
            "callback_error": error,
            "callback_completed_at": now,
            "updated_at": now,
        }
        if callback_run_id is not None:
            values["callback_run_id"] = callback_run_id
        with self.engine.begin() as conn:
            conn.execute(update(agent_runs).where(agent_runs.c.id == run_id).values(**values))

    def mark_callback_pending(self, run_id: str, *, updated_at: Optional[str] = None) -> None:
        now = updated_at or _utc_now_iso()
        values: dict[str, Any] = {
            "callback_status": "pending",
            "callback_error": None,
            "callback_completed_at": None,
            "updated_at": now,
        }
        with self.engine.begin() as conn:
            conn.execute(update(agent_runs).where(agent_runs.c.id == run_id).values(**values))

    def mark_run_queued_from_running(
        self,
        run_id: str,
        *,
        updated_at: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        now = updated_at or _utc_now_iso()
        values: dict[str, Any] = {
            "status": "queued",
            "started_at": None,
            "updated_at": now,
        }
        if metadata is not None:
            existing = self.get_run(run_id) or {}
            merged = dict(existing.get("metadata") or {})
            merged.update(metadata)
            values["metadata_json"] = _json_dumps(merged)
        row_to_publish = None
        with self.engine.begin() as conn:
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .where(agent_runs.c.status.in_(_status_query_values("running")))
                .values(**values)
            )
            if result.rowcount:
                row_to_publish = dict(
                    conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().one()
                )
        _publish_run_rows_updated([row_to_publish])
        return row_to_publish is not None

    def claim_queued_run_for_workbench(
        self,
        run_id: str,
        *,
        started_at: Optional[str] = None,
    ) -> bool:
        return self.claim_queued_runs_for_workbench([run_id], started_at=started_at) == [run_id]

    def claim_queued_runs_for_workbench(
        self,
        run_ids: list[str],
        *,
        started_at: Optional[str] = None,
    ) -> list[str]:
        with run_update_event_transaction(self.engine) as conn:
            return claim_queued_runs_for_workbench_in_connection(conn, run_ids, started_at=started_at)

    def inspect_queued_runs_for_workbench(self, run_ids: list[str]) -> tuple[list[str], list[str]]:
        with run_update_event_transaction(self.engine) as conn:
            return inspect_queued_runs_for_workbench_in_connection(conn, run_ids)

    def record_run_message(
        self,
        run_id: str,
        *,
        text: str,
        message_id: str | None = None,
        terminal_status: Optional[str] = None,
        error: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        now = updated_at or _utc_now_iso()
        row_to_publish = None
        with self.engine.begin() as conn:
            row = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().first()
            if not row:
                return
            existing_text = str(row["result_text"] or "")
            incoming = str(text or "")
            if existing_text and incoming:
                result_text = f"{existing_text}\n\n{incoming}"
            else:
                result_text = existing_text or incoming
            message_ids = _json_loads(row["message_ids_json"], [])
            if message_id:
                message_ids.append(message_id)
            values: dict[str, Any] = {
                "result_text": result_text,
                "message_ids_json": _json_dumps(message_ids),
                "updated_at": now,
            }
            if terminal_status:
                values["status"] = normalize_run_status(terminal_status)
                values["completed_at"] = now
                if error is not None:
                    values["error"] = str(error)
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .values(**values)
            )
            if terminal_status and result.rowcount:
                row_to_publish = dict(
                    conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1)).mappings().one()
                )
        _publish_run_rows_updated([row_to_publish])

    def record_run_output(
        self,
        run_id: str,
        *,
        output_id: str,
        text: str,
        message_id: str | None = None,
        sequence: int | None = None,
        provenance: Optional[dict[str, Any]] = None,
        terminal_status: Optional[str] = None,
        error: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Append one idempotent Run output and optionally settle the Run once.

        One of the TWO settling consumers of the ``deferred_terminal_*`` family
        :meth:`defer_run_terminal` parks — :meth:`settle_deferred_run` is the other.
        Every field of that family is consumed here, including the parked metadata,
        because a run whose intent was parked settles on whichever road reaches it
        first and both owe the same answer. The rest of the family's readers are
        read-only gates that key on ``deferred_terminal_status`` alone
        (``list_deferred_runs``, ``settle_without_result``'s deferred-owner decline,
        the recovery/sweep exemptions and the teardown reconciler's) and consume
        nothing.
        """

        now = updated_at or _utc_now_iso()
        identity = str(output_id or "").strip()
        if not identity:
            raise ValueError("output_id is required")
        recorded = False
        terminal_transition = False
        text_backfilled = False
        run_payload: Optional[dict[str, Any]] = None
        row_to_publish = None
        with self.engine.begin() as conn:
            row = conn.execute(
                select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
            ).mappings().first()
            if not row:
                return {
                    "recorded": False,
                    "terminal_transition": False,
                    "run": None,
                }

            result_payload = _json_loads(row["result_payload_json"], {})
            if not isinstance(result_payload, dict):
                result_payload = {}
            payload_changed = False
            effective_terminal_status = terminal_status
            effective_terminal_error = error
            deferred_result_text = None
            deferred_metadata: Optional[dict[str, Any]] = None
            deferred_status = ""
            if terminal_status and "deferred_terminal_status" in result_payload:
                deferred_status = normalize_run_status(
                    result_payload.get("deferred_terminal_status")
                )
                effective_terminal_status = _stronger_terminal_status(
                    result_payload.pop("deferred_terminal_status", None),
                    terminal_status,
                )
                deferred_error = result_payload.pop("deferred_terminal_error", None)
                deferred_result_text = result_payload.pop("deferred_terminal_result_text", None)
                # The FOURTH field of the deferred family, and this is its SECOND
                # settling consumer — ``settle_deferred_run`` is the other, and until
                # HFR-329 it was the only one that knew this key existed. A parked
                # intent reaches THIS road whenever the backend's terminal output
                # arrives before the Activity lifecycle applies the intent, so leaving
                # the key unread settled the run with no ``interrupt_reason`` at all.
                #
                # ALWAYS POPPED, whether or not it is merged below: this call consumes
                # the deferred intent, and a key left behind in ``result_payload_json``
                # is a parked cause with no settlement left to reach — replayable by
                # the next reader of the family and stale forever.
                parked_metadata = result_payload.pop("deferred_terminal_metadata", None)
                if isinstance(parked_metadata, dict) and parked_metadata:
                    deferred_metadata = parked_metadata
                if deferred_error is not None:
                    effective_terminal_error = str(deferred_error)
                payload_changed = True
            raw_outputs = result_payload.get("outputs")
            outputs = [dict(item) for item in raw_outputs if isinstance(item, dict)] if isinstance(raw_outputs, list) else []
            visible_text = str(text or "")
            if visible_text.strip() and not any(str(item.get("id") or "") == identity for item in outputs):
                output_entry: dict[str, Any] = {
                    "id": identity,
                    "text": visible_text,
                }
                if message_id:
                    output_entry["message_id"] = message_id
                if sequence is not None:
                    output_entry["sequence"] = sequence
                if provenance:
                    output_entry["provenance"] = dict(provenance)
                outputs.append(output_entry)
                result_payload["outputs"] = outputs
                recorded = True
                payload_changed = True

            message_ids = _json_loads(row["message_ids_json"], [])
            if not isinstance(message_ids, list):
                message_ids = []
            if recorded and message_id and message_id not in message_ids:
                message_ids.append(message_id)

            if payload_changed:
                values: dict[str, Any] = {
                    "result_payload_json": _json_dumps(result_payload),
                    "updated_at": now,
                }
                if recorded:
                    values["message_ids_json"] = _json_dumps(message_ids)
                conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.id == run_id)
                    .values(**values)
                )
            if effective_terminal_status:
                terminal_result_text = (
                    str(deferred_result_text)
                    if deferred_result_text is not None
                    else visible_text
                )
                base_terminal_values: dict[str, Any] = {
                    # Structured outputs remain available in result_payload, but
                    # result_text is the one terminal result used by callbacks.
                    "result_text": terminal_result_text,
                }
                if effective_terminal_error is not None:
                    base_terminal_values["error"] = str(effective_terminal_error)

                # One bounded re-read mirrors ``settle_run_terminal``. A Stop on a
                # running row changes only ``cancel_requested``; the first CAS loses,
                # then the second decision sees the flag and settles canceled.
                terminal_row = row
                for final_attempt in (False, True):
                    if normalize_run_status(terminal_row["status"]) in TERMINAL_RUN_STATUSES:
                        break
                    status, guards = _cancel_aware_terminal_status(
                        terminal_row, effective_terminal_status
                    )
                    # THE SETTLEMENT EQUALITY RULE, decided HERE — inside the bounded
                    # re-read, against the ``status`` this attempt will write, never the
                    # pre-loop value. See ``_deferred_metadata_for_settlement``, which
                    # owns the rule for this caller and for ``settle_deferred_run``.
                    parked_cause = _deferred_metadata_for_settlement(
                        deferred_metadata,
                        run_id=run_id,
                        settling_status=status,
                        deferred_status=deferred_status,
                    )
                    terminal_values = {
                        **base_terminal_values,
                        "status": status,
                        "completed_at": now,
                        "updated_at": now,
                    }
                    _merge_owed_failure_notice(
                        terminal_values,
                        conn=conn,
                        run_id=run_id,
                        status=status,
                        source_kind=terminal_row["source_kind"],
                        parent_run_id=terminal_row["parent_run_id"],
                        row_metadata_json=terminal_row["metadata_json"],
                        extra_metadata=parked_cause,
                        now=now,
                    )
                    transition = conn.execute(
                        update(agent_runs)
                        .where(agent_runs.c.id == run_id)
                        .where(*guards)
                        .values(**terminal_values)
                    )
                    if transition.rowcount:
                        terminal_transition = True
                        break
                    if final_attempt:
                        break
                    terminal_row = (
                        conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1))
                        .mappings()
                        .one()
                    )
                row = terminal_row
                if not terminal_transition:
                    # The row was already terminal, so the guarded UPDATE above
                    # matched nothing and ``result_text`` -- the one terminal
                    # result callbacks read -- would stay empty forever. Harness
                    # rows reach this state routinely: the scheduler settles a
                    # scheduled/watch run at dispatch, long before the agent's
                    # result is delivered. Backfill the text (and the error where
                    # none was recorded) with its own emptiness guard so a real
                    # terminal transition's values are never overwritten, and
                    # leave ``status`` / ``completed_at`` alone so settlement
                    # timing is unchanged.
                    #
                    # A parked cause is dropped here too, and must be: the row was
                    # settled by another writer, whose transition already stamped (or
                    # declined to stamp) the notice, and a notice is never re-stamped.
                    # Writing ``interrupt_reason`` now could not reach
                    # ``_owed_failure_notice_for_transition`` in time to pick a lane; it
                    # would only relabel a settled row's health classification after
                    # the fact. Too late is the same as never (HFR-110).
                    stored_status = normalize_run_status(row["status"])
                    incoming_status = normalize_run_status(effective_terminal_status)
                    # Backfill only when the stored outcome and the delivered
                    # outcome AGREE. This repairs the case PR1 exists for -- a row
                    # the scheduler settled at dispatch, whose text never landed
                    # because the terminal UPDATE is scoped to queued|running --
                    # and refuses every disagreement.
                    #
                    # An earlier revision enumerated which pairs were unsafe
                    # (refuse cancellations; allow error only onto ``failed``).
                    # Review found two contradictions that enumeration missed, in
                    # both directions: a late failure landing on a ``succeeded``
                    # row, then a late success body landing on a swept ``failed``
                    # row. The second is the more instructive one -- the callback
                    # would have reported "the report, actually fine" for a run
                    # recorded as failed, because ``_build_callback_message``
                    # prefers ``result_text`` over the failure fallback.
                    #
                    # Equality is used instead of a longer list because it is
                    # total: it cannot miss a pair. A genuine outcome
                    # disagreement means the stored status is wrong, and settling
                    # the real terminal result is PR7's job -- PR1 must not paper
                    # over it by writing text that contradicts the status.
                    if stored_status == incoming_status:
                        if terminal_result_text.strip():
                            filled = conn.execute(
                                update(agent_runs)
                                .where(agent_runs.c.id == run_id)
                                .where(func.coalesce(agent_runs.c.result_text, "") == "")
                                .values(result_text=terminal_result_text, updated_at=now)
                            )
                            text_backfilled = bool(filled.rowcount)
                        if effective_terminal_error is not None:
                            filled_error = conn.execute(
                                update(agent_runs)
                                .where(agent_runs.c.id == run_id)
                                .where(func.coalesce(agent_runs.c.error, "") == "")
                                .values(error=str(effective_terminal_error), updated_at=now)
                            )
                            text_backfilled = text_backfilled or bool(filled_error.rowcount)

            if payload_changed or terminal_transition or text_backfilled:
                row_to_publish = dict(
                    conn.execute(
                        select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
                    ).mappings().one()
                )
                run_payload = self._run_from_row(row_to_publish)
            else:
                run_payload = self._run_from_row(row)
        _publish_run_rows_updated([row_to_publish])
        return {
            "recorded": recorded,
            "terminal_transition": terminal_transition,
            # True when an already-terminal row was given its missing terminal
            # text/error. Distinct from ``terminal_transition`` on purpose: no
            # status changed, so callers must not read it as a settlement.
            "text_backfilled": text_backfilled,
            "run": run_payload,
        }

    def settle_run_terminal(
        self,
        run_id: str,
        *,
        terminal_status: str,
        error: Optional[str] = None,
        result_text: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        updated_at: Optional[str] = None,
        task_id: Optional[str] = None,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        """Terminalize a non-terminal run in one guarded write.

        This is the settlement writer for a turn that ended WITHOUT the backend
        emitting a terminal result (see ``docs/plans/agent-run-zombie-settlement.md``).
        Unlike ``update_run_status`` — whose UPDATE has no status predicate and would
        clobber a row another actor already settled — the UPDATE here is scoped to
        ``queued|running``, so a concurrent ``vibe runs cancel`` that lands first wins
        and this call becomes a no-op.

        ``cancel_requested`` is read inside the same transaction: a run the user asked
        to cancel settles ``canceled``, never ``failed``, without needing a second
        write. Rows carrying a deferred terminal intent are left alone — the Activity
        lifecycle owns those.

        ``task_id`` / ``session_key`` / ``session_id`` are the identity columns the
        claimed-request completion resolves late (a ``scheduled`` row learns its real
        target only once the task definition has been read). They are facts about
        WHICH conversation the run belongs to, not claims about its outcome, so they
        are written whether or not the status transition lands — otherwise routing
        the completion through this guarded writer would lose them on exactly the
        rows another actor settled first.

        Returns the terminal status actually written, or ``None`` when nothing was
        written (already terminal, deferred, or missing).
        """

        now = updated_at or _utc_now_iso()
        row_to_publish = None
        written_status: Optional[str] = None
        with self.engine.begin() as conn:
            row = conn.execute(
                select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
            ).mappings().first()
            if not row:
                return None
            identity: dict[str, Any] = {}
            if task_id is not None:
                identity["definition_id"] = task_id
            if session_key is not None:
                identity["legacy_session_key"] = session_key
            if session_id is not None:
                identity["session_id"] = session_id
            if identity:
                conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.id == run_id)
                    .values(**identity, updated_at=now)
                )
            # ONE re-read on a refused write, never a loop, for the reason
            # ``cancel_not_requested`` gives: refusing the write is only half the fix,
            # because a run left ``running`` with nothing to settle it is the zombie
            # this writer exists to prevent. The second pass re-decides from the row as
            # it now stands, which is where the cancel is visible, so it writes
            # ``canceled`` rather than being refused again.
            for final_attempt in (False, True):
                if row is None:
                    break
                if normalize_run_status(row["status"]) in TERMINAL_RUN_STATUSES:
                    return None
                result_payload = _json_loads(row["result_payload_json"], {})
                if isinstance(result_payload, dict) and result_payload.get("deferred_terminal_status"):
                    # The Activity lifecycle already owns this row's terminal state.
                    return None
                status, guards = _cancel_aware_terminal_status(row, terminal_status)
                values: dict[str, Any] = {
                    "status": status,
                    "completed_at": now,
                    "updated_at": now,
                }
                if error is not None:
                    values["error"] = str(error)
                if result_text is not None:
                    values["result_text"] = str(result_text)
                _merge_owed_failure_notice(
                    values,
                    conn=conn,
                    run_id=run_id,
                    status=status,
                    source_kind=row["source_kind"],
                    parent_run_id=row["parent_run_id"],
                    row_metadata_json=row["metadata_json"],
                    extra_metadata=metadata or None,
                    now=now,
                )
                # ``_cancel_aware_terminal_status`` re-asserts the cancellation fact
                # this branch read. A running cancel changes only the flag, so a status
                # predicate alone cannot stop a stale failure from overwriting Stop.
                transition = conn.execute(
                    update(agent_runs).where(agent_runs.c.id == run_id).where(*guards).values(**values)
                )
                if transition.rowcount:
                    written_status = status
                    row_to_publish = dict(
                        conn.execute(
                            select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
                        ).mappings().one()
                    )
                    break
                if final_attempt:
                    break
                row = (
                    conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1))
                    .mappings()
                    .first()
                )
        _publish_run_rows_updated([row_to_publish])
        return written_status

    def defer_run_terminal(
        self,
        run_id: str,
        *,
        terminal_status: str,
        error: Optional[str] = None,
        result_text: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        updated_at: Optional[str] = None,
    ) -> bool:
        """Remember a terminal intent and result while an Activity blocks it.

        ``metadata`` is the run-metadata the eventual terminal write owes — today
        ``interrupt_reason`` for a run terminalized out of band. It is REMEMBERED
        HERE rather than written now, and that is the whole point: a defer and its
        settlement are two statements, so a cause stamped by a third write could be
        lost to a crash between them, and one stamped after the settlement would
        arrive too late for ``_owed_failure_notice_for_transition`` to read (the
        notice is never overwritten once stamped). Parked in
        ``result_payload_json`` beside the other deferred fields, it survives the
        gap and :meth:`settle_deferred_run` folds it into the SAME guarded UPDATE
        that transitions the status.
        """

        now = updated_at or _utc_now_iso()
        row_to_publish = None
        with self.engine.begin() as conn:
            row = conn.execute(
                select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
            ).mappings().first()
            if not row or normalize_run_status(row["status"]) in TERMINAL_RUN_STATUSES:
                return False
            result_payload = _json_loads(row["result_payload_json"], {})
            if not isinstance(result_payload, dict):
                result_payload = {}
            normalized = _stronger_terminal_status(
                result_payload.get("deferred_terminal_status"),
                terminal_status,
            )
            status_changed = result_payload.get("deferred_terminal_status") != normalized
            error_text = str(error) if error is not None else None
            error_changed = bool(
                error_text is not None
                and not result_payload.get("deferred_terminal_error")
            )
            deferred_result_text = str(result_text) if result_text is not None else None
            result_text_changed = bool(
                deferred_result_text is not None
                and result_payload.get("deferred_terminal_result_text") != deferred_result_text
            )
            deferred_metadata = dict(metadata) if metadata else None
            metadata_changed = bool(
                deferred_metadata is not None
                and result_payload.get("deferred_terminal_metadata") != deferred_metadata
            )
            if (
                not status_changed
                and not error_changed
                and not result_text_changed
                and not metadata_changed
            ):
                return False
            result_payload["deferred_terminal_status"] = normalized
            if error_changed:
                result_payload["deferred_terminal_error"] = error_text
            if result_text_changed:
                result_payload["deferred_terminal_result_text"] = deferred_result_text
            if metadata_changed:
                result_payload["deferred_terminal_metadata"] = deferred_metadata
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .values(
                    result_payload_json=_json_dumps(result_payload),
                    updated_at=now,
                )
            )
            row_to_publish = dict(
                conn.execute(
                    select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
                ).mappings().one()
            )
        _publish_run_rows_updated([row_to_publish])
        return True

    def settle_deferred_run(
        self,
        run_id: str,
        *,
        terminal_status: Optional[str] = None,
        error: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> bool:
        """Apply one stored terminal intent after owned Activities become terminal.

        Any metadata :meth:`defer_run_terminal` parked with the intent is popped
        here and folded into the same guarded UPDATE, BEFORE the owed-notice
        stamper reads the merged blob — so an ``interrupt_reason`` recorded at
        defer time both survives a crash in the gap and reaches
        ``_owed_failure_notice_for_transition`` in time to pick the interruption
        lane rather than the ordinary-failure one.

        THE MERGE IS CONDITIONAL, on the equality rule this method shares with
        ``record_run_output`` (``_deferred_metadata_for_settlement``, HFR-331). Popping
        is not: the intent is consumed by this call either way, or the key is left in
        ``result_payload_json`` as a parked cause with no settlement left to reach.
        """

        now = updated_at or _utc_now_iso()
        row_to_publish = None
        transitioned = False
        with self.engine.begin() as conn:
            row = conn.execute(
                select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
            ).mappings().first()
            if not row:
                return False
            # A deferred failure is still subordinate to Stop. Re-decide once when
            # the cancellation CAS loses so a running row becomes canceled instead
            # of remaining a zombie or receiving a false failure notice.
            for final_attempt in (False, True):
                if normalize_run_status(row["status"]) in TERMINAL_RUN_STATUSES:
                    break
                result_payload = _json_loads(row["result_payload_json"], {})
                if not isinstance(result_payload, dict):
                    break
                deferred_status = str(
                    result_payload.get("deferred_terminal_status") or ""
                ).strip()
                if not deferred_status:
                    break
                result_payload.pop("deferred_terminal_status", None)
                deferred_error = result_payload.pop("deferred_terminal_error", None)
                deferred_result_text = result_payload.pop(
                    "deferred_terminal_result_text", None
                )
                deferred_metadata = result_payload.pop("deferred_terminal_metadata", None)
                if not isinstance(deferred_metadata, dict) or not deferred_metadata:
                    deferred_metadata = None
                requested_status = (
                    _stronger_terminal_status(deferred_status, terminal_status)
                    if terminal_status
                    else normalize_run_status(deferred_status)
                )
                status, guards = _cancel_aware_terminal_status(row, requested_status)
                # THE SETTLEMENT EQUALITY RULE — the same one ``record_run_output``
                # applies, and this call is why it lives in a shared helper (HFR-331).
                # ``_cancel_aware_terminal_status`` above can flip a deferred ``failed``
                # to ``canceled`` when the user's Stop raced the settlement, and the
                # metadata used to ride along unconditionally: a row recorded as
                # ``canceled`` — user intent — carrying ``interrupt_reason=evicted``,
                # infrastructure metadata describing a cancellation nobody's
                # infrastructure caused. It is decided against ``status``, the value this
                # attempt actually writes, and re-decided on the re-read pass because the
                # payload is re-popped there too.
                parked_cause = _deferred_metadata_for_settlement(
                    deferred_metadata,
                    run_id=run_id,
                    settling_status=status,
                    deferred_status=deferred_status,
                )
                values: dict[str, Any] = {
                    "status": status,
                    "completed_at": now,
                    "updated_at": now,
                    "result_payload_json": _json_dumps(result_payload),
                }
                effective_error = deferred_error if deferred_error is not None else error
                if effective_error is not None:
                    values["error"] = str(effective_error)
                if deferred_result_text is not None:
                    values["result_text"] = str(deferred_result_text)
                _merge_owed_failure_notice(
                    values,
                    conn=conn,
                    run_id=run_id,
                    status=status,
                    source_kind=row["source_kind"],
                    parent_run_id=row["parent_run_id"],
                    row_metadata_json=row["metadata_json"],
                    extra_metadata=parked_cause,
                    now=now,
                )
                transition = conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.id == run_id)
                    .where(*guards)
                    .values(**values)
                )
                if transition.rowcount:
                    transitioned = True
                    row_to_publish = dict(
                        conn.execute(
                            select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
                        ).mappings().one()
                    )
                    break
                if final_attempt:
                    break
                row = (
                    conn.execute(select(agent_runs).where(agent_runs.c.id == run_id).limit(1))
                    .mappings()
                    .first()
                )
                if row is None:
                    break
        _publish_run_rows_updated([row_to_publish])
        return transitioned

    def find_callback_run(
        self,
        *,
        parent_run_id: str,
        source_actor: str,
    ) -> Optional[dict[str, Any]]:
        """Return the callback Run for one parent callback identity."""

        with self.engine.connect() as conn:
            row = conn.execute(
                select(agent_runs)
                .where(agent_runs.c.run_type == "agent_run")
                .where(agent_runs.c.source_kind == "callback")
                .where(agent_runs.c.parent_run_id == parent_run_id)
                .where(agent_runs.c.source_actor == source_actor)
                .order_by(agent_runs.c.created_at, agent_runs.c.id)
                .limit(1)
            ).mappings().first()
            return self._run_from_row(row) if row else None

    def recover_processing_runs(self) -> None:
        with run_update_event_transaction(self.engine) as conn:
            now = _utc_now_iso()
            rows = list(
                conn.execute(
                    select(agent_runs.c.id, agent_runs.c.result_payload_json)
                    .where(agent_runs.c.status.in_(_status_query_values("running")))
                    .where(agent_runs.c.run_type != "watch_runtime")
                ).mappings()
            )
            recovered_ids = []
            for row in rows:
                result_payload = _json_loads(row["result_payload_json"], {})
                if isinstance(result_payload, dict) and result_payload.get(
                    "deferred_terminal_status"
                ):
                    continue
                recovered_ids.append(str(row["id"]))
            if recovered_ids:
                conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.id.in_(recovered_ids))
                    .values(status="queued", started_at=None, pid=None, updated_at=now)
                )
            _refresh_recovered_coalesced_workbench_runs_in_connection(conn, now=now)
            _defer_run_ids_updated_from_connection(conn, recovered_ids)

    def stamp_run_session_id(
        self,
        run_id: str,
        *,
        session_id: str,
        agent_backend: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> bool:
        """Associate a still-open run with the session it is actually executing in.

        WHY THIS EXISTS. A ``create_per_run`` run mints its session at dispatch time,
        and until now that id lived only in a Python local until the run completed:
        ``settle_run_terminal``'s identity block was the first and only writer of
        ``agent_runs.session_id`` for those rows. So while the run was in flight the
        column was NULL, and a session teardown had no way to ask the database "which
        runs belong to the session I am reclaiming" — exactly the row a teardown
        reconciler most needs to find, because the same policy has no session lock key
        either and so is invisible to every in-memory join as well.

        ``agent_backend`` IS THE SECOND HALF OF THE SAME FACT (HFR-337). The caller
        that knows which session a run is executing in is the caller that just walked
        the routing ladder to decide which Agent it goes to, so both facts are known
        at the same instant and are written by one UPDATE. HFR-328 could only stamp
        the backend at ENQUEUE, from the request's ``agent_name`` — which resolves
        nothing for a run that pins no Agent and follows the session/scope/global
        default. Those runs held a lane nothing could identify, and End's fail-closed
        ownership gate consequently skipped the cancel and tore the runtime down
        around a live execution.

        FILLED, NEVER OVERWRITTEN, matching the enqueue stamp's rule: a run that
        already names a backend was pinned by a caller who resolved its own dispatch
        target, and a reservation must not be able to re-label somebody else's
        identity. ``COALESCE(NULLIF(...))`` does that inside the single statement, so
        no read-then-write window exists.

        NARROW ON PURPOSE. It writes ``session_id``, optionally fills
        ``agent_backend``, bumps ``updated_at`` and touches nothing else: these are
        associations, not state changes, and a reservation must never be able to move
        a run's status, error or result. The ``WHERE`` is guarded to
        ``NON_TERMINAL_RUN_STATUSES`` for the same reason every other writer in this
        file is guarded — a late reservation racing a terminal write must lose, not
        re-stamp identity onto settled history.

        Returns whether a row was actually updated; ``False`` when the run is missing
        or already terminal.
        """

        resolved_run_id = str(run_id or "").strip()
        resolved_session_id = str(session_id or "").strip()
        if not resolved_run_id or not resolved_session_id:
            return False
        resolved_backend = str(agent_backend or "").strip()
        now = updated_at or _utc_now_iso()
        values: dict[str, Any] = {"session_id": resolved_session_id, "updated_at": now}
        if resolved_backend:
            values["agent_backend"] = func.coalesce(
                func.nullif(agent_runs.c.agent_backend, ""), literal(resolved_backend)
            )
        row_to_publish = None
        with self.engine.begin() as conn:
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == resolved_run_id)
                .where(agent_runs.c.status.in_(NON_TERMINAL_RUN_STATUSES))
                .values(**values)
            )
            if not result.rowcount:
                return False
            row_to_publish = dict(
                conn.execute(
                    select(agent_runs).where(agent_runs.c.id == resolved_run_id).limit(1)
                ).mappings().one()
            )
        _publish_run_rows_updated([row_to_publish])
        return True

    def record_run_skip_reason(self, run_id: str, *, reason: str, at: Optional[str] = None) -> bool:
        """Record WHY the drain deferred a queued run — only when it changes.

        The sweep must not guess why a row sat in ``queued``, so the drain records it.
        The subtlety is that this write cannot be tick-triggered.
        ``ScheduledTaskService._watch_store`` decides whether to drain from
        ``maybe_reload()`` → ``SqliteInvalidationProbe.has_external_write()``, which
        bumps on *any* write to the DB file — including ours. Stamping on every drain
        pass would therefore self-sustain a write → reload → re-drain → write loop for
        as long as a transport stayed down.

        So the write is transition-triggered: if the stored reason already matches,
        nothing is written and the probe stays quiet. A permanently unavailable
        transport costs exactly one write, ever. ``last_skip_at`` consequently means
        "when this reason started", which is more useful than a refreshed timestamp.

        Deliberately NOT wrapped in ``run_update_event_transaction``: this is
        diagnostic metadata, not a state change worth an SSE frame. ``updated_at`` is
        left alone too — bumping it would make a stranded row look freshly touched and
        would defeat the hold TTL, which reads exactly that column.

        Returns ``True`` when a write actually happened.
        """

        instant = at or _utc_now_iso()
        raw_metadata = func.coalesce(agent_runs.c.metadata_json, "{}")
        metadata = case(
            (func.json_valid(raw_metadata) == 1, raw_metadata),
            else_=literal("{}"),
        )
        with self.engine.begin() as conn:
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .where(agent_runs.c.status.in_(_status_query_values("queued")))
                .where(
                    cast(
                        func.coalesce(
                            func.json_extract(metadata, "$.last_skip_reason"), ""
                        ),
                        Text,
                    )
                    != reason
                )
                .values(
                    metadata_json=func.json_set(
                        metadata,
                        "$.last_skip_reason",
                        reason,
                        "$.last_skip_at",
                        instant,
                    )
                )
            )
        return bool(result.rowcount)

    def _clear_transport_skip_evidence(self, run_ids: set[str]) -> int:
        """Forget a ``transport_unavailable`` stamp whose outage has demonstrably ended.

        Counterpart to :meth:`record_run_skip_reason`, with the same two properties
        that keep it from feeding the drain loop: it is TRANSITION-triggered (after the
        clear there is no reason left to match, so a still-deliverable row costs zero
        writes on every later sweep), and it leaves ``updated_at`` alone so a queue hold
        keeps aging from its own clock.

        Scoped to the transport reason on purpose: another writer's reason is not ours
        to erase, and a row re-stamped between the select and here is left as it is —
        it will be reconsidered, with a fresh ``last_skip_at``, next sweep.
        """

        normalized_ids = sorted({str(run_id) for run_id in run_ids if str(run_id).strip()})
        if not normalized_ids:
            return 0
        raw_metadata = func.coalesce(agent_runs.c.metadata_json, "{}")
        metadata = case(
            (func.json_valid(raw_metadata) == 1, raw_metadata),
            else_=literal("{}"),
        )
        with self.engine.begin() as conn:
            result = conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id.in_(normalized_ids))
                .where(agent_runs.c.status.in_(_status_query_values("queued")))
                .where(
                    cast(func.json_extract(metadata, "$.last_skip_reason"), Text)
                    == SKIP_REASON_TRANSPORT_UNAVAILABLE
                )
                .values(
                    metadata_json=func.json_remove(
                        metadata,
                        "$.last_skip_reason",
                        "$.last_skip_at",
                    )
                )
            )
        cleared = int(result.rowcount or 0)
        if cleared:
            logger.debug("Cleared recovered transport skip evidence on %s harness run(s)", cleared)
        return cleared

    def sweep_stale_runs(
        self,
        *,
        owned_run_ids: set[str],
        error_texts: dict[str, str],
        deliverable_run_ids: Optional[set[str]] = None,
        busy_session_ids: Optional[set[str]] = None,
        now: Optional[str] = None,
        orphan_grace_seconds: int = 0,
        queued_ttl_seconds: int = 0,
        hold_ttl_seconds: int = 0,
    ) -> list[SweptRun]:
        """Terminalize runs that provably have nothing left to settle them.

        Three evidence-based classes (``docs/plans/agent-run-zombie-settlement.md``
        §4.2). Each is disabled by passing ``0`` for its window:

        - ``orphaned``: a ``running`` agent run with no live owner, older than
          ``orphan_grace_seconds``. The grace period is what keeps a run that is
          legitimately starting up from being swept.
        - ``transport_unavailable``: a ``queued`` run whose recorded skip reason says
          its transport is down, whose ``last_skip_at`` (when that reason started, not
          when the run was enqueued) is older than ``queued_ttl_seconds``, AND which the
          caller still cannot deliver. The reason is read off the row, never
          re-derived, so a run deferred for capacity or a session lock — both of which
          are progress — is never swept.
        - ``queue_hold_expired``: a ``queued`` run holding a workbench queue slot that
          has not been touched in ``hold_ttl_seconds``, in a session with no live turn.

        ``owned_run_ids`` exempts a row from **every** class, not just ``orphaned``. A
        coalesced workbench turn claims its secondary runs and deliberately leaves them
        ``queued`` while the primary settles them, so a live owner has to outrank the
        queue TTLs too — otherwise a turn that outlives ``hold_ttl_seconds`` would have
        its own siblings failed underneath it, reintroducing the turn-duration timeout
        this design does not have. It must be the union of every ownership source; the
        caller is responsible for failing closed (passing "everything is owned", or not calling
        at all) when it cannot enumerate owners. This method cannot tell an empty set
        meaning "nothing is running" from one meaning "I could not look".

        ``deliverable_run_ids`` is the same contract for the transport class, and it is
        why the recorded reason alone is not enough. The drain stops at its concurrency
        cap, so rows below the cut are never re-examined and keep an old
        ``transport_unavailable`` stamp long after their platform reconnected. Without a
        live second opinion the sweep would fail a run that is merely waiting for a free
        slot. Every id listed here is exempt. A listed row also has its stale
        ``transport_unavailable`` evidence CLEARED, so a later outage is aged from its
        own start: capacity keeps the drain from re-stamping a row below its cut, so
        without the clear a recovered-then-failed-again transport would be read as one
        continuous outage and skip the whole configured reconnect window (Codex P2).

        ``busy_session_ids`` is the same contract again, for the hold class: a run the
        gate parked behind a live turn is NOT reported by ``owned_agent_run_ids`` (the
        live turn only owns the ids of the run it is itself executing), so a legitimate
        Workbench turn outliving ``hold_ttl_seconds`` would have its own queued follower
        failed even though the gate would flush it on completion (Codex P2). The set is
        session ids, not run ids, because that is the granularity the gate occupies.
        ``None`` means "no exemptions", so a caller that cannot enumerate live turns
        must fail closed by disabling the class (``hold_ttl_seconds=0``), exactly as it
        does for deliverability.

        Candidate selection is read-only; each row is then terminalized through
        :meth:`settle_run_terminal`, so every write inherits the same guards — scoped
        to ``queued|running``, ``cancel_requested`` honored, deferred/Activity-owned
        rows left alone, ``metadata_json`` merged rather than replaced. A row someone
        else settles between the select and the write is simply skipped.
        """

        now_iso = now or _utc_now_iso()
        now_dt = _parse_iso_instant(now_iso) or datetime.now(timezone.utc)

        def _older_than(value: Any, seconds: int) -> bool:
            if seconds <= 0:
                return False
            instant = _parse_iso_instant(value)
            if instant is None:
                # Undateable row: never sweep it.
                return False
            return instant <= now_dt - timedelta(seconds=seconds)

        with self.engine.begin() as conn:
            rows = list(
                conn.execute(
                    select(agent_runs)
                    .where(
                        agent_runs.c.status.in_(
                            _status_query_values("queued") + _status_query_values("running")
                        )
                    )
                    .where(agent_runs.c.run_type != "watch_runtime")
                ).mappings()
            )

        candidates: list[tuple[dict[str, Any], str]] = []
        recovered_ids: set[str] = set()
        for row in rows:
            result_payload = _json_loads(row["result_payload_json"], {})
            if isinstance(result_payload, dict) and result_payload.get("deferred_terminal_status"):
                # The Activity lifecycle owns this row's terminal state.
                continue
            metadata = _json_loads(row["metadata_json"], {})
            metadata = metadata if isinstance(metadata, dict) else {}
            status = normalize_run_status(row["status"])
            run_id = str(row["id"])
            if run_id in owned_run_ids:
                # A live owner outranks every TTL, whatever the row's status. This is
                # NOT only about ``running`` rows: a coalesced workbench turn claims its
                # secondary runs and deliberately leaves them ``queued`` with
                # ``workbench_queue_holds_run`` while the primary settles them, and
                # ``owned_agent_run_ids`` reports all of them as owned. Aging those out
                # would fail live siblings mid-turn — a turn-duration timeout by the back
                # door, which this design explicitly does not have.
                continue
            reason: Optional[str] = None
            if status == "running":
                # Restricted to ``agent_run`` on purpose: when ``scheduled``/``watch``
                # rows settle is owned by a separate plan. Widen only alongside it.
                if str(row["run_type"] or "") == "agent_run" and _older_than(
                    row["started_at"] or row["created_at"], orphan_grace_seconds
                ):
                    reason = SWEEP_REASON_ORPHANED
            elif status == "queued":
                if (
                    metadata.get("last_skip_reason") == SKIP_REASON_TRANSPORT_UNAVAILABLE
                    and run_id in (deliverable_run_ids or set())
                ):
                    # The outage this row remembers is OVER. Retire the evidence now,
                    # while we can still see both halves of it: the drain breaks at its
                    # concurrency cap, so a row below the cut is never re-stamped, and a
                    # stale ``last_skip_at`` would make the NEXT outage look like a
                    # continuation of this one and skip its whole TTL (Codex P2).
                    recovered_ids.add(run_id)
                # Hold before transport: it is the more specific piece of evidence, and
                # its TTL is deliberately the longest so an actively recovering queue
                # survives.
                if (
                    metadata.get("workbench_queue_holds_run")
                    # A live turn in this row's session is why it is parked. The gate
                    # will flush it when that turn ends, and the turn does NOT report
                    # this run as owned — it owns only the ids it is executing itself.
                    and str(row["session_id"] or "") not in (busy_session_ids or set())
                    and _older_than(row["updated_at"], hold_ttl_seconds)
                ):
                    reason = SWEEP_REASON_QUEUE_HOLD_EXPIRED
                elif (
                    metadata.get("last_skip_reason") == SKIP_REASON_TRANSPORT_UNAVAILABLE
                    # Two independent facts, both required: the drain recorded that this
                    # row's platform was down, AND the caller still cannot deliver it.
                    # The stamp alone goes stale below the drain's concurrency cap.
                    and run_id not in (deliverable_run_ids or set())
                    # Age from when the transport problem STARTED, not from when the
                    # run was enqueued. ``record_run_skip_reason`` is
                    # transition-triggered, so ``last_skip_at`` is exactly that instant.
                    # Using ``created_at`` would make a run that had already been
                    # queued past the TTL for a healthy reason (capacity, a busy
                    # session) sweepable the moment its transport blinked, skipping the
                    # whole configured reconnect window. Absent or unparseable => never
                    # swept: the reason and its timestamp are written together, so a
                    # reason without one is evidence this writer did not produce.
                    and _older_than(metadata.get("last_skip_at"), queued_ttl_seconds)
                ):
                    reason = SWEEP_REASON_TRANSPORT_UNAVAILABLE
            if reason is not None:
                candidates.append((dict(row), reason))

        if recovered_ids:
            self._clear_transport_skip_evidence(recovered_ids)

        swept: list[SweptRun] = []
        for row, reason in candidates:
            run_id = str(row["id"])
            written = self.settle_run_terminal(
                run_id,
                terminal_status="failed",
                error=error_texts.get(reason),
                metadata={"interrupt_reason": reason},
                updated_at=now_iso,
            )
            if written is None:
                # Settled by someone else in the meantime — nothing to report.
                continue
            logger.warning("Harness run %s swept as %s (%s)", run_id, written, reason)
            swept.append(
                SweptRun(
                    run_id=run_id,
                    status=written,
                    interrupt_reason=reason,
                    run_type=str(row["run_type"] or "") or None,
                    task_id=str(row["definition_id"] or "") or None,
                    session_id=str(row["session_id"] or "") or None,
                    session_key=str(row["legacy_session_key"] or "") or None,
                )
            )
        return swept

    # --- owed failure notices -------------------------------------------------

    def list_owed_failure_notices(self, *, limit: int = 20, now: Optional[str] = None) -> list[dict[str, Any]]:
        """Runs owing a user-visible failure notice whose backoff has elapsed.

        Ordered by ``(created_at, id)`` so the drain sees a streak's earliest row
        first and the canonical choice is deterministic.

        **Eligibility is decided in SQL, before the limit.** An earlier revision
        filtered the notice state in Python and argued there was no filter-after-LIMIT
        hazard because the limit applied after ordering. That was right about
        correctness and silent about cost, which is the part that mattered: with no
        state predicate and no SQL ``LIMIT``, the steady state — every historical
        failure already ``sent``/``skipped``/``failed`` — made this tick scan and
        JSON-decode the ENTIRE failed-run history every two seconds to return an empty
        list, at a cost growing without bound over the database's lifetime.

        ``json_valid`` guards the extraction inside a ``CASE``, for the same reason as
        the health window and with a sharper consequence: ``json_extract`` raises
        ``malformed JSON`` and fails the whole STATEMENT, so one unparseable blob would
        stop the drain finding ANY owed notice — every failure notification in the
        system silenced by a single bad row. A row whose metadata will not parse cannot
        hold a readable notice anyway, so it is excluded rather than rescued.

        The Python re-check below is kept as a second layer: it tolerates a notice
        stored as something other than a dict, and it is now cheap because only
        eligible rows reach it. It may only re-decide rows whose BLOB CHANGED between
        the seek and the read — it may never be narrower than the predicate above.
        Narrower means a row that is selected inside the limit and dropped outside it,
        with no state transition to stop it being selected again: a permanent hole in
        the batch. ``owed_notice_eligible`` is therefore normalized to the comparison
        SQLite makes here, storage classes and all.
        """

        instant = now or _utc_now_iso()
        owed: list[dict[str, Any]] = []
        # Literal SQL, NOT a composed ``case(...)``: SQLAlchemy renders the ``1`` and
        # the JSON path as BOUND PARAMETERS, and SQLite cannot match an index
        # expression against a query expression containing binds. The composed form
        # produced a correct result and silently kept the full scan — the index was
        # built, ignored, and nothing said so.
        #
        # These strings must stay byte-identical to the migration's; the query-plan
        # test fails if they drift, which is what keeps the duplication honest.
        notice_state = literal_column(OWED_NOTICE_STATE_SQL)
        notice_kind = literal_column(OWED_NOTICE_KIND_SQL)
        next_attempt_at = literal_column(OWED_NOTICE_NEXT_ATTEMPT_SQL)
        stmt = (
            # Both ordinary terminal VERDICTS, not just ``failed``. A binding-change notice is
            # stamped on the run that recovered the binding, and when the rebound
            # retry works that run settles ``succeeded`` — filtering on ``failed``
            # made the notice durable and permanently unreachable, which is the
            # silent-replacement bug this widening exists to close.
            #
            # ``canceled`` is admitted only for that same notice kind. Stop can land
            # AFTER the rebind and its notice commit but before terminal settlement;
            # the rebind still happened, its durable marker prevents a later restamp,
            # and excluding the row would strand the pending notice forever. Ordinary
            # canceled runs still owe nothing and remain outside the drain.
            #
            # Free of cost and of plan risk: ``ix_agent_runs_owed_notice`` indexes
            # ``(state, next_attempt_at, created_at, id)`` and NOT ``status``, so the
            # seek is on the notice state either way and ``status``/``kind`` stay a
            # post-filter over the handful of rows that actually own a pending notice.
            select(agent_runs)
            .where(
                or_(
                    agent_runs.c.status.in_(
                        [*_status_query_values("failed"), *_status_query_values("succeeded")]
                    ),
                    and_(
                        agent_runs.c.status.in_(_status_query_values("canceled")),
                        notice_kind == NOTICE_KIND_BINDING_CHANGE,
                    ),
                )
            )
            .where(
                or_(
                    agent_runs.c.run_type.is_(None),
                    agent_runs.c.run_type != _WATCH_RUNTIME_RUN_TYPE,
                )
            )
            .where(notice_state == NOTICE_PENDING)
            # A pure range term, so the index can CONSTRAIN it instead of the engine
            # filtering every pending entry per row. ISO-8601 in UTC sorts
            # lexicographically, which every other timestamp comparison here relies on.
            .where(next_attempt_at <= instant)
            # Ordered on the index prefix, so there is no temp sort AND the LIMIT can
            # short-circuit. Batch order is not load-bearing for correctness — the
            # canonical notice is chosen by ``failure_streak_decision``, not by arrival
            # order — and least-recently-deferred-first is the fairer sequence anyway.
            .order_by(next_attempt_at, agent_runs.c.created_at, agent_runs.c.id)
            .limit(max(1, limit))
        )
        with self.engine.connect() as conn:
            for row in conn.execute(stmt).mappings():
                notice = _json_loads(row["metadata_json"], {})
                notice = notice.get(OWED_FAILURE_NOTICE_KEY) if isinstance(notice, dict) else None
                # The same predicate the delivery pass re-checks before it claims, so
                # the listing and the claim cannot disagree about what "eligible" is.
                # A row whose wait has not elapsed is skipped rather than delivered,
                # which is what keeps a failing transport from firing every 2 s — and,
                # since a claimant's lease is written to the same field, what keeps a
                # second owner out of a delivery already in flight.
                if not owed_notice_eligible(notice, instant):
                    continue
                owed.append(self._run_from_row(row))
                if len(owed) >= max(1, limit):
                    break
        return owed

    def stamp_binding_change_notice(
        self,
        run_id: str,
        *,
        task_id: str,
        signature: str,
        action: str,
        reason: str,
        previous_session_id: Optional[str],
        new_session_id: Optional[str],
        settings_preserved: bool,
        now: Optional[str] = None,
        _conn: Any = None,
    ) -> Optional[dict[str, Any]]:
        """Owe the user a notice about a session binding that was replaced.

        The one notice a TERMINAL TRANSITION cannot stamp. Every other owed notice is
        folded into the UPDATE that moves a run to ``failed``
        (``_merge_owed_failure_notice``), which is what makes a settlement path added
        later inherit it for free. A rebind whose retry SUCCEEDS produces no failed
        transition at all — ``error`` is ``None`` and the row settles ``succeeded`` —
        so the news that the user's pinned session was swapped had no writer and no
        reader. This is that writer, and it is deliberately the only one. The normal
        caller commits this notice and the definition's durable dedup marker in the
        same transaction, so neither effect can survive without the other.

        Everything downstream is unchanged. The blob has the same shape as a failure
        notice plus an additive ``kind``/``binding``, so the drain's receipt, backoff
        and dead-letter protocol carries it without a parallel path.

        Identity is ``binding:{task_id}:{signature}`` — the transition's own key, not
        the run's. A rebind that somehow re-stamps against a later run therefore
        collides on the notification the user already has, rather than sending a
        second one; and it can never collide with the bare run id an ordinary backend
        failure uses for the SAME run, which is the mistake the interruption lane
        already had to be taught (see ``_owed_failure_notice_for_transition``).

        Never over an existing notice: a genuine failure notice on this row outranks
        the binding news, and re-stamping would reset ``attempts`` and resurrect a
        dead letter.

        And never onto a run something else TERMINALIZED in the meantime. This is the
        one owed-notice writer with no terminal transition to ride — it runs on a live
        run, before ``complete()`` — so its read and its write are two statements, and
        under pysqlite ``engine.begin()`` holds no lock across the first (no ``BEGIN``
        is emitted for a bare SELECT; see ``upsert_definition_in_connection`` and
        ``update_owed_failure_notice``). Round 7 audited this stamp and excused it as
        "a pre-read stamp", which was right about the ORDER and wrong about the
        CONNECTION: a second one can settle the row in that gap, and there are three
        damage directions — two the CAS must refuse, and one it must NOT.

        * The terminal writer settles ``failed`` and stamps its OWN failure notice in
          the same UPDATE (``_merge_owed_failure_notice``), and the whole-blob write
          this used to issue put the binding blob over the top — the user told their
          session was swapped and never told the run failed.
        * The row settles ``canceled`` before this CAS, so the stamp is refused: a Stop
          that already won outranks binding news. This is distinct from Stop landing
          AFTER the stamp committed. In that later ordering the rebind already happened
          and its marker prevents a restamp, so ``list_owed_failure_notices`` admits
          canceled rows carrying this specific notice kind.
        * The row settles ``succeeded`` — the ORDINARY outcome of the rebind this
          notice exists for — and a status CAS that refuses here loses the notice
          PERMANENTLY rather than deferring it. A successful settlement writes no
          notice of its own, so the slot is left empty. The store-level method also
          remains correct when called outside the combined marker transaction: refusing
          this direction would silently lose the news that the pinned session changed.

        So the read is re-asserted in the WHERE clause — the status the SELECT saw,
        verbatim, no cancellation request, plus ``owed_notice_absent()`` — and the
        loss is read off ``rowcount``, the ``DefinitionWriteExpectation`` idiom
        ``update_owed_failure_notice`` already follows. ``json_set`` rather than a
        composed blob for the same atomicity reason one level down: the run is LIVE
        here, so sibling metadata
        keys are being written concurrently (the sweep's ``interrupt_reason``, the
        settler's ``ok`` marker), and a status+notice CAS over a whole-blob write
        would still clobber whichever of those landed in the gap. Only the one key
        this method owns is written.

        A lost CAS then re-reads the row ONCE and decides on the WINNER's status. The
        policy is TOTAL over the terminal statuses, so no outcome falls through to an
        accidental default:

        ================ ==================================================
        winner           outcome
        ================ ==================================================
        ``failed``       refuse — its own failure notice owns the slot and
                         outranks this news
        ``canceled``     refuse — the user's Stop won before this stamp;
                         only a binding notice committed before a later
                         Stop is admitted by the drain
        ``succeeded``    STAMP — no other writer owes anything, the slot is
                         legitimately owed, and
                         ``list_owed_failure_notices`` selects ``succeeded``
                         precisely to carry it
        anything else    refuse — still live (nothing terminalized, so the
                         loss was the notice slot or a malformed blob),
                         slot occupied, or unreadable metadata
        ================ ==================================================

        EXACTLY ONE retry, and the reason is CONVERGENCE rather than luck. It is NOT
        that the first no-op UPDATE holds a write lock — an UPDATE that matches zero
        rows may never escalate to RESERVED, so nothing here can be argued from lock
        acquisition. It is that ``succeeded`` is TERMINAL to every GUARDED writer:
        ``settle_run_terminal`` and the rest of the terminal set are conditioned on a
        non-terminal status, so none of them can move the row once the re-read sees
        ``succeeded``, and the retry's status predicate cannot go stale a second time.
        The notice slot is equally settled: another stamp on this run sees the occupied
        slot, and no terminal transition remains to stamp a failure notice.
        The remaining writer that can touch this row is a sibling-key ``json_set`` (the
        sweep's ``interrupt_reason``), which changes neither the status nor the slot and
        so cannot make the retry lose.

        Not claimed: that the status is IMMUTABLE. ``update_run_status`` writes by id
        with no status predicate, and the cancel-bookkeeping and requeue paths reach it
        (``mark_run_canceled``, ``requeue``); on an already-terminal row that is a
        caller bug, but it is reachable prose-wise and the argument must not pretend
        otherwise. It is harmless HERE either way: such a write inside this window
        moves the status off the re-read value, the retry's CAS matches zero rows, and
        the method returns ``None``. The failure mode of a broken invariant is a lost
        notice, never a notice mis-stamped onto a row that moved — so a second failure
        would mean something upstream is wrong, not that a third attempt would help.
        """

        instant = now or _utc_now_iso()
        transaction = nullcontext(_conn) if _conn is not None else self.engine.begin()
        with transaction as conn:
            row = conn.execute(
                select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
            ).mappings().first()
            if not row:
                return None
            if bool(row["cancel_requested"]) or normalize_run_status(row["status"]) == "canceled":
                return None
            metadata = _json_loads(row["metadata_json"], {})
            if not isinstance(metadata, dict):
                return None
            existing = metadata.get(OWED_FAILURE_NOTICE_KEY)
            if isinstance(existing, dict) and str(existing.get("state") or "").strip():
                return None
            notice = {
                "state": NOTICE_PENDING,
                "attempts": 0,
                # Always an instant, never ``None`` — see the eligibility expressions.
                "next_attempt_at": instant,
                "failure_id": f"binding:{task_id}:{signature}",
                "kind": NOTICE_KIND_BINDING_CHANGE,
                # No interruption: the lane is decided by ``kind``, and leaving this
                # ``None`` keeps ``is_interruption`` answering the same question it
                # always did rather than becoming a two-meaning field.
                "interrupt_reason": None,
                "binding": {
                    "task_id": task_id,
                    "action": action,
                    "reason": reason,
                    "previous_session_id": previous_session_id,
                    "new_session_id": new_session_id,
                    "settings_preserved": bool(settings_preserved),
                },
                "error": None,
                "ack_evidence": None,
                "stamped_at": instant,
            }
            # ``coalesce`` so a row that has never carried metadata is stamped rather
            # than refused: ``json_set(NULL, …)`` is NULL, which would erase the column.
            # ``json_valid`` over the SAME expression so a MALFORMED blob is refused
            # instead of raising ``malformed JSON`` out of the rebind path — the write-time
            # half of the discipline ``OWED_NOTICE_STATE_SQL`` documents (HFR-084). Refused
            # rather than repaired: the previous whole-blob write silently replaced an
            # unreadable blob with a fresh one, discarding whatever else was in it.
            metadata_source = func.coalesce(agent_runs.c.metadata_json, "{}")

            def _stamp_against(observed_status: Any) -> int:
                """The guarded write, conditioned on status and no explicit Stop."""

                return conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.id == run_id)
                    # The status the SELECT read, verbatim rather than normalized:
                    # this is a compare-and-swap on the value that was actually seen,
                    # and widening it through ``_status_query_values`` would let an
                    # alias transition slip past the guard.
                    .where(agent_runs.c.status == observed_status)
                    # An explicit Stop outranks binding-recovery news. This closes
                    # both shapes: queued Stop already changed the status to canceled;
                    # running Stop changed only cancel_requested.
                    .where(cancel_not_requested())
                    .where(*owed_notice_absent())
                    .where(func.json_valid(metadata_source) == 1)
                    .values(
                        metadata_json=func.json_set(
                            metadata_source,
                            f"$.{OWED_FAILURE_NOTICE_KEY}",
                            func.json(_json_dumps(notice)),
                        ),
                        updated_at=instant,
                    )
                ).rowcount

            if _stamp_against(row["status"]):
                return notice

            # The CAS lost. Re-read ONCE and decide on the winner: see the policy
            # table above. Only a ``succeeded`` winner is retried, because it is the
            # only terminal status that writes no notice of its own and so leaves a
            # slot this method legitimately owes.
            settled = (
                conn.execute(
                    select(agent_runs.c.status, agent_runs.c.metadata_json)
                    .where(agent_runs.c.id == run_id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            winner = normalize_run_status(settled["status"]) if settled else None
            if winner == "succeeded":
                later = _json_loads(settled["metadata_json"], {})
                occupant = later.get(OWED_FAILURE_NOTICE_KEY) if isinstance(later, dict) else None
                slot_is_empty = isinstance(later, dict) and not (
                    isinstance(occupant, dict) and str(occupant.get("state") or "").strip()
                )
                # The retry re-asserts the RE-READ status, not the original one, and
                # keeps ``owed_notice_absent()``/``json_valid`` unchanged — so it is
                # the same compare-and-swap over a fresher observation, never a
                # weakened one. Terminal status plus single-stamper marker is what
                # makes one attempt sufficient; there is no loop.
                if slot_is_empty and _stamp_against(settled["status"]):
                    return notice

            # NOTHING was written. Reporting ``None`` rather than the composed notice
            # matters for the same reason it does in ``update_owed_failure_notice``:
            # the caller must not treat its own view as what the row now says.
            logger.debug(
                "binding notice for %s not stamped: read status %r, settled as %r, "
                "and that outcome either owns the notice slot or reserves it",
                run_id,
                row["status"],
                None if settled is None else settled["status"],
            )
            return None

    def update_owed_failure_notice(
        self,
        run_id: str,
        *,
        expect: Optional[tuple[str, int, str]] = None,
        **fields: Any,
    ) -> Optional[dict[str, Any]]:
        """Merge fields into one run's owed notice, in a single guarded write.

        Guarded on the notice still EXISTING rather than on the run's status: the run
        is already terminal by construction, and the thing that must not be clobbered
        is a notice another pass resolved.

        ``expect`` adds the second half of that: the ``(state, attempts,
        next_attempt_at)`` the caller DECIDED FROM, as ``notice_write_expectation`` read
        it, re-asserted here so a write cannot land behind a newer one. Existence alone is not enough for the
        drain, which checks service ownership ONCE at the top of a pass and then
        AWAITS delivery: a lock handoff can lapse the outgoing owner's lease while its
        coroutine is still suspended in that send, so the incoming owner reads the same
        pending notice, delivers it and acknowledges, and the resumed pass then writes
        its stale ``pending`` retry or ``failed`` dead letter over the ``sent``. The
        ``failed`` direction is the one that hurts: it is terminal, so a receipt the
        user already has is buried for good.

        The predicate goes in the UPDATE's WHERE clause, not in Python, for the reason
        ``upsert_definition_in_connection`` spells out: the SELECT below reserves
        nothing — pysqlite emits no ``BEGIN`` for a bare SELECT, so the write lock is
        first taken by the UPDATE — and a comparison made in the gap between them is a
        check-then-act that two passes can both pass. Evaluated by SQLite in the
        writing statement, the loser matches zero rows and is detected by ``rowcount``.
        ``attempts`` is part of the predicate so two passes that both read attempt N
        cannot both consume it. DELIBERATELY NOT ``updated_at`` — see
        ``DefinitionWriteExpectation``: a row-version guard refuses benign writes, and
        a freshly stamped notice carries no such marker at all.

        The loser SILENTLY no-ops (``None``, nothing written) instead of raising. It has
        nothing to repair — the next 2 s tick re-reads whatever the winner settled — and
        an exception would be caught by the drain's per-row handler and logged on every
        handoff. ``expect=None`` keeps the unguarded merge for the stamp/rewind callers,
        which write no predicate and so behave exactly as before.

        ``expect`` is keyword-only AND declared before ``**fields`` on purpose:
        ``fields`` is merged verbatim into the notice blob, so a positional or
        trailing spelling would silently persist the expectation as notice content.
        """

        now = _utc_now_iso()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(agent_runs).where(agent_runs.c.id == run_id).limit(1)
            ).mappings().first()
            if not row:
                return None
            metadata = _json_loads(row["metadata_json"], {})
            if not isinstance(metadata, dict):
                return None
            notice = metadata.get(OWED_FAILURE_NOTICE_KEY)
            if not isinstance(notice, dict):
                return None
            merged = dict(notice)
            merged.update(fields)
            merged["updated_at"] = now
            metadata[OWED_FAILURE_NOTICE_KEY] = merged
            stmt = update(agent_runs).where(agent_runs.c.id == run_id)
            if expect is not None:
                stmt = stmt.where(*owed_notice_state_unchanged(expect))
            result = conn.execute(
                stmt.values(metadata_json=_json_dumps(metadata), updated_at=now)
            )
            if expect is not None and not result.rowcount:
                # LOST, and nothing was written. Reporting ``None`` rather than the
                # payload matters: the caller must not treat its own merged view as
                # what the row now says.
                logger.debug(
                    "owed failure notice for %s moved from %s; stale write dropped",
                    run_id,
                    expect,
                )
                return None
            return merged

    def owed_failure_notice(self, run_id: str) -> Optional[dict[str, Any]]:
        run = self.get_run(run_id)
        notice = (run or {}).get("metadata") or {}
        notice = notice.get(OWED_FAILURE_NOTICE_KEY) if isinstance(notice, dict) else None
        return notice if isinstance(notice, dict) else None

    def _definition_history_scope(self, definition_id: str, *columns: Any) -> Any:
        """One definition's history, minus every row class that is not a verdict.

        Same exclusions as the health window and for the same reasons — most
        sharply, the watch supervisor heartbeat flips its predecessor to
        ``succeeded`` on every write, and a ``succeeded`` row bearing the watch's
        ``definition_id`` sitting between two failures CLOSES the streak, so every
        watch failure would read as a first failure and notify. Fixing only the
        deferral predicate would trade a permanent silence for daily spam.

        Interruptions are dropped HERE, in SQL, rather than while classifying: they
        are transparent to a streak — neither joining one nor closing one — so a row
        the classifier would skip must never reach it, exactly as the health window
        argues. Letting one join would absorb a D1 notice into an unrelated streak
        and skip it as a duplicate.
        """

        return (
            select(*columns)
            .where(agent_runs.c.definition_id == definition_id)
            .where(
                or_(
                    agent_runs.c.run_type.is_(None),
                    agent_runs.c.run_type != _WATCH_RUNTIME_RUN_TYPE,
                )
            )
            .where(_not_an_out_of_band_interruption())
        )

    def consecutive_definition_failures_with_code(
        self,
        definition_id: str,
        failure_code: str,
        *,
        limit: int,
    ) -> int:
        """Count a bounded suffix of failed verdicts carrying ``failure_code``.

        The current run is still ``running`` when binding recovery asks, so this
        reads only prior terminal verdicts. A success or a differently-classified
        failure closes the suffix; canceled and out-of-band interruption rows are
        transparent through ``_definition_history_scope``.
        """

        if limit <= 0:
            return 0
        statement = (
            self._definition_history_scope(
                str(definition_id),
                agent_runs.c.status,
                agent_runs.c.metadata_json,
            )
            .where(agent_runs.c.status.in_(_status_query_values("succeeded") + _status_query_values("failed")))
            .order_by(agent_runs.c.created_at.desc(), agent_runs.c.id.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(statement).all()
        count = 0
        for status, metadata_json in rows:
            if normalize_run_status(status) != "failed":
                break
            metadata = _json_loads(metadata_json, {})
            if not isinstance(metadata, dict) or metadata.get("failure_code") != failure_code:
                break
            count += 1
        return count

    def failure_streak_decision(self, definition_id: str, run_id: str) -> dict[str, Any]:
        """The three facts ``core.failure_notices.decide`` needs, in ONE statement.

        Not the streak. The drain never wanted a streak — it wanted to know whether
        this run belongs to one, whether anybody in it has already told the user, and
        which row is the one still trying. Returning the rows themselves was what made
        the read cost the streak's LENGTH and forced the caller to redo the
        classification in Python.

        ``in_streak``
            ``run_id`` is a verdict of this definition — not an interruption, not a
            heartbeat, not another definition's run, not nonterminal. When it is
            ``False`` the other two facts are ``False``/``None`` BY CONSTRUCTION
            rather than by a Python guard: the window predicates carry an
            anchor-membership term, so a run that belongs to no streak yields an empty
            window instead of the definition's whole history.
        ``has_sent_elsewhere``
            some OTHER row of the same streak has notice state ``sent``. Evidence of
            delivery anywhere in the streak makes this row a duplicate.
        ``earliest_pending_id``
            the earliest still-``pending`` row of the streak by ``(created_at, id)``:
            the canonical notice. ``failed`` (dead-lettered) and ``skipped`` rows drop
            out, which is how promotion works — a streak whose canonical exhausted its
            retries still owes the user the news.

        The streak is still exactly what it was: the run of verdicts strictly between
        the two ``succeeded`` rows bracketing this one, so a success on either side
        closes it and the next failure after a recovery notifies again.

        WHY ONE STATEMENT, and this is the whole point of the shape (two reasons, both
        load-bearing):

        1. ONE SNAPSHOT. pysqlite does not open a transaction for reads, so three bare
           ``SELECT``s saw three different databases. A success settling between the
           "following success" seek and the range query MERGES two streaks: the later
           streak's rows are then read as members of the earlier one, and a ``sent``
           notice belonging to the earlier outage makes ``decide`` answer SKIP for a
           LIVE one. That is a lost notice — the D1 direction, not the duplicate
           direction. The previous docstring argued the reads were safe because "every
           row they read is already SETTLED", which is true of each row individually
           and says nothing about which rows the WINDOW contains; the boundaries are
           what moves. One statement is one SQLite read snapshot, so the boundaries and
           the rows inside them are read from the same database.
        2. A BOUNDED READ. The range query materialised every row of the streak and
           JSON-decoded each one. For a definition that has never succeeded the streak
           IS the lifetime, and ``2 * len(streak) + 2`` decodes is O(lifetime) — the
           bound HFR-095 claimed was a bound on the answer, which is only a bound when
           the answer is small. Three scalars cross into Python now and NO metadata
           blob is decoded here at all: the notice states are compared inside SQLite
           through ``OWED_NOTICE_STATE_SQL``.

        Every term is a seek on ``ix_agent_runs_definition_streak``
        (``(definition_id, created_at, id)``, migration ``20260729_0042``): the same
        two row-value boundary seeks as before, then the window itself constrained on
        BOTH ends — ``(definition_id=? AND (created_at,id)>(?,?) AND
        (created_at,id)<(?,?))``.
        """

        succeeded = _status_query_values("succeeded")
        verdicts = succeeded + _status_query_values("failed")
        # The sequence key, as a ROW VALUE. ``created_at`` alone is not a position:
        # these are application-written ISO strings and several writers stamp a whole
        # batch with one value, so a bare ``created_at <`` both loses rows tied with a
        # boundary and can leave a SUCCESS inside the window — which silently merges
        # two streaks into one and skips the second one's notice as a duplicate. A row
        # value keeps the tie-break IN the comparison, and SQLite can constrain
        # ``(created_at, id) < (?, ?)`` with an index instead of sorting.
        position = tuple_(agent_runs.c.created_at, agent_runs.c.id)

        # The anchor's position, as a SUBQUERY rather than a value fetched first. That
        # is the difference between one statement and two, and therefore between one
        # snapshot and two.
        anchor_created = (
            self._definition_history_scope(definition_id, agent_runs.c.created_at)
            .where(agent_runs.c.id == run_id)
            .where(agent_runs.c.status.in_(verdicts))
            .limit(1)
            .scalar_subquery()
        )
        here = tuple_(anchor_created, literal(str(run_id)))

        def _closing_success(column: Any, before: bool) -> Any:
            """One element of the nearest bracketing success's position."""

            stmt = self._definition_history_scope(definition_id, column).where(
                agent_runs.c.status.in_(succeeded)
            )
            if before:
                stmt = stmt.where(position < here).order_by(
                    agent_runs.c.created_at.desc(), agent_runs.c.id.desc()
                )
            else:
                stmt = stmt.where(position > here).order_by(
                    agent_runs.c.created_at, agent_runs.c.id
                )
            return stmt.limit(1).scalar_subquery()

        # AN ABSENT BOUNDARY IS A SENTINEL, NOT A DROPPED PREDICATE. The three-statement
        # form knew at build time whether each seek had found anything and simply
        # omitted the term when it had not; a single statement cannot, and expressing
        # it as ``(boundary IS NULL OR position > boundary)`` would make the term a
        # DISJUNCTION, which SQLite cannot use as an index constraint — the plan would
        # name the index while the range stayed a per-row filter, which is the exact
        # failure ``20260728_0040`` shipped and HFR-086 pinned.
        #
        # Below every position: the empty string, which sorts at or below every value
        # of both keys. The one position it excludes is ``('', '')`` itself — a row
        # with an empty primary key AND an empty ``created_at``, which no writer can
        # produce (``enqueue_run`` stamps an id and an ISO instant on every row).
        #
        # Above every position: the definition's own LAST ``created_at`` with a
        # character appended. This is derived rather than a magic high constant because
        # a constant would have to out-sort every possible ``created_at`` byte string
        # and nothing guarantees that, while ``X < X || 'x'`` holds for every ``X``
        # under BINARY collation (``X`` is a strictly shorter prefix). It is found by
        # the same ``LIMIT 1`` index seek as the boundaries, not by a ``max()`` over
        # the definition.
        above_every_position = func.coalesce(
            select(agent_runs.c.created_at)
            .where(agent_runs.c.definition_id == definition_id)
            .order_by(agent_runs.c.created_at.desc(), agent_runs.c.id.desc())
            .limit(1)
            .scalar_subquery(),
            "",
        ) + literal("x")
        opened = tuple_(
            func.coalesce(_closing_success(agent_runs.c.created_at, True), ""),
            func.coalesce(_closing_success(agent_runs.c.id, True), ""),
        )
        closed = tuple_(
            func.coalesce(_closing_success(agent_runs.c.created_at, False), above_every_position),
            func.coalesce(_closing_success(agent_runs.c.id, False), ""),
        )

        # The notice state, read by SQLite. ``OWED_NOTICE_STATE_SQL`` referenced rather
        # than retyped, under the same ``coalesce``/``CAST`` shape
        # ``owed_notice_state_unchanged`` uses and normalized to agree with the Python
        # side value for value: a notice that is not an object, a state stored as a
        # number, and a malformed blob all read as "not this state" on both sides.
        notice_state = cast(func.coalesce(literal_column(OWED_NOTICE_STATE_SQL), ""), Text)

        def _window(*columns: Any) -> Any:
            return (
                self._definition_history_scope(definition_id, *columns)
                .where(agent_runs.c.status.in_(verdicts))
                # ANCHOR MEMBERSHIP, as a window term. Without it a ``run_id`` that is
                # not a verdict of this definition leaves both boundaries NULL, both
                # sentinels apply, and the "window" becomes the definition's ENTIRE
                # history — unbounded, and answering about a streak the run is not in.
                # The old form got this from an early ``return []``; a single statement
                # has to say it in SQL. It is an uncorrelated term, so SQLite evaluates
                # it once and skips the subquery outright when it is false.
                .where(anchor_created.isnot(None))
                .where(position > opened)
                .where(position < closed)
            )

        statement = select(
            exists(
                self._definition_history_scope(definition_id, literal(1))
                .where(agent_runs.c.id == run_id)
                .where(agent_runs.c.status.in_(verdicts))
            ).label("in_streak"),
            # ``id != run_id``: evidence of delivery by SOMEBODY ELSE. This row's own
            # ``sent`` state is not evidence that it is a duplicate.
            exists(
                _window(literal(1))
                .where(agent_runs.c.id != run_id)
                .where(notice_state == NOTICE_SENT)
            ).label("has_sent_elsewhere"),
            _window(agent_runs.c.id)
            .where(notice_state == NOTICE_PENDING)
            .order_by(agent_runs.c.created_at, agent_runs.c.id)
            .limit(1)
            .scalar_subquery()
            .label("earliest_pending_id"),
        )

        with self.engine.connect() as conn:
            row = conn.execute(statement).mappings().first()
        earliest = (row or {}).get("earliest_pending_id")
        return {
            "in_streak": bool((row or {}).get("in_streak")),
            "has_sent_elsewhere": bool((row or {}).get("has_sent_elsewhere")),
            "earliest_pending_id": str(earliest) if earliest is not None else None,
        }

    def earliest_unsettled_run_before(
        self,
        definition_id: str,
        *,
        created_at: str,
        run_id: str,
        stale_after_seconds: Optional[float] = None,
        now: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """An earlier-created execution of this definition that has not settled.

        The streak is only computable over a settled PREFIX. ``create_per_run``
        definitions hold no execution lock, so executions genuinely overlap and
        completion order need not follow ``created_at``: a later-created run can fail
        first, become canonical and send, and then an earlier-created run fails and
        becomes the new earliest — a second notice for one outage.

        ``watch_runtime`` is excluded, and that exclusion is load-bearing in the
        opposite direction from the streak's: the heartbeat is earlier-created and
        permanently nonterminal, so every failed watch run would defer behind its own
        supervisor forever and never deliver a notice at all.

        ``stale_after_seconds`` bounds the wait. The plan's argument for an unbounded
        wait is that "settling every nonterminal run is precisely what PR1/PR2/PR7
        guarantee" — but PR2/PR4/PR7 are not landed, so on the current tree a queued
        row for a paused definition can sit nonterminal indefinitely and the notice
        would never be delivered. Past the cap the row is treated as settled, which
        risks a duplicate notice rather than a lost one; the plan chooses that
        direction explicitly ("a duplicated notice is a papercut, a lost one is the
        D1 violation"). Remove the cap once those PRs land.

        Both filters are SQL TERMS, not a Python ``continue``. This runs once per
        pending owed notice on the two-second drain tick, exactly like the eligibility
        lookup and the streak read, and it was the last read in that path deciding in
        Python what the index could decide for it: selecting every queued/running row
        for the definition made a definition holding a large nonterminal backlog — a
        paused ``create_per_run`` task, a queue drained slower than it fills — pay that
        whole backlog per notice per tick to answer a question whose answer is at most
        one row. ``ORDER BY created_at, id LIMIT 1`` over ``ix_agent_runs_definition_streak``
        (``(definition_id, created_at, id)``, migration ``20260729_0042``) is the same
        answer as the first row the loop accepted.
        """

        instant = _parse_iso_instant(now) or datetime.now(timezone.utc)
        # The anchor position as a ROW VALUE, for the reason ``failure_streak_decision``
        # spells out: ``created_at`` alone is not a position, because several writers stamp
        # a whole batch with one value, and a row value keeps the tie-break IN the
        # comparison where SQLite can constrain it with the index.
        position = tuple_(agent_runs.c.created_at, agent_runs.c.id)
        here = tuple_(literal(str(created_at)), literal(str(run_id)))
        stmt = (
            select(agent_runs.c.id, agent_runs.c.created_at, agent_runs.c.status)
            .where(agent_runs.c.definition_id == definition_id)
            .where(
                or_(
                    agent_runs.c.run_type.is_(None),
                    agent_runs.c.run_type != _WATCH_RUNTIME_RUN_TYPE,
                )
            )
            .where(
                agent_runs.c.status.in_(
                    _status_query_values("queued") + _status_query_values("running")
                )
            )
            .where(position < here)
            .order_by(agent_runs.c.created_at, agent_runs.c.id)
            .limit(1)
        )
        if stale_after_seconds is not None:
            # ONE DOCUMENTED DIVERGENCE from the Python filter this replaces. That one
            # asked ``_parse_iso_instant`` and SKIPPED the staleness test when the
            # answer was ``None``, so a row whose ``created_at`` cannot be parsed read
            # as fresh and blocked the notice for as long as it stayed nonterminal —
            # which, for an unparseable timestamp, is a wait nothing can bound. A
            # lexicographic cutoff has no such escape and may class the same value as
            # stale, treating it as settled. That is the duplicate-not-lost direction
            # the cap itself already chose, so it is the acceptable side to land on.
            # Pinned by ``test_an_unparseable_created_at_reads_as_stale_rather_than_as_a_blocker``.
            #
            # ``>=`` because the Python test skipped on ``> stale_after_seconds``: a row
            # exactly at the cap was kept, and it still is.
            cutoff = (instant - timedelta(seconds=stale_after_seconds)).isoformat()
            stmt = stmt.where(agent_runs.c.created_at >= cutoff)
        with self.engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            return None
        return {"id": row["id"], "created_at": row["created_at"], "status": row["status"]}

    # --- derived definition health -------------------------------------------
    #
    # ONE STATEMENT, and per definition a BOUNDED SEEK. Those two requirements pull
    # in opposite directions and both are load-bearing, so the shape is not a matter
    # of taste:
    #
    # * one statement, because ``_enrich_definitions`` resolves the whole page here
    #   (see the ``definition health`` lookup: "Derived health for the whole page in
    #   one query") and ``test_a_page_costs_a_fixed_number_of_queries`` pins a page's
    #   statement count at a fixed budget this read holds exactly one slot in. That
    #   budget is #1033's invariant: the per-row enrichment it replaced issued a query
    #   per row, and a 30-row page paid it thirty times over. A statement per
    #   definition here would put it straight back.
    # * a bounded seek, because ``HEALTH_WINDOW_RUNS`` advertises ten verdicts and the
    #   window-function form it replaced had to rank the definition's ENTIRE 72 h
    #   window before ``position <= 10`` could discard anything. On a definition firing
    #   every minute that is thousands of rows examined per list row, and the
    #   settled-time index cannot stop early because ``row_number()`` needs them all.
    #
    # The one shape that does both, SQLite having no ``LATERAL``: iterate the id list
    # as a virtual table (``json_each``) and answer each id with a CORRELATED scalar
    # subquery that seeks ``(definition_id, settled DESC, id DESC)`` and stops at
    # ``LIMIT HEALTH_WINDOW_RUNS``, aggregated into one JSON blob per definition. The
    # measured plan is a bounded ``SEARCH`` with no ``SCAN agent_runs`` and no temp
    # B-tree; the residual ``SCAN d`` walks the id list and ``SCAN recent`` walks the
    # already-limited ten-row co-routine. There is no ranking step left, which is the
    # literal ask: the limit is applied before anything ranks.
    #
    # The honest framing of the trade: the budget invariant counts STATEMENTS because
    # statement dispatch was the per-row cost #1033 removed. N bounded seeks are the
    # irreducible work of answering N health questions — what changed is that each is
    # now a ten-row early exit instead of a full-window rank, paid inside one dispatch.
    #
    # ORDER IS RE-ESTABLISHED IN PYTHON. ``json_group_array`` makes no promise about
    # the order it aggregates in — the inner ``ORDER BY`` is what bounds the seek, not
    # what orders the array — so the blob is sorted by ``(settled, id)`` descending
    # after decoding. Ten entries per definition, so it costs nothing, and relying on
    # the aggregate's incidental order would be a badge that flips between reads with
    # no write in between.
    #
    # ``agent_runs`` filtered to one definition is a history of ROWS, and health is
    # a function of settled OUTCOMES only. Four row classes therefore have to be
    # excluded, and every one of them by predicate rather than while classifying,
    # because the ``LIMIT`` is applied to whatever the predicates let through —
    # anything the classifier would ignore must never reach it:
    #
    # 1. the watch supervisor heartbeat (``run_type = watch_runtime``), which shares
    #    the watch's ``definition_id``, is refreshed to the waiter's ``started_at``
    #    on every restart, and flips its predecessor to ``succeeded`` — so it both
    #    presents as the newest "run" and closes a failure streak;
    # 2. nonterminal executions: a failing recurring definition's next fire is the
    #    newest row for the definition and is not an outcome, so reading "the latest
    #    run failed" off it reports an actively failing definition as healthy for the
    #    whole duration of its next attempt;
    # 3. ``canceled``: a cancellation is the absence of an outcome, so N cancelled
    #    retries would displace the failure they are supposed to be transparent to;
    # 4. out-of-band interruptions — but by MEMBERSHIP in ``RUN_INTERRUPTION_REASONS``,
    #    never by ``interrupt_reason IS NOT NULL``. Nullness would also exclude
    #    ``no_terminal_result`` / ``refused_concurrent_turn`` / ``transport_unavailable``
    #    / ``queue_hold_expired``, which are the ordinary per-fire failures this
    #    whole feature exists to surface.

    def last_success_settled_at(
        self,
        definition_id: str,
        *,
        conn: Any = None,
    ) -> Optional[str]:
        """When this definition last SUCCEEDED, or ``None`` if it never has.

        D5 requires the failure notice's body to say "when it last succeeded", and no
        read for it existed. This is that read and nothing more: ONE instant, so ONE
        row.

        The seek is the ``_health_rows`` seek with the window predicates removed —
        ``ix_agent_runs_definition_settled`` (``(definition_id, coalesce(completed_at,
        created_at) desc, id desc)``, migration ``20260728_0039``) supplies both the
        equality and the order, so ``LIMIT 1`` early-exits on the index with nothing
        sorted. Every filter is a SQL TERM rather than a Python ``continue``: this runs
        once per notice on the two-second drain tick, and a definition with a long
        history must not pay for it (the HFR-068 lesson).

        THREE SPELLINGS SHARED BY NAME, never retyped — ``_SETTLED_AT`` (the index's own
        second key), ``_status_query_values("succeeded")`` (the column holds legacy
        spellings alongside canonical ones, so a literal ``'succeeded'`` would miss every
        row written as ``completed``) and the ``settled DESC, id DESC`` tie-break (these
        timestamps are application-written ISO strings and several writers stamp a whole
        batch with one value, so without the secondary key "the last success" is whichever
        row SQLite happens to return first).

        ``watch_runtime`` is excluded for the same reason the health window excludes it:
        the supervisor heartbeat is not the definition succeeding, and it flips to
        ``succeeded`` on every restart — so without this term a permanently broken watch
        would report a fresh "last succeeded" instant on every service restart.
        """

        statement = (
            select(_SETTLED_AT)
            .where(agent_runs.c.definition_id == str(definition_id))
            .where(
                or_(
                    agent_runs.c.run_type.is_(None),
                    agent_runs.c.run_type != _WATCH_RUNTIME_RUN_TYPE,
                )
            )
            .where(agent_runs.c.status.in_(_status_query_values("succeeded")))
            .order_by(_SETTLED_AT.desc(), agent_runs.c.id.desc())
            .limit(1)
        )
        if conn is not None:
            value = conn.execute(statement).scalar_one_or_none()
        else:
            with self.engine.connect() as active:
                value = active.execute(statement).scalar_one_or_none()
        text_value = str(value or "").strip()
        return text_value or None

    def _health_rows(
        self,
        definition_ids: Sequence[str],
        *,
        now: Optional[str] = None,
        conn: Any = None,
    ) -> dict[str, list[Optional[str]]]:
        """Each definition's last N verdicts within T hours, newest first.

        ``None`` in place of a verdict means the row is UNREADABLE — its
        ``metadata_json`` will not parse — so nothing about it can be classified and
        ``_classify_health`` degrades the whole definition to ``HEALTH_UNKNOWN``. It is
        carried as a value rather than raised, because the window is per definition and
        one bad row must not take the page down (HFR-072).
        """

        ids = [str(value or "").strip() for value in definition_ids]
        ids = [value for value in dict.fromkeys(ids) if value]
        if not ids:
            return {}
        instant = _parse_iso_instant(now) or datetime.now(timezone.utc)
        cutoff = (instant - timedelta(hours=HEALTH_WINDOW_HOURS)).isoformat()

        settled_at = _SETTLED_AT
        # Not a literal ``('succeeded', 'failed')``: the column holds legacy
        # spellings alongside canonical ones, so a literal list would miss every row
        # written as ``completed`` and report a healthy definition as failing.
        verdicts = _status_query_values("succeeded") + _status_query_values("failed")

        # The id list travels as ONE json parameter and is iterated as a virtual
        # table, so the bound-parameter count is constant in the number of definitions
        # — which is what lets the unpaged harness list stay a single statement where
        # an ``IN`` list had to be chunked by ``_id_batches``.
        id_list = func.json_each(_json_dumps(ids)).table_valued("value").alias("d")
        # READABILITY, selected alongside the verdict rather than read separately — see
        # the note on ``json_array`` below for why it belongs in this statement.
        #
        # A NULL or empty column is VALID: an absent blob is not an unreadable one, and
        # ``json_valid(NULL)`` is NULL while ``json_valid('')`` is 0, both of which would
        # otherwise mark every metadata-free run unreadable.
        #
        # Valid is NOT enough: ``json_valid('[]')`` and ``json_valid('"value"')`` are 1,
        # but the metadata SCHEMA — an object with keys — cannot be read out of a
        # top-level array, string, number, boolean, or JSON null, so those rows are
        # exactly as unclassifiable as a malformed blob and get the same treatment.
        # The type check sits in a CASE branch BEHIND the validity check, deliberately:
        # ``json_type`` on malformed input raises and would fail the whole statement —
        # the very failure mode the ``json_valid`` guards exist to prevent (HFR-072) —
        # and SQLite evaluates CASE branches lazily, so the invalid arm never reaches it.
        _metadata_blob = func.coalesce(func.nullif(agent_runs.c.metadata_json, ""), "{}")
        readable = case(
            (func.json_valid(_metadata_blob) == 1, func.json_type(_metadata_blob) == "object"),
            else_=literal(False),
        )
        recent = (
            select(
                settled_at.label("settled"),
                agent_runs.c.id.label("id"),
                agent_runs.c.status.label("status"),
                readable.label("readable"),
            )
            # Correlated to the id currently being iterated, which is what makes the
            # LIMIT below per-definition rather than per-batch.
            .where(agent_runs.c.definition_id == id_list.c.value)
            .where(
                or_(
                    agent_runs.c.run_type.is_(None),
                    agent_runs.c.run_type != _WATCH_RUNTIME_RUN_TYPE,
                )
            )
            .where(agent_runs.c.status.in_(verdicts))
            .where(settled_at >= cutoff)
            # The same expression the streak read excludes interruptions with, shared
            # by NAME rather than retyped: two copies of a JSON extract drift
            # silently, and the copy that drifts is the one the planner stops
            # matching. Its ``CASE json_valid`` guard is also what keeps one
            # unparseable blob from failing the whole statement (HFR-072).
            .where(_not_an_out_of_band_interruption())
            # ``id DESC`` is a required tie-break, not tidiness: these timestamps are
            # application-written ISO strings and several writers stamp a whole batch
            # with ONE value, so without a secondary key "the newest run" is whichever
            # row SQLite happens to return first and the badge can flip between reads
            # with no write in between. ``list_pending_callbacks`` already orders this
            # way one screen away — and both keys are the index's own, so the order is
            # free and the LIMIT early-exits on it.
            .order_by(settled_at.desc(), agent_runs.c.id.desc())
            .limit(HEALTH_WINDOW_RUNS)
            # Explicit: the inner seek must NOT put the id list in its own FROM, or
            # every definition would answer with every definition's runs.
            .correlate(id_list)
            .subquery("recent")
        )
        # ``settled`` and ``id`` are carried out of SQL, not dropped, because the sort
        # has to be redone in Python — see the block comment: ``json_group_array``
        # promises no order.
        #
        # READABILITY IS THE FOURTH ELEMENT, and it travels inside the SAME statement
        # rather than as a second read, because the page budget invariant holds this
        # lookup to one statement and the window predicates are pinned byte-identical by
        # the round-10/11 plan tests. ``INTERRUPT_REASON_SQL``'s ``CASE json_valid``
        # guard keeps ONE bad blob from failing the whole statement (HFR-072) — it was
        # never a claim that the row is classifiable, and a NULL extract passes
        # ``reason IS NULL``, so without this element a malformed row was counted as an
        # ordinary verdict and a definition whose history could not be read got a
        # confident ``healthy`` or ``failing``. ``HEALTH_UNKNOWN``'s own docstring
        # promises unknown for exactly this row.
        blob = select(
            func.json_group_array(
                func.json_array(recent.c.settled, recent.c.id, recent.c.status, recent.c.readable)
            )
        ).scalar_subquery()
        statement = select(id_list.c.value.label("definition_id"), blob.label("verdicts"))

        verdicts_by_definition: dict[str, list[Optional[str]]] = {}

        def _collect(active: Any) -> None:
            for row in active.execute(statement):
                entries = _json_loads(row[1], [])
                if not isinstance(entries, list) or not entries:
                    # A definition with no verdicts is ABSENT from the mapping rather
                    # than mapped to an empty list, exactly as the batched form was:
                    # ``_classify_health`` already defaults a missing id to no
                    # verdicts, and inventing a key here would change what
                    # ``definition_health_batch`` reports for a never-run definition.
                    continue
                rows = [entry for entry in entries if isinstance(entry, list) and len(entry) == 4]
                rows.sort(key=lambda entry: (str(entry[0] or ""), str(entry[1] or "")), reverse=True)
                verdicts_by_definition[str(row[0])] = [
                    # Raw column values, so the legacy spellings ``_status_query_values``
                    # deliberately matched have to be normalized here — the batched form
                    # did the same one line down. An unreadable row carries ``None``
                    # instead of a verdict it has no standing to report.
                    normalize_run_status(entry[2]) if entry[3] else None
                    for entry in rows
                ]

        if conn is not None:
            _collect(conn)
        else:
            with self.engine.connect() as owned:
                _collect(owned)
        return verdicts_by_definition

    @staticmethod
    def _classify_health(verdicts: list[Optional[str]]) -> dict[str, Any]:
        """Health from one definition's verdicts, newest first.

        ``failing`` when the latest verdict failed, ``degraded`` when the latest
        succeeded but a failure is still inside the window, ``healthy`` otherwise.
        Deliberately weaker than acknowledgment: it answers "has this been unhealthy
        recently", not "has a human seen it". A single success downgrades ``failing``
        to ``degraded`` rather than erasing it, which is the P6 bug, and the window
        ages out on its own so nothing has to be dismissed.

        A ``None`` verdict is an UNREADABLE row (``_health_rows``), and ONE of them
        anywhere in the window degrades the whole definition to ``HEALTH_UNKNOWN``
        rather than being skipped. Skipping is not the conservative choice it looks
        like: both answers this function can give are claims over the WHOLE window —
        ``failing`` reads the newest verdict and ``healthy`` asserts the absence of a
        failure across all of them — so a window with a hole in it cannot support
        either. The counters go out as ``(0, 0)``, the same shape
        ``definition_health_batch`` reports when the read itself fails, so a caller
        rendering "N consecutive failures" beside an unknown badge cannot print a
        number that was never computed.
        """

        if any(status is None for status in verdicts):
            return {
                "health": HEALTH_UNKNOWN,
                "consecutive_failures": 0,
                "recent_failures": 0,
            }

        consecutive = 0
        for status in verdicts:
            if status != "failed":
                break
            consecutive += 1
        recent = sum(1 for status in verdicts if status == "failed")
        if consecutive:
            health = HEALTH_FAILING
        elif recent:
            health = HEALTH_DEGRADED
        else:
            health = HEALTH_HEALTHY
        return {
            "health": health,
            "consecutive_failures": consecutive,
            "recent_failures": recent,
        }

    def definition_health_batch(
        self,
        definition_ids: Sequence[str],
        *,
        now: Optional[str] = None,
        conn: Any = None,
    ) -> dict[str, dict[str, Any]]:
        """Derived health for many definitions in one indexed query.

        A read failure degrades to ``unknown`` for every definition rather than
        propagating: the health badge must not be the only thing standing between a
        bad row and an empty Harness list, and ``last_error`` renders independently
        of it.
        """

        ids = [str(value or "").strip() for value in definition_ids]
        ids = [value for value in dict.fromkeys(ids) if value]
        if not ids:
            return {}
        try:
            verdicts_by_definition = self._health_rows(ids, now=now, conn=conn)
        except Exception:
            logger.warning("definition health lookup failed; reporting unknown", exc_info=True)
            return {
                definition_id: {
                    "health": HEALTH_UNKNOWN,
                    "consecutive_failures": 0,
                    "recent_failures": 0,
                }
                for definition_id in ids
            }
        return {
            definition_id: self._classify_health(verdicts_by_definition.get(definition_id, []))
            for definition_id in ids
        }

    def definition_health(
        self,
        definition_id: str,
        *,
        now: Optional[str] = None,
    ) -> dict[str, Any]:
        """Derived health for one definition."""

        batch = self.definition_health_batch([definition_id], now=now)
        return batch.get(
            str(definition_id or "").strip(),
            {"health": HEALTH_HEALTHY, "consecutive_failures": 0, "recent_failures": 0},
        )

    def write_watch_runtime(self, payload: dict[str, Any], *, updated_at: str) -> None:
        watches = payload.get("watches", {}) if isinstance(payload, dict) else {}
        with self.engine.begin() as conn:
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.run_type == "watch_runtime")
                .where(agent_runs.c.status.in_(_status_query_values("running") + _status_query_values("queued")))
                .values(status="succeeded", completed_at=updated_at, updated_at=updated_at)
            )
            for watch_id, runtime_payload in watches.items():
                if not isinstance(runtime_payload, dict):
                    continue
                run_id = f"runtime:{watch_id}"
                values = self._run_values(
                    {
                        "id": run_id,
                        "request_type": "watch_runtime",
                        "status": "running" if runtime_payload.get("running") else "completed",
                        "definition_id": watch_id,
                        "pid": runtime_payload.get("pid"),
                        "created_at": runtime_payload.get("started_at") or updated_at,
                        "started_at": runtime_payload.get("started_at"),
                        "updated_at": runtime_payload.get("updated_at") or updated_at,
                        "metadata": runtime_payload,
                    }
                )
                existing = conn.execute(
                    select(agent_runs.c.id).where(agent_runs.c.id == run_id).limit(1)
                ).scalar_one_or_none()
                if existing:
                    conn.execute(update(agent_runs).where(agent_runs.c.id == run_id).values(**values))
                else:
                    conn.execute(insert(agent_runs).values(**values))

    def load_watch_runtime(self) -> dict[str, Any]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(agent_runs)
                .where(agent_runs.c.run_type == "watch_runtime")
                .where(agent_runs.c.status == "running")
            ).mappings()
            watches: dict[str, Any] = {}
            for row in rows:
                payload = _json_loads(row["metadata_json"], {})
                watch_id = row["definition_id"]
                if watch_id:
                    watches[str(watch_id)] = {
                        "running": True,
                        "pid": row["pid"],
                        "started_at": row["started_at"],
                        "updated_at": row["updated_at"],
                    } | (payload if isinstance(payload, dict) else {})
            return {"watches": watches}

    def _scheduled_task_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": payload["id"],
            "definition_type": "scheduled",
            "name": payload.get("name"),
            "agent_name": payload.get("agent_name"),
            "session_policy": payload.get("session_policy") or ("existing" if payload.get("session_id") or payload.get("session_key") else None),
            "session_id": payload.get("session_id"),
            "legacy_session_key": payload.get("session_key") or None,
            "prompt": payload.get("prompt") or payload.get("message") or "",
            "message": payload.get("message") or payload.get("prompt") or "",
            "message_payload_json": self._message_payload_json(payload),
            "schedule_type": payload.get("schedule_type") or "",
            "cron": payload.get("cron"),
            "run_at": payload.get("run_at"),
            "timezone": payload.get("timezone") or "UTC",
            "command_json": None,
            "shell_command": None,
            "prefix": None,
            "cwd": payload.get("cwd"),
            "mode": None,
            "timeout_seconds": None,
            "lifetime_timeout_seconds": None,
            "retry_exit_codes_json": None,
            "retry_delay_seconds": None,
            "post_to": payload.get("post_to"),
            "deliver_key": payload.get("deliver_key"),
            "enabled": 1 if payload.get("enabled", True) else 0,
            "deleted_at": payload.get("deleted_at"),
            "created_at": payload.get("created_at") or payload.get("updated_at"),
            "updated_at": payload.get("updated_at") or payload.get("created_at"),
            "last_started_at": None,
            "last_finished_at": None,
            "retired_at": None,
            "last_event_at": None,
            "last_run_at": payload.get("last_run_at"),
            "last_run_id": payload.get("last_run_id"),
            "last_error": payload.get("last_error"),
            "last_exit_code": None,
            "metadata_json": _json_dumps(payload.get("metadata") or {}),
        }

    def _watch_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": payload["id"],
            "definition_type": "watch",
            "name": payload.get("name"),
            "agent_name": payload.get("agent_name"),
            "session_policy": payload.get("session_policy") or ("existing" if payload.get("session_id") or payload.get("session_key") else None),
            "session_id": payload.get("session_id"),
            "legacy_session_key": payload.get("session_key") or None,
            "prompt": None,
            "message": payload.get("message") or payload.get("prefix"),
            "message_payload_json": self._message_payload_json(payload),
            "schedule_type": None,
            "cron": None,
            "run_at": None,
            "timezone": None,
            "command_json": _json_dumps(payload.get("command") or []),
            "shell_command": payload.get("shell_command"),
            "prefix": payload.get("prefix"),
            "cwd": payload.get("cwd"),
            "mode": payload.get("mode") or "once",
            "timeout_seconds": float(payload.get("timeout_seconds", 21600.0)),
            "lifetime_timeout_seconds": float(payload.get("lifetime_timeout_seconds", 0.0)),
            "retry_exit_codes_json": _json_dumps(payload.get("retry_exit_codes") or []),
            "retry_delay_seconds": float(payload.get("retry_delay_seconds", 30.0)),
            "post_to": payload.get("post_to"),
            "deliver_key": payload.get("deliver_key"),
            "enabled": 1 if payload.get("enabled", True) else 0,
            "deleted_at": payload.get("deleted_at"),
            "created_at": payload.get("created_at") or payload.get("updated_at"),
            "updated_at": payload.get("updated_at") or payload.get("created_at"),
            "last_started_at": payload.get("last_started_at"),
            "last_finished_at": payload.get("last_finished_at"),
            "retired_at": payload.get("retired_at"),
            "last_event_at": payload.get("last_event_at"),
            "last_run_at": None,
            "last_run_id": None,
            "last_error": payload.get("last_error"),
            "last_exit_code": payload.get("last_exit_code"),
            "metadata_json": _json_dumps(payload.get("metadata") or {}),
        }

    def _run_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        created_at = payload.get("created_at") or payload.get("updated_at")
        message = payload.get("message") or payload.get("prompt")
        return {
            "id": payload["id"],
            "definition_id": payload.get("definition_id") or payload.get("task_id"),
            "run_type": payload.get("request_type") or payload.get("run_type") or "hook_send",
            "status": normalize_run_status(payload.get("status")),
            "source_kind": payload.get("source_kind"),
            "source_actor": payload.get("source_actor"),
            "parent_run_id": payload.get("parent_run_id"),
            "agent_name": payload.get("agent_name"),
            "agent_id": payload.get("agent_id"),
            "agent_backend": payload.get("agent_backend"),
            "model": payload.get("model") or payload.get("agent_model"),
            "reasoning_effort": payload.get("reasoning_effort") or payload.get("agent_reasoning_effort"),
            "session_policy": payload.get("session_policy"),
            "session_id": payload.get("session_id"),
            "legacy_session_key": payload.get("session_key") or payload.get("legacy_session_key"),
            "post_to": payload.get("post_to"),
            "deliver_key": payload.get("deliver_key"),
            "prompt": payload.get("prompt") or message,
            "message": message,
            "message_payload_json": self._message_payload_json(payload),
            "result_text": payload.get("result_text"),
            "result_payload_json": self._payload_json(payload, "result_payload", "result_payload_json"),
            "message_ids_json": self._payload_json(payload, "message_ids", "message_ids_json"),
            "callback_session_id": payload.get("callback_session_id"),
            "callback_status": payload.get("callback_status"),
            "callback_error": payload.get("callback_error"),
            "callback_run_id": payload.get("callback_run_id"),
            "callback_completed_at": payload.get("callback_completed_at"),
            "cancel_requested": 1 if payload.get("cancel_requested") else 0,
            "cancel_requested_at": payload.get("cancel_requested_at"),
            "pid": payload.get("pid"),
            "exit_code": payload.get("exit_code"),
            "error": payload.get("error"),
            "stdout": payload.get("stdout"),
            "stderr": payload.get("stderr"),
            "created_at": created_at,
            "started_at": payload.get("started_at"),
            "completed_at": payload.get("completed_at"),
            "updated_at": payload.get("updated_at") or created_at,
            "metadata_json": _json_dumps(payload.get("metadata") or {}),
        }

    @staticmethod
    def _scheduled_task_from_row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "agent_name": row["agent_name"],
            "session_policy": row["session_policy"],
            "session_key": row["legacy_session_key"] or "",
            "session_id": row["session_id"],
            "prompt": row["prompt"] or "",
            "message": row["message"] or row["prompt"] or "",
            "message_payload": _json_loads(row["message_payload_json"], None),
            "schedule_type": row["schedule_type"] or "",
            "post_to": row["post_to"],
            "deliver_key": row["deliver_key"],
            "cwd": row["cwd"],
            "cron": row["cron"],
            "run_at": row["run_at"],
            "timezone": row["timezone"] or "UTC",
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_run_at": row["last_run_at"],
            "last_run_id": row["last_run_id"],
            "last_error": row["last_error"],
            "metadata": _json_loads(row["metadata_json"], {}),
            "lifecycle_state": _row_lifecycle_state(row),
        }

    @staticmethod
    def _watch_from_row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "agent_name": row["agent_name"],
            "session_policy": row["session_policy"],
            "session_key": row["legacy_session_key"] or "",
            "session_id": row["session_id"],
            "command": _json_loads(row["command_json"], []),
            "shell_command": row["shell_command"],
            "prefix": row["prefix"],
            "message": row["message"] or row["prefix"],
            "message_payload": _json_loads(row["message_payload_json"], None),
            "cwd": row["cwd"],
            "mode": row["mode"] or "once",
            "timeout_seconds": float(row["timeout_seconds"] or 21600.0),
            "lifetime_timeout_seconds": float(row["lifetime_timeout_seconds"] or 0.0),
            "retry_exit_codes": [int(code) for code in _json_loads(row["retry_exit_codes_json"], [])],
            "retry_delay_seconds": float(row["retry_delay_seconds"] or 30.0),
            "post_to": row["post_to"],
            "deliver_key": row["deliver_key"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_started_at": row["last_started_at"],
            "last_finished_at": row["last_finished_at"],
            "retired_at": row["retired_at"],
            "last_event_at": row["last_event_at"],
            "last_error": row["last_error"],
            "last_exit_code": row["last_exit_code"],
            "metadata": _json_loads(row["metadata_json"], {}),
            "lifecycle_state": _row_lifecycle_state(row),
        }

    @staticmethod
    def _run_from_row(row: Any) -> dict[str, Any]:
        metadata = _json_loads(row["metadata_json"], {})
        return {
            "id": row["id"],
            "request_type": row["run_type"],
            "run_type": row["run_type"],
            "status": normalize_run_status(row["status"]),
            "definition_id": row["definition_id"],
            "task_id": row["definition_id"],
            "source_kind": row["source_kind"],
            "source_actor": row["source_actor"],
            # ``source_actor`` is polymorphic: a *session id* when another agent
            # spawned this run, but a parent run id, a "vault:<request>" handle
            # or an activity id for every other ``source_kind``. Narrowing it to
            # the one case that names a session — the same guard the agent graph
            # applies when it draws spawn edges — is what lets the projection
            # below resolve it without trying to look up "vault:abc" as a
            # session and reporting it deleted.
            "source_session_id": row["source_actor"] if row["source_kind"] == "agent" else None,
            "parent_run_id": row["parent_run_id"],
            "agent_name": row["agent_name"],
            "agent_id": row["agent_id"],
            "agent_backend": row["agent_backend"],
            "model": row["model"],
            "reasoning_effort": row["reasoning_effort"],
            "session_policy": row["session_policy"],
            "session_key": row["legacy_session_key"],
            "session_id": row["session_id"],
            "post_to": row["post_to"],
            "deliver_key": row["deliver_key"],
            "prompt": row["prompt"],
            "message": row["message"] or row["prompt"],
            "message_payload": _json_loads(row["message_payload_json"], None),
            "result_text": row["result_text"],
            "result_payload": _json_loads(row["result_payload_json"], None),
            "message_ids": _json_loads(row["message_ids_json"], []),
            "callback_session_id": row["callback_session_id"],
            "callback_status": row["callback_status"],
            "callback_error": row["callback_error"],
            "callback_run_id": row["callback_run_id"],
            "callback_completed_at": row["callback_completed_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "cancel_requested_at": row["cancel_requested_at"],
            "pid": row["pid"],
            "exit_code": row["exit_code"],
            "error": row["error"],
            "stdout": row["stdout"],
            "stderr": row["stderr"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "updated_at": row["updated_at"],
            "metadata": metadata,
            "session_fork": metadata.get("session_fork") if isinstance(metadata, dict) else None,
            "ok": None if row["completed_at"] is None else normalize_run_status(row["status"]) == "succeeded",
        }

    def _enrich_definitions(
        self, rows: list[dict[str, Any]], conn: Any, *, definition_type: str
    ) -> list[dict[str, Any]]:
        """Project a page of task/watch rows into the fields the Harness UI reads.

        The single chokepoint every list and get path goes through, so a field
        cannot exist on one surface and be missing on another. Batched like
        ``_enrich_runs``: a fixed number of round trips whatever the page size.
        The per-row version this replaces was called from six sites and resolved
        one row per query, which a 30-row page paid thirty times over — and the
        unpaged list once per row in the whole store.

        Beyond the stored columns it adds what the row actually shows: how a
        finished row ended, when a waiting task fires next, since when it has
        been waiting, and — for watches — whether the waiter process is still
        alive. The last one is a fact about the *process*, deliberately not a
        state: a waiter that died leaves a row that is still armed, and the
        difference between those two is the whole point of showing it.
        """

        if not rows:
            return rows
        # One guard per lookup, not one around all of them. These are
        # independent questions — who owns this row, where it is delivered, and
        # whether its waiter is alive — and a single ``try`` made the first
        # failure blank out the other two. That is how a lookup problem could
        # silently take ``process_alive`` with it and leave every watch row
        # saying "liveness unknown" for a reason nothing on screen named.
        #
        # ``warning``, not ``debug``: degrading to blanks is a visible loss of
        # information, so it belongs in a log the operator actually reads.
        def _lookup(what: str, fetch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
            try:
                return fetch()
            except Exception:
                logger.warning("harness definition enrichment failed: %s", what, exc_info=True)
                return {}

        summaries = _lookup(
            "session summaries",
            lambda: self._session_summaries(
                conn, {value for row in rows if (value := row.get("session_id"))}
            ),
        )
        # Only rows with no id at all fall back to the legacy key / delivery
        # target, exactly as _session_summary does: a named session that
        # fails to resolve is deleted, not re-labelled as its channel.
        key_summaries = _lookup(
            "session keys",
            lambda: self._key_summaries(
                conn,
                {
                    value
                    for row in rows
                    if not row.get("session_id")
                    for field in _DEFINITION_SESSION_KEY_FIELDS
                    if (value := row.get(field))
                },
            ),
        )
        runtimes: dict[str, dict[str, Any]] = {}
        started: dict[str, str] = {}
        if definition_type == "watch":
            runtimes = _lookup(
                "watch runtimes",
                lambda: self._watch_runtimes(conn, [row.get("id") for row in rows]),
            )
        # Derived health for the whole page in one query. It goes here rather than in
        # a per-surface payload builder because this is the only chokepoint the CLI,
        # the list endpoint and the detail pane all pass through — and the Harness
        # detail pane has no fetch of its own, it re-renders the row the list
        # returned, so a field that is not on the row cannot reach it.
        health = _lookup(
            "definition health",
            lambda: self.definition_health_batch(
                [row.get("id") for row in rows if row.get("id")],
                conn=conn,
            ),
        )
        if any(row.get("lifecycle_state") == "running" for row in rows):
            started = _lookup(
                "in-flight run starts",
                lambda: self._in_flight_started_at(
                    conn,
                    [
                        row.get("id")
                        for row in rows
                        if row.get("lifecycle_state") == "running"
                    ],
                ),
            )
        for row in rows:
            session_id = row.get("session_id")
            row.update(
                self._pick_session_summary(
                    summaries.get(session_id or ""),
                    []
                    if session_id
                    else [key_summaries.get(row.get(field) or "") for field in _DEFINITION_SESSION_KEY_FIELDS],
                )
            )
            row_health = health.get(row.get("id") or "") or {
                "health": HEALTH_UNKNOWN,
                "consecutive_failures": 0,
                "recent_failures": 0,
            }
            row["health"] = row_health["health"]
            row["consecutive_failures"] = row_health["consecutive_failures"]
            row["recent_failures"] = row_health["recent_failures"]
            state = row.get("lifecycle_state")
            row["lifecycle_detail"] = definition_lifecycle_detail(
                lifecycle_state=state,
                definition_type=definition_type,
                last_run_at=row.get("last_run_at"),
                last_exit_code=row.get("last_exit_code"),
                last_error=row.get("last_error"),
            )
            row["next_run_at"] = compute_next_run_at(
                enabled=bool(row.get("enabled")),
                schedule_type=row.get("schedule_type"),
                cron=row.get("cron"),
                run_at=row.get("run_at"),
                timezone_name=row.get("timezone"),
            )
            # Only meaningful while waiting: a paused row's last start is history,
            # not a wait anyone is still in.
            row["waiting_since"] = row.get("last_started_at") if state == "waiting" else None
            # And only meaningful while running — from the run that *is* running,
            # never from ``last_started_at``. That column is the definition's last
            # cycle, which for a watch that fired yesterday and started a fresh run
            # a minute ago would render "running 1d". Null while the run is merely
            # queued: it has not started, so no duration exists to print.
            row["running_since"] = started.get(row.get("id") or "") if state == "running" else None
            if definition_type == "watch":
                runtime = runtimes.get(row.get("id") or "")
                row["runtime"] = runtime or {}
                # ``None``, not ``False``: no heartbeat row at all means we have
                # never seen this waiter, which is not the same as having seen it
                # exit — and the row must not claim a waiter is dead on the
                # strength of never having looked.
                row["process_alive"] = None if runtime is None else bool(runtime.get("running"))
        return rows

    @staticmethod
    def _watch_runtimes(conn: Any, watch_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Each watch's supervisor heartbeat row, for many watches at once.

        ``load_watch_runtime`` cannot serve this: it selects only *running*
        heartbeats, which is what the supervisor wants (live waiters) but
        collapses the two answers a row has to tell apart — a waiter that exited
        and a waiter never seen. Absent from this result means the latter.
        """

        runtimes: dict[str, dict[str, Any]] = {}
        for batch in _id_batches(watch_ids):
            rows = conn.execute(
                select(
                    agent_runs.c.definition_id,
                    agent_runs.c.status,
                    agent_runs.c.pid,
                    agent_runs.c.started_at,
                    agent_runs.c.updated_at,
                    agent_runs.c.metadata_json,
                )
                .where(agent_runs.c.run_type == _WATCH_RUNTIME_RUN_TYPE)
                .where(agent_runs.c.definition_id.in_(batch))
            ).mappings()
            for row in rows:
                payload = _json_loads(row["metadata_json"], {})
                runtime = {
                    "pid": row["pid"],
                    "started_at": row["started_at"],
                    "updated_at": row["updated_at"],
                    **(payload if isinstance(payload, dict) else {}),
                }
                # The heartbeat's metadata was written while the waiter was up and
                # is never rewritten when it exits, so the row's own status — not
                # the stored payload — is what says whether it is still alive.
                runtime["running"] = normalize_run_status(row["status"]) == "running"
                runtimes[row["definition_id"]] = runtime
        return runtimes

    @staticmethod
    def _in_flight_started_at(conn: Any, definition_ids: Iterable[str]) -> dict[str, str]:
        """When each definition's in-flight run started, for many at once.

        Deliberately the *same* set of runs ``definition_lifecycle_expression``
        tests for: same ``run_type`` exclusion, same statuses. A row is
        ``running`` because one of these exists, so the duration it shows has to
        come from one of these too — reading ``run_definitions.last_started_at``
        instead would date the row's previous cycle and print a duration nothing
        is actually spending.

        Missing means the run has not started (a queued run has no
        ``started_at``), which callers must render as no duration rather than as
        a zero-length one.
        """

        started: dict[str, str] = {}
        for batch in _id_batches(definition_ids):
            rows = conn.execute(
                select(agent_runs.c.definition_id, agent_runs.c.started_at)
                .where(agent_runs.c.definition_id.in_(batch))
                .where(
                    or_(
                        agent_runs.c.run_type.is_(None),
                        agent_runs.c.run_type != _WATCH_RUNTIME_RUN_TYPE,
                    )
                )
                .where(
                    agent_runs.c.status.in_(
                        [*_status_query_values("queued"), *_status_query_values("running")]
                    )
                )
                .where(agent_runs.c.started_at.is_not(None))
                # Concurrent runs against one definition are possible; the row
                # asks how long *this* burst of work has been going, so the
                # earliest start is the honest answer. Descending order plus the
                # last-write-wins loop below leaves exactly that one.
                .order_by(agent_runs.c.started_at.desc())
            ).mappings()
            for row in rows:
                started[row["definition_id"]] = row["started_at"]
        return started

    def _enrich_runs(self, runs: list[dict[str, Any]], conn: Any) -> list[dict[str, Any]]:
        """Project a page of raw run rows into the fields the Harness UI reads.

        Runs were the last harness payload rendered raw: the row headline was
        the run id and the bound session was an unresolvable hash. This is the
        single chokepoint that gives them the same resolved session summary
        Tasks/Watches already get (``_session_summary`` semantics, so a
        workbench session stays linkable and an IM session stays labelled),
        plus the originating definition's name.

        Batched by construction — three queries for the whole page regardless
        of its size — because the list endpoint pages 30 rows at a time and a
        per-row resolve would be 60+ round trips. Sites are grouped by source
        rather than resolved one at a time, so adding a site to
        ``_RUN_PROJECTIONS`` costs no extra round trip.
        """
        if not runs:
            return runs
        session_sites = [site for site in _RUN_PROJECTIONS if site.source == "session"]
        definition_sites = [site for site in _RUN_PROJECTIONS if site.source == "definition"]
        try:
            summaries = self._session_summaries(
                conn,
                {
                    value
                    for run in runs
                    for site in session_sites
                    if (value := run.get(site.id_field))
                },
            )
            # Only sites with no id at all fall back to the legacy key /
            # delivery target, exactly as _session_summary does: a named session
            # that fails to resolve is deleted, not re-labelled as its channel.
            keys = {
                value
                for run in runs
                for site in session_sites
                if not run.get(site.id_field)
                for field in site.key_fields
                if (value := run.get(field))
            }
            key_summaries = self._key_summaries(conn, keys)
            definitions = self._definition_summaries(
                conn,
                {
                    value
                    for run in runs
                    for site in definition_sites
                    if (value := run.get(site.id_field))
                },
            )
            for run in runs:
                for site in _RUN_PROJECTIONS:
                    if site.source == "session":
                        session_id = run.get(site.id_field)
                        summary = self._pick_session_summary(
                            summaries.get(session_id or ""),
                            []
                            if session_id
                            else [key_summaries.get(run.get(field) or "") for field in site.key_fields],
                        )
                    else:
                        summary = definitions.get(run.get(site.id_field) or "") or _BLANK_DEFINITION_SUMMARY
                    if site.payload_key is None:
                        run.update(summary)
                    else:
                        # A nested site says "there is nothing here" with None.
                        # The all-null summary means the opposite — the row was
                        # referenced and is gone — and the UI renders that
                        # differently, so the two must not collapse.
                        run[site.payload_key] = summary if run.get(site.id_field) else None
        except Exception:
            # Degrade to the shape the UI expects rather than a KeyError: every
            # site still produces its field, just empty. Derived from the same
            # table, so a new site cannot leave a hole only the error path hits.
            logger.debug("harness run enrichment failed", exc_info=True)
            blanks = {"session": self._blank_session_summary, "definition": lambda: _BLANK_DEFINITION_SUMMARY}
            for run in runs:
                for site in _RUN_PROJECTIONS:
                    if site.payload_key is not None:
                        run.setdefault(site.payload_key, None)
                        continue
                    for field, blank in blanks[site.source]().items():
                        run.setdefault(field, blank)
        return runs

    @staticmethod
    def _blank_session_summary() -> dict[str, Any]:
        """The all-null summary: no session resolved. A run whose ``session_id``
        is set but lands here names a session row that no longer exists, and the
        UI says so instead of printing the bare id."""
        return {
            "session_title": None,
            "session_platform": None,
            "session_scope_kind": None,
            "session_label": None,
            "session_is_workbench": False,
            "session_openable": False,
        }

    @staticmethod
    def _pick_session_summary(
        by_id: Optional[dict[str, Any]], by_key: list[Optional[dict[str, Any]]]
    ) -> dict[str, Any]:
        """``_session_summary``'s precedence, applied to pre-resolved lookups."""
        for candidate in (by_id, *by_key):
            if candidate:
                return dict(candidate)
        return SQLiteBackgroundTaskStore._blank_session_summary()

    @staticmethod
    def _session_summary(
        conn: Any,
        session_id: Optional[str],
        session_key: Optional[str],
        deliver_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Resolve a task/watch's bound session into UI-facing display fields.

        A workbench binding (avibe ``project`` scope) carries a concrete
        ``session_id`` and a human ``title`` and is linkable to its chat. A
        legacy IM binding lives in ``session_key``. A ``create_per_run``
        definition has neither — it mints a fresh session each run and stores
        only its target scope in ``deliver_key`` — so that is used as a final
        fallback for the platform + channel label. Key-based targets are never
        linkable (no concrete session to open). Best-effort: never raises into
        the harness list.

        The key fallback applies only when there is no ``session_id`` at all. A
        row that names a session is describing *that* session, and an id that no
        longer resolves means it was deleted — one of the four states the UI
        renders (plan §4.2). Falling through to the delivery key would relabel a
        vanished session as a live IM channel, which matters because a
        ``create_per_run`` execution stores both: its own fresh ``session_id``
        and the definition's ``deliver_key``.
        """
        try:
            if session_id:
                row = conn.execute(
                    SQLiteBackgroundTaskStore._session_summary_query().where(
                        agent_sessions.c.id == session_id
                    ).limit(1)
                ).mappings().first()
                if row is not None:
                    return SQLiteBackgroundTaskStore._summary_from_session_row(row)
            else:
                for key in (session_key, deliver_key):
                    resolved = SQLiteBackgroundTaskStore._summary_from_session_key(conn, key)
                    if resolved is not None:
                        return resolved
        except Exception:
            logger.debug("harness session summary resolution failed", exc_info=True)
        return SQLiteBackgroundTaskStore._blank_session_summary()

    @staticmethod
    def _session_scope_join():
        """A session with its scope, outer-joined — a workbench session may have
        no scope row at all. Shared so the summary projection and the search
        predicate that has to find those same labels cannot drift apart."""
        return agent_sessions.join(scopes, scopes.c.id == agent_sessions.c.scope_id, isouter=True)

    @staticmethod
    def _session_summary_query():
        return select(
            agent_sessions.c.id,
            agent_sessions.c.scope_id,
            agent_sessions.c.title,
            scopes.c.platform,
            scopes.c.scope_type,
            scopes.c.native_id,
            scopes.c.display_name,
            scopes.c.native_type,
        ).select_from(SQLiteBackgroundTaskStore._session_scope_join())

    @staticmethod
    def _summary_from_session_row(row: Any) -> dict[str, Any]:
        platform = (row["platform"] or "").strip()
        scope_type = (row["scope_type"] or "").strip()
        # A presentation fact only — which icon and which label to render. It is
        # deliberately *not* the link rule any more: see ``session_openable``.
        is_workbench = row["scope_id"] is None or platform == "avibe" or scope_type == "project"
        return {
            "session_title": row["title"],
            "session_platform": platform or None,
            "session_scope_kind": scope_type or None,
            "session_label": row["title"] if is_workbench else (row["display_name"] or row["native_id"]),
            "session_is_workbench": is_workbench,
            "session_openable": session_openable_in_chat(
                session_id=row["id"], scope_native_type=row["native_type"]
            ),
        }

    @staticmethod
    def _session_summaries(conn: Any, session_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """``_session_summary``'s id branch for many ids at once. Ids with no
        surviving session row are simply absent from the result."""
        summaries: dict[str, dict[str, Any]] = {}
        for batch in _id_batches(session_ids):
            rows = conn.execute(
                SQLiteBackgroundTaskStore._session_summary_query().where(agent_sessions.c.id.in_(batch))
            ).mappings()
            for row in rows:
                summaries[row["id"]] = SQLiteBackgroundTaskStore._summary_from_session_row(row)
        return summaries

    @staticmethod
    def _summary_from_session_key(conn: Any, key: Optional[str]) -> Optional[dict[str, Any]]:
        """Parse a "<platform>::<channel|user>::<native_id>[::thread::<id>]" key
        into a non-linkable session summary, resolving the channel display name.
        Shared by the legacy ``session_key`` and the ``create_per_run``
        ``deliver_key`` paths. Returns None when ``key`` is empty/malformed."""
        parts = SQLiteBackgroundTaskStore._parse_session_key(key)
        if parts is None:
            return None
        platform, scope_type, native_id = parts
        drow = conn.execute(
            select(scopes.c.display_name)
            .where(scopes.c.platform == platform)
            .where(scopes.c.scope_type == scope_type)
            .where(scopes.c.native_id == native_id)
            .limit(1)
        ).mappings().first()
        display_name = drow["display_name"] if drow is not None else None
        return SQLiteBackgroundTaskStore._summary_from_key_parts(parts, display_name)

    @staticmethod
    def _key_summaries(conn: Any, keys: Iterable[str]) -> dict[str, dict[str, Any]]:
        """``_summary_from_session_key`` for many keys in one query."""
        parsed = {key: SQLiteBackgroundTaskStore._parse_session_key(key) for key in dict.fromkeys(keys)}
        triples = {parts for parts in parsed.values() if parts is not None}
        if not triples:
            return {}
        display_names: dict[tuple[str, str, str], Any] = {}
        # Three bound parameters per triple, so the batches are a third the size
        # of an id resolver's. An unpaged harness list on a store with a few
        # hundred legacy-keyed rows is exactly the case that overflows.
        for batch in _id_batches(triples, params_per_value=3):
            rows = conn.execute(
                select(scopes.c.platform, scopes.c.scope_type, scopes.c.native_id, scopes.c.display_name).where(
                    or_(
                        *(
                            and_(
                                scopes.c.platform == platform,
                                scopes.c.scope_type == scope_type,
                                scopes.c.native_id == native_id,
                            )
                            for platform, scope_type, native_id in batch
                        )
                    )
                )
            ).mappings()
            for row in rows:
                display_names[(row["platform"], row["scope_type"], row["native_id"])] = row["display_name"]
        return {
            key: SQLiteBackgroundTaskStore._summary_from_key_parts(parts, display_names.get(parts))
            for key, parts in parsed.items()
            if parts is not None
        }

    @staticmethod
    def _parse_session_key(key: Optional[str]) -> Optional[tuple[str, str, str]]:
        if not key:
            return None
        parts = key.split("::")
        if len(parts) < 3 or not parts[0] or not parts[2]:
            return None
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _summary_from_key_parts(
        parts: tuple[str, str, str], display_name: Optional[str]
    ) -> dict[str, Any]:
        platform, scope_type, native_id = parts
        return {
            "session_title": None,
            "session_platform": platform,
            "session_scope_kind": scope_type,
            "session_label": display_name or native_id,
            "session_is_workbench": False,
            # A delivery key names a channel, not a session; there is no id to
            # open even though the label reads like one.
            "session_openable": False,
        }

    @staticmethod
    def _definition_summaries(conn: Any, definition_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Name the task/watch a run came from, in one query.

        Soft-deleted definitions are included on purpose: a run outlives the
        definition that produced it, and "from 夜间巡检 (deleted)" is more
        useful than an orphan id. ``definition_deleted`` lets the UI drop the
        link instead of pointing at a row that is gone.
        """
        ids = [value for value in dict.fromkeys(definition_ids) if value]
        if not ids:
            return {}
        rows = conn.execute(
            select(
                run_definitions.c.id,
                run_definitions.c.name,
                run_definitions.c.definition_type,
                run_definitions.c.deleted_at,
            ).where(run_definitions.c.id.in_(ids))
        ).mappings()
        return {
            row["id"]: {
                "definition_name": row["name"],
                "definition_kind": _DEFINITION_KINDS.get(row["definition_type"]),
                "definition_deleted": row["deleted_at"] is not None,
            }
            for row in rows
        }

    @staticmethod
    def _message_payload_json(payload: dict[str, Any]) -> Optional[str]:
        return SQLiteBackgroundTaskStore._payload_json(payload, "message_payload", "message_payload_json")

    @staticmethod
    def _payload_json(payload: dict[str, Any], object_key: str, json_key: str) -> Optional[str]:
        if payload.get(json_key) is not None:
            return payload.get(json_key)
        if payload.get(object_key) is not None:
            return _json_dumps(payload.get(object_key))
        return None
