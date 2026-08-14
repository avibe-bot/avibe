"""OAuth channel dispatch state for Model Hub."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional, Protocol, get_args

from .adapter import OAuthFlowState

OAuthChannel = Literal["native_cli", "hub"]
OAuthNonceStatus = Literal["released", "in_flight", "committed"]
_CLIENT_NONCE_PATTERN = re.compile(r"^ofn_[a-z0-9]{16,64}$")
NATIVE_OAUTH_SIGNED_OUT_DETAIL_KEY = "settings.models.source.oauthSignedOut"


@dataclass(frozen=True)
class OAuthFlowBinding:
    channel: OAuthChannel
    source_id: Optional[str]
    vendor: Optional[str]
    intent: Literal["create", "reauth"] = "create"
    completed: bool = False
    recovered: bool | None = None
    interrupted_pairs: tuple[dict[str, object], ...] = ()
    client_nonce: str | None = None
    expires_at_iso: str | None = None
    terminal_state: Literal["cancelled"] | None = None


@dataclass(frozen=True)
class OAuthNonceClaim:
    """The result of claiming one exact OAuth-start nonce tuple."""

    client_nonce: str
    vendor: str
    channel: OAuthChannel
    status: OAuthNonceStatus
    flow_id: str | None = None
    owner: bool = False


@dataclass
class _PendingNonceClaim:
    client_nonce: str
    vendor: str
    channel: OAuthChannel
    event: threading.Event
    status: OAuthNonceStatus = "in_flight"
    flow_id: str | None = None


class OAuthAdapter(Protocol):
    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState: ...

    async def oauth_status(self, flow_id: str) -> OAuthFlowState: ...

    async def submit_oauth(self, flow_id: str, value: str) -> OAuthFlowState: ...

    async def cancel_oauth(self, flow_id: str) -> None: ...


@dataclass(frozen=True)
class NativeOAuthSourceStatus:
    """Non-secret native source metadata resolved after CLI login succeeds."""

    signed_in: bool
    account_label: str | None


class NativeOAuthAdapter(OAuthAdapter, Protocol):
    async def start_reauth(
        self,
        source_id: str,
        vendor: str,
        *,
        on_irreversible_start: Callable[
            [], Callable[[], None] | None
        ] | None = None,
    ) -> OAuthFlowState: ...

    def completed_source_status(self, flow_id: str) -> NativeOAuthSourceStatus: ...

    def release_login_slot(self, flow_id: str) -> None: ...


class NativeOAuthUnavailableError(RuntimeError):
    pass


class UnavailableNativeOAuthAdapter:
    """Fail closed until a native CLI OAuth integration is available."""

    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState:
        raise NativeOAuthUnavailableError

    async def start_reauth(
        self,
        source_id: str,
        vendor: str,
        *,
        on_irreversible_start: Callable[
            [], Callable[[], None] | None
        ] | None = None,
    ) -> OAuthFlowState:
        raise NativeOAuthUnavailableError

    async def oauth_status(self, flow_id: str) -> OAuthFlowState:
        raise NativeOAuthUnavailableError

    async def submit_oauth(self, flow_id: str, value: str) -> OAuthFlowState:
        raise NativeOAuthUnavailableError

    async def cancel_oauth(self, flow_id: str) -> None:
        raise NativeOAuthUnavailableError

    def completed_source_status(self, flow_id: str) -> NativeOAuthSourceStatus:
        raise NativeOAuthUnavailableError

    def release_login_slot(self, flow_id: str) -> None:
        raise NativeOAuthUnavailableError


class OAuthFlowRegistry:
    """Persist non-secret OAuth identity and own nonce claim transitions."""

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = 100,
        now: Callable[[], datetime] | None = None,
    ):
        self.path = path
        self.max_entries = max_entries
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._pending_nonces: dict[tuple[str, str, OAuthChannel], _PendingNonceClaim] = {}

    @staticmethod
    def _nonce_key(
        client_nonce: str,
        vendor: str,
        channel: OAuthChannel,
    ) -> tuple[str, str, OAuthChannel]:
        if not isinstance(client_nonce, str) or not _CLIENT_NONCE_PATTERN.fullmatch(client_nonce):
            raise ValueError("invalid OAuth client nonce")
        if not isinstance(vendor, str) or not vendor:
            raise ValueError("invalid OAuth vendor")
        if channel not in get_args(OAuthChannel):
            raise ValueError("invalid OAuth channel")
        return client_nonce, vendor, channel

    def _expired(self, expires_at_iso: str | None) -> bool:
        if expires_at_iso is None:
            return False
        expires_at = datetime.fromisoformat(expires_at_iso)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= self._now()

    def _latest_nonce_binding_locked(
        self,
        flows: dict[str, OAuthFlowBinding],
        key: tuple[str, str, OAuthChannel],
    ) -> tuple[str, OAuthFlowBinding] | None:
        for flow_id, binding in reversed(tuple(flows.items())):
            if (
                binding.client_nonce,
                binding.vendor,
                binding.channel,
            ) != key:
                continue
            if binding.expires_at_iso is not None and self._expired(binding.expires_at_iso):
                continue
            return flow_id, binding
        return None

    def _finish_pending_locked(
        self,
        key: tuple[str, str, OAuthChannel],
        *,
        status: OAuthNonceStatus,
        flow_id: str | None,
    ) -> None:
        pending = self._pending_nonces.get(key)
        if pending is None:
            return
        pending.status = status
        pending.flow_id = flow_id
        pending.event.set()
        self._pending_nonces.pop(key, None)

    def _read(self) -> dict[str, OAuthFlowBinding]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        flows = {}
        for flow_id, value in payload.items():
            if not isinstance(flow_id, str):
                continue
            if not isinstance(value, dict):
                continue
            if set(value) != {
                "channel",
                "source_id",
                "vendor",
                "intent",
                "completed",
                "recovered",
                "interrupted_pairs",
                "client_nonce",
                "expires_at_iso",
                "terminal_state",
            }:
                continue
            channel = value.get("channel")
            source_id = value.get("source_id")
            vendor = value.get("vendor")
            intent = value.get("intent")
            completed = value.get("completed")
            recovered = value.get("recovered")
            interrupted_pairs = value.get("interrupted_pairs")
            client_nonce = value.get("client_nonce")
            expires_at_iso = value.get("expires_at_iso")
            terminal_state = value.get("terminal_state")
            if (
                channel in {"native_cli", "hub"}
                and (source_id is None or (isinstance(source_id, str) and source_id))
                and (vendor is None or (isinstance(vendor, str) and vendor))
                and intent in {"create", "reauth"}
                and isinstance(completed, bool)
                and (recovered is None or isinstance(recovered, bool))
                and isinstance(interrupted_pairs, list)
                and all(isinstance(item, dict) for item in interrupted_pairs)
                and (
                    client_nonce is None
                    or (
                        isinstance(client_nonce, str)
                        and _CLIENT_NONCE_PATTERN.fullmatch(client_nonce)
                    )
                )
                and (expires_at_iso is None or isinstance(expires_at_iso, str))
                and terminal_state in {None, "cancelled"}
                and not (client_nonce and expires_at_iso is None)
            ):
                flows[flow_id] = OAuthFlowBinding(
                    channel=channel,
                    source_id=source_id,
                    vendor=vendor,
                    intent=intent,
                    completed=completed,
                    recovered=recovered,
                    interrupted_pairs=tuple(interrupted_pairs),
                    client_nonce=client_nonce,
                    expires_at_iso=expires_at_iso,
                    terminal_state=terminal_state,
                )
        return flows

    def _write(self, payload: dict[str, OAuthFlowBinding]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        bounded = dict(list(payload.items())[-self.max_entries :])
        content = json.dumps(
            {
                flow_id: {
                    "channel": binding.channel,
                    "source_id": binding.source_id,
                    "vendor": binding.vendor,
                    "intent": binding.intent,
                    "completed": binding.completed,
                    "recovered": binding.recovered,
                    "interrupted_pairs": list(binding.interrupted_pairs),
                    "client_nonce": binding.client_nonce,
                    "expires_at_iso": binding.expires_at_iso,
                    "terminal_state": binding.terminal_state,
                }
                for flow_id, binding in bounded.items()
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, self.path)

    def remember(
        self,
        flow_id: str,
        channel: OAuthChannel,
        source_id: str,
        vendor: str,
        *,
        intent: Literal["create", "reauth"] = "create",
        recovered: bool | None = None,
        replace_flow_id: str | None = None,
        client_nonce: str | None = None,
        expires_at_iso: str | None = None,
    ) -> None:
        with self._lock:
            flows = self._read()
            nonce_key = (
                self._nonce_key(client_nonce, vendor, channel)
                if client_nonce is not None
                else None
            )
            if client_nonce is not None and expires_at_iso is None:
                raise ValueError("nonce-bound OAuth flow requires an expiry")
            if nonce_key is not None:
                existing = self._latest_nonce_binding_locked(flows, nonce_key)
                if existing is not None and existing[0] not in {
                    flow_id,
                    replace_flow_id,
                }:
                    raise ValueError("OAuth client nonce is already committed")
            if replace_flow_id is not None:
                flows.pop(replace_flow_id, None)
            flows.pop(flow_id, None)
            flows[flow_id] = OAuthFlowBinding(
                channel=channel,
                source_id=source_id,
                vendor=vendor,
                intent=intent,
                recovered=recovered,
                client_nonce=client_nonce,
                expires_at_iso=expires_at_iso,
            )
            self._write(flows)
            if nonce_key is not None:
                self._finish_pending_locked(
                    nonce_key,
                    status="committed",
                    flow_id=flow_id,
                )

    def complete(
        self,
        flow_id: str,
        *,
        recovered: bool | None = None,
        interrupted_pairs: list[dict[str, object]] | None = None,
    ) -> None:
        with self._lock:
            flows = self._read()
            binding = flows.get(flow_id)
            if binding is None:
                raise KeyError(flow_id)
            flows[flow_id] = OAuthFlowBinding(
                channel=binding.channel,
                source_id=binding.source_id,
                vendor=binding.vendor,
                intent=binding.intent,
                completed=True,
                recovered=recovered,
                interrupted_pairs=tuple(interrupted_pairs or ()),
                client_nonce=binding.client_nonce,
                expires_at_iso=binding.expires_at_iso,
                terminal_state=binding.terminal_state,
            )
            self._write(flows)

    def retain_cancelled(self, flow_id: str) -> None:
        """Keep a nonce-bound flow replayable after explicit cancellation."""

        with self._lock:
            flows = self._read()
            binding = flows.get(flow_id)
            if binding is None:
                raise KeyError(flow_id)
            if binding.client_nonce is None:
                flows.pop(flow_id, None)
            else:
                flows[flow_id] = OAuthFlowBinding(
                    channel=binding.channel,
                    source_id=binding.source_id,
                    vendor=binding.vendor,
                    intent=binding.intent,
                    completed=binding.completed,
                    recovered=binding.recovered,
                    interrupted_pairs=binding.interrupted_pairs,
                    client_nonce=binding.client_nonce,
                    expires_at_iso=binding.expires_at_iso,
                    terminal_state="cancelled",
                )
            self._write(flows)

    def claim_nonce(
        self,
        client_nonce: str,
        vendor: str,
        channel: OAuthChannel,
    ) -> OAuthNonceClaim:
        """Atomically claim an OAuth-start tuple before provider work."""

        key = self._nonce_key(client_nonce, vendor, channel)
        with self._lock:
            flows = self._read()
            committed = self._latest_nonce_binding_locked(flows, key)
            if committed is not None:
                return OAuthNonceClaim(
                    client_nonce,
                    vendor,
                    channel,
                    "committed",
                    flow_id=committed[0],
                )
            pending = self._pending_nonces.get(key)
            if pending is not None:
                return OAuthNonceClaim(
                    client_nonce,
                    vendor,
                    channel,
                    pending.status,
                    flow_id=pending.flow_id,
                )
            self._pending_nonces[key] = _PendingNonceClaim(
                client_nonce,
                vendor,
                channel,
                threading.Event(),
            )
            return OAuthNonceClaim(
                client_nonce,
                vendor,
                channel,
                "in_flight",
                owner=True,
            )

    async def wait_for_nonce(self, claim: OAuthNonceClaim) -> OAuthNonceClaim:
        """Await the owner settling an in-flight nonce claim."""

        key = self._nonce_key(claim.client_nonce, claim.vendor, claim.channel)
        with self._lock:
            pending = self._pending_nonces.get(key)
            if pending is None:
                flows = self._read()
                committed = self._latest_nonce_binding_locked(flows, key)
                return OAuthNonceClaim(
                    claim.client_nonce,
                    claim.vendor,
                    claim.channel,
                    "committed" if committed is not None else "released",
                    flow_id=committed[0] if committed else None,
                )
            event = pending.event
        await asyncio.to_thread(event.wait)
        with self._lock:
            flows = self._read()
            committed = self._latest_nonce_binding_locked(flows, key)
            if committed is not None:
                return OAuthNonceClaim(
                    claim.client_nonce,
                    claim.vendor,
                    claim.channel,
                    "committed",
                    flow_id=committed[0],
                )
            return OAuthNonceClaim(
                claim.client_nonce,
                claim.vendor,
                claim.channel,
                "released",
            )

    def release_nonce(
        self,
        client_nonce: str,
        vendor: str,
        channel: OAuthChannel,
    ) -> None:
        """Release a failed or cancelled pre-flow claim after cleanup."""

        key = self._nonce_key(client_nonce, vendor, channel)
        with self._lock:
            self._finish_pending_locked(key, status="released", flow_id=None)

    def channel(self, flow_id: str) -> OAuthChannel | None:
        binding = self.binding(flow_id)
        return binding.channel if binding is not None else None

    def binding(self, flow_id: str) -> OAuthFlowBinding | None:
        with self._lock:
            return self._read().get(flow_id)

    def pending_reauth(
        self,
        source_id: str,
    ) -> tuple[str, OAuthFlowBinding] | None:
        with self._lock:
            flows = self._read()
            for flow_id, binding in reversed(tuple(flows.items())):
                if (
                    binding.source_id == source_id
                    and binding.intent == "reauth"
                    and not binding.completed
                ):
                    return flow_id, binding
        return None

    def forget(self, flow_id: str) -> None:
        with self._lock:
            flows = self._read()
            if flow_id not in flows:
                return
            flows.pop(flow_id)
            self._write(flows)
