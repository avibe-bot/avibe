from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    from vibe import api

    service = object()
    calls = []

    class Gateway:
        def __init__(self, value, *, language_provider):
            calls.append(("gateway", value, language_provider))
            self.service = value
            self.language_provider = language_provider

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
    presence_probes = []
    probe_failure = [False]
    block_full_probe = [False]
    opencode_present = [False]
    full_probe_started = threading.Event()
    full_probe_release = threading.Event()

    def resolve_cli_paths(binaries, *, include_npm_global=True):
        presence_probes.append((binaries, include_npm_global))
        if probe_failure[0]:
            raise OSError("CLI inventory unavailable")
        result = {
            binary: f"/usr/bin/{binary}" if binary == "codex" else None
            for binary in binaries
        }
        if "opencode" in result and opencode_present[0]:
            result["opencode"] = "/usr/bin/opencode"
        if block_full_probe[0] and len(binaries) > 1:
            full_probe_started.set()
            full_probe_release.wait(timeout=2)
        return result

    monkeypatch.setattr(api, "resolve_cli_paths", resolve_cli_paths)
    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(language="zh")
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
    assert captured["cli_present_override"]("codex") is True
    assert captured["cli_present_override"]("claude") is False
    assert presence_probes == [(["claude", "codex", "opencode"], False)]
    probe_failure[0] = True
    captured["cli_presence_refresh"](True, ("opencode",))
    assert captured["cli_present_override"]("codex") is True
    assert presence_probes[-1] == (["opencode"], True)

    probe_failure[0] = False
    block_full_probe[0] = True
    full_refresh = threading.Thread(
        target=captured["cli_presence_refresh"],
        args=(True, None),
    )
    full_refresh.start()
    assert full_probe_started.wait(timeout=0.5)
    opencode_present[0] = True
    captured["cli_presence_refresh"](True, ("opencode",))
    full_probe_release.set()
    full_refresh.join(timeout=0.5)
    assert not full_refresh.is_alive()
    assert captured["cli_present_override"]("opencode") is True
    assert controller.model_hub_turn_gateway.language_provider() == "zh"
    assert calls == [
        ("gateway", service, controller.model_hub_turn_gateway.language_provider),
        ("router", service, controller.model_hub_turn_gateway),
    ]

    runtime_config = object()
    latest = SimpleNamespace(
        model_hub=SimpleNamespace(
            agents={"codex": SimpleNamespace(mode="hub")},
        ),
        agents=SimpleNamespace(codex=runtime_config),
    )
    controller.backend_restart_coordinator = SimpleNamespace(
        request_restart=AsyncMock(return_value="restarted"),
    )
    controller.agent_service = SimpleNamespace(
        invalidate_model_hub_runtime=AsyncMock(),
        refresh_runtime_config=AsyncMock(),
    )
    monkeypatch.setattr(V2Config, "load", classmethod(lambda _cls: latest))

    asyncio.run(captured["backend_catalog_changed"]("codex"))

    assert controller.config.model_hub is latest.model_hub
    controller.backend_restart_coordinator.request_restart.assert_awaited_once_with(
        "codex"
    )
    controller.agent_service.invalidate_model_hub_runtime.assert_not_awaited()
    controller.agent_service.refresh_runtime_config.assert_not_awaited()

    latest.model_hub.agents["codex"].mode = "direct"
    controller.backend_restart_coordinator.request_restart.reset_mock()

    asyncio.run(captured["backend_catalog_changed"]("codex"))

    controller.agent_service.invalidate_model_hub_runtime.assert_awaited_once_with(
        "codex"
    )
    controller.backend_restart_coordinator.request_restart.assert_not_awaited()
    controller.agent_service.refresh_runtime_config.assert_not_awaited()


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
