from __future__ import annotations

from collections import Counter
import hashlib
import importlib.util
from pathlib import Path
import re

import pytest


def _manifest_module():
    path = Path(__file__).resolve().parents[1] / "ios/scripts/mobile_capture_manifest.py"
    spec = importlib.util.spec_from_file_location("mobile_capture_manifest", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pbx_objects(project: str, section_name: str) -> dict[str, str]:
    section = project.split(f"/* Begin {section_name} section */", 1)[1].split(
        f"/* End {section_name} section */", 1
    )[0]
    return dict(re.findall(
        r"(?ms)^\s*([0-9A-Fa-f]{24}) /\*[^\n]*?\*/ = (\{.*?\});",
        section,
    ))


def _validate_fixture_membership(project: str) -> None:
    """Validate this checkout, without Git history or a moving remote baseline."""
    definitions = set(re.findall(
        r"(?m)^\s*([0-9A-Fa-f]{24})(?: /\*[^\n]*?\*/)? = \{",
        project,
    ))
    builds = _pbx_objects(project, "PBXBuildFile")
    files = _pbx_objects(project, "PBXFileReference")
    sources = _pbx_objects(project, "PBXSourcesBuildPhase")
    resources = _pbx_objects(project, "PBXResourcesBuildPhase")
    groups = _pbx_objects(project, "PBXGroup")
    for body in [*builds.values(), *sources.values(), *resources.values(), *groups.values()]:
        references = set(re.findall(r"\b[0-9A-Fa-f]{24}\b", body))
        assert references <= definitions, f"Dangling PBX references: {references - definitions}"

    def phase_files(body: str) -> set[str]:
        result = set()
        for build_id in re.findall(r"\b[0-9A-Fa-f]{24}\b", body):
            assert build_id in builds, f"Missing PBXBuildFile {build_id}"
            ref = re.search(r"fileRef = ([0-9A-Fa-f]{24})", builds[build_id])
            assert ref, f"Missing fileRef in {build_id}"
            assert ref.group(1) in files, f"Missing PBXFileReference {ref.group(1)}"
            result.add(files[ref.group(1)])
        return result

    fixture_id = "900000000000000000000005"
    fixture_files = phase_files(sources[fixture_id])
    for name in ("NativeBridge.swift", "NativeBadge.swift", "FixtureNativeBridgeSupport.swift"):
        assert any(f"path = {name};" in entry for entry in fixture_files), name
    for phase_id, body in sources.items():
        entries = phase_files(body)
        if phase_id != fixture_id:
            assert not any("FixtureNativeBridgeSupport.swift" in entry for entry in entries)
    for body in resources.values():
        phase_files(body)
    fixture_resources = phase_files(resources["A00000000000000000000004"])
    assert any("path = MobileApp;" in entry for entry in fixture_resources)


def test_fixture_bridge_membership_is_self_contained():
    project = Path(__file__).resolve().parents[1] / "ios/Avibe.xcodeproj/project.pbxproj"
    _validate_fixture_membership(project.read_text(encoding="utf-8"))


@pytest.mark.parametrize("missing_id", [
    "2000000000000000000000B1",  # shared production push processor
    "91000000000000000000000A",  # real MobileApp fixture resource
])
def test_fixture_membership_rejects_dangling_references(missing_id: str):
    project = Path(__file__).resolve().parents[1] / "ios/Avibe.xcodeproj/project.pbxproj"
    broken = re.sub(
        rf"(?m)^\s*{missing_id} /\*[^\n]*?\*/ = \{{[^\n]*\}};\n",
        "",
        project.read_text(encoding="utf-8"),
    )
    with pytest.raises(AssertionError, match="Dangling PBX references"):
        _validate_fixture_membership(broken)


def test_fixture_membership_rejects_support_in_production_sources():
    project = Path(__file__).resolve().parents[1] / "ios/Avibe.xcodeproj/project.pbxproj"
    text = project.read_text(encoding="utf-8")
    source_phase = _pbx_objects(text, "PBXSourcesBuildPhase")["900000000000000000000001"]
    broken_phase = source_phase.replace(
        "files = (",
        "files = (\n91000000000000000000000E /* fixture support */,",
        1,
    )
    with pytest.raises(AssertionError):
        _validate_fixture_membership(text.replace(source_phase, broken_phase, 1))


def test_ios_project_object_ids_are_unique():
    """Duplicate IDs silently redirect app sources into XCTest-only files."""
    project = Path(__file__).resolve().parents[1] / "ios/Avibe.xcodeproj/project.pbxproj"
    identities = re.findall(
        r"^\s*([0-9A-Fa-f]{24})(?: /\*.*?\*/)? = \{\s*isa =",
        project.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert identities
    duplicates = [identity for identity, count in Counter(identities).items() if count > 1]
    assert not duplicates, f"Duplicate PBX object definitions: {duplicates}"


def test_capture_manifest_binds_exact_bytes_and_all_24_named_states(tmp_path: Path):
    """A stale/static bundle or missing locale/theme must not look like final captures."""
    bundle = tmp_path / "MobileApp"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "mobile.html").write_bytes(b"<html>real entry</html>")
    (bundle / "assets/app.js").write_bytes(b"real built javascript")
    manifest = _manifest_module().capture_manifest(bundle, "a" * 40)
    assert manifest["source_sha"] == "a" * 40
    assert manifest["bundle_sha256"] == {
        "mobile.html": hashlib.sha256(b"<html>real entry</html>").hexdigest(),
        "assets/app.js": hashlib.sha256(b"real built javascript").hexdigest(),
    }
    expected = {
        f"mobile-{state}-{language}-{theme}"
        for state in ("default", "offline-open", "open-failed", "stale", "account", "first-run")
        for language in ("zh", "en")
        for theme in ("light", "dark")
    }
    assert len(manifest["expected_attachments"]) == 24
    assert set(manifest["expected_attachments"]) == expected
    (bundle / "assets/app.js").write_bytes(b"different merged UI")
    assert _manifest_module().capture_manifest(bundle, "a" * 40)["bundle_sha256"] != manifest["bundle_sha256"]


@pytest.mark.parametrize("sha", ["master", "abc123", "A" * 40])
def test_capture_manifest_rejects_moving_or_abbreviated_source(tmp_path: Path, sha: str):
    with pytest.raises(ValueError, match="exact lowercase"):
        _manifest_module().capture_manifest(tmp_path, sha)


def test_capture_manifest_rejects_static_fixture_and_symlinked_bundle(tmp_path: Path):
    module = _manifest_module()
    (tmp_path / "mobile.html").write_text("static fixture", encoding="utf-8")
    with pytest.raises(ValueError, match="real built"):
        module.capture_manifest(tmp_path, "b" * 40)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/link.js").symlink_to(tmp_path / "mobile.html")
    with pytest.raises(ValueError, match="symbolic links"):
        module.capture_manifest(tmp_path, "b" * 40)
