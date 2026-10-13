"""Shared integration test cases for all platforms.

Each platform test module imports these and runs them with its driver.
This avoids duplicating test logic across slack/discord/feishu.
"""

import pytest


@pytest.mark.allow_network(reason="drives the real Slack, Discord and Feishu APIs with platform tokens")
class PlatformIntegrationTests:
    """Mixin class with shared integration test cases.

    Subclass must provide a `driver` fixture that returns a PlatformDriver.
    """

    @pytest.mark.asyncio
    async def test_send_message_and_get_reply(self, driver):
        """Bot B sends a message, Bot A replies."""
        reply = await driver.send_and_wait_reply(
            "Hello, please respond with exactly: PONG",
            timeout=120,
        )
        assert "PONG" in reply.text.upper() or len(reply.text) > 0, f"Expected Bot A to reply, got: {reply.text!r}"

    @pytest.mark.asyncio
    async def test_start_command(self, driver):
        """Bot B sends /start, Bot A replies with welcome."""
        reply = await driver.send_command("start", timeout=30)
        # /start should produce a welcome message (exact text varies by language)
        assert len(reply.text) > 0, "Expected non-empty reply from /start"

    @pytest.mark.asyncio
    async def test_cwd_command(self, driver):
        """Bot B sends /cwd, Bot A replies with current working directory."""
        reply = await driver.send_command("cwd", timeout=30)
        assert "/" in reply.text or "cwd" in reply.text.lower(), f"Expected CWD path in reply, got: {reply.text!r}"

    @pytest.mark.asyncio
    async def test_clear_command(self, driver):
        """Bot B sends /clear, Bot A confirms session cleared."""
        reply = await driver.send_command("clear", timeout=30)
        assert len(reply.text) > 0, "Expected confirmation from /clear"

    @pytest.mark.asyncio
    async def test_thread_continuation(self, driver):
        """Bot B sends a message, then continues in the same thread."""
        # First message
        reply1 = await driver.send_and_wait_reply(
            "Please respond with: FIRST",
            timeout=120,
        )
        assert reply1.message_id, "Expected a message ID for threading"

        # Follow-up in the thread (use reply's thread_id or message_id)
        thread_id = reply1.thread_id or reply1.message_id
        reply2 = await driver.send_and_wait_reply(
            "Now respond with: SECOND",
            timeout=120,
            thread_id=thread_id,
        )
        assert len(reply2.text) > 0, "Expected Bot A to reply in thread"
