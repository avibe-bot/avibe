"""Bounded check/channel requests to the native update owner, never installs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from config.atomic_io import write_atomic
from core.computer_use import desktop_computer_use_dir
from vibe.desktop_update_request import (
    DesktopUpdateRequestError,
    _REQUEST_LOCK,
    _remote_in_flight,
    _remote_request_expired,
    _request_pending,
    read_desktop_update_request,
)
from vibe.desktop_update_status import read_desktop_update_status

CONTROL_REQUEST_FILE = "desktop-update-control.json"
CONTROL_REQUEST_TTL = timedelta(minutes=2)
CONTROL_REQUEST_MAX_BYTES = 4096
CONTROL_TERMINAL_PHASES = frozenset({"completed", "failed", "expired"})


@dataclass(frozen=True)
class ControlDecision:
    request: dict[str, Any]
    reused: bool
    needs_nudge: bool


def control_request_path(**kwargs: Any) -> Path:
    return desktop_computer_use_dir(**kwargs) / CONTROL_REQUEST_FILE


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_control_request(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    fields = {"schema_version", "request_id", "action", "state", "created_at", "expires_at"}
    action = payload.get("action")
    if action in {"prereleases", "auto_update"}:
        fields.add("enabled")
        if type(payload.get("enabled")) is not bool:
            return None
    elif action != "check":
        return None
    if set(payload) != fields or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        return None
    request_id = payload.get("request_id")
    try:
        parsed_id = UUID(request_id) if isinstance(request_id, str) else None
    except ValueError:
        return None
    if parsed_id is None or str(parsed_id) != request_id:
        return None
    if payload.get("state") not in ("pending", "expired"):
        return None
    created, expires = parse_timestamp(payload.get("created_at")), parse_timestamp(payload.get("expires_at"))
    if created is None or expires is None or not timedelta(0) < expires - created <= CONTROL_REQUEST_TTL:
        return None
    return dict(payload, created_at=timestamp(created), expires_at=timestamp(expires))


def read_control_request(path: Path | None = None) -> dict[str, Any] | None:
    try:
        with (path or control_request_path()).open("rb") as stream:
            raw = stream.read(CONTROL_REQUEST_MAX_BYTES + 1)
        if len(raw) > CONTROL_REQUEST_MAX_BYTES:
            return None
        return parse_control_request(json.loads(raw))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _pending(request: dict[str, Any], now: datetime) -> bool:
    created, expires = parse_timestamp(request["created_at"]), parse_timestamp(request["expires_at"])
    return request["state"] == "pending" and created is not None and expires is not None and created <= now < expires


def _receipt_matches(receipt: dict[str, Any], request: dict[str, Any]) -> bool:
    return all(receipt.get(key) == request.get(key) for key in (
        "request_id", "action", "enabled", "created_at", "expires_at",
    ))


def control_in_flight(path: Path, status: dict[str, Any] | None, now: datetime) -> bool:
    """Used under the shared request lock, including by the legacy install route."""
    existing = read_control_request(path)
    receipt = status.get("control_request") if status else None
    if receipt is not None:
        created = parse_timestamp(receipt.get("created_at"))
        expires = parse_timestamp(receipt.get("expires_at"))
        if (receipt.get("phase") not in CONTROL_TERMINAL_PHASES
                and created is not None and expires is not None and created <= now < expires):
            return True
        if existing is not None and _receipt_matches(receipt, existing):
            return False
    return existing is not None and _pending(existing, now)


def _write(request: dict[str, Any], path: Path) -> None:
    write_atomic(path, json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")


def create_or_reuse_control_request(
    action: str,
    enabled: bool | None = None,
    *,
    path: Path | None = None,
    status_path: Path | None = None,
    now: datetime | None = None,
) -> ControlDecision:
    if action not in {"check", "prereleases", "auto_update"} or (
        type(enabled) is not bool if action in {"prereleases", "auto_update"} else enabled is not None
    ):
        raise DesktopUpdateRequestError("invalid_request")
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    target = path or control_request_path()
    with _REQUEST_LOCK:
        status = read_desktop_update_status(status_path)
        if not status or action not in status.get("host_actions", []):
            raise DesktopUpdateRequestError("desktop_update_action_unsupported")
        install = read_desktop_update_request(target.with_name("desktop-update-request.json"))
        remote = status["remote_request"]
        if (_remote_in_flight(remote) and not _remote_request_expired(remote, install, moment)) or (
            install is not None and _request_pending(install, moment)
            and (remote is None or remote["request_id"] != install["request_id"])
        ):
            raise DesktopUpdateRequestError("update_in_progress")
        existing = read_control_request(target)
        receipt = status.get("control_request")
        if control_in_flight(target, status, moment):
            if (
                existing is None or not _pending(existing, moment)
                or existing["action"] != action or existing.get("enabled") != enabled
                or (receipt is not None and receipt["phase"] not in CONTROL_TERMINAL_PHASES
                    and not _receipt_matches(receipt, existing))
            ):
                raise DesktopUpdateRequestError("update_in_progress")
            acknowledged = receipt is not None and _receipt_matches(receipt, existing)
            return ControlDecision(existing, reused=True, needs_nudge=not acknowledged)
        request = {
            "schema_version": 1, "request_id": str(uuid4()), "action": action, "state": "pending",
            "created_at": timestamp(moment), "expires_at": timestamp(moment + CONTROL_REQUEST_TTL),
        }
        if action in {"prereleases", "auto_update"}:
            request["enabled"] = enabled
        _write(request, target)
        return ControlDecision(request, reused=False, needs_nudge=True)


def expire_control_request(request_id: str, *, path: Path | None = None) -> bool:
    target = path or control_request_path()
    with _REQUEST_LOCK:
        request = read_control_request(target)
        if request is None or request["request_id"] != request_id or request["state"] != "pending":
            return False
        request["state"] = "expired"
        _write(request, target)
        return True


def acknowledged_control_request(request: dict[str, Any], *, status_path: Path | None = None) -> dict[str, Any] | None:
    status = read_desktop_update_status(status_path)
    receipt = status.get("control_request") if status else None
    return receipt if receipt is not None and _receipt_matches(receipt, request) else None
