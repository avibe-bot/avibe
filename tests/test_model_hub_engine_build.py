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
FIXTURE_BINARY = b"CLIProxyAPI v7.3.16"
FIXTURE_DOCUMENTS = {
    "LICENSE": b"LICENSE",
    "README.md": b"README.md",
    "README_CN.md": b"README_CN.md",
    "config.example.yaml": b"config.example.yaml",
}
# platform -> (asset arch, GOOS, GOARCH, binary member)
FIXTURE_TARGETS = {
    "darwin-arm64": ("darwin_aarch64", "darwin", "arm64", "cli-proxy-api"),
    "darwin-x64": ("darwin_amd64", "darwin", "amd64", "cli-proxy-api"),
    "linux-amd64": ("linux_amd64", "linux", "amd64", "cli-proxy-api"),
    "linux-arm64": ("linux_aarch64", "linux", "arm64", "cli-proxy-api"),
    "windows-amd64": ("windows_amd64", "windows", "amd64", "cli-proxy-api.exe"),
    "windows-arm64": ("windows_aarch64", "windows", "arm64", "cli-proxy-api.exe"),
}
# USTAR fixture: the binary member then the documents above, epoch 123, root
# ownership, 0755 executable/0644 docs; gzip mtime 0. Keyed by binary member.
FIXTURE_ARCHIVES = {
    "cli-proxy-api": (234, "fb13ef1030f49b5b64993f9596a227ac519b38d637b1a61fa95d3c73fc3d5ab8"),
    "cli-proxy-api.exe": (237, "b6dca6c7afaf0e6be1958edf957bfcfb702e4a420019a119b8c6470b9566a37a"),
}


def _patch(path: Path, *lines: str) -> Path:
    path.write_text("\n".join([*lines, ""]), encoding="utf-8")
    return path


def _manifest(tmp_path: Path, *, source_sha: str, patches: list[Path]) -> Path:
    version = "v7.3.16"
    release_tag = "model-hub-engine-v7.3.16-3"
    assets = [
        {
            "platform": platform,
            "url": (
                f"{guard.OWNED_RELEASE_ROOT}/{release_tag}/"
                f"CLIProxyAPI_7.3.16_{asset_arch}.tar.gz"
            ),
            "size_bytes": FIXTURE_ARCHIVES[binary_name][0],
            "sha256": FIXTURE_ARCHIVES[binary_name][1],
            "binary_sha256": "0e18ced596a1e68e5dfdcc5c8bf98668f405061aeca0fc2a4501aeeeeebf809d",
            "bin_path": binary_name,
        }
        for platform, (asset_arch, _goos, _goarch, binary_name) in FIXTURE_TARGETS.items()
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
                    "patches": [
                        {"path": patch.name, "sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}
                        for patch in patches
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _candidate_manifest(tmp_path: Path, *, source_sha: str, patches: list[Path]) -> Path:
    path = _manifest(tmp_path, source_sha=source_sha, patches=patches)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["assets"] = [
        {"platform": asset["platform"], "url": asset["url"]}
        for asset in payload["assets"]
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("patch_mode", "wrong_pinned_size"),
    [("pinned", False), ("pinned", True), ("changed", False)],
)
def test_build_source_release_applies_patches_in_order_and_materializes_six_targets(
    tmp_path: Path,
    monkeypatch,
    wrong_pinned_size: bool,
    patch_mode: str,
) -> None:
    source = tmp_path / "source"
    (source / "cmd" / "server").mkdir(parents=True)
    for name, data in FIXTURE_DOCUMENTS.items():
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
    # The second patch edits the file the first creates, so it applies only in
    # the recorded order.
    patches = [
        _patch(
            tmp_path / "first.patch",
            "diff --git a/patched-marker.txt b/patched-marker.txt",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/patched-marker.txt",
            "@@ -0,0 +1 @@",
            "+patched",
        ),
        _patch(
            tmp_path / "second.patch",
            "diff --git a/patched-marker.txt b/patched-marker.txt",
            "--- a/patched-marker.txt",
            "+++ b/patched-marker.txt",
            "@@ -1 +1 @@",
            "-patched",
            "+patched twice",
        ),
    ]
    manifest = _manifest(tmp_path, source_sha=source_sha, patches=patches)
    monkeypatch.setattr(builder, "REPO_ROOT", tmp_path)
    if patch_mode == "changed":
        # A provenance-only change leaves all compiled/archive bytes intact.
        # Output hash validation alone cannot detect this changed build input.
        patches[1].write_bytes(b"# changed provenance\n" + patches[1].read_bytes())
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
            assert (cwd / "patched-marker.txt").read_text(encoding="utf-8") == "patched twice\n"
            binary = Path(command[command.index("-o") + 1])
            binary.write_bytes(FIXTURE_BINARY)
            return ""
        if Path(command[0]).name.startswith("cli-proxy-api") and command[1:] == ["--help"]:
            return "CLIProxyAPI Version: v7.3.16, Commit: fixture\n"
        return real_run(command, cwd=cwd, env=env)

    monkeypatch.setattr(builder, "_run", fake_run)
    output = tmp_path / "output"
    if patch_mode == "changed":
        with pytest.raises(builder.BuildError, match="patch.*checksum.*second.patch"):
            builder.build_source_release(
                manifest,
                output,
                source_repository=str(source),
            )
        assert not commands
        return
    if wrong_pinned_size:
        with pytest.raises(guard.ReleaseGuardError, match="archive size mismatch"):
            builder.build_source_release(
                manifest,
                output,
                source_repository=str(source),
                go_binary="go",
            )
        return

    generated_manifest = builder.build_source_release(
        manifest,
        output,
        source_repository=str(source),
        go_binary="go",
    )

    for asset_arch, _goos, _goarch, binary_name in FIXTURE_TARGETS.values():
        with tarfile.open(output / f"CLIProxyAPI_7.3.16_{asset_arch}.tar.gz", "r:gz") as bundle:
            expected = {binary_name: FIXTURE_BINARY, **FIXTURE_DOCUMENTS}
            assert bundle.getnames() == list(expected)
            for member in bundle.getmembers():
                assert member.isfile()
                assert member.mode == (0o755 if member.name == binary_name else 0o644)
                assert member.mtime == 123
                assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
                assert bundle.extractfile(member).read() == expected[member.name]
    assert generated_manifest.read_bytes() == manifest.read_bytes()
    assert [command[3] for command, _ in commands if command[:3] == ["git", "apply", "--whitespace=error"]] == [
        str(patch.resolve()) for patch in patches
    ]
    build_commands = [command for command, _ in commands if command[:2] == ["go", "build"]]
    build_envs = [env for command, env in commands if command[:2] == ["go", "build"]]
    assert sorted(
        (env["GOOS"], env["GOARCH"], Path(command[command.index("-o") + 1]).name)
        for command, env in zip(build_commands, build_envs)
    ) == sorted((goos, goarch, binary_name) for _arch, goos, goarch, binary_name in FIXTURE_TARGETS.values())
    assert {env["CGO_ENABLED"] for env in build_envs} == {"0"}
    assert all(
        "-ldflags" in command and "-X main.Version=v7.3.16" in command
        for command in build_commands
    )

    guard.verify_release_assets(manifest, output)


def test_candidate_mode_writes_real_digests_and_owned_urls_without_pinned_hashes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    (source / "cmd" / "server").mkdir(parents=True)
    for name, data in FIXTURE_DOCUMENTS.items():
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
    subprocess.run(["git", "-C", str(source), "commit", "--quiet", "-m", "fixture"], check=True)
    source_sha = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    patches = [
        _patch(
            tmp_path / "first.patch",
            "diff --git a/patched-marker.txt b/patched-marker.txt",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/patched-marker.txt",
            "@@ -0,0 +1 @@",
            "+patched",
        ),
        _patch(
            tmp_path / "second.patch",
            "diff --git a/patched-marker.txt b/patched-marker.txt",
            "--- a/patched-marker.txt",
            "+++ b/patched-marker.txt",
            "@@ -1 +1 @@",
            "-patched",
            "+patched twice",
        ),
    ]
    manifest = _candidate_manifest(tmp_path, source_sha=source_sha, patches=patches)
    monkeypatch.setattr(builder, "REPO_ROOT", tmp_path)
    real_run = builder._run

    def fake_run(command, *, cwd=None, env=None):
        if command[:2] == ["go", "version"]:
            return "go version go1.26.0 darwin/arm64\n"
        if command[:2] == ["go", "build"]:
            marker = cwd / "patched-marker.txt"
            if marker.exists():
                assert marker.read_text(encoding="utf-8") == "patched twice\n"
            binary = Path(command[command.index("-o") + 1])
            binary.write_bytes(FIXTURE_BINARY)
            return ""
        if Path(command[0]).name.startswith("cli-proxy-api") and command[1:] == ["--help"]:
            return "CLIProxyAPI Version: v7.3.16, Commit: fixture\n"
        return real_run(command, cwd=cwd, env=env)

    monkeypatch.setattr(builder, "_run", fake_run)
    candidate_bytes = manifest.read_bytes()
    malformed = json.loads(candidate_bytes)
    malformed["assets"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(builder.BuildError, match="must not contain pinned asset digests"):
        builder.build_source_release(
            manifest,
            tmp_path / "rejected-candidate-output",
            candidate=True,
            source_repository=str(source),
            go_binary="go",
        )
    manifest.write_bytes(candidate_bytes)
    missing_toolchain = json.loads(candidate_bytes)
    missing_toolchain["build"].pop("go_version")
    manifest.write_text(json.dumps(missing_toolchain), encoding="utf-8")
    with pytest.raises(builder.BuildError, match="must record an exact go_version"):
        builder.build_source_release(
            manifest,
            tmp_path / "missing-toolchain-output",
            candidate=True,
            source_repository=str(source),
            go_binary="go",
        )
    manifest.write_bytes(candidate_bytes)

    output = tmp_path / "candidate-output"
    generated_manifest = builder.build_source_release(
        manifest,
        output,
        candidate=True,
        source_repository=str(source),
        go_binary="go",
    )

    candidate_payload = json.loads(manifest.read_text(encoding="utf-8"))
    generated_payload = json.loads(generated_manifest.read_text(encoding="utf-8"))
    assert all(
        not any(key in asset for key in ("size_bytes", "sha256", "binary_sha256"))
        for asset in candidate_payload["assets"]
    )
    assert {asset["platform"] for asset in generated_payload["assets"]} == set(FIXTURE_TARGETS)
    assert generated_payload["assets"] != candidate_payload["assets"]
    for asset in generated_payload["assets"]:
        archive = output / Path(asset["url"]).name
        assert asset["url"].startswith(
            f"{guard.OWNED_RELEASE_ROOT}/{generated_payload['asset_release_tag']}/"
        )
        assert asset["size_bytes"] == archive.stat().st_size
        assert asset["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
        with tarfile.open(archive, "r:gz") as bundle:
            binary = bundle.extractfile(asset["bin_path"])
            assert binary is not None
            assert asset["binary_sha256"] == hashlib.sha256(binary.read()).hexdigest()

    assert guard.verify_release_assets(generated_manifest, output).version == "v7.3.16"

    patchless = json.loads(candidate_bytes)
    patchless["build"]["patches"] = []
    manifest.write_text(json.dumps(patchless), encoding="utf-8")
    patchless_output = tmp_path / "patchless-candidate-output"
    patchless_manifest = builder.build_source_release(
        manifest,
        patchless_output,
        candidate=True,
        source_repository=str(source),
        go_binary="go",
    )
    assert guard.verify_release_assets(patchless_manifest, patchless_output).version == "v7.3.16"
