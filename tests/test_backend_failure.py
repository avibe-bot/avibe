from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, call

from core.backend_failure import emit_backend_failure
from modules.agents.base import AgentRequest
from modules.im import MessageContext


def _request() -> AgentRequest:
    context = MessageContext(
        user_id="U1",
        channel_id="C1",
        platform="avibe",
        platform_specific={"turn_token": "turn-1"},
    )
    return AgentRequest(
        context=context,
        message="work",
        working_path="/tmp/work",
        base_session_id="session-1",
        composite_session_id="session-1:/tmp/work",
        session_key="avibe::project::p1",
    )


class BackendFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_recovery_owns_the_only_terminal_settlement(self) -> None:
        request = _request()
        controller = SimpleNamespace(
            agent_auth_service=SimpleNamespace(
                maybe_emit_auth_recovery_message=AsyncMock(return_value=True)
            ),
            emit_agent_message=AsyncMock(),
        )

        handled_auth = await emit_backend_failure(
            controller,
            request.context,
            "claude",
            "401 Unauthorized",
            request=request,
        )

        self.assertTrue(handled_auth)
        controller.emit_agent_message.assert_not_awaited()

    async def test_settles_lifecycle_when_auth_recovery_delivery_raises(self) -> None:
        request = _request()
        controller = SimpleNamespace(
            agent_auth_service=SimpleNamespace(
                maybe_emit_auth_recovery_message=AsyncMock(
                    side_effect=RuntimeError("button delivery unavailable")
                )
            ),
            emit_agent_message=AsyncMock(),
        )

        with self.assertRaisesRegex(RuntimeError, "button delivery unavailable"):
            await emit_backend_failure(
                controller,
                request.context,
                "codex",
                "401 Unauthorized",
                request=request,
            )

        controller.emit_agent_message.assert_awaited_once_with(
            request.context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=request.output,
            terminal_error="401 Unauthorized",
        )

    async def test_separates_notify_from_terminal_settlement(self) -> None:
        request = _request()
        controller = SimpleNamespace(
            agent_auth_service=SimpleNamespace(
                maybe_emit_auth_recovery_message=AsyncMock(return_value=False)
            ),
            emit_agent_message=AsyncMock(),
        )

        handled_auth = await emit_backend_failure(
            controller,
            request.context,
            "codex",
            "provider unavailable",
            display_text="Codex failed: provider unavailable",
            request=request,
        )

        self.assertFalse(handled_auth)
        auth_call = controller.agent_auth_service.maybe_emit_auth_recovery_message.await_args
        self.assertEqual(
            auth_call.args,
            (request.context, "codex", "Codex failed: provider unavailable"),
        )
        self.assertEqual(auth_call.kwargs["terminal_error"], "provider unavailable")
        terminal_output = auth_call.kwargs["output"]
        notify_call, terminal_call = controller.emit_agent_message.await_args_list
        self.assertEqual(
            notify_call,
            call(
                request.context,
                "notify",
                "Codex failed: provider unavailable",
                output=ANY,
            ),
        )
        self.assertFalse(notify_call.kwargs["output"].settles_run)
        self.assertEqual(
            notify_call.kwargs["output"].idempotency_key,
            "backend-failure:turn-1",
        )
        self.assertEqual(
            notify_call.kwargs["output"].metadata,
            {
                "backend": "codex",
                "event": "backend_failure",
                "failure_id": "turn-1",
            },
        )
        self.assertEqual(
            terminal_call,
            call(
                request.context,
                "result",
                "",
                is_error=True,
                level="silent",
                output=terminal_output,
                terminal_error="provider unavailable",
            ),
        )

    async def test_identity_is_stable_for_duplicate_terminal_events(self) -> None:
        request = _request()
        controller = SimpleNamespace(
            agent_auth_service=SimpleNamespace(
                maybe_emit_auth_recovery_message=AsyncMock(return_value=False)
            ),
            emit_agent_message=AsyncMock(),
        )

        await emit_backend_failure(controller, request.context, "claude", "server error", request=request)
        await emit_backend_failure(controller, request.context, "claude", "server error", request=request)

        notify_outputs = [
            item.kwargs["output"]
            for item in controller.emit_agent_message.await_args_list
            if item.args[1] == "notify"
        ]
        self.assertEqual(
            [output.idempotency_key for output in notify_outputs],
            ["backend-failure:turn-1", "backend-failure:turn-1"],
        )

    async def test_settles_lifecycle_when_notify_raises(self) -> None:
        request = _request()
        terminal_calls = []

        async def emit(_context, message_type, _text, **kwargs):
            if message_type == "notify":
                raise RuntimeError("delivery unavailable")
            terminal_calls.append(kwargs)

        controller = SimpleNamespace(
            agent_auth_service=SimpleNamespace(
                maybe_emit_auth_recovery_message=AsyncMock(return_value=False)
            ),
            emit_agent_message=emit,
        )

        with self.assertRaisesRegex(RuntimeError, "delivery unavailable"):
            await emit_backend_failure(
                controller,
                request.context,
                "opencode",
                "transport failed",
                request=request,
            )

        self.assertEqual(
            terminal_calls,
            [
                {
                    "is_error": True,
                    "level": "silent",
                    "output": request.output,
                    "terminal_error": "transport failed",
                }
            ],
        )
