from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


REPOSITORY = "avibe-bot/avibe"
RUNTIME_REPOSITORY = "avibe-bot/vibe-show-runtime"
MANIFEST_ASSET_NAME = "show-runtime-manifest.json"
EXPECTED_PLATFORMS = {
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "win32-arm64",
    "win32-x64",
}
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
UrlOpener = Callable[..., Any]


def validate_manifest_bytes(content: bytes, *, release_tag: str | None = None) -> dict[str, Any]:
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Show Runtime manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Show Runtime manifest must be a JSON object")
    if manifest.get("schema_version") != 1:
        raise ValueError("Show Runtime manifest schema_version must be 1")

    runtime_version = manifest.get("runtime_version")
    if not isinstance(runtime_version, str) or not _COMMIT_RE.fullmatch(runtime_version):
        raise ValueError("Show Runtime manifest runtime_version must be a 40-character hex commit")
    runtime_source = manifest.get("runtime_source")
    if not isinstance(runtime_source, dict):
        raise ValueError("Show Runtime manifest runtime_source must be an object")
    if runtime_source.get("repo") != RUNTIME_REPOSITORY or runtime_source.get("ref") != runtime_version:
        raise ValueError("Show Runtime manifest runtime_source must pin the declared runtime_version")
    if not isinstance(manifest.get("minimum_node"), str) or not manifest["minimum_node"]:
        raise ValueError("Show Runtime manifest minimum_node must be non-empty")

    archives = manifest.get("archives")
    if not isinstance(archives, dict) or set(archives) != EXPECTED_PLATFORMS:
        missing = sorted(EXPECTED_PLATFORMS - set(archives or {}))
        extra = sorted(set(archives or {}) - EXPECTED_PLATFORMS)
        raise ValueError(f"Show Runtime manifest platforms mismatch; missing={missing}, extra={extra}")
    for platform, archive in archives.items():
        _validate_archive(platform, archive, release_tag=release_tag)
    return manifest


def validate_manifest_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(
            "Cannot build an installable Avibe artifact without vibe/show_runtime_manifest.json. "
            "Run `python scripts/prepare_local_show_runtime_manifest.py` first."
        )
    try:
        return validate_manifest_bytes(path.read_bytes())
    except ValueError as exc:
        raise RuntimeError(f"Invalid {path}: {exc}") from exc


def prepare_manifest(
    output: Path,
    *,
    release_tag: str | None = None,
    opener: UrlOpener = urllib.request.urlopen,
) -> dict[str, Any]:
    release_url = _release_api_url(release_tag)
    release = _read_json(release_url, opener=opener)
    resolved_tag = release.get("tag_name")
    if not isinstance(resolved_tag, str) or not resolved_tag:
        raise RuntimeError("GitHub release response is missing tag_name")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError(f"GitHub release {resolved_tag} has no assets")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == MANIFEST_ASSET_NAME]
    if len(matches) != 1:
        raise RuntimeError(f"GitHub release {resolved_tag} must contain exactly one {MANIFEST_ASSET_NAME}")

    asset = matches[0]
    download_url = asset.get("browser_download_url")
    digest = asset.get("digest")
    if not isinstance(download_url, str) or not download_url.startswith("https://github.com/"):
        raise RuntimeError(f"GitHub release {resolved_tag} has an invalid manifest download URL")
    if not isinstance(digest, str) or not digest.startswith("sha256:") or not _SHA256_RE.fullmatch(digest[7:]):
        raise RuntimeError(f"GitHub release {resolved_tag} is missing the manifest SHA256 digest")

    content = _read_bytes(download_url, opener=opener)
    actual_digest = hashlib.sha256(content).hexdigest()
    if actual_digest != digest[7:]:
        raise RuntimeError(
            f"Show Runtime manifest digest mismatch for {resolved_tag}: expected {digest[7:]}, got {actual_digest}"
        )
    manifest = validate_manifest_bytes(content, release_tag=resolved_tag)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return manifest


def _validate_archive(platform: str, archive: object, *, release_tag: str | None) -> None:
    if not isinstance(archive, dict):
        raise ValueError(f"Show Runtime archive {platform} must be an object")
    expected_name = f"vibe-show-runtime-node-{platform}.tgz"
    if archive.get("name") != expected_name:
        raise ValueError(f"Show Runtime archive {platform} must be named {expected_name}")
    sha256 = archive.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise ValueError(f"Show Runtime archive {platform} has an invalid SHA256")
    size = archive.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError(f"Show Runtime archive {platform} has an invalid size")
    url = archive.get("url")
    if not isinstance(url, str):
        raise ValueError(f"Show Runtime archive {platform} has no URL")
    parsed = urllib.parse.urlparse(url)
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    expected_prefix = ["avibe-bot", "avibe", "releases", "download"]
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parts[:4] != expected_prefix:
        raise ValueError(f"Show Runtime archive {platform} must use an immutable avibe-bot/avibe release URL")
    if len(parts) != 6 or parts[-1] != expected_name:
        raise ValueError(f"Show Runtime archive {platform} has an invalid release URL path")
    if release_tag is not None and parts[4] != release_tag:
        raise ValueError(f"Show Runtime archive {platform} does not belong to release {release_tag}")


def _release_api_url(release_tag: str | None) -> str:
    base = f"https://api.github.com/repos/{REPOSITORY}/releases"
    if release_tag is None:
        return f"{base}/latest"
    return f"{base}/tags/{urllib.parse.quote(release_tag, safe='')}"


def _read_json(url: str, *, opener: UrlOpener) -> dict[str, Any]:
    content = _read_bytes(url, opener=opener, accept="application/vnd.github+json")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"GitHub returned invalid JSON from {url}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"GitHub returned an invalid release payload from {url}")
    return value


def _read_bytes(url: str, *, opener: UrlOpener, accept: str = "application/octet-stream") -> bytes:
    headers = {"Accept": accept, "User-Agent": "avibe-local-wheel-builder"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlparse(url).hostname in {"api.github.com", "github.com"}:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    with opener(request, timeout=30) as response:
        return response.read()
