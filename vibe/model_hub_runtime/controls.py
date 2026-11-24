"""Read-only engine dependency projection and bounded, controller-owned job receipts.

Receipts are observations, never selection, retry, or recovery authority. A new
controller reports an abandoned job as interrupted rather than replaying it.
"""
from __future__ import annotations

import time
from pathlib import Path

from vibe.model_hub_runtime.journal import read_document, write_document


def auto_update_enabled() -> bool:
    from config.v2_config import V2Config

    try:
        config = V2Config.load()
        if config.whole_config_recovery or "update" in config.recovered_sections:
            return False
        return config.update.model_hub_engine_auto_update is True
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False  # never turn a policy read failure into adoption permission


def set_auto_update(enabled: bool) -> dict:
    from config.v2_config import update_config_fields

    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    update_config_fields(lambda config: setattr(config.update, "model_hub_engine_auto_update", enabled))
    return {"ok": True, "auto_update": enabled}


def project(status: dict, *, default: str | None, automatic: bool, participation: str | None,
            live: bool, operation: dict | None = None) -> dict:
    """Project verified execution, channel availability and operator action separately."""
    state = status.get("state", "idle")
    reason = status.get("reason")
    channel = status.get("channel") or {}
    active = (status.get("running") or {}).get("asset_release_tag") if live else None
    available = status.get("available")
    if state in {"blocked", "rolling_back", "rolled_back", "deferred"}:
        pass
    elif state == "withdrawing":
        reason = "engine_withdrawn"
    elif state == "activating":
        state = "updating"
    elif participation:
        state, reason = participation, participation
    elif not live:
        state, reason = "waiting_for_controller", "waiting_for_controller"
    elif operation and operation["status"] == "running" and state not in {"applied", "withdrawing"}:
        state = "updating" if operation["kind"] == "update" else "checking"
    elif state == "applied":
        pass
    elif reason == "requires_avibe_update":
        state = reason
    elif channel.get("state") in {"unavailable", "rejected", "offline", "uncommissioned", "opted_out"}:
        state, reason = channel["state"], channel.get("reason")
    elif state == "up_to_date" and reason not in {"up_to_date", "adoption_disabled"}:
        state = "unavailable"
    if available == (status.get("committed") or {}).get("asset_release_tag") or participation or not live:
        available = None
    return {
        "active_release": active, "default_release": default, "available_release": available,
        "min_avibe_version": status.get("min_avibe_version") if available or state == "requires_avibe_update" else None,
        "auto_update": automatic, "update_state": state, "update_reason": reason,
        "last_checked_at": status.get("last_checked_at"), "last_result": status.get("last_result"),
        "operation_id": operation["operation_id"] if operation else None,
    }


class EngineJobs:
    """Single controller writer; retain the most recent 32 receipts across restarts."""

    def __init__(self, directory: Path):
        self.path = directory / "engine-operation-receipts.json"

    def store(self, job: dict) -> None:
        rows = (read_document(self.path) or {}).get("jobs", [])
        rows = [row for row in rows if row["operation_id"] != job["operation_id"]]
        write_document(self.path, {"jobs": [*rows[-31:], job]})

    def get(self, operation_id: str) -> dict:
        from core.handlers.model_hub import ModelHubError

        rows = (read_document(self.path) or {}).get("jobs", [])
        for row in reversed(rows):
            if row["operation_id"] == operation_id:
                if row["status"] == "running":
                    return {**row, "ok": False, "status": "failed", "update_state": "failed",
                            "update_reason": "operation_interrupted"}
                return row
        raise ModelHubError("job_not_found", status=404)

    @staticmethod
    def started(operation_id: str, update: bool) -> dict:
        return {"ok": True, "job_id": operation_id, "operation_id": operation_id,
                "backend": "model-hub-engine", "kind": "update" if update else "check",
                "started_at": time.time(), "finished_at": None, "status": "running",
                "update_state": "updating" if update else "checking", "update_reason": None}
