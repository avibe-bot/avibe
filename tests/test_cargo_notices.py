"""Target-graph coverage and fail-closed notice provenance, without Cargo/network.

The Apple zip tests cannot catch collecting unrelated workspace/platform crates,
misclassifying source modules, or accepting unreviewed licensing exceptions.
"""
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import cargo_notices as notices


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "LICENSE-MIT").write_text("upstream root notice")
    (source / "Cargo.toml").write_text('[package]\nname="root"\nversion="1.0.0"\n')
    root = {"id": "root", "name": "root", "version": "1.0.0", "license": "MIT",
            "source": None, "manifest_path": str(source / "Cargo.toml")}
    dependency = tmp_path / "vendor/dep"
    dependency.mkdir(parents=True)
    (dependency / "Cargo.toml").write_text('[package]\nname="dep"\nversion="1.0.0"\n')
    (dependency / "LICENSE").write_text("actual upstream notice")
    package = {"id": "dep", "name": "dep", "version": "1.0.0", "license": "MIT", "source": notices.REGISTRY,
               "manifest_path": str(dependency / "Cargo.toml"), "authors": ["Upstream author"],
               "repository": "https://github.com/example/project"}
    (dependency / ".cargo-checksum.json").write_text(json.dumps({"package": "a" * 64, "files": {}}))
    lockfile = source / "Cargo.lock"
    lockfile.write_text('version=3\n[[package]]\nname="root"\nversion="1.0.0"\n'
                       f'[[package]]\nname="dep"\nversion="1.0.0"\nsource="{notices.REGISTRY}"\nchecksum="{"a" * 64}"\n')
    policy = {"expressions": {"MIT": "MIT", "MPL-2.0": "MPL-2.0"}, "packages": {}}
    policy_path = tmp_path / "policy.json"

    def collect():
        policy_path.write_text(json.dumps(policy))
        with patch.object(notices, "resolved_packages", return_value=[root, package]):
            return notices.collect_notices(lockfile, ["aarch64-apple-ios"], "1.91.0", "root",
                                           policy_path, tmp_path / "out")
    return dependency, package, policy, collect


def test_target_graph_traverses_all_kinds_but_not_unreachable_workspace_or_platform_packages(tmp_path):
    lockfile = tmp_path / "Cargo.lock"
    lockfile.write_text("unchanged lock")
    packages = [{"id": x, "name": x, "version": "1.0.0"} for x in ["root", "normal", "macro", "dev", "sim", "android", "node"]]
    def metadata(command, **kwargs):
        target = command[-1]
        dependencies = ["normal", "macro", "dev"] + (["sim"] if target == "simulator" else [])
        nodes = [{"id": p["id"], "deps": []} for p in packages]
        nodes[0]["deps"] = [{"pkg": x} for x in dependencies]
        nodes[-1]["deps"] = [{"pkg": "android"}]
        return json.dumps({"packages": packages, "workspace_members": ["root", "node"], "resolve": {"nodes": nodes}})
    with patch.object(notices.subprocess, "check_output", side_effect=metadata) as cargo:
        result = notices.resolved_packages(lockfile, ["device", "simulator"], "1.91.0", "root")
    assert {p["name"] for p in result} == {"root", "normal", "macro", "dev", "sim"}
    assert cargo.call_count == 2
    assert all("--locked" in c.args[0] and "--filter-platform" in c.args[0] for c in cargo.call_args_list)


def test_bundled_notices_and_workspace_root_are_preserved(fixture):
    _, _, _, collect = fixture
    result = collect()
    assert result["counts"] == {"bundled": 2}
    assert result["packages"][0]["selected_license"] == "MIT"
    assert result["packages"][1]["files"] == ["LICENSES/dep-1.0.0/LICENSE"]


@pytest.mark.parametrize("fault", ["expression", "missing", "source-code", "checksum", "source"])
def test_unknown_or_mismatched_license_inputs_fail_closed(fixture, fault):
    root, package, _, collect = fixture
    expected = "no reviewed redistribution"
    if fault == "expression":
        package["license"] = "AGPL-3.0-only"
        expected = "unreviewed license expression"
    elif fault == "checksum":
        (root / ".cargo-checksum.json").write_text('{"package":"wrong"}')
        expected = "vendor/lock checksum mismatch"
    elif fault == "source":
        package["source"] = "git+https://example.com/unreviewed"
        expected = "unreviewed crate source"
    else:
        (root / "LICENSE").unlink()
        if fault == "source-code":
            (root / "copying.rs").write_text("fn copying() {}")
    with pytest.raises(ValueError, match=expected):
        collect()


@pytest.mark.parametrize("kind", ["upstream", "standard"])
@pytest.mark.parametrize("fault", [None, "content", "crate", "expression", "url", "network"])
def test_reviewed_supplements_preserve_provenance_and_fail_closed(fixture, kind, fault):
    root, _, policy, collect = fixture
    (root / "LICENSE").unlink()
    payload = b"immutable license text; no invented copyright"
    url = f"https://raw.githubusercontent.com/example/project/{'b' * 40}/LICENSE"
    entry = {"crate_sha256": "a" * 64, "license": "MIT", "class": kind,
             "files": [{"name": "STANDARD-MIT.txt" if kind == "standard" else "LICENSE",
                        "url": url, "sha256": digest(payload)}]}
    policy["packages"]["dep-1.0.0"] = entry
    if fault == "content": entry["files"][0]["sha256"] = "0" * 64
    if fault == "crate": entry["crate_sha256"] = "0" * 64
    if fault == "expression": entry["license"] = "Apache-2.0"
    if fault == "url": entry["files"][0]["url"] = url.replace("b" * 40, "master")
    with patch.object(notices.urllib.request, "urlopen", side_effect=OSError("offline") if fault == "network" else None,
                      return_value=io.BytesIO(payload)):
        if fault:
            with pytest.raises((ValueError, OSError)):
                collect()
        else:
            result = collect()
            p = result["packages"][1]
            assert p["class"] == kind
            assert p["authors"] == ["Upstream author"]
            assert p["supplemental_sources"] == entry["files"]
            assert ("LICENSES/dep-1.0.0/Cargo.toml" in p["files"]) == (kind == "standard")


@pytest.mark.parametrize("modified", [False, True])
def test_mpl_notice_links_exact_unmodified_covered_source(fixture, modified):
    root, package, _, collect = fixture
    package["license"] = "MPL-2.0"
    (root / ".cargo_vcs_info.json").write_text(json.dumps({"git": {"sha1": "c" * 40}}))
    file = root / "lib.rs"
    file.write_bytes(b"upstream covered source")
    checksum = {"package": "a" * 64, "files": {"lib.rs": digest(file.read_bytes())}}
    (root / ".cargo-checksum.json").write_text(json.dumps(checksum))
    if modified:
        file.write_text("modified source")
        with pytest.raises(ValueError, match="MPL source was modified"):
            collect()
    else:
        result = collect()
        source = result["mpl_sources"][0]
        assert source["modification_status"] == "unmodified"
        assert source["registry_url"].endswith("/dep/dep-1.0.0.crate")
        assert source["upstream_url"].endswith("/tree/" + "c" * 40)
        notice = (root.parents[1] / "out/NOTICE.md").read_text()
        assert source["registry_url"] in notice and source["upstream_url"] in notice
