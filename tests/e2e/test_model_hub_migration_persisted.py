"""Persisted sources under a populated runtime environment, over HTTP/IPC."""

from __future__ import annotations

import json

import pytest
import yaml

from config.v2_config import V2Config
from tests.e2e.test_model_hub_migration import _write
from tests.e2e.test_model_hub_mock_upstream import _json_request
from tests.e2e.test_model_hub_sources import _configure_protocol

pytestmark = pytest.mark.e2e_model_hub

KEY = "fixture-persisted-migration-static-key"
RUNTIME_ENV = {
    "ANTHROPIC_API_KEY": "fixture-runtime-anthropic",
    "ANTHROPIC_AUTH_TOKEN": "fixture-runtime-bearer",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
    "OPENAI_API_KEY": "fixture-runtime-openai",
    "OPENAI_BASE_URL": "http://127.0.0.1:9",
}


def _seed_hub(app) -> None:
    app._initialize_config()
    path = app.avibe_home / "config/config.json"
    payload = json.loads(path.read_text())
    config = V2Config.default()
    config.model_hub.enabled = True
    for backend in ("claude", "codex", "opencode"):
        config.model_hub.agents[backend].mode = "hub"
    payload["model_hub"] = config.model_hub.to_payload()
    _write(path, json.dumps(payload))


@pytest.mark.parametrize("shape", [
    "claude-shell", "claude-bearer", "codex-shell", "opencode-file",
    "claude-shell-padded", "codex-shell-padded",
])
def test_f4_persisted_configuration_migrates_with_runtime_auth_present(
    model_hub_app_factory, mock_llm_upstream, shape,
):
    """F4: inherited values neither supply nor block a persisted credential."""
    backend = shape.split("-")[0]
    shell_fixture = "-shell" in shape
    protocol = "anthropic" if backend == "claude" else "openai_responses"
    _configure_protocol(mock_llm_upstream, protocol, models=[{"id": "mock-model"}])
    mock_llm_upstream.configure(
        required_api_key=KEY,
        required_auth_scheme="bearer" if backend == "claude" else "protocol",
    )
    source_paths = []
    retained = "# 保留终端偏好\nexport EDITOR='vim'\n"

    def seed(app):
        _seed_hub(app)
        if shell_fixture:
            prefix = "ANTHROPIC" if backend == "claude" else "OPENAI"
            credential_name = "ANTHROPIC_AUTH_TOKEN" if backend == "claude" else "OPENAI_API_KEY"
            path = app.home / ".bashrc"
            saved_key = f" \t{KEY} \t" if shape.endswith("-padded") else KEY
            _write(path, retained + (
                f"export {credential_name}='{saved_key}'\n"
                f"export {prefix}_BASE_URL='{mock_llm_upstream.url}'\n"
            ))
            path.chmod(0o640)
        elif shape == "claude-bearer":
            path = app.home / ".claude/settings.json"
            _write(path, json.dumps({
                "env": {"ANTHROPIC_AUTH_TOKEN": KEY, "ANTHROPIC_BASE_URL": mock_llm_upstream.url},
                "permissions": {"allow": ["Read"]},
            }))
        else:
            path = app.home / ".config/opencode/opencode.json"
            _write(path, json.dumps({
                "provider": {"openai": {"options": {"apiKey": KEY, "baseURL": mock_llm_upstream.url}}},
                "theme": "system",
            }))
        source_paths.append(path)

    with model_hub_app_factory(extra_env=RUNTIME_ENV, before_start=seed) as app:
        before_env = dict(app.env)
        response = app.client.post("/api/models/migration/scan", {})
        assert response.status == 200, response.json()
        rows = response.json()["scan"]["items"]
        assert len(rows) == 1, rows
        assert rows[0]["backend"] == backend
        assert rows[0]["selected"] is True
        assert rows[0]["proposed_action"] == "import"
        assert rows[0]["source_paths"] == [str(source_paths[0])]
        assert KEY not in json.dumps(rows)
        applied = app.client.post("/api/models/migration/apply", {"item_ids": [rows[0]["id"]]})
        assert applied.status == 200, applied.json()
        assert applied.json()["applied"] == 1
        [source] = applied.json()["sources"]
        assert source["supply_channel"] == "hub"
        assert app.env == before_env
        if shell_fixture:
            assert source_paths[0].read_bytes() == retained.encode()
            assert source_paths[0].stat().st_mode & 0o777 == 0o640
        elif shape == "claude-bearer":
            assert json.loads(source_paths[0].read_text()) == {"env": {}, "permissions": {"allow": ["Read"]}}
        else:
            assert json.loads(source_paths[0].read_text())["theme"] == "system"
        assert app.client.post("/api/models/migration/scan", {}).json()["scan"]["items"] == []

        if backend == "claude":
            # Test the pinned engine consumer, not just direct discovery proof.
            status = app.client.get("/api/models/runtime/status").json()["runtime"]["status"]
            listening = status["listening"]
            [config_path] = list((app.avibe_home / "runtime/model-hub/state/instances").glob("*/config.yaml"))
            engine_config = yaml.safe_load(config_path.read_text())
            [credential] = engine_config["claude-api-key"]
            mock_llm_upstream.reset_requests()
            result, _, body = _json_request(
                f"http://{listening['host']}:{listening['port']}", "/v1/messages",
                method="POST",
                headers={"X-Api-Key": engine_config["api-keys"][0]},
                body={
                    "model": f"{credential['prefix']}/mock-model",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "fixture only"}],
                },
                timeout=20,
            )
            assert result == 200, body
            [request] = [r for r in mock_llm_upstream.requests() if r["path"] == "/v1/messages"]
            assert request["headers"]["authorization"] == f"Bearer {KEY}"
            assert "x-api-key" not in request["headers"]


@pytest.mark.parametrize("required", [None, "fixture-rejected-key"], ids=["public", "wrong-key"])
def test_f4_unproven_shell_key_is_not_removed(
    model_hub_app_factory, mock_llm_upstream, required,
):
    """F4: a readable static file is not proof of credential authentication."""
    _configure_protocol(mock_llm_upstream, "anthropic")
    mock_llm_upstream.configure(required_api_key=required, required_auth_scheme="bearer")
    paths = []

    def seed(app):
        _seed_hub(app)
        path = app.home / ".zshrc"
        _write(path, (
            f"export ANTHROPIC_AUTH_TOKEN='{KEY}'\n"
            f"export ANTHROPIC_BASE_URL='{mock_llm_upstream.url}'\n"
        ))
        paths.append(path)

    with model_hub_app_factory(extra_env=RUNTIME_ENV, before_start=seed) as app:
        before = paths[0].read_bytes()
        rows = app.client.post("/api/models/migration/scan", {}).json()["scan"]["items"]
        assert len(rows) == 1 and rows[0]["proposed_action"] == "import"
        result = app.client.post("/api/models/migration/apply", {"item_ids": [rows[0]["id"]]})
        assert result.status == 409, result.json()
        assert paths[0].read_bytes() == before
        assert app.client.get("/api/models/sources").json()["sources"] == []


@pytest.mark.parametrize("shape", ["settings", "shell"])
def test_f4_custom_api_key_transport_is_refused_without_cleanup(
    model_hub_app_factory, mock_llm_upstream, shape,
):
    """F4: discovery cannot prove a header the pinned engine will not send."""
    _configure_protocol(mock_llm_upstream, "anthropic")
    mock_llm_upstream.configure(required_api_key=KEY, required_auth_scheme="protocol")
    paths = []

    def seed(app):
        _seed_hub(app)
        if shape == "shell":
            path = app.home / ".zshrc"
            _write(path, (
                f"export ANTHROPIC_API_KEY='{KEY}'\n"
                f"export ANTHROPIC_BASE_URL='{mock_llm_upstream.url}'\n"
            ))
        else:
            path = app.home / ".claude/settings.json"
            _write(path, json.dumps({
                "env": {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": mock_llm_upstream.url},
            }))
        paths.append(path)

    with model_hub_app_factory(extra_env=RUNTIME_ENV, before_start=seed) as app:
        before = paths[0].read_bytes()
        response = app.client.post("/api/models/migration/scan", {})
        assert response.status == 200, response.json()
        [row] = response.json()["scan"]["items"]
        assert row["selected"] is False
        assert row["notes_key"] == "settings.models.migration.blocked.transport"
        refused = app.client.post("/api/models/migration/apply", {"item_ids": [row["id"]]})
        assert refused.status == 409, refused.json()
        assert paths[0].read_bytes() == before
        assert app.client.get("/api/models/sources").json()["sources"] == []
        assert mock_llm_upstream.requests() == []
