"""Unit tests for per-Agent env overrides (``VibeAgent.metadata["env"]``).

Two Agents on the same claude backend must be able to run with isolated
authentication (e.g. one OAuth via Claude Code's credential store, one
``ANTHROPIC_API_KEY``) while sharing the backend binary, settings, and
skills. The override surface is intentionally tiny: a plain env mapping in
Agent metadata, resolved by ``core.vibe_agents.resolve_agent_env_overrides``
and applied on top of ``build_claude_subprocess_env`` output at spawn time.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from core.handlers.session_handler import SessionHandler
from core.vibe_agents import (
    VibeAgent,
    resolve_agent_env_overrides,
    vibe_agent_name_from_platform_payload,
)


def _agent(metadata: dict | None) -> VibeAgent:
    return VibeAgent(
        id=str(uuid4()),
        name="worker",
        normalized_name="worker",
        backend="claude",
        metadata=metadata if metadata is not None else {},
    )


# ---------------------------------------------------------------------------
# resolve_agent_env_overrides
# ---------------------------------------------------------------------------


def test_no_agent_returns_empty() -> None:
    assert resolve_agent_env_overrides(None) == {}


def test_metadata_without_env_returns_empty() -> None:
    assert resolve_agent_env_overrides(_agent({"builtin": True})) == {}


def test_non_dict_env_metadata_is_ignored() -> None:
    assert resolve_agent_env_overrides(_agent({"env": "ANTHROPIC_API_KEY=sk-x"})) == {}
    assert resolve_agent_env_overrides(_agent({"env": ["ANTHROPIC_API_KEY"]})) == {}


def test_literal_values_pass_through() -> None:
    agent = _agent({"env": {"ANTHROPIC_API_KEY": "sk-ant-agent", "ANTHROPIC_BASE_URL": "https://relay.example"}})
    assert resolve_agent_env_overrides(agent) == {
        "ANTHROPIC_API_KEY": "sk-ant-agent",
        "ANTHROPIC_BASE_URL": "https://relay.example",
    }


def test_values_are_coerced_to_strings_and_none_is_skipped() -> None:
    agent = _agent({"env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": 32000, "DROPPED": None}})
    assert resolve_agent_env_overrides(agent) == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000"}


def test_blank_and_non_string_keys_are_skipped() -> None:
    agent = _agent({"env": {"": "x", "  ": "y", 7: "z", " PADDED ": "kept"}})
    assert resolve_agent_env_overrides(agent) == {"PADDED": "kept"}


def test_env_var_indirection_resolves_from_base_env() -> None:
    agent = _agent({"env": {"ANTHROPIC_API_KEY": "${WORK_ANTHROPIC_KEY}"}})
    out = resolve_agent_env_overrides(agent, base_env={"WORK_ANTHROPIC_KEY": "sk-ant-indirect"})
    assert out == {"ANTHROPIC_API_KEY": "sk-ant-indirect"}


def test_unset_env_var_indirection_is_skipped_not_empty() -> None:
    # Injecting "" would silently break auth; a missing secret must stay
    # visible as "no override" instead.
    agent = _agent({"env": {"ANTHROPIC_API_KEY": "${MISSING_KEY}"}})
    assert resolve_agent_env_overrides(agent, base_env={}) == {}


def test_bare_dollar_brace_literal_is_kept() -> None:
    agent = _agent({"env": {"MARKER": "${}"}})
    assert resolve_agent_env_overrides(agent, base_env={}) == {"MARKER": "${}"}


# ---------------------------------------------------------------------------
# vibe_agent_name_from_platform_payload
# ---------------------------------------------------------------------------


def test_payload_none_returns_none() -> None:
    assert vibe_agent_name_from_platform_payload(None) is None
    assert vibe_agent_name_from_platform_payload({}) is None


def test_resolved_vibe_agent_name_wins() -> None:
    payload = {
        "resolved_vibe_agent": {"name": "cc-api"},
        "agent_session_target": {"agent_name": "other"},
    }
    assert vibe_agent_name_from_platform_payload(payload) == "cc-api"


def test_agent_session_target_is_fallback() -> None:
    payload = {"agent_session_target": {"agent_name": "cc-api"}}
    assert vibe_agent_name_from_platform_payload(payload) == "cc-api"


def test_blank_names_are_ignored() -> None:
    payload = {
        "resolved_vibe_agent": {"name": "  "},
        "agent_session_target": {"agent_name": ""},
    }
    assert vibe_agent_name_from_platform_payload(payload) is None


# ---------------------------------------------------------------------------
# SessionHandler._vibe_agent_env_overrides glue
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, agent: VibeAgent | None):
        self._agent = agent
        self.requested: list[str] = []

    def get(self, name: str) -> VibeAgent | None:
        self.requested.append(name)
        return self._agent


def _handler_with_store(store) -> SimpleNamespace:
    return SimpleNamespace(controller=SimpleNamespace(vibe_agent_store=store))


def _context(payload: dict | None) -> SimpleNamespace:
    return SimpleNamespace(platform_specific=payload)


def test_handler_resolves_agent_env_from_payload() -> None:
    agent = _agent({"env": {"ANTHROPIC_API_KEY": "sk-ant-agent"}})
    store = _FakeStore(agent)
    fake_self = _handler_with_store(store)
    context = _context({"resolved_vibe_agent": {"name": "worker"}})

    out = SessionHandler._vibe_agent_env_overrides(fake_self, context)

    assert out == {"ANTHROPIC_API_KEY": "sk-ant-agent"}
    assert store.requested == ["worker"]


def test_handler_without_agent_in_payload_is_noop() -> None:
    store = _FakeStore(_agent({"env": {"ANTHROPIC_API_KEY": "sk-ant-agent"}}))
    fake_self = _handler_with_store(store)

    assert SessionHandler._vibe_agent_env_overrides(fake_self, _context(None)) == {}
    assert store.requested == []


def test_handler_tolerates_store_lookup_failure() -> None:
    class _BrokenStore:
        def get(self, name: str):
            raise RuntimeError("db locked")

    fake_self = _handler_with_store(_BrokenStore())
    context = _context({"resolved_vibe_agent": {"name": "worker"}})

    assert SessionHandler._vibe_agent_env_overrides(fake_self, context) == {}


def test_handler_without_store_is_noop() -> None:
    fake_self = SimpleNamespace(controller=SimpleNamespace())
    context = _context({"resolved_vibe_agent": {"name": "worker"}})

    assert SessionHandler._vibe_agent_env_overrides(fake_self, context) == {}
