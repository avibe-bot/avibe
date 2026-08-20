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

The cache is two tiers over one policy. Memory answers a warm process; the file
answers a cold one; a miss in both probes and writes through to each. Failures
are cached too, on a much shorter TTL, because a probe that just failed is the
one most likely to fail again — but only where there is no live success to keep.
A failed probe reports this process's luck reaching the registry, not what the
registry publishes, so it may fill a gap and never overwrite knowledge.

Nothing here is authoritative. Every entry is a best-effort answer with an
expiry, and a caller that reads a stale or absent one gets the same behaviour it
had before this file existed: it probes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple

from config import paths
from config.atomic_io import write_atomic

logger = logging.getLogger(__name__)

__all__ = ["cache_path", "cached_latest"]

#: Bumped when the on-disk shape changes. A file written by a newer Avibe is
#: ignored rather than guessed at, and the next write replaces it — this is a
#: cache, so discarding it costs one probe and never a correctness problem.
SCHEMA_VERSION = 1

SUCCESS_TTL_SECONDS = 3600.0
#: Failed lookups (network down, registry hiccup, rate limit) re-probe sooner so
#: a transient outage doesn't pin "—" for the full hour.
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


def _read_file() -> dict[str, _Entry]:
    """Load the persisted entries, discarding anything we cannot trust.

    A cache file is written by whichever Avibe process got there first, which
    may be an older or newer build, and may have died mid-write on a machine
    without atomic renames. Every failure mode collapses to the same answer:
    fewer entries, one more probe.
    """

    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Latest-version cache unreadable: %s", exc)
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}

    out: dict[str, _Entry] = {}
    for name, entry in entries.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        at = entry.get("at")
        value = entry.get("value")
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            continue
        if value is not None and not isinstance(value, str):
            continue
        out[name] = _Entry(float(at), value)
    return out


def _persist(name: str, entry: _Entry) -> _Entry:
    """Publish *entry* for *name*, and answer with whichever entry now stands.

    Read-modify-write with no cross-process lock: two processes updating two
    different dependencies at the same instant can drop one of the two entries.
    The cost of that loss is one extra probe on the next run, which is cheaper
    than a lock file that every CLI invocation would have to acquire.

    That tolerance has exactly one exception, and it is why this returns an
    entry instead of nothing. Two processes missing the *same* name both probe,
    and if the successful one lands first the slower failure would overwrite it
    — a loss that does not cost one probe but a whole ``FAILURE_TTL_SECONDS``
    window of ``latest_unavailable``, which for askill is not a slow path but a
    forced ~30s reinstall. So a failure may fill a gap and never displace a live
    success, and a process whose own probe lost still answers with the better
    entry rather than the one it happens to hold.
    """

    persisted = _read_file()
    if entry.value is None:
        standing = persisted.get(name)
        if standing is not None and standing.value is not None and standing.is_live(entry.at):
            return standing
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
    return entry


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

    standing = _persist(name, _Entry(time.time(), fetch()))
    with _LOCK:
        _MEMORY[name] = standing
    return standing.value
