"""User-visible failure copy consumes exact Hub facts, not native error text."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.backend_failure import emit_backend_failure, emit_replayed_backend_failure
from core.handlers.model_hub.provenance import (
    TurnOutcomeProjectionInput,
    TurnSupplyFacts,
    render_turn_outcome_copy,
)
from modules.im import MessageContext
from vibe.i18n import t


def unavailable():
    return TurnOutcomeProjectionInput(
        outcome="exhausted",
        discriminator="final_supply_state",
        supply_facts=TurnSupplyFacts(
            backend="codex",
            model="模型/alpha",
            supply_state="waiting",
            source="Relay 服务",
            retry_at="2026-09-09T04:30:00+00:00",
        ),
    )


@pytest.mark.parametrize("backend", ["codex", "claude", "opencode"])
@pytest.mark.parametrize("platform", ["avibe", "slack"])
@pytest.mark.parametrize("language", ["en", "zh"])
async def test_shared_failure_uses_exact_hub_copy_and_preserves_terminal_evidence(
    backend, platform, language,
):
    projection = unavailable()
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


async def test_historical_notice_does_not_consume_a_live_turn_projection():
    project = Mock(return_value=unavailable())
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


@pytest.mark.parametrize("language", ["en", "zh"])
def test_terminal_copy_never_promises_automatic_recovery(language):
    params = {"model": "test", "source": "relay", "retry_at": "2026-09-09T04:00:00Z"}
    for key in ("modelHub.launch.waiting", "modelHub.launch.waiting_without_retry"):
        text = t(key, language, **params)
        assert ("已结束" if language == "zh" else "has ended") in text
        assert "2026-09-09" not in text
        assert "自动恢复。" not in text
        assert "has recovered" not in text
