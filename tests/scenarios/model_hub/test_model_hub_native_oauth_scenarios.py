from __future__ import annotations

import asyncio

import pytest

from tests.scenario_harness.model_hub_native_oauth import NativeOAuthScenarioHarness


def test_mh_oauth_native_001_claude_paste_code_happy_path():
    """Scenario: MH-OAUTH-NATIVE-001."""

    async def run():
        harness = NativeOAuthScenarioHarness()
        harness.auth_status["claude"] = {
            "active_auth_mode": "oauth",
        }

        started = await harness.adapter.start_oauth("src_claude01", "anthropic")

        assert started.state == "awaiting_action"
        assert started.expects == "paste_code"
        assert started.auth_url == "https://claude.ai/oauth/authorize?test=true"
        assert started.device_code is None
        assert started.credential_ref is None

        submitted = await harness.adapter.submit_oauth(
            started.flow_id,
            "code-value#state-value",
        )
        assert submitted.state == "verifying"
        assert harness.agent_auth.submissions == [(started.flow_id, "code-value#state-value")]

        harness.agent_auth.complete(started.flow_id)
        completed = await harness.adapter.oauth_status(started.flow_id)
        source_status = harness.adapter.completed_source_status(started.flow_id)

        assert completed.state == "success"
        assert completed.source_id == "src_claude01"
        assert completed.credential_ref is None
        assert source_status.signed_in is True
        assert source_status.account_label is None

    asyncio.run(run())


def test_mh_oauth_native_002_codex_device_code_self_completes():
    """Scenario: MH-OAUTH-NATIVE-002."""

    async def run():
        harness = NativeOAuthScenarioHarness()
        harness.auth_status["codex"] = {
            "active_auth_mode": "oauth",
            "chatgpt_account": {"email": "chatgpt-owner@example.com"},
        }

        started = await harness.adapter.start_oauth("src_chatgpt01", "openai")
        assert started.state == "starting"
        assert started.expects == "none"

        harness.agent_auth.expose_codex_device_flow(started.flow_id)
        awaiting = await harness.adapter.oauth_status(started.flow_id)
        assert awaiting.state == "awaiting_action"
        assert awaiting.auth_url == "https://auth.openai.com/codex/device"
        assert awaiting.device_code == "T74L-XU61D"
        assert awaiting.expects == "none"

        harness.agent_auth.complete(started.flow_id)
        completed = await harness.adapter.oauth_status(started.flow_id)
        source_status = harness.adapter.completed_source_status(started.flow_id)

        assert completed.state == "success"
        assert completed.credential_ref is None
        assert harness.agent_auth.submissions == []
        assert source_status.signed_in is True
        assert source_status.account_label == "chatgpt-owner@example.com"

    asyncio.run(run())


def test_mh_oauth_native_003_cancel_and_timeout_terminate_cleanly():
    """Scenario: MH-OAUTH-NATIVE-003."""

    async def run():
        harness = NativeOAuthScenarioHarness()

        timed = await harness.adapter.start_oauth("src_timeout01", "anthropic")
        harness.agent_auth.timeout(timed.flow_id)
        timed_out = await harness.adapter.oauth_status(timed.flow_id)

        assert timed_out.state == "failed"
        assert timed_out.error_key == "settings.models.oauth.error.timeout"
        assert timed_out.credential_ref is None

        signed_out = await harness.adapter.start_oauth("src_signedout01", "anthropic")
        harness.auth_status["claude"] = {"active_auth_mode": "none"}
        harness.agent_auth.complete(signed_out.flow_id)
        await harness.adapter.oauth_status(signed_out.flow_id)
        assert harness.adapter.completed_source_status(signed_out.flow_id).signed_in is False

        cancelled = await harness.adapter.start_oauth("src_cancel01", "openai")
        await harness.adapter.cancel_oauth(cancelled.flow_id)

        assert harness.agent_auth.cancelled == [cancelled.flow_id]
        with pytest.raises(KeyError):
            await harness.adapter.oauth_status(cancelled.flow_id)

    asyncio.run(run())
