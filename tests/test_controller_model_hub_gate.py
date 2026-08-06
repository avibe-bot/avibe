from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from config.v2_config import V2Config
from core.controller import Controller
from modules.agents.model_hub import resolve_model_hub_launch, resolve_opencode_overlay_launch


def test_controller_leaves_model_hub_aggregate_absent_by_default(monkeypatch):
    import core.handlers.model_hub as model_hub

    factory_calls = 0

    def create_service():
        nonlocal factory_calls
        factory_calls += 1
        return object()

    monkeypatch.delenv("VIBE_MODEL_HUB_ENABLED", raising=False)
    monkeypatch.setattr(model_hub, "create_default_service", create_service)
    controller = Controller.__new__(Controller)

    controller._init_model_hub()

    assert controller.model_hub_service is None
    assert controller.model_hub_turn_gateway is None
    assert controller.model_hub_runtime is None
    assert factory_calls == 0


def test_controller_builds_one_model_hub_aggregate_after_explicit_opt_in(monkeypatch):
    import core.handlers.model_hub as model_hub
    import core.handlers.model_hub.turn_gateway as turn_gateway
    import modules.agents.model_hub as agent_model_hub

    service = object()
    calls = []

    class Gateway:
        def __init__(self, value):
            calls.append(("gateway", value))
            self.service = value

    class Router:
        def __init__(self, *, service, turn_gateway):
            calls.append(("router", service, turn_gateway))
            self.service = service
            self.turn_gateway = turn_gateway

    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    captured = {}

    def create_service(**kwargs):
        captured.update(kwargs)
        return service

    monkeypatch.setattr(model_hub, "create_default_service", create_service)
    monkeypatch.setattr(turn_gateway, "ModelHubTurnGateway", Gateway)
    monkeypatch.setattr(agent_model_hub, "ModelHubRuntimeRouter", Router)
    controller = Controller.__new__(Controller)
    controller.vibe_agent_store = SimpleNamespace(
        get_default_agent=lambda: SimpleNamespace(
            backend="codex",
            model="agent-model",
        )
    )

    controller._init_model_hub()

    assert controller.model_hub_service is service
    assert controller.model_hub_turn_gateway.service is service
    assert controller.model_hub_runtime.service is service
    assert controller.model_hub_runtime.turn_gateway is controller.model_hub_turn_gateway
    assert captured["requested_model_override"]("codex") == "agent-model"
    assert captured["requested_model_override"]("claude") is None
    assert calls == [
        ("gateway", service),
        ("router", service, controller.model_hub_turn_gateway),
    ]


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_disabled_controller_resolver_is_direct_without_loading_model_hub_config(
    monkeypatch,
    backend,
):
    def corrupt_config(*_args, **_kwargs):
        raise json.JSONDecodeError("corrupt model_hub config", "{", 1)

    monkeypatch.setattr(V2Config, "load", classmethod(corrupt_config))
    controller = Controller.__new__(Controller)
    controller.model_hub_runtime = None

    launch = asyncio.run(resolve_model_hub_launch(controller, backend, f"{backend}-native"))

    assert launch.backend == backend
    assert launch.channel == "direct"
    assert launch.requested_model == f"{backend}-native"
    assert launch.target_model == f"{backend}-native"
    assert launch.runtime_model == f"{backend}-native"


def test_disabled_controller_opencode_overlay_resolver_is_direct_without_config_read(monkeypatch):
    def corrupt_config(*_args, **_kwargs):
        raise json.JSONDecodeError("corrupt model_hub config", "{", 1)

    monkeypatch.setattr(V2Config, "load", classmethod(corrupt_config))
    controller = Controller.__new__(Controller)
    controller.model_hub_runtime = None

    launch = asyncio.run(resolve_opencode_overlay_launch(controller, "openai/gpt-direct", None))

    assert launch.channel == "direct"
    assert launch.runtime_model == "openai/gpt-direct"
