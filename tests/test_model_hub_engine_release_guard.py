from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from scripts import build_model_hub_engine
from scripts import model_hub_engine_release_guard as guard


def _archive(binary: bytes, bin_path: str) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as bundle:
            member = tarfile.TarInfo(bin_path)
            member.mode = 0o755
            member.size = len(binary)
            bundle.addfile(member, io.BytesIO(binary))
    return output.getvalue()


def _manifest(tmp_path: Path) -> tuple[Path, dict[str, bytes], dict[str, bytes]]:
    version = "v7.2.149"
    asset_release_tag = "model-hub-engine-v7.2.149-1"
    owned_root = f"{guard.OWNED_RELEASE_ROOT}/{asset_release_tag}"
    source_sha = "2a6b87aca083a5bf498ac1f68a1b636c500d7aaa"
    owned: dict[str, bytes] = {}
    built: dict[str, bytes] = {}
    assets = []
    for platform in sorted(guard.EXPECTED_PLATFORMS):
        upstream_platform = guard.UPSTREAM_PLATFORM_NAMES[platform]
        name = f"CLIProxyAPI_7.2.149_{upstream_platform}.tar.gz"
        bin_path = "cli-proxy-api.exe" if platform.startswith("windows-") else "cli-proxy-api"
        archive = _archive(f"{platform}-binary".encode(), bin_path)
        archive_sha = hashlib.sha256(archive).hexdigest()
        binary_sha = hashlib.sha256(f"{platform}-binary".encode()).hexdigest()
        url = f"{owned_root}/{name}"
        assets.append(
            {
                "platform": platform,
                "url": url,
                "size_bytes": len(archive),
                "sha256": archive_sha,
                "binary_sha256": binary_sha,
                "bin_path": bin_path,
            }
        )
        owned[url] = archive
        owned[f"{url}.sha256"] = f"{archive_sha}  {name}\n".encode()
        built[name] = archive

    payload = {
        "schema_version": 1,
        "name": "cliproxyapi",
        "version": version,
        "source": "router-for-me/CLIProxyAPI",
        "source_url": f"https://github.com/router-for-me/CLIProxyAPI/tree/{source_sha}",
        "source_sha": source_sha,
        "release_tag": version,
        "asset_release_tag": asset_release_tag,
        "license": "MIT",
        "assets": assets,
    }
    manifest_path = tmp_path / "cliproxyapi_manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    owned[f"{owned_root}/model-hub-engine-manifest.json"] = manifest_path.read_bytes()
    return manifest_path, owned, built


def _fake_download(remote: dict[str, bytes]):
    def download(url: str, destination: Path, **_kwargs) -> None:
        try:
            payload = remote[url]
        except KeyError as exc:
            raise guard.ReleaseGuardError(f"missing test asset: {url}") from exc
        destination.write_bytes(payload)

    return download


def test_manifest_go_version_cli_accepts_alternate_stable_pin(tmp_path: Path) -> None:
    manifest_path, _owned, _built = _manifest(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["build"] = {"go_version": "go1.27.1"}
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(guard.REPO_ROOT / "scripts/model_hub_engine_release_guard.py"),
            "--manifest",
            str(manifest_path),
            "go-version",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "go1.27.1"


@pytest.mark.parametrize(
    "build",
    [{}, {"go_version": "1.27.1"}, {"go_version": "go1.27"}, {"go_version": "go1.27.1-beta1"}],
)
def test_manifest_go_version_cli_rejects_missing_or_nonstable_pin(tmp_path: Path, build: dict) -> None:
    manifest_path, _owned, _built = _manifest(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["build"] = build
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(guard.REPO_ROOT / "scripts/model_hub_engine_release_guard.py"),
            "--manifest",
            str(manifest_path),
            "go-version",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "exact stable Go version" in result.stderr


def _run_workflow_go_pin_step(tmp_path: Path, build: dict | None) -> tuple[subprocess.CompletedProcess[str], bytes, bytes]:
    import yaml

    workflow = yaml.safe_load(
        (guard.REPO_ROOT / ".github/workflows/model-hub-engine-release-guard.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["build-patched-source"]["steps"]
    producer = next(step for step in steps if step.get("id") == "go_pin")["run"]
    staged = tmp_path / "workflow"
    (staged / "scripts").mkdir(parents=True)
    (staged / "vibe/model_hub_runtime").mkdir(parents=True)
    shutil.copy2(
        guard.REPO_ROOT / "scripts/model_hub_engine_release_guard.py",
        staged / "scripts/model_hub_engine_release_guard.py",
    )
    manifest = staged / "vibe/model_hub_runtime/cliproxyapi_manifest.json"
    payload = {"build": build} if build is not None else {}
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    before = manifest.read_bytes()
    output = staged / "github-output"
    environment = os.environ.copy()
    environment["GITHUB_OUTPUT"] = str(output)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", producer],
        cwd=staged,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, output.read_bytes() if output.exists() else b"", before


def test_publication_workflow_produces_bare_go_semver_for_setup_go(tmp_path: Path) -> None:
    import yaml

    workflow = yaml.safe_load(
        (guard.REPO_ROOT / ".github/workflows/model-hub-engine-release-guard.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["build-patched-source"]["steps"]
    resolve_index = next(index for index, step in enumerate(steps) if step.get("id") == "go_pin")
    resolve = steps[resolve_index]
    setup_index = next(
        index for index, step in enumerate(steps) if str(step.get("uses", "")).startswith("actions/setup-go@")
    )
    setup = steps[setup_index]

    assert resolve_index < setup_index
    assert "raw_go_version" in resolve["run"]
    assert "${raw_go_version#go}" in resolve["run"]
    assert setup["with"]["go-version"] == "${{ steps.go_pin.outputs.go_version }}"
    assert 'go-version: "1.26.9"' not in (guard.REPO_ROOT / ".github/workflows/model-hub-engine-release-guard.yml").read_text(
        encoding="utf-8"
    )

    for raw_version, expected in (("go1.26.9", "1.26.9"), ("go1.27.1", "1.27.1")):
        result, output, before = _run_workflow_go_pin_step(tmp_path / raw_version, {"go_version": raw_version})
        assert result.returncode == 0, result.stderr
        assert output == f"go_version={expected}\n".encode()
        assert before == (tmp_path / raw_version / "workflow/vibe/model_hub_runtime/cliproxyapi_manifest.json").read_bytes()


@pytest.mark.parametrize(
    "build",
    [None, {}, {"go_version": "1.27.1"}, {"go_version": "go1.27"}, {"go_version": "go1.27.1-beta1"}],
)
def test_publication_workflow_rejects_invalid_go_pin_without_consumer_output(
    tmp_path: Path,
    build: dict | None,
) -> None:
    result, output, before = _run_workflow_go_pin_step(tmp_path, build)
    assert result.returncode != 0
    assert output == b""
    assert before == (tmp_path / "workflow/vibe/model_hub_runtime/cliproxyapi_manifest.json").read_bytes()


def test_fetch_owned_release_materializes_exact_verified_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, owned, _built = _manifest(tmp_path)
    monkeypatch.setattr(guard, "_download", _fake_download(owned))

    spec = guard.fetch_release_assets(manifest_path, tmp_path / "backup")
    verified = guard.verify_release_assets(manifest_path, tmp_path / "backup")

    assert spec.asset_release_tag == "model-hub-engine-v7.2.149-1"
    assert verified.expected_asset_names == {
        path.name for path in (tmp_path / "backup").iterdir()
    }


def test_fetch_source_verifies_upstream_bytes_and_builds_publishable_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _owned, built = _manifest(tmp_path)
    def build_source(manifest: Path, output: Path) -> Path:
        output.mkdir()
        output_manifest = output / "model-hub-engine-manifest.json"
        output_manifest.write_bytes(manifest.read_bytes())
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        for raw in payload["assets"]:
            name = Path(raw["url"]).name
            archive = built[name]
            (output / name).write_bytes(archive)
            (output / f"{name}.sha256").write_text(
                f"{hashlib.sha256(archive).hexdigest()}  {name}\n",
                encoding="utf-8",
            )
        return output_manifest

    monkeypatch.setattr(build_model_hub_engine, "build_source_release", build_source)

    spec = guard.fetch_upstream_assets(manifest_path, tmp_path / "publish")

    assert spec.upstream_release_tag == "v7.2.149"
    guard.verify_release_assets(manifest_path, tmp_path / "publish")


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda path: path.write_bytes(path.read_bytes() + b"tampered"), "size mismatch"),
        (lambda path: path.write_text("wrong checksum\n", encoding="utf-8"), "sidecar mismatch"),
    ],
)
def test_verify_rejects_changed_release_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    match,
) -> None:
    manifest_path, owned, _built = _manifest(tmp_path)
    monkeypatch.setattr(guard, "_download", _fake_download(owned))
    backup = tmp_path / "backup"
    guard.fetch_release_assets(manifest_path, backup)
    target = next(backup.glob("*.tar.gz" if match == "size mismatch" else "*.sha256"))
    mutate(target)

    with pytest.raises(guard.ReleaseGuardError, match=match):
        guard.verify_release_assets(manifest_path, backup)


@pytest.mark.parametrize("entry_kind", ["unexpected", "directory"])
def test_verify_rejects_non_release_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    manifest_path, owned, _built = _manifest(tmp_path)
    monkeypatch.setattr(guard, "_download", _fake_download(owned))
    backup = tmp_path / "backup"
    guard.fetch_release_assets(manifest_path, backup)
    if entry_kind == "unexpected":
        (backup / "unexpected.txt").write_text("unexpected", encoding="utf-8")
        expected = "unexpected=.*unexpected.txt"
    else:
        (backup / "nested").mkdir()
        expected = "unsafe entries"

    with pytest.raises(guard.ReleaseGuardError, match=expected):
        guard.verify_release_assets(manifest_path, backup)


def test_failed_fetch_preserves_last_verified_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, owned, _built = _manifest(tmp_path)
    del owned[next(url for url in owned if url.endswith(".tar.gz"))]
    monkeypatch.setattr(guard, "_download", _fake_download(owned))
    backup = tmp_path / "backup"
    backup.mkdir()
    marker = backup / "last-good"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(guard.ReleaseGuardError, match="missing test asset"):
        guard.fetch_release_assets(manifest_path, backup)

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_manifest_rejects_non_owned_release_url(tmp_path: Path) -> None:
    manifest_path, _owned, _built = _manifest(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["assets"][0]["url"] = (
        "https://github.com/router-for-me/CLIProxyAPI/releases/download/"
        "v7.2.149/CLIProxyAPI_7.2.149_darwin_aarch64.tar.gz"
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(guard.ReleaseGuardError, match="outside the Avibe-owned release"):
        guard.load_release_spec(manifest_path)


@pytest.mark.parametrize(
    ("platform", "bin_path"),
    [("windows-amd64", "cli-proxy-api"), ("linux-amd64", "cli-proxy-api.exe"), ("darwin-arm64", "bin/cli-proxy-api")],
)
def test_manifest_pins_the_engine_binary_name_per_platform(
    tmp_path: Path, platform: str, bin_path: str,
) -> None:
    # Windows resolves an extensionless path by appending .exe, so a Windows
    # asset must name the .exe member and no other asset may.
    manifest_path, _owned, _built = _manifest(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    next(asset for asset in payload["assets"] if asset["platform"] == platform)["bin_path"] = bin_path
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(guard.ReleaseGuardError, match="bin_path is invalid"):
        guard.load_release_spec(manifest_path)


@pytest.mark.parametrize(
    "remote_state",
    ["valid", "missing", "extra", "corrupt", "bad_upload", "published", "download_once", "unavailable"],
)
def test_draft_publication_repairs_exact_asset_set_before_becoming_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_state: str,
) -> None:
    # Contract: either publish the exact pinned bytes, or preserve the draft.
    # The old workflow deleted only expected names, so an extra asset survived
    # every recovery. Source-text assertions never exercised this transition.
    manifest, owned, _built = _manifest(tmp_path)
    monkeypatch.setattr(guard, "_download", _fake_download(owned))
    verified = tmp_path / "verified"
    guard.fetch_release_assets(manifest, verified)
    expected = {path.name: path.read_bytes() for path in verified.iterdir()}
    remote = dict(expected)
    target = next(name for name in expected if name.endswith(".tar.gz"))
    if remote_state == "missing":
        del remote[target]
    elif remote_state == "extra":
        remote["unexpected.txt"] = b"interrupted draft"
    elif remote_state in {"corrupt", "bad_upload"}:
        remote[target] = b"corrupt upload"
    is_draft = remote_state != "published"
    calls = []

    def fake_gh(command, **kwargs):
        nonlocal is_draft
        assert command[:2] == ["gh", "release"]
        assert command[-2:] == ["--repo", "fixture/engine"]
        assert command[3] == "model-hub-engine-v7.2.149-1"
        calls.append(command[2])
        action = command[2]
        stdout = ""
        if action == "view":
            stdout = json.dumps({"isDraft": is_draft, "assets": [{"name": name} for name in remote]})
        elif action == "download":
            if remote_state == "unavailable" or (
                remote_state == "download_once" and calls.count("download") == 1
            ):
                raise subprocess.CalledProcessError(1, command)
            destination = Path(command[command.index("--dir") + 1])
            for name, data in remote.items():
                (destination / name).write_bytes(data)
        elif action == "delete-asset":
            del remote[command[4]]
        elif action == "upload":
            for filename in command[4:-2]:
                path = Path(filename)
                remote[path.name] = path.read_bytes()
            if remote_state == "bad_upload":
                remote[target] = b"corrupt upload"
        elif action == "edit":
            assert "--draft=false" in command
            is_draft = False
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    monkeypatch.setattr(guard.subprocess, "run", fake_gh)
    result = guard.main([
        "--manifest", str(manifest), "publish-draft",
        "--asset-dir", str(verified), "--repo", "fixture/engine",
    ])
    if remote_state in {"bad_upload", "published", "unavailable"}:
        assert result == 1
        assert is_draft is (remote_state != "published")
        assert "edit" not in calls
        if remote_state == "published":
            assert "delete-asset" not in calls and "upload" not in calls
    else:
        assert result == 0
        assert not is_draft
        assert remote == expected
        assert calls[-2:] == ["download", "edit"]
        if remote_state == "valid":
            assert "delete-asset" not in calls and "upload" not in calls


def test_workflow_has_scheduled_backup_and_non_clobbering_recovery() -> None:
    workflow = (
        guard.REPO_ROOT / ".github/workflows/model-hub-engine-release-guard.yml"
    ).read_text(encoding="utf-8")

    assert "schedule:" in workflow
    assert "continue-on-error: true" in workflow
    assert 'actions/artifacts"' in workflow
    assert "MANIFEST_SHA: ${{ steps.manifest.outputs.sha256 }}" in workflow
    assert "model-hub-engine-release-backup-${{ steps.manifest.outputs.sha256 }}" in workflow
    assert "retention-days: 90" in workflow
    assert "--latest=false" in workflow
    assert "missing_assets" in workflow
    assert "--clobber" not in workflow
    assert "--json isDraft" in workflow
    assert workflow.count("model_hub_engine_release_guard.py publish-draft") == 2
    assert "model_hub_engine_release_guard.py verify" in workflow
    assert "publish-patched-source:" in workflow
    assert "needs: build-patched-source" in workflow
    assert "needs: [publish-patched-source]" in workflow
    assert "needs.publish-patched-source.result == 'success'" in workflow
    assert '--target "$GITHUB_SHA"' in workflow
    assert "actions/download-artifact@" in workflow
    assert "python3 scripts/model_hub_engine_release_guard.py verify" in workflow
    assert 'gh release create "$release_tag"' in workflow
    assert 'python3 scripts/model_hub_engine_release_guard.py fetch' in workflow
