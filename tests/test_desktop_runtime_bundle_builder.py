from __future__ import annotations

import importlib.util
import json
import os
import subprocess
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
    npm_cli = tmp_path / "payload" / "tools" / "npm" / "bin" / "npm-cli.js"
    environment = builder.private_probe_environment(probe_home, node, npm_cli)

    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_AUTH_TOKEN" not in environment
    assert environment["HOME"] == str(probe_home)
    assert environment["CODEX_HOME"] == str(probe_home / "codex")
    assert environment["PATH"].split(builder.os.pathsep)[0] == str(node.parent)
    assert environment["AVIBE_DESKTOP_NPM_CLI"] == str(npm_cli)
    assert environment["AVIBE_DESKTOP_BACKENDS_ROOT"] == str(probe_home / "backends")


def test_runtime_sources_schema_two_contains_only_node_and_npm_tools():
    sources = json.loads((SCRIPT.parents[1] / "runtime-sources.json").read_text(encoding="utf-8"))

    assert sources["schema_version"] == 2
    assert sources["npm_version"] == "10.9.8"
    assert set(sources["sdist_build_allowlist"]) == {"claude-agent-sdk", "http-ece"}
    assert "codex_version" not in sources
    assert "codex_license" not in sources
    for target, config in sources["targets"].items():
        expected_source = "node_modules/npm" if "windows" in target else "lib/node_modules/npm"
        assert config["npm_source"] == expected_source
        assert config["npm_entrypoint"] == "tools/npm/bin/npm-cli.js"
        assert not any(key.startswith("codex") for key in config)


def test_python_runtime_verification_checks_bundled_claude_paths(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    private_python = tmp_path / "python" / "bin" / "python3"

    builder.verify_python_runtime_excludes_agent_backends(private_python, tmp_path)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:3] == [str(private_python), "-I", "-c"]
    assert 'for name in ("claude", "claude.exe")' in command[3]
    assert kwargs == {"cwd": tmp_path, "check": True}


def test_node_toolchain_normalizes_bundled_npm_without_command_shims(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "LICENSE").write_text("avibe license", encoding="utf-8")
    node_root = tmp_path / "node-distribution"
    (node_root / "bin").mkdir(parents=True)
    (node_root / "bin" / "node").write_bytes(b"node")
    npm = node_root / "lib" / "node_modules" / "npm"
    (npm / "bin").mkdir(parents=True)
    (npm / "bin" / "npm-cli.js").write_text("npm cli", encoding="utf-8")
    (npm / "lib").mkdir()
    (npm / "lib" / "npm.js").write_text("npm library", encoding="utf-8")
    semver = npm / "node_modules" / "semver" / "bin" / "semver.js"
    semver.parent.mkdir(parents=True)
    semver.write_text("semver cli", encoding="utf-8")
    bin_dir = npm / "node_modules" / ".bin"
    bin_dir.mkdir()
    if os.name != "nt":
        (bin_dir / "semver").symlink_to("../semver/bin/semver.js")
    (npm / "package.json").write_text('{"version":"10.9.8"}', encoding="utf-8")
    (npm / "LICENSE").write_text("npm license", encoding="utf-8")
    (node_root / "LICENSE").write_text("node license", encoding="utf-8")
    monkeypatch.setattr(builder, "REPO_ROOT", repository)
    monkeypatch.setattr(builder, "extract_source", lambda _archive, _destination: node_root)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="10.9.8\n", stderr="")

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    payload = tmp_path / "payload"
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    builder.install_node_toolchain(
        {
            "os": "macos",
            "node_source": "bin/node",
            "node_entrypoint": "tools/bin/node",
            "npm_source": "lib/node_modules/npm",
            "npm_entrypoint": "tools/npm/bin/npm-cli.js",
        },
        tmp_path / "node.tar.gz",
        payload,
        work_dir,
        "10.9.8",
    )

    assert {path.name for path in (payload / "tools").iterdir()} == {"bin", "npm"}
    assert {path.name for path in (payload / "tools" / "bin").iterdir()} == {"node"}
    assert (payload / "tools" / "npm" / "lib" / "npm.js").read_text() == "npm library"
    if os.name != "nt":
        copied_semver = payload / "tools" / "npm" / "node_modules" / ".bin" / "semver"
        assert copied_semver.read_text() == "semver cli"
        assert not copied_semver.is_symlink()
    assert (payload / "licenses" / "npm-LICENSE").read_text() == "npm license"
    assert calls == [
        [
            str(payload / "tools" / "bin" / "node"),
            str(payload / "tools" / "npm" / "bin" / "npm-cli.js"),
            "--version",
        ]
    ]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires extra Windows privileges")
def test_node_toolchain_rejects_npm_links_outside_package(monkeypatch, tmp_path):
    node_root = tmp_path / "node-distribution"
    (node_root / "bin").mkdir(parents=True)
    (node_root / "bin" / "node").write_bytes(b"node")
    npm = node_root / "lib" / "node_modules" / "npm"
    npm.mkdir(parents=True)
    outside = node_root / "outside"
    outside.write_text("outside", encoding="utf-8")
    (npm / "link").symlink_to(outside)
    monkeypatch.setattr(builder, "extract_source", lambda _archive, _destination: node_root)

    with pytest.raises(SystemExit, match="link escapes its package"):
        builder.install_node_toolchain(
            {
                "os": "macos",
                "node_source": "bin/node",
                "node_entrypoint": "tools/bin/node",
                "npm_source": "lib/node_modules/npm",
                "npm_entrypoint": "tools/npm/bin/npm-cli.js",
            },
            tmp_path / "node.tar.gz",
            tmp_path / "payload",
            tmp_path,
            "10.9.8",
        )


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


def test_runtime_zip_is_byte_for_byte_deterministic(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "runtime").write_bytes(b"immutable runtime")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    first_metadata = builder.create_runtime_zip(payload, first)
    second_metadata = builder.create_runtime_zip(payload, second)

    assert first_metadata == second_metadata
    assert first.read_bytes() == second.read_bytes()
