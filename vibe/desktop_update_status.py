"""The Avibe desktop app's last update check, relayed to browsers.

Only the desktop app decides whether its computer can install a release: it
owns the update channel, skipped versions, signature checks, and updater
artifacts. After each completed check it writes ``update-status.json`` beside
``computer-use.json`` (``desktop/runtime-host/src/update_status.rs``). A
desktop-managed Runtime relays that record (the check's result, the app's own
version, its channel, and when it checked) to the Web UI and the CLI, so every
surface shows the same conclusion, and never decides availability itself.

A file this module cannot use reads as no conclusion, so a desktop app that
predates the file keeps the neutral guidance. A field this module cannot use
reads as unknown without discarding the rest of the record, and unknown fields
are ignored, so a newer app never breaks an older Runtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID

from core.computer_use import desktop_computer_use_dir

logger = logging.getLogger(__name__)

UPDATE_STATUS_SCHEMA_VERSION = 1
UPDATE_STATUS_FILE = "update-status.json"


class DesktopUpdateStatus(TypedDict):
    # The completed check's result: "current", "available", "disabled" (this
    # build cannot update itself) or "error" (the check failed); None when the
    # record does not say.
    phase: str | None
    # The desktop app's own SemVer version, not the Runtime's PEP 440 one.
    desktop_version: str | None
    # Whether the app's channel also offers pre-releases; None when unknown.
    prereleases: bool | None
    # When the app finished that check, RFC 3339.
    checked_at: str | None
    latest: str | None
    has_update: bool
    remote_install_allowed: bool
    remote_request: "RemoteDesktopUpdateStatus | None"
    host_actions: list[str]
    control_request: dict[str, Any] | None


class RemoteDesktopUpdateStatus(TypedDict):
    request_id: str
    target: str
    phase: str
    progress: int | None
    error_code: str | None
    updated_at: str


CHECK_PHASES = frozenset({"current", "available", "disabled", "error"})
CHANNEL_PRERELEASES = {"stable": False, "test": True}
CHECK_FAILED_MESSAGE = "The desktop app's last update check failed."

REMOTE_UPDATE_PHASES = frozenset(
    {
        "requested",
        "acknowledged",
        "awaiting_host",
        "checking",
        "downloading",
        "installing",
        "restarting",
        "completed",
        "failed",
        "declined",
        "expired",
    }
)


def desktop_update_status_path(**kwargs: Any) -> Path:
    """``update-status.json`` in the desktop app-data directory, independent of ``AVIBE_HOME``."""

    return desktop_computer_use_dir(**kwargs) / UPDATE_STATUS_FILE


def parse_desktop_update_status(payload: object) -> DesktopUpdateStatus | None:
    if not isinstance(payload, dict):
        return None
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != UPDATE_STATUS_SCHEMA_VERSION:
        return None
    has_update = payload.get("has_update")
    latest = payload.get("latest")
    if not isinstance(has_update, bool) or not (latest is None or isinstance(latest, str)):
        return None
    latest = (latest or "").strip() or None
    if has_update and latest is None:
        return None
    remote_install_allowed = payload.get("remote_install_allowed", False)
    if not isinstance(remote_install_allowed, bool):
        remote_install_allowed = False
    phase = payload.get("phase")
    if not isinstance(phase, str) or phase not in CHECK_PHASES:
        phase = None
    # `has_update` is the shell's conclusion; a phase that disagrees with it is
    # not one this Runtime can show.
    if has_update:
        phase = "available"
    elif phase == "available":
        phase = None
    desktop_version = payload.get("current")
    if not isinstance(desktop_version, str) or not desktop_version.strip() or len(desktop_version) > 96:
        desktop_version = None
    channel = payload.get("channel")
    prereleases = CHANNEL_PRERELEASES.get(channel) if isinstance(channel, str) else None
    return {
        "phase": phase,
        "desktop_version": desktop_version.strip() if desktop_version else None,
        "prereleases": prereleases,
        "checked_at": _parse_timestamp(payload.get("checked_at")),
        "latest": latest,
        "has_update": has_update,
        "remote_install_allowed": remote_install_allowed,
        "remote_request": _parse_remote_request(payload.get("remote_request")),
        "host_actions": _parse_host_actions(payload.get("host_actions")),
        "control_request": _parse_control_receipt(payload.get("control_request")),
    }


def _parse_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _parse_host_actions(payload: object) -> list[str]:
    if not isinstance(payload, list):
        return []
    return [action for action in ("check", "prereleases", "install") if action in payload]


def _parse_control_receipt(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    request_id, action, phase = (payload.get(key) for key in ("request_id", "action", "phase"))
    try:
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            return None
    except ValueError:
        return None
    if action not in ("check", "prereleases") or phase not in ("checking", "completed", "failed", "expired"):
        return None
    enabled = payload.get("enabled")
    if (type(enabled) is not bool if action == "prereleases" else enabled is not None):
        return None
    dates = {key: _parse_timestamp(payload.get(key)) for key in ("created_at", "expires_at", "updated_at")}
    if any(value is None for value in dates.values()):
        return None
    created = datetime.fromisoformat(dates["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(dates["expires_at"].replace("Z", "+00:00"))
    if not timedelta(0) < expires - created <= timedelta(minutes=2):
        return None
    error = payload.get("error_code")
    if error is not None and (not isinstance(error, str) or not error.strip() or len(error) > 128):
        return None
    return {
        "request_id": request_id, "action": action, "enabled": enabled, "phase": phase,
        "error_code": error, **dates,
    }


def _parse_remote_request(payload: object) -> RemoteDesktopUpdateStatus | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("request_id")
    target = payload.get("target")
    phase = payload.get("phase")
    progress = payload.get("progress")
    error_code = payload.get("error_code")
    updated_at = payload.get("updated_at")
    if (
        not isinstance(request_id, str)
        or not request_id.strip()
        or len(request_id) > 128
        or not isinstance(target, str)
        or not target.strip()
        or len(target) > 96
        or not isinstance(phase, str)
        or phase not in REMOTE_UPDATE_PHASES
        or not (
            progress is None
            or (
                isinstance(progress, int)
                and not isinstance(progress, bool)
                and 0 <= progress <= 100
            )
        )
        or not (
            error_code is None
            or (
                isinstance(error_code, str)
                and bool(error_code.strip())
                and len(error_code) <= 128
            )
        )
        or _parse_timestamp(updated_at) is None
    ):
        return None
    return {
        "request_id": request_id,
        "target": target,
        "phase": phase,
        "progress": progress,
        "error_code": error_code,
        "updated_at": updated_at,
    }


def read_desktop_update_status(path: Path | None = None) -> DesktopUpdateStatus | None:
    """The desktop app's last conclusion, or None when there is none to relay."""

    target = path or desktop_update_status_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        logger.debug("Ignoring unusable desktop update status %s: %s", target, exc)
        return None
    status = parse_desktop_update_status(payload)
    if status is None:
        logger.debug("Ignoring invalid desktop update status %s", target)
    return status


def desktop_update_snapshot(runtime_version: str, path: Path | None = None) -> dict[str, Any]:
    """What ``/api/version`` and ``vibe check-update`` report for a desktop-managed Runtime.

    ``current`` stays the Runtime's own version, which remote update receipts
    are verified against; ``desktop_version`` is the app the user updates.
    ``error`` is set only when the app's last check failed, never for a missing
    record, which has no conclusion to relay.
    """

    status = read_desktop_update_status(path)
    phase = status["phase"] if status else None
    control = status.get("control_request") if status else None
    if control is not None and control["phase"] == "checking":
        now = datetime.now(timezone.utc)
        created = datetime.fromisoformat(control["created_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(control["expires_at"].replace("Z", "+00:00"))
        if created <= now < expires:
            phase = "checking"
        else:
            control = {**control, "phase": "expired", "error_code": "expired"}
    return {
        "current": runtime_version,
        "latest": status["latest"] if status else None,
        "has_update": status["has_update"] if status else False,
        "error": CHECK_FAILED_MESSAGE if phase == "error" else None,
        "phase": phase,
        "desktop_version": status["desktop_version"] if status else None,
        "prereleases": status["prereleases"] if status else None,
        "checked_at": status["checked_at"] if status else None,
        "remote_install_allowed": status["remote_install_allowed"] if status else False,
        "remote_request": status["remote_request"] if status else None,
        "host_actions": status["host_actions"] if status else [],
        "control_request": control,
    }
