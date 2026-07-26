from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "desktop" / "scripts" / "build-runtime-bundle.py"
SPEC = importlib.util.spec_from_file_location("desktop_runtime_bundle_builder", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_private_probe_environment_does_not_inherit_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "must-not-cross-boundary")
    monkeypatch.setenv("CODEX_HOME", "/real/codex-home")
    monkeypatch.setenv("HOME", "/real/home")

    probe_home = tmp_path / "probe"
    node = tmp_path / "payload" / "tools" / "bin" / "node"
    codex = tmp_path / "payload" / "tools" / "bin" / "codex"
    environment = builder.private_probe_environment(probe_home, node, codex)

    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_AUTH_TOKEN" not in environment
    assert environment["HOME"] == str(probe_home)
    assert environment["CODEX_HOME"] == str(probe_home / "codex")
    assert environment["PATH"].split(builder.os.pathsep)[0] == str(node.parent)
    assert environment["PATH"].split(builder.os.pathsep)[1] == str(
        codex.parent.parent / "codex-path"
    )


def test_copy_codex_runtime_preserves_the_complete_target_package(tmp_path):
    source = tmp_path / "package" / "vendor" / "target"
    (source / "bin").mkdir(parents=True)
    (source / "codex-path").mkdir()
    (source / "codex-resources" / "zsh" / "bin").mkdir(parents=True)
    (source / "bin" / "codex").write_bytes(b"codex")
    (source / "bin" / "codex-code-mode-host").write_bytes(b"host")
    (source / "codex-path" / "rg").write_bytes(b"rg")
    (source / "codex-resources" / "zsh" / "bin" / "zsh").write_bytes(b"zsh")
    (source / "codex-package.json").write_text("{}", encoding="utf-8")
    destination = tmp_path / "payload" / "tools"
    (destination / "bin").mkdir(parents=True)
    (destination / "bin" / "node").write_bytes(b"node")

    builder.copy_codex_runtime(source, destination)

    assert (destination / "bin" / "node").read_bytes() == b"node"
    assert (destination / "bin" / "codex").read_bytes() == b"codex"
    assert (destination / "bin" / "codex-code-mode-host").read_bytes() == b"host"
    assert (destination / "codex-path" / "rg").read_bytes() == b"rg"
    assert (destination / "codex-resources" / "zsh" / "bin" / "zsh").read_bytes() == b"zsh"


def test_npm_executable_resolves_the_windows_command_shim(monkeypatch):
    lookups = []
    monkeypatch.setattr(
        builder.shutil,
        "which",
        lambda name: lookups.append(name) or (r"C:\node\npm.cmd" if name == "npm.cmd" else None),
    )

    assert builder.npm_executable(platform_name="nt") == r"C:\node\npm.cmd"
    assert lookups == ["npm.cmd"]


def test_existing_show_manifest_must_match_the_pinned_digest(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    manifest = repository / "vibe" / "show_runtime_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("tampered", encoding="utf-8")
    monkeypatch.setattr(builder, "REPO_ROOT", repository)

    with pytest.raises(SystemExit, match="does not match"):
        builder.ensure_show_runtime_manifest(
            {"show_runtime_manifest": {"sha256": "0" * 64}},
            tmp_path / "cache",
        )


def test_runtime_zip_uses_portable_relative_path_order(tmp_path):
    payload = tmp_path / "payload"
    (payload / "z").mkdir(parents=True)
    (payload / "A").mkdir()
    (payload / "z" / "entry").write_bytes(b"z")
    (payload / "A" / "entry").write_bytes(b"a")
    archive = tmp_path / "runtime.zip"

    builder.create_runtime_zip(payload, archive)

    with zipfile.ZipFile(archive) as runtime:
        assert runtime.namelist() == ["A/entry", "z/entry"]
