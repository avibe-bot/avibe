"""Persisted sources under a populated runtime environment, over HTTP/IPC."""

from __future__ import annotations

import json

import pytest
import yaml

from config.v2_config import V2Config
from core.handlers.model_hub.migration_journal import NativeTakeoverJournal
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


def _assert_cpa_bearer_consumption(app, upstream, key: str) -> None:
    """Read only fixture engine output; consume it through the actual CPA."""
    status = app.client.get("/api/models/runtime/status").json()["runtime"]["status"]
    listening = status["listening"]
    [config_path] = list((app.avibe_home / "runtime/model-hub/state/instances").glob("*/config.yaml"))
    engine_config = yaml.safe_load(config_path.read_text())
    [credential] = engine_config["claude-api-key"]
    upstream.reset_requests()
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
    [request] = [r for r in upstream.requests() if r["path"] == "/v1/messages"]
    assert request["headers"]["authorization"] == f"Bearer {key}"
    assert "x-api-key" not in request["headers"]


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
            _assert_cpa_bearer_consumption(app, mock_llm_upstream, KEY)


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


@pytest.mark.parametrize("profile,writer", [
    (".bashrc", "printf -vOPENAI_API_KEY %s fixture-dynamic"),
    (".bashrc", "read -raOPENAI_API_KEY"),
    (".bashrc", "printf '%n' OPENAI_API_KEY"),
    (".bashrc", "read 'OPENAI_API_KEY[0]'"),
    (".bashrc", 'read "-$FLAGS" OPENAI_API_KEY'),
    (".bashrc", 'printf "$OPTIONS" OPENAI_API_KEY %s fixture-dynamic'),
    (".zshrc", "read -p OPENAI_API_KEY"),
    (".bashrc", "wait -npOPENAI_API_KEY 123"),
    (".bashrc", 'wait "$OPTIONS" OPENAI_API_KEY 123'),
    (".bashrc", "compgen -V OPENAI_API_KEY -W fixture"),
    (".bashrc", "trap 'read OPENAI_API_KEY' DEBUG"),
    (".bashrc", "complete -W '$(read OPENAI_API_KEY)' fixture-command"),
    (".bashrc", "jobs -x read OPENAI_API_KEY"),
    (".bashrc", "exec {OPENAI_API_KEY}>/fixture/data"),
    (".bashrc", "coproc OPENAI_API_KEY { :; }"),
    (".bashrc", "eval " * 32 + "read OPENAI_API_KEY"),
    (".zshrc", "print -v OPENAI_API_KEY fixture"),
    (".zshrc", "set -A OPENAI_API_KEY fixture"),
    (".zshrc", "integer OPENAI_API_KEY"),
    (".zshrc", "getln OPENAI_API_KEY"),
    (".zshrc", "shift 1 OPENAI_API_KEY"),
    (".zshrc", "zmodload -FL -P OPENAI_API_KEY zsh/parameter"),
    (".zshrc", "emulate zsh -c 'read OPENAI_API_KEY'"),
    (".zshrc", "emulate sh -c 'read -p OPENAI_API_KEY'"),
    (".zshrc", "emulate ksh -c 'print -v OPENAI_API_KEY fixture'"),
    (".zshrc", "logout 'OPENAI_API_KEY=1'"),
    (".zshrc", "fc -e 'read OPENAI_API_KEY'"),
])
def test_f4_explicit_dynamic_writer_refuses_http_apply_without_proof(
    model_hub_app_factory, mock_llm_upstream, profile, writer,
):
    _configure_protocol(mock_llm_upstream, "openai_responses")
    mock_llm_upstream.configure(required_api_key=KEY)
    paths = []

    def seed(app):
        _seed_hub(app)
        path = app.home / profile
        _write(path, (
            f"export OPENAI_API_KEY='{KEY}'\n"
            f"export OPENAI_BASE_URL='{mock_llm_upstream.url}'\n"
            f"{writer}\n"
        ))
        paths.append(path)

    with model_hub_app_factory(extra_env=RUNTIME_ENV, before_start=seed) as app:
        before = paths[0].read_bytes()
        scan = app.client.post("/api/models/migration/scan", {})
        assert scan.status == 200, scan.json()
        [row] = scan.json()["scan"]["items"]
        assert row["selected"] is False
        assert row["notes_key"] == "settings.models.migration.blocked.dynamic_shell"
        refused = app.client.post("/api/models/migration/apply", {"item_ids": [row["id"]]})
        assert refused.status == 409, refused.json()
        assert paths[0].read_bytes() == before
        assert app.client.get("/api/models/sources").json()["sources"] == []
        assert mock_llm_upstream.requests() == []


@pytest.mark.parametrize("profile,data", [
    (".bashrc", "source 'OPENAI_API_KEY=fixture-script'"),
    (".bashrc", "rg --fixed-strings OPENAI_API_KEY /fixture/input"),
    (".bashrc", "compgen -W OPENAI_API_KEY=fixture-data"),
    (".bashrc", "wait -p unrelated OPENAI_API_KEY"),
    (".bashrc", "eval " * 32 + "echo OPENAI_API_KEY"),
    (".zshrc", "print -R -v OPENAI_API_KEY"),
    (".zshrc", "emulate sh -c 'wait -p OPENAI_API_KEY'"),
    (".zshrc", "emulate sh -c 'mapfile OPENAI_API_KEY'"),
])
def test_f4_shell_data_roles_allow_http_migration_without_changing_data(
    model_hub_app_factory, mock_llm_upstream, profile, data,
):
    """Written argv data is preserved, not executed or promoted into code."""
    _configure_protocol(mock_llm_upstream, "openai_responses", models=[{"id": "mock-model"}])
    mock_llm_upstream.configure(required_api_key=KEY)
    paths = []
    retained = f"# 保留数据与换行\r\n{data}\r\n".encode()

    def seed(app):
        _seed_hub(app)
        path = app.home / profile
        _write(path, (
            f"export OPENAI_API_KEY='{KEY}'\r\n"
            f"export OPENAI_BASE_URL='{mock_llm_upstream.url}'\r\n"
        ))
        path.write_bytes(path.read_bytes() + retained)
        path.chmod(0o640)
        paths.append(path)

    with model_hub_app_factory(extra_env=RUNTIME_ENV, before_start=seed) as app:
        before_env = dict(app.env)
        response = app.client.post("/api/models/migration/scan", {})
        assert response.status == 200, response.json()
        [row] = response.json()["scan"]["items"]
        assert row["selected"] is True
        assert row["proposed_action"] == "import"
        applied = app.client.post("/api/models/migration/apply", {"item_ids": [row["id"]]})
        assert applied.status == 200, applied.json()
        assert applied.json()["applied"] == 1
        assert len(applied.json()["sources"]) == 1
        assert paths[0].read_bytes() == retained
        assert paths[0].stat().st_mode & 0o777 == 0o640
        assert app.env == before_env
        assert app.client.post("/api/models/migration/scan", {}).json()["scan"]["items"] == []


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_f4_replace_migrated_bearer_without_old_secret_retains_cpa_transport(
    model_hub_app_factory, mock_llm_upstream, damage,
):
    _configure_protocol(mock_llm_upstream, "anthropic", models=[{"id": "mock-model"}])
    mock_llm_upstream.configure(required_api_key=KEY, required_auth_scheme="bearer")

    def seed(app):
        _seed_hub(app)
        _write(app.home / ".profile", (
            f"export ANTHROPIC_AUTH_TOKEN='{KEY}'\n"
            f"export ANTHROPIC_BASE_URL='{mock_llm_upstream.url}'\n"
        ))

    with model_hub_app_factory(extra_env=RUNTIME_ENV, before_start=seed) as app:
        [row] = app.client.post("/api/models/migration/scan", {}).json()["scan"]["items"]
        applied = app.client.post("/api/models/migration/apply", {"item_ids": [row["id"]]})
        assert applied.status == 200, applied.json()
        [source] = applied.json()["sources"]
        assert source["credential_ref"].startswith("cred_auth_bearer_")
        _assert_cpa_bearer_consumption(app, mock_llm_upstream, KEY)
        config_path = app.avibe_home / "config/config.json"
        agents_before = json.loads(config_path.read_text())["model_hub"]["agents"]
        old_path = (
            app.avibe_home / "runtime/model-hub/state/credentials"
            / f"{source['credential_ref']}.json"
        )
        assert old_path.is_file()
        if damage == "missing":
            old_path.unlink()
        else:
            old_path.write_bytes(b"{fixture-corrupt")
        replacement = "fixture-replacement-bearer-key"
        mock_llm_upstream.configure(required_api_key=replacement)
        response = app.client.put(
            f"/api/models/sources/{source['id']}/credential", {"key": replacement},
        )
        assert response.status == 200, response.json()
        updated = response.json()["source"]
        assert updated["id"] == source["id"]
        assert updated["credential_ref"] != source["credential_ref"]
        assert updated["credential_ref"].startswith("cred_auth_bearer_")
        assert json.loads(config_path.read_text())["model_hub"]["agents"] == agents_before
        assert not old_path.exists()
        pending = app.avibe_home / "state/model_hub_pending_revocations.json"
        assert json.loads(pending.read_text()) == []
        _assert_cpa_bearer_consumption(app, mock_llm_upstream, replacement)


@pytest.mark.parametrize("history", ["marked", "legacy-unknown"])
def test_f4_oauth_history_fence_survives_later_api_batch_over_http(
    model_hub_app_factory, mock_llm_upstream, history,
):
    """Synthetic recorded custody is consumed by real Controller IPC apply."""
    _configure_protocol(mock_llm_upstream, "openai_responses", models=[{"id": "mock-model"}])
    mock_llm_upstream.configure(required_api_key=KEY)
    paths = []

    def seed(app):
        _seed_hub(app)
        _write(app.home / ".config/opencode/opencode.json", json.dumps({
            "provider": {"openai": {"options": {"apiKey": KEY, "baseURL": mock_llm_upstream.url}}},
        }))
        native = app.home / ".codex/auth.json"
        _write(native, json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "fixture-restored-codex-access",
                "refresh_token": "fixture-restored-codex-refresh",
                "account_id": "fixture-codex-account",
            },
        }))
        _write(app.home / ".codex/config.toml", 'cli_auth_credentials_store = "file"\n')
        receipt = app.avibe_home / "state/native-takeover/last-completed.json"
        NativeTakeoverJournal(receipt).save({
            "version": 1, "phase": "complete", "backends": ["claude"],
            "items": [{"id": "fixture-old-confirmation", "backend": "claude", "kind": "oauth_native"}],
            "source_ids": [], "source_credentials": {}, "outcome": "success",
            **({"oauth_custody_backends": ["codex"]} if history == "marked" else {}),
        })
        paths.extend([native, receipt])

    with model_hub_app_factory(extra_env=RUNTIME_ENV, before_start=seed) as app:
        before = paths[0].read_bytes()
        scan = app.client.post("/api/models/migration/scan", {})
        assert scan.status == 200, scan.json()
        [api] = [item for item in scan.json()["scan"]["items"] if item["backend"] == "opencode"]
        applied = app.client.post("/api/models/migration/apply", {"item_ids": [api["id"]]})
        assert applied.status == 200, applied.json()
        assert "codex" in json.loads(paths[1].read_text())["oauth_custody_backends"]
        sources_before = app.client.get("/api/models/sources").json()["sources"]
        [oauth] = app.client.post("/api/models/migration/scan", {}).json()["scan"]["items"]
        assert oauth["backend"] == "codex" and oauth["proposed_action"] == "import"
        mock_llm_upstream.reset_requests()
        refused = app.client.post("/api/models/migration/apply", {"item_ids": [oauth["id"]]})
        assert refused.status == 409, refused.json()
        assert refused.json()["error"] == "migration_reauthorization_required"
        assert paths[0].read_bytes() == before
        assert app.client.get("/api/models/sources").json()["sources"] == sources_before
        assert mock_llm_upstream.requests() == []
