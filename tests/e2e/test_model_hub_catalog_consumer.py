"""Exercise projected metadata in the actual CLI, without a managed engine."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from config.v2_config import ModelHubBackendModelConfig
from modules.agents.codex.transport import CodexTransport
from modules.agents.model_hub import (
    ModelHubLaunch,
    build_claude_hub_env,
    build_codex_hub_launch,
    claude_setting_sources_for_launch,
    claude_settings_for_launch,
)
from tests.e2e.drivers.model_hub_app import ModelHubTestApp
from tests.e2e.drivers.mock_llm_upstream import MockLLMUpstream
from vibe.backend_model_catalog import _codex_hub_catalog_bytes


pytestmark = pytest.mark.e2e_model_hub

# A complete one-pixel PNG, supplied directly to the turn rather than downloaded.
IMAGE_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def rejected_external_proxy():
    # Reserve a loopback port without listening: external HTTP(S) attempts fail
    # at this test-owned transport boundary instead of reaching a real service.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{reserved.getsockname()[1]}"


def _isolated_runtime(tmp_path, rejected_external_proxy):
    runtime = ModelHubTestApp(Path(__file__).resolve().parents[2], tmp_path / "runtime")
    runtime.home.mkdir(parents=True)
    Path(runtime.env["TMPDIR"]).mkdir()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        runtime.env[key] = rejected_external_proxy
    return runtime


def _loopback_cli(binary, runtime):
    sandbox = shutil.which("sandbox-exec")
    if sandbox is None:
        return str(binary)
    # On macOS also enforce loopback-only egress below the CLI's HTTP stack,
    # including background clients that might not honor standard proxy settings.
    profile = '(version 1) (allow default) (deny network-outbound) (allow network-outbound (remote ip "localhost:*"))'
    wrapper = runtime.home / "loopback-cli"
    wrapper.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sandbox)} -p {shlex.quote(profile)} {shlex.quote(str(binary))} \"$@\"\n",
    )
    wrapper.chmod(0o700)
    return str(wrapper)


@pytest.fixture
def codex_catalog_runtime(tmp_path, rejected_external_proxy):
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("Codex executable is unavailable")
    runtime = _isolated_runtime(tmp_path, rejected_external_proxy)
    binary = _loopback_cli(binary, runtime)
    Path(runtime.env["CODEX_HOME"]).mkdir()
    node = shutil.which("node")
    if node:
        runtime.env["PATH"] += os.pathsep + str(Path(node).parent)
    # This fixture uses the harness's allowlisted environment, never the live
    # credential stores. The native binary can only discover test-owned config.
    exported = subprocess.run(
        [binary, "debug", "models", "--bundled", "-c", "model_catalog_json=null"],
        cwd=runtime.home,
        env=runtime.env,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return binary, runtime, exported.stdout


async def _codex_turn(binary, runtime, gateway, catalog, *, effort, native_config="", turns=1):
    codex_home = Path(runtime.env["CODEX_HOME"])
    config_path = codex_home / "config.toml"
    config_path.write_text('cli_auth_credentials_store = "file"\n' + native_config)
    catalog_path = runtime.home / "models.json"
    catalog_path.write_bytes(catalog)
    model_id = json.loads(catalog)["models"][0]["slug"]
    launch = ModelHubLaunch(
        backend="codex",
        channel="hub",
        requested_model=model_id,
        target_model="unrelated-upstream-alias",
        runtime_model=model_id,
        gateway_base_url=gateway.url,
        gateway_token="isolated-catalog-fixture",
    )
    args, env = build_codex_hub_launch([], runtime.env, launch, model_catalog_path=catalog_path)
    transport = CodexTransport(
        binary=binary, cwd=str(runtime.home), runtime_args=args, runtime_env=env,
    )
    notifications = []
    completed = asyncio.Event()

    async def notify(method, params):
        notifications.append((method, params))
        if method == "turn/completed":
            completed.set()

    transport.on_notification(notify)
    try:
        await transport.start()
        models = await transport.send_request("model/list", {})
        config = await transport.send_request("config/read", {})
        thread = await transport.send_request(
            "thread/start",
            {"model": model_id, "cwd": str(runtime.home), "approvalPolicy": "never"},
        )
        for _turn in range(turns):
            completed.clear()
            await transport.send_request(
                "turn/start",
                {
                    "threadId": thread["thread"]["id"],
                    "input": [
                        {"type": "text", "text": "Describe this image: 中文"},
                        {"type": "image", "url": IMAGE_URL},
                    ],
                    **({"effort": effort} if effort is not None else {}),
                },
            )
            await asyncio.wait_for(completed.wait(), timeout=30)
    finally:
        await transport.stop()
    assert config_path.read_text().startswith('cli_auth_credentials_store = "file"\n' + native_config)
    bodies = [request["body"] for request in gateway.requests() if request["path"] == "/v1/responses"]
    assert bodies, notifications
    assert [
        params["turn"]["status"] for method, params in notifications if method == "turn/completed"
    ] == ["completed"] * turns
    return bodies, models, notifications, config["config"]


@pytest.mark.parametrize("effort", [None, "none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_codex_unknown_catalog_preserves_supplied_image_and_effort(codex_catalog_runtime, effort):
    """MH-PROTOCOL-004: absence must survive the real catalog/turn consumer."""
    binary, runtime, raw_catalog = codex_catalog_runtime
    model = ModelHubBackendModelConfig.from_payload({"id": "custom-unknown"})
    # Round-trip the released shape so empty lists are covered, not just missing
    # JSON keys. The model's alias deliberately differs from its route target.
    catalog = _codex_hub_catalog_bytes(raw_catalog, [model.to_payload()])
    with MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        bodies, _models, _notifications, _config = asyncio.run(
            _codex_turn(binary, runtime, gateway, catalog, effort=effort),
        )
    body = bodies[0]
    assert body["model"] == model.id
    content = [part for item in body["input"] for part in item.get("content", [])]
    images = [part for part in content if part.get("type") == "input_image"]
    assert [part["image_url"] for part in images] == [IMAGE_URL]
    assert all(part.get("detail") != "original" for part in images)
    assert any("中文" in part.get("text", "") for part in content)
    assert (body.get("reasoning") or {}).get("effort") == effort
    assert not body.get("service_tier")


@pytest.mark.parametrize(
    "metadata,effort,expected_effort,image_expected",
    [
        ({"input_modalities": ["text"], "supports_reasoning": False}, None, "none", False),
        ({"input_modalities": ["text"], "supports_reasoning": False, "reasoning_efforts": ["high"]}, None, "none", False),
        ({"input_modalities": ["text", "image"], "reasoning_efforts": ["low", "high"]}, None, "low", True),
        ({"input_modalities": ["text", "image"], "reasoning_efforts": ["low", "high"]}, "high", "high", True),
        ({"supports_reasoning": True}, "high", "high", True),
    ],
)
def test_codex_explicit_catalog_choices_reach_the_consumer(
    codex_catalog_runtime, metadata, effort, expected_effort, image_expected,
):
    """MH-PROTOCOL-004: explicit negatives and effort lists remain meaningful."""
    binary, runtime, raw_catalog = codex_catalog_runtime
    model = ModelHubBackendModelConfig.from_payload({"id": "explicit-custom", **metadata})
    catalog = _codex_hub_catalog_bytes(raw_catalog, [model.to_payload()])
    with MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        bodies, _models, _notifications, _config = asyncio.run(
            _codex_turn(binary, runtime, gateway, catalog, effort=effort),
        )
    content = [part for item in bodies[0]["input"] for part in item.get("content", [])]
    images = [part for part in content if part.get("type") == "input_image"]
    assert [part["image_url"] for part in images] == ([IMAGE_URL] if image_expected else [])
    assert bodies[0]["reasoning"]["effort"] == expected_effort


@pytest.mark.parametrize("explicit_context", [None, 64_000, 512_000])
@pytest.mark.parametrize("model_kind", ["custom", "native"])
def test_codex_native_context_override_wins_over_catalog_planning(codex_catalog_runtime, explicit_context, model_kind):
    """MH-PROTOCOL-004: the actual context consumer retains native overrides."""
    binary, runtime, raw_catalog = codex_catalog_runtime
    model_id = "context-alias" if model_kind == "custom" else json.loads(raw_catalog)["models"][0]["slug"]
    model = ModelHubBackendModelConfig(id=model_id, context_window=128_000, max_output_tokens=32_000)
    catalog = _codex_hub_catalog_bytes(raw_catalog, [model.to_payload()])
    native_config = 'model = "unrelated-native-default"\n'
    if explicit_context is not None:
        native_config += f"model_context_window = {explicit_context}\nmodel_auto_compact_token_limit = 300000\n"
    with MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        bodies, _models, notifications, config = asyncio.run(
            _codex_turn(binary, runtime, gateway, catalog, effort=None, native_config=native_config),
        )
    usage = [params["tokenUsage"] for method, params in notifications if method == "thread/tokenUsage/updated"]
    percent = json.loads(catalog)["models"][0].get("effective_context_window_percent", 95)
    assert usage, [method for method, _params in notifications]
    if explicit_context is not None:
        assert config["model_context_window"] == explicit_context
        assert config["model_auto_compact_token_limit"] == 300_000
    assert usage[-1]["modelContextWindow"] == (explicit_context or model.context_window) * percent // 100
    assert bodies[0]["model"] == model.id
    # BackendModel output metadata has no Codex output-cap projection.
    assert "max_output_tokens" not in bodies[0]


@pytest.mark.parametrize(
    "native_config,input_tokens,compacts",
    [
        ("", 150_000, True),
        ("model_context_window = 512000\nmodel_auto_compact_token_limit = 300000\n", 150_000, False),
        ("model_context_window = 512000\nmodel_auto_compact_token_limit = 300000\n", 350_000, True),
    ],
)
def test_codex_auto_compaction_consumes_resolved_limits(
    codex_catalog_runtime, monkeypatch, native_config, input_tokens, compacts,
):
    """MH-PROTOCOL-004: compaction follows the active window and explicit threshold."""
    from tests.e2e.drivers import mock_llm_upstream

    binary, runtime, raw_catalog = codex_catalog_runtime
    model = ModelHubBackendModelConfig(id="compact-alias", context_window=128_000)
    catalog = _codex_hub_catalog_bytes(raw_catalog, [model.to_payload()])
    original_frames = mock_llm_upstream._responses_stream_frames
    calls = 0

    def frames_with_large_first_usage(model_id):
        nonlocal calls
        frames = original_frames(model_id)
        calls += 1
        if calls == 1:
            completed = json.loads(frames[-1].split(b"data: ", 1)[1])
            completed["response"]["usage"]["input_tokens"] = input_tokens
            completed["response"]["usage"]["total_tokens"] = input_tokens + 5
            frames[-1] = mock_llm_upstream._sse("response.completed", completed)
        return frames

    monkeypatch.setattr(mock_llm_upstream, "_responses_stream_frames", frames_with_large_first_usage)
    with MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        bodies, _models, notifications, _config = asyncio.run(
            _codex_turn(binary, runtime, gateway, catalog, effort=None, native_config=native_config, turns=2),
        )
    compacted = [
        params for method, params in notifications
        if method == "item/completed" and params.get("item", {}).get("type") == "contextCompaction"
    ]
    assert bool(compacted) is compacts
    assert len(bodies) == (3 if compacts else 2)


@pytest.mark.parametrize("channel", ["hub", "native_cli"])
@pytest.mark.parametrize("settings_scope", ["parent", "project", "local", "catalog"])
def test_claude_consumer_preserves_explicit_limit_settings(tmp_path, rejected_external_proxy, channel, settings_scope):
    """MH-CLAUDE-LAUNCH-001: limits remain separate from transport ownership."""
    import claude_agent_sdk

    binary = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if not binary.is_file():
        pytest.skip("The installed Claude SDK has no bundled CLI")
    runtime = _isolated_runtime(tmp_path, rejected_external_proxy)
    binary = _loopback_cli(binary, runtime)
    project = runtime.home / "project"
    project.mkdir(parents=True)
    selected_limits = {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "234567",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1234",
    }
    fixture_settings = None
    if settings_scope in {"project", "local"}:
        filename = "settings.local.json" if settings_scope == "local" else "settings.json"
        fixture_settings = project / ".claude" / filename
        fixture_settings.parent.mkdir()
        fixture_settings.write_text(json.dumps({"env": selected_limits}))
    elif settings_scope == "parent":
        runtime.env.update(selected_limits)
    runtime.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    runtime.env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
    with MockLLMUpstream() as gateway:
        gateway.configure(protocol="anthropic")
        launch = ModelHubLaunch(
            backend="claude",
            channel=channel,
            requested_model="planning-alias",
            target_model="custom-limit-model",
            runtime_model="custom-limit-model",
            gateway_base_url=gateway.url,
            gateway_token="isolated-claude-fixture",
            context_window=128_000,
            max_output_tokens=4096,
        )
        # Native CLI authority is tested with synthetic local credentials, never
        # OAuth/keychain discovery. All filesystem paths belong to the fixture.
        runtime.env["ANTHROPIC_BASE_URL"] = gateway.url
        runtime.env["ANTHROPIC_AUTH_TOKEN"] = "isolated-native-fixture"
        env = build_claude_hub_env(runtime.env, launch)
        result = subprocess.run(
            [
                str(binary), "-p", "Reply with hello 中文.", "--output-format", "json",
                "--model", launch.runtime_model, "--tools", "", "--max-turns", "1",
                "--setting-sources", ",".join(claude_setting_sources_for_launch(launch)),
                "--settings", claude_settings_for_launch("{}", launch),
            ],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (result.stdout[-2000:], result.stderr[-2000:])
        bodies = [request["body"] for request in gateway.requests() if request["path"] == "/v1/messages"]
    assert bodies
    expected_output = 4096 if settings_scope == "catalog" else 1234
    assert bodies[-1]["model"] == launch.runtime_model
    assert bodies[-1]["max_tokens"] == expected_output
    # Normal Claude compaction currently ignores MAX_CONTEXT_TOKENS; do not
    # certify its modelUsage display as a consumer of this environment key.
    # SessionHandler coverage separately checks delivery of that planning value.
    if fixture_settings is not None:
        assert json.loads(fixture_settings.read_text()) == {"env": selected_limits}
