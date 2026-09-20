import json
from pathlib import Path

import pytest

from scripts.generate_show_runtime_manifest import build_manifest


PLATFORMS = (
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "win32-arm64",
    "win32-x64",
)


def test_generate_show_runtime_manifest_records_all_platform_archives(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    for platform in PLATFORMS:
        (archive_dir / f"vibe-show-runtime-node-{platform}.tgz").write_bytes(f"runtime-{platform}".encode())

    output = tmp_path / "manifest.json"
    manifest = build_manifest(
        archive_dir=archive_dir,
        tag="gh-v2.4.0rc1",
        repo="avibe-bot/avibe",
        runtime_ref="runtime-sha",
        output=output,
    )

    assert set(manifest["archives"]) == set(PLATFORMS)
    assert output.exists()
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["runtime_version"] == "runtime-sha"
    assert written["archives"]["linux-x64"]["url"] == (
        "https://github.com/avibe-bot/avibe/releases/download/gh-v2.4.0rc1/"
        "vibe-show-runtime-node-linux-x64.tgz"
    )
    assert written["archives"]["linux-x64"]["size"] == len(b"runtime-linux-x64")


def test_generate_show_runtime_manifest_fails_when_platform_archive_missing(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    for platform in PLATFORMS[:-1]:
        (archive_dir / f"vibe-show-runtime-node-{platform}.tgz").write_bytes(b"runtime")

    with pytest.raises(SystemExit, match="Missing Show Runtime archives"):
        build_manifest(
            archive_dir=archive_dir,
            tag="v2.4.0",
            repo="avibe-bot/avibe",
            runtime_ref="runtime-sha",
            output=tmp_path / "manifest.json",
        )


@pytest.mark.parametrize("failure", [None, "missing", "different", "no_ssr"])
def test_release_uses_one_router_from_the_actual_runtime_builds(tmp_path: Path, failure) -> None:
    router = b"export function SsrRouterProvider() { return null }\n"
    for platform in PLATFORMS:
        (tmp_path / f"vibe-show-runtime-node-{platform}.tgz").write_bytes(b"runtime")
        (tmp_path / f"show-router-{platform}.tsx").write_bytes(router)
    candidate = tmp_path / "show-router-win32-arm64.tsx"
    if failure == "missing":
        candidate.unlink()
    elif failure == "different":
        candidate.write_bytes(router + b"// drift\n")
    elif failure == "no_ssr":
        for platform in PLATFORMS:
            (tmp_path / f"show-router-{platform}.tsx").write_bytes(b"export function RouterView() {}\n")
    output = tmp_path / "package/show_runtime_manifest.json"
    router_output = tmp_path / "package/show_router.tsx"
    kwargs = dict(
        archive_dir=tmp_path,
        tag="v3.0.15",
        repo="avibe-bot/avibe",
        runtime_ref="runtime-sha",
        output=output,
        router_output=router_output,
    )
    if failure:
        with pytest.raises(SystemExit, match="router template"):
            build_manifest(**kwargs)
        assert not output.exists()
        assert not router_output.exists()
    else:
        build_manifest(**kwargs)
        assert router_output.read_bytes() == router
        assert output.exists()
