"""Patched engine HTTP -> existing loopback mock, with actual Avibe registrations.

MH-EFFORT-001 supplementary engine acceptance: the registered Avibe scenario
still owns gateway/resolver evidence; these inactive-source consumers close
its downstream engine seam without claiming shipped behavior.

Run through verify.py. All credentials, Source records, processes and writes
belong to the explicit evidence directory; this never launches an installed
engine or reads an installed engine's state.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import copy
from contextlib import ExitStack
from dataclasses import replace
import http.client
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import zlib
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import yaml

from fixture import verify_fixture
from isolation import isolated_environment, namespace_receipt


opener = build_opener(ProxyHandler({}))
PROTOCOLS = ("anthropic", "openai_responses", "openai_chat")
PATHS = dict(zip(PROTOCOLS, ("/v1/messages", "/v1/responses", "/v1/chat/completions")))
PROFILES = ("known", "known_nil", "unknown", "narrow", "empty")
TEXT = "中文 fixture — reasoning intent"


def request(connection, path, body=None):
    headers = {"Authorization": f"Bearer {connection.gateway_token}", "Content-Type": "application/json"}
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    try:
        response = opener.open(Request(connection.base_url + path, data=raw, headers=headers), timeout=12)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read()


def supplement_empty_profile(config):
    """Assert generated registrations; add only the labeled ENGINE-only case."""
    for section in ("claude-api-key", "codex-api-key", "openai-compatibility"):
        for entry in config[section]:
            for model in entry["models"]:
                assert model["name"] == model["alias"]
                if entry["prefix"].endswith("-narrow"):
                    assert model["thinking"] == {"levels": ["high", "low"]}
                else:
                    assert "thinking" not in model
                if entry["prefix"].endswith("-empty"):
                    # The frozen product deliberately does not generate this.
                    model["thinking"] = {}


class DiagnosticInstaller:
    """Only the preverified task binary; no installed-state or download lookup."""

    def __init__(self, binary):
        self.binary = binary

    def status(self):
        return {"installed": True, "version": "inactive-source-diagnostic",
                "install_dir": str(self.binary.parent)}

    def resolve_engine_path(self):
        return self.binary


class FrozenEngine:
    """Constructor seams only; frozen Avibe owns validation, health and lifecycle."""

    def __init__(self, binary, state, store, log):
        from core.handlers.model_hub.adapter import SourceBinding
        from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
        from vibe.model_hub_runtime.supervisor import EngineSupervisor

        self.binding_type = SourceBinding
        self.binary, self.state, self.store, self.log = binary, state, store, log
        self.processes, self.ports, self.launches = [], [], []
        self.fail_next = False
        self.loop = asyncio.Runner()
        self.supervisor = EngineSupervisor(
            installer=DiagnosticInstaller(binary), state_store=store,
            process_factory=self.launch, port_allocator=self.allocate_port,
        )
        self.adapter = CLIProxyEngineAdapter(supervisor=self.supervisor, state_store=store)

    def allocate_port(self):
        # Force a changed connection to catch stale-port consumers on restart.
        while True:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            if port not in self.ports:
                self.ports.append(port)
                return port

    def launch(self, command, **kwargs):
        from config.atomic_io import write_atomic

        assert command[0] == str(self.binary) and command[1] == "-config"
        config_path = Path(command[2]).resolve(strict=True)
        assert config_path.is_relative_to(self.store.root)
        config = yaml.safe_load(config_path.read_text())
        supplement_empty_profile(config)
        write_atomic(config_path, yaml.safe_dump(config, sort_keys=False))
        # Preserve the product's cwd/stdin/umask/process-group ownership. Only
        # task-local environment, output and local-catalog selection differ.
        kwargs["env"] = {**kwargs["env"], **isolated_environment(self.state)}
        kwargs["stdout"], kwargs["stderr"] = self.log, subprocess.STDOUT
        failure = self.fail_next
        self.fail_next = False
        actual = [sys.executable, "-B", "-c", "raise SystemExit(23)"] if failure else [*command, "--local-model"]
        process = subprocess.Popen(actual, **kwargs)
        self.processes.append(process)
        self.launches.append({"pid": process.pid, "injected_exit": 23 if failure else None,
                              "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest()})
        return process

    def sync(self, sources):
        bindings = [
            self.binding_type(**{name: getattr(source, name) for name in (
                "source_id", "vendor", "protocol", "base_url", "credential_ref",
                "allowed_origins", "model_ids", "model_reasoning_efforts", "route_model_ids",
            )})
            for source in sources
        ]
        self.loop.run(self.adapter.sync_sources(bindings))

    def connection(self):
        client = self.supervisor.client_if_running()
        assert client is not None
        return client.connection

    def close(self):
        try:
            self.supervisor.stop()
        finally:
            self.loop.close()
            assert all(process.poll() is not None for process in self.processes)


def wait_registered(engine, expected_routes):
    connection = engine.connection()
    deadline = time.monotonic() + 20
    while True:
        assert engine.processes[-1].poll() is None
        try:
            status, raw = request(connection, "/v1/models")
            registered = {model["id"] for model in json.loads(raw).get("data", [])}
            if status == 200 and expected_routes <= registered:
                return connection
        except (URLError, TimeoutError):
            pass
        assert time.monotonic() < deadline, "fixture registration deadline"
        time.sleep(0.1)


def payload(protocol, model, intent, stream):
    if protocol == "openai_responses":
        result = {
            "model": model,
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": TEXT}]}],
            "stream": stream,
        }
    else:
        result = {"model": model, "messages": [{"role": "user", "content": TEXT}], "stream": stream}
        if protocol == "anthropic":
            result["max_tokens"] = 64000
    if protocol == "anthropic":
        if intent in ("strong", "future"):
            result["thinking"] = {"type": "adaptive"}
            result["output_config"] = {"effort": "max" if intent == "strong" else "ultra"}
        elif intent == "none":
            result["thinking"] = {"type": "disabled"}
        elif intent in ("budget", "budget_outside"):
            result["thinking"] = {"type": "enabled", "budget_tokens": 4096 if intent == "budget" else 256}
        elif intent == "auto":
            result["thinking"] = {"type": "enabled"}
        elif intent == "summary":
            result["thinking"] = {"type": "adaptive", "display": "omitted"}
    elif protocol == "openai_responses":
        if intent in ("strong", "none", "future", "auto"):
            effort = {"strong": "xhigh", "none": "none", "future": "ultra", "auto": "auto"}[intent]
            result["reasoning"] = {"effort": effort, "summary": "auto"}
        elif intent == "summary":
            result["reasoning"] = {"summary": "auto"}
    elif intent in ("strong", "none", "future", "auto"):
        result["reasoning_effort"] = {"strong": "xhigh", "none": "none", "future": "ultra", "auto": "auto"}[intent]
    elif intent == "summary":
        result["reasoning"] = {"exclude": True}
    return result


def fragment(body):
    return {key: copy.deepcopy(body[key]) for key in ("reasoning", "reasoning_effort", "thinking", "output_config") if key in body}


def assert_policy(row):
    source, target, profile, intent = (row[key] for key in ("frontend", "upstream", "profile", "intent"))
    expected_error = source == "openai_chat" and target == "openai_responses" and profile == "narrow" and intent == "absent"
    assert row["status"] == (400 if expected_error else 200), row
    assert row["upstream_count"] == (0 if expected_error else 1), row
    if expected_error:
        return
    body = row["out"]
    if source == target:
        assert body == row["in"], row
        return
    if intent == "none":
        if target == "anthropic":
            assert body == {"thinking": {"type": "disabled"}}, row
        else:
            effort = body.get("reasoning_effort") if target == "openai_chat" else body.get("reasoning", {}).get("effort")
            assert effort == "none", row
        return
    if intent == "strong":
        if target == "anthropic":
            if profile == "empty":
                assert body["thinking"]["budget_tokens"] == 31999, row
            else:
                effort = {"known": "max", "known_nil": "xhigh", "unknown": "xhigh", "narrow": "high"}[profile]
                assert body["thinking"]["type"] == "adaptive" and body["output_config"]["effort"] == effort, row
        else:
            field = body.get("reasoning_effort") if target == "openai_chat" else body.get("reasoning", {}).get("effort")
            original = "max" if source == "anthropic" else "xhigh"
            if profile == "narrow" or (target == "openai_chat" and profile != "empty"):
                expected = "high"
            elif target == "openai_responses" and profile == "known":
                expected = "xhigh"
            else:
                expected = original
            assert field == expected, row
    elif target == "openai_responses":
        # Preserve translator defaults and known positive conversion constraints.
        expected = "low" if profile == "narrow" else "medium"
        assert body.get("reasoning", {}).get("effort") == expected, row
    else:
        assert not body, row


def synthetic_images():
    """Four valid, distinct RGB PNGs; uncompressed IDAT yields ~42 MiB JSON."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)

    images = []
    for index in range(4):
        width, height = 2048, 1344
        scanline = b"\0" + bytes((index * 60, 80, 160)) * width
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanline * height, level=0))
            + chunk(b"IEND", b"")
        )
        images.append(base64.b64encode(png).decode("ascii"))
    return images


def add_images(body, protocol, images):
    text = {"type": "text" if protocol != "openai_responses" else "input_text", "text": TEXT}
    if protocol == "anthropic":
        parts = [text, *({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}} for image in images)]
    elif protocol == "openai_chat":
        parts = [text, *({"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}} for image in images)]
    else:
        parts = [text, *({"type": "input_image", "image_url": "data:image/png;base64," + image} for image in images)]
    key = "input" if protocol == "openai_responses" else "messages"
    body[key][0]["content"] = parts


def image_and_text(body, protocol):
    key = "input" if protocol == "openai_responses" else "messages"
    parts = body[key][0]["content"]
    text, images = [], []
    for part in parts:
        if part["type"] in ("text", "input_text"):
            text.append(part["text"])
        elif part["type"] == "image":
            assert part["source"]["type"] == "base64" and part["source"]["media_type"] == "image/png"
            images.append(part["source"]["data"])
        else:
            url = part["image_url"]
            if isinstance(url, dict):
                url = url["url"]
            assert url.startswith("data:image/png;base64,")
            images.append(url.removeprefix("data:image/png;base64,"))
    return text, images


def integration_consumers(connection, targets, long_targets, mocks, mock_module):
    results = []
    for protocol in PROTOCOLS:
        for route, model, key in long_targets[protocol]:
            for stream in (False, True):
                for mock in mocks.values():
                    mock.reset_requests()
                status, response = request(
                    connection, PATHS[protocol],
                    payload(protocol, route, "strong", stream),
                )
                assert status == 200 and b"mock response" in response
                captured = mocks[protocol].requests()
                assert len(captured) == 1
                assert not any(mock.requests() for other, mock in mocks.items() if other != protocol)
                outbound = captured[0]
                # Compare FULL identities, never display labels, heads, or hashes.
                assert outbound["body"]["model"] == model
                assert outbound["body"]["model"].encode("utf-8") == model.encode("utf-8")
                assert outbound["path"] == PATHS[protocol]
                values = set(outbound["headers"].values())
                assert key in values or f"Bearer {key}" in values
                assert f"Bearer {connection.gateway_token}" not in values
                results.append({"kind": "long-id", "protocol": protocol, "stream": stream,
                                "utf8_bytes": len(model.encode()), "identity": model})

    # Only this reused MOCK's receive budget changes. No gateway, engine,
    # product constant, or production configuration is changed.
    previous_limit = mock_module.MAX_REQUEST_BODY_BYTES
    mock_module.MAX_REQUEST_BODY_BYTES = 48 * 1024 * 1024
    try:
        images = synthetic_images()
        for protocol in PROTOCOLS:
            for mock in mocks.values():
                mock.reset_requests()
            route, model, key = targets[protocol, "unknown"]
            body = payload(protocol, route, "strong", False)
            add_images(body, protocol, images)
            size = len(json.dumps(body, ensure_ascii=False).encode())
            assert 42 * 1024 * 1024 <= size < 43 * 1024 * 1024
            status, response = request(connection, PATHS[protocol], body)
            assert status == 200 and b"mock response" in response, f"{protocol} large request returned {status}"
            captured = mocks[protocol].requests()
            assert len(captured) == 1
            assert not any(mock.requests() for other, mock in mocks.items() if other != protocol)
            outbound = captured[0]
            assert outbound["path"] == PATHS[protocol] and outbound["body"]["model"] == model
            values = set(outbound["headers"].values())
            assert key in values or f"Bearer {key}" in values
            assert f"Bearer {connection.gateway_token}" not in values
            text, actual_images = image_and_text(outbound["body"], protocol)
            # Compare exact data independently of allowed envelope/cache changes.
            assert text == [TEXT] and actual_images == images
            assert fragment(outbound["body"]) == fragment(body)
            results.append({"kind": "multi-image", "protocol": protocol, "request_bytes": size,
                            "image_sha256": [hashlib.sha256(value.encode()).hexdigest() for value in actual_images],
                            "text": text})
            mocks[protocol].reset_requests()
    finally:
        mock_module.MAX_REQUEST_BODY_BYTES = previous_limit
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--fixture-sha256", required=True)
    args = parser.parse_args()
    binary, state = args.binary.resolve(strict=True), args.state.resolve()
    fixture = args.fixture.resolve(strict=True)
    envelope = namespace_receipt()
    if envelope["network"] != "loopback" or Path(envelope["fixture"]) != fixture or not state.is_relative_to(Path(envelope["state"])):
        raise RuntimeError("Wire fixture must run within its private loopback and read-only source envelope.")
    verify_fixture(fixture, args.fixture_sha256)
    # All Avibe imports resolve from the verified commit export, not this
    # recipe's checkout or caller cwd. The clean child has no PYTHONPATH.
    sys.path.insert(0, str(fixture))
    from vibe.model_hub_runtime.state import EngineStateError, EngineStateStore, SourceRecord
    from vibe.model_hub_runtime.supervisor import EngineUnavailableError

    spec = importlib.util.spec_from_file_location(
        "mock_upstream", fixture / "tests/e2e/drivers/mock_llm_upstream.py",
    )
    mock_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock_module)
    # An existing evidence run is preserved, never overwritten.
    state.mkdir(parents=True, exist_ok=False)
    store = EngineStateStore(state / "engine-state")
    rows, sources, targets, long_targets = [], [], {}, {}
    lifecycle = {}

    with ExitStack() as stack:
        mocks = {protocol: stack.enter_context(mock_module.MockLLMUpstream()) for protocol in PROTOCOLS}
        for protocol, mock in mocks.items():
            mock.configure(protocol=protocol)
            for profile in PROFILES:
                prefix = f"intent-{protocol.replace('_', '-')}-{profile}"
                model = {
                    "known": "claude-opus-4-6" if protocol == "anthropic" else "gpt-5.4",
                    "known_nil": "claude-3-5-haiku-20241022",
                    "unknown": "intent-custom", "narrow": "intent-narrow", "empty": "intent-empty",
                }[profile]
                base = mock.url + ("/v1" if protocol != "anthropic" else "")
                key = f"fake-outbound-{prefix}"
                credential = store.store_api_key(key, protocol=protocol, base_url=base)
                sources.append(SourceRecord(
                    source_id=f"src_{len(sources):08x}", vendor="custom", protocol=protocol,
                    base_url=base, credential_ref=credential, allowed_origins=(mock.url,),
                    model_ids=() if profile == "unknown" else (model,),
                    route_model_ids=(model,) if profile == "unknown" else (),
                    prefix=prefix,
                    model_reasoning_efforts=((model, ("low", "high")),) if profile == "narrow" else (),
                ))
                targets[protocol, profile] = (f"{prefix}/{model}", model, key)
        long_head = "模型-" * 4000
        long_models = ("单独-" + "🌙" * 5000, long_head + "尾甲", long_head + "尾乙", "路由-" + "🎛️" * 3000)
        assert all(len(model.encode()) > 16384 for model in long_models)
        for protocol, mock in mocks.items():
            prefix, key = f"intent-long-{protocol}", f"fake-long-{protocol}"
            base = mock.url + ("/v1" if protocol != "anthropic" else "")
            sources.append(SourceRecord(
                source_id=f"src_{len(sources):08x}", vendor="custom", protocol=protocol,
                base_url=base, credential_ref=store.store_api_key(key, protocol=protocol, base_url=base),
                allowed_origins=(mock.url,), model_ids=long_models[:3],
                route_model_ids=(long_models[3],), prefix=prefix,
            ))
            long_targets[protocol] = [(f"{prefix}/{model}", model, key) for model in long_models]
        # Seed deterministic test prefixes, then validate the entire projection
        # through the real adapter/state owner before the initial child launch.
        store.replace_sources(sources)
        expected_routes = {target[0] for target in targets.values()}
        expected_routes.update(target[0] for values in long_targets.values() for target in values)
        with (state / "engine.log").open("wb") as log:
            engine = FrozenEngine(binary, state, store, log)
            try:
                engine.sync(sources)
                assert store.list_sources() == sources and not engine.processes
                initial = engine.supervisor.ensure_running()
                connection = wait_registered(engine, expected_routes)
                assert connection == initial
                for target in PROTOCOLS:
                    for frontend in PROTOCOLS:
                        intents = ["strong", "none", "absent"]
                        if frontend == target:
                            intents += ["summary", "auto", "future"]
                            if frontend == "anthropic":
                                intents += ["budget", "budget_outside"]
                        for profile in PROFILES:
                            route, model, key = targets[target, profile]
                            for intent in intents:
                                for stream in (False, True):
                                    for mock in mocks.values():
                                        mock.reset_requests()
                                    body = payload(frontend, route, intent, stream)
                                    status, response = request(connection, PATHS[frontend], body)
                                    captured = mocks[target].requests()
                                    assert not any(mock.requests() for protocol, mock in mocks.items() if protocol != target)
                                    assert len(captured) <= 1
                                    outbound = captured[0] if captured else None
                                    if outbound:
                                        assert outbound["path"] == PATHS[target]
                                        assert outbound["body"]["model"] == model
                                        assert TEXT in json.dumps(outbound["body"], ensure_ascii=False)
                                        values = set(outbound["headers"].values())
                                        assert key in values or f"Bearer {key}" in values
                                        assert f"Bearer {connection.gateway_token}" not in values
                                        assert b"mock response" in response
                                    row = {
                                        "frontend": frontend, "upstream": target, "profile": profile,
                                        "intent": intent, "stream": stream, "status": status,
                                        "upstream_count": len(captured), "in": fragment(body),
                                        "out": fragment(outbound["body"]) if outbound else None,
                                    }
                                    rows.append(row)
                                    assert_policy(row)

                integration = integration_consumers(connection, targets, long_targets, mocks, mock_module)
                (state / "integration.json").write_text(json.dumps(integration, ensure_ascii=False, indent=2))

                # Consume the real frozen adapter transaction, barrier, config
                # writer and supervisor stop/start. No watcher-only contract.
                replacement_mock = stack.enter_context(mock_module.MockLLMUpstream())
                replacement_mock.configure(protocol="openai_responses")
                index = next(i for i, source in enumerate(sources) if source.prefix == "intent-openai-responses-unknown")
                previous = sources[index]
                base = replacement_mock.url + "/v1"
                key = "fake-replaced-source-key"
                candidate = replace(previous, base_url=base, allowed_origins=(replacement_mock.url,),
                                    credential_ref=store.store_api_key(key, protocol=previous.protocol, base_url=base))
                next_sources = [candidate if i == index else source for i, source in enumerate(sources)]
                route, model, old_key = targets["openai_responses", "unknown"]

                # Focused real consumer: invalid credentials fail validation
                # before any child is stopped or projection is committed.
                original_child = engine.processes[-1]
                invalid = [replace(candidate, credential_ref=previous.credential_ref)
                           if i == index else source for i, source in enumerate(sources)]
                try:
                    engine.sync(invalid)
                except EngineStateError as exc:
                    assert "credential does not match" in str(exc)
                else:
                    raise AssertionError("source validation was bypassed")
                assert store.list_sources() == sources
                assert len(engine.processes) == 1 and original_child.poll() is None
                lifecycle["invalid_source_no_restart"] = "pass"

                # A genuinely exiting child exercises the product's rollback,
                # recovery health check, and failure cleanup, not a restart mock.
                engine.fail_next = True
                try:
                    engine.sync(next_sources)
                except EngineUnavailableError as exc:
                    assert exc.error_key == "models.engine.health_failed"
                else:
                    raise AssertionError("failed replacement did not surface")
                assert store.list_sources() == sources
                assert len(engine.processes) == 3
                assert original_child.poll() is not None and engine.processes[1].poll() == 23
                connection = wait_registered(engine, expected_routes)
                assert connection.base_url != initial.base_url
                for mock in [*mocks.values(), replacement_mock]:
                    mock.reset_requests()
                status, _ = request(connection, "/v1/responses", payload("openai_responses", route, "future", False))
                assert status == 200 and not replacement_mock.requests()
                captured = mocks["openai_responses"].requests()
                assert len(captured) == 1 and captured[0]["body"]["model"] == model
                assert captured[0]["headers"]["authorization"] == f"Bearer {old_key}"
                lifecycle["failed_replacement_rollback_and_recovery"] = "pass"

                previous_connection, previous_child = connection, engine.processes[-1]
                engine.sync(next_sources)
                assert store.list_sources() == next_sources
                assert len(engine.processes) == 4 and previous_child.poll() is not None
                assert engine.processes[-1].poll() is None
                connection = wait_registered(engine, expected_routes)
                assert connection.base_url != previous_connection.base_url
                assert store.get_source(previous.source_id).prefix == previous.prefix
                assert store.get_source(previous.source_id).route_model_ids == previous.route_model_ids
                for stream in (False, True):
                    for mock in [*mocks.values(), replacement_mock]:
                        mock.reset_requests()
                    status, _ = request(connection, "/v1/responses",
                                        payload("openai_responses", route, "none", stream))
                    assert status == 200 and not any(mock.requests() for mock in mocks.values())
                    captured = replacement_mock.requests()
                    assert len(captured) == 1
                    assert captured[0]["body"]["model"] == model
                    assert captured[0]["body"]["reasoning"]["effort"] == "none"
                    assert captured[0]["headers"]["authorization"] == f"Bearer {key}"
                    values = set(captured[0]["headers"].values())
                    assert not {old_key, f"Bearer {old_key}", f"Bearer {connection.gateway_token}"} & values
                lifecycle["committed_replacement_stream_and_nonstream"] = "pass"

                # HTTP client cancellation plus immediate reuse of the same
                # source. Precise upstream context cancellation is also covered
                # by the Go executor test (this reused mock pauses deliberately).
                mock = mocks["anthropic"]
                mock.configure(stream="pause_after_first_output")
                route, _, _ = targets["anthropic", "unknown"]
                http_connection = http.client.HTTPConnection("127.0.0.1", int(connection.base_url.rsplit(":", 1)[1]), timeout=5)
                http_connection.request("POST", "/v1/messages",
                                   json.dumps(payload("anthropic", route, "strong", True)).encode(),
                                   {"Authorization": f"Bearer {connection.gateway_token}", "Content-Type": "application/json"})
                response = http_connection.getresponse()
                assert response.status == 200
                while True:
                    line = response.readline()
                    assert line, "stream ended before first output"
                    if b"mock response" in line:
                        break
                if http_connection.sock is not None:
                    http_connection.sock.shutdown(socket.SHUT_RDWR)
                response.close()
                http_connection.close()
                mock.configure(stream="healthy")
                status, _ = request(connection, "/v1/messages",
                                    payload("anthropic", route, "future", False))
                assert status == 200
                lifecycle["cancellation_and_reuse"] = "pass"

                # Failed startup with no prior child cannot leave an owned
                # listener/process behind. Product stop/health failure owns it.
                engine.supervisor.stop()
                engine.fail_next = True
                try:
                    engine.supervisor.ensure_running()
                except EngineUnavailableError as exc:
                    assert exc.error_key == "models.engine.health_failed"
                else:
                    raise AssertionError("failed startup did not surface")
                assert engine.supervisor.client_if_running() is None
                assert len(engine.processes) == 5 and engine.processes[-1].poll() == 23
                lifecycle["failed_startup_cleanup"] = "pass"
            finally:
                (state / "matrix.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
                try:
                    engine.close()
                finally:
                    lifecycle["children"] = [
                        {**launch, "returncode": process.poll()}
                        for launch, process in zip(engine.launches, engine.processes)
                    ]
                    (state / "lifecycle.json").write_text(json.dumps(lifecycle, indent=2))
    assert len(rows) == 380
    for row in rows:
        if row["stream"]:
            peer = next(item for item in rows if all(item[key] == row[key] for key in ("frontend", "upstream", "profile", "intent")) and not item["stream"])
            assert row["out"] == peer["out"]
    verify_fixture(fixture, args.fixture_sha256)
    print(json.dumps({"matrix_cases": len(rows), "successful": sum(row["status"] == 200 for row in rows),
                      "replacement": "pass", "cancellation_and_reuse": "pass", "processes_reaped": True,
                      "lifecycle_regressions": lifecycle,
                      "long_identity_cases": 24, "multi_image_cases": 3,
                      "matrix_file": str(state / "matrix.json"), "integration_file": str(state / "integration.json")}))


if __name__ == "__main__":
    main()
