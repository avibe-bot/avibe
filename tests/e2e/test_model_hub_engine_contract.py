"""What Avibe needs from the pinned CPA binary, independent of its version.

These cases run against whichever engine ``VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH``
names, so one suite characterizes the shipped engine and an upgrade candidate.
Every upstream is a loopback fixture; the engine, its config and its
management API are real. Each case writes a value-free evidence summary to
``tmp_path / "evidence.json"`` so two engines' runs can be compared.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from core.handlers.model_hub.service import ModelHubError
from tests.e2e.test_model_hub_runtime import _isolated_engine_adapter
from tests.scenario_harness.model_hub import MemoryModelHubStore, service_for
from tests.test_model_hub_l3 import _source
from tests.test_model_hub_vibey_consumer import _vibey_service
from vibe.model_hub_runtime import config as engine_config
from vibe.model_hub_runtime.client import EngineClient, EngineClientError
from vibe.model_hub_runtime.config import expected_model_names
from vibe.model_hub_runtime.state import SourceRecord


pytestmark = pytest.mark.e2e_model_hub

_UNUSED_UPSTREAM = "http://127.0.0.1:9"
_READY_SECONDS = 15.0


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _await(condition, *, what: str, seconds: float = _READY_SECONDS):
    """Wait for an engine-side effect Avibe also waits for, never longer than ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"engine never reached: {what}")
        time.sleep(0.1)


def _shape(value, depth: int = 0):
    """Keys and JSON types only, so evidence never carries a value."""
    if isinstance(value, dict):
        return {key: _shape(item, depth + 1) for key, item in sorted(value.items())} if depth < 3 else "object"
    if isinstance(value, list):
        merged: dict = {}
        for item in value:
            shaped = _shape(item, depth + 1)
            if isinstance(shaped, dict):
                merged.update(shaped)
            else:
                return [shaped]
        return [merged] if merged else []
    return type(value).__name__


def _all_protocol_sources(store) -> list[SourceRecord]:
    """One API-key Source per protocol, together using every YAML feature Avibe renders."""
    sources = []
    for index, (protocol, vendor, models, efforts, route_ids, text_only) in enumerate((
        ("openai_chat", "custom", ("chat-model", "chat-text-only"), (), (), ("chat-text-only",)),
        ("openai_responses", "custom", ("responses-model",), (("responses-model", ("low", "high")),), (), ()),
        ("anthropic", "custom", ("anthropic-model",), (), ("anthropic-routed",), ()),
        ("google", "custom", ("gemini-model",), (), (), ()),
    )):
        base_url = f"{_UNUSED_UPSTREAM}/fixture-{index}"
        credential_ref = store.store_api_key(
            f"sk-synthetic-contract-{index}", vendor=vendor, protocol=protocol, base_url=base_url,
        )
        sources.append(SourceRecord(
            source_id=f"src_contract{index:03d}",
            vendor=vendor,
            protocol=protocol,
            base_url=base_url,
            credential_ref=credential_ref,
            allowed_origins=(),
            model_ids=models,
            prefix=f"avibe-{index:024x}",
            model_reasoning_efforts=efforts,
            route_model_ids=route_ids,
            text_only_model_ids=text_only,
        ))
    return sources


def _write_evidence(tmp_path, payload) -> None:
    text = json.dumps(payload, sort_keys=True)
    (tmp_path / "evidence.json").write_text(text, encoding="utf-8")
    # One line per case, so a run's captured output compares across engines.
    print(f"EVIDENCE {os.environ.get('PYTEST_CURRENT_TEST', '').split(' ')[0]} {text}")


def _without_secret(text: str) -> tuple[dict, str]:
    document = yaml.safe_load(text)
    secret = document["remote-management"].pop("secret-key")
    return document, secret


def _secret_masked_sha(data: bytes) -> str:
    """The file's sha with the salted management-key hash masked, comparable across runs."""
    masked = re.sub(rb"(secret-key:\s*)\S+", rb"\1<secret>", data)
    return _sha(masked)


def test_engine_keeps_avibe_config_content_across_start_and_hot_reload(tmp_path, monkeypatch) -> None:
    """The engine applies Avibe's config.yaml as written, at start and on PUT reload.

    Every load replaces the plaintext management key with a salted bcrypt hash
    and re-encodes the file (CPA ``config_load.go``), so the bytes Avibe wrote
    never survive a load. Everything else must: no migrated, dropped, added or
    renamed setting, and the key Avibe holds must still authenticate.
    """
    written: dict[str, str] = {}
    original_write = engine_config.write_atomic

    def recording_write(path, text, *args, **kwargs):
        written[str(path)] = text
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(engine_config, "write_atomic", recording_write)
    sent: list[str] = []
    original_put = EngineClient.put_config_yaml

    def recording_put(self, text, **kwargs):
        sent.append(text)
        return original_put(self, text, **kwargs)

    monkeypatch.setattr(EngineClient, "put_config_yaml", recording_put)

    with _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        store = adapter.state_store
        store.replace_sources(_all_protocol_sources(store))
        connection = adapter.supervisor.ensure_running()
        [config_path] = list(store.root.glob("instances/*/config.yaml"))
        start_text = written[str(config_path)]
        client = EngineClient(connection)
        expected = expected_model_names(store.list_sources(), store, "1")
        _await(
            lambda: all(client.list_model_names(timeout=1.0).get(model) == name for model, name in expected.items()),
            what="start projection listed",
        )
        settings = client.management_request("GET", "/config")
        after_start = config_path.read_bytes()

        previous = store.list_sources()
        store.set_models("src_contract000", ("chat-model", "chat-text-only", "chat-added"))
        adapter.supervisor.reload_config_if_running(previous)
        [reload_text] = sent
        client.management_request("GET", "/config")
        after_reload = config_path.read_bytes()

    evidence = {}
    for phase, avibe_text, on_disk in (("start", start_text, after_start), ("reload", reload_text, after_reload)):
        rendered, plaintext = _without_secret(avibe_text)
        loaded, stored = _without_secret(on_disk.decode())
        evidence[phase] = {
            "avibe_sha": _sha(avibe_text.encode()),
            "file_sha": _sha(on_disk),
            "file_bytes_equal_avibe": on_disk == avibe_text.encode(),
            "file_secret_masked_sha": _secret_masked_sha(on_disk),
            "stored_secret_kind": "bcrypt" if stored.startswith(("$2a$", "$2b$", "$2y$")) else (
                "plaintext" if stored == plaintext else "other"),
            "content_equal_without_secret": loaded == rendered,
            "added_keys": sorted(set(loaded) - set(rendered)),
            "dropped_keys": sorted(set(rendered) - set(loaded)),
        }
    evidence["get_config_shape"] = _shape(settings)
    _write_evidence(tmp_path, evidence)
    for phase in ("start", "reload"):
        assert evidence[phase]["content_equal_without_secret"], evidence[phase]
        assert evidence[phase]["stored_secret_kind"] in {"bcrypt", "plaintext"}, evidence[phase]
    # The settings Avibe relies on were read, not dropped as unknown keys.
    assert settings.get("request-retry") == 0
    assert settings.get("disable-cooling") is True
    assert settings.get("force-model-prefix") is True
    assert settings.get("routing", {}).get("strategy") == "fill-first"


def _jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJl"


@contextmanager
def _capturing_server():
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            seen.append({"path": self.path, "authorization": self.headers.get("Authorization")})
            body = b'{"fixture":"quota"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Fixture", "yes")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _call(client: EngineClient, method: str, path: str, **kwargs):
    try:
        return {"ok": True, "body": client.management_request(method, path, **kwargs)}
    except EngineClientError as exc:
        return {"ok": False, "status": exc.status_code, "error_type": exc.error_type}


def test_engine_management_surface_avibe_reads(tmp_path, monkeypatch) -> None:
    """Every /v0/management call Avibe makes answers with the fields Avibe reads."""
    access_token = "synthetic-access-token-contract"
    account_id = "acct-synthetic-contract"
    auth_name = "codex-contract-fixture.json"
    grant = {
        "type": "codex",
        "access_token": access_token,
        "refresh_token": "",
        "id_token": _jwt({
            "email": "fixture@example.invalid",
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id, "chatgpt_plan_type": "plus"},
        }),
        "account_id": account_id,
        "email": "fixture@example.invalid",
        "expired": "2099-01-01T00:00:00Z",
        "last_refresh": "2026-01-01T00:00:00Z",
    }
    evidence: dict = {}
    with _capturing_server() as (upstream, seen), _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        client = EngineClient(adapter.supervisor.ensure_running())
        adapter.state_store.write_oauth_auth_file(auth_name, grant)

        def listed():
            files = client.management_request("GET", "/auth-files").get("files") or []
            return next((item for item in files if item.get("name") == auth_name), None)

        record = _await(listed, what="auth file listed")
        evidence["auth_files_item"] = _shape(record)
        assert record.get("auth_index")
        assert (record.get("provider") or record.get("type")) == "codex"
        assert (record.get("id_token") or {}).get("chatgpt_account_id") == account_id

        models = _call(client, "GET", "/auth-files/models", query={"name": auth_name})
        evidence["auth_files_models"] = {"ok": models["ok"], "shape": _shape(models.get("body"))}
        assert models["ok"] and isinstance(models["body"].get("models"), list) and models["body"]["models"]

        prefix = "avibe-" + "c" * 24
        patched = _call(client, "PATCH", "/auth-files/fields", payload={"name": auth_name, "prefix": prefix})
        evidence["auth_files_fields"] = {"ok": patched["ok"], "shape": _shape(patched.get("body"))}
        assert patched["ok"]
        _await(
            lambda: any(model.startswith(f"{prefix}/") for model in client.list_model_ids(timeout=1.0)),
            what="prefixed OAuth models listed",
        )

        called = _call(client, "POST", "/api-call", payload={
            "auth_index": record["auth_index"],
            "method": "GET",
            "url": f"{upstream}/quota",
            "header": {"Authorization": "Bearer $TOKEN$"},
        })
        evidence["api_call"] = {"ok": called["ok"], "shape": _shape(called.get("body"))}
        assert called["ok"] and called["body"].get("status_code") == 200
        assert seen and seen[-1]["authorization"] == f"Bearer {access_token}"

        flows = {}
        for endpoint in ("/codex-auth-url", "/anthropic-auth-url"):
            started = _call(client, "GET", endpoint, query={"is_webui": "true"})
            assert started["ok"], (endpoint, started)
            state = started["body"].get("state")
            assert state and (started["body"].get("url") or started["body"].get("verification_uri"))
            waiting = _call(client, "GET", "/get-auth-status", query={"state": state})
            refused = _call(client, "POST", "/oauth-callback", payload={
                "provider": "codex" if "codex" in endpoint else "anthropic",
                "state": "not-" + state,
                "code": "synthetic-code",
            })
            cancelled = _call(client, "DELETE", "/oauth-session", query={"state": state})
            after_cancel = _call(client, "GET", "/get-auth-status", query={"state": state})
            flows[endpoint] = {
                "start": _shape(started["body"]),
                "status_waiting": waiting.get("body", waiting),
                "callback_wrong_state": {"ok": refused["ok"], "status": refused.get("status")},
                "cancel": {"ok": cancelled["ok"], "status": cancelled.get("status")},
                "status_after_cancel": after_cancel.get("body", after_cancel),
            }
            assert waiting["ok"] and waiting["body"].get("status") == "wait"
            assert cancelled["ok"]
        evidence["oauth"] = flows

        deleted = _call(client, "DELETE", "/auth-files", query={"name": auth_name})
        evidence["auth_files_delete"] = {"ok": deleted["ok"], "shape": _shape(deleted.get("body"))}
        assert deleted["ok"]
        _await(lambda: listed() is None, what="auth file removed")

        # Characterization only: whether a freshly loaded Gemini (Antigravity)
        # grant lists models before the engine has talked to Google.
        antigravity = "antigravity-contract-fixture.json"
        adapter.state_store.write_oauth_auth_file(antigravity, {
            "type": "antigravity",
            "access_token": "synthetic-antigravity-token",
            "refresh_token": "",
            "email": "fixture@example.invalid",
            "project_id": "synthetic-project",
            "expired": "2099-01-01T00:00:00Z",
        })
        _await(
            lambda: any(item.get("name") == antigravity
                        for item in client.management_request("GET", "/auth-files").get("files") or []),
            what="antigravity auth file listed",
        )
        counts = []
        for _ in range(5):
            listing = _call(client, "GET", "/auth-files/models", query={"name": antigravity})
            counts.append(len((listing.get("body") or {}).get("models") or []))
            time.sleep(1)
        evidence["antigravity_models_first_5s"] = counts
    _write_evidence(tmp_path, evidence)


# The engine's embedded Antigravity catalog (models.json at v8.0.23). The
# fixture's entitlement answer names all of them except one, so a narrowing
# that the probe applies is observable.
_ANTIGRAVITY_IDS = (
    "claude-opus-4-6-thinking", "claude-sonnet-4-6", "claude-opus-5-5-high", "claude-sonnet-5-5-high",
    "gemini-3.6-flash-high", "gemini-3.7-flash-high", "gemini-3.8-flash-high", "gemini-3-flash",
    "gemini-3.1-flash-image", "gemini-pro-agent", "gemini-3.1-pro-low", "gpt-oss-120b-medium",
    "gemini-3.1-flash-lite", "gemini-3.5-flash-lite",
)
_ANTIGRAVITY_NOT_ENTITLED = "gemini-3.1-flash-lite"
_ANTIGRAVITY_PROBE_DELAY = 3.0
_ANTIGRAVITY_PREFIX = "avibe-" + "a" * 24


@contextmanager
def _antigravity_upstream(probe: str):
    """Google's Antigravity endpoints on loopback: entitlement probe plus generation."""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append({"path": self.path, "at": time.monotonic()})
            if self.path.startswith("/v1internal:fetchAvailableModels"):
                if probe == "failure":
                    body, status = b'{"error":{"code":503,"message":"fixture outage"}}', 503
                else:
                    time.sleep(_ANTIGRAVITY_PROBE_DELAY)
                    entitled = [model for model in _ANTIGRAVITY_IDS if model != _ANTIGRAVITY_NOT_ENTITLED]
                    body = json.dumps({"models": {model: {} for model in entitled}}).encode()
                    status = 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            chunk = {"response": {"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]},
                                                  "finishReason": "STOP"}],
                                  "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1,
                                                    "totalTokenCount": 2}}}
            body = b"data: " + json.dumps(chunk).encode() + b"\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _antigravity_grant(upstream: str, *, prefix: str | None = None) -> dict:
    grant = {
        "type": "antigravity",
        "access_token": "synthetic-antigravity-token",
        "refresh_token": "",
        "email": "fixture@example.invalid",
        "project_id": "synthetic-project",
        "expired": "2099-01-01T00:00:00Z",
        "base_url": upstream,
    }
    if prefix:
        grant["prefix"] = prefix
    return grant


def _engine_chat(client: EngineClient, model: str) -> tuple[int, str]:
    request = urllib.request.Request(
        client.connection.base_url + "/v1/chat/completions",
        data=json.dumps({"model": model, "messages": [{"role": "user", "content": "x"}]}).encode(),
        headers={"Authorization": f"Bearer {client.connection.gateway_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _antigravity_timeline(client: EngineClient, auth_name: str, started: float, *, seconds: float) -> list[dict]:
    """Listing and routing, sampled from the moment the grant is loaded."""
    entitled = f"{_ANTIGRAVITY_PREFIX}/gemini-3-flash"
    excluded = f"{_ANTIGRAVITY_PREFIX}/{_ANTIGRAVITY_NOT_ENTITLED}"
    samples = []
    deadline = started + seconds
    while time.monotonic() < deadline:
        listing = _call(client, "GET", "/auth-files/models", query={"name": auth_name})
        # A prefixed grant lists its routed IDs; compare the upstream model names.
        ids = {str(item.get("id")).rsplit("/", 1)[-1] for item in (listing.get("body") or {}).get("models") or []}
        samples.append({
            "t": round(time.monotonic() - started, 2),
            "listed": len(ids),
            "excluded_listed": _ANTIGRAVITY_NOT_ENTITLED in ids,
            "entitled_status": _engine_chat(client, entitled)[0],
            "excluded_status": _engine_chat(client, excluded)[0],
        })
        time.sleep(0.5)
    return samples


@pytest.mark.parametrize("probe", ["delayed_success", "failure"])
def test_antigravity_grant_routes_at_once_and_narrows_when_the_probe_answers(tmp_path, monkeypatch, probe) -> None:
    """A Gemini (Antigravity) grant routes from the moment the engine loads it.

    Avibe discovers a new grant's models right after the OAuth flow and routes
    to them right after every engine start. Since v8.0.14 the stock engine
    lists nothing until its per-account entitlement probe answers; Avibe's
    engine patch registers the static catalog meanwhile, the probe narrows it,
    and a failed probe leaves it in place (router-for-me/CLIProxyAPI#6531).
    """
    auth_name = "antigravity-window-fixture.json"
    evidence: dict = {"probe": probe}
    with _antigravity_upstream(probe) as (upstream, seen), _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        client = EngineClient(adapter.supervisor.ensure_running())
        adapter.state_store.write_oauth_auth_file(auth_name, _antigravity_grant(upstream, prefix=_ANTIGRAVITY_PREFIX))
        loaded = time.monotonic()
        _await(
            lambda: any(item.get("name") == auth_name
                        for item in client.management_request("GET", "/auth-files").get("files") or []),
            what="antigravity grant listed",
        )
        evidence["after_grant_load"] = _antigravity_timeline(
            client, auth_name, loaded, seconds=_ANTIGRAVITY_PROBE_DELAY + 5)
        adapter.supervisor.stop()
        restarted = time.monotonic()
        client = EngineClient(adapter.supervisor.ensure_running())
        evidence["after_engine_restart"] = _antigravity_timeline(
            client, auth_name, restarted, seconds=_ANTIGRAVITY_PROBE_DELAY + 5)
        evidence["probe_calls"] = sum(1 for item in seen if "fetchAvailableModels" in item["path"])
    _write_evidence(tmp_path, evidence)
    for phase in ("after_grant_load", "after_engine_restart"):
        samples = evidence[phase]
        assert samples[0]["listed"] > 0 and samples[0]["entitled_status"] == 200, (phase, samples)
        assert all(sample["entitled_status"] == 200 for sample in samples), (phase, samples)
        if probe == "delayed_success":
            final = samples[-1]
            assert not final["excluded_listed"] and final["excluded_status"] == 400, (phase, samples)
        else:
            assert all(sample["excluded_listed"] and sample["excluded_status"] == 200 for sample in samples), (
                phase, samples)


async def _refresh(service, source_id: str) -> dict:
    try:
        result = await service.refresh_source(source_id)
        outcome = {"ok": True, "models": sorted(model["id"] for model in result["source"].get("models") or [])}
    except ModelHubError as exc:
        outcome = {"ok": False, "error": exc.code}
    state = service._source(service.store.load(), source_id).state
    return {**outcome, "source_state": [state.status, state.detail_key]}


async def test_antigravity_source_refresh_right_after_engine_start(tmp_path, monkeypatch) -> None:
    """A Gemini subscription Source refreshed right after the engine starts keeps its models."""
    auth_name = "antigravity-refresh-fixture.json"
    with _antigravity_upstream("delayed_success") as (upstream, seen), \
            _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        adapter.state_store.write_oauth_auth_file(auth_name, _antigravity_grant(upstream))
        source = _source("src_gemini0001", "Gemini subscription", vendor="gemini", protocol="openai_chat",
                         model_id="gemini-3-flash")
        source.kind = "subscription"
        source.billing = "monthly"
        source.credential_ref = adapter.state_store.bind_oauth_credential("src_gemini0001", "gemini", auth_name)
        config = _vibey_service(tmp_path, [source]).store.config
        service = service_for(tmp_path, MemoryModelHubStore(config), adapter)
        await service.runtime_start()
        started = time.monotonic()
        first = await _refresh(service, "src_gemini0001")
        first["after_seconds"] = round(time.monotonic() - started, 2)
        client = adapter.supervisor.client_if_running()

        def narrowed():
            listing = client.management_request("GET", "/auth-files/models", query={"name": auth_name})
            ids = {item.get("id") for item in listing.get("models") or []}
            return ids and _ANTIGRAVITY_NOT_ENTITLED not in ids

        await asyncio.to_thread(_await, narrowed, what="entitlement narrowing applied")
        narrowed_after = round(time.monotonic() - started, 2)
        second = await _refresh(service, "src_gemini0001")
    _write_evidence(tmp_path, {"refresh_at_start": first, "narrowed_after_seconds": narrowed_after,
                               "refresh_after_narrowing": second})
    assert first["ok"] and "gemini-3-flash" in first["models"], first
    assert second["ok"] and _ANTIGRAVITY_NOT_ENTITLED not in second["models"], second
    assert second["source_state"][0] == "standby", second
