"""Per-turn Model Hub selection and backend runtime injection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, cast

from config import paths
from config.v2_config import ModelHubConfig
from core.handlers.model_hub.classification import ResolutionDecision
from core.handlers.model_hub.events import EventAgent, EventReason
from core.handlers.model_hub.identifiers import parse_opencode_model_id
from core.handlers.model_hub.service import ModelHubError, ModelHubService, create_default_service


BackendName = Literal["claude", "codex", "opencode"]
LaunchChannel = Literal["direct", "native_cli", "hub"]

_CONTEXT_LAUNCH_ATTR = "_vibe_model_hub_launch"
_CONTEXT_FAILURE_RECORDED_ATTR = "_vibe_model_hub_failure_recorded"
_NATIVE_QUOTA_RE = re.compile(
    r"(?:quota|usage|credit|billing).{0,32}(?:exhaust|exceed|limit|deplet|insufficient)|"
    r"(?:exhaust|exceed|limit|deplet|insufficient).{0,32}(?:quota|usage|credit|billing)|"
    r"(?:hit|reached).{0,24}(?:usage )?limit|limit.{0,24}reset",
    re.IGNORECASE,
)
_NATIVE_RATE_RE = re.compile(r"(?:\b429\b|rate[_ -]?limit|too many requests)", re.IGNORECASE)
_SERVER_ERROR_RE = re.compile(r"(?:\b5\d\d\b|server[_ -]?error|internal server error)", re.IGNORECASE)
_NETWORK_ERROR_RE = re.compile(
    r"(?:timed?\s*out|timeout|connection (?:failed|reset|refused)|network (?:error|unreachable))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ModelHubLaunch:
    backend: BackendName
    channel: LaunchChannel
    requested_model: str
    target_model: str
    runtime_model: str
    source_id: Optional[str] = None
    gateway_base_url: Optional[str] = None
    gateway_token: Optional[str] = None

    @property
    def fingerprint(self) -> str:
        if self.channel == "direct":
            return "direct"
        if self.channel == "native_cli":
            return f"native_cli:{self.source_id or ''}"
        token_hash = hashlib.sha256((self.gateway_token or "").encode()).hexdigest()
        return ":".join(
            (
                self.channel,
                self.gateway_base_url or "",
                token_hash,
            )
        )


@dataclass(frozen=True)
class OpenCodeOverlay:
    path: Path
    content_hash: str
    content: bytes
    checked_identifiers: tuple[str, ...]


def bind_launch(context: Any, launch: ModelHubLaunch) -> None:
    try:
        setattr(context, _CONTEXT_LAUNCH_ATTR, launch)
        setattr(context, _CONTEXT_FAILURE_RECORDED_ATTR, False)
    except (AttributeError, TypeError):
        return


def launch_for_context(context: Any) -> ModelHubLaunch | None:
    value = getattr(context, _CONTEXT_LAUNCH_ATTR, None)
    return value if isinstance(value, ModelHubLaunch) else None


async def resolve_model_hub_launch(
    controller: Any,
    backend: BackendName,
    requested_model: str,
) -> ModelHubLaunch:
    router = getattr(controller, "model_hub_runtime", None)
    resolver = getattr(router, "resolve", None)
    if callable(resolver):
        return await resolver(backend, requested_model)
    return ModelHubLaunch(
        backend=backend,
        channel="direct",
        requested_model=requested_model,
        target_model=requested_model,
        runtime_model=requested_model,
    )


def build_claude_hub_env(
    base_env: dict[str, str],
    launch: ModelHubLaunch,
) -> dict[str, str]:
    """Return a hub-only Claude environment without inherited auth routing."""

    if launch.channel != "hub" or not launch.gateway_base_url or not launch.gateway_token:
        return dict(base_env)
    result = dict(base_env)
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        result.pop(key, None)
    result["ANTHROPIC_BASE_URL"] = launch.gateway_base_url
    result["ANTHROPIC_AUTH_TOKEN"] = launch.gateway_token
    return result


def build_codex_hub_launch(
    base_args: list[str],
    base_env: dict[str, str],
    launch: ModelHubLaunch,
) -> tuple[list[str], dict[str, str] | None]:
    """Return app-server global overrides and environment for a Hub turn."""

    if launch.channel != "hub" or not launch.gateway_base_url or not launch.gateway_token:
        return list(base_args), None
    env = dict(base_env)
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY"):
        env.pop(key, None)
    env["AVIBE_MODEL_HUB_TOKEN"] = launch.gateway_token
    provider = "avibe_model_hub"
    gateway_v1 = f"{launch.gateway_base_url.rstrip('/')}/v1"
    overrides = [
        "-c",
        f'model_provider="{provider}"',
        "-c",
        f'model_providers.{provider}.name="Avibe Model Hub"',
        "-c",
        f'model_providers.{provider}.base_url="{gateway_v1}"',
        "-c",
        f'model_providers.{provider}.env_key="AVIBE_MODEL_HUB_TOKEN"',
        "-c",
        f'model_providers.{provider}.wire_api="responses"',
        "-c",
        f"model_providers.{provider}.requires_openai_auth=false",
    ]
    return overrides + list(base_args), env


def _provider_package(protocol: str) -> str:
    if protocol == "anthropic":
        return "@ai-sdk/anthropic"
    if protocol == "openai_responses":
        return "@ai-sdk/openai"
    return "@ai-sdk/openai-compatible"


def _provider_base_url(gateway_base_url: str, protocol: str) -> str:
    # All supported OpenCode SDK adapters expect their versioned API root.
    return f"{gateway_base_url.rstrip('/')}/v1"


def overlay_identifier_bytes(content: bytes) -> bytes:
    """Canonical visible identifier projection used by MH-OVL-001."""

    payload = json.loads(content)
    identifiers = [
        f"{provider_id}/{model_id}"
        for provider_id, provider in sorted(payload.get("provider", {}).items())
        for model_id in sorted(provider.get("models", {}))
    ]
    return json.dumps(identifiers, ensure_ascii=True, separators=(",", ":")).encode()


class ModelHubRuntimeRouter:
    """Select a supply channel once per turn and build ephemeral injections."""

    def __init__(
        self,
        *,
        service: ModelHubService | None = None,
        overlay_path: Path | None = None,
    ) -> None:
        if service is None:
            from vibe.model_hub_runtime import get_model_hub_engine_adapter

            service = create_default_service(adapter=get_model_hub_engine_adapter())
        self.service = service
        self.overlay_path = overlay_path or paths.get_runtime_dir() / "model-hub" / "opencode-overlay.json"
        self._last_launch: dict[BackendName, ModelHubLaunch] = {}
        self._pending_switch_reason: dict[BackendName, EventReason] = {}
        self._pending_source_failure: dict[BackendName, tuple[str, EventReason]] = {}

    @staticmethod
    def _target_model(config: ModelHubConfig, backend: BackendName, requested_model: str) -> str:
        agent = config.agents[backend]
        return next(
            (
                mapping.target_model_id
                for mapping in agent.mappings
                if mapping.enabled and mapping.builtin_id == requested_model
            ),
            requested_model,
        )

    async def _source_prefix(self, source_id: str) -> str:
        adapter = self.service.adapter
        getter = getattr(adapter, "source_prefix", None)
        if callable(getter):
            value = getter(source_id)
            if asyncio.iscoroutine(value):
                value = await value
            if isinstance(value, str) and value:
                return value
        state_store = getattr(adapter, "state_store", None)
        state_getter = getattr(state_store, "get_source", None)
        if callable(state_getter):
            record = await asyncio.to_thread(state_getter, source_id)
            prefix = getattr(record, "prefix", None)
            if isinstance(prefix, str) and prefix:
                return prefix
        raise ModelHubError("engine_down", status=503)

    async def _gateway_credentials(self) -> tuple[str, str]:
        await self.service._ensure_engine_synced()
        status = await self.service._engine_call(self.service.adapter.start())
        if status.listen_port is None:
            raise ModelHubError("engine_down", status=503)
        token = await self.service._engine_call(self.service.adapter.gateway_token())
        return f"http://{status.listen_host}:{status.listen_port}", token

    def _emit_channel_switch(self, current: ModelHubLaunch) -> None:
        previous = self._last_launch.get(current.backend)
        self._last_launch[current.backend] = current
        if previous is None or previous.channel == current.channel:
            return
        if {previous.channel, current.channel} != {"native_cli", "hub"}:
            return
        reason = self._pending_switch_reason.pop(current.backend, None)
        if reason is None:
            reason = "recovery" if current.channel == "native_cli" else "manual"
        self.service._record_event(
            agent=cast(EventAgent, current.backend),
            kind="channel_switch",
            model_id=current.target_model,
            reason=reason,
            from_source=previous.source_id,
            to_source=current.source_id,
            now=self.service.now(),
        )

    def _emit_source_switch(self, current: ModelHubLaunch, config: ModelHubConfig) -> None:
        pending = self._pending_source_failure.get(current.backend)
        if pending is None or not current.source_id:
            return
        failed_source_id, reason = pending
        if failed_source_id == current.source_id:
            self._pending_source_failure.pop(current.backend, None)
            return
        failed_source = next((source for source in config.sources if source.id == failed_source_id), None)
        current_source = next((source for source in config.sources if source.id == current.source_id), None)
        if failed_source is None or current_source is None:
            self._pending_source_failure.pop(current.backend, None)
            return
        self.service._emit_switch(
            agent=cast(EventAgent, current.backend),
            model_id=current.target_model,
            failed_source=failed_source,
            failed_reason=reason,
            source=current_source,
        )
        self._pending_source_failure.pop(current.backend, None)

    async def resolve(self, backend: BackendName, requested_model: str) -> ModelHubLaunch:
        requested_model = str(requested_model or "").strip()
        config = self.service.store.load()
        agent = config.agents[backend]
        if agent.mode == "direct":
            launch = ModelHubLaunch(
                backend=backend,
                channel="direct",
                requested_model=requested_model,
                target_model=requested_model,
                runtime_model=requested_model,
            )
            self._emit_channel_switch(launch)
            return launch
        if not requested_model:
            raise ModelHubError("mapping_target_unavailable", status=409)

        target_model = self._target_model(config, backend, requested_model)
        if target_model != requested_model:
            self.service._record_event(
                agent=cast(EventAgent, backend),
                kind="mapping_applied",
                model_id=target_model,
                reason="mapping",
                from_label=requested_model,
                now=self.service.now(),
            )
        provider: str | None = None
        if backend == "opencode":
            try:
                provider, target_model = parse_opencode_model_id(target_model)
            except ValueError:
                raise ModelHubError("mapping_target_unavailable", status=409) from None
            if agent.menu is None or f"{provider}/{target_model}" not in agent.menu.checked:
                raise ModelHubError("mapping_target_unavailable", status=409)
        candidates = await self.service._resolution_candidates(backend, target_model, provider=provider)
        if not candidates:
            raise ModelHubError("mapping_target_unavailable", status=409)
        source = candidates[0]
        if source.supply_channel == "native_cli":
            if self.service.revocations.list():
                try:
                    await self.service._ensure_engine_synced()
                except ModelHubError:
                    # Native launch is independent; the durable journal retries later.
                    pass
            launch = ModelHubLaunch(
                backend=backend,
                channel="native_cli",
                requested_model=requested_model,
                target_model=target_model,
                runtime_model=target_model,
                source_id=source.id,
            )
        else:
            gateway_base_url, gateway_token = await self._gateway_credentials()
            prefix = await self._source_prefix(source.id)
            launch = ModelHubLaunch(
                backend=backend,
                channel="hub",
                requested_model=requested_model,
                target_model=target_model,
                runtime_model=f"{prefix}/{target_model}",
                source_id=source.id,
                gateway_base_url=gateway_base_url,
                gateway_token=gateway_token,
            )
        self._emit_source_switch(launch, config)
        self._emit_channel_switch(launch)
        return launch

    async def record_native_failure(self, context: Any, diagnostic: str) -> bool:
        """Record a terminal source failure for the next per-turn resolution.

        The method name is retained for existing backend call sites; Hub launches
        use the same cooldown state so the next turn can select a backup source.
        """

        launch = launch_for_context(context)
        if launch is None or launch.channel not in {"native_cli", "hub"} or not launch.source_id:
            return False
        if getattr(context, _CONTEXT_FAILURE_RECORDED_ATTR, False):
            return False
        if _NATIVE_QUOTA_RE.search(diagnostic):
            decision = ResolutionDecision("fallback", reason="quota_exhausted", cooldown_seconds=300)
        elif _NATIVE_RATE_RE.search(diagnostic):
            decision = ResolutionDecision("fallback", reason="rate_limited", cooldown_seconds=60)
        elif _SERVER_ERROR_RE.search(diagnostic):
            decision = ResolutionDecision("fallback", reason="server_error", cooldown_seconds=30)
        elif _NETWORK_ERROR_RE.search(diagnostic):
            decision = ResolutionDecision("fallback", reason="network", cooldown_seconds=30)
        else:
            return False
        config = self.service.store.load()
        source = next((item for item in config.sources if item.id == launch.source_id), None)
        if source is None:
            return False
        await self.service._cooldown(
            source,
            decision,
            agent=cast(EventAgent, launch.backend),
            model_id=launch.target_model,
        )
        setattr(context, _CONTEXT_FAILURE_RECORDED_ATTR, True)
        reason = cast(EventReason, decision.reason)
        self._pending_source_failure[launch.backend] = (launch.source_id, reason)
        if launch.channel == "native_cli":
            self._pending_switch_reason[launch.backend] = reason
        return True

    async def prepare_opencode_overlay(self) -> OpenCodeOverlay | None:
        config = self.service.store.load()
        agent = config.agents["opencode"]
        if agent.mode == "direct":
            return None
        checked = tuple(agent.menu.checked if agent.menu else ())
        if not checked:
            raise ModelHubError("mapping_target_unavailable", status=409)

        gateway_base_url, gateway_token = await self._gateway_credentials()
        providers: dict[str, dict[str, Any]] = {}
        for identifier in sorted(checked):
            try:
                provider_id, model_id = parse_opencode_model_id(identifier)
            except ValueError:
                raise ModelHubError("mapping_target_unavailable", status=409) from None
            candidates = await self.service._resolution_candidates(
                "opencode",
                model_id,
                provider=provider_id,
            )
            source = next((candidate for candidate in candidates if candidate.supply_channel == "hub"), None)
            if source is None:
                raise ModelHubError("mapping_target_unavailable", status=409)
            prefix = await self._source_prefix(source.id)
            package = _provider_package(source.protocol)
            base_url = _provider_base_url(gateway_base_url, source.protocol)
            provider = providers.setdefault(
                provider_id,
                {
                    "name": provider_id,
                    "npm": package,
                    "options": {"apiKey": gateway_token, "baseURL": base_url},
                    "models": {},
                },
            )
            if provider["npm"] != package or provider["options"]["baseURL"] != base_url:
                raise ModelHubError("mapping_target_unavailable", status=409)
            model = next(item for item in source.models if item.id == model_id)
            provider["models"][model_id] = {
                "id": f"{prefix}/{model_id}",
                "name": model.display_name or model_id,
            }

        content = (
            json.dumps(
                {"$schema": "https://opencode.ai/config.json", "provider": providers},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        content_hash = hashlib.sha256(content).hexdigest()
        self._secure_write_overlay(content)
        return OpenCodeOverlay(
            path=self.overlay_path,
            content_hash=content_hash,
            content=content,
            checked_identifiers=checked,
        )

    def _secure_write_overlay(self, content: bytes) -> None:
        self.overlay_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if self.overlay_path.read_bytes() == content:
                return
        except FileNotFoundError:
            pass
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.overlay_path.name}.",
            dir=self.overlay_path.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.overlay_path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def opencode_model_for_overlay(model: str | None, overlay: OpenCodeOverlay | None) -> str | None:
    if overlay is None:
        return model
    candidate = str(model or "").strip()
    if not candidate:
        return overlay.checked_identifiers[0]
    if candidate in overlay.checked_identifiers:
        return candidate
    matches = [identifier for identifier in overlay.checked_identifiers if identifier.endswith(f"/{candidate}")]
    if len(matches) == 1:
        return matches[0]
    raise ModelHubError("mapping_target_unavailable", status=409)
