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
import zipfile
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
    for name in ("model_hub_engine_release_guard.py", "release_asset_download.py"):
        shutil.copy2(guard.REPO_ROOT / "scripts" / name, staged / "scripts" / name)
    # The guard's manifest schema lives in the packaged tree; the step runs on a bare checkout.
    (staged / "core").mkdir()
    for name in ("__init__.py", "model_hub_engine_manifest.py"):
        shutil.copy2(guard.REPO_ROOT / "core" / name, staged / "core" / name)
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


class _FakeGitHub:
    """One engine release, its stored backups, and the GitHub CLI calls made against them."""

    REPOSITORY = "fixture/engine"

    def __init__(self, expected: dict[str, bytes]) -> None:
        self.release: dict[str, bytes] | None = dict(expected)
        self.is_draft = False
        self.backups: list[dict] = []
        self.calls: list[list[str]] = []

    def add_backup(self, identifier: int, files: dict[str, bytes], *, expired: bool = False) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            for name, data in files.items():
                bundle.writestr(name, data)
        self.backups.append(
            {"id": identifier, "created_at": f"2026-11-{identifier:02d}T00:00:00Z", "expired": expired,
             "zip": output.getvalue()}
        )

    def download(self, url: str, destination: Path, **_kwargs) -> None:
        name = url.rsplit("/", 1)[-1]
        if self.release is None or self.is_draft or name not in self.release:
            raise guard.ReleaseGuardError(f"release asset download failed (404): {url}")
        destination.write_bytes(self.release[name])

    def run(self, command, **kwargs):
        self.calls.append(list(command))
        if command[:2] == ["gh", "api"]:
            return self._api(command, kwargs)
        assert command[:2] == ["gh", "release"] and command[-2:] == ["--repo", self.REPOSITORY], command
        action, arguments = command[2], command[4:-2]
        stdout = ""
        if action == "view":
            if self.release is None:
                raise subprocess.CalledProcessError(1, command, stderr="release not found")
            stdout = json.dumps({"isDraft": self.is_draft, "assets": [{"name": name} for name in self.release]})
        elif action == "create":
            assert self.release is None and "--latest=false" in arguments
            self.release = {Path(item).name: Path(item).read_bytes() for item in arguments if Path(item).is_file()}
        elif action == "upload":
            for item in arguments:
                assert Path(item).name not in self.release, "an existing asset must never be replaced"
                self.release[Path(item).name] = Path(item).read_bytes()
        elif action == "download":
            destination = Path(arguments[arguments.index("--dir") + 1])
            for name, data in self.release.items():
                (destination / name).write_bytes(data)
        elif action == "edit":
            self.is_draft = False
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    def _api(self, command, kwargs):
        target = next(item for item in command[2:] if item.startswith("repos/"))
        if target.endswith("/actions/artifacts"):
            (name,) = [item.removeprefix("name=") for item in command if item.startswith("name=")]
            listing = {
                "artifacts": [
                    {"id": backup["id"], "name": name, "created_at": backup["created_at"], "expired": backup["expired"]}
                    for backup in self.backups
                ]
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(listing))
        identifier = int(target.split("/")[-2])
        (backup,) = [item for item in self.backups if item["id"] == identifier]
        kwargs["stdout"].write(backup["zip"])
        return subprocess.CompletedProcess(command, 0)

    def actions(self) -> list[str]:
        return [command[2] for command in self.calls if command[:2] == ["gh", "release"]]


@pytest.fixture
def engine_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest, owned, _built = _manifest(tmp_path)
    monkeypatch.setattr(guard, "_download", _fake_download(owned))
    verified = tmp_path / "verified"
    guard.fetch_release_assets(manifest, verified)
    expected = {path.name: path.read_bytes() for path in verified.iterdir()}
    github = _FakeGitHub(expected)
    monkeypatch.setattr(guard, "_download", github.download)
    monkeypatch.setattr(guard.subprocess, "run", github.run)
    return manifest, expected, github


def _guard(tmp_path: Path, manifest: Path):
    return guard.guard_release(
        manifest, tmp_path / "out", repository=_FakeGitHub.REPOSITORY, target="a" * 40, work_dir=tmp_path / "work",
    )


def _archive_name(expected: dict[str, bytes]) -> str:
    return sorted(name for name in expected if name.endswith(".tar.gz"))[0]


def test_a_release_that_verifies_is_left_alone(tmp_path: Path, engine_release) -> None:
    manifest, expected, github = engine_release

    spec, recovered = _guard(tmp_path, manifest)

    assert recovered is False and spec.asset_release_tag == "model-hub-engine-v7.2.149-1"
    assert github.calls == []
    assert {path.name: path.read_bytes() for path in (tmp_path / "out").iterdir()} == expected


@pytest.mark.parametrize("lost", ["archive", "sidecar", "manifest", "several"])
def test_assets_a_published_release_lost_are_restored_from_a_verified_backup_without_touching_the_rest(
    tmp_path: Path, engine_release, lost: str,
) -> None:
    """MH-ENGINE-CHANNEL-011: lost assets come back from a backup that still verifies; the rest is untouched."""

    manifest, expected, github = engine_release
    archive = _archive_name(expected)
    names = {
        "archive": [archive],
        "sidecar": [f"{archive}.sha256"],
        "manifest": ["model-hub-engine-manifest.json"],
        "several": [archive, f"{archive}.sha256", "model-hub-engine-manifest.json"],
    }[lost]
    for name in names:
        del github.release[name]
    github.add_backup(1, expected)

    _spec, recovered = _guard(tmp_path, manifest)

    assert recovered is True and github.release == expected
    (upload,) = [command for command in github.calls if command[2] == "upload"]
    assert sorted(Path(item).name for item in upload[4:-2]) == sorted(names)
    assert set(github.actions()) == {"view", "upload"}


def test_published_bytes_that_changed_under_their_names_are_never_overwritten(tmp_path: Path, engine_release) -> None:
    """MH-ENGINE-CHANNEL-011: recovery never replaces an asset that exists under its name."""

    manifest, expected, github = engine_release
    archive = _archive_name(expected)
    github.release[archive] = b"replaced upstream"
    github.add_backup(1, expected)

    with pytest.raises(guard.ReleaseGuardError, match="refusing to overwrite pinned bytes"):
        _guard(tmp_path, manifest)

    assert github.release[archive] == b"replaced upstream"
    assert github.actions() == ["view"]


def test_a_release_that_is_gone_is_created_again_from_the_backup_as_a_non_latest_release(
    tmp_path: Path, engine_release,
) -> None:
    manifest, expected, github = engine_release
    github.release = None
    github.add_backup(1, expected)

    _spec, recovered = _guard(tmp_path, manifest)

    assert recovered is True and github.release == expected and github.is_draft is False
    (create,) = [command for command in github.calls if command[2] == "create"]
    assert create[create.index("--target") + 1] == "a" * 40 and "--latest=false" in create


def test_an_interrupted_draft_is_completed_verified_and_published(tmp_path: Path, engine_release) -> None:
    manifest, expected, github = engine_release
    github.is_draft = True
    del github.release[_archive_name(expected)]
    github.add_backup(1, expected)

    _spec, recovered = _guard(tmp_path, manifest)

    assert recovered is True and github.release == expected and github.is_draft is False
    assert github.actions()[-2:] == ["download", "edit"]


def test_only_a_backup_whose_bytes_still_verify_is_used_newest_first(tmp_path: Path, engine_release) -> None:
    manifest, expected, github = engine_release
    archive = _archive_name(expected)
    del github.release[archive]
    github.add_backup(9, {**expected, archive: b"corrupt backup"})
    github.add_backup(8, {**expected, "unexpected.txt": b"not a release asset"})
    github.add_backup(7, {name: data for name, data in expected.items() if name != archive})
    github.add_backup(6, expected, expired=True)
    github.add_backup(5, expected)
    github.add_backup(4, {**expected, archive: b"older and wrong"})

    _spec, recovered = _guard(tmp_path, manifest)

    assert recovered is True and github.release == expected
    downloaded = [command[2].split("/")[-2] for command in github.calls if command[2].endswith("/zip")]
    assert downloaded == ["9", "8", "7", "5"]


def test_without_a_verified_backup_nothing_is_uploaded(tmp_path: Path, engine_release) -> None:
    manifest, expected, github = engine_release
    archive = _archive_name(expected)
    del github.release[archive]
    github.add_backup(1, {**expected, archive: b"corrupt backup"})

    with pytest.raises(guard.ReleaseGuardError, match="no verified backup"):
        _guard(tmp_path, manifest)

    assert archive not in github.release and github.actions() == []


def test_the_guard_covers_the_packaged_engine_and_every_release_the_channel_lists(tmp_path: Path) -> None:
    """MH-ENGINE-CHANNEL-011: the guard's list is the packaged engine plus every channel-listed release."""

    (tmp_path / "packaged").mkdir()
    packaged, _owned, _built = _manifest(tmp_path / "packaged")
    listed = tmp_path / "listed"
    listed.mkdir()
    # The packaged release listed again with the same bytes, and two others.
    (listed / "model-hub-engine-v7.2.149-1.json").write_bytes(packaged.read_bytes())
    for build in (2, 3):
        other = json.loads(packaged.read_text(encoding="utf-8"))
        tag = f"model-hub-engine-v7.2.149-{build}"
        other["asset_release_tag"] = tag
        for asset in other["assets"]:
            asset["url"] = asset["url"].replace("model-hub-engine-v7.2.149-1", tag)
        (listed / f"{tag}.json").write_text(json.dumps(other), encoding="utf-8")

    releases = guard.plan_guarded_releases(packaged, listed, tmp_path / "plan")

    assert releases == [f"model-hub-engine-v7.2.149-{build}" for build in (1, 2, 3)]
    assert (tmp_path / "plan" / "model-hub-engine-v7.2.149-1.json").read_bytes() == packaged.read_bytes()
    assert guard.plan_guarded_releases(packaged, None, tmp_path / "alone") == ["model-hub-engine-v7.2.149-1"]
    # One release has one manifest.
    changed = json.loads(packaged.read_text(encoding="utf-8"))
    changed["assets"][0]["sha256"] = "0" * 64
    (listed / "model-hub-engine-v7.2.149-1.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(guard.ReleaseGuardError, match="two different manifests"):
        guard.plan_guarded_releases(packaged, listed, tmp_path / "conflict")


def test_the_guard_workflow_plans_every_guarded_release_and_recovers_each_through_the_script() -> None:
    import yaml

    text = (guard.REPO_ROOT / ".github/workflows/model-hub-engine-release-guard.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    plan, job = workflow["jobs"]["plan"], workflow["jobs"]["guard"]
    plan_run = "\n".join(step.get("run", "") for step in plan["steps"])
    guard_run = "\n".join(step.get("run", "") for step in job["steps"])

    assert "schedule" in workflow[True] and len(workflow[True]["schedule"]) == 2
    # The publication path is unchanged, and the guard still follows it on a dispatch.
    assert plan["needs"] == ["publish-patched-source"]
    assert "needs.publish-patched-source.result == 'success'" in plan["if"]
    assert text.count("model_hub_engine_release_guard.py publish-draft") == 1
    assert '--target "$GITHUB_SHA"' in text and "--latest=false" in text and "--clobber" not in text
    # The plan reads the channel through the shipped keys and keeps the packaged engine in the list.
    assert plan["permissions"] == {"contents": "read"}
    assert "model_hub_engine_channel.py commissioned" in plan_run
    assert "model_hub_engine_channel.py listed" in plan_run
    assert "model_hub_engine_release_guard.py plan" in plan_run
    # Each release is verified and recovered by the tested script, then backed up under its manifest digest.
    assert job["needs"] == ["plan"] and job["if"] == "always() && needs.plan.result == 'success'"
    assert job["strategy"] == {"fail-fast": False, "matrix": {"release": "${{ fromJSON(needs.plan.outputs.releases) }}"}}
    assert "guard \\\n" in guard_run and '--manifest "$RUNNER_TEMP/guarded-manifests/$RELEASE.json"' in guard_run
    backup = job["steps"][-1]
    assert backup["with"]["name"] == guard.BACKUP_ARTIFACT_PREFIX + "${{ steps.guard.outputs.sha256 }}"
    assert backup["with"]["retention-days"] == 90
    assert "github.event.schedule == '53 4 * * 0'" in backup["if"] and "steps.guard.outputs.recovered == 'true'" in backup["if"]
    # It restores pinned bytes only: no signing key, no bucket credential, no protected environment.
    for forbidden in ("secrets.", "environment:", "model_hub_engine_channel.py sign", "model_hub_engine_channel.py publish", "s3api"):
        assert forbidden not in text
