import asyncio
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent_auth_service import (
    AgentAuthFlow,
    AgentAuthService,
    BackendLoginInProgressError,
    classify_auth_error,
    verify_opencode_auth_list_output,
)
from modules.claude_sdk_compat import CLAUDE_SDK_MAX_BUFFER_SIZE
from modules.im import MessageContext


class _IsolatedClaudeConfigDirMixin:
    def setUp(self):
        super().setUp()
        self._claude_config_dir_tmp = tempfile.TemporaryDirectory()
        self._previous_claude_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(self._claude_config_dir_tmp.name) / ".claude")
        # V2Config writes go through the cross-process transaction, which
        # resolves config.json from AVIBE_HOME — isolate it per test so
        # no test touches the developer's real ~/.avibe.
        self._avibe_home_tmp = tempfile.TemporaryDirectory()
        self._previous_avibe_home = os.environ.get("AVIBE_HOME")
        os.environ["AVIBE_HOME"] = self._avibe_home_tmp.name

    def tearDown(self):
        if self._previous_claude_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._previous_claude_config_dir
        self._claude_config_dir_tmp.cleanup()
        if self._previous_avibe_home is None:
            os.environ.pop("AVIBE_HOME", None)
        else:
            os.environ["AVIBE_HOME"] = self._previous_avibe_home
        self._avibe_home_tmp.cleanup()
        super().tearDown()


@contextmanager
def _temporary_vibe_home(tmp_path: Path):
    previous_home = os.environ.get("AVIBE_HOME")
    os.environ["AVIBE_HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if previous_home is None:
            os.environ.pop("AVIBE_HOME", None)
        else:
            os.environ["AVIBE_HOME"] = previous_home


class _StubIMClient:
    def __init__(self):
        self.sent_messages = []
        self.sent_button_messages = []

    async def send_message(self, context, text, parse_mode=None):
        self.sent_messages.append((context.channel_id, text))
        return "msg-1"

    async def send_message_with_buttons(self, context, text, keyboard, parse_mode=None):
        self.sent_button_messages.append((context.channel_id, text, keyboard))
        return "btn-1"


class _StubConfig(SimpleNamespace):
    def __init__(self):
        super().__init__(
            platform="slack",
            language="en",
            agents=SimpleNamespace(
                codex=SimpleNamespace(cli_path="codex", auth_mode="oauth"),
                claude=SimpleNamespace(cli_path="claude"),
                opencode=SimpleNamespace(cli_path="opencode"),
            ),
            save_calls=0,
        )

    def save(self):
        self.save_calls += 1


class _StubController:
    def __init__(self):
        self.config = _StubConfig()
        self.im_client = _StubIMClient()
        self.agent_service = SimpleNamespace(agents={})
        self.settings_manager = SimpleNamespace(sessions={})
        self.sessions = SimpleNamespace(get_agent_session_id=lambda *args, **kwargs: None)
        self.session_handler = SimpleNamespace(
            get_session_info=lambda context: ("base-1", "/tmp/workdir", "base-1:/tmp/workdir")
        )
        self.resolve_agent_for_context = AsyncMock(return_value="codex")
        # Outbound status chokepoint: a handled (auth) recovery settles the failed
        # turn by emitting a terminal error result through here.
        self.emit_agent_message = AsyncMock()

    def get_im_client_for_context(self, context):
        return self.im_client

    def _get_settings_key(self, context):
        return context.channel_id

    def _get_lang(self):
        return "en"

    def resolve_agent_for_context(self, context):
        return "codex"

    def get_opencode_overrides(self, context):
        return (None, None, None)


class AgentAuthServiceTests(_IsolatedClaudeConfigDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_handle_setup_command_submits_code(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        service.submit_code = AsyncMock()
        context = MessageContext(user_id="U1", channel_id="C1")

        await service.handle_setup_command(context, "code 123456")

        service.submit_code.assert_awaited_once_with(context, "123456")

    async def test_handle_setup_command_starts_explicit_backend(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        service.start_setup = AsyncMock()
        context = MessageContext(user_id="U1", channel_id="C1")

        await service.handle_setup_command(context, "claude")

        service.start_setup.assert_awaited_once_with(
            context,
            backend="claude",
            force_reset=True,
            claude_login_method=None,
        )

    async def test_handle_setup_command_supports_opencode_alias(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        service.start_setup = AsyncMock()
        context = MessageContext(user_id="U1", channel_id="C1")

        await service.handle_setup_command(context, "oc")

        service.start_setup.assert_awaited_once_with(
            context,
            backend="opencode",
            force_reset=True,
            claude_login_method=None,
        )

    async def test_handle_setup_command_supports_claude_console_login_option(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        service.start_setup = AsyncMock()
        context = MessageContext(user_id="U1", channel_id="C1")

        await service.handle_setup_command(context, "claude console")

        service.start_setup.assert_awaited_once_with(
            context,
            backend="claude",
            force_reset=True,
            claude_login_method="console",
        )

    async def test_maybe_emit_auth_recovery_message_sends_reset_button(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        with patch("core.message_mirror.persist_agent_message") as persist:
            handled = await service.maybe_emit_auth_recovery_message(
                context,
                "codex",
                "❌ Codex error: 401 Unauthorized",
            )

        self.assertTrue(handled)
        self.assertEqual(len(controller.im_client.sent_button_messages), 1)
        _, text, keyboard = controller.im_client.sent_button_messages[0]
        self.assertIn("401 Unauthorized", text)
        self.assertEqual(keyboard.buttons[0][0].callback_data, "auth_setup:codex")
        # The transient IM message references the inline reset button.
        self.assertIn("button", text.lower())
        # A durable notify is ALSO persisted (the single home for it) so the web Chat
        # shows the error + reset instruction, not just the transient button payload.
        # But the durable row has NO inline button, so the persisted copy must be
        # BUTTON-FREE and point at the cross-platform `/setup` command instead of
        # "the button below" (a dangling instruction on the workbench) (Codex P2).
        persist.assert_called_once()
        persisted_ctx, persisted_kind, persisted_text = persist.call_args.args[:3]
        self.assertEqual(persisted_kind, "notify")
        self.assertIn("401 Unauthorized", persisted_text)
        self.assertIn("/setup codex", persisted_text)  # actionable without a button
        self.assertNotIn("button", persisted_text.lower())  # no dangling button reference

    async def test_maybe_emit_auth_recovery_message_classifies_terminal_diagnostic(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        with patch("core.message_mirror.persist_agent_message"):
            handled = await service.maybe_emit_auth_recovery_message(
                context,
                "claude",
                "❌ Claude Code 进程已终止；会话已重置，请重试。",
                terminal_error="Cannot write to terminated process after OAuth login failed",
            )

        self.assertTrue(handled)
        self.assertEqual(len(controller.im_client.sent_button_messages), 1)

    async def test_maybe_emit_auth_recovery_message_settles_turn_for_auth_error(self):
        # An AUTH error is handled here (reset button + persisted notify). The
        # recovery message is a button row, not a result, so this settles the
        # failed turn through the OUTBOUND chokepoint: a terminal error result
        # that turns the dot red + releases the SSE waiter.
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        with patch("core.message_mirror.persist_agent_message"):
            handled = await service.maybe_emit_auth_recovery_message(
                context, "codex", "❌ Codex error: 401 Unauthorized"
            )

        self.assertTrue(handled)
        controller.emit_agent_message.assert_awaited_once_with(
            context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=ANY,
            terminal_error="❌ Codex error: 401 Unauthorized",
        )

    async def test_auth_recovery_carries_primary_receipt_into_turn_settlement(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={
                "turn_token": "turn-auth",
                "task_execution_id": "run-auth",
            },
        )

        with patch(
            "core.message_mirror.persist_agent_message",
            return_value={"id": "message-auth"},
        ):
            handled = await service.maybe_emit_auth_recovery_message(
                context,
                "codex",
                "❌ Codex error: 401 Unauthorized",
            )

        self.assertTrue(handled)
        terminal_output = controller.emit_agent_message.await_args.kwargs["output"]
        self.assertEqual(
            terminal_output.metadata["turn_failure_notification"],
            {
                "failure_id": "turn:turn-auth",
                "ack_evidence": "receipt",
                "delivered": True,
            },
        )

    async def test_auth_recovery_does_not_persist_an_undelivered_external_message(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        service._send_message_with_button = AsyncMock(return_value=None)
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={
                "turn_token": "turn-auth-undelivered",
                "task_execution_id": "run-auth-undelivered",
            },
        )

        with patch(
            "core.message_mirror.persist_agent_message",
            return_value={"id": "message-auth-undelivered"},
        ) as persist:
            handled = await service.maybe_emit_auth_recovery_message(
                context,
                "codex",
                "Codex error: 401 Unauthorized",
            )

        self.assertTrue(handled)
        persist.assert_not_called()
        terminal_output = controller.emit_agent_message.await_args.kwargs["output"]
        self.assertEqual(
            terminal_output.metadata["turn_failure_notification"],
            {
                "failure_id": "turn:turn-auth-undelivered",
                "ack_evidence": None,
                "delivered": False,
            },
        )

    async def test_auth_recovery_keeps_local_persistence_without_a_send_id(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        service._send_message_with_button = AsyncMock(return_value=None)
        context = MessageContext(
            user_id="U1",
            channel_id="ses-local",
            platform="avibe",
            platform_specific={
                "turn_token": "turn-auth-local",
                "task_execution_id": "run-auth-local",
            },
        )

        with patch(
            "core.message_mirror.persist_agent_message",
            return_value={"id": "message-auth-local"},
        ) as persist:
            handled = await service.maybe_emit_auth_recovery_message(
                context,
                "codex",
                "Codex error: 401 Unauthorized",
            )

        self.assertTrue(handled)
        persist.assert_called_once()
        terminal_output = controller.emit_agent_message.await_args.kwargs["output"]
        self.assertEqual(
            terminal_output.metadata["turn_failure_notification"],
            {
                "failure_id": "turn:turn-auth-local",
                "ack_evidence": "receipt",
                "delivered": True,
            },
        )

    async def test_auth_recovery_sends_and_persists_to_delivery_override(self):
        controller = _StubController()
        source_client = controller.im_client
        target_client = _StubIMClient()
        controller.get_im_client_for_context = lambda context: (
            target_client if context.platform == "telegram" else source_client
        )
        service = AgentAuthService(controller)
        context = MessageContext(
            user_id="U-source",
            channel_id="C-source",
            platform="slack",
            platform_specific={
                "turn_token": "turn-auth-routed",
                "task_execution_id": "run-auth-routed",
                "delivery_override": {
                    "user_id": "U-target",
                    "channel_id": "C-target",
                    "platform": "telegram",
                    "thread_id": "topic-target",
                },
            },
        )

        with patch(
            "core.message_mirror.persist_agent_message",
            return_value={"id": "message-auth-routed"},
        ) as persist:
            handled = await service.maybe_emit_auth_recovery_message(
                context,
                "codex",
                "❌ Codex error: 401 Unauthorized",
            )

        self.assertTrue(handled)
        self.assertEqual(source_client.sent_button_messages, [])
        self.assertEqual(target_client.sent_button_messages[0][0], "C-target")
        persisted_context = persist.call_args.args[0]
        self.assertEqual(
            (
                persisted_context.platform,
                persisted_context.channel_id,
                persisted_context.thread_id,
            ),
            ("telegram", "C-target", "topic-target"),
        )
        terminal_call = controller.emit_agent_message.await_args
        self.assertIs(terminal_call.args[0], context)
        self.assertTrue(
            terminal_call.kwargs["output"].metadata["turn_failure_notification"]["delivered"]
        )

    async def test_codex_api_key_auth_error_points_to_key_settings_without_oauth_button(self):
        from config.v2_compat import to_app_config
        from config.v2_config import AgentsConfig, RuntimeConfig, SlackConfig, V2Config

        controller = _StubController()
        v2_config = V2Config(
            mode="self_host",
            version="v2",
            slack=SlackConfig(),
            runtime=RuntimeConfig(default_cwd="."),
            agents=AgentsConfig(),
        )
        v2_config.agents.codex.auth_mode = "api_key"
        controller.config = to_app_config(v2_config)
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        with patch("core.message_mirror.persist_agent_message") as persist:
            handled = await service.maybe_emit_auth_recovery_message(
                context,
                "codex",
                "❌ Codex error: 401 Unauthorized",
            )

        self.assertTrue(handled)
        self.assertEqual(controller.im_client.sent_button_messages, [])
        self.assertEqual(len(controller.im_client.sent_messages), 1)
        _, text = controller.im_client.sent_messages[0]
        self.assertIn("API key", text)
        self.assertIn("Base URL", text)
        self.assertNotIn("OAuth", text)
        persisted_text = persist.call_args.args[2]
        self.assertIn("API key", persisted_text)
        self.assertNotIn("/setup codex", persisted_text)

    async def test_maybe_emit_auth_recovery_message_defers_non_auth_error_to_caller(self):
        # A NON-auth terminal error returns False: the calling backend emits its
        # OWN terminal error result (which settles the dot via the same outbound
        # chokepoint), so this method must NOT also settle it here.
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        handled = await service.maybe_emit_auth_recovery_message(
            context, "codex", "RuntimeError: connection reset by peer"
        )

        self.assertFalse(handled)  # not an auth error → no recovery button
        controller.emit_agent_message.assert_not_awaited()

    async def test_handle_process_text_emits_codex_link_once_url_and_code_exist(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-1",
            backend="codex",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
        )
        service._flows[flow.flow_key] = flow

        await service._handle_process_text(context, "codex", "https://auth.openai.com/codex/device")
        await service._handle_process_text(context, "codex", "T74L-XU61D")

        self.assertEqual(len(controller.im_client.sent_messages), 1)
        _, text = controller.im_client.sent_messages[0]
        self.assertIn("T74L-XU61D", text)
        self.assertIn("https://auth.openai.com/codex/device", text)

    async def test_start_setup_starts_codex_waiter(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        process = SimpleNamespace(stdout=object(), returncode=None)
        service._start_codex_process = AsyncMock(return_value=process)
        service._read_codex_output = AsyncMock()
        service._wait_for_completion = AsyncMock()

        await service.start_setup(context, backend="codex", force_reset=True)
        await asyncio.sleep(0)

        service._start_codex_process.assert_awaited_once_with(force_reset=True)
        service._read_codex_output.assert_awaited_once_with(process, context, "codex")
        service._wait_for_completion.assert_awaited_once()

    async def test_start_setup_starts_claude_control_flow_and_emits_manual_url(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        mock_client = SimpleNamespace()
        service._start_claude_control_flow = AsyncMock(return_value=(mock_client, "https://platform.claude.com/oauth/code", None))
        service._wait_for_claude_completion = AsyncMock()

        await service.start_setup(context, backend="claude", force_reset=True, claude_login_method="console")

        service._start_claude_control_flow.assert_awaited_once_with(
            context,
            force_reset=True,
            login_with_claude_ai=False,
        )
        self.assertEqual(len(controller.im_client.sent_messages), 2)
        self.assertIn("https://platform.claude.com/oauth/code", controller.im_client.sent_messages[1][1])
        flow = service._flows["C1:claude"]
        self.assertIs(flow.claude_client, mock_client)
        self.assertTrue(flow.login_prompt_sent)

    async def test_active_claude_auth_client_pids_includes_im_and_web_flows(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task

        def _client(pid):
            return SimpleNamespace(_transport=SimpleNamespace(_process=SimpleNamespace(pid=pid)))

        im_flow = AgentAuthFlow(
            flow_id="flow-im",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=_client(501),
        )
        service._flow_registry.put(im_flow, flow_key=im_flow.flow_key)
        service._web_flows["web-flow"] = SimpleNamespace(
            backend="claude",
            claude_client=_client(502),
        )

        pids = service.active_claude_auth_client_pids()

        self.assertEqual(pids, {501, 502})
        self.assertFalse(service.has_active_claude_auth_client_with_unknown_pid())

    async def test_active_claude_auth_client_unknown_pid_detected(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task

        flow = AgentAuthFlow(
            flow_id="flow-im",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=SimpleNamespace(),
        )
        service._flow_registry.put(flow, flow_key=flow.flow_key)

        self.assertEqual(service.active_claude_auth_client_pids(), set())
        self.assertTrue(service.has_active_claude_auth_client_with_unknown_pid())

    async def test_start_setup_reports_claude_start_failure(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        service._start_claude_control_flow = AsyncMock(
            side_effect=RuntimeError("Failed to clear Claude Code settings env")
        )

        await service.start_setup(context, backend="claude", force_reset=True, claude_login_method="console")

        self.assertEqual(len(controller.im_client.sent_messages), 1)
        self.assertEqual(len(controller.im_client.sent_button_messages), 1)
        _, text, keyboard = controller.im_client.sent_button_messages[0]
        self.assertIn("failed", text.lower())
        self.assertIn("Failed to clear Claude Code settings env", text)
        self.assertEqual(keyboard.buttons[0][0].callback_data, "auth_setup:claude")
        self.assertNotIn("C1:claude", service._flows)

    async def test_start_setup_localizes_native_login_conflict(self):
        controller = _StubController()
        controller._get_lang = lambda: "zh"
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        service._start_auth_flow = AsyncMock(
            side_effect=BackendLoginInProgressError("anthropic", "claude")
        )

        await service.start_setup(
            context,
            backend="claude",
            force_reset=True,
            claude_login_method="console",
        )

        _, text, _keyboard = controller.im_client.sent_button_messages[0]
        self.assertIn("登录正在进行中", text)
        self.assertNotIn("native_login_in_progress", text)

    async def test_start_claude_control_flow_restores_settings_on_auth_start_failure(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        with tempfile.TemporaryDirectory() as home:
            claude_home = Path(home) / ".claude"
            claude_home.mkdir()
            previous_claude_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
            try:
                from vibe.claude_config import read_claude_settings_env

                (claude_home / "settings.json").write_text(
                    '{"env":{"ANTHROPIC_API_KEY":"sk-old","ANTHROPIC_BASE_URL":"https://relay.example"}}',
                    encoding="utf-8",
                )
                service._create_claude_control_client = AsyncMock(return_value=SimpleNamespace())
                service._send_claude_control_request = AsyncMock(side_effect=RuntimeError("control failed"))
                service._disconnect_claude_client = AsyncMock()

                with self.assertRaisesRegex(RuntimeError, "control failed"):
                    await service._start_claude_control_flow(
                        context,
                        force_reset=False,
                        login_with_claude_ai=True,
                    )

                self.assertEqual(
                    read_claude_settings_env(),
                    {
                        "ANTHROPIC_API_KEY": "sk-old",
                        "ANTHROPIC_BASE_URL": "https://relay.example",
                    },
                )
            finally:
                if previous_claude_config_dir is None:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = previous_claude_config_dir

    async def test_start_setup_prompts_for_claude_login_method_when_unspecified(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        await service.start_setup(context, backend="claude", force_reset=True, claude_login_method=None)

        self.assertEqual(len(controller.im_client.sent_button_messages), 1)
        _, text, keyboard = controller.im_client.sent_button_messages[0]
        self.assertIn("sign-in source", text.lower())
        self.assertIn("/setup claude claudeai", text)
        self.assertIn("/setup claude console", text)
        self.assertEqual(keyboard.buttons[0][0].callback_data, "auth_setup:claude:claudeai")
        self.assertEqual(keyboard.buttons[0][1].callback_data, "auth_setup:claude:console")

    async def test_handle_process_text_marks_opencode_flow_awaiting_api_key(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-3",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            pty_master_fd=10,
            provider="opencode",
        )
        service._flows[flow.flow_key] = flow

        await service._handle_process_text(
            context,
            "opencode",
            "Create an api key at https://opencode.ai/auth",
        )
        await service._handle_process_text(
            context,
            "opencode",
            "E\nn\nt\ne\nr\ny\no\nu\nr\nA\nP\nI\nk\ne\ny",
        )

        self.assertTrue(flow.awaiting_code)
        self.assertEqual(len(controller.im_client.sent_messages), 2)
        self.assertIn("https://opencode.ai/auth", controller.im_client.sent_messages[0][1])
        self.assertIn("reply", controller.im_client.sent_messages[1][1].lower())

    async def test_start_setup_uses_direct_opencode_api_key_flow_for_opencode_provider(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        service._resolve_opencode_provider = AsyncMock(return_value="opencode")
        service._start_opencode_process = AsyncMock()

        await service.start_setup(context, backend="opencode", force_reset=True)

        service._start_opencode_process.assert_not_awaited()
        flow = service._flows["C1:opencode"]
        self.assertTrue(flow.awaiting_code)
        self.assertIsNone(flow.pty_master_fd)
        self.assertEqual(flow.url, "https://opencode.ai/auth")
        self.assertIn("reply", controller.im_client.sent_messages[1][1].lower())

    async def test_handle_process_text_reprompts_when_opencode_requests_api_key_again(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-opencode-retry",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            pty_master_fd=12,
            awaiting_code=False,
            code_prompt_sent=True,
            provider="opencode",
        )
        service._flows[flow.flow_key] = flow

        await service._handle_process_text(
            context,
            "opencode",
            "E\nn\nt\ne\nr\ny\no\nu\nr\nA\nP\nI\nk\ne\ny",
        )

        self.assertTrue(flow.awaiting_code)
        self.assertEqual(len(controller.im_client.sent_messages), 1)
        self.assertIn("still waiting", controller.im_client.sent_messages[0][1].lower())

    async def test_resolve_opencode_provider_prefers_override_model(self):
        controller = _StubController()
        controller.get_opencode_overrides = lambda context: ("build", "openai/gpt-5.4", None)
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        provider = await service._resolve_opencode_provider(context)

        self.assertEqual(provider, "openai")

    async def test_resolve_opencode_provider_prefers_existing_session_runtime_provider(self):
        controller = _StubController()
        controller.sessions = SimpleNamespace(
            get_agent_session_id=lambda session_key, composite_key, agent_name: "ses-existing"
        )
        mock_server = SimpleNamespace(
            list_messages=AsyncMock(
                return_value=[
                    {"info": {"role": "assistant", "providerID": "openai", "modelID": "gpt-5.3-chat-latest"}}
                ]
            ),
            get_default_agent_from_config=lambda: "build",
            get_agent_model_from_config=lambda agent_name: None,
        )
        controller.agent_service = SimpleNamespace(
            agents={"opencode": SimpleNamespace(_get_server=AsyncMock(return_value=mock_server))}
        )
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        provider = await service._resolve_opencode_provider(context)

        self.assertEqual(provider, "openai")
        mock_server.list_messages.assert_awaited_once_with("ses-existing", "/tmp/workdir")

    async def test_resolve_opencode_provider_in_topic_uses_runtime_session_key(self):
        controller = _StubController()
        controller._get_settings_key = Mock(return_value="thread::-1001::42")
        controller._get_session_key = Mock(return_value="telegram::-1001")
        controller.sessions = SimpleNamespace(get_agent_session_id=Mock(return_value="ses-existing"))
        mock_server = SimpleNamespace(
            list_messages=AsyncMock(
                return_value=[
                    {"info": {"role": "assistant", "providerID": "openai", "modelID": "gpt-5.3-chat-latest"}}
                ]
            ),
            get_default_agent_from_config=lambda: "build",
            get_agent_model_from_config=lambda agent_name: None,
        )
        controller.agent_service = SimpleNamespace(
            agents={"opencode": SimpleNamespace(_get_server=AsyncMock(return_value=mock_server))}
        )
        service = AgentAuthService(controller)
        context = MessageContext(
            user_id="7",
            channel_id="-1001",
            thread_id="42",
            platform="telegram",
            platform_specific={"is_forum": True, "is_topic_message": True},
        )

        provider = await service._resolve_opencode_provider(context)

        self.assertEqual(provider, "openai")
        controller._get_session_key.assert_called_once_with(context)
        controller.sessions.get_agent_session_id.assert_called_once_with(
            "telegram::-1001",
            "base-1:/tmp/workdir",
            "opencode",
        )
        controller._get_settings_key.assert_not_called()

    async def test_handle_process_text_emits_opencode_device_flow_for_openai(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-4",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            pty_master_fd=10,
            provider="openai",
        )
        service._flows[flow.flow_key] = flow

        await service._handle_process_text(
            context,
            "opencode",
            "Go to: https://auth.openai.com/codex/device",
        )
        await service._handle_process_text(
            context,
            "opencode",
            "Enter code: TRVY-E7DCU",
        )

        self.assertEqual(len(controller.im_client.sent_messages), 1)
        message = controller.im_client.sent_messages[0][1]
        self.assertIn("https://auth.openai.com/codex/device", message)
        self.assertIn("TRVY-E7DCU", message)

    async def test_submit_code_prefers_same_user_flow_waiting_for_code(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        codex_flow = AgentAuthFlow(
            flow_id="flow-codex",
            backend="codex",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
        )
        opencode_flow = AgentAuthFlow(
            flow_id="flow-opencode",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            pty_master_fd=17,
            awaiting_code=True,
            provider="openrouter",
        )
        service._flows[codex_flow.flow_key] = codex_flow
        service._flows[opencode_flow.flow_key] = opencode_flow

        with patch("core.agent_auth_service.os.write") as mock_write:
            await service.submit_code(context, "secret-value")

        mock_write.assert_called_once_with(17, b"secret-value\n")
        self.assertFalse(opencode_flow.awaiting_code)
        self.assertIn("opencode", controller.im_client.sent_messages[0][1].lower())

    async def test_submit_code_installs_direct_opencode_api_key_without_pty(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-opencode-direct",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            provider="opencode",
            awaiting_code=True,
        )
        service._flows[flow.flow_key] = flow
        service._flows_by_id[flow.flow_id] = flow
        service._install_opencode_api_key = AsyncMock()
        service._refresh_backend_runtime = AsyncMock()
        service._clear_backend_sessions_for_context = AsyncMock()

        await service.submit_code(context, "sk-opencode-secret", backend_hint="opencode")

        service._install_opencode_api_key.assert_awaited_once_with("opencode", "sk-opencode-secret")
        service._refresh_backend_runtime.assert_awaited_once_with("opencode")
        service._clear_backend_sessions_for_context.assert_awaited_once_with("opencode", context)
        self.assertIn("active again", controller.im_client.sent_messages[0][1].lower())
        self.assertNotIn(flow.flow_key, service._flows)

    async def test_install_opencode_api_key_uses_server_auth_endpoint(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        order = []

        async def set_api_key_auth(provider, api_key):
            order.append(("set", provider, api_key))

        server = SimpleNamespace(set_api_key_auth=AsyncMock(side_effect=set_api_key_auth))
        controller.agent_service = SimpleNamespace(
            agents={"opencode": SimpleNamespace(_get_server=AsyncMock(return_value=server))}
        )

        cleanup_calls = []

        async def fake_to_thread(func, *args, **kwargs):
            cleanup_calls.append((func, args, kwargs))
            order.append(("cleanup", args[0]))
            return func(*args, **kwargs)

        with patch("core.agent_auth_service.asyncio.to_thread", side_effect=fake_to_thread):
            await service._install_opencode_api_key("opencode", "sk-opencode-secret")

        self.assertEqual(len(cleanup_calls), 1)
        _, args, kwargs = cleanup_calls[0]
        self.assertEqual(args, ("opencode",))
        self.assertEqual(kwargs, {"logger_instance": unittest.mock.ANY})
        server.set_api_key_auth.assert_awaited_once_with("opencode", "sk-opencode-secret")
        self.assertEqual(order, [("set", "opencode", "sk-opencode-secret"), ("cleanup", "opencode")])

    async def test_submit_code_allows_proactive_claude_code_submission(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        claude_flow = AgentAuthFlow(
            flow_id="flow-claude-code",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=SimpleNamespace(),
            login_prompt_sent=True,
        )
        service._flows[claude_flow.flow_key] = claude_flow

        service._send_claude_callback = AsyncMock()

        await service.submit_code(context, "auth-code#oauth-state")

        service._send_claude_callback.assert_awaited_once_with(claude_flow.claude_client, "auth-code", "oauth-state")
        self.assertIn("claude", controller.im_client.sent_messages[0][1].lower())

    async def test_submit_code_rejects_invalid_claude_callback_format(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-claude-invalid",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=SimpleNamespace(),
            login_prompt_sent=True,
        )
        service._flows[flow.flow_key] = flow
        service._send_claude_callback = AsyncMock()

        await service.submit_code(context, "12345678")

        service._send_claude_callback.assert_not_awaited()
        self.assertIn("authorizationCode#state", controller.im_client.sent_messages[0][1])

    async def test_maybe_consume_setup_reply_accepts_plain_claude_callback_value(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-claude-plain",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=SimpleNamespace(),
            login_prompt_sent=True,
        )
        service._flows[flow.flow_key] = flow
        service.submit_code = AsyncMock()

        consumed = await service.maybe_consume_setup_reply(context, "auth-code#oauth-state")

        self.assertTrue(consumed)
        service.submit_code.assert_awaited_once_with(context, "auth-code#oauth-state", backend_hint="claude")

    async def test_maybe_consume_setup_reply_prefers_claude_callback_over_opencode_waiting_key(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        claude_flow = AgentAuthFlow(
            flow_id="flow-claude-priority",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=SimpleNamespace(),
            login_prompt_sent=True,
        )
        opencode_flow = AgentAuthFlow(
            flow_id="flow-opencode-priority",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            provider="opencode",
            awaiting_code=True,
        )
        service._flows[claude_flow.flow_key] = claude_flow
        service._flows[opencode_flow.flow_key] = opencode_flow
        service.submit_code = AsyncMock()

        consumed = await service.maybe_consume_setup_reply(context, "auth-code#oauth-state")

        self.assertTrue(consumed)
        service.submit_code.assert_awaited_once_with(context, "auth-code#oauth-state", backend_hint="claude")

    async def test_maybe_consume_setup_reply_accepts_plain_opencode_key_while_waiting(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-opencode-plain",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            pty_master_fd=11,
            awaiting_code=True,
            provider="opencode",
        )
        service._flows[flow.flow_key] = flow
        service.submit_code = AsyncMock()

        consumed = await service.maybe_consume_setup_reply(context, "sk-opencode-secret")

        self.assertTrue(consumed)
        service.submit_code.assert_awaited_once_with(context, "sk-opencode-secret", backend_hint="opencode")

    async def test_maybe_consume_setup_reply_fences_duplicate_before_submission(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-opencode-duplicate",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            pty_master_fd=11,
            awaiting_code=True,
            provider="opencode",
        )
        service._flows[flow.flow_key] = flow
        service.submit_code = AsyncMock()
        claim_native_event = Mock(return_value=False)

        consumed = await service.maybe_consume_setup_reply(
            context,
            "sk-opencode-secret",
            claim_native_event=claim_native_event,
        )

        self.assertTrue(consumed)
        claim_native_event.assert_called_once_with()
        service.submit_code.assert_not_awaited()

    async def test_maybe_consume_setup_reply_accepts_non_sk_opencode_credential(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-opencode-alt-cred",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            pty_master_fd=11,
            awaiting_code=True,
            provider="opencode",
        )
        service._flows[flow.flow_key] = flow
        service.submit_code = AsyncMock()

        consumed = await service.maybe_consume_setup_reply(context, "oc_live_Abcdef1234567890")

        self.assertTrue(consumed)
        service.submit_code.assert_awaited_once_with(context, "oc_live_Abcdef1234567890", backend_hint="opencode")

    async def test_maybe_consume_setup_reply_ignores_noncredential_opencode_plain_text(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-opencode-ignore",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            pty_master_fd=11,
            awaiting_code=True,
            provider="opencode",
        )
        service._flows[flow.flow_key] = flow
        service.submit_code = AsyncMock()

        consumed = await service.maybe_consume_setup_reply(context, "hello world")

        self.assertFalse(consumed)
        service.submit_code.assert_not_awaited()

    async def test_maybe_consume_setup_reply_ignores_spaced_opencode_plain_text(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-opencode-space-ignore",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            pty_master_fd=11,
            awaiting_code=True,
            provider="opencode",
        )
        service._flows[flow.flow_key] = flow
        service.submit_code = AsyncMock()

        consumed = await service.maybe_consume_setup_reply(context, "this is still normal chat")

        self.assertFalse(consumed)
        service.submit_code.assert_not_awaited()

    async def test_maybe_consume_setup_reply_ignores_separator_only_opencode_text(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-opencode-separator-ignore",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            pty_master_fd=11,
            awaiting_code=True,
            provider="opencode",
        )
        service._flows[flow.flow_key] = flow
        service.submit_code = AsyncMock()

        consumed = await service.maybe_consume_setup_reply(context, "--------------------")

        self.assertFalse(consumed)
        service.submit_code.assert_not_awaited()

    async def test_maybe_consume_setup_reply_ignores_plain_text_without_callback_shape(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-claude-plain-ignore",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=SimpleNamespace(),
            login_prompt_sent=True,
        )
        service._flows[flow.flow_key] = flow
        service.submit_code = AsyncMock()

        consumed = await service.maybe_consume_setup_reply(context, "hello world")

        self.assertFalse(consumed)
        service.submit_code.assert_not_awaited()

    async def test_drop_flow_preserves_replacement_flow_with_same_key(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task

        existing_flow = AgentAuthFlow(
            flow_id="flow-old",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=SimpleNamespace(),
        )
        replacement_flow = AgentAuthFlow(
            flow_id="flow-new",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=SimpleNamespace(),
        )
        service._flows[existing_flow.flow_key] = replacement_flow
        service._flows_by_id[existing_flow.flow_id] = existing_flow
        service._flows_by_id[replacement_flow.flow_id] = replacement_flow

        service._drop_flow(existing_flow)

        self.assertIs(service._flows[existing_flow.flow_key], replacement_flow)
        self.assertNotIn(existing_flow.flow_id, service._flows_by_id)
        self.assertIs(service._flows_by_id[replacement_flow.flow_id], replacement_flow)

    async def test_wait_for_claude_completion_refreshes_runtime_on_success(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-claude-wait",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=SimpleNamespace(),
        )
        service._flows[flow.flow_key] = flow
        service._flows_by_id[flow.flow_id] = flow
        service._send_claude_control_request = AsyncMock(return_value={})
        service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))
        service._refresh_backend_runtime = AsyncMock()
        service._disconnect_claude_client = AsyncMock()

        with patch("vibe.claude_config.apply_claude_auth") as cleanup:
            await service._wait_for_claude_completion(flow)

        service._send_claude_control_request.assert_awaited_once_with(
            flow.claude_client,
            {"subtype": "claude_oauth_wait_for_completion"},
            timeout=service.setup_timeout_seconds,
        )
        cleanup.assert_called()
        # The persisted write goes through the cross-process config
        # transaction; the live mirror fires only for fields the
        # transaction decided to change, computed from the lock-fresh
        # snapshot. The stub claude starts without auth_mode_set, so the
        # marker mirror applies; auth_mode itself is asserted on the
        # persisted file (the fresh snapshot already said "oauth").
        self.assertTrue(controller.config.agents.claude.auth_mode_set)
        from config.v2_config import V2Config as _V2

        self.assertEqual(_V2.load().agents.claude.auth_mode, "oauth")
        self.assertTrue(_V2.load().agents.claude.auth_mode_set)
        service._refresh_backend_runtime.assert_awaited_once_with("claude")
        service._disconnect_claude_client.assert_awaited_once_with(flow.claude_client)
        self.assertIn("login is active again", controller.im_client.sent_messages[0][1].lower())
        self.assertNotIn(flow.flow_key, service._flows)

    async def test_wait_for_claude_completion_persists_oauth_to_v2_when_controller_is_compat(self):
        from config.v2_config import AgentsConfig, ClaudeConfig, RuntimeConfig, SlackConfig, V2Config

        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-claude-compat-persist",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=SimpleNamespace(),
        )
        service._flows[flow.flow_key] = flow
        service._flows_by_id[flow.flow_id] = flow
        service._send_claude_control_request = AsyncMock(return_value={})
        service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))
        service._refresh_backend_runtime = AsyncMock()
        service._disconnect_claude_client = AsyncMock()

        with tempfile.TemporaryDirectory() as home:
            with _temporary_vibe_home(Path(home)):
                V2Config(
                    mode="self_host",
                    version="v2",
                    slack=SlackConfig(),
                    runtime=RuntimeConfig(default_cwd="/tmp/work"),
                    agents=AgentsConfig(
                        claude=ClaudeConfig(
                            auth_mode="oauth",
                            auth_mode_set=False,
                        )
                    ),
                ).save()
                controller.config = SimpleNamespace(
                    language="en",
                    claude=SimpleNamespace(auth_mode="oauth", auth_mode_set=False),
                )
                with patch("vibe.claude_config.apply_claude_auth"):
                    await service._wait_for_claude_completion(flow)

                saved = V2Config.load()

        self.assertEqual(saved.agents.claude.auth_mode, "oauth")
        self.assertTrue(saved.agents.claude.auth_mode_set)
        self.assertTrue(controller.config.claude.auth_mode_set)

    async def test_persist_backend_auth_mode_runs_config_transaction_off_event_loop(self):
        controller = _StubController()
        controller.config.agents.claude = SimpleNamespace(
            auth_mode="api_key",
            auth_mode_set=False,
        )
        service = AgentAuthService(controller)
        service._clear_claude_settings_env_for_oauth = AsyncMock()

        fresh_config = SimpleNamespace(
            agents=SimpleNamespace(
                claude=SimpleNamespace(auth_mode="api_key", auth_mode_set=False),
            ),
        )
        transaction_thread = None

        def fake_update_config_fields(mutator):
            nonlocal transaction_thread
            transaction_thread = threading.current_thread()
            mutator(fresh_config)
            return fresh_config

        with patch(
            "config.v2_config.update_config_fields",
            side_effect=fake_update_config_fields,
        ):
            caller_thread = threading.current_thread()
            await service._persist_backend_auth_mode("claude", "oauth")

        self.assertIsNotNone(transaction_thread)
        self.assertIsNot(transaction_thread, caller_thread)
        self.assertEqual(fresh_config.agents.claude.auth_mode, "oauth")
        self.assertTrue(fresh_config.agents.claude.auth_mode_set)
        self.assertEqual(controller.config.agents.claude.auth_mode, "oauth")
        self.assertTrue(controller.config.agents.claude.auth_mode_set)

    async def test_wait_for_claude_completion_reports_settings_cleanup_failure(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        fake_client = SimpleNamespace()
        flow = AgentAuthFlow(
            flow_id="flow-claude-cleanup-fails",
            backend="claude",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=None,
            reader_task=done_task,
            waiter_task=done_task,
            claude_client=fake_client,
        )
        service._flows[flow.flow_key] = flow
        service._flows_by_id[flow.flow_id] = flow
        service._send_claude_control_request = AsyncMock(return_value={})
        service._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))
        service._refresh_backend_runtime = AsyncMock()
        service._disconnect_claude_client = AsyncMock()

        with patch(
            "vibe.claude_config.apply_claude_auth",
            side_effect=OSError("settings locked"),
        ):
            await service._wait_for_claude_completion(flow)

        service._refresh_backend_runtime.assert_not_awaited()
        service._disconnect_claude_client.assert_awaited_once_with(fake_client)
        self.assertIn("failed", controller.im_client.sent_button_messages[0][1].lower())
        self.assertIn("Failed to clear Claude Code settings env", controller.im_client.sent_button_messages[0][1])
        self.assertNotIn(flow.flow_key, service._flows)

    async def test_start_opencode_process_closes_master_fd_on_spawn_failure(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        service._resolve_opencode_provider = AsyncMock(return_value="openai")

        with (
            patch("core.agent_auth_service.os.openpty", return_value=(303, 404)),
            patch("core.agent_auth_service.os.close") as mock_close,
            patch("core.agent_auth_service.asyncio.create_subprocess_exec", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                await service._start_opencode_process(context, force_reset=False)

        mock_close.assert_any_call(303)
        mock_close.assert_any_call(404)

    async def test_read_pty_output_exits_after_process_finishes_without_output(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        process = SimpleNamespace(returncode=None)
        master_fd, slave_fd = os.openpty()

        async def finish_process():
            await asyncio.sleep(0.05)
            process.returncode = 0
            os.close(slave_fd)

        finisher = asyncio.create_task(finish_process())
        try:
            await asyncio.wait_for(service._read_pty_output(process, master_fd, context, "claude"), timeout=0.5)
        finally:
            await finisher

    async def test_refresh_backend_runtime_uses_backend_specific_runtime_refresh(self):
        controller = _StubController()
        controller.agent_service.agents["codex"] = SimpleNamespace(refresh_auth_state=AsyncMock())
        controller.agent_service.agents["claude"] = SimpleNamespace(refresh_auth_state=AsyncMock())
        service = AgentAuthService(controller)
        service._refresh_opencode_server = AsyncMock()

        await service._refresh_backend_runtime("codex")
        await service._refresh_backend_runtime("claude")
        await service._refresh_backend_runtime("opencode")

        controller.agent_service.agents["codex"].refresh_auth_state.assert_awaited_once()
        controller.agent_service.agents["claude"].refresh_auth_state.assert_awaited_once()
        service._refresh_opencode_server.assert_awaited_once()

    async def test_refresh_backend_runtime_prefers_runtime_config_reload(self):
        controller = _StubController()
        agent = SimpleNamespace(
            refresh_runtime_config=AsyncMock(),
            refresh_auth_state=AsyncMock(),
        )
        controller.agent_service.agents["codex"] = agent
        service = AgentAuthService(controller)
        runtime_config = object()
        service._load_backend_runtime_config = Mock(return_value=runtime_config)

        await service._refresh_backend_runtime("codex")

        service._load_backend_runtime_config.assert_called_once_with("codex")
        agent.refresh_runtime_config.assert_awaited_once_with(runtime_config)
        agent.refresh_auth_state.assert_not_awaited()

    async def test_refresh_backend_runtime_releases_runtime_tokens_after_refresh(self):
        controller = _StubController()
        runtime_tokens = {"session:/repo": "token-1"}
        controller.agent_service.runtime_turn_tokens_for_backend = Mock(return_value=runtime_tokens)
        controller.agent_service.release_runtime_turn_tokens = Mock()
        controller.agent_service.refresh_runtime_config = AsyncMock(return_value=True)
        controller.agent_service.agents["codex"] = SimpleNamespace()
        service = AgentAuthService(controller)
        runtime_config = object()
        service._load_backend_runtime_config = Mock(return_value=runtime_config)

        await service._refresh_backend_runtime("codex")

        controller.agent_service.runtime_turn_tokens_for_backend.assert_called_once_with("codex")
        controller.agent_service.refresh_runtime_config.assert_awaited_once_with("codex", runtime_config)
        controller.agent_service.release_runtime_turn_tokens.assert_called_once_with(runtime_tokens)

    async def test_clear_backend_sessions_for_context_routes_through_agent_service(self):
        controller = _StubController()
        controller.agent_service.clear_backend_sessions = AsyncMock(return_value=1)
        controller.agent_service.agents["codex"] = SimpleNamespace(clear_sessions=AsyncMock())
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")

        await service._clear_backend_sessions_for_context("codex", context)

        controller.agent_service.clear_backend_sessions.assert_awaited_once_with("codex", "C1")
        controller.agent_service.agents["codex"].clear_sessions.assert_not_awaited()

    async def test_clear_backend_sessions_for_topic_uses_runtime_session_key(self):
        controller = _StubController()
        controller._get_session_key = Mock(return_value="telegram::-1001")
        controller.agent_service.clear_backend_sessions = AsyncMock(return_value=1)
        service = AgentAuthService(controller)
        context = MessageContext(
            user_id="7",
            channel_id="-1001",
            thread_id="42",
            platform="telegram",
            platform_specific={"is_forum": True, "is_topic_message": True},
        )

        await service._clear_backend_sessions_for_context("opencode", context)

        controller._get_session_key.assert_called_once_with(context)
        controller.agent_service.clear_backend_sessions.assert_awaited_once_with(
            "opencode",
            "telegram::-1001",
        )

    async def test_refresh_backend_runtime_registers_codex_when_enabled_after_startup(self):
        from config.v2_compat import CodexCompatConfig
        from modules.agents.codex import CodexAgent
        from modules.agents.service import AgentService

        controller = _StubController()
        controller.config.codex = None
        controller.agent_service = AgentService(controller)
        register = controller.agent_service.register
        controller.agent_service.register = Mock(side_effect=register)
        service = AgentAuthService(controller)
        runtime_config = CodexCompatConfig(
            enabled=True,
            binary="/Users/rk/.nvm/versions/node/v24.12.0/bin/codex",
            extra_args=[],
        )
        service._load_backend_runtime_config = Mock(return_value=runtime_config)

        with patch.object(
            CodexAgent,
            "prepare_model_hub_runtime",
            new=AsyncMock(side_effect=RuntimeError("catalog export unavailable")),
        ) as prepare_model_hub_runtime:
            await service._refresh_backend_runtime("codex")

        service._load_backend_runtime_config.assert_called_once_with("codex")
        prepare_model_hub_runtime.assert_not_awaited()
        controller.agent_service.register.assert_called_once()
        registered = controller.agent_service.agents["codex"]
        self.assertEqual(registered.name, "codex")
        self.assertIs(registered.codex_config, runtime_config)
        self.assertIs(controller.config.codex, runtime_config)

    async def test_refresh_backend_runtime_unregisters_disabled_codex(self):
        from modules.agent_router import AgentRouter
        from modules.agents.service import AgentService

        controller = _StubController()
        controller.agent_service = AgentService(controller)
        controller.agent_router = AgentRouter.from_file(None, platform="slack", fallback_backend="codex")
        controller.config.codex = SimpleNamespace(binary="/old/codex")
        controller.config.default_backend = "codex"
        controller.agent_service.register(SimpleNamespace(name="claude"))
        agent = SimpleNamespace(name="codex", shutdown_runtime=AsyncMock())
        controller.agent_service.register(agent)
        service = AgentAuthService(controller)
        service._load_backend_runtime_config = Mock(return_value=None)
        service._load_saved_enabled_backends = Mock(return_value=["claude"])
        service._sync_builtin_default_agents = Mock(wraps=service._sync_builtin_default_agents)

        await service._refresh_backend_runtime("codex")

        self.assertNotIn("codex", controller.agent_service.agents)
        self.assertIsNone(controller.config.codex)
        self.assertEqual(controller.agent_router.global_default, "codex")
        self.assertEqual(controller.agent_router.platform_routes["slack"].default, "codex")
        self.assertEqual(controller.config.default_backend, "codex")
        agent.shutdown_runtime.assert_awaited_once()
        service._sync_builtin_default_agents.assert_called_once_with()

    async def test_refresh_backend_runtime_does_not_restore_legacy_default_after_late_registration(self):
        from config.v2_compat import CodexCompatConfig
        from modules.agent_router import AgentRouter
        from modules.agents.codex import CodexAgent
        from modules.agents.service import AgentService

        controller = _StubController()
        controller.agent_service = AgentService(controller)
        controller.config.codex = None
        controller.config.default_backend = "claude"
        controller.agent_router = AgentRouter.from_file(None, platform="slack", fallback_backend="claude")
        service = AgentAuthService(controller)
        runtime_config = CodexCompatConfig(enabled=True, binary="/opt/codex", extra_args=[])
        service._load_backend_runtime_config = Mock(return_value=runtime_config)
        service._load_saved_enabled_backends = Mock(return_value=["codex"])
        service._sync_builtin_default_agents = Mock(wraps=service._sync_builtin_default_agents)

        with patch.object(
            CodexAgent,
            "prepare_model_hub_runtime",
            new=AsyncMock(return_value=Path("/runtime/codex-hub.json")),
        ):
            await service._refresh_backend_runtime("codex")

        self.assertEqual(controller.agent_router.global_default, "claude")
        self.assertEqual(controller.agent_router.platform_routes["slack"].default, "claude")
        self.assertEqual(controller.config.default_backend, "claude")
        service._sync_builtin_default_agents.assert_called_once_with()

    async def test_sync_builtin_default_agents_uses_saved_enabled_config(self):
        controller = _StubController()
        controller.vibe_agent_store = SimpleNamespace(ensure_builtin_default_agents=Mock())
        service = AgentAuthService(controller)
        service._load_saved_enabled_backends = Mock(return_value=["codex"])

        service._sync_builtin_default_agents()

        controller.vibe_agent_store.ensure_builtin_default_agents.assert_called_once_with(["codex"])

    async def test_refresh_opencode_runtime_reloads_v2_cli_path(self):
        from config.v2_config import AgentsConfig, OpenCodeConfig, RuntimeConfig, SlackConfig, V2Config
        from config.v2_compat import OpenCodeCompatConfig

        controller = _StubController()
        previous_server = SimpleNamespace(
            reload_runtime_config=AsyncMock(),
            detach_after_deferred_refresh=AsyncMock(),
            refresh_global_config=AsyncMock(return_value=False),
        )

        class _FakeOpenCodeAgent:
            def __init__(self) -> None:
                self.refreshed = None

            async def _get_server(self):
                return previous_server

            async def refresh_runtime_config(self, opencode_config):
                self.refreshed = opencode_config
                controller.config.opencode = opencode_config
                await previous_server.detach_after_deferred_refresh()
                await previous_server.reload_runtime_config(
                    binary=opencode_config.binary,
                    port=opencode_config.port,
                    request_timeout_seconds=opencode_config.request_timeout_seconds,
                )

        agent = _FakeOpenCodeAgent()
        controller.agent_service.agents["opencode"] = agent
        service = AgentAuthService(controller)

        with tempfile.TemporaryDirectory() as home:
            with _temporary_vibe_home(Path(home)):
                V2Config(
                    mode="self_host",
                    version="v2",
                    slack=SlackConfig(),
                    runtime=RuntimeConfig(default_cwd="/tmp/work"),
                    agents=AgentsConfig(
                        opencode=OpenCodeConfig(
                            enabled=True,
                            cli_path="/opt/opencode/bin/opencode",
                        )
                    ),
                ).save()

                await service._refresh_backend_runtime("opencode")

        self.assertIsInstance(agent.refreshed, OpenCodeCompatConfig)
        self.assertEqual(agent.refreshed.binary, "/opt/opencode/bin/opencode")
        previous_server.reload_runtime_config.assert_awaited_once_with(
            binary="/opt/opencode/bin/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        previous_server.detach_after_deferred_refresh.assert_awaited_once()

    async def test_opencode_agent_refresh_runtime_config_updates_cached_server(self):
        from config.v2_compat import OpenCodeCompatConfig
        from modules.agents.opencode.agent import OpenCodeAgent

        old_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        new_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        calls: list[str] = []

        async def _detach() -> None:
            calls.append("detach")

        async def _reload_runtime_config(**kwargs) -> None:
            calls.append("reload")

        previous_server = SimpleNamespace(
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
            reload_runtime_config=AsyncMock(side_effect=_reload_runtime_config),
            detach_after_deferred_refresh=AsyncMock(side_effect=_detach),
            refresh_global_config=AsyncMock(return_value=False),
        )
        agent = OpenCodeAgent.__new__(OpenCodeAgent)
        agent.opencode_config = old_config
        agent.controller = SimpleNamespace(config=SimpleNamespace(opencode=old_config))
        agent._client_manager = SimpleNamespace(reset_config=AsyncMock(return_value=previous_server))

        await agent.refresh_runtime_config(new_config)

        self.assertIs(agent.opencode_config, new_config)
        self.assertIs(agent.controller.config.opencode, new_config)
        agent._client_manager.reset_config.assert_awaited_once_with(new_config)
        previous_server.detach_after_deferred_refresh.assert_awaited_once()
        previous_server.reload_runtime_config.assert_awaited_once_with(
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        self.assertEqual(calls, ["detach", "reload"])

    async def test_opencode_agent_refresh_runtime_config_uses_global_config_refresh(self):
        from config.v2_compat import OpenCodeCompatConfig
        from modules.agents.opencode.agent import OpenCodeAgent

        old_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        new_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        previous_server = SimpleNamespace(
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
            refresh_global_config=AsyncMock(return_value=True),
            detach_after_deferred_refresh=AsyncMock(),
            reload_runtime_config=AsyncMock(),
        )
        agent = OpenCodeAgent.__new__(OpenCodeAgent)
        agent.opencode_config = old_config
        agent.controller = SimpleNamespace(config=SimpleNamespace(opencode=old_config))
        agent._client_manager = SimpleNamespace(reset_config=AsyncMock(return_value=previous_server))

        await agent.refresh_runtime_config(new_config)

        previous_server.refresh_global_config.assert_awaited_once()
        previous_server.detach_after_deferred_refresh.assert_not_awaited()
        previous_server.reload_runtime_config.assert_awaited_once_with(
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )

    async def test_opencode_agent_force_refresh_restarts_after_global_config_change(self):
        from config.v2_compat import OpenCodeCompatConfig
        from modules.agents.opencode.agent import OpenCodeAgent

        old_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        new_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        previous_server = SimpleNamespace(
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
            refresh_global_config=AsyncMock(return_value=True),
            detach_after_deferred_refresh=AsyncMock(),
            reload_runtime_config=AsyncMock(),
        )
        agent = OpenCodeAgent.__new__(OpenCodeAgent)
        agent.opencode_config = old_config
        agent.controller = SimpleNamespace(config=SimpleNamespace(opencode=old_config))
        agent._client_manager = SimpleNamespace(reset_config=AsyncMock(return_value=previous_server))

        await agent.refresh_runtime_config(new_config, force=True)

        previous_server.refresh_global_config.assert_not_awaited()
        previous_server.detach_after_deferred_refresh.assert_awaited_once_with(force=True)
        previous_server.reload_runtime_config.assert_awaited_once_with(
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )

    async def test_opencode_agent_refresh_runtime_config_attaches_uncached_server(self):
        from config.v2_compat import OpenCodeCompatConfig
        from modules.agents.opencode.agent import OpenCodeAgent
        from modules.agents.opencode.server import OpenCodeServerManager

        old_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/old/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        new_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/new/opencode",
            port=4100,
            request_timeout_seconds=15,
        )
        live_server = SimpleNamespace(
            binary="/old/opencode",
            port=4096,
            request_timeout_seconds=60,
            refresh_global_config=AsyncMock(return_value=True),
            detach_after_deferred_refresh=AsyncMock(),
            reload_runtime_config=AsyncMock(),
        )
        agent = OpenCodeAgent.__new__(OpenCodeAgent)
        agent.opencode_config = old_config
        agent.controller = SimpleNamespace(config=SimpleNamespace(opencode=old_config))
        agent._client_manager = SimpleNamespace(
            reset_config=AsyncMock(return_value=None),
        )

        with patch.object(
            OpenCodeServerManager,
            "get_instance_if_managed_server_exists",
            AsyncMock(return_value=live_server),
        ) as get_instance:
            await agent.refresh_runtime_config(new_config)

        agent._client_manager.reset_config.assert_awaited_once_with(new_config)
        get_instance.assert_awaited_once_with(
            binary="/old/opencode",
            port=4096,
            request_timeout_seconds=60,
            resource_governor=agent.controller._agent_resource_governor,
        )
        live_server.refresh_global_config.assert_not_awaited()
        live_server.detach_after_deferred_refresh.assert_awaited_once()
        live_server.reload_runtime_config.assert_awaited_once_with(
            binary="/new/opencode",
            port=4100,
            request_timeout_seconds=15,
        )

    async def test_opencode_agent_refresh_runtime_config_skips_uncached_refresh_without_managed_server(self):
        from config.v2_compat import OpenCodeCompatConfig
        from modules.agents.opencode.agent import OpenCodeAgent
        from modules.agents.opencode.server import OpenCodeServerManager

        old_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        new_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        agent = OpenCodeAgent.__new__(OpenCodeAgent)
        agent.opencode_config = old_config
        agent.controller = SimpleNamespace(config=SimpleNamespace(opencode=old_config))
        agent._client_manager = SimpleNamespace(
            reset_config=AsyncMock(return_value=None),
        )

        with patch.object(
            OpenCodeServerManager,
            "get_instance_if_managed_server_exists",
            AsyncMock(return_value=None),
        ) as get_instance:
            await agent.refresh_runtime_config(new_config)

        agent._client_manager.reset_config.assert_awaited_once_with(new_config)
        get_instance.assert_awaited_once_with(
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
            resource_governor=agent.controller._agent_resource_governor,
        )
        self.assertIs(agent.opencode_config, new_config)
        self.assertIs(agent.controller.config.opencode, new_config)

    async def test_web_opencode_server_uses_v2_runtime_resource_governance(self):
        from config.v2_config import AgentsConfig, RuntimeConfig, SlackConfig, V2Config
        from modules.agents.opencode.server import OpenCodeServerManager

        controller = _StubController()
        service = AgentAuthService(controller)
        v2_config = V2Config(
            mode="self_host",
            version="v2",
            slack=SlackConfig(),
            agents=AgentsConfig(),
            runtime=RuntimeConfig(
                default_cwd=".",
                resource_governance={
                    "mode": "enabled",
                    "agent_group_name": "web-oauth-agents",
                },
            ),
        )
        server = SimpleNamespace(ensure_running=AsyncMock())

        with (
            patch("config.v2_config.V2Config.load", return_value=v2_config),
            patch.object(
                OpenCodeServerManager,
                "get_instance",
                AsyncMock(return_value=server),
            ) as get_instance,
        ):
            result = await service._opencode_server()

        self.assertIs(result, server)
        server.ensure_running.assert_awaited_once()
        self.assertNotIn(
            "model_hub_overlay_required",
            get_instance.await_args.kwargs,
        )
        governor = get_instance.await_args.kwargs["resource_governor"]
        self.assertEqual(governor.mode, "enabled")
        self.assertEqual(governor.config["agent_group_name"], "web-oauth-agents")

    async def test_web_opencode_server_requires_controller_overlay_in_hub_mode(self):
        from config.v2_config import (
            AgentsConfig,
            RuntimeConfig,
            SlackConfig,
            V2Config,
        )
        from modules.agents.opencode.server import (
            OpenCodeModelHubOverlayRequiredError,
            OpenCodeServerManager,
        )

        controller = _StubController()
        service = AgentAuthService(controller)
        v2_config = V2Config(
            mode="self_host",
            version="v2",
            slack=SlackConfig(),
            agents=AgentsConfig(),
            runtime=RuntimeConfig(default_cwd="."),
        )
        v2_config.model_hub.agents["opencode"].mode = "hub"
        server = SimpleNamespace(
            ensure_running=AsyncMock(
                side_effect=OpenCodeModelHubOverlayRequiredError(
                    "controller overlay is not ready"
                )
            )
        )

        with (
            patch("config.v2_config.V2Config.load", return_value=v2_config),
            patch.object(
                OpenCodeServerManager,
                "get_instance",
                AsyncMock(return_value=server),
            ),
        ):
            result = await service._opencode_server()

        self.assertIsNone(result)
        server.ensure_running.assert_awaited_once()

    async def test_opencode_agent_refresh_runtime_config_restarts_uncached_adopted_server_on_refresh_miss(self):
        from config.v2_compat import OpenCodeCompatConfig
        from modules.agents.opencode.agent import OpenCodeAgent
        from modules.agents.opencode.server import OpenCodeServerManager

        old_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        new_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        live_server = SimpleNamespace(
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
            refresh_global_config=AsyncMock(return_value=False),
            detach_after_deferred_refresh=AsyncMock(),
            reload_runtime_config=AsyncMock(),
        )
        agent = OpenCodeAgent.__new__(OpenCodeAgent)
        agent.opencode_config = old_config
        agent.controller = SimpleNamespace(config=SimpleNamespace(opencode=old_config))
        agent._client_manager = SimpleNamespace(
            reset_config=AsyncMock(return_value=None),
        )

        with patch.object(
            OpenCodeServerManager,
            "get_instance_if_managed_server_exists",
            AsyncMock(return_value=live_server),
        ):
            await agent.refresh_runtime_config(new_config)

        live_server.refresh_global_config.assert_awaited_once()
        live_server.detach_after_deferred_refresh.assert_awaited_once()
        live_server.reload_runtime_config.assert_awaited_once_with(
            binary="/opencode",
            port=4096,
            request_timeout_seconds=60,
        )

    async def test_opencode_agent_refresh_runtime_config_restarts_when_runtime_changes(self):
        from config.v2_compat import OpenCodeCompatConfig
        from modules.agents.opencode.agent import OpenCodeAgent

        old_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/old/opencode",
            port=4096,
            request_timeout_seconds=60,
        )
        new_config = OpenCodeCompatConfig(
            enabled=True,
            binary="/new/opencode",
            port=4100,
            request_timeout_seconds=15,
        )
        previous_server = SimpleNamespace(
            binary="/old/opencode",
            port=4096,
            request_timeout_seconds=60,
            refresh_global_config=AsyncMock(return_value=True),
            detach_after_deferred_refresh=AsyncMock(),
            reload_runtime_config=AsyncMock(),
        )
        agent = OpenCodeAgent.__new__(OpenCodeAgent)
        agent.opencode_config = old_config
        agent.controller = SimpleNamespace(config=SimpleNamespace(opencode=old_config))
        agent._client_manager = SimpleNamespace(reset_config=AsyncMock(return_value=previous_server))

        await agent.refresh_runtime_config(new_config)

        previous_server.refresh_global_config.assert_not_awaited()
        previous_server.detach_after_deferred_refresh.assert_awaited_once()
        previous_server.reload_runtime_config.assert_awaited_once_with(
            binary="/new/opencode",
            port=4100,
            request_timeout_seconds=15,
        )

    async def test_refresh_claude_runtime_reloads_v2_cli_path(self):
        from config.v2_config import AgentsConfig, ClaudeConfig, RuntimeConfig, SlackConfig, V2Config
        from config.v2_compat import ClaudeCompatConfig

        controller = _StubController()
        agent = SimpleNamespace(refresh_runtime_config=AsyncMock())
        controller.agent_service.agents["claude"] = agent
        service = AgentAuthService(controller)

        with tempfile.TemporaryDirectory() as home:
            with _temporary_vibe_home(Path(home)):
                V2Config(
                    mode="self_host",
                    version="v2",
                    slack=SlackConfig(),
                    runtime=RuntimeConfig(default_cwd="/tmp/work"),
                    agents=AgentsConfig(
                        claude=ClaudeConfig(
                            enabled=True,
                            cli_path="/opt/claude/bin/claude",
                        )
                    ),
                ).save()

                await service._refresh_backend_runtime("claude")

        runtime_config = agent.refresh_runtime_config.await_args.args[0]
        self.assertIsInstance(runtime_config, ClaudeCompatConfig)
        self.assertEqual(runtime_config.cli_path, "/opt/claude/bin/claude")

    async def test_codex_runtime_config_reload_updates_binary_before_refresh(self):
        from config.v2_compat import CodexCompatConfig
        from modules.agents.codex.agent import CodexAgent

        old_config = CodexCompatConfig(enabled=True, binary="/old/codex", extra_args=[])
        new_config = CodexCompatConfig(enabled=True, binary="/new/codex", extra_args=[])
        agent = CodexAgent.__new__(CodexAgent)
        agent.codex_config = old_config
        agent._model_hub_catalog_path = Path("/runtime/codex-old.json")
        agent._model_hub_catalog_lock = asyncio.Lock()
        agent._model_hub_catalog_generation = 0
        agent.controller = SimpleNamespace(config=SimpleNamespace(codex=old_config))
        agent.refresh_auth_state = AsyncMock()

        with patch(
            "vibe.backend_model_catalog.prepare_codex_hub_catalog",
            side_effect=RuntimeError("catalog export must not run"),
        ) as prepare_catalog:
            await agent.refresh_runtime_config(new_config)

        prepare_catalog.assert_not_called()
        self.assertIs(agent.codex_config, new_config)
        self.assertIs(agent.controller.config.codex, new_config)
        self.assertIsNone(agent._model_hub_catalog_path)
        self.assertEqual(agent._model_hub_catalog_generation, 1)
        agent.refresh_auth_state.assert_awaited_once()

    async def test_claude_runtime_config_reload_updates_cli_path_before_refresh(self):
        from config.v2_compat import ClaudeCompatConfig
        from modules.agents.claude_agent import ClaudeAgent

        old_config = ClaudeCompatConfig(
            enabled=True,
            permission_mode="bypassPermissions",
            cwd="/tmp/work",
            cli_path="/old/claude",
        )
        new_config = ClaudeCompatConfig(
            enabled=True,
            permission_mode="bypassPermissions",
            cwd="/tmp/work",
            cli_path="/new/claude",
        )
        session_handler = SimpleNamespace(config=None)
        agent = ClaudeAgent.__new__(ClaudeAgent)
        agent.config = SimpleNamespace(claude=old_config)
        agent.controller = SimpleNamespace(config=SimpleNamespace(claude=old_config))
        agent.session_handler = session_handler
        agent.refresh_auth_state = AsyncMock()

        await agent.refresh_runtime_config(new_config)

        self.assertIs(agent.config.claude, new_config)
        self.assertIs(agent.controller.config.claude, new_config)
        self.assertIs(session_handler.config, agent.controller.config)
        agent.refresh_auth_state.assert_awaited_once()

    async def test_create_claude_control_client_sets_large_sdk_buffer(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        captured = {}

        class _StubClaudeAgentOptions:
            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        class _StubClaudeSDKClient:
            def __init__(self, options):
                captured["options"] = options

            async def connect(self):
                captured["connected"] = True

        with (
            patch("core.agent_auth_service.ClaudeAgentOptions", _StubClaudeAgentOptions),
            patch("core.agent_auth_service.ClaudeSDKClient", _StubClaudeSDKClient),
        ):
            await service._create_claude_control_client(context)

        self.assertTrue(captured["connected"])
        self.assertEqual(captured["options"].max_buffer_size, CLAUDE_SDK_MAX_BUFFER_SIZE)
        self.assertEqual(captured["options"].env["AVIBE_CLAUDE_PROCESS_OWNER"], "auth")
        self.assertEqual(captured["options"].settings, '{"autoMemoryEnabled":false}')

    async def test_claude_control_flow_start_in_flight_disables_unknown_pid_guard(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        started = asyncio.Event()
        release = asyncio.Event()

        async def _create_client(_context):
            started.set()
            await release.wait()
            return SimpleNamespace()

        service._create_claude_control_client = AsyncMock(side_effect=_create_client)
        service._send_claude_control_request = AsyncMock(
            return_value={"manualUrl": "https://platform.claude.com/oauth/code"}
        )
        service._begin_claude_oauth_attempt = AsyncMock(return_value=None)

        task = asyncio.create_task(
            service._start_claude_control_flow(
                MessageContext(user_id="U1", channel_id="C1"),
                force_reset=False,
                login_with_claude_ai=True,
            )
        )
        await started.wait()

        self.assertTrue(service.has_active_claude_auth_client_with_unknown_pid())

        release.set()
        await task
        self.assertFalse(service.has_active_claude_auth_client_with_unknown_pid())

    async def test_verify_login_reports_opencode_segmentation_fault(self):
        controller = _StubController()
        service = AgentAuthService(controller)
        context = MessageContext(user_id="U1", channel_id="C1")
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        flow = AgentAuthFlow(
            flow_id="flow-opencode-verify",
            backend="opencode",
            settings_key="C1",
            initiator_user_id="U1",
            context=context,
            process=SimpleNamespace(returncode=None),
            reader_task=done_task,
            waiter_task=done_task,
            provider="openai",
        )

        fake_process = SimpleNamespace(
            returncode=-11,
            communicate=AsyncMock(return_value=(b"Segmentation fault\n", b"")),
        )

        with patch("core.agent_auth_service.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_process)):
            ok, detail = await service._verify_login(flow)

        self.assertFalse(ok)
        self.assertIn("Segmentation fault", detail)


class ClassifyAuthErrorTests(unittest.TestCase):
    def test_codex_401_requires_reset(self):
        self.assertTrue(classify_auth_error("codex", "unexpected status 401 Unauthorized"))

    def test_codex_missing_token_data_requires_reset(self):
        self.assertTrue(classify_auth_error("codex", "Codex turn failed: Token data is not available."))

    def test_non_auth_error_is_ignored(self):
        self.assertFalse(classify_auth_error("codex", "temporary network timeout"))

    def test_opencode_credential_error_requires_reset(self):
        self.assertTrue(classify_auth_error("opencode", "OpenCode error: missing provider credential"))

    def test_opencode_provider_mentions_without_auth_evidence_are_ignored(self):
        diagnostics = (
            "NativeSessionEndedBeforeResult - OpenCode ended without a model reply. "
            "Provider: openai; model: gpt-5.6-terra.",
            "ProviderError - rate limited",
            "Provider model is unavailable",
        )
        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                self.assertFalse(classify_auth_error("opencode", diagnostic))


class VerifyOpenCodeAuthListOutputTests(unittest.TestCase):
    def test_target_provider_must_exist_in_output(self):
        text = """
        ┌ Credentials ~/.local/share/opencode/auth.json
        │ anthropic 1 credential
        └ 1 credentials
        """

        self.assertFalse(verify_opencode_auth_list_output(text, "openai"))

    def test_target_provider_uses_its_own_credential_count(self):
        text = """
        ┌ Credentials ~/.local/share/opencode/auth.json
        │ openai 0 credentials
        │ anthropic 1 credential
        └ 1 credentials
        """

        self.assertFalse(verify_opencode_auth_list_output(text, "openai"))
        self.assertTrue(verify_opencode_auth_list_output(text, "anthropic"))

    def test_provider_does_not_match_header_path(self):
        text = """
        ┌ Credentials ~/.local/share/opencode/auth.json
        │ anthropic 1 credential
        └ 1 credentials
        """

        self.assertFalse(verify_opencode_auth_list_output(text, "opencode"))


if __name__ == "__main__":
    unittest.main()
