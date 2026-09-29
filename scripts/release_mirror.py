#!/usr/bin/env python3
"""Mirror published GitHub Release assets into the dl.avibe.bot R2 bucket.

GitHub stays the source of truth. The mirror holds byte-identical copies under
``releases/<tag>/<asset>`` and a release index at ``index/releases.json``; see
``docs/plans/release-download-mirror.md`` for the contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote


RELEASE_PREFIX = "releases/"
INDEX_KEY = "index/releases.json"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
INDEX_CACHE_CONTROL = "public, max-age=60"
DEFAULT_KEEP_PRERELEASES = 20
INDEX_SCHEMA_VERSION = 1

_NAME_RE = re.compile(r"[A-Za-z0-9._+-]+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


class MirrorError(RuntimeError):
    """Raised when the mirror cannot be proven to match GitHub."""


@dataclass(frozen=True)
class Asset:
    name: str
    size: int
    sha256: str
    url: str
    content_type: str


@dataclass(frozen=True)
class Release:
    tag: str
    prerelease: bool
    published_at: str
    commit: str
    assets: tuple[Asset, ...]

    def key(self, asset: Asset) -> str:
        return f"{RELEASE_PREFIX}{self.tag}/{asset.name}"


@dataclass(frozen=True)
class Plan:
    uploads: tuple[tuple[str, Asset], ...]
    deletions: tuple[str, ...]
    # Keys whose published bytes changed upstream. Edge caches may still hold
    # the old copy for up to a year, so these fail the run after repair.
    changed: tuple[str, ...]
    index: bytes


def parse_releases(
    repository: str, payload: Sequence[Mapping[str, Any]], commits: Mapping[str, str]
) -> list[Release]:
    """Return every published release, validated against the mirror contract."""
    releases = []
    for item in payload:
        if item["draft"]:
            continue
        tag = item["tag_name"]
        if not _NAME_RE.fullmatch(tag):
            raise MirrorError(f"release tag is not mirrorable: {tag!r}")
        commit = commits.get(tag)
        if commit is None or not _COMMIT_RE.fullmatch(commit):
            raise MirrorError(f"published release has no tag commit: {tag}")
        assets = []
        for raw in item["assets"]:
            name = raw["name"]
            if not _NAME_RE.fullmatch(name):
                raise MirrorError(f"asset name is not mirrorable: {tag}/{name!r}")
            # Clients derive a mirror URL by replacing this exact prefix.
            expected = f"https://github.com/{repository}/releases/download/{quote(tag)}/{quote(name)}"
            if raw["browser_download_url"] != expected:
                raise MirrorError(f"unexpected download URL for {tag}/{name}: {raw['browser_download_url']}")
            algorithm, _, digest = str(raw.get("digest") or "").partition(":")
            if algorithm != "sha256" or not _SHA256_RE.fullmatch(digest):
                raise MirrorError(f"asset has no sha256 digest: {tag}/{name}")
            assets.append(
                Asset(
                    name=name,
                    size=int(raw["size"]),
                    sha256=digest,
                    url=expected,
                    content_type=raw.get("content_type") or "application/octet-stream",
                )
            )
        releases.append(
            Release(
                tag=tag,
                prerelease=bool(item["prerelease"]),
                published_at=item["published_at"],
                commit=commit,
                assets=tuple(sorted(assets, key=lambda asset: asset.name)),
            )
        )
    return releases


def select(releases: Sequence[Release], keep_prereleases: int) -> list[Release]:
    """Keep every full release and the newest prereleases, newest first."""
    ordered = sorted(releases, key=lambda release: (release.published_at, release.tag), reverse=True)
    prereleases = [release for release in ordered if release.prerelease][:keep_prereleases]
    return [release for release in ordered if not release.prerelease or release in prereleases]


def render_index(repository: str, releases: Sequence[Release]) -> bytes:
    document = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "repository": repository,
        "releases": [
            {
                "tag": release.tag,
                "prerelease": release.prerelease,
                "published_at": release.published_at,
                "commit": release.commit,
                "assets": [
                    {"name": asset.name, "size": asset.size, "sha256": asset.sha256}
                    for asset in release.assets
                ],
            }
            for release in releases
        ],
    }
    return (json.dumps(document, separators=(",", ":")) + "\n").encode()


def _indexed_digests(index: bytes | None) -> dict[str, str]:
    if index is None:
        return {}
    try:
        document = json.loads(index)
        return {
            f"{RELEASE_PREFIX}{release['tag']}/{asset['name']}": asset["sha256"]
            for release in document["releases"]
            for asset in release["assets"]
        }
    except (ValueError, KeyError, TypeError) as exc:
        # The index is this job's derived state; re-verify every object instead.
        print(f"warning: ignoring unreadable mirror index: {exc}", file=sys.stderr)
        return {}


def plan(
    repository: str,
    releases: Sequence[Release],
    objects: Mapping[str, int],
    previous_index: bytes | None,
) -> Plan:
    """Compare the selected releases with the bucket's ``releases/`` objects."""
    if not releases:
        raise MirrorError("GitHub returned no published releases; refusing to reconcile")
    indexed = _indexed_digests(previous_index)
    uploads, changed, expected = [], [], set()
    for release in releases:
        for asset in release.assets:
            key = release.key(asset)
            expected.add(key)
            prior = indexed.get(key)
            if prior is not None and prior != asset.sha256:
                changed.append(key)
                uploads.append((key, asset))
            elif prior is None or objects.get(key) != asset.size:
                # Only an object this job verified and indexed is trusted.
                uploads.append((key, asset))
    deletions = sorted(key for key in objects if key.startswith(RELEASE_PREFIX) and key not in expected)
    return Plan(
        uploads=tuple(uploads),
        deletions=tuple(deletions),
        changed=tuple(changed),
        index=render_index(repository, releases),
    )


def download(asset: Asset, destination: Path, *, attempts: int = 3) -> None:
    """Download ``asset`` and prove its size and sha256 match GitHub's digest."""
    request = urllib.request.Request(asset.url, headers={"User-Agent": "avibe-release-mirror/1"})
    for attempt in range(1, attempts + 1):
        digest, size = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > asset.size:
                        break
                    digest.update(chunk)
                    output.write(chunk)
        except urllib.error.HTTPError as exc:
            if not (exc.code == 429 or 500 <= exc.code < 600) or attempt == attempts:
                raise MirrorError(f"download failed ({exc.code}): {asset.url}") from exc
        except (OSError, urllib.error.URLError) as exc:
            if attempt == attempts:
                raise MirrorError(f"download failed: {asset.url}: {exc}") from exc
        else:
            if size != asset.size or digest.hexdigest() != asset.sha256:
                raise MirrorError(f"downloaded bytes do not match GitHub's digest: {asset.url}")
            return
        time.sleep(float(attempt))


def _run(command: Sequence[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise MirrorError(f"{command[0]} {command[1]} failed: {detail}")
    return completed.stdout


def github_releases(repository: str) -> list[dict[str, Any]]:
    pages = json.loads(_run(["gh", "api", "--paginate", "--slurp", f"repos/{repository}/releases?per_page=100"]))
    return [release for page in pages for release in page]


def tag_commits(repository: str) -> dict[str, str]:
    commits: dict[str, str] = {}
    listing = _run(["git", "ls-remote", "--tags", f"https://github.com/{repository}.git"])
    for line in listing.splitlines():
        sha, ref = line.split("\t")
        tag = ref.removeprefix("refs/tags/")
        if tag.endswith("^{}"):
            commits[tag[:-3]] = sha
        else:
            commits.setdefault(tag, sha)
    return commits


class Bucket:
    def __init__(self, name: str, endpoint_url: str) -> None:
        self.name = name
        self.endpoint_url = endpoint_url

    def _s3api(self, *arguments: str) -> str:
        return _run(["aws", "s3api", *arguments, "--bucket", self.name, "--endpoint-url", self.endpoint_url])

    def objects(self) -> dict[str, int]:
        output = self._s3api("list-objects-v2", "--output", "json")
        listing = json.loads(output) if output.strip() else {}
        return {item["Key"]: int(item["Size"]) for item in listing.get("Contents") or []}

    def read(self, key: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="avibe-release-mirror-") as temporary:
            path = Path(temporary) / "object"
            self._s3api("get-object", "--key", key, str(path))
            return path.read_bytes()

    def put(self, key: str, path: Path, *, content_type: str, cache_control: str, sha256: str) -> None:
        self._s3api(
            "put-object", "--key", key, "--body", str(path),
            "--content-type", content_type, "--cache-control", cache_control,
            "--metadata", f"sha256={sha256}",
        )

    def delete(self, key: str) -> None:
        # DeleteObjects requires a request checksum that R2 may not implement.
        self._s3api("delete-object", "--key", key)


def reconcile(repository: str, bucket: Bucket, *, keep_prereleases: int, dry_run: bool) -> int:
    releases = select(parse_releases(repository, github_releases(repository), tag_commits(repository)), keep_prereleases)
    objects = bucket.objects()
    previous_index = bucket.read(INDEX_KEY) if INDEX_KEY in objects else None
    result = plan(repository, releases, objects, previous_index)
    size = sum(asset.size for _, asset in result.uploads)
    print(
        f"{len(releases)} releases mirrored; {len(result.uploads)} uploads ({size / 1e9:.2f} GB), "
        f"{len(result.deletions)} deletions, index {'unchanged' if result.index == previous_index else 'changed'}"
    )
    for key in result.changed:
        print(f"error: published bytes changed upstream: {key}", file=sys.stderr)
    if dry_run:
        for key, _ in result.uploads:
            print(f"would upload {key}")
        for key in result.deletions:
            print(f"would delete {key}")
    else:
        with tempfile.TemporaryDirectory(prefix="avibe-release-mirror-") as temporary:
            path = Path(temporary) / "asset"
            for key, asset in result.uploads:
                download(asset, path)
                bucket.put(
                    key, path, content_type=asset.content_type,
                    cache_control=IMMUTABLE_CACHE_CONTROL, sha256=asset.sha256,
                )
                print(f"uploaded {key}")
            if result.index != previous_index:
                path.write_bytes(result.index)
                bucket.put(
                    INDEX_KEY, path, content_type="application/json",
                    cache_control=INDEX_CACHE_CONTROL, sha256=hashlib.sha256(result.index).hexdigest(),
                )
                print(f"wrote {INDEX_KEY}")
        # Delete only after the index stops listing these objects.
        for key in result.deletions:
            bucket.delete(key)
            print(f"deleted {key}")
    if result.changed:
        print("error: purge the changed URLs from the dl.avibe.bot cache", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--endpoint-url", required=True)
    parser.add_argument("--keep-prereleases", type=int, default=DEFAULT_KEEP_PRERELEASES)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        return reconcile(
            arguments.repository,
            Bucket(arguments.bucket, arguments.endpoint_url),
            keep_prereleases=arguments.keep_prereleases,
            dry_run=arguments.dry_run,
        )
    except MirrorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
