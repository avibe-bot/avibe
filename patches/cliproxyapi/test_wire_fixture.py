"""Pure recipe consumers; actual lifecycle regressions run in isolated wire."""

import copy
import builtins
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import wire_matrix
from fixture import source_digest


def test_actual_wire_refuses_hardlinked_fixture_before_import_or_effect(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    target = fixture / "unpatched.py"
    target.write_bytes(b"raise RuntimeError('must never import')\n")
    expected = source_digest(fixture)
    build = tmp_path / "prior-build"
    (build / "bin").mkdir(parents=True)
    binary = build / "bin/cli-proxy-api"
    binary.write_bytes(b"task-only binary sentinel; never executed")
    output = tmp_path / "output"
    output.mkdir()
    state = output / "wire"
    os.link(target, output / "writable-alias")
    proof = {"network": "loopback", "fixture": str(fixture), "output": str(output),
             "selected_build": {"output": str(build)}}
    monkeypatch.setattr(wire_matrix, "namespace_receipt", lambda: proof)
    monkeypatch.setattr(sys, "argv", ["wire_matrix.py", "--binary", str(binary),
        "--state", str(state), "--fixture", str(fixture), "--fixture-sha256", expected])
    original_import = builtins.__import__
    imports = []

    def importing(name, *args, **kwargs):
        if name.startswith("vibe."):
            imports.append(name)
            pytest.fail("Rejected fixture reached Avibe import.")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    monkeypatch.setattr(wire_matrix.importlib.util, "spec_from_file_location",
                        lambda *_a, **_kw: pytest.fail("Rejected fixture reached dynamic import."))
    monkeypatch.setattr(wire_matrix.subprocess, "run",
                        lambda *_a, **_kw: pytest.fail("Rejected fixture reached process effect."))
    original_path = list(sys.path)
    with pytest.raises(ValueError, match="single-link regular"):
        wire_matrix.main()
    assert sys.path == original_path and not imports and not state.exists()
    assert target.read_bytes() == b"raise RuntimeError('must never import')\n"


@pytest.mark.parametrize("section", ["claude-api-key", "codex-api-key", "openai-compatibility"])
def test_engine_only_empty_profile_preserves_all_generated_fields(section):
    config = {name: [] for name in ("claude-api-key", "codex-api-key", "openai-compatibility")}
    config[section] = [
        {"prefix": "intent-protocol-" + profile, "api-key": "fake-only-key",
         "base-url": "http://127.0.0.1:1234", "headers": {"api-mode": "chat"},
         "models": [{"name": "模型完整", "alias": "模型完整", "context-window": 123,
                     **({"thinking": {"levels": ["high", "low"]}} if profile == "narrow" else {})}]}
        for profile in ("known", "unknown", "narrow", "empty")
    ]
    expected = copy.deepcopy(config)
    expected[section][-1]["models"][0]["thinking"] = {}
    wire_matrix.supplement_empty_profile(config)
    assert config == expected


def test_generated_metadata_must_not_be_silently_overwritten():
    config = {name: [] for name in ("claude-api-key", "codex-api-key", "openai-compatibility")}
    config["openai-compatibility"] = [
        {"prefix": "intent-chat-empty", "models": [
            {"name": "model", "alias": "model", "thinking": {"levels": ["future"]}},
        ]},
    ]
    previous = copy.deepcopy(config)
    with pytest.raises(AssertionError):
        wire_matrix.supplement_empty_profile(config)
    assert config == previous


def test_requests_follow_returned_connection_and_never_a_cached_token(monkeypatch):
    observed = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return b"mock response"

    def open_request(request, timeout):
        observed.append((request.full_url, request.get_header("Authorization"),
                         json.loads(request.data), timeout))
        return Response()

    monkeypatch.setattr(wire_matrix, "opener", SimpleNamespace(open=open_request))
    for port, token in ((1234, "fake-initial"), (5678, "fake-replacement")):
        connection = SimpleNamespace(base_url=f"http://127.0.0.1:{port}", gateway_token=token)
        assert wire_matrix.request(connection, "/v1/responses", {"model": "完整模型"}) == (200, b"mock response")
    assert observed == [
        ("http://127.0.0.1:1234/v1/responses", "Bearer fake-initial", {"model": "完整模型"}, 12),
        ("http://127.0.0.1:5678/v1/responses", "Bearer fake-replacement", {"model": "完整模型"}, 12),
    ]


def test_installer_seam_only_selects_the_preverified_task_binary():
    binary = Path("/test-owned/diagnostic/cli-proxy-api")
    installer = wire_matrix.DiagnosticInstaller(binary)
    assert installer.resolve_engine_path() == binary
    assert installer.status()["install_dir"] == str(binary.parent)
    assert not hasattr(installer, "install")
