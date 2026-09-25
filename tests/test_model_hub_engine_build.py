from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import build_model_hub_engine as builder
from scripts import model_hub_engine_release_guard as guard


def _manifest(tmp_path: Path, *, source_sha: str = "c" * 40) -> Path:
    version = "v7.3.16"
    release_tag = "model-hub-engine-v7.3.16-3"
    assets = [
        {
            "platform": platform,
            "url": (
                f"{guard.OWNED_RELEASE_ROOT}/{release_tag}/"
                f"CLIProxyAPI_7.3.16_{asset_arch}.tar.gz"
            ),
            "size_bytes": 1,
            "sha256": "0" * 64,
            "binary_sha256": "0" * 64,
            "bin_path": "cli-proxy-api",
        }
        for platform, (_goos, _goarch, asset_arch) in builder.TARGETS.items()
    ]
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "cliproxyapi",
                "version": version,
                "source": "router-for-me/CLIProxyAPI",
                "source_url": f"https://github.com/router-for-me/CLIProxyAPI/tree/{source_sha}",
                "source_sha": source_sha,
                "release_tag": version,
                "asset_release_tag": release_tag,
                "license": "MIT",
                "assets": assets,
                "build": {"go_version": "go1.26.0", "source_date_epoch": 123},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_build_source_release_applies_patch_and_materializes_four_targets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    (source / "cmd" / "server").mkdir(parents=True)
    for name in builder.ARCHIVE_MEMBERS[1:]:
        (source / name).write_text(name, encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "--quiet", "-m", "fixture"],
        check=True,
    )
    source_sha = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    patch = tmp_path / "compat.patch"
    patch.write_text(
        "\n".join(
            [
                "diff --git a/patched-marker.txt b/patched-marker.txt",
                "new file mode 100644",
                "--- /dev/null",
                "+++ b/patched-marker.txt",
                "@@ -0,0 +1 @@",
                "+patched",
                "",
            ]
        ),
        encoding="utf-8",
    )
    manifest = _manifest(tmp_path, source_sha=source_sha)
    commands: list[tuple[list[str], dict[str, str] | None]] = []
    real_run = builder._run

    def fake_run(command, *, cwd=None, env=None):
        commands.append((list(command), dict(env) if env is not None else None))
        if command[:2] == ["go", "version"]:
            return "go version go1.26.0 darwin/arm64\n"
        if command[:2] == ["go", "build"]:
            binary = Path(command[command.index("-o") + 1])
            binary.write_bytes(b"CLIProxyAPI v7.3.16")
            return ""
        if command[0].endswith("cli-proxy-api") and command[1:] == ["--help"]:
            return "CLIProxyAPI Version: v7.3.16, Commit: fixture\n"
        return real_run(command, cwd=cwd, env=env)

    monkeypatch.setattr(builder, "_run", fake_run)
    output = tmp_path / "output"
    with pytest.raises(
        guard.ReleaseGuardError,
        match="archive size mismatch",
    ):
        builder.build_source_release(
            manifest,
            output,
            patch_path=patch,
            source_repository=str(source),
            go_binary="go",
        )

    generated_manifest = output / "model-hub-engine-manifest.json"
    assert generated_manifest.exists()
    assert len(list(output.glob("*.tar.gz"))) == 4
    assert generated_manifest.read_bytes() == manifest.read_bytes()
    assert any(command[:3] == ["git", "apply", "--whitespace=error"] for command, _ in commands)
    build_envs = [env for command, env in commands if command[:2] == ["go", "build"]]
    assert {env["GOOS"] for env in build_envs} == {"darwin", "linux"}
    assert {env["GOARCH"] for env in build_envs} == {"amd64", "arm64"}
    assert {env["CGO_ENABLED"] for env in build_envs} == {"0"}
    build_commands = [command for command, _ in commands if command[:2] == ["go", "build"]]
    assert all(
        "-ldflags" in command and "-X main.Version=v7.3.16" in command
        for command in build_commands
    )

    pinned = json.loads(manifest.read_text(encoding="utf-8"))
    for asset in pinned["assets"]:
        archive = output / Path(asset["url"]).name
        asset["size_bytes"] = archive.stat().st_size
        asset["sha256"] = builder._sha256(archive)
        asset["binary_sha256"] = hashlib.sha256(b"CLIProxyAPI v7.3.16").hexdigest()
    manifest.write_text(json.dumps(pinned, indent=2) + "\n", encoding="utf-8")
    verified_output = tmp_path / "verified-output"
    verified_manifest = builder.build_source_release(
        manifest,
        verified_output,
        patch_path=patch,
        source_repository=str(source),
        go_binary="go",
    )
    assert verified_manifest.read_bytes() == manifest.read_bytes()
    guard.verify_release_assets(manifest, verified_output)
