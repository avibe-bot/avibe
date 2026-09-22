"""Inspect shipped bytes, then import the installed wheel away from the source."""

from email.parser import BytesParser
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest


@pytest.fixture(autouse=True)
def isolated_artifact_home(tmp_path, monkeypatch):
    """Also isolate the sparse-checkout release job, which has no conftest."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    for key, suffix in {"HOME": "", "AVIBE_HOME": ".avibe", "XDG_CONFIG_HOME": ".config",
                        "XDG_DATA_HOME": ".local/share", "XDG_CACHE_HOME": ".cache",
                        "XDG_STATE_HOME": ".local/state", "CODEX_HOME": ".codex",
                        "CLAUDE_CONFIG_DIR": ".claude"}.items():
        monkeypatch.setenv(key, str(home / suffix))
    for key in list(os.environ):
        if key.startswith("AVIBE_CALLER_") or key in {"AVIBE_SESSION_ID", "AVIBE_RUN_ID", "VIBE_INTERNAL_DISPATCH_SOCKET"}:
            monkeypatch.delenv(key)
    monkeypatch.setattr(Path, "home", lambda: home)


def _artifact(name):
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for artifact acceptance")
    path = Path(value).resolve()
    assert path.is_file(), path
    return path


def _assert_members_and_metadata(members, metadata):
    # Product modules, native companions, assets, and skills must all be absent;
    # generic names such as pwaRouteMemory are not the retired product.
    forbidden = [name for name in members if any(
        part == "memory" or part == "avibe_memory" or part.startswith("memory_")
        or part.startswith("avibe_memory-") or part.startswith("avibe-memory")
        or part.startswith("use-avibe-memory")
        for part in Path(name).parts
    )]
    assert forbidden == []
    parsed = BytesParser().parsebytes(metadata)
    assert parsed["Name"] == "avibe-os"
    assert "memory" not in parsed.get_all("Provides-Extra", [])
    assert all("avibe-memory" not in item.lower().replace("_", "-")
               for item in parsed.get_all("Requires-Dist", []))
    for required in ("vibe/cli.py", "core/controller.py", "vibe/show_runtime_manifest.json",
                     "vibe/show_router.tsx", "vibe/ui/dist/index.html"):
        assert required in members, required


def test_built_wheel_and_sdist_contract():
    wheel = _artifact("AVIBE_CORE_WHEEL")
    sdist = _artifact("AVIBE_CORE_SDIST")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata, = [name for name in names if name.endswith(".dist-info/METADATA")]
        _assert_members_and_metadata(names, archive.read(metadata))
        manifest = archive.read("vibe/show_runtime_manifest.json")
    with tarfile.open(sdist) as archive:
        files = {member.name.partition("/")[2]: member for member in archive.getmembers() if member.isfile()}
        # Source assets are relocated into vibe/ui by the wheel builder.
        normalized = [name.replace("ui/dist/", "vibe/ui/dist/", 1) if name.startswith("ui/dist/") else name
                      for name in files]
        _assert_members_and_metadata(normalized, archive.extractfile(files["PKG-INFO"]).read())
        assert archive.extractfile(files["vibe/show_runtime_manifest.json"]).read() == manifest
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("manifest_contract", root / "scripts/show_runtime_manifest_asset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate_manifest_bytes(manifest)


def test_installed_wheel_imports_and_cli_without_memory(tmp_path):
    wheel = _artifact("AVIBE_CORE_WHEEL")
    environment = tmp_path / "installed"
    # Install the declared dependency closure in a fresh environment; neither
    # source imports nor an already installed optional companion can satisfy it.
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run([str(python), "-m", "pip", "install", str(wheel)],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    script = r'''
import importlib.abc, importlib.util, pathlib, sys
class NoMemory(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "avibe_memory" or fullname.startswith("avibe_memory."):
            raise ModuleNotFoundError("Memory is unavailable in this installation")
sys.meta_path.insert(0, NoMemory())
import vibe, vibe.cli, vibe.runtime, vibe.service_main, core.controller
assert pathlib.Path(vibe.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()), vibe.__file__
for name in ("vibe.memory_contract", "vibe.ui_memory_routes", "vibe.model_service", "core.memory"):
    assert importlib.util.find_spec(name) is None, name
from vibe.ui_server import app
from config.v2_config import V2Config
V2Config.from_payload({"version": "v2", "mode": "self_host", "runtime": {"default_cwd": str(pathlib.Path.cwd())}, "agents": {}}).save()
assert app.test_client().get("/api/memory/status", base_url="http://127.0.0.1:15131").status_code == 404
print("installed startup imports and removed HTTP contract passed")
'''
    probe = subprocess.run([str(python), "-I", "-c", script], cwd=tmp_path,
                           capture_output=True, text=True, timeout=60)
    assert probe.returncode == 0, probe.stdout + probe.stderr
    cli = subprocess.run([str(python), "-I", "-m", "vibe", "memory", "status"], cwd=tmp_path,
                         capture_output=True, text=True, timeout=30)
    assert cli.returncode == 2 and "invalid choice: 'memory'" in cli.stderr
