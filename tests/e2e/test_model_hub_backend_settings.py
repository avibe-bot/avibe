"""Real backend launches keep Model Hub transport ownership."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.v2_config import ModelHubBackendModelConfig, ModelHubMenuConfig
from core.handlers.model_hub.identifiers import OPENCODE_PROVIDER_BY_NATIVE_PROTOCOL
from modules.agents.model_hub import ModelHubRuntimeRouter
from modules.agents.opencode.caller_context import PLUGIN_FILENAME, PLUGIN_SOURCE
from tests.e2e.drivers.model_hub_app import ModelHubTestApp
from tests.e2e.drivers.mock_llm_upstream import MockLLMUpstream
from tests.e2e.test_model_hub_routing import _request_credential
from tests.e2e.test_model_hub_runtime import _install_engine, _local_engine_manifest
from tests.e2e.test_model_hub_sources import SYNTHETIC_API_KEY, _configure_protocol, _create_source
from tests.scenario_harness.model_hub import (
    MemoryModelHubStore,
    ModelHubScenarioAdapter,
    config_with_sources,
    fixed_model,
    service_for,
    source,
)
from vibe.opencode_config import managed_opencode_runtime_config_content


pytestmark = pytest.mark.e2e_model_hub


@pytest.mark.parametrize("provider_id", ["native-relay", "avibe_model_hub"])
def test_codex_native_provider_settings_cannot_change_hub_egress(
    model_hub_app_factory, mock_llm_upstream, provider_id,
) -> None:
    """MH-CODEX-LAUNCH-001: native auth cannot change a Hub turn's egress."""
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("Codex executable is unavailable")
    menu_model = fixed_model("codex")
    routed_model = "upstream-route-model"
    native_key = "sk-native-provider-fixture"

    def prepare_runtime(app):
        node = shutil.which("node")
        if node:
            app.env["PATH"] += os.pathsep + str(Path(node).parent)

    with MockLLMUpstream() as native, model_hub_app_factory(
        extra_env={"VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH": _local_engine_manifest()},
        before_start=prepare_runtime,
    ) as app:
        _configure_protocol(native, "openai_responses", models=[{"id": menu_model}])
        native_settings = app.home / ".codex" / "config.toml"
        native_settings.parent.mkdir(parents=True, exist_ok=True)
        native_payload = (
            'cli_auth_credentials_store = "file"\n'
            f'model_provider = "{provider_id}"\n'
            f'[model_providers.{provider_id}]\n'
            'name = "Native relay"\n'
            f'base_url = "{native.url}/v1"\n'
            'wire_api = "responses"\n'
            f'experimental_bearer_token = "{native_key}"\n'
            f'http_headers = {{ "Authorization" = "Bearer {native_key}", "x-api-key" = "{native_key}" }}\n'
        )
        native_settings.write_text(native_payload)
        (native_settings.parent / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": native_key}))
        configured = app.client.post("/api/config", {
            "agents": {"codex": {"enabled": True, "cli_path": binary}},
        })
        assert configured.status == 200, configured.json()
        _install_engine(app)
        _configure_protocol(mock_llm_upstream, "openai_responses", models=[{"id": routed_model}])
        supplied, _ = _create_source(
            app, mock_llm_upstream, protocol="openai_responses",
            extra={"base_url": mock_llm_upstream.url + "/v1"},
        )
        mode = app.client.patch("/api/models/agents/codex/mode", {"mode": "hub"})
        assert mode.status == 200, mode.json()
        chain = app.client.put(f"/api/models/agents/codex/chain?model={menu_model}", {
            "hops": [{"source_id": supplied["id"], "model_id": routed_model}],
        })
        assert chain.status == 200, chain.json()
        agent = app.client.post("/api/agents", {
            "name": "codex-hub-settings-e2e", "backend": "codex", "model": menu_model,
        })
        assert agent.status == 200, agent.json()
        project = app.client.post("/api/projects", {
            "folder_path": str(app.home), "display_name": "Codex Hub settings E2E",
        })
        assert project.status == 201, project.json()
        session = app.client.post("/api/sessions", {
            "project_id": project.json()["id"], "agent_name": "codex-hub-settings-e2e", "model": menu_model,
        })
        assert session.status == 201, session.json()
        session_id = session.json()["id"]
        mock_llm_upstream.reset_requests()
        native.reset_requests()
        sent = app.client.post(f"/api/sessions/{session_id}/messages", {"text": "hello"})
        assert sent.status == 202, sent.json()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            observed = mock_llm_upstream.requests() + native.requests()
            if any(item["path"] == "/v1/responses" for item in observed):
                break
            time.sleep(0.1)
        assert not [item for item in native.requests() if item["path"] == "/v1/responses"]
        captured = [item for item in mock_llm_upstream.requests() if item["path"] == "/v1/responses"]
        assert captured, app.diagnostics()
        assert all(item["body"]["model"] == routed_model for item in captured)
        assert all(_request_credential(item) == SYNTHETIC_API_KEY for item in captured)
        # Codex may append project trust, but must not rewrite native auth.
        assert native_settings.read_text().startswith(native_payload)


@pytest.mark.parametrize("options_scope", ["provider", "model", "model_headers"])
@pytest.mark.parametrize("native_protocol", ["openai_responses", "anthropic"])
def test_opencode_native_settings_cannot_change_hub_transport(tmp_path, options_scope, native_protocol):
    """MH-OPENCODE-LAUNCH-001: native config cannot change Hub transport."""
    binary = shutil.which("opencode")
    if binary is None:
        pytest.skip("OpenCode executable is unavailable")
    model = "hub-settings-model"
    provider_id = OPENCODE_PROVIDER_BY_NATIVE_PROTOCOL[native_protocol]
    request_path = "/v1/messages" if native_protocol == "anthropic" else "/v1/responses"
    provider_npm = "@ai-sdk/anthropic" if native_protocol == "anthropic" else "@ai-sdk/openai"
    token = "hub-transport-fixture-token"
    native_key = "native-opencode-fixture-token"
    runtime = ModelHubTestApp(Path(__file__).resolve().parents[2], tmp_path / "runtime")
    runtime.home.mkdir(parents=True)
    Path(runtime.env["TMPDIR"]).mkdir(parents=True)
    with MockLLMUpstream() as gateway, MockLLMUpstream() as native:
        _configure_protocol(gateway, native_protocol, models=[{"id": model}])
        _configure_protocol(native, native_protocol, models=[{"id": model}])
        config = config_with_sources(
            [source("src_settings01", [model], vendor="openai", protocol="openai_responses")],
            backend="opencode", menu_model=model,
        )
        config.agents["opencode"].models = [ModelHubBackendModelConfig(
            id=model, origin="manual", native_protocol=native_protocol,
            context_window=32000, max_output_tokens=4096,
        )]
        config.agents["opencode"].menu = ModelHubMenuConfig(checked=[model])
        service = service_for(tmp_path, MemoryModelHubStore(config), ModelHubScenarioAdapter())
        router = ModelHubRuntimeRouter(
            service=service,
            turn_gateway=SimpleNamespace(endpoint=AsyncMock(return_value=(gateway.url, token))),
            overlay_path=tmp_path / "overlay.json",
        )
        overlay = asyncio.run(router.prepare_opencode_overlay())
        assert overlay is not None
        stale_options = {
            "baseURL": native.url + "/v1", "apiKey": native_key,
            "headers": {"Authorization": f"Bearer {native_key}", "x-api-key": native_key},
        }
        provider = {"npm": provider_npm, "models": {model: {}}}
        if options_scope == "provider":
            provider["options"] = stale_options
        elif options_scope == "model_headers":
            provider["models"][model]["headers"] = stale_options["headers"]
        else:
            provider["models"][model]["options"] = stale_options
        native_settings = runtime.home / "opencode.json"
        native_payload = json.dumps({"$schema": "https://opencode.ai/config.json", "provider": {provider_id: provider}})
        native_settings.write_text(native_payload)
        env = {
            **runtime.env,
            "OPENCODE_CONFIG": str(overlay.path),
            "OPENCODE_CONFIG_CONTENT": managed_opencode_runtime_config_content(overlay.content),
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "AVIBE_OPENCODE_MODEL_HUB": "1",
        }
        plugin = runtime.home / ".config" / "opencode" / "plugins" / PLUGIN_FILENAME
        plugin.parent.mkdir(parents=True)
        plugin.write_text(PLUGIN_SOURCE)
        process = subprocess.Popen(
            [binary, "run", "--format", "json", "--model", f"{provider_id}/{model}", "hello"],
            cwd=runtime.home, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=40)
        finally:
            ModelHubTestApp._stop_process(process)
        assert not [item for item in native.requests() if item["path"] == request_path]
        captured = [item for item in gateway.requests() if item["path"] == request_path]
        assert captured, (stdout.decode(), stderr.decode())
        assert all(_request_credential(item) == token for item in captured), [item["headers"] for item in captured]
        assert all(item["body"]["model"] == model for item in captured)
        assert native_settings.read_text() == native_payload
