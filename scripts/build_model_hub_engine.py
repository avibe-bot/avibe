#!/usr/bin/env python3
"""Build the Avibe-owned Model Hub engine from a pinned patched source tree."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "vibe" / "model_hub_runtime" / "cliproxyapi_manifest.json"
DEFAULT_PATCH = REPO_ROOT / "patches" / "model-hub-cpa-v7.3.16" / "openai-compat-native-token-field.patch"
DEFAULT_SOURCE_REPOSITORY = "https://github.com/router-for-me/CLIProxyAPI.git"

TARGETS = {
    "darwin-arm64": ("darwin", "arm64", "darwin_aarch64"),
    "darwin-x64": ("darwin", "amd64", "darwin_amd64"),
    "linux-amd64": ("linux", "amd64", "linux_amd64"),
    "linux-arm64": ("linux", "arm64", "linux_aarch64"),
}
ARCHIVE_MEMBERS = ("cli-proxy-api", "LICENSE", "README.md", "README_CN.md", "config.example.yaml")


class BuildError(RuntimeError):
    """Raised when a pinned Model Hub source build cannot be trusted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        raise BuildError(f"unable to run {' '.join(command)}: {exc}") from exc
    if result.returncode:
        raise BuildError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout.strip()}"
        )
    return result.stdout


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read Model Hub engine manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise BuildError("Model Hub engine manifest must be an object")
    required = ("version", "source_sha", "source_url", "release_tag", "asset_release_tag", "assets")
    if any(not isinstance(payload.get(key), str) or not payload[key] for key in required[:-1]):
        raise BuildError("Model Hub engine manifest has incomplete source identity")
    if not isinstance(payload["assets"], list):
        raise BuildError("Model Hub engine manifest assets must be a list")
    if {item.get("platform") for item in payload["assets"] if isinstance(item, dict)} != set(TARGETS):
        raise BuildError("Model Hub engine manifest platform set is invalid")
    return payload


def _checkout_source(
    destination: Path,
    *,
    repository: str,
    source_sha: str,
) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    _run(["git", "init", "--quiet", str(destination)])
    _run(["git", "-C", str(destination), "remote", "add", "origin", repository])
    _run(
        ["git", "-C", str(destination), "fetch", "--depth=1", "origin", source_sha],
    )
    _run(["git", "-C", str(destination), "checkout", "--quiet", "--detach", "FETCH_HEAD"])
    actual = _run(["git", "-C", str(destination), "rev-parse", "HEAD"]).strip()
    if actual != source_sha:
        raise BuildError(f"source checkout resolved {actual}, expected {source_sha}")


def _prepare_source(
    source_root: Path,
    *,
    repository: str,
    source_sha: str,
    patch_path: Path,
    source_dir: Path | None,
) -> Path:
    if source_dir is None:
        checkout = source_root / "source"
        _checkout_source(checkout, repository=repository, source_sha=source_sha)
    else:
        checkout = source_root / "source"
        if not source_dir.is_dir() or source_dir.is_symlink():
            raise BuildError("provided Model Hub source directory is unsafe")
        shutil.copytree(source_dir, checkout, symlinks=True)
    if not patch_path.is_file() or patch_path.is_symlink():
        raise BuildError("Model Hub compatibility patch is missing or unsafe")
    _run(["git", "apply", "--whitespace=error", str(patch_path)], cwd=checkout)
    return checkout


def _archive(
    source: Path,
    destination: Path,
    *,
    source_date_epoch: int,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
                for member_name in ARCHIVE_MEMBERS:
                    path = source / member_name
                    if not path.is_file() or path.is_symlink():
                        raise BuildError(f"source build is missing a safe archive member: {member_name}")
                    data = path.read_bytes()
                    member = tarfile.TarInfo(member_name)
                    member.size = len(data)
                    member.mode = 0o755 if member_name == "cli-proxy-api" else 0o644
                    member.mtime = source_date_epoch
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    bundle.addfile(member, io.BytesIO(data))


def _asset_name(version: str, platform: str) -> str:
    return f"CLIProxyAPI_{version.removeprefix('v')}_{TARGETS[platform][2]}.tar.gz"


def build_source_release(
    manifest_path: Path,
    output_dir: Path,
    *,
    source_repository: str = DEFAULT_SOURCE_REPOSITORY,
    patch_path: Path = DEFAULT_PATCH,
    source_dir: Path | None = None,
    go_binary: str = "go",
) -> Path:
    """Build all pinned targets and return the generated manifest path.

    ``source_dir`` is a test seam for an already isolated checkout. Production
    callers leave it unset so this function always creates a fresh checkout at
    the manifest's exact source SHA before applying the repository-owned patch.
    """

    payload = _load_manifest(manifest_path)
    output_dir = output_dir.resolve()
    if output_dir.exists():
        if not output_dir.is_dir() or output_dir.is_symlink() or any(output_dir.iterdir()):
            raise BuildError("Model Hub build output must be a fresh empty directory")
    else:
        output_dir.mkdir(parents=True)

    build_metadata = payload.get("build")
    if not isinstance(build_metadata, dict):
        build_metadata = {}
    source_date_epoch = build_metadata.get("source_date_epoch", 0)
    if isinstance(source_date_epoch, bool) or not isinstance(source_date_epoch, int) or source_date_epoch < 0:
        raise BuildError("Model Hub build source_date_epoch is invalid")

    with tempfile.TemporaryDirectory(prefix="avibe-model-hub-build-") as temporary:
        checkout = _prepare_source(
            Path(temporary),
            repository=source_repository,
            source_sha=str(payload["source_sha"]),
            patch_path=patch_path.resolve(),
            source_dir=source_dir.resolve() if source_dir is not None else None,
        )
        go_version = _run([go_binary, "version"], cwd=checkout).strip()
        if not go_version.startswith("go version "):
            raise BuildError("Go toolchain identity is unreadable")

        generated_assets: list[dict[str, Any]] = []
        for platform in sorted(TARGETS):
            goos, goarch, _asset_arch = TARGETS[platform]
            build_dir = Path(temporary) / "artifacts" / platform
            build_dir.mkdir(parents=True)
            binary = build_dir / "cli-proxy-api"
            environment = os.environ.copy()
            environment.update(
                {
                    "CGO_ENABLED": "0",
                    "GOOS": goos,
                    "GOARCH": goarch,
                }
            )
            _run(
                [
                    go_binary,
                    "build",
                    "-mod=readonly",
                    "-trimpath",
                    "-buildvcs=false",
                    "-o",
                    str(binary),
                    "./cmd/server",
                ],
                cwd=checkout,
                env=environment,
            )
            for member_name in ARCHIVE_MEMBERS[1:]:
                shutil.copy2(checkout / member_name, build_dir / member_name)
            archive_name = _asset_name(str(payload["version"]), platform)
            archive_path = output_dir / archive_name
            _archive(build_dir, archive_path, source_date_epoch=source_date_epoch)
            generated_assets.append(
                {
                    "platform": platform,
                    "url": next(
                        item["url"]
                        for item in payload["assets"]
                        if isinstance(item, dict) and item.get("platform") == platform
                    ),
                    "size_bytes": archive_path.stat().st_size,
                    "sha256": _sha256(archive_path),
                    "binary_sha256": _sha256(binary),
                    "bin_path": "cli-proxy-api",
                }
            )
            (output_dir / f"{archive_name}.sha256").write_text(
                f"{generated_assets[-1]['sha256']}  {archive_name}\n",
                encoding="utf-8",
            )

    try:
        patch_identity = str(patch_path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        patch_identity = patch_path.resolve().name
    generated = {
        **payload,
        "assets": generated_assets,
        "build": {
            **build_metadata,
            "builder": "scripts/build_model_hub_engine.py",
            "go_version": go_version,
            "cgo_enabled": False,
            "trimpath": True,
            "buildvcs": False,
            "patch": patch_identity,
            "patch_sha256": _sha256(patch_path),
            "source_date_epoch": source_date_epoch,
        },
    }
    generated_manifest = output_dir / "model-hub-engine-manifest.json"
    generated_manifest.write_text(
        json.dumps(generated, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # Import lazily so the builder can be unit-tested without importing the
    # network-facing release guard during source preparation.
    from scripts.model_hub_engine_release_guard import verify_release_assets

    verify_release_assets(generated_manifest, output_dir)
    return generated_manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--patch", type=Path, default=DEFAULT_PATCH)
    parser.add_argument("--source-repository", default=DEFAULT_SOURCE_REPOSITORY)
    parser.add_argument("--go", default="go", dest="go_binary")
    parser.add_argument("output_dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        generated_manifest = build_source_release(
            args.manifest,
            args.output_dir,
            source_repository=args.source_repository,
            patch_path=args.patch,
            go_binary=args.go_binary,
        )
    except BuildError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "manifest": str(generated_manifest)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
