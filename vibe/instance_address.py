"""This instance's address: Host roles, the change journal and cleanup authority.

Rules and their reasons are in ``docs/plans/instance-address-change.md``. In
short: tenant content is served only on the managed name recorded on this
computer; a former name is answered with cleaning only while a fresh state from
avibe.bot lists it for this instance; every other managed name gets nothing.

This module lives in the Web UI process. It keeps no authority on disk: the
permission to clean is held in memory and bound to the pairing that read it, so
a restart or a new pairing starts with none.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, IO, Mapping
from urllib.parse import urlsplit

from config import paths
from vibe.binding_admission import OperationBusy, admitted
from config.atomic_io import write_atomic

logger = logging.getLogger(__name__)

# Same pattern the direct lifecycle uses for a managed hostname; a test pins the
# two together.
MANAGED_HOSTNAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-app\.avibe\.bot$")
_MANAGED_SUFFIX = "-app.avibe.bot"
# The claims grammar for a prefix; avibe.bot remains the authority on a name.
_PREFIX = re.compile(r"^[a-z0-9][a-z0-9-]{0,44}[a-z0-9]$")

CLEANUP_AUTHORITY_SECONDS = 60.0
STATE_READ_MIN_INTERVAL_SECONDS = 5.0
REACTIVATION_SETTLE_SECONDS = 5.0
FINAL_FRAME_GRACE_SECONDS = 3.0
CONFIRMATION_TTL_SECONDS = 600.0
DISPLAY_REFRESH_SECONDS = 60.0
_DRAIN_TIMEOUT_SECONDS = 15.0
_FIRST_ANSWER_WAIT_SECONDS = 12.0
# Longest wait before asking again about a rename avibe.bot has accepted ("pending").
_PENDING_RETRY_MAX_SECONDS = 5.0
_STATE_SCHEMA_VERSION = 1
_RECEIPT_CONTEXT = b"avibe-address-receipt-v1\x00"

ADDRESS_CHANGED_EVENT = "instance.address.changed"
ADDRESS_RETIRED_EVENT = "instance.address-retired"
RETIRED_HEADER = "X-Avibe-Address"
RETIRED_HEADER_VALUE = "retired"
CLEARED_REPORT_PATH = "/__avibe/address-cleared"

# Indirection so tests can advance each clock on its own (a sleeping computer
# advances only the wall clock).
_monotonic = time.monotonic
_wall = time.time
_sleep = time.sleep

_LOCK = threading.RLock()
# Counts cleaning responses being written, for the drain. Its own lock, held
# only to add or subtract, so the event loop can take it without ever waiting.
_DRAINED = threading.Condition(threading.Lock())


class AddressError(Exception):
    """A refusal the settings page can word."""

    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


# --- names ------------------------------------------------------------------


def normalize_host(value: str | None) -> str:
    """The one spelling of a Host: lower case, no port, no trailing dot."""

    raw_host = (value or "").lower().strip()
    if raw_host.startswith("[") and "]" in raw_host:
        host = raw_host[1 : raw_host.index("]")]
    elif raw_host.count(":") > 1:
        host = raw_host
    else:
        host = raw_host.split(":", 1)[0]
    return host.rstrip(".")


def is_managed_name(host: str | None) -> bool:
    return bool(host) and MANAGED_HOSTNAME.fullmatch(host or "") is not None


def hostname_for_prefix(prefix: str) -> str:
    return f"{prefix}{_MANAGED_SUFFIX}"


def _valid_prefix(prefix: Any) -> bool:
    # avibe.bot's rule for a new prefix (avibe-bot-backend lib/claims/prefix.ts):
    # also, ``<prefix>-app`` must not read as an IDNA label (``xx--``).
    return (
        isinstance(prefix, str)
        and _PREFIX.fullmatch(prefix) is not None
        and f"{prefix}-app"[2:4] != "--"
        and is_managed_name(hostname_for_prefix(prefix))
    )


# --- the local binding --------------------------------------------------------


@dataclass(frozen=True)
class Binding:
    """What this computer's configuration says about its own address."""

    active_host: str
    instance_id: str
    registry_binding: str
    epoch: str | None
    paired: bool
    instance_kind: str
    backend_origin: str

    @property
    def pairing(self) -> tuple[str, str]:
        return self.epoch or '', self.registry_binding


_BINDING_CACHE: tuple[tuple[Any, ...], Binding | None] | None = None
_binding_failure_logged: tuple[Any, ...] | None = None


def _binding_from_config(config: Any) -> Binding:
    from vibe.account_session_revoke import pairing_epoch
    cloud = config.remote_access.vibe_cloud
    active = ""
    try:
        parsed = urlsplit(str(cloud.public_url or "").strip())
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            active = normalize_host(parsed.hostname)
    except ValueError:
        active = ""
    backend = ""
    try:
        parsed_backend = urlsplit(str(cloud.backend_url or "").strip())
        if parsed_backend.scheme == "https" and parsed_backend.netloc:
            backend = f"https://{parsed_backend.netloc.lower()}"
    except ValueError:
        backend = ""
    from vibe.device_metadata import binding_key
    return Binding(
        active_host=active,
        instance_id=str(cloud.instance_id or "").strip(),
        epoch=pairing_epoch(config),
        registry_binding=binding_key(cloud.instance_id, cloud.session_secret),
        paired=bool(cloud.is_runtime_paired()),
        instance_kind=str(cloud.instance_kind or ""),
        backend_origin=backend,
    )


def current_binding() -> Binding | None:
    """Read the local binding, reloading the config only when its file changed.

    ``None`` means the configuration cannot be read; a managed Host then gets
    no content and no cleaning.
    """

    global _BINDING_CACHE, _binding_failure_logged

    path = paths.get_config_path()
    try:
        stat = path.stat()
        signature: tuple[Any, ...] = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except OSError:
        signature = (str(path), None)
    cached = _BINDING_CACHE
    if cached is not None and cached[0] == signature:
        return cached[1]
    binding: Binding | None
    if signature[1] is None:
        binding = None
    else:
        try:
            from core.services import settings as settings_service

            binding = _binding_from_config(settings_service.load_config())
        except Exception:
            # Not cached: one transient read error must not refuse every managed
            # Host until the file next changes. Logged once per file, not per request.
            if _binding_failure_logged != signature:
                _binding_failure_logged = signature
                logger.warning("instance address: configuration unreadable", exc_info=True)
            return None
    _BINDING_CACHE = (signature, binding)
    return binding


def _load_config() -> Any:
    from core.services import settings as settings_service

    return settings_service.load_config()


# --- durable state (display values, journal, receipt key; never authority) ------


def _state_path() -> Path:
    return paths.get_state_dir() / "instance-address.json"


def _empty_state(instance_id: str) -> dict[str, Any]:
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "instance_id": instance_id,
        "generation": None,
        "cleanup": [],
        "release_delay_seconds": None,
        "active_release_at_if_renamed_now": None,
        "state_read_at": None,
        "quiet": [],
        "returning": None,
        "journal": None,
        "last_operation": None,
        "unrecognized_change": None,
        "receipt_key": None,
    }


def _load_state(instance_id: str) -> dict[str, Any]:
    """The stored state of ``instance_id``; another instance's journal, quiet set
    and lists are never inherited (the receipt key is: it identifies browsers,
    not an instance)."""

    try:
        payload = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    state = _empty_state(instance_id)
    if not isinstance(payload, dict) or payload.get("schema_version") != _STATE_SCHEMA_VERSION:
        return state
    if isinstance(payload.get("receipt_key"), str):
        state["receipt_key"] = payload["receipt_key"]
    if payload.get("instance_id") != instance_id or not instance_id:
        return state
    for key in state:
        if key in payload:
            state[key] = payload[key]
    if not isinstance(state["cleanup"], list):
        state["cleanup"] = []
    if not isinstance(state["quiet"], list):
        state["quiet"] = []
    return state


def _save_state(state: Mapping[str, Any]) -> None:
    write_atomic(_state_path(), json.dumps(state, indent=2, sort_keys=True))


class _PairingChanged(Exception):
    """This computer is now paired as another instance; the work in hand is not its work."""


def _require_instance(instance_id: str) -> None:
    current = current_binding()
    if current is None:
        raise OSError("instance address: local pairing temporarily unreadable")
    if current.instance_id != instance_id:
        raise _PairingChanged()


def _update_state(instance_id: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _LOCK:
        # The state file is one per computer. Work begun for one instance must
        # never write over the state of the instance this computer is paired as
        # now, however late that work comes back from a wait.
        _require_instance(instance_id)
        state = _load_state(instance_id)
        mutate(state)
        _save_state(state)
        return state


# --- receipts -----------------------------------------------------------------


def _receipt_key() -> bytes:
    # A reset in a separate process deletes this file. Do not retain its key
    # across requests; a device snapshot reads it once before its DB transaction.
    with _LOCK:
        binding = current_binding()
        instance_id = binding.instance_id if binding else ""
        state = _load_state(instance_id)
        encoded = state.get("receipt_key")
        if isinstance(encoded, str):
            try:
                key = base64.urlsafe_b64decode(encoded.encode("ascii"))
                if len(key) == 32:
                    return key
            except (ValueError, UnicodeEncodeError):
                pass
        key = secrets.token_bytes(32)
        state["receipt_key"] = base64.urlsafe_b64encode(key).decode("ascii")
        _save_state(state)
        return key


def receipt_for_session(session_id: str) -> str:
    """The keyed one-way value a browser posts back after clearing (rule F2)."""

    return _receipt_with_key(_receipt_key(), session_id)


def _receipt_with_key(key: bytes, session_id: str) -> str:
    digest = hmac.new(key, _RECEIPT_CONTEXT + session_id.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def receipt_hash(receipt: str) -> str:
    return hashlib.sha256(receipt.encode("utf-8")).hexdigest()


_RECEIPT_SHAPE = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")


def _session_id_from_cookie(cookie_value: str | None) -> str | None:
    """The session id a cookie names, read without verifying anything.

    The id is 192 random bits: presenting it is the proof. It selects only the
    presenter's own row in a list, never access.
    """

    from urllib.parse import unquote

    from vibe import remote_access

    if not cookie_value or "." not in cookie_value:
        return None
    if len(cookie_value.encode("utf-8", "ignore")) > remote_access.SESSION_COOKIE_MAX_VALUE_BYTES:
        return None
    try:
        payload = json.loads(unquote(cookie_value.rsplit(".", 1)[0]))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("browser_session_id")
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", session_id):
        return None
    return session_id


def receipt_for_cleanup_request(host: str, session_cookie: str | None) -> str | None:
    """The receipt to embed in a cleaning page, when the cookie names a listed session."""

    session_id = _session_id_from_cookie(session_cookie)
    binding = current_binding()
    if session_id is None or binding is None or not binding.instance_id:
        return None
    receipt = receipt_for_session(session_id)
    try:
        from storage import instance_address_clearing
        from storage.db import get_cached_sqlite_engine

        with get_cached_sqlite_engine().connect() as conn:
            listed = instance_address_clearing.receipt_is_listed(
                conn, instance_id=binding.instance_id, hostname=host, receipt_hash=receipt_hash(receipt)
            )
    except Exception:
        logger.debug("instance address: receipt lookup failed", exc_info=True)
        return None
    return receipt if listed else None


def record_cleared(host: str, receipt: str) -> bool:
    """Mark the receipt's own device cleared. Unknown and repeated receipts change nothing."""

    if not _RECEIPT_SHAPE.fullmatch(receipt or ""):
        return False
    binding = current_binding()
    if binding is None or not binding.instance_id:
        return False
    try:
        from storage import instance_address_clearing
        from storage.db import get_cached_sqlite_engine

        with get_cached_sqlite_engine().begin() as conn:
            return instance_address_clearing.mark_cleared(
                conn,
                instance_id=binding.instance_id,
                hostname=host,
                receipt_hash=receipt_hash(receipt),
                now=int(_wall()),
            )
    except Exception:
        logger.warning("instance address: could not record a clearing receipt", exc_info=True)
        return False


# --- cleanup authority (memory only) ----------------------------------------------


@dataclass
class _Authority:
    pairing: tuple[str, str]
    generation: int
    hosts: dict[str, float | None]
    started_monotonic: float
    started_wall: float


_authority: _Authority | None = None
_authority_epoch = 0  # raised whenever authority is dropped; a late read cannot restore it
_quiet: set[str] = set()
_quiet_loaded_for: str | None = None
_inflight: dict[str, list[IO[str]]] = {}
_last_read_started: float | None = None
_last_read_failed: float | None = None
_read_refused = False  # the last read got a definitive refusal from avibe.bot
# avibe.bot's answer that it will not tell this pairing the address state, nor
# act on its renames, until remote access is bound again: (pairing, code). While
# it stands for the pairing in use nothing asks again by itself.
_STANDING_REFUSALS = frozenset({"rebind_required"})
_standing_refusal: tuple[tuple[str, str], str] | None = None
# What the last state read was refused with, when the page has a word for it:
# (pairing, HTTP status, code), as a rename refused the same way reaches the
# page. Unlike a standing refusal it stops nothing from asking again.
_READ_PAGE_ERRORS: dict[str, tuple[int, str]] = {
    "invalid_device_secret": (409, "pairing_invalid"),
    "forbidden": (409, "rename_ineligible"),
    "rename_ineligible": (409, "rename_ineligible"),
}
_read_refusal: tuple[tuple[str, str], int, str] | None = None
# ``ineligible_reason`` while avibe.bot has never answered a state read for this
# instance: an avibe.bot that does not have the address endpoints yet, or one
# that has not been reached since pairing. Not a switch: a fact, read from the
# stored state, that ends by itself with the first state read that succeeds.
STATE_UNKNOWN_REASON = "address_state_unknown"
# The pairing whose latest state read was answered 404: avibe.bot does not have
# the address endpoints now (not yet, or no longer, as after a rollback). Until a
# read succeeds the address is treated as never answered. In memory only: every
# opening of the page and every start reads first, and the stored state, with its
# list of old names, is left as it is.
_endpoint_absent: tuple[str, str] | None = None
_known_cleanup: tuple[str, frozenset[str]] | None = None
_read_lock = threading.Lock()
_retired_at_flip: dict[str, str] = {}  # old host -> new origin, for streams open at the switch


def _parse_instant(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _quiet_for(journal: Any) -> set[str]:
    """The quiet set is a function of the journal: the name an unfinished change
    is taking back, and nothing else. It is written only together with the
    journal, so no crash can leave a name quiet with nothing left to end it."""

    if isinstance(journal, dict) and journal.get("quieted") and is_managed_name(journal.get("target_hostname")):
        return {journal["target_hostname"]}
    return set()


def _load_quiet(binding: Binding) -> None:
    global _quiet, _quiet_loaded_for

    if _quiet_loaded_for == binding.instance_id:
        return
    _quiet = _quiet_for(_load_state(binding.instance_id).get("journal"))
    _quiet_loaded_for = binding.instance_id


def _drop_authority() -> None:
    global _authority, _authority_epoch

    with _LOCK:
        _authority = None
        _authority_epoch += 1


def _cleanup_lease_path() -> Path:
    from vibe.binding_admission import publication_lease_path
    return publication_lease_path()


def cleanup_publication_lock():
    from storage.lock import MigrationFileLock
    return MigrationFileLock(_cleanup_lease_path(), timeout_seconds=_DRAIN_TIMEOUT_SECONDS)


def drain_for_reset() -> None:
    """Called after sealing reset. No new cleaning headers may pass the fence."""
    from vibe.binding_admission import reset_in_progress
    if not reset_in_progress():
        raise RuntimeError('reset intent required')
    with cleanup_publication_lock():
        pass  # marker remains durable after all earlier senders have finished


def _note_standing_refusal(binding: Binding, code: str) -> None:
    """avibe.bot named a refusal that only a new binding of remote access ends."""

    global _standing_refusal, _read_refusal

    # No permission is given, so none is held, and no reply still on its way restores one.
    _drop_authority()
    with _LOCK:
        _standing_refusal = (binding.pairing, code)
        _read_refusal = None


def _standing_refusal_for(binding: Binding | None) -> str | None:
    standing = _standing_refusal
    return standing[1] if standing is not None and binding is not None and standing[0] == binding.pairing else None


def _read_refusal_for(binding: Binding | None) -> tuple[int, str] | None:
    refusal = _read_refusal
    return refusal[1:] if refusal is not None and binding is not None and refusal[0] == binding.pairing else None


def _cleanup_allowed_locked(host: str, binding: Binding) -> bool:
    from vibe.binding_admission import reset_in_progress
    if reset_in_progress():
        return False
    authority = _authority
    if authority is None or not binding.paired or authority.pairing != binding.pairing:
        return False
    if host == binding.active_host or host not in authority.hosts:
        return False
    _load_quiet(binding)
    if host in _quiet:
        return False
    now_wall = _wall()
    wall_elapsed = now_wall - authority.started_wall
    if _monotonic() - authority.started_monotonic > CLEANUP_AUTHORITY_SECONDS:
        return False
    if wall_elapsed < 0 or wall_elapsed > CLEANUP_AUTHORITY_SECONDS:
        return False
    release_at = authority.hosts[host]
    return release_at is None or now_wall < release_at


def decide(raw_host: str | None, forwarded_host: str | None = None) -> str:
    """``"pass"``, ``"refuse"`` or ``"cleanup"`` (still to be confirmed) for a request.

    Content is refused when the request's own Host or a forwarded Host is a
    managed name that is not the active one. Cleaning is considered for the
    request's own Host only.
    """

    host = normalize_host(raw_host)
    forwarded = normalize_host(forwarded_host) if forwarded_host else ""
    host_managed = is_managed_name(host)
    forwarded_managed = is_managed_name(forwarded)
    if not host_managed and not forwarded_managed:
        return "pass"
    binding = current_binding()
    active = binding.active_host if binding is not None else ""
    if forwarded_managed and forwarded != active:
        return "refuse"
    if not host_managed or (active and host == active):
        return "pass"
    if binding is None or not binding.paired or forwarded:
        return "refuse"
    return "cleanup"


def cleanup_ready(raw_host: str | None) -> bool:
    """Whether the permission held right now covers this Host. No file or network read."""

    host = normalize_host(raw_host)
    binding = current_binding()
    if binding is None or not binding.paired or not is_managed_name(host) or host == binding.active_host:
        return False
    with _LOCK:
        return _cleanup_allowed_locked(host, binding)


def cleanup_read_worthwhile(raw_host: str | None) -> bool:
    """Whether reading the state now could let this Host be cleaned.

    Only for a name avibe.bot last listed for this instance (or before any read
    in this process), and not for a quiet one. How often a read really happens
    is ``refresh_state``'s business.
    """

    global _known_cleanup

    host = normalize_host(raw_host)
    binding = current_binding()
    if binding is None or not binding.paired or not is_managed_name(host) or host == binding.active_host:
        return False
    with _LOCK:
        _load_quiet(binding)
        if host in _quiet:
            return False
        if _known_cleanup is None or _known_cleanup[0] != binding.instance_id:
            _known_cleanup = (
                binding.instance_id,
                frozenset(
                    entry.get("hostname")
                    for entry in _load_state(binding.instance_id).get("cleanup", [])
                    if isinstance(entry, dict)
                ),
            )
        # A name never listed is worth a read only before any read in this process.
        return host in _known_cleanup[1] or _last_read_started is None


def _read_too_recent() -> bool:
    last = _last_read_started
    if _last_read_failed is not None and (last is None or _last_read_failed > last):
        last = _last_read_failed  # after a failure the wait runs from its end
    return last is not None and _monotonic() - last < STATE_READ_MIN_INTERVAL_SECONDS


def begin_cleanup_response(raw_host: str | None) -> bool | None:
    """The last check, under the role lock, immediately before headers are written.

    Called on the event loop, so it never waits. ``None`` means the role lock is
    held right now (a state is being applied, a role is changing): the caller
    may ask again in a moment, and answers with nothing if it stays so.
    """

    host = normalize_host(raw_host)
    binding = current_binding()
    if binding is None:
        return False
    if not _LOCK.acquire(blocking=False):
        return None
    try:
        if not _cleanup_allowed_locked(host, binding):
            return False
        # Register before the final generation/fence check. Once we release
        # this lease a writer can publish, even if its process is not ours.
        from storage.lock import try_shared_file_lease
        from vibe import binding_health
        lease = try_shared_file_lease(_cleanup_lease_path())
        if lease is None:
            return None
        try:
            if (binding != current_binding() or not _cleanup_allowed_locked(host, binding)
                    or binding_health.rejected_at_epoch(binding.epoch) is not None):
                return False
            with _DRAINED:
                _inflight.setdefault(host, []).append(lease)
                lease = None
            return True
        finally:
            if lease is not None:
                lease.close()
    finally:
        _LOCK.release()


def end_cleanup_response(raw_host: str | None) -> None:
    host = normalize_host(raw_host)
    with _DRAINED:
        leases = _inflight.get(host)
        if leases:
            leases.pop().close()
            if not leases:
                del _inflight[host]
        _DRAINED.notify_all()


def _quiet_and_drain(binding: Binding, journal: dict[str, Any]) -> dict[str, Any]:
    """Stop cleaning the journal's target and wait for cleaning responses already started on it."""

    host = journal["target_hostname"]
    if not journal.get("quieted"):
        journal = {**journal, "quieted": True}
        _set_journal(binding, journal)
    with _DRAINED:
        deadline = _monotonic() + _DRAIN_TIMEOUT_SECONDS
        while _inflight.get(host):
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise RuntimeError("cleanup responses did not drain")
            _DRAINED.wait(timeout=min(remaining, 0.5))
    return journal


def retired_destination(stream_host: str | None) -> str | None:
    """For a stream admitted on a Host that was active until the switch: the new origin."""

    destination = _retired_at_flip.get(normalize_host(stream_host))
    if destination is None:
        return None  # the common case, answered without a lock or a config read
    binding = current_binding()
    if binding is None or normalize_host(stream_host) == binding.active_host:
        return None
    return destination


# --- reading the state from avibe.bot ---------------------------------------------


def _validated_state(binding: Binding, payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("instance_id") != binding.instance_id:
        return None
    generation = payload.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        return None
    active = normalize_host(payload.get("active_hostname")) if isinstance(payload.get("active_hostname"), str) else ""
    if not is_managed_name(active):
        return None
    raw_cleanup = payload.get("cleanup_hostnames")
    if not isinstance(raw_cleanup, list) or len(raw_cleanup) > 64:
        return None
    cleanup: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw_cleanup:
        if not isinstance(entry, dict) or not isinstance(entry.get("hostname"), str):
            return None
        hostname = normalize_host(entry["hostname"])
        release_at = entry.get("release_at")
        if not is_managed_name(hostname) or hostname == active or hostname in seen:
            return None  # overlap or a malformed list is an unreadable state
        if release_at is not None and _parse_instant(release_at) is None:
            return None
        seen.add(hostname)
        cleanup.append({"hostname": hostname, "release_at": release_at})
    delay = payload.get("release_delay_seconds")
    estimate = payload.get("active_release_at_if_renamed_now")
    client_id = payload.get("oidc_client_id")
    operation = payload.get("last_rename_operation_id")
    return {
        "generation": generation,
        "active_hostname": active,
        "cleanup": cleanup,
        "release_delay_seconds": delay if isinstance(delay, int) and not isinstance(delay, bool) and delay > 0 else None,
        "active_release_at_if_renamed_now": estimate if _parse_instant(estimate) is not None else None,
        "oidc_client_id": client_id if isinstance(client_id, str) and client_id else None,
        "last_rename_operation_id": operation if isinstance(operation, str) and operation else None,
    }


@dataclass
class _StateRead:
    binding: Binding
    state: dict[str, Any]
    started_monotonic: float
    started_wall: float
    epoch: int


def _read_is_stale_locked(read: _StateRead) -> bool:
    """Under the role lock: whether this read may no longer set roles or grant cleaning.

    Stale when authority was dropped since it started (a ``401``), when the
    pairing changed, or when a newer generation is already recorded. Checked at
    every point a read is applied, however long ago it was taken.
    """

    from vibe.binding_health import rejected_at_epoch
    if read.epoch != _authority_epoch or rejected_at_epoch(read.binding.epoch) is not None:
        return True
    current = current_binding()
    if current is None or current.pairing != read.binding.pairing:
        return True
    stored_generation = _load_state(read.binding.instance_id).get("generation")
    return isinstance(stored_generation, int) and read.state["generation"] < stored_generation


def _read_state_once(binding: Binding, *, again: bool = False) -> _StateRead | None:
    """One read of ``hostname-state``. ``None`` when it gave no usable state.

    While a standing refusal holds for this pairing no request is made: the
    answer would be the same until remote access is bound again, which makes
    another pairing. ``again`` is for a person who asks now.
    """

    global _read_refused, _standing_refusal, _read_refusal, _endpoint_absent

    from vibe import remote_access

    if not again and _standing_refusal_for(binding) is not None:
        return None
    with _LOCK:
        epoch = _authority_epoch
    started_monotonic, started_wall = _monotonic(), _wall()

    def latest() -> bool:
        # Under the role lock: no refusal was noted and no permission dropped
        # since this request left, and the binding is the one it was made for.
        # An answer that is not the latest changes nothing that is remembered.
        return epoch == _authority_epoch and current_binding() == binding

    try:
        payload = remote_access._device_json_request(_load_config(), "POST", "hostname-state", {}, timeout=8.0)
    except remote_access.BackendRequestError as exc:
        # A definitive answer (not a throttle, not a server fault): asking again
        # soon would get the same one.
        definitive = 400 <= exc.status < 500 and exc.status != 429
        _read_refused = definitive
        code = exc.payload.get("error") if isinstance(exc.payload, dict) else None
        if not isinstance(code, str):
            code = ""  # an object or a list where the code belongs names nothing
        with _LOCK:
            if latest():
                if definitive:
                    # Another definitive answer takes a standing refusal's place;
                    # a throttle or a server fault says nothing about it.
                    _standing_refusal = None
                    _endpoint_absent = binding.pairing if exc.status == 404 else None
                worded = _READ_PAGE_ERRORS.get(code) if definitive else None
                _read_refusal = (binding.pairing, *worded) if worded is not None else None
        if exc.status == 401:
            # The binding is no longer current: no cleaning anywhere, and no
            # reply still on its way may bring it back. Only this status means
            # so: an endpoint avibe.bot does not have answers 404.
            _drop_authority()
        if exc.status == 409 and code in _STANDING_REFUSALS:
            _note_standing_refusal(binding, code)
        return None
    except Exception:
        _read_refused = False
        with _LOCK:
            if latest():
                _read_refusal = None  # no answer says nothing, also not about a refusal that stands
        return None
    _read_refused = False
    state = _validated_state(binding, payload)
    with _LOCK:
        if not latest():
            return None
        _read_refusal = None
        _endpoint_absent = None  # the endpoint answered, whatever the shape of its answer
        if state is None:
            return None
        _standing_refusal = None
    return _StateRead(binding, state, started_monotonic, started_wall, epoch)


def refresh_state(*, force: bool = False, wait: bool = True, again: bool = False) -> bool:
    """Read the state unless one was read in the last few seconds; apply it.

    One read at a time. ``wait=False`` returns at once when a read is in flight
    instead of queueing behind it. After a failed read the interval runs from
    the failure's end, so slow failures are not retried back to back. ``again``
    asks even while a standing refusal holds (see ``_read_state_once``).
    """

    global _last_read_started, _last_read_failed

    binding = current_binding()
    if binding is None or not binding.paired or not is_managed_name(binding.active_host):
        return False
    if not _read_lock.acquire(blocking=wait):
        return False
    try:
        with _LOCK:
            if not force and _read_too_recent():
                return False
            _last_read_started = _monotonic()
        read = _read_state_once(binding, again=True) if again else _read_state_once(binding)
        if read is None:
            with _LOCK:
                _last_read_failed = _monotonic()
            return False
        return _apply_state(read)
    finally:
        _read_lock.release()


def _apply_state(read: _StateRead) -> bool:
    try:
        with admitted('address'):
            return _apply_admitted_state(read)
    except OperationBusy:
        return False


def _apply_admitted_state(read: _StateRead) -> bool:
    global _authority

    global _known_cleanup

    binding, state = read.binding, read.state
    prune = False
    with _LOCK:
        if _read_is_stale_locked(read):
            return False  # an older or disowned reply; roles stay as they are
        stored = _load_state(binding.instance_id)
        # Asked of the list as it stood before this state replaces it.
        was_cleanup = any(
            isinstance(entry, dict) and entry.get("hostname") == state["active_hostname"]
            for entry in stored.get("cleanup", [])
        )
        _known_cleanup = (binding.instance_id, frozenset(entry["hostname"] for entry in state["cleanup"]))

        def record(target: dict[str, Any]) -> None:
            target["generation"] = state["generation"]
            target["cleanup"] = state["cleanup"]
            target["release_delay_seconds"] = state["release_delay_seconds"]
            target["active_release_at_if_renamed_now"] = state["active_release_at_if_renamed_now"]
            target["state_read_at"] = int(read.started_wall)

        _update_state(binding.instance_id, record)
        if state["active_hostname"] == binding.active_host:
            _load_quiet(binding)
            _authority = _Authority(
                pairing=binding.pairing,
                generation=state["generation"],
                hosts={entry["hostname"]: _parse_instant(entry["release_at"]) for entry in state["cleanup"]},
                started_monotonic=read.started_monotonic,
                started_wall=read.started_wall,
            )
            prune = True
        else:
            # avibe.bot names another active name than this computer does.
            _authority = None
            if was_cleanup:
                # A name coming back, in whatever order the journal learns of it,
                # goes through quiet, drain and settle (rule B3).
                _update_state(
                    binding.instance_id,
                    lambda target: target.__setitem__("returning", state["active_hostname"]),
                )
            _note_other_active_name(binding, stored, state, was_cleanup)
    if prune:
        # Outside the role lock: the event loop and every request take that lock.
        # The active name's own list is kept: a switch in progress has just
        # written it for the name about to retire, which this state, read
        # before the switch, cannot list yet.
        _forget_closed_lists(
            binding, {"cleanup": [*state["cleanup"], {"hostname": binding.active_host}]}
        )
    return True


def _note_other_active_name(
    binding: Binding, stored: Mapping[str, Any], state: Mapping[str, Any], was_cleanup: bool
) -> None:
    """Under the role lock: journal a change made elsewhere, or let our own request settle it."""

    journal = stored.get("journal")
    if isinstance(journal, dict) and journal.get("origin") == "local" and journal.get("phase") == "intent":
        if not isinstance(journal.get("held"), dict):
            # Our own request is still unanswered here; its replay settles it.
            _wake_worker()
            return
        target = journal.get("target_hostname")
        if state["active_hostname"] == target and state["last_rename_operation_id"] == journal.get("operation_id"):
            # avibe.bot names this operation as the one that made the change: the
            # address is ours to take. Not the permission to delete Health data:
            # that needs the generation the operation was committed at (rule A5),
            # which only its receipt carries, and the current one is no proof of
            # it (a later operation may have raised it). Without it, nothing is
            # deleted.
            release = next((entry.get("release_at") for entry in state["cleanup"]
                            if entry.get("hostname") == journal.get("previous_hostname")), None)
            _set_journal(binding, {
                **{key: value for key, value in journal.items() if key != "held"},
                "phase": "committed",
                "receipt": {
                    "committed_generation": None,
                    "previous_hostname": journal.get("previous_hostname"),
                    "hostname": target,
                    "previous_release_at": release if _parse_instant(release) is not None else None,
                    "oidc_client_id": state.get("oidc_client_id"),
                },
            })
        else:
            # Another change: adopted as one made elsewhere, which deletes nothing.
            _set_journal(binding, None)
            journal = None
    if not isinstance(journal, dict):
        _update_state(
            binding.instance_id,
            lambda target: target.__setitem__(
                "journal",
                {
                    "origin": "elsewhere",
                    "phase": "committed",
                    "operation_id": state["last_rename_operation_id"],
                    "previous_hostname": binding.active_host,
                    "target_hostname": state["active_hostname"],
                    # A name coming back goes through quiet, drain and settle (rule B3).
                    "is_return": was_cleanup,
                    "created_at": int(_wall()),
                    "steps": {},
                },
            ),
        )
    _wake_worker()


def _forget_closed_lists(binding: Binding, state: Mapping[str, Any]) -> None:
    try:
        from storage import instance_address_clearing
        from storage.db import get_cached_sqlite_engine

        with get_cached_sqlite_engine().begin() as conn:
            instance_address_clearing.forget_except(
                conn,
                instance_id=binding.instance_id,
                keep_hostnames=[entry["hostname"] for entry in state["cleanup"]],
            )
    except Exception:
        logger.debug("instance address: could not prune clearing lists", exc_info=True)


def note_generation_hint(value: Any) -> None:
    """A ``hostname_generation`` seen on another reply: a reason to read, never a permission."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return
    binding = current_binding()
    if binding is None or not binding.paired:
        return
    stored = _load_state(binding.instance_id).get("generation")
    if stored != value:
        _wake_worker(read_state=True)


# --- the operation ------------------------------------------------------------------


_confirmations: dict[str, dict[str, Any]] = {}
_worker_wake = threading.Event()
_worker_read_requested = threading.Event()
_worker_started = False
_worker_guard = threading.Lock()
_first_answer = threading.Condition(_LOCK)
_hooks: dict[str, Callable[..., Any]] = {}


def _eligibility(binding: Binding | None) -> str | None:
    if binding is None or not binding.paired:
        return "not_paired"
    if binding.instance_kind != "personal":
        return "not_personal"
    if not is_managed_name(binding.active_host):
        return "address_not_managed"
    return None


def _state_ever_read(binding: Binding) -> bool:
    """Whether avibe.bot has answered a state read for this instance (stored, so it
    survives a restart) and has the endpoint now (its latest read was not a 404)."""

    if _endpoint_absent is not None and _endpoint_absent == binding.pairing:
        return False
    return isinstance(_load_state(binding.instance_id).get("state_read_at"), int)


def _device_rows(binding: Binding) -> list[dict[str, Any]]:
    """The devices a change signs out: the registry's Owner view, every account."""

    from storage import device_registry
    from vibe import remote_access

    config = _load_config()
    rows = device_registry.list_devices(
        remote_access.device_binding(config), None, owner=True, caller=None, now=int(_wall())
    )
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "display_name": row["display_name"],
            "account": {"email": row["account"]["email"]} if row.get("account") else None,
            "last_active_at": row["last_active_at"],
            "push_only": bool(row.get("push_only")),
        }
        for row in rows
    ]


def _health_connected() -> bool:
    try:
        from storage.mobile_store import MobileStore

        store = MobileStore(existing_only=True)
    except Exception:
        return False
    try:
        with store.transaction() as conn:
            return conn.execute("SELECT 1 FROM devices WHERE revoked_at IS NULL LIMIT 1").fetchone() is not None
    except Exception:
        return False


def _hold_days(state: Mapping[str, Any]) -> int | None:
    delay = state.get("release_delay_seconds")
    return max(1, round(delay / 86400)) if isinstance(delay, int) and delay > 0 else None


_held_checked_at: float | None = None


def _check_held(binding: Binding, *, force: bool = False) -> bool:
    """One read of the state for a held change, asked for by the Owner; past a
    standing refusal, but at most once a minute unless ``force``. Whether it read."""

    global _held_checked_at

    now = _monotonic()
    with _LOCK:
        if not force and _held_checked_at is not None and now - _held_checked_at < DISPLAY_REFRESH_SECONDS:
            return False
        _held_checked_at = now
    refresh_state(force=True, again=True)
    return True


def preview(prefix: Any, actor: tuple[str, ...]) -> dict[str, Any]:
    """First of the two requests: what a change to ``prefix`` would do (rule A2)."""

    binding = current_binding()
    reason = _eligibility(binding)
    if reason is not None or binding is None:
        raise AddressError(409, "rename_ineligible", reason=reason)
    if not _state_ever_read(binding):
        # avibe.bot has not answered for this address yet: nothing is asked of it here.
        raise AddressError(409, "rename_ineligible", reason=STATE_UNKNOWN_REASON)
    if not _valid_prefix(prefix):
        raise AddressError(400, "invalid_prefix")
    target = hostname_for_prefix(prefix)
    if target == binding.active_host:
        raise AddressError(409, "same_address")
    journal = _load_state(binding.instance_id).get("journal")
    if isinstance(journal, dict) and isinstance(journal.get("held"), dict):
        # A held change: the Owner asking is one read of the state, also past a
        # standing refusal, which may show what became of it. Read only: nothing
        # is sent again and no refusal is set aside.
        _check_held(binding, force=True)
        journal = _load_state(binding.instance_id).get("journal")
    if isinstance(journal, dict):
        raise AddressError(409, "operation_in_progress")
    # The Owner asks now: one read, also when avibe.bot's last word was a standing refusal.
    refresh_state(force=True, again=True)
    if not _state_ever_read(binding):
        raise AddressError(409, "rename_ineligible", reason=STATE_UNKNOWN_REASON)
    refusal = _standing_refusal_for(binding)
    if refusal is not None:
        raise AddressError(409, refusal)
    worded = _read_refusal_for(binding)
    if worded is not None:
        # Refused as a rename would be: the same code, the same way out.
        raise AddressError(*worded)
    state = _load_state(binding.instance_id)
    generation = state.get("generation")
    read_at = state.get("state_read_at")
    if not isinstance(generation, int) or not isinstance(read_at, int) or _wall() - read_at > DISPLAY_REFRESH_SECONDS:
        raise AddressError(503, "unavailable")
    if isinstance(state.get("journal"), dict) or current_binding() != binding:
        raise AddressError(409, "address_changed")
    confirmation_id = secrets.token_urlsafe(32)
    expires_at = _wall() + CONFIRMATION_TTL_SECONDS
    with _LOCK:
        for known, entry in list(_confirmations.items()):
            if entry["actor"] == actor or entry["expires_monotonic"] <= _monotonic():
                _confirmations.pop(known, None)
        _confirmations[confirmation_id] = {
            "actor": actor,
            "prefix": prefix,
            "target": target,
            "generation": generation,
            "active": binding.active_host,
            "pairing": binding.pairing,
            "expires_monotonic": _monotonic() + CONFIRMATION_TTL_SECONDS,
        }
    return {
        "ok": True,
        "confirmation_id": confirmation_id,
        "expires_at": int(expires_at),
        "hostname": target,
        "is_return": any(entry.get("hostname") == target for entry in state.get("cleanup", [])),
        "hold_days": _hold_days(state),
        "release_at_if_changed_now": state.get("active_release_at_if_renamed_now"),
        "devices": _device_rows(binding),
        "health_connected": _health_connected(),
    }


def _admit_change(confirmation_id: Any, actor: tuple[str, ...]) -> tuple[Binding, str]:
    binding = current_binding()
    reason = _eligibility(binding)
    if reason is not None or binding is None:
        raise AddressError(409, "rename_ineligible", reason=reason)
    if not _state_ever_read(binding):
        raise AddressError(409, "rename_ineligible", reason=STATE_UNKNOWN_REASON)
    with _LOCK:
        entry = _confirmations.pop(confirmation_id, None) if isinstance(confirmation_id, str) else None
        if entry is None or entry["actor"] != actor or entry["expires_monotonic"] <= _monotonic():
            raise AddressError(409, "confirmation_expired")
        if current_binding() != binding:
            raise AddressError(409, "address_changed")
        state = _load_state(binding.instance_id)
        if isinstance(state.get("journal"), dict):
            raise AddressError(409, "operation_in_progress")
        if (
            entry["pairing"] != binding.pairing
            or entry["active"] != binding.active_host
            or entry["generation"] != state.get("generation")
        ):
            raise AddressError(409, "address_changed")
        # avibe.bot accepts only a UUID here (its ``renameTuple``); a repeat sends
        # this same id from the journal, so it is made once, with the intent.
        operation_id = str(uuid.uuid4())
        journal = {
            "origin": "local",
            "phase": "intent",
            "operation_id": operation_id,
            "expected_generation": entry["generation"],
            "expected_hostname": binding.active_host,
            "new_prefix": entry["prefix"],
            "previous_hostname": binding.active_host,
            "target_hostname": entry["target"],
            "is_return": any(item.get("hostname") == entry["target"] for item in state.get("cleanup", [])),
            "confirmed_by": actor[0],
            "created_at": int(_wall()),
            "steps": {},
        }
        state["journal"] = journal
        state["last_operation"] = None
        _save_state(state)
    return binding, operation_id


def change(confirmation_id: Any, actor: tuple[str, ...]) -> tuple[int, dict[str, Any]]:
    try:
        with admitted('address'):
            binding, operation_id = _admit_change(confirmation_id, actor)
    except OperationBusy:
        raise AddressError(409, 'operation_in_progress') from None
    # The durable journal now owns admission. Release the OS lock before waking
    # the worker or waiting for it: the worker needs that same lock to proceed.
    _wake_worker()
    deadline = _monotonic() + _FIRST_ANSWER_WAIT_SECONDS
    with _first_answer:
        while _monotonic() < deadline:
            state = _load_state(binding.instance_id)
            journal = state.get("journal")
            last = state.get("last_operation")
            if isinstance(last, dict) and last.get("operation_id") == operation_id:
                if last.get("state") == "failed":
                    extra = {k: v for k, v in last.items() if k not in {"operation_id", "state", "error", "status"}}
                    raise AddressError(int(last.get("status") or 409), str(last.get("error") or "unavailable"), **extra)
                return 200, {"ok": True, "state": "applied", "hostname": last.get("hostname"), "public_url": last.get("public_url")}
            if isinstance(journal, dict) and (journal.get("phase") == "committed" or _accepted(journal)):
                # avibe.bot has this operation in hand ("pending"), or has committed it.
                return 202, {"ok": True, "state": "applying"}
            _first_answer.wait(timeout=0.25)
    return 202, {"ok": True, "state": "pending"}


def describe() -> dict[str, Any]:
    """What the settings page shows: the address, the operation, and the old names' lists."""

    binding = current_binding()
    reason = _eligibility(binding)
    if binding is None:
        return {"ok": True, "hostname": None, "public_url": None, "eligible": False, "ineligible_reason": reason,
                "hold_days": None, "release_at_if_changed_now": None, "operation": None, "state_error": None,
                "unrecognized_change": None, "cleanup": [], "health": {"connected": False, "cleanup": "none"}}
    state = _load_state(binding.instance_id)
    read_at = state.get("state_read_at")
    # One way to read per opening of the page, never two.
    if isinstance(state.get("journal"), dict) and isinstance(state["journal"].get("held"), dict):
        # The Owner looking at a held change checks it, at most once a minute.
        if _check_held(binding):
            state = _load_state(binding.instance_id)
    elif reason is None and (not isinstance(read_at, int) or _wall() - read_at > DISPLAY_REFRESH_SECONDS):
        # Also while the state was never read: opening the page asks again (no
        # more often than the read interval), so the page follows avibe.bot
        # without a restart once it answers.
        refresh_state()
        state = _load_state(binding.instance_id)
    if reason is None and not _state_ever_read(binding):
        reason = STATE_UNKNOWN_REASON
    journal = state.get("journal")
    operation = None
    if isinstance(journal, dict):
        operation = {
            "state": "pending" if journal.get("phase") == "intent" and not _accepted(journal) else "applying",
            "hostname": journal.get("target_hostname"),
            "error": None,
        }
        held = journal.get("held")
        if journal.get("phase") == "intent" and isinstance(held, dict):
            # Refused after avibe.bot may already have taken the request: not
            # failed, not sent again, waiting for the state to say what became of it.
            operation.update({"error": held.get("error"), "unconfirmed": True})
    elif isinstance(state.get("last_operation"), dict) and state["last_operation"].get("state") == "failed":
        last = state["last_operation"]
        operation = {key: value for key, value in last.items() if key not in {"operation_id", "status"}}
    names = [entry["hostname"] for entry in state.get("cleanup", []) if isinstance(entry, dict)]
    lists: dict[str, list[dict]] = {name: [] for name in names}
    try:
        from storage import instance_address_clearing
        from storage.db import get_cached_sqlite_engine

        with get_cached_sqlite_engine().connect() as conn:
            lists = instance_address_clearing.list_for(conn, instance_id=binding.instance_id, hostnames=names)
    except Exception:
        logger.debug("instance address: clearing lists unavailable", exc_info=True)
    cleanup = []
    for entry in state.get("cleanup", []):
        if not isinstance(entry, dict):
            continue
        devices = lists.get(entry["hostname"], [])
        retired_at = min((device["retired_at"] for device in devices), default=None)
        cleanup.append(
            {
                "hostname": entry["hostname"],
                "retired_at": retired_at,
                "release_at": entry.get("release_at"),
                # Never complete: a browser whose sign-in lapsed before the
                # change, or older than the device registry, is in no list.
                "complete": False,
                "incomplete_reason": "sessions_expired_before_rename_not_listed",
                "unconfirmed": sum(1 for device in devices if device["state"] == "unconfirmed"),
                "devices": [{k: v for k, v in device.items() if k != "retired_at"} for device in devices],
            }
        )
    health_pending = bool(isinstance(journal, dict) and journal.get("origin") == "local"
                          and "flip" in (journal.get("steps") or {}) and "health" not in (journal.get("steps") or {}))
    return {
        "ok": True,
        "hostname": binding.active_host or None,
        "public_url": f"https://{binding.active_host}" if binding.active_host else None,
        "eligible": reason is None,
        "ineligible_reason": reason,
        "hold_days": _hold_days(state),
        "release_at_if_changed_now": state.get("active_release_at_if_renamed_now"),
        "operation": operation,
        # A standing refusal of the address state: what is shown is as last
        # read, and no old name is cleaned, until remote access is bound again.
        "state_error": _standing_refusal_for(binding),
        "unrecognized_change": state.get("unrecognized_change"),
        "cleanup": cleanup,
        "health": {"connected": _health_connected(), "cleanup": "pending" if health_pending else "none"},
    }


# --- the worker: asks avibe.bot, then applies ----------------------------------------


def start(*, wake_pages: Callable[[], None], close_connections: Callable[[set[str]], None]) -> None:
    """Start the worker once per process; it resumes an unfinished journal first."""

    global _worker_started

    _hooks["wake_pages"] = wake_pages
    _hooks["close_connections"] = close_connections
    with _worker_guard:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker_loop, name="instance-address", daemon=True).start()
    _wake_worker(read_state=True)


def _wake_worker(*, read_state: bool = False) -> None:
    if read_state:
        _worker_read_requested.set()
    _worker_wake.set()


_OWED_READ_RETRY_SECONDS = 30.0
_OWED_READ_RETRY_MAX_SECONDS = 3600.0


def _worker_loop() -> None:
    delay = 0.0
    owed_delay = _OWED_READ_RETRY_SECONDS
    while True:
        _worker_wake.wait(timeout=delay if delay > 0 else None)
        _worker_wake.clear()
        try:
            if _read_when_asked():
                owed_delay = _OWED_READ_RETRY_SECONDS
                delay = _advance_operation()
            else:
                # Still owed. Network errors and server faults are asked again
                # soon; a definitive refusal (an endpoint that is not there yet,
                # a kind of instance it will never answer) ever more slowly.
                retry = owed_delay
                owed_delay = min(owed_delay * 2, _OWED_READ_RETRY_MAX_SECONDS) if _read_refused else _OWED_READ_RETRY_SECONDS
                delay = min(_advance_operation() or retry, retry)
        except _PairingChanged:
            delay = 0.0  # the journal belonged to another pairing; this one starts from its own state
        except Exception:
            logger.warning("instance address: operation step failed; it will be retried", exc_info=True)
            delay = 15.0


def _any_journal_stored() -> bool:
    try:
        payload = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("journal"), dict)


def _read_when_asked() -> bool:
    """Read the state if a read was asked for. ``False`` while one is still owed.

    The request stands until a read succeeds: a change seen while a journal was
    open, or a hint, must not be lost to one failed request, because nothing
    else would ask again once the generation is recorded.
    """

    if not _worker_read_requested.is_set():
        return True
    binding = current_binding()
    if _eligibility(binding) is not None and (binding is None or _journal(binding) is None):
        # Not a personal, paired instance on a managed name: it has no former
        # names and nothing to read for. A pairing or a hint asks again.
        _worker_read_requested.clear()
        return True
    if refresh_state(force=True):
        _worker_read_requested.clear()
        return True
    return False


def _accepted(journal: dict[str, Any]) -> bool:
    """avibe.bot answered "pending" to this operation and has not refused a repeat."""
    return bool(journal.get("accepted")) and not isinstance(journal.get("held"), dict)


def _journal(binding: Binding) -> dict[str, Any] | None:
    journal = _load_state(binding.instance_id).get("journal")
    return journal if isinstance(journal, dict) else None


def _set_journal(binding: Binding, journal: dict[str, Any] | None, *, last: dict[str, Any] | None = None) -> None:
    global _quiet, _quiet_loaded_for

    def mutate(state: dict[str, Any]) -> None:
        state["journal"] = journal
        state["quiet"] = sorted(_quiet_for(journal))
        if last is not None:
            state["last_operation"] = last

    with _first_answer:
        _update_state(binding.instance_id, mutate)
        _quiet, _quiet_loaded_for = _quiet_for(journal), binding.instance_id
        _first_answer.notify_all()


def _advance_operation() -> float:
    try:
        with admitted('address'):
            return _advance_admitted_operation()
    except OperationBusy:
        return 15.0 if _any_journal_stored() else 0.0


def _advance_admitted_operation() -> float:
    """Run the journal as far as it can go now; return seconds until the next try (0 = idle)."""

    binding = current_binding()
    if binding is None or not binding.paired:
        # An unfinished journal is looked at again; nothing else would wake it.
        return 30.0 if _any_journal_stored() else 0.0
    journal = _journal(binding)
    if journal is None:
        return 0.0
    if journal.get("origin") == "local" and journal.get("phase") == "intent":
        if isinstance(journal.get("held"), dict):
            return 0.0  # not sent again; a state read that shows the outcome resolves it
        return _request_change(binding, journal)
    return _apply_committed(binding, journal)


_PAGE_ERRORS: dict[str, tuple[int, str]] = {
    "invalid_prefix": (400, "invalid_prefix"),
    "taken": (409, "taken"),
    "name_holding_limit_reached": (409, "name_holding_limit_reached"),
    "rename_ineligible": (409, "rename_ineligible"),
    "forbidden": (409, "rename_ineligible"),
    "binding_changed": (409, "address_changed"),
    "operation_conflict": (409, "address_changed"),
    "invalid_device_secret": (409, "pairing_invalid"),
    # avibe.bot's record of this computer's tunnel does not match what the tunnel
    # really is. Definitive for this operation: it is settled here and not sent
    # again; the way out is to bind remote access again, then try again.
    "remote_access_configuration_mismatch": (409, "remote_access_configuration_mismatch"),
    # This computer's credential is valid, but avibe.bot holds more than one
    # pairing for the instance and will not choose one. Definitive in the same
    # way, with the same way out; the cause is another, so is the code.
    "rebind_required": (409, "rebind_required"),
}
_LIMIT_FIELDS = ("limit", "held_count", "earliest_release_at")
# A code avibe.bot sends that the page has no own word for is passed on as it is,
# when it looks like one; the page says the change was not made, with the code.
_WIRE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# The form avibe.bot's request rule takes for an operation id. An id made by an
# earlier version (a 43-character token) is refused by that rule before the
# rename is looked at, so such a refusal proves the request never landed.
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _request_change(binding: Binding, journal: dict[str, Any]) -> float:
    from vibe import remote_access

    target = journal["target_hostname"]
    if journal.get("is_return"):
        # Before asking for one of our own old names back: stop cleaning it and
        # let responses already started finish (rule B3). At every attempt: a
        # drain that timed out last time has not been waited for.
        journal = _quiet_and_drain(binding, journal)
    body = {
        "operation_id": journal["operation_id"],
        "expected_generation": journal["expected_generation"],
        "expected_hostname": journal["expected_hostname"],
        "new_prefix": journal["new_prefix"],
    }
    # Whether an earlier sending of this request may have been taken by avibe.bot:
    # it was answered "pending", or not answered for sure (no answer, a server
    # fault, an answer this contract does not know, a crash while it was out).
    # Recorded before the request leaves, so a crash counts as not knowing.
    landed_before = bool(journal.get("may_have_landed"))
    if not landed_before:
        journal = {**journal, "may_have_landed": True}
        _set_journal(binding, journal)
    try:
        reply = remote_access._device_json_request(_load_config(), "POST", "hostname-renames", body, timeout=20.0)
    except remote_access.BackendRequestError as exc:
        status, payload = exc.status, exc.payload if isinstance(exc.payload, dict) else {}
        if status == 429 or status >= 500:
            if status == 429 and not landed_before:
                # Turned away before it was looked at: this sending did not land.
                journal = {**journal, "may_have_landed": False}
                _set_journal(binding, journal)
            retry = payload.get("retry_after_seconds")
            return float(min(max(retry, 1), 600)) if isinstance(retry, (int, float)) else 30.0
        code = payload.get("error") if isinstance(payload.get("error"), str) else ""
        if code in _PAGE_ERRORS:
            page_status, page_code = _PAGE_ERRORS[code]
        elif status == 404:
            # No such endpoint at avibe.bot: the address cannot be changed from here now.
            page_status, page_code = 409, "rename_ineligible"
        elif 400 <= status < 500:
            # A definitive refusal: said as what it is, never as "unavailable",
            # which means a fault that passes. The request is not sent again.
            # ``resource_acl_device_rejected`` is the request helper's own word
            # for a body that was not JSON, not a code avibe.bot sent.
            named = _WIRE_CODE.fullmatch(code) and code != "resource_acl_device_rejected"
            page_status, page_code = 409, code if named else "rename_refused"
        else:
            page_status, page_code = 503, "unavailable"
        if status == 401:
            # The binding is refused: no cleaning anywhere from this moment, and
            # no state read still on its way may bring it back, whatever the
            # read after this refusal manages to learn.
            _drop_authority()
            page_status, page_code = 409, "pairing_invalid"
        elif status == 409 and code in _STANDING_REFUSALS:
            # The same answer stands for the state read, which is not asked again.
            _note_standing_refusal(binding, code)
        failure = {"operation_id": journal["operation_id"], "state": "failed", "hostname": target,
                   "error": page_code, "status": page_status}
        if page_code == "name_holding_limit_reached":
            failure.update({field: payload.get(field) for field in _LIMIT_FIELDS})
        old_id_refused = (
            status == 400 and code == "invalid_request" and not _UUID.fullmatch(str(journal["operation_id"]))
        )
        if landed_before and not old_id_refused:
            # This sending was refused, but an earlier one may have been taken and
            # still be in avibe.bot's hands (a refusal of a repeat does not cancel
            # it). Not a failure of the change: stop sending, keep the journal with
            # its request and its local origin, and let a state read that shows the
            # outcome settle it (``_note_other_active_name``).
            held = {key: value for key, value in failure.items() if key not in {"operation_id", "state", "hostname"}}
            _set_journal(binding, {**journal, "held": held})
            refresh_state(force=True)
            return 0.0
        _refuse_operation(binding, journal, failure)
        return 0.0
    except Exception:
        return 10.0  # no answer: the same request is sent again
    if reply.get("status") == "pending":
        if reply.get("operation_id") == journal["operation_id"] and not journal.get("accepted"):
            # Display only: the page says "changing" now. Recovery still follows
            # may_have_landed; a later refusal of a repeat holds the journal as before.
            _set_journal(binding, {**journal, "accepted": True})
        # The owner is watching: ask again within seconds, not at avibe.bot's
        # longer hint. The same operation_id is idempotent; 429 still backs off above.
        retry = reply.get("retry_after_seconds")
        return float(min(max(retry, 1), _PENDING_RETRY_MAX_SECONDS)) if isinstance(retry, (int, float)) else _PENDING_RETRY_MAX_SECONDS
    receipt_host = normalize_host(reply.get("hostname")) if isinstance(reply.get("hostname"), str) else ""
    if (
        reply.get("status") != "committed"
        or reply.get("operation_id") != journal["operation_id"]
        or reply.get("instance_id") != binding.instance_id
        or receipt_host != target
        or not isinstance(reply.get("committed_generation"), int)
    ):
        return 30.0  # not an answer this contract knows; keep the same request
    committed = {
        **journal,
        "phase": "committed",
        "receipt": {
            "committed_generation": reply["committed_generation"],
            "previous_hostname": normalize_host(reply.get("previous_hostname"))
            if isinstance(reply.get("previous_hostname"), str) else journal["previous_hostname"],
            "hostname": receipt_host,
            "previous_release_at": reply.get("previous_release_at")
            if _parse_instant(reply.get("previous_release_at")) is not None else None,
            "oidc_client_id": reply.get("oidc_client_id") if isinstance(reply.get("oidc_client_id"), str) else None,
        },
    }
    _set_journal(binding, committed)
    return _apply_committed(binding, committed)


def _refuse_operation(binding: Binding, journal: dict[str, Any], failure: dict[str, Any]) -> None:
    if journal.get("quieted"):
        # The permission goes first: once the name is no longer quiet it cleans
        # again only if a fresh state still lists it.
        _drop_authority()
    # One write ends the journal and the quiet state together.
    _set_journal(binding, None, last=failure)
    # "Read current state" after any refusal: a change made elsewhere while our
    # request was unanswered, or an earlier attempt of ours that did commit, is
    # then seen now and not at some later hint.
    refresh_state(force=True)


def _apply_committed(binding: Binding, journal: dict[str, Any]) -> float:
    """Apply a committed change. Every step is recorded and safe to repeat."""

    from vibe.binding_health import rejected_at

    if rejected_at(_load_config()) is not None:
        # The issuer may already have renamed this instance. Retire only local
        # reconciliation; a receipt is not authority for more destructive work.
        _drop_authority()
        from storage.lock import MigrationLockTimeout
        try:
            with cleanup_publication_lock():
                _set_journal(binding, None, last={
                    'operation_id': journal.get('operation_id'), 'hostname': journal['target_hostname'],
                    'state': 'interrupted', 'error': 'reconciliation_interrupted', 'status': 409,
                })
        except MigrationLockTimeout:
            return 1.0
        return 0.0
    steps = dict(journal.get("steps") or {})
    target = journal["target_hostname"]
    local = journal.get("origin") == "local"

    def done(name: str, **values: Any) -> None:
        nonlocal journal
        steps[name] = int(_wall())
        journal = {**journal, **values, "steps": dict(steps)}
        _set_journal(binding, journal)

    if "flip" not in steps:
        read = _read_state_once(binding)
        if read is None:
            return 10.0  # roles change only on a state read from avibe.bot
        state = read.state
        # First of all: a reply older than what is already known is read again.
        # The one that really arrives after our own commit is the state from
        # before it, which still names the old address; taken at its word it
        # would end the journal of a change avibe.bot did commit.
        committed_generation = (journal.get("receipt") or {}).get("committed_generation")
        with _LOCK:
            stale = _read_is_stale_locked(read)
        if stale or (local and isinstance(committed_generation, int) and state["generation"] < committed_generation):
            return 2.0
        if state["active_hostname"] != target:
            if state["active_hostname"] == binding.active_host:
                # The address this journal describes is not the address any
                # more, and this computer never left the current one.
                _refuse_operation(binding, journal, {
                    "operation_id": journal.get("operation_id"), "state": "failed", "hostname": target,
                    "error": "address_changed", "status": 409,
                })
                return 0.0
            # Another change followed; finish as a change made elsewhere: nothing is deleted.
            target = state["active_hostname"]
            journal = {**journal, "origin": "elsewhere", "target_hostname": target}
            local = False
        destructive = bool(
            local
            and state.get("last_rename_operation_id") == journal.get("operation_id")
            and state["generation"] == committed_generation
        )
        previous = journal.get("previous_hostname") or ""
        if binding.active_host != target:
            previous = binding.active_host
        if binding.active_host != target and binding.active_host:
            # The list and its receipts exist before any page is told to clear,
            # or a page that reports at once would be reporting to nothing. The
            # sign-out transaction snapshots again for a session that began since.
            _snapshot_devices(binding, binding.active_host)
        if destructive and (binding.active_host != target or not journal.get("health_cutoff")):
            # Fixed before the new address can be reached, and without opening
            # the Health store: a phone paired there afterwards is not this
            # change's to delete, however late the Health step runs. Taken anew at
            # each attempt until the address has moved, so a pairing made on the
            # old address during a long retry is not left behind. The store's own
            # clock is the real one.
            journal = {**journal, "health_cutoff": datetime.now(timezone.utc).isoformat()}
            _set_journal(binding, journal)
        try:
            journal = _flip(binding, journal, read, target)
        except _StaleState:
            return 2.0
        # The cut-off written before the switch stands only if the process died
        # between the config write and this record; it errs towards keeping data.
        done("flip", previous_hostname=previous, destructive=destructive, origin=journal["origin"],
             health_cutoff=journal.get("switched_at") or journal.get("health_cutoff"))
        binding = current_binding() or binding
        wake = _hooks.get("wake_pages")
        if wake is not None:
            try:
                wake()
            except Exception:
                logger.debug("instance address: waking pages failed", exc_info=True)
    previous = journal.get("previous_hostname") or ""
    owner = binding.instance_id
    if "sessions" not in steps:
        _require_instance(owner)  # never another pairing's sessions
        _end_sessions(binding, previous)
        done("sessions")
        _sleep(FINAL_FRAME_GRACE_SECONDS)
    if "closed" not in steps:
        close = _hooks.get("close_connections")
        if close is not None and previous:
            try:
                close({previous})
            except Exception:
                logger.debug("instance address: closing old connections failed", exc_info=True)
        done("closed")
    if "health" not in steps:
        _require_instance(owner)  # never another pairing's Health data
        if journal.get("destructive") and journal.get("origin") == "local":
            # A store that cannot be opened raises: the step is retried and the
            # state route keeps saying "pending", never "none".
            _revoke_health(journal.get("health_cutoff"))
        done("health")
    _require_instance(owner)
    final_binding = current_binding() or binding
    summary = {
        "operation_id": journal.get("operation_id"),
        "state": "applied",
        "hostname": final_binding.active_host,
        "public_url": f"https://{final_binding.active_host}",
    }

    def finish(state: dict[str, Any]) -> None:
        state["journal"] = None
        state["quiet"] = []
        state["last_operation"] = summary
        if journal.get("origin") == "local":
            state["unrecognized_change"] = None
        else:
            state["unrecognized_change"] = {
                "previous_hostname": previous,
                "hostname": final_binding.active_host,
                "noticed_at": steps.get("flip"),
            }

    global _quiet, _quiet_loaded_for

    with _first_answer:
        _update_state(final_binding.instance_id, finish)
        _quiet, _quiet_loaded_for = set(), final_binding.instance_id
        _first_answer.notify_all()
    if _hooks.get("wake_pages") is not None:
        try:
            _hooks["wake_pages"]()
        except Exception:
            pass
    # A further change seen while this journal was open was recorded but not
    # journaled (one journal at a time), and its generation now equals every
    # later hint. Read again now that the journal is closed, so it is adopted.
    _wake_worker(read_state=True)
    return 0.0


class _StaleState(Exception):
    """The state in hand is older than one already recorded; read again."""


def _flip(binding: Binding, journal: dict[str, Any], read: _StateRead, target: str) -> dict[str, Any]:
    """The role flip (rule B1 step 1): one config write, then roles from the state just read."""

    global _authority, _known_cleanup

    from config.v2_config import update_config_fields
    from vibe import remote_access

    state = read.state
    previous = binding.active_host
    stored = _load_state(binding.instance_id)
    known_cleanup = {entry.get("hostname") for entry in stored.get("cleanup", []) if isinstance(entry, dict)}
    with _LOCK:
        _load_quiet(binding)
        was_cleanup = (
            bool(journal.get("is_return"))
            or stored.get("returning") == target
            or target in known_cleanup
            or target in _quiet
            or (_authority is not None and target in _authority.hosts)
        )
    if was_cleanup:
        # Any change from cleanup to active: quiet, drain, settle (rule B3). The
        # settle is no proof that a browser finished with an earlier response.
        journal = _quiet_and_drain(binding, {**journal, "target_hostname": target})
        _sleep(REACTIVATION_SETTLE_SECONDS)
    public_url = f"https://{target}"
    receipt = journal.get("receipt") or {}
    client_id = receipt.get("oidc_client_id") or state.get("oidc_client_id")
    expected = (binding.instance_id, previous)

    def mutate(config: Any) -> None:
        cloud = config.remote_access.vibe_cloud
        current = _binding_from_config(config)
        if (current.instance_id, current.active_host) != expected or current.pairing != binding.pairing:
            raise RuntimeError("the pairing changed while the address was being switched")
        cloud.public_url = public_url
        cloud.redirect_uri = f"{public_url}/auth/callback"
        if client_id:
            cloud.client_id = client_id

    # The config write can wait for another process's write, so it is not made
    # under the role lock (the event loop and every request take that lock).
    # Instead no name is cleaned while the address moves: the permission is
    # dropped first and granted again, from this read, only afterwards.
    with _LOCK:
        # Another read may have recorded a newer state, or the binding may have
        # been refused, while this one was in flight or the name was settling.
        # Such a reply must not name the active address nor decide what is deleted.
        if _read_is_stale_locked(read):
            raise _StaleState()
        _authority = None
    if previous != target:
        update_config_fields(mutate)
        # The moment the old address stopped being served. A Health pairing made
        # before it was made there, also one completed while a returning name
        # settled; one made after it belongs to the new address.
        journal = {**journal, "switched_at": datetime.now(timezone.utc).isoformat()}
    with _LOCK:
        flipped = current_binding()
        if flipped is None or flipped.active_host != target:
            raise RuntimeError("the address switch was not recorded")
        retired = previous if previous != target else (journal.get("previous_hostname") or "")
        if retired and retired != target:
            _retired_at_flip[retired] = public_url
        _retired_at_flip.pop(target, None)
        if _read_is_stale_locked(read):
            # The address has moved; nothing is cleaned until a read made now says so.
            _wake_worker(read_state=True)
        else:
            _authority = _Authority(
                pairing=flipped.pairing,
                generation=state["generation"],
                hosts={
                    entry["hostname"]: _parse_instant(entry["release_at"])
                    for entry in state["cleanup"]
                    if entry["hostname"] != target
                },
                started_monotonic=read.started_monotonic,
                started_wall=read.started_wall,
            )

            def record(target_state: dict[str, Any]) -> None:
                target_state["generation"] = state["generation"]
                target_state["cleanup"] = [entry for entry in state["cleanup"] if entry["hostname"] != target]
                target_state["returning"] = None
                target_state["release_delay_seconds"] = state["release_delay_seconds"]
                target_state["active_release_at_if_renamed_now"] = state["active_release_at_if_renamed_now"]
                target_state["state_read_at"] = int(read.started_wall)

            _update_state(flipped.instance_id, record)
            _known_cleanup = (
                flipped.instance_id,
                frozenset(entry["hostname"] for entry in state["cleanup"] if entry["hostname"] != target),
            )
    # A name that is active again, or no longer listed, has no list any more:
    # kept, its marks would be read as true of a later retirement. Outside the
    # role lock, which the event loop and every request take.
    _forget_closed_lists(flipped, {"cleanup": [entry for entry in state["cleanup"] if entry["hostname"] != target]})
    remote_access.discard_managed_active_hostnames()
    remote_access.record_connector_public_url(public_url)
    return journal


def _snapshot_devices(binding: Binding, hostname: str) -> None:
    from storage import instance_address_clearing
    from storage.db import get_cached_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from vibe import remote_access

    ensure_sqlite_state()
    _require_instance(binding.instance_id)
    registry_binding = remote_access.device_binding(_load_config())
    key = _receipt_key()  # never take the role lock inside the DB write transaction
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        instance_address_clearing.snapshot(
            conn,
            instance_id=binding.instance_id,
            hostname=hostname,
            binding=registry_binding,
            now=int(_wall()),
            receipt_hash_for=lambda session_id: receipt_hash(_receipt_with_key(key, session_id)),
        )


def _end_sessions(binding: Binding, previous: str) -> None:
    """End every remote session in the host form, snapshotting the device list first.

    Neither of the sign-out's deferred effects is armed: no account sign-out is
    delegated to avibe.bot and no Health owner check is queued (rule C1).
    """

    from storage import device_registry, instance_address_clearing
    from storage.importer import ensure_sqlite_state
    from vibe import remote_access

    config = _load_config()
    registry_binding = remote_access.device_binding(config)
    now = int(_wall())
    ensure_sqlite_state()
    key = _receipt_key()  # never take the role lock inside the DB write transaction

    def snapshot(conn: Any) -> None:
        if previous:
            instance_address_clearing.snapshot(
                conn,
                instance_id=binding.instance_id,
                hostname=previous,
                binding=registry_binding,
                now=now,
                receipt_hash_for=lambda session_id: receipt_hash(_receipt_with_key(key, session_id)),
            )

    from vibe.account_session_revoke import pairing_epoch

    ids, _count, _results, _deferred = device_registry.sign_out(
        registry_binding,
        None,
        owner=True,
        caller=None,
        device_id=None,
        now=now,
        lifetime=remote_access.SESSION_ABSOLUTE_TTL_SECONDS,
        delegate=False,
        host_epoch=None,
        receipt_scope=pairing_epoch(config),
        prepare=snapshot,
    )
    for session_id in ids:
        remote_access.publish_browser_session_revoked(session_id)


def _health_store() -> Any:
    """The private Health store, or ``None`` when this computer never had one.

    A store that exists but cannot be opened or read raises: "could not look"
    must never be recorded as "nothing to end".
    """

    from storage.mobile_store import MobileStore, mobile_store_path

    if not mobile_store_path().exists():
        return None
    return MobileStore(existing_only=True)


def _revoke_health(cutoff: Any) -> None:
    """End the Health pairings made before ``cutoff`` with the existing revoke; delete their synced days."""

    before = datetime.fromisoformat(cutoff) if isinstance(cutoff, str) and cutoff else None
    store = _health_store()
    if store is None or before is None:
        return
    with store.transaction() as conn:
        rows = conn.execute("SELECT id, created_at FROM devices WHERE revoked_at IS NULL").fetchall()
    for row in rows:
        try:
            paired_at = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            paired_at = None
        if paired_at is None or paired_at < before:
            store.revoke(device_id=row["id"], caller="address_change")
