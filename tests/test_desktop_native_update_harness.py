"""Hermetic pre-Cargo regressions for the hosted native acceptance harness.

These tests never launch a native app, Controller, installer or network request.
The hosted workflow remains the authority for actual startup and replacement.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
from pathlib import Path
import shutil
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "desktop/src-tauri/tests/native_update"


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(HARNESS))
    modules = {}
    for name in ("build", "runtime"):
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
