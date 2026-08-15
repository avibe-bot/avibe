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
from typing import Any, Iterator, Mapping
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import v2_config as config_module  # noqa: E402
from config.v2_config import ModelHubConfig  # noqa: E402
from core.handlers.model_hub import service as service_module  # noqa: E402
from core.handlers.model_hub.service import ModelHubError, ModelHubService  # noqa: E402

SEED_PATH = ROOT / "scripts/model_hub_mock_seed.json"
SEQUENCES_PATH = ROOT / "scripts/model_hub_mock_sequences.json"
OUTPUT_PATH = (
    ROOT
    / "ui/src/components/settings/models/modelHubMockCorpus.json"
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

    def snapshot(self) -> dict[str, Any]:
        return {
            "credential_cursor": self.credential_cursor,
            "retargeted": copy.deepcopy(self.retargeted),
            "discoveries": copy.deepcopy(self.discoveries),
            "synced_bindings": copy.deepcopy(self.synced_bindings),
            "revoked": list(self.revoked),
            "discovery_script": copy.deepcopy(self.discovery_script),
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
            self.service = ModelHubService(
                store=self.store,
                adapter=self.adapter,
                events=UnregisteredCollaborator("events"),
                provenance=UnregisteredCollaborator("provenance"),
                native_oauth_adapter=UnregisteredCollaborator("native_oauth"),
                oauth_flows=UnregisteredCollaborator("oauth_registry"),
                revocations=self.revocations,
                migration_claude_oauth_probe=self._unregistered_migration_probe,
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

    @staticmethod
    def _unregistered_migration_probe() -> bool:
        raise UnregisteredFixtureAccess("unregistered fixture collaborator: migration")

    def config_snapshot(self) -> dict[str, Any]:
        return self.store.load().to_payload()

    def world_snapshot(self) -> dict[str, Any]:
        world = copy.deepcopy(self.world_template)
        world["clock"]["cursor"] = self.ids.cursor
        world["adapter"] = self.adapter.snapshot()
        world["revocations"] = self.revocations.snapshot()
        world["service"] = {
            "source_create_nonces": sorted(self.service._source_create_nonces),
            "next_settlement_generation": self.service._next_settlement_generation,
            "latest_source_attempt_generation": copy.deepcopy(
                self.service._latest_source_attempt_generation
            ),
            "engine_synced": self.service._engine_synced,
            "engine_preparation_failed": self.service._engine_preparation_failed,
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


async def _execute(runtime: FixtureRuntime, request: dict[str, Any]) -> Any:
    operation = request["operation"]
    path = request.get("path", {})
    body = request.get("body", {"present": False})
    value = copy.deepcopy(body.get("value")) if body.get("present") else None
    if operation == "deleteSource":
        force = request.get("query", {}).get("force", {}).get("value") is True
        return await runtime.service.delete_source(
            path["id"],
            force=force,
            confirmed_remove_hops=(value or {}).get("would_remove_hops"),
            confirmed_interruptions=(value or {}).get("would_interrupt"),
        )
    if operation == "patchSource":
        return await runtime.service.patch_source(path["id"], value)
    if operation == "putAgentSources":
        return await runtime.service.set_agent_sources(path["backend"], value)
    if operation == "getAgentSources":
        return runtime.service.get_agent_sources(path["backend"])
    if operation == "getAgentChain":
        model = request.get("query", {}).get("model", {}).get("value")
        return runtime.service.agent_chain(path["backend"], model)
    raise UnregisteredFixtureAccess(f"unregistered corpus operation: {operation}")


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
            if existing is not None and _canonical(existing) != _canonical(record):
                raise RuntimeError(f"duplicate transition key changed outcome: {transition_id}")
            transitions[transition_id] = record
            transition_ids.append(transition_id)
            records.append(record)
        traces[sequence["id"]] = records
        sequences.append({"id": sequence["id"], "transition_ids": transition_ids})
    corpus = {
        "version": 1,
        "generator": GENERATOR_COMMAND,
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
    print(
        f"wrote {len(corpus['transitions'])} transitions across "
        f"{len(corpus['sequences'])} sequences to {OUTPUT_PATH.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
