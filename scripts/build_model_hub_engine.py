#!/usr/bin/env python3
"""Build the Avibe-owned Model Hub engine from a pinned patched source tree."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "vibe" / "model_hub_runtime" / "cliproxyapi_manifest.json"
DEFAULT_SOURCE_REPOSITORY = "https://github.com/router-for-me/CLIProxyAPI.git"
OWNED_RELEASE_ROOT = "https://github.com/avibe-bot/avibe/releases/download"

TARGETS = {
    "darwin-arm64": ("darwin", "arm64", "darwin_aarch64"),
    "darwin-x64": ("darwin", "amd64", "darwin_amd64"),
    "linux-amd64": ("linux", "amd64", "linux_amd64"),
    "linux-arm64": ("linux", "arm64", "linux_aarch64"),
    "windows-amd64": ("windows", "amd64", "windows_amd64"),
    "windows-arm64": ("windows", "arm64", "windows_aarch64"),
}
DOCUMENT_MEMBERS = ("LICENSE", "README.md", "README_CN.md", "config.example.yaml")
_FIXED_BUILD_SETTINGS = {
    "cgo_enabled": False,
    "trimpath": True,
    "buildvcs": False,
}
_GO_ARCHITECTURE_LEVEL_VARS = (
    "GO386",
    "GOAMD64",
    "GOARM",
    "GOARM64",
    "GOMIPS",
    "GOMIPS64",
    "GOPPC64",
    "GORISCV64",
    "GOWASM",
)
_GO_BUILD_CONTROL_ENV = {
    "GO111MODULE": "on",
    "GOENV": "off",
    "GOFLAGS": "",
    "GOEXPERIMENT": "",
    "GOFIPS140": "off",
    "GODEBUG": "",
    "GOTOOLCHAIN": "local",
    "GOWORK": "off",
    "GO_EXTLINK_ENABLED": "0",
}


class BuildError(RuntimeError):
    """Raised when a pinned Model Hub source build cannot be trusted."""


def _controlled_go_environment(
    *,
    goos: str | None = None,
    goarch: str | None = None,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the reproducible Go environment used by version and build commands.

    The variables below are the Go toolchain's documented build/toolchain controls;
    caches, module downloads, and the caller's PATH remain available. Ambient
    architecture levels, flags, experiments, workspace discovery, persisted Go
    configuration, and automatic toolchain switching do not belong to a recorded
    six-platform build identity.
    """

    environment = dict(base_environment) if base_environment is not None else os.environ.copy()
    for name in (*_GO_ARCHITECTURE_LEVEL_VARS, "GOOS", "GOARCH", "CGO_ENABLED"):
        environment.pop(name, None)
    environment.update(_GO_BUILD_CONTROL_ENV)
    if goos is None or goarch is None:
        return environment
    if goarch == "amd64":
        environment["GOAMD64"] = "v1"
    elif goarch == "arm64":
        environment["GOARM64"] = "v8.0"
    else:  # pragma: no cover - TARGETS currently contains only these architectures.
        raise BuildError(f"unsupported Model Hub Go architecture: {goarch}")
    environment.update({"GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": "0"})
    return environment


def _candidate_build_metadata(build_metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize omitted fixed settings and reject contradictory candidate input."""

    effective = dict(build_metadata)
    for field, expected in _FIXED_BUILD_SETTINGS.items():
        if field in build_metadata and build_metadata[field] != expected:
            raise BuildError(
                f"candidate Model Hub build metadata {field} contradicts the fixed build setting"
            )
        effective[field] = expected
    return effective


def _go_toolchain_version(output: str) -> str:
    fields = output.split()
    if len(fields) < 3 or fields[:2] != ["go", "version"] or not fields[2].startswith("go"):
        raise BuildError("Go toolchain identity is unreadable")
    if re.fullmatch(r"go[0-9]+\.[0-9]+\.[0-9]+", fields[2]) is None:
        raise BuildError("Go toolchain version is not stable")
    return fields[2]


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


def _binary_name(platform: str) -> str:
    return "cli-proxy-api.exe" if TARGETS[platform][0] == "windows" else "cli-proxy-api"


def _pinned_patches(
    build_metadata: Mapping[str, Any],
    *,
    allow_empty: bool = False,
) -> list[Path]:
    """Return the manifest's patches in apply order, each checked against its digest."""

    recorded = build_metadata.get("patches")
    if not isinstance(recorded, list) or (not recorded and not allow_empty):
        raise BuildError("Model Hub manifest compatibility patch identity is invalid")
    patches: list[Path] = []
    for item in recorded:
        path = item.get("path") if isinstance(item, dict) else None
        digest = item.get("sha256") if isinstance(item, dict) else None
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise BuildError("Model Hub manifest compatibility patch identity is invalid")
        patch_path = REPO_ROOT / path
        if not patch_path.is_file() or patch_path.is_symlink():
            raise BuildError(f"Model Hub compatibility patch is missing or unsafe: {path}")
        if _sha256(patch_path) != digest:
            raise BuildError(f"Model Hub compatibility patch checksum differs from pinned manifest: {path}")
        patches.append(patch_path.resolve())
    if len(set(patches)) != len(patches):
        raise BuildError("Model Hub manifest lists a compatibility patch twice")
    return patches


def _prepare_source(
    source_root: Path,
    *,
    repository: str,
    source_sha: str,
    patches: Sequence[Path],
) -> Path:
    checkout = source_root / "source"
    _checkout_source(checkout, repository=repository, source_sha=source_sha)
    for patch_path in patches:
        _run(["git", "apply", "--whitespace=error", str(patch_path)], cwd=checkout)
    return checkout


def _archive(
    source: Path,
    destination: Path,
    *,
    binary_name: str,
    source_date_epoch: int,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
                for member_name in (binary_name, *DOCUMENT_MEMBERS):
                    path = source / member_name
                    if not path.is_file() or path.is_symlink():
                        raise BuildError(f"source build is missing a safe archive member: {member_name}")
                    data = path.read_bytes()
                    member = tarfile.TarInfo(member_name)
                    member.size = len(data)
                    member.mode = 0o755 if member_name == binary_name else 0o644
                    member.mtime = source_date_epoch
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    bundle.addfile(member, io.BytesIO(data))


def _asset_name(version: str, platform: str) -> str:
    return f"CLIProxyAPI_{version.removeprefix('v')}_{TARGETS[platform][2]}.tar.gz"


def _candidate_asset_urls(payload: Mapping[str, Any]) -> dict[str, str]:
    """Validate candidate asset identity without accepting untrusted digests."""

    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise BuildError("candidate Model Hub engine manifest assets must be a list")
    expected_tag = payload.get("asset_release_tag")
    version = payload.get("version")
    if not isinstance(expected_tag, str) or not expected_tag:
        raise BuildError("candidate Model Hub engine manifest asset release tag is missing")
    if not isinstance(version, str) or not version:
        raise BuildError("candidate Model Hub engine manifest version is missing")

    urls: dict[str, str] = {}
    for item in raw_assets:
        if not isinstance(item, dict):
            raise BuildError("candidate Model Hub engine manifest asset is invalid")
        platform = item.get("platform")
        if platform not in TARGETS or platform in urls:
            raise BuildError("candidate Model Hub engine manifest platform set is invalid")
        if any(key in item for key in ("size_bytes", "sha256", "binary_sha256")):
            raise BuildError(
                "candidate Model Hub engine manifest must not contain pinned asset digests"
            )
        expected_url = (
            f"{OWNED_RELEASE_ROOT}/{expected_tag}/{_asset_name(version, platform)}"
        )
        url = item.get("url", expected_url)
        if url != expected_url:
            raise BuildError(f"candidate Model Hub engine asset URL is not Avibe-owned: {platform}")
        urls[platform] = url
    if set(urls) != set(TARGETS):
        raise BuildError("candidate Model Hub engine manifest platform set is invalid")
    return urls


def _native_target() -> str | None:
    goos = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    goarch = {
        "aarch64": "arm64",
        "amd64": "amd64",
        "arm64": "arm64",
        "x86_64": "amd64",
    }.get(platform.machine().lower())
    if goos is None or goarch is None:
        return None
    return next(
        (
            target
            for target, (target_goos, target_goarch, _asset_arch) in TARGETS.items()
            if target_goos == goos and target_goarch == goarch
        ),
        None,
    )


def _verify_built_binary(
    binary: Path,
    *,
    version: str,
    target: str,
    checkout: Path,
) -> None:
    try:
        binary_bytes = binary.read_bytes()
    except OSError as exc:
        raise BuildError(f"cannot read built Model Hub binary: {exc}") from exc
    if version.encode("ascii") not in binary_bytes:
        raise BuildError(f"built Model Hub binary does not contain pinned version: {target}")

    if target != _native_target():
        return
    output = _run([str(binary), "--help"], cwd=checkout)
    match = re.search(r"CLIProxyAPI Version:\s*([^,\s]+)", output)
    if match is None or match.group(1) != version:
        reported = match.group(1) if match is not None else "unreadable"
        raise BuildError(
            f"built Model Hub binary reported {reported}, expected {version}: {target}"
        )


def build_source_release(
    manifest_path: Path,
    output_dir: Path,
    *,
    candidate: bool = False,
    source_repository: str = DEFAULT_SOURCE_REPOSITORY,
    go_binary: str = "go",
) -> Path:
    """Build a pinned release or a freshly-digested candidate manifest.

    Candidate mode accepts source/patch metadata and owned planned asset URLs,
    but never trusts or requires asset digests from the input. It writes a new
    manifest containing the bytes produced by this build before running the
    same release guard used by strict pinned mode.
    """
    payload = _load_manifest(manifest_path)
    candidate_urls = _candidate_asset_urls(payload) if candidate else None
    output_dir = output_dir.resolve()
    if output_dir.exists():
        if not output_dir.is_dir() or output_dir.is_symlink() or any(output_dir.iterdir()):
            raise BuildError("Model Hub build output must be a fresh empty directory")
    else:
        output_dir.mkdir(parents=True)

    build_metadata = payload.get("build")
    if not isinstance(build_metadata, dict):
        build_metadata = {}
    effective_build_metadata = _candidate_build_metadata(build_metadata) if candidate else build_metadata
    patches = _pinned_patches(build_metadata, allow_empty=candidate)
    source_date_epoch = build_metadata.get("source_date_epoch", 0)
    if isinstance(source_date_epoch, bool) or not isinstance(source_date_epoch, int) or source_date_epoch < 0:
        raise BuildError("Model Hub build source_date_epoch is invalid")

    with tempfile.TemporaryDirectory(prefix="avibe-model-hub-build-") as temporary:
        checkout = _prepare_source(
            Path(temporary),
            repository=source_repository,
            source_sha=str(payload["source_sha"]),
            patches=patches,
        )
        go_environment = _controlled_go_environment()
        go_version = _go_toolchain_version(
            _run([go_binary, "version"], cwd=checkout, env=go_environment)
        )
        pinned_go_version = build_metadata.get("go_version")
        if candidate and (
            not isinstance(pinned_go_version, str)
            or re.fullmatch(r"go[0-9]+\.[0-9]+\.[0-9]+", pinned_go_version) is None
        ):
            raise BuildError("candidate Model Hub build must record an exact go_version")
        if isinstance(pinned_go_version, str) and pinned_go_version and go_version != pinned_go_version:
            raise BuildError(
                f"Go toolchain {go_version} differs from pinned manifest toolchain {pinned_go_version}"
            )

        generated_assets: list[dict[str, Any]] = []
        for platform in sorted(TARGETS):
            goos, goarch, _asset_arch = TARGETS[platform]
            build_dir = Path(temporary) / "artifacts" / platform
            build_dir.mkdir(parents=True)
            binary = build_dir / _binary_name(platform)
            environment = _controlled_go_environment(
                goos=goos,
                goarch=goarch,
                base_environment=go_environment,
            )
            _run(
                [
                    go_binary,
                    "build",
                    "-mod=readonly",
                    "-trimpath",
                    "-buildvcs=false",
                    "-ldflags",
                    f"-X main.Version={payload['version']}",
                    "-o",
                    str(binary),
                    "./cmd/server",
                ],
                cwd=checkout,
                env=environment,
            )
            _verify_built_binary(
                binary,
                version=str(payload["version"]),
                target=platform,
                checkout=checkout,
            )
            for member_name in DOCUMENT_MEMBERS:
                shutil.copy2(checkout / member_name, build_dir / member_name)
            archive_name = _asset_name(str(payload["version"]), platform)
            archive_path = output_dir / archive_name
            _archive(
                build_dir,
                archive_path,
                binary_name=binary.name,
                source_date_epoch=source_date_epoch,
            )
            generated_assets.append(
                {
                    "platform": platform,
                    "url": (
                        candidate_urls[platform]
                        if candidate_urls is not None
                        else next(
                            item["url"]
                            for item in payload["assets"]
                            if isinstance(item, dict) and item.get("platform") == platform
                        )
                    ),
                    "size_bytes": archive_path.stat().st_size,
                    "sha256": _sha256(archive_path),
                    "binary_sha256": _sha256(binary),
                    "bin_path": binary.name,
                }
            )
            (output_dir / f"{archive_name}.sha256").write_text(
                f"{generated_assets[-1]['sha256']}  {archive_name}\n",
                encoding="utf-8",
            )

    generated_manifest = output_dir / "model-hub-engine-manifest.json"
    if candidate:
        candidate_payload = dict(payload)
        candidate_payload["assets"] = generated_assets
        candidate_payload["build"] = effective_build_metadata
        generated_manifest.write_text(
            json.dumps(candidate_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        verification_manifest = generated_manifest
    else:
        # The checked-in manifest is the publication contract. A generated
        # manifest may describe what this runner produced, but it must not
        # replace the pinned sizes, digests, or metadata used by installed
        # clients.
        generated_manifest.write_bytes(manifest_path.read_bytes())
        verification_manifest = manifest_path

    # Import lazily so the builder can be unit-tested without importing the
    # network-facing release guard during source preparation.
    try:
        from scripts.model_hub_engine_release_guard import verify_release_assets
    except ModuleNotFoundError as exc:
        if exc.name != "scripts":
            raise
        from model_hub_engine_release_guard import verify_release_assets

    # Candidate mode uses the same release guard, but verifies the freshly
    # generated manifest rather than the checked-in pinned contract.
    verify_release_assets(verification_manifest, output_dir)
    return generated_manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source-repository", default=DEFAULT_SOURCE_REPOSITORY)
    parser.add_argument("--go", default="go", dest="go_binary")
    parser.add_argument(
        "--candidate",
        action="store_true",
        help="write a freshly-digested candidate manifest instead of checking pinned digests",
    )
    parser.add_argument("output_dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        generated_manifest = build_source_release(
            args.manifest,
            args.output_dir,
            candidate=args.candidate,
            source_repository=args.source_repository,
            go_binary=args.go_binary,
        )
    except BuildError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "manifest": str(generated_manifest)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
