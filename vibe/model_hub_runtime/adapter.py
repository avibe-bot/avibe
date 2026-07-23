from __future__ import annotations

import asyncio
import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from core.handlers.model_hub.adapter import (
    EngineHealth,
    EngineStatus,
    OAuthFlowState,
    OriginNotAllowedError,
    RawCallOutcome,
    RawOutcomeKind,
    SourceBinding,
)
from vibe.model_hub_runtime.client import (
    EngineClient,
    EngineClientError,
    EngineInvokeHandle,
    completed_handle,
    probe_models,
)
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore
from vibe.model_hub_runtime.supervisor import (
    EngineSupervisor,
    EngineUnavailableError,
    get_engine_supervisor,
)


_OAUTH_ENDPOINTS = {
    "anthropic": ("/anthropic-auth-url", "anthropic", "claude"),
    "openai": ("/codex-auth-url", "codex", "codex"),
    "codex": ("/codex-auth-url", "codex", "codex"),
    "antigravity": ("/antigravity-auth-url", "antigravity", "antigravity"),
    "kimi": ("/kimi-auth-url", "kimi", "kimi"),
    "xai": ("/xai-auth-url", "xai", "xai"),
}
_WEBUI_OAUTH_VENDORS = frozenset({"anthropic", "openai", "codex", "antigravity"})


@dataclass(frozen=True)
class _AuthRecord:
    identity: str
    name: str
    provider: str
    fingerprint: str


@dataclass
class _OAuthFlow:
    flow_id: str
    source_id: str
    engine_state: str
    vendor: str
    callback_provider: str
    auth_provider: str
    expects: str
    auth_url: str | None
    device_code: str | None
    expires_at_iso: str
    before_auth_fingerprints: dict[str, str]
    state: str = "awaiting_action"
    error_key: str | None = None
    credential_ref: str | None = None
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def snapshot(self) -> OAuthFlowState:
        return OAuthFlowState(
            flow_id=self.flow_id,
            source_id=self.source_id,
            vendor=self.vendor,
            state=self.state,
            auth_url=self.auth_url,
            device_code=self.device_code,
            expects=self.expects,
            instructions_key=f"models.oauth.{self.vendor}.{self.expects}",
            error_key=self.error_key,
            expires_at_iso=self.expires_at_iso,
            credential_ref=self.credential_ref,
        )


class CLIProxyEngineAdapter:
    """Managed CLIProxyAPI implementation of the frozen EngineAdapter contract."""

    def __init__(
        self,
        *,
        supervisor: EngineSupervisor | None = None,
        state_store: EngineStateStore | None = None,
    ) -> None:
        self.supervisor = supervisor or get_engine_supervisor()
        self.state_store = state_store or self.supervisor.state_store
        self._routing_lock = asyncio.Lock()
        self._oauth_flows: dict[str, _OAuthFlow] = {}
        self._active_oauth_providers: set[str] = set()
        self._oauth_lock = threading.RLock()

    async def ensure_installed(self) -> EngineStatus:
        async with self._routing_lock:
            install = await asyncio.to_thread(self.supervisor.installer.ensure)
            if not install.get("ok"):
                reason = str(install.get("reason") or "engine_install_failed")
                raise EngineUnavailableError("models.engine.install_failed", reason=reason)
            if install.get("changed"):
                await asyncio.to_thread(self.supervisor.restart_if_running)
            return await self.status()

    async def start(self) -> EngineStatus:
        await asyncio.to_thread(self.supervisor.ensure_running)
        return await self.status()

    async def stop(self) -> None:
        await asyncio.to_thread(self.supervisor.stop)

    async def status(self) -> EngineStatus:
        raw = await asyncio.to_thread(self.supervisor.status)
        status = raw["status"]
        listening = status.get("listening") or {}
        return EngineStatus(
            health=EngineHealth(status["health"]),
            installed_version=status.get("installed_version"),
            verified=bool(status.get("verified")),
            listen_host="127.0.0.1",
            listen_port=listening.get("port"),
            last_check_iso=status.get("last_check"),
        )

    async def gateway_token(self) -> str:
        connection = await asyncio.to_thread(self.supervisor.ensure_running)
        return connection.gateway_token

    async def sync_sources(self, bindings: Sequence[SourceBinding]) -> None:
        async with self._routing_lock:
            previous = await asyncio.to_thread(self.state_store.list_sources)
            was_running = await asyncio.to_thread(self.supervisor.client_if_running) is not None
            await asyncio.to_thread(self.state_store.sync_sources, bindings)
            try:
                await asyncio.to_thread(self.supervisor.restart_if_running)
            except Exception:
                await asyncio.to_thread(self.state_store.replace_sources, previous)
                if was_running:
                    try:
                        await asyncio.to_thread(self.supervisor.ensure_running)
                    except Exception as restore_error:
                        raise EngineStateError(
                            "source sync failed and the previous engine state could not be restored"
                        ) from restore_error
                raise

    async def provision_credential(
        self,
        vendor: str,
        protocol: str,
        secret: str,
        base_url: str | None,
    ) -> str:
        return await asyncio.to_thread(
            self.state_store.store_api_key,
            secret,
            vendor=vendor,
            protocol=protocol,
            base_url=base_url,
        )

    async def revoke_credential(self, credential_ref: str) -> None:
        await asyncio.to_thread(
            self.state_store.assert_credential_unbound,
            credential_ref,
        )
        metadata = await asyncio.to_thread(
            self.state_store.credential_metadata,
            credential_ref,
        )
        auth_name = metadata.get("auth_name") if metadata["kind"] == "oauth" else None
        if auth_name:
            client = await asyncio.to_thread(self.supervisor.client_if_running)
            if client is not None:
                try:
                    await asyncio.to_thread(
                        client.management_request,
                        "DELETE",
                        "/auth-files",
                        query={"name": str(auth_name)},
                        timeout=1.0,
                    )
                except EngineClientError:
                    pass
            await asyncio.to_thread(self.state_store.delete_oauth_auth_file, str(auth_name))
            await asyncio.to_thread(self.state_store.audit_auth_permissions, enforce=True)
        await asyncio.to_thread(self.supervisor.invalidate_configs)
        await asyncio.to_thread(self.state_store.revoke_credential, credential_ref)

    async def discover_models(
        self,
        vendor: str,
        protocol: str,
        base_url: str | None,
        credential_ref: str,
    ) -> Sequence[str]:
        metadata = await asyncio.to_thread(
            self.state_store.credential_metadata,
            credential_ref,
        )
        normalized_vendor = vendor.strip().lower()
        if metadata["kind"] == "oauth":
            if metadata.get("vendor") != normalized_vendor or base_url is not None:
                raise EngineStateError("credential does not match discovery target")
            client = await asyncio.to_thread(self.supervisor.client)
            payload = await asyncio.to_thread(
                client.management_request,
                "GET",
                "/auth-files/models",
                query={"name": str(metadata["auth_name"])},
            )
            return _model_ids(payload)
        normalized_base_url = await asyncio.to_thread(
            self.state_store.validate_api_key_target,
            credential_ref,
            vendor=normalized_vendor,
            protocol=protocol,
            base_url=base_url,
        )
        secret = await asyncio.to_thread(self.state_store.read_api_key, credential_ref)
        try:
            return await probe_models(
                vendor=normalized_vendor,
                protocol=protocol,
                base_url=normalized_base_url,
                secret=secret,
            )
        except EngineClientError as exc:
            raise EngineStateError("model discovery failed") from exc

    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState:
        await asyncio.to_thread(self.state_store.validate_source_id, source_id)
        normalized_vendor = vendor.strip().lower()
        endpoint = _OAUTH_ENDPOINTS.get(normalized_vendor)
        if endpoint is None:
            raise EngineStateError("unsupported OAuth vendor")
        engine_endpoint, callback_provider, auth_provider = endpoint
        with self._oauth_lock:
            self._expire_oauth_flows_locked()
            if auth_provider in self._active_oauth_providers:
                raise EngineStateError("an OAuth flow for this provider is already active")
            self._active_oauth_providers.add(auth_provider)
        try:
            client = await asyncio.to_thread(self.supervisor.client)
            before = await asyncio.to_thread(_auth_inventory, client)
            payload = await asyncio.to_thread(
                client.management_request,
                "GET",
                engine_endpoint,
                query={"is_webui": "true"} if normalized_vendor in _WEBUI_OAUTH_VENDORS else None,
            )
            engine_state = str(payload.get("state") or "").strip()
            if not engine_state:
                raise EngineStateError("engine OAuth response omitted state")
            device_code = str(payload.get("user_code") or "").strip() or None
            flow_kind = str(payload.get("flow") or "").strip().lower()
            expects = "none" if device_code or flow_kind == "device" else "paste_callback_url"
            expires_in = max(1, int(payload.get("expires_in") or 300))
            flow = _OAuthFlow(
                flow_id=f"oaf_{secrets.token_hex(12)}",
                source_id=source_id,
                engine_state=engine_state,
                vendor=normalized_vendor,
                callback_provider=callback_provider,
                auth_provider=auth_provider,
                expects=expects,
                auth_url=str(payload.get("url") or payload.get("verification_uri") or "").strip() or None,
                device_code=device_code,
                expires_at_iso=(datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(),
                before_auth_fingerprints={identity: record.fingerprint for identity, record in before.items()},
            )
        except Exception:
            with self._oauth_lock:
                self._active_oauth_providers.discard(auth_provider)
            raise
        with self._oauth_lock:
            self._oauth_flows[flow.flow_id] = flow
        return flow.snapshot()

    async def oauth_status(self, flow_id: str) -> OAuthFlowState:
        flow = self._get_flow(flow_id)
        async with flow.operation_lock:
            self._expire_oauth_flow(flow)
            if flow.state in {"success", "failed", "cancelled"}:
                return flow.snapshot()
            try:
                client = await asyncio.to_thread(self.supervisor.client)
                payload = await asyncio.to_thread(
                    client.management_request,
                    "GET",
                    "/get-auth-status",
                    query={"state": flow.engine_state},
                )
                status = str(payload.get("status") or "").strip().lower()
                if status == "ok":
                    await self._complete_oauth(flow, client)
                elif status == "error":
                    self._fail_flow(flow, "models.oauth.upstream_failed")
                elif flow.state != "verifying":
                    flow.state = "awaiting_action"
            except (EngineClientError, EngineUnavailableError):
                self._fail_flow(flow, "models.oauth.engine_unavailable")
            return flow.snapshot()

    async def submit_oauth(self, flow_id: str, value: str) -> OAuthFlowState:
        flow = self._get_flow(flow_id)
        async with flow.operation_lock:
            self._expire_oauth_flow(flow)
            if flow.state in {"success", "failed", "cancelled"}:
                raise EngineStateError("OAuth flow is no longer active")
            if flow.expects == "none":
                raise EngineStateError("this OAuth flow does not accept a submission")
            submitted = value.strip()
            if not submitted:
                raise EngineStateError("OAuth submission is empty")
            payload: dict[str, str] = {
                "provider": flow.callback_provider,
                "state": flow.engine_state,
            }
            if submitted.startswith(("http://", "https://")):
                payload["redirect_url"] = submitted
            else:
                payload["code"] = submitted
            try:
                client = await asyncio.to_thread(self.supervisor.client)
                await asyncio.to_thread(
                    client.management_request,
                    "POST",
                    "/oauth-callback",
                    payload=payload,
                )
            except (EngineClientError, EngineUnavailableError):
                self._fail_flow(flow, "models.oauth.submission_failed")
                return flow.snapshot()
            flow.state = "verifying"
            return flow.snapshot()

    async def cancel_oauth(self, flow_id: str) -> None:
        flow = self._get_flow(flow_id)
        async with flow.operation_lock:
            self._expire_oauth_flow(flow)
            if flow.state in {"success", "failed", "cancelled"}:
                return
            try:
                client = await asyncio.to_thread(self.supervisor.client)
                await asyncio.to_thread(
                    client.management_request,
                    "DELETE",
                    "/oauth-session",
                    query={"state": flow.engine_state},
                )
            except (EngineClientError, EngineUnavailableError):
                pass
            flow.state = "cancelled"
            self._release_provider(flow)

    async def invoke(
        self,
        source_id: str,
        model_id: str,
        request: Mapping[str, Any],
        stream: bool,
        origin: str,
    ) -> EngineInvokeHandle:
        async with self._routing_lock:
            source = await asyncio.to_thread(self.state_store.get_source, source_id)
            if source is None:
                raise EngineStateError("source is not registered")
            if source.allowed_origins and origin not in source.allowed_origins:
                raise OriginNotAllowedError(
                    f"origin {origin!r} is not allowed to use source {source_id!r}"
                )
            try:
                client = await asyncio.to_thread(self.supervisor.client)
            except EngineUnavailableError:
                return completed_handle(
                    RawCallOutcome(
                        kind=RawOutcomeKind.NETWORK_ERROR,
                        http_status=None,
                        error_code=None,
                        redacted_message=None,
                        stream_started=False,
                        model_id=model_id,
                        source_id=source_id,
                    )
                )
            return await client.invoke(source, model_id, request, stream=stream)

    async def _complete_oauth(self, flow: _OAuthFlow, client: EngineClient) -> None:
        inventory = await asyncio.to_thread(_auth_inventory, client)
        provider_records = [record for record in inventory.values() if record.provider == flow.auth_provider]
        candidates = [
            record
            for record in provider_records
            if flow.before_auth_fingerprints.get(record.identity) != record.fingerprint
        ]
        if not candidates and len(provider_records) == 1:
            candidates = provider_records
        if len(candidates) != 1:
            if not candidates:
                flow.state = "verifying"
                return
            self._fail_flow(flow, "models.oauth.ambiguous_engine_binding")
            return
        auth = candidates[0]
        try:
            existing_credential_ref = await asyncio.to_thread(
                self.state_store.oauth_credential_ref,
                auth.name,
            )
            credential_ref = await asyncio.to_thread(
                self.state_store.bind_oauth_credential,
                flow.source_id,
                flow.vendor,
                auth.name,
            )
            credential = await asyncio.to_thread(
                self.state_store.credential_metadata,
                credential_ref,
            )
        except EngineStateError:
            self._fail_flow(flow, "models.oauth.binding_failed")
            return
        try:
            await asyncio.to_thread(
                client.management_request,
                "PATCH",
                "/auth-files/fields",
                payload={"name": auth.name, "prefix": credential["prefix"]},
            )
            await asyncio.to_thread(self.state_store.audit_auth_permissions, enforce=True)
        except (EngineClientError, EngineStateError):
            if existing_credential_ref is None:
                if auth.identity not in flow.before_auth_fingerprints:
                    try:
                        await asyncio.to_thread(
                            client.management_request,
                            "DELETE",
                            "/auth-files",
                            query={"name": auth.name},
                        )
                    except EngineClientError:
                        pass
                    try:
                        await asyncio.to_thread(
                            self.state_store.delete_oauth_auth_file,
                            auth.name,
                        )
                    except EngineStateError:
                        pass
                try:
                    await asyncio.to_thread(
                        self.state_store.revoke_credential,
                        credential_ref,
                    )
                except EngineStateError:
                    pass
            self._fail_flow(flow, "models.oauth.binding_failed")
            return
        flow.credential_ref = credential_ref
        flow.state = "success"
        self._release_provider(flow)

    def _get_flow(self, flow_id: str) -> _OAuthFlow:
        with self._oauth_lock:
            self._expire_oauth_flows_locked()
            flow = self._oauth_flows.get(flow_id)
        if flow is None:
            raise EngineStateError("OAuth flow is unknown")
        return flow

    def _fail_flow(self, flow: _OAuthFlow, error_key: str) -> None:
        flow.state = "failed"
        flow.error_key = error_key
        self._release_provider(flow)

    def _release_provider(self, flow: _OAuthFlow) -> None:
        with self._oauth_lock:
            self._active_oauth_providers.discard(flow.auth_provider)

    def _expire_oauth_flow(self, flow: _OAuthFlow) -> None:
        with self._oauth_lock:
            self._expire_oauth_flow_locked(flow, datetime.now(timezone.utc))

    def _expire_oauth_flows_locked(self) -> None:
        now = datetime.now(timezone.utc)
        for flow in self._oauth_flows.values():
            if flow.operation_lock.locked():
                continue
            self._expire_oauth_flow_locked(flow, now)

    def _expire_oauth_flow_locked(self, flow: _OAuthFlow, now: datetime) -> None:
        if flow.state in {"success", "failed", "cancelled"}:
            return
        try:
            expires_at = datetime.fromisoformat(flow.expires_at_iso)
        except ValueError:
            expires_at = now
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            flow.state = "failed"
            flow.error_key = "models.oauth.expired"
            self._active_oauth_providers.discard(flow.auth_provider)


def _auth_inventory(client: EngineClient) -> dict[str, _AuthRecord]:
    payload = client.management_request("GET", "/auth-files")
    files = payload.get("files")
    if not isinstance(files, list):
        raise EngineClientError("engine auth inventory is invalid", error_type="invalid_json")
    inventory: dict[str, _AuthRecord] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        identity = str(item.get("id") or item.get("auth_index") or item.get("name") or "").strip()
        name = str(item.get("name") or item.get("id") or "").strip()
        provider = str(item.get("provider") or item.get("type") or "").strip().lower()
        if identity and name and provider:
            fingerprint = json.dumps(
                {
                    key: item.get(key)
                    for key in (
                        "modtime",
                        "updated_at",
                        "last_refresh",
                        "status",
                        "status_message",
                        "size",
                        "disabled",
                        "unavailable",
                    )
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            inventory[identity] = _AuthRecord(
                identity=identity,
                name=name,
                provider=provider,
                fingerprint=fingerprint,
            )
    return inventory


def _model_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    models = payload.get("models")
    if not isinstance(models, list):
        return ()
    result: list[str] = []
    for item in models:
        value = (
            item.get("id") or item.get("alias") or item.get("name")
            if isinstance(item, dict)
            else item
        )
        if isinstance(value, str) and value and value not in result:
            result.append(value)
    return tuple(result)


_adapter: CLIProxyEngineAdapter | None = None


def get_model_hub_engine_adapter() -> CLIProxyEngineAdapter:
    global _adapter
    if _adapter is None:
        _adapter = CLIProxyEngineAdapter()
    return _adapter


def set_model_hub_engine_adapter_for_tests(adapter: CLIProxyEngineAdapter | None) -> None:
    global _adapter
    _adapter = adapter
