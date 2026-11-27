"""Verify routing against a compiled catalog and receipts from the result tree."""

import json
from pathlib import Path
import subprocess

import pytest

from scripts.ci_native_pr_tests import (
    CONTRACTS,
    Owners,
    changed_sources,
    declared_classes,
    enumeration,
    fixture_env,
    selection,
    verify,
    verify_membership,
)

TARGET = "AvibeTests"
COMMON = "AvibeTests/CommonTests/testAlways"
PHONE = "AvibeTests/PhoneTests/testOnPhone"
OPTIONAL = "AvibeTests/DirectIrohInteropTests/testNeedsSidecar"
ALL = CONTRACTS | {COMMON, PHONE, OPTIONAL}


def repo(tmp, files):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=case", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp,
            check=True,
            capture_output=True,
        )

    git("init")
    for path, text in files.items():
        file = tmp / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
    git("add", ".")
    git("commit", "--allow-empty", "-m", "base")


def tree(statuses, bundle=TARGET):
    return {
        "testNodes": [
            {
                "nodeType": "Test Plan",
                "children": [
                    {
                        "nodeType": "Unit test bundle",
                        "name": bundle,
                        "children": [
                            {"nodeType": "Test Case", "nodeIdentifier": s.split("/", 1)[1] + "()", "result": status}
                            for s, status in statuses.items()
                        ],
                    }
                ],
            }
        ]
    }


@pytest.fixture
def swift_parser():
    # The hosted native job installs and checks both imports before running this
    # suite. Linux unit shards do not install native CI-only parser bindings.
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_swift")


def test_conditional_second_class_selected_and_verified(tmp_path, swift_parser):
    path = "ios/AvibeTests/UnrelatedFilename.swift"
    repo(tmp_path, {path: "class CommonTests: XCTestCase {}"})
    (tmp_path / path).write_text(
        "class CommonTests: XCTestCase {}\n#if os(iOS)\nclass PhoneTests: XCTestCase {}\n#endif\nclass FakeClock: NSObject {}"
    )
    sources = changed_sources([path], tmp_path, "HEAD")
    selectors, required = selection(TARGET, sources[TARGET], ALL, set())
    assert set(selectors) == CONTRACTS | {"AvibeTests/CommonTests", "AvibeTests/PhoneTests"}
    assert required == CONTRACTS | {COMMON, PHONE}
    for absent in required:
        with pytest.raises(ValueError):
            verify(tree(dict.fromkeys(required - {absent}, "Passed")), TARGET, required)


def test_deletion_and_rename_read_old_extension_but_not_vanished_class(tmp_path, swift_parser):
    old, new = "ios/AvibeTests/Before.swift", "ios/AvibeTests/After.swift"
    repo(tmp_path, {old: "extension CommonTests {}\nclass RemovedTests: XCTestCase {}"})
    (tmp_path / old).unlink()
    deleted = changed_sources([old], tmp_path, "HEAD")
    assert selection(TARGET, deleted[TARGET], ALL, set())[1] == CONTRACTS | {COMMON}
    (tmp_path / new).write_text("class PhoneTests: XCTestCase {}")
    renamed = changed_sources([old, new], tmp_path, "HEAD")
    assert selection(TARGET, renamed[TARGET], ALL, set())[1] == CONTRACTS | {COMMON, PHONE}
    assert selection(TARGET, [(Owners({"RemovedTests"}), False, tmp_path / "Gone.swift")], ALL, set())[1] == CONTRACTS


def test_app_only_and_helpers_and_other_scheme(tmp_path):
    assert not changed_sources(["ios/Avibe/App.swift"], tmp_path, "HEAD")
    assert selection(TARGET, [], ALL, set()) == (sorted(CONTRACTS), CONTRACTS)
    assert (
        selection(
            TARGET,
            [(Owners({"NativeBadgeTests", "FakeAppBadgeCenter", "NSObject"}), True, tmp_path / "Badge.swift")],
            ALL,
            set(),
        )[0].count(TARGET)
        == 0
    )
    assert selection(TARGET, [(Owners({"FakeClock"}), True, tmp_path / "Support.swift")], ALL, set()) == (
        [TARGET],
        CONTRACTS,
    )
    fix = "AvibeSheetFixtureTests/HoldTouchUITests/testTouch"
    assert selection(
        "AvibeSheetFixtureTests", [(Owners({"HoldTouchUITests"}), True, tmp_path / "Sheet.swift")], {fix}, set()
    ) == (["AvibeSheetFixtureTests/HoldTouchUITests"], {fix})


def test_real_shared_helper_and_protocol_reference(swift_parser):
    root = Path(__file__).resolve().parents[1] / "ios/AvibeTests"
    available = ALL | {f"{TARGET}/DirectPathAppTests/testPath", f"{TARGET}/DirectIrohInteropProtocolTests/testProtocol"}
    helper = root / "WebKitReadiness.swift"
    protocol = root / "DirectIrohInteropProtocolTests.swift"
    owners = declared_classes(helper.read_bytes())
    assert "DirectPathAppTests" not in owners.classes | owners.extensions
    assert selection(TARGET, [(owners, True, helper)], available, set()) == ([TARGET], CONTRACTS)
    owners = declared_classes(protocol.read_bytes())
    assert "DirectIrohInteropTests" not in owners.classes | owners.extensions
    assert selection(TARGET, [(owners, True, protocol)], available, set())[1] == CONTRACTS | {
        f"{TARGET}/DirectIrohInteropProtocolTests/testProtocol"
    }
    assert declared_classes(
        b'// class Bogus {}\nlet text = #"extension Bogus {}"#\nextension Mod.PhoneTests {}'
    ) == Owners(extensions={"Mod.PhoneTests"})


def test_ambiguous_parse_requires_full_receipts_even_on_deletion(tmp_path, swift_parser):
    name = "ios/AvibeTests/Old.swift"
    repo(tmp_path, {name: "class {"})
    (tmp_path / name).unlink()
    src = changed_sources([name], tmp_path, "HEAD")
    assert selection(TARGET, src[TARGET], ALL, set()) == ([TARGET], ALL - {OPTIONAL})


@pytest.mark.parametrize("owners", [{"DirectIrohInteropTests"}, {"DirectIrohInteropTests", "PhoneTests"}])
def test_named_master_only_exception_does_not_waive_other_receipts(owners):
    selected, required = selection(TARGET, [(Owners(owners), True, Path("Mixed.swift"))], ALL, set())
    assert not any("DirectIrohInteropTests" in s for s in [*selected, *required])
    assert required == (CONTRACTS | {PHONE} if "PhoneTests" in owners else CONTRACTS)


def test_unknown_ownership_cannot_hide_disabled_method(swift_parser):
    source = Path(__file__).resolve().parents[1] / "ios/AvibeTests/MobileOnboardingTests.swift"
    method = f"{TARGET}/MobileOnboardingTests/testBundledPageLoadsItsReactApplicationThroughTheAppScheme"
    # Use the production parser; the rule holds even if a newer grammar can
    # establish the class directly later.
    try:
        owners = declared_classes(source.read_bytes())
    except ValueError:
        owners = None
    with pytest.raises(ValueError, match="unavailable"):
        selection(TARGET, [(owners, True, source)], ALL, {method})


@pytest.mark.parametrize("old_only", [False, True])
def test_unresolved_extension_cannot_hide_behind_known_class(tmp_path, swift_parser, old_only):
    path = "ios/AvibeTests/Suites.swift"
    name = tmp_path / path
    mixed = (
        "class CommonTests: XCTestCase {}\n"
        "typealias Alternate = PhoneTests\n"
        "extension Alternate { func testViaAlias() {} }"
    )
    repo(tmp_path, {path: mixed if old_only else "class CommonTests: XCTestCase {}"})
    name.write_text("class CommonTests: XCTestCase {}" if old_only else mixed)
    sources = changed_sources([path], tmp_path, "HEAD")[TARGET]
    method = f"{TARGET}/PhoneTests/testViaAlias"
    catalog = ALL | {method}
    selectors, required = selection(TARGET, sources, catalog - {OPTIONAL}, {OPTIONAL})
    assert selectors == [TARGET] and required == catalog - {OPTIONAL}
    with pytest.raises(ValueError, match="testViaAlias"):
        verify(tree(dict.fromkeys(required - {method}, "Passed")), TARGET, required)
    with pytest.raises(ValueError, match="unavailable"):
        selection(TARGET, sources, catalog - {method}, {method})


def test_native_capture_env_is_scoped(tmp_path):
    env = {"KEEP": "yes"}
    with fixture_env(set(), env, tmp_path) as got:
        assert got == env
    with fixture_env({f"{TARGET}/NativeScreenCaptureTests/testCaptureNativeScreens"}, env, tmp_path) as got:
        assert got == {**env, "TEST_RUNNER_AVIBE_NATIVE_CAPTURES": "1"}
    assert env == {"KEEP": "yes"}


@pytest.mark.parametrize("valid", [True, False])
def test_show_fixture_requires_own_readiness_and_cleans_up(tmp_path, monkeypatch, valid):
    class Child:
        pid = 1234
        closed = False

        def poll(self):
            return None

        def terminate(self):
            self.closed = True

        def wait(self, timeout):
            assert self.closed

    child = Child()

    def spawn(args):
        assert args[:3] == ["node", "ios/scripts/show_share_foreground.mjs", "--report-file"]
        Path(args[-1]).write_text(json.dumps({"pid": child.pid if valid else -1, "url": "http://127.0.0.1:8765"}))
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(ValueError, match="after readiness" if valid else "Invalid Show"):
        with fixture_env({f"{TARGET}/ShowShareForegroundTests/testShow"}, {}, tmp_path) as env:
            assert env["TEST_RUNNER_AVIBE_I1_FOREGROUND_ORIGIN"] == "http://127.0.0.1:8765"
            raise ValueError("after readiness")
    assert child.closed and not list(tmp_path.iterdir())


def test_unregistered_file_is_not_a_helper_only_green(tmp_path):
    swift = tmp_path / "New.swift"
    sources = [(Owners({"PhoneTests"}), True, swift)]
    with pytest.raises(ValueError, match="compiler file list"):
        verify_membership(TARGET, sources, tmp_path)
    listing = (
        tmp_path
        / "Build/Intermediates.noindex/Avibe.build/Debug-iphonesimulator/AvibeTests.build/Objects-normal/arm64/AvibeTests.SwiftFileList"
    )
    listing.parent.mkdir(parents=True)
    listing.write_text(str(swift) + "\n")
    verify_membership(TARGET, sources, tmp_path)


def test_only_required_identities_must_pass_from_the_right_bundle():
    statuses = dict.fromkeys(ALL, "Passed")
    statuses[OPTIONAL] = "Skipped"
    verify(tree(statuses), TARGET, CONTRACTS)
    with pytest.raises(ValueError):
        verify(tree(statuses), TARGET, CONTRACTS | {OPTIONAL})
    with pytest.raises(ValueError):
        verify(tree(statuses, "UnrelatedTests"), TARGET, CONTRACTS)
    forged = tree(dict.fromkeys(CONTRACTS, "Passed"))
    forged["testNodes"][0]["children"].append({"nodeType": "Failure Message", "name": PHONE, "result": "Passed"})
    with pytest.raises(ValueError):
        verify(forged, TARGET, CONTRACTS | {PHONE})


def test_disabled_touched_method_is_not_passing_evidence(tmp_path):
    config = {
        "enabledTests": [{"identifier": s + "()"} for s in ALL - {PHONE}],
        "disabledTests": [{"identifier": PHONE + "()"}],
    }
    enabled, disabled = enumeration({"errors": [], "values": [config]}, TARGET)
    with pytest.raises(ValueError, match="unavailable"):
        selection(TARGET, [(Owners({"PhoneTests"}), True, tmp_path / "Phone.swift")], enabled, disabled)
    with pytest.raises(ValueError):
        enumeration({"errors": ["failed"], "values": [config]}, TARGET)
