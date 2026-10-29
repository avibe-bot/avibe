"""The Avibe desktop app's last update check, relayed to browsers.

Only the desktop app decides whether its computer can install a release: it
owns the update channel, skipped versions, signature checks, and updater
artifacts. After each completed check it writes ``update-status.json`` beside
``computer-use.json`` (``desktop/runtime-host/src/update_status.rs``). A
desktop-managed Runtime copies ``latest`` and ``has_update`` from that file and
never decides availability itself.

A file this module cannot use reads as no conclusion, so a desktop app that
predates the file keeps the neutral guidance. Unknown fields are ignored so a
newer app never breaks an older Runtime.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, TypedDict

from core.computer_use import desktop_computer_use_dir

logger = logging.getLogger(__name__)

UPDATE_STATUS_SCHEMA_VERSION = 1
UPDATE_STATUS_FILE = "update-status.json"


class DesktopUpdateStatus(TypedDict):
    latest: str | None
    has_update: bool
    remote_install_allowed: bool
    remote_request: "RemoteDesktopUpdateStatus | None"


class RemoteDesktopUpdateStatus(TypedDict):
    request_id: str
    target: str
    phase: str
    progress: int | None
    error_code: str | None
    updated_at: str


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
    return {
        "latest": latest,
        "has_update": has_update,
        "remote_install_allowed": remote_install_allowed,
        "remote_request": _parse_remote_request(payload.get("remote_request")),
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
        or not isinstance(updated_at, str)
        or len(updated_at) > 64
    ):
        return None
    try:
        parsed_updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_updated_at.tzinfo is None:
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
