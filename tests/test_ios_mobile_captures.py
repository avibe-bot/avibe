from __future__ import annotations

from collections import Counter
import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess

import pytest


def _manifest_module():
    path = Path(__file__).resolve().parents[1] / "ios/scripts/mobile_capture_manifest.py"
    spec = importlib.util.spec_from_file_location("mobile_capture_manifest", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def _pbx_phase(project: str, phase_id: str) -> str:
    if phase_id.startswith("A"):
        section_name = "PBXResourcesBuildPhase"
    else:
        section_name = "PBXSourcesBuildPhase"
    section = project.split(f"/* Begin {section_name} section */", 1)[1].split(
        f"/* End {section_name} section */", 1
    )[0]
    match = re.search(
        rf"(?ms)^\s*{phase_id} /\*.*?\*/ = \{{.*?^\s*\}};",
        section,
    )
    assert match, f"missing PBX phase {phase_id}"
    return match.group(0)


def _pbx_section_object(project: str, section_name: str, object_id: str) -> str:
    section = project.split(f"/* Begin {section_name} section */", 1)[1].split(
        f"/* End {section_name} section */", 1
    )[0]
    match = re.search(
        rf"(?m)^\s*{object_id} /\*.*?\*/ = \{{.*?\}};",
        section,
    )
    assert match, f"missing PBX object {object_id} in {section_name}"
    return match.group(0)


def _app_target_membership_closure(project: str) -> set[str]:
    closure: set[str] = set()
    for phase_id in ("900000000000000000000001", "A00000000000000000000001"):
        phase = _pbx_phase(project, phase_id)
        closure.add(phase)
        build_ids = re.findall(
            r"^\s*([0-9A-Fa-f]{24}) /\*.*? in (?:Sources|Resources) \*/",
            phase,
            re.MULTILINE,
        )
        for build_id in build_ids:
            build = _pbx_section_object(project, "PBXBuildFile", build_id)
            closure.add(build)
            file_ref = re.search(r"fileRef = ([0-9A-Fa-f]{24})", build)
            assert file_ref, f"build file {build_id} has no fileRef"
            closure.add(_pbx_section_object(project, "PBXFileReference", file_ref.group(1)))
    return closure


def test_fixture_bridge_additions_leave_avibe_target_membership_byte_identical():
    """The production App target must remain exactly the master membership closure."""
    project_path = Path(__file__).resolve().parents[1] / "ios/Avibe.xcodeproj/project.pbxproj"
    current = project_path.read_text(encoding="utf-8")
    master = subprocess.run(
        ["git", "show", "origin/master:ios/Avibe.xcodeproj/project.pbxproj"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert _app_target_membership_closure(current) == _app_target_membership_closure(master)

    fixture_phase = _pbx_phase(current, "900000000000000000000005")
    for build_id in (
        "91000000000000000000000D",  # production NativeBridge.swift
        "91000000000000000000000F",  # production NativeBadge.swift
        "91000000000000000000000E",  # fixture dependency shapes
    ):
        assert build_id in fixture_phase
    for build_id in (
        "91000000000000000000000D",
        "91000000000000000000000F",
        "91000000000000000000000E",
    ):
        assert build_id not in _pbx_phase(current, "900000000000000000000001")


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
