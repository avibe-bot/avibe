"""Frozen npm-provider installs: no resolver, scripts, or project-file copying."""

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.show_runtime import ShowRuntimeManager
from tests.show_runtime_npm_helpers import write_npm_runtime_project


def _manager(tmp_path, monkeypatch, project=None, *, package_spec=None):
    monkeypatch.delenv("VIBE_SHOW_RUNTIME_NPM_PROJECT", raising=False)
    monkeypatch.delenv("VIBE_SHOW_RUNTIME_PACKAGE_SPEC", raising=False)
    if project is not None:
        monkeypatch.setenv("VIBE_SHOW_RUNTIME_NPM_PROJECT", str(project))
    return ShowRuntimeManager(
        workspace_root=tmp_path / "show",
        runtime_dir=tmp_path / "runtime",
        runtime_source="npm",
        package_spec=package_spec,
    )


def _install_fake_binary(staging):
    binary = ShowRuntimeManager._npm_bin_path(staging)
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.mark.parametrize("lock_version", [2, 3])
@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("package_spec", [None, "1.0.0", "@avibe/show-runtime@1.0.0"])
def test_npm_provider_installs_exact_project_without_scripts_or_resolution(
    tmp_path, monkeypatch, offline, package_spec, lock_version,
):
    project = write_npm_runtime_project(tmp_path / "reviewed", lock_version=lock_version)
    (project / ".npmrc").write_text("ignore-scripts=false\n", encoding="utf-8")
    (project / "not-an-input").write_text("do not copy", encoding="utf-8")
    source_bytes = {name: (project / name).read_bytes() for name in ("package.json", "package-lock.json")}
    manager = _manager(tmp_path, monkeypatch, project, package_spec=package_spec)
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda name: [name])
    calls = []

    def install(argv, **kwargs):
        calls.append(argv)
        assert argv[:2] == ["npm", "ci"]
        for flag in ("--ignore-scripts", "--omit=dev", "--include=optional", "--global=false", "--no-audit", "--no-fund"):
            assert flag in argv
        assert ("--offline" in argv) is offline
        staging = Path(argv[argv.index("--prefix") + 1])
        assert kwargs["cwd"] == staging
        assert {p.name: p.read_bytes() for p in staging.iterdir()} == source_bytes
        _install_fake_binary(staging)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("core.show_runtime.subprocess.run", install)
    result = manager._install_managed_runtime_locked(force=True, offline=offline)

    assert result.command is not None
    assert len(calls) == 1
    installed = manager._package_install_dir()
    metadata = json.loads((installed / ".vibe-show-runtime.json").read_text())
    assert metadata["lock_sha256"] == hashlib.sha256(source_bytes["package-lock.json"]).hexdigest()
    assert metadata["package_sha256"] == hashlib.sha256(source_bytes["package.json"]).hexdigest()
    assert {name: (project / name).read_bytes() for name in source_bytes} == source_bytes


@pytest.mark.parametrize("invalid", [
    "no_project", "no_lock", "bad_json", "lock_v1", "no_root", "no_runtime", "no_integrity", "weak_integrity",
    "malformed_integrity", "file", "git", "http", "credentials", "workspace", "link", "bundled", "override",
    "directory_record", "path_traversal", "local_dependency", "alias", "spec_mismatch", "legacy_file", "legacy_requires",
])
def test_npm_provider_rejects_unlocked_or_external_sources_without_spawning(tmp_path, monkeypatch, invalid):
    project = write_npm_runtime_project(tmp_path / "reviewed")
    package = json.loads((project / "package.json").read_text())
    lock = json.loads((project / "package-lock.json").read_text())
    entry = lock["packages"]["node_modules/@avibe/show-runtime"]
    reason = "runtime_npm_project_invalid"
    if invalid == "no_project":
        project = None
        reason = "runtime_npm_project_missing"
    elif invalid == "no_lock":
        (project / "package-lock.json").unlink()
    elif invalid == "bad_json":
        (project / "package-lock.json").write_text("{", encoding="utf-8")
    else:
        if invalid == "lock_v1":
            lock["lockfileVersion"] = 1
        elif invalid == "no_root":
            del lock["packages"][""]
        elif invalid == "no_runtime":
            del package["dependencies"]["@avibe/show-runtime"]
        elif invalid == "no_integrity":
            del entry["integrity"]
        elif invalid == "weak_integrity":
            entry["integrity"] = "sha1-AAAAAAAAAAAAAAAAAAAAAAAAAAA="
        elif invalid == "malformed_integrity":
            entry["integrity"] = "sha512-aW52YWxpZA=="
        elif invalid in {"file", "git", "http", "credentials"}:
            entry["resolved"] = {
                "file": "file:../arbitrary-source",
                "git": "git+https://example.invalid/runtime#abc",
                "http": "http://example.invalid/runtime.tgz",
                "credentials": "https://username:secret@example.invalid/runtime.tgz",
            }[invalid]
        elif invalid == "workspace":
            package["workspaces"] = ["packages/*"]
        elif invalid == "link":
            entry["link"] = True
        elif invalid == "bundled":
            entry["inBundle"] = True
        elif invalid == "override":
            package["overrides"] = {"@avibe/show-runtime": "file:../runtime"}
        elif invalid == "directory_record":
            lock["packages"]["packages/runtime"] = entry.copy()
        elif invalid == "path_traversal":
            lock["packages"]["node_modules/../../runtime"] = entry.copy()
        elif invalid in {"local_dependency", "alias"}:
            entry["dependencies"] = {"extra": "file:../extra" if invalid == "local_dependency" else "npm:other@1.0.0"}
        elif invalid in {"legacy_file", "legacy_requires"}:
            legacy = entry.copy()
            if invalid == "legacy_file":
                legacy["resolved"] = "file:../unreviewed"
            else:
                legacy["requires"] = {"extra": "git+https://example.invalid/extra"}
            lock["lockfileVersion"] = 2
            lock["dependencies"] = {"@avibe/show-runtime": legacy}
        elif invalid == "spec_mismatch":
            reason = "runtime_npm_spec_mismatch"
        (project / "package.json").write_text(json.dumps(package), encoding="utf-8")
        (project / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    manager = _manager(
        tmp_path, monkeypatch, project,
        package_spec="@avibe/show-runtime@2.0.0" if invalid == "spec_mismatch" else None,
    )
    # Deliberately don't provide npm: configuration errors must precede command lookup.
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda _name: pytest.fail("npm must not resolve"))
    monkeypatch.setattr("core.show_runtime.subprocess.run", lambda *_a, **_kw: pytest.fail("npm must not spawn"))

    assert manager._install_npm_runtime(force=True) is None
    assert manager._install_reason == reason
    assert "VIBE_SHOW_RUNTIME_" in manager.install_log_path.read_text()
    assert not (manager.runtime_dir / "package" / "current.json").exists()


@pytest.mark.parametrize("failure", ["missing", "invalid", "mismatch"])
def test_npm_project_configuration_failure_preserves_retained_runtime(tmp_path, monkeypatch, failure):
    project = None if failure == "missing" else write_npm_runtime_project(tmp_path / "project")
    if failure == "invalid":
        (project / "package-lock.json").unlink()
    manager = _manager(tmp_path, monkeypatch, project, package_spec="2.0.0" if failure == "mismatch" else None)
    monkeypatch.setattr("core.show_runtime._resolve_command", lambda _name: None)
    monkeypatch.setattr("core.show_runtime.subprocess.run", lambda *_a, **_kw: pytest.fail("npm must not spawn"))

    unavailable = manager.prepare(force=True)
    assert unavailable["ok"] is False
    assert unavailable["install"]["failure_class"] == "configured"
    assert unavailable["install"]["recovery_action"] == "change_setting"
    binary = _install_fake_binary(manager.runtime_dir / "package")
    failed = manager.prepare(force=True)
    assert failed["ok"] is False
    assert failed["reason"] == unavailable["reason"]
    assert failed["install"]["state"] == "installed"
    assert binary.exists()
    assert manager.prepare(offline=True)["ok"] is True


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm required for frozen installer integration")
def test_real_npm_ci_uses_cached_integrity_and_disables_lifecycle_scripts(tmp_path, monkeypatch):
    # This tests the real npm boundary missed by argv assertions: cached locked
    # bytes install offline, neither root nor dependency scripts execute, and
    # manifest/lock drift fails without changing the active runtime or inputs.
    from core.show_runtime import _resolve_command

    npm = _resolve_command("npm")
    assert npm is not None
    lifecycle = 'node -e "require(\'fs\').writeFileSync(\'lifecycle-ran\', \'unsafe\')"'
    payloads = {
        "package/package.json": json.dumps({
            "name": "@avibe/show-runtime", "version": "1.0.0",
            "bin": {"avibe-show-runtime": "cli.js"},
            "scripts": {"preinstall": lifecycle, "postinstall": lifecycle},
        }).encode(),
        "package/cli.js": b"#!/usr/bin/env node\nconsole.log('fixture runtime');\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if name.endswith(".js") else 0o644
            archive.addfile(info, io.BytesIO(content))
    tarball = tmp_path / "runtime.tgz"
    tarball.write_bytes(buffer.getvalue())
    project = write_npm_runtime_project(tmp_path / "project", tarball=buffer.getvalue())
    package_path = project / "package.json"
    package = json.loads(package_path.read_text())
    package["scripts"] = {"preinstall": lifecycle, "postinstall": lifecycle, "prepare": lifecycle}
    package_path.write_text(json.dumps(package), encoding="utf-8")
    monkeypatch.setenv("npm_config_cache", str(tmp_path / "npm-cache"))
    monkeypatch.setenv("npm_config_ignore_scripts", "false")
    # cache add reads this local fixture only; the provider itself accepts only
    # HTTPS URLs and consumes the cached blob by its locked SRI, never by path.
    seeded = subprocess.run(
        [*npm, "cache", "add", str(tarball), "--ignore-scripts", "--offline", "--no-audit", "--no-fund"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
    )
    assert seeded.returncode == 0, seeded.stderr
    manager = _manager(tmp_path, monkeypatch, project)
    first = manager.prepare(force=True, offline=True)
    assert first["ok"] is True, manager.install_log_path.read_text()
    installed = manager._package_install_dir()
    assert not list(tmp_path.rglob("lifecycle-ran"))
    assert (installed / "node_modules" / "@avibe" / "show-runtime" / "cli.js").read_bytes() == payloads["package/cli.js"]
    lock_bytes = (project / "package-lock.json").read_bytes()
    package["dependencies"]["@avibe/show-runtime"] = "2.0.0"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    failed = manager.prepare(force=True, offline=True)
    assert failed["ok"] is False
    assert failed["reason"] == "runtime_install_failed"
    assert manager._package_install_dir() == installed
    assert (project / "package-lock.json").read_bytes() == lock_bytes
    assert not list(tmp_path.rglob("lifecycle-ran"))
