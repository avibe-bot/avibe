from __future__ import annotations

import json
from pathlib import Path

from scripts import build_model_hub_engine as builder
from scripts import model_hub_engine_release_guard as guard


def _manifest(tmp_path: Path) -> Path:
    version = "v7.3.16"
    source_sha = "c" * 40
    release_tag = "model-hub-engine-v7.3.16-2"
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
                "build": {"source_date_epoch": 123},
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
    patch = tmp_path / "compat.patch"
    patch.write_text("test patch", encoding="utf-8")
    manifest = _manifest(tmp_path)
    commands: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(command, *, cwd=None, env=None):
        commands.append((list(command), dict(env) if env is not None else None))
        if command[:2] == ["go", "version"]:
            return "go version go1.26.4 darwin/arm64\n"
        if command[:2] == ["go", "build"]:
            binary = Path(command[command.index("-o") + 1])
            binary.write_bytes(f"{env['GOOS']}-{env['GOARCH']}".encode())
            return ""
        return ""

    monkeypatch.setattr(builder, "_run", fake_run)
    output = tmp_path / "output"
    generated_manifest = builder.build_source_release(
        manifest,
        output,
        patch_path=patch,
        source_dir=source,
        go_binary="go",
    )

    assert generated_manifest == output / "model-hub-engine-manifest.json"
    assert len(list(output.glob("*.tar.gz"))) == 4
    generated = json.loads(generated_manifest.read_text(encoding="utf-8"))
    assert generated["build"]["cgo_enabled"] is False
    assert generated["build"]["patch_sha256"] == builder._sha256(patch)
    assert any(command[:3] == ["git", "apply", "--whitespace=error"] for command, _ in commands)
    build_envs = [env for command, env in commands if command[:2] == ["go", "build"]]
    assert {env["GOOS"] for env in build_envs} == {"darwin", "linux"}
    assert {env["GOARCH"] for env in build_envs} == {"amd64", "arm64"}
    assert {env["CGO_ENABLED"] for env in build_envs} == {"0"}
    guard.verify_release_assets(generated_manifest, output)
