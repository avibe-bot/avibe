import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.scenario_harness.auth_setup import AuthSetupScenarioHarness, FakeProcess
from tests.scenario_harness.core import ScenarioExpect, ScenarioRunner, ScenarioStep


class _FakeNextTurnRuntime:
    def __init__(self):
        self.refreshed = False
        self.cleared_settings_keys = []

    async def refresh(self):
        self.refreshed = True

    async def clear_sessions(self, settings_key: str):
        self.cleared_settings_keys.append(settings_key)

    def run_turn(self, settings_key: str) -> str:
        assert self.refreshed, "Expected runtime to be refreshed before the next turn"
        assert settings_key in self.cleared_settings_keys, "Expected stale sessions to be cleared before the next turn"
        return "turn-ok"


class _FakeCodexNextTurnRuntime:
    def __init__(self):
        self.refreshed = False

    async def refresh(self):
        self.refreshed = True

    def run_turn(self) -> str:
        assert self.refreshed, "Expected Codex runtime to be refreshed before the next turn"
        return "turn-ok"


class AgentAuthSetupScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_failure_scenario_emits_reset_path(self):
        """Scenario: AUTH-SETUP-202"""
        harness = AuthSetupScenarioHarness()
        fake_process = FakeProcess()
        runner = ScenarioRunner(harness)
        harness.service._start_codex_process = AsyncMock(return_value=fake_process)
        harness.service._read_codex_output = AsyncMock(return_value=None)
        harness.service._verify_login = AsyncMock(return_value=(False, "not logged in"))
        harness.service._refresh_backend_runtime = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
            ScenarioStep(
                "emit_device_url",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Open this URL to authenticate: https://auth.openai.com/codex/device",
                ),
            ),
        )

        fake_process.finish(0)
        await harness.flow("codex").waiter_task

        harness.service._refresh_backend_runtime.assert_not_awaited()
        ScenarioExpect.step_history(runner, ["start_setup", "emit_device_url"])
        ScenarioExpect.text_contains(harness, "failed")
        ScenarioExpect.text_contains(harness, "not logged in")
        ScenarioExpect.button_callback_contains(harness, "auth_setup:codex")
        ScenarioExpect.flow_missing(harness, "C1:codex")

    async def test_claude_startup_cleanup_failure_emits_reset_path(self):
        """Scenario: AUTH-SETUP-208"""
        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        harness.service._start_claude_control_flow = AsyncMock(
            side_effect=RuntimeError("Failed to clear Claude Code settings env")
        )

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
        )

        ScenarioExpect.step_history(runner, ["start_setup"])
        ScenarioExpect.text_contains(harness, "starting claude", index=0)
        ScenarioExpect.text_contains(harness, "failed", index=1)
        ScenarioExpect.text_contains(harness, "Failed to clear Claude Code settings env", index=1)
        ScenarioExpect.button_callback_contains(harness, "auth_setup:claude")
        ScenarioExpect.flow_missing(harness, "C1:claude")

    async def test_codex_reentry_scenario_replaces_existing_flow(self):
        """Scenario: AUTH-SETUP-201"""
        harness = AuthSetupScenarioHarness()
        first_process = FakeProcess()
        second_process = FakeProcess()
        runner = ScenarioRunner(harness)
        harness.service._start_codex_process = AsyncMock(side_effect=[first_process, second_process])
        harness.service._read_codex_output = AsyncMock(return_value=None)

        await runner.run(
            ScenarioStep(
                "start_first_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
        )
        first_flow = harness.flow("codex")
        self.assertFalse(first_flow.waiter_task.done())

        await runner.run(
            ScenarioStep(
                "start_second_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
        )

        second_flow = harness.flow("codex")
        self.assertIsNot(first_flow, second_flow)
        self.assertTrue(first_flow.waiter_task.cancelled())
        self.assertGreaterEqual(first_process.terminate_calls, 1)
        ScenarioExpect.step_history(runner, ["start_first_setup", "start_second_setup"])
        ScenarioExpect.text_contains(harness, "starting codex", index=0)
        ScenarioExpect.text_contains(harness, "starting codex", index=1)

    async def test_codex_device_auth_scenario_reaches_terminal_success(self):
        """Scenario: AUTH-SETUP-001"""
        harness = AuthSetupScenarioHarness()
        fake_process = FakeProcess()
        runner = ScenarioRunner(harness)
        harness.service._start_codex_process = AsyncMock(return_value=fake_process)
        harness.service._read_codex_output = AsyncMock(return_value=None)
        harness.service._verify_login = AsyncMock(return_value=(True, "Logged in using ChatGPT"))
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._persist_backend_auth_mode = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
            ScenarioStep(
                "emit_device_url",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Open this URL to authenticate: https://auth.openai.com/codex/device",
                ),
            ),
            ScenarioStep(
                "emit_device_code",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Then enter this code: T74L-XU61D",
                ),
            ),
        )

        flow = harness.flow("codex")
        self.assertFalse(flow.waiter_task.done())
        fake_process.finish(0)
        await flow.waiter_task

        harness.service._persist_backend_auth_mode.assert_awaited_once_with("codex", "oauth")
        harness.service._refresh_backend_runtime.assert_awaited_once_with("codex")
        ScenarioExpect.step_history(runner, ["start_setup", "emit_device_url", "emit_device_code"])
        ScenarioExpect.text_contains(harness, "starting codex", index=0)
        ScenarioExpect.text_contains(harness, "https://auth.openai.com/codex/device", index=1)
        ScenarioExpect.text_contains(harness, "T74L-XU61D", index=1)
        ScenarioExpect.text_contains(harness, "codex login is active again")
        ScenarioExpect.flow_missing(harness, "C1:codex")

    async def test_codex_successful_setup_refreshes_runtime_before_the_next_turn(self):
        """Scenario: AUTH-SETUP-901"""
        harness = AuthSetupScenarioHarness()
        fake_process = FakeProcess()
        runtime = _FakeCodexNextTurnRuntime()
        runner = ScenarioRunner(harness)
        harness.controller.agent_service.agents["codex"] = SimpleNamespace(
            refresh_auth_state=AsyncMock(side_effect=runtime.refresh)
        )
        harness.service._start_codex_process = AsyncMock(return_value=fake_process)
        harness.service._read_codex_output = AsyncMock(return_value=None)
        harness.service._verify_login = AsyncMock(return_value=(True, "Logged in using ChatGPT"))

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
            ScenarioStep(
                "emit_device_url",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Open this URL to authenticate: https://auth.openai.com/codex/device",
                ),
            ),
        )

        flow = harness.flow("codex")
        fake_process.finish(0)
        await flow.waiter_task

        await runner.run(
            ScenarioStep(
                "next_turn_after_success",
                lambda h: runtime.run_turn(),
            ),
        )

        harness.controller.agent_service.agents["codex"].refresh_auth_state.assert_awaited_once()
        ScenarioExpect.step_history(runner, ["start_setup", "emit_device_url", "next_turn_after_success"])
        ScenarioExpect.text_contains(harness, "codex login is active again")
        ScenarioExpect.flow_missing(harness, "C1:codex")

    async def test_claude_wrong_user_cannot_submit_callback_into_active_flow(self):
        """Scenario: AUTH-SETUP-103"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        runner = ScenarioRunner(harness)
        callback_payloads = []
        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )
        harness.service._wait_for_claude_completion = AsyncMock(return_value=None)
        harness.service._send_claude_callback = AsyncMock(
            side_effect=lambda client, authorization_code, state: callback_payloads.append((client, authorization_code, state))
        )

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
        )

        intruder_context = harness.make_context(user_id="U2")
        consumed = await harness.service.maybe_consume_setup_reply(intruder_context, "auth-code#oauth-state")
        self.assertFalse(consumed)
        self.assertEqual(callback_payloads, [])

        await runner.run(
            ScenarioStep(
                "intruder_submit_callback",
                lambda h: h.service.submit_code(intruder_context, "auth-code#oauth-state", backend_hint="claude"),
            ),
        )

        ScenarioExpect.step_history(runner, ["start_setup", "intruder_submit_callback"])
        ScenarioExpect.text_contains(harness, "starting claude", index=0)
        ScenarioExpect.text_contains(harness, "https://platform.claude.com/oauth/code/callback", index=1)
        ScenarioExpect.text_contains(harness, "only the user who started this setup flow")
        self.assertIn("C1:claude", harness.service._flows)
        self.assertEqual(callback_payloads, [])

    async def test_callback_submission_and_fallback_command_do_not_double_consume_claude_flow(self):
        """Scenario: AUTH-SETUP-105"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        completion_released = asyncio.Event()
        callback_payloads = []
        runner = ScenarioRunner(harness)

        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )

        async def fake_control_request(client, request, timeout=900.0):
            self.assertIs(client, fake_client)
            if request["subtype"] == "claude_oauth_wait_for_completion":
                await completion_released.wait()
                return {}
            raise AssertionError(f"unexpected control request: {request}")

        async def fake_send_callback(client, authorization_code, state):
            self.assertIs(client, fake_client)
            callback_payloads.append((authorization_code, state))
            completion_released.set()

        harness.service._send_claude_control_request = AsyncMock(side_effect=fake_control_request)
        harness.service._send_claude_callback = AsyncMock(side_effect=fake_send_callback)
        harness.service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._disconnect_claude_client = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
            ScenarioStep(
                "submit_plain_callback",
                lambda h: h.service.maybe_consume_setup_reply(h.context, "auth-code#oauth-state"),
            ),
        )

        flow = harness.flow("claude")
        await flow.waiter_task
        self.assertEqual(callback_payloads, [("auth-code", "oauth-state")])

        await runner.run(
            ScenarioStep(
                "submit_fallback_after_completion",
                lambda h: h.service.handle_setup_command(h.context, "code auth-code#oauth-state"),
            ),
        )

        ScenarioExpect.step_history(runner, ["start_setup", "submit_plain_callback", "submit_fallback_after_completion"])
        ScenarioExpect.text_contains(harness, "submitted")
        ScenarioExpect.text_contains(harness, "there is no active setup flow")
        self.assertEqual(callback_payloads, [("auth-code", "oauth-state")])
        ScenarioExpect.flow_missing(harness, "C1:claude")

    async def test_claude_manual_callback_scenario_accepts_plain_reply_and_completes(self):
        """Scenario: AUTH-SETUP-002"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        completion_released = asyncio.Event()
        callback_payloads = []
        runner = ScenarioRunner(harness)

        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )

        async def fake_control_request(client, request, timeout=900.0):
            self.assertIs(client, fake_client)
            if request["subtype"] == "claude_oauth_wait_for_completion":
                await completion_released.wait()
                return {}
            raise AssertionError(f"unexpected control request: {request}")

        async def fake_send_callback(client, authorization_code, state):
            self.assertIs(client, fake_client)
            callback_payloads.append((authorization_code, state))
            completion_released.set()

        harness.service._send_claude_control_request = AsyncMock(side_effect=fake_control_request)
        harness.service._send_claude_callback = AsyncMock(side_effect=fake_send_callback)
        harness.service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._disconnect_claude_client = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
            ScenarioStep(
                "submit_callback_reply",
                lambda h: h.service.maybe_consume_setup_reply(h.context, "auth-code#oauth-state"),
            ),
        )

        flow = harness.flow("claude")
        self.assertFalse(flow.waiter_task.done())
        await flow.waiter_task

        self.assertEqual(callback_payloads, [("auth-code", "oauth-state")])
        harness.service._refresh_backend_runtime.assert_awaited_once_with("claude")
        harness.service._disconnect_claude_client.assert_awaited_once_with(fake_client)
        ScenarioExpect.step_history(runner, ["start_setup", "submit_callback_reply"])
        ScenarioExpect.text_contains(harness, "starting claude", index=0)
        ScenarioExpect.text_contains(harness, "https://platform.claude.com/oauth/code/callback", index=1)
        ScenarioExpect.text_contains(harness, "submitted", index=2)
        ScenarioExpect.text_contains(harness, "claude login is active again")
        ScenarioExpect.flow_missing(harness, "C1:claude")

    async def test_claude_malformed_callback_keeps_flow_active_and_instructs_retry(self):
        """Scenario: AUTH-SETUP-102"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        runner = ScenarioRunner(harness)
        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )
        harness.service._wait_for_claude_completion = AsyncMock(return_value=None)
        harness.service._send_claude_callback = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
            ScenarioStep(
                "submit_malformed_callback",
                lambda h: h.service.submit_code(h.context, "not-a-valid-callback", backend_hint="claude"),
            ),
        )

        harness.service._send_claude_callback.assert_not_awaited()
        ScenarioExpect.step_history(runner, ["start_setup", "submit_malformed_callback"])
        ScenarioExpect.text_contains(harness, "authorizationCode#state")
        self.assertIn("C1:claude", harness.service._flows)

    async def test_concurrent_setup_flows_route_replies_to_the_matching_backend(self):
        """Scenario: AUTH-SETUP-205"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        completion_released = asyncio.Event()
        callback_payloads = []
        runner = ScenarioRunner(harness)

        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )
        harness.service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        harness.service._install_opencode_api_key = AsyncMock()
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._clear_backend_sessions_for_context = AsyncMock()

        async def fake_control_request(client, request, timeout=900.0):
            self.assertIs(client, fake_client)
            if request["subtype"] == "claude_oauth_wait_for_completion":
                await completion_released.wait()
                return {}
            raise AssertionError(f"unexpected control request: {request}")

        async def fake_send_callback(client, authorization_code, state):
            self.assertIs(client, fake_client)
            callback_payloads.append((authorization_code, state))
            completion_released.set()

        harness.service._send_claude_control_request = AsyncMock(side_effect=fake_control_request)
        harness.service._send_claude_callback = AsyncMock(side_effect=fake_send_callback)
        harness.service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))

        await runner.run(
            ScenarioStep(
                "start_claude_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
            ScenarioStep(
                "start_opencode_setup",
                lambda h: h.service.start_setup(h.context, backend="opencode", force_reset=True),
            ),
        )

        consumed_callback = await harness.service.maybe_consume_setup_reply(harness.context, "auth-code#oauth-state")
        self.assertTrue(consumed_callback)
        self.assertEqual(callback_payloads, [("auth-code", "oauth-state")])
        self.assertTrue(harness.flow("opencode").awaiting_code)
        self.assertIn("C1:claude", harness.service._flows)

        consumed_credential = await harness.service.maybe_consume_setup_reply(
            harness.context,
            "oc_live_Abcdef1234567890",
        )
        self.assertTrue(consumed_credential)

        await harness.flow("claude").waiter_task

        harness.service._install_opencode_api_key.assert_awaited_once_with("opencode", "oc_live_Abcdef1234567890")
        harness.service._clear_backend_sessions_for_context.assert_any_await("opencode", harness.context)
        ScenarioExpect.step_history(runner, ["start_claude_setup", "start_opencode_setup"])
        ScenarioExpect.text_contains(harness, "starting claude", index=0)
        ScenarioExpect.text_contains(harness, "starting opencode", index=2)
        ScenarioExpect.text_contains(harness, "submitted")
        ScenarioExpect.text_contains(harness, "opencode login is active again")
        ScenarioExpect.text_contains(harness, "claude login is active again")
        ScenarioExpect.flow_missing(harness, "C1:claude")
        ScenarioExpect.flow_missing(harness, "C1:opencode")

    async def test_claude_timeout_emits_recoverable_terminal_state(self):
        """Scenario: AUTH-SETUP-203"""
        harness = AuthSetupScenarioHarness()
        fake_client = object()
        runner = ScenarioRunner(harness)
        completion_started = asyncio.Event()
        release_completion = asyncio.Event()

        harness.service.setup_timeout_seconds = 0.01
        harness.service._start_claude_control_flow = AsyncMock(
            return_value=(fake_client, "https://platform.claude.com/oauth/code/callback", None)
        )

        async def fake_control_request(client, request, timeout=900.0):
            self.assertIs(client, fake_client)
            if request["subtype"] == "claude_oauth_wait_for_completion":
                completion_started.set()
                await release_completion.wait()
                return {}
            raise AssertionError(f"unexpected control request: {request}")

        harness.service._send_claude_control_request = AsyncMock(side_effect=fake_control_request)
        harness.service._disconnect_claude_client = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
        )

        flow = harness.flow("claude")
        await completion_started.wait()
        await flow.waiter_task

        ScenarioExpect.step_history(runner, ["start_setup"])
        ScenarioExpect.text_contains(harness, "timed out")
        ScenarioExpect.button_callback_contains(harness, "auth_setup:claude")
        ScenarioExpect.flow_missing(harness, "C1:claude")
        harness.service._disconnect_claude_client.assert_awaited_once_with(fake_client)

    async def test_opencode_direct_key_scenario_installs_key_and_refreshes_runtime(self):
        """Scenario: AUTH-SETUP-003"""
        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        harness.service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        harness.service._install_opencode_api_key = AsyncMock()
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._clear_backend_sessions_for_context = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="opencode", force_reset=True),
            ),
        )
        flow = harness.flow("opencode")
        self.assertTrue(flow.awaiting_code)
        self.assertEqual(flow.url, "https://opencode.ai/auth")

        await runner.run(
            ScenarioStep(
                "submit_direct_credential",
                lambda h: h.service.maybe_consume_setup_reply(
                    h.context,
                    "oc_live_Abcdef1234567890",
                ),
            ),
        )

        harness.service._install_opencode_api_key.assert_awaited_once_with("opencode", "oc_live_Abcdef1234567890")
        harness.service._refresh_backend_runtime.assert_awaited_once_with("opencode")
        harness.service._clear_backend_sessions_for_context.assert_awaited_once_with("opencode", harness.context)
        ScenarioExpect.step_history(runner, ["start_setup", "submit_direct_credential"])
        ScenarioExpect.text_contains(harness, "starting opencode", index=0)
        ScenarioExpect.text_contains(harness, "https://opencode.ai/auth", index=1)
        ScenarioExpect.text_contains(harness, "opencode login is active again")
        ScenarioExpect.flow_missing(harness, "C1:opencode")

    async def test_opencode_invalid_reply_keeps_flow_recoverable_until_valid_retry(self):
        """Scenario: AUTH-SETUP-104"""
        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        harness.service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        harness.service._install_opencode_api_key = AsyncMock()
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._clear_backend_sessions_for_context = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="opencode", force_reset=True),
            ),
        )

        flow = harness.flow("opencode")
        before_count = len(harness.rendered_texts())
        consumed_invalid = await harness.service.maybe_consume_setup_reply(harness.context, "--------------------")
        self.assertFalse(consumed_invalid)
        self.assertTrue(flow.awaiting_code)
        self.assertEqual(len(harness.rendered_texts()), before_count)
        harness.service._install_opencode_api_key.assert_not_awaited()

        await runner.run(
            ScenarioStep(
                "submit_valid_retry",
                lambda h: h.service.maybe_consume_setup_reply(
                    h.context,
                    "oc_live_Abcdef1234567890",
                ),
            ),
        )

        harness.service._install_opencode_api_key.assert_awaited_once_with("opencode", "oc_live_Abcdef1234567890")
        harness.service._refresh_backend_runtime.assert_awaited_once_with("opencode")
        harness.service._clear_backend_sessions_for_context.assert_awaited_once_with("opencode", harness.context)
        ScenarioExpect.step_history(runner, ["start_setup", "submit_valid_retry"])
        ScenarioExpect.text_contains(harness, "opencode login is active again")
        ScenarioExpect.flow_missing(harness, "C1:opencode")

    async def test_successful_setup_refreshes_runtime_before_the_next_turn(self):
        """Scenario: AUTH-SETUP-204"""
        harness = AuthSetupScenarioHarness()
        runtime = _FakeNextTurnRuntime()
        runner = ScenarioRunner(harness)

        harness.controller.agent_service.agents["opencode"] = SimpleNamespace(
            clear_sessions=AsyncMock(side_effect=runtime.clear_sessions)
        )
        harness.service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        harness.service._install_opencode_api_key = AsyncMock()
        harness.service._refresh_opencode_server = AsyncMock(side_effect=runtime.refresh)

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="opencode", force_reset=True),
            ),
            ScenarioStep(
                "submit_direct_credential",
                lambda h: h.service.maybe_consume_setup_reply(
                    h.context,
                    "oc_live_Abcdef1234567890",
                ),
            ),
            ScenarioStep(
                "next_turn_after_success",
                lambda h: runtime.run_turn(h.context.channel_id),
            ),
        )

        harness.service._install_opencode_api_key.assert_awaited_once_with("opencode", "oc_live_Abcdef1234567890")
        harness.service._refresh_opencode_server.assert_awaited_once()
        ScenarioExpect.step_history(runner, ["start_setup", "submit_direct_credential", "next_turn_after_success"])
        ScenarioExpect.text_contains(harness, "opencode login is active again")
        ScenarioExpect.flow_missing(harness, "C1:opencode")

    async def test_timed_out_flow_allows_clean_restart_without_stale_state(self):
        """Scenario: AUTH-SETUP-206"""
        harness = AuthSetupScenarioHarness()
        first_client = object()
        second_client = object()
        first_started = asyncio.Event()
        first_release = asyncio.Event()
        second_started = asyncio.Event()
        second_release = asyncio.Event()
        runner = ScenarioRunner(harness)

        harness.service.setup_timeout_seconds = 0.01
        harness.service._start_claude_control_flow = AsyncMock(
            side_effect=[
                (first_client, "https://platform.claude.com/oauth/code/callback?attempt=1", None),
                (second_client, "https://platform.claude.com/oauth/code/callback?attempt=2", None),
            ]
        )

        async def fake_control_request(client, request, timeout=900.0):
            if request["subtype"] != "claude_oauth_wait_for_completion":
                raise AssertionError(f"unexpected control request: {request}")
            if client is first_client:
                first_started.set()
                await first_release.wait()
                return {}
            if client is second_client:
                second_started.set()
                await second_release.wait()
                return {}
            raise AssertionError(f"unexpected client: {client!r}")

        harness.service._send_claude_control_request = AsyncMock(side_effect=fake_control_request)
        harness.service._disconnect_claude_client = AsyncMock()
        harness.service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))
        harness.service._refresh_backend_runtime = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_first_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
        )

        first_flow = harness.flow("claude")
        await first_started.wait()
        await first_flow.waiter_task
        ScenarioExpect.flow_missing(harness, "C1:claude")

        await runner.run(
            ScenarioStep(
                "start_second_setup",
                lambda h: h.service.start_setup(
                    h.context,
                    backend="claude",
                    force_reset=True,
                    claude_login_method="console",
                ),
            ),
        )

        second_flow = harness.flow("claude")
        await second_started.wait()
        self.assertIsNot(first_flow, second_flow)
        self.assertIs(second_flow.claude_client, second_client)
        self.assertTrue(second_flow.login_prompt_sent)
        self.assertEqual(harness.service._start_claude_control_flow.await_count, 2)
        ScenarioExpect.step_history(runner, ["start_first_setup", "start_second_setup"])
        ScenarioExpect.text_contains(harness, "attempt=1")
        ScenarioExpect.text_contains(harness, "timed out")
        ScenarioExpect.text_contains(harness, "attempt=2")
        await harness.service._terminate_flow(second_flow)

    async def test_failed_codex_setup_does_not_leave_stale_runtime_for_next_attempt(self):
        """Scenario: AUTH-SETUP-207"""
        harness = AuthSetupScenarioHarness()
        first_process = FakeProcess()
        second_process = FakeProcess()
        runner = ScenarioRunner(harness)
        harness.service._start_codex_process = AsyncMock(side_effect=[first_process, second_process])
        harness.service._read_codex_output = AsyncMock(return_value=None)
        harness.service._verify_login = AsyncMock(side_effect=[(False, "not logged in"), (True, "Logged in using ChatGPT")])
        harness.service._refresh_backend_runtime = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_first_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
            ScenarioStep(
                "emit_first_device_url",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Open this URL to authenticate: https://auth.openai.com/codex/device?attempt=1",
                ),
            ),
        )

        first_flow = harness.flow("codex")
        first_process.finish(0)
        await first_flow.waiter_task
        ScenarioExpect.flow_missing(harness, "C1:codex")
        harness.service._refresh_backend_runtime.assert_not_awaited()

        await runner.run(
            ScenarioStep(
                "start_second_setup",
                lambda h: h.service.start_setup(h.context, backend="codex", force_reset=True),
            ),
            ScenarioStep(
                "emit_second_device_url",
                lambda h: h.service._handle_process_text(
                    h.context,
                    "codex",
                    "Open this URL to authenticate: https://auth.openai.com/codex/device?attempt=2",
                ),
            ),
        )

        second_flow = harness.flow("codex")
        second_process.finish(0)
        await second_flow.waiter_task

        harness.service._refresh_backend_runtime.assert_awaited_once_with("codex")
        ScenarioExpect.step_history(
            runner,
            ["start_first_setup", "emit_first_device_url", "start_second_setup", "emit_second_device_url"],
        )
        ScenarioExpect.text_contains(harness, "not logged in")
        ScenarioExpect.text_contains(harness, "codex login is active again")
        ScenarioExpect.flow_missing(harness, "C1:codex")

    async def test_opencode_waiting_key_scenario_ignores_plain_chat(self):
        """Scenario: AUTH-SETUP-101"""
        harness = AuthSetupScenarioHarness()
        runner = ScenarioRunner(harness)
        harness.service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        harness.service._install_opencode_api_key = AsyncMock()
        harness.service._refresh_backend_runtime = AsyncMock()
        harness.service._clear_backend_sessions_for_context = AsyncMock()

        await runner.run(
            ScenarioStep(
                "start_setup",
                lambda h: h.service.start_setup(h.context, backend="opencode", force_reset=True),
            ),
        )
        flow = harness.flow("opencode")
        self.assertTrue(flow.awaiting_code)
        before_count = len(harness.rendered_texts())

        consumed = await harness.service.maybe_consume_setup_reply(harness.context, "hello world")

        self.assertFalse(consumed)
        self.assertTrue(flow.awaiting_code)
        self.assertEqual(len(harness.rendered_texts()), before_count)
        harness.service._install_opencode_api_key.assert_not_awaited()
        ScenarioExpect.step_history(runner, ["start_setup"])


if __name__ == "__main__":
    unittest.main()
