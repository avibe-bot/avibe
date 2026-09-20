from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
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


def _load_prepare_script(monkeypatch: pytest.MonkeyPatch):
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts" / "prepare_local_show_runtime_manifest.py"
    monkeypatch.syspath_prepend(str(script.parent))
    spec = importlib.util.spec_from_file_location("prepare_local_show_runtime_manifest", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@pytest.mark.parametrize("invocation_directory", ["repository", "scripts"])
def test_prepare_local_manifest_default_output_is_repository_relative(
    invocation_directory: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = Path(__file__).resolve().parents[1]
    module = _load_prepare_script(monkeypatch)
    captured_output: list[Path] = []

    def fake_prepare_manifest(output: Path, *, release_tag: str | None = None):
        captured_output.append(output)
        return {"runtime_version": RUNTIME_VERSION, "archives": {"linux-x64": {}}}

    monkeypatch.setattr(module, "prepare_manifest", fake_prepare_manifest)
    monkeypatch.setattr(sys, "argv", ["prepare_local_show_runtime_manifest.py"])
    monkeypatch.chdir(repository if invocation_directory == "repository" else repository / "scripts")

    assert module.main() == 0
    assert captured_output == [repository / "vibe" / "show_runtime_manifest.json"]
    assert json.loads(capsys.readouterr().out)["output"] == str(captured_output[0])


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


@pytest.mark.parametrize("newest_broken", [False, True])
def test_default_supplier_paginates_official_releases_before_validating_assets(tmp_path, newest_broken):
    content = (json.dumps(_manifest()) + "\n").encode()
    url = f"https://github.com/avibe-bot/avibe/releases/download/{RELEASE_TAG}/show-runtime-manifest.json"
    official = {
        "tag_name": RELEASE_TAG, "draft": False, "prerelease": False,
        "assets": [{"name": "show-runtime-manifest.json", "browser_download_url": url,
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest()}],
    }
    first = [
        {"tag_name": f"model-hub-engine-v99.0.{index}", "draft": False, "prerelease": False}
        for index in range(100)
    ]
    second = [
        official,
        {"tag_name": "v3.0.7", "draft": False, "prerelease": False, "assets": []},
        {"tag_name": "v9.0.0", "draft": True, "prerelease": False},
        {"tag_name": "v8.0.0", "draft": False, "prerelease": True},
        {"tag_name": "v7.0.0rc1", "draft": False, "prerelease": False},
        {"tag_name": "gh-v6.0.0", "draft": False, "prerelease": False},
    ]
    if newest_broken:
        second.append({"tag_name": "v4.0.0", "draft": False, "prerelease": False, "assets": []})
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == url:
            return _Response(content)
        assert request.full_url.startswith("https://api.github.com/repos/avibe-bot/avibe/releases?")
        assert "/latest" not in request.full_url
        if request.full_url.endswith("&page=1"):
            return _Response(json.dumps(first).encode())
        assert request.full_url.endswith("&page=2")
        return _Response(json.dumps(second).encode())

    output = tmp_path / "manifest.json"
    if newest_broken:
        output.write_bytes(b"previous verified manifest")
        with pytest.raises(RuntimeError, match="v4.0.0 must contain exactly one"):
            prepare_manifest(output, opener=opener)
        assert output.read_bytes() == b"previous verified manifest"
        assert requests == [
            f"https://api.github.com/repos/avibe-bot/avibe/releases?per_page=100&page={page}"
            for page in (1, 2)
        ]
    else:
        assert prepare_manifest(output, opener=opener)["runtime_version"] == RUNTIME_VERSION
        assert output.read_bytes() == content
        assert len(requests) == 3


@pytest.mark.parametrize("payload", [[], {}, [None], [{"tag_name": "v3.1.0"}]])
def test_default_supplier_fails_closed_without_complete_release_evidence(tmp_path, payload):
    output = tmp_path / "manifest.json"
    with pytest.raises(RuntimeError):
        prepare_manifest(output, opener=lambda *args, **kwargs: _Response(json.dumps(payload).encode()))
    assert not output.exists()


@pytest.mark.parametrize("mismatch", ["tag", "repository", "asset-tag"])
def test_explicit_supplier_cannot_redirect_manifest_identity(tmp_path, mismatch):
    content = json.dumps(_manifest()).encode()
    tag = "v3.0.9" if mismatch == "tag" else RELEASE_TAG
    repo = "other/repo" if mismatch == "repository" else "avibe-bot/avibe"
    asset_tag = "v3.0.9" if mismatch == "asset-tag" else RELEASE_TAG
    payload = {
        "tag_name": tag,
        "assets": [{"name": "show-runtime-manifest.json",
                    "browser_download_url": f"https://github.com/{repo}/releases/download/{asset_tag}/show-runtime-manifest.json",
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest()}],
    }
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        assert "/releases/tags/" in request.full_url
        return _Response(json.dumps(payload).encode())

    with pytest.raises(RuntimeError, match="requested tag|invalid manifest download URL"):
        prepare_manifest(tmp_path / "manifest.json", release_tag=RELEASE_TAG, opener=opener)
    assert len(requests) == 1


def test_manifest_build_helpers_are_in_the_sdist_contract(tmp_path):
    import runpy
    import shutil
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    repository = Path(__file__).resolve().parents[1]
    config = tomllib.loads((repository / "pyproject.toml").read_text())
    included = config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    # Recreate the actual explicitly shipped helper set away from this checkout.
    for name in included:
        if name.startswith("scripts/") and "*" not in name:
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repository / name, target)
    helpers = runpy.run_path(str(tmp_path / "scripts/show_runtime_manifest_asset.py"))
    assert helpers["validate_manifest_bytes"](json.dumps(_manifest()).encode()) == _manifest()
