from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from vibe import desktop_backends


def _desktop_env(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / "runtime" / "1.0.0" / "digest"
    node = runtime / "tools" / ("node.exe" if os.name == "nt" else "node")
    npm_cli = runtime / "tools" / "npm" / "bin" / "npm-cli.js"
    node.parent.mkdir(parents=True)
    npm_cli.parent.mkdir(parents=True)
    node.write_bytes(b"MZ\0\0" if os.name == "nt" else b"\x7fELF")
    node.chmod(0o755)
    npm_cli.write_text("// npm\n", encoding="utf-8")
    return {
        "AVIBE_DESKTOP_RUNTIME_ROOT": str(runtime),
        "VIBE_SHOW_RUNTIME_NODE_BIN": str(node),
        "AVIBE_DESKTOP_NPM_CLI": str(npm_cli),
        "AVIBE_DESKTOP_BACKENDS_ROOT": str(tmp_path / "backends"),
        "PATH": str(tmp_path / "external-bin"),
        "NPM_CONFIG_REGISTRY": "https://attacker.invalid/",
        "npm_config_prefix": str(tmp_path / "system-prefix"),
        "NODE_OPTIONS": "--require attacker.js",
    }


def _write_native(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ\0\0" if os.name == "nt" else b"\x7fELF")
    path.chmod(0o755)


def _fake_npm_install(
    backend: str,
    *,
    codex_layout: str = "hoisted",
    calls: list[tuple[list[str], dict[str, str], Path]] | None = None,
):
    spec = desktop_backends.BACKEND_SPECS[backend]

    def fake_run(command, *, cwd, env, timeout_seconds):
        staging = Path(command[command.index("--prefix") + 1])
        package_dir = staging / "node_modules" / Path(*spec.package_path)
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(
            json.dumps({"name": spec.package, "version": "1.2.3"}),
            encoding="utf-8",
        )
        if backend == "codex":
            os_name, arch = desktop_backends._native_target()
            target_name = f"codex-{os_name}-{arch}"
            if codex_layout == "nested":
                target_root = package_dir / "node_modules" / "@openai" / target_name
            else:
                target_root = staging / "node_modules" / "@openai" / target_name
            executable = target_root / "vendor" / "target-triple" / "bin" / (
                "codex.exe" if os_name == "win32" else "codex"
            )
        elif backend == "claude":
            os_name, arch = desktop_backends._native_target()
            target_root = staging / "node_modules" / "@anthropic-ai" / f"claude-code-{os_name}-{arch}"
            executable = target_root / ("claude.exe" if os_name == "win32" else "claude")
        else:
            os_name, arch = desktop_backends._native_target()
            target_os = "windows" if os_name == "win32" else os_name
            target_root = staging / "node_modules" / f"opencode-{target_os}-{arch}"
            executable = target_root / "bin" / ("opencode.exe" if os_name == "win32" else "opencode")
        _write_native(executable)
        if calls is not None:
            calls.append((command, dict(env), cwd))
        return subprocess.CompletedProcess(command, 0, "installed", "")

    return fake_run


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_install_publishes_verified_native_backend(monkeypatch, tmp_path, backend):
    env = _desktop_env(tmp_path)
    calls: list[tuple[list[str], dict[str, str], Path]] = []
    monkeypatch.setattr(desktop_backends, "_run_command", _fake_npm_install(backend, calls=calls))
    monkeypatch.setattr(
        desktop_backends.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, f"{backend} 1.2.3", ""),
    )
    activated: list[str] = []

    result = desktop_backends.install_desktop_backend(
        backend,
        base_env=env,
        activate=lambda path: activated.append(path),
    )

    assert result.version == "1.2.3"
    assert result.path == activated[0]
    assert desktop_backends.resolve_published_desktop_backend(backend, env) == result.path
    descriptor = json.loads((Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / backend / "current.json").read_text())
    assert descriptor["package"] == desktop_backends.BACKEND_SPECS[backend].package
    assert not Path(descriptor["executable"]).is_absolute()
    assert ".staging-" not in descriptor["executable"]

    command, install_env, cwd = calls[0]
    assert command[:2] == [env["VIBE_SHOW_RUNTIME_NODE_BIN"], env["AVIBE_DESKTOP_NPM_CLI"]]
    assert command[-1] == desktop_backends.BACKEND_SPECS[backend].package
    assert "--ignore-scripts" in command
    assert f"--registry={desktop_backends.NPM_REGISTRY}" in command
    assert cwd == Path(install_env["NPM_CONFIG_PREFIX"])
    assert install_env["NPM_CONFIG_REGISTRY"] == desktop_backends.NPM_REGISTRY
    assert install_env["NPM_CONFIG_PREFIX"] == command[command.index("--prefix") + 1]
    assert install_env["NPM_CONFIG_CACHE"] == str(cwd / ".npm-cache")
    assert install_env["NPM_CONFIG_USERCONFIG"].startswith(install_env["NPM_CONFIG_PREFIX"])
    assert "npm_config_prefix" not in install_env
    assert "NODE_OPTIONS" not in install_env


def test_codex_accepts_nested_target_package(monkeypatch, tmp_path):
    env = _desktop_env(tmp_path)
    monkeypatch.setattr(
        desktop_backends,
        "_run_command",
        _fake_npm_install("codex", codex_layout="nested"),
    )
    monkeypatch.setattr(
        desktop_backends.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "codex-cli 1.2.3", ""),
    )

    result = desktop_backends.install_desktop_backend("codex", base_env=env)

    assert Path(result.path).name == ("codex.exe" if os.name == "nt" else "codex")


def test_failed_install_keeps_current_descriptor_and_removes_staging(monkeypatch, tmp_path):
    env = _desktop_env(tmp_path)
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "claude"
    backend_root.mkdir(parents=True)
    previous = b'{"existing":true}\n'
    (backend_root / "current.json").write_bytes(previous)
    monkeypatch.setattr(
        desktop_backends,
        "_run_command",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "npm failed"),
    )

    with pytest.raises(desktop_backends.DesktopBackendError) as raised:
        desktop_backends.install_desktop_backend("claude", base_env=env)

    assert raised.value.code == "npm_install_failed"
    assert (backend_root / "current.json").read_bytes() == previous
    assert list(backend_root.glob(".staging-*")) == []
    assert list(backend_root.glob("releases/*")) == []


def test_descriptor_publication_failure_does_not_activate_config(monkeypatch, tmp_path):
    env = _desktop_env(tmp_path)
    monkeypatch.setattr(desktop_backends, "_run_command", _fake_npm_install("opencode"))
    monkeypatch.setattr(
        desktop_backends.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "opencode 1.2.3", ""),
    )
    state = {"path": "opencode"}

    def activate(path):
        state["path"] = path

    monkeypatch.setattr(
        desktop_backends,
        "_write_current_descriptor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(desktop_backends.DesktopBackendError):
        desktop_backends.install_desktop_backend("opencode", base_env=env, activate=activate)

    assert state["path"] == "opencode"


def test_descriptor_is_published_before_activation_and_restored_on_failure(monkeypatch, tmp_path):
    env = _desktop_env(tmp_path)
    root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"])
    backend_root = root / "claude"
    old_executable = backend_root / "releases" / "old" / "claude"
    _write_native(old_executable)
    old_descriptor = {
        "schema_version": desktop_backends.CURRENT_DESCRIPTOR_SCHEMA_VERSION,
        "backend": "claude",
        "package": "@anthropic-ai/claude-code",
        "version": "1.0.0",
        "executable": old_executable.relative_to(root).as_posix(),
    }
    backend_root.mkdir(parents=True, exist_ok=True)
    (backend_root / "current.json").write_text(json.dumps(old_descriptor), encoding="utf-8")
    monkeypatch.setattr(desktop_backends, "_run_command", _fake_npm_install("claude"))
    monkeypatch.setattr(
        desktop_backends.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "claude 1.2.3", ""),
    )

    def fail_activation(new_path):
        assert desktop_backends.resolve_published_desktop_backend("claude", env) == new_path
        raise OSError("config write failed")

    with pytest.raises(desktop_backends.DesktopBackendError):
        desktop_backends.install_desktop_backend("claude", base_env=env, activate=fail_activation)

    assert desktop_backends.resolve_published_desktop_backend("claude", env) == str(old_executable)


def test_resolver_rejects_descriptor_traversal_and_non_native_file(tmp_path):
    env = _desktop_env(tmp_path)
    backend_root = Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"]) / "codex"
    backend_root.mkdir(parents=True)
    outside = tmp_path / "outside" / "codex"
    outside.parent.mkdir()
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o755)

    base = {
        "schema_version": desktop_backends.CURRENT_DESCRIPTOR_SCHEMA_VERSION,
        "backend": "codex",
        "package": "@openai/codex",
        "version": "1.2.3",
    }
    (backend_root / "current.json").write_text(
        json.dumps({**base, "executable": "../outside/codex"}),
        encoding="utf-8",
    )
    assert desktop_backends.resolve_published_desktop_backend("codex", env) is None

    local = backend_root / "releases" / "one" / "codex"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n", encoding="utf-8")
    local.chmod(0o755)
    (backend_root / "current.json").write_text(
        json.dumps(
            {
                **base,
                "executable": local.relative_to(Path(env["AVIBE_DESKTOP_BACKENDS_ROOT"])).as_posix(),
            }
        ),
        encoding="utf-8",
    )
    assert desktop_backends.resolve_published_desktop_backend("codex", env) is None
