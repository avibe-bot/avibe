from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.show_runtime_manifest_asset import (
    EXPECTED_PLATFORMS,
    prepare_manifest,
    validate_manifest_bytes,
    validate_manifest_file,
)


RELEASE_TAG = "v3.0.8"
RUNTIME_VERSION = "c2d5acc3a021cf62161919214a63a51ff313351b"


class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self.content


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "runtime_version": RUNTIME_VERSION,
        "runtime_source": {"repo": "avibe-bot/vibe-show-runtime", "ref": RUNTIME_VERSION},
        "minimum_node": "^20.19.0 || >=22.12.0",
        "archives": {
            platform: {
                "name": f"vibe-show-runtime-node-{platform}.tgz",
                "url": (
                    f"https://github.com/avibe-bot/avibe/releases/download/{RELEASE_TAG}/"
                    f"vibe-show-runtime-node-{platform}.tgz"
                ),
                "sha256": hashlib.sha256(platform.encode()).hexdigest(),
                "size": 100,
            }
            for platform in EXPECTED_PLATFORMS
        },
    }


def test_validate_manifest_requires_the_complete_pinned_platform_set() -> None:
    manifest = _manifest()
    manifest["archives"].pop("linux-x64")

    with pytest.raises(ValueError, match="platforms mismatch"):
        validate_manifest_bytes(json.dumps(manifest).encode(), release_tag=RELEASE_TAG)


@pytest.mark.parametrize(
    "minimum_node",
    ["", " ", "definitely-not-a-node-range", "^20", ">=22.12.0 <23.0.0", ">=22.12.0 ||"],
)
def test_validate_manifest_rejects_unsupported_node_requirements(minimum_node: str) -> None:
    manifest = _manifest()
    manifest["minimum_node"] = minimum_node

    with pytest.raises(ValueError, match="unsupported Node requirement"):
        validate_manifest_bytes(json.dumps(manifest).encode(), release_tag=RELEASE_TAG)


def test_validate_manifest_file_rejects_an_uninstallable_wheel_source(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Cannot build an installable Avibe artifact"):
        validate_manifest_file(tmp_path / "missing.json")


def test_prepare_manifest_verifies_release_asset_digest_and_writes_output(tmp_path: Path) -> None:
    manifest_content = (json.dumps(_manifest()) + "\n").encode()
    asset_url = f"https://github.com/avibe-bot/avibe/releases/download/{RELEASE_TAG}/show-runtime-manifest.json"
    release_content = json.dumps(
        {
            "tag_name": RELEASE_TAG,
            "assets": [
                {
                    "name": "show-runtime-manifest.json",
                    "browser_download_url": asset_url,
                    "digest": f"sha256:{hashlib.sha256(manifest_content).hexdigest()}",
                }
            ],
        }
    ).encode()

    def opener(request, timeout):
        assert timeout == 30
        return _Response(manifest_content if request.full_url == asset_url else release_content)

    output = tmp_path / "vibe" / "show_runtime_manifest.json"
    result = prepare_manifest(output, release_tag=RELEASE_TAG, opener=opener)

    assert result["runtime_version"] == RUNTIME_VERSION
    assert output.read_bytes() == manifest_content


def test_prepare_manifest_rejects_a_release_asset_digest_mismatch(tmp_path: Path) -> None:
    manifest_content = json.dumps(_manifest()).encode()
    asset_url = f"https://github.com/avibe-bot/avibe/releases/download/{RELEASE_TAG}/show-runtime-manifest.json"
    release_content = json.dumps(
        {
            "tag_name": RELEASE_TAG,
            "assets": [
                {
                    "name": "show-runtime-manifest.json",
                    "browser_download_url": asset_url,
                    "digest": f"sha256:{'0' * 64}",
                }
            ],
        }
    ).encode()

    def opener(request, timeout):
        return _Response(manifest_content if request.full_url == asset_url else release_content)

    with pytest.raises(RuntimeError, match="digest mismatch"):
        prepare_manifest(tmp_path / "manifest.json", release_tag=RELEASE_TAG, opener=opener)
