from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from vibe import release_dependencies as release
from vibe.upgrade import UpgradePlan, execute_upgrade_plan

ROOT = Path(__file__).resolve().parents[1]
PINS = "example==1.2.3 \\\n    --hash=sha256:" + "a" * 64 + "\n"


def wheel(tmp_path, version="3.2.6", constraints=PINS):
    target = tmp_path / f"avibe_os-{version}-py3-none-any.whl"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(f"avibe_os-{version}.dist-info/METADATA", f"Name: avibe-os\nVersion: {version}\n")
        archive.writestr("vibe/__init__.py", "raise RuntimeError('must never import downloaded code')")
        if constraints is not None:
            archive.writestr(release.CONSTRAINT_MEMBER, constraints)
    return target


def test_reads_target_wheel_without_importing_it(tmp_path):
    target = wheel(tmp_path)
    resolved, constraints = release.prepare_release(str(target), tmp_path / "staging")
    assert resolved == str(target)
    assert constraints.read_text() == PINS


@pytest.mark.parametrize("version,allowed", [("3.2.5", True), ("3.1.0", True), ("3.2.5rc1", True), ("3.2.5.dev1", True), ("3.2.4.post1", True), ("3.2.4rc1.post1", True), ("3.2.5.post0", False), ("3.2.5+local", False), ("3.2.6.dev1", False), ("3.2.6", False), ("3.3.0rc1", False)])
def test_only_released_legacy_artifacts_may_lack_lock(tmp_path, version, allowed):
    target = wheel(tmp_path, version=version, constraints=None)
    if allowed:
        assert release.prepare_release(str(target), tmp_path / "out") == (str(target), None)
    else:
        with pytest.raises(ValueError, match="missing dependency constraints"):
            release.prepare_release(str(target), tmp_path / "out")


@pytest.mark.parametrize("pins", ["", "example>=1\n", "-r other.txt\n", "example @ https://evil.invalid/pkg.whl\n", "example==1 --index-url https://evil.invalid\n"])
def test_rejects_unlocked_or_option_injecting_constraints(tmp_path, pins):
    target = wheel(tmp_path, constraints=pins)
    with pytest.raises(ValueError):
        release.prepare_release(str(target), tmp_path / "out")


def test_pypi_resolution_pins_wheel_and_verifies_hash(monkeypatch, tmp_path):
    data = wheel(tmp_path).read_bytes()
    url = "https://files.pythonhosted.org/avibe_os-3.2.6-py3-none-any.whl"
    metadata = {"urls": [{"filename": "avibe_os-3.2.6-py3-none-any.whl", "url": url, "digests": {"sha256": hashlib.sha256(data).hexdigest()}}]}
    calls = []

    def get(request_url, limit):
        calls.append(request_url)
        return data if request_url == url else json.dumps(metadata).encode()

    monkeypatch.setattr(release, "_get", get)
    target, constraints = release.prepare_release("avibe-os==3.2.6", tmp_path / "out")
    assert Path(target).read_bytes() == data
    assert constraints.read_text() == PINS
    assert calls == ["https://pypi.org/pypi/avibe-os/3.2.6/json", url]


@pytest.mark.parametrize("pep508", [False, True])
def test_explicit_release_url_hash_is_enforced(monkeypatch, tmp_path, pep508):
    data = wheel(tmp_path).read_bytes()
    monkeypatch.setattr(release, "_get", lambda *_: data)
    spec = "https://dl.avibe.bot/releases/v3.2.6/avibe_os-3.2.6-py3-none-any.whl#sha256=" + "0" * 64
    if pep508:
        spec = "avibe-os @ " + spec
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        release.prepare_release(spec, tmp_path / "out")


def test_missing_pypi_uses_only_first_party_release_index(monkeypatch):
    calls = []
    metadata = {"repository": "avibe-bot/avibe", "schema_version": 1, "releases": [
        {"tag": "gh-v9.0.0rc1", "prerelease": True, "assets": [{"name": "avibe_os-9.0.0rc1-py3-none-any.whl", "sha256": "b" * 64}]},
        {"tag": "v3.2.6", "prerelease": False, "assets": [{"name": "avibe_os-3.2.6-py3-none-any.whl", "sha256": "a" * 64}]},
    ]}

    def get(url, limit):
        calls.append(url)
        if "pypi.org" in url:
            raise OSError("unreachable")
        return json.dumps(metadata).encode()

    monkeypatch.setattr(release, "_get", get)
    assert release._official_wheel(None) == ("https://dl.avibe.bot/releases/v3.2.6/avibe_os-3.2.6-py3-none-any.whl", "a" * 64)
    assert calls == ["https://pypi.org/pypi/avibe-os/json", release.RELEASE_INDEX]


@pytest.mark.parametrize("tags", [["v3.2.6", "v3.2.6.post1"], ["v3.2.6.post1"], ["gh-v9.0.0", "v3.2.6.post1"]])
def test_mirror_latest_includes_official_post_releases(monkeypatch, tags):
    from scripts.release_package_version import package_version_from_release_tag

    metadata = {"schema_version": 1, "repository": "avibe-bot/avibe", "releases": [
        {"tag": tag, "prerelease": False, "assets": [{
            "name": f"avibe_os-{package_version_from_release_tag(tag)}-py3-none-any.whl", "sha256": "a" * 64,
        }]} for tag in tags
    ]}
    monkeypatch.setattr(release, "_get", lambda *_: json.dumps(metadata).encode())
    assert release._mirror_wheel(None)[0] == "https://dl.avibe.bot/releases/v3.2.6.post1/avibe_os-3.2.6.post1-py3-none-any.whl"


def test_explicit_mirror_tag_normalizes_package_filename(monkeypatch):
    metadata = {"schema_version": 1, "repository": "avibe-bot/avibe", "releases": [{
        "tag": "gh-v3.2.5-dev1", "prerelease": True,
        "assets": [{"name": "avibe_os-3.2.5.dev1-py3-none-any.whl", "sha256": "a" * 64}],
    }]}
    monkeypatch.setattr(release, "_get", lambda *_: json.dumps(metadata).encode())
    assert release._mirror_wheel("3.2.5.dev1")[0] == "https://dl.avibe.bot/releases/gh-v3.2.5-dev1/avibe_os-3.2.5.dev1-py3-none-any.whl"


def test_bootstrap_public_version_grammar_and_order_match_release_owner():
    from packaging.version import Version
    from scripts.release_package_version import package_version_from_release_tag, official_stable_version_key

    versions = [f"{base}{phase}{post}" for base in ["03.02.004", "3.2.5", "3.2.6"]
                for phase in ["", "a1", ".b2", "-rc3", ".dev4"] for post in ["", ".post1"]]
    accepted = []
    for version in versions:
        try:
            canonical = package_version_from_release_tag("gh-v" + version)
        except ValueError:
            assert release._public_version(version) is None
            continue
        parsed = release._public_version(version)
        assert parsed[0] == canonical
        assert parsed[2] == (official_stable_version_key("v" + canonical) is not None)
        accepted.append(version)
    assert sorted(accepted, key=lambda value: release._public_version(value)[1]) == sorted(accepted, key=Version)


def test_upgrade_passes_target_constraints_and_keeps_files_until_install_exits(tmp_path):
    target = wheel(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert Path(command[-1]).read_text() == PINS
        assert Path(command[3]).is_file()
        return subprocess.CompletedProcess(command, 0)

    plan = UpgradePlan(["uv", "tool", "install", str(target)], {}, "uv", release_spec_index=3)
    assert execute_upgrade_plan(plan, run=run).returncode == 0
    assert calls[0][-2] == "--constraints"
    assert not Path(calls[0][-1]).exists()


def test_pip_constraints_do_not_accidentally_enable_partial_hash_mode(tmp_path):
    target = wheel(tmp_path)

    def run(command, **kwargs):
        assert command[-2] == "--constraint"
        assert Path(command[-1]).read_text().strip() == "example==1.2.3"
        return subprocess.CompletedProcess(command, 0)

    plan = UpgradePlan([sys.executable, "-m", "pip", "install", str(target)], {}, "pip", release_spec_index=4)
    assert execute_upgrade_plan(plan, run=run).returncode == 0


def test_artifact_cdn_failure_retries_same_version_not_latest(monkeypatch, tmp_path):
    data = wheel(tmp_path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    primary = "https://files.pythonhosted.org/avibe_os-3.2.6-py3-none-any.whl"
    monkeypatch.setattr(release, "_official_wheel", lambda version: (primary, digest))
    seen = []

    def mirror(version):
        seen.append(version)
        return "https://dl.avibe.bot/releases/v3.2.6/avibe_os-3.2.6-py3-none-any.whl", digest

    def get(url, limit):
        if url == primary:
            raise OSError("blocked CDN")
        return data

    monkeypatch.setattr(release, "_mirror_wheel", mirror)
    monkeypatch.setattr(release, "_get", get)
    _, constraints = release.prepare_release("avibe-os", tmp_path / "out")
    assert constraints.read_text() == PINS
    assert seen == ["3.2.6"]


def test_missing_constraints_does_not_invoke_installer(tmp_path):
    target = wheel(tmp_path, constraints=None)
    plan = UpgradePlan(["uv", "tool", "install", str(target)], {}, "uv", release_spec_index=3)
    with pytest.raises(ValueError):
        execute_upgrade_plan(plan, run=lambda *_args, **_kwargs: pytest.fail("must not install"))


def test_installer_embedded_resolver_is_current():
    subprocess.run([sys.executable, str(ROOT / "scripts/sync_install_dependencies.py"), "--check"], check=True)


def test_installer_passes_constraints_and_never_tries_third_party_index(tmp_path):
    script = (ROOT / "install.sh").read_text()
    candidate = script.split("install_package_candidate() {", 1)[1].split("\nresolve_vibe_on_original_path()", 1)[0]
    shell = "install_package_candidate() {" + candidate + "\n" + '''
prepare_release_candidate() {
    printf '%s' "$1" > "$2/package-spec"
    printf '%s' "$2/constraints.txt" > "$2/constraints-path"
    printf 'example==1.2.3' > "$2/constraints.txt"
}
uv_tool_install() {
    printf '%s\\n' "$@"
    test -f "$4"
}
install_package_candidate avibe-os
'''
    result = subprocess.run(["bash", "-c", shell], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[:3] == ["avibe-os", "--force", "--constraints"]
    assert "pypi.tuna.tsinghua.edu.cn" not in script
    assert 'install_package_candidate "git+https://' not in script
