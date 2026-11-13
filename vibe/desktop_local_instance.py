"""Local Desktop enrollment correlation; pairing recovery stays with remote_access."""

from __future__ import annotations

import hashlib
import json
import os
import socket
from typing import Any, Mapping
from uuid import UUID, uuid4

from config import paths
from config.atomic_io import write_atomic
from config.v2_config import V2Config, config_file_lock, config_recovery_refusal
from vibe import remote_access


class LocalInstanceError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


def _identity_path():
    return paths.get_state_dir() / "installation-id.json"


def _installation_id() -> str | None:
    path = _identity_path()
    if not os.path.lexists(path):
        return None
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("invalid identity file")
        value = json.loads(path.read_text(encoding="utf-8"))
        identifier = value["installation_id"]
        if value["schema_version"] != 1 or str(UUID(identifier)) != identifier:
            raise ValueError("invalid installation identity")
        return identifier
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LocalInstanceError("local_identity_unavailable", 503) from exc


def _config() -> V2Config:
    try:
        config = V2Config.load()
    except FileNotFoundError:
        config = V2Config.default()
    if config_recovery_refusal(config, "remote_access"):
        raise LocalInstanceError("local_binding_unavailable", 503)
    return config


def _pending() -> dict[str, Any] | None:
    pending, error = remote_access._read_pending_pairing_record()
    if error:
        raise LocalInstanceError("local_enrollment_unavailable", 503)
    return pending


def _revision(config: V2Config, installation_id: str | None, pending: Mapping | None) -> str:
    # Hash the complete binding, including its private credential generation.
    # This comparison value is neither a credential nor an ownership claim.
    binding = remote_access._pairing_identity_fingerprint(
        remote_access._pairing_identity_from_cloud(config.remote_access.vibe_cloud)
    )
    return hashlib.sha256(json.dumps([
        binding, installation_id, pending.get("operation_id") if pending else None,
    ], separators=(",", ":")).encode()).hexdigest()


def _descriptor(config: V2Config, installation_id: str | None, pending: Mapping | None) -> dict[str, Any]:
    cloud = config.remote_access.vibe_cloud
    enrollment = None
    if pending and isinstance(pending.get("desktop_enrollment"), dict):
        desktop = pending["desktop_enrollment"]
        phase = pending["phase"]
        state = (
            "failed" if pending.get("desktop_failure") or phase in {"revoked", "retirement_pending"}
            else "reconciling" if phase in {"redeemed", "applied"}
            else "redeeming" if pending.get("redeem_request")
            else "prepared"
        )
        enrollment = {
            "operation_id": pending["operation_id"],
            "account_id": desktop["account_id"],
            "state": state,
        }
    return {
        "schema_version": 1,
        "installation_id": installation_id,
        "device_name": socket.gethostname(),
        "binding_revision": _revision(config, installation_id, pending),
        "pairing": {
            "instance_id": cloud.instance_id,
            "backend_url": cloud.backend_url,
            "public_url": cloud.public_url,
        } if cloud.instance_id else None,
        "remote_access": {
            "configured_enabled": cloud.enabled,
            "requested": remote_access._connector_requested_path().is_file(),
            "running": remote_access._cloudflared_pid_state(remote_access._read_pid()) == "cloudflared",
        },
        "enrollment": enrollment,
    }


def describe() -> dict[str, Any]:
    """Read a coherent, nonsecret snapshot without initializing an identity."""
    with config_file_lock():
        return _descriptor(_config(), _installation_id(), _pending())


def prepare(account_id: Any, expected_binding_revision: Any) -> dict[str, Any]:
    if (
        not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 200
        or not isinstance(expected_binding_revision, str)
    ):
        raise LocalInstanceError("invalid_request", 400)
    with config_file_lock():
        config, installation_id, pending = _config(), _installation_id(), _pending()
        if _revision(config, installation_id, pending) != expected_binding_revision:
            raise LocalInstanceError("local_binding_changed")
        cloud = config.remote_access.vibe_cloud
        if cloud.instance_id:
            return {"local": _descriptor(config, installation_id, pending), "operation_id": None}
        # A damaged partial binding is not an invitation to replace it.
        if any((cloud.instance_secret, cloud.tunnel_token, cloud.client_id, cloud.session_secret)):
            raise LocalInstanceError("local_binding_unavailable", 503)
        if pending:
            desktop = pending.get("desktop_enrollment")
            if (
                not isinstance(desktop, dict) or desktop["account_id"] != account_id
                or desktop["installation_id"] != installation_id
                or pending["source_fingerprint"] != remote_access._pairing_identity_fingerprint(
                    remote_access._pairing_identity_from_cloud(cloud)
                )
                or pending["phase"] in {"revoked", "retirement_pending"}
            ):
                raise LocalInstanceError("local_instance_conflict")
        else:
            if installation_id is None:
                installation_id = str(uuid4())
                write_atomic(_identity_path(), json.dumps({
                    "schema_version": 1, "installation_id": installation_id,
                }))
            pending = remote_access._new_pairing_claim(config, "", socket.gethostname())
            pending["desktop_enrollment"] = {
                "installation_id": installation_id, "account_id": account_id,
            }
            remote_access._write_pending_pairing_record(pending)
        return {
            "local": _descriptor(config, installation_id, pending),
            "operation_id": pending["operation_id"],
        }


def validate_guard(guard: Any, config: V2Config, pending: Mapping | None) -> None:
    """Called with the pairing lock held before any redemption or publication."""
    if not isinstance(guard, dict) or any(
        not isinstance(guard.get(key), str) or not guard[key]
        for key in ("installation_id", "operation_id", "account_id", "expected_binding_revision")
    ):
        raise LocalInstanceError("invalid_request", 400)
    installation_id = _installation_id()
    if _revision(config, installation_id, pending) != guard["expected_binding_revision"]:
        raise LocalInstanceError("local_binding_changed")
    if (
        not pending or pending["operation_id"] != guard["operation_id"]
        or pending.get("desktop_enrollment") != {
            "installation_id": guard["installation_id"], "account_id": guard["account_id"],
        }
        or installation_id != guard["installation_id"]
        or pending["phase"] in {"revoked", "retirement_pending"}
    ):
        raise LocalInstanceError("local_instance_conflict")
