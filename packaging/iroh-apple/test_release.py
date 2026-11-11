"""Publication contract: reject false provenance, runtime claims and collisions.

These fixture-only tests exercise the read-only release boundary. The existing
binary gate cannot exercise refusals without bad artifacts or a real release.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location("iroh_release", Path(__file__).with_name("release.py"))
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.tag = "iroh-apple-v1.2.0-1"
        self.sha = "a" * 40
        self.env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/master",
                    "GITHUB_REPOSITORY": "avibe-bot/avibe"}

    def fixture(self, runtime="17.0"):
        d = self.directory
        manifest = {"iroh_ffi_tag": "v1.2.0", "iroh_ffi_commit": "66a8d619a4d791d458d582f312df6e0ab42bc541",
                    "iroh_core_version": "1.4.1", "ios_deployment_target": "17.0", "ios_runtime_tested": runtime}
        (d / "manifest.json").write_text(json.dumps(manifest))
        (d / "ios-runtime-test.json").write_text(json.dumps({"runtime_version": runtime,
            "runtime_identifier": f"com.apple.CoreSimulator.SimRuntime.iOS-{runtime.replace('.', '-')}",
            "xcodebuild_exit_code": 0}))
        (d / "IrohLib.swift").write_text("// generated wrapper\n")
        (d / "ios-runtime-test.log").write_text("Executed 23 tests, with 0 failures\n** TEST SUCCEEDED **\n")
        for sdk in ("iphoneos", "iphonesimulator"):
            (d / f"xcodebuild-{sdk}.log").write_text("** BUILD SUCCEEDED **\n")
        (d / "patched-check.log").write_text("private Apple datapath check passed: scanned 4 binary/archive candidate(s)\n")
        (d / "positive-control-check.log").write_text("ios-arm64/libiroh_ffi.a\nsendmsg_x\nrecvmsg_x\n"
            "positive control passed: found 1 forbidden symbol/string hit(s) in 1 binary/archive candidate(s)\n")
        for target in release.IOS_TARGETS:
            (d / f"feature-graph-{target}.txt").write_text('iroh feature "metrics"\niroh feature "tls-ring"\n')
        with zipfile.ZipFile(d / release.ZIP_NAME, "w") as archive:
            archive.writestr("Iroh.xcframework/Info.plist", "fixture")
            archive.writestr("Iroh.xcframework/ios-arm64/libiroh_ffi.a", b"device")
            archive.writestr("Iroh.xcframework/ios-arm64_x86_64-simulator/libiroh_ffi-ios-simulator.a", b"simulator")
        (d / f"{release.ZIP_NAME}.sha256").write_text(release.digest(d / release.ZIP_NAME) + "  archive.zip\n")
        with zipfile.ZipFile(d / release.SUPPORT_NAME, "w") as archive:
            archive.write(d / "IrohLib.swift", "IrohLib.swift")
            for name in ("LICENSE-APACHE", "LICENSE-MIT"):
                archive.writestr(f"LICENSES/iroh-ffi/{name}", "fixture license")
            for name in ("LICENSE-APACHE", "LICENSE-MIT", "COPYRIGHT.html"):
                archive.writestr(f"LICENSES/rust-1.91.0/{name}", "fixture Rust notice")
            archive.writestr("LICENSES/packages.json", json.dumps([{"name": "iroh-ffi", "version": "1.2.0",
                "files": ["LICENSES/iroh-ffi/LICENSE-MIT"]}]))

    def test_manifest_matches_actual_files_and_keeps_pin_pending(self):
        self.fixture()
        result = release.prepare(self.directory, self.tag, self.sha, "123")
        self.assertEqual(result["release_state"], "pending")
        self.assertEqual(result["iroh_core_version"], "1.4.1")
        self.assertEqual(result["tested_runtime"], "17.0")
        self.assertEqual(result["source_commit"], self.sha)
        self.assertEqual(result["sha256"], release.digest(self.directory / release.ZIP_NAME))
        self.assertEqual(result["assets"]["IrohLib.swift"]["sha256"], release.digest(self.directory / "IrohLib.swift"))
        release.publication_evidence(result)

    def test_gate_fallback_is_recorded_but_cannot_be_published(self):
        self.fixture("17.4")
        result = release.prepare(self.directory, self.tag, self.sha, "123")
        self.assertEqual(result["tested_runtime"], "17.4")
        with self.assertRaisesRegex(ValueError, "equal to deployment"):
            release.publication_evidence(result)

    def test_dispatch_authority_and_tag_pin(self):
        release.publication_context(self.env, self.tag)
        for key, value in (("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_REF", "refs/heads/topic"),
                           ("GITHUB_REF", "refs/tags/v1.0.0"), ("GITHUB_REPOSITORY", "someone/fork")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                release.publication_context({**self.env, key: value}, self.tag)
        for tag in ("v1.2.0", "iroh-apple-v1.2.1-1", "iroh-apple-v1.2.0-0", "iroh-apple-v1.2.0-2", "", "x\ny"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.publication_context(self.env, tag)

    def test_existing_bare_tag_draft_release_and_api_errors_refuse(self):
        for pages in (([[{"name": self.tag}]], [[]]), ([[]], [[{"tag_name": self.tag, "draft": True}]])):
            with self.subTest(pages=pages), patch.object(release, "gh_json", side_effect=pages):
                with self.assertRaisesRegex(ValueError, "already exists"):
                    release.refuse_existing_tag(self.tag)
        with patch.object(release, "gh_json", side_effect=subprocess.CalledProcessError(1, "gh")):
            with self.assertRaises(subprocess.CalledProcessError):
                release.refuse_existing_tag(self.tag)
        with patch.object(release, "gh_json", return_value=[[]]):
            release.refuse_existing_tag(self.tag)

    def test_corrupt_or_missing_evidence_is_rejected(self):
        cases = [
            ("manifest.json", '{"iroh_ffi_tag":"v9.9.9"}', "pin mismatch"),
            ("ios-runtime-test.json", '{"runtime_version":"17.0","xcodebuild_exit_code":65}', "runtime identifier"),
            ("ios-runtime-test.log", "** TEST SUCCEEDED **", "executed passing"),
            ("patched-check.log", "check failed", "patched check"),
            ("positive-control-check.log", "sendmsg_x recvmsg_x in macos-arm64", "iOS positive"),
            ("IrohLib.xcframework.zip.sha256", "0" * 64, "checksum mismatch"),
        ]
        for name, text, error in cases:
            with self.subTest(name=name):
                self.fixture()
                (self.directory / name).write_text(text)
                with self.assertRaisesRegex(ValueError, error):
                    release.prepare(self.directory, self.tag, self.sha, "123")
        self.fixture()
        (self.directory / "IrohLib.swift").unlink()
        with self.assertRaisesRegex(ValueError, "missing artifact"):
            release.prepare(self.directory, self.tag, self.sha, "123")

    def test_warnings_in_all_three_build_logs_refuse(self):
        for name in ("xcodebuild-iphoneos.log", "xcodebuild-iphonesimulator.log", "ios-runtime-test.log"):
            with self.subTest(name=name):
                self.fixture()
                with (self.directory / name).open("a") as output:
                    output.write("object was built for newer 'iOS-simulator' version (17.5) than being linked (17.0)\n")
                with self.assertRaisesRegex(ValueError, "deployment mismatch"):
                    release.prepare(self.directory, self.tag, self.sha, "123")

    def test_support_collects_licenses_and_rejects_missing_notice_source(self):
        self.fixture()
        source = self.directory / "upstream"
        source.mkdir()
        for name in ("LICENSE-APACHE", "LICENSE-MIT"):
            (source / name).write_text("upstream license fixture")
        package = self.directory / "dependency"
        package.mkdir()
        (package / "LICENSE").write_text("dependency license fixture")
        metadata = {"packages": [{"name": "dependency", "version": "1.0.0", "license": "MIT",
                                  "manifest_path": str(package / "Cargo.toml")}]}
        rust_notices = self.directory / "share/doc/rust"
        rust_notices.mkdir(parents=True)
        for name in ("LICENSE-APACHE", "LICENSE-MIT", "COPYRIGHT.html"):
            (rust_notices / name).write_text("Rust distribution notice fixture")
        with patch.object(release.subprocess, "check_output", side_effect=[json.dumps(metadata), str(self.directory)] * 2):
            release.support_bundle(source, self.directory)
            with zipfile.ZipFile(self.directory / release.SUPPORT_NAME) as archive:
                self.assertEqual(archive.read("LICENSES/dependency-1.0.0/LICENSE"), b"dependency license fixture")
            (package / "LICENSE").unlink()
            with self.assertRaisesRegex(ValueError, "no redistribution license"):
                release.support_bundle(source, self.directory)

    def test_auxiliary_tag_is_not_an_official_product_version(self):
        # Exercise the existing release selector, not a duplicated tag regex.
        helper_path = Path(__file__).resolve().parents[2] / "scripts/release_package_version.py"
        spec = importlib.util.spec_from_file_location("version_helpers", helper_path)
        helpers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helpers)
        self.assertIsNone(helpers.official_stable_version_key(self.tag))
        official = {"tag_name": "v3.1.0", "draft": False, "prerelease": False}
        self.assertEqual(helpers.latest_official_release([{"tag_name": self.tag}, official]), official)


if __name__ == "__main__":
    unittest.main()
