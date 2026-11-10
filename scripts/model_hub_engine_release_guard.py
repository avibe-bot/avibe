#!/usr/bin/env python3
"""Verify and materialize the manifest-pinned Model Hub engine release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from pathlib import Path
from runpy import run_path

download_to_path = run_path(str(Path(__file__).with_name("release_asset_download.py")))["download_to_path"]

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The manifest schema has one owner, which installed clients also use to check
# a channel release; ``core`` imports nothing, so a bare runner can load it.
from core import model_hub_engine_manifest as engine_manifest  # noqa: E402
from core.model_hub_engine_manifest import (  # noqa: E402,F401
    ASSET_TAG_RE,
    EXPECTED_PLATFORMS,
    MAX_ARCHIVE_BYTES,
    OWNED_RELEASE_ROOT,
    SHA256_RE,
    SOURCE_SHA_RE,
    UPSTREAM_PLATFORM_NAMES,
    VERSION_RE,
    ArchiveSpec,
    ReleaseSpec,
)

DEFAULT_MANIFEST = REPO_ROOT / "vibe" / "model_hub_runtime" / "cliproxyapi_manifest.json"
#: A verified copy of one release's assets, kept as a workflow artifact and
#: named by the digest of the manifest that pins it.
BACKUP_ARTIFACT_PREFIX = "model-hub-engine-release-backup-"
GO_VERSION_RE = re.compile(r"go[0-9]+\.[0-9]+\.[0-9]+")


class ReleaseGuardError(RuntimeError):
    """Raised when a pinned CPA release cannot be trusted or materialized."""


def manifest_go_version(manifest_path: Path) -> str:
    """Return the exact stable Go pin used by source publication and recovery."""

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseGuardError(f"cannot read Model Hub engine manifest: {exc}") from exc
    build = payload.get("build") if isinstance(payload, dict) else None
    version = build.get("go_version") if isinstance(build, dict) else None
    if not isinstance(version, str) or GO_VERSION_RE.fullmatch(version) is None:
        raise ReleaseGuardError("Model Hub engine manifest build.go_version must be an exact stable Go version")
    return version


def load_release_spec(manifest_path: Path) -> ReleaseSpec:
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise ReleaseGuardError(f"cannot read Model Hub engine manifest: {exc}") from exc
    try:
        return engine_manifest.parse_release_manifest(manifest_bytes)
    except engine_manifest.EngineManifestError as exc:
        raise ReleaseGuardError(str(exc)) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_archive(path: Path, archive: ArchiveSpec) -> None:
    if not path.is_file() or path.stat().st_size != archive.size:
        raise ReleaseGuardError(f"Model Hub engine archive size mismatch: {archive.name}")
    if _sha256(path) != archive.sha256:
        raise ReleaseGuardError(f"Model Hub engine archive checksum mismatch: {archive.name}")
    try:
        with tarfile.open(path, "r:gz") as bundle:
            member = bundle.getmember(archive.bin_path)
            stream = bundle.extractfile(member)
            binary_sha256 = (
                hashlib.sha256(stream.read()).hexdigest()
                if stream is not None and member.isfile()
                else ""
            )
    except (KeyError, OSError, tarfile.TarError) as exc:
        raise ReleaseGuardError(f"invalid Model Hub engine archive: {archive.name}") from exc
    if binary_sha256 != archive.binary_sha256:
        raise ReleaseGuardError(f"Model Hub engine binary checksum mismatch: {archive.name}")


def _verify_sidecar(path: Path, archive: ArchiveSpec) -> None:
    if not path.is_file():
        raise ReleaseGuardError(f"missing Model Hub engine checksum: {path.name}")
    try:
        fields = path.read_text(encoding="utf-8").strip().split()
    except OSError as exc:
        raise ReleaseGuardError(f"cannot read Model Hub engine checksum: {path.name}") from exc
    if fields != [archive.sha256, archive.name]:
        raise ReleaseGuardError(f"Model Hub engine checksum sidecar mismatch: {path.name}")


def verify_release_assets(manifest_path: Path, asset_dir: Path) -> ReleaseSpec:
    spec = load_release_spec(manifest_path)
    if not asset_dir.is_dir():
        raise ReleaseGuardError("Model Hub engine asset directory is missing")
    entries = list(asset_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ReleaseGuardError("Model Hub engine asset directory contains unsafe entries")
    actual_names = {path.name for path in entries}
    if actual_names != spec.expected_asset_names:
        raise ReleaseGuardError(
            "Model Hub engine asset set mismatch: "
            f"missing={sorted(spec.expected_asset_names - actual_names)}, "
            f"unexpected={sorted(actual_names - spec.expected_asset_names)}"
        )
    if (asset_dir / "model-hub-engine-manifest.json").read_bytes() != spec.manifest_bytes:
        raise ReleaseGuardError("published Model Hub engine manifest differs from the packaged manifest")
    for archive in spec.archives:
        _verify_archive(asset_dir / archive.name, archive)
        _verify_sidecar(asset_dir / f"{archive.name}.sha256", archive)
    return spec


def _gh_release(repository: str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["gh", "release", *arguments, "--repo", repository],
            check=True,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ReleaseGuardError(f"unable to invoke GitHub CLI: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise ReleaseGuardError(
            f"GitHub release {arguments[0]} failed ({exc.returncode}): "
            f"{(exc.stderr or '').strip()}"
        ) from exc
    return result.stdout


def publish_draft_release(
    manifest_path: Path, asset_dir: Path, *, repository: str,
) -> ReleaseSpec:
    """Repair an unpublished asset set, verify its bytes, then publish it.

    Both first publication and backup recovery use this boundary. Published
    releases are immutable here. Verified local bytes can replace an incomplete
    or unreadable draft; publication still requires a successful remote check.
    """
    spec = verify_release_assets(manifest_path, asset_dir)
    snapshot = json.loads(
        _gh_release(repository, "view", spec.asset_release_tag, "--json", "isDraft,assets")
    )
    if snapshot.get("isDraft") is not True:
        raise ReleaseGuardError("refusing to replace assets on a published release")
    current_names = {asset["name"] for asset in snapshot["assets"]}

    with tempfile.TemporaryDirectory(prefix="avibe-engine-draft-") as temporary:
        draft_dir = Path(temporary) / "assets"

        def download() -> None:
            draft_dir.mkdir()
            _gh_release(
                repository, "download", spec.asset_release_tag, "--dir", str(draft_dir),
            )

        try:
            download()
            verify_release_assets(manifest_path, draft_dir)
        except ReleaseGuardError as exc:
            print(f"Repairing unverified draft assets: {exc}", file=sys.stderr)
            # Replacement owns the entire unpublished set, including names
            # that are absent from the manifest. Never leave extra draft bytes.
            for name in sorted(current_names):
                _gh_release(repository, "delete-asset", spec.asset_release_tag, name, "--yes")
            _gh_release(
                repository, "upload", spec.asset_release_tag,
                *(str(asset_dir / name) for name in sorted(spec.expected_asset_names)),
            )
            shutil.rmtree(draft_dir)
            download()
            verify_release_assets(manifest_path, draft_dir)
        _gh_release(repository, "edit", spec.asset_release_tag, "--draft=false")
    return spec


def _gh_api(*arguments: str, destination: Path | None = None) -> str:
    """Call the GitHub API through the CLI; a binary answer goes to ``destination``."""

    command = ["gh", "api", *arguments]
    try:
        if destination is None:
            return subprocess.run(command, check=True, capture_output=True, text=True).stdout
        with destination.open("wb") as stream:
            subprocess.run(command, check=True, stdout=stream, stderr=subprocess.PIPE)
        return ""
    except OSError as exc:
        raise ReleaseGuardError(f"unable to invoke GitHub CLI: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        raise ReleaseGuardError(f"GitHub API call failed ({exc.returncode}): {stderr.strip()}") from exc


def _extract_backup(archive: Path, destination: Path, spec: ReleaseSpec) -> None:
    """Unpack a backup that holds only this release's expected, flat asset names."""

    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if member.is_dir():
                continue
            if member.filename not in spec.expected_asset_names or member.file_size > MAX_ARCHIVE_BYTES:
                raise ReleaseGuardError(f"backup holds an unexpected entry: {member.filename}")
            with bundle.open(member) as source, (destination / member.filename).open("wb") as output:
                shutil.copyfileobj(source, output)


def verified_backups(manifest_path: Path, work_dir: Path, *, repository: str) -> Iterator[Path]:
    """Yield stored backups of this manifest's release that still verify, newest first.

    A backup is trusted for its bytes, not its name: each one is unpacked and
    checked against the manifest before it is offered.
    """

    spec = load_release_spec(manifest_path)
    name = BACKUP_ARTIFACT_PREFIX + hashlib.sha256(spec.manifest_bytes).hexdigest()
    listing = json.loads(
        _gh_api("--method", "GET", f"repos/{repository}/actions/artifacts", "-f", f"name={name}", "-f", "per_page=100")
    )
    artifacts = [
        artifact
        for artifact in listing.get("artifacts") or []
        if isinstance(artifact, dict) and artifact.get("name") == name and artifact.get("expired") is False
    ]
    work_dir.mkdir(parents=True, exist_ok=True)
    for artifact in sorted(artifacts, key=lambda item: str(item.get("created_at")), reverse=True):
        identifier = artifact.get("id")
        archive = work_dir / f"backup-{identifier}.zip"
        candidate = work_dir / f"backup-{identifier}"
        try:
            _gh_api(f"repos/{repository}/actions/artifacts/{identifier}/zip", destination=archive)
            _extract_backup(archive, candidate, spec)
            verify_release_assets(manifest_path, candidate)
        except (ReleaseGuardError, OSError, zipfile.BadZipFile) as exc:
            print(f"Skipping unverified backup {identifier}: {exc}", file=sys.stderr)
            continue
        finally:
            archive.unlink(missing_ok=True)
        yield candidate


def recover_release(manifest_path: Path, restore_dir: Path, *, repository: str, target: str) -> ReleaseSpec:
    """Put back what a release lost, using bytes its manifest pins.

    Only asset names that are missing are uploaded. An asset that exists under
    its expected name is never replaced: when every name is present and the
    published release still does not verify, published bytes changed, and this
    stops instead of overwriting them. A draft is repaired and published
    through ``publish_draft_release``; a release that is gone is created again.
    """

    spec = verify_release_assets(manifest_path, restore_dir)
    try:
        snapshot = json.loads(_gh_release(repository, "view", spec.asset_release_tag, "--json", "isDraft,assets"))
    except ReleaseGuardError:
        snapshot = None
    if snapshot is None:
        _gh_release(
            repository, "create", spec.asset_release_tag,
            "--target", target, "--latest=false",
            "--title", f"Avibe Model Hub engine {spec.version}",
            "--notes", "Avibe-managed CPA assets recovered from a manifest-verified backup.",
            *(str(restore_dir / name) for name in sorted(spec.expected_asset_names)),
        )
        return spec
    is_draft = snapshot.get("isDraft") is True
    existing = {asset.get("name") for asset in snapshot.get("assets") or [] if isinstance(asset, dict)}
    missing = sorted(spec.expected_asset_names - existing)
    if missing:
        _gh_release(repository, "upload", spec.asset_release_tag, *(str(restore_dir / name) for name in missing))
    elif not is_draft:
        raise ReleaseGuardError(
            "every expected asset name exists but verification failed; refusing to overwrite pinned bytes"
        )
    if is_draft:
        publish_draft_release(manifest_path, restore_dir, repository=repository)
    return spec


def guard_release(
    manifest_path: Path, output_dir: Path, *, repository: str, target: str, work_dir: Path,
) -> tuple[ReleaseSpec, bool]:
    """Verify a published release, restoring it from a verified backup when it does not verify.

    Returns the release and whether it had to be recovered. This restores
    exactly the bytes a manifest pins. It decides nothing about which engine
    anyone adopts: it holds no channel key and never writes the channel index.
    """

    try:
        return fetch_release_assets(manifest_path, output_dir), False
    except ReleaseGuardError as exc:
        print(f"The published release does not verify: {exc}", file=sys.stderr)
    restore = next(verified_backups(manifest_path, work_dir, repository=repository), None)
    if restore is None:
        raise ReleaseGuardError("the published release does not verify and no verified backup of it exists")
    recover_release(manifest_path, restore, repository=repository, target=target)
    return fetch_release_assets(manifest_path, output_dir), True


def plan_guarded_releases(packaged_manifest: Path, listed_dir: Path | None, output_dir: Path) -> list[str]:
    """Collect the manifest of every release the guard keeps available.

    That is the engine Avibe packages, and every release the signed channel
    index lists, withdrawn ones and rollback targets included. One release has
    one manifest: the same tag with other bytes is an error, not a choice.
    """

    manifests: dict[str, bytes] = {}
    sources = [packaged_manifest]
    if listed_dir is not None:
        sources += sorted(path for path in listed_dir.glob("*.json") if path.is_file() and not path.is_symlink())
    for source in sources:
        spec = load_release_spec(source)
        if manifests.setdefault(spec.asset_release_tag, spec.manifest_bytes) != spec.manifest_bytes:
            raise ReleaseGuardError(f"{spec.asset_release_tag} is pinned by two different manifests")
    output_dir.mkdir(parents=True, exist_ok=True)
    for tag, manifest_bytes in manifests.items():
        (output_dir / f"{tag}.json").write_bytes(manifest_bytes)
    return sorted(manifests)


def _download(url: str, destination: Path, *, max_bytes: int) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "avibe-model-hub-engine-release-guard/1"},
    )
    try:
        download_to_path(request, destination, max_bytes=max_bytes)
    except ValueError as exc:
        raise ReleaseGuardError(f"release asset exceeds its size limit: {url}") from exc
    except urllib.error.HTTPError as exc:
        raise ReleaseGuardError(f"release asset download failed ({exc.code}): {url}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseGuardError(f"release asset download failed: {url}: {exc}") from exc


def fetch_release_assets(manifest_path: Path, output_dir: Path) -> ReleaseSpec:
    spec = load_release_spec(manifest_path)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        for archive in spec.archives:
            _download(archive.owned_url, staging / archive.name, max_bytes=archive.size)
            if (staging / archive.name).stat().st_size != archive.size:
                raise ReleaseGuardError(f"Model Hub engine archive size mismatch: {archive.name}")
            _download(
                f"{archive.owned_url}.sha256",
                staging / f"{archive.name}.sha256",
                max_bytes=256,
            )
        _download(
            f"{spec.owned_release_url}/model-hub-engine-manifest.json",
            staging / "model-hub-engine-manifest.json",
            max_bytes=max(len(spec.manifest_bytes), 1),
        )
        verify_release_assets(manifest_path, staging)
        if output_dir.exists():
            if not output_dir.is_dir() or output_dir.is_symlink():
                raise ReleaseGuardError("Model Hub engine output path is not a safe directory")
            shutil.rmtree(output_dir)
        staging.replace(output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return spec


def fetch_upstream_assets(manifest_path: Path, output_dir: Path) -> ReleaseSpec:
    try:
        from scripts.build_model_hub_engine import build_source_release
    except ModuleNotFoundError as exc:
        if exc.name != "scripts":
            raise
        from build_model_hub_engine import build_source_release

    generated_manifest = build_source_release(manifest_path, output_dir)
    return load_release_spec(generated_manifest)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch", help="Fetch and verify Avibe-owned release assets.")
    fetch.add_argument("--output-dir", type=Path, required=True)
    fetch_source = subparsers.add_parser(
        "fetch-source",
        help="Build patched source assets for initial Avibe release publication.",
    )
    fetch_source.add_argument("--output-dir", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="Verify a materialized release directory.")
    verify.add_argument("--asset-dir", type=Path, required=True)
    subparsers.add_parser(
        "go-version",
        help="Print and validate the exact stable Go version recorded in the manifest.",
    )
    publish_draft = subparsers.add_parser(
        "publish-draft", help="Repair and verify a draft using manifest-pinned assets before publication.",
    )
    publish_draft.add_argument("--asset-dir", type=Path, required=True)
    publish_draft.add_argument("--repo", required=True)
    guard = subparsers.add_parser(
        "guard", help="Verify the published release; restore it from a verified backup if it does not verify.",
    )
    guard.add_argument("--repo", required=True)
    guard.add_argument("--target", required=True, help="commit a recreated release points at")
    guard.add_argument("--output-dir", type=Path, required=True)
    guard.add_argument("--work-dir", type=Path, required=True)
    plan = subparsers.add_parser(
        "plan", help="Collect the manifests of the packaged engine and of every channel-listed release.",
    )
    plan.add_argument("--listed-dir", type=Path)
    plan.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "fetch":
            spec = fetch_release_assets(args.manifest, args.output_dir)
        elif args.command == "fetch-source":
            spec = fetch_upstream_assets(args.manifest, args.output_dir)
        elif args.command == "publish-draft":
            spec = publish_draft_release(args.manifest, args.asset_dir, repository=args.repo)
        elif args.command == "go-version":
            print(manifest_go_version(args.manifest))
            return 0
        elif args.command == "plan":
            tags = plan_guarded_releases(args.manifest, args.listed_dir, args.output_dir)
            print(json.dumps({"ok": True, "releases": tags}))
            return 0
        elif args.command == "guard":
            spec, recovered = guard_release(
                args.manifest, args.output_dir, repository=args.repo, target=args.target, work_dir=args.work_dir,
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "asset_release_tag": spec.asset_release_tag,
                        "manifest_sha256": hashlib.sha256(spec.manifest_bytes).hexdigest(),
                        "recovered": recovered,
                    }
                )
            )
            return 0
        else:
            spec = verify_release_assets(args.manifest, args.asset_dir)
    except ReleaseGuardError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "version": spec.version,
                "asset_release_tag": spec.asset_release_tag,
                "asset_count": len(spec.expected_asset_names),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
