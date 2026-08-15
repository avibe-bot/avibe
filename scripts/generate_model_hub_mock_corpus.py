#!/usr/bin/env python3
"""Record Model Hub mock transitions from the authoritative Python service."""

from __future__ import annotations

import argparse
import asyncio
import base64
import builtins
import copy
import dataclasses
import hashlib
import io
import json
import os
import socket
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import v2_config as config_module  # noqa: E402
from config.v2_config import ModelHubConfig  # noqa: E402
from core.handlers.model_hub.adapter import (  # noqa: E402
    OAuthFlowState,
    ObservationDiscovery,
    ObservationOutcome,
    SourceObservation,
)
from core.handlers.model_hub import migration as migration_module  # noqa: E402
from core.handlers.model_hub import service as service_module  # noqa: E402
from core.handlers.model_hub.oauth import (  # noqa: E402
    OAuthFlowBinding,
    OAuthNonceClaim,
)
from core.handlers.model_hub.service import (  # noqa: E402
    EngineUnavailableError,
    ModelHubError,
    ModelHubService,
)
from vibe.opencode_config import OpenCodeConfigProbeResult  # noqa: E402

SEED_PATH = Path(
    os.environ.get(
        "MODEL_HUB_MOCK_SEED_PATH",
        ROOT / "scripts/model_hub_mock_seed.json",
    )
)
SEQUENCES_PATH = Path(
    os.environ.get(
        "MODEL_HUB_MOCK_SEQUENCES_PATH",
        ROOT / "scripts/model_hub_mock_sequences.json",
    )
)
OUTPUT_PATH = Path(
    os.environ.get(
        "MODEL_HUB_MOCK_OUTPUT_PATH",
        ROOT / "ui/src/components/settings/models/modelHubMockCorpus.json",
    )
)
GENERATOR_COMMAND = "python3 scripts/generate_model_hub_mock_corpus.py"


class UnregisteredFixtureAccess(RuntimeError):
    pass


class SealedDateTime(datetime):
    @classmethod
    def now(cls, *_args: Any, **_kwargs: Any) -> Any:
        raise UnregisteredFixtureAccess("real clock access is forbidden while recording")

    @classmethod
    def utcnow(cls) -> Any:
        raise UnregisteredFixtureAccess("real clock access is forbidden while recording")

    @classmethod
    def today(cls) -> Any:
        raise UnregisteredFixtureAccess("real clock access is forbidden while recording")


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _json_value(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"Fixture world contains a non-JSON value: {type(value).__name__}")


def _canonical(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _token(value: Any) -> str:
    encoded = base64.urlsafe_b64encode(_canonical(value).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")


def _decode_token(value: str) -> dict[str, Any]:
    padded = value + "=" * (-len(value) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("record-miss token must decode to an object")
    return decoded


class MemoryConfigStore:
    def __init__(self, payload: dict[str, Any]):
        self.config = ModelHubConfig.from_payload(copy.deepcopy(payload))

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = ModelHubConfig.from_payload(config.to_payload())

    def ensure_writable(self) -> None:
        return None


@dataclass(frozen=True)
class PendingRevocation:
    source_id: str
    credential_ref: str
    operation: str = "revoke_credential"


class MemoryRevocations:
    def __init__(self, rows: list[dict[str, str]]):
        self.rows = [PendingRevocation(**row) for row in rows]

    def list(self) -> list[PendingRevocation]:
        return list(self.rows)

    def add(
        self,
        source_id: str,
        credential_ref: str,
        *,
        operation: str = "revoke_credential",
    ) -> None:
        row = PendingRevocation(source_id, credential_ref, operation)
        if row not in self.rows:
            self.rows.append(row)

    def remove(self, source_id: str, credential_ref: str) -> None:
        self.rows = [
            row
            for row in self.rows
            if (row.source_id, row.credential_ref) != (source_id, credential_ref)
        ]

    def snapshot(self) -> list[dict[str, str]]:
        return [dataclasses.asdict(row) for row in self.rows]


class MemoryOAuthFlows:
    def __init__(self, payload: dict[str, Any]):
        self.bindings = {
            flow_id: OAuthFlowBinding(**binding)
            for flow_id, binding in payload["bindings"].items()
        }
        self.pending_nonces = {
            tuple(item) for item in payload["pending_nonces"]
        }

    def binding(self, flow_id: str) -> OAuthFlowBinding | None:
        return self.bindings.get(flow_id)

    def remember(
        self,
        flow_id: str,
        channel: str,
        source_id: str,
        vendor: str,
        **kwargs: Any,
    ) -> None:
        self.bindings[flow_id] = OAuthFlowBinding(
            channel=channel,
            source_id=source_id,
            vendor=vendor,
            intent=kwargs.get("intent", "create"),
            recovered=kwargs.get("recovered"),
            client_nonce=kwargs.get("client_nonce"),
            expires_at_iso=kwargs.get("expires_at_iso"),
        )
        client_nonce = kwargs.get("client_nonce")
        if client_nonce is not None:
            self.pending_nonces.discard((client_nonce, vendor, channel))

    def claim_nonce(
        self,
        client_nonce: str,
        vendor: str,
        channel: str,
    ) -> OAuthNonceClaim:
        for flow_id, binding in self.bindings.items():
            if (
                binding.client_nonce == client_nonce
                and binding.vendor == vendor
                and binding.channel == channel
            ):
                return OAuthNonceClaim(
                    client_nonce,
                    vendor,
                    channel,
                    "committed",
                    flow_id=flow_id,
                )
        key = (client_nonce, vendor, channel)
        if key in self.pending_nonces:
            return OAuthNonceClaim(
                client_nonce,
                vendor,
                channel,
                "in_flight",
            )
        self.pending_nonces.add(key)
        return OAuthNonceClaim(
            client_nonce,
            vendor,
            channel,
            "in_flight",
            owner=True,
        )

    def release_nonce(
        self,
        client_nonce: str,
        vendor: str,
        channel: str,
    ) -> None:
        self.pending_nonces.discard((client_nonce, vendor, channel))

    def snapshot(self) -> dict[str, Any]:
        return {
            "bindings": {
                flow_id: _json_value(binding)
                for flow_id, binding in self.bindings.items()
            },
            "pending_nonces": sorted([list(item) for item in self.pending_nonces]),
        }

    def __getattr__(self, member: str) -> Any:
        raise UnregisteredFixtureAccess(
            f"unregistered OAuth registry access: {member}"
        )


class UnregisteredCollaborator:
    def __init__(self, name: str):
        self.name = name

    def __getattr__(self, member: str) -> Any:
        raise UnregisteredFixtureAccess(
            f"unregistered fixture collaborator access: {self.name}.{member}"
        )


class RecordedAdapter:
    """Only the adapter calls needed by the checked-in corpus are registered."""

    def __init__(self, payload: dict[str, Any]):
        self.credential_cursor = int(payload["credential_cursor"])
        self.retargeted = copy.deepcopy(payload["retargeted"])
        self.discoveries = copy.deepcopy(payload["discoveries"])
        self.synced_bindings = copy.deepcopy(payload["synced_bindings"])
        self.revoked = list(payload["revoked"])
        self.discovery_script = copy.deepcopy(payload["discovery_script"])
        self.observation_script = copy.deepcopy(payload["observation_script"])
        self.provisioned = copy.deepcopy(payload["provisioned"])
        self.oauth_started = copy.deepcopy(payload["oauth_started"])
        self.oauth_cursor = int(payload["oauth_cursor"])

    async def provision_transient_credential(
        self,
        vendor: str,
        _secret: str,
        base_url: str | None,
    ) -> str:
        self.credential_cursor += 1
        credential_ref = f"cred_recorded{self.credential_cursor:03d}"
        self.provisioned.append(
            {
                "kind": "transient",
                "vendor": vendor,
                "base_url": base_url,
                "credential_ref": credential_ref,
            }
        )
        return credential_ref

    async def provision_credential(
        self,
        vendor: str,
        protocol: str,
        _secret: str,
        base_url: str | None,
    ) -> str:
        self.credential_cursor += 1
        credential_ref = f"cred_recorded{self.credential_cursor:03d}"
        self.provisioned.append(
            {
                "kind": "permanent",
                "vendor": vendor,
                "protocol": protocol,
                "base_url": base_url,
                "credential_ref": credential_ref,
            }
        )
        return credential_ref

    async def observe_source(
        self,
        vendor: str,
        base_url: str | None,
        _credential_ref: str,
        protocol_order: tuple[str, ...],
    ) -> SourceObservation:
        key = f"{vendor}|{base_url or ''}"
        if key not in self.observation_script:
            raise UnregisteredFixtureAccess(
                f"unregistered observation fixture: {key}"
            )
        return SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol=protocol_order[0],
            discovery=ObservationDiscovery.SUCCEEDED,
            model_ids=tuple(self.observation_script[key]),
        )

    async def retarget_api_key_credential(
        self,
        credential_ref: str,
        vendor: str,
        protocol: str,
        base_url: str | None,
    ) -> str:
        self.credential_cursor += 1
        replacement = f"cred_recorded{self.credential_cursor:03d}"
        self.retargeted.append(
            {
                "credential_ref": credential_ref,
                "vendor": vendor,
                "protocol": protocol,
                "base_url": base_url,
                "replacement": replacement,
            }
        )
        return replacement

    async def discover_models(
        self,
        vendor: str,
        protocol: str,
        base_url: str | None,
        credential_ref: str,
    ) -> tuple[str, ...]:
        key = f"{vendor}|{protocol}|{base_url or ''}"
        if key not in self.discovery_script:
            raise UnregisteredFixtureAccess(
                f"unregistered discovery fixture: {key}"
            )
        models = list(self.discovery_script[key])
        self.discoveries.append(
            {
                "vendor": vendor,
                "protocol": protocol,
                "base_url": base_url,
                "credential_ref": credential_ref,
                "models": models,
            }
        )
        return tuple(models)

    async def sync_sources(self, bindings: Any) -> None:
        self.synced_bindings.append(_json_value(list(bindings)))

    async def revoke_credential(self, credential_ref: str) -> None:
        self.revoked.append(credential_ref)

    async def start(self) -> Any:
        raise EngineUnavailableError

    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState:
        self.oauth_cursor += 1
        flow_id = f"flow_recorded{self.oauth_cursor:03d}"
        self.oauth_started.append(
            {"flow_id": flow_id, "source_id": source_id, "vendor": vendor}
        )
        return OAuthFlowState(
            flow_id=flow_id,
            source_id=source_id,
            vendor=vendor,
            state="awaiting_action",
            auth_url="https://fixture.invalid/oauth",
            device_code=None,
            expects="paste_code",
            instructions_key="settings.models.oauth.pasteCode.hint",
            error_key=None,
            expires_at_iso="2026-08-15T08:15:00+00:00",
            credential_ref=None,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "credential_cursor": self.credential_cursor,
            "retargeted": copy.deepcopy(self.retargeted),
            "discoveries": copy.deepcopy(self.discoveries),
            "synced_bindings": copy.deepcopy(self.synced_bindings),
            "revoked": list(self.revoked),
            "discovery_script": copy.deepcopy(self.discovery_script),
            "observation_script": copy.deepcopy(self.observation_script),
            "provisioned": copy.deepcopy(self.provisioned),
            "oauth_started": copy.deepcopy(self.oauth_started),
            "oauth_cursor": self.oauth_cursor,
        }

    def __getattr__(self, member: str) -> Any:
        raise UnregisteredFixtureAccess(
            f"unregistered adapter access: {member}"
        )


class FrozenIds:
    def __init__(self, payload: dict[str, Any]):
        self.values = list(payload["source_ids"])
        self.cursor = int(payload["cursor"])

    def next_source_id(self) -> str:
        if self.cursor >= len(self.values):
            raise UnregisteredFixtureAccess("frozen source id stream exhausted")
        value = self.values[self.cursor]
        self.cursor += 1
        return value


class FixtureRuntime:
    def __init__(self, config: dict[str, Any], world: dict[str, Any]):
        self.world_template = copy.deepcopy(world)
        self.ids = FrozenIds(world["clock"])
        self.now_value = datetime.fromisoformat(world["clock"]["now"])
        facts = world["agent_facts"]
        with _fixture_guards(self.world_template, self.ids.next_source_id):
            self.store = MemoryConfigStore(config)
            self.adapter = RecordedAdapter(world["adapter"])
            self.revocations = MemoryRevocations(world["revocations"])
            self.oauth_flows = MemoryOAuthFlows(world["oauth_registry"])
            self.service = ModelHubService(
                store=self.store,
                adapter=self.adapter,
                events=UnregisteredCollaborator("events"),
                provenance=UnregisteredCollaborator("provenance"),
                native_oauth_adapter=UnregisteredCollaborator("native_oauth"),
                oauth_flows=self.oauth_flows,
                revocations=self.revocations,
                migration_claude_oauth_probe=lambda: bool(
                    world["migration"]["claude_oauth_signed_in"]
                ),
                requested_model_override=lambda backend: facts["requested_model"][backend],
                selected_agent_override=lambda backend: facts["selected_agent"][backend],
                named_agents_override=lambda backend: [
                    tuple(item) for item in facts["named_agents"][backend]
                ],
                cli_present_override=lambda backend: facts["cli_present"][backend],
                cli_presence_refresh=lambda: None,
                now=lambda: self.now_value,
            )
        self.service.native_source_ready = (
            lambda backend, _source: facts["native_source_ready"][backend]
        )
        service_state = world["service"]
        self.service._source_create_nonces = set(
            service_state["source_create_nonces"]
        )
        self.service._next_settlement_generation = service_state[
            "next_settlement_generation"
        ]
        self.service._latest_source_attempt_generation = copy.deepcopy(
            service_state["latest_source_attempt_generation"]
        )
        self.service._engine_synced = service_state["engine_synced"]
        self.service._engine_preparation_failed = service_state[
            "engine_preparation_failed"
        ]

    def config_snapshot(self) -> dict[str, Any]:
        return self.store.load().to_payload()

    def world_snapshot(self) -> dict[str, Any]:
        world = copy.deepcopy(self.world_template)
        world["clock"]["cursor"] = self.ids.cursor
        world["adapter"] = self.adapter.snapshot()
        world["revocations"] = self.revocations.snapshot()
        world["oauth_registry"] = self.oauth_flows.snapshot()
        world["service"] = {
            "source_create_nonces": sorted(self.service._source_create_nonces),
            "next_settlement_generation": self.service._next_settlement_generation,
            "latest_source_attempt_generation": copy.deepcopy(
                self.service._latest_source_attempt_generation
            ),
            "engine_synced": self.service._engine_synced,
            "engine_preparation_failed": self.service._engine_preparation_failed,
        }
        self.service._oauth_start_tasks = {
            key: task
            for key, task in self.service._oauth_start_tasks.items()
            if not task.done()
        }
        if self.service._oauth_start_tasks:
            raise UnregisteredFixtureAccess("fixture cannot snapshot live OAuth tasks")
        return world

    def read_projections(self) -> dict[str, Any]:
        agents = self.service.list_agents()
        chains: dict[str, dict[str, Any]] = {}
        config = self.store.load()
        for backend, agent in config.agents.items():
            if agent.mode != "hub":
                continue
            chains[backend] = {
                model_id: self.service.agent_chain(backend, model_id)
                for model_id in agent.routes
            }
        return {
            "sources": self.service.list_sources(),
            "agents": agents,
            "agent_sources": {agent["backend"]: agent for agent in agents},
            "agent_chains": chains,
        }


def _forbid(kind: str):
    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise UnregisteredFixtureAccess(f"real {kind} access is forbidden while recording")

    return denied


@contextmanager
def _fixture_guards(
    world: dict[str, Any],
    next_source_id: Any,
) -> Iterator[None]:
    builtin_ids = world["agent_facts"]["builtin_model_ids"]
    migration = world["migration"]

    def registered_builtin_ids(backend: str) -> tuple[str, ...]:
        if backend not in builtin_ids:
            raise UnregisteredFixtureAccess(
                f"unregistered builtin model catalog lookup: {backend}"
            )
        return tuple(builtin_ids[backend])

    with (
        patch.object(service_module, "_source_id", next_source_id),
        patch.object(service_module, "_utc_now", _forbid("clock")),
        patch.object(service_module, "datetime", SealedDateTime),
        patch.object(service_module, "time", UnregisteredCollaborator("clock")),
        patch.object(service_module, "uuid", UnregisteredCollaborator("id_stream")),
        patch.object(service_module, "_builtin_model_ids", registered_builtin_ids),
        patch.object(config_module, "model_hub_fixed_menu_ids", registered_builtin_ids),
        patch.object(
            migration_module,
            "read_claude_settings_env",
            lambda _home: copy.deepcopy(migration["claude_settings_env"]),
        ),
        patch.object(
            migration_module,
            "read_claude_oauth_signed_in",
            lambda _home: bool(migration["claude_oauth_signed_in"]),
        ),
        patch.object(
            migration_module,
            "_load_auth",
            lambda _path: copy.deepcopy(migration["codex_auth"]),
        ),
        patch.object(
            migration_module,
            "read_codex_auth_state",
            lambda _home: copy.deepcopy(migration["codex_state"]),
        ),
        patch.object(
            migration_module,
            "load_first_opencode_user_config",
            lambda *, home: OpenCodeConfigProbeResult(
                config=copy.deepcopy(migration["opencode_config"])
            ),
        ),
        patch.object(
            migration_module,
            "read_opencode_provider_auth_entries",
            lambda *, home: copy.deepcopy(migration["opencode_auth"]),
        ),
        patch.object(
            migration_module,
            "_load_opencode_provider_catalog",
            lambda _home: copy.deepcopy(migration["opencode_catalog"]),
        ),
        patch.object(builtins, "open", _forbid("filesystem")),
        patch.object(io, "open", _forbid("filesystem")),
        patch.object(os, "open", _forbid("filesystem")),
        patch.object(socket, "socket", _forbid("network")),
        patch.object(socket, "create_connection", _forbid("network")),
    ):
        yield


@contextmanager
def sealed_execution(runtime: FixtureRuntime) -> Iterator[None]:
    """Fail generation if server policy escapes the registered fixture world."""

    with _fixture_guards(
        runtime.world_template,
        runtime.ids.next_source_id,
    ):
        yield


def _confirmed_request(previous: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    outcome = previous["outcome"]
    if outcome["kind"] != "error":
        raise ValueError("confirm_previous_guard requires a server guard error")
    data = outcome["data"]
    operation = previous["key"]["request"]["operation"]
    request = copy.deepcopy(previous["key"]["request"])
    if operation == "deleteSource":
        request["query"] = {"force": {"present": True, "value": True}}
        request["body"] = {
            "present": True,
            "value": {
                "would_remove_hops": data.get("would_remove_hops", []),
                "would_interrupt": data.get("would_interrupt", []),
            },
        }
    elif operation == "patchSource":
        body = copy.deepcopy(request["body"]["value"])
        body.update(
            {
                "force": True,
                "would_remove_hops": data.get("would_remove_hops", []),
                "would_interrupt": data.get("would_interrupt", []),
            }
        )
        request["body"] = {"present": True, "value": body}
    else:
        raise ValueError(f"no exact-echo expansion for {operation}")
    return request


OperationHandler = Callable[[FixtureRuntime, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class OperationSpec:
    handler: OperationHandler
    recording_probe: dict[str, Any]


OPERATION_DISPATCH: dict[str, OperationSpec] = {}


def _request(
    operation: str,
    *,
    path: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    body: Any = None,
    body_present: bool = False,
) -> dict[str, Any]:
    return {
        "operation": operation,
        "path": path or {},
        "query": query or {},
        "body": (
            {"present": True, "value": body}
            if body_present
            else {"present": False}
        ),
    }


def _operation(
    name: str,
    recording_probe: dict[str, Any],
) -> Callable[[OperationHandler], OperationHandler]:
    def register(handler: OperationHandler) -> OperationHandler:
        if name in OPERATION_DISPATCH:
            raise RuntimeError(f"duplicate corpus operation: {name}")
        OPERATION_DISPATCH[name] = OperationSpec(handler, recording_probe)
        return handler

    return register


def _body_value(request: dict[str, Any]) -> Any:
    body = request["body"]
    return copy.deepcopy(body.get("value")) if body.get("present") else None


@_operation(
    "observeApiKeySource",
    _request(
        "observeApiKeySource",
        body={
            "vendor": "custom",
            "base_url": "https://missing.example/v1",
            "key": "test-only",
            "protocol_order": [
                "openai_chat",
                "openai_responses",
                "anthropic",
            ],
        },
        body_present=True,
    ),
)
async def _observe_source(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    result = await runtime.service.observe_source(_body_value(request))
    return result["observation"]


@_operation(
    "createApiKeySource",
    _request(
        "createApiKeySource",
        body={
            "kind": "api_key",
            "vendor": "custom",
            "display_name": "Missing",
            "base_url": "https://missing.example/v1",
            "supply_channel": "hub",
            "key": "test-only",
            "client_nonce": "scn_0000000000000001",
            "protocol_order": [
                "openai_chat",
                "openai_responses",
                "anthropic",
            ],
        },
        body_present=True,
    ),
)
async def _create_source(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.create_source(_body_value(request))


@_operation(
    "deleteSource",
    _request("deleteSource", path={"id": "src_missing0001"}),
)
async def _delete_source(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    value = _body_value(request) or {}
    force = request["query"].get("force", {}).get("value") is True
    return await runtime.service.delete_source(
        request["path"]["id"],
        force=force,
        confirmed_remove_hops=value.get("would_remove_hops"),
        confirmed_interruptions=value.get("would_interrupt"),
    )


@_operation(
    "patchSource",
    _request(
        "patchSource",
        path={"id": "src_missing0001"},
        body={"display_name": "Missing"},
        body_present=True,
    ),
)
async def _patch_source(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.patch_source(
        request["path"]["id"],
        _body_value(request),
    )


@_operation(
    "refreshSource",
    _request(
        "refreshSource",
        path={"id": "src_missing0001"},
        body={},
        body_present=True,
    ),
)
async def _refresh_source(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    value = _body_value(request) or {}
    return await runtime.service.refresh_source(
        request["path"]["id"],
        force=value.get("force") is True,
        confirmed_remove_hops=value.get("would_remove_hops"),
        confirmed_interruptions=value.get("would_interrupt"),
    )


@_operation(
    "replaceCredential",
    _request(
        "replaceCredential",
        path={"id": "src_missing0001"},
        body={"key": "test-only"},
        body_present=True,
    ),
)
async def _replace_credential(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.replace_credential(
        request["path"]["id"],
        _body_value(request),
    )


@_operation(
    "reauthSource",
    _request(
        "reauthSource",
        path={"id": "src_missing0001"},
        body={"acknowledge_irreversible": True},
        body_present=True,
    ),
)
async def _reauth_source(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    result = await runtime.service.reauth_source(
        request["path"]["id"],
        _body_value(request),
    )
    return result["flow"]


@_operation(
    "putAgentSources",
    _request(
        "putAgentSources",
        path={"backend": "claude"},
        body={"order": []},
        body_present=True,
    ),
)
async def _put_agent_sources(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.set_agent_sources(
        request["path"]["backend"],
        _body_value(request),
    )


@_operation(
    "getAgentSources",
    _request("getAgentSources", path={"backend": "missing"}),
)
async def _get_agent_sources(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return runtime.service.get_agent_sources(request["path"]["backend"])


@_operation(
    "getAgentChain",
    _request(
        "getAgentChain",
        path={"backend": "missing"},
        query={"model": {"present": True, "value": "missing"}},
    ),
)
async def _get_agent_chain(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    model = request["query"].get("model", {}).get("value")
    return runtime.service.agent_chain(request["path"]["backend"], model)


@_operation(
    "putAgentChain",
    _request(
        "putAgentChain",
        path={"backend": "claude"},
        query={"model": {"present": True, "value": "claude-opus-4-6"}},
        body={"hops": []},
        body_present=True,
    ),
)
async def _put_agent_chain(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    model = request["query"].get("model", {}).get("value")
    return await runtime.service.set_agent_chain(
        request["path"]["backend"],
        model,
        _body_value(request),
    )


@_operation(
    "probeAgent",
    _request(
        "probeAgent",
        path={"backend": "claude"},
        body={"model": "missing"},
        body_present=True,
    ),
)
async def _probe_agent(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.probe_agent(
        request["path"]["backend"],
        (_body_value(request) or {}).get("model"),
    )


@_operation(
    "setAgentMode",
    _request(
        "setAgentMode",
        path={"backend": "claude"},
        body={"mode": "direct"},
        body_present=True,
    ),
)
async def _set_agent_mode(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.set_agent_mode(
        request["path"]["backend"],
        (_body_value(request) or {}).get("mode"),
    )


@_operation(
    "putMenu",
    _request(
        "putMenu",
        body={"menu": {"view": "full", "checked": []}},
        body_present=True,
    ),
)
async def _put_menu(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.set_opencode_menu(
        (_body_value(request) or {}).get("menu")
    )


@_operation(
    "addCustomModel",
    _request(
        "addCustomModel",
        path={"sourceId": "src_missing0001"},
        body={"model_id": "missing", "reasoning_efforts": []},
        body_present=True,
    ),
)
async def _add_custom_model(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.add_custom_model(
        request["path"]["sourceId"],
        _body_value(request),
    )


@_operation(
    "updateModelReasoningEfforts",
    _request(
        "updateModelReasoningEfforts",
        path={"sourceId": "src_missing0001", "modelId": "missing"},
        body={"reasoning_efforts": []},
        body_present=True,
    ),
)
async def _update_reasoning(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.update_model_reasoning_efforts(
        request["path"]["sourceId"],
        request["path"]["modelId"],
        _body_value(request),
    )


@_operation(
    "deleteCustomModel",
    _request(
        "deleteCustomModel",
        path={"sourceId": "src_missing0001", "modelId": "missing"},
        body={},
        body_present=True,
    ),
)
async def _delete_custom_model(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    value = _body_value(request) or {}
    result = await runtime.service.delete_custom_model(
        request["path"]["sourceId"],
        request["path"]["modelId"],
        force=value.get("force") is True,
        confirmed_remove_hops=value.get("would_remove_hops"),
        confirmed_interruptions=value.get("would_interrupt"),
    )
    return result["source"]


@_operation("scanMigration", _request("scanMigration"))
async def _scan_migration(runtime: FixtureRuntime, _request: dict[str, Any]) -> Any:
    return runtime.service.migration_scan()


@_operation(
    "applyMigration",
    _request(
        "applyMigration",
        body={"item_ids": []},
        body_present=True,
    ),
)
async def _apply_migration(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.migration_apply(
        (_body_value(request) or {}).get("item_ids")
    )


@_operation("startRuntime", _request("startRuntime"))
async def _start_runtime(runtime: FixtureRuntime, _request: dict[str, Any]) -> Any:
    return await runtime.service.runtime_start()


@_operation(
    "startOAuth",
    _request(
        "startOAuth",
        body={
            "vendor": "anthropic",
            "channel": "hub",
            "client_nonce": "ofn_0000000000000001",
        },
        body_present=True,
    ),
)
async def _start_oauth(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    result = await runtime.service.oauth_start(_body_value(request))
    return result["flow"]


@_operation(
    "getOAuthStatus",
    _request("getOAuthStatus", path={"flowId": "flow_missing"}),
)
async def _get_oauth_status(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.oauth_status(request["path"]["flowId"])


@_operation(
    "submitOAuth",
    _request(
        "submitOAuth",
        body={"flow_id": "flow_missing", "value": "test-only"},
        body_present=True,
    ),
)
async def _submit_oauth(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    return await runtime.service.oauth_submit(_body_value(request))


@_operation(
    "cancelOAuth",
    _request(
        "cancelOAuth",
        body={"flow_id": "flow_missing"},
        body_present=True,
    ),
)
async def _cancel_oauth(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    await runtime.service.oauth_cancel(
        (_body_value(request) or {}).get("flow_id")
    )
    return None


async def _execute(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    operation = request["operation"]
    spec = OPERATION_DISPATCH.get(operation)
    if spec is None:
        raise UnregisteredFixtureAccess(
            f"no authoritative server dispatch is registered for corpus operation: {operation}"
        )
    return await spec.handler(runtime, request)


async def _record_transition(
    runtime: FixtureRuntime,
    request: dict[str, Any],
    *,
    sequence_id: str,
    step: int,
) -> dict[str, Any]:
    pre_config = runtime.config_snapshot()
    pre_world = runtime.world_snapshot()
    key_body = {
        "version": 1,
        "pre": {
            "model_hub_config_sha256": _sha256(pre_config),
            "fixture_world_sha256": _sha256(pre_world),
        },
        "request": request,
    }
    key = {**key_body, "id": _token(key_body)}
    try:
        with sealed_execution(runtime):
            result = await _execute(runtime, request)
    except ModelHubError as error:
        outcome = {
            "kind": "error",
            "error": error.code,
            "detail": error.detail,
            "status": error.status,
            "data": copy.deepcopy(error.data),
        }
    else:
        outcome = {"kind": "success", "value": _json_value(result)}
    with sealed_execution(runtime):
        reads = runtime.read_projections()
    post_config = runtime.config_snapshot()
    post_world = runtime.world_snapshot()
    return {
        "key": key,
        "pre": {"config": pre_config, "fixture_world": pre_world},
        "outcome": outcome,
        "post": {
            "model_hub_config_sha256": _sha256(post_config),
            "fixture_world_sha256": _sha256(post_world),
            "config": post_config,
            "fixture_world": post_world,
            "reads": reads,
        },
        "sequence_id": sequence_id,
        "step": step,
    }


def _transition_identity(record: dict[str, Any]) -> dict[str, Any]:
    """Exclude sequence location while retaining state, request, and outcome."""

    return {
        "key": record["key"],
        "pre": record["pre"],
        "outcome": record["outcome"],
        "post": record["post"],
    }


async def _generate(
    seed: dict[str, Any],
    sequence_spec: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    seed_runtime = FixtureRuntime(seed["config"], seed["fixture_world"])
    with sealed_execution(seed_runtime):
        seed_reads = seed_runtime.read_projections()
    seed_config = seed_runtime.config_snapshot()
    seed_world = seed_runtime.world_snapshot()
    transitions: dict[str, dict[str, Any]] = {}
    sequences: list[dict[str, Any]] = []
    traces: dict[str, list[dict[str, Any]]] = {}
    for sequence in sequence_spec["sequences"]:
        runtime = FixtureRuntime(seed["config"], seed["fixture_world"])
        records: list[dict[str, Any]] = []
        transition_ids: list[str] = []
        for step, action in enumerate(sequence["actions"], start=1):
            request = (
                _confirmed_request(records[-1], action)
                if action.get("confirm_previous_guard") is True
                else copy.deepcopy(action)
            )
            request.pop("confirm_previous_guard", None)
            request.setdefault("query", {})
            request.setdefault("body", {"present": False})
            record = await _record_transition(
                runtime,
                request,
                sequence_id=sequence["id"],
                step=step,
            )
            transition_id = record["key"]["id"]
            existing = transitions.get(transition_id)
            if existing is not None and _canonical(
                _transition_identity(existing)
            ) != _canonical(_transition_identity(record)):
                raise RuntimeError(f"duplicate transition key changed outcome: {transition_id}")
            transitions[transition_id] = record
            transition_ids.append(transition_id)
            records.append(record)
        traces[sequence["id"]] = records
        sequences.append({"id": sequence["id"], "transition_ids": transition_ids})
    corpus = {
        "version": 1,
        "generator": GENERATOR_COMMAND,
        "recording_operations": [
            {
                "operation": operation,
                "request": copy.deepcopy(spec.recording_probe),
            }
            for operation, spec in sorted(OPERATION_DISPATCH.items())
        ],
        "seed": {
            "model_hub_config_sha256": _sha256(seed_config),
            "fixture_world_sha256": _sha256(seed_world),
            "config": seed_config,
            "fixture_world": seed_world,
            "reads": seed_reads,
        },
        "sequences": sequences,
        "transitions": list(transitions.values()),
    }
    return corpus, traces


def _render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _render_corpus(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _record_miss(
    token: str,
    sequence_spec: dict[str, Any],
    corpus: dict[str, Any],
    traces: dict[str, list[dict[str, Any]]],
) -> None:
    missing = _decode_token(token)
    if set(missing) != {"version", "pre", "request"} or missing["version"] != 1:
        raise ValueError("record-miss token is not a v1 transition key")
    target_pre = missing["pre"]
    prefix: list[dict[str, Any]] | None = None
    if target_pre == {
        "model_hub_config_sha256": corpus["seed"]["model_hub_config_sha256"],
        "fixture_world_sha256": corpus["seed"]["fixture_world_sha256"],
    }:
        prefix = []
    if prefix is None:
        by_id = {
            sequence["id"]: sequence
            for sequence in sequence_spec["sequences"]
        }
        for sequence_id, records in traces.items():
            for index, record in enumerate(records):
                post = record["post"]
                if target_pre == {
                    "model_hub_config_sha256": _sha256(post["config"]),
                    "fixture_world_sha256": _sha256(post["fixture_world"]),
                }:
                    prefix = copy.deepcopy(by_id[sequence_id]["actions"][: index + 1])
                    break
            if prefix is not None:
                break
    if prefix is None:
        raise ValueError("missing pre-state is not reachable from a recorded sequence")
    action = copy.deepcopy(missing["request"])
    sequence_id = f"recorded-{hashlib.sha256(token.encode()).hexdigest()[:12]}"
    if any(item["id"] == sequence_id for item in sequence_spec["sequences"]):
        return
    sequence_spec["sequences"].append(
        {"id": sequence_id, "actions": [*prefix, action]}
    )
    SEQUENCES_PATH.write_text(_render(sequence_spec), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--record-miss")
    args = parser.parse_args()
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    sequence_spec = json.loads(SEQUENCES_PATH.read_text(encoding="utf-8"))
    corpus, traces = asyncio.run(_generate(seed, sequence_spec))
    if args.record_miss:
        if args.check:
            parser.error("--check and --record-miss are mutually exclusive")
        _record_miss(args.record_miss, sequence_spec, corpus, traces)
        sequence_spec = json.loads(SEQUENCES_PATH.read_text(encoding="utf-8"))
        corpus, _ = asyncio.run(_generate(seed, sequence_spec))
    rendered = _render_corpus(corpus)
    if args.check:
        if not OUTPUT_PATH.exists() or OUTPUT_PATH.read_text(encoding="utf-8") != rendered:
            print(
                "Model Hub mock corpus is stale; run " + GENERATOR_COMMAND,
                file=sys.stderr,
            )
            return 1
        return 0
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    output_label = (
        OUTPUT_PATH.relative_to(ROOT)
        if OUTPUT_PATH.is_relative_to(ROOT)
        else OUTPUT_PATH
    )
    print(
        f"wrote {len(corpus['transitions'])} transitions across "
        f"{len(corpus['sequences'])} sequences to {output_label}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
