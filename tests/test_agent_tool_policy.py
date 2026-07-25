"""Session-only background tools must be redirected to the Avibe Harness.

Covers the pure policy in ``core/agent_tool_policy.py`` and the Claude adapter
in ``core/handlers/session_handler.py``. Everything here is in-process: no
config, no SQLite state, no Claude SDK, no subprocess.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tool_policy as policy
from core.handlers import session_handler as sh


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,tool_input",
    [
        ("Agent", {}),  # background is the tool's default
        ("Agent", {"run_in_background": True}),
        ("ScheduleWakeup", {"delaySeconds": 600, "prompt": "x", "reason": "y"}),
        ("CronCreate", {"cron": "0 9 * * *", "prompt": "x"}),
        ("CronCreate", {"cron": "0 9 * * *", "prompt": "x", "durable": False}),
        ("Workflow", {"script": "export const meta = {}"}),
    ],
)
def test_session_only_background_calls_are_denied(tool_name, tool_input):
    decision = policy.check_tool_call(tool_name, tool_input, env={})
    assert decision.denied
    assert "vibe " in decision.reason  # every deny names an executable alternative


@pytest.mark.parametrize(
    "tool_name,tool_input",
    [
        ("Agent", {"run_in_background": False}),  # resolves inside this turn
        ("ScheduleWakeup", {"stop": True}),  # ending a loop must stay reachable
        ("CronCreate", {"cron": "0 9 * * *", "prompt": "x", "durable": True}),
    ],
)
def test_durable_or_synchronous_calls_are_allowed(tool_name, tool_input):
    assert policy.check_tool_call(tool_name, tool_input, env={}).allowed


@pytest.mark.parametrize("tool_name", ["Bash", "Read", "Write", "TaskCreate", ""])
def test_unrelated_tools_are_untouched(tool_name):
    assert policy.check_tool_call(tool_name, {"run_in_background": True}, env={}).allowed


def test_missing_tool_input_is_treated_as_the_tool_default():
    assert policy.check_tool_call("Agent", None, env={}).denied


def test_env_escape_hatch_restores_backend_native_behavior():
    env = {policy.ALLOW_NATIVE_BACKGROUND_TOOLS_ENV: "1"}
    assert policy.check_tool_call("Agent", {}, env=env).allowed
    assert policy.check_tool_call("Workflow", {}, env=env).allowed
    assert policy.native_background_tools_allowed(env) is True


def test_blank_escape_hatch_does_not_disable_the_policy():
    env = {policy.ALLOW_NATIVE_BACKGROUND_TOOLS_ENV: "   "}
    assert policy.native_background_tools_allowed(env) is False
    assert policy.check_tool_call("Agent", {}, env=env).denied


def test_always_session_only_names_are_a_subset_of_the_policy():
    covered = set(policy.session_only_background_tool_names())
    assert set(policy.ALWAYS_SESSION_ONLY_TOOL_NAMES) <= covered
    # Names with a legitimate non-background form must not be blocked by name.
    assert "Agent" not in policy.ALWAYS_SESSION_ONLY_TOOL_NAMES
    assert "CronCreate" not in policy.ALWAYS_SESSION_ONLY_TOOL_NAMES


# --------------------------------------------------------------------------
# claude adapter
# --------------------------------------------------------------------------


class _Controller:
    """Minimal stand-in for BaseHandler's controller dependency."""

    def __init__(self):
        self.config = object()
        self.im_client = object()
        self.settings_manager = object()
        self.sessions = object()
        self.session_manager = object()
        self.claude_sessions = {}
        self.receiver_tasks = {}
        self.stored_session_mappings = {}


def _handler() -> sh.SessionHandler:
    return sh.SessionHandler(_Controller())


def _run_guard(tool_name, tool_input):
    return asyncio.run(
        _handler()._guard_session_only_background_tools(
            {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input},
            "toolu_test",
            None,
        )
    )


def test_guard_denies_a_background_subagent():
    out = _run_guard("Agent", {"prompt": "go", "run_in_background": True})
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert "vibe agent run" in specific["permissionDecisionReason"]


def test_guard_allows_a_synchronous_subagent():
    assert _run_guard("Agent", {"prompt": "go", "run_in_background": False}) == {}


def test_guard_allows_unrelated_tools():
    assert _run_guard("Bash", {"command": "ls"}) == {}


def test_guard_never_raises_on_a_malformed_payload():
    # A guard that throws would take the whole turn down with it.
    out = asyncio.run(_handler()._guard_session_only_background_tools({}, None, None))
    assert out == {}


def test_hooks_are_wired_when_the_sdk_supports_them(monkeypatch):
    captured = {}

    class _HookMatcher:
        def __init__(self, matcher=None, hooks=None, timeout=None):
            captured["matcher"] = matcher
            captured["hooks"] = hooks

    monkeypatch.setattr(sh, "CLAUDE_SDK_HOOKS_AVAILABLE", True)
    monkeypatch.setattr(sh, "HookMatcher", _HookMatcher)
    monkeypatch.delenv(policy.ALLOW_NATIVE_BACKGROUND_TOOLS_ENV, raising=False)

    handler = _handler()
    hooks = handler._build_claude_tool_policy_hooks()

    assert list(hooks) == ["PreToolUse"]
    assert set(captured["matcher"].split("|")) == set(policy.session_only_background_tool_names())
    assert captured["hooks"] == [handler._guard_session_only_background_tools]
    # The precise hook path owns enforcement, so nothing extra is denied by name.
    assert handler._claude_disallowed_tools(hooks) == sh.CLAUDE_REMOTE_DISALLOWED_TOOLS


def test_older_sdk_falls_back_to_a_name_level_deny_list(monkeypatch):
    monkeypatch.setattr(sh, "CLAUDE_SDK_HOOKS_AVAILABLE", False)
    monkeypatch.setattr(sh, "HookMatcher", None)
    monkeypatch.delenv(policy.ALLOW_NATIVE_BACKGROUND_TOOLS_ENV, raising=False)

    handler = _handler()
    assert handler._build_claude_tool_policy_hooks() is None

    disallowed = handler._claude_disallowed_tools(None)
    for name in sh.CLAUDE_REMOTE_DISALLOWED_TOOLS:
        assert name in disallowed
    for name in policy.ALWAYS_SESSION_ONLY_TOOL_NAMES:
        assert name in disallowed
    assert len(disallowed) == len(set(disallowed))


def test_escape_hatch_disables_both_enforcement_paths(monkeypatch):
    monkeypatch.setattr(sh, "CLAUDE_SDK_HOOKS_AVAILABLE", True)
    monkeypatch.setenv(policy.ALLOW_NATIVE_BACKGROUND_TOOLS_ENV, "1")

    handler = _handler()
    assert handler._build_claude_tool_policy_hooks() is None
    assert handler._claude_disallowed_tools(None) == sh.CLAUDE_REMOTE_DISALLOWED_TOOLS


def test_disallowed_tools_constant_is_not_mutated(monkeypatch):
    monkeypatch.setattr(sh, "CLAUDE_SDK_HOOKS_AVAILABLE", False)
    monkeypatch.delenv(policy.ALLOW_NATIVE_BACKGROUND_TOOLS_ENV, raising=False)
    before = list(sh.CLAUDE_REMOTE_DISALLOWED_TOOLS)

    _handler()._claude_disallowed_tools(None)

    assert sh.CLAUDE_REMOTE_DISALLOWED_TOOLS == before
