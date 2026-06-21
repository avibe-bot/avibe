from __future__ import annotations

from types import SimpleNamespace

from core.controller import Controller
from modules.im import MessageContext


class _StubController(Controller):
    def __init__(self):
        pass


def _context() -> MessageContext:
    return MessageContext(
        user_id="U123",
        channel_id="C123",
        platform="slack",
        platform_specific={"is_dm": False},
    )


def test_resolve_vibe_agent_for_context_uses_catalog_default_when_scope_has_no_agent() -> None:
    controller = _StubController()
    default_agent = SimpleNamespace(name="reviewer", backend="codex")
    controller.primary_platform = "slack"
    controller._get_settings_key = lambda context: context.channel_id
    controller.vibe_agent_store = SimpleNamespace(
        require=lambda name: (_ for _ in ()).throw(ValueError(f"agent '{name}' not found")),
        get_builtin_default_agent_for_backend=lambda backend: None,
        get_default_agent=lambda: default_agent,
    )
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: SimpleNamespace(agent_name=None, agent_backend="opencode")
    )
    assert controller.resolve_vibe_agent_for_context(_context(), required=False) is default_agent


def test_resolve_vibe_agent_for_context_ignores_legacy_scope_backend() -> None:
    controller = _StubController()
    default_agent = SimpleNamespace(name="reviewer", backend="codex")
    controller.primary_platform = "slack"
    controller._get_settings_key = lambda context: context.channel_id
    controller.vibe_agent_store = SimpleNamespace(
        require=lambda name: (_ for _ in ()).throw(ValueError(f"agent '{name}' not found")),
        get_builtin_default_agent_for_backend=lambda backend: (_ for _ in ()).throw(
            AssertionError("scope backend should not be mapped to an Agent")
        ),
        get_default_agent=lambda: default_agent,
    )
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: SimpleNamespace(agent_name=None, agent_backend="opencode")
    )

    assert controller.resolve_vibe_agent_for_context(_context(), required=False) is default_agent


def test_resolve_agent_for_context_ignores_legacy_scope_backend() -> None:
    controller = _StubController()
    default_agent = SimpleNamespace(name="reviewer", backend="codex")
    controller.primary_platform = "slack"
    controller._get_settings_key = lambda context: context.channel_id
    controller.agent_service = SimpleNamespace(agents={"opencode": object(), "codex": object()})
    controller.vibe_agent_store = SimpleNamespace(
        require=lambda name: (_ for _ in ()).throw(ValueError(f"agent '{name}' not found")),
        get_builtin_default_agent_for_backend=lambda backend: (_ for _ in ()).throw(
            AssertionError("scope backend should not be mapped to an Agent")
        ),
        get_default_agent=lambda: default_agent,
    )
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: SimpleNamespace(agent_name=None, agent_backend="opencode")
    )
    controller.agent_router = SimpleNamespace(resolve=lambda platform, settings_key: "claude")

    assert controller.resolve_agent_for_context(_context()) == "codex"


def test_resolve_agent_for_context_uses_default_agent_not_router_default() -> None:
    controller = _StubController()
    default_agent = SimpleNamespace(name="reviewer", backend="codex")
    controller.primary_platform = "slack"
    controller._get_settings_key = lambda context: context.channel_id
    controller.agent_service = SimpleNamespace(agents={"opencode": object(), "codex": object()}, default_agent="opencode")
    controller.vibe_agent_store = SimpleNamespace(
        require=lambda name: (_ for _ in ()).throw(ValueError(f"agent '{name}' not found")),
        get_builtin_default_agent_for_backend=lambda backend: None,
        get_default_agent=lambda: default_agent,
    )
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: SimpleNamespace(agent_name=None, agent_backend=None)
    )
    controller.agent_router = SimpleNamespace(resolve=lambda platform, settings_key: "opencode")

    assert controller.resolve_agent_for_context(_context()) == "codex"


def test_resolve_agent_for_context_reports_unregistered_default_agent_backend() -> None:
    controller = _StubController()
    default_agent = SimpleNamespace(name="opencode", backend="opencode")
    controller.primary_platform = "slack"
    controller._get_settings_key = lambda context: context.channel_id
    controller.agent_service = SimpleNamespace(agents={"claude": object()}, default_agent="claude")
    controller.vibe_agent_store = SimpleNamespace(
        require=lambda name: (_ for _ in ()).throw(ValueError(f"agent '{name}' not found")),
        get_builtin_default_agent_for_backend=lambda backend: None,
        get_default_agent=lambda: default_agent,
    )
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: SimpleNamespace(agent_name=None, agent_backend=None)
    )

    assert controller.resolve_agent_for_context(_context()) == "opencode"


def test_codex_overrides_prefer_scope_level_model_and_reasoning() -> None:
    controller = _StubController()
    controller.primary_platform = "slack"
    controller._get_settings_key = lambda context: context.channel_id
    routing = SimpleNamespace(
        codex_agent=None,
        codex_model="gpt-5.4",
        codex_reasoning_effort="high",
        model="gpt-5.5",
        reasoning_effort="xhigh",
    )
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: routing
    )

    assert controller.get_codex_overrides(_context()) == (None, "gpt-5.5", "xhigh")


def test_builtin_agent_model_overrides_only_apply_to_matching_backend() -> None:
    controller = _StubController()
    controller.primary_platform = "slack"
    controller._get_settings_key = lambda context: context.channel_id
    routing = SimpleNamespace(
        agent_name="claude",
        codex_agent=None,
        opencode_agent=None,
        model="claude-opus-4-8",
        reasoning_effort="max",
    )
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: routing
    )

    assert controller.get_codex_overrides(_context()) == (None, None, None)
    assert controller.get_opencode_overrides(_context()) == (None, None, None)


def test_builtin_agent_model_overrides_still_apply_to_selected_backend() -> None:
    controller = _StubController()
    controller.primary_platform = "slack"
    controller._get_settings_key = lambda context: context.channel_id
    routing = SimpleNamespace(
        agent_name="codex",
        codex_agent=None,
        model="gpt-5.5",
        reasoning_effort="xhigh",
    )
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: routing
    )

    assert controller.get_codex_overrides(_context()) == (None, "gpt-5.5", "xhigh")


def test_avibe_run_target_agent_does_not_read_im_routing() -> None:
    controller = _StubController()
    reviewer = SimpleNamespace(name="reviewer", backend="codex")
    controller.primary_platform = "slack"
    controller._get_settings_key = lambda context: context.channel_id
    controller.agent_service = SimpleNamespace(agents={"codex": object(), "claude": object()})
    controller.vibe_agent_store = SimpleNamespace(
        require_enabled=lambda name: reviewer if name == "reviewer" else None,
        get_builtin_default_agent_for_backend=lambda backend: None,
        get_default_agent=lambda: SimpleNamespace(name="default", backend="claude"),
    )
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: (_ for _ in ()).throw(AssertionError("routing should not be read"))
    )

    ctx = MessageContext(
        user_id="user-1",
        channel_id="ses-1",
        platform="avibe",
        platform_specific={"agent_run_target": {"agent_name": "reviewer", "agent_backend": "codex"}},
    )

    assert controller.resolve_vibe_agent_for_context(ctx, required=False) is reviewer
    assert controller.resolve_agent_for_context(ctx) == "codex"


def test_avibe_run_target_overrides_do_not_read_im_routing() -> None:
    controller = _StubController()
    controller.primary_platform = "slack"
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: (_ for _ in ()).throw(AssertionError("routing should not be read"))
    )

    ctx = MessageContext(
        user_id="user-1",
        channel_id="ses-1",
        platform="avibe",
        platform_specific={
            "agent_run_target": {
                "agent_backend": "codex",
                "agent_variant": "reviewer",
                "model": "gpt-5.5",
                "reasoning_effort": "xhigh",
            }
        },
    )

    assert controller.get_codex_overrides(ctx) == ("reviewer", "gpt-5.5", "xhigh")
    assert controller.get_opencode_overrides(ctx) == ("reviewer", "gpt-5.5", "xhigh")


def test_avibe_run_target_overrides_ignore_backend_and_default_variants() -> None:
    controller = _StubController()
    controller.primary_platform = "slack"
    controller.get_settings_manager_for_context = lambda context: SimpleNamespace(
        get_channel_routing=lambda settings_key: (_ for _ in ()).throw(AssertionError("routing should not be read"))
    )

    codex_ctx = MessageContext(
        user_id="user-1",
        channel_id="ses-1",
        platform="avibe",
        platform_specific={
            "agent_run_target": {
                "agent_backend": "codex",
                "agent_variant": "codex",
                "model": "gpt-5.5",
                "reasoning_effort": "xhigh",
            }
        },
    )
    default_ctx = MessageContext(
        user_id="user-1",
        channel_id="ses-2",
        platform="avibe",
        platform_specific={
            "agent_run_target": {
                "agent_backend": "opencode",
                "agent_variant": "default",
                "model": "gpt-5.5",
                "reasoning_effort": "xhigh",
            }
        },
    )
    agent_name_ctx = MessageContext(
        user_id="user-1",
        channel_id="ses-3",
        platform="avibe",
        platform_specific={
            "agent_run_target": {
                "agent_name": "contract-bot",
                "agent_backend": "codex",
                "agent_variant": "contract-bot",
                "model": "gpt-5.5",
                "reasoning_effort": "xhigh",
            }
        },
    )

    assert controller.get_codex_overrides(codex_ctx) == (None, "gpt-5.5", "xhigh")
    assert controller.get_opencode_overrides(default_ctx) == (None, "gpt-5.5", "xhigh")
    assert controller.get_codex_overrides(agent_name_ctx) == (None, "gpt-5.5", "xhigh")
