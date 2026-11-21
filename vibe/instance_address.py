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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from config import paths
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
_DRAINED = threading.Condition(_LOCK)


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
    return isinstance(prefix, str) and _PREFIX.fullmatch(prefix) is not None and is_managed_name(hostname_for_prefix(prefix))


# --- the local binding --------------------------------------------------------


@dataclass(frozen=True)
class Binding:
    """What this computer's configuration says about its own address."""

    active_host: str
    instance_id: str
    secret_digest: str
    paired: bool
    instance_kind: str
    backend_origin: str

    @property
    def pairing(self) -> tuple[str, str]:
        return self.instance_id, self.secret_digest


_BINDING_CACHE: tuple[tuple[Any, ...], Binding | None] | None = None


def _binding_from_config(config: Any) -> Binding:
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
    secret = str(cloud.instance_secret or "")
    return Binding(
        active_host=active,
        instance_id=str(cloud.instance_id or "").strip(),
        secret_digest=hashlib.sha256(secret.encode("utf-8")).hexdigest() if secret else "",
        paired=bool(cloud.is_runtime_paired()),
        instance_kind=str(cloud.instance_kind or ""),
        backend_origin=backend,
    )


def current_binding() -> Binding | None:
    """Read the local binding, reloading the config only when its file changed.

    ``None`` means the configuration cannot be read; a managed Host then gets
    no content and no cleaning.
    """

    global _BINDING_CACHE

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
            logger.warning("instance address: configuration unreadable", exc_info=True)
            binding = None
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


def _update_state(instance_id: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _LOCK:
        state = _load_state(instance_id)
        mutate(state)
        _save_state(state)
        return state


# --- receipts -----------------------------------------------------------------


_receipt_key_cache: tuple[str, bytes] | None = None


def _receipt_key() -> bytes:
    global _receipt_key_cache

    with _LOCK:
        cached = _receipt_key_cache
        if cached is not None and cached[0] == str(_state_path()):
            return cached[1]
        key = _stored_receipt_key()
        _receipt_key_cache = (str(_state_path()), key)
        return key


def _stored_receipt_key() -> bytes:
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

    digest = hmac.new(_receipt_key(), _RECEIPT_CONTEXT + session_id.encode("utf-8"), hashlib.sha256).digest()
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
_inflight: dict[str, int] = {}
_last_read_started: float | None = None
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


def _load_quiet(binding: Binding) -> None:
    global _quiet, _quiet_loaded_for

    if _quiet_loaded_for == binding.instance_id:
        return
    _quiet = {host for host in _load_state(binding.instance_id).get("quiet", []) if is_managed_name(host)}
    _quiet_loaded_for = binding.instance_id


def _drop_authority() -> None:
    global _authority, _authority_epoch

    with _LOCK:
        _authority = None
        _authority_epoch += 1


def _cleanup_allowed_locked(host: str, binding: Binding) -> bool:
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


def confirm_cleanup(raw_host: str | None) -> bool:
    """Whether a cleaning response may be produced for this Host now.

    Reads the state from avibe.bot when the held permission does not cover the
    Host and the Host could be one of ours. Blocking; call from a thread.
    """

    host = normalize_host(raw_host)
    binding = current_binding()
    if binding is None or not binding.paired or not is_managed_name(host) or host == binding.active_host:
        return False
    with _LOCK:
        if _cleanup_allowed_locked(host, binding):
            return True
        _load_quiet(binding)
        if host in _quiet:
            return False
        known = {
            entry.get("hostname")
            for entry in _load_state(binding.instance_id).get("cleanup", [])
            if isinstance(entry, dict)
        }
        never_read = _last_read_started is None
    if host not in known and not never_read:
        return False
    refresh_state()
    with _LOCK:
        return _cleanup_allowed_locked(host, binding)


def begin_cleanup_response(raw_host: str | None) -> bool:
    """The last check, under the role lock, immediately before headers are written."""

    host = normalize_host(raw_host)
    binding = current_binding()
    with _LOCK:
        if binding is None or not _cleanup_allowed_locked(host, binding):
            return False
        _inflight[host] = _inflight.get(host, 0) + 1
        return True


def end_cleanup_response(raw_host: str | None) -> None:
    host = normalize_host(raw_host)
    with _DRAINED:
        remaining = _inflight.get(host, 0) - 1
        if remaining > 0:
            _inflight[host] = remaining
        else:
            _inflight.pop(host, None)
        _DRAINED.notify_all()


def _quiet_and_drain(binding: Binding, host: str) -> None:
    """Stop cleaning ``host`` and wait for cleaning responses already started on it."""

    with _DRAINED:
        _load_quiet(binding)
        _quiet.add(host)
        _persist_quiet(binding)
        deadline = _monotonic() + _DRAIN_TIMEOUT_SECONDS
        while _inflight.get(host, 0) > 0:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise RuntimeError("cleanup responses did not drain")
            _DRAINED.wait(timeout=min(remaining, 0.5))


def _persist_quiet(binding: Binding) -> None:
    quiet = sorted(_quiet)
    _update_state(binding.instance_id, lambda state: state.__setitem__("quiet", quiet))


def retired_destination(stream_host: str | None) -> str | None:
    """For a stream admitted on a Host that was active until the switch: the new origin."""

    host = normalize_host(stream_host)
    with _LOCK:
        destination = _retired_at_flip.get(host)
    binding = current_binding()
    if destination is None or binding is None or host == binding.active_host:
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


def _read_state_once(binding: Binding) -> _StateRead | None:
    """One read of ``hostname-state``. ``None`` when it gave no usable state."""

    from vibe import remote_access

    with _LOCK:
        epoch = _authority_epoch
    started_monotonic, started_wall = _monotonic(), _wall()
    try:
        payload = remote_access._device_json_request(_load_config(), "POST", "hostname-state", {}, timeout=8.0)
    except remote_access.BackendRequestError as exc:
        if exc.status == 401:
            # The binding is no longer current: no cleaning anywhere, and no
            # reply still on its way may bring it back.
            _drop_authority()
        return None
    except Exception:
        return None
    state = _validated_state(binding, payload)
    with _LOCK:
        if state is None or epoch != _authority_epoch or current_binding() != binding:
            return None
    return _StateRead(binding, state, started_monotonic, started_wall)


def refresh_state(*, force: bool = False) -> bool:
    """Read the state unless one was started in the last few seconds; apply it.

    One read at a time: a caller that finds a read in flight waits for it and
    uses its result.
    """

    global _last_read_started

    binding = current_binding()
    if binding is None or not binding.paired or not is_managed_name(binding.active_host):
        return False
    with _read_lock:
        with _LOCK:
            last = _last_read_started
            if not force and last is not None and _monotonic() - last < STATE_READ_MIN_INTERVAL_SECONDS:
                return False
            _last_read_started = _monotonic()
        read = _read_state_once(binding)
        if read is None:
            return False
        return _apply_state(read)


def _apply_state(read: _StateRead) -> bool:
    global _authority

    binding, state = read.binding, read.state
    with _LOCK:
        stored = _load_state(binding.instance_id)
        stored_generation = stored.get("generation")
        if isinstance(stored_generation, int) and state["generation"] < stored_generation:
            return False  # an older reply; roles stay as they are

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
            _forget_closed_lists(binding, state)
            return True
        # avibe.bot names another active name than this computer does.
        _authority = None
        journal = stored.get("journal")
        if isinstance(journal, dict) and journal.get("origin") == "local" and journal.get("phase") == "intent":
            # Our own request is still unanswered here; its replay settles it.
            _wake_worker()
            return True
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
                        "created_at": int(_wall()),
                        "steps": {},
                    },
                ),
            )
        _wake_worker()
        return True


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


def preview(prefix: Any, actor: tuple[str, ...]) -> dict[str, Any]:
    """First of the two requests: what a change to ``prefix`` would do (rule A2)."""

    binding = current_binding()
    reason = _eligibility(binding)
    if reason is not None or binding is None:
        raise AddressError(409, "rename_ineligible", reason=reason)
    if not _valid_prefix(prefix):
        raise AddressError(400, "invalid_prefix")
    target = hostname_for_prefix(prefix)
    if target == binding.active_host:
        raise AddressError(409, "same_address")
    if isinstance(_load_state(binding.instance_id).get("journal"), dict):
        raise AddressError(409, "operation_in_progress")
    refresh_state(force=True)
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


def change(confirmation_id: Any, actor: tuple[str, ...]) -> tuple[int, dict[str, Any]]:
    """Second request: write the intent, start the operation, wait briefly for avibe.bot."""

    binding = current_binding()
    reason = _eligibility(binding)
    if reason is not None or binding is None:
        raise AddressError(409, "rename_ineligible", reason=reason)
    with _LOCK:
        entry = _confirmations.pop(confirmation_id, None) if isinstance(confirmation_id, str) else None
        if entry is None or entry["actor"] != actor or entry["expires_monotonic"] <= _monotonic():
            raise AddressError(409, "confirmation_expired")
        state = _load_state(binding.instance_id)
        if isinstance(state.get("journal"), dict):
            raise AddressError(409, "operation_in_progress")
        if (
            entry["pairing"] != binding.pairing
            or entry["active"] != binding.active_host
            or entry["generation"] != state.get("generation")
        ):
            raise AddressError(409, "address_changed")
        operation_id = secrets.token_urlsafe(32)
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
            if isinstance(journal, dict) and journal.get("phase") == "committed":
                return 202, {"ok": True, "state": "applying"}
            _first_answer.wait(timeout=0.25)
    return 202, {"ok": True, "state": "pending"}


def describe() -> dict[str, Any]:
    """What the settings page shows: the address, the operation, and the old names' lists."""

    binding = current_binding()
    reason = _eligibility(binding)
    if binding is None:
        return {"ok": True, "hostname": None, "public_url": None, "eligible": False, "ineligible_reason": reason,
                "hold_days": None, "release_at_if_changed_now": None, "operation": None,
                "unrecognized_change": None, "cleanup": [], "health": {"connected": False, "cleanup": "none"}}
    state = _load_state(binding.instance_id)
    read_at = state.get("state_read_at")
    if reason is None and (not isinstance(read_at, int) or _wall() - read_at > DISPLAY_REFRESH_SECONDS):
        refresh_state()
        state = _load_state(binding.instance_id)
    journal = state.get("journal")
    operation = None
    if isinstance(journal, dict):
        operation = {
            "state": "pending" if journal.get("phase") == "intent" else "applying",
            "hostname": journal.get("target_hostname"),
            "error": None,
        }
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


def _worker_loop() -> None:
    delay = 0.0
    while True:
        _worker_wake.wait(timeout=delay if delay > 0 else None)
        _worker_wake.clear()
        try:
            if _worker_read_requested.is_set():
                _worker_read_requested.clear()
                refresh_state(force=True)
            delay = _advance_operation()
        except Exception:
            logger.warning("instance address: operation step failed; it will be retried", exc_info=True)
            delay = 15.0


def _journal(binding: Binding) -> dict[str, Any] | None:
    journal = _load_state(binding.instance_id).get("journal")
    return journal if isinstance(journal, dict) else None


def _set_journal(binding: Binding, journal: dict[str, Any] | None, *, last: dict[str, Any] | None = None) -> None:
    def mutate(state: dict[str, Any]) -> None:
        state["journal"] = journal
        if last is not None:
            state["last_operation"] = last

    with _first_answer:
        _update_state(binding.instance_id, mutate)
        _first_answer.notify_all()


def _advance_operation() -> float:
    """Run the journal as far as it can go now; return seconds until the next try (0 = idle)."""

    binding = current_binding()
    if binding is None or not binding.paired:
        return 0.0
    journal = _journal(binding)
    if journal is None:
        return 0.0
    if journal.get("origin") == "local" and journal.get("phase") == "intent":
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
}
_LIMIT_FIELDS = ("limit", "held_count", "earliest_release_at")


def _request_change(binding: Binding, journal: dict[str, Any]) -> float:
    from vibe import remote_access

    target = journal["target_hostname"]
    if journal.get("is_return") and not journal.get("quieted"):
        # Before asking for one of our own old names back: stop cleaning it and
        # let responses already started finish (rule B3).
        _quiet_and_drain(binding, target)
        journal = {**journal, "quieted": True}
        _set_journal(binding, journal)
    body = {
        "operation_id": journal["operation_id"],
        "expected_generation": journal["expected_generation"],
        "expected_hostname": journal["expected_hostname"],
        "new_prefix": journal["new_prefix"],
    }
    try:
        reply = remote_access._device_json_request(_load_config(), "POST", "hostname-renames", body, timeout=20.0)
    except remote_access.BackendRequestError as exc:
        status, payload = exc.status, exc.payload if isinstance(exc.payload, dict) else {}
        if status == 429 or status >= 500:
            retry = payload.get("retry_after_seconds")
            return float(min(max(retry, 1), 600)) if isinstance(retry, (int, float)) else 30.0
        code = payload.get("error") if isinstance(payload.get("error"), str) else ""
        page_status, page_code = _PAGE_ERRORS.get(code, (503, "unavailable"))
        if status == 401:
            page_status, page_code = 409, "pairing_invalid"
        failure = {"operation_id": journal["operation_id"], "state": "failed", "hostname": target,
                   "error": page_code, "status": page_status}
        if page_code == "name_holding_limit_reached":
            failure.update({field: payload.get(field) for field in _LIMIT_FIELDS})
        _refuse_operation(binding, journal, failure)
        return 0.0
    except Exception:
        return 10.0  # no answer: the same request is sent again
    if reply.get("status") == "pending":
        retry = reply.get("retry_after_seconds")
        return float(min(max(retry, 1), 60)) if isinstance(retry, (int, float)) else 5.0
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
    _set_journal(binding, None, last=failure)
    if journal.get("quieted"):
        # The name cleans again only if a fresh state still lists it.
        with _LOCK:
            _load_quiet(binding)
            _quiet.discard(journal["target_hostname"])
            _persist_quiet(binding)
            _drop_authority()
        refresh_state(force=True)


def _apply_committed(binding: Binding, journal: dict[str, Any]) -> float:
    """Apply a committed change. Every step is recorded and safe to repeat."""

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
            and state["generation"] == (journal.get("receipt") or {}).get("committed_generation")
        )
        previous = journal.get("previous_hostname") or ""
        if binding.active_host != target:
            previous = binding.active_host
        if binding.active_host != target and binding.active_host:
            # The list and its receipts exist before any page is told to clear,
            # or a page that reports at once would be reporting to nothing. The
            # sign-out transaction snapshots again for a session that began since.
            _snapshot_devices(binding, binding.active_host)
        if destructive and not isinstance(journal.get("health_targets"), list):
            # Fixed before the new address can be reached: a phone paired there
            # afterwards is not this change's to delete, however late the Health
            # step runs.
            journal = {**journal, "health_targets": _active_health_devices()}
            _set_journal(binding, journal)
        _flip(binding, journal, read, target)
        done("flip", previous_hostname=previous, destructive=destructive, origin=journal["origin"])
        binding = current_binding() or binding
        wake = _hooks.get("wake_pages")
        if wake is not None:
            try:
                wake()
            except Exception:
                logger.debug("instance address: waking pages failed", exc_info=True)
    previous = journal.get("previous_hostname") or ""
    if "sessions" not in steps:
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
        if journal.get("destructive") and journal.get("origin") == "local":
            targets = journal.get("health_targets")
            _revoke_health(targets if isinstance(targets, list) else [])
        done("health")
    final_binding = current_binding() or binding
    summary = {
        "operation_id": journal.get("operation_id"),
        "state": "applied",
        "hostname": final_binding.active_host,
        "public_url": f"https://{final_binding.active_host}",
    }

    def finish(state: dict[str, Any]) -> None:
        state["journal"] = None
        state["last_operation"] = summary
        if journal.get("origin") == "local":
            state["unrecognized_change"] = None
        else:
            state["unrecognized_change"] = {
                "previous_hostname": previous,
                "hostname": final_binding.active_host,
                "noticed_at": steps.get("flip"),
            }

    with _first_answer:
        _update_state(final_binding.instance_id, finish)
        _first_answer.notify_all()
    if _hooks.get("wake_pages") is not None:
        try:
            _hooks["wake_pages"]()
        except Exception:
            pass
    return 0.0


def _flip(binding: Binding, journal: dict[str, Any], read: _StateRead, target: str) -> None:
    """The role flip (rule B1 step 1): one config write, then roles from the state just read."""

    global _authority

    from config.v2_config import update_config_fields
    from vibe import remote_access

    state = read.state
    previous = binding.active_host
    known_cleanup = {
        entry.get("hostname") for entry in _load_state(binding.instance_id).get("cleanup", []) if isinstance(entry, dict)
    }
    with _LOCK:
        _load_quiet(binding)
        was_cleanup = target in known_cleanup or target in _quiet or (
            _authority is not None and target in _authority.hosts
        )
    if was_cleanup:
        # Any change from cleanup to active: quiet, drain, settle (rule B3). The
        # settle is no proof that a browser finished with an earlier response.
        _quiet_and_drain(binding, target)
        _sleep(REACTIVATION_SETTLE_SECONDS)
    public_url = f"https://{target}"
    receipt = journal.get("receipt") or {}
    client_id = receipt.get("oidc_client_id") or state.get("oidc_client_id")
    expected = (binding.instance_id, previous)

    def mutate(config: Any) -> None:
        cloud = config.remote_access.vibe_cloud
        current = _binding_from_config(config)
        if (current.instance_id, current.active_host) != expected or current.secret_digest != binding.secret_digest:
            raise RuntimeError("the pairing changed while the address was being switched")
        cloud.public_url = public_url
        cloud.redirect_uri = f"{public_url}/auth/callback"
        if client_id:
            cloud.client_id = client_id

    with _LOCK:
        if previous != target:
            update_config_fields(mutate)
        flipped = current_binding()
        if flipped is None or flipped.active_host != target:
            raise RuntimeError("the address switch was not recorded")
        _quiet.discard(target)
        _persist_quiet(flipped)
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
        retired = previous if previous != target else (journal.get("previous_hostname") or "")
        if retired and retired != target:
            _retired_at_flip[retired] = public_url
        _retired_at_flip.pop(target, None)

        def record(target_state: dict[str, Any]) -> None:
            target_state["generation"] = state["generation"]
            target_state["cleanup"] = [entry for entry in state["cleanup"] if entry["hostname"] != target]
            target_state["release_delay_seconds"] = state["release_delay_seconds"]
            target_state["active_release_at_if_renamed_now"] = state["active_release_at_if_renamed_now"]
            target_state["state_read_at"] = int(read.started_wall)

        _update_state(flipped.instance_id, record)
        # A name that is active again, or no longer listed, has no list any more:
        # kept, its marks would be read as true of a later retirement.
        _forget_closed_lists(
            flipped, {"cleanup": [entry for entry in state["cleanup"] if entry["hostname"] != target]}
        )
    remote_access.discard_managed_active_hostnames()
    remote_access.record_connector_public_url(public_url)


def _snapshot_devices(binding: Binding, hostname: str) -> None:
    from storage import instance_address_clearing
    from storage.db import get_cached_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from vibe import remote_access

    ensure_sqlite_state()
    registry_binding = remote_access.device_binding(_load_config())
    with get_cached_sqlite_engine().begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        instance_address_clearing.snapshot(
            conn,
            instance_id=binding.instance_id,
            hostname=hostname,
            binding=registry_binding,
            now=int(_wall()),
            receipt_hash_for=lambda session_id: receipt_hash(receipt_for_session(session_id)),
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

    def snapshot(conn: Any) -> None:
        if previous:
            instance_address_clearing.snapshot(
                conn,
                instance_id=binding.instance_id,
                hostname=previous,
                binding=registry_binding,
                now=now,
                receipt_hash_for=lambda session_id: receipt_hash(receipt_for_session(session_id)),
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


def _active_health_devices() -> list[str]:
    try:
        from storage.mobile_store import MobileStore

        store = MobileStore(existing_only=True)
    except Exception:
        return []
    with store.transaction() as conn:
        return [row["id"] for row in conn.execute("SELECT id FROM devices WHERE revoked_at IS NULL")]


def _revoke_health(device_ids: list[str]) -> None:
    """End these Health pairings with the existing revoke and delete their synced days."""

    if not device_ids:
        return
    from storage.mobile_store import MobileStore

    store = MobileStore(existing_only=True)
    for device_id in device_ids:
        store.revoke(device_id=device_id, caller="address_change")
