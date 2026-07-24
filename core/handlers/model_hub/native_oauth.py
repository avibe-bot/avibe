"""Bridge Model Hub native subscription OAuth to the existing CLI login flows."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from .adapter import OAuthFlowState
from .events import contains_credential_material
from .oauth import NativeOAuthSourceStatus, NativeOAuthUnavailableError

_VENDOR_BACKENDS = {"anthropic": "claude", "openai": "codex"}
_INSTRUCTIONS_KEYS = {
    "anthropic": "settings.models.oauth.pasteCode.hint",
    "openai": "settings.models.oauth.deviceCode.hint",
}
_TIMEOUT_ERROR_KEY = "settings.models.oauth.error.timeout"
_GENERIC_ERROR_KEY = "settings.models.oauth.error.generic"
_MAX_FLOWS = 100


class AgentAuthService(Protocol):
    setup_timeout_seconds: float

    async def start_web_setup(self, backend: str, *, force_reset: bool = True) -> Any: ...

    def get_web_flow_status(self, flow_id: str) -> dict[str, Any]: ...

    async def submit_web_code(self, flow_id: str, code: str) -> dict[str, Any]: ...

    async def cancel_web_flow(self, flow_id: str) -> dict[str, Any]: ...


@dataclass
class _FlowBinding:
    source_id: str
    vendor: str
    backend: str
    expires_at_iso: str
    source_status: NativeOAuthSourceStatus | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_label_part(value: object, *, email: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 64
        or any(ord(character) < 32 for character in candidate)
        or (email and re.fullmatch(r"[^@\s]+@[^@\s]+", candidate) is None)
        or contains_credential_material(candidate)
    ):
        return None
    return candidate


def _account_label(status: Mapping[str, Any]) -> str | None:
    account = status.get("chatgpt_account")
    explicit = _safe_label_part(status.get("account_label"))
    if explicit is not None:
        return explicit

    email = _safe_label_part(status.get("email"), email=True)
    if isinstance(account, Mapping):
        email = email or _safe_label_part(account.get("email"), email=True)
        plan_type = _safe_label_part(account.get("plan_type"))
        organizations = account.get("organizations")
        organization = None
        if isinstance(organizations, list):
            candidates = [item for item in organizations if isinstance(item, Mapping)]
            selected = next((item for item in candidates if item.get("is_default") is True), None)
            selected = selected or next(iter(candidates), None)
            if selected is not None:
                organization = _safe_label_part(selected.get("title"))
        parts = [part for part in (email, plan_type, organization) if part is not None]
        if parts:
            label = " \u00b7 ".join(parts)
            return label if len(label) <= 192 else email
    return email


def _signed_in(backend: str, status: Mapping[str, Any]) -> bool:
    active_auth_mode = status.get("active_auth_mode")
    if active_auth_mode == "oauth":
        return True
    if active_auth_mode not in {None, "none"}:
        return False
    if backend == "claude":
        if active_auth_mode == "none":
            return False
        return status.get("has_oauth_credentials") is True
    if backend == "codex":
        # AgentAuthService reports success only after its own Codex CLI probe.
        # The Settings status reader cannot see tokens in the default keyring,
        # so an otherwise non-API-key completion must trust that probe.
        return True
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

        flow = await self._agent_auth_service.start_web_setup(backend, force_reset=False)
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
