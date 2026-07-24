"""Bridge Model Hub native subscription OAuth to the existing CLI login flows."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from .adapter import OAuthFlowState
from .events import contains_credential_material
from .oauth import NativeOAuthUnavailableError

_VENDOR_BACKENDS = {"anthropic": "claude", "openai": "codex"}
_INSTRUCTIONS_KEYS = {
    "anthropic": "settings.models.oauth.pasteCode.hint",
    "openai": "settings.models.oauth.deviceCode.hint",
}
_TIMEOUT_ERROR_KEY = "settings.models.oauth.error.timeout"
_GENERIC_ERROR_KEY = "settings.models.oauth.error.generic"
_MAX_FLOWS = 100
NATIVE_OAUTH_SIGNED_OUT_DETAIL_KEY = "settings.models.source.oauthSignedOut"


class AgentAuthService(Protocol):
    setup_timeout_seconds: float

    async def start_web_setup(self, backend: str, *, force_reset: bool = True) -> Any: ...

    def get_web_flow_status(self, flow_id: str) -> dict[str, Any]: ...

    async def submit_web_code(self, flow_id: str, code: str) -> dict[str, Any]: ...

    async def cancel_web_flow(self, flow_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class NativeOAuthSourceStatus:
    """Non-secret native source metadata resolved after CLI login succeeds."""

    signed_in: bool
    account_label: str | None


@dataclass
class _FlowBinding:
    source_id: str
    vendor: str
    backend: str
    expires_at_iso: str
    source_status: NativeOAuthSourceStatus | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_account_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 64
        or re.fullmatch(r"[^@\s]+@[^@\s]+", candidate) is None
        or contains_credential_material(candidate)
    ):
        return None
    return candidate


def _account_label(status: Mapping[str, Any]) -> str | None:
    account = status.get("chatgpt_account")
    candidates = (
        status.get("account_label"),
        status.get("email"),
        account.get("email") if isinstance(account, Mapping) else None,
    )
    return next((label for value in candidates if (label := _safe_account_label(value))), None)


def _signed_in(backend: str, status: Mapping[str, Any]) -> bool:
    if status.get("active_auth_mode") == "oauth":
        return True
    if backend == "claude":
        return status.get("has_oauth_credentials") is True
    if backend == "codex":
        if status.get("has_chatgpt_tokens") is True:
            return True
        # Codex's default keyring store is intentionally opaque to the file
        # status reader. The AgentAuthService success probe is authoritative in
        # that case, so do not turn a just-completed login into a false error.
        return status.get("auth_mode_uncertain") is True
    return False


class AgentAuthNativeOAuthAdapter:
    """Translate Model Hub OAuth flows to AgentAuthService web-login flows."""

    def __init__(
        self,
        agent_auth_service: AgentAuthService,
        *,
        auth_status_reader: Callable[[str], Mapping[str, Any]],
        now: Callable[[], datetime] = _utc_now,
    ):
        self._agent_auth_service = agent_auth_service
        self._auth_status_reader = auth_status_reader
        self._now = now
        self._flows: dict[str, _FlowBinding] = {}

    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState:
        backend = _VENDOR_BACKENDS.get(vendor)
        if backend is None:
            raise NativeOAuthUnavailableError

        flow = await self._agent_auth_service.start_web_setup(backend, force_reset=True)
        flow_id = getattr(flow, "flow_id", None)
        if not isinstance(flow_id, str) or not flow_id:
            raise NativeOAuthUnavailableError
        expires_at = self._now() + timedelta(seconds=self._agent_auth_service.setup_timeout_seconds)
        self._remember(
            flow_id,
            _FlowBinding(
                source_id=source_id,
                vendor=vendor,
                backend=backend,
                expires_at_iso=expires_at.isoformat(),
            ),
        )
        return await self._state_from_payload(flow_id, self._flow_payload(flow))

    async def oauth_status(self, flow_id: str) -> OAuthFlowState:
        binding = self._binding(flow_id)
        payload = self._agent_auth_service.get_web_flow_status(flow_id)
        if payload.get("ok") is not True:
            if payload.get("error") == "flow_not_found":
                self._flows.pop(flow_id, None)
                raise KeyError(flow_id)
            raise NativeOAuthUnavailableError
        return await self._state_from_payload(flow_id, payload, binding=binding)

    async def submit_oauth(self, flow_id: str, value: str) -> OAuthFlowState:
        binding = self._binding(flow_id)
        if binding.vendor != "anthropic":
            raise NativeOAuthUnavailableError
        result = await self._agent_auth_service.submit_web_code(flow_id, value)
        if result.get("ok") is not True:
            if result.get("error") == "flow_not_found":
                self._flows.pop(flow_id, None)
                raise KeyError(flow_id)
            raise NativeOAuthUnavailableError
        return await self.oauth_status(flow_id)

    async def cancel_oauth(self, flow_id: str) -> None:
        self._binding(flow_id)
        result = await self._agent_auth_service.cancel_web_flow(flow_id)
        if result.get("ok") is not True:
            if result.get("error") == "flow_not_found":
                self._flows.pop(flow_id, None)
                raise KeyError(flow_id)
            raise NativeOAuthUnavailableError
        self._flows.pop(flow_id, None)

    def completed_source_status(self, flow_id: str) -> NativeOAuthSourceStatus:
        status = self._binding(flow_id).source_status
        if status is None:
            raise KeyError(flow_id)
        return status

    def _binding(self, flow_id: str) -> _FlowBinding:
        try:
            return self._flows[flow_id]
        except KeyError:
            raise KeyError(flow_id) from None

    def _remember(self, flow_id: str, binding: _FlowBinding) -> None:
        self._flows.pop(flow_id, None)
        self._flows[flow_id] = binding
        while len(self._flows) > _MAX_FLOWS:
            self._flows.pop(next(iter(self._flows)))

    @staticmethod
    def _flow_payload(flow: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "flow_id": getattr(flow, "flow_id", None),
            "state": getattr(flow, "state", None),
            "url": getattr(flow, "url", None),
            "device_code": getattr(flow, "device_code", None),
            "error": getattr(flow, "error", None),
        }

    async def _state_from_payload(
        self,
        flow_id: str,
        payload: Mapping[str, Any],
        *,
        binding: _FlowBinding | None = None,
    ) -> OAuthFlowState:
        binding = binding or self._binding(flow_id)
        state = {
            "awaiting_code": "awaiting_action",
            "starting": "starting",
            "verifying": "verifying",
            "success": "success",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(str(payload.get("state")), "failed")
        if state == "success" and binding.source_status is None:
            binding.source_status = await self._read_source_status(binding.backend)

        error_key = None
        if state == "failed":
            error_key = _TIMEOUT_ERROR_KEY if payload.get("error") == "timed_out" else _GENERIC_ERROR_KEY
        elif state == "cancelled":
            error_key = _GENERIC_ERROR_KEY

        return OAuthFlowState(
            flow_id=flow_id,
            source_id=binding.source_id,
            vendor=binding.vendor,
            state=state,
            auth_url=payload.get("url") if isinstance(payload.get("url"), str) else None,
            device_code=(payload.get("device_code") if isinstance(payload.get("device_code"), str) else None),
            expects="paste_code" if binding.vendor == "anthropic" else "none",
            instructions_key=_INSTRUCTIONS_KEYS[binding.vendor],
            error_key=error_key,
            expires_at_iso=binding.expires_at_iso,
            credential_ref=None,
        )

    async def _read_source_status(self, backend: str) -> NativeOAuthSourceStatus:
        try:
            status = await asyncio.to_thread(self._auth_status_reader, backend)
            if not isinstance(status, Mapping):
                raise TypeError("invalid auth status")
        except Exception:  # noqa: BLE001
            # AgentAuthService reached success only after its own CLI status
            # probe. Preserve that verified signal when the display-status read
            # is temporarily unavailable; account identity can remain absent.
            return NativeOAuthSourceStatus(signed_in=True, account_label=None)
        return NativeOAuthSourceStatus(
            signed_in=_signed_in(backend, status),
            account_label=_account_label(status),
        )


def create_native_oauth_adapter() -> AgentAuthNativeOAuthAdapter:
    """Resolve the shared web-login service and sanctioned auth status readers."""

    from vibe import api

    def read_auth_status(backend: str) -> Mapping[str, Any]:
        if backend == "claude":
            return api.get_claude_auth()
        if backend == "codex":
            return api.get_codex_auth()
        raise NativeOAuthUnavailableError

    return AgentAuthNativeOAuthAdapter(
        api._get_oauth_service(),
        auth_status_reader=read_auth_status,
    )
