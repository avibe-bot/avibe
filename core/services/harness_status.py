"""Pure projection for the bounded Harness live/anomaly operator view."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable


def _parse_instant(timestamp: object) -> datetime | None:
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    text = timestamp.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        observed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc)


def _seconds_since(timestamp: object, now: datetime) -> float | None:
    observed = _parse_instant(timestamp)
    if observed is None:
        return None
    return max(0.0, (now - observed).total_seconds())


def _canonical_workdir(value: object) -> str | None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    return os.path.normcase(os.path.realpath(os.path.expanduser(cleaned)))


def _runtime_identity(row: dict[str, Any]) -> str:
    return str(
        row.get("session_id")
        or row.get("base_session_id")
        or row.get("native_session_id")
        or f"{row.get('backend')}:{row.get('pid')}"
    )


def _project_runtime_agent(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "backend",
            "state",
            "base_session_id",
            "session_id",
            "agent_name",
            "title",
            "workdir",
            "pid",
            "model",
            "elapsed_seconds",
            "openable_in_chat",
        )
    }


def _project_run(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    explicit_activity = row.get("last_activity_at")
    activity_at = explicit_activity or row.get("started_at")
    return {
        "id": row.get("id"),
        "run_type": row.get("run_type") or row.get("request_type"),
        "status": row.get("status"),
        "agent_name": row.get("agent_name"),
        "agent_backend": row.get("agent_backend"),
        "session_id": row.get("session_id"),
        "definition_id": row.get("definition_id") or row.get("task_id"),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "updated_at": row.get("updated_at"),
        "last_activity_at": activity_at,
        "activity_basis": "output" if explicit_activity else ("start" if activity_at else None),
        "activity_age_seconds": _seconds_since(activity_at, now),
        "liveness": "queued" if row.get("status") == "queued" else "unknown",
        "anomaly_codes": [],
    }


def _project_watch(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "id",
            "name",
            "session_id",
            "lifecycle_state",
            "process_alive",
            "health",
            "processing_health",
            "last_started_at",
            "last_finished_at",
            "last_event_at",
            "last_exit_code",
            "last_error",
        )
    }


def _project_task(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "id",
            "name",
            "session_id",
            "agent_name",
            "lifecycle_state",
            "next_run_at",
            "last_run_at",
            "last_error",
        )
    }


def build_harness_status(
    *,
    runs: Iterable[dict[str, Any]],
    watches: Iterable[dict[str, Any]],
    tasks: Iterable[dict[str, Any]],
    runtime_snapshot: dict[str, Any],
    truncated: dict[str, bool] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Combine durable and controller facts without inventing lifecycle state."""

    observed_now = now or datetime.now(timezone.utc)
    if observed_now.tzinfo is None:
        observed_now = observed_now.replace(tzinfo=timezone.utc)
    observed_now = observed_now.astimezone(timezone.utc)

    run_rows = [_project_run(dict(row), observed_now) for row in runs]
    watch_rows = [_project_watch(dict(row)) for row in watches]
    task_rows = [_project_task(dict(row)) for row in tasks]
    task_rows.sort(
        key=lambda row: (
            (instant := _parse_instant(row.get("next_run_at"))) is None,
            instant or datetime.max.replace(tzinfo=timezone.utc),
            str(row.get("id") or ""),
        )
    )
    runtime_rows = [
        _project_runtime_agent(dict(row))
        for row in runtime_snapshot.get("agents", [])
        if isinstance(row, dict)
    ]
    anomalies: list[dict[str, Any]] = []

    controller_available = bool(runtime_snapshot.get("available"))
    ownership_available = bool(runtime_snapshot.get("ownership_available"))
    owned_run_ids = {
        str(run_id)
        for run_id in runtime_snapshot.get("owned_run_ids", [])
        if str(run_id or "").strip()
    }
    if not controller_available:
        anomalies.append(
            {
                "code": "controller_unavailable",
                "severity": "error",
                "detail": runtime_snapshot.get("error") or "controller_unavailable",
            }
        )
    elif not ownership_available:
        anomalies.append(
            {
                "code": "run_ownership_unknown",
                "severity": "error",
                "detail": runtime_snapshot.get("ownership_error")
                or "ownership_unavailable",
            }
        )

    for run in run_rows:
        if run["status"] != "running":
            continue
        if controller_available and ownership_available:
            if str(run["id"]) in owned_run_ids:
                run["liveness"] = "owned"
            else:
                run["liveness"] = "owner_missing"
                run["anomaly_codes"].append("run_owner_missing")
                anomalies.append(
                    {
                        "code": "run_owner_missing",
                        "severity": "error",
                        "run_id": run["id"],
                        "last_activity_at": run["last_activity_at"],
                        "activity_age_seconds": run["activity_age_seconds"],
                    }
                )

    for watch in watch_rows:
        if (
            watch.get("lifecycle_state") != "waiting"
            or watch.get("process_alive") is not False
        ):
            continue
        failed = bool(str(watch.get("last_error") or "").strip()) or watch.get("health") == "failing"
        code = "watch_waiter_failed" if failed else "watch_waiter_missing"
        watch["anomaly_codes"] = [code]
        anomalies.append(
            {
                "code": code,
                "severity": "error",
                "watch_id": watch.get("id"),
                "last_error": watch.get("last_error"),
                "last_exit_code": watch.get("last_exit_code"),
            }
        )

    active_by_workdir: dict[str, list[dict[str, Any]]] = {}
    for runtime in runtime_rows:
        if runtime.get("state") != "active":
            continue
        workdir = _canonical_workdir(runtime.get("workdir"))
        if workdir:
            active_by_workdir.setdefault(workdir, []).append(runtime)
    for workdir, members in active_by_workdir.items():
        identities = {_runtime_identity(member) for member in members}
        backends = {str(member.get("backend") or "") for member in members}
        pids = {
            int(member["pid"])
            for member in members
            if isinstance(member.get("pid"), int)
        }
        # Several Session rows can legitimately project one shared backend process
        # (Codex is cwd-scoped). That is one writer, not a conflict. Distinct
        # backends or distinct process ids are the evidence this read-only view has
        # for concurrent writers; pid-less rows remain separate runtime owners.
        pidless_identities = {
            _runtime_identity(member)
            for member in members
            if not isinstance(member.get("pid"), int)
        }
        writer_count = (
            len(backends)
            if len(backends) > 1
            else len(pids) + len(pidless_identities)
        )
        if writer_count < 2:
            continue
        for member in members:
            member.setdefault("anomaly_codes", []).append("active_workdir_conflict")
        anomalies.append(
            {
                "code": "active_workdir_conflict",
                "severity": "warning",
                "workdir": workdir,
                "session_ids": sorted(
                    {
                        str(member.get("session_id") or member.get("base_session_id"))
                        for member in members
                    }
                ),
                "backends": sorted({str(member.get("backend")) for member in members}),
            }
        )

    return {
        "generated_at": observed_now.isoformat(),
        "controller": {
            "available": controller_available,
            "ownership_available": ownership_available,
            "error": runtime_snapshot.get("error")
            or runtime_snapshot.get("ownership_error"),
        },
        "runs": run_rows,
        "watches": watch_rows,
        "tasks": task_rows,
        "runtime_agents": runtime_rows,
        "anomalies": anomalies,
        "counts": {
            "active_runs": len(run_rows),
            "armed_watches": len(watch_rows),
            "enabled_tasks": len(task_rows),
            "runtime_agents": len(runtime_rows),
            "anomalies": len(anomalies),
        },
        "truncated": dict(truncated or {}),
    }
