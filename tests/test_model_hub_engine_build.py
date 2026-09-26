from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import build_model_hub_engine as builder
from scripts import model_hub_engine_release_guard as guard


# Independent fixture contract: do not derive names, contents, or expected
# digests from builder constants or a previous invocation of the builder.
FIXTURE_MEMBERS = {
    "cli-proxy-api": b"CLIProxyAPI v7.3.16",
    "LICENSE": b"LICENSE",
    "README.md": b"README.md",
    "README_CN.md": b"README_CN.md",
    "config.example.yaml": b"config.example.yaml",
}
FIXTURE_TARGETS = {
    "darwin-arm64": "darwin_aarch64",
    "darwin-x64": "darwin_amd64",
    "linux-amd64": "linux_amd64",
    "linux-arm64": "linux_aarch64",
}


def _manifest(tmp_path: Path, *, source_sha: str, patch: Path) -> Path:
    version = "v7.3.16"
    release_tag = "model-hub-engine-v7.3.16-3"
    assets = [
        {
            "platform": platform,
            "url": (
                f"{guard.OWNED_RELEASE_ROOT}/{release_tag}/"
                f"CLIProxyAPI_7.3.16_{asset_arch}.tar.gz"
            ),
            # USTAR fixture: members above in declared order, epoch 123,
            # root ownership, 0755 executable/0644 docs; gzip mtime 0.
            "size_bytes": 234,
            "sha256": "fb13ef1030f49b5b64993f9596a227ac519b38d637b1a61fa95d3c73fc3d5ab8",
            "binary_sha256": "0e18ced596a1e68e5dfdcc5c8bf98668f405061aeca0fc2a4501aeeeeebf809d",
            "bin_path": "cli-proxy-api",
        }
        for platform, asset_arch in FIXTURE_TARGETS.items()
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
                "build": {
                    "go_version": "go1.26.0",
                    "source_date_epoch": 123,
                    "patch": patch.name,
                    "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("patch_mode", "wrong_pinned_size"),
    [("explicit", False), ("default", False), ("explicit", True), ("changed", False)],
)
def test_build_source_release_applies_patch_and_materializes_four_targets(
    tmp_path: Path,
    monkeypatch,
    wrong_pinned_size: bool,
    patch_mode: str,
) -> None:
    source = tmp_path / "source"
    (source / "cmd" / "server").mkdir(parents=True)
    for name, data in FIXTURE_MEMBERS.items():
        if name != "cli-proxy-api":
            (source / name).write_bytes(data)
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
    manifest = _manifest(tmp_path, source_sha=source_sha, patch=patch)
    monkeypatch.setattr(builder, "REPO_ROOT", tmp_path)
    if patch_mode == "changed":
        # A provenance-only change leaves all compiled/archive bytes intact.
        # Output hash validation alone cannot detect this changed build input.
        patch.write_bytes(b"# changed provenance\n" + patch.read_bytes())
    if wrong_pinned_size:
        pinned = json.loads(manifest.read_text(encoding="utf-8"))
        pinned["assets"][0]["size_bytes"] = 1
        manifest.write_text(json.dumps(pinned), encoding="utf-8")
    commands: list[tuple[list[str], dict[str, str] | None]] = []
    real_run = builder._run

    def fake_run(command, *, cwd=None, env=None):
        commands.append((list(command), dict(env) if env is not None else None))
        if command[:2] == ["go", "version"]:
            return "go version go1.26.0 darwin/arm64\n"
        if command[:2] == ["go", "build"]:
            assert (cwd / "patched-marker.txt").read_text(encoding="utf-8") == "patched\n"
            binary = Path(command[command.index("-o") + 1])
            binary.write_bytes(FIXTURE_MEMBERS["cli-proxy-api"])
            return ""
        if command[0].endswith("cli-proxy-api") and command[1:] == ["--help"]:
            return "CLIProxyAPI Version: v7.3.16, Commit: fixture\n"
        return real_run(command, cwd=cwd, env=env)

    monkeypatch.setattr(builder, "_run", fake_run)
    output = tmp_path / "output"
    if patch_mode == "changed":
        with pytest.raises(builder.BuildError, match="patch.*checksum"):
            builder.build_source_release(
                manifest,
                output,
                patch_path=patch,
                source_repository=str(source),
            )
        assert not commands
        return
    if wrong_pinned_size:
        with pytest.raises(guard.ReleaseGuardError, match="archive size mismatch"):
            builder.build_source_release(
                manifest,
                output,
                patch_path=patch,
                source_repository=str(source),
                go_binary="go",
            )
        return

    generated_manifest = builder.build_source_release(
        manifest,
        output,
        source_repository=str(source),
        go_binary="go",
        **({"patch_path": patch} if patch_mode == "explicit" else {}),
    )

    assert {path.name for path in output.glob("*.tar.gz")} == {
        f"CLIProxyAPI_7.3.16_{arch}.tar.gz" for arch in FIXTURE_TARGETS.values()
    }
    for archive in output.glob("*.tar.gz"):
        with tarfile.open(archive, "r:gz") as bundle:
            assert bundle.getnames() == list(FIXTURE_MEMBERS)
            for member in bundle.getmembers():
                assert member.isfile()
                assert member.mode == (0o755 if member.name == "cli-proxy-api" else 0o644)
                assert member.mtime == 123
                assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
                assert bundle.extractfile(member).read() == FIXTURE_MEMBERS[member.name]
    assert generated_manifest.read_bytes() == manifest.read_bytes()
    assert any(command[:3] == ["git", "apply", "--whitespace=error"] for command, _ in commands)
    build_envs = [env for command, env in commands if command[:2] == ["go", "build"]]
    assert len(build_envs) == 4
    assert {(env["GOOS"], env["GOARCH"]) for env in build_envs} == {
        ("darwin", "arm64"), ("darwin", "amd64"), ("linux", "amd64"), ("linux", "arm64"),
    }
    assert {env["CGO_ENABLED"] for env in build_envs} == {"0"}
    build_commands = [command for command, _ in commands if command[:2] == ["go", "build"]]
    assert all(
        "-ldflags" in command and "-X main.Version=v7.3.16" in command
        for command in build_commands
    )

    guard.verify_release_assets(manifest, output)
