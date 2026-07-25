"""Cross-backend policy for backend-native, session-only background tools.

Some agent backends expose primitives that schedule work inside the CLI process
itself: background subagents, self-scheduled wakeups, in-session cron jobs, and
multi-agent workflows. Their results are delivered only while that process is
alive, so anything still pending when the session ends is lost silently — the
user is never told, and the work is not recorded anywhere Avibe can inspect.

Avibe Harness already provides durable equivalents for every one of them
(``vibe agent run``, ``vibe task add``, ``vibe watch add``), which survive
restarts, record an ``agent_runs`` row, and deliver through a callback. The
system prompt asks agents to prefer the Harness, but instruction alone is not
enough: an agent that reaches for the backend-native tool anyway produces work
that quietly disappears.

A background shell is session-only for the same reason, but its background form
is overwhelmingly used for work that finishes inside the turn, so it is advised
rather than denied: the call proceeds with a note pointing at `vibe watch add`
for anything that might outlive the turn.

This module owns the decision. Backends adapt it to whatever enforcement seam
they have:

- Claude Code: an in-process SDK ``PreToolUse`` hook (see
  ``core/handlers/session_handler.py``), plus ``disallowed_tools`` as a
  name-level backstop.
- Codex / OpenCode: no session-only background primitives exist today, so
  nothing is gated. When one is added, register its tool name here and it is
  covered without touching this logic again.

The policy is intentionally pure — no SDK imports, no I/O — so it can be tested
directly and reused by any backend adapter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

# Set to a truthy value to let the agent use backend-native background tools
# anyway. Intended for the narrow case where a user explicitly asks for
# backend-native behavior and accepts that the result dies with the session.
ALLOW_NATIVE_BACKGROUND_TOOLS_ENV = "AVIBE_ALLOW_NATIVE_BACKGROUND_TOOLS"

_HARNESS_HINT = (
    "Avibe Harness records the run in `agent_runs` and delivers the result "
    "through a callback, so it survives a restart."
)


@dataclass(frozen=True)
class ToolPolicyDecision:
    """Outcome of a policy check for one tool call.

    Three shapes: allowed and silent, allowed with ``advice`` attached, or
    denied with a ``reason``. The middle one exists for tools whose background
    form is legitimate inside a turn but lossy across one — a hard block there
    would cost more than it saves.
    """

    allowed: bool
    reason: str = ""
    advice: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed


ALLOWED = ToolPolicyDecision(allowed=True)


def _deny(reason: str) -> ToolPolicyDecision:
    return ToolPolicyDecision(allowed=False, reason=reason)


def _advise(advice: str) -> ToolPolicyDecision:
    return ToolPolicyDecision(allowed=True, advice=advice)


def _check_agent(tool_input: Dict[str, Any]) -> ToolPolicyDecision:
    # Background is the tool's default, so only an explicit false is synchronous.
    if tool_input.get("run_in_background") is False:
        return ALLOWED
    return _deny(
        "A background subagent dies with this agent process and its result is "
        "never delivered.\n"
        "Use the durable Harness instead:\n"
        "  vibe agent run --agent <name> --message-file <path> "
        "--callback-session-id $AVIBE_SESSION_ID\n"
        "A synchronous subagent is still fine — re-call this tool with "
        "run_in_background: false when you need the result in this same turn.\n"
        f"{_HARNESS_HINT}"
    )


def _check_schedule_wakeup(tool_input: Dict[str, Any]) -> ToolPolicyDecision:
    # Ending an already-running loop must stay reachable.
    if tool_input.get("stop") is True:
        return ALLOWED
    return _deny(
        "A self-scheduled wakeup is session-only and never fires once this "
        "agent process exits.\n"
        "Use the durable Harness instead:\n"
        "  vibe task add --at <ISO-8601> --message ...   (one-shot)\n"
        '  vibe task add --cron "<expr>" --message ...   (recurring)\n'
        "If you are waiting on an external signal rather than a clock, use "
        "`vibe watch add` instead.\n"
        f"{_HARNESS_HINT}"
    )


def _check_cron_create(tool_input: Dict[str, Any]) -> ToolPolicyDecision:
    if tool_input.get("durable") is True:
        return ALLOWED
    return _deny(
        "A non-durable cron job lives only in this session and is gone when "
        "the agent process exits.\n"
        "Use the durable Harness instead:\n"
        '  vibe task add --cron "<expr>" --message ...\n'
        f"{_HARNESS_HINT}"
    )


def _check_bash(tool_input: Dict[str, Any]) -> ToolPolicyDecision:
    # A background shell is session-only too, but unlike the tools above it is
    # overwhelmingly used for work that finishes inside the turn (a build, a
    # push, a test run). Denying all of it would cost more than the occasional
    # lost result, so this advises instead of blocking.
    if tool_input.get("run_in_background") is not True:
        return ALLOWED
    return _advise(
        "This background shell is session-only: it dies with the agent process, "
        "and its output is lost if the session ends before it finishes. That is "
        "fine for work that completes inside this turn. If it may outlive the "
        "turn — a long build, a deploy, a CI or review wait, a remote job — run "
        "it under a durable watch instead:\n"
        "  vibe watch add --name <label> --message <what to do with the result> "
        "-- <command>\n"
        "Note that `nohup ... &` or a detached `&` inside the command escapes "
        "this check entirely and is never recoverable; prefer the watch."
    )


def _check_workflow(tool_input: Dict[str, Any]) -> ToolPolicyDecision:
    return _deny(
        "A backend-native workflow runs in the background and is scoped to "
        "this agent process, so a workflow still running when the session ends "
        "is lost.\n"
        "Express the orchestration through the Harness instead — one "
        "`vibe agent run` per unit of work, each with "
        "--callback-session-id $AVIBE_SESSION_ID.\n"
        f"{_HARNESS_HINT}"
    )


# Tool name -> predicate. Registering a name here is all a new backend-native
# background primitive needs; every adapter picks it up automatically.
_SESSION_ONLY_BACKGROUND_TOOLS: Dict[str, Callable[[Dict[str, Any]], ToolPolicyDecision]] = {
    "Agent": _check_agent,
    "Bash": _check_bash,  # advisory only
    "ScheduleWakeup": _check_schedule_wakeup,
    "CronCreate": _check_cron_create,
    "Workflow": _check_workflow,
}

# Tools that are session-only under every input, so a name-level deny list is a
# faithful backstop for them. `Agent` and `CronCreate` are excluded because they
# have legitimate non-background forms that only argument inspection can tell
# apart, and `Bash` because this policy never denies it.
ALWAYS_SESSION_ONLY_TOOL_NAMES: Tuple[str, ...] = ("ScheduleWakeup", "Workflow")


def session_only_background_tool_names() -> Tuple[str, ...]:
    """Every tool name this policy inspects, for matcher construction."""
    return tuple(_SESSION_ONLY_BACKGROUND_TOOLS)


def native_background_tools_allowed(env: Optional[Dict[str, str]] = None) -> bool:
    """True when the operator has opted back into backend-native background work."""
    source = os.environ if env is None else env
    return bool(source.get(ALLOW_NATIVE_BACKGROUND_TOOLS_ENV, "").strip())


def check_tool_call(
    tool_name: str,
    tool_input: Optional[Dict[str, Any]] = None,
    *,
    env: Optional[Dict[str, str]] = None,
) -> ToolPolicyDecision:
    """Decide whether one tool call may proceed.

    Unknown tools and every tool outside the session-only background family are
    allowed unconditionally; this policy only ever acts on a registered name.
    """
    if native_background_tools_allowed(env):
        return ALLOWED
    check = _SESSION_ONLY_BACKGROUND_TOOLS.get(tool_name)
    if check is None:
        return ALLOWED
    return check(tool_input or {})
