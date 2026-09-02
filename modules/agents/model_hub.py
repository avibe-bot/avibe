"""Per-turn Model Hub selection and backend runtime injection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional, cast

from config import paths
from config.atomic_io import write_atomic
from config.v2_config import ModelHubConfig, ModelHubSourceConfig
from core.handlers.model_hub.classification import ResolutionDecision
from core.handlers.model_hub.events import (
    EventAgent,
    EventReason,
)
from core.handlers.model_hub.provenance import (
    ENGINE_DOWN_TURN_OUTCOME,
    TurnOutcomeProjectionInput,
    exact_hop_blockers,
    produce_turn_outcome,
    render_turn_outcome_copy,
    supply_interruption_reason,
)
from core.handlers.model_hub.resolver import (
    BackendName,
    ModelHubTurnResolution,
    resolve_model_hub_turn,
    source_after_cooldown_recovery,
    source_eligible_for_backend,
)
from core.handlers.model_hub.service import (
    PRE_ATTEMPT_SETTLEMENT_GENERATION,
    ModelHubError,
    ModelHubService,
    create_default_service,
    project_opencode_public_model,
)
from core.services.settings import load_config_or_default
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from vibe.codex_config import format_toml_basic_string
from vibe.opencode_config import model_hub_runtime_provider_id


LaunchChannel = Literal["direct", "native_cli", "hub"]
TurnMode = Literal["direct", "hub"]

_CONTEXT_LAUNCH_ATTR = "_vibe_model_hub_launch"
_CONTEXT_MODE_ATTR = "_vibe_model_hub_turn_mode"
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
    settlement_generation: Optional[int] = field(default=None, repr=False)

    @property
    def retires_replaced_hub_scope(self) -> bool:
        """Whether replacing a Hub runtime with this launch must revoke its scope.

        Only a `hub` launch mints a gateway token, and it mints its own before
        the replaced runtime is torn down — so retiring the scope then would
        revoke the new token. Every other channel leaves the old credential
        with no owner, and a surviving subprocess holding it could keep making
        billable gateway requests, so the scope must be retired.
        """

        return self.channel != "hub"

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
    provider_id: str
    checked_identifiers: tuple[str, ...]
    available_identifiers: tuple[str, ...]
    launches: tuple[ModelHubLaunch, ...] = field(repr=False)


def bind_launch(context: Any, launch: ModelHubLaunch) -> None:
    try:
        setattr(context, _CONTEXT_LAUNCH_ATTR, launch)
        setattr(
            context,
            _CONTEXT_MODE_ATTR,
            "direct" if launch.channel == "direct" else "hub",
        )
        setattr(context, _CONTEXT_FAILURE_RECORDED_ATTR, False)
    except (AttributeError, TypeError):
        return


def bind_turn_mode(context: Any, mode: TurnMode) -> None:
    try:
        setattr(context, _CONTEXT_MODE_ATTR, mode)
    except (AttributeError, TypeError):
        return


def turn_mode_for_context(context: Any) -> TurnMode | None:
    value = getattr(context, _CONTEXT_MODE_ATTR, None)
    return value if value in {"direct", "hub"} else None


def launch_for_context(context: Any) -> ModelHubLaunch | None:
    value = getattr(context, _CONTEXT_LAUNCH_ATTR, None)
    return value if isinstance(value, ModelHubLaunch) else None


def persisted_launch_identity(launch: ModelHubLaunch | None) -> dict[str, str] | None:
    """Return the non-secret source identity needed to restore failure routing."""

    if launch is None or launch.channel == "direct" or not launch.source_id:
        return None
    # The attempt's settlement generation is deliberately not persisted: it
    # indexes the minting runtime's in-memory ledger and means nothing to the
    # runtime that restores this identity. ``bind_persisted_launch`` supplies the
    # only generation that is true across runtimes.
    return {
        "backend": launch.backend,
        "channel": launch.channel,
        "source_id": launch.source_id,
        "target_model": launch.target_model,
    }


def bind_persisted_launch(context: Any, payload: object) -> ModelHubLaunch | None:
    if not isinstance(payload, Mapping):
        return None
    backend = payload.get("backend")
    channel = payload.get("channel")
    source_id = payload.get("source_id")
    target_model = payload.get("target_model")
    if (
        backend not in {"claude", "codex", "opencode"}
        or channel not in {"native_cli", "hub"}
        or not isinstance(source_id, str)
        or not source_id
        or not isinstance(target_model, str)
        or not target_model
    ):
        return None
    launch = ModelHubLaunch(
        backend=cast(BackendName, backend),
        channel=cast(LaunchChannel, channel),
        requested_model=target_model,
        target_model=target_model,
        runtime_model=target_model,
        source_id=source_id,
        # Restoring an identity means the attempt outlived the runtime that
        # started it, so every attempt this runtime starts is newer. The
        # pre-attempt generation states exactly that: a later attempt on this
        # Source rejects this settlement, and an untouched Source still accepts it.
        settlement_generation=PRE_ATTEMPT_SETTLEMENT_GENERATION,
    )
    bind_launch(context, launch)
    return launch


def claude_setting_sources_for_launch(launch: ModelHubLaunch | None) -> list[str]:
    if launch is not None and launch.channel == "hub":
        # ~/.claude/settings.json env values override subprocess env. Keep
        # project/local CLAUDE.md loading, but mask the user settings source so
        # persisted native auth cannot bypass the ephemeral gateway injection.
        return ["project", "local"]
    return ["user", "project", "local"]


async def resolve_model_hub_launch(
    controller: Any,
    backend: BackendName,
    requested_model: str,
    *,
    process_scope: Optional[str] = None,
) -> ModelHubLaunch:
    router = getattr(controller, "model_hub_runtime", None)
    resolver = getattr(router, "resolve", None)
    if callable(resolver):
        manager = getattr(controller, "session_turns", None)
        turn_lookup = getattr(manager, "model_hub_turn_id_for_task", None)
        turn_id = turn_lookup() if callable(turn_lookup) else None
        try:
            return await resolver(
                backend,
                requested_model,
                process_scope=process_scope,
                turn_id=turn_id,
            )
        except ModelHubError as exc:
            raise _localized_launch_error(
                controller,
                backend,
                requested_model,
                exc,
            ) from None
    return ModelHubLaunch(
        backend=backend,
        channel="direct",
        requested_model=requested_model,
        target_model=requested_model,
        runtime_model=requested_model,
    )


async def resolve_opencode_overlay_launch(
    controller: Any,
    requested_model: str,
    overlay: OpenCodeOverlay | None,
) -> ModelHubLaunch:
    router = getattr(controller, "model_hub_runtime", None)
    resolver = getattr(router, "resolve_opencode_overlay_launch", None)
    if overlay is not None and callable(resolver):
        try:
            return await resolver(overlay, requested_model)
        except ModelHubError as exc:
            raise _localized_launch_error(
                controller,
                "opencode",
                requested_model,
                exc,
            ) from None
    return await resolve_model_hub_launch(controller, "opencode", requested_model)


def _localized_launch_error(
    controller: Any,
    backend: BackendName,
    requested_model: str,
    error: ModelHubError,
) -> ModelHubError:
    if error.turn_outcome is None:
        return error
    language = str(
        getattr(getattr(controller, "config", None), "language", "en")
        or "en"
    )
    detail = render_turn_outcome_copy(error.turn_outcome, language)
    if detail is None:
        return error
    return ModelHubError(
        error.code,
        status=error.status,
        detail=detail,
        supply_state=error.supply_state,
        data=error.data,
        blockers=error.blockers,
        turn_outcome=error.turn_outcome,
    )


def build_claude_hub_env(
    base_env: dict[str, str],
    launch: ModelHubLaunch,
) -> dict[str, str]:
    """Return a hub-only Claude environment without inherited auth routing."""

    if launch.channel != "hub" or not launch.gateway_base_url or not launch.gateway_token:
        return dict(base_env)
    result = {
        key: value
        for key, value in base_env.items()
        if not key.startswith("ANTHROPIC_") and key != "CLAUDE_CODE_OAUTH_TOKEN"
    }
    result["ANTHROPIC_BASE_URL"] = launch.gateway_base_url
    result["ANTHROPIC_AUTH_TOKEN"] = launch.gateway_token
    return result


def build_codex_hub_launch(
    base_args: list[str],
    base_env: dict[str, str],
    launch: ModelHubLaunch,
    *,
    model_catalog_path: Path | None = None,
) -> tuple[list[str], dict[str, str] | None]:
    """Return app-server global overrides and environment for a Hub turn."""

    if launch.channel != "hub" or not launch.gateway_base_url or not launch.gateway_token:
        return list(base_args), None
    if model_catalog_path is None:
        raise ValueError("Codex Model Hub launches require a provider-safe model catalog")
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
        f"model_providers.{provider}.supports_websockets=false",
        "-c",
        f"model_providers.{provider}.requires_openai_auth=false",
        "-c",
        f"model_catalog_json={format_toml_basic_string(str(model_catalog_path))}",
    ]
    return overrides + list(base_args), env


def _provider_package() -> str:
    # OpenCode speaks one stable frontend protocol to the local Gateway. The
    # persisted exact hop selects the upstream protocol behind that boundary.
    return "@ai-sdk/openai-compatible"


def _provider_base_url(gateway_base_url: str) -> str:
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
        turn_gateway: ModelHubTurnGateway | None = None,
        overlay_path: Path | None = None,
        native_cli_ready: Callable[[BackendName], bool] | None = None,
    ) -> None:
        if service is None:
            service = create_default_service()
        self.service = service
        self.turn_gateway = turn_gateway
        self.overlay_path = overlay_path or paths.get_runtime_dir() / "model-hub" / "opencode-overlay.json"
        self._uses_default_native_cli_ready = native_cli_ready is None
        self.native_cli_ready = native_cli_ready or self._default_native_cli_ready
        self.service.native_source_ready = self._native_source_ready
        self._last_launch: dict[tuple[BackendName, str], ModelHubLaunch] = {}
        self._last_supply_state: dict[
            tuple[BackendName, str],
            tuple[str, Optional[EventReason]],
        ] = {}

    @staticmethod
    def _default_native_cli_ready(
        backend: BackendName,
        *,
        verified_oauth: bool = False,
    ) -> bool:
        runtime_config = getattr(load_config_or_default().agents, backend)
        cli_path = str(getattr(runtime_config, "cli_path", "") or "").strip()
        if (
            not bool(getattr(runtime_config, "enabled", False))
            or not cli_path
            or shutil.which(os.path.expanduser(cli_path)) is None
        ):
            return False
        if backend == "claude":
            from vibe.claude_config import read_claude_settings_env

            conflicts = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"}
            settings_env = read_claude_settings_env()
            return not any(key in settings_env or os.environ.get(key) for key in conflicts)
        if backend != "codex":
            return False

        from vibe.codex_config import get_codex_config_paths, read_codex_auth_state

        conflicts = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY"}
        if any(os.environ.get(key) for key in conflicts):
            return False
        state = read_codex_auth_state()
        if (
            state.get("has_api_key")
            or (not state.get("has_chatgpt_tokens") and not verified_oauth)
            or state.get("base_url")
        ):
            return False
        config_path, _ = get_codex_config_paths()
        try:
            if config_path.exists():
                try:
                    import tomllib  # type: ignore[attr-defined]
                except ImportError:  # pragma: no cover - Python < 3.11
                    import tomli as tomllib  # type: ignore[no-redef]
                config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            else:
                config = {}
        except (OSError, ValueError):
            return False
        provider = config.get("model_provider") if isinstance(config, dict) else None
        return provider is None or provider == "openai"

    def _native_source_ready(
        self,
        backend: BackendName,
        source: ModelHubSourceConfig,
    ) -> bool:
        if not self._uses_default_native_cli_ready:
            return self.native_cli_ready(backend)
        return self._default_native_cli_ready(
            backend,
            verified_oauth=backend == "codex" and source.state.status == "active",
        )

    def _unavailable_native_source_ids(
        self,
        config: ModelHubConfig,
        backend: BackendName,
    ) -> frozenset[str]:
        return frozenset(
            source.id
            for source in config.sources
            if source.supply_channel == "native_cli"
            and source_eligible_for_backend(source, backend)
            and not self._native_source_ready(
                backend,
                source_after_cooldown_recovery(source, self.service.now()),
            )
        )

    async def _resolve_turn(
        self,
        config: ModelHubConfig,
        backend: BackendName,
        requested_model: str,
        *,
        supply_channel: Literal["hub"] | None = None,
    ) -> tuple[ModelHubConfig, ModelHubTurnResolution]:
        resolution = resolve_model_hub_turn(
            config,
            backend,
            requested_model,
            now=self.service.now(),
            unavailable_source_ids=self._unavailable_native_source_ids(config, backend),
            supply_channel=supply_channel,
        )
        if not resolution.recoverable_source_ids:
            return config, resolution
        await self.service._recover_resolution_sources(resolution)
        config = self.service.store.load()
        return config, resolve_model_hub_turn(
            config,
            backend,
            requested_model,
            now=self.service.now(),
            unavailable_source_ids=self._unavailable_native_source_ids(config, backend),
            supply_channel=supply_channel,
        )

    @staticmethod
    def _route_key(launch: ModelHubLaunch) -> tuple[BackendName, str]:
        return launch.backend, launch.requested_model

    @staticmethod
    def _direct_launch(backend: BackendName, requested_model: str) -> ModelHubLaunch:
        return ModelHubLaunch(
            backend=backend,
            channel="direct",
            requested_model=requested_model,
            target_model=requested_model,
            runtime_model=requested_model,
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
        raise ModelHubError(
            "engine_down",
            status=503,
            turn_outcome=ENGINE_DOWN_TURN_OUTCOME,
        )

    async def _gateway_credentials(
        self,
        backend: BackendName,
        *,
        process_scope: Optional[str],
        turn_id: Optional[str],
        requested_model_id: Optional[str] = None,
        resolved_model_id: Optional[str] = None,
        source_id: Optional[str] = None,
        via_mapping: bool = False,
    ) -> tuple[str, str]:
        if self.turn_gateway is not None:
            return await self.turn_gateway.endpoint(
                backend,
                process_scope=process_scope,
                turn_id=turn_id,
                requested_model_id=requested_model_id,
                resolved_model_id=resolved_model_id,
                source_id=source_id,
                via_mapping=via_mapping,
            )
        await self.service._ensure_engine_synced()
        status = await self.service._engine_call(self.service.adapter.start())
        if status.listen_port is None:
            raise ModelHubError(
                "engine_down",
                status=503,
                turn_outcome=ENGINE_DOWN_TURN_OUTCOME,
            )
        token = await self.service._engine_call(self.service.adapter.gateway_token())
        return f"http://{status.listen_host}:{status.listen_port}", token

    @staticmethod
    def _source_for_id(
        config: ModelHubConfig,
        source_id: object,
    ) -> ModelHubSourceConfig | None:
        if not isinstance(source_id, str):
            return None
        return next((source for source in config.sources if source.id == source_id), None)

    def _transition_context(
        self,
        current: ModelHubLaunch,
        config: ModelHubConfig,
    ) -> tuple[ModelHubLaunch | None, ModelHubSourceConfig | None, EventReason | None]:
        route_key = self._route_key(current)
        previous = self._last_launch.get(route_key)
        route_events = [
            event
            for event in self.service.events.list(limit=100)
            if event.get("agent") == current.backend and event.get("model_id") == current.requested_model
        ]
        pending_source: ModelHubSourceConfig | None = None
        pending_reason: EventReason | None = None
        for index, event in enumerate(route_events):
            if event.get("kind") != "cooldown":
                continue
            failed_source_id = event.get("from_source")
            consumed = any(
                (
                    newer.get("kind") == "switch"
                    and newer.get("from_source") == failed_source_id
                )
                or (
                    newer.get("kind") == "recover"
                    and newer.get("to_source") == failed_source_id
                )
                for newer in route_events[:index]
            )
            if not consumed:
                pending_source = self._source_for_id(config, failed_source_id)
                reason = event.get("reason")
                if reason in {"quota_exhausted", "rate_limited", "server_error", "network"}:
                    pending_reason = cast(EventReason, reason)
                break

        if previous is None:
            for event in route_events:
                if event.get("kind") == "cooldown":
                    source = self._source_for_id(config, event.get("from_source"))
                elif event.get("kind") in {"switch", "channel_switch"}:
                    if event.get("kind") == "channel_switch" and event.get("to_source") is None:
                        previous = self._direct_launch(
                            current.backend,
                            current.requested_model,
                        )
                        break
                    source = self._source_for_id(config, event.get("to_source"))
                else:
                    continue
                if source is not None:
                    previous = ModelHubLaunch(
                        backend=current.backend,
                        channel=cast(LaunchChannel, source.supply_channel),
                        requested_model=current.requested_model,
                        target_model=current.target_model,
                        runtime_model=current.runtime_model,
                        source_id=source.id,
                    )
                    break
        return previous, pending_source, pending_reason

    def _emit_transition(
        self,
        current: ModelHubLaunch,
        config: ModelHubConfig,
    ) -> None:
        previous, failed_source, failed_reason = self._transition_context(current, config)
        route_key = self._route_key(current)
        self._last_launch[route_key] = current
        current_source = self._source_for_id(config, current.source_id)
        if (
            failed_source is not None
            and failed_reason is not None
            and current_source is not None
            and failed_source.id != current_source.id
        ):
            self.service._emit_switch(
                agent=cast(EventAgent, current.backend),
                model_id=current.requested_model,
                failed_source=failed_source,
                failed_reason=failed_reason,
                source=current_source,
            )
        if previous is None:
            return
        if previous.channel == current.channel:
            return
        if "direct" in {previous.channel, current.channel}:
            return
        if {previous.channel, current.channel} != {"native_cli", "hub"}:
            return
        if (
            previous.source_id is None
            or current.source_id is None
            or previous.source_id != current.source_id
        ):
            return
        reason = failed_reason or ("recovery" if current.channel == "native_cli" else "manual")
        self.service._record_event(
            agent=cast(EventAgent, current.backend),
            kind="channel_switch",
            model_id=current.requested_model,
            reason=reason,
            from_source=previous.source_id,
            to_source=previous.source_id,
            from_label=current_source.display_name,
            to_label=current_source.display_name,
            now=self.service.now(),
        )

    def _no_candidate_error(
        self,
        *,
        backend: BackendName,
        requested_model: str,
        process_scope: Optional[str],
        turn_id: Optional[str],
    ) -> ModelHubError:
        projection_config, projection_resolution = self.service._inspect_terminal_chain(
            backend=backend,
            model_id=requested_model,
        )
        turn_outcome = produce_turn_outcome(
            (
                "turn.no_candidate.unconfigured"
                if projection_resolution.route_unconfigured
                else "turn.no_candidate.blocked"
            ),
            config=projection_config,
            resolution=projection_resolution,
        )
        facts = turn_outcome.supply_facts
        if facts is None:
            raise AssertionError("no-candidate outcome must carry supply facts")
        supply_state = facts.supply_state
        normalized_scope = (
            str(process_scope or "").strip()
            or f"{backend}:untracked"
        )
        if self.turn_gateway is not None:
            self.turn_gateway.correlation.mark_no_candidate(
                backend=backend,
                process_scope=normalized_scope,
                turn_id=turn_id,
                requested_model_id=requested_model,
                supply_state=supply_state,
                blockers=exact_hop_blockers(projection_resolution),
            )
        if (
            not projection_resolution.matching_sources
            or projection_resolution.structural_blocker_reason is not None
        ):
            reason = cast(
                EventReason,
                supply_interruption_reason(projection_config, projection_resolution),
            )
            supply_key = (backend, requested_model)
            current_state = ("interrupted", reason)
            if self._last_supply_state.get(supply_key) != current_state:
                self.service._record_event(
                    agent=cast(EventAgent, backend),
                    kind="supply_interrupted",
                    model_id=requested_model,
                    reason=reason,
                    now=self.service.now(),
                )
                self._last_supply_state[supply_key] = current_state
        return ModelHubError(
            "mapping_target_unavailable",
            status=409,
            supply_state=supply_state,
            blockers=exact_hop_blockers(projection_resolution),
            turn_outcome=turn_outcome,
        )

    def settle_turn(
        self,
        turn_id: str,
        *,
        settled_by: Optional[str],
        ts: str,
        mode: Optional[Literal["direct", "hub"]] = None,
    ) -> None:
        try:
            if mode is not None:
                self.service.note_turn_mode(turn_id, mode)
        finally:
            if self.turn_gateway is not None:
                self.turn_gateway.correlation.settle(
                    turn_id,
                    settled_by=settled_by,
                    ts=ts,
                )

    def retire_process_scope(
        self,
        backend: BackendName,
        process_scope: str,
        *,
        terminal_turn_id: Optional[str] = None,
    ) -> None:
        if self.turn_gateway is not None:
            self.turn_gateway.correlation.retire_scope(
                backend,
                process_scope,
                terminal_turn_id=terminal_turn_id,
            )

    def turn_mode(self, backend: BackendName) -> TurnMode:
        return cast(TurnMode, self.service.store.load().agents[backend].mode)

    async def resolve(
        self,
        backend: BackendName,
        requested_model: str,
        *,
        process_scope: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> ModelHubLaunch:
        requested_model = str(requested_model or "").strip()
        config = self.service.store.load()
        config, resolution = await self._resolve_turn(
            config,
            backend,
            requested_model,
        )
        if resolution.channel == "direct":
            launch = self._direct_launch(backend, requested_model)
            self._emit_transition(launch, config)
            return launch
        if resolution.source is None:
            raise self._no_candidate_error(
                backend=backend,
                requested_model=requested_model,
                process_scope=process_scope,
                turn_id=turn_id,
            )

        target_model = resolution.target_model
        source = resolution.source
        self._last_supply_state[(backend, requested_model)] = ("ok", None)
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
                settlement_generation=self.service._reserve_settlement_generation(
                    source.id
                ),
            )
            if self.turn_gateway is not None:
                self.turn_gateway.correlation.begin_native_attempt(
                    backend=backend,
                    process_scope=str(process_scope or "").strip() or f"{backend}:untracked",
                    turn_id=turn_id,
                    requested_model_id=requested_model,
                    source_id=source.id,
                    resolved_model_id=target_model,
                    via_mapping=False,
                )
        else:
            gateway_base_url, gateway_token = await self._gateway_credentials(
                backend,
                process_scope=str(process_scope or "").strip() or f"{backend}:untracked",
                turn_id=turn_id,
                requested_model_id=requested_model,
                resolved_model_id=target_model,
                source_id=source.id,
                via_mapping=False,
            )
            runtime_model = target_model
            if self.turn_gateway is None:
                prefix = await self._source_prefix(source.id)
                runtime_model = f"{prefix}/{target_model}"
            launch = ModelHubLaunch(
                backend=backend,
                channel="hub",
                requested_model=requested_model,
                target_model=target_model,
                runtime_model=runtime_model,
                source_id=source.id,
                gateway_base_url=gateway_base_url,
                gateway_token=gateway_token,
            )
        self._emit_transition(launch, config)
        return launch

    async def resolve_opencode_overlay_launch(
        self,
        overlay: OpenCodeOverlay,
        requested_model: str,
    ) -> ModelHubLaunch:
        """Activate the exact source snapshot used to build an overlay."""

        launch = next(
            (item for item in overlay.launches if item.requested_model == requested_model),
            None,
        )
        if launch is None:
            config = self.service.store.load()
            config, resolution = await self._resolve_turn(
                config,
                "opencode",
                requested_model,
                supply_channel="hub",
            )
            if resolution.source is None:
                raise self._no_candidate_error(
                    backend="opencode",
                    requested_model=requested_model,
                    process_scope="opencode:shared-server",
                    turn_id=None,
                )
            raise ModelHubError("mapping_target_unavailable", status=409)
        config = self.service.store.load()
        self._emit_transition(launch, config)
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
        if launch.channel == "hub" and self.turn_gateway is not None:
            turn_id = str(
                (getattr(context, "platform_specific", None) or {}).get(
                    "turn_token"
                )
                or ""
            ).strip()
            self.turn_gateway.correlation.fail_hub_attempt(turn_id)
            setattr(context, _CONTEXT_FAILURE_RECORDED_ATTR, True)
            return False
        decision: ResolutionDecision | None
        if _NATIVE_QUOTA_RE.search(diagnostic):
            decision = ResolutionDecision("fallback", reason="quota_exhausted", cooldown_seconds=300)
        elif _NATIVE_RATE_RE.search(diagnostic):
            decision = ResolutionDecision("fallback", reason="rate_limited", cooldown_seconds=60)
        elif _SERVER_ERROR_RE.search(diagnostic):
            decision = ResolutionDecision("fallback", reason="server_error", cooldown_seconds=30)
        elif _NETWORK_ERROR_RE.search(diagnostic):
            decision = ResolutionDecision("fallback", reason="network", cooldown_seconds=30)
        else:
            decision = None
        if launch.channel == "native_cli" and self.turn_gateway is not None:
            turn_id = str(
                (getattr(context, "platform_specific", None) or {}).get(
                    "turn_token"
                )
                or ""
            ).strip()
            self.turn_gateway.correlation.fail_native_attempt(
                turn_id,
                reason=(
                    decision.reason
                    if decision is not None and decision.reason is not None
                    else "unclassified_error"
                ),
            )
        if decision is None:
            return False
        config = self.service.store.load()
        source = next((item for item in config.sources if item.id == launch.source_id), None)
        if source is None:
            return False
        _reason, persisted = await self.service._settle_fallback_source(
            source,
            decision,
            backend=launch.backend,
            model_id=launch.requested_model,
            settlement_generation=launch.settlement_generation,
        )
        setattr(context, _CONTEXT_FAILURE_RECORDED_ATTR, True)
        return persisted

    async def prepare_opencode_overlay(self) -> OpenCodeOverlay | None:
        config = self.service.store.load()
        agent = config.agents["opencode"]
        if agent.mode == "direct":
            return None
        checked = tuple(agent.menu.checked if agent.menu else ())
        if not checked:
            return None
        gateway_base_url, gateway_token = await self._gateway_credentials(
            "opencode",
            process_scope="opencode:shared-server",
            turn_id=None,
        )
        overlay_provider_id = model_hub_runtime_provider_id(gateway_token)
        providers: dict[str, dict[str, Any]] = {}
        projected_identifiers: list[str] = []
        available_identifiers: list[str] = []
        launches: list[ModelHubLaunch] = []
        for identifier in dict.fromkeys(checked):
            config, resolution = await self._resolve_turn(
                config,
                "opencode",
                identifier,
                supply_channel="hub",
            )
            menu_provider_id = resolution.menu_provider_id
            menu_model_id = resolution.menu_model_id
            if menu_provider_id is None or menu_model_id is None:
                raise ModelHubError("mapping_target_unavailable", status=409)
            inspection = (
                resolution.candidate_hops[0]
                if resolution.candidate_hops
                else None
            )
            if inspection is None:
                # Keep every configured route's public identifier stable in the
                # overlay. Per-turn resolution still rejects unavailable hops.
                inspection = (
                    resolution.projectable_hops[0]
                    if resolution.projectable_hops
                    else None
                )
            if (
                inspection is None
                or inspection.source is None
                or inspection.model_id is None
            ):
                continue
            source = inspection.source
            exact_model_id = inspection.model_id
            package = _provider_package()
            base_url = _provider_base_url(gateway_base_url)
            provider = providers.setdefault(
                overlay_provider_id,
                {
                    "name": "Avibe Model Hub",
                    "npm": package,
                    "options": {"apiKey": gateway_token, "baseURL": base_url},
                    "models": {},
                },
            )
            runtime_model = identifier
            if self.turn_gateway is None:
                prefix = await self._source_prefix(source.id)
                runtime_model = f"{prefix}/{exact_model_id}"
            projected_model = project_opencode_public_model(identifier, resolution)
            if projected_model is None:
                continue
            projected_model["id"] = runtime_model
            provider["models"][identifier] = projected_model
            projected_identifiers.append(identifier)
            if resolution.candidate_hops:
                available_identifiers.append(identifier)
                launches.append(
                    ModelHubLaunch(
                        backend="opencode",
                        channel="hub",
                        requested_model=identifier,
                        target_model=exact_model_id,
                        runtime_model=runtime_model,
                        source_id=source.id,
                        gateway_base_url=gateway_base_url,
                        gateway_token=gateway_token,
                    )
                )

        if not projected_identifiers:
            raise ModelHubError("mapping_target_unavailable", status=409)

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
            provider_id=overlay_provider_id,
            checked_identifiers=tuple(projected_identifiers),
            available_identifiers=tuple(available_identifiers),
            launches=tuple(launches),
        )

    def _secure_write_overlay(self, content: bytes) -> None:
        # ``mode=0o700`` on the directory is this method's own concern; the file is
        # 0600 by ``write_atomic``.
        self.overlay_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if self.overlay_path.read_bytes() == content:
                return
        except FileNotFoundError:
            pass
        write_atomic(self.overlay_path, content)


def opencode_requested_model_for_overlay(
    model: str | None,
    overlay: OpenCodeOverlay | None,
) -> str | None:
    if overlay is None:
        return model
    candidate = str(model or "").strip()
    if not candidate:
        if not overlay.available_identifiers:
            raise ModelHubError("mapping_target_unavailable", status=409)
        return overlay.available_identifiers[0]
    if candidate in overlay.checked_identifiers:
        return candidate
    raise ModelHubError("mapping_target_unavailable", status=409)


def opencode_model_for_overlay(
    model: str | None,
    overlay: OpenCodeOverlay | None,
) -> str | None:
    requested_model = opencode_requested_model_for_overlay(model, overlay)
    if overlay is None or requested_model is None:
        return requested_model
    return f"{overlay.provider_id}/{requested_model}"


def opencode_model_catalog_for_overlay(
    overlay: OpenCodeOverlay,
) -> dict[str, Any]:
    """Project the private overlay into the model metadata needed by its turn."""

    payload = json.loads(overlay.content)
    provider = payload.get("provider", {}).get(overlay.provider_id, {})
    models = provider.get("models", {}) if isinstance(provider, dict) else {}
    return {
        "providers": [{"id": overlay.provider_id, "models": models}],
        "default": {},
    }
