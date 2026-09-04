"""Session-only background tools must be redirected to the Avibe Harness.

Covers the pure policy in ``core/agent_tool_policy.py`` and the Claude adapter
in ``core/handlers/session_handler.py``. Everything here is in-process: no
config, no SQLite state, no Claude SDK, no subprocess.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import agent_tool_policy as policy
from core import system_prompt_injection as spi
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
    decision = policy.check_tool_call(tool_name, tool_input)
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
    assert policy.check_tool_call(tool_name, tool_input).allowed


@pytest.mark.parametrize("tool_name", ["Read", "Write", "TaskCreate", "WebFetch", ""])
def test_unrelated_tools_are_untouched(tool_name):
    decision = policy.check_tool_call(tool_name, {"run_in_background": True})
    assert decision.allowed
    assert not decision.advice


def test_background_shell_is_advised_not_denied():
    decision = policy.check_tool_call("Bash", {"command": "make", "run_in_background": True})
    assert decision.allowed  # a hard block would cost more than it saves
    assert "vibe watch add" in decision.advice
    assert "nohup" in decision.advice  # detaching escapes the check entirely


@pytest.mark.parametrize(
    "tool_input",
    [
        {"command": "ls"},
        {"command": "ls", "run_in_background": False},
    ],
)
def test_foreground_shell_gets_no_advice(tool_input):
    decision = policy.check_tool_call("Bash", tool_input)
    assert decision.allowed
    assert not decision.advice


def test_bash_is_never_denied_under_any_input():
    for tool_input in ({}, {"run_in_background": True}, {"run_in_background": "yes"}):
        assert policy.check_tool_call("Bash", tool_input).allowed


def test_agent_denial_points_out_that_concurrency_survives():
    # Without this, an agent reading the deny can conclude it must now fan work
    # out serially. Several synchronous calls in one message still run at once,
    # so the only thing background buys is outliving the turn.
    reason = policy.check_tool_call("Agent", {}).reason
    assert "run concurrently" in reason
    assert "run_in_background: false" in reason


# The scheduler denials whose text embeds runnable `vibe task add` examples.
_SCHEDULER_DENIALS = (
    ("CronCreate", {"cron": "0 9 * * *", "prompt": "x"}),
    ("ScheduleWakeup", {"delaySeconds": 600, "prompt": "x", "reason": "y"}),
)


def test_scheduler_denials_offer_the_command_task_form():
    # A scheduled shell command has no message to write, so a denial that shows
    # only `--message` forms leaves the agent no Harness equivalent for it and
    # pushes it back toward the session-only tool this policy just refused.
    for tool_name, tool_input in _SCHEDULER_DENIALS:
        reason = policy.check_tool_call(tool_name, tool_input).reason
        assert 'vibe task add --cron "<expr>" --shell "<cmd>"' in reason

# Trailing "  (recurring)" style annotations are prose, not argv.
_EXAMPLE_ANNOTATION = re.compile(r"\s*\([^()]*\)\s*$")


def _embedded_task_examples() -> list[str]:
    examples: list[str] = []
    for tool_name, tool_input in _SCHEDULER_DENIALS:
        reason = policy.check_tool_call(tool_name, tool_input).reason
        for raw in reason.splitlines():
            line = raw.strip()
            if line.startswith("vibe task add"):
                examples.append(_EXAMPLE_ANNOTATION.sub("", line))
    return examples


def test_embedded_task_examples_parse_against_the_real_cli():
    # These denials are live callers: an agent copies the example verbatim. A
    # renamed or removed flag must fail here, not in the user's shell.
    from vibe import cli  # imported lazily; the policy itself pulls in no CLI

    examples = _embedded_task_examples()
    # CronCreate embeds 2 (--message, --shell); ScheduleWakeup embeds 3
    # (one-shot --message, recurring --message, --shell).
    assert len(examples) == 5
    for example in examples:
        argv = shlex.split(example)[1:]  # drop the leading `vibe`
        cli.build_parser().parse_args(argv)  # SystemExit(2) if an example rots


def test_missing_tool_input_is_treated_as_the_tool_default():
    assert policy.check_tool_call("Agent", None).denied


def test_removed_environment_override_cannot_disable_the_policy(monkeypatch):
    monkeypatch.setenv("AVIBE_ALLOW_NATIVE_BACKGROUND_TOOLS", "1")
    assert policy.check_tool_call("Agent", {}).denied
    assert policy.check_tool_call("Workflow", {}).denied


def test_always_session_only_names_are_a_subset_of_the_policy():
    covered = set(policy.session_only_background_tool_names())
    assert set(policy.ALWAYS_SESSION_ONLY_TOOL_NAMES) <= covered
    # Names with a legitimate non-background form must not be blocked by name,
    # and an advisory-only tool must never reach a deny list at all.
    assert "Agent" not in policy.ALWAYS_SESSION_ONLY_TOOL_NAMES
    assert "CronCreate" not in policy.ALWAYS_SESSION_ONLY_TOOL_NAMES
    assert "Bash" not in policy.ALWAYS_SESSION_ONLY_TOOL_NAMES


def test_name_only_fallback_never_strands_a_running_loop():
    # `ScheduleWakeup {"stop": true}` is the only way to end a dynamic loop.
    # A name-level block cannot see the argument, so listing the tool there
    # would make an already-running loop unstoppable on older SDKs.
    assert "ScheduleWakeup" not in policy.ALWAYS_SESSION_ONLY_TOOL_NAMES
    assert policy.check_tool_call("ScheduleWakeup", {"stop": True}).allowed


@pytest.mark.parametrize("name", policy.ALWAYS_SESSION_ONLY_TOOL_NAMES)
def test_name_only_fallback_entries_are_denied_under_every_input(name):
    # A name-level block is faithful only if no input to that tool is allowed.
    for tool_input in ({}, {"stop": True}, {"durable": True}, {"run_in_background": False}):
        assert policy.check_tool_call(name, tool_input).denied


# --------------------------------------------------------------------------
# injected prompt
# --------------------------------------------------------------------------


def test_shared_prompt_module_has_no_top_level_claude_import():
    # `core/system_prompt_injection.py` is imported by every backend adapter,
    # and the Codex adapter is loaded under a stub `modules` namespace where a
    # Claude-only import raises at module scope. A top-level import here breaks
    # collection of that suite outright, so the dependency stays inside the
    # Claude branch of the selector.
    import ast

    source = Path(spi.__file__).read_text(encoding="utf-8")
    for node in ast.parse(source).body:  # module scope only, not nested
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("modules.claude"), (
                f"top-level import of {node.module} makes this module "
                "unloadable for non-Claude backends"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("modules.claude")


def test_prompt_describes_enforcement_when_the_policy_is_active(monkeypatch):
    monkeypatch.setattr(spi, "_claude_sdk_hooks_available", lambda: True)
    section = spi._build_tool_policy_section("claude")
    assert "is blocked at the tool layer" in section
    assert "are all denied" in section


def test_prompt_admits_partial_enforcement_without_argument_aware_hooks(monkeypatch):
    # The name-only fallback cannot inspect arguments, so it blocks Workflow and
    # nothing else. Claiming full denial would stop the agent from self-policing
    # exactly the calls no gate is covering.
    monkeypatch.setattr(spi, "_claude_sdk_hooks_available", lambda: False)
    section = spi._build_tool_policy_section("claude")
    assert "only partly blocked" in section
    assert "A native multi-agent workflow is denied outright" in section
    assert "are **not** stopped here" in section
    assert "is blocked at the tool layer" not in section
    assert "are all denied" not in section


def test_hookless_prompt_names_every_tool_the_name_list_leaves_open(monkeypatch):
    # Whatever `check_tool_call` can deny but the name-only list does not carry
    # is unguarded on this path, and the prompt is the only thing left telling
    # the agent so. `Bash` is exempt because the policy never denies it.
    monkeypatch.setattr(spi, "_claude_sdk_hooks_available", lambda: False)
    section = spi._build_tool_policy_section("claude")
    unguarded = {
        name
        for name in policy.session_only_background_tool_names()
        if name not in policy.ALWAYS_SESSION_ONLY_TOOL_NAMES and name != "Bash"
    }
    assert unguarded == {"Agent", "ScheduleWakeup", "CronCreate"}
    described = {
        "Agent": "background subagent",
        "ScheduleWakeup": "self-scheduled wakeup",
        "CronCreate": "non-durable in-session cron job",
    }
    for name in unguarded:
        assert described[name] in section, f"{name} is unguarded but unmentioned"


def test_prompt_claims_no_gate_on_backends_that_install_none(monkeypatch):
    # Only the Claude session handler installs the hook, so hook availability in
    # an importable SDK says nothing about a Codex or OpenCode session.
    monkeypatch.setattr(spi, "_claude_sdk_hooks_available", lambda: True)
    for backend in ("codex", "opencode", "unknown"):
        section = spi._build_tool_policy_section(backend)
        assert "is not gated in this runtime" in section
        assert "blocked at the tool layer" not in section
        assert "only partly blocked" not in section
        assert "vibe watch add" in section

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
    assert _run_guard("Read", {"file_path": "/tmp/x"}) == {}


def test_guard_stays_silent_on_a_foreground_shell():
    assert _run_guard("Bash", {"command": "ls"}) == {}


def test_guard_advises_on_a_background_shell_without_granting_permission():
    out = _run_guard("Bash", {"command": "make", "run_in_background": True})
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert "vibe watch add" in specific["additionalContext"]
    # Injecting context must not double as an approval: an explicit "allow" here
    # would override any permission hook the user configured for Bash.
    assert "permissionDecision" not in specific


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

    handler = _handler()
    hooks = handler._build_claude_tool_policy_hooks()

    assert list(hooks) == ["PreToolUse"]
    assert set(captured["matcher"].split("|")) == set(
        policy.session_only_background_tool_names()
    )
    assert captured["hooks"] == [handler._guard_session_only_background_tools]
    # The precise hook path owns enforcement, so nothing extra is denied by name.
    assert handler._claude_disallowed_tools(hooks) == sh.CLAUDE_REMOTE_DISALLOWED_TOOLS


def test_removed_environment_override_cannot_disable_claude_hooks(monkeypatch):
    class _HookMatcher:
        def __init__(self, matcher=None, hooks=None, timeout=None):
            self.matcher = matcher
            self.hooks = hooks

    monkeypatch.setattr(sh, "CLAUDE_SDK_HOOKS_AVAILABLE", True)
    monkeypatch.setattr(sh, "HookMatcher", _HookMatcher)
    monkeypatch.setenv("AVIBE_ALLOW_NATIVE_BACKGROUND_TOOLS", "1")

    assert _handler()._build_claude_tool_policy_hooks() is not None


def test_older_sdk_falls_back_to_a_name_level_deny_list(monkeypatch):
    monkeypatch.setattr(sh, "CLAUDE_SDK_HOOKS_AVAILABLE", False)
    monkeypatch.setattr(sh, "HookMatcher", None)

    handler = _handler()
    assert handler._build_claude_tool_policy_hooks() is None

    disallowed = handler._claude_disallowed_tools(None)
    for name in sh.CLAUDE_REMOTE_DISALLOWED_TOOLS:
        assert name in disallowed
    for name in policy.ALWAYS_SESSION_ONLY_TOOL_NAMES:
        assert name in disallowed
    assert len(disallowed) == len(set(disallowed))

def test_disallowed_tools_constant_is_not_mutated(monkeypatch):
    monkeypatch.setattr(sh, "CLAUDE_SDK_HOOKS_AVAILABLE", False)
    before = list(sh.CLAUDE_REMOTE_DISALLOWED_TOOLS)

    _handler()._claude_disallowed_tools(None)

    assert sh.CLAUDE_REMOTE_DISALLOWED_TOOLS == before
