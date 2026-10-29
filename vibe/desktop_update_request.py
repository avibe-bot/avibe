"""Runtime-owned requests for the desktop shell's signed updater."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import threading
from typing import Any, Literal, TypedDict
from uuid import UUID, uuid4

from config.atomic_io import write_atomic
from core.computer_use import desktop_computer_use_dir
from vibe.desktop_update_status import (
    DesktopUpdateStatus,
    RemoteDesktopUpdateStatus,
    read_desktop_update_status,
)

logger = logging.getLogger(__name__)

UPDATE_REQUEST_SCHEMA_VERSION = 1
UPDATE_REQUEST_FILE = "desktop-update-request.json"
UPDATE_REQUEST_TTL = timedelta(minutes=30)
_TARGET_VERSION = re.compile(
    r"^[0-9]+(?:\.[0-9]+){2}"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_TERMINAL_PHASES = frozenset({"completed", "failed", "declined", "expired"})
_ACKNOWLEDGED_PHASES = frozenset(
    {
        "acknowledged",
        "awaiting_host",
        "checking",
        "downloading",
        "installing",
        "restarting",
        *_TERMINAL_PHASES,
    }
)
_REQUEST_LOCK = threading.Lock()


class DesktopUpdateRequest(TypedDict):
    schema_version: int
    request_id: str
    target: str
    state: Literal["pending", "expired"]
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class RequestDecision:
    request: DesktopUpdateRequest
    reused: bool
    needs_nudge: bool


class DesktopUpdateRequestError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def desktop_update_request_path(**kwargs: Any) -> Path:
    return desktop_computer_use_dir(**kwargs) / UPDATE_REQUEST_FILE


def _utc_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def valid_target_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    target = value.strip()
    if target != value or len(target) > 96 or not _TARGET_VERSION.fullmatch(target):
        return None
    return target


def parse_desktop_update_request(payload: object) -> DesktopUpdateRequest | None:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "request_id",
        "target",
        "state",
        "created_at",
        "expires_at",
    }:
        return None
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != UPDATE_REQUEST_SCHEMA_VERSION:
        return None
    request_id = payload.get("request_id")
    try:
        parsed_id = UUID(request_id) if isinstance(request_id, str) else None
    except ValueError:
        return None
    if parsed_id is None or str(parsed_id) != request_id:
        return None
    target = valid_target_version(payload.get("target"))
    state = payload.get("state")
    created_at = _parse_timestamp(payload.get("created_at"))
    expires_at = _parse_timestamp(payload.get("expires_at"))
    if target is None or state not in {"pending", "expired"}:
        return None
    if created_at is None or expires_at is None or expires_at <= created_at:
        return None
    return {
        "schema_version": UPDATE_REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "target": target,
        "state": state,
        "created_at": _utc_timestamp(created_at),
        "expires_at": _utc_timestamp(expires_at),
    }


def read_desktop_update_request(path: Path | None = None) -> DesktopUpdateRequest | None:
    target = path or desktop_update_request_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        logger.debug("Ignoring unusable desktop update request %s: %s", target, exc)
        return None
    request = parse_desktop_update_request(payload)
    if request is None:
        logger.debug("Ignoring invalid desktop update request %s", target)
    return request


def _write_request(request: DesktopUpdateRequest, path: Path) -> None:
    write_atomic(
        path,
        json.dumps(request, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
    )


def _request_pending(request: DesktopUpdateRequest, now: datetime) -> bool:
    expires_at = _parse_timestamp(request["expires_at"])
    return (
        request["state"] == "pending"
        and expires_at is not None
        and expires_at > now
    )


def _remote_in_flight(remote: RemoteDesktopUpdateStatus | None) -> bool:
    return remote is not None and remote["phase"] not in _TERMINAL_PHASES


def _awaiting_host_expired(
    remote: RemoteDesktopUpdateStatus | None,
    existing: DesktopUpdateRequest | None,
    now: datetime,
) -> bool:
    if remote is None or remote["phase"] != "awaiting_host":
        return False
    if existing is not None and existing["request_id"] == remote["request_id"]:
        return not _request_pending(existing, now)
    updated_at = _parse_timestamp(remote["updated_at"])
    return updated_at is not None and updated_at + UPDATE_REQUEST_TTL <= now


def create_or_reuse_desktop_update_request(
    target: str,
    *,
    request_path: Path | None = None,
    status_path: Path | None = None,
    now: datetime | None = None,
) -> RequestDecision:
    """Create one request, or return the same live request for an idempotent retry."""

    normalized_target = valid_target_version(target)
    if normalized_target is None:
        raise DesktopUpdateRequestError("no_update_available")
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    path = request_path or desktop_update_request_path()
    with _REQUEST_LOCK:
        status = read_desktop_update_status(status_path)
        remote = status["remote_request"] if status else None
        existing = read_desktop_update_request(path)
        awaiting_expired = _awaiting_host_expired(remote, existing, moment)
        if _remote_in_flight(remote) and not awaiting_expired:
            if remote["target"] != normalized_target:
                raise DesktopUpdateRequestError("update_in_progress")
            if existing is not None and existing["request_id"] == remote["request_id"]:
                return RequestDecision(existing, reused=True, needs_nudge=False)
            # The shell has already acknowledged this request. Its status is the
            # idempotency record even if the short-lived nudge file disappeared.
            return RequestDecision(
                {
                    "schema_version": UPDATE_REQUEST_SCHEMA_VERSION,
                    "request_id": remote["request_id"],
                    "target": remote["target"],
                    "state": "pending",
                    "created_at": remote["updated_at"],
                    "expires_at": _utc_timestamp(moment + UPDATE_REQUEST_TTL),
                },
                reused=True,
                needs_nudge=False,
            )

        terminal_request_id = (
            remote["request_id"]
            if remote is not None
            and (remote["phase"] in _TERMINAL_PHASES or awaiting_expired)
            else None
        )
        if (
            existing is not None
            and _request_pending(existing, moment)
            and existing["request_id"] != terminal_request_id
        ):
            if existing["target"] != normalized_target:
                raise DesktopUpdateRequestError("update_in_progress")
            return RequestDecision(existing, reused=True, needs_nudge=True)

        request: DesktopUpdateRequest = {
            "schema_version": UPDATE_REQUEST_SCHEMA_VERSION,
            "request_id": str(uuid4()),
            "target": normalized_target,
            "state": "pending",
            "created_at": _utc_timestamp(moment),
            "expires_at": _utc_timestamp(moment + UPDATE_REQUEST_TTL),
        }
        _write_request(request, path)
        return RequestDecision(request, reused=False, needs_nudge=True)


def expire_desktop_update_request(
    request_id: str,
    *,
    path: Path | None = None,
) -> bool:
    """Expire only the still-pending matching request."""

    target = path or desktop_update_request_path()
    with _REQUEST_LOCK:
        current = read_desktop_update_request(target)
        if (
            current is None
            or current["request_id"] != request_id
            or current["state"] != "pending"
        ):
            return False
        expired: DesktopUpdateRequest = {**current, "state": "expired"}
        _write_request(expired, target)
        return True


def acknowledged_remote_request(
    request_id: str,
    *,
    path: Path | None = None,
) -> tuple[DesktopUpdateStatus, RemoteDesktopUpdateStatus] | None:
    status = read_desktop_update_status(path)
    remote = status["remote_request"] if status else None
    if (
        status is None
        or remote is None
        or remote["request_id"] != request_id
        or remote["phase"] not in _ACKNOWLEDGED_PHASES
    ):
        return None
    return status, remote
