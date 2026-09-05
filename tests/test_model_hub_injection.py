from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11
    import tomli as tomllib  # type: ignore[no-redef]

from core.handlers.model_hub.service import (
    PRE_ATTEMPT_SETTLEMENT_GENERATION,
    ModelHubError,
)
from modules.agents.model_hub import (
    ModelHubLaunch,
    build_claude_hub_env,
    build_codex_hub_launch,
    bind_launch,
    bind_persisted_launch,
    claude_setting_sources_for_launch,
    claude_settings_for_launch,
    launch_for_context,
    opencode_model_for_overlay,
    opencode_requested_model_for_overlay,
    persisted_launch_identity,
)
from modules.agents.opencode.agent import resolve_opencode_model_dict


def hub_launch(**overrides) -> ModelHubLaunch:
    values = {
        "backend": "claude",
        "channel": "hub",
        "requested_model": "menu-model",
        "target_model": "concrete-upstream-id",
        "runtime_model": "concrete-upstream-id",
        "source_id": "src_inject01",
        "gateway_base_url": "http://127.0.0.1:15220",
        "gateway_token": "local-test-token",
    }
    values.update(overrides)
    return ModelHubLaunch(**values)


def test_persisted_launch_identity_keeps_exact_route_target_without_mapping_flag():
    launch = hub_launch(settlement_generation=17)
    assert persisted_launch_identity(launch) == {
        "backend": "claude",
        "channel": "hub",
        "source_id": "src_inject01",
        "target_model": "concrete-upstream-id",
    }
    assert "via_mapping" not in persisted_launch_identity(launch)


def test_context_binding_restores_the_pre_attempt_settlement_generation():
    context = SimpleNamespace()
    launch = hub_launch(settlement_generation=17)
    bind_launch(context, launch)
    assert launch_for_context(context) == launch
    restored = bind_persisted_launch(context, persisted_launch_identity(launch))
    assert restored is not None
    assert restored.target_model == "concrete-upstream-id"
    assert restored.requested_model == "concrete-upstream-id"
    # The minting runtime's generation indexes a ledger this runtime no longer
    # has, so the restored attempt settles as older than anything this runtime
    # can start rather than inheriting a number it cannot compare.
    assert restored.settlement_generation == PRE_ATTEMPT_SETTLEMENT_GENERATION
    assert restored.settlement_generation < 1


@pytest.mark.parametrize(
    "released_payload",
    [
        pytest.param(
            {
                "backend": "opencode",
                "channel": "native_cli",
                "source_id": "src_released01",
                "target_model": "released-model",
            },
            id="released_shape",
        ),
        pytest.param(
            {
                "backend": "opencode",
                "channel": "native_cli",
                "source_id": "src_released01",
                "target_model": "released-model",
                "settlement_generation": 4096,
                "unknown_future_key": "ignored",
            },
            id="shape_carrying_a_foreign_generation",
        ),
    ],
)
def test_persisted_launch_identity_loads_released_shapes_without_certifying_freshness(
    released_payload,
):
    # Persisted-shape discipline: an on-disk identity from any release loads
    # cleanly, and no value inside it can make a restored attempt look newer
    # than the attempts this runtime started.
    context = SimpleNamespace()
    restored = bind_persisted_launch(context, json.loads(json.dumps(released_payload)))

    assert restored is not None
    assert restored.source_id == "src_released01"
    assert restored.settlement_generation == PRE_ATTEMPT_SETTLEMENT_GENERATION
    assert launch_for_context(context) == restored


def test_direct_launch_does_not_inject_provider_credentials():
    launch = hub_launch(channel="direct", source_id=None, gateway_base_url=None, gateway_token=None)
    base_env = {"ANTHROPIC_AUTH_TOKEN": "user-token", "PATH": "/bin"}
    assert build_claude_hub_env(base_env, launch) == base_env
    assert build_codex_hub_launch(["--flag"], {"OPENAI_API_KEY": "user-key"}, launch) == (["--flag"], None)
    assert claude_setting_sources_for_launch(launch) == ["user", "project", "local"]


def test_hub_launch_masks_inherited_claude_auth_and_injects_gateway():
    launch = hub_launch(context_window=128_000, max_output_tokens=32_000)
    env = build_claude_hub_env(
        {
            "ANTHROPIC_AUTH_TOKEN": "user-token",
            "ANTHROPIC_BASE_URL": "https://user.example",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "999999",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "999999",
            "PATH": "/bin",
        },
        launch,
    )
    assert env["ANTHROPIC_BASE_URL"] == launch.gateway_base_url
    assert env["ANTHROPIC_AUTH_TOKEN"] == launch.gateway_token
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "128000"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32000"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert claude_setting_sources_for_launch(launch) == ["project", "local"]


def test_claude_hub_settings_own_connection_after_native_and_sdk_env_merges():
    launch = hub_launch()
    inherited = {
        "ANTHROPIC_API_KEY": "parent-key",
        "ANTHROPIC_AUTH_TOKEN": "parent-token",
        "ANTHROPIC_BASE_URL": "https://parent.example",
        "ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer other-key",
        "CLAUDE_CODE_OAUTH_TOKEN": "parent-oauth",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR": "3",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR": "4",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "PATH": "/bin",
    }
    settings = json.loads(claude_settings_for_launch('{"autoMemoryEnabled":false}', launch))
    subprocess_env = {**inherited, **build_claude_hub_env(inherited, launch)}
    effective_env = {**subprocess_env, **inherited, **settings["env"]}

    assert effective_env["ANTHROPIC_BASE_URL"] == launch.gateway_base_url
    assert effective_env["ANTHROPIC_AUTH_TOKEN"] == launch.gateway_token
    for key in inherited.keys() - {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "PATH"}:
        assert effective_env[key] == ""
    assert effective_env["PATH"] == "/bin"
    assert settings["autoMemoryEnabled"] is False
    assert settings["apiKeyHelper"] == ""


@pytest.mark.parametrize("channel", [None, "direct", "native_cli"])
def test_claude_native_settings_are_unchanged_without_a_hub_launch(channel):
    settings = '{"autoMemoryEnabled":false}'
    launch = hub_launch(channel=channel) if channel is not None else None
    assert claude_settings_for_launch(settings, launch) == settings


@pytest.mark.parametrize("missing", ["gateway_base_url", "gateway_token"])
def test_claude_hub_launch_cannot_fall_back_to_native_auth(missing):
    launch = hub_launch(**{missing: None})
    with pytest.raises(ModelHubError, match="engine_down"):
        build_claude_hub_env({"ANTHROPIC_AUTH_TOKEN": "native-token"}, launch)
    with pytest.raises(ModelHubError, match="engine_down"):
        claude_settings_for_launch("{}", launch)


def test_native_cli_launch_keeps_auth_and_applies_catalog_limits():
    launch = hub_launch(
        channel="native_cli",
        gateway_base_url=None,
        gateway_token=None,
        context_window=128_000,
        max_output_tokens=32_000,
    )
    base_env = {
        "ANTHROPIC_AUTH_TOKEN": "user-token",
        "ANTHROPIC_BASE_URL": "https://user.example",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "999999",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "999999",
        "PATH": "/bin",
    }

    env = build_claude_hub_env(base_env, launch)

    assert env["ANTHROPIC_AUTH_TOKEN"] == "user-token"
    assert env["ANTHROPIC_BASE_URL"] == "https://user.example"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "128000"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32000"
    assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in env


def test_codex_hub_launch_uses_responses_wire_api_and_ephemeral_token(tmp_path):
    """MH-PROTOCOL-004: Hub launches consume a prepared standard Responses catalog."""

    launch = hub_launch(backend="codex")
    catalog_path = tmp_path / "codex-hub-models.json"
    args, env = build_codex_hub_launch(
        ["--model", launch.runtime_model],
        {"OPENAI_API_KEY": "user-key"},
        launch,
        model_catalog_path=catalog_path,
    )
    rendered = " ".join(args)
    assert 'wire_api="responses"' in rendered
    assert "model_provider=\"avibe_model_hub\"" in rendered
    assert f'model_catalog_json="{catalog_path}"' in rendered
    assert env == {"AVIBE_MODEL_HUB_TOKEN": "local-test-token"}


def test_codex_hub_launch_requires_provider_safe_catalog():
    launch = hub_launch(backend="codex")
    with pytest.raises(ValueError, match="provider-safe model catalog"):
        build_codex_hub_launch([], {}, launch)


def test_codex_hub_launch_uses_toml_safe_catalog_path(tmp_path):
    launch = hub_launch(backend="codex")
    catalog_path = tmp_path / ("models-\x7f-" + chr(0x1F680) + ".json")

    args, _env = build_codex_hub_launch(
        [],
        {},
        launch,
        model_catalog_path=catalog_path,
    )

    override = next(arg for arg in args if arg.startswith("model_catalog_json="))
    encoded_path = override.split("=", 1)[1]
    assert "\x7f" not in encoded_path
    assert "\\u007f" in encoded_path
    assert chr(0x1F680) in encoded_path
    assert "\\ud83d" not in override
    assert tomllib.loads(f"path = {encoded_path}\n")["path"] == str(catalog_path)


def test_opencode_overlay_addresses_bare_ids_through_their_native_provider():
    overlay = SimpleNamespace(
        model_provider_ids=(
            ("gpt-5", "avibe-openai"),
            ("claude-opus-5", "avibe-anthropic"),
            ("moonshotai/kimi-k2", "avibe-openai"),
        ),
        checked_identifiers=("gpt-5", "claude-opus-5", "moonshotai/kimi-k2"),
        available_identifiers=("gpt-5", "claude-opus-5", "moonshotai/kimi-k2"),
    )
    assert opencode_requested_model_for_overlay("gpt-5", overlay) == "gpt-5"
    assert opencode_model_for_overlay("gpt-5", overlay) == "avibe-openai/gpt-5"
    assert opencode_model_for_overlay("claude-opus-5", overlay) == (
        "avibe-anthropic/claude-opus-5"
    )
    slash_model = opencode_model_for_overlay("moonshotai/kimi-k2", overlay)
    assert slash_model == "avibe-openai/moonshotai/kimi-k2"
    assert resolve_opencode_model_dict(slash_model, default_provider=None) == {
        "providerID": "avibe-openai",
        "modelID": "moonshotai/kimi-k2",
    }
    with pytest.raises(ModelHubError):
        opencode_model_for_overlay("missing", overlay)


def test_opencode_overlay_never_repairs_a_prefixed_identifier():
    overlay = SimpleNamespace(
        model_provider_ids=(("gpt-5", "avibe-openai"),),
        checked_identifiers=("gpt-5",),
        available_identifiers=("gpt-5",),
    )
    with pytest.raises(ModelHubError):
        opencode_model_for_overlay("openai/gpt-5", overlay)


def test_fallback_launch_identity_is_stable_for_same_route():
    first = hub_launch(target_model="model-a", runtime_model="model-a", source_id="src_inject01")
    second = hub_launch(target_model="model-b", runtime_model="model-b", source_id="src_inject02")
    assert first.fingerprint == second.fingerprint
    assert first.source_id != second.source_id


@pytest.mark.parametrize("channel", ["hub", "native_cli"])
def test_claude_launch_fingerprint_covers_process_bound_catalog_metadata(channel):
    common = {
        "channel": channel,
        "gateway_base_url": "http://127.0.0.1:15220" if channel == "hub" else None,
        "gateway_token": "local-test-token" if channel == "hub" else None,
        "source_id": "src_inject01",
    }
    baseline = hub_launch(
        **common,
        context_window=128_000,
        max_output_tokens=32_000,
        reasoning_efforts=("high",),
    )

    for changed in (
        hub_launch(**common, context_window=256_000, max_output_tokens=32_000, reasoning_efforts=("high",)),
        hub_launch(**common, context_window=128_000, max_output_tokens=64_000, reasoning_efforts=("high",)),
        hub_launch(**common, context_window=128_000, max_output_tokens=32_000, reasoning_efforts=()),
    ):
        assert changed.fingerprint != baseline.fingerprint


def test_codex_launch_fingerprint_ignores_metadata_loaded_by_the_app_server():
    baseline = hub_launch(
        backend="codex",
        context_window=128_000,
        max_output_tokens=32_000,
        reasoning_efforts=("high",),
    )
    changed = hub_launch(
        backend="codex",
        context_window=256_000,
        max_output_tokens=64_000,
        reasoning_efforts=(),
    )

    assert changed.fingerprint == baseline.fingerprint
