"""Hermetic pre-Cargo regressions for the hosted native acceptance harness.

These tests never launch a native app, Controller, installer or network request.
The hosted workflow remains the authority for actual startup and replacement.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from urllib.error import HTTPError
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "desktop/src-tauri/tests/native_update"


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(HARNESS))
    modules = {}
    for name in ("build", "runtime", "guard"):
        spec = importlib.util.spec_from_file_location(f"native_update_{name}", HARNESS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[name] = module
    return modules


def test_real_fixture_adaptation_keeps_installer_and_owner_sources(harness, tmp_path):
    paths = [
        "desktop/src-tauri/src/lib.rs", "desktop/src-tauri/src/main.rs",
        "desktop/src-tauri/src/updater.rs", "desktop/src-tauri/src/update_install.rs",
        "desktop/src-tauri/src/update_windows.ps1", "desktop/runtime-host/src/update.rs",
        "desktop/src-tauri/src/updater/activity.rs", "core/update_activity.py",
        "core/internal_server.py", "vibe/internal_client.py",
        "desktop/src-tauri/examples/support/fixture_native_update.rs",
        "desktop/src-tauri/tests/native_update/fixture-installer.nsi",
    ]
    for name in paths:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    record = harness["build"].adapt(tmp_path)
    assert record["callback_variant"] == "skip-aware-five-arguments"
    assert record["presentation_lifecycle"]["production_present"] == "present_guarded"
    for name, digest in record["protected_sources"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
        assert (tmp_path / name).read_bytes() == (ROOT / name).read_bytes()
    changed = {name for name in paths if (tmp_path / name).read_bytes() != (ROOT / name).read_bytes()}
    assert changed == {"desktop/src-tauri/src/lib.rs", "desktop/src-tauri/src/main.rs",
                       "desktop/src-tauri/src/updater.rs"}


@pytest.mark.parametrize("missing", [
    'app.manage(pet::Pet::new(data.join("pet.json")));',
    ".plugin(tauri_plugin_global_shortcut::Builder::new()",
])
def test_fixture_requires_real_auxiliary_state_before_client_attach(harness, missing):
    library = (ROOT / "desktop/src-tauri/src/lib.rs").read_text()
    fixture = (ROOT / "desktop/src-tauri/examples/support/fixture_native_update.rs").read_text()
    validate = harness["build"].validate_presentation_lifecycle
    validate(library, fixture)
    with pytest.raises(RuntimeError, match="lifecycle missing"):
        validate(library, fixture.replace(missing, ""))
    pet = 'app.manage(pet::Pet::new(data.join("pet.json")));'
    with pytest.raises(RuntimeError, match="lifecycle order"):
        validate(library, fixture.replace(pet, "") + "\n" + pet)


def test_pinned_portable_nsis_is_verified_before_extraction(harness, monkeypatch, tmp_path):
    build = harness["build"]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("nsis-3.11/makensis.exe", b"test compiler, never executed")
    payload = archive.getvalue()
    requested = []

    def download(url, *, timeout):
        requested.append((url, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(build.urllib.request, "urlopen", download)
    # A wrong digest must not create or extract anything.
    with pytest.raises(RuntimeError, match="digest mismatch"):
        build.provision_nsis(tmp_path)
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(build, "NSIS_SHA256", hashlib.sha256(payload).hexdigest())
    compiler = build.provision_nsis(tmp_path)
    assert compiler == tmp_path / "tools/nsis-3.11/makensis.exe"
    assert compiler.read_bytes() == b"test compiler, never executed"
    assert requested == [(build.NSIS_URL, 60)] * 2


def test_controller_constructor_precedes_async_lifecycle(harness, monkeypatch, tmp_path):
    runtime = harness["runtime"]
    calls = []
    controller = object()

    class Listener:
        def close(self):
            calls.append("close")

    listener = Listener()

    async def reconciliation():
        calls.append("constructor_reconciliation")

    def construct(root):
        assert root == tmp_path
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        # This matches Controller._init_model_hub's synchronous lifecycle.
        asyncio.run(reconciliation())
        return controller, listener

    async def serve(root, owner, socket):
        assert (root, owner, socket) == (tmp_path, controller, listener)
        assert asyncio.get_running_loop().is_running()
        calls.append("serve")

    monkeypatch.setattr(runtime, "hosted", lambda root: root)
    monkeypatch.setattr(runtime, "deny_external", lambda root: calls.append("deny_external"))
    monkeypatch.setattr(runtime, "create_controller", construct)
    monkeypatch.setattr(runtime, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["runtime.py", "--root", str(tmp_path)])
    monkeypatch.setenv("VIBE_INTERNAL_DISPATCH_SOCKET", "isolated-test")
    runtime.main()
    assert calls == ["deny_external", "constructor_reconciliation", "serve", "close"]


@pytest.fixture
def admission(harness, monkeypatch, tmp_path):
    guard = harness["guard"]
    event = tmp_path / "event.json"
    source = "1" * 40
    event.write_text(json.dumps({"action": "labeled", "label": {"name": "native-update-acceptance"},
        "pull_request": {"base": {"ref": "master"},
                         "head": {"sha": source, "repo": {"full_name": guard.REPOSITORY}}}}))
    for key, value in {
        "GITHUB_REPOSITORY": guard.REPOSITORY, "ACCEPTANCE_SOURCE_SHA": source,
        "GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "pull_request",
        "ACCEPTANCE_EVENT_SHA": "2" * 40, "ACCEPTANCE_WORKFLOW_SHA": "3" * 40,
        "ACCEPTANCE_WORKFLOW_REF": "fixture", "GH_TOKEN": "test-only-workflow-token",
    }.items():
        monkeypatch.setenv(key, value)

    def git(*args, **kwargs):
        assert "GH_TOKEN" not in os.environ
        return source + "\n"

    monkeypatch.setattr(guard.subprocess, "check_output", git)
    return guard


def test_admission_scopes_token_to_fixed_origin_get_without_evidence_leak(admission, monkeypatch):
    guard = admission
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append(request.full_url)
            assert request.full_url.startswith(
                "https://api.github.com/repos/avibe-bot/avibe/actions/workflows/desktop-release.yml/runs?")
            assert request.get_method() == "GET"
            assert request.get_header("Authorization") == "Bearer test-only-workflow-token"
            assert timeout == 20
            return io.BytesIO(b'{"total_count":0,"workflow_runs":[]}')

    def opener(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], guard.NoRedirect)
        return Opener()

    monkeypatch.setattr(guard, "build_opener", opener)
    result = guard.check()
    assert len(calls) == len(guard.ACTIVE)
    assert all(count == 0 for count in result["active_counts"].values())
    assert "test-only-workflow-token" not in json.dumps(result)
    assert "GH_TOKEN" not in os.environ
    with pytest.raises(RuntimeError, match="redirect refused"):
        guard.NoRedirect().redirect_request(None, None, None, None, None, None)


@pytest.mark.parametrize("failure", ["active", "malformed", "rate_limit", "missing_token"])
def test_admission_still_fails_closed(admission, monkeypatch, failure):
    guard = admission

    class Opener:
        def open(self, request, *, timeout):
            if failure == "rate_limit":
                raise HTTPError(request.full_url, 403, "rate limit exceeded", {}, None)
            data = ({"total_count": 1, "workflow_runs": [{}]} if failure == "active"
                    else {"total_count": "0", "workflow_runs": []})
            return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(guard, "build_opener", lambda *args: Opener())
    if failure == "missing_token":
        monkeypatch.delenv("GH_TOKEN")
    with pytest.raises((RuntimeError, HTTPError)) as error:
        guard.check()
    assert "test-only-workflow-token" not in str(error.value)
    assert "GH_TOKEN" not in os.environ


def test_workflow_token_is_read_only_and_guard_step_scoped():
    workflow = yaml.safe_load((ROOT / ".github/workflows/desktop-native-update-acceptance.yml").read_text())
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert "GH_TOKEN" not in workflow.get("env", {})
    guards = 0
    for job in workflow["jobs"].values():
        assert "GH_TOKEN" not in job.get("env", {})
        for step in job["steps"]:
            if "native_update/guard.py" in step.get("run", ""):
                assert step["env"] == {"GH_TOKEN": "${{ github.token }}"}
                guards += 1
            else:
                assert "GH_TOKEN" not in step.get("env", {})
    assert guards == 3


@pytest.mark.parametrize("method,field", [("start", "data"), ("success", "executable")])
def test_observer_compares_real_file_identity_across_native_path_spellings(tmp_path, method, field):
    # Execute the actual observer assertion, without starting its processes.
    tree = ast.parse((HARNESS / "run.py").read_text())
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == method)
    assertion = next(node for node in ast.walk(function) if isinstance(node, ast.Assert)
                     and isinstance(node.test, ast.Call) and isinstance(node.test.func, ast.Attribute)
                     and node.test.func.attr == "samefile")
    code = compile(ast.Module(body=[assertion], type_ignores=[]), "observer-path-assertion", "exec")
    root = tmp_path / "case"
    (root / "native-state").mkdir(parents=True)
    binary = root / "installation/avibe-desktop.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"fixture")
    if os.name == "nt":
        alias = Path("\\\\?\\" + str(root.resolve()))
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
    relative = "native-state" if field == "data" else "installation/avibe-desktop.exe"
    namespace = {"Path": Path, "self": SimpleNamespace(root=root, binary=binary),
                 "initial": {field: str(alias / relative)}, "target": {field: str(alias / relative)}}
    exec(code, namespace)
    other = tmp_path / "different" / relative
    other.parent.mkdir(parents=True)
    if field == "data":
        other.mkdir()
    else:
        other.write_bytes(b"fixture")  # Same bytes/name are not the same installed file.
    namespace["initial"][field] = namespace["target"][field] = str(other)
    with pytest.raises(AssertionError):
        exec(code, namespace)
    namespace["initial"][field] = namespace["target"][field] = str(tmp_path / "missing")
    with pytest.raises(FileNotFoundError):
        exec(code, namespace)
