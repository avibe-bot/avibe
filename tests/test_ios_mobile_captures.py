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
