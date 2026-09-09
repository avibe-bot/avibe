"""User-visible failure copy consumes exact Hub facts, not native error text."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from config.v2_config import ModelHubBackendModelConfig, ModelHubSourceStateConfig
from core.backend_failure import emit_backend_failure, emit_replayed_backend_failure
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.provenance import (
    render_turn_outcome_copy,
)
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter, bind_launch
from modules.im import MessageContext
from tests.test_model_hub_l3 import _canonicalize_fixed_test_routes, _outcome
from tests.test_model_hub_resolution import _config, _service, _source
from tests.test_model_hub_retry_policy import clock_service
from vibe.i18n import t


def unavailable(tmp_path, backend="codex"):
    source = _source("src_copy0001", ("模型/alpha",))
    source.display_name = "Relay 服务"
    source.state = ModelHubSourceStateConfig(
        status="cooldown",
        detail_key="models.source.cooldown.server_error",
        retry_at="2026-09-09T04:30:00+00:00",
    )
    config = _config([source], model="模型/alpha")
    config.agents[backend].models.append(ModelHubBackendModelConfig(id="模型/alpha", origin="manual"))
    service, _, _ = _service(tmp_path, config)
    config, resolution = service._inspect_terminal_chain(backend=backend, model_id="模型/alpha")
    return service._produce_exhausted_terminal_outcome(
        config=config, resolution=resolution,
    )


@pytest.mark.parametrize("backend", ["codex", "claude", "opencode"])
@pytest.mark.parametrize("platform", ["avibe", "slack"])
@pytest.mark.parametrize("language", ["en", "zh"])
async def test_shared_failure_uses_exact_hub_copy_and_preserves_terminal_evidence(
    tmp_path, backend, platform, language,
):
    projection = unavailable(tmp_path, backend)
    project = Mock(return_value=projection)
    controller = SimpleNamespace(
        config=SimpleNamespace(language=language),
        model_hub_turn_gateway=SimpleNamespace(
            correlation=SimpleNamespace(terminal_projection=project),
        ),
        emit_agent_message=AsyncMock(return_value="msg-failed"),
    )
    context = MessageContext(
        user_id="user", channel_id="channel", platform=platform,
        platform_specific={"turn_token": "turn-exact"},
    )
    diagnostic = (
        "Codex turn failed: unexpected status 503 Service Unavailable; "
        "url: http://127.0.0.1:49800/codex/v1/responses"
    )

    await emit_backend_failure(controller, context, backend, diagnostic)

    project.assert_called_once_with("turn-exact", backend=backend)
    notify, terminal = controller.emit_agent_message.await_args_list
    assert notify.args[1] == "notify"
    assert notify.args[2] == render_turn_outcome_copy(projection, language)
    assert "127.0.0.1" not in notify.args[2]
    assert "2026-09-09" not in notify.args[2]
    assert "Relay 服务" in notify.args[2]
    assert notify.kwargs["output"].metadata["event"] == "backend_failure"
    assert terminal.args[1:] == ("result", "")
    assert terminal.kwargs["terminal_error"] == diagnostic
    assert terminal.kwargs["level"] == "silent"


@pytest.mark.parametrize("identity", [None, "", 123, "different-turn"])
async def test_no_exact_projection_keeps_unrelated_native_failure(identity):
    project = Mock(return_value=None)
    controller = SimpleNamespace(
        model_hub_turn_gateway=SimpleNamespace(
            correlation=SimpleNamespace(terminal_projection=project),
        ),
        emit_agent_message=AsyncMock(),
    )
    context = MessageContext(
        user_id="user", channel_id="channel", platform="avibe",
        platform_specific={"turn_token": identity},
    )
    await emit_backend_failure(
        controller, context, "codex", "native failed",
        display_text="Native process exited unexpectedly",
    )
    assert controller.emit_agent_message.await_args_list[0].args[2] == (
        "Native process exited unexpectedly"
    )
    if identity == "different-turn":
        project.assert_called_once_with(identity, backend="codex")
    else:
        project.assert_not_called()


async def test_historical_notice_does_not_consume_a_live_turn_projection(tmp_path):
    project = Mock(return_value=unavailable(tmp_path))
    controller = SimpleNamespace(
        model_hub_turn_gateway=SimpleNamespace(
            correlation=SimpleNamespace(terminal_projection=project),
        ),
        emit_agent_message=AsyncMock(),
    )
    context = MessageContext(
        user_id="user", channel_id="channel", platform="avibe",
        platform_specific={"turn_token": "new-live-turn"},
    )
    await emit_replayed_backend_failure(
        controller, context, "codex", "old failure",
        display_text="Original failure notice", failure_id="old-failure",
    )
    project.assert_not_called()
    assert controller.emit_agent_message.await_count == 1
    assert controller.emit_agent_message.await_args.args[2] == "Original failure notice"


@pytest.mark.parametrize("backend,endpoint,status", [
    ("codex", "responses", 400), ("claude", "messages", 424),
])
@pytest.mark.parametrize("language", ["en", "zh"])
async def test_gateway_terminal_survives_native_recorder_before_shared_notice(
    tmp_path, backend, endpoint, status, language,
):
    """MH-RETRY-COPY-001: actual HTTP terminal -> native recorder -> one clean notice."""
    service, clock = clock_service(tmp_path, outcomes=[
        _outcome(RawOutcomeKind.NETWORK_ERROR, source_id="src_recovery01")
        for _ in range(20)
    ])
    service.store.load().sources[0].display_name = "Relay 服务"
    models = _canonicalize_fixed_test_routes(service)
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
    context = MessageContext(
        user_id="user", channel_id="channel", platform="avibe",
        platform_specific={"turn_token": "turn-copy"},
    )
    controller = SimpleNamespace(
        config=SimpleNamespace(language=language),
        model_hub_turn_gateway=gateway,
        emit_agent_message=AsyncMock(return_value="msg-failed"),
    )
    try:
        launch = await router.resolve(
            backend, models[backend], process_scope="fixture-copy", turn_id="turn-copy",
        )
        bind_launch(context, launch)
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=5)) as client:
            async with client.post(
                f"{launch.gateway_base_url}/v1/{endpoint}",
                headers={"Authorization": f"Bearer {launch.gateway_token}"},
                json={"model": launch.runtime_model, "stream": False},
            ) as response:
                assert response.status == status
                assert (await response.json())["error"]["code"] == "model_hub_recovery_exhausted"
                assert "Retry-After" not in response.headers
        assert clock.elapsed == 91
        assert len(service.adapter.invocations) == 8
        controller.emit_agent_message.assert_not_awaited()
        projection = gateway.correlation.terminal_projection("turn-copy", backend=backend)
        assert projection is not None
        diagnostic = f"Native error {status}, URL http://127.0.0.1/model-request"
        assert await router.record_native_failure(context, diagnostic) is False
        assert gateway.correlation.terminal_projection("turn-copy", backend=backend) == projection
        await emit_backend_failure(controller, context, backend, diagnostic)
        notice, terminal = controller.emit_agent_message.await_args_list
        assert notice.args[2] == render_turn_outcome_copy(projection, language)
        assert "Relay 服务" in notice.args[2]
        assert "127.0.0.1" not in notice.args[2]
        assert ("已结束" if language == "zh" else "has ended") in notice.args[2]
        assert terminal.kwargs["terminal_error"] == diagnostic
        assert gateway.correlation.recovery_snapshot("turn-copy") == []
    finally:
        await gateway.close()


@pytest.mark.parametrize("language", ["en", "zh"])
def test_terminal_copy_never_promises_automatic_recovery(language):
    """MH-RETRY-COPY-001: final failures never promise that a timer will recover a Source."""
    params = {"model": "test", "source": "relay", "retry_at": "2026-09-09T04:00:00Z"}
    for key in ("modelHub.launch.waiting", "modelHub.launch.waiting_without_retry"):
        text = t(key, language, **params)
        assert ("已结束" if language == "zh" else "has ended") in text
        assert "2026-09-09" not in text
        assert "自动恢复。" not in text
        assert "has recovered" not in text
