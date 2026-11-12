"""Release boundary: candidates cannot self-certify; public pins require public bytes.

The existing artifact transport tests cover ZIP/run/attempt provenance. These
fixtures cover the sidecar-specific archive, publication and non-latest boundary
which those generic helpers and the consumer installer cannot exercise.
"""
from __future__ import annotations

import io
import shutil
import json
import subprocess
import tarfile
import tomllib

import pytest

from scripts import direct_transport_release as release
from scripts import release_mirror as mirror
from scripts.release_package_version import latest_official_release, official_stable_version_key

SOURCE = "a" * 40
RUN = "98765"
TAG = "direct-transport-v0.1.0-1"
BEFORE = {"id": 123, "tag_name": "v3.2.6"}


def archive_files(target):
    binary = "avibe-direct-transport" + (".exe" if "windows" in target else "")
    cargo = tomllib.loads((release.ROOT / "direct-transport/Cargo.toml").read_text())
    lock = (release.ROOT / "direct-transport/Cargo.lock").read_bytes()
    packages = [{"name": "fixture", "version": "1.0.0", "files": ["LICENSES/fixture/LICENSE"]}]
    provenance = {"packages": packages, "targets": [target], "root_package": "avibe-direct-transport",
                  "lockfile_sha256": release.digest(lock),
                  "policy_sha256": release.digest(release.policy_bytes())}
    files = {binary: b"inert test bytes, never executed", "Cargo.lock": lock, "feature-graph.txt": b"fixture",
             "feature-evidence.json": b"{}", "LICENSES/NOTICE.md": b"fixture notice",
             "LICENSES/fixture/LICENSE": b"fixture license", "LICENSES/packages.json": json.dumps(packages).encode(),
             "LICENSES/provenance.json": json.dumps(provenance).encode(), "LICENSES/policy.json": release.policy_bytes()}
    for name in ("COPYRIGHT.html", "licenses/Apache-2.0.txt", "licenses/MIT.txt"):
        files[f"LICENSES/rust-1.91.0/{name}"] = b"fixture rust notice"
    manifest = {"name": "avibe-direct-transport", "version": "0.1.0", "target": target,
                "source_sha": SOURCE, "pull_request_head_sha": SOURCE, "workflow_run_id": RUN, "workflow_run_attempt": 1,
                "direct_dependencies": cargo["dependencies"], "binary": binary,
                "binary_bytes": len(files[binary]), "binary_sha256": release.digest(files[binary]),
                "cargo_lock_sha256": release.digest(lock), "patched_dependencies": False,
                "distribution": "qualified-workflow-artifact", "transport_liveness": {"idle_timeout_ms": 6000, "keep_alive_ms": 1500},
                "licenses_provenance_sha256": release.digest(files["LICENSES/provenance.json"]), "features": {},
                "qualification": {
                    "native_contract_tests": "passed", "resolved_feature_graph": "passed",
                    "linux_static": "passed" if "linux" in target else "not-applicable",
                    "linux_idle_and_outage_tolerance": "passed" if "linux" in target else "not-applicable",
                    "linux_traffic_and_address_changes": "passed" if "linux" in target else "not-applicable",
                    "private_apple_datapath": "passed-with-positive-control" if "apple" in target else "not-applicable"}}
    files["manifest.json"] = json.dumps(manifest).encode()
    return files


def pack(path, files, extra=None):
    with tarfile.open(path, "w:gz") as bundle:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
        if extra:
            bundle.addfile(extra)


@pytest.mark.parametrize("target", release.TARGETS.values())
def test_complete_archive_preserves_root_binary_and_notices(tmp_path, target):
    path = tmp_path / "artifact.tar.gz"
    pack(path, archive_files(target))
    result = release.verify_archive(path, target, SOURCE, RUN, 1)
    assert result["patched_dependencies"] is False
    assert result["binary"].startswith("avibe-direct-transport")


@pytest.mark.parametrize("field,value,error", [
    ("source_sha", "b" * 40, "source/run/target"),
    ("pull_request_head_sha", "b" * 40, "source/run/target"),
    ("workflow_run_attempt", 2, "source/run/target"),
    ("workflow_run_id", "123", "source/run/target"),
    ("patched_dependencies", True, "stock qualified"),
    ("distribution", "workflow-artifact-only", "stock qualified"),
    ("binary_sha256", "0" * 64, "binary identity"),
    ("licenses_provenance_sha256", "0" * 64, "notice provenance"),
    ("qualification", {}, "required qualification"),
])
def test_archive_rejects_unqualified_or_misattributed_bytes(tmp_path, field, value, error):
    target = "aarch64-apple-darwin"
    files = archive_files(target)
    manifest = json.loads(files["manifest.json"])
    manifest[field] = value
    files["manifest.json"] = json.dumps(manifest).encode()
    path = tmp_path / "artifact.tar.gz"
    pack(path, files)
    with pytest.raises(ValueError, match=error):
        release.verify_archive(path, target, SOURCE, RUN, 1)


@pytest.mark.parametrize("name", ["LICENSES/fixture/LICENSE", "LICENSES/NOTICE.md", "LICENSES/rust-1.91.0/COPYRIGHT.html"])
def test_missing_redistribution_text_fails_before_distribution(tmp_path, name):
    files = archive_files("aarch64-apple-darwin")
    del files[name]
    path = tmp_path / "artifact.tar.gz"
    pack(path, files)
    with pytest.raises(ValueError, match="notice"):
        release.verify_archive(path, "aarch64-apple-darwin", SOURCE, RUN, 1)


@pytest.mark.parametrize("kind", ["traversal", "duplicate", "link", "device"])
def test_archive_members_cannot_escape_or_override(tmp_path, kind):
    files = archive_files("aarch64-apple-darwin")
    member = tarfile.TarInfo({"traversal": "../outside", "duplicate": "manifest.json",
                            "link": "LICENSES/link", "device": "LICENSES/device"}[kind])
    if kind == "link":
        member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
    if kind == "device":
        member.type = tarfile.CHRTYPE
    path = tmp_path / "artifact.tar.gz"
    pack(path, files, member)
    with pytest.raises(ValueError, match="unsafe|duplicate|links and devices"):
        release.verify_archive(path, "aarch64-apple-darwin", SOURCE, RUN, 1)


@pytest.mark.parametrize("tag", ["v3.2.6", "iroh-apple-v1.2.0-1", "direct-transport-v0.1.0-0", "direct-transport-v0.1.0-01", "direct-transport-v0.2.0-1", "../bad"])
def test_publication_tag_cannot_be_an_application_or_apple_release(tag):
    with pytest.raises(ValueError, match="tag"):
        release.identity(tag, SOURCE)


@pytest.mark.parametrize("overrides", [{"event": "pull_request"}, {"head_branch": "feature"}, {"head_repository": {"full_name": "fork/avibe"}}])
def test_only_exact_master_qualification_can_promote(monkeypatch, overrides):
    run = {"event": "push", "head_branch": "master", "head_repository": {"full_name": release.REPO}} | overrides
    monkeypatch.setattr(release.artifacts, "check_run", lambda *args, **kwargs: run)
    with pytest.raises(ValueError, match="master run"):
        release.qualified_run(SOURCE, RUN, 1)


@pytest.fixture
def packet(tmp_path, monkeypatch):
    directory = tmp_path / "assets"
    directory.mkdir()
    manifest = {"schema_version": 1, "release_state": "pending", "direct_transport_version": "0.1.0",
                "source": release.REPO, "release_tag": TAG,
                "source_url": f"https://github.com/{release.REPO}/releases/tag/{TAG}", "archives": {}}
    for platform, target in release.TARGETS.items():
        name = f"avibe-direct-transport-{target}.tar.gz"
        pack(directory / name, archive_files(target))
        sha = release.file_hash(directory / name)
        (directory / (name + ".sha256")).write_text(f"{sha}  {name}\n")
        manifest["archives"][platform] = {"name": name, "sha256": sha, "size": (directory / name).stat().st_size,
                                         "url": f"https://github.com/{release.REPO}/releases/download/{TAG}/{name}",
                                         "bin_path": "avibe-direct-transport" + (".exe" if "windows" in target else ""),
                                         "binary_sha256": release.digest(b"inert test bytes, never executed"), "format": "tar.gz"}
    release.write_json(directory / release.RECORD, {"qualification_run_id": RUN, "qualification_attempt": 1,
                                                   "source_sha": SOURCE, "candidate_manifest": manifest})
    state = {"id": 555, "tag_name": TAG, "draft": False, "prerelease": False,
             "body": release.NOTES + f"\nSource: `{SOURCE}`\n", "assets": []}
    for path in directory.iterdir():
        state["assets"].append({"id": len(state["assets"]) + 1, "name": path.name, "size": path.stat().st_size,
                                "digest": "sha256:" + release.file_hash(path), "state": "uploaded",
                                "browser_download_url": f"https://github.com/{release.REPO}/releases/download/{TAG}/{path.name}"})
    monkeypatch.setenv("GH_TOKEN", "fixture-not-a-credential")
    monkeypatch.setattr(release, "release", lambda tag: state)
    monkeypatch.setattr(release, "latest", lambda: BEFORE)
    monkeypatch.setattr(release.backstop, "resolve_tag_sha", lambda *args: SOURCE)
    monkeypatch.setattr(release, "api", lambda *args, **kwargs: state["assets"])

    def download(url, path, size):
        path.write_bytes((directory / path.name).read_bytes())
    monkeypatch.setattr(release, "public_download", download)
    return directory, state


def test_public_readback_emits_five_platform_proposal_but_never_edits_input(packet):
    directory, _ = packet
    original = (directory / release.RECORD).read_bytes()
    result = release.verify_public(TAG, SOURCE, directory, BEFORE)
    assert result["manifest_proposal"]["release_state"] == "published"
    assert set(result["manifest_proposal"]["archives"]) == {"darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64", "win32-x64"}
    assert len(result["assets"]) == 11
    assert result["latest_before"] == result["latest_after"] == BEFORE
    assert (directory / release.RECORD).read_bytes() == original


@pytest.mark.parametrize("defect", ["draft", "prerelease", "notes", "tag", "latest", "missing", "size", "hash", "url", "public-bytes", "public-unavailable"])
def test_no_published_pin_without_all_public_proofs(packet, monkeypatch, defect):
    directory, state = packet
    if defect in {"draft", "prerelease"}:
        state[defect] = True
    elif defect == "notes":
        state["body"] = "announcement"
    elif defect == "tag":
        monkeypatch.setattr(release.backstop, "resolve_tag_sha", lambda *args: "b" * 40)
    elif defect == "latest":
        monkeypatch.setattr(release, "latest", lambda: {"id": 555, "tag_name": TAG})
    elif defect == "missing":
        state["assets"].pop()
    elif defect in {"size", "hash", "url"}:
        state["assets"][0][{"size": "size", "hash": "digest", "url": "browser_download_url"}[defect]] = 1 if defect == "size" else "wrong"
    elif defect == "public-bytes":
        monkeypatch.setattr(release, "public_download", lambda url, path, size: path.write_bytes(b"wrong"))
    else:
        def unavailable(*args):
            raise OSError("public download unavailable")
        monkeypatch.setattr(release, "public_download", unavailable)
    with pytest.raises((ValueError, OSError)):
        release.verify_public(TAG, SOURCE, directory, BEFORE)


def test_publish_requires_exact_confirmation_and_workflow_before_any_mutation(packet, monkeypatch):
    directory, _ = packet
    monkeypatch.setattr(release, "api", lambda *args, **kwargs: pytest.fail("unexpected API mutation"))
    with pytest.raises(ValueError, match="confirmation"):
        release.publish(TAG, SOURCE, directory, "yes")
    with pytest.raises(ValueError, match="master workflow"):
        release.publish(TAG, SOURCE, directory, f"publish {TAG} {SOURCE} {RUN}/1")


def test_publish_uses_explicit_non_latest_flags_and_never_clobbers(packet, monkeypatch):
    directory, state = packet
    for key, value in {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": release.REPO,
                       "GITHUB_REF": "refs/heads/master", "GITHUB_SHA": SOURCE}.items():
        monkeypatch.setenv(key, value)

    def reprove(tag, source, run_id, attempt, dest):
        import shutil
        shutil.copytree(directory, dest)
        release.write_json(dest.parent / "backstop-proof.json", {"source_sha": SOURCE, "run_id": 321, "run_attempt": 2})
    monkeypatch.setattr(release, "prepare", reprove)
    monkeypatch.setattr(release, "release", lambda tag: None)
    calls = []

    def api(path, payload=None, **kwargs):
        calls.append((path, payload))
        if path.startswith("git/matching"):
            return []
        if path == "releases":
            return {**state, "draft": True, "assets": []}
        return {}

    def run(command, **kwargs):
        calls.append((command, kwargs.get("input")))
        assert "--clobber" not in command
        return subprocess.CompletedProcess(command, 0, "{}", "")
    monkeypatch.setattr(release, "api", api)
    monkeypatch.setattr(release.subprocess, "run", run)
    monkeypatch.setattr(release, "qualified_run", lambda *args: {})
    monkeypatch.setattr(release, "verify_public", lambda *args: {"verified": True})
    assert release.publish(TAG, SOURCE, directory, f"publish {TAG} {SOURCE} {RUN}/1") == {
        "verified": True, "backstop": {"source_sha": SOURCE, "run_id": 321, "run_attempt": 2}}
    create = next(payload for path, payload in calls if path == "releases")
    assert create["draft"] is True and create["prerelease"] is False and create["make_latest"] == "false"
    final = next(json.loads(payload) for command, payload in calls if isinstance(command, list) and "PATCH" in command)
    assert final == {"draft": False, "prerelease": False, "make_latest": "false"}


def test_auxiliary_release_cannot_change_official_latest_mirror_or_notification():
    from core import update_checker
    rows = [{"tag_name": "v3.2.6", "draft": False, "prerelease": False},
            {"tag_name": TAG, "draft": False, "prerelease": False}]
    assert official_stable_version_key(TAG) is None
    assert latest_official_release(rows) == rows[0]
    auxiliary = mirror.Release(repository=release.REPO, tag=TAG, prerelease=False,
                               published_at="2099-01-01T00:00:00Z", commit=SOURCE, assets=(), silent=True)
    assert mirror.latest_installers([auxiliary]) == {}
    assert mirror.silent_release_notes(release.NOTES)
    assert update_checker._parse_update_notification_policy(release.NOTES) == update_checker.UPDATE_NOTIFICATION_POLICY_NONE


def test_public_url_readback_never_attaches_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leave-this-process")
    requests = []
    def open_public(request, **kwargs):
        requests.append(request)
        return io.BytesIO(b"public")
    monkeypatch.setattr(release.urllib.request, "urlopen", open_public)
    path = tmp_path / "bytes"
    url = f"https://github.com/{release.REPO}/releases/download/{TAG}/archive.tar.gz"
    release.public_download(url, path, 6)
    assert requests[0].full_url == url
    assert not any(key.lower() == "authorization" for key, _ in requests[0].header_items())
    with pytest.raises(ValueError, match="size|exceeds"):
        release.public_download(url, path, 5)


def test_actual_locked_five_target_notice_inventory_has_complete_policy():
    # Metadata was read from 278 checksum-verified registry .crates selected by
    # the real five-target qualification graphs, not invented license fixtures.
    inventory = json.loads((release.ROOT / "tests/fixtures/direct_transport/notice-inventory.json").read_text())
    lock_bytes = (release.ROOT / "direct-transport/Cargo.lock").read_bytes()
    assert release.digest(lock_bytes) == inventory["lockfile_sha256"], "refresh the real graph audit after a lock change"
    lock = {(p["name"], p["version"]): p for p in tomllib.loads(lock_bytes.decode())["package"]}
    policy = json.loads(release.policy_bytes())
    assert len(inventory["packages"]) == 278
    for package in inventory["packages"]:
        key = f"{package['name']}-{package['version']}"
        assert package["crate_sha256"] == lock[(package["name"], package["version"])]["checksum"]
        assert package["license"] in policy["expressions"], key
        if not package["bundled_notices"] and not package["license_file"]:
            assert policy["packages"][key]["crate_sha256"] == package["crate_sha256"], key
            assert policy["packages"][key]["license"] == package["license"], key
            assert policy["packages"][key]["files"], key
    assert policy["composition"]["base_sha256"] == release.file_hash(release.ROOT / "packaging/iroh-apple/license-sources.json")


def test_real_prepare_packet_is_stable_across_successful_backstop_rerun(packet, tmp_path, monkeypatch):
    directory, _ = packet
    proof = {"run_id": 1234, "run_attempt": 1}
    monkeypatch.setattr(release.subprocess, "check_output", lambda *args, **kwargs: SOURCE + "\n")
    monkeypatch.setattr(release.backstop, "_require_on_master", lambda *args: None)
    monkeypatch.setattr(release.backstop, "prove_backstop_sha", lambda *args, **kwargs:
                        release.backstop.BackstopProof(source_sha=SOURCE, event="push", **proof))
    monkeypatch.setattr(release, "qualified_run", lambda *args: {"run_started_at": "2026-11-13T00:00:00Z"})
    monkeypatch.setattr(release.artifacts, "successful_job", lambda *args, **kwargs: {"id": 42})
    monkeypatch.setattr(release.artifacts, "artifact_for_job", lambda repo, run, sha, name, job:
                        {"id": 99, "digest": "sha256:" + "f" * 64, "name": name})

    def download(repo, artifact, output):
        output.mkdir()
        name = artifact["name"]
        if name.startswith("avibe-direct-transport-"):
            target = name.removeprefix("avibe-direct-transport-").removesuffix("-" + SOURCE)
            filename = f"avibe-direct-transport-{target}.tar.gz"
            for suffix in ("", ".sha256"):
                shutil.copyfile(directory / (filename + suffix), output / (filename + suffix))
            (output / "manifest.json").write_bytes(archive_files(target)["manifest.json"])
        else:
            report = {"failures": []} if name.startswith("network-") else {
                "all_passed": True, "mode": "qualification", "results": [{"passed": True}] * 11}
            release.write_json(output / "report.json", report)
    monkeypatch.setattr(release.artifacts, "download_verified", download)
    first, second = tmp_path / "first/assets", tmp_path / "second/assets"
    release.prepare(TAG, SOURCE, RUN, 1, first)
    proof.update(run_id=1235, run_attempt=2)
    release.prepare(TAG, SOURCE, RUN, 1, second)
    assert {p.name: p.read_bytes() for p in first.iterdir()} == {p.name: p.read_bytes() for p in second.iterdir()}
    assert json.loads((first.parent / "backstop-proof.json").read_text())["run_attempt"] == 1
    assert json.loads((second.parent / "backstop-proof.json").read_text())["run_attempt"] == 2
    assert "backstop" not in json.loads((second / release.RECORD).read_text())


def retry_context(directory, monkeypatch):
    for key, value in {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": release.REPO,
                       "GITHUB_REF": "refs/heads/master", "GITHUB_SHA": SOURCE}.items():
        monkeypatch.setenv(key, value)
    def reprove(tag, source, run_id, attempt, dest):
        shutil.copytree(directory, dest)
        release.write_json(dest.parent / "backstop-proof.json", {"source_sha": SOURCE, "run_id": 999, "run_attempt": 2})
    monkeypatch.setattr(release, "prepare", reprove)
    monkeypatch.setattr(release, "qualified_run", lambda *args: {})


@pytest.mark.parametrize("missing_tag", [True, False])
def test_public_retry_never_mutates_even_when_public_tag_is_missing(packet, monkeypatch, missing_tag):
    directory, state = packet
    retry_context(directory, monkeypatch)
    def read_only(path, payload=None, **kwargs):
        assert payload is None, "public retry must not POST any state"
        assert path.startswith("releases/555/assets?"), "must not enter tag-creation path"
        return state["assets"]
    monkeypatch.setattr(release, "api", read_only)
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: pytest.fail("public retry must not mutate"))
    if missing_tag:
        def missing(*args):
            raise ValueError("public tag missing")
        monkeypatch.setattr(release.backstop, "resolve_tag_sha", missing)
        with pytest.raises(ValueError, match="public tag missing"):
            release.publish(TAG, SOURCE, directory, f"publish {TAG} {SOURCE} {RUN}/1")
    else:
        receipt = release.publish(TAG, SOURCE, directory, f"publish {TAG} {SOURCE} {RUN}/1")
        assert receipt["manifest_proposal"]["release_state"] == "published"
        assert receipt["backstop"]["run_attempt"] == 2


def test_interrupted_draft_retry_after_backstop_rerun_does_not_replace_assets(packet, monkeypatch):
    directory, state = packet
    state["draft"] = True
    retry_context(directory, monkeypatch)
    mutations = []
    def api(path, payload=None, **kwargs):
        assert payload is None, "existing tag/draft must not be recreated"
        if path.startswith("git/matching-refs/"):
            return [{"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": SOURCE,
                     "url": f"https://api.github.com/repos/{release.REPO}/git/commits/{SOURCE}"}}]
        assert path.startswith("releases/555/assets?")
        return state["assets"]
    def finalize(command, **kwargs):
        assert command[:2] == ["gh", "api"] and "PATCH" in command, "no upload/clobber on matching retry"
        mutations.append(json.loads(kwargs["input"]))
        state["draft"] = False
        return subprocess.CompletedProcess(command, 0, "{}", "")
    monkeypatch.setattr(release, "api", api)
    monkeypatch.setattr(release.subprocess, "run", finalize)
    receipt = release.publish(TAG, SOURCE, directory, f"publish {TAG} {SOURCE} {RUN}/1")
    assert receipt["manifest_proposal"]["release_state"] == "published"
    assert receipt["backstop"]["run_attempt"] == 2
    assert mutations == [{"draft": False, "prerelease": False, "make_latest": "false"}]
