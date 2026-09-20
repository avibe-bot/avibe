"""One owner for "what is the newest published version of X", cached on disk.

The answer used to live only in a module-level dict, which is right for the
long-running service and useless for the CLI: ``vibe runtime prepare`` is a
fresh process that asks GitHub for askill's newest release, exits, and throws
the answer away. Install, upgrade, every regression sync, and every tenant
update pay that probe again — and they pay it against the unauthenticated
GitHub budget of 60 requests per hour per IP, shared with the opencode probe
that reads the same cache.

Exhausting that budget is not a slow path, it is a wrong one: a 403 makes the
latest lookup fail, prepare cannot establish currency, and it falls back to
reinstalling askill — about 30 seconds of network to land the version already on
disk. So persisting the answer across processes matters more for the failures it
prevents than for the round trip it skips.

The cache is two tiers holding different things. The file tier holds *answers*,
so a cold process inherits what a previous one paid GitHub for. A failed probe
stops at the memory tier, which is the tier that needs it: a long-running
service polling a dead registry should back off, a fresh CLI process has nothing
to back off from.

Keeping failures off disk is also what removes the interesting race rather than
narrowing it. Two processes missing the same name both probe, outside any
cross-process lock, and no check-then-write can stop a slower failure from
landing on a fresh success — a guard only shrinks the window. A failure that
never reaches the file cannot overwrite anything, so every remaining
read-modify-write race is success-over-success, where the loser costs one extra
probe.

Inheriting a failure was never worth that window anyway: it does not avoid the
fallback it looks like it avoids. A process reading a cached ``None`` reports
``latest_unavailable`` and reinstalls askill; a process that re-probes might find
the blip over instead. Persisting the failure turns "maybe" into "certainly
reinstall" to save one HTTP request against a budget that, in the only case this
arises, is already exhausted.

Nothing here is authoritative. Every entry is a best-effort answer with an
expiry, and a caller that reads a stale or absent one gets the same behaviour it
had before this file existed: it probes.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

from config import paths
from config.atomic_io import write_atomic

logger = logging.getLogger(__name__)

__all__ = ["cache_path", "cached_latest"]

#: Bumped when the on-disk shape changes. A file written by a newer Avibe is
#: ignored rather than guessed at, and the next write replaces it — this is a
#: cache, so discarding it costs one probe and never a correctness problem.
SCHEMA_VERSION = 1

SUCCESS_TTL_SECONDS = 3600.0
#: Governs the memory tier only. A failed lookup (network down, registry hiccup,
#: rate limit) re-probes sooner so a transient outage doesn't pin "—" for the
#: full hour, and never outlives the process that had the bad luck.
FAILURE_TTL_SECONDS = 120.0


class _Entry(NamedTuple):
    at: float
    value: str | None

    def is_live(self, now: float) -> bool:
        age = now - self.at
        if age < 0:
            # A timestamp from the future is a clock that moved, not a fresh
            # answer. Treating it as live would pin the entry until the clock
            # caught up, which for a backwards system-clock step is unbounded.
            return False
        return age < (SUCCESS_TTL_SECONDS if self.value else FAILURE_TTL_SECONDS)


_LOCK = threading.Lock()
_MEMORY: dict[str, _Entry] = {}


def cache_path() -> Path:
    return paths.get_state_dir() / "latest_versions.json"


def _entry_from(raw: Any) -> _Entry | None:
    """Turn one untrusted mapping into an answer, or into nothing. Never raises.

    The guard is the boundary, not a list of the ways parsing has gone wrong so
    far. Two review rounds each supplied the next member of such a list — a
    non-UTF-8 file raising ``UnicodeDecodeError`` where only ``JSONDecodeError``
    was expected, then ``float(10**400)`` raising ``OverflowError``, which is an
    ``ArithmeticError`` and so was outside the widened ``ValueError`` too. A
    third enumeration would predict a fourth round.

    So this function owns the whole conversion, and its contract is a property:
    whatever the file says, the caller gets an ``_Entry`` it can use or ``None``.
    A field check added later inherits that, instead of needing its own
    ``except``.

    The explicit checks above the guard are still worth their lines, because
    they reject values that do *not* raise: ``inf`` and ``NaN`` survive a JSON
    round trip, compare false against every TTL, and would then be written back
    out as the non-standard ``Infinity``/``NaN`` tokens. And a null value is not
    something this version writes, so it is not something it trusts — corruption
    must not pin ``latest_unavailable`` on every cold process.
    """

    try:
        if not isinstance(raw, dict):
            return None
        at = raw.get("at")
        value = raw.get("value")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return None
        if not isinstance(value, str) or not value:
            return None
        at = float(at)
        if not math.isfinite(at):
            return None
        return _Entry(at, value)
    except Exception as exc:
        logger.debug("Latest-version cache entry unusable: %s", exc)
        return None


def _read_file() -> dict[str, _Entry]:
    """Load the persisted entries, discarding anything we cannot trust.

    A cache file is written by whichever Avibe process got there first, which
    may be an older or newer build, and may have died mid-write on a machine
    without atomic renames. Every failure mode collapses to the same answer:
    fewer entries, one more probe — per entry, so one unreadable name never
    costs its neighbours their answers.
    """

    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug("Latest-version cache unreadable: %s", exc)
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}

    out: dict[str, _Entry] = {}
    for name, entry in entries.items():
        if not isinstance(name, str):
            continue
        parsed = _entry_from(entry)
        if parsed is not None:
            out[name] = parsed
    return out


def _persist(name: str, entry: _Entry) -> None:
    """Write *entry* through to disk, leaving the other names alone.

    A failed probe stops here, at the one place that can enforce it: whatever a
    caller hands over, only answers reach the file. That is what makes the
    same-name race disappear instead of narrow (see the module docstring), and
    it is not the caller's rule to remember.

    What remains is a read-modify-write with no cross-process lock, so two
    processes publishing two *different* dependencies at the same instant can
    drop one of the two entries. That costs one extra probe on the next run,
    which is cheaper than a lock file every CLI invocation would have to
    acquire — and with failures excluded, the same-name collision is now
    success-over-success, where either winner is a correct answer.
    """

    if entry.value is None:
        return
    persisted = _read_file()
    persisted[name] = entry
    payload = {
        "schema_version": SCHEMA_VERSION,
        "entries": {key: {"at": item.at, "value": item.value} for key, item in persisted.items()},
    }
    try:
        write_atomic(cache_path(), json.dumps(payload, indent=2) + "\n")
    except OSError as exc:
        # A read-only or full state directory must not break a version lookup;
        # the caller still has its answer, it just won't outlive this process.
        logger.debug("Latest-version cache not persisted: %s", exc)


def cached_latest(name: str, fetch: Callable[[], str | None]) -> str | None:
    """Return the newest published version of *name*, probing only when stale.

    *fetch* is the upstream probe, called at most once and never while holding
    the lock: it is a network round trip, and unrelated lookups should not queue
    behind it.
    """

    now = time.time()
    with _LOCK:
        cached = _MEMORY.get(name)
    if cached is not None and cached.is_live(now):
        return cached.value

    persisted = _read_file().get(name)
    if persisted is not None and persisted.is_live(now):
        with _LOCK:
            _MEMORY[name] = persisted
        return persisted.value

    entry = _Entry(time.time(), fetch())
    with _LOCK:
        _MEMORY[name] = entry
    _persist(name, entry)
    return entry.value
