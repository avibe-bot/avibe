"""The durable record of reset-card uses whose outcome is not settled.

Using a card cannot be undone, so a use is claimed here before anything is sent
to the vendor and the claim outlives the request, the page and the process. One
row per vendor account, keyed by an opaque digest: it holds the request id, the
card kind, when the press came and whether the use is still in flight. No grant,
credit, organization or account id is ever written.

Every way of not knowing fails closed:

* A claim returns only after its row is durably on disk (``journal.write_durable``,
  the engine journal's strict publication). If that cannot be confirmed the claim
  is refused and nothing is sent.
* A row is removed only by a write issued after its outcome was established. A
  write that fails can therefore leave a lock that should have gone, never remove
  one that should have stayed: an outcome that is unknown keeps the row its claim
  published, whatever happens to later writes.
* The file is this user's own private regular file in a private directory of its
  own. A link, a special file, another owner, wider permissions, an oversized or
  malformed document, an unknown field or a repeated key leave every account
  locked. Only a file that was never created reads as "no claims". Nothing here
  repairs or overwrites what it could not read.
* A row found in flight when the process starts has no press behind it any more:
  it is an unknown outcome.
* An unknown outcome is lifted only by a reading of that account the user asked
  for, begun after the outcome became unknown and at least
  ``UNKNOWN_OUTCOME_WAIT`` later: a vendor that did not answer may still finish
  the request, and a count read a few seconds on would look untouched.

All disk access happens under one mutex and belongs on a worker thread, never on
the controller's event loop. The controller process is the only writer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional
from uuid import uuid4

from core import private_fs
from vibe.model_hub_runtime.journal import write_durable

from .quota import CardReadingFloor, _iso, _moment

logger = logging.getLogger(__name__)

KINDS = frozenset({"all", "fable", "session", "weekly", "account"})
IN_FLIGHT = "in_flight"
UNCONFIRMED = "unconfirmed"
# How long after an outcome became unknown a reading must begin to lift its lock.
UNKNOWN_OUTCOME_WAIT = timedelta(seconds=60)

_VERSION = 1
# A row is about 170 bytes; the cap bounds every read and is enforced on write.
_MAX_BYTES = 64 * 1024
_ACCOUNT = re.compile(r"[a-f0-9]{64}")
# A request id is a UUID as written: the shape both vendors' other clients send.
_REQUEST = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
_ROW_KEYS = frozenset({"request_id", "kind", "since", "reason"})


class FenceUnavailable(Exception):
    """No claim was taken: the account is locked, or the store cannot be trusted or written."""


@dataclass
class _Row:
    request_id: str
    kind: str
    since: str
    reason: str
    # Readings numbered above this began after the outcome became unknown.
    # In memory only: a row loaded from disk predates every reading of this process.
    mark: int = 0
    # When the outcome became unknown, as far as this process can tell: when its press
    # ended without an answer, or when the row was found at startup. None while in flight.
    unknown_at: Optional[datetime] = None

    def stored(self) -> dict[str, str]:
        return {"request_id": self.request_id, "kind": self.kind, "since": self.since, "reason": self.reason}

    def public(self) -> dict[str, str]:
        return {"kind": self.kind, "since": self.since, "reason": self.reason}


@dataclass(frozen=True)
class FenceView:
    """What a quota summary may show: no request id, and accounts only as opaque keys."""

    damaged: bool
    locks: Mapping[str, dict[str, str]]
    # For a lock that cannot be attributed to a row: the oldest claim, or when the store was opened.
    since: str

    @property
    def locked(self) -> bool:
        return self.damaged or bool(self.locks)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("repeated key")
    return value


def _require_private(path: Path, info: os.stat_result, *, directory: bool, fd: Optional[int] = None) -> None:
    """Raise unless ``path`` is this user's own private directory or regular file, and not a link."""

    reparse_point = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if reparse_point or not (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode):
        raise ValueError("not a plain entry")
    if os.name != "nt" and info.st_uid != os.geteuid():
        raise ValueError("foreign owner")
    if not directory and (info.st_nlink != 1 or info.st_size > _MAX_BYTES):
        raise ValueError("linked or oversized")
    # POSIX: exactly 0700 / 0600. Windows: owned by this user, granting only this user and the machine.
    if not private_fs.is_private(path, info.st_mode, fd=fd):
        raise ValueError("not private")


def _parse(raw: bytes) -> dict[str, _Row]:
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    if (
        type(document) is not dict
        or set(document) != {"version", "locks"}
        or type(document["version"]) is not int
        or document["version"] != _VERSION
        or type(document["locks"]) is not dict
    ):
        raise ValueError("unknown document")
    rows: dict[str, _Row] = {}
    for account, row in document["locks"].items():
        if (
            not _ACCOUNT.fullmatch(account)
            or type(row) is not dict
            or set(row) != _ROW_KEYS
            or not all(type(value) is str for value in row.values())
            or not _REQUEST.fullmatch(row["request_id"])
            or row["kind"] not in KINDS
            or row["reason"] not in (IN_FLIGHT, UNCONFIRMED)
        ):
            raise ValueError("unknown row")
        since = _moment(row["since"])
        if since is None or _iso(since) != row["since"]:
            raise ValueError("unknown time")
        # Whatever it said, no press of this process stands behind it.
        rows[account] = _Row(row["request_id"], row["kind"], row["since"], UNCONFIRMED)
    return rows


def _read(path: Path) -> dict[str, _Row]:
    """The rows in ``path``; empty only when it was never created."""

    try:
        named = path.lstat()
    except FileNotFoundError:
        return {}
    _require_private(path, named, directory=False)
    # Never through a link, and never waiting on a FIFO or a device.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        _require_private(path, opened, directory=False, fd=descriptor)
        if not os.path.samestat(named, opened):
            raise ValueError("replaced while opening")
        chunks: list[bytes] = []
        remaining = _MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) > _MAX_BYTES:
        raise ValueError("oversized")
    return _parse(raw)


class CardFences:
    """One account, one unsettled use. See the module docstring for what fails closed."""

    def __init__(self, directory: Path, *, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        # A directory of its own: its privacy is checked, and on Windows inherited by the file.
        self.directory = directory
        self.path = directory / "fences.json"
        self._now = now
        self._mutex = threading.Lock()
        self._rows: dict[str, _Row] = {}
        self._damaged = False
        self._loaded = False
        self._opened = _iso(datetime.now().astimezone())

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            try:
                directory = self.directory.lstat()
            except FileNotFoundError:
                return
            _require_private(self.directory, directory, directory=True)
            rows = _read(self.path)
            # When each became unknown is not on disk. Found now is the latest it can be.
            found_at = self._now()
            for row in rows.values():
                row.unknown_at = found_at
            self._rows = rows
        except Exception:  # noqa: BLE001 - whatever stops the read, the answer is "locked"
            # No detail: a hostile document must not reach the log either.
            self._damaged = True
            logger.error("Model Hub reset card fence cannot be read; card use stays locked")

    def _publish(self, rows: Mapping[str, _Row]) -> None:
        """Replace the document durably, or raise. Never follows or repairs an unsafe entry."""

        document = {"version": _VERSION, "locks": {account: row.stored() for account, row in rows.items()}}
        data = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(data) > _MAX_BYTES:
            raise ValueError("too many unsettled uses")
        private_fs.ensure_private_directory(self.directory, repair=False)
        _require_private(self.directory, self.directory.lstat(), directory=True)
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            _require_private(self.path, current, directory=False)
        write_durable(self.path, data)
        _require_private(self.path, self.path.lstat(), directory=False)

    def claim(self, account: str, kind: str, now: datetime) -> str:
        """Lock ``account`` for one use and return its request id, durably recorded.

        Raises ``FenceUnavailable`` when the account already has an unsettled use,
        the store is damaged, or the row could not be confirmed on disk; nothing
        may be sent then.
        """

        with self._mutex:
            self._load()
            if self._damaged or not _ACCOUNT.fullmatch(account) or kind not in KINDS or account in self._rows:
                raise FenceUnavailable
            row = _Row(str(uuid4()), kind, _iso(now), IN_FLIGHT)
            try:
                self._publish({**self._rows, account: row})
            except Exception:  # noqa: BLE001 - not confirmed on disk, for whatever reason
                # The write may have reached the disk without being confirmed. That
                # can only leave a lock for a request never sent; the next write
                # replaces it, and a restart reads it as an unknown outcome.
                raise FenceUnavailable from None
            self._rows[account] = row
            return row.request_id

    def settle(self, account: str, request_id: str, *, unknown: bool, mark: int) -> bool:
        """Close this press's claim. True: released. False: the account stays locked as unconfirmed.

        ``unknown`` keeps the lock. Otherwise the row is removed, and if that
        cannot be written the lock is kept instead: the caller reports the use
        as unconfirmed, and the user's next forced reading lifts it. Never raises.
        """

        with self._mutex:
            row = self._rows.get(account)
            if row is None or row.request_id != request_id:
                # A press settles only the row it claimed. Anything else means the
                # store no longer describes what was sent.
                self._damaged = True
                return False
            if not unknown:
                remaining = {key: value for key, value in self._rows.items() if key != account}
                try:
                    self._publish(remaining)
                except Exception:  # noqa: BLE001 - not released: the lock is kept below
                    pass
                else:
                    self._rows = remaining
                    return True
            row.reason, row.mark, row.unknown_at = UNCONFIRMED, mark, self._now()
            try:
                self._publish(self._rows)
            except Exception:  # noqa: BLE001
                # The claim's own row is still on disk and locks the account after a restart.
                pass
            return False

    def unknown_floor(self) -> Optional[CardReadingFloor]:
        """What one reading must be to answer every unknown outcome; None when there is none to lift."""

        with self._mutex:
            self._load()
            unknown = [row for row in self._rows.values() if row.reason == UNCONFIRMED and row.unknown_at is not None]
            if self._damaged or not unknown:
                return None
            return CardReadingFloor(
                sequence=max(row.mark for row in unknown),
                ripe_at=max(row.unknown_at for row in unknown) + UNKNOWN_OUTCOME_WAIT,
            )

    def release_after_readings(self, readings: Iterable[tuple[str, int, datetime]]) -> None:
        """Lift each unconfirmed lock that a later reading of its own account answers.

        ``readings`` are (account, sequence, started_at) of authoritative card
        readings the user asked for. A lock is lifted only by a reading numbered
        above its mark, so begun after the outcome became unknown, that also began
        at least ``UNKNOWN_OUTCOME_WAIT`` after that moment. A use still in flight
        is never touched. A failed write lifts nothing.
        """

        with self._mutex:
            self._load()
            if self._damaged:
                return
            remaining = dict(self._rows)
            for account, sequence, started_at in readings:
                row = remaining.get(account)
                if (
                    row is not None
                    and row.reason == UNCONFIRMED
                    and row.unknown_at is not None
                    and sequence > row.mark
                    and started_at >= row.unknown_at + UNKNOWN_OUTCOME_WAIT
                ):
                    del remaining[account]
            if len(remaining) == len(self._rows):
                return
            try:
                self._publish(remaining)
            except Exception:  # noqa: BLE001 - nothing is lifted
                return
            self._rows = remaining

    def view(self) -> FenceView:
        with self._mutex:
            self._load()
            return FenceView(
                damaged=self._damaged,
                locks={account: row.public() for account, row in self._rows.items()},
                since=min(self._rows.values(), key=lambda row: _moment(row.since)).since if self._rows else self._opened,
            )
