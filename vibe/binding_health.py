"""Generation-scoped evidence from the scheduled, authenticated revision read.

An issuer rejection is not a deletion verdict. This module never clears a pairing,
changes the access preference, or stops a running connector.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import time
from urllib.parse import quote, urlsplit

from config import paths
from config.atomic_io import write_atomic
from config.v2_config import V2Config, config_file_lock
from vibe.account_session_revoke import pairing_epoch


def _path():
    return paths.get_state_dir() / "remote-binding-rejection.json"


def rejected_at(config: V2Config | None) -> float | None:
    return rejected_at_epoch(pairing_epoch(config))


def rejected_at_epoch(epoch: str | None) -> float | None:
    if not epoch:
        return None
    try:
        record = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("epoch") != epoch or type(record.get("since")) not in (int, float):
        return None
    return record["since"]


def _set(config: V2Config, *, rejected: bool) -> None:
    """Pairing writers share this lock. A delayed read cannot affect another generation."""
    with config_file_lock():
        current = V2Config.load()
        if not current.remote_access.vibe_cloud.enabled or pairing_epoch(current) != pairing_epoch(config):
            return
        from vibe import remote_access
        journal, error = remote_access._read_pending_pairing_record()
        if error or journal:
            return  # An exact redeem may already have committed; recovery owns this verdict.
        if rejected:
            if rejected_at(current) is None:
                write_atomic(_path(), json.dumps({"epoch": pairing_epoch(current), "since": time.time()}))
        else:
            _path().unlink(missing_ok=True)


def connector_start_denial(config: V2Config | None) -> str | None:
    from vibe.binding_admission import reset_in_progress
    if reset_in_progress():
        return 'reset_in_progress'
    return 'binding_rejected' if rejected_at(config) is not None else None


def clear_after_publication() -> None:
    """Called under the same config lock that has just installed a new pairing."""
    _path().unlink(missing_ok=True)


def read_revision(config: V2Config) -> dict:
    """The issuer gets its secret only over the validated pairing transport.

    Never treat a response from the proxy handshake, a redirect, a malformed
    reply or an arbitrary error page as evidence about this installation.
    """
    from vibe import remote_access

    creds = config.remote_access.vibe_cloud.runtime_credentials()
    if not creds:
        raise ValueError("missing_binding")
    base, instance, secret = creds
    target, _ = remote_access._normalize_pairing_backend_url(base)
    if target is None or instance in {".", ".."}:
        raise ValueError("revision_unavailable")
    proxy = remote_access._validated_backend_proxy_url(target.base_url)
    if target.requires_proxy and not proxy:
        raise ValueError("revision_unavailable")
    path = urlsplit(target.base_url).path.rstrip("/") + "/api/v1/instances/" + quote(instance, safe="") + "/authorization-revision"
    for conn in remote_access._validated_backend_connections(target, 8, proxy):
        try:
            try:
                conn.connect()
            except (OSError, http.client.HTTPException):
                continue
            conn.request("GET", path, headers={"Host": target.host_header, "Accept": "application/json",
                         "Cache-Control": "no-cache, no-store", "X-Vibe-Device-Secret": secret})
            response = conn.getresponse()
            if response.status not in (200, 401) or (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
                break
            if (response.getheader("Age") or "0").strip() != "0":
                break
            raw = response.read(4097)
            if len(raw) > 4096:
                break
            data = json.loads(raw)
            if response.status == 401 and data == {"error": "invalid_device_secret"}:
                raise remote_access.BackendRequestError(401, {"error": "invalid_device_secret"})
            if response.status == 200 and isinstance(data, dict):
                remote_access._normalize_authorization_revision(data.get("authorization_revision"))
                return data
            break
        finally:
            conn.close()
    raise ValueError("revision_unavailable")


@dataclass
class RevisionMonitor:
    """One instance per scheduled worker. Restart drops partial evidence, not the verdict."""
    epoch: str | None = None
    first: float | None = None
    next_probe: float = 0.0

    def reset(self) -> None:
        self.epoch, self.first, self.next_probe = None, None, 0.0

    def due(self, config: V2Config, now: float) -> bool:
        epoch = pairing_epoch(config)
        if epoch != self.epoch or not config.remote_access.vibe_cloud.enabled:
            self.epoch, self.first, self.next_probe = epoch, None, 0.0
        return now >= self.next_probe

    def observe(self, config: V2Config, result: str, now: float) -> None:
        if pairing_epoch(config) != self.epoch:
            return
        if result == "rejected":
            if self.first is None:
                self.first = now
            elif now - self.first >= 15 and rejected_at(config) is None:
                _set(config, rejected=True)
        else:
            self.first = None
            if result == "accepted" and rejected_at(config) is not None:
                _set(config, rejected=False)
        since = rejected_at(config)
        self.next_probe = now + (300 if since is not None and time.time() - since >= 120 else 15)
