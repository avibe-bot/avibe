from __future__ import annotations

import ast
import asyncio
import inspect
import json
import tempfile
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from aiohttp import web
from jsonschema import Draft7Validator, FormatChecker
from sqlalchemy import create_engine, delete, select

from config.v2_config import (
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubBackendModelConfig,
    ModelHubConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    model_hub_fixed_menu_ids,
)
from core.handlers.model_hub.adapter import (
    DiscoveredModel,
    EngineHealth,
    EngineStatus,
    RawCallOutcome,
    RawOutcomeKind,
    SOURCE_PROTOCOLS,
)
from core.handlers.model_hub.classification import ResolutionDecision, classify_outcome
from core.handlers.model_hub.events import (
    BoundedEventLog,
    EVENT_REASON_AUTHORITY,
    EventKind,
    EventReason,
    build_resolution_event,
    event_reason_label,
)
from core.handlers.model_hub.provenance import (
    BoundedProvenanceStore,
    ENGINE_DOWN_TURN_OUTCOME,
    ExactHopBlocker,
    TURN_OUTCOME_RENDERING_AUTHORITY,
    TurnOutcomeProductionError,
    TurnCorrelationRegistry,
    exact_hop_blockers,
    produce_turn_outcome,
    project_turn_outcome_copy,
    render_turn_outcome_copy,
)
from core.handlers.model_hub.request import ModelHubRequest
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import (
    ModelHubError,
    ModelHubService,
    ResolvedInvocation,
    project_opencode_public_model,
)
from core.handlers.model_hub.turn_gateway import (
    ModelHubTurnGateway,
    _RenderedTurnOutcome,
    render_protocol_terminal_event,
)
from core.handlers.model_hub.stream_wire import ProtocolSSEState, ProtocolUsageReport
from core.handlers.model_hub.usage import BoundedUsageLedger, UsageWriter, _ledger_executor
from core.run_settlement import (
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
)
from core.services.dispatch import TurnDispatchOutcome
from core.session_turns import SessionTurnManager
from modules.agents.model_hub import (
    ModelHubLaunch,
    ModelHubRuntimeRouter,
    _localized_launch_error,
    bind_launch,
    bind_turn_mode,
    opencode_model_catalog_for_overlay,
)
from storage.models import agent_sessions, messages, metadata
from vibe.i18n import t as i18n_t
from vibe.model_hub_runtime.adapter import (
    _AuthenticationEvidence,
    CLIProxyEngineAdapter,
    _parse_protocol_authenticated_evidence,
    _probe_protocol_response,
    _PROTOCOL_OBSERVATION_TAXONOMY,
    _ProtocolEvidence,
    _ProtocolObservationShape,
    _ProtocolProof,
)
from vibe.model_hub_runtime.api_key_vendors import api_key_vendor_catalog
from vibe.model_hub_runtime.client import EngineClientError, probe_models
from vibe.model_hub_runtime.state import EngineStateStore


CONTRACTS = Path(__file__).parents[1] / "docs" / "plans" / "model-hub-contracts"
MODEL_HUB_FIXTURES = Path(__file__).parent / "fixtures" / "model_hub"
TERMINAL_SETTLEMENT_BOUNDARIES = json.loads(
    (MODEL_HUB_FIXTURES / "terminal_settlement_boundaries.json").read_text(encoding="utf-8")
)["cases"]
RELEASED_V5_PERMISSION_DENIED = json.loads(
    (MODEL_HUB_FIXTURES / "released_v5_permission_denied.json").read_text(encoding="utf-8")
)
E64_SETTLEMENT_BOUNDARIES = json.loads(
    (MODEL_HUB_FIXTURES / "e64_settlement_boundaries.json").read_text(encoding="utf-8")
)
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
CATALOG_API_KEY_VENDOR_PROTOCOL_CASES = tuple(
    pytest.param(entry.id, entry.protocol, id=entry.id)
    for entry in api_key_vendor_catalog()
)


def _assert_valid(schema_name: str, payload: dict) -> None:
    schema = json.loads((CONTRACTS / schema_name).read_text(encoding="utf-8"))
    errors = sorted(
        Draft7Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    assert not errors, [error.message for error in errors]


def _turn_outcome_matrix() -> dict[str, str]:
    lines = (
        Path(__file__).parents[1] / "docs/plans/model-hub.md"
    ).read_text(encoding="utf-8").splitlines()
    heading = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("**Turn-outcome copy matrix")
    )
    table = next(
        index
        for index in range(heading + 1, len(lines))
        if lines[index].startswith("| Decision |")
    )
    rows: dict[str, str] = {}
    for line in lines[table + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows[cells[0].strip("`")] = cells[1].strip("`")
    return rows


def test_turn_outcome_rendering_authority_covers_matrix_and_locales() -> None:
    assert _turn_outcome_matrix() == {
        decision: rule.outcome
        for decision, rule in TURN_OUTCOME_RENDERING_AUTHORITY.items()
    }
    assert len(
        {
            (rule.outcome, rule.discriminator)
            for rule in TURN_OUTCOME_RENDERING_AUTHORITY.values()
        }
    ) == len(TURN_OUTCOME_RENDERING_AUTHORITY)

    projected_keys = {
        key
        for rule in TURN_OUTCOME_RENDERING_AUTHORITY.values()
        for _variant, key in rule.copy_keys
        if key is not None
    }
    locale_launch_keys = None
    for locale in ("en", "zh"):
        payload = json.loads(
            (Path(__file__).parents[1] / f"vibe/i18n/{locale}.json").read_text(
                encoding="utf-8"
            )
        )
        launch_keys = {
            f"modelHub.launch.{key}" for key in payload["modelHub"]["launch"]
        }
        locale_launch_keys = locale_launch_keys or launch_keys
        assert launch_keys == locale_launch_keys
        assert launch_keys == {
            key for key in projected_keys if key.startswith("modelHub.launch.")
        }
        assert all(i18n_t(key, locale) != key for key in projected_keys)


@pytest.mark.parametrize(
    ("protocol", "expected_shape"),
    [
        ("anthropic", "anthropic"),
        ("openai_responses", "responses"),
        ("openai_chat", "chat"),
    ],
)
def test_terminal_event_renderer_uses_native_protocol_shape(
    protocol: str,
    expected_shape: str,
) -> None:
    event = render_protocol_terminal_event(
        protocol,
        "modelHub.launch.retry",
        "Retry directly.",
        next_sequence_number=0,
    )

    assert "model_hub_terminal" not in str(event)
    if expected_shape == "anthropic":
        assert event == {
            "type": "error",
            "error": {"type": "api_error", "message": "Retry directly."},
        }
        assert isinstance(event["type"], str)
        assert isinstance(event["error"], dict)
        assert isinstance(event["error"]["type"], str)
        assert isinstance(event["error"]["message"], str)
    elif expected_shape == "responses":
        assert event == {
            "type": "error",
            "code": "modelHub.launch.retry",
            "message": "Retry directly.",
            "param": None,
            "sequence_number": 0,
        }
        assert isinstance(event["type"], str)
        assert isinstance(event["code"], str)
        assert isinstance(event["message"], str)
        assert event["param"] is None or isinstance(event["param"], str)
        assert isinstance(event["sequence_number"], int)
    else:
        assert event == {
            "object": "chat.completion.chunk",
            "type": "error",
            "error": {
                "type": "server_error",
                "code": "modelHub.launch.retry",
                "message": "Retry directly.",
            },
            "choices": [],
        }
        assert isinstance(event["object"], str)
        assert isinstance(event["type"], str)
        assert isinstance(event["error"], dict)
        assert isinstance(event["error"]["type"], str)
        assert isinstance(event["error"]["code"], str)
        assert isinstance(event["error"]["message"], str)
        assert isinstance(event["choices"], list)


def test_responses_terminal_event_discards_partial_sequence_before_injection() -> None:
    wire = ProtocolSSEState("openai_responses")
    wire.observe(b'data: {"type":"response.output_text.delta","sequence_number":7}\n\n')
    wire.observe(b'data: {"type":"response.output_text.delta","sequence_number":8}')

    assert wire.next_sequence_number == 8
    assert wire.invalidate_partial_frame() == b"\ndata: {}\n\n"
    assert wire.next_sequence_number == 8
    event = render_protocol_terminal_event(
        "openai_responses",
        "modelHub.launch.retry",
        "Retry directly.",
        next_sequence_number=wire.next_sequence_number,
    )
    assert event["sequence_number"] == 8


def test_responses_terminal_event_continues_sequence_on_cr_only_frames() -> None:
    wire = ProtocolSSEState("openai_responses")
    wire.observe(
        b'data: {"type":"response.output_text.delta","sequence_number":7}\r\r'
        b'data: {"type":"response.output_text.delta","sequence_number":8}\r\r'
    )

    assert wire.invalidate_partial_frame() == b""
    assert wire.next_sequence_number == 9
    event = render_protocol_terminal_event(
        "openai_responses",
        "modelHub.launch.retry",
        "Retry directly.",
        next_sequence_number=wire.next_sequence_number,
    )
    assert event["sequence_number"] == 9


def test_turn_outcome_copy_projection_has_one_runtime_owner() -> None:
    root = Path(__file__).parents[1]
    owner = root / "core/handlers/model_hub/provenance.py"
    runtime_files = [
        *sorted((root / "core/handlers/model_hub").glob("*.py")),
        root / "modules/agents/model_hub.py",
    ]

    assert "modelHub.launch." in owner.read_text(encoding="utf-8")
    for path in runtime_files:
        if path == owner:
            continue
        source = path.read_text(encoding="utf-8")
        assert "modelHub.launch." not in source
        assert '"copy_key"' not in source

    excluded = {".git", ".venv", "node_modules"}

    def is_projection_constructor(node: ast.Call) -> bool:
        return (
            isinstance(node.func, ast.Name)
            and node.func.id == "TurnOutcomeProjectionInput"
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "TurnOutcomeProjectionInput"
        )

    constructor_calls: dict[Path, set[int]] = {}
    for path in root.rglob("*.py"):
        if any(part in excluded for part in path.parts):
            continue
        calls = [
            node.lineno
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and is_projection_constructor(node)
        ]
        if calls:
            constructor_calls[path] = set(calls)
    owner_tree = ast.parse(owner.read_text(encoding="utf-8"))
    producer = next(
        node
        for node in owner_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "produce_turn_outcome"
    )
    producer_calls = {
        node.lineno
        for node in ast.walk(producer)
        if isinstance(node, ast.Call)
        and is_projection_constructor(node)
    }
    assert producer_calls
    assert constructor_calls == {owner: producer_calls}


def test_gateway_handle_termination_has_one_settlement_owner() -> None:
    path = Path(__file__).parents[1] / "core/handlers/model_hub/turn_gateway.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_settle_consumed_handle"
    )
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }

    def is_call(node: ast.AST, name: str) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        )

    all_outcome_calls = {
        node.lineno for node in ast.walk(tree) if is_call(node, "outcome")
    }
    owner_outcome_calls = {
        node.lineno for node in ast.walk(owner) if is_call(node, "outcome")
    }
    all_close_calls = {
        node.lineno for node in ast.walk(tree) if is_call(node, "close_stream")
    }
    owner_close_calls = {
        node.lineno for node in ast.walk(owner) if is_call(node, "close_stream")
    }
    all_settlement_calls = {
        node.lineno
        for node in ast.walk(tree)
        if is_call(node, "settle_handle_outcome")
    }
    owner_settlement_calls = {
        node.lineno
        for node in ast.walk(owner)
        if is_call(node, "settle_handle_outcome")
    }
    settlement_nodes = [
        node
        for node in ast.walk(owner)
        if is_call(node, "settle_handle_outcome")
    ]
    assert owner_outcome_calls
    assert owner_close_calls
    assert owner_settlement_calls
    assert all_outcome_calls == owner_outcome_calls
    assert all_close_calls == owner_close_calls
    assert max(owner_close_calls) < min(owner_outcome_calls)
    assert all_settlement_calls == owner_settlement_calls
    assert all(
        any(keyword.arg == "termination_origin" for keyword in node.keywords)
        for node in settlement_nodes
    )
    assert all(
        any(keyword.arg == "record_attempt" for keyword in node.keywords)
        for node in settlement_nodes
    )

    cancel_handlers = [
        (function.name, handler)
        for function in functions.values()
        for node in ast.walk(function)
        if isinstance(node, ast.Try)
        for handler in node.handlers
        if isinstance(handler.type, ast.Attribute)
        and isinstance(handler.type.value, ast.Name)
        and handler.type.value.id == "asyncio"
        and handler.type.attr == "CancelledError"
    ]
    assert [name for name, _handler in cancel_handlers] == ["_handle_request"]

    request_runner = functions["_run_request_turn"]
    registered_closers = [
        node
        for node in ast.walk(request_runner)
        if is_call(node, "push_async_callback")
    ]
    assert len(registered_closers) == 1
    assert isinstance(registered_closers[0].args[0], ast.Attribute)
    assert registered_closers[0].args[0].attr == "close_stream"


def test_terminal_chain_reinspection_has_no_execution_channel_input() -> None:
    path = Path(__file__).parents[1] / "core/handlers/model_hub/service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    owner = functions["_inspect_terminal_chain"]
    owner_inputs = {
        argument.arg
        for argument in (*owner.args.args, *owner.args.kwonlyargs)
    }
    assert "supply_channel" not in owner_inputs

    def calls_owner(function_name: str) -> bool:
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_inspect_terminal_chain"
            for node in ast.walk(functions[function_name])
        )

    assert calls_owner("_produce_attempt_terminal_outcome")
    assert calls_owner("resolve")


def _terminal_resolution_facts(
    config: ModelHubConfig,
    *,
    model: str = "shared-model",
    supply_status: str,
    structural_reason: str | None = None,
    blocker_reason: str | None = None,
    next_hop: tuple[str, str] | None = None,
) -> SimpleNamespace:
    source = config.sources[0] if config.sources else None
    inspections = (
        (
            SimpleNamespace(
                runnable=False,
                source=source,
                source_id=(source.id if source is not None else None),
                model_id=model,
                reason=blocker_reason,
            ),
        )
        if blocker_reason is not None
        else ()
    )
    candidates = (
        (
            SimpleNamespace(
                source_id=next_hop[0],
                model_id=next_hop[1],
            ),
        )
        if next_hop is not None
        else ()
    )
    return SimpleNamespace(
        backend="claude",
        requested_model=model,
        target_model=model,
        matching_sources=tuple(config.sources),
        inspected_hops=inspections,
        candidate_hops=candidates,
        supply_status=supply_status,
        structural_blocker_reason=structural_reason,
    )


@pytest.mark.parametrize(
    ("decision", "variant", "expected_key"),
    tuple(
        (decision, variant, key)
        for decision, rule in TURN_OUTCOME_RENDERING_AUTHORITY.items()
        for variant, key in rule.copy_keys
    ),
)
def test_every_turn_outcome_matrix_variant_projects_or_stays_silent(
    decision: str,
    variant: str,
    expected_key: str | None,
) -> None:
    producer_kwargs = {}
    if variant in {"waiting", "interrupted"}:
        if decision == "turn.no_candidate.unconfigured":
            config = _config([])
        else:
            source = _source(
                "src_matrix001",
                "Matrix source",
                status=("cooldown" if variant == "waiting" else "needs_action"),
                retry_at=(
                    (NOW + timedelta(minutes=5)).isoformat()
                    if variant == "waiting"
                    else None
                ),
            )
            if variant == "interrupted":
                source.state.detail_key = (
                    "models.source.needs_action.credential_revoked"
                )
            config = _config([source])
        resolution = _terminal_resolution_facts(
            config,
            supply_status=variant,
            structural_reason=(
                "route_unconfigured"
                if decision == "turn.no_candidate.unconfigured"
                else None
            ),
            blocker_reason=(
                "credential_revoked" if variant == "interrupted" else None
            ),
        )
        producer_kwargs = {"config": config, "resolution": resolution}
        if decision == "turn.streamed_fallback":
            producer_kwargs["attempted_hop"] = (
                "src_attempted01",
                "shared-model",
            )
            producer_kwargs["source_transition_persisted"] = True
    elif variant == "transition_unpersisted":
        producer_kwargs = {"source_transition_persisted": False}
    elif variant == "waiting_without_retry":
        source = _source("src_matrix_ready", "Recovered source")
        config = _config([source])
        resolution = _terminal_resolution_facts(
            config,
            supply_status="degraded",
            next_hop=(source.id, "shared-model"),
        )
        producer_kwargs = {"config": config, "resolution": resolution}
    elif variant == "next_current":
        source = _source("src_matrix002", "Next source")
        config = _config([source])
        resolution = _terminal_resolution_facts(
            config,
            supply_status="degraded",
            next_hop=(source.id, "shared-model"),
        )
        producer_kwargs = {
            "config": config,
            "resolution": resolution,
            "attempted_hop": ("src_attempted02", "shared-model"),
            "source_transition_persisted": True,
        }
    projection = produce_turn_outcome(
        decision,
        stream_started=variant == "stream_started",
        **producer_kwargs,
    )

    copy = project_turn_outcome_copy(projection)

    assert (copy.key if copy is not None else None) == expected_key
    rendered = render_turn_outcome_copy(projection, "en")
    assert (rendered is None) == (expected_key is None)


def test_turn_outcome_producer_rejects_missing_streamed_fallback_facts() -> None:
    with pytest.raises(TurnOutcomeProductionError, match="missing"):
        produce_turn_outcome("turn.streamed_fallback")


def test_recovered_exhaustion_projects_waiting_without_a_past_retry_time() -> None:
    source = _source("src_recovered01", "Recovered source")
    config = _config([source])
    resolution = _terminal_resolution_facts(
        config,
        supply_status="degraded",
        next_hop=(source.id, "shared-model"),
    )

    projection = produce_turn_outcome(
        "turn.exhausted",
        config=config,
        resolution=resolution,
    )
    copy = project_turn_outcome_copy(projection)

    assert projection.outcome == "exhausted"
    assert projection.supply_facts is not None
    assert projection.supply_facts.supply_state == "waiting"
    assert projection.supply_facts.retry_at == ""
    assert copy is not None
    assert copy.key == "modelHub.launch.waiting_without_retry"


def test_recovered_no_candidate_projects_waiting_without_retry_time() -> None:
    source = _source("src_recovered02", "Recovered source")
    config = _config([source])
    resolution = _terminal_resolution_facts(
        config,
        supply_status="degraded",
        next_hop=(source.id, "shared-model"),
    )

    projection = produce_turn_outcome(
        "turn.no_candidate.blocked",
        config=config,
        resolution=resolution,
    )
    copy = project_turn_outcome_copy(projection)

    assert projection.supply_facts is not None
    assert projection.supply_facts.supply_state == "waiting"
    assert projection.supply_facts.retry_at == ""
    assert copy is not None
    assert copy.key == "modelHub.launch.waiting_without_retry"


def test_route_unconfigured_launch_copy_exists_in_each_backend_locale() -> None:
    config = _config([])
    resolution = _terminal_resolution_facts(
        config,
        model="menu-model",
        supply_status="interrupted",
        structural_reason="route_unconfigured",
    )
    failure = ModelHubError(
        "no_candidate",
        turn_outcome=produce_turn_outcome(
            "turn.no_candidate.unconfigured",
            config=config,
            resolution=resolution,
        ),
    )
    for locale in ("en", "zh"):
        rendered = _localized_launch_error(
            SimpleNamespace(config=SimpleNamespace(language=locale)),
            "claude",
            "menu-model",
            failure,
        )
        assert rendered.detail == i18n_t(
            "modelHub.launch.route_unconfigured",
            locale,
            model="menu-model",
        )


def test_structural_blocker_copy_uses_its_reason_instead_of_source_status() -> None:
    source = _source(
        "src_blocker01",
        "Exact source",
    )
    config = _config([source])
    config.agents["claude"].routes["shared-model"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "removed-model"),)
    )
    resolution = _terminal_resolution_facts(
        config,
        supply_status="interrupted",
        blocker_reason="model_unsupported",
    )
    failure = ModelHubError(
        "no_candidate",
        turn_outcome=produce_turn_outcome(
            "turn.no_candidate.blocked",
            config=config,
            resolution=resolution,
        ),
    )

    rendered = _localized_launch_error(
        SimpleNamespace(config=SimpleNamespace(language="en")),
        "claude",
        "menu-model",
        failure,
    )

    assert event_reason_label("model_unsupported", "en") in rendered.detail
    assert "standby" not in rendered.detail


class MemoryStore:
    def __init__(self, config: ModelHubConfig):
        self.config = config
        self.recovery_warning = False
        self.requested_models = {
            "claude": "shared-model",
            "codex": "shared-model",
            "opencode": "openai/shared-model",
        }

    def load(self) -> ModelHubConfig:
        if self.recovery_warning:
            return ModelHubConfig.from_payload(self.config.to_payload())
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        if self.recovery_warning:
            raise ValueError("Config was loaded with recovery warnings")
        self.config = config

    def requested_model(self, backend: str) -> str:
        return self.requested_models.get(backend, "")


class _EngineObservation:
    """The adapter-side half of `InvokeHandle` these fakes all answer the same way.

    Every fake below stands in for an engine that reports nothing about a body
    until the gateway reads it, which is the state the contract calls `None`.
    A fake that hands over a real observation sets `_observed` and thereby says
    so explicitly, instead of being the only one that remembered the member.
    """

    _observed = None

    @property
    def observed(self):
        return self._observed


class InvokeHandle(_EngineObservation):
    def __init__(self, outcome: RawCallOutcome):
        self._outcome = outcome

    @property
    def stream(self):
        return None

    @property
    def outcome_available(self) -> bool:
        return True

    async def close_stream(self) -> None:
        return None

    async def outcome(self) -> RawCallOutcome:
        return self._outcome


class LiveInvokeHandle(_EngineObservation):
    def __init__(
        self,
        outcome: RawCallOutcome,
        chunks: tuple[bytes, ...],
        observed: ProtocolSSEState | None = None,
    ):
        self._outcome = outcome
        self._observed = observed
        self._stream = self._iterate(chunks)

    @staticmethod
    async def _iterate(chunks: tuple[bytes, ...]):
        for chunk in chunks:
            yield chunk

    @property
    def stream(self):
        return self._stream

    @property
    def outcome_available(self) -> bool:
        return True

    async def close_stream(self) -> None:
        await self._stream.aclose()

    async def outcome(self) -> RawCallOutcome:
        return self._outcome


class BlockingLiveInvokeHandle(_EngineObservation):
    def __init__(self, outcome: RawCallOutcome):
        self._outcome = outcome
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._release = asyncio.Event()
        self._available = False
        self._stream = self._iterate()

    async def _iterate(self):
        self.started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self._available = True
        yield b"data: {}\n\n"

    @property
    def stream(self):
        return self._stream

    @property
    def outcome_available(self) -> bool:
        return self._available

    async def close_stream(self) -> None:
        await self._stream.aclose()

    def release(self) -> None:
        self._release.set()

    async def outcome(self) -> RawCallOutcome:
        return self._outcome


class MidStreamBlockingInvokeHandle(BlockingLiveInvokeHandle):
    async def _iterate(self):
        yield b"data: {\"type\":\"response.output_text.delta\"}\n\n"
        self.started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self._available = True
        yield b"data: [DONE]\n\n"


class BrokenUpstreamInvokeHandle(_EngineObservation):
    def __init__(self, outcome: RawCallOutcome):
        self._outcome = outcome
        self._stream = self._iterate()

    @staticmethod
    async def _iterate():
        if False:
            yield b""
        raise ConnectionResetError("upstream disconnected")

    @property
    def stream(self):
        return self._stream

    @property
    def outcome_available(self) -> bool:
        return True

    async def close_stream(self) -> None:
        await self._stream.aclose()

    async def outcome(self) -> RawCallOutcome:
        return self._outcome


@contextmanager
def _occupied_ledger_writer():
    """Hold the one thread ledger writes run on, so a queued write cannot start.

    That is the window where cancelling whoever awaits a write used to cancel the
    write itself, and it is only reachable while the writing thread is busy.
    """

    occupied = threading.Event()
    release = threading.Event()

    def occupy() -> None:
        occupied.set()
        release.wait(5)

    occupant = _ledger_executor().submit(occupy)
    assert occupied.wait(5)
    try:
        yield
    finally:
        release.set()
        occupant.result(timeout=5)


class FakeStreamResponse:
    def __init__(
        self,
        *,
        prepare_error: BaseException | None = None,
        write_error: BaseException | None = None,
        eof_error: BaseException | None = None,
        eof_reached: asyncio.Event | None = None,
    ) -> None:
        self.prepare_error = prepare_error
        self.write_error = write_error
        self.eof_error = eof_error
        self.eof_reached = eof_reached
        self.writes: list[bytes] = []
        self.eof_called = False

    async def prepare(self, _request) -> None:
        if self.prepare_error is not None:
            raise self.prepare_error
        return None

    async def write(self, _chunk: bytes) -> None:
        self.writes.append(_chunk)
        if self.write_error is not None:
            raise self.write_error
        return None

    async def write_eof(self) -> None:
        self.eof_called = True
        if self.eof_reached is not None:
            self.eof_reached.set()
        if self.eof_error is not None:
            raise self.eof_error
        return None


class DeferredLifecycleHandle(_EngineObservation):
    def __init__(self, outcome: RawCallOutcome):
        self._outcome = outcome
        self._available = False
        self._stream = self._iterate()
        self.close_calls = 0
        self.outcome_calls = 0

    async def _iterate(self):
        try:
            yield b"data: {}\n\n"
        finally:
            self._available = True

    @property
    def stream(self):
        return self._stream

    @property
    def outcome_available(self) -> bool:
        return self._available

    async def close_stream(self) -> None:
        if self.close_calls:
            return
        self.close_calls += 1
        await self._stream.aclose()

    async def outcome(self) -> RawCallOutcome:
        self.outcome_calls += 1
        if not self._available:
            await asyncio.Event().wait()
        return self._outcome


class ObservedUnsettledHandle(_EngineObservation):
    """A live handle whose prelude proved billing but not a terminal outcome."""

    def __init__(self, observed: ProtocolSSEState):
        self._observed = observed
        self._stream = self._iterate()
        self.close_calls = 0

    @staticmethod
    async def _iterate():
        if False:
            yield b""

    @property
    def stream(self):
        return self._stream

    @property
    def outcome_available(self) -> bool:
        return False

    async def close_stream(self) -> None:
        self.close_calls += 1
        await self._stream.aclose()

    async def outcome(self) -> RawCallOutcome:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class RepeatedCancellationHandle(_EngineObservation):
    def __init__(self, outcome: RawCallOutcome, blocked_phase: str):
        self._outcome = outcome
        self._blocked_phase = blocked_phase
        self.started = asyncio.Event()
        self.phase_started = asyncio.Event()
        self.release_phase = asyncio.Event()
        self.close_calls = 0
        self.outcome_calls = 0
        self._available = False
        self._stream = self._iterate()

    async def _iterate(self):
        yield b"data: [DONE]\n\n"
        self.started.set()
        await asyncio.Event().wait()

    @property
    def stream(self):
        return self._stream

    @property
    def outcome_available(self) -> bool:
        return self._available

    async def close_stream(self) -> None:
        self.close_calls += 1
        if self.close_calls > 1:
            return
        if self._blocked_phase == "resource_teardown":
            self.phase_started.set()
            await self.release_phase.wait()
        self._available = True
        await self._stream.aclose()

    async def outcome(self) -> RawCallOutcome:
        self.outcome_calls += 1
        if self._blocked_phase == "handle_outcome":
            self.phase_started.set()
            await self.release_phase.wait()
        return self._outcome


class NeverResolvingCloseHandle(_EngineObservation):
    def __init__(self, outcome: RawCallOutcome):
        self._outcome = outcome
        self.started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.closed = asyncio.Event()
        self._stream = self._iterate()

    async def _iterate(self):
        yield b"data: [DONE]\n\n"
        self.started.set()
        await asyncio.Event().wait()

    @property
    def stream(self):
        return self._stream

    @property
    def outcome_available(self) -> bool:
        return False

    async def close_stream(self) -> None:
        self.close_started.set()
        while not self.release_close.is_set():
            try:
                await self.release_close.wait()
            except asyncio.CancelledError:
                continue
        self.closed.set()

    async def outcome(self) -> RawCallOutcome:
        return self._outcome


def _prepared_gateway_request(
    gateway: ModelHubTurnGateway,
    *,
    turn_id: str,
    requested_model: str,
    source_id: str,
    stream: bool,
) -> SimpleNamespace:
    token = gateway.correlation.prepare_gateway_turn(
        backend="codex",
        token=gateway.correlation.credentials("codex", "/repo", turn_id),
        requested_model_id=requested_model,
        resolved_model_id="shared-model",
        source_id=source_id,
        via_mapping=False,
    )
    return SimpleNamespace(
        match_info={"backend": "codex", "endpoint": "responses"},
        headers={"Authorization": f"Bearer {token}"},
        json=AsyncMock(
            return_value={
                "model": "shared-model",
                "input": "ping",
                "stream": stream,
            }
        ),
    )


class ProbeAdapter:
    def __init__(
        self,
        outcomes: list[RawCallOutcome],
        live_handles: list[LiveInvokeHandle | BlockingLiveInvokeHandle] | None = None,
    ):
        self.outcomes = deque(outcomes)
        self.live_handles = deque(live_handles or [])
        self.invocations: list[tuple[str, str, str]] = []
        self.requests: list[ModelHubRequest] = []
        self.refreshable_credential_refs: set[str] = set()
        self.capability_queries: list[str] = []

    async def ensure_installed(self, *, force: bool = False) -> EngineStatus:
        return await self.status()

    async def sync_sources(self, _bindings) -> None:
        return None

    async def revoke_credential(self, _credential_ref: str) -> None:
        return None

    async def credential_supports_refresh(self, credential_ref: str) -> bool:
        self.capability_queries.append(credential_ref)
        return credential_ref in self.refreshable_credential_refs

    async def invoke(self, source_id, model_id, request, _stream, origin):
        self.invocations.append((source_id, model_id, origin))
        self.requests.append(request)
        if self.live_handles:
            return self.live_handles.popleft()
        return InvokeHandle(self.outcomes.popleft())

    async def status(self) -> EngineStatus:
        return EngineStatus(
            health=EngineHealth.OK,
            installed_version="test",
            verified=True,
            listen_host="127.0.0.1",
            listen_port=15220,
            last_check_iso=NOW.isoformat(),
        )


def _source(
    source_id: str,
    label: str,
    *,
    channel: str = "hub",
    status: str = "standby",
    retry_at: str | None = None,
    vendor: str = "openai",
    protocol: str = "openai_responses",
    model_id: str = "shared-model",
) -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind="subscription" if channel == "native_cli" else "api_key",
        vendor=vendor,
        display_name=label,
        protocol=protocol,
        supply_channel=channel,
        billing="monthly" if channel == "native_cli" else "metered",
        state=ModelHubSourceStateConfig(
            status=status,
            retry_at=retry_at,
            detail_key=("models.source.cooldown.rate_limited" if status == "cooldown" else None),
        ),
        models=[
            ModelHubModelConfig(
                id=model_id,
                provenance="discovered",
            )
        ],
        credential_ref=None if channel == "native_cli" else f"cred_{source_id}",
    )


def _config(sources: list[ModelHubSourceConfig]) -> ModelHubConfig:
    agents = {
        backend: ModelHubAgentSupplyConfig.default(backend, mode="hub") for backend in ("claude", "codex", "opencode")
    }
    for backend, agent in agents.items():
        eligible = [source for source in sources if ModelHubConfig.source_eligible_for_backend(source, backend)]
        agent.sources = ModelHubAgentSourcesConfig(
            order=[source.id for source in eligible],
        )
        requested_model = "openai/shared-model" if backend == "opencode" else "shared-model"
        agent.routes[requested_model] = ModelHubRouteConfig(
            hops=tuple(
                ModelHubRouteHopConfig(
                    source_id=source.id,
                    model_id=source.models[0].id,
                )
                for source in eligible
            )
        )
    agents["opencode"].models = [
        ModelHubBackendModelConfig(id="openai/shared-model")
    ]
    agents["opencode"].menu.checked = ["openai/shared-model"]
    return ModelHubConfig(sources=sources, agents=agents)


def _outcome(
    kind: RawOutcomeKind,
    *,
    status: int | None = None,
    code: str | None = None,
    message: str | None = None,
    source_id: str = "src_primary01",
    stream_started: bool = False,
    usage: ProtocolUsageReport | None = None,
) -> RawCallOutcome:
    return RawCallOutcome(
        kind=kind,
        http_status=status,
        error_code=code,
        redacted_message=message,
        stream_started=stream_started,
        model_id="shared-model",
        source_id=source_id,
        usage=usage,
    )


def _service(
    tmp_path: Path,
    *,
    sources: list[ModelHubSourceConfig],
    outcomes: list[RawCallOutcome] | None = None,
    live_handles: list[LiveInvokeHandle | BlockingLiveInvokeHandle] | None = None,
) -> ModelHubService:
    store = MemoryStore(_config(sources))
    return ModelHubService(
        store=store,
        adapter=ProbeAdapter(outcomes or [], live_handles),
        events=BoundedEventLog(tmp_path / "events.json"),
        provenance=BoundedProvenanceStore(tmp_path / "provenance.json"),
        usage=BoundedUsageLedger(tmp_path / "usage.json", now=lambda: NOW),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=lambda: NOW,
        requested_model_override=store.requested_model,
    )


def _canonicalize_fixed_test_routes(
    service: ModelHubService,
) -> dict[str, str]:
    selected = {}
    config = service.store.load()
    for backend in ("claude", "codex"):
        menu_ids = model_hub_fixed_menu_ids(backend)
        selected[backend] = menu_ids[0]
        shared_route = config.agents[backend].routes.get(
            "shared-model",
            ModelHubRouteConfig(),
        )
        config.agents[backend].routes = {
            model_id: (shared_route if model_id == selected[backend] else ModelHubRouteConfig())
            for model_id in menu_ids
        }
        service.store.requested_models[backend] = selected[backend]
    return selected


def _begin_hub_attempt(
    registry: TurnCorrelationRegistry,
    *,
    turn_id: str,
    scope: str = "/repo",
) -> str | None:
    token = registry.credentials("codex", scope, turn_id)
    exact_turn = registry.begin_gateway_request(
        backend="codex",
        token=token,
        requested_model_id="shared-model",
    )
    registry.begin_attempt(
        exact_turn,
        source_id="src_primary01",
        resolved_model_id="shared-model",
        channel="hub",
        via_mapping=False,
    )
    return exact_turn


def test_process_credentials_record_only_exact_turns(tmp_path: Path) -> None:
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)

    token = registry.credentials("codex", "/repo", "turn_exact")
    assert token == registry.credentials("codex", "/repo", "turn_exact")
    assert token != registry.credentials("codex", "/other", "turn_other")
    assert registry.authenticates("codex", token) is True
    assert registry.authenticates("claude", token) is False
    assert registry.credentials("claude", "session-a", "turn_a") != registry.credentials(
        "claude",
        "session-b",
        "turn_b",
    )

    exact_turn = registry.begin_gateway_request(
        backend="codex",
        token=token,
        requested_model_id="shared-model",
    )
    registry.begin_attempt(
        exact_turn,
        source_id="src_primary01",
        resolved_model_id="shared-model",
        channel="hub",
        via_mapping=False,
    )
    success = _outcome(RawOutcomeKind.SUCCESS)
    registry.finish_attempt(
        exact_turn,
        outcome=success,
        decision=classify_outcome(success),
    )
    registry.settle(
        "turn_exact",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_exact")
    assert record is not None
    assert record["outcome"] == "served"
    _assert_valid("turn-provenance.schema.json", record)


def test_stopped_settlement_cannot_erase_committed_served_history(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "stop-after-served.json")
    registry = TurnCorrelationRegistry(store)
    turn_id = "turn_stop_after_served"
    exact_turn = _begin_hub_attempt(registry, turn_id=turn_id)
    success = _outcome(RawOutcomeKind.SUCCESS)
    registry.finish_attempt(
        exact_turn,
        outcome=success,
        decision=classify_outcome(success),
    )
    registry.record_turn_outcome(turn_id, produce_turn_outcome("turn.served"))

    with caplog.at_level("INFO", logger="core.handlers.model_hub.provenance"):
        registry.settle(
            turn_id,
            settled_by=SETTLED_BY_STOPPED,
            ts=NOW.isoformat(),
        )

    record = store.get(turn_id)
    assert record is not None
    assert record["outcome"] == "served"
    assert record["served"]["source_id"] == "src_primary01"
    assert record["canceled_attempt"] is None
    assert "Ignored stopped settlement" in caplog.text
    _assert_valid("turn-provenance.schema.json", record)


def test_stopped_settlement_cannot_erase_committed_protocol_failure(
    tmp_path: Path,
) -> None:
    fixture = E64_SETTLEMENT_BOUNDARIES["stopped_after_terminal"]
    store = BoundedProvenanceStore(tmp_path / "stop-after-protocol.json")
    registry = TurnCorrelationRegistry(store)
    turn_id = "turn_stop_after_protocol"
    exact_turn = _begin_hub_attempt(registry, turn_id=turn_id)
    failure = _outcome(RawOutcomeKind.PROTOCOL_ERROR, stream_started=True)
    registry.finish_attempt(
        exact_turn,
        outcome=failure,
        decision=classify_outcome(failure),
    )

    registry.settle(
        turn_id,
        settled_by=SETTLED_BY_STOPPED,
        ts=NOW.isoformat(),
    )

    record = store.get(turn_id)
    assert record is not None
    assert record["outcome"] == fixture["expected_outcome"]
    assert record["terminal_error"]["reason"] == fixture["terminal_reason"]
    assert record["canceled_attempt"] is None
    _assert_valid("turn-provenance.schema.json", record)


def test_released_v5_permission_denied_records_degrade_at_read_boundary(
    tmp_path: Path,
) -> None:
    provenance_path = tmp_path / "provenance.json"
    events_path = tmp_path / "events.json"
    provenance_path.write_text(
        json.dumps(RELEASED_V5_PERMISSION_DENIED["provenance"]),
        encoding="utf-8",
    )
    events_path.write_text(
        json.dumps(RELEASED_V5_PERMISSION_DENIED["resolution_events"]),
        encoding="utf-8",
    )

    record = BoundedProvenanceStore(provenance_path).get("turn_01k9x6db")
    event = BoundedEventLog(events_path).list()[0]
    degraded_reason = RELEASED_V5_PERMISSION_DENIED["degraded_reason"]

    assert record is not None
    assert record["failed_attempts"][0]["reason"] == degraded_reason
    assert event["reason"] == degraded_reason
    _assert_valid("turn-provenance.schema.json", record)
    _assert_valid("resolution-event.schema.json", event)
    assert RELEASED_V5_PERMISSION_DENIED["provenance"][0]["failed_attempts"][0]["reason"] == "permission_denied"
    assert RELEASED_V5_PERMISSION_DENIED["resolution_events"][0]["reason"] == "permission_denied"


def test_retired_process_scope_revokes_token_and_fails_closed(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "retired-scope.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("codex", "/repo", "turn_evicted")
    launch = registry.prepare_gateway_turn(
        backend="codex",
        token=token,
        requested_model_id="shared-model",
        resolved_model_id="shared-model",
        source_id="src_primary01",
        via_mapping=False,
    )
    assert launch != token
    turn_id = registry.begin_gateway_request(
        backend="codex",
        token=token,
        requested_model_id="shared-model",
    )
    registry.begin_attempt(
        turn_id,
        source_id="src_primary01",
        resolved_model_id="shared-model",
        channel="hub",
        via_mapping=False,
    )

    registry.retire_scope("codex", "/repo")

    # Every credential the scope minted is revoked, not only the one that
    # names the process: a launch credential is as good as a token.
    assert registry.authenticates("codex", token) is False
    assert registry.authenticates("codex", launch) is False
    replacement = registry.credentials("codex", "/repo", "turn_replacement")
    assert replacement != token
    assert registry.authenticates("codex", replacement) is True
    registry.settle(
        "turn_evicted",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    assert store.get("turn_evicted") is None

    registry.settle(
        "turn_replacement",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    registry.retire_scope("codex", "/repo")
    assert registry._scopes == {}
    assert registry._credentials == {}


def test_terminal_failure_survives_exact_scope_retirement(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "terminal-retirement.json")
    registry = TurnCorrelationRegistry(store)
    registry.begin_native_attempt(
        backend="claude",
        process_scope="session:/repo",
        turn_id="turn_startup_failure",
        requested_model_id="claude-opus",
        source_id="src_native01",
        resolved_model_id="claude-opus",
        via_mapping=False,
    )
    registry.fail_native_attempt(
        "turn_startup_failure",
        reason="unclassified_error",
    )

    registry.retire_scope(
        "claude",
        "session:/repo",
        terminal_turn_id="turn_startup_failure",
    )
    registry.settle(
        "turn_startup_failure",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_startup_failure")
    assert record is not None
    assert record["outcome"] == "exhausted"
    assert record["failed_attempts"][0]["reason"] == "unclassified_error"
    _assert_valid("turn-provenance.schema.json", record)


def test_terminal_retirement_does_not_restore_ambiguous_scope(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "ambiguous-retirement.json")
    registry = TurnCorrelationRegistry(store)
    registry.begin_native_attempt(
        backend="claude",
        process_scope="session:/repo",
        turn_id="turn_first",
        requested_model_id="claude-opus",
        source_id="src_native01",
        resolved_model_id="claude-opus",
        via_mapping=False,
    )
    registry.begin_native_attempt(
        backend="claude",
        process_scope="session:/repo",
        turn_id="turn_second",
        requested_model_id="claude-opus",
        source_id="src_native01",
        resolved_model_id="claude-opus",
        via_mapping=False,
    )
    registry.fail_native_attempt("turn_first", reason="unclassified_error")

    registry.retire_scope(
        "claude",
        "session:/repo",
        terminal_turn_id="turn_first",
    )
    registry.settle(
        "turn_first",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    assert store.get("turn_first") is None


def _routing_registry(tmp_path: Path, label: str) -> tuple[TurnCorrelationRegistry, str]:
    store = BoundedProvenanceStore(tmp_path / f"routing-{label}.json")
    registry = TurnCorrelationRegistry(store)
    return registry, registry.credentials("claude", "session:/repo", "turn_route01")


def _prepare_route(
    registry: TurnCorrelationRegistry,
    token: str,
    *,
    turn_id: str,
    requested: str,
    resolved: str,
    source_id: str = "src_primary01",
) -> str:
    """Prepare a route and return the credential its launch must use.

    Mirrors `ModelHubTurnGateway.endpoint`: the scope credential goes in, the
    credential bound to this route comes back, and that is the one the
    launched process authenticates with.
    """

    return registry.prepare_gateway_turn(
        backend="claude",
        token=token,
        turn_id=turn_id,
        requested_model_id=requested,
        resolved_model_id=resolved,
        source_id=source_id,
        via_mapping=True,
    )


def _resolution(registry: TurnCorrelationRegistry, token: str, model: str) -> str | None:
    return registry.claim_gateway_request(
        backend="claude",
        token=token,
        prepared_turn_id=None,
        gateway_model_id=model,
    ).caller_model_id


@pytest.mark.parametrize(
    "arrives",
    [
        pytest.param("after_its_turn_settled", id="after_its_turn_settled"),
        pytest.param("while_another_model_is_live", id="while_another_model_is_live"),
    ],
)
def test_gateway_keeps_a_live_process_routable_outside_its_turn_windows(
    tmp_path: Path,
    arrives: str,
) -> None:
    """Routing is a property of the process scope, not of an open turn window.

    A launched CLI keeps issuing gateway requests between the turns Avibe
    dispatches — tool loops, agent-initiated continuations, transport retries.
    Those requests carry the upstream model id, which only the route the
    process was launched on can translate back into the caller model the
    resolver routes from, so routing tied to an open turn window makes every
    one of them unroutable.
    """

    registry, token = _routing_registry(tmp_path, arrives)
    route_token = _prepare_route(
        registry,
        token,
        turn_id="turn_route01",
        requested="caller-model",
        resolved="hub-model",
    )
    registry.settle(
        "turn_route01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    if arrives == "while_another_model_is_live":
        # A dispatched turn on a different model is the common case: the CLI
        # keeps finishing work for the previous one. Its route answers for the
        # model it prepared and must say nothing about any other.
        registry.credentials("claude", "session:/repo", "turn_route02")
        _prepare_route(
            registry,
            token,
            turn_id="turn_route02",
            requested="other-caller",
            resolved="other-hub-model",
        )

    assert _resolution(registry, route_token, "hub-model") == "caller-model"
    # Reach follows the route this launch was made on, so a model that route
    # does not name stays unroutable rather than becoming reachable by naming
    # it — including one another launch on the same process can reach.
    assert _resolution(registry, route_token, "never-routed") is None
    assert _resolution(registry, route_token, "other-hub-model") is None


@pytest.mark.parametrize(
    ("scenario", "rival_caller", "rival_source"),
    [
        pytest.param("two_caller_models", "rival-caller", "src_primary01", id="two_caller_models"),
        pytest.param("two_sources", "caller-model", "src_backup001", id="two_sources"),
    ],
)
def test_gateway_answers_each_route_that_aliases_one_upstream_model(
    tmp_path: Path,
    scenario: str,
    rival_caller: str,
    rival_source: str,
) -> None:
    """A request is routed by the credential it arrives on, not by its model.

    Two menu entries may resolve to one upstream model — deliberately, as two
    price tiers or two Sources for the same model — and one process scope
    launches both. The wire carries the upstream id alone, so a credential
    naming only the process cannot say which of them a request belongs to, and
    the routing table has to either guess or refuse. Binding the credential to
    the route removes the question: each launch answers for its own route, and
    that stays true once the turns that prepared them have settled.
    """

    registry, token = _routing_registry(tmp_path, scenario)
    registry.credentials("claude", "session:/repo", "turn_route02")
    first = _prepare_route(
        registry,
        token,
        turn_id="turn_route01",
        requested="caller-model",
        resolved="hub-model",
        source_id="src_primary01",
    )
    rival = _prepare_route(
        registry,
        token,
        turn_id="turn_route02",
        requested=rival_caller,
        resolved="hub-model",
        source_id=rival_source,
    )

    assert first != rival
    for turn_id in (None, "turn_route01", "turn_route02"):
        if turn_id is not None:
            registry.settle(
                turn_id,
                settled_by=SETTLED_BY_TERMINAL_RESULT,
                ts=NOW.isoformat(),
            )
        assert _resolution(registry, first, "hub-model") == "caller-model"
        assert _resolution(registry, rival, "hub-model") == rival_caller
        # The scope credential names the process and no route, so it answers
        # for neither rather than picking one.
        assert _resolution(registry, token, "hub-model") is None


def test_gateway_routing_dies_with_the_process_scope(tmp_path: Path) -> None:
    """A retired scope revokes its credentials, so its routes stop answering.

    Every credential the scope minted, not only the one that names the process
    itself: a launch credential outliving its scope would keep a dead
    runtime's routes answering on a token nothing can revoke.
    """

    registry, token = _routing_registry(tmp_path, "retired")
    route_token = _prepare_route(
        registry,
        token,
        turn_id="turn_route01",
        requested="caller-model",
        resolved="hub-model",
    )
    registry.settle(
        "turn_route01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    assert _resolution(registry, route_token, "hub-model") == "caller-model"

    registry.retire_scope("claude", "session:/repo")

    assert registry.authenticates("claude", token) is False
    assert registry.authenticates("claude", route_token) is False
    assert _resolution(registry, route_token, "hub-model") is None


@pytest.mark.parametrize(
    ("settled_resolved", "live_resolved"),
    [
        pytest.param("hub-settled", "hub-live", id="distinct_upstream_models"),
        pytest.param("hub-shared", "hub-shared", id="one_aliased_upstream_model"),
    ],
)
def test_gateway_attributes_a_request_only_to_a_turn_that_claims_its_model(
    tmp_path: Path,
    settled_resolved: str,
    live_resolved: str,
) -> None:
    """Serving a scope-routed request must not cost the live turn its record.

    Routing and attribution share one entry point, so an out-of-turn request is
    resolved while some unrelated turn is the only open window. Binding it there
    would tell that turn a model it never asked for arrived on its token, which
    marks it ambiguous — and an ambiguous trace makes `settle` drop the record
    for the request the turn goes on to make itself.

    A live turn's claim is its whole route, not the upstream model the route
    ends at, so the aliased case holds too: two menu entries resolving to one
    upstream model are two routes, and the live one owns only its own.
    """

    store = BoundedProvenanceStore(tmp_path / "routing-attribution.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("claude", "session:/repo", "turn_settled01")
    settled_token = _prepare_route(
        registry,
        token,
        turn_id="turn_settled01",
        requested="caller-settled",
        resolved=settled_resolved,
    )
    registry.settle(
        "turn_settled01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    assert registry.credentials("claude", "session:/repo", "turn_live01") == token
    live_token = _prepare_route(
        registry,
        token,
        turn_id="turn_live01",
        requested="caller-live",
        resolved=live_resolved,
    )

    with registry.gateway_terminalizer(
        backend="claude",
        token=settled_token,
    ) as out_of_turn:
        assert out_of_turn.resolution_model(settled_resolved) == "caller-settled"
        # Its own turn has already settled, so there is nothing to attribute to.
        assert out_of_turn.turn_id is None

    with registry.gateway_terminalizer(backend="claude", token=live_token) as live:
        assert live.resolution_model(live_resolved) == "caller-live"
        assert live.turn_id == "turn_live01"

    registry.settle(
        "turn_live01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    record = store.get("turn_live01")
    assert record is not None
    assert record["terminal_error"] == {
        "source_id": "src_primary01",
        "configured_model_id": live_resolved,
        "channel": "hub",
        "reason": "protocol_error",
        "stream_started": False,
    }


def test_a_canceled_turn_reports_the_attempt_it_waited_on_longest(
    tmp_path: Path,
) -> None:
    """A turn holding several requests reports the one in flight the longest.

    The settlement record has one attempt slot, but a live process issues
    concurrent requests on one turn and each carries its own identity — a
    request that fell back to a backup Source is not the same attempt as a peer
    still holding the primary. The slot has to name one of them, and the oldest
    entry is the only stable answer: entries are removed as their requests
    settle, so whatever is still first arrived before all the others and no
    later request can displace it.
    """

    store = BoundedProvenanceStore(tmp_path / "routing-oldest.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("claude", "session:/repo", "turn_live01")
    live_token = _prepare_route(
        registry,
        token,
        turn_id="turn_live01",
        requested="caller-live",
        resolved="hub-live",
    )

    with registry.gateway_terminalizer(backend="claude", token=live_token) as first:
        assert first.resolution_model("hub-live") == "caller-live"
        # This request fell back to a backup hop and is awaiting upstream.
        first.begin_attempt(
            source_id="src_backup001",
            resolved_model_id="hub-live",
            channel="hub",
            via_mapping=True,
        )
        with registry.gateway_terminalizer(
            backend="claude",
            token=live_token,
        ) as second:
            assert second.resolution_model("hub-live") == "caller-live"
            # Cancelled while both are open, before either has settled.
            registry.settle(
                "turn_live01",
                settled_by=SETTLED_BY_STOPPED,
                ts=NOW.isoformat(),
            )

    record = store.get("turn_live01")
    assert record is not None
    assert record["outcome"] == "canceled"
    assert record["canceled_attempt"] == {
        "source_id": "src_backup001",
        "configured_model_id": "hub-live",
        "channel": "hub",
    }


def test_an_unclaimed_request_leaves_no_attempt_on_the_live_turn(
    tmp_path: Path,
) -> None:
    """A turn that issued no gateway request must settle as having made none.

    The terminalizer opens the live turn's attempt on the way in, before any
    model id has been read — `fail` runs before routing and needs a turn to
    fail. So a request routed from scope history has to give that attempt back
    on its way out: left armed, it settles the live turn as having canceled or
    interrupted a Hub attempt that only ever belonged to the settled turn.
    """

    store = BoundedProvenanceStore(tmp_path / "routing-phantom.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("claude", "session:/repo", "turn_settled01")
    settled_token = _prepare_route(
        registry,
        token,
        turn_id="turn_settled01",
        requested="caller-settled",
        resolved="hub-settled",
    )
    registry.settle(
        "turn_settled01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    assert registry.credentials("claude", "session:/repo", "turn_live01") == token
    _prepare_route(
        registry,
        token,
        turn_id="turn_live01",
        requested="caller-live",
        resolved="hub-live",
    )

    with registry.gateway_terminalizer(
        backend="claude",
        token=settled_token,
    ) as out_of_turn:
        assert out_of_turn.resolution_model("hub-settled") == "caller-settled"

    registry.settle(
        "turn_live01",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    assert store.get("turn_live01") is None


def test_an_unclaimed_request_leaves_an_attempt_in_flight_untouched(
    tmp_path: Path,
) -> None:
    """A request records what it attempted, whoever else is on its turn.

    An attempt identity describes one request, and a live process issues
    concurrent ones on one turn. So a delayed request for a settled turn's
    route arrives while the live turn's own request is awaiting an upstream
    result, and it touches the turn's pending state twice: on the way in to
    arm a launch identity, and on the way out to give it back. Either move,
    aimed at the turn rather than at the request, erases the attempt actually
    in flight — and `finish_attempt` reconstructs nothing from an absent one,
    so the live turn settles having served a model with no record of which
    Source and model served it.

    Stated as the surviving provenance rather than as the two moves, so any
    later route to the same erasure fails here.
    """

    store = BoundedProvenanceStore(tmp_path / "routing-concurrent.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("claude", "session:/repo", "turn_settled01")
    settled_token = _prepare_route(
        registry,
        token,
        turn_id="turn_settled01",
        requested="caller-settled",
        resolved="hub-settled",
    )
    registry.settle(
        "turn_settled01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    assert registry.credentials("claude", "session:/repo", "turn_live01") == token
    live_token = _prepare_route(
        registry,
        token,
        turn_id="turn_live01",
        requested="caller-live",
        resolved="hub-live",
        source_id="src_live0001",
    )

    with registry.gateway_terminalizer(backend="claude", token=live_token) as live:
        assert live.resolution_model("hub-live") == "caller-live"
        live.begin_attempt(
            source_id="src_live0001",
            resolved_model_id="hub-live",
            channel="hub",
            via_mapping=True,
        )
        # The live request is now awaiting upstream. A delayed request for the
        # settled turn's route lands on the same process and same live turn.
        with registry.gateway_terminalizer(
            backend="claude",
            token=settled_token,
        ) as delayed:
            assert delayed.resolution_model("hub-settled") == "caller-settled"
        success = _outcome(RawOutcomeKind.SUCCESS, source_id="src_live0001")
        live.finish_attempt(outcome=success, decision=classify_outcome(success))

    registry.settle(
        "turn_live01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    record = store.get("turn_live01")
    assert record is not None
    assert record["served"] == {
        "source_id": "src_live0001",
        "configured_model_id": "hub-live",
        "channel": "hub",
    }


def test_a_request_gives_back_only_its_own_launch_identity(
    tmp_path: Path,
) -> None:
    """Giving back a launch identity must not take a real attempt with it.

    The reverse arrival order of the test above: a delayed request arms its
    launch identity first, and only then does the live request begin its own
    attempt. The delayed one is now stale, and it names the same Source and
    model as the live attempt whenever the live request went out on its
    primary hop — which is the ordinary case. So a give-back cannot be decided
    by what a launch identity looks like, only by whose it is; by value the two
    are indistinguishable, and the live turn loses the provenance of a request
    it really did make.
    """

    store = BoundedProvenanceStore(tmp_path / "routing-stale-prep.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("claude", "session:/repo", "turn_settled01")
    settled_token = _prepare_route(
        registry,
        token,
        turn_id="turn_settled01",
        requested="caller-settled",
        resolved="hub-settled",
    )
    registry.settle(
        "turn_settled01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    assert registry.credentials("claude", "session:/repo", "turn_live01") == token
    live_token = _prepare_route(
        registry,
        token,
        turn_id="turn_live01",
        requested="caller-live",
        resolved="hub-live",
        source_id="src_live0001",
    )

    # The delayed request arms its launch identity first.
    with registry.gateway_terminalizer(
        backend="claude",
        token=settled_token,
    ) as delayed:
        # Only then does the live request begin its own attempt, on the primary
        # hop — the same Source and model the identity above named.
        with registry.gateway_terminalizer(backend="claude", token=live_token) as live:
            assert live.resolution_model("hub-live") == "caller-live"
            live.begin_attempt(
                source_id="src_live0001",
                resolved_model_id="hub-live",
                channel="hub",
                via_mapping=True,
            )
            assert delayed.resolution_model("hub-settled") == "caller-settled"
            success = _outcome(RawOutcomeKind.SUCCESS, source_id="src_live0001")
            live.finish_attempt(outcome=success, decision=classify_outcome(success))

    registry.settle(
        "turn_live01",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    record = store.get("turn_live01")
    assert record is not None
    assert record["served"] == {
        "source_id": "src_live0001",
        "configured_model_id": "hub-live",
        "channel": "hub",
    }


def test_gateway_serves_a_request_that_arrives_after_its_turn_settled(
    tmp_path: Path,
) -> None:
    """The 409 this fixes, end to end: same request, before and after settlement.

    ``service.adapter.invocations`` is the evidence that the caller model was
    the routing input — only the route keyed by it carries this hop.
    """

    async def exercise() -> None:
        primary = _source(
            "src_primary01",
            "Primary",
            vendor="anthropic",
            protocol="anthropic",
            model_id="hub-model",
        )
        service = _service(
            tmp_path,
            sources=[primary],
            outcomes=[_outcome(RawOutcomeKind.SUCCESS, source_id=primary.id)],
        )
        service.store.config.agents["claude"].routes["caller-model"] = ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(primary.id, "hub-model"),)
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "claude",
            process_scope="session:/repo",
            turn_id="turn_between_windows",
            requested_model_id="caller-model",
            resolved_model_id="hub-model",
            source_id=primary.id,
        )
        gateway.correlation.settle(
            "turn_between_windows",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/messages",
                    json={
                        "model": "hub-model",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                        "stream": False,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 200
                await response.read()
        finally:
            await gateway.close()

        assert service.adapter.invocations == [(primary.id, "hub-model", "claude")]

    asyncio.run(exercise())


def test_gateway_models_endpoint_serves_authenticated_backend_catalog(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = _service(
            tmp_path,
            sources=[_source("src_primary01", "Primary")],
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_models_catalog",
            requested_model_id="shared-model",
            resolved_model_id="shared-model",
            source_id="src_primary01",
        )
        expected = [
            model["id"]
            for model in service.backend_catalog_models("codex")
            if model["routeable"] is True
        ]
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                unauthorized = await client.get(f"{base_url}/v1/models")
                assert unauthorized.status == 401
                response = await client.get(
                    f"{base_url}/v1/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 200
                payload = await response.json()
                assert [model["id"] for model in payload["data"]] == expected
                assert payload["has_more"] is False
                assert payload["first_id"] == expected[0]
                assert payload["last_id"] == expected[-1]
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_runtime_resolution_sees_controller_reconcile_after_remote_catalog_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from core.controller import Controller
    from vibe import backend_model_catalog

    async def exercise() -> None:
        model_id = "gpt-runtime-refresh"
        source = _source(
            "src_refresh001",
            "Refresh provider",
            model_id=model_id,
        )
        service = _service(tmp_path, sources=[source])
        for backend in ("claude", "codex"):
            fixed_agent = service.store.config.agents[backend]
            fixed_agent.routes = {
                model.id: ModelHubRouteConfig() for model in fixed_agent.models
            }
        agent = service.store.config.agents["codex"]
        snapshot = {
            "codex": {
                "generation": "stable-upstream-generation",
                "models": [{"id": model.id} for model in agent.models],
            }
        }
        monkeypatch.setattr(
            service,
            "_builtin_snapshots",
            lambda _backends: snapshot,
        )
        await service.reconcile_builtin_models(("codex",), notify=False)

        snapshot["codex"]["models"] = [
            *snapshot["codex"]["models"],
            {"id": model_id, "display_name": "Runtime refresh"},
        ]
        snapshot["codex"]["generation"] = "refreshed-upstream-generation"
        controller = Controller.__new__(Controller)
        controller.model_hub_service = service
        controller._loop = asyncio.get_running_loop()
        controller._model_hub_snapshot_refresh_pending = threading.Event()
        controller._model_hub_snapshot_reconcile_task = None
        monkeypatch.setattr(
            backend_model_catalog,
            "_REMOTE_REFRESH_COMPLETED",
            controller._model_hub_snapshot_refresh_completed,
        )
        monkeypatch.setattr(
            backend_model_catalog,
            "refresh_remote_catalog_now",
            lambda _url: {},
        )

        backend_model_catalog._refresh_remote_catalog_worker()
        await asyncio.sleep(0)
        reconcile_task = controller._model_hub_snapshot_reconcile_task
        if reconcile_task is not None:
            await reconcile_task

        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            launch = await router.resolve(
                "codex",
                model_id,
                process_scope="/repo",
                turn_id="turn_after_catalog_refresh",
            )
        finally:
            await gateway.close()

        assert launch.requested_model == model_id
        assert launch.target_model == model_id
        assert launch.source_id == source.id
        assert model_id in {
            model.id for model in service.store.config.agents["codex"].models
        }

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("path", "body", "status", "reason"),
    [
        ("responses", "{", 400, "invalid_parameter"),
        ("responses", "[]", 400, "invalid_parameter"),
        ("responses", '{"stream":false}', 400, "invalid_parameter"),
        ("unsupported", '{"model":"shared-model"}', 404, "protocol_error"),
    ],
)
def test_authenticated_gateway_validation_failure_is_correlated(
    tmp_path: Path,
    path: str,
    body: str,
    status: int,
    reason: str,
) -> None:
    async def exercise() -> None:
        service = _service(
            tmp_path,
            sources=[_source("src_primary01", "Primary")],
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_invalid_gateway_request",
            requested_model_id="shared-model",
            resolved_model_id="shared-model",
            source_id="src_primary01",
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/{path}",
                    data=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                assert response.status == status
                await response.read()
        finally:
            await gateway.close()

        gateway.correlation.settle(
            "turn_invalid_gateway_request",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_invalid_gateway_request")
        assert record is not None
        assert record["outcome"] == "failed_terminal"
        assert record["terminal_error"] == {
            "source_id": "src_primary01",
            "configured_model_id": "shared-model",
            "channel": "hub",
            "reason": reason,
            "stream_started": False,
        }
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_opencode_gateway_round_trips_aliased_tool_names(tmp_path: Path) -> None:
    async def exercise() -> None:
        handle = LiveInvokeHandle(
            _outcome(RawOutcomeKind.SUCCESS, stream_started=True),
            (
                b'data: {"choices":[{"delta":{"tool_calls":[{"function":'
                b'{"name":"avibe_todo_write","arguments":"{}"}}]}}]}\n\n',
                b"data: [DONE]\n\n",
            ),
        )
        service = _service(
            tmp_path,
            sources=[
                _source(
                    "src_primary01",
                    "Anthropic relay",
                    vendor="custom",
                    protocol="anthropic",
                )
            ],
            live_handles=[handle],
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "opencode",
            process_scope="opencode:shared-server",
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/chat/completions",
                    json={
                        "model": "openai/shared-model",
                        "messages": [{"role": "user", "content": "update the list"}],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "todowrite",
                                    "parameters": {"type": "object"},
                                },
                            }
                        ],
                        "stream": True,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 200
                body = await response.read()
        finally:
            await gateway.close()

        adapter = service.adapter
        assert isinstance(adapter, ProbeAdapter)
        forwarded = adapter.requests[0]
        assert forwarded["tools"][0]["function"]["name"] == "avibe_todo_write"
        assert b'"name":"todowrite"' in body
        assert b'"name":"avibe_todo_write"' not in body

    asyncio.run(exercise())


def test_gateway_terminalizer_records_pre_observer_engine_down_before_return(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        service = _service(
            tmp_path,
            sources=[_source("src_primary01", "Primary")],
        )

        async def fail_sync(_bindings) -> None:
            raise RuntimeError("engine unavailable")

        service.adapter.sync_sources = fail_sync
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_pre_observer_engine_down",
            requested_model_id="shared-model",
            resolved_model_id="shared-model",
            source_id="src_primary01",
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={
                        "model": "shared-model",
                        "input": "ping",
                        "stream": False,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 503
                payload = await response.json()
                assert payload["error"]["message"] == i18n_t(
                    "modelHub.errors.engine_down",
                    "en",
                )

            trace = gateway.correlation._traces["turn_pre_observer_engine_down"]
            assert trace.pending_attempt is None
            assert trace.terminal_error == {
                "source_id": None,
                "configured_model_id": None,
                "channel": None,
                "reason": "engine_down",
                "stream_started": False,
            }
        finally:
            await gateway.close()

        gateway.correlation.settle(
            "turn_pre_observer_engine_down",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_pre_observer_engine_down")
        assert record is not None
        assert record["outcome"] == "failed_terminal"
        assert record["terminal_error"] == {
            "source_id": None,
            "configured_model_id": None,
            "channel": None,
            "reason": "engine_down",
            "stream_started": False,
        }
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


@pytest.mark.parametrize("entry", ["process", "gateway"])
def test_no_candidate_provenance_carries_exact_hop_blockers_from_each_entry(
    tmp_path: Path,
    entry: str,
) -> None:
    turn_id = f"turn_blocked_{entry}"
    store = BoundedProvenanceStore(tmp_path / f"blocked-{entry}.json")
    registry = TurnCorrelationRegistry(store)
    blockers = (
        ExactHopBlocker(
            source_id="src_primary01",
            model_id="shared-model",
            reason="model_unsupported",
        ),
        ExactHopBlocker(
            source_id="src_backup001",
            model_id="shared-model",
            reason="credential_revoked",
        ),
    )
    if entry == "process":
        registry.mark_no_candidate(
            backend="claude",
            process_scope="/repo",
            turn_id=turn_id,
            requested_model_id="shared-model",
            supply_state="interrupted",
            blockers=blockers,
        )
    else:
        token = registry.prepare_gateway_turn(
            backend="claude",
            token=registry.credentials("claude", "/repo", turn_id),
            requested_model_id="shared-model",
            resolved_model_id="shared-model",
            source_id="src_primary01",
            via_mapping=False,
        )
        with registry.gateway_terminalizer(
            backend="claude",
            token=token,
        ) as terminalizer:
            terminalizer.mark_no_candidate("interrupted", blockers)

    registry.settle(
        turn_id,
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get(turn_id)
    assert record is not None
    assert record["outcome"] == "no_candidate"
    assert record["blockers"] == [
        {
            "source_id": "src_primary01",
            "model_id": "shared-model",
            "reason": "model_unsupported",
        },
        {
            "source_id": "src_backup001",
            "model_id": "shared-model",
            "reason": "credential_revoked",
        },
    ]
    _assert_valid("turn-provenance.schema.json", record)


def test_runtime_no_candidate_projects_live_exact_hop_blockers(
    tmp_path: Path,
) -> None:
    source = _source("src_blocked01", "Blocked")
    source.state = ModelHubSourceStateConfig(
        status="needs_action",
        detail_key="models.source.needs_action.credential_revoked",
    )
    service = _service(tmp_path, sources=[source])
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            router.resolve(
                "claude",
                "shared-model",
                process_scope="/repo",
                turn_id="turn_runtime_blocked",
            )
        )

    assert exc_info.value.supply_state == "interrupted"
    router.settle_turn(
        "turn_runtime_blocked",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    record = service.provenance.get("turn_runtime_blocked")
    assert record is not None
    assert record["outcome"] == "no_candidate"
    assert record["blockers"] == [
        {
            "source_id": source.id,
            "model_id": "shared-model",
            "reason": "credential_revoked",
        }
    ]
    _assert_valid("turn-provenance.schema.json", record)


def test_runtime_launch_carries_claude_catalog_capabilities(tmp_path: Path) -> None:
    source = _source("src_catalogcap", "Catalog capability")
    service = _service(tmp_path, sources=[source])
    agent = service.store.config.agents["claude"]
    agent.models = [
        ModelHubBackendModelConfig(
            id="shared-model",
            context_window=128_000,
            max_output_tokens=32_000,
            supports_tools=True,
            supports_reasoning=True,
            reasoning_efforts=["low", "high"],
        )
    ]
    agent.routes = {
        "shared-model": ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(source.id, "shared-model"),)
        )
    }
    gateway = SimpleNamespace(
        endpoint=AsyncMock(
            return_value=("http://127.0.0.1:19000/claude", "gateway-token")
        )
    )
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)

    launch = asyncio.run(router.resolve("claude", "shared-model"))

    assert launch.context_window == 128_000
    assert launch.max_output_tokens == 32_000
    assert launch.supports_tools is True
    assert launch.supports_reasoning is True
    assert launch.reasoning_efforts == ("low", "high")


def test_native_cli_launch_carries_claude_catalog_capabilities(tmp_path: Path) -> None:
    source = _source(
        "src_nativecatalog",
        "Native catalog capability",
        channel="native_cli",
        vendor="anthropic",
        protocol="anthropic",
    )
    service = _service(tmp_path, sources=[source])
    agent = service.store.config.agents["claude"]
    agent.models = [
        ModelHubBackendModelConfig(
            id="shared-model",
            context_window=128_000,
            max_output_tokens=32_000,
            supports_tools=True,
            supports_reasoning=True,
            reasoning_efforts=["low", "max"],
        )
    ]
    agent.routes = {
        "shared-model": ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(source.id, "shared-model"),)
        )
    }
    router = ModelHubRuntimeRouter(
        service=service,
        native_cli_ready=lambda _backend: True,
    )

    launch = asyncio.run(router.resolve("claude", "shared-model"))

    assert launch.channel == "native_cli"
    assert launch.context_window == 128_000
    assert launch.max_output_tokens == 32_000
    assert launch.supports_tools is True
    assert launch.supports_reasoning is True
    assert launch.reasoning_efforts == ("low", "max")


def test_runtime_launch_suppresses_efforts_when_reasoning_is_disabled(
    tmp_path: Path,
) -> None:
    source = _source("src_noreasoning", "No reasoning")
    service = _service(tmp_path, sources=[source])
    agent = service.store.config.agents["claude"]
    agent.models = [
        ModelHubBackendModelConfig(
            id="shared-model",
            supports_reasoning=False,
            reasoning_efforts=["low", "high"],
        )
    ]
    agent.routes = {
        "shared-model": ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(source.id, "shared-model"),)
        )
    }
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(
            endpoint=AsyncMock(
                return_value=("http://127.0.0.1:19000/claude", "gateway-token")
            )
        ),
    )

    launch = asyncio.run(router.resolve("claude", "shared-model"))

    assert launch.supports_reasoning is False
    assert launch.reasoning_efforts == ()


def test_runtime_no_candidate_reinspects_the_full_chain_for_terminal_facts(
    tmp_path: Path,
) -> None:
    hub = _source(
        "src_precheckhub",
        "Hub cooling",
        status="cooldown",
        retry_at=(NOW + timedelta(hours=1)).isoformat(),
    )
    native = _source("src_prechecknative", "Native ready", channel="native_cli")
    service = _service(tmp_path, sources=[hub, native])
    requested_model = _canonicalize_fixed_test_routes(service)["codex"]
    service.store.config.agents["codex"].routes[requested_model] = ModelHubRouteConfig(
        hops=(
            ModelHubRouteHopConfig(hub.id, "shared-model"),
            ModelHubRouteHopConfig(native.id, "shared-model"),
        )
    )

    with pytest.raises(ModelHubError) as raised:
        asyncio.run(
            service.resolve(
                backend="codex",
                model_id=requested_model,
                request=ModelHubRequest(
                    {"model": requested_model, "input": "ping"},
                    protocol="openai_responses",
                ),
                supply_channel="hub",
            )
        )

    projection = raised.value.turn_outcome
    assert projection is not None
    assert projection.supply_facts is not None
    assert projection.supply_facts.supply_state == "waiting"
    assert raised.value.blockers == exact_hop_blockers(
        service._inspect_terminal_chain(backend="codex", model_id=requested_model)[1]
    )


def test_gateway_no_candidate_projects_live_exact_hop_blockers(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_blocked02", "Blocked")
        source.state = ModelHubSourceStateConfig(
            status="needs_action",
            detail_key="models.source.needs_action.credential_revoked",
        )
        service = _service(tmp_path, sources=[source])
        service.store.config.agents["codex"].routes["shared-model"] = ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(source.id, "removed-model"),)
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_gateway_blocked",
            requested_model_id="shared-model",
            resolved_model_id="removed-model",
            source_id=source.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={"model": "removed-model", "input": "ping", "stream": False},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 409
                await response.read()
        finally:
            await gateway.close()

        gateway.correlation.settle(
            "turn_gateway_blocked",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_gateway_blocked")
        assert record is not None
        assert record["outcome"] == "no_candidate"
        assert record["blockers"] == [
            {
                "source_id": source.id,
                "model_id": "removed-model",
                "reason": "model_unsupported",
            }
        ]
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_gateway_preserves_exhausted_provenance_after_all_hops_fallback(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first = _source("src_primary01", "Primary")
        second = _source("src_backup001", "Backup")
        service = _service(
            tmp_path,
            sources=[first, second],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    source_id=first.id,
                ),
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=503,
                    source_id=second.id,
                ),
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        service.store.config.agents["codex"].routes[requested_model] = ModelHubRouteConfig(
            hops=(
                ModelHubRouteHopConfig(first.id, "shared-model"),
                ModelHubRouteHopConfig(second.id, "shared-model"),
            )
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_gateway_exhausted",
            requested_model_id=requested_model,
            resolved_model_id="shared-model",
            source_id=first.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={"model": "shared-model", "input": "ping", "stream": False},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 503
                payload = await response.json()
                assert payload["error"]["code"] == "mapping_target_unavailable"
                cooled = service.store.load().sources
                assert payload["error"]["message"] == i18n_t(
                    "modelHub.launch.waiting",
                    "en",
                    model=requested_model,
                    source=", ".join(source.display_name for source in cooled),
                    retry_at=min(source.state.retry_at or "" for source in cooled),
                )
        finally:
            await gateway.close()

        gateway.correlation.settle(
            "turn_gateway_exhausted",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_gateway_exhausted")
        assert record is not None
        assert record["outcome"] == "exhausted"
        assert record["terminal_error"] is None
        assert [attempt["source_id"] for attempt in record["failed_attempts"]] == [
            first.id,
            second.id,
        ]
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_gateway_exhaustion_uses_no_time_copy_when_an_earlier_hop_recovers(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first = _source("src_recovery01", "Recovered while waiting")
        second = _source("src_recovery02", "Still cooling")
        service = _service(
            tmp_path,
            sources=[first, second],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    source_id=first.id,
                ),
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=503,
                    source_id=second.id,
                ),
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        service.store.config.agents["codex"].routes[requested_model] = (
            ModelHubRouteConfig(
                hops=(
                    ModelHubRouteHopConfig(first.id, "shared-model"),
                    ModelHubRouteHopConfig(second.id, "shared-model"),
                )
            )
        )
        clock = {"now": NOW}
        service.now = lambda: clock["now"]
        invoke = service.adapter.invoke

        async def advance_during_later_attempt(
            source_id,
            model_id,
            request,
            stream,
            origin,
        ):
            if source_id == second.id:
                clock["now"] = NOW + timedelta(minutes=10)
            return await invoke(source_id, model_id, request, stream, origin)

        service.adapter.invoke = advance_during_later_attempt
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_exhausted_after_recovery",
            requested_model_id=requested_model,
            resolved_model_id="shared-model",
            source_id=first.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={"model": "shared-model", "input": "ping", "stream": False},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 503
                payload = await response.json()
                assert payload["error"]["message"] == i18n_t(
                    "modelHub.launch.waiting_without_retry",
                    "en",
                    model=requested_model,
                )
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_gateway_streamed_fallback_settles_source_before_rendering_next_current(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first = _source("src_primary01", "Primary")
        second = _source("src_backup001", "Backup")
        service = _service(
            tmp_path,
            sources=[first, second],
            outcomes=[
                _outcome(
                    RawOutcomeKind.TIMEOUT,
                    status=200,
                    source_id=first.id,
                    stream_started=True,
                ),
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        service.store.config.agents["codex"].routes[requested_model] = (
            ModelHubRouteConfig(
                hops=(
                    ModelHubRouteHopConfig(first.id, "shared-model"),
                    ModelHubRouteHopConfig(second.id, "shared-model"),
                )
            )
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_gateway_streamed_fallback",
            requested_model_id=requested_model,
            resolved_model_id="shared-model",
            source_id=first.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={"model": "shared-model", "input": "ping", "stream": False},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 502
                payload = await response.json()
                assert payload["error"] == {
                    "type": "stream_interrupted",
                    "code": "stream_interrupted",
                    "message": i18n_t("modelHub.errors.stream_interrupted", "en"),
                }
        finally:
            await gateway.close()

        assert service.adapter.invocations == [
            (first.id, "shared-model", "codex")
        ]
        persisted = {source.id: source for source in service.store.load().sources}
        assert persisted[first.id].state.status == "standby"
        assert persisted[second.id].state.status == "standby"
        assert service.agent_chain("codex", requested_model)["current"] == {
            "source_id": first.id,
            "model_id": "shared-model",
        }

        gateway.correlation.settle(
            "turn_gateway_streamed_fallback",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_gateway_streamed_fallback")
        assert record is not None
        assert record["outcome"] == "failed_terminal"
        assert record["terminal_error"] == {
            "source_id": first.id,
            "configured_model_id": "shared-model",
            "channel": "hub",
            "reason": "stream_interrupted",
            "stream_started": True,
        }
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_gateway_live_settlement_emits_matrix_copy_before_eof(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_streamcopy1", "Stream copy")
        backup = _source("src_streamcopy2", "Stream copy backup")
        service = _service(
            tmp_path,
            sources=[source, backup],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.TIMEOUT,
                        status=200,
                        source_id=source.id,
                        stream_started=True,
                    ),
                    (
                        b'data: {"type":"response.output_text.delta",'
                        b'"sequence_number":7}\n\n',
                        b"data: partial",
                    ),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        response = FakeStreamResponse()
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_stream_copy",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=response,
        ):
            result = await gateway._handle_request(request)
        assert result is response
        assert response.writes[:2] == [
            b'data: {"type":"response.output_text.delta",'
            b'"sequence_number":7}\n\n',
            b"data: partial",
        ]
        terminal = response.writes[-1]
        assert terminal.startswith(b"\ndata: {}\n\nevent: error\ndata: ")
        payload = json.loads(
            terminal.removeprefix(b"\ndata: {}\n\nevent: error\ndata: ")
        )
        assert payload["sequence_number"] == 8
        assert payload["code"] == "modelHub.errors.stream_interrupted"
        assert response.eof_called

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "case",
    TERMINAL_SETTLEMENT_BOUNDARIES,
    ids=lambda case: f"{case['forwarded_terminal']}-{case['settlement']}",
)
def test_gateway_emits_at_most_one_wire_terminal(case: dict[str, object]) -> None:
    async def exercise() -> None:
        gateway = ModelHubTurnGateway(SimpleNamespace())  # type: ignore[arg-type]
        response = FakeStreamResponse()
        outcomes = {
            "none": None,
            "silent": _RenderedTurnOutcome(None, None),
            "copy": _RenderedTurnOutcome(
                "modelHub.errors.engine_down",
                i18n_t("modelHub.errors.engine_down", "en"),
            ),
        }
        forwarded = case["forwarded_terminal"]
        await gateway._write_stream_terminal_copy(
            response,  # type: ignore[arg-type]
            "openai_responses",
            outcomes[str(case["settlement"])],
            ProtocolSSEState("openai_responses"),
            forwarded_terminal=cast(str | None, forwarded),  # type: ignore[arg-type]
        )
        assert bool(response.writes) is case["write_terminal"]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("stream_started", "expected_key", "expected_next_current"),
    [
        (True, "modelHub.launch.retry", True),
        (False, "modelHub.launch.waiting_without_retry", False),
    ],
)
def test_terminal_reinspection_uses_every_channel_in_a_mixed_chain(
    tmp_path: Path,
    stream_started: bool,
    expected_key: str,
    expected_next_current: bool,
) -> None:
    async def exercise() -> None:
        hub = _source("src_mixedhub1", "Hub source")
        native = _source(
            "src_mixednative1",
            "Native source",
            channel="native_cli",
        )
        service = _service(
            tmp_path,
            sources=[hub, native],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    code="rate_limit_error",
                    source_id=hub.id,
                    stream_started=stream_started,
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        service.store.config.agents["codex"].routes[requested_model] = (
            ModelHubRouteConfig(
                hops=(
                    ModelHubRouteHopConfig(hub.id, "shared-model"),
                    ModelHubRouteHopConfig(native.id, "shared-model"),
                )
            )
        )

        with pytest.raises(ModelHubError) as raised:
            await service.resolve(
                backend="codex",
                model_id=requested_model,
                request=ModelHubRequest(
                    {"model": requested_model, "input": "ping"},
                    protocol="openai_responses",
                ),
                supply_channel="hub",
            )

        projection = raised.value.turn_outcome
        assert projection is not None
        assert projection.next_current_changed is expected_next_current
        assert project_turn_outcome_copy(projection).key == expected_key
        assert service.adapter.invocations == [
            (hub.id, "shared-model", "codex")
        ]
        assert service.agent_chain("codex", requested_model)["current"] == {
            "source_id": native.id,
            "model_id": "shared-model",
        }

    asyncio.run(exercise())


def test_preoutput_network_fallback_does_not_mutate_source_health(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first = _source("src_network001", "Network source")
        second = _source("src_network002", "Backup source")
        service = _service(
            tmp_path,
            sources=[first, second],
            outcomes=[
                _outcome(
                    RawOutcomeKind.NETWORK_ERROR,
                    source_id=first.id,
                ),
                _outcome(RawOutcomeKind.SUCCESS, source_id=second.id),
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]

        resolved = await service.resolve(
            backend="codex",
            model_id=requested_model,
            request=ModelHubRequest(
                {"model": requested_model, "input": "ping"},
                protocol="openai_responses",
            ),
            supply_channel="hub",
        )

        assert resolved.source_id == second.id
        assert [source.state.status for source in service.store.load().sources] == [
            "standby",
            "standby",
        ]
        assert service.adapter.invocations == [
            (first.id, "shared-model", "codex"),
            (second.id, "shared-model", "codex"),
        ]

    asyncio.run(exercise())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("kind", "status", "code", "expected_outcome", "expected_source_status"),
    [
        (RawOutcomeKind.SUCCESS, 200, None, "served", "standby"),
        (
            RawOutcomeKind.PROTOCOL_ERROR,
            502,
            "upstream_protocol_error",
            "failed_terminal",
            "standby",
        ),
        (
            RawOutcomeKind.HTTP_ERROR,
            200,
            "permission_error",
            "failed_terminal",
            "standby",
        ),
        (RawOutcomeKind.TIMEOUT, 200, None, "failed_terminal", "standby"),
    ],
)
def test_gateway_handle_terminal_matrix_always_uses_service_settlement(
    tmp_path: Path,
    stream: bool,
    kind: RawOutcomeKind,
    status: int,
    code: str | None,
    expected_outcome: str,
    expected_source_status: str,
) -> None:
    async def exercise() -> None:
        source = _source("src_livehandle", "Live handle")
        outcome = _outcome(
            kind,
            status=status,
            code=code,
            source_id=source.id,
            stream_started=True,
        )
        chunks = (
            (b"data: {}\n\n",)
            if stream
            else (b'{"id":"response-live-handle"}',)
        )
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[LiveInvokeHandle(outcome, chunks)],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        service.settle_handle_outcome = AsyncMock(
            wraps=service.settle_handle_outcome
        )
        turn_id = f"turn_live_{kind.value}_{stream}"
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id=turn_id,
            requested_model_id=requested_model,
            resolved_model_id="shared-model",
            source_id=source.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={
                        "model": "shared-model",
                        "input": "ping",
                        "stream": stream,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                payload = await response.read()
                if stream or kind is RawOutcomeKind.SUCCESS:
                    assert response.status == 200
                elif code == "permission_error":
                    assert response.status == 403
                else:
                    assert response.status == 502
                if not stream and kind is RawOutcomeKind.PROTOCOL_ERROR:
                    assert json.loads(payload)["error"]["message"] == i18n_t(
                        "modelHub.errors.upstream_protocol_error",
                        "en",
                    )
                    assert b"request_incompatible" not in payload
                if stream and kind is RawOutcomeKind.PROTOCOL_ERROR:
                    assert b"event: error" in payload
                    assert b"upstream_protocol_error" in payload
        finally:
            await gateway.close()

        service.settle_handle_outcome.assert_awaited_once()
        assert (
            service.settle_handle_outcome.await_args.kwargs["termination_origin"]
            == "upstream_terminal"
        )
        assert service.store.load().sources[0].state.status == expected_source_status
        gateway.correlation.settle(
            turn_id,
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get(turn_id)
        assert record is not None
        assert record["outcome"] == expected_outcome
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_handle_settlement_requires_an_explicit_termination_origin(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_origin01", "Explicit origin")
        service = _service(tmp_path, sources=[source])
        outcome = _outcome(
            RawOutcomeKind.NETWORK_ERROR,
            source_id=source.id,
        )
        resolved = ResolvedInvocation(
            backend="codex",
            requested_model_id="shared-model",
            source_id=source.id,
            source_label=source.display_name,
            model_id="shared-model",
            handle=None,
            outcome=None,
        )

        with pytest.raises(TypeError, match="termination_origin"):
            await service.settle_handle_outcome(resolved, outcome)  # type: ignore[call-arg]

        record_attempt = Mock()
        settlement = await service.settle_handle_outcome(
            resolved,
            None,
            termination_origin="downstream_cancel",
            record_attempt=record_attempt,
        )
        assert settlement.outcome is None
        assert settlement.decision is None
        assert settlement.turn_outcome == produce_turn_outcome("turn.canceled")
        assert render_turn_outcome_copy(settlement.turn_outcome, "en") is None
        assert service.store.load().sources[0].state.status == "standby"
        record_attempt.assert_not_called()

    asyncio.run(exercise())


def test_handle_settlement_records_history_before_source_transition_event(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_ordered1", "Ordered settlement")
        service = _service(tmp_path, sources=[source])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        settlement_generation = service._reserve_settlement_generation(source.id)
        resolved = ResolvedInvocation(
            backend="codex",
            requested_model_id=requested_model,
            source_id=source.id,
            source_label=source.display_name,
            model_id="shared-model",
            handle=None,
            outcome=None,
            settlement_generation=settlement_generation,
        )
        outcome = _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=429,
            code="rate_limit_error",
            source_id=source.id,
            stream_started=True,
        )
        order: list[str] = []
        record_event = service._record_event
        settle_source = service._settle_fallback_source

        def ordered_event(**kwargs) -> None:
            order.append("event")
            record_event(**kwargs)

        async def ordered_source_mutation(*args, **kwargs):
            order.append("source_mutation")
            return await settle_source(*args, **kwargs)

        service._record_event = Mock(side_effect=ordered_event)
        service._settle_fallback_source = AsyncMock(
            side_effect=ordered_source_mutation
        )

        await service.settle_handle_outcome(
            resolved,
            outcome,
            termination_origin="upstream_terminal",
            record_attempt=lambda _outcome, _decision: order.append("provenance"),
        )

        assert order == ["provenance", "source_mutation", "event"]

    asyncio.run(exercise())


def test_engine_down_settlement_does_not_mutate_source(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_enginedown1", "Engine unavailable")
        service = _service(tmp_path, sources=[source])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        resolved = ResolvedInvocation(
            backend="codex",
            requested_model_id=requested_model,
            source_id=source.id,
            source_label=source.display_name,
            model_id="shared-model",
            handle=None,
            outcome=None,
        )
        outcome = _outcome(
            RawOutcomeKind.NETWORK_ERROR,
            status=200,
            code="engine_down",
            message="loopback transport failed",
            source_id=source.id,
            stream_started=True,
        )
        record_attempt = Mock()

        settlement = await service.settle_handle_outcome(
            resolved,
            outcome,
            termination_origin="upstream_terminal",
            record_attempt=record_attempt,
        )

        assert settlement.decision is not None
        assert settlement.decision.error_code == "engine_down"
        assert settlement.decision.reason is None
        assert settlement.turn_outcome == produce_turn_outcome(
            "turn.engine_down",
            stream_started=True,
        )
        assert service.store.load().sources[0].state.status == "standby"
        assert service.events.list() == []
        record_attempt.assert_called_once()

    asyncio.run(exercise())


def test_bare_stream_network_settlement_keeps_source_health_unchanged(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_barenetwork1", "Incomplete stream")
        service = _service(tmp_path, sources=[source])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        resolved = ResolvedInvocation(
            backend="codex",
            requested_model_id=requested_model,
            source_id=source.id,
            source_label=source.display_name,
            model_id="shared-model",
            handle=None,
            outcome=None,
        )
        outcome = _outcome(
            RawOutcomeKind.NETWORK_ERROR,
            status=200,
            source_id=source.id,
            stream_started=True,
        )
        record_attempt = Mock()

        settlement = await service.settle_handle_outcome(
            resolved,
            outcome,
            termination_origin="upstream_terminal",
            record_attempt=record_attempt,
        )

        assert settlement.decision is not None
        assert settlement.decision.reason == "network"
        assert service.store.load().sources[0].state.status == "standby"
        assert settlement.turn_outcome is None
        assert service.events.list() == []
        record_attempt.assert_called_once()

    asyncio.run(exercise())


@pytest.mark.parametrize("refreshable", (True, False))
def test_streamed_auth_settlement_uses_exact_credential_capability(
    tmp_path: Path,
    refreshable: bool,
) -> None:
    async def exercise() -> None:
        source = _source("src_streamauth1", "Stream auth")
        service = _service(tmp_path, sources=[source])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        assert source.credential_ref is not None
        if refreshable:
            service.adapter.refreshable_credential_refs.add(source.credential_ref)
        settlement_generation = service._reserve_settlement_generation(source.id)
        resolved = ResolvedInvocation(
            backend="codex",
            requested_model_id=requested_model,
            source_id=source.id,
            source_label=source.display_name,
            model_id="shared-model",
            handle=None,
            outcome=None,
            credential_ref=source.credential_ref,
            settlement_generation=settlement_generation,
        )
        outcome = _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=401,
            code="authentication_error",
            source_id=source.id,
            stream_started=True,
        )
        record_attempt = Mock()

        settlement = await service.settle_handle_outcome(
            resolved,
            outcome,
            termination_origin="upstream_terminal",
            record_attempt=record_attempt,
        )

        assert settlement.decision is not None
        assert settlement.decision.action == "surface"
        assert settlement.decision.error_code == "stream_interrupted"
        persisted = service.store.load().sources[0]
        if refreshable:
            assert settlement.decision.reason is None
            assert settlement.turn_outcome is None
            assert persisted.state.status == "standby"
        else:
            assert settlement.decision.reason == "credential_revoked"
            assert settlement.turn_outcome is not None
            assert persisted.state.status == "needs_action"
        assert service.adapter.capability_queries == [source.credential_ref]
        record_attempt.assert_called_once()

    asyncio.run(exercise())


def test_streamed_fallback_reports_an_unpersisted_recovery_transition_honestly(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_recoverytx1", "Recovery warning")
        service = _service(tmp_path, sources=[source])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        service.store.recovery_warning = True
        resolved = ResolvedInvocation(
            backend="codex",
            requested_model_id=requested_model,
            source_id=source.id,
            source_label=source.display_name,
            model_id="shared-model",
            handle=None,
            outcome=None,
            credential_ref=source.credential_ref,
        )
        outcome = _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=429,
            code="rate_limit_error",
            source_id=source.id,
            stream_started=True,
        )
        record_attempt = Mock()

        settlement = await service.settle_handle_outcome(
            resolved,
            outcome,
            termination_origin="upstream_terminal",
            record_attempt=record_attempt,
        )

        projection = settlement.turn_outcome
        assert projection is not None
        assert projection.source_transition_persisted is False
        assert projection.next_current_changed is False
        assert projection.supply_facts is None
        assert project_turn_outcome_copy(projection).key == (
            "modelHub.errors.stream_interrupted"
        )
        assert service.store.config.sources[0].state.status == "standby"
        assert service.events.list() == []
        record_attempt.assert_called_once()

    asyncio.run(exercise())


def test_streamed_blocker_rejected_by_generation_reports_unpersisted(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_stalegen01", "Stale settlement")
        service = _service(tmp_path, sources=[source])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        older_generation = service._reserve_settlement_generation(source.id)
        service._reserve_settlement_generation(source.id)
        resolved = ResolvedInvocation(
            backend="codex",
            requested_model_id=requested_model,
            source_id=source.id,
            source_label=source.display_name,
            model_id="shared-model",
            handle=None,
            outcome=None,
            credential_ref=source.credential_ref,
            settlement_generation=older_generation,
        )
        outcome = _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=401,
            code="authentication_error",
            source_id=source.id,
            stream_started=True,
        )

        settlement = await service.settle_handle_outcome(
            resolved,
            outcome,
            termination_origin="upstream_terminal",
            record_attempt=Mock(),
        )

        projection = settlement.turn_outcome
        assert projection is not None
        assert projection.source_transition_persisted is False
        assert projection.next_current_changed is False
        assert projection.supply_facts is None
        assert service.store.load().sources[0].state.status == "standby"
        assert service.events.list() == []

    asyncio.run(exercise())


def test_streamed_cooldown_rejected_by_generation_reports_unpersisted(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_stalecool01", "Stale cooldown")
        service = _service(tmp_path, sources=[source])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        older_generation = service._reserve_settlement_generation(source.id)
        service._reserve_settlement_generation(source.id)
        resolved = ResolvedInvocation(
            backend="codex",
            requested_model_id=requested_model,
            source_id=source.id,
            source_label=source.display_name,
            model_id="shared-model",
            handle=None,
            outcome=None,
            credential_ref=source.credential_ref,
            settlement_generation=older_generation,
        )
        outcome = _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=429,
            code="rate_limit_error",
            source_id=source.id,
            stream_started=True,
        )
        record_attempt = Mock()

        settlement = await service.settle_handle_outcome(
            resolved,
            outcome,
            termination_origin="upstream_terminal",
            record_attempt=record_attempt,
        )

        projection = settlement.turn_outcome
        assert projection is not None
        assert projection.source_transition_persisted is False
        assert projection.next_current_changed is False
        assert projection.supply_facts is None
        assert service.store.load().sources[0].state.status == "standby"
        assert service.events.list() == []
        record_attempt.assert_called_once()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("failure_phase", "expected_outcome_calls"),
    [("prepare", 0), ("write", 1)],
)
def test_gateway_closes_producer_before_downstream_cancel_settlement(
    tmp_path: Path,
    failure_phase: str,
    expected_outcome_calls: int,
) -> None:
    async def exercise() -> None:
        source = _source("src_close0001", "Closed producer")
        outcome = _outcome(
            RawOutcomeKind.NETWORK_ERROR,
            source_id=source.id,
            stream_started=failure_phase == "write",
        )
        handle = DeferredLifecycleHandle(outcome)
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[handle],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        response = FakeStreamResponse(
            prepare_error=(
                ConnectionResetError("downstream prepare failed")
                if failure_phase == "prepare"
                else None
            ),
            write_error=(
                ConnectionResetError("downstream write failed")
                if failure_phase == "write"
                else None
            ),
        )
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id=f"turn_close_{failure_phase}",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        service.settle_handle_outcome = AsyncMock(
            wraps=service.settle_handle_outcome
        )

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=response,
        ):
            with pytest.raises(ConnectionError, match="downstream"):
                await asyncio.wait_for(
                    gateway._handle_request(request),
                    timeout=1,
                )

        assert handle.close_calls == 1
        assert handle.outcome_calls == expected_outcome_calls
        service.settle_handle_outcome.assert_awaited_once()
        expected_origin = (
            "upstream_terminal" if failure_phase == "write" else "downstream_cancel"
        )
        assert (
            service.settle_handle_outcome.await_args.kwargs["termination_origin"]
            == expected_origin
        )
        if failure_phase == "write":
            assert service.store.load().sources[0].state.status == "standby"
            assert service.events.list() == []
        else:
            assert service.store.load().sources[0].state.status == "standby"
            assert service.events.list() == []

    asyncio.run(exercise())


def test_gateway_close_barrier_preserves_terminal_error_seen_on_failed_write(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_barrier01", "Barrier source")
        outcome = _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=429,
            code="rate_limit_error",
            source_id=source.id,
            stream_started=True,
        )
        handle = DeferredLifecycleHandle(outcome)
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_fact_barrier",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        service.settle_handle_outcome = AsyncMock(wraps=service.settle_handle_outcome)

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(
                write_error=ConnectionResetError("terminal frame write failed")
            ),
        ):
            with pytest.raises(ConnectionError, match="terminal frame"):
                await gateway._handle_request(request)

        assert handle.close_calls == 1
        assert handle.outcome_calls == 1
        call = service.settle_handle_outcome.await_args
        assert call.kwargs["termination_origin"] == "upstream_terminal"
        assert call.args[1] is outcome
        assert service.store.load().sources[0].state.status == "cooldown"

    asyncio.run(exercise())


def test_gateway_downstream_cancellation_does_not_mutate_source(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_cancel01", "Canceled downstream")
        outcome = _outcome(
            RawOutcomeKind.NETWORK_ERROR,
            source_id=source.id,
        )
        handle = BlockingLiveInvokeHandle(outcome)
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[handle],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        settled = asyncio.Event()
        settle_handle_outcome = service.settle_handle_outcome

        async def settle(*args, **kwargs):
            result = await settle_handle_outcome(*args, **kwargs)
            settled.set()
            return result

        service.settle_handle_outcome = AsyncMock(
            side_effect=settle,
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_downstream_cancel",
            requested_model_id=requested_model,
            resolved_model_id="shared-model",
            source_id=source.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={
                        "model": "shared-model",
                        "input": "ping",
                        "stream": True,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                await handle.started.wait()
                response.close()
                await asyncio.wait_for(handle.cancelled.wait(), timeout=1)
                await asyncio.wait_for(settled.wait(), timeout=1)
        finally:
            await gateway.close()

        service.settle_handle_outcome.assert_awaited_once()
        assert (
            service.settle_handle_outcome.await_args.kwargs["termination_origin"]
            == "downstream_cancel"
        )
        assert service.store.load().sources[0].state.status == "standby"
        assert service.events.list() == []

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["resolve", "first_byte", "stream"])
def test_gateway_cancellation_matrix_settles_once_without_upstream_facts(
    tmp_path: Path,
    phase: str,
) -> None:
    async def exercise() -> None:
        source = _source("src_cancelmx1", "Cancellation matrix")
        outcome = _outcome(RawOutcomeKind.NETWORK_ERROR, source_id=source.id)
        handle = (
            MidStreamBlockingInvokeHandle(outcome)
            if phase == "stream"
            else BlockingLiveInvokeHandle(outcome)
        )
        service = _service(
            tmp_path / phase,
            sources=[source],
            live_handles=[] if phase == "resolve" else [handle],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        resolve_started = asyncio.Event()
        if phase == "resolve":
            async def blocked_resolve(**_kwargs):
                resolve_started.set()
                await asyncio.Event().wait()

            service.resolve = AsyncMock(side_effect=blocked_resolve)
        service.settle_handle_outcome = AsyncMock(
            wraps=service.settle_handle_outcome
        )
        gateway = ModelHubTurnGateway(service)
        turn_id = f"turn_cancel_matrix_{phase}"
        request = _prepared_gateway_request(
            gateway,
            turn_id=turn_id,
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(),
        ):
            task = asyncio.create_task(gateway._handle_request(request))
            await asyncio.wait_for(
                resolve_started.wait() if phase == "resolve" else handle.started.wait(),
                timeout=1,
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        service.settle_handle_outcome.assert_awaited_once()
        settlement_call = service.settle_handle_outcome.await_args
        assert settlement_call.kwargs["termination_origin"] == "downstream_cancel"
        assert settlement_call.args[1] is None
        assert service.store.load().sources[0].state.status == "standby"
        assert service.events.list() == []
        if phase == "resolve":
            trace = gateway.correlation._traces[turn_id]
            assert trace.pending_attempt is None
        gateway.correlation.settle(
            turn_id,
            settled_by=SETTLED_BY_STOPPED,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get(turn_id)
        assert record is not None
        assert record["outcome"] == "canceled"
        assert record["terminal_error"] is None
        if phase == "resolve":
            assert record["canceled_attempt"] is None

    asyncio.run(exercise())


def test_gateway_cancellation_during_upstream_settlement_preserves_history(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_eofdone1", "EOF completed")
        outcome = _outcome(RawOutcomeKind.SUCCESS, source_id=source.id)
        handle = LiveInvokeHandle(outcome, (b"data: [DONE]\n\n",))
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[handle],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        settlement_started = asyncio.Event()
        release_settlement = asyncio.Event()
        settle_handle_outcome = service.settle_handle_outcome

        async def blocked_settlement(*args, **kwargs):
            settlement_started.set()
            await release_settlement.wait()
            return await settle_handle_outcome(*args, **kwargs)

        service.settle_handle_outcome = AsyncMock(side_effect=blocked_settlement)
        gateway = ModelHubTurnGateway(service)
        turn_id = "turn_cancel_after_eof"
        request = _prepared_gateway_request(
            gateway,
            turn_id=turn_id,
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        eof_reached = asyncio.Event()

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(eof_reached=eof_reached),
        ):
            task = asyncio.create_task(gateway._handle_request(request))
            await asyncio.wait_for(settlement_started.wait(), timeout=1)
            task.cancel()
            release_settlement.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        service.settle_handle_outcome.assert_awaited_once()
        assert (
            service.settle_handle_outcome.await_args.kwargs["termination_origin"]
            == "upstream_terminal"
        )
        gateway.correlation.settle(
            turn_id,
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get(turn_id)
        assert record is not None
        assert record["outcome"] == "served"
        assert service.store.load().sources[0].state.status == "standby"
        assert service.events.list() == []

    asyncio.run(exercise())


def test_resolver_settles_a_bodyless_attempt_that_beats_cancellation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_cancelbuf", "Cancelled buffered response")
        outcome = _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=429,
            code="rate_limit_error",
            source_id=source.id,
            usage=ProtocolUsageReport.of(
                input_tokens=321,
                cached_input_tokens=0,
                output_tokens=7,
            ),
        )
        service = _service(tmp_path, sources=[source], outcomes=[outcome])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        invoke_started = asyncio.Event()
        release_invoke = asyncio.Event()
        invoke = service.adapter.invoke

        async def blocked_invoke(*args, **kwargs):
            invoke_started.set()
            await release_invoke.wait()
            return await invoke(*args, **kwargs)

        service.adapter.invoke = blocked_invoke
        task = asyncio.create_task(
            service.resolve(
                backend="codex",
                model_id=requested_model,
                request={},
                stream=False,
                supply_channel="hub",
            )
        )
        await asyncio.wait_for(invoke_started.wait(), timeout=1)
        task.cancel()
        release_invoke.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 1
        assert metered["input_tokens"] == 321
        assert metered["output_tokens"] == 7
        assert service.store.load().sources[0].state.status == "cooldown"
        assert [event["reason"] for event in service.events.list()] == ["rate_limited"]

    asyncio.run(exercise())


def test_resolver_meters_an_observed_stream_that_beats_cancellation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_cancelobs", "Cancelled observed response")
        observed = ProtocolSSEState("openai_responses")
        observed.model_output_started = True
        observed.usage = ProtocolUsageReport.of(
            input_tokens=144,
            cached_input_tokens=32,
            output_tokens=9,
        )
        handle = ObservedUnsettledHandle(observed)
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        invoke_started = asyncio.Event()
        release_invoke = asyncio.Event()
        invoke = service.adapter.invoke

        async def blocked_invoke(*args, **kwargs):
            invoke_started.set()
            await release_invoke.wait()
            return await invoke(*args, **kwargs)

        service.adapter.invoke = blocked_invoke
        task = asyncio.create_task(
            service.resolve(
                backend="codex",
                model_id=requested_model,
                request={},
                stream=True,
                supply_channel="hub",
            )
        )
        await asyncio.wait_for(invoke_started.wait(), timeout=1)
        task.cancel()
        release_invoke.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 1
        assert metered["input_tokens"] == 144
        assert metered["cached_input_tokens"] == 32
        assert metered["output_tokens"] == 9
        assert handle.close_calls == 1
        assert service.store.load().sources[0].state.status == "standby"
        assert service.events.list() == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "blocked_phase",
    [
        "resource_teardown",
        "handle_outcome",
        "service_settlement",
        "service_transition",
    ],
)
def test_gateway_repeated_cancellation_drains_settlement_before_reraise(
    tmp_path: Path,
    blocked_phase: str,
) -> None:
    async def exercise() -> None:
        source = _source("src_canceldr1", "Repeated cancellation")
        outcome = _outcome(
            (
                RawOutcomeKind.HTTP_ERROR
                if blocked_phase == "service_transition"
                else RawOutcomeKind.SUCCESS
            ),
            status=(429 if blocked_phase == "service_transition" else None),
            code=(
                "rate_limit_error"
                if blocked_phase == "service_transition"
                else None
            ),
            source_id=source.id,
            stream_started=True,
        )
        handle = RepeatedCancellationHandle(outcome, blocked_phase)
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        settle_handle_outcome = service.settle_handle_outcome

        async def blocked_settlement(*args, **kwargs):
            if blocked_phase == "service_settlement":
                handle.phase_started.set()
                await handle.release_phase.wait()
            return await settle_handle_outcome(*args, **kwargs)

        service.settle_handle_outcome = AsyncMock(side_effect=blocked_settlement)
        settle_fallback_source = service._settle_fallback_source

        async def blocked_transition(*args, **kwargs):
            handle.phase_started.set()
            await handle.release_phase.wait()
            return await settle_fallback_source(*args, **kwargs)

        if blocked_phase == "service_transition":
            service._settle_fallback_source = AsyncMock(
                side_effect=blocked_transition
            )
        gateway = ModelHubTurnGateway(service)
        record_turn_outcome = gateway.correlation.record_turn_outcome
        gateway.correlation.record_turn_outcome = Mock(
            side_effect=record_turn_outcome
        )
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_repeated_cancel",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        response = FakeStreamResponse()

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=response,
        ):
            task = asyncio.create_task(gateway._handle_request(request))
            await asyncio.wait_for(handle.started.wait(), timeout=1)
            task.cancel()
            await asyncio.wait_for(handle.phase_started.wait(), timeout=1)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            assert not task.done()
            handle.release_phase.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        service.settle_handle_outcome.assert_awaited_once()
        gateway.correlation.record_turn_outcome.assert_called_once()
        assert (
            service.settle_handle_outcome.await_args.kwargs["termination_origin"]
            == "upstream_terminal"
        )
        gateway.correlation.settle(
            "turn_repeated_cancel",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_repeated_cancel")
        assert record is not None
        if blocked_phase == "service_transition":
            assert record["outcome"] == "failed_terminal"
            assert service.store.load().sources[0].state.status == "cooldown"
            assert [event["reason"] for event in service.events.list()] == [
                "rate_limited"
            ]
        else:
            assert record["outcome"] == "served"
            assert record["terminal_error"] is None
        assert handle.close_calls >= 1
        assert handle.outcome_calls == 1

    asyncio.run(exercise())


def test_settlement_timeout_preserves_an_already_committed_upstream_fact(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_t3barrier1", "Committed terminal fact")
        outcome = _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=429,
            code="rate_limit_error",
            source_id=source.id,
            stream_started=True,
        )
        handle = LiveInvokeHandle(
            outcome,
            (b'data: {"error":{"type":"rate_limit_error"}}\n\n',),
        )
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        transition_started = asyncio.Event()
        never_release = asyncio.Event()

        async def blocked_transition(*_args, **_kwargs):
            transition_started.set()
            await never_release.wait()

        service._settle_fallback_source = AsyncMock(side_effect=blocked_transition)
        gateway = ModelHubTurnGateway(service, transport_timeout=0.02)
        turn_id = "turn_t3_barrier"
        request = _prepared_gateway_request(
            gateway,
            turn_id=turn_id,
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(),
        ):
            task = asyncio.create_task(gateway._handle_request(request))
            await asyncio.wait_for(transition_started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        assert gateway.resource_leak_records == (("settlement", turn_id),)
        gateway.correlation.settle(
            turn_id,
            settled_by=SETTLED_BY_STOPPED,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get(turn_id)
        assert record is not None
        assert record["outcome"] == "failed_terminal"
        assert record["terminal_error"]["reason"] == "stream_interrupted"
        assert record["terminal_error"]["source_id"] == source.id
        assert service.store.load().sources[0].state.status == "standby"

    asyncio.run(exercise())


def test_gateway_abandons_never_resolving_teardown_at_transport_deadline(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        source = _source("src_teardown_timeout", "Teardown timeout")
        handle = NeverResolvingCloseHandle(_outcome(RawOutcomeKind.SUCCESS, source_id=source.id))
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service, transport_timeout=0.02)
        turn_id = "turn_teardown_timeout"
        request = _prepared_gateway_request(
            gateway,
            turn_id=turn_id,
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(),
        ):
            task = asyncio.create_task(gateway._handle_request(request))
            await asyncio.wait_for(handle.started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        assert gateway.resource_leak_records == (("resource_teardown", turn_id),)
        gateway.correlation.settle(
            turn_id,
            settled_by=SETTLED_BY_STOPPED,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get(turn_id)
        assert record is not None
        assert record["outcome"] == "failed_terminal"
        assert record["terminal_error"] == {
            "source_id": None,
            "configured_model_id": None,
            "channel": None,
            "reason": "engine_down",
            "stream_started": False,
        }
        assert service.store.load().sources[0].state.status == "standby"
        assert service.events.list() == []
        handle.release_close.set()
        await asyncio.wait_for(handle.closed.wait(), timeout=1)

    with caplog.at_level("ERROR", logger="core.handlers.model_hub.turn_gateway"):
        asyncio.run(exercise())
    assert "Abandoned Model Hub turn resource" in caplog.text


def test_gateway_eof_disconnect_after_upstream_outcome_keeps_upstream_history(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_eofdrop1", "EOF disconnect")
        outcome = _outcome(RawOutcomeKind.SUCCESS, source_id=source.id)
        handle = LiveInvokeHandle(outcome, (b"data: [DONE]\n\n",))
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        turn_id = "turn_eof_disconnect"
        request = _prepared_gateway_request(
            gateway,
            turn_id=turn_id,
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        service.settle_handle_outcome = AsyncMock(
            wraps=service.settle_handle_outcome
        )

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(
                eof_error=ConnectionResetError("downstream EOF failed")
            ),
        ):
            with pytest.raises(ConnectionError, match="downstream EOF failed"):
                await gateway._handle_request(request)

        assert (
            service.settle_handle_outcome.await_args.kwargs["termination_origin"]
            == "upstream_terminal"
        )
        gateway.correlation.settle(
            turn_id,
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get(turn_id)
        assert record is not None
        assert record["outcome"] == "served"
        assert record["terminal_error"] is None
        assert service.store.load().sources[0].state.status == "standby"

    asyncio.run(exercise())


def test_gateway_upstream_stream_failure_remains_source_attributable(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_upstream01", "Broken upstream")
        outcome = _outcome(
            RawOutcomeKind.NETWORK_ERROR,
            source_id=source.id,
            stream_started=True,
        )
        handle = BrokenUpstreamInvokeHandle(outcome)
        service = _service(tmp_path, sources=[source])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        service.adapter.live_handles.append(handle)
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_upstream_disconnect",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        service.settle_handle_outcome = AsyncMock(
            wraps=service.settle_handle_outcome
        )

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(),
        ):
            with pytest.raises(ConnectionResetError, match="upstream disconnected"):
                await gateway._handle_request(request)

        service.settle_handle_outcome.assert_awaited_once()
        assert (
            service.settle_handle_outcome.await_args.kwargs["termination_origin"]
            == "upstream_terminal"
        )
        assert service.store.load().sources[0].state.status == "standby"
        assert service.events.list() == []

    asyncio.run(exercise())


def test_shared_source_transition_event_is_emitted_once_for_concurrent_turns(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_sharedtx1", "Shared transition")
        service = _service(tmp_path, sources=[source])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        settlement_generation = service._reserve_settlement_generation(source.id)
        resolved = ResolvedInvocation(
            backend="codex",
            requested_model_id=requested_model,
            source_id=source.id,
            source_label=source.display_name,
            model_id="shared-model",
            handle=None,
            outcome=None,
            settlement_generation=settlement_generation,
        )
        outcome = _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=429,
            code="rate_limit_error",
            source_id=source.id,
            stream_started=True,
        )
        record_attempts = [Mock(), Mock()]

        await asyncio.gather(
            *(
                service.settle_handle_outcome(
                    resolved,
                    outcome,
                    termination_origin="upstream_terminal",
                    record_attempt=record_attempt,
                )
                for record_attempt in record_attempts
            )
        )

        assert all(record_attempt.call_count == 1 for record_attempt in record_attempts)
        assert service.store.load().sources[0].state.status == "cooldown"
        assert [event["reason"] for event in service.events.list()] == [
            "rate_limited"
        ]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_provenance"),
    [
        (RawOutcomeKind.SUCCESS, 200, "served"),
        (RawOutcomeKind.NETWORK_ERROR, 502, "failed_terminal"),
    ],
)
def test_live_handle_settlement_survives_concurrent_source_deletion(
    tmp_path: Path,
    kind: RawOutcomeKind,
    expected_status: int,
    expected_provenance: str,
) -> None:
    async def exercise() -> None:
        source = _source("src_deletedlive1", "Deleted during live call")
        outcome = _outcome(
            kind,
            source_id=source.id,
            stream_started=kind is RawOutcomeKind.NETWORK_ERROR,
        )
        handle = BlockingLiveInvokeHandle(outcome)
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[handle],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        with pytest.raises(ModelHubError) as delete_refusal:
            await service.delete_source(source.id)
        turn_id = f"turn_deleted_live_{kind.value}"
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id=turn_id,
            requested_model_id=requested_model,
            resolved_model_id="shared-model",
            source_id=source.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response_task = asyncio.create_task(
                    client.post(
                        f"{base_url}/v1/responses",
                        json={
                            "model": "shared-model",
                            "input": "ping",
                            "stream": False,
                        },
                        headers={"Authorization": f"Bearer {token}"},
                    )
                )
                await asyncio.wait_for(handle.started.wait(), timeout=1)
                await service.delete_source(
                    source.id,
                    force=True,
                    confirmed_remove_hops=delete_refusal.value.data[
                        "would_remove_hops"
                    ],
                    confirmed_interruptions=delete_refusal.value.data[
                        "would_interrupt"
                    ],
                )
                handle.release()
                response = await asyncio.wait_for(response_task, timeout=1)
                payload = await response.read()
                assert response.status == expected_status
                assert b"source_not_found" not in payload
        finally:
            handle.release()
            await gateway.close()

        assert service.store.load().sources == []
        gateway.correlation.settle(
            turn_id,
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get(turn_id)
        assert record is not None
        assert record["outcome"] == expected_provenance
        assert service.events.list() == []

    asyncio.run(exercise())


def test_gateway_localizes_terminal_permission_denial_without_switching(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_primary01", "Primary")
        service = _service(
            tmp_path,
            sources=[source],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=403,
                    code="permission_error",
                    source_id=source.id,
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service, language_provider=lambda: "zh")
        base_url, token = await gateway.endpoint(
            "codex",
            process_scope="/repo",
            turn_id="turn_permission_denied",
            requested_model_id=requested_model,
            resolved_model_id="shared-model",
            source_id=source.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/responses",
                    json={"model": "shared-model", "input": "ping", "stream": False},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status == 403
                payload = await response.json()
                assert payload["error"] == {
                    "type": "request_incompatible",
                    "code": "request_incompatible",
                    "message": i18n_t("modelHub.launch.request_incompatible", "zh"),
                }
        finally:
            await gateway.close()

        assert service.adapter.invocations == [
            (source.id, "shared-model", "codex")
        ]
        assert service.store.load().sources[0].state.status == "standby"
        gateway.correlation.settle(
            "turn_permission_denied",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_permission_denied")
        assert record is not None
        assert record["outcome"] == "failed_terminal"
        assert record["failed_attempts"] == []
        assert record["terminal_error"] == {
            "source_id": source.id,
            "configured_model_id": "shared-model",
            "channel": "hub",
            "reason": "invalid_parameter",
            "stream_started": False,
        }
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_gateway_provenance_retains_pre_mapping_model_identity(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("codex", "/repo", "turn_mapped")
    registry.prepare_gateway_turn(
        backend="codex",
        token=token,
        requested_model_id="gpt-5",
        resolved_model_id="custom-gpt-5",
        source_id="src_primary01",
        via_mapping=True,
    )

    exact_turn = registry.begin_gateway_request(
        backend="codex",
        token=token,
        requested_model_id="custom-gpt-5",
    )
    registry.begin_attempt(
        exact_turn,
        source_id="src_primary01",
        resolved_model_id="custom-gpt-5",
        channel="hub",
        via_mapping=False,
    )
    success = _outcome(RawOutcomeKind.SUCCESS)
    registry.finish_attempt(
        exact_turn,
        outcome=success,
        decision=classify_outcome(success),
    )
    registry.settle(
        "turn_mapped",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_mapped")
    assert record is not None
    assert record["requested_model_id"] == "gpt-5"
    assert record["served"] == {
        "source_id": "src_primary01",
        "configured_model_id": "custom-gpt-5",
        "channel": "hub",
    }
    _assert_valid("turn-provenance.schema.json", record)


@pytest.mark.parametrize(
    "wire_model_id",
    [
        pytest.param("alias-a", id="canonical_backend_id"),
        pytest.param("model-b", id="legacy_upstream_target"),
    ],
)
def test_gateway_accepts_canonical_backend_id_and_unique_legacy_target(
    tmp_path: Path,
    wire_model_id: str,
) -> None:
    """A route answers for both model ids a request on it may name.

    The launch is told which id to send, so that one is canonical. A process
    started before Avibe made it explicit still sends the resolved upstream
    id, and its requests must keep routing: the credential already named the
    route, so accepting the older shape resolves an alias rather than
    guessing between routes.
    """

    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)
    launch_token = registry.prepare_gateway_turn(
        backend="codex",
        token=registry.credentials("codex", "/repo", "turn_alias"),
        requested_model_id="alias-a",
        resolved_model_id="model-b",
        source_id="src_primary01",
        via_mapping=False,
        gateway_request_model_id="alias-a",
    )

    routing = registry.claim_gateway_request(
        backend="codex",
        token=launch_token,
        prepared_turn_id="turn_alias",
        gateway_model_id=wire_model_id,
    )

    assert routing.caller_model_id == "alias-a"
    assert routing.owner_turn_id == "turn_alias"


def test_gateway_uses_persisted_exact_hops_for_failover(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        requested_model = "claude-opus-4-5"
        primary_model = "claude-opus-4-5-20251101"
        backup_model = "claude-opus-4-5-20250929"
        primary = _source(
            "src_primary01",
            "Primary",
            vendor="anthropic",
            protocol="anthropic",
            model_id=primary_model,
        )
        backup = _source(
            "src_backup001",
            "Backup",
            vendor="anthropic",
            protocol="anthropic",
            model_id=backup_model,
        )
        service = _service(
            tmp_path,
            sources=[primary, backup],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    source_id=primary.id,
                ),
                _outcome(
                    RawOutcomeKind.SUCCESS,
                    source_id=backup.id,
                ),
            ],
        )
        _canonicalize_fixed_test_routes(service)
        service.store.config.agents["claude"].routes[requested_model] = ModelHubRouteConfig(
            hops=(
                ModelHubRouteHopConfig(primary.id, primary_model),
                ModelHubRouteHopConfig(backup.id, backup_model),
            )
        )
        gateway = ModelHubTurnGateway(service)
        base_url, token = await gateway.endpoint(
            "claude",
            process_scope="/repo",
            turn_id="turn_native_alias",
            requested_model_id=requested_model,
            resolved_model_id=primary_model,
            source_id=primary.id,
        )
        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{base_url}/v1/messages",
                    json={
                        "model": primary_model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                        "stream": False,
                    },
                    headers={"x-api-key": token},
                )
                assert response.status == 200
                await response.read()
        finally:
            await gateway.close()

        assert service.adapter.invocations == [
            (primary.id, primary_model, "claude"),
            (backup.id, backup_model, "claude"),
        ]
        failover_events = [event for event in service.events.list(limit=20) if event["kind"] in {"cooldown", "switch"}]
        assert {event["model_id"] for event in failover_events} == {requested_model}

        gateway.correlation.settle(
            "turn_native_alias",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
        )
        record = service.provenance.get("turn_native_alias")
        assert record is not None
        assert record["requested_model_id"] == requested_model
        assert record["served"]["source_id"] == backup.id
        assert record["served"]["configured_model_id"] == backup_model
        _assert_valid("turn-provenance.schema.json", record)

    asyncio.run(exercise())


def test_gateway_keeps_prepared_route_during_overlapping_turns(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        requested_model = "claude-opus-4-8"
        target_model = "gpt-5.6-luna"
        source = _source(
            "src_gptrelay01",
            "GPT relay",
            model_id=target_model,
        )
        outcome = _outcome(
            RawOutcomeKind.SUCCESS,
            status=200,
            source_id=source.id,
        )
        response_body = (
            b'{"id":"msg_fixture","type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":"ok"}],'
            b'"model":"gpt-5.6-luna","stop_reason":"end_turn",'
            b'"usage":{"input_tokens":1,"output_tokens":1}}'
        )
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[LiveInvokeHandle(outcome, (response_body,))],
        )
        _canonicalize_fixed_test_routes(service)
        service.store.config.agents["claude"].routes[requested_model] = ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(source.id, target_model),)
        )
        gateway = ModelHubTurnGateway(service)
        first_base_url, first_token = await gateway.endpoint(
            "claude",
            process_scope="session:shared-claude",
            turn_id="turn_overlap_first",
            requested_model_id=requested_model,
            resolved_model_id=target_model,
            source_id=source.id,
        )
        second_base_url, second_token = await gateway.endpoint(
            "claude",
            process_scope="session:shared-claude",
            turn_id="turn_overlap_second",
            requested_model_id=requested_model,
            resolved_model_id=target_model,
            source_id=source.id,
        )
        assert first_base_url == second_base_url
        assert first_token == second_token

        try:
            async with aiohttp.ClientSession(trust_env=False) as client:
                response = await client.post(
                    f"{second_base_url}/v1/messages",
                    json={
                        "model": target_model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                        "stream": False,
                    },
                    headers={"x-api-key": second_token},
                )
                assert response.status == 200
                await response.read()
        finally:
            await gateway.close()

        assert service.adapter.invocations == [
            (source.id, target_model, "claude"),
        ]

    asyncio.run(exercise())


def test_gateway_model_outside_prepared_turn_fails_closed(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials("codex", "/repo", "turn_expected")
    registry.prepare_gateway_turn(
        backend="codex",
        token=token,
        requested_model_id="gpt-5",
        resolved_model_id="custom-gpt-5",
        source_id="src_primary01",
        via_mapping=True,
    )

    assert (
        registry.begin_gateway_request(
            backend="codex",
            token=token,
            requested_model_id="untracked-model",
        )
        is None
    )
    registry.settle(
        "turn_expected",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )
    assert store.get("turn_expected") is None


def test_native_terminal_failure_is_not_recorded_as_served(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "native-failure.json")
    registry = TurnCorrelationRegistry(store)
    registry.begin_native_attempt(
        backend="codex",
        process_scope="/repo",
        turn_id="turn_native_failure",
        requested_model_id="shared-model",
        source_id="src_primary01",
        resolved_model_id="shared-model",
        via_mapping=False,
    )

    registry.fail_native_attempt(
        "turn_native_failure",
        reason="quota_exhausted",
    )
    registry.settle(
        "turn_native_failure",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_native_failure")
    assert record is not None
    assert record["outcome"] == "exhausted"
    assert record["served"] is None
    assert record["failed_attempts"] == [
        {
            "source_id": "src_primary01",
            "configured_model_id": "shared-model",
            "channel": "native_cli",
            "reason": "quota_exhausted",
        }
    ]
    _assert_valid("turn-provenance.schema.json", record)


def test_fsm_drop_overrides_a_completed_gateway_attempt(tmp_path: Path) -> None:
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)
    exact_turn = _begin_hub_attempt(registry, turn_id="turn_drop")
    success = _outcome(RawOutcomeKind.SUCCESS)
    registry.finish_attempt(
        exact_turn,
        outcome=success,
        decision=classify_outcome(success),
    )

    registry.settle(
        "turn_drop",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_drop")
    assert record is not None
    assert record["outcome"] == "failed_terminal"
    assert record["terminal_error"]["reason"] == "stream_interrupted"
    assert record["served"] is None
    _assert_valid("turn-provenance.schema.json", record)


def test_backend_terminal_failure_overrides_gateway_success(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "backend-terminal.json")
    registry = TurnCorrelationRegistry(store)
    exact_turn = _begin_hub_attempt(
        registry,
        turn_id="turn_backend_terminal",
    )
    success = _outcome(RawOutcomeKind.SUCCESS)
    registry.finish_attempt(
        exact_turn,
        outcome=success,
        decision=classify_outcome(success),
    )

    registry.fail_hub_attempt("turn_backend_terminal")
    registry.settle(
        "turn_backend_terminal",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_backend_terminal")
    assert record is not None
    assert record["outcome"] == "failed_terminal"
    assert record["served"] is None
    assert record["terminal_error"]["reason"] == "protocol_error"
    _assert_valid("turn-provenance.schema.json", record)


def test_lost_terminal_retains_completed_fallback_attempts(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "lost-terminal.json")
    registry = TurnCorrelationRegistry(store)
    exact_turn = _begin_hub_attempt(
        registry,
        turn_id="turn_lost_terminal",
    )
    failure = _outcome(
        RawOutcomeKind.HTTP_ERROR,
        status=429,
    )
    registry.finish_attempt(
        exact_turn,
        outcome=failure,
        decision=classify_outcome(failure),
    )

    registry.settle(
        "turn_lost_terminal",
        settled_by=SETTLED_BY_NO_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )

    record = store.get("turn_lost_terminal")
    assert record is not None
    assert record["outcome"] == "exhausted"
    assert record["failed_attempts"][0]["reason"] == "rate_limited"
    _assert_valid("turn-provenance.schema.json", record)


def test_ambiguous_mixed_and_opencode_attempts_leave_no_record(
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "provenance.json")
    registry = TurnCorrelationRegistry(store)

    first = _begin_hub_attempt(registry, turn_id="turn_first")
    assert first == "turn_first"
    registry.credentials("codex", "/repo", "turn_second")
    assert (
        registry.begin_gateway_request(
            backend="codex",
            token=registry.credentials("codex", "/repo", "turn_second"),
            requested_model_id="shared-model",
        )
        is None
    )
    registry.settle("turn_first", settled_by=SETTLED_BY_STOPPED)
    registry.settle("turn_second", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert store.get("turn_first") is None
    assert store.get("turn_second") is None

    mixed = _begin_hub_attempt(
        registry,
        turn_id="turn_mixed",
        scope="/mixed",
    )
    registry.credentials("codex", "/mixed", None)
    success = _outcome(RawOutcomeKind.SUCCESS)
    registry.finish_attempt(
        mixed,
        outcome=success,
        decision=classify_outcome(success),
    )
    registry.settle("turn_mixed", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert store.get("turn_mixed") is None

    opencode_token = registry.credentials(
        "opencode",
        "opencode:shared-server",
        "turn_opencode",
    )
    assert (
        registry.begin_gateway_request(
            backend="opencode",
            token=opencode_token,
            requested_model_id="openai/shared-model",
        )
        is None
    )
    registry.settle("turn_opencode", settled_by=SETTLED_BY_TERMINAL_RESULT)
    assert store.get("turn_opencode") is None


def test_fsm_cancel_drop_and_control_classification(tmp_path: Path) -> None:
    async def exercise(
        *,
        turn_id: str,
        mode: str,
    ) -> dict | None:
        store = BoundedProvenanceStore(tmp_path / f"{turn_id}.json")
        registry = TurnCorrelationRegistry(store)
        runtime = SimpleNamespace(
            settle_turn=lambda value, *, settled_by, ts, mode=None: registry.settle(
                value,
                settled_by=settled_by,
                ts=ts,
            )
        )
        controller = SimpleNamespace(
            model_hub_runtime=runtime,
            command_handler=SimpleNamespace(handle_stop=AsyncMock(return_value=True)),
            set_agent_status=lambda _session_id, _status: None,
        )
        manager = SessionTurnManager(controller)
        manager.flush_queue = AsyncMock(return_value=False)
        started = asyncio.Event()
        hold = asyncio.Event()

        async def dispatch(_controller, _context, _text, **_kwargs):
            current_turn = manager.model_hub_turn_id_for_task()
            assert current_turn == turn_id
            exact_turn = _begin_hub_attempt(
                registry,
                turn_id=current_turn,
            )
            started.set()
            if mode == "cancel":
                await hold.wait()
            if mode == "served":
                success = _outcome(RawOutcomeKind.SUCCESS)
                registry.finish_attempt(
                    exact_turn,
                    outcome=success,
                    decision=classify_outcome(success),
                )
                settlement = SETTLED_BY_TERMINAL_RESULT
            else:
                settlement = SETTLED_BY_NO_TERMINAL_RESULT
            return TurnDispatchOutcome(error=None, settled_by=settlement)

        context = SimpleNamespace(
            platform="avibe",
            platform_specific={
                "turn_token": turn_id,
                "agent_session_target": {"agent_backend": "codex"},
            },
        )
        with patch(
            "core.session_turns.dispatch_turn_with_outcome",
            side_effect=dispatch,
        ):
            await manager._run(turn_id, context, "test")
            task = manager.in_flight[turn_id].task
            await started.wait()
            if mode == "cancel":
                result = await manager.cancel(turn_id)
                assert result["status"] == "cancel_requested"
            await asyncio.gather(task, return_exceptions=True)
        return store.get(turn_id)

    async def run_cases() -> tuple[dict, dict, dict]:
        canceled = await exercise(turn_id="turn_cancel", mode="cancel")
        dropped = await exercise(turn_id="turn_drop", mode="drop")
        served = await exercise(turn_id="turn_control", mode="served")
        assert canceled is not None
        assert dropped is not None
        assert served is not None
        return canceled, dropped, served

    canceled, dropped, served = asyncio.run(run_cases())
    assert canceled["outcome"] == "canceled"
    assert canceled["canceled_attempt"]["source_id"] == "src_primary01"
    assert dropped["outcome"] == "failed_terminal"
    assert dropped["terminal_error"]["reason"] == "stream_interrupted"
    assert served["outcome"] == "served"
    for record in (canceled, dropped, served):
        _assert_valid("turn-provenance.schema.json", record)


def test_fsm_settlement_persists_the_turn_time_mode() -> None:
    runtime = SimpleNamespace(settle_turn=Mock())
    manager = SessionTurnManager(SimpleNamespace(model_hub_runtime=runtime))
    context = SimpleNamespace(platform_specific={"turn_token": "turn_direct"})
    bind_launch(
        context,
        ModelHubLaunch(
            backend="codex",
            channel="direct",
            requested_model="gpt-5",
            target_model="gpt-5",
            runtime_model="gpt-5",
        ),
    )

    manager._settle_model_hub_turn(
        context,
        SETTLED_BY_TERMINAL_RESULT,
    )

    runtime.settle_turn.assert_called_once()
    assert runtime.settle_turn.call_args.kwargs["mode"] == "direct"


def test_fsm_settlement_persists_model_less_direct_mode() -> None:
    runtime = SimpleNamespace(settle_turn=Mock())
    manager = SessionTurnManager(SimpleNamespace(model_hub_runtime=runtime))
    context = SimpleNamespace(platform_specific={"turn_token": "turn_opencode_default"})
    bind_turn_mode(context, "direct")

    manager._settle_model_hub_turn(
        context,
        SETTLED_BY_TERMINAL_RESULT,
    )

    runtime.settle_turn.assert_called_once()
    assert runtime.settle_turn.call_args.kwargs["mode"] == "direct"


def test_settlement_retires_correlation_when_mode_persistence_fails(
    tmp_path: Path,
) -> None:
    correlation = Mock()
    service = _service(
        tmp_path,
        sources=[_source("src_primary01", "Primary")],
    )
    service.note_turn_mode = Mock(side_effect=OSError("mode store unavailable"))
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(correlation=correlation),
    )

    with pytest.raises(OSError, match="mode store unavailable"):
        router.settle_turn(
            "turn_mode_failure",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=NOW.isoformat(),
            mode="hub",
        )

    correlation.settle.assert_called_once_with(
        "turn_mode_failure",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
        ts=NOW.isoformat(),
    )


def test_opencode_overlay_projects_menu_identity_to_exact_hop_model(tmp_path: Path) -> None:
    source = _source("src_overlay01", "Overlay", model_id="upstream-model")
    source.models[0].reasoning_efforts = ["medium"]
    config = _config([source])
    agent = config.agents["opencode"]
    agent.routes.pop("openai/shared-model")
    agent.routes["openai/menu-model"] = ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(source.id, "upstream-model"),))
    agent.models = [
        ModelHubBackendModelConfig(
            id="openai/menu-model",
            reasoning_efforts=["low", "high"],
        )
    ]
    agent.menu.checked = ["openai/menu-model"]
    service = _service(tmp_path, sources=[source])
    service.store.config = config
    gateway = SimpleNamespace(
        endpoint=AsyncMock(return_value=("http://127.0.0.1:19000", "gateway-token")),
    )
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=gateway,
        overlay_path=tmp_path / "overlay.json",
    )

    overlay = asyncio.run(router.prepare_opencode_overlay())

    assert overlay is not None
    payload = json.loads(overlay.content)
    assert overlay.provider_id.startswith("avibe-model-hub-")
    assert overlay.provider_id != "avibe-model-hub"
    provider = payload["provider"][overlay.provider_id]
    assert provider["models"]["openai/menu-model"]["id"] == "openai/menu-model"
    assert provider["models"]["openai/menu-model"]["variants"] == {
        "high": {"reasoningEffort": "high"},
        "low": {"reasoningEffort": "low"},
    }
    assert overlay.launches[0].target_model == "upstream-model"
    assert opencode_model_catalog_for_overlay(overlay) == {
        "providers": [
            {
                "id": overlay.provider_id,
                "models": provider["models"],
            }
        ],
        "default": {},
    }


def test_opencode_public_models_follow_persisted_config_without_overlay(
    tmp_path: Path,
) -> None:
    source = _source("src_catalog01", "Catalog", model_id="upstream-model")
    source.models[0].display_name = "Current model"
    source.models[0].reasoning_efforts = ["low", "high"]
    config = _config([source])
    agent = config.agents["opencode"]
    agent.routes = {
        "custom/current-model": ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(source.id, "upstream-model"),)
        )
    }
    agent.models = [
        ModelHubBackendModelConfig(
            id="custom/current-model",
            display_name="Current model",
            reasoning_efforts=["low", "high"],
        )
    ]
    agent.menu.checked = ["custom/current-model"]
    service = _service(tmp_path, sources=[source])
    service.store.config = config

    assert service.opencode_public_models() == {
        "custom/current-model": {
            "id": "custom/current-model",
            "name": "Current model",
            "variants": {
                "low": {"reasoningEffort": "low"},
                "high": {"reasoningEffort": "high"},
            },
        }
    }

    service.store.config.agents["opencode"].mode = "direct"
    assert service.opencode_public_models() == {}


def test_opencode_public_model_hides_preserved_efforts_when_reasoning_is_disabled(
    tmp_path: Path,
) -> None:
    source = _source("src_no_reasoning", "No reasoning", model_id="upstream-model")
    config = _config([source])
    agent = config.agents["opencode"]
    agent.models = [
        ModelHubBackendModelConfig(
            id="custom/no-reasoning",
            supports_reasoning=False,
            reasoning_efforts=["low", "high"],
        )
    ]
    agent.menu.checked = ["custom/no-reasoning"]
    service = _service(tmp_path, sources=[source])
    service.store.config = config

    assert service.opencode_public_models()["custom/no-reasoning"] == {
        "id": "custom/no-reasoning",
        "name": "custom/no-reasoning",
        "reasoning": False,
    }


@pytest.mark.parametrize(
    ("input_modalities", "output_modalities", "expected"),
    [
        (["text"], [], {"input": ["text"]}),
        ([], ["text"], {"output": ["text"]}),
        (["text", "image"], ["text"], {"input": ["text", "image"], "output": ["text"]}),
        ([], [], None),
    ],
)
def test_opencode_public_model_omits_unspecified_modality_directions(
    input_modalities: list[str],
    output_modalities: list[str],
    expected: dict[str, list[str]] | None,
) -> None:
    model = ModelHubBackendModelConfig(
        id="custom/modalities",
        input_modalities=input_modalities,
        output_modalities=output_modalities,
    )

    projected = project_opencode_public_model(model)

    if expected is None:
        assert "modalities" not in projected
    else:
        assert projected["modalities"] == expected


def test_opencode_overlay_private_provider_id_is_credential_scoped(
    tmp_path: Path,
) -> None:
    source = _source("src_overlay10", "Overlay")
    service = _service(tmp_path, sources=[source])
    endpoint = AsyncMock(
        side_effect=(
            ("http://127.0.0.1:19000/opencode", "gateway-token-one"),
            ("http://127.0.0.1:19000/opencode", "gateway-token-two"),
        )
    )
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(endpoint=endpoint),
        overlay_path=tmp_path / "overlay.json",
    )

    first = asyncio.run(router.prepare_opencode_overlay())
    second = asyncio.run(router.prepare_opencode_overlay())

    assert first is not None
    assert second is not None
    assert first.provider_id != second.provider_id
    assert set(json.loads(first.content)["provider"]) == {first.provider_id}
    assert set(json.loads(second.content)["provider"]) == {second.provider_id}


def test_opencode_overlay_supports_mixed_protocols_under_one_provider(tmp_path: Path) -> None:
    first = _source(
        "src_overlay11",
        "First",
        vendor="custom",
        protocol="anthropic",
        model_id="first-model",
    )
    second = _source(
        "src_overlay12",
        "Second",
        vendor="custom",
        protocol="openai_responses",
        model_id="second-model",
    )
    config = _config([first, second])
    agent = config.agents["opencode"]
    agent.models = [
        ModelHubBackendModelConfig(id="custom/first-model"),
        ModelHubBackendModelConfig(id="custom/second-model"),
    ]
    agent.menu.checked = ["custom/first-model", "custom/second-model"]
    agent.routes = {
        "custom/first-model": ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(first.id, "first-model"),)),
        "custom/second-model": ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(second.id, "second-model"),)),
    }
    service = _service(tmp_path, sources=[first, second])
    service.store.config = config
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(
            endpoint=AsyncMock(return_value=("http://127.0.0.1:19000/opencode", "gateway-token")),
        ),
        overlay_path=tmp_path / "overlay.json",
    )

    overlay = asyncio.run(router.prepare_opencode_overlay())

    assert overlay is not None
    payload = json.loads(overlay.content)
    assert set(payload["provider"]) == {overlay.provider_id}
    provider = payload["provider"][overlay.provider_id]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:19000/opencode/v1"
    assert set(provider["models"]) == {
        "custom/first-model",
        "custom/second-model",
    }
    assert {launch.target_model for launch in overlay.launches} == {
        "first-model",
        "second-model",
    }


def test_opencode_overlay_selects_supported_fallback_by_exact_hop(tmp_path: Path) -> None:
    source = _source(
        "src_overlay03",
        "Overlay",
        model_id="supported-model",
    )
    config = _config([source])
    agent = config.agents["opencode"]
    agent.routes.pop("openai/shared-model")
    agent.routes["openai/menu-model"] = ModelHubRouteConfig(
        hops=(
            ModelHubRouteHopConfig(source.id, "stale-model"),
            ModelHubRouteHopConfig(source.id, "supported-model"),
        )
    )
    agent.models = [ModelHubBackendModelConfig(id="openai/menu-model")]
    agent.menu.checked = ["openai/menu-model"]
    service = _service(tmp_path, sources=[source])
    service.store.config = config
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(
            endpoint=AsyncMock(return_value=("http://127.0.0.1:19000", "gateway-token")),
        ),
        overlay_path=tmp_path / "overlay.json",
    )

    overlay = asyncio.run(router.prepare_opencode_overlay())

    assert overlay is not None
    assert [launch.target_model for launch in overlay.launches] == ["supported-model"]


def test_opencode_overlay_preserves_checked_route_with_stale_exact_hop(
    tmp_path: Path,
) -> None:
    source = _source(
        "src_overlay04",
        "Overlay",
        model_id="current-model",
    )
    config = _config([source])
    agent = config.agents["opencode"]
    agent.routes.pop("openai/shared-model")
    agent.routes["openai/menu-model"] = ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(source.id, "stale-model"),))
    agent.models = [ModelHubBackendModelConfig(id="openai/menu-model")]
    agent.menu.checked = ["openai/menu-model"]
    service = _service(tmp_path, sources=[source])
    service.store.config = config
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(
            endpoint=AsyncMock(return_value=("http://127.0.0.1:19000", "gateway-token")),
        ),
        overlay_path=tmp_path / "overlay.json",
    )

    overlay = asyncio.run(router.prepare_opencode_overlay())

    assert overlay is not None
    provider = json.loads(overlay.content)["provider"][overlay.provider_id]
    assert provider["models"] == {
        "openai/menu-model": {
            "id": "openai/menu-model",
            "name": "openai/menu-model",
        }
    }
    assert overlay.checked_identifiers == ("openai/menu-model",)
    assert overlay.available_identifiers == ()
    assert overlay.launches == ()


def test_production_adapter_retargets_api_keys_without_exposing_or_mutating_them(
    tmp_path: Path,
) -> None:
    state_store = EngineStateStore(tmp_path / "engine-state")
    old_ref = state_store.store_api_key(
        "test-retarget-secret",
        vendor="custom",
        protocol="openai_chat",
        base_url="https://old-relay.example/v1",
    )
    oauth_ref = state_store.bind_oauth_credential(
        "src_oauthrefresh",
        "openai",
        "codex-test.json",
    )
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )

    replacement_ref = asyncio.run(
        adapter.retarget_api_key_credential(
            old_ref,
            "custom",
            "openai_chat",
            "https://new-relay.example/v1?api-version=2026-08-10",
        )
    )

    assert replacement_ref != old_ref
    assert state_store.credential_metadata(old_ref)["base_url"] == ("https://old-relay.example/v1")
    assert state_store.credential_metadata(replacement_ref)["base_url"] == (
        "https://new-relay.example/v1?api-version=2026-08-10"
    )
    assert state_store.read_api_key(old_ref) == "test-retarget-secret"
    assert state_store.read_api_key(replacement_ref) == "test-retarget-secret"
    assert asyncio.run(adapter.credential_supports_refresh(old_ref)) is False
    assert asyncio.run(adapter.credential_supports_refresh(oauth_ref)) is True


def test_source_observation_reduces_the_order_at_the_first_authenticated_proof(
    tmp_path: Path,
) -> None:
    base_url = "https://relay.example/v1?api-version=2026-07-23"
    state_store = EngineStateStore(tmp_path / "engine-state")
    credential_ref = state_store.store_api_key(
        "test-observation-key",
        vendor="custom",
        protocol=SOURCE_PROTOCOLS[-1],
        base_url=base_url,
    )
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )
    unsupported = EngineClientError(
        "unsupported protocol path",
        status_code=404,
    )
    proven_accepted = _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
    )
    proven_rejected = _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.REJECTED,
    )
    proven_unknown = _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )
    generic_request_accepted = _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
        shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
    )
    unproven_unknown = _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )
    excluded_404 = _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
        shape=_ProtocolObservationShape.HTTP_404,
    )
    non_json = _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
        shape=_ProtocolObservationShape.NON_JSON,
    )

    hinted_order = tuple(reversed(SOURCE_PROTOCOLS))

    async def every_candidate_is_supported(**_kwargs) -> _ProtocolEvidence:
        return proven_accepted

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=every_candidate_is_supported),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="upstream-model"),)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
                adapter.observe_source(
                    "custom",
                    base_url,
                    credential_ref,
                    hinted_order,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == hinted_order[0]
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == [hinted_order[0]]
    assert inventory_probe.await_args.kwargs["protocol"] == hinted_order[0]

    async def indistinguishable_response(**_kwargs) -> _ProtocolEvidence:
        return unproven_unknown

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=indistinguishable_response),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="upstream-model"),)),
        ) as inventory_probe,
    ):
        ambiguous = asyncio.run(
                adapter.observe_source(
                    "custom",
                    base_url,
                    credential_ref,
                    hinted_order,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is None
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(hinted_order)
    inventory_probe.assert_not_awaited()

    pairwise_order = ("openai_chat", "openai_responses")

    async def openai_pairwise_elimination(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "openai_chat":
            return generic_request_accepted
        if kwargs["protocol"] == "openai_responses":
            return excluded_404
        raise AssertionError(kwargs["protocol"])

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=openai_pairwise_elimination),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="deepseek-chat"),)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
                adapter.observe_source(
                    "custom",
                    base_url,
                    credential_ref,
                    pairwise_order,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == "openai_chat"
    assert observed.model_ids == ("deepseek-chat",)
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(pairwise_order)
    assert inventory_probe.await_args.kwargs["protocol"] == "openai_chat"

    openai_family = {"openai_responses", "openai_chat"}

    async def indistinguishable_openai_family(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] in openai_family:
            return generic_request_accepted
        return unproven_unknown

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=indistinguishable_openai_family),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        ambiguous = asyncio.run(
                adapter.observe_source(
                    "custom",
                    base_url,
                    credential_ref,
                    SOURCE_PROTOCOLS,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is True
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()

    async def structured_unknown_sibling(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "openai_chat":
            return generic_request_accepted
        if kwargs["protocol"] == "openai_responses":
            return unproven_unknown
        return unproven_unknown

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=structured_unknown_sibling),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        ambiguous = asyncio.run(
                adapter.observe_source(
                    "custom",
                    base_url,
                    credential_ref,
                    SOURCE_PROTOCOLS,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is True
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()

    async def anthropic_server_error_blocks_openai_pairwise(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            return proven_unknown
        if kwargs["protocol"] == "openai_responses":
            return generic_request_accepted
        if kwargs["protocol"] == "openai_chat":
            return excluded_404
        raise AssertionError(kwargs["protocol"])

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=anthropic_server_error_blocks_openai_pairwise),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        ambiguous = asyncio.run(
                adapter.observe_source(
                    "custom",
                    base_url,
                    credential_ref,
                    SOURCE_PROTOCOLS,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is True
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()

    timeout = EngineClientError("protocol observation timed out", error_type="timeout")

    async def anthropic_timeout_blocks_openai_pairwise(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            raise timeout
        if kwargs["protocol"] == "openai_responses":
            return generic_request_accepted
        if kwargs["protocol"] == "openai_chat":
            return excluded_404
        raise AssertionError(kwargs["protocol"])

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=anthropic_timeout_blocks_openai_pairwise),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        ambiguous = asyncio.run(
                adapter.observe_source(
                    "custom",
                    base_url,
                    credential_ref,
                    SOURCE_PROTOCOLS,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is True
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()

    async def anthropic_competes_with_openai_pairwise(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            return generic_request_accepted
        if kwargs["protocol"] == "openai_responses":
            return generic_request_accepted
        if kwargs["protocol"] == "openai_chat":
            return excluded_404
        raise AssertionError(kwargs["protocol"])

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=anthropic_competes_with_openai_pairwise),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        ambiguous = asyncio.run(
                adapter.observe_source(
                    "custom",
                    base_url,
                    credential_ref,
                    SOURCE_PROTOCOLS,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is True
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()

    unproven_rejected = _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.REJECTED,
    )

    async def accepted_then_rejected_without_proof(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            return generic_request_accepted
        if kwargs["protocol"] == "openai_responses":
            return unproven_rejected
        if kwargs["protocol"] == "openai_chat":
            return unproven_unknown
        raise AssertionError(kwargs["protocol"])

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=accepted_then_rejected_without_proof),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        ambiguous = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is True
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()

    async def rejected_anthropic_allows_openai_pairwise(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            return unproven_rejected
        if kwargs["protocol"] == "openai_responses":
            return excluded_404
        if kwargs["protocol"] == "openai_chat":
            return generic_request_accepted
        raise AssertionError(kwargs["protocol"])

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=rejected_anthropic_allows_openai_pairwise),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="chat-only-model"),)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == "openai_chat"
    assert observed.authenticated is True
    assert observed.model_ids == ("chat-only-model",)
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    assert inventory_probe.await_args.kwargs["protocol"] == "openai_chat"

    transient_non_json = _parse_protocol_authenticated_evidence(
        "openai_chat",
        502,
        "<html>bad gateway</html>",
    )
    assert transient_non_json == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )

    async def transient_non_json_sibling_blocks_pairwise(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            return excluded_404
        if kwargs["protocol"] == "openai_responses":
            return generic_request_accepted
        if kwargs["protocol"] == "openai_chat":
            return transient_non_json
        raise AssertionError(kwargs["protocol"])

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=transient_non_json_sibling_blocks_pairwise),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        ambiguous = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is True
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()

    async def anthropic_wrapperless_then_absent_openai(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            return generic_request_accepted
        if kwargs["protocol"] == "openai_responses":
            return excluded_404
        if kwargs["protocol"] == "openai_chat":
            return non_json
        raise AssertionError(kwargs["protocol"])

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=anthropic_wrapperless_then_absent_openai),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="relay-model"),)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == "anthropic"
    assert observed.model_ids == ("relay-model",)
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    assert inventory_probe.await_args.kwargs["protocol"] == "anthropic"

    async def anthropic_wrapperless_with_rejected_openai(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            return generic_request_accepted
        if kwargs["protocol"] in openai_family:
            return unproven_rejected
        raise AssertionError(kwargs["protocol"])

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=anthropic_wrapperless_with_rejected_openai),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(
                return_value=(DiscoveredModel(id="anthropic-relay-model"),)
            ),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == "anthropic"
    assert observed.authenticated is True
    assert observed.model_ids == ("anthropic-relay-model",)
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    assert inventory_probe.await_args.kwargs["protocol"] == "anthropic"

    credential_param_rejected = _parse_protocol_authenticated_evidence(
        "openai_responses",
        400,
        json.dumps(
            {
                "error": {
                    "type": "invalid_request_error",
                    "param": "api_key",
                }
            }
        ),
    )
    assert credential_param_rejected == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.REJECTED,
    )

    async def credential_param_rejection_blocks_pairwise_proof(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            return unproven_rejected
        if kwargs["protocol"] == "openai_responses":
            return credential_param_rejected
        if kwargs["protocol"] == "openai_chat":
            return excluded_404
        raise AssertionError(kwargs["protocol"])

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=credential_param_rejection_blocks_pairwise_proof),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        rejected = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert rejected.outcome.value == "authentication_failed"
    assert rejected.protocol is None
    assert rejected.authenticated is False
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(return_value=generic_request_accepted),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                ("anthropic",),
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == "anthropic"
    assert observed.authenticated is True
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == ["anthropic"]
    assert inventory_probe.await_args.kwargs["protocol"] == "anthropic"

    async def shaped_server_failure(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == "anthropic":
            body = {"type": "error", "error": {"type": "api_error"}}
        else:
            body = {"error": {"type": "server_error"}}
        evidence = _parse_protocol_authenticated_evidence(
            kwargs["protocol"],
            500,
            json.dumps(body),
        )
        assert evidence == proven_unknown
        return evidence

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=shaped_server_failure),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="unverified-model"),)),
        ) as inventory_probe,
    ):
        upstream_error = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert upstream_error.outcome.value == "adapter_error"
    assert upstream_error.reachable is True
    assert upstream_error.authenticated is None
    assert upstream_error.protocol is None
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()
    _assert_valid(
        "observation-result.schema.json",
        {
            "contract_version": 7,
            "outcome": "adapter_error",
            "reachable": True,
            "authenticated": "unknown",
            "protocol": None,
            "discovery": "not_attempted",
            "models": [],
            "model_metadata": [],
        },
    )

    proved_protocol = SOURCE_PROTOCOLS[1]

    async def later_protocol_probe(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] != proved_protocol:
            raise unsupported
        return proven_accepted

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=later_protocol_probe),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="upstream-model"),)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == proved_protocol
    assert observed.model_ids == ("upstream-model",)
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS[:2])
    assert inventory_probe.await_args.kwargs["protocol"] == proved_protocol
    assert inventory_probe.await_args.kwargs["base_url"] == base_url

    rejected_protocol = SOURCE_PROTOCOLS[0]
    accepted_protocol = SOURCE_PROTOCOLS[1]

    async def rejected_then_authenticated(**kwargs) -> _ProtocolEvidence:
        if kwargs["protocol"] == rejected_protocol:
            return proven_rejected
        return proven_accepted

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=rejected_then_authenticated),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(
                return_value=(DiscoveredModel(id="later-protocol-model"),)
            ),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.authenticated is True
    assert observed.protocol == accepted_protocol
    assert observed.model_ids == ("later-protocol-model",)
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == [
        rejected_protocol,
        accepted_protocol,
    ]
    assert inventory_probe.await_args.kwargs["protocol"] == accepted_protocol

    async def reject_every_candidate(**kwargs) -> _ProtocolEvidence:
        return _parse_protocol_authenticated_evidence(
            kwargs["protocol"],
            401,
            json.dumps(
                {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                        "param": None,
                    }
                }
            ),
        )

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=reject_every_candidate),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="unreachable-model"),)),
        ) as inventory_probe,
    ):
        rejected = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert rejected.outcome.value == "authentication_failed"
    assert rejected.reachable is True
    assert rejected.authenticated is False
    assert rejected.protocol is None
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()


def test_source_observation_accepts_catalog_pin_and_custom_declaration_without_shape_proof(
    tmp_path: Path,
) -> None:
    state_store = EngineStateStore(tmp_path / "engine-state")
    catalog_ref = state_store.store_api_key(
        "test-deepseek-key",
        vendor="deepseek",
        protocol="openai_chat",
        base_url=None,
    )
    custom_ref = state_store.store_api_key(
        "test-custom-key",
        vendor="custom",
        protocol="openai_chat",
        base_url="https://api.deepseek.com",
    )
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )
    generic_request_accepted = _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
        shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
    )

    async def identical_three_path_shape(**_kwargs) -> _ProtocolEvidence:
        return generic_request_accepted

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=identical_three_path_shape),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="deepseek-chat"),)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "deepseek",
                None,
                catalog_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == "openai_chat"
    assert observed.model_ids == ("deepseek-chat",)
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    assert inventory_probe.await_args.kwargs["protocol"] == "openai_chat"

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=identical_three_path_shape),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        ambiguous = asyncio.run(
            adapter.observe_source(
                "custom",
                "https://api.deepseek.com",
                custom_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is True
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(SOURCE_PROTOCOLS)
    inventory_probe.assert_not_awaited()

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(side_effect=identical_three_path_shape),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="deepseek-chat"),)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "custom",
                "https://api.deepseek.com",
                custom_ref,
                ("openai_chat",),
            )
        )

    assert observed.outcome.value == "observed"
    assert observed.protocol == "openai_chat"
    assert observed.model_ids == ("deepseek-chat",)
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == ["openai_chat"]
    assert inventory_probe.await_args.kwargs["protocol"] == "openai_chat"


def test_qwen_catalog_pin_observation_accepts_wrapperless_authenticated_validation_response(
    tmp_path: Path,
) -> None:
    state_store = EngineStateStore(tmp_path / "engine-state")
    credential_ref = state_store.store_api_key(
        "test-qwen-key",
        vendor="qwen",
        protocol="openai_chat",
        base_url=None,
    )
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )

    async def scenario() -> tuple[list[str], object, dict[str, object]]:
        requests: list[str] = []

        async def capture_probe(request: web.Request) -> web.Response:
            requests.append(request.path)
            if request.path == _PROTOCOL_OBSERVATION_TAXONOMY["openai_chat"].request_path:
                return web.json_response(
                    {
                        "code": "InvalidParameter",
                        "message": "messages is required",
                    },
                    status=400,
                )
            return web.json_response({}, status=404)

        app = web.Application()
        app.router.add_post("/{tail:.*}", capture_probe)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        try:
            with (
                patch.dict(
                    "vibe.model_hub_runtime.adapter._OFFICIAL_BASE_URLS",
                    {"qwen": origin},
                    clear=False,
                ),
                patch(
                    "vibe.model_hub_runtime.adapter.probe_models",
                    new=AsyncMock(return_value=(DiscoveredModel(id="qwen-plus"),)),
                ) as inventory_probe,
            ):
                observed = await adapter.observe_source(
                    "qwen",
                    None,
                    credential_ref,
                    SOURCE_PROTOCOLS,
                )
                assert inventory_probe.await_args is not None
                inventory_kwargs = dict(inventory_probe.await_args.kwargs)
        finally:
            await runner.cleanup()
        return requests, observed, inventory_kwargs

    requests, observed, inventory_kwargs = asyncio.run(scenario())

    assert observed.outcome.value == "observed"
    assert observed.protocol == "openai_chat"
    assert observed.authenticated is True
    assert observed.model_ids == ("qwen-plus",)
    assert requests == [
        _PROTOCOL_OBSERVATION_TAXONOMY[protocol].request_path
        for protocol in SOURCE_PROTOCOLS
    ]
    assert inventory_kwargs["vendor"] == "qwen"
    assert inventory_kwargs["protocol"] == "openai_chat"
    assert inventory_kwargs["base_url"] is None


def test_openrouter_catalog_pin_observation_accepts_nested_numeric_authenticated_validation_response(
    tmp_path: Path,
) -> None:
    state_store = EngineStateStore(tmp_path / "engine-state")
    credential_ref = state_store.store_api_key(
        "test-openrouter-key",
        vendor="openrouter",
        protocol="openai_chat",
        base_url=None,
    )
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )

    async def scenario() -> tuple[list[str], object, dict[str, object]]:
        requests: list[str] = []

        async def capture_probe(request: web.Request) -> web.Response:
            requests.append(request.path)
            if request.path == _PROTOCOL_OBSERVATION_TAXONOMY["openai_chat"].request_path:
                return web.json_response(
                    {
                        "error": {
                            "code": 400,
                            "message": "messages is required",
                        }
                    },
                    status=400,
                )
            return web.json_response({}, status=404)

        app = web.Application()
        app.router.add_post("/{tail:.*}", capture_probe)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        try:
            with (
                patch.dict(
                    "vibe.model_hub_runtime.adapter._OFFICIAL_BASE_URLS",
                    {"openrouter": origin},
                    clear=False,
                ),
                patch(
                    "vibe.model_hub_runtime.adapter.probe_models",
                    new=AsyncMock(return_value=(DiscoveredModel(id="openrouter/auto"),)),
                ) as inventory_probe,
            ):
                observed = await adapter.observe_source(
                    "openrouter",
                    None,
                    credential_ref,
                    SOURCE_PROTOCOLS,
                )
                assert inventory_probe.await_args is not None
                inventory_kwargs = dict(inventory_probe.await_args.kwargs)
        finally:
            await runner.cleanup()
        return requests, observed, inventory_kwargs

    requests, observed, inventory_kwargs = asyncio.run(scenario())

    assert observed.outcome.value == "observed"
    assert observed.protocol == "openai_chat"
    assert observed.authenticated is True
    assert observed.model_ids == ("openrouter/auto",)
    assert requests == [
        _PROTOCOL_OBSERVATION_TAXONOMY[protocol].request_path
        for protocol in SOURCE_PROTOCOLS
    ]
    assert inventory_kwargs["vendor"] == "openrouter"
    assert inventory_kwargs["protocol"] == "openai_chat"
    assert inventory_kwargs["base_url"] is None


def _catalog_owner_status_body(vendor: str, status: int) -> dict[str, object]:
    if status == 401 and vendor == "openrouter":
        return {
            "error": {
                "code": 401,
                "message": "Credentials are invalid",
            }
        }
    if status == 400 and vendor == "zhipuai":
        return {
            "error": {
                "code": "1214",
                "message": "messages is required",
            }
        }
    if status == 400 and vendor == "openrouter":
        return {
            "error": {
                "code": 400,
                "message": "invalid API key",
            }
        }
    return {
        "error": {
            "code": f"{vendor}-{status}",
            "message": f"{vendor} response body",
        }
    }


def _run_catalog_pin_observation(
    tmp_path: Path,
    *,
    vendor: str,
    protocol: str,
    status: int,
    body: dict[str, object],
) -> tuple[list[str], object, dict[str, object] | None]:
    state_store = EngineStateStore(tmp_path / f"engine-state-{vendor}-{status}")
    credential_ref = state_store.store_api_key(
        f"test-{vendor}-{status}",
        vendor=vendor,
        protocol=protocol,
        base_url=None,
    )
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )

    async def scenario() -> tuple[list[str], object, dict[str, object] | None]:
        requests: list[str] = []

        async def capture_probe(request: web.Request) -> web.Response:
            requests.append(request.path)
            return web.json_response(body, status=status)

        app = web.Application()
        app.router.add_post("/{tail:.*}", capture_probe)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        try:
            with (
                patch.dict(
                    "vibe.model_hub_runtime.adapter._OFFICIAL_BASE_URLS",
                    {vendor: origin},
                    clear=False,
                ),
                patch(
                    "vibe.model_hub_runtime.adapter.probe_models",
                    new=AsyncMock(return_value=(DiscoveredModel(id=f"{vendor}/auto"),)),
                ) as inventory_probe,
            ):
                observed = await adapter.observe_source(
                    vendor,
                    None,
                    credential_ref,
                    (protocol,),
                )
                inventory_kwargs = (
                    dict(inventory_probe.await_args.kwargs)
                    if inventory_probe.await_args is not None
                    else None
                )
        finally:
            await runner.cleanup()
        return requests, observed, inventory_kwargs

    return asyncio.run(scenario())


@pytest.mark.parametrize(("vendor", "protocol"), CATALOG_API_KEY_VENDOR_PROTOCOL_CASES)
def test_catalog_pin_observation_accepts_any_nonempty_json_400_response(
    tmp_path: Path,
    vendor: str,
    protocol: str,
) -> None:
    requests, observed, inventory_kwargs = _run_catalog_pin_observation(
        tmp_path,
        vendor=vendor,
        protocol=protocol,
        status=400,
        body=_catalog_owner_status_body(vendor, 400),
    )

    assert observed.outcome.value == "observed"
    assert observed.protocol == protocol
    assert observed.authenticated is True
    assert observed.model_ids == (f"{vendor}/auto",)
    assert requests == [_PROTOCOL_OBSERVATION_TAXONOMY[protocol].request_path]
    assert inventory_kwargs is not None
    assert inventory_kwargs["vendor"] == vendor
    assert inventory_kwargs["protocol"] == protocol
    assert inventory_kwargs["base_url"] is None


@pytest.mark.parametrize(("vendor", "protocol"), CATALOG_API_KEY_VENDOR_PROTOCOL_CASES)
def test_catalog_pin_observation_rejects_any_json_401_response(
    tmp_path: Path,
    vendor: str,
    protocol: str,
) -> None:
    requests, observed, inventory_kwargs = _run_catalog_pin_observation(
        tmp_path,
        vendor=vendor,
        protocol=protocol,
        status=401,
        body=_catalog_owner_status_body(vendor, 401),
    )

    assert observed.outcome.value == "authentication_failed"
    assert observed.protocol is None
    assert observed.authenticated is False
    assert observed.model_ids == ()
    assert requests == [_PROTOCOL_OBSERVATION_TAXONOMY[protocol].request_path]
    assert inventory_kwargs is None


def test_custom_auto_numeric_auth_failure_message_stays_authentication_failed(
    tmp_path: Path,
) -> None:
    state_store = EngineStateStore(tmp_path / "engine-state-custom-auto")
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )

    async def scenario() -> tuple[list[str], object]:
        requests: list[str] = []

        async def capture_probe(request: web.Request) -> web.Response:
            requests.append(request.path)
            return web.json_response(
                {
                    "error": {
                        "code": 400,
                        "message": "invalid API key",
                    }
                },
                status=400,
            )

        app = web.Application()
        app.router.add_post("/{tail:.*}", capture_probe)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        credential_ref = state_store.store_api_key(
            "test-custom-auto-invalid-key",
            vendor="custom",
            protocol="openai_chat",
            base_url=origin,
        )
        try:
            with patch(
                "vibe.model_hub_runtime.adapter.probe_models",
                new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
            ) as inventory_probe:
                observed = await adapter.observe_source(
                    "custom",
                    origin,
                    credential_ref,
                    SOURCE_PROTOCOLS,
                )
                inventory_probe.assert_not_awaited()
        finally:
            await runner.cleanup()
        return requests, observed

    requests, observed = asyncio.run(scenario())

    assert observed.outcome.value == "authentication_failed"
    assert observed.protocol is None
    assert observed.authenticated is False
    assert observed.model_ids == ()
    assert requests == [
        _PROTOCOL_OBSERVATION_TAXONOMY[protocol].request_path
        for protocol in SOURCE_PROTOCOLS
    ]


@pytest.mark.parametrize(
    ("vendor", "base_url", "credential_vendor", "credential_protocol", "protocol_order"),
    [
        ("deepseek", None, "deepseek", "openai_chat", ("openai_chat",)),
        (
            "custom",
            "https://api.deepseek.com",
            "custom",
            "openai_chat",
            ("openai_chat",),
        ),
    ],
    ids=("catalog_pin", "custom_declared"),
)
def test_source_observation_catalog_pin_and_declaration_still_require_authentication(
    tmp_path: Path,
    vendor: str,
    base_url: str | None,
    credential_vendor: str,
    credential_protocol: str,
    protocol_order: tuple[str, ...],
) -> None:
    state_store = EngineStateStore(tmp_path / "engine-state")
    credential_ref = state_store.store_api_key(
        "test-observation-key",
        vendor=credential_vendor,
        protocol=credential_protocol,
        base_url=base_url,
    )
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )
    rejected = _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.REJECTED,
    )

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(return_value=rejected),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as inventory_probe,
    ):
        failed = asyncio.run(
            adapter.observe_source(
                vendor,
                base_url,
                credential_ref,
                protocol_order,
            )
        )

    assert failed.outcome.value == "authentication_failed"
    assert failed.protocol is None
    assert failed.authenticated is False
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == list(protocol_order)
    inventory_probe.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "initial_authentication", "expected_outcome", "expected_authenticated"),
    [
        (400, _AuthenticationEvidence.REJECTED, "observed", True),
        (401, _AuthenticationEvidence.ACCEPTED, "authentication_failed", False),
    ],
    ids=("request_error_accepts", "auth_error_rejects"),
)
def test_custom_declared_observation_uses_owner_status_before_parser_verdict(
    tmp_path: Path,
    status: int,
    initial_authentication: _AuthenticationEvidence,
    expected_outcome: str,
    expected_authenticated: bool,
) -> None:
    base_url = "https://relay.example/v1"
    state_store = EngineStateStore(tmp_path / f"engine-state-custom-declared-{status}")
    credential_ref = state_store.store_api_key(
        f"test-custom-declared-{status}",
        vendor="custom",
        protocol="openai_chat",
        base_url=base_url,
    )
    adapter = CLIProxyEngineAdapter(
        supervisor=Mock(),
        state_store=state_store,
    )
    parser_evidence = _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=initial_authentication,
        shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
        status=status,
    )

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_protocol_response",
            new=AsyncMock(return_value=parser_evidence),
        ) as protocol_probe,
        patch(
            "vibe.model_hub_runtime.adapter.probe_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="declared-model"),)),
        ) as inventory_probe,
    ):
        observed = asyncio.run(
            adapter.observe_source(
                "custom",
                base_url,
                credential_ref,
                ("openai_chat",),
            )
        )

    assert observed.outcome.value == expected_outcome
    assert observed.authenticated is expected_authenticated
    assert [call.kwargs["protocol"] for call in protocol_probe.await_args_list] == ["openai_chat"]
    if expected_outcome == "observed":
        assert observed.protocol == "openai_chat"
        assert observed.model_ids == ("declared-model",)
        assert inventory_probe.await_args is not None
        assert inventory_probe.await_args.kwargs["protocol"] == "openai_chat"
    else:
        assert observed.protocol is None
        inventory_probe.assert_not_awaited()


def test_oauth_observation_uses_the_bound_auth_index_and_requires_response_proof(
    tmp_path: Path,
) -> None:
    state_store = EngineStateStore(tmp_path / "engine-state")
    credential_ref = state_store.bind_oauth_credential(
        "src_oauthproof",
        "openai",
        "codex-test.json",
    )
    client = Mock()
    api_calls: list[dict] = []

    def management_request(method, path, *, query=None, payload=None):
        if path == "/auth-files":
            return {
                "files": [
                    {
                        "id": "codex-test",
                        "auth_index": "auth-index-test",
                        "name": "codex-test.json",
                        "provider": "codex",
                        "id_token": {"chatgpt_account_id": "account-test"},
                    }
                ]
            }
        if path == "/api-call":
            api_calls.append(payload)
            return {
                "status_code": 400,
                "header": {},
                "body": json.dumps(
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "param": "input",
                        }
                    }
                ),
            }
        if path == "/auth-files/models":
            assert query == {"name": "codex-test.json"}
            return {"models": [{"id": "gpt-5.6"}]}
        raise AssertionError((method, path))

    client.management_request.side_effect = management_request
    supervisor = Mock()
    supervisor.client.return_value = client
    adapter = CLIProxyEngineAdapter(
        supervisor=supervisor,
        state_store=state_store,
    )

    observed = asyncio.run(
        adapter.observe_source(
            "openai",
            None,
            credential_ref,
            SOURCE_PROTOCOLS,
        )
    )

    assert observed.outcome.value == "observed"
    assert observed.protocol == "openai_responses"
    assert observed.model_ids == ("gpt-5.6",)
    assert len(api_calls) == 1
    assert api_calls[0]["auth_index"] == "auth-index-test"
    assert api_calls[0]["header"]["Chatgpt-Account-Id"] == "account-test"
    assert api_calls[0]["url"].endswith("/responses")
    assert json.loads(api_calls[0]["data"]) == dict(
        _PROTOCOL_OBSERVATION_TAXONOMY["openai_responses"].request_body
    )

    def ambiguous_management_request(method, path, *, query=None, payload=None):
        if path == "/auth-files":
            return management_request(method, path, query=query, payload=payload)
        if path == "/api-call":
            return {"status_code": 200, "header": {}, "body": '{"ok":true}'}
        raise AssertionError((method, path))

    client.management_request.side_effect = ambiguous_management_request
    ambiguous = asyncio.run(
        adapter.observe_source(
            "openai",
            None,
            credential_ref,
            SOURCE_PROTOCOLS,
        )
    )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is None


def test_oauth_observation_does_not_use_api_key_catalog_pins_without_shape_proof(
    tmp_path: Path,
) -> None:
    state_store = EngineStateStore(tmp_path / "engine-state")
    credential_ref = state_store.bind_oauth_credential(
        "src_oauthcatalog",
        "openai",
        "codex-test.json",
    )
    client = Mock()

    def management_request(method, path, *, query=None, payload=None):
        if path == "/auth-files":
            return {
                "files": [
                    {
                        "id": "codex-test",
                        "auth_index": "auth-index-test",
                        "name": "codex-test.json",
                        "provider": "codex",
                    }
                ]
            }
        raise AssertionError((method, path))

    client.management_request.side_effect = management_request
    supervisor = Mock()
    supervisor.client.return_value = client
    adapter = CLIProxyEngineAdapter(
        supervisor=supervisor,
        state_store=state_store,
    )
    accepted_unproven = _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
        shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
    )

    with (
        patch(
            "vibe.model_hub_runtime.adapter._probe_oauth_protocol_response",
            return_value=accepted_unproven,
        ) as oauth_probe,
        patch.object(
            adapter,
            "discover_models",
            new=AsyncMock(return_value=(DiscoveredModel(id="should-not-discover"),)),
        ) as discover_models,
    ):
        ambiguous = asyncio.run(
            adapter.observe_source(
                "openai",
                None,
                credential_ref,
                SOURCE_PROTOCOLS,
            )
        )

    assert ambiguous.outcome.value == "ambiguous"
    assert ambiguous.protocol is None
    assert ambiguous.authenticated is True
    discover_models.assert_not_awaited()
    assert [call.kwargs["protocol"] for call in oauth_probe.call_args_list] == list(SOURCE_PROTOCOLS)


DEEPSEEK_AUTHENTICATION_ERROR_PAYLOAD = {
    "error": {
        "message": "Authentication Fails, Your api key: **** is invalid",
        "type": "authentication_error",
        "param": None,
        "code": "invalid_request_error",
    }
}
DEEPSEEK_MODEL_NOT_FOUND_PAYLOAD = {
    "error": {
        "type": "invalid_request_error",
        "param": None,
        "code": "model_not_found",
    }
}
ANTHROPIC_RELAY_REQUEST_ERROR_PAYLOAD = {
    "error": {
        "message": "model is required",
        "type": "invalid_request_error",
    }
}


@pytest.mark.parametrize(
    ("protocol", "success_body", "request_error_body", "auth_error_body"),
    [
        (
            "anthropic",
            {"type": "message"},
            {"type": "error", "error": {"type": "invalid_request_error"}},
            {"type": "error", "error": {"type": "authentication_error"}},
        ),
        (
            "openai_responses",
            {"object": "response"},
            {"error": {"type": "invalid_request_error", "param": "input"}},
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                    "param": "input",
                }
            },
        ),
        (
            "openai_chat",
            {"object": "chat.completion"},
            {"error": {"type": "invalid_request_error", "param": "messages"}},
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                    "param": "messages",
                }
            },
        ),
    ],
)
def test_protocol_evidence_parser_requires_candidate_specific_response_shapes(
    protocol: str,
    success_body: dict,
    request_error_body: dict,
    auth_error_body: dict,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        200,
        json.dumps(success_body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
    )
    assert _parse_protocol_authenticated_evidence(
        protocol,
        400,
        json.dumps(request_error_body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
    )
    assert _parse_protocol_authenticated_evidence(
        protocol,
        401,
        json.dumps(auth_error_body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.REJECTED,
    )
    assert _parse_protocol_authenticated_evidence(
        protocol,
        400,
        "{}",
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
        shape=_ProtocolObservationShape.UNSTRUCTURED,
    )


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            400,
            ANTHROPIC_RELAY_REQUEST_ERROR_PAYLOAD,
            _ProtocolEvidence(
                protocol=_ProtocolProof.UNPROVEN,
                authentication=_AuthenticationEvidence.ACCEPTED,
                shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
            ),
        ),
        (
            401,
            {"error": {"type": "authentication_error"}},
            _ProtocolEvidence(
                protocol=_ProtocolProof.UNPROVEN,
                authentication=_AuthenticationEvidence.REJECTED,
            ),
        ),
        (
            400,
            {"error": {"type": "future_error"}},
            _ProtocolEvidence(
                protocol=_ProtocolProof.UNPROVEN,
                authentication=_AuthenticationEvidence.UNKNOWN,
            ),
        ),
    ],
)
def test_anthropic_relay_openai_style_error_wrapper_preserves_canonical_semantics(
    status: int,
    body: dict,
    expected: _ProtocolEvidence,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        "anthropic",
        status,
        json.dumps(body),
    ) == expected


@pytest.mark.parametrize(
    ("protocol", "status", "body"),
    [
        (
            "anthropic",
            500,
            {"type": "error", "error": {"type": "api_error"}},
        ),
        (
            "openai_responses",
            500,
            {"error": {"type": "server_error"}},
        ),
        (
            "openai_chat",
            500,
            {"error": {"type": "server_error"}},
        ),
        (
            "anthropic",
            429,
            {"type": "error", "error": {"type": "rate_limit_error"}},
        ),
        (
            "openai_responses",
            429,
            {"error": {"type": "rate_limit_exceeded"}},
        ),
        (
            "openai_chat",
            429,
            {"error": {"type": "rate_limit_exceeded"}},
        ),
        (
            "anthropic",
            400,
            {"type": "error", "error": {"type": "future_error"}},
        ),
        (
            "openai_responses",
            400,
            {"error": {"type": "future_error", "param": "input"}},
        ),
        (
            "openai_chat",
            400,
            {"error": {"type": "future_error", "param": "messages"}},
        ),
    ],
)
def test_protocol_evidence_table_defaults_shaped_non_auth_rows_to_unknown(
    protocol: str,
    status: int,
    body: dict,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        status,
        json.dumps(body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )


@pytest.mark.parametrize(
    ("protocol", "status", "body"),
    [
        (
            "anthropic",
            404,
            {"type": "error", "error": {"type": "not_found_error"}},
        ),
    ],
)
def test_anthropic_protocol_evidence_table_accepts_authenticated_model_errors(
    protocol: str,
    status: int,
    body: dict,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        status,
        json.dumps(body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.PROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
    )


@pytest.mark.parametrize(
    ("protocol", "status", "body"),
    [
        (
            "openai_responses",
            404,
            {"error": {"type": "invalid_request_error", "code": "model_not_found"}},
        ),
        (
            "openai_chat",
            404,
            {"error": {"type": "invalid_request_error", "code": "model_not_found"}},
        ),
        (
            "openai_responses",
            400,
            DEEPSEEK_MODEL_NOT_FOUND_PAYLOAD,
        ),
        (
            "openai_chat",
            400,
            DEEPSEEK_MODEL_NOT_FOUND_PAYLOAD,
        ),
        (
            "openai_responses",
            422,
            DEEPSEEK_MODEL_NOT_FOUND_PAYLOAD,
        ),
        (
            "openai_chat",
            422,
            DEEPSEEK_MODEL_NOT_FOUND_PAYLOAD,
        ),
    ],
)
def test_openai_model_errors_without_family_param_record_accepted_but_unproven_evidence(
    protocol: str,
    status: int,
    body: dict,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        status,
        json.dumps(body),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
        shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
    )


@pytest.mark.parametrize("protocol", ("openai_responses", "openai_chat"))
def test_openai_request_error_without_family_param_records_accepted_but_unproven_evidence(
    protocol: str,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        400,
        json.dumps(
            {
                "error": {
                    "type": "invalid_request_error",
                    "param": None,
                }
            }
        ),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
        shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
    )


def test_qwen_wrapperless_invalid_parameter_request_error_counts_as_authenticated_openai_chat() -> None:
    assert _parse_protocol_authenticated_evidence(
        "openai_chat",
        400,
        json.dumps(
            {
                "code": "InvalidParameter",
                "message": "messages is required",
            }
        ),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.ACCEPTED,
        shape=_ProtocolObservationShape.GENERIC_REQUEST_ERROR,
    )


@pytest.mark.parametrize("protocol", ("openai_responses", "openai_chat"))
def test_openai_family_nested_numeric_request_error_stays_unknown_before_owner_override(
    protocol: str,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        400,
        json.dumps(
            {
                "error": {
                    "code": 400,
                    "message": "missing required parameter",
                }
            }
        ),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )
    assert _parse_protocol_authenticated_evidence(
        protocol,
        400,
        json.dumps(
            {
                "error": {
                    "code": 400,
                    "message": "missing required parameter",
                }
            }
        ),
        vendor="openrouter",
        request_root="https://openrouter.ai/api/v1",
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )


@pytest.mark.parametrize("protocol", ("openai_responses", "openai_chat"))
def test_openai_family_nested_numeric_auth_failure_message_is_rejected(
    protocol: str,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        400,
        json.dumps(
            {
                "error": {
                    "code": 400,
                    "message": "invalid API key",
                }
            }
        ),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.REJECTED,
    )


@pytest.mark.parametrize("protocol", SOURCE_PROTOCOLS)
@pytest.mark.parametrize("status", (400, 422))
def test_request_error_with_credential_param_is_rejected(
    protocol: str,
    status: int,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        status,
        json.dumps(
            {
                "error": {
                    "type": "invalid_request_error",
                    "param": "api_key",
                }
            }
        ),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.REJECTED,
    )


def test_shared_openai_authentication_rejection_does_not_prove_a_protocol() -> None:
    body = json.dumps(
        {
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_api_key",
                "param": None,
            }
        }
    )

    for protocol in ("openai_responses", "openai_chat"):
        assert _parse_protocol_authenticated_evidence(
            protocol,
            401,
            body,
        ) == _ProtocolEvidence(
            protocol=_ProtocolProof.UNPROVEN,
            authentication=_AuthenticationEvidence.REJECTED,
        )


@pytest.mark.parametrize("protocol", ("openai_responses", "openai_chat"))
def test_deepseek_authentication_rejection_with_null_param_stays_unproven(
    protocol: str,
) -> None:
    assert _parse_protocol_authenticated_evidence(
        protocol,
        401,
        json.dumps(DEEPSEEK_AUTHENTICATION_ERROR_PAYLOAD),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.REJECTED,
    )


def test_structured_but_unrecognized_openai_error_is_not_exclusion_evidence() -> None:
    assert _parse_protocol_authenticated_evidence(
        "openai_chat",
        400,
        json.dumps({"error": {"type": "future_error"}}),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )


def test_request_error_404_and_non_json_responses_are_pairwise_exclusion_shapes() -> None:
    assert _parse_protocol_authenticated_evidence(
        "openai_responses",
        404,
        json.dumps({}),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
        shape=_ProtocolObservationShape.HTTP_404,
    )
    assert _parse_protocol_authenticated_evidence(
        "openai_responses",
        400,
        "<html>route not found</html>",
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
        shape=_ProtocolObservationShape.NON_JSON,
    )
    assert _parse_protocol_authenticated_evidence(
        "openai_responses",
        502,
        "<html>bad gateway</html>",
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )


def test_top_level_authentication_rejection_is_classified_without_forging_protocol() -> None:
    body = json.dumps({"code": "INVALID_API_KEY", "message": "Invalid API key"})

    for protocol in SOURCE_PROTOCOLS:
        assert _parse_protocol_authenticated_evidence(
            protocol,
            401,
            body,
        ) == _ProtocolEvidence(
            protocol=_ProtocolProof.UNPROVEN,
            authentication=_AuthenticationEvidence.REJECTED,
        )


def test_protocol_observation_consumers_cannot_classify_from_status_codes() -> None:
    module_path = Path(__file__).parents[1] / "vibe/model_hub_runtime/adapter.py"
    module_source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(module_source)
    assert _PROTOCOL_OBSERVATION_TAXONOMY.keys() == set(SOURCE_PROTOCOLS)
    assert _PROTOCOL_OBSERVATION_TAXONOMY["anthropic"].request_body == {
        "max_tokens": 0,
        "messages": [],
    }
    assert _PROTOCOL_OBSERVATION_TAXONOMY["openai_responses"].request_path != _PROTOCOL_OBSERVATION_TAXONOMY[
        "openai_chat"
    ].request_path
    assert _PROTOCOL_OBSERVATION_TAXONOMY["openai_responses"].request_body == {
        "model": "__avibe_model_hub_probe__"
    }
    assert _PROTOCOL_OBSERVATION_TAXONOMY["openai_chat"].request_body == {
        "model": "__avibe_model_hub_probe__"
    }
    consumers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_probe_protocol_response", "_probe_oauth_protocol_response"}
    }

    assert consumers.keys() == {
        "_probe_protocol_response",
        "_probe_oauth_protocol_response",
    }
    assert "json={}" not in module_source
    assert '"data": "{}"' not in module_source
    assert "_endpoint_for_protocol(" not in module_source
    for consumer in consumers.values():
        consumer_source = ast.get_source_segment(module_source, consumer)
        assert consumer_source is not None
        assert "_PROTOCOL_OBSERVATION_TAXONOMY" in consumer_source
        calls = [node for node in ast.walk(consumer) if isinstance(node, ast.Call)]
        assert any(
            isinstance(call.func, ast.Name) and call.func.id == "_parse_protocol_authenticated_evidence"
            for call in calls
        )
        compared_statuses = {
            constant.value
            for compare in ast.walk(consumer)
            if isinstance(compare, ast.Compare)
            for constant in ast.walk(compare)
            if isinstance(constant, ast.Constant)
            and isinstance(constant.value, int)
            and not isinstance(constant.value, bool)
        }
        assert not compared_statuses

    auth_branch_offenders = []
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name != "_parse_protocol_authenticated_evidence"
    ):
        for branch in (node for node in ast.walk(function) if isinstance(node, (ast.If, ast.IfExp))):
            condition = ast.unparse(branch.test)
            branch_source = ast.unparse(branch)
            if any(token in condition for token in ("status", "error")) and "_AuthenticationEvidence" in branch_source:
                auth_branch_offenders.append((function.name, condition))
    assert not auth_branch_offenders


def test_protocol_observation_preserves_query_on_each_distinct_upstream_path() -> None:
    query = "api-version=2026-07-23"

    async def scenario() -> list[tuple[str, str, dict]]:
        requests: list[tuple[str, str, dict]] = []

        async def reject_empty_probe(request: web.Request) -> web.Response:
            requests.append((request.path, request.query_string, await request.json()))
            return web.json_response({"error": {"type": "invalid_request"}}, status=400)

        app = web.Application()
        app.router.add_post("/{tail:.*}", reject_empty_probe)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        try:
            for protocol in SOURCE_PROTOCOLS:
                await _probe_protocol_response(
                    vendor="custom",
                    protocol=protocol,
                    base_url=f"http://127.0.0.1:{port}/v1?{query}",
                    secret="test-observation-key",
                )
        finally:
            await runner.cleanup()
        return requests

    requests = asyncio.run(scenario())
    paths = [path for path, _query, _body in requests]

    assert len(paths) == len(SOURCE_PROTOCOLS)
    assert len(set(paths)) == len(paths)
    assert {request_query for _path, request_query, _body in requests} == {query}
    assert [body for _path, _query, body in requests] == [
        dict(_PROTOCOL_OBSERVATION_TAXONOMY[protocol].request_body)
        for protocol in SOURCE_PROTOCOLS
    ]


def test_protocol_observation_adds_standard_v1_paths_to_a_bare_origin() -> None:
    async def scenario() -> list[str]:
        requests: list[str] = []

        async def capture_probe(request: web.Request) -> web.Response:
            requests.append(request.path)
            return web.json_response({"error": {"type": "invalid_request"}}, status=400)

        app = web.Application()
        app.router.add_post("/{tail:.*}", capture_probe)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        try:
            for protocol in SOURCE_PROTOCOLS:
                await _probe_protocol_response(
                    vendor="custom",
                    protocol=protocol,
                    base_url=f"http://127.0.0.1:{port}",
                    secret="test-observation-key",
                )
        finally:
            await runner.cleanup()
        return requests

    assert asyncio.run(scenario()) == [
        _PROTOCOL_OBSERVATION_TAXONOMY[protocol].request_path
        for protocol in SOURCE_PROTOCOLS
    ]


def test_openai_probe_requests_are_mutually_distinguishable() -> None:
    responses = _PROTOCOL_OBSERVATION_TAXONOMY["openai_responses"]
    chat = _PROTOCOL_OBSERVATION_TAXONOMY["openai_chat"]

    assert responses.request_path != chat.request_path
    assert "input" not in responses.request_body
    assert "messages" not in chat.request_body
    assert _parse_protocol_authenticated_evidence(
        "openai_responses",
        400,
        json.dumps({"error": {"type": "invalid_request_error", "param": "messages"}}),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )
    assert _parse_protocol_authenticated_evidence(
        "openai_chat",
        400,
        json.dumps({"error": {"type": "invalid_request_error", "param": "input"}}),
    ) == _ProtocolEvidence(
        protocol=_ProtocolProof.UNPROVEN,
        authentication=_AuthenticationEvidence.UNKNOWN,
    )


def test_model_discovery_preserves_query_when_appending_models_path() -> None:
    query = "api-version=2026-07-23"

    async def scenario() -> tuple[str, str, tuple[DiscoveredModel, ...]]:
        request_target: tuple[str, str] | None = None

        async def list_models(request: web.Request) -> web.Response:
            nonlocal request_target
            request_target = (request.path, request.query_string)
            return web.json_response({"data": [{"id": "upstream-model"}]})

        app = web.Application()
        app.router.add_get("/{tail:.*}", list_models)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        try:
            models = await probe_models(
                vendor="custom",
                protocol="openai_chat",
                base_url=f"http://127.0.0.1:{port}/v1?{query}",
                secret="test-observation-key",
            )
        finally:
            await runner.cleanup()
        assert request_target is not None
        return request_target[0], request_target[1], models

    path, request_query, models = asyncio.run(scenario())

    assert path == "/v1/models"
    assert request_query == query
    assert models == (DiscoveredModel(id="upstream-model"),)


def test_model_discovery_adds_standard_v1_path_to_a_bare_origin() -> None:
    async def scenario() -> str:
        request_path = ""

        async def list_models(request: web.Request) -> web.Response:
            nonlocal request_path
            request_path = request.path
            return web.json_response({"data": [{"id": "upstream-model"}]})

        app = web.Application()
        app.router.add_get("/{tail:.*}", list_models)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        try:
            await probe_models(
                vendor="custom",
                protocol="anthropic",
                base_url=f"http://127.0.0.1:{port}",
                secret="test-observation-key",
            )
        finally:
            await runner.cleanup()
        return request_path

    assert asyncio.run(scenario()) == "/v1/models"


def test_blocked_exact_hop_emits_one_supply_interruption(tmp_path: Path) -> None:
    source = _source("src_blocked01", "Blocked")
    service = _service(tmp_path, sources=[source])
    service.store.config.agents["claude"].routes["shared-model"] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(source.id, "removed-model"),)
    )
    router = ModelHubRuntimeRouter(service=service)

    for _attempt in range(2):
        with pytest.raises(ModelHubError):
            asyncio.run(router.resolve("claude", "shared-model"))

    events = [event for event in service.list_events(limit=20) if event["kind"] == "supply_interrupted"]
    assert len(events) == 1
    assert events[0]["reason"] == "model_unsupported"


@pytest.mark.parametrize(
    ("route", "native_ready", "reason"),
    [
        (ModelHubRouteConfig(), True, "route_unconfigured"),
        (
            ModelHubRouteConfig(hops=(ModelHubRouteHopConfig("src_missing01", "shared-model"),)),
            True,
            "source_missing",
        ),
    ],
)
def test_supply_interruption_preserves_exact_structural_reason(
    tmp_path: Path,
    route: ModelHubRouteConfig,
    native_ready: bool,
    reason: str,
) -> None:
    source = _source("src_present01", "Present")
    service = _service(tmp_path, sources=[source])
    service.store.config.agents["claude"].routes["shared-model"] = route
    router = ModelHubRuntimeRouter(
        service=service,
        native_cli_ready=lambda _backend: native_ready,
    )

    with pytest.raises(ModelHubError):
        asyncio.run(router.resolve("claude", "shared-model"))

    event = next(item for item in service.list_events(limit=20) if item["kind"] == "supply_interrupted")
    assert event["reason"] == reason
    _assert_valid("resolution-event.schema.json", event)


def test_supply_interruption_preserves_native_process_reason(tmp_path: Path) -> None:
    source = _source(
        "src_native011",
        "Native",
        channel="native_cli",
        vendor="anthropic",
        protocol="anthropic",
    )
    service = _service(tmp_path, sources=[source])
    router = ModelHubRuntimeRouter(
        service=service,
        native_cli_ready=lambda _backend: False,
    )

    with pytest.raises(ModelHubError):
        asyncio.run(router.resolve("claude", "shared-model"))

    event = next(item for item in service.list_events(limit=20) if item["kind"] == "supply_interrupted")
    assert event["reason"] == "native_cli_unavailable"
    _assert_valid("resolution-event.schema.json", event)


def test_same_scope_concurrency_is_absent_and_sequential_control_is_present(
    tmp_path: Path,
) -> None:
    async def run() -> BoundedProvenanceStore:
        store = BoundedProvenanceStore(tmp_path / "concurrency.json")
        registry = TurnCorrelationRegistry(store)
        runtime = SimpleNamespace(
            settle_turn=lambda value, *, settled_by, ts, mode=None: registry.settle(
                value,
                settled_by=settled_by,
                ts=ts,
            )
        )
        controller = SimpleNamespace(
            model_hub_runtime=runtime,
            command_handler=SimpleNamespace(handle_stop=AsyncMock(return_value=True)),
            set_agent_status=lambda _session_id, _status: None,
        )
        manager = SessionTurnManager(controller)
        manager.flush_queue = AsyncMock(return_value=False)
        started = {turn_id: asyncio.Event() for turn_id in ("turn_one", "turn_two", "turn_sequential")}
        release = {turn_id: asyncio.Event() for turn_id in ("turn_one", "turn_two", "turn_sequential")}

        async def dispatch(_controller, context, _text, **_kwargs):
            turn_id = manager.model_hub_turn_id_for_task()
            assert turn_id is not None
            exact_turn = _begin_hub_attempt(
                registry,
                turn_id=turn_id,
                scope="/shared-cwd",
            )
            started[turn_id].set()
            await release[turn_id].wait()
            success = _outcome(RawOutcomeKind.SUCCESS)
            registry.finish_attempt(
                exact_turn,
                outcome=success,
                decision=classify_outcome(success),
            )
            return TurnDispatchOutcome(
                error=None,
                settled_by=SETTLED_BY_TERMINAL_RESULT,
            )

        def context(turn_id: str):
            return SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "turn_token": turn_id,
                    "agent_session_target": {"agent_backend": "codex"},
                },
            )

        with patch(
            "core.session_turns.dispatch_turn_with_outcome",
            side_effect=dispatch,
        ):
            await manager._run("session-one", context("turn_one"), "one")
            first_task = manager.in_flight["session-one"].task
            await manager._run("session-two", context("turn_two"), "two")
            second_task = manager.in_flight["session-two"].task
            await started["turn_one"].wait()
            await started["turn_two"].wait()
            await manager.cancel("session-one")
            release["turn_two"].set()
            await asyncio.gather(
                first_task,
                second_task,
                return_exceptions=True,
            )

            assert store.get("turn_one") is None
            assert store.get("turn_two") is None

            await manager._run(
                "session-sequential",
                context("turn_sequential"),
                "sequential",
            )
            sequential_task = manager.in_flight["session-sequential"].task
            await started["turn_sequential"].wait()
            release["turn_sequential"].set()
            await asyncio.gather(sequential_task, return_exceptions=True)
        return store

    store = asyncio.run(run())
    sequential = store.get("turn_sequential")
    assert sequential is not None
    assert sequential["outcome"] == "served"
    _assert_valid("turn-provenance.schema.json", sequential)


def test_chain_projection_and_probe_latency_partition(tmp_path: Path) -> None:
    cooling = _source(
        "src_cooling01",
        "Cooling",
        status="cooldown",
        retry_at=(NOW + timedelta(minutes=5)).isoformat(),
    )
    primary = _source("src_primary01", "Primary")
    service = _service(
        tmp_path,
        sources=[cooling, primary],
        outcomes=[_outcome(RawOutcomeKind.SUCCESS)],
    )
    menu_model = _canonicalize_fixed_test_routes(service)["claude"]

    chain = service.agent_chain("claude", menu_model)
    assert [item["source_id"] for item in chain["chain"]] == [
        "src_cooling01",
        "src_primary01",
    ]
    assert chain["supply_state"] == "ok"
    assert chain["current"] == {
        "source_id": "src_primary01",
        "model_id": "shared-model",
    }
    assert all(item["channel"] == "hub" for item in chain["chain"])
    assert all(item["reason"] is None for item in chain["chain"])
    _assert_valid("agent-chain.schema.json", chain)

    probe = asyncio.run(service.probe_agent("claude", menu_model))
    assert probe["channel"] == "hub"
    assert probe["reachable"] is True
    assert probe["source_id"] == "src_primary01"
    assert isinstance(probe["latency_ms"], int)
    assert probe["error"] is None
    _assert_valid("probe-result.schema.json", probe)

    rate_service = _service(
        tmp_path / "rate",
        sources=[_source("src_primary01", "Primary")],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=429,
                code="rate_limited",
            )
        ],
    )
    rate_model = _canonicalize_fixed_test_routes(rate_service)["claude"]
    rate_limited = asyncio.run(rate_service.probe_agent("claude", rate_model))
    assert rate_limited["reachable"] is False
    assert isinstance(rate_limited["latency_ms"], int)
    assert rate_limited["error"] == "models.source.cooldown.rate_limited"
    _assert_valid("probe-result.schema.json", rate_limited)

    unclassified_service = _service(
        tmp_path / "unclassified",
        sources=[_source("src_primary01", "Primary")],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=418,
                code="unexpected_upstream_failure",
            )
        ],
    )
    unclassified_model = _canonicalize_fixed_test_routes(unclassified_service)["claude"]
    unclassified = asyncio.run(unclassified_service.probe_agent("claude", unclassified_model))
    assert unclassified["reachable"] is False
    assert isinstance(unclassified["latency_ms"], int)
    assert unclassified["error"] == "models.source.error.unclassified"
    assert unclassified_service.store.load().sources[0].state.status == "error"
    _assert_valid("probe-result.schema.json", unclassified)

    request_error_service = _service(
        tmp_path / "request-error",
        sources=[_source("src_primary01", "Primary")],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=400,
                code="invalid_parameter",
            )
        ],
    )
    request_error_model = _canonicalize_fixed_test_routes(request_error_service)["claude"]
    request_error = asyncio.run(request_error_service.probe_agent("claude", request_error_model))
    assert request_error["reachable"] is False
    assert isinstance(request_error["latency_ms"], int)
    assert request_error["error"] == "models.source.error.unclassified"
    assert request_error_service.store.load().sources[0].state.status == "standby"
    assert request_error_service.events.list(limit=10) == []
    _assert_valid("probe-result.schema.json", request_error)

    anthropic_not_found = _service(
        tmp_path / "anthropic-not-found",
        sources=[_source("src_primary01", "Primary")],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=404,
                code="not_found_error",
            )
        ],
    )
    not_found_model = _canonicalize_fixed_test_routes(anthropic_not_found)["claude"]
    not_found = asyncio.run(anthropic_not_found.probe_agent("claude", not_found_model))
    assert not_found["reachable"] is False
    assert anthropic_not_found.store.load().sources[0].state.status == "standby"
    assert anthropic_not_found.events.list(limit=10) == []
    _assert_valid("probe-result.schema.json", not_found)


@pytest.mark.xfail(
    strict=True,
    reason="AC-50: cooldown.network → backoff.connection_failed 契约先行,实现随 I7 落地",
)
def test_probe_transport_failures_await_ac50_backoff_contract(tmp_path: Path) -> None:
    for name, kind in (
        ("network", RawOutcomeKind.NETWORK_ERROR),
        ("timeout", RawOutcomeKind.TIMEOUT),
    ):
        outcome = _outcome(kind)
        assert outcome.stream_started is False
        service = _service(
            tmp_path / name,
            sources=[_source("src_primary01", "Primary")],
            outcomes=[outcome],
        )
        menu_model = _canonicalize_fixed_test_routes(service)["claude"]
        config_before = json.loads(json.dumps(service.store.load().to_payload()))
        service.store.save = Mock(wraps=service.store.save)

        result = asyncio.run(service.probe_agent("claude", menu_model))

        assert result["reachable"] is False
        assert result["latency_ms"] is None
        assert result["error"] == "models.source.backoff.connection_failed"
        _assert_valid("probe-result.schema.json", result)
        service.store.save.assert_not_called()
        assert service.store.load().to_payload() == config_before
        assert service.store.load().sources[0].state.status == "standby"
        assert service.store.load().sources[0].state.detail_key is None

        chain = service.agent_chain("claude", menu_model)
        hop = chain["chain"][0]
        assert hop["health"] == "backoff"
        assert hop["runnable"] is False
        assert hop["reason"] == "models.source.backoff.connection_failed"
        assert datetime.fromisoformat(hop["retry_at"]) > NOW
        assert chain["current"] is None
        assert chain["supply_state"] == "waiting"
        assert service.events.list(limit=10)[0]["reason"] == "network"
        _assert_valid("agent-chain.schema.json", chain)


def test_probe_401_uses_exact_credential_refresh_capability(tmp_path: Path) -> None:
    static_source = _source("src_staticprobe", "Static")
    static_service = _service(
        tmp_path / "static",
        sources=[static_source],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=401,
                code="unauthorized",
                source_id=static_source.id,
            )
        ],
    )
    static_model = _canonicalize_fixed_test_routes(static_service)["claude"]
    static_adapter = cast(ProbeAdapter, static_service.adapter)

    static_probe = asyncio.run(static_service.probe_agent("claude", static_model))

    assert static_probe["reachable"] is False
    assert static_probe["error"] == ("models.source.needs_action.credential_revoked")
    assert static_adapter.invocations == [(static_source.id, "shared-model", "claude")]
    assert static_adapter.capability_queries == [static_source.credential_ref]
    assert static_service.store.load().sources[0].state.detail_key == ("models.source.needs_action.credential_revoked")

    refreshable_source = _source("src_refreshprobe", "Refreshable")
    refreshable_service = _service(
        tmp_path / "refreshable",
        sources=[refreshable_source],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=401,
                code="unauthorized",
                source_id=refreshable_source.id,
            ),
            _outcome(
                RawOutcomeKind.SUCCESS,
                status=200,
                source_id=refreshable_source.id,
            ),
        ],
    )
    refreshable_model = _canonicalize_fixed_test_routes(refreshable_service)["claude"]
    refreshable_adapter = cast(ProbeAdapter, refreshable_service.adapter)
    assert refreshable_source.credential_ref is not None
    refreshable_adapter.refreshable_credential_refs.add(refreshable_source.credential_ref)

    refreshable_probe = asyncio.run(refreshable_service.probe_agent("claude", refreshable_model))

    assert refreshable_probe["reachable"] is True
    assert refreshable_adapter.invocations == [
        (refreshable_source.id, "shared-model", "claude"),
        (refreshable_source.id, "shared-model", "claude"),
    ]
    assert refreshable_adapter.capability_queries == [refreshable_source.credential_ref]


@pytest.mark.parametrize(
    ("refreshable", "expected_detail"),
    (
        (False, "models.source.needs_action.credential_revoked"),
        (True, "models.source.needs_action.oauth_expired"),
    ),
)
def test_probe_2xx_native_auth_error_uses_classified_blocker_detail(
    tmp_path: Path,
    refreshable: bool,
    expected_detail: str,
) -> None:
    source = _source("src_probe2xxauth", "Native auth envelope")
    outcomes = [
        _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=200,
            code="invalid_api_key",
            source_id=source.id,
        )
    ]
    if refreshable:
        outcomes.append(
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=200,
                code="invalid_api_key",
                source_id=source.id,
            )
        )
    service = _service(tmp_path, sources=[source], outcomes=outcomes)
    model = _canonicalize_fixed_test_routes(service)["claude"]
    adapter = cast(ProbeAdapter, service.adapter)
    if refreshable:
        assert source.credential_ref is not None
        adapter.refreshable_credential_refs.add(source.credential_ref)

    result = asyncio.run(service.probe_agent("claude", model))

    assert result["error"] == expected_detail
    persisted = service.store.load().sources[0].state
    assert persisted.status == "needs_action"
    assert persisted.detail_key == expected_detail


@pytest.mark.parametrize(
    ("backend", "source_protocol", "request_protocol", "request_keys"),
    [
        (
            "claude",
            source_protocol,
            "anthropic",
            {"model", "max_tokens", "messages"},
        )
        for source_protocol in SOURCE_PROTOCOLS
    ]
    + [
        (
            "codex",
            source_protocol,
            "openai_responses",
            {"model", "max_output_tokens", "input"},
        )
        for source_protocol in SOURCE_PROTOCOLS
    ]
    + [
        (
            "opencode",
            source_protocol,
            source_protocol,
            (
                {"model", "max_output_tokens", "input"}
                if source_protocol == "openai_responses"
                else {"model", "max_tokens", "messages"}
            ),
        )
        for source_protocol in SOURCE_PROTOCOLS
    ],
)
def test_probe_request_matches_live_backend_protocol_matrix(
    tmp_path: Path,
    backend: str,
    source_protocol: str,
    request_protocol: str,
    request_keys: set[str],
) -> None:
    source = _source("src_primary01", "Primary")
    source.protocol = source_protocol
    service = _service(
        tmp_path,
        sources=[source],
        outcomes=[_outcome(RawOutcomeKind.SUCCESS)],
    )

    requested_model = "openai/shared-model" if backend == "opencode" else "shared-model"
    result = asyncio.run(service.probe_agent(backend, requested_model))

    assert result["reachable"] is True
    assert service.adapter.invocations == [("src_primary01", "shared-model", backend)]
    request = service.adapter.requests[0]
    assert request.protocol == request_protocol
    assert set(request) == request_keys


def test_native_chain_visibility_and_probe_readiness(tmp_path: Path) -> None:
    native = _source(
        "src_nativecli1",
        "Native CLI",
        channel="native_cli",
    )
    service = _service(tmp_path, sources=[native])

    chain = service.agent_chain("codex", "shared-model")
    assert chain["chain"] == [
        {
            "source_id": native.id,
            "model_id": "shared-model",
            "channel": "native_cli",
            "health": "healthy",
            "runnable": True,
            "reason": None,
            "retry_at": None,
        }
    ]
    assert chain["current"] == {"source_id": native.id, "model_id": "shared-model"}
    assert chain["supply_state"] == "ok"
    _assert_valid("agent-chain.schema.json", chain)

    probe = asyncio.run(service.probe_agent("codex", "shared-model"))
    assert probe == {
        "contract_version": 7,
        "backend": "codex",
        "channel": "native_cli",
        "reachable": True,
        "source_id": native.id,
        "model_id": "shared-model",
        "latency_ms": None,
        "error": None,
    }
    assert service.adapter.invocations == []
    _assert_valid("probe-result.schema.json", probe)


def test_native_unavailability_is_orthogonal_to_health(
    tmp_path: Path,
) -> None:
    native = _source(
        "src_nativecli1",
        "Native CLI",
        channel="native_cli",
        status="cooldown",
        retry_at=(NOW + timedelta(minutes=5)).isoformat(),
    )
    service = _service(tmp_path, sources=[native])
    service.native_source_ready = lambda _backend, _source: False

    chain = service.agent_chain("codex", "shared-model")
    assert chain["chain"][0]["health"] == "cooldown"
    assert chain["chain"][0]["reason"] == "native_cli_unavailable"
    assert chain["chain"][0]["runnable"] is False
    assert chain["current"] is None
    assert chain["supply_state"] == "interrupted"
    _assert_valid("agent-chain.schema.json", chain)


def test_native_probe_rechecks_readiness_after_selection(
    tmp_path: Path,
) -> None:
    native = _source(
        "src_nativecli1",
        "Native CLI",
        channel="native_cli",
    )
    service = _service(tmp_path, sources=[native])
    readiness = iter((True, False))
    service.native_source_ready = lambda _backend, _source: next(readiness)

    probe = asyncio.run(service.probe_agent("codex", "shared-model"))
    assert probe["channel"] == "native_cli"
    assert probe["reachable"] is False
    assert probe["latency_ms"] is None
    assert probe["error"] == "models.probe.native_cli_unavailable"
    assert service.adapter.invocations == []
    _assert_valid("probe-result.schema.json", probe)


def test_probe_no_candidate_and_direct_mode_are_typed(tmp_path: Path) -> None:
    cooling = _source(
        "src_cooling01",
        "Cooling",
        status="cooldown",
        retry_at=(NOW + timedelta(minutes=5)).isoformat(),
    )
    service = _service(tmp_path, sources=[cooling])

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.probe_agent("claude", "shared-model"))
    assert exc_info.value.code == "probe_no_candidate"
    assert exc_info.value.detail == "models.probe.no_candidate.waiting"
    assert exc_info.value.data == {
        "supply": {
            "supply_state": "waiting",
            "retry_at": cooling.state.retry_at,
        }
    }

    service.store.config.agents["claude"].mode = "direct"
    with pytest.raises(ModelHubError) as chain_error:
        service.agent_chain("claude", "shared-model")
    with pytest.raises(ModelHubError) as probe_error:
        asyncio.run(service.probe_agent("claude", "shared-model"))
    for error in (chain_error.value, probe_error.value):
        assert error.code == "direct_mode"
        assert error.detail == "models.hub.direct_mode"


def test_source_failure_event_is_single_grain_and_retained(tmp_path: Path) -> None:
    source = _source("src_primary01", "Shared paid source")
    service = _service(
        tmp_path,
        sources=[source],
        outcomes=[
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=402,
                code="payment_required",
                message="balance exhausted",
            )
        ],
    )
    menu_model = _canonicalize_fixed_test_routes(service)["claude"]

    probe = asyncio.run(service.probe_agent("claude", menu_model))
    assert probe["reachable"] is False
    events = service.list_events(limit=20)
    failures = [event for event in events if event["kind"] == "needs_action"]
    assert len(failures) == 1
    event = failures[0]
    assert event["agent"] == "claude"
    assert event["from_source"] == source.id
    assert event["severity"] == "action_required"
    assert "Shared paid source" in event["human_en"]
    assert "Shared paid source" in event["human_zh"]
    _assert_valid("resolution-event.schema.json", event)

    agents = {item["backend"]: item for item in service.list_agents()}
    assert agents["claude"]["supply_status"] == "interrupted"
    assert agents["codex"]["supply_status"] == "interrupted"

    service.store.config.sources[0].state = ModelHubSourceStateConfig(status="standby")
    recovered_agents = {item["backend"]: item for item in service.list_agents()}
    assert recovered_agents["claude"]["supply_status"] == "ok"
    assert recovered_agents["codex"]["supply_status"] == "ok"
    assert service.list_events(limit=20)[0] == event

    service.store.config.sources.clear()
    assert service.list_events(limit=20)[0] == event


def test_retired_mapping_event_is_rejected_and_channel_switch_retains_subject() -> None:
    with pytest.raises(ValueError):
        build_resolution_event(
            agent="codex",
            kind=cast(EventKind, "mapping" + "_applied"),
            model_id="target-model",
            reason=cast(EventReason, "map" + "ping"),
            from_label="requested-model",
            now=NOW,
        )
    channel_switch = build_resolution_event(
        agent="system",
        kind="channel_switch",
        model_id=None,
        reason="manual",
        from_source="src_primary01",
        to_source="src_primary01",
        from_label="Primary source",
        to_label="Primary source",
        now=NOW,
    )

    for text in (channel_switch.human_en, channel_switch.human_zh):
        assert "Primary source" in text


def test_resolution_event_reason_contract_matches_runtime_vocabulary() -> None:
    schema = json.loads((CONTRACTS / "resolution-event.schema.json").read_text(encoding="utf-8"))
    authority = tuple(EVENT_REASON_AUTHORITY)
    assert tuple(schema["properties"]["reason"]["enum"]) == authority

    locale_reasons = {
        locale: json.loads((Path(__file__).parents[1] / f"vibe/i18n/{locale}.json").read_text(encoding="utf-8"))[
            "modelHub"
        ]["events"]["reason"]
        for locale in ("en", "zh")
    }
    assert all(tuple(reasons) == authority for reasons in locale_reasons.values())
    assert all(
        event_reason_label(reason, locale) != f"modelHub.events.reason.{reason}"
        for locale in locale_reasons
        for reason in authority
    )


@pytest.mark.parametrize("reason", tuple(EVENT_REASON_AUTHORITY))
def test_every_authoritative_reason_has_an_event_emission_path(reason: EventReason) -> None:
    reason_class = EVENT_REASON_AUTHORITY[reason]
    fields: dict = {
        "agent": "system",
        "model_id": None,
        "reason": reason,
        "now": NOW,
    }
    if reason_class == "structural":
        fields.update(agent="codex", kind="supply_interrupted", model_id="model")
    elif reason_class == "self_healing":
        fields.update(kind="cooldown", from_source="src_reason01")
    elif reason_class == "non_self_healing":
        fields.update(kind="needs_action", from_source="src_reason01")
    elif reason_class == "request_scoped":
        fields.update(
            agent="codex",
            kind="switch",
            model_id="model",
            from_source="src_reason01",
            to_source="src_reason02",
        )
    elif reason in {"upstream_tiers", "catalog_tiers"}:
        fields.update(
            kind="reasoning_efforts_override",
            model_id="model",
            from_source="src_reason01",
        )
    elif reason == "recovery":
        fields.update(kind="recover", to_source="src_reason01")
    else:
        fields.update(
            kind="channel_switch",
            from_source="src_reason01",
            to_source="src_reason01",
        )

    event = build_resolution_event(**fields)

    assert event.reason == reason
    assert event.human_en
    assert event.human_zh


def test_shared_source_cooldown_emits_only_on_state_transition(
    tmp_path: Path,
) -> None:
    source = _source("src_primary01", "Shared source")
    service = _service(tmp_path, sources=[source])
    menu_models = _canonicalize_fixed_test_routes(service)
    decision = classify_outcome(
        _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=429,
            code="rate_limited",
        )
    )

    async def cool_twice() -> tuple[bool, bool]:
        first_generation = service._reserve_settlement_generation(source.id)
        _first_reason, first_persisted = await service._settle_fallback_source(
            source,
            decision,
            backend="claude",
            model_id=menu_models["claude"],
            settlement_generation=first_generation,
        )
        repeated_generation = service._reserve_settlement_generation(source.id)
        _repeated_reason, repeated_persisted = await service._settle_fallback_source(
            source,
            decision,
            backend="codex",
            model_id=menu_models["codex"],
            settlement_generation=repeated_generation,
        )
        return first_persisted, repeated_persisted

    assert asyncio.run(cool_twice()) == (True, False)

    events = service.list_events(limit=20)
    assert len(events) == 1
    assert events[0]["kind"] == "cooldown"
    assert events[0]["agent"] == "claude"


def test_late_shorter_cooldown_keeps_the_longest_concurrent_deadline(
    tmp_path: Path,
) -> None:
    fixture = E64_SETTLEMENT_BOUNDARIES["concurrent_cooldown"]
    source = _source("src_cooldownmax01", "Shared source")
    service = _service(tmp_path, sources=[source])
    menu_models = _canonicalize_fixed_test_routes(service)

    async def cool_in_completion_order() -> None:
        for backend, seconds, reason in (
            ("claude", fixture["first_seconds"], "quota_exhausted"),
            ("codex", fixture["late_seconds"], "server_error"),
        ):
            generation = service._reserve_settlement_generation(source.id)
            await service._settle_fallback_source(
                source,
                ResolutionDecision(
                    "fallback",
                    reason=reason,
                    cooldown_seconds=seconds,
                ),
                backend=backend,
                model_id=menu_models[backend],
                settlement_generation=generation,
            )

    asyncio.run(cool_in_completion_order())

    persisted = service.store.load().sources[0]
    assert persisted.state.status == "cooldown"
    assert persisted.state.retry_at == (
        NOW + timedelta(seconds=fixture["expected_seconds"])
    ).isoformat()
    assert persisted.state.detail_key == "models.source.cooldown.quota_exhausted"


def test_late_transient_settlement_preserves_needs_action_state(
    tmp_path: Path,
) -> None:
    source = _source("src_needsaction01", "Credential source")
    service = _service(tmp_path, sources=[source])
    menu_models = _canonicalize_fixed_test_routes(service)
    transient = classify_outcome(
        _outcome(
            RawOutcomeKind.HTTP_ERROR,
            status=503,
            code="api_error",
        )
    )

    async def settle_in_order() -> None:
        blocker_generation = service._reserve_settlement_generation(source.id)
        await service._settle_fallback_source(
            source,
            ResolutionDecision("fallback", reason="credential_revoked"),
            backend="claude",
            model_id=menu_models["claude"],
            detail_key="models.source.needs_action.credential_revoked",
            settlement_generation=blocker_generation,
        )
        transient_generation = service._reserve_settlement_generation(source.id)
        await service._settle_fallback_source(
            source,
            transient,
            backend="codex",
            model_id=menu_models["codex"],
            settlement_generation=transient_generation,
        )

    asyncio.run(settle_in_order())

    persisted = service.store.load().sources[0]
    assert persisted.state.status == "needs_action"
    assert persisted.state.detail_key == "models.source.needs_action.credential_revoked"


def test_late_equal_priority_blocker_preserves_the_newer_reason(
    tmp_path: Path,
) -> None:
    source = _source("src_blockerorder", "Shared credential")
    service = _service(tmp_path, sources=[source])
    menu_models = _canonicalize_fixed_test_routes(service)
    older_generation = service._reserve_settlement_generation(source.id)
    newer_generation = service._reserve_settlement_generation(source.id)

    async def settle_in_completion_order() -> tuple[bool, bool]:
        _newer_reason, newer_persisted = await service._settle_fallback_source(
            source,
            ResolutionDecision("fallback", reason="account_banned"),
            backend="claude",
            model_id=menu_models["claude"],
            detail_key="models.source.needs_action.account_banned",
            settlement_generation=newer_generation,
        )
        _older_reason, older_persisted = await service._settle_fallback_source(
            source,
            ResolutionDecision("fallback", reason="credential_expired"),
            backend="codex",
            model_id=menu_models["codex"],
            detail_key="models.source.needs_action.oauth_expired",
            settlement_generation=older_generation,
        )
        return newer_persisted, older_persisted

    assert asyncio.run(settle_in_completion_order()) == (True, False)

    persisted = service.store.load().sources[0]
    assert persisted.state.status == "needs_action"
    assert persisted.state.detail_key == "models.source.needs_action.account_banned"


def test_event_emission_rejects_unknown_source_ids(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        sources=[_source("src_primary01", "Primary")],
    )
    service._record_event(
        agent="claude",
        kind="switch",
        model_id="shared-model",
        reason="manual",
        from_source="src_unknown01",
        to_source="src_primary01",
        now=NOW,
    )
    service._record_event(
        agent="claude",
        kind="switch",
        model_id="shared-model",
        reason="manual",
        from_source="src_primary01",
        to_source="src_unknown02",
        now=NOW,
    )
    assert service.list_events(limit=20) == []


@pytest.mark.parametrize(
    "fields",
    [
        {
            "agent": "system",
            "kind": "supply_interrupted",
            "model_id": "shared-model",
            "reason": "no_enabled_source",
        },
        {
            "agent": "claude",
            "kind": "cooldown",
            "model_id": "shared-model",
            "reason": "credential_revoked",
            "from_source": "src_primary01",
        },
        {
            "agent": "claude",
            "kind": "needs_action",
            "model_id": "shared-model",
            "reason": "network",
            "from_source": "src_primary01",
        },
        {
            "agent": "claude",
            "kind": "switch",
            "model_id": "shared-model",
            "reason": "no_eligible_source",
            "from_source": "src_primary01",
            "to_source": "src_backup001",
        },
    ],
)
def test_event_builder_rejects_cross_grain_shapes(fields: dict) -> None:
    with pytest.raises(ValueError):
        build_resolution_event(**fields)


def test_provenance_absence_codes_are_distinguishable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        sources=[_source("src_primary01", "Primary")],
    )
    monkeypatch.setattr(
        service,
        "_known_turn",
        lambda turn_id: (
            ("codex", "direct")
            if turn_id == "turn_direct"
            else (("codex", "hub") if turn_id == "turn_ambiguous" else (None, None))
        ),
    )

    service.store.config.agents["codex"].mode = "hub"
    with pytest.raises(ModelHubError) as direct:
        service.get_turn_provenance("turn_direct")
    assert direct.value.code == "provenance_unavailable"
    assert direct.value.detail == "models.provenance.direct_mode"

    service.store.config.agents["codex"].mode = "direct"
    with pytest.raises(ModelHubError) as ambiguous:
        service.get_turn_provenance("turn_ambiguous")
    assert ambiguous.value.code == "provenance_unavailable"
    assert ambiguous.value.detail == "models.provenance.attribution_ambiguous"

    with pytest.raises(ModelHubError) as unknown:
        service.get_turn_provenance("turn_unknown")
    assert unknown.value.code == "turn_not_found"
    assert unknown.value.status == 404


def test_turn_mode_marker_is_deleted_with_its_turn_record(
    monkeypatch,
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'state.db'}")
    metadata.create_all(engine)
    now = NOW.isoformat()
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id="ses_turn_mode",
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="anchor",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            messages.insert().values(
                id="msg_turn_mode",
                session_id="ses_turn_mode",
                platform="avibe",
                author="user",
                type="user",
                source="user",
                content_json="{}",
                metadata_json=json.dumps({"turn_id": "turn_direct"}),
                created_at=now,
                updated_at=now,
            )
        )

    service = _service(
        tmp_path,
        sources=[_source("src_primary01", "Primary")],
    )
    monkeypatch.setattr(
        "core.handlers.model_hub.service.get_cached_sqlite_engine",
        lambda: engine,
    )
    service.note_turn_mode("turn_direct", "direct")
    with engine.connect() as conn:
        stored_mode = conn.execute(
            select(
                messages.c.metadata_json,
            ).where(messages.c.id == "msg_turn_mode")
        ).scalar_one()
    assert json.loads(stored_mode)["model_hub_mode"] == "direct"

    with pytest.raises(ModelHubError) as unavailable:
        service.get_turn_provenance("turn_direct")
    assert unavailable.value.code == "provenance_unavailable"
    assert unavailable.value.detail == "models.provenance.direct_mode"

    with engine.begin() as conn:
        conn.execute(delete(messages).where(messages.c.id == "msg_turn_mode"))

    with pytest.raises(ModelHubError) as deleted:
        service.get_turn_provenance("turn_direct")
    assert deleted.value.code == "turn_not_found"


def test_known_opencode_turn_is_fail_closed_but_not_unknown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = BoundedProvenanceStore(tmp_path / "opencode-provenance.json")
    registry = TurnCorrelationRegistry(store)
    token = registry.credentials(
        "opencode",
        "opencode:shared-server",
        "turn_opencode",
    )

    assert (
        registry.begin_gateway_request(
            backend="opencode",
            token=token,
            requested_model_id="openai/shared-model",
        )
        is None
    )
    registry.settle(
        "turn_opencode",
        settled_by=SETTLED_BY_TERMINAL_RESULT,
    )
    assert store.get("turn_opencode") is None

    service = _service(
        tmp_path,
        sources=[_source("src_primary01", "Primary")],
    )
    service.provenance = store
    monkeypatch.setattr(
        service,
        "_known_turn",
        lambda turn_id: (("opencode", None) if turn_id == "turn_opencode" else (None, None)),
    )

    with pytest.raises(ModelHubError) as unavailable:
        service.get_turn_provenance("turn_opencode")
    assert unavailable.value.code == "provenance_unavailable"
    assert unavailable.value.detail == "models.provenance.attribution_ambiguous"


def _usage_of(service: ModelHubService, source_id: str) -> dict:
    summary = service.usage_summary(days=30)
    matches = [source for source in summary["sources"] if source["source_id"] == source_id]
    return matches[0] if matches else {}


def test_gateway_meters_a_buffered_served_turn_from_the_adapter_outcome(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_meterbuf01", "Buffered meter")
        body = json.dumps(
            {
                "id": "resp_buffered",
                "usage": {
                    "input_tokens": 4096,
                    "input_tokens_details": {"cached_tokens": 3072},
                    "output_tokens": 128,
                },
            }
        ).encode("utf-8")
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS,
                        status=200,
                        source_id=source.id,
                        usage=ProtocolUsageReport.of(
                            input_tokens=4096,
                            cached_input_tokens=3072,
                            output_tokens=128,
                        ),
                    ),
                    (body,),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_buffered",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )

        result = await gateway._handle_request(request)

        assert result.status == 200
        assert result.body == body
        metered = _usage_of(service, source.id)
        assert metered["label"] == "Buffered meter"
        assert metered["requests"] == 1
        assert metered["token_reports"] == 1
        assert metered["input_tokens"] == 4096
        assert metered["cached_input_tokens"] == 3072
        assert metered["output_tokens"] == 128
        assert [model["model_id"] for model in metered["models"]] == ["shared-model"]

    asyncio.run(exercise())


def test_gateway_spools_and_replays_a_large_buffered_response(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_largebuf01", "Large buffered response")
        body = (
            b'{"payload":"'
            + b"x" * (512 * 1024)
            + b'","usage":{"input_tokens":4096,"input_tokens_details":'
            b'{"cached_tokens":3072},"output_tokens":128}}'
        )
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS,
                        status=200,
                        source_id=source.id,
                        stream_started=True,
                        usage=ProtocolUsageReport.of(
                            input_tokens=4096,
                            cached_input_tokens=3072,
                            output_tokens=128,
                        ),
                    ),
                    (body[:200_000], body[200_000:400_000], body[400_000:]),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_large_buffered",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )
        downstream = FakeStreamResponse()

        event_loop_thread = threading.get_ident()
        write_threads: list[int] = []
        spooled_file = tempfile.SpooledTemporaryFile

        class TrackingSpool:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._file = spooled_file(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                self._file.close()

            def write(self, data: bytes) -> int:
                write_threads.append(threading.get_ident())
                return self._file.write(data)

            def __getattr__(self, name: str):
                return getattr(self._file, name)

        with (
            patch(
                "core.handlers.model_hub.turn_gateway.web.StreamResponse",
                return_value=downstream,
            ),
            patch(
                "core.handlers.model_hub.turn_gateway.tempfile.SpooledTemporaryFile",
                TrackingSpool,
            ),
        ):
            result = await gateway._handle_request(request)

        assert result is downstream
        assert b"".join(downstream.writes) == body
        assert downstream.eof_called is True
        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 1
        assert metered["input_tokens"] == 4096
        assert metered["cached_input_tokens"] == 3072
        assert metered["output_tokens"] == 128
        assert write_threads
        assert event_loop_thread not in write_threads

    asyncio.run(exercise())


def test_gateway_drains_a_cancelled_spool_write_before_closing(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_spoolown1", "Owned spool")
        handle = LiveInvokeHandle(
            _outcome(
                RawOutcomeKind.SUCCESS,
                source_id=source.id,
                stream_started=True,
            ),
            (b'{"output":"ok"}',),
        )
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_owned_spool_write",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )
        write_started = threading.Event()
        release_write = threading.Event()
        write_finished = threading.Event()
        closed = threading.Event()
        close_raced_write: list[bool] = []
        spooled_file = tempfile.SpooledTemporaryFile

        class TrackingSpool:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._file = spooled_file(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                close_raced_write.append(not write_finished.is_set())
                self._file.close()
                closed.set()

            def write(self, data: bytes) -> int:
                write_started.set()
                assert release_write.wait(timeout=1)
                result = self._file.write(data)
                write_finished.set()
                return result

            def __getattr__(self, name: str):
                return getattr(self._file, name)

        with patch(
            "core.handlers.model_hub.turn_gateway.tempfile.SpooledTemporaryFile",
            TrackingSpool,
        ):
            task = asyncio.create_task(gateway._handle_request(request))
            assert await asyncio.to_thread(write_started.wait, 1)
            task.cancel()
            await asyncio.sleep(0)
            assert not closed.is_set()
            release_write.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        assert write_finished.is_set()
        assert closed.is_set()
        assert close_raced_write == [False]
        await gateway.close()

    asyncio.run(exercise())


def test_gateway_meters_a_streamed_served_turn_from_the_wire(tmp_path: Path) -> None:
    async def exercise() -> None:
        source = _source("src_meterwire01", "Streamed meter")
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS,
                        status=200,
                        source_id=source.id,
                        stream_started=True,
                    ),
                    (
                        b'event: response.output_text.delta\ndata: {"type":'
                        b'"response.output_text.delta","sequence_number":1}\n\n',
                        b'event: response.completed\ndata: {"type":"response.completed",'
                        b'"sequence_number":2,"response":{"usage":{"input_tokens":900,'
                        b'"output_tokens":64}}}\n\n',
                    ),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_streamed",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(),
        ):
            await gateway._handle_request(request)

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 1
        assert metered["input_tokens"] == 900
        assert metered["output_tokens"] == 64

    asyncio.run(exercise())


def test_a_stream_that_forwarded_output_is_metered_without_a_token_report(
    tmp_path: Path,
) -> None:
    """Review 4959575659 finding 10: `requests` is what our own code measured.

    Model output reached the client, so the call reached the model — a connection
    lost after a text delta is still a request that happened. Nobody reported its
    tokens, which is exactly what `token_reports` staying at zero records.
    """

    async def exercise() -> None:
        source = _source("src_meterpart01", "Interrupted stream")
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.NETWORK_ERROR,
                        source_id=source.id,
                        stream_started=True,
                    ),
                    (
                        b'event: response.output_text.delta\ndata: {"type":'
                        b'"response.output_text.delta","sequence_number":1}\n\n',
                    ),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_partial",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(),
        ):
            await gateway._handle_request(request)

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 0
        assert metered["input_tokens"] == 0
        assert metered["output_tokens"] == 0

    asyncio.run(exercise())


def test_a_buffered_response_cancelled_while_settling_is_still_metered(
    tmp_path: Path,
) -> None:
    """Review 4960016618: the boundary meters the turn's report, not one shape's.

    Upstream delivered a complete billed response and the client then went away
    while the turn was settling. The buffered shape has no wire tracker for the
    boundary to read, so a report that lived only in the request frame's local
    would vanish exactly when the turn was already billed.
    """

    async def exercise() -> None:
        source = _source("src_meterbuf01", "Cancelled buffered turn")
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS,
                        status=200,
                        source_id=source.id,
                        stream_started=True,
                        usage=ProtocolUsageReport.of(
                            input_tokens=704,
                            cached_input_tokens=0,
                            output_tokens=21,
                        ),
                    ),
                    (b'{"usage":{"input_tokens":704,"output_tokens":21}}',),
                )
            ],
        )
        settling = asyncio.Event()
        release = asyncio.Event()
        settle_handle_outcome = service.settle_handle_outcome

        async def blocked_settlement(*args: object, **kwargs: object):
            settling.set()
            await release.wait()
            return await settle_handle_outcome(*args, **kwargs)

        service.settle_handle_outcome = blocked_settlement
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_buffered_cancel",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )

        turn = asyncio.create_task(gateway._handle_request(request))
        await asyncio.wait_for(settling.wait(), timeout=1)
        turn.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, timeout=1)

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 1
        assert metered["input_tokens"] == 704
        assert metered["output_tokens"] == 21

    asyncio.run(exercise())


def test_buffered_adapter_facts_survive_cancellation_before_body_consumption(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_meterproj1", "Cancelled projection")
        body_started = asyncio.Event()

        class BufferedHandle(LiveInvokeHandle):
            async def _blocked_body(self):
                body_started.set()
                await asyncio.Event().wait()
                if False:
                    yield b""

            def __init__(self, outcome: RawCallOutcome) -> None:
                self._outcome = outcome
                self._observed = None
                self._stream = self._blocked_body()

        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                BufferedHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS,
                        status=200,
                        source_id=source.id,
                        stream_started=True,
                        usage=ProtocolUsageReport.of(
                            input_tokens=901,
                            cached_input_tokens=0,
                            output_tokens=22,
                        ),
                    ),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service, transport_timeout=0.2)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_projection_cancel",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )

        turn = asyncio.create_task(gateway._handle_request(request))
        await asyncio.wait_for(body_started.wait(), timeout=1)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, timeout=1)

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 1
        assert metered["input_tokens"] == 901
        assert metered["output_tokens"] == 22

    asyncio.run(exercise())


def test_a_cancelled_buffered_turn_is_counted_even_when_it_reported_no_tokens(
    tmp_path: Path,
) -> None:
    """Review 4964520496: a request is self-measured, so a silent vendor still owes one.

    Same cancellation as above with the one thing removed that was carrying it —
    the usage block. `requests` is measured by our own code and must not depend on
    what upstream chose to report, which is exactly what `token_reports` staying
    at zero records.
    """

    async def exercise() -> None:
        source = _source("src_meterbuf02", "Cancelled silent turn")
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS,
                        status=200,
                        source_id=source.id,
                        stream_started=True,
                    ),
                    (b'{"output":[{"type":"message","content":[]}]}',),
                )
            ],
        )
        settling = asyncio.Event()
        release = asyncio.Event()
        settle_handle_outcome = service.settle_handle_outcome

        async def blocked_settlement(*args: object, **kwargs: object):
            settling.set()
            await release.wait()
            return await settle_handle_outcome(*args, **kwargs)

        service.settle_handle_outcome = blocked_settlement
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_buffered_silent_cancel",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )

        turn = asyncio.create_task(gateway._handle_request(request))
        await asyncio.wait_for(settling.wait(), timeout=1)
        turn.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, timeout=1)

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 0

    asyncio.run(exercise())


def test_a_cancelled_turn_cannot_take_the_ledger_write_it_queued_with_it(
    tmp_path: Path,
) -> None:
    """Review 4964754924: the gateway owns the write, so no ending can cancel it.

    Occupy the writing thread and the ledger write is queued but not started —
    the window where cancelling the awaiting task also cancels the work. The turn
    dies there, and the row still lands, because what the turn owns is the
    decision to meter and not the write that carries it out.
    """

    async def exercise() -> None:
        source = _source("src_meterown01", "Owned write")
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS,
                        status=200,
                        source_id=source.id,
                        usage=ProtocolUsageReport.of(
                            input_tokens=512,
                            cached_input_tokens=0,
                            output_tokens=16,
                        ),
                    ),
                    (b'{"usage":{"input_tokens":512,"output_tokens":16}}',),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_owned_write",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )

        with _occupied_ledger_writer():
            turn = asyncio.create_task(gateway._handle_request(request))
            while not gateway._usage_writer.unpersisted:
                await asyncio.sleep(0.01)
            # The scenario is only the scenario while the write is still queued.
            assert _usage_of(service, source.id) == {}

            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(turn, timeout=5)

        await gateway.close()

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["input_tokens"] == 512
        assert metered["output_tokens"] == 16

    asyncio.run(exercise())


def test_a_cancelled_resolve_cannot_take_the_ledger_write_it_queued_with_it(
    tmp_path: Path,
) -> None:
    """Review 4964894667: the same window at the other metering owner.

    A call the resolver consumed itself still reports tokens the vendor billed.
    Occupy the writing thread and its row is queued but not started; the caller
    is cancelled there, and the row lands anyway. Neither owner keeps a write of
    its own, so neither can lose one — `UsageWriter` holds both.
    """

    async def exercise() -> None:
        source = _source("src_meterown02", "Owned resolve write")
        service = _service(
            tmp_path,
            sources=[source],
            outcomes=[
                _outcome(
                    RawOutcomeKind.SUCCESS,
                    status=200,
                    source_id=source.id,
                    usage=ProtocolUsageReport.of(
                        input_tokens=512,
                        cached_input_tokens=0,
                        output_tokens=16,
                    ),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]

        with _occupied_ledger_writer():
            resolve = asyncio.create_task(
                service.resolve(
                    backend="codex",
                    model_id=requested_model,
                    request=ModelHubRequest(
                        {"model": requested_model, "input": "ping"},
                        protocol="openai_responses",
                    ),
                    supply_channel="hub",
                )
            )
            while not service.usage_writer.unpersisted:
                await asyncio.sleep(0.01)
            # The scenario is only the scenario while the write is still queued.
            assert _usage_of(service, source.id) == {}

            resolve.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(resolve, timeout=5)

        assert await service.usage_writer.drain(timeout=5) == 0

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["input_tokens"] == 512
        assert metered["output_tokens"] == 16

    asyncio.run(exercise())


def test_a_burst_of_metering_neither_borrows_the_shared_pool_nor_grows_unbounded(
    tmp_path: Path,
) -> None:
    """Review 4965076681: the writes queue on their own thread, a batch at a time.

    `record_many` holds the ledger's lock across an fsync, so one shared-pool job
    per completed call would park that many workers on a lock that admits one,
    and unrelated `asyncio.to_thread` work would wait behind metering for nothing.
    Hold the loop's default executor for the whole test and metering still lands:
    it was never borrowing from there.

    What bounds the queue is in the same run. Calls that arrive while a flush is
    on disk are taken together by the next one, so a burst costs transactions in
    proportion to how long the disk takes rather than to how hard the hub is
    driven — and every row still lands, which dropping them would not do.
    """

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        shared = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(shared)
        borrowed_started = threading.Event()
        give_back = threading.Event()

        def borrow() -> None:
            borrowed_started.set()
            give_back.wait(5)

        borrower = loop.run_in_executor(shared, borrow)
        assert borrowed_started.wait(5)

        source = _source("src_meterburst1", "Burst")
        service = _service(tmp_path, sources=[source])
        transactions: list[int] = []
        fold = service.usage.record_many

        def counting(calls) -> None:
            transactions.append(len(calls))
            fold(calls)

        service.usage.record_many = counting
        writer = service.usage_writer

        def meter() -> None:
            writer.record(
                source_id=source.id,
                model_id="gpt-5-codex",
                usage=ProtocolUsageReport.of(
                    input_tokens=1, cached_input_tokens=0, output_tokens=1
                ),
                at=NOW,
            )

        with _occupied_ledger_writer():
            meter()
            # White-box on purpose: the batch boundary is the thing under test,
            # and it is only observable once the first flush is holding one.
            while not writer._writing:
                await asyncio.sleep(0.01)
            for _ in range(7):
                meter()

        assert await writer.drain(timeout=5) == 0
        # Eight calls, two trips to disk — the second took every call that piled
        # up behind the first, which is the whole of the bound. It carries one row
        # rather than seven, because the seven were headed for one row anyway; the
        # requests below are what proves that folded and lost are different things.
        assert transactions == [1, 1]

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 8
        assert metered["input_tokens"] == 8

        give_back.set()
        await borrower
        shared.shutdown()

    asyncio.run(exercise())


def test_no_ending_of_a_turn_decides_for_itself_what_the_call_did() -> None:
    """The metering facts have one owner, so an ending is a *when*, not a *what*.

    Endings that answered locally answered in the vocabulary of the shape they
    happened to see, and the boundary — which can see either — got the buffered
    one wrong. An ending added later is covered by construction if it cannot pass
    the answer in, so that is what is asserted rather than today's three endings.
    """

    path = Path(__file__).parents[1] / "core/handlers/model_hub/turn_gateway.py"
    calls = [
        node
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_record_usage"
    ]

    assert calls, "the gateway must still meter the calls whose body it forwards"
    assert all(len(call.args) == 1 and not call.keywords for call in calls)


def test_no_downstream_ending_after_adoption_can_drop_the_turn_from_the_ledger(
    tmp_path: Path,
) -> None:
    """Review 4965076681: the gateway takes the body before it can serve it.

    Between adopting the upstream body and reading it, the gateway prepares a
    downstream response — and a client that leaves in that gap leaves behind
    neither a wire tracker nor a buffered verdict for the boundary to read. The
    vendor billed the call either way, so what the ledger must not depend on is
    *where* the turn died.

    Driven off `FakeStreamResponse`'s own failure points instead of a list of
    them: a downstream step that becomes failable later is covered the moment it
    can fail, rather than the moment someone remembers this test.
    """

    failure_points = [
        name for name in inspect.signature(FakeStreamResponse).parameters if name.endswith("_error")
    ]
    assert failure_points

    async def exercise(point: str) -> None:
        state = tmp_path / point
        state.mkdir()
        source = _source("src_meterdrop01", "Adopted body")
        service = _service(
            state,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(RawOutcomeKind.SUCCESS, status=200, source_id=source.id),
                    (
                        b'event: response.output_text.delta\ndata: {"type":'
                        b'"response.output_text.delta","sequence_number":1}\n\n',
                    ),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id=f"turn_meter_{point}",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(**{point: ConnectionResetError("client left")}),
        ):
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(gateway._handle_request(request), timeout=5)
        await gateway.close()

        assert _usage_of(service, source.id).get("requests") == 1, point

    for point in failure_points:
        asyncio.run(exercise(point))


def test_the_tokens_the_engine_already_read_survive_every_ending_before_our_first(
    tmp_path: Path,
) -> None:
    """MH-USAGE-001, review 4965405530: the engine read this body's head before we did.

    The gateway asks the engine for a stream, and the engine only knows there is
    one because it read far enough to see the first model output — which for
    Anthropic is past `message_start`, the frame carrying the input tokens the
    vendor already billed. Those bytes then reach the gateway as a replay it
    re-tokenizes itself, so every fact it holds about the call starts existing
    only once forwarding starts. `requests` survived that gap because adoption
    is a fact of the turn; the token counts had nowhere to come from.

    Same enumeration as the sibling above, for the same reason: the property is
    that no downstream ending decides what upstream reported, so the endings are
    read off the response double rather than listed here.
    """

    failure_points = [
        name for name in inspect.signature(FakeStreamResponse).parameters if name.endswith("_error")
    ]
    assert failure_points

    prelude = (
        b'event: message_start\ndata: {"type":"message_start","message":'
        b'{"usage":{"input_tokens":900,"cache_read_input_tokens":128}}}\n\n'
    )

    async def exercise(point: str) -> None:
        state = tmp_path / f"prelude_{point}"
        state.mkdir()
        source = _source("src_meterprel01", "Prelude-billed body")
        engine_view = ProtocolSSEState("anthropic")
        engine_view.observe(prelude)
        assert engine_view.usage is not None
        service = _service(
            state,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(RawOutcomeKind.SUCCESS, status=200, source_id=source.id),
                    (prelude,),
                    observed=engine_view,
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id=f"turn_meter_prelude_{point}",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(**{point: ConnectionResetError("client left")}),
        ):
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(gateway._handle_request(request), timeout=5)
        await gateway.close()

        metered = _usage_of(service, source.id)
        assert metered.get("requests") == 1, point
        assert metered.get("token_reports") == 1, point
        assert metered.get("input_tokens") == 1028, point

    for point in failure_points:
        asyncio.run(exercise(point))


def test_a_turn_cancelled_before_it_read_the_body_is_still_a_call_that_happened(
    tmp_path: Path,
) -> None:
    """The same gap on the buffered path, which the review did not reach.

    A non-streaming turn adopts the whole body and reads it in one go, so a
    client that disconnects while that read is in flight leaves the boundary
    exactly as empty-handed as the prepare gap does. Same adoption, same answer,
    and `token_reports` at zero is the ledger saying nobody got to read the
    tokens rather than that there were none.
    """

    async def exercise() -> None:
        source = _source("src_meterdrain01", "Adopted buffered body")
        handle = BlockingLiveInvokeHandle(
            _outcome(RawOutcomeKind.SUCCESS, status=200, source_id=source.id)
        )
        service = _service(tmp_path, sources=[source], live_handles=[handle])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_cancelled_drain",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )

        turn = asyncio.create_task(gateway._handle_request(request))
        await asyncio.wait_for(handle.started.wait(), timeout=5)
        # The scenario is only the scenario while nothing has been read yet.
        assert _usage_of(service, source.id) == {}

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, timeout=5)
        await gateway.close()

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 0

    asyncio.run(exercise())


def test_gateway_meters_a_failed_turn_that_upstream_already_billed(
    tmp_path: Path,
) -> None:
    """A vendor that reported tokens billed us even when the stream failed."""

    async def exercise() -> None:
        source = _source("src_meterfail01", "Billed failure")
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.HTTP_ERROR,
                        status=500,
                        source_id=source.id,
                        stream_started=True,
                    ),
                    (
                        b'event: response.output_text.delta\ndata: {"type":'
                        b'"response.output_text.delta","sequence_number":1}\n\n',
                        b'event: error\ndata: {"type":"error","code":"server_error",'
                        b'"sequence_number":2,"usage":{"input_tokens":41,'
                        b'"output_tokens":0}}\n\n',
                    ),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_billed_failure",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(),
        ):
            await gateway._handle_request(request)

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 1
        assert metered["input_tokens"] == 41
        # The failure keeps its own surface; metering does not replace it.
        assert service.store.load().sources[0].state.status != "standby"

    asyncio.run(exercise())


def test_gateway_meters_nothing_for_a_turn_that_never_reached_the_model(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        source = _source("src_meternone01", "Unreached", status="needs_action")
        service = _service(tmp_path, sources=[source])
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        service.store.config.agents["codex"].routes[requested_model] = ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(source.id, "removed-model"),)
        )
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_unreached",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )

        result = await gateway._handle_request(request)

        assert result.status == 409
        summary = service.usage_summary(days=30)
        assert summary["sources"] == []
        assert summary["totals"]["requests"] == 0

    asyncio.run(exercise())


def test_a_surfaced_buffered_error_still_meters_the_tokens_it_billed(
    tmp_path: Path,
) -> None:
    """The call never hands a body onward, so only the resolver can meter it."""

    async def exercise() -> None:
        source = _source("src_meterbuferr", "Surfaced billing")
        service = _service(
            tmp_path,
            sources=[source],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=400,
                    code="invalid_request_error",
                    source_id=source.id,
                    usage=ProtocolUsageReport.of(
                        input_tokens=310,
                        cached_input_tokens=64,
                        output_tokens=0,
                    ),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]

        with pytest.raises(ModelHubError) as raised:
            await service.resolve(
                backend="codex",
                model_id=requested_model,
                request=ModelHubRequest(
                    {"model": requested_model, "input": "ping"},
                    protocol="openai_responses",
                ),
                supply_channel="hub",
            )

        assert raised.value.code == "upstream_request_invalid"
        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 1
        assert metered["input_tokens"] == 310
        assert metered["cached_input_tokens"] == 64
        assert metered["last_metered_at"] == NOW.isoformat()

    asyncio.run(exercise())


def test_a_billed_failover_hop_is_metered_against_the_source_that_billed_it(
    tmp_path: Path,
) -> None:
    """MH-USAGE-002: every hop that reported tokens billed its own Source, not the last one."""

    async def exercise() -> None:
        first = _source("src_meterhop001", "Billed hop")
        second = _source("src_meterhop002", "Serving hop")
        service = _service(
            tmp_path,
            sources=[first, second],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    code="rate_limit_error",
                    source_id=first.id,
                    usage=ProtocolUsageReport.of(
                        input_tokens=88,
                        cached_input_tokens=0,
                        output_tokens=0,
                    ),
                ),
                _outcome(
                    RawOutcomeKind.SUCCESS,
                    status=200,
                    source_id=second.id,
                    usage=ProtocolUsageReport.of(
                        input_tokens=120,
                        cached_input_tokens=0,
                        output_tokens=17,
                    ),
                ),
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        service.store.config.agents["codex"].routes[requested_model] = ModelHubRouteConfig(
            hops=(
                ModelHubRouteHopConfig(first.id, "shared-model"),
                ModelHubRouteHopConfig(second.id, "shared-model"),
            )
        )

        resolved = await service.resolve(
            backend="codex",
            model_id=requested_model,
            request=ModelHubRequest(
                {"model": requested_model, "input": "ping"},
                protocol="openai_responses",
            ),
            supply_channel="hub",
        )

        assert resolved.source_id == second.id
        assert _usage_of(service, first.id)["input_tokens"] == 88
        assert _usage_of(service, second.id)["input_tokens"] == 120
        # One turn, two upstream calls: the unit is the call, so the total says so.
        assert service.usage_summary(days=30)["totals"]["requests"] == 2

    asyncio.run(exercise())


def test_a_call_that_reached_no_model_is_never_metered(tmp_path: Path) -> None:
    """A rejected credential billed nothing; source health already reports it."""

    async def exercise() -> None:
        source = _source("src_meterauth01", "Rejected")
        service = _service(
            tmp_path,
            sources=[source],
            outcomes=[
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=401,
                    code="authentication_error",
                    source_id=source.id,
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]

        with pytest.raises(ModelHubError):
            await service.resolve(
                backend="codex",
                model_id=requested_model,
                request=ModelHubRequest(
                    {"model": requested_model, "input": "ping"},
                    protocol="openai_responses",
                ),
                supply_channel="hub",
            )

        assert service.usage_summary(days=30)["totals"]["requests"] == 0

    asyncio.run(exercise())


def test_a_downstream_disconnect_meters_the_terminal_frame_exactly_once(
    tmp_path: Path,
) -> None:
    """The wire read the tokens before the write failed, so the turn was billed."""

    async def exercise() -> None:
        source = _source("src_meterdrop01", "Dropped client")
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS,
                        status=200,
                        source_id=source.id,
                        stream_started=True,
                    ),
                    (
                        b'event: response.completed\ndata: {"type":"response.completed",'
                        b'"sequence_number":1,"response":{"usage":{"input_tokens":512,'
                        b'"output_tokens":33}}}\n\n',
                    ),
                )
            ],
        )
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_disconnect",
            requested_model=requested_model,
            source_id=source.id,
            stream=True,
        )
        response = FakeStreamResponse(
            write_error=ConnectionResetError("downstream write failed")
        )

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=response,
        ):
            with pytest.raises(ConnectionError, match="downstream"):
                await asyncio.wait_for(gateway._handle_request(request), timeout=1)

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["token_reports"] == 1
        assert metered["input_tokens"] == 512
        assert metered["output_tokens"] == 33

    asyncio.run(exercise())


def test_neither_metering_owner_writes_the_ledger_on_the_event_loop(
    tmp_path: Path,
) -> None:
    """MH-USAGE-004: a ledger read-modify-write is file I/O; the loop must not wait."""

    async def exercise() -> None:
        recording_threads: list[int] = []

        def capture(_calls: object) -> None:
            recording_threads.append(threading.get_ident())

        resolver_source = _source("src_meterloop01", "Resolver owner")
        resolver_service = _service(
            tmp_path / "resolver",
            sources=[resolver_source],
            outcomes=[
                _outcome(
                    RawOutcomeKind.SUCCESS,
                    status=200,
                    source_id=resolver_source.id,
                    usage=ProtocolUsageReport.of(
                        input_tokens=5,
                        cached_input_tokens=0,
                        output_tokens=1,
                    ),
                )
            ],
        )
        resolver_service.usage.record_many = capture
        requested_model = _canonicalize_fixed_test_routes(resolver_service)["codex"]
        await resolver_service.resolve(
            backend="codex",
            model_id=requested_model,
            request=ModelHubRequest(
                {"model": requested_model, "input": "ping"},
                protocol="openai_responses",
            ),
            supply_channel="hub",
        )

        gateway_source = _source("src_meterloop02", "Gateway owner")
        gateway_service = _service(
            tmp_path / "gateway",
            sources=[gateway_source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS, status=200, source_id=gateway_source.id
                    ),
                    (json.dumps({"id": "resp", "usage": {"input_tokens": 9}}).encode(),),
                )
            ],
        )
        gateway_service.usage.record_many = capture
        gateway_model = _canonicalize_fixed_test_routes(gateway_service)["codex"]
        gateway = ModelHubTurnGateway(gateway_service)
        await gateway._handle_request(
            _prepared_gateway_request(
                gateway,
                turn_id="turn_meter_offloop",
                requested_model=gateway_model,
                source_id=gateway_source.id,
                stream=False,
            )
        )

        assert len(recording_threads) == 2
        assert threading.get_ident() not in recording_threads

    asyncio.run(exercise())


def test_a_usage_ledger_failure_cannot_change_the_served_turn(tmp_path: Path) -> None:
    """Metering is a report, so a ledger fault must stay invisible downstream."""

    async def exercise() -> None:
        source = _source("src_meterbust01", "Broken ledger")
        body = json.dumps({"id": "resp_ok", "usage": {"input_tokens": 7}}).encode("utf-8")
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(RawOutcomeKind.SUCCESS, status=200, source_id=source.id),
                    (body,),
                )
            ],
        )
        service.usage.record_many = Mock(side_effect=OSError("read-only state directory"))
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_broken_ledger",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )

        result = await gateway._handle_request(request)

        assert result.status == 200
        assert result.body == body
        service.usage.record_many.assert_called_once()
        assert service.store.load().sources[0].state.status == "standby"

    asyncio.run(exercise())


def test_a_settlement_that_raises_still_meters_the_call_it_was_settling(
    tmp_path: Path,
) -> None:
    """MH-USAGE-010: the vendor billed the call before our bookkeeping got a turn.

    Metering used to be each ending's own step, placed after that ending's
    bookkeeping, so a settlement that raised on the way through took the row with
    it. Both shapes, because the two reach the ending from different frames and
    each used to carry its own copy of the order — and `requests == 1` rather than
    `>= 1`, because the boundary that catches the raise re-enters the same ending.
    """

    async def exercise(stream: bool) -> None:
        state = tmp_path / ("stream" if stream else "buffer")
        state.mkdir()
        source = _source("src_meterraise1", "Broken settlement")
        chunks = (
            (
                b'event: response.completed\ndata: {"type":"response.completed",'
                b'"sequence_number":1,"response":{"usage":{"input_tokens":256,'
                b'"output_tokens":12}}}\n\n',
            )
            if stream
            else (b'{"usage":{"input_tokens":256,"output_tokens":12}}',)
        )
        service = _service(
            state,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS,
                        status=200,
                        source_id=source.id,
                        stream_started=stream,
                        usage=(
                            None
                            if stream
                            else ProtocolUsageReport.of(
                                input_tokens=256,
                                cached_input_tokens=0,
                                output_tokens=12,
                            )
                        ),
                    ),
                    chunks,
                )
            ],
        )

        async def exploding_settlement(*args: object, **kwargs: object):
            raise RuntimeError("settlement exploded")

        service.settle_handle_outcome = exploding_settlement
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id=f"turn_meter_raise_{'stream' if stream else 'buffer'}",
            requested_model=requested_model,
            source_id=source.id,
            stream=stream,
        )

        with patch(
            "core.handlers.model_hub.turn_gateway.web.StreamResponse",
            return_value=FakeStreamResponse(),
        ):
            with pytest.raises(RuntimeError, match="settlement exploded"):
                await asyncio.wait_for(gateway._handle_request(request), timeout=5)
        await gateway.close()

        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1, stream
        assert metered["token_reports"] == 1, stream
        assert metered["input_tokens"] == 256, stream
        assert metered["output_tokens"] == 12, stream

    for stream in (False, True):
        asyncio.run(exercise(stream))


def test_a_ledger_that_stopped_answering_cannot_hold_the_served_turn_open(
    tmp_path: Path,
) -> None:
    """MH-USAGE-011: waiting for the row is an ordering convenience, not a gate.

    Metering waits out its own write so a client that opens the usage tab right
    after its call already sees that call. Unbounded, the convenience becomes the
    turn's critical path: hold the one thread ledger writes run on and the served
    response waits behind a disk that has stopped answering. Bounded, the turn is
    served and the row stays queued — timed out is not dropped, which is the other
    half of the same wait.
    """

    async def exercise() -> None:
        source = _source("src_meterhang01", "Unresponsive ledger")
        body = json.dumps({"usage": {"input_tokens": 64, "output_tokens": 4}}).encode("utf-8")
        service = _service(
            tmp_path,
            sources=[source],
            live_handles=[
                LiveInvokeHandle(
                    _outcome(
                        RawOutcomeKind.SUCCESS,
                        status=200,
                        source_id=source.id,
                        usage=ProtocolUsageReport.of(
                            input_tokens=64,
                            cached_input_tokens=0,
                            output_tokens=4,
                        ),
                    ),
                    (body,),
                )
            ],
        )
        service.usage_writer = UsageWriter(service.usage, durability_wait=0.05)
        requested_model = _canonicalize_fixed_test_routes(service)["codex"]
        gateway = ModelHubTurnGateway(service)
        request = _prepared_gateway_request(
            gateway,
            turn_id="turn_meter_hung_ledger",
            requested_model=requested_model,
            source_id=source.id,
            stream=False,
        )

        with _occupied_ledger_writer():
            result = await asyncio.wait_for(gateway._handle_request(request), timeout=1)
            assert result.status == 200
            assert result.body == body
            # The scenario is only the scenario while the write is still queued,
            # and a queued write is still the writer's to finish.
            assert service.usage_writer.unpersisted == 1
            assert _usage_of(service, source.id) == {}

        assert await service.usage_writer.drain(timeout=5) == 0
        metered = _usage_of(service, source.id)
        assert metered["requests"] == 1
        assert metered["input_tokens"] == 64
        assert metered["output_tokens"] == 4

    asyncio.run(exercise())
