"""Bounded, credential-free persistence for Model Hub token usage.

Only the code that sees a complete upstream model response can count tokens, and
one turn can make several upstream calls. Two callers therefore report calls here,
over populations that ``InvokeHandle.stream is not None`` keeps disjoint: the
resolver reports every call it consumed itself, including a failover hop that
billed us before the turn moved on, and the turn gateway reports every call whose
body it forwarded. This module owns what happens to those counts afterwards: one
bounded daily aggregate per source and model, and the read shape the settings page
consumes.

Two properties are deliberate. `requests` is self-measured by our own code and is
always available; token counts are vendor-reported and may be absent, which is
why `token_reports` is tracked separately instead of treating a missing report as
zero usage. And nothing here ever feeds admission, routing, or cooldown — a
hostile upstream must not be able to change resolution behavior by lying about
usage.

What "bounded" bounds is the file: a fixed number of daily rows over a fixed
retention window, each carrying counters the settings page can still read back
exactly. It was never a bound on the counts themselves — how much a user spends
is not ours to cap — so every aggregate here is an exact sum, and
`USAGE_COUNTER_CEILING` constrains only what crosses into the file and back out
of it. A row carrying a counter past it is dropped at whichever door it reaches,
loudly, rather than rewritten into a number nobody spent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from concurrent.futures import Executor
from concurrent.futures import Future as CFuture
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Final, Mapping, Optional, Sequence

from .identifiers import persisted_ledger_key, usage_ledger_key
from .state_file import write_state_document
from .stream_wire import ProtocolUsageReport

logger = logging.getLogger(__name__)

# Roughly two months of daily rows: long enough for a monthly view plus the
# previous cycle, short enough that the file stays small on a busy machine.
USAGE_RETENTION_DAYS: Final = 62
USAGE_MAX_ROWS: Final = 400
USAGE_DEFAULT_WINDOW_DAYS: Final = 30
USAGE_HOURLY_RETENTION_HOURS: Final = 24
USAGE_WINDOW_KEYS: Final = ("24h", "7d", "30d", "60d")
# The largest integer the settings page holds exactly. Nothing on this side of the
# wire needs it: `json.loads` reads a counter of any size into an exact `int`, and
# so do the rpc and client hops. The doubles appear one boundary later, in the
# browser parsing the published summary, and this is the only number in the module
# that comes from there — which is worth saying plainly, because calling it a
# property of the file is what made it look reusable as a bound on a sum.
#
# It is applied at the file's doors because that is where a value can still be
# refused. A published aggregate is a sum, and refusing a sum is the defect this
# module was fixed for; admitting a row only up to this instead bounds every
# published count at `USAGE_MAX_ROWS` times it, a range the consumer holds as a
# finite double and the contract can therefore declare and satisfy — with no
# ceiling anywhere on how much a user may spend.
#
# Deliberately not the per-report ceiling, and nothing between the two doors
# clamps to it. Nothing this module writes can approach it either: a window spans
# at most `USAGE_RETENTION_DAYS` days of calls each bounded by
# `stream_wire.USAGE_REPORT_TOKEN_CEILING`, so what it actually guards is a corrupt
# or hand-edited file. There it drops the row rather than saturating it: a
# saturated counter is a number nobody spent, and saturating a subset with its
# superset forces a cached-input share to exactly 100% — the artifact this module
# exists to not produce.
USAGE_COUNTER_CEILING: Final = 2**53 - 1
# The largest count `summary` can publish, and the maximum
# `usage-summary.schema.json` declares. Derived rather than imposed: a published
# count is a sum over one window, `window` reports at most `USAGE_MAX_ROWS` rows,
# and no row reaches it carrying a counter past `USAGE_COUNTER_CEILING` — not from
# the file, where `_counter` clears each one, and not from a merge, which `_read`
# re-checks because a sum of cleared addends is not itself cleared.
#
# Both capacities binding the reported set is what makes the product a bound on
# what this module can publish, rather than a description of the files it happens
# to write. Deriving it from the write path alone was true of every file this
# ledger produces and false of every other one — a reader handed a larger file
# published a total above its own contract.
#
# Saying it out loud is what lets the contract carry a maximum that is both true
# and satisfiable; the two things tried before were a maximum the producer could
# violate, and none at all, which left a consumer nothing to size against and said
# nothing about where exactness ends. Never a clamp: it states the range, and
# nothing compares a sum against it. Every value in the range is a finite double,
# only the part above `USAGE_COUNTER_CEILING` rounds, and reaching that at all
# takes a corrupt file — a window spans at most `USAGE_RETENTION_DAYS` days of
# real calls.
USAGE_PUBLISHED_COUNT_BOUND: Final = USAGE_MAX_ROWS * USAGE_COUNTER_CEILING
# Anything older than every instant this ledger can hold, so a row whose recency
# cannot be read — it recorded no instant, or recorded one that has not happened —
# sorts as the least recently metered.
_OLDEST_INSTANT: Final = datetime.min.replace(tzinfo=timezone.utc)

_COUNTER_KEYS: Final = (
    "requests",
    "token_reports",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
)
# The cross-field guarantees the read contract makes, as (subset, superset).
# Each one is repaired on read, so a corrupt or hand-edited file degrades into a
# smaller true statement instead of publishing an impossible one: a coverage
# figure above 100% is more misleading than a conservative one.
_COUNTER_SUBSETS: Final = (
    ("cached_input_tokens", "input_tokens"),
    ("token_reports", "requests"),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _carried(moment: datetime) -> Optional[datetime]:
    """Carry one moment through every conversion this module performs, or refuse it.

    `datetime` conversion is not total. A value near either end of the representable
    range, offset far enough, leaves that range on the way to another zone and raises
    `OverflowError` — which is an `ArithmeticError`, so it passes straight through a
    handler written for bad data and takes the flush task with it, stopping metering
    for the rest of the process while the row that caused it stays on disk.

    Bounding the accepted years would leave a free parameter for the next value to
    probe. The conversion itself is the bound instead: what this module can carry is
    what its own conversions return, measured rather than declared. Both are
    performed here — UTC for the spelling it publishes, local for the day it buckets
    — so nothing that escapes this door can raise at either of them later.
    """

    try:
        carried = moment.astimezone(timezone.utc)
        carried.astimezone()
    except (OverflowError, OSError, ValueError):
        return None
    return carried


def _aware(moment: datetime) -> datetime:
    """Read one caller-supplied moment, taking a naive one as local time.

    The same rule `_instant` applies to persisted text, so a naive value means the
    same thing however it reached this module.

    Total, unlike the persisted-read door: a caller is reporting a call an upstream
    already billed, and a moment this module cannot carry is a reason to date the row
    by the only instant it can measure, never a reason to lose the row.
    """

    return _carried(moment) or _utc_now()


def local_usage_day(moment: datetime) -> date:
    """Bucket one moment into a local-calendar day.

    Avibe is local-first and the settings page already presents local days, so a
    day boundary here is the user's midnight, not UTC's.
    """

    return _aware(moment).astimezone().date()


def _counter(value: object) -> Optional[int]:
    """Read one persisted counter, or None when its whole row is unreadable.

    Two unusable shapes, two answers, because they cost different things. A value
    that is not a count — not an integer, a bool, negative — carries no magnitude,
    and zero is the one substitute that claims none either: it is the identity of
    every sum this module performs, so the rest of the row still reports what it
    does know.

    A value above `USAGE_COUNTER_CEILING` is the opposite problem. It carries a
    magnitude no published document could carry to its reader, so every substitute
    invents one in its place, and an
    invented counter that large dominates whatever aggregate it enters; saturating
    it would additionally hand a subset and its superset the same number and force
    a cached-input share to exactly 100%. So that row is not read at all. It is
    dropped and counted with every other unusable row, and the next write is what
    takes it out of the file.

    No reading of a real count is refused here: the ceiling is a property of the
    consumer, and `_write` is what keeps this module from ever persisting a counter
    past it.
    """

    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    if value < 0:
        return 0
    if value > USAGE_COUNTER_CEILING:
        return None
    return value


def _text(value: object) -> Optional[str]:
    """Read one persisted text field, dropping anything that is not text.

    No length bound here: the only text fields this reads are a calendar day and
    an instant, and the parsers below already reject anything that is not one.
    The key fields go through the two key functions instead, and which one depends
    on the direction: a live call is keyed by `usage_ledger_key`, which folds a long
    identity rather than refusing it because the hub already served that call, while
    a row read back from the file goes through `persisted_ledger_key`, which
    recognizes the key a previous write derived instead of deriving it again.
    """

    if not isinstance(value, str):
        return None
    return value.strip() or None


def _calendar_day(value: str) -> Optional[date]:
    """Read one ISO calendar day, or None when the text is not a bare date."""

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _instant(value: object) -> Optional[datetime]:
    """Parse one persisted instant, or None when it is not an ISO date-time.

    A bare date is a day, not an instant, and the date-time parser would read one
    as midnight — inventing a time of day this ledger never wrote. The same parser
    that recognizes the ``day`` key decides that here, so the module holds one
    notion of what a day is.

    A naive value is read as local time, the same calendar the day buckets use.

    Parsing is not the whole of reading it: a value `fromisoformat` accepts may still
    be one no conversion can carry, so it goes through `_carried` before it escapes
    and degrades to absent when it cannot make the trip.
    """

    if not isinstance(value, str) or _calendar_day(value) is not None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _carried(parsed)


def _timestamp(value: object) -> Optional[str]:
    """Publish one persisted instant in the one spelling this module writes.

    The read surface promises an RFC 3339 date-time, and `datetime.fromisoformat`
    accepts far more than RFC 3339 describes — naive, space-separated, and, after
    an offset, even seconds. Publishing the file's text, or re-publishing the
    parsed value in the file's own offset, both leave the output shape decided by
    the input: fix one spelling and the next one is still reachable.

    So the file supplies only the instant, and this module supplies the spelling.
    Normalizing to UTC means the offset is `+00:00` by construction, whatever the
    file said, and there is no remaining spelling for a hand-edited value to
    reach. An unparseable value degrades the field to absent.
    """

    parsed = _instant(_text(value))
    if parsed is None:
        return None
    return parsed.isoformat()


def _local(moment: datetime) -> datetime:
    """Return one carried instant in the server's local timezone."""

    return _aware(moment).astimezone()


def _local_midnight(day: date) -> datetime:
    """Construct a server-local midnight for a calendar date."""

    # Start naive so the OS applies the offset and DST rule for this date,
    # rather than borrowing today's offset for a historical calendar day.
    return datetime.combine(day, time.min).astimezone()


def _overlaps_local_day(start: datetime, end: datetime, day: date) -> bool:
    """Whether an instant interval intersects a local calendar day.

    A UTC hour can straddle local midnight in a fractional-offset zone. Its
    contributions then belong to two daily owners, not just the start's date.
    Unrepresentable persisted dates are invalid evidence, never a read failure.
    """

    try:
        day_start = _local_midnight(day)
        day_end = _local_midnight(day + timedelta(days=1))
        return start < end and start < day_end and end > day_start
    except (OverflowError, OSError, ValueError):
        return False


def _hour_start(moment: datetime) -> datetime:
    """Return the UTC boundary of the actual hour containing ``moment``."""

    return _aware(moment).astimezone(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )


def _hour_key(moment: datetime) -> str:
    """Spell one hourly bucket with its canonical UTC boundary."""

    return _hour_start(moment).isoformat(timespec="seconds")


def _hour_key_parts(value: object, *, day: Optional[date] = None) -> Optional[tuple[str, datetime]]:
    """Parse and normalize a persisted hourly key to its UTC boundary."""

    if not isinstance(value, str) or not value.strip() or _calendar_day(value.strip()) is not None:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    try:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
    except (OverflowError, OSError, ValueError):
        return None
    carried = _carried(parsed)
    if carried is None:
        return None
    if carried.minute or carried.second or carried.microsecond:
        return None
    if day is not None:
        try:
            end = carried + timedelta(hours=1)
        except OverflowError:
            return None
        if not _overlaps_local_day(carried, end, day):
            return None
    return carried.isoformat(timespec="seconds"), carried


def _hour_starts(now: datetime) -> tuple[datetime, ...]:
    """Return the 24 actual consecutive UTC-hour starts ending at ``now``."""

    current = _hour_start(now)
    return tuple(
        current - timedelta(hours=offset)
        for offset in range(USAGE_HOURLY_RETENTION_HOURS - 1, -1, -1)
    )


def _merge_hour_slices(target: dict, incoming: dict) -> None:
    """Merge optional hourly slices without changing the daily aggregate."""

    target_hours = target.get("hours")
    incoming_hours = incoming.get("hours")
    target_complete = target.get("hourly_history_complete") is True
    incoming_complete = incoming.get("hourly_history_complete") is True

    if target_hours is None:
        target["hours"] = None if incoming_hours is None else [dict(item) for item in incoming_hours]
    elif incoming_hours is not None:
        by_key = {item["key"]: item for item in target_hours}
        for item in incoming_hours:
            existing = by_key.get(item["key"])
            if existing is None:
                by_key[item["key"]] = dict(item)
                continue
            _accumulate(existing, item)
            existing["last_metered_at"] = _newer_timestamp(
                existing.get("last_metered_at"),
                item.get("last_metered_at"),
            )
        target["hours"] = sorted(by_key.values(), key=lambda item: item["key"])

    target_expired = target.get("hourly_expired_totals")
    incoming_expired = incoming.get("hourly_expired_totals")
    if target_expired is None or incoming_expired is None:
        target["hourly_expired_totals"] = None
    else:
        expired = dict(target_expired)
        _accumulate(expired, incoming_expired)
        target["hourly_expired_totals"] = expired

    target["hourly_history_complete"] = target_complete and incoming_complete


def _normalize_hour_slice(item: object) -> Optional[dict]:
    """Normalize one nested hourly slice, or drop it as corrupt."""

    if not isinstance(item, dict):
        return None
    # The UTC key is durable evidence. Re-checking it against the row's local
    # calendar day would reinterpret old data after the host timezone changes and
    # discard a slice that still belongs in the 24-hour projection.
    parts = _hour_key_parts(item.get("key"))
    if parts is None:
        return None
    key, _start = parts
    counters: dict[str, int] = {}
    for counter_key in _COUNTER_KEYS:
        value = item.get(counter_key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        if value > USAGE_COUNTER_CEILING:
            return None
        counters[counter_key] = value
    for subset, superset in _COUNTER_SUBSETS:
        if counters[subset] > counters[superset]:
            return None
    return {
        "key": key,
        **counters,
        "last_metered_at": _timestamp(item.get("last_metered_at")),
    }


def _normalize_hourly_totals(value: object) -> Optional[dict]:
    """Normalize counters pruned from the retained hourly history."""

    if not isinstance(value, dict):
        return None
    totals: dict[str, int] = {}
    for key in _COUNTER_KEYS:
        counter = value.get(key)
        if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
            return None
        if counter > USAGE_COUNTER_CEILING:
            return None
        totals[key] = counter
    for subset, superset in _COUNTER_SUBSETS:
        if totals[subset] > totals[superset]:
            return None
    return totals


def _normalize_row(row: object) -> Optional[dict]:
    """Project one persisted row onto the current shape, or drop it.

    Only ever handed keys, never live identities: a caller metering a call derives
    its key first, so this function asks the one question a row can be asked.
    """

    if not isinstance(row, dict):
        return None
    day = _text(row.get("day"))
    source_id = persisted_ledger_key(row.get("source_id"))
    model_id = persisted_ledger_key(row.get("model_id"))
    if day is None or source_id is None or model_id is None:
        return None
    # Same rule as the instant above: the file supplies the day, this module
    # supplies its spelling. `date.fromisoformat` also reads `20260818` and
    # `2026-W34-2`, and the window bounds are compared as `YYYY-MM-DD` text — so
    # a row kept in another valid spelling would be silently outside every
    # window it belongs to, without even counting as dropped.
    calendar_day = _calendar_day(day)
    if calendar_day is None:
        return None
    counters: dict[str, int] = {}
    for key in _COUNTER_KEYS:
        counter = _counter(row.get(key))
        if counter is None:
            return None
        counters[key] = counter
    normalized = {
        "day": calendar_day.isoformat(),
        "source_id": source_id,
        "model_id": model_id,
        **counters,
    }
    for subset, superset in _COUNTER_SUBSETS:
        normalized[subset] = min(normalized[subset], normalized[superset])
    normalized["last_metered_at"] = _timestamp(row.get("last_metered_at"))
    raw_hours = row.get("hours")
    if isinstance(raw_hours, list):
        # An empty list with the producer's complete flag can be ordinary
        # retention expiry: the daily owner outlives its 24-hour slices.
        by_key: dict[str, dict] = {}
        invalid_hour = False
        duplicate_hour = False
        over_capacity = False
        for item in raw_hours:
            normalized_hour = _normalize_hour_slice(item)
            if normalized_hour is None:
                invalid_hour = True
                continue
            key = normalized_hour["key"]
            if key in by_key:
                # A duplicate key is ambiguous persisted evidence. Discard the
                # nested history immediately so an unbounded duplicate list
                # cannot consume memory or be counted repeatedly.
                duplicate_hour = True
                invalid_hour = True
                break
            by_key[key] = normalized_hour
            if len(by_key) > USAGE_HOURLY_RETENTION_HOURS:
                # A corrupt file can contain arbitrarily many unique slices.
                # Once the explicit capacity is exceeded, no temporal subset is
                # authoritative enough to repair, so stop retaining them.
                over_capacity = True
                invalid_hour = True
                break
        if duplicate_hour or over_capacity:
            hours = []
        else:
            hours = list(by_key.values())
        nested_totals = _empty_totals()
        for item in hours:
            _accumulate(nested_totals, item)
        if any(nested_totals[key] > counters[key] for key in _COUNTER_KEYS):
            # The daily row is the authoritative released aggregate. A nested
            # corruption that exceeds it cannot be repaired by clamping one
            # arbitrary hour, so discard all hourly slices and report the
            # temporal history as unavailable.
            hours = []
            invalid_hour = True
        normalized["hours"] = sorted(hours, key=lambda item: item["key"])
        normalized["hourly_history_complete"] = (
            row.get("hourly_history_complete") is True and not invalid_hour
        )
        normalized["hourly_expired_totals"] = _normalize_hourly_totals(
            row.get("hourly_expired_totals")
        )
        if (
            row.get("hourly_expired_totals") is not None
            and normalized["hourly_expired_totals"] is None
        ):
            normalized["hourly_history_complete"] = False
    else:
        # Released files have no hourly field. Their daily totals remain valid,
        # but no hour may be invented from the daily row's last timestamp.
        normalized["hours"] = None
        normalized["hourly_history_complete"] = False
        normalized["hourly_expired_totals"] = None
    return normalized


@dataclass(frozen=True)
class SourceIdentity:
    """One Source as config holds it, for joining onto the rows metered under it.

    Nested rather than two flat maps, because a metered model's identity *is* the
    pair: the same common model ID lives on several Sources, and a flat model map
    cannot say which one it came from — it answered for a model removed from Source A
    as long as Source B still listed it, labelling a retained row as though the
    identity were still there. Arity is the whole of that defect, so it is carried in
    the type rather than in a rule each caller has to remember.

    `label` is the only genuinely caller-owned text here. A model's label is its own
    identity, which the ledger can derive, and which matters only because a folded row
    publishes a key rather than the identifier the user typed.
    """

    source_id: str
    label: Optional[str] = None
    model_ids: Sequence[str] = ()


@dataclass(frozen=True)
class _LedgerRead:
    rows: list[dict]
    degraded: bool


def _keyed_identities(
    identities: Optional[Sequence[SourceIdentity]],
) -> tuple[dict[str, Optional[str]], dict[tuple[str, str], str]]:
    """Key what config holds the way the rows it will join were keyed.

    The read half of the rule `record_many` applies on the write half: an identity a
    caller holds is not the key its rows carry, and a long one differs from it
    entirely. Deriving here rather than in the caller is what keeps a join from
    silently missing exactly the identities the fold exists for — and keying both
    levels here is what keeps a model's join inside its own Source.

    Two identities that key the same are the same identity spelled differently —
    `usage_ledger_key` is injective over anything else — so the later one wins, as it
    would in the mapping a caller built.
    """

    sources: dict[str, Optional[str]] = {}
    models: dict[tuple[str, str], str] = {}
    for identity in identities or ():
        source_key = usage_ledger_key(identity.source_id)
        if source_key is None:
            continue
        sources[source_key] = identity.label
        for model_id in identity.model_ids:
            model_key = usage_ledger_key(model_id)
            if model_key is not None:
                models[(source_key, model_key)] = model_id
    return sources, models


def _recency(row: dict, ceiling: datetime) -> tuple[str, datetime]:
    """Order rows oldest-metered first, so the bound evicts what costs least.

    Ordering by key instead would evict by spelling: an early-sorting model would
    be recreated and evicted again on every write while later-sorting stale rows
    survived, so its usage could never accumulate. Instants are compared as points
    in time — text order is not time order once two rows carry different offsets.

    An instant later than `ceiling` is not a recency at all. Nothing was metered
    after the reading the caller just took, so such a row is not the set's most
    recently used one — it is one whose recency cannot be read, which is the case
    `_OLDEST_INSTANT` already answers for a row that recorded no instant. It gets
    the same answer, and not because a corrupt row deserves to lose: a row this
    ordering cannot place is the only row it can evict without discarding usage it
    can account for.

    Bounding it to `ceiling` instead is not enough, which is worth stating because
    it is the obvious remedy. That makes the row as recent as the reading, so it
    still outranks every row metered before it and still evicts real usage; it
    narrows the lie without changing who pays for it. Placing the bound here at all
    — rather than leaving it to whoever assembled the rows — is what stops this from
    recurring: `_retained` keeps a future instant out of the file, and each time
    that was the only place it happened, the report path ordered by the raw value.

    The day is a different question and is deliberately not answered here. A row
    dated after today reports nothing to anybody, so it does not belong in the set
    at all; ranking it as though it were today's would still let it evict a real
    row. Membership is each caller's own filter — a retention window on the way in,
    the requested window on the way out — and the one caller that had neither is the
    defect this ordering keeps being handed.
    """

    metered = _instant(row["last_metered_at"])
    if metered is None or metered > ceiling:
        return (row["day"], _OLDEST_INSTANT)
    return (row["day"], metered)


def _row_key(row: dict) -> tuple[str, str, str]:
    return (row["day"], row["source_id"], row["model_id"])


def _empty_totals() -> dict:
    return {key: 0 for key in _COUNTER_KEYS}


def _accumulate(target: dict, row: dict) -> None:
    """Add one row's counters into an aggregate, exactly.

    Nothing is clamped here, and that absence is the point. Every addend already
    passed the door that bounds it — a live report at
    `stream_wire.USAGE_REPORT_TOKEN_CEILING`, a persisted one at
    `USAGE_COUNTER_CEILING` — so a ceiling on the sum would no longer protect the
    aggregate from a hostile upstream. It would cap how much usage the user is
    allowed to have had, and it read as exactly that: a truncated total is
    indistinguishable from a real one, and clamping a subset and its superset
    independently drove every cached-input share to exactly 100% once either
    saturated, which presents a broken number as perfect caching.

    Two callers persist what they accumulate — the duplicate-key merge in `_read`
    and the fold in `record_many` — and neither makes a bound belong here. A sum
    that outgrows the file is a fact about the file, so `_write` answers it, for
    every writer at once and without a published aggregate ever being quietly
    reduced to keep a row writable.
    """

    for key in _COUNTER_KEYS:
        target[key] += row[key]


def _newer_timestamp(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    """Keep the later of two instants, comparing points in time.

    Text order is not time order once two rows carry different UTC offsets, which
    a state file merged across machines or an older release can hold.
    """

    if candidate is None:
        return current
    if current is None:
        return candidate
    current_instant = _instant(current)
    candidate_instant = _instant(candidate)
    if current_instant is None:
        return candidate
    if candidate_instant is None:
        return current
    return candidate if candidate_instant > current_instant else current


def _retain_hour_slices(row: dict, measured: datetime) -> dict:
    """Keep only the recent, usable hour slices needed by the 24-hour report."""

    hours = row.get("hours")
    if hours is None:
        return row

    current_start = _hour_start(measured).astimezone(timezone.utc)
    oldest_start = current_start - timedelta(hours=USAGE_HOURLY_RETENTION_HOURS - 1)
    retained: dict[str, dict] = {}
    expired = row.get("hourly_expired_totals")
    expired_known = isinstance(expired, dict)
    if expired_known:
        expired = dict(expired)
    incomplete = row.get("hourly_history_complete") is not True
    for item in hours:
        parts = _hour_key_parts(item.get("key"))
        if parts is None:
            incomplete = True
            continue
        key, start = parts
        instant = start.astimezone(timezone.utc)
        metered = _instant(item.get("last_metered_at"))
        if instant > measured or (metered is not None and metered > measured):
            # Future slices cannot be evidence of usage and must not occupy the
            # bounded recent history. A current-hour aggregate may also contain
            # future-stamped calls; its counters cannot be split retrospectively.
            incomplete = True
            continue
        if instant < oldest_start:
            # Keep the expired portion of the daily aggregate separate so a
            # partial oldest local day can distinguish ordinary retention from a
            # missing in-horizon slice.
            if expired_known:
                _accumulate(expired, item)
            continue
        retained[key] = {**item, "key": key}
    latest_metered = _instant(row.get("last_metered_at"))
    if (
        not incomplete
        and row.get("requests", 0) > 0
        and latest_metered is not None
        and oldest_start <= latest_metered <= measured
        and _hour_key(latest_metered) not in retained
    ):
        logger.warning(
            "Model Hub usage ledger row %s has complete hourly history without "
            "its latest metered hour; publishing it as incomplete",
            _row_key(row),
        )
        incomplete = True
    row_day = _calendar_day(row.get("day", ""))
    if not incomplete and row_day is not None:
        try:
            day_start = _local_midnight(row_day).astimezone(timezone.utc)
            day_end = _local_midnight(row_day + timedelta(days=1)).astimezone(timezone.utc)
        except (OverflowError, OSError, ValueError):
            day_start = None
            day_end = None
        if (
            day_start is not None
            and day_end is not None
            and day_start <= measured
            and day_end > oldest_start
        ):
            nested_totals = _empty_totals()
            for item in retained.values():
                _accumulate(nested_totals, item)
            if expired_known:
                _accumulate(nested_totals, expired)
            if not expired_known or any(
                nested_totals[key] != row[key] for key in _COUNTER_KEYS
            ):
                logger.warning(
                    "Model Hub usage ledger row %s cannot reconcile its complete "
                    "in-horizon day with retained and expired hourly counters; "
                    "publishing it as incomplete",
                    _row_key(row),
                )
                incomplete = True
    if len(retained) > USAGE_HOURLY_RETENTION_HOURS:
        retained = dict(
            sorted(
                retained.items(),
                key=lambda item: _hour_key_parts(item[0])[1],
            )[-USAGE_HOURLY_RETENTION_HOURS:]
        )
        incomplete = True
    return {
        **row,
        "hours": sorted(retained.values(), key=lambda item: item["key"]),
        "hourly_expired_totals": expired if expired_known else None,
        "hourly_history_complete": not incomplete,
    }


class BoundedUsageLedger:
    """Persist metered upstream-call token counts as a bounded daily aggregate."""

    def __init__(
        self,
        path: Path,
        *,
        max_rows: int = USAGE_MAX_ROWS,
        retention_days: int = USAGE_RETENTION_DAYS,
        now: Callable[[], datetime] = _utc_now,
    ):
        self.path = path
        self.max_rows = max_rows
        self.retention_days = retention_days
        # Not a second clock: hub callers pass their own so a fixed service clock
        # still decides every day this ledger writes. What matters is that this one
        # is *read* where the write happens rather than handed in from a call.
        self._now = now
        self._lock = threading.RLock()

    def _within_capacity(self, rows: list[dict], *, measured: datetime) -> list[dict]:
        """Return at most `max_rows` of these rows, evicting the least recently metered.

        `_recency` orders by day first, so this may only be handed rows that are
        already reportable. Given a future-dated row it does the opposite of its
        job: that row outranks every real one, survives, and then reads refuse it
        — which is the defect `_retained` exists to close, and which reappeared
        the one time this was called on rows straight out of the file.

        So the two callers are the two places a reportable set is formed, and
        neither is the parse. `_write` calls it on rows `_retained` has already
        placed inside the ledger's own day window; `window` calls it on rows it
        has already filtered to the requested one. Both keep the same survivors
        in the same order.

        `measured` is what those two filters cannot supply: the day they bound is
        only the first half of the order, and an instant inside today can still be
        one no clock has reached. It is the reading each caller already took for its
        own window, so the eviction and the placement answer to one clock — and on
        the write path it changes nothing, because `_retained` has already brought
        every instant back under it.
        """

        if len(rows) <= self.max_rows:
            return rows
        return sorted(rows, key=lambda row: _recency(row, measured))[-self.max_rows :]

    def _read(self) -> _LedgerRead:
        if not self.path.exists():
            return _LedgerRead([], False)
        # Degrading to empty keeps a broken optional-feature file from failing
        # startup, but the next write replaces that file — so this is the last
        # moment its history is recoverable, and saying nothing would erase it
        # silently.
        # Every way decoding can fail, by category rather than by the shapes a
        # particular file happens to hold: invalid UTF-8 and an integer past the
        # digit limit both raise plain `ValueError`, deep nesting raises
        # `RecursionError`, and either one escaping here would take down the read
        # route and then stop metering entirely — the loud-failure outcome this
        # degradation exists to prevent.
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            logger.warning("Model Hub usage ledger %s is unreadable: %s", self.path, exc)
            return _LedgerRead([], True)
        if not isinstance(payload, list):
            logger.warning("Model Hub usage ledger %s is not a list of rows", self.path)
            return _LedgerRead([], True)
        rows: dict[tuple[str, str, str], dict] = {}
        dropped = 0
        for item in payload:
            row = _normalize_row(item)
            if row is None:
                dropped += 1
                continue
            existing = rows.get(_row_key(row))
            if existing is None:
                rows[_row_key(row)] = row
                continue
            _accumulate(existing, row)
            existing["last_metered_at"] = _newer_timestamp(
                existing["last_metered_at"],
                row["last_metered_at"],
            )
            _merge_hour_slices(existing, row)
        if dropped:
            logger.warning(
                "Model Hub usage ledger %s dropped %d unusable row(s)", self.path, dropped
            )
        # A merge builds a counter no row in the file carried. `_counter` cleared
        # each addend, and their sum can still land past the ceiling — only a
        # file holding duplicate keys reaches this, which is one this ledger never
        # wrote. Same answer as every other door, for the same reason: a row whose
        # magnitude no published document could carry is dropped, not saturated.
        held = []
        degraded = dropped > 0
        for row in rows.values():
            if all(row[key] <= USAGE_COUNTER_CEILING for key in _COUNTER_KEYS):
                held.append(row)
                continue
            degraded = True
            logger.warning(
                "Model Hub usage ledger %s dropped row %s: merged counters outgrew "
                "what the file can carry",
                self.path,
                _row_key(row),
            )
        return _LedgerRead(sorted(held, key=_row_key), degraded)

    def _write(self, rows: list[dict], *, measured: datetime) -> None:
        """Persist the rows the file can hold, at both of the capacities it has.

        `max_rows` is one, and `_within_capacity` is where it is applied for this
        door and for `window` together — holding it here alone is what let a larger
        file be read back and published whole. The other capacity is what a stored
        counter may be without putting the published document out of the
        range its reader holds exactly, and it needs the same door for a reason
        the exact merges upstream make unavoidable: folding an increment onto a row
        near the ceiling, or merging two duplicate-keyed rows a corrupt file holds,
        can produce a counter past it. Persisting that would put the loss somewhere
        worse than here — the next read would find a row it cannot use, and the
        usage would go missing with nothing having said so.

        So the row stops where it stops fitting, once, with its identity in the
        log. That is also what heals the file: a bucket whose stored counters were
        never real disappears instead of saturating, and the next call recorded
        against it starts a row that means what it says.
        """

        holdable: list[dict] = []
        for row in rows:
            if all(row[key] <= USAGE_COUNTER_CEILING for key in _COUNTER_KEYS):
                holdable.append(_retain_hour_slices(row, measured))
                continue
            logger.warning(
                "Model Hub usage ledger %s dropped row %s: counters outgrew what the "
                "file can carry",
                self.path,
                _row_key(row),
            )
        retained = self._within_capacity(holdable, measured=measured)
        write_state_document(self.path, sorted(retained, key=_row_key))

    def record(
        self,
        *,
        source_id: str,
        model_id: str,
        usage: Optional[ProtocolUsageReport],
        at: datetime,
    ) -> None:
        """Fold one metered upstream call into its day's row.

        The new row goes through the same normalization as a persisted one, so
        what this module writes and what it reads can never disagree about a
        bound, a subset, or an identity.

        `at` is a value the caller captured when its call ended; the horizon and
        the ceiling below are a reading taken where the write happens. The two are
        not interchangeable, because metering runs off the event loop and reaches
        this lock in whatever order the executor ran it: a call stamped just before
        local midnight can persist after one stamped just after it. Handing the
        captured value to a ledger-wide bound would then date the newer row into
        the future and drop it. So `at` decides this call's own bucket and stamp
        and nothing else — up to the moment it is persisted, since nothing is
        metered later than the write that records it.
        """

        self.record_many((UsageCall(source_id=source_id, model_id=model_id, usage=usage, at=at),))

    def record_many(self, calls: Sequence["UsageCall"]) -> None:
        """Fold a batch of metered calls into their days' rows in one transaction.

        The batch is the reason a backlog cannot grow: one read, one retention
        pass and one fsync serve however many calls arrived while the previous
        batch was being written, so the queue drains in bursts of whatever size
        it reached rather than one durable write at a time. Folding is what makes
        that safe — every call still lands in its own day's row with its own
        stamp, so a batch of ten is arithmetically the ten separate writes it
        replaces, never a summary of them. A call that already speaks for several
        (`UsageCall.requests`) folds by the same arithmetic onto the same row those
        calls would each have reached.

        One `persisted_at` reading covers the batch, for the same reason `record`
        takes its own: it is the moment these calls reached the disk, and none of
        them was metered later than that.
        """

        if not calls:
            return
        with self._lock:
            persisted_at = _aware(self._now())
            rows = {_row_key(row): row for row in self._read().rows}
            folded = False
            for call in calls:
                metered_at = min(_aware(call.at), persisted_at)
                usage = call.usage
                # A live call carries identities, not keys, and this is the one place
                # they become one: derive here and every row downstream — new,
                # accumulated, or read back next restart — is already a key, so no
                # later surface can re-derive one that was folded and orphan its row.
                source_key = usage_ledger_key(call.source_id)
                model_key = usage_ledger_key(call.model_id)
                increment = None
                if source_key is not None and model_key is not None:
                    increment = _normalize_row(
                        {
                            "day": local_usage_day(metered_at).isoformat(),
                            "source_id": source_key,
                            "model_id": model_key,
                            "requests": call.requests,
                            "token_reports": call.requests if usage is not None else 0,
                            "input_tokens": usage.input_tokens if usage else 0,
                            "cached_input_tokens": usage.cached_input_tokens if usage else 0,
                            "output_tokens": usage.output_tokens if usage else 0,
                            "last_metered_at": metered_at.isoformat(),
                            "hours": [
                                {
                                    "key": _hour_key(metered_at),
                                    "requests": call.requests,
                                    "token_reports": call.requests if usage is not None else 0,
                                    "input_tokens": usage.input_tokens if usage else 0,
                                    "cached_input_tokens": usage.cached_input_tokens if usage else 0,
                                    "output_tokens": usage.output_tokens if usage else 0,
                                    "last_metered_at": metered_at.isoformat(),
                                }
                            ],
                            "hourly_expired_totals": _empty_totals(),
                            "hourly_history_complete": True,
                        }
                    )
                if increment is None:
                    # Reachable only for a value no config can hold — not text, or
                    # empty — because a long identity is folded to a bounded key
                    # rather than refused. Loud rather than silent so a caller that
                    # invents an identifier shows up as lost metering instead of as
                    # a quietly incomplete tab.
                    logger.warning(
                        "Model Hub usage metering skipped a call with an unusable identifier",
                        extra={"source_id_usable": source_key is not None},
                    )
                    continue
                existing = rows.get(_row_key(increment))
                if existing is None:
                    rows[_row_key(increment)] = increment
                else:
                    _accumulate(existing, increment)
                    existing["last_metered_at"] = _newer_timestamp(
                        existing["last_metered_at"],
                        increment["last_metered_at"],
                    )
                    _merge_hour_slices(existing, increment)
                folded = True
            if not folded:
                return
            retained = self._retained(list(rows.values()), persisted_at)
            self._write(retained, measured=persisted_at)

    def _retained(self, rows: list[dict], measured: datetime) -> list[dict]:
        """Keep the rows this ledger's own clock can place, bounded at both edges.

        `window` already refuses to report a row dated after today, so a future row
        contributes to nothing a reader can see — while still holding one of the
        `max_rows` slots and outranking every real row in `_recency`, which evicts
        the least recently metered. A clock that jumps forward while many pairs are
        metered and is then corrected would therefore fill the ledger with rows that
        report nothing and evict every new one, and metering would stop until those
        dates arrive. Retention keeping what reads refuse is the defect; one window
        with both edges, measured by this module rather than declared by the file,
        is what closes it.

        A row inside the window may still claim an instant that has not happened.
        That is not evidence of a misplaced row, only of an unmeasurable recency, so
        it is bounded rather than dropped: the file supplies the instant, this module
        supplies its spelling and its ceiling.

        `measured` is read at the write, never handed in from a call — see `record`.
        """

        today = local_usage_day(measured)
        oldest = (today - timedelta(days=self.retention_days - 1)).isoformat()
        newest = today.isoformat()
        ceiling = _aware(measured).isoformat()
        placed = []
        for row in rows:
            if not oldest <= row["day"] <= newest:
                continue
            metered = _instant(row["last_metered_at"])
            if metered is not None and metered > measured:
                row = {**row, "last_metered_at": ceiling}
            placed.append(_retain_hour_slices(row, measured))
        return placed

    def _window_rows(self, *, days: int, now: datetime) -> tuple[list[dict], bool]:
        """Return reportable rows and whether reading the ledger degraded."""

        bounded_days = max(1, min(int(days), self.retention_days))
        today = local_usage_day(now)
        first_day = (today - timedelta(days=bounded_days - 1)).isoformat()
        last_day = today.isoformat()
        with self._lock:
            read = self._read()
        rows = read.rows
        placed = [row for row in rows if first_day <= row["day"] <= last_day]
        # The reportable set is formed here, so this is where the file's row
        # capacity bounds what `summary` can publish — after the date filter, never
        # before it. Every row left is one a reader can see, which is the condition
        # `_within_capacity` needs to evict the right one.
        #
        # `_write` never emits more rows than the file holds, so reaching this at
        # all means reading a file this ledger did not write. That is worth saying
        # once, unlike eviction on the write path, which is retention working as
        # designed and happens constantly.
        held = self._within_capacity(placed, measured=_aware(now))
        if len(held) < len(placed):
            logger.warning(
                "Model Hub usage ledger %s held %d reportable row(s) over its "
                "capacity of %d; dropped the least recently metered",
                self.path,
                len(placed) - len(held),
                self.max_rows,
            )
        return sorted(held, key=_row_key), read.degraded

    def window(self, *, days: int, now: datetime) -> list[dict]:
        """Return the rows inside the trailing local-day window, oldest first."""

        rows, _degraded = self._window_rows(days=days, now=now)
        return rows

    def _hourly_rows(
        self,
        *,
        starts: Sequence[datetime],
        now: datetime,
    ) -> tuple[list[dict], bool]:
        """Select hourly owners by durable UTC evidence before local projection."""

        report_instant = _aware(now)
        horizon_start = starts[0].astimezone(timezone.utc)
        current_start = starts[-1].astimezone(timezone.utc)
        with self._lock:
            read = self._read()

        candidates: list[dict] = []
        for row in read.rows:
            latest = _instant(row.get("last_metered_at"))
            if latest is not None and latest < horizon_start:
                has_in_horizon_hour = False
                for item in row.get("hours") or ():
                    parts = _hour_key_parts(item.get("key"))
                    if parts is None:
                        continue
                    _key, start = parts
                    start = start.astimezone(timezone.utc)
                    if horizon_start <= start <= current_start:
                        has_in_horizon_hour = True
                        break
                if not has_in_horizon_hour:
                    continue
            row_day = _calendar_day(row["day"])
            if row_day is not None and _overlaps_local_day(
                horizon_start,
                report_instant,
                row_day,
            ):
                candidates.append(row)
                continue

            if latest is not None and horizon_start <= latest <= report_instant:
                candidates.append(row)
                continue

            for item in row.get("hours") or ():
                parts = _hour_key_parts(item.get("key"))
                if parts is None:
                    continue
                _key, start = parts
                start = start.astimezone(timezone.utc)
                if horizon_start <= start <= current_start:
                    candidates.append(row)
                    break

        held = self._within_capacity(candidates, measured=report_instant)
        if len(held) < len(candidates):
            logger.warning(
                "Model Hub usage ledger %s held %d hourly report row(s) over "
                "its capacity of %d; dropped the least recently metered",
                self.path,
                len(candidates) - len(held),
                self.max_rows,
            )
        return sorted(held, key=_row_key), read.degraded

    def summary(
        self,
        *,
        days: int = USAGE_DEFAULT_WINDOW_DAYS,
        now: datetime,
        identities: Optional[Sequence[SourceIdentity]] = None,
    ) -> dict:
        """Aggregate the window into the read shape the settings page consumes.

        Labels are joined here rather than persisted, because a display name is
        user-supplied text this ledger has no business storing and a join keeps a
        rename visible immediately instead of freezing old copies. What arrives is
        what config holds — identities, nested by Source — and this method keys it,
        because a row is keyed and the component holding config is not the one that
        knows that: the caller that keyed its own map looked a label up by
        `row["source_id"]`, so a source whose ID is past the admission bound reported
        no label while still existing, and its renames never appeared.

        The nesting is the other half of the same rule. A model's identity is its
        Source and its ID together, so a flat model map answers for the wrong Source
        whenever a common model ID is listed on more than one — which is every
        interesting case. `SourceIdentity` makes that arity part of the argument's
        type, so there is no keying convention left for a caller to hold.

        A model's label is its own identity, which only matters for a folded row.
        There the key is a head plus a digest, so a tab drawing `model_id` would
        show a string nobody typed; the label carries the identity back.
        """

        bounded_days = max(1, min(int(days), self.retention_days))
        today = local_usage_day(now)
        rows, _degraded = self._window_rows(days=bounded_days, now=now)
        return self._summary_from_rows(
            rows,
            window_days=bounded_days,
            from_day=(today - timedelta(days=bounded_days - 1)).isoformat(),
            to_day=today.isoformat(),
            identities=identities,
        )

    def _summary_from_rows(
        self,
        rows: Sequence[dict],
        *,
        window_days: int,
        from_day: str,
        to_day: str,
        identities: Optional[Sequence[SourceIdentity]],
    ) -> dict:
        """Aggregate one exact set of daily or hourly rows."""

        keyed_source_labels, keyed_model_labels = _keyed_identities(identities)

        totals = _empty_totals()
        sources: dict[str, dict] = {}
        by_day: dict[str, dict] = {}
        for row in rows:
            _accumulate(totals, row)

            source = sources.setdefault(
                row["source_id"],
                {
                    "source_id": row["source_id"],
                    "label": keyed_source_labels.get(row["source_id"]),
                    **_empty_totals(),
                    "last_metered_at": None,
                    "models": {},
                },
            )
            _accumulate(source, row)
            source["last_metered_at"] = _newer_timestamp(
                source["last_metered_at"],
                row["last_metered_at"],
            )
            model = source["models"].setdefault(
                row["model_id"],
                {
                    "model_id": row["model_id"],
                    "label": keyed_model_labels.get((row["source_id"], row["model_id"])),
                    **_empty_totals(),
                },
            )
            _accumulate(model, row)

            day = by_day.setdefault(row["day"], {"day": row["day"], **_empty_totals()})
            _accumulate(day, row)

        return {
            "window_days": window_days,
            "from_day": from_day,
            "to_day": to_day,
            "totals": totals,
            "sources": [
                {
                    **{key: value for key, value in source.items() if key != "models"},
                    "models": sorted(
                        source["models"].values(),
                        key=lambda model: (-model["requests"], model["model_id"]),
                    ),
                }
                for source in sorted(
                    sources.values(),
                    key=lambda source: (-source["requests"], source["source_id"]),
                )
            ],
            "days": [by_day[day] for day in sorted(by_day)],
        }

    def report(
        self,
        *,
        window: str,
        now: datetime,
        identities: Optional[Sequence[SourceIdentity]] = None,
    ) -> dict:
        """Build one modern dense report without reallocating daily history."""

        if window not in USAGE_WINDOW_KEYS:
            raise ValueError(f"unsupported usage window: {window!r}")
        if window == "24h":
            return self._hourly_report(now=now, identities=identities)
        days = int(window[:-1])
        bounded_days = max(1, min(days, self.retention_days))
        report_local = _local(now)
        today = report_local.date()
        from_day = today - timedelta(days=bounded_days - 1)
        rows, read_degraded = self._window_rows(days=bounded_days, now=now)
        summary = self._summary_from_rows(
            rows,
            window_days=bounded_days,
            from_day=from_day.isoformat(),
            to_day=today.isoformat(),
            identities=identities,
        )
        by_day = {
            row["day"]: []
            for row in rows
            if from_day.isoformat() <= row["day"] <= today.isoformat()
        }
        for row in rows:
            if row["day"] in by_day:
                by_day[row["day"]].append(
                    {
                        "source_id": row["source_id"],
                        "model_id": row["model_id"],
                        **{key: row[key] for key in _COUNTER_KEYS},
                    }
                )
        buckets = []
        for index in range(bounded_days):
            bucket_day = from_day + timedelta(days=index)
            start = _local_midnight(bucket_day)
            end = (
                report_local
                if bucket_day == today
                else _local_midnight(bucket_day + timedelta(days=1))
            )
            buckets.append(
                {
                    "key": bucket_day.isoformat(),
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                    "history_complete": not read_degraded,
                    "rows": sorted(
                        by_day.get(bucket_day.isoformat(), []),
                        key=lambda row: (row["source_id"], row["model_id"]),
                    ),
                }
            )
        return {
            **summary,
            "window_key": window,
            "granularity": "day",
            "from_at": buckets[0]["start_at"],
            "to_at": report_local.isoformat(),
            "buckets": buckets,
        }

    def _hourly_report(
        self,
        *,
        now: datetime,
        identities: Optional[Sequence[SourceIdentity]],
    ) -> dict:
        """Project only measured nested slices onto 24 actual consecutive hours."""

        report_instant = _aware(now)
        starts = _hour_starts(now)
        interval_by_instant = {
            start.astimezone(timezone.utc): (start, index)
            for index, start in enumerate(starts)
        }
        first_start = starts[0]
        report_local = _local(now)
        last_day = report_local.date()
        # DST plus a fractional offset can make these 24 actual intervals touch
        # three local dates. Select rows from their durable UTC-hour evidence
        # before applying the current host timezone to their local presentation.
        rows, read_degraded = self._hourly_rows(starts=starts, now=report_instant)
        measured_by_bucket: list[dict[tuple[str, str], dict]] = [
            {} for _ in starts
        ]
        incomplete: set[int] = set(range(len(starts))) if read_degraded else set()

        for row in rows:
            # Read-only projection must apply the same temporal evidence policy
            # as persistence, including future slices in an otherwise valid day.
            row = _retain_hour_slices(row, report_instant)
            if row["requests"] <= 0:
                continue
            row_day = _calendar_day(row["day"])
            if row.get("hourly_history_complete") is not True:
                overlapping = False
                if row_day is not None:
                    for index, start in enumerate(starts):
                        end = (
                            starts[index + 1]
                            if index + 1 < len(starts)
                            else report_instant
                        )
                        if _overlaps_local_day(start, end, row_day):
                            incomplete.add(index)
                            overlapping = True
                if not overlapping:
                    # A legacy daily-only row can still be selected by its
                    # durable last-metered instant after a timezone change,
                    # while its persisted local day no longer maps to this
                    # bucket grid. Its requests cannot be allocated to any
                    # current hour, so every bucket remains uncertain.
                    incomplete.update(range(len(starts)))
            for item in row.get("hours") or ():
                parts = _hour_key_parts(item.get("key"))
                if parts is None:
                    continue
                _key, start = parts
                bucket = interval_by_instant.get(start)
                if bucket is None:
                    continue
                _start_utc, index = bucket
                key = (row["source_id"], row["model_id"])
                projected = measured_by_bucket[index].get(key)
                if projected is None:
                    start_local = start.astimezone()
                    projected = {
                        "day": start_local.date().isoformat(),
                        "source_id": row["source_id"],
                        "model_id": row["model_id"],
                        **{counter: 0 for counter in _COUNTER_KEYS},
                        "last_metered_at": None,
                    }
                    measured_by_bucket[index][key] = projected
                _accumulate(projected, item)
                projected["last_metered_at"] = _newer_timestamp(
                    projected["last_metered_at"],
                    item.get("last_metered_at"),
                )

        bucket_rows = []
        summary_rows = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else report_instant
            start_local = start.astimezone()
            end_local = end.astimezone()
            projected_rows = [
                {
                    "source_id": row["source_id"],
                    "model_id": row["model_id"],
                    **{counter: row[counter] for counter in _COUNTER_KEYS},
                }
                for row in sorted(
                    measured_by_bucket[index].values(),
                    key=lambda row: (row["source_id"], row["model_id"]),
                )
            ]
            bucket_rows.append(
                {
                    "key": start_local.isoformat(timespec="seconds"),
                    "start_at": start_local.isoformat(),
                    "end_at": end_local.isoformat(),
                    "history_complete": index not in incomplete,
                    "rows": projected_rows,
                }
            )
            summary_rows.extend(measured_by_bucket[index].values())

        days = sorted({
            *(start.astimezone().date().isoformat() for start in starts),
            last_day.isoformat(),
        })
        summary = self._summary_from_rows(
            summary_rows,
            window_days=len(days),
            from_day=days[0],
            to_day=days[-1],
            identities=identities,
        )
        return {
            **summary,
            "window_key": "24h",
            "granularity": "hour",
            "from_at": first_start.astimezone().isoformat(),
            "to_at": report_local.isoformat(),
            "buckets": bucket_rows,
        }


class _AbandonableWriter(Executor):
    """One serialized worker for ledger writes that the process can walk away from.

    `ThreadPoolExecutor` is the obvious way to spell "one worker thread", and it is
    the one mechanism this module may not use: its workers are non-daemon and it
    registers an `atexit` hook that joins them. A worker wedged in `fsync` on an
    unresponsive disk therefore holds interpreter shutdown open forever — *after*
    every bounded wait in this module has already returned and told its caller the
    write was unfinished. Bounding the coroutine is not the same as being able to
    abandon the work, and this is the resource where only the second one counts:
    metering is optional, so a stop or restart must never wait on it.

    A daemon thread says exactly that, and CPython does not join one at shutdown.
    The `Executor` interface is kept so `run_in_executor` still bridges the thread
    to the loop; a worker is started on the first write and lives as long as the
    process, which is the same single idle thread the pool cost.
    """

    def __init__(self) -> None:
        self._queue: "queue.SimpleQueue[tuple[CFuture, Callable, tuple, dict]]" = queue.SimpleQueue()
        self._worker = threading.Thread(
            target=self._serve,
            name="model-hub-usage",
            daemon=True,
        )
        self._worker.start()

    def submit(self, fn: Callable, /, *args: object, **kwargs: object) -> CFuture:
        future: CFuture = CFuture()
        self._queue.put((future, fn, args, kwargs))
        return future

    def _serve(self) -> None:
        while True:
            future, fn, args, kwargs = self._queue.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - relayed to the waiting future
                future.set_exception(exc)
            else:
                future.set_result(result)


_LEDGER_EXECUTOR_LOCK: Final = threading.Lock()
_LEDGER_EXECUTOR: Optional[_AbandonableWriter] = None
# How long a metering caller waits for its own row to reach the disk. Sized for a
# local read-modify-write and an fsync plus scheduling slack, which is orders of
# magnitude under it, so nothing but a disk that has stopped answering gets here.
_DURABILITY_WAIT_SECONDS: Final = 2.0


def _ledger_executor() -> _AbandonableWriter:
    """The one thread ledger writes run on, created when something is first metered.

    Deliberately not the loop's default executor. `record_many` holds the ledger's
    lock across an fsync, so submitting one job per completed call there would
    occupy that many shared workers while all but one waited on the lock — and
    whatever else in the process reaches for a thread would wait behind metering
    for no gain, since the lock admits one writer regardless. Owning the
    serialization makes it explicit instead of emergent, and it costs a single
    idle thread once anything has been metered at all.

    Owning it is also what makes it abandonable; see `_AbandonableWriter` for why
    the stdlib pool is the one mechanism that cannot be used here.
    """

    global _LEDGER_EXECUTOR
    with _LEDGER_EXECUTOR_LOCK:
        if _LEDGER_EXECUTOR is None:
            _LEDGER_EXECUTOR = _AbandonableWriter()
        return _LEDGER_EXECUTOR


@dataclass(frozen=True)
class UsageCall:
    """One metered upstream call, in the shape the ledger folds it in.

    Or several of them. `requests` is how many calls this value speaks for, so a
    queue can fold the ones that will land on a single row before they reach the
    disk without a second shape existing for a folded call. Every call folded
    together agrees on whether tokens were reported — that is part of what makes
    them one row — so `token_reports` stays derivable from `usage` instead of
    becoming a second additive field able to disagree with `requests`.
    """

    source_id: str
    model_id: str
    usage: Optional[ProtocolUsageReport]
    at: datetime
    requests: int = 1

    @property
    def fold_key(self) -> tuple[str, str, str, str, bool]:
        """What makes two calls one daily row and one hourly slice.

        The ledger's own row key plus whether tokens were reported, which is the
        part that keeps a fold arithmetically identical to the calls it replaces.
        The hour is part of the queue key so a flush cannot merge calls from
        different hours before the ledger sees their temporal identity. The local
        date must also remain: a UTC hour can cross local midnight, and folding
        those calls would move the earlier day's counts into the later day.
        """

        return (
            local_usage_day(self.at).isoformat(),
            _hour_key(self.at),
            self.source_id,
            self.model_id,
            self.usage is not None,
        )

    def folded_with(self, other: "UsageCall") -> "UsageCall":
        """Fold a call that shares this one's row into a single value.

        Arithmetically the calls it replaces: counts add, token counts add, and
        the stamp is the later of the two — which is what the row would have kept
        anyway, since `record_many` keeps the newer stamp. Nothing is bounded here;
        every counter is bounded where rows are normalized, so a fold that
        saturates the ceiling saturates it identically.
        """

        reports = [report for report in (self.usage, other.usage) if report is not None]
        usage = None
        if reports:
            usage = ProtocolUsageReport.of(
                input_tokens=sum(report.input_tokens for report in reports),
                cached_input_tokens=sum(report.cached_input_tokens for report in reports),
                output_tokens=sum(report.output_tokens for report in reports),
            )
        return replace(
            self,
            usage=usage,
            at=max(_aware(self.at), _aware(other.at)),
            requests=self.requests + other.requests,
        )


@dataclass
class _QueuedRow:
    """One queued row and the future every call folded into it is waiting on."""

    call: UsageCall
    done: "asyncio.Future[None]"


class UsageWriter:
    """Async ownership of every ledger write, so nothing can take one along.

    Metering has two populations — a call either hands its body onward or it does
    not — but the property that a queued write outlives whatever queued it belongs
    to neither of them. It lives here once. A write is this object's from the
    moment it exists, so a caller cancelled mid-flight loses its own ordering and
    never the row, and a population added later inherits that by construction
    rather than by remembering to.

    Queue, not fan-out, and folded rather than capped. Calls accumulate while the
    flush ahead of them is on disk and the next flush takes all of them in one
    transaction; the ones heading for a single row fold into a single queued row as
    they arrive. The backlog is therefore bounded by the identities config holds
    rather than by how hard the hub is driven or by how long one write takes — a
    flush stuck on an unresponsive disk cannot grow it past that bound, which is
    the whole of what "bounded" can mean here. The alternatives both cost
    something this module exists to prevent: a capacity means choosing rows to
    drop, which loses billed usage, and blocking the caller stalls a served turn
    on a disk that is already failing.
    """

    def __init__(self, ledger: BoundedUsageLedger, *, durability_wait: float = _DURABILITY_WAIT_SECONDS):
        self.ledger = ledger
        self._durability_wait = durability_wait
        self._pending: dict[tuple[str, str, str, str, bool], _QueuedRow] = {}
        # The batch on its way to disk, kept visible so a drain that times out
        # can count it: in the executor is not the same as persisted.
        self._writing: tuple[_QueuedRow, ...] = ()
        self._flush: Optional[asyncio.Task[None]] = None
        # Whether the last flush lost its batch, so an outage is reported when it
        # starts and ends rather than once per failed write.
        self._dropping = False

    def record(
        self,
        *,
        source_id: str,
        model_id: str,
        usage: Optional[ProtocolUsageReport],
        at: datetime,
    ) -> "asyncio.Future[None]":
        """Own one call and queue it, handing back what will finish it.

        Synchronous on purpose: nothing may suspend between a caller deciding to
        meter a call and this object owning the result, or a cancellation could
        land in a window where the call is neither metered nor still meterable.

        `at` is the caller's own reading from when its call ended, carried rather
        than re-read, because the flush that persists this can start well after
        it and a queued write is still a report about the moment it finished.

        A caller that wants the row on disk before it returns hands the result to
        `wait_recorded`; one that does not can simply drop it. Callers whose calls
        fold together share one future, which is the same promise each would have
        had alone: it completes when their row reaches the disk.
        """

        call = UsageCall(source_id=source_id, model_id=model_id, usage=usage, at=at)
        queued = self._pending.get(call.fold_key)
        if queued is None:
            queued = _QueuedRow(call=call, done=asyncio.get_running_loop().create_future())
            self._pending[call.fold_key] = queued
        else:
            queued.call = queued.call.folded_with(call)
        if self._flush is None or self._flush.done():
            self._flush = asyncio.create_task(self._flush_pending())
        return queued.done

    async def wait_recorded(self, write: "asyncio.Future[None]") -> bool:
        """Wait out one queued write, bounded, and answer whether it landed.

        Two properties every metering caller wants and none of them owns. The
        shield is why a caller cancelled here loses its ordering and not the row.
        The bound is why a ledger that has stopped answering cannot hold a served
        turn open behind it: the wait exists so a caller reading the usage tab
        right after its own call sees that call, which is an ordering convenience,
        not a durability requirement the turn is allowed to fail for.

        Both used to be spelled at each metering call site, which made the bound
        the one property a new call site could omit by writing the obvious thing —
        and it did: awaiting a write with no deadline puts a stuck disk on the
        turn's critical path. Here there is nothing to omit.

        A timed-out write is not lost or cancelled. It stays this writer's, keeps
        its place in the queue, and keeps counting in `unpersisted` until the disk
        answers; only the caller's wait for it ends.
        """

        try:
            await asyncio.wait_for(asyncio.shield(write), self._durability_wait)
        except asyncio.TimeoutError:
            return False
        return True

    @property
    def unpersisted(self) -> int:
        """How many metered calls this writer still owes the ledger.

        Queued and in-flight both count: handed to the writing thread is not the
        same as on disk. Calls, not queued rows — folding is how this object stays
        bounded, and answering in rows would make a backlog look like it shrank
        because the hub got busier.
        """

        return sum(queued.call.requests for queued in (*self._pending.values(), *self._writing))

    async def drain(self, *, timeout: float) -> int:
        """Wait out the calls still queued; answers how many did not reach disk.

        Bounded, because a ledger that cannot be reached must not hold a shutdown
        open — the same trade every owned drain in the hub makes.
        """

        flush = self._flush
        if flush is not None and not flush.done():
            await asyncio.wait((flush,), timeout=timeout)
        return self.unpersisted

    async def _flush_pending(self) -> None:
        """Write queued calls a batch at a time until nothing is left waiting.

        The loop ends only with the queue empty and without having awaited since
        it saw that, which is what lets `record` treat a finished flush as proof
        that it must start the next one.
        """

        loop = asyncio.get_running_loop()
        while self._pending:
            batch = tuple(self._pending.values())
            self._pending = {}
            self._writing = batch
            try:
                await loop.run_in_executor(
                    _ledger_executor(),
                    self.ledger.record_many,
                    tuple(queued.call for queued in batch),
                )
            except (OSError, ValueError) as exc:
                self._report_dropped(batch, exc)
            else:
                self._report_recovered()
            finally:
                self._writing = ()
            for queued in batch:
                if not queued.done.done():
                    queued.done.set_result(None)

    def _report_dropped(self, batch: Sequence[_QueuedRow], exc: BaseException) -> None:
        """Report that metering stopped, once per outage rather than per flush.

        A ledger that cannot be written must not hold up the turns it meters, so a
        failed batch is lost by design — but a state directory that has gone
        read-only then looks exactly like a hub with nothing to record, which is
        the one reading that makes this module's absence invisible. Saying it at
        `debug` said it to nobody.

        The transition carries the information, so the transition is what is
        logged: bounded by how often the ledger changes state rather than by how
        long an outage lasts, which is the bound a counter would be approximating.
        """

        calls = sum(queued.call.requests for queued in batch)
        if self._dropping:
            logger.debug(
                "Model Hub usage metering still cannot write %s, dropped %d call(s): %s",
                self.ledger.path,
                calls,
                exc,
            )
            return
        self._dropping = True
        logger.warning(
            "Model Hub usage metering cannot write %s and is dropping metered calls "
            "(%d lost in this batch); the usage tab will under-report until it recovers: %s",
            self.ledger.path,
            calls,
            exc,
        )

    def _report_recovered(self) -> None:
        """Close an outage the same way it was opened, so the gap has both edges."""

        if not self._dropping:
            return
        self._dropping = False
        logger.warning("Model Hub usage metering recovered; %s is writable again", self.ledger.path)
