from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import stat
import sys
import tarfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from core import managed_runtime
from core.handlers.model_hub.adapter import (
    EngineHealth,
    OriginNotAllowedError,
    RawOutcomeKind,
    SourceBinding,
)
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.client import EngineClient
from vibe.model_hub_runtime.config import write_engine_config
from vibe.model_hub_runtime.installer import EngineRuntimeManager
from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore
from vibe.model_hub_runtime.supervisor import EngineSupervisor, EngineUnavailableError


def _write_fixture_archive(tmp_path: Path, *, version: str = "7.2.95") -> tuple[Path, bytes]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    binary = (
        f'#!/bin/sh\nif [ "$1" = "--help" ]; then\n  echo \'CLIProxyAPI Version: {version}, Commit: fixture\' >&2\nfi\n'
    ).encode()
    archive = tmp_path / "CLIProxyAPI_fixture.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("cli-proxy-api")
        member.mode = 0o755
        member.size = len(binary)
        tar.addfile(member, io.BytesIO(binary))
    return archive, binary


def _write_fixture_manifest(
    tmp_path: Path,
    archive: Path,
    binary: bytes,
    *,
    archive_sha256: str | None = None,
) -> Path:
    platform_tag = managed_runtime.runtime_platform_tag()
    manifest_platform = "linux-amd64" if platform_tag == "linux-x64" else platform_tag
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "cliproxyapi",
                "version": "v7.2.95",
                "source": "router-for-me/CLIProxyAPI",
                "source_url": "https://example.test/source",
                "source_sha": "f71ec0eb6776854457892452cf28c47f0d658251",
                "release_tag": "v7.2.95",
                "license": "MIT",
                "assets": [
                    {
                        "platform": manifest_platform,
                        "url": archive.as_uri(),
                        "size_bytes": archive.stat().st_size,
                        "sha256": archive_sha256 or hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "binary_sha256": hashlib.sha256(binary).hexdigest(),
                        "bin_path": "cli-proxy-api",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _binding(credential_ref: str, **overrides: object) -> SourceBinding:
    payload = {
        "source_id": "src_fixture123",
        "vendor": "custom",
        "protocol": "openai_compatible",
        "base_url": "https://api.example.test/v1",
        "credential_ref": credential_ref,
        "allowed_origins": (),
        "model_ids": ("model-a",),
    }
    payload.update(overrides)
    return SourceBinding(**payload)  # type: ignore[arg-type]


def test_packaged_manifest_matches_frozen_runtime_dependency_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)

    manifest = manager.contract_manifest()

    assert manifest == {
        "name": "cliproxyapi",
        "version": "v7.2.95",
        "source_sha": "f71ec0eb6776854457892452cf28c47f0d658251",
        "assets": [
            {
                "platform": "darwin-arm64",
                "url": "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.95/CLIProxyAPI_7.2.95_darwin_aarch64.tar.gz",
                "size_bytes": 14384655,
                "sha256": "c7ccc28b7db5d1799999a9e22725ccc6bd0e36d9aa023da6b52b7c1a71aad978",
            },
            {
                "platform": "linux-amd64",
                "url": "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.95/CLIProxyAPI_7.2.95_linux_amd64.tar.gz",
                "size_bytes": 15401775,
                "sha256": "826604e2dbf11913b0f373047f7bca1829eb2bab8a45d3a1916cc2534c7a9fd5",
            },
        ],
    }

    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "darwin-x64")
    unsupported = EngineRuntimeManager(
        runtime_dir=tmp_path / "unsupported-runtime",
        offline=True,
    ).ensure()
    assert unsupported["ok"] is False
    assert unsupported["reason"] == "model_hub_engine_platform_unsupported"


def test_engine_installer_is_idempotent_and_rejects_tampered_archive(tmp_path: Path) -> None:
    archive, binary = _write_fixture_archive(tmp_path / "good")
    manifest = _write_fixture_manifest(tmp_path / "good", archive, binary)
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)

    first = manager.ensure()
    second = manager.ensure()

    assert first["ok"] is True
    assert first["changed"] is True
    assert second["ok"] is True
    assert second["changed"] is False
    assert manager.status()["installed"] is True

    bad_archive, bad_binary = _write_fixture_archive(tmp_path / "bad")
    bad_manifest = _write_fixture_manifest(
        tmp_path / "bad",
        bad_archive,
        bad_binary,
        archive_sha256="1" * 64,
    )
    rejected = EngineRuntimeManager(
        runtime_dir=tmp_path / "bad-runtime",
        manifest_path=bad_manifest,
    ).ensure()
    assert rejected["ok"] is False
    assert rejected["reason"] == "model_hub_engine_archive_checksum_mismatch"


def test_config_generation_is_private_and_never_logs_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    store = EngineStateStore(tmp_path / "state")
    instance_dir, runtime_secrets = store.prepare_instance("install-1")
    credential_ref = store.store_api_key(
        "upstream-secret-value",
        base_url="https://api.example.test/v1",
    )
    store.sync_sources([_binding(credential_ref)])
    config_path = instance_dir / "config.yaml"

    write_engine_config(
        config_path,
        host="127.0.0.1",
        port=18231,
        auth_dir=store.auth_dir,
        runtime_secrets=runtime_secrets,
        sources=store.list_sources(),
        state_store=store,
    )

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert payload["host"] == "127.0.0.1"
    assert payload["usage-statistics-enabled"] is False
    assert payload["force-model-prefix"] is True
    assert payload["request-retry"] == 0
    assert payload["max-retry-credentials"] == 1
    assert payload["plugins"]["enabled"] is False
    assert payload["remote-management"]["allow-remote"] is False
    assert payload["remote-management"]["disable-control-panel"] is True
    assert payload["openai-compatibility"][0]["api-key-entries"][0]["api-key"] == ("upstream-secret-value")
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.auth_dir.stat().st_mode) == 0o700
    credential_path = next((store.root / "credentials").iterdir())
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
    for secret in (
        runtime_secrets.management_key,
        runtime_secrets.gateway_token,
        "upstream-secret-value",
    ):
        assert secret not in caplog.text


def test_state_rejects_unsafe_inputs_and_auth_permissions(tmp_path: Path) -> None:
    store = EngineStateStore(tmp_path / "state")
    store.prepare_instance("install-1")
    credential_ref = store.store_api_key(
        "secret",
        base_url="https://api.example.test/v1",
    )

    with pytest.raises(EngineStateError, match="invalid source base URL"):
        store.sync_sources([_binding(credential_ref, base_url="https://user:password@example.test/v1")])

    auth_file = store.auth_dir / "oauth.json"
    auth_file.write_text("{}", encoding="utf-8")
    auth_file.chmod(0o644)
    with pytest.raises(EngineStateError, match="credential permissions are unsafe"):
        store.audit_auth_permissions()
    store.audit_auth_permissions(enforce=True)
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600


def test_state_removes_secret_bearing_configs_on_upgrade_and_revocation(tmp_path: Path) -> None:
    store = EngineStateStore(tmp_path / "state")
    old_instance, _ = store.prepare_instance("install-old")
    old_config = old_instance / "config.yaml"
    old_config.write_text("api-key: old-secret\n", encoding="utf-8")
    old_config.chmod(0o600)

    current_instance, _ = store.prepare_instance("install-current")
    assert not old_instance.exists()

    current_config = current_instance / "config.yaml"
    current_config.write_text("api-key: current-secret\n", encoding="utf-8")
    current_config.chmod(0o600)
    store.clear_runtime_configs()
    assert not current_config.exists()


def test_oauth_source_bindings_are_scoped_and_follow_reauthentication(tmp_path: Path) -> None:
    store = EngineStateStore(tmp_path / "state")
    first_ref = store.bind_oauth_credential(
        "src_fixture123",
        "anthropic",
        "claude-first.json",
    )
    binding = _binding(
        first_ref,
        vendor="anthropic",
        protocol="anthropic",
        base_url=None,
        allowed_origins=("claude",),
    )

    with pytest.raises(EngineStateError, match="requires at least one allowed origin"):
        store.sync_sources([SourceBinding(**{**binding.__dict__, "allowed_origins": ()})])
    with pytest.raises(EngineStateError, match="does not match"):
        store.sync_sources([SourceBinding(**{**binding.__dict__, "source_id": "src_other1234"})])
    with pytest.raises(EngineStateError, match="does not match"):
        store.sync_sources([SourceBinding(**{**binding.__dict__, "vendor": "openai"})])

    first = store.sync_sources([binding])[0]
    assert first.prefix == store.credential_metadata(first_ref)["prefix"]

    replacement_ref = store.bind_oauth_credential(
        "src_fixture123",
        "anthropic",
        "claude-replacement.json",
    )
    replacement = store.sync_sources([SourceBinding(**{**binding.__dict__, "credential_ref": replacement_ref})])[0]
    assert replacement.prefix == store.credential_metadata(replacement_ref)["prefix"]
    assert replacement.prefix != first.prefix

    assert (
        store.bind_oauth_credential(
            "src_fixture123",
            "anthropic",
            "claude-replacement.json",
        )
        == replacement_ref
    )
    with pytest.raises(EngineStateError, match="already bound to another source"):
        store.bind_oauth_credential(
            "src_other1234",
            "anthropic",
            "claude-replacement.json",
        )


@contextmanager
def _models_endpoint():
    class Handler(BaseHTTPRequestHandler):
        authorization: str | None = None

        def log_message(self, *args):
            pass

        def do_GET(self):
            Handler.authorization = self.headers.get("Authorization")
            body = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_adapter_provisions_probes_and_revokes_credential(tmp_path: Path) -> None:
    class Supervisor:
        def __init__(self, store: EngineStateStore) -> None:
            self.state_store = store

        def invalidate_configs(self) -> None:
            self.state_store.clear_runtime_configs()

    async def run(base_url: str, handler) -> None:
        store = EngineStateStore(tmp_path / "state")
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(store),  # type: ignore[arg-type]
            state_store=store,
        )
        credential_ref = await adapter.provision_credential(
            "custom",
            "openai_compatible",
            "probe-secret",
            base_url,
        )

        models = await adapter.discover_models(
            "custom",
            "openai_compatible",
            f"{base_url}/",
            credential_ref,
        )

        assert models == ("model-a", "model-b")
        assert handler.authorization == "Bearer probe-secret"
        with pytest.raises(EngineStateError, match="does not match"):
            await adapter.discover_models(
                "custom",
                "openai_compatible",
                "https://different.example/v1",
                credential_ref,
            )
        await adapter.revoke_credential(credential_ref)
        with pytest.raises(EngineStateError, match="unavailable"):
            store.credential_metadata(credential_ref)

    with _models_endpoint() as (base_url, handler):
        asyncio.run(run(base_url, handler))


def _write_mock_engine(path: Path) -> None:
    script = f"""#!{sys.executable}
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import yaml

config_path = sys.argv[sys.argv.index('-config') + 1]
with open(config_path, encoding='utf-8') as handle:
    config = yaml.safe_load(handle)
gateway = config['api-keys'][0]
management = config['remote-management']['secret-key']

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/v1/models' and self.headers.get('Authorization') == f'Bearer {{gateway}}':
            self._json(200, {{'object': 'list', 'data': []}})
            return
        if self.path == '/v0/management/config' and self.headers.get('X-Management-Key') == management:
            self._json(200, {{'host': config['host']}})
            return
        if self.path.startswith('/v0/management/auth-files/models'):
            self._json(200, {{'models': [{{'id': 'model-a'}}]}})
            return
        self._json(401, {{'error': {{'type': 'unauthorized'}}}})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        payload = json.loads(self.rfile.read(length))
        if self.headers.get('Authorization') != f'Bearer {{gateway}}':
            self._json(401, {{'error': {{'type': 'unauthorized'}}}})
            return
        if payload['model'].endswith('/rate-limited'):
            self._json(429, {{'error': {{'type': 'quota_exceeded', 'message': 'upstream-secret'}}}})
            return
        if payload['model'].endswith('/slow-stream'):
            first = b'data: {{"type":"content_block_delta"}}\\n\\n'
            second = b'data: {{"type":"message_stop"}}\\n\\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(first) + len(second)))
            self.end_headers()
            self.wfile.write(first)
            self.wfile.flush()
            time.sleep(0.15)
            self.wfile.write(second)
            return
        if payload.get('stream'):
            body = b'data: {{"type":"message_stop"}}\\n\\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(200, {{'id': 'response-1', 'model': payload['model']}})

HTTPServer((config['host'], config['port']), Handler).serve_forever()
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


class _FixtureInstaller:
    def __init__(self, binary: Path, install_dir: Path) -> None:
        self.binary = binary
        self.install_dir = install_dir
        self.changed = True

    def ensure(self):
        result = {
            "ok": True,
            "path": str(self.binary),
            "install_dir": str(self.install_dir),
            "version": "v7.2.95",
            "changed": self.changed,
        }
        self.changed = False
        return result

    def status(self):
        return {"installed": True, "version": "v7.2.95"}

    def contract_manifest(self):
        return {
            "name": "cliproxyapi",
            "version": "v7.2.95",
            "source_sha": "f" * 40,
            "assets": [],
        }


def _fixture_supervisor(tmp_path: Path) -> tuple[EngineSupervisor, EngineStateStore]:
    binary = tmp_path / "mock-engine"
    _write_mock_engine(binary)
    installer = _FixtureInstaller(binary, tmp_path / "versions" / "install-1")
    store = EngineStateStore(tmp_path / "state")
    return (
        EngineSupervisor(installer=installer, state_store=store, startup_timeout=5),
        store,
    )


def test_supervisor_starts_checks_health_and_stops_mock_engine(tmp_path: Path) -> None:
    supervisor, store = _fixture_supervisor(tmp_path)

    first = supervisor.ensure_running()
    assert first.base_url.startswith("http://127.0.0.1:")
    assert supervisor.status()["status"]["health"] == "ok"
    config_path = store.root / "instances" / "install-1" / "config.yaml"
    first_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert first_config["api-keys"] == [first.gateway_token]
    assert first_config["remote-management"]["secret-key"] == first.management_key

    supervisor.stop()
    assert supervisor.status()["status"]["health"] == "down"
    second = supervisor.ensure_running()
    assert second.gateway_token == first.gateway_token
    assert second.management_key == first.management_key
    supervisor.stop()


def test_adapter_enforces_origin_and_returns_raw_outcomes(tmp_path: Path) -> None:
    async def run() -> None:
        supervisor, store = _fixture_supervisor(tmp_path)
        adapter = CLIProxyEngineAdapter(supervisor=supervisor, state_store=store)
        credential_ref = await adapter.provision_credential(
            "custom",
            "openai_compatible",
            "upstream-secret",
            "https://api.example.test/v1",
        )
        await adapter.sync_sources(
            [
                _binding(
                    credential_ref,
                    allowed_origins=("codex",),
                    model_ids=("model-a", "rate-limited"),
                )
            ]
        )

        started = await adapter.start()
        assert started.health is EngineHealth.OK
        assert started.listen_host == "127.0.0.1"
        assert await adapter.gateway_token()

        with pytest.raises(OriginNotAllowedError):
            await adapter.invoke("src_fixture123", "model-a", {}, False, "claude")

        handle = await adapter.invoke("src_fixture123", "model-a", {}, False, "codex")
        assert handle.stream is not None
        payload = b"".join([chunk async for chunk in handle.stream])
        assert json.loads(payload)["model"].endswith("/model-a")
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.SUCCESS
        assert outcome.stream_started is True

        failed = await adapter.invoke("src_fixture123", "rate-limited", {}, False, "codex")
        assert failed.stream is None
        failure = await failed.outcome()
        assert failure.kind is RawOutcomeKind.HTTP_ERROR
        assert failure.http_status == 429
        assert failure.error_code == "quota_exceeded"
        assert "upstream-secret" not in (failure.redacted_message or "")
        await adapter.stop()
        with pytest.raises(EngineStateError, match="still bound"):
            await adapter.revoke_credential(credential_ref)
        await adapter.sync_sources([])
        await adapter.revoke_credential(credential_ref)

    asyncio.run(run())


def test_adapter_stream_outcome_commits_after_first_byte(tmp_path: Path) -> None:
    async def run() -> None:
        supervisor, store = _fixture_supervisor(tmp_path)
        adapter = CLIProxyEngineAdapter(supervisor=supervisor, state_store=store)
        credential_ref = await adapter.provision_credential(
            "custom",
            "openai_compatible",
            "upstream-secret",
            "https://api.example.test/v1",
        )
        await adapter.sync_sources([_binding(credential_ref)])
        await adapter.start()

        handle = await adapter.invoke("src_fixture123", "model-a", {}, True, "codex")
        assert handle.stream is not None
        body = b"".join([chunk async for chunk in handle.stream])
        assert body.startswith(b"data:")
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.SUCCESS
        assert outcome.stream_started is True
        await adapter.stop()

    asyncio.run(run())


def test_engine_client_does_not_apply_a_total_turn_timeout(tmp_path: Path) -> None:
    async def run() -> None:
        supervisor, store = _fixture_supervisor(tmp_path)
        credential_ref = store.store_api_key(
            "upstream-secret",
            base_url="https://api.example.test/v1",
        )
        store.sync_sources([_binding(credential_ref, model_ids=("slow-stream",))])
        connection = supervisor.ensure_running()
        source = store.get_source("src_fixture123")
        assert source is not None

        handle = await EngineClient(connection, timeout=0.05).invoke(
            source,
            "slow-stream",
            {},
            stream=True,
        )
        assert handle.stream is not None
        body = b"".join([chunk async for chunk in handle.stream])
        assert b"content_block_delta" in body
        assert b"message_stop" in body
        assert (await handle.outcome()).kind is RawOutcomeKind.SUCCESS
        supervisor.stop()

    asyncio.run(run())


@pytest.mark.parametrize("refresh_existing", [False, True])
def test_oauth_flow_binds_new_or_refreshed_auth_record(
    tmp_path: Path,
    refresh_existing: bool,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.auth_calls = 0
            self.patches: list[dict[str, object]] = []

        def management_request(self, method, path, *, query=None, payload=None, timeout=None):
            if path == "/auth-files":
                self.auth_calls += 1
                if self.auth_calls == 1:
                    if not refresh_existing:
                        return {"files": []}
                    return {
                        "files": [
                            {
                                "id": "claude-account.json",
                                "name": "claude-account.json",
                                "provider": "claude",
                                "modtime": "2026-07-23T04:00:00Z",
                            }
                        ]
                    }
                return {
                    "files": [
                        {
                            "id": "claude-account.json",
                            "name": "claude-account.json",
                            "provider": "claude",
                            "modtime": "2026-07-23T04:01:00Z",
                        }
                    ]
                }
            if path == "/anthropic-auth-url":
                return {"state": "engine-state", "url": "https://example.test/oauth"}
            if path == "/get-auth-status":
                return {"status": "ok"}
            if path == "/auth-files/fields":
                self.patches.append(dict(payload or {}))
                return {"status": "ok"}
            raise AssertionError((method, path, query, payload, timeout))

    class Supervisor:
        def __init__(self, store: EngineStateStore, client: Client) -> None:
            self.state_store = store
            self._client = client

        def client(self):
            return self._client

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        store.prepare_instance("install-1")
        (store.auth_dir / "claude-account.json").write_text("{}", encoding="utf-8")
        (store.auth_dir / "claude-account.json").chmod(0o600)
        existing_ref = None
        existing_prefix = None
        if refresh_existing:
            existing_ref = store.bind_oauth_credential(
                "src_fixture123",
                "anthropic",
                "claude-account.json",
            )
            existing_prefix = store.credential_metadata(existing_ref)["prefix"]
        client = Client()
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(store, client),  # type: ignore[arg-type]
            state_store=store,
        )

        flow = await adapter.start_oauth("src_fixture123", "anthropic")
        with pytest.raises(EngineStateError, match="already active"):
            await adapter.start_oauth("src_other1234", "anthropic")
        completed, concurrent = await asyncio.gather(
            adapter.oauth_status(flow.flow_id),
            adapter.oauth_status(flow.flow_id),
        )

        assert completed.state == "success"
        assert completed.source_id == "src_fixture123"
        assert completed.credential_ref and completed.credential_ref.startswith("cred_")
        if refresh_existing:
            assert completed.credential_ref == existing_ref
        assert concurrent.credential_ref == completed.credential_ref
        assert client.patches[0]["name"] == "claude-account.json"
        assert str(client.patches[0]["prefix"]).startswith("avibe-")
        if refresh_existing:
            assert client.patches[0]["prefix"] == existing_prefix
            assert len(list((store.root / "credentials").glob("*.json"))) == 1
        repeated = await adapter.oauth_status(flow.flow_id)
        assert repeated.credential_ref == completed.credential_ref
        assert len(client.patches) == 1
        await adapter.cancel_oauth(flow.flow_id)
        assert (await adapter.oauth_status(flow.flow_id)).state == "success"

    asyncio.run(run())


def test_oauth_flow_releases_provider_after_engine_failure_or_expiry(tmp_path: Path) -> None:
    class Client:
        def __init__(self) -> None:
            self.starts = 0

        def management_request(self, method, path, *, query=None, payload=None, timeout=None):
            if path == "/auth-files":
                return {"files": []}
            if path == "/anthropic-auth-url":
                self.starts += 1
                return {
                    "state": f"engine-state-{self.starts}",
                    "url": "https://example.test/oauth",
                }
            raise AssertionError((method, path, query, payload, timeout))

    class Supervisor:
        def __init__(self, store: EngineStateStore, client: Client) -> None:
            self.state_store = store
            self._client = client
            self.unavailable = False

        def client(self):
            if self.unavailable:
                raise EngineUnavailableError("engine unavailable")
            return self._client

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        client = Client()
        supervisor = Supervisor(store, client)
        adapter = CLIProxyEngineAdapter(
            supervisor=supervisor,  # type: ignore[arg-type]
            state_store=store,
        )

        failed_flow = await adapter.start_oauth("src_fixture123", "anthropic")
        supervisor.unavailable = True
        failed = await adapter.oauth_status(failed_flow.flow_id)
        assert failed.state == "failed"
        assert failed.error_key == "models.oauth.engine_unavailable"

        supervisor.unavailable = False
        expiring_flow = await adapter.start_oauth("src_other1234", "anthropic")
        adapter._oauth_flows[expiring_flow.flow_id].expires_at_iso = "2000-01-01T00:00:00+00:00"
        replacement = await adapter.start_oauth("src_third1234", "anthropic")
        assert replacement.state == "awaiting_action"
        expired = await adapter.oauth_status(expiring_flow.flow_id)
        assert expired.state == "failed"
        assert expired.error_key == "models.oauth.expired"

    asyncio.run(run())


def test_supervisor_fails_closed_with_direct_mode_escape(tmp_path: Path) -> None:
    class FailedInstaller:
        def ensure(self):
            return {"ok": False, "reason": "model_hub_engine_archive_checksum_mismatch"}

        def status(self):
            return {"installed": False, "version": None}

        def contract_manifest(self):
            return {
                "name": "cliproxyapi",
                "version": "v7.2.95",
                "source_sha": "f" * 40,
                "assets": [],
            }

    supervisor = EngineSupervisor(
        installer=FailedInstaller(),
        state_store=EngineStateStore(tmp_path / "state"),
    )

    with pytest.raises(EngineUnavailableError) as exc_info:
        supervisor.ensure_running()
    assert exc_info.value.error_key == "models.engine.install_failed"
    assert exc_info.value.reason == "model_hub_engine_archive_checksum_mismatch"
    assert exc_info.value.direct_mode_available is True
