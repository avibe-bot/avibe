"""The strict schema of a published Model Hub engine release manifest.

One validator for both sides of an engine release. The release guard checks
what it publishes with it, and the engine channel checks every manifest it is
about to trust with it, so a manifest a client accepts is one publication
could have produced. See docs/plans/model-hub-engine-channel.md.

Standard library only: the release scripts load this module on a bare
runner, before any dependency is installed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OWNED_RELEASE_ROOT = "https://github.com/avibe-bot/avibe/releases/download"
EXPECTED_PLATFORMS = frozenset(
    {
        "darwin-arm64",
        "darwin-x64",
        "linux-amd64",
        "linux-arm64",
        "windows-amd64",
        "windows-arm64",
    }
)
UPSTREAM_PLATFORM_NAMES = {
    "darwin-arm64": "darwin_aarch64",
    "darwin-x64": "darwin_amd64",
    "linux-amd64": "linux_amd64",
    "linux-arm64": "linux_aarch64",
    "windows-amd64": "windows_amd64",
    "windows-arm64": "windows_aarch64",
}
ASSET_TAG_RE = re.compile(r"model-hub-engine-v[0-9]+\.[0-9]+\.[0-9]+-[1-9][0-9]*")
VERSION_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MANIFEST_ASSET_NAME = "model-hub-engine-manifest.json"

_REVISION_RE = re.compile(r"model-hub-engine-v([0-9]+)\.([0-9]+)\.([0-9]+)-([1-9][0-9]*)")


class EngineManifestError(ValueError):
    """Raised when bytes are not a publishable Model Hub engine manifest."""


@dataclass(frozen=True)
class ArchiveSpec:
    platform: str
    name: str
    owned_url: str
    size: int
    sha256: str
    binary_sha256: str
    bin_path: str


@dataclass(frozen=True)
class ReleaseSpec:
    manifest_bytes: bytes
    version: str
    upstream_release_tag: str
    asset_release_tag: str
    owned_release_url: str
    archives: tuple[ArchiveSpec, ...]

    @property
    def expected_asset_names(self) -> set[str]:
        names = {MANIFEST_ASSET_NAME}
        for archive in self.archives:
            names.add(archive.name)
            names.add(f"{archive.name}.sha256")
        return names

    @property
    def revision(self) -> tuple[int, int, int, int]:
        return release_revision(self.asset_release_tag)

    def archive_for(self, platform: str) -> ArchiveSpec | None:
        return next((archive for archive in self.archives if archive.platform == platform), None)


def release_revision(asset_release_tag: str) -> tuple[int, int, int, int]:
    """Order an engine release: upstream major, minor, patch, then Avibe build.

    The same upstream version rebuilt by Avibe is a different, newer release,
    so the build revision takes part in the comparison.
    """

    match = _REVISION_RE.fullmatch(asset_release_tag) if isinstance(asset_release_tag, str) else None
    if match is None:
        raise EngineManifestError("Model Hub engine asset release tag is invalid")
    major, minor, patch, build = (int(part) for part in match.groups())
    return major, minor, patch, build


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EngineManifestError(f"Model Hub engine manifest repeats the field {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise EngineManifestError(f"Model Hub engine manifest contains the non-JSON number {name}")


def _required_string(payload: dict, key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise EngineManifestError(f"{context}.{key} must be a non-empty string")
    return value


def parse_release_manifest(manifest_bytes: bytes) -> ReleaseSpec:
    """Validate exact manifest bytes and describe the release they pin.

    The bytes are kept as given: a manifest is identified by its digest, so
    nothing here reserializes it. A repeated field is rejected because readers
    that keep the first value and readers that keep the last would otherwise
    disagree about which release the same bytes pin.
    """

    try:
        payload = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineManifestError(f"cannot read Model Hub engine manifest: {exc}") from exc
    except RecursionError as exc:
        raise EngineManifestError("cannot read Model Hub engine manifest: nesting is too deep") from exc

    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise EngineManifestError("Model Hub engine manifest schema_version must be 1")
    if payload.get("name") != "cliproxyapi":
        raise EngineManifestError("Model Hub engine manifest name must be cliproxyapi")
    if payload.get("source") != "router-for-me/CLIProxyAPI":
        raise EngineManifestError("Model Hub engine manifest source is invalid")
    if payload.get("license") != "MIT":
        raise EngineManifestError("Model Hub engine manifest license is invalid")

    version = _required_string(payload, "version", "manifest")
    upstream_release_tag = _required_string(payload, "release_tag", "manifest")
    asset_release_tag = _required_string(payload, "asset_release_tag", "manifest")
    source_sha = _required_string(payload, "source_sha", "manifest")
    source_url = _required_string(payload, "source_url", "manifest")
    if VERSION_RE.fullmatch(version) is None or upstream_release_tag != version:
        raise EngineManifestError("Model Hub engine version and upstream release tag are invalid")
    if ASSET_TAG_RE.fullmatch(asset_release_tag) is None:
        raise EngineManifestError("Model Hub engine asset release tag is invalid")
    if not asset_release_tag.startswith(f"model-hub-engine-{version}-"):
        raise EngineManifestError("Model Hub engine asset release tag does not match version")
    if SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise EngineManifestError("Model Hub engine source SHA is invalid")
    if source_url != f"https://github.com/router-for-me/CLIProxyAPI/tree/{source_sha}":
        raise EngineManifestError("Model Hub engine source URL is invalid")

    owned_release_url = f"{OWNED_RELEASE_ROOT}/{asset_release_tag}"
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise EngineManifestError("Model Hub engine manifest assets must be a list")

    archives: list[ArchiveSpec] = []
    seen_platforms: set[str] = set()
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_assets):
        context = f"assets[{index}]"
        if not isinstance(raw, dict):
            raise EngineManifestError(f"{context} must be an object")
        platform = _required_string(raw, "platform", context)
        owned_url = _required_string(raw, "url", context)
        sha256 = _required_string(raw, "sha256", context)
        binary_sha256 = _required_string(raw, "binary_sha256", context)
        bin_path = _required_string(raw, "bin_path", context)
        size = raw.get("size_bytes")
        name = owned_url.rsplit("/", 1)[-1]
        expected_name = (
            f"CLIProxyAPI_{version.removeprefix('v')}_"
            f"{UPSTREAM_PLATFORM_NAMES.get(platform, '')}.tar.gz"
        )
        if platform in seen_platforms or platform not in EXPECTED_PLATFORMS:
            raise EngineManifestError(f"{context}.platform is invalid or duplicated")
        if Path(name).name != name or name in seen_names or name != expected_name:
            raise EngineManifestError(f"{context}.url has an invalid asset name")
        if owned_url != f"{owned_release_url}/{name}":
            raise EngineManifestError(f"{context}.url is outside the Avibe-owned release")
        if SHA256_RE.fullmatch(sha256) is None or SHA256_RE.fullmatch(binary_sha256) is None:
            raise EngineManifestError(f"{context} has an invalid digest")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise EngineManifestError(f"{context}.size_bytes is invalid")
        if bin_path != ("cli-proxy-api.exe" if platform.startswith("windows-") else "cli-proxy-api"):
            raise EngineManifestError(f"{context}.bin_path is invalid")
        seen_platforms.add(platform)
        seen_names.add(name)
        archives.append(
            ArchiveSpec(
                platform=platform,
                name=name,
                owned_url=owned_url,
                size=size,
                sha256=sha256,
                binary_sha256=binary_sha256,
                bin_path=bin_path,
            )
        )
    if seen_platforms != EXPECTED_PLATFORMS:
        raise EngineManifestError("Model Hub engine manifest platform set is invalid")

    return ReleaseSpec(
        manifest_bytes=bytes(manifest_bytes),
        version=version,
        upstream_release_tag=upstream_release_tag,
        asset_release_tag=asset_release_tag,
        owned_release_url=owned_release_url,
        archives=tuple(sorted(archives, key=lambda archive: archive.platform)),
    )
