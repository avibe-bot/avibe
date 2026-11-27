"""Verify routing against a compiled catalog and receipts from the result tree."""

import subprocess

import pytest

from scripts.ci_native_pr_tests import CONTRACTS, changed_sources, enumeration, selection, verify, verify_membership

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


def test_conditional_second_class_selected_and_verified(tmp_path):
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


def test_deletion_and_rename_read_old_extension_but_not_vanished_class(tmp_path):
    old, new = "ios/AvibeTests/Before.swift", "ios/AvibeTests/After.swift"
    repo(tmp_path, {old: "extension CommonTests {}\nclass RemovedTests: XCTestCase {}"})
    (tmp_path / old).unlink()
    deleted = changed_sources([old], tmp_path, "HEAD")
    assert selection(TARGET, deleted[TARGET], ALL, set())[1] == CONTRACTS | {COMMON}
    (tmp_path / new).write_text("class PhoneTests: XCTestCase {}")
    renamed = changed_sources([old, new], tmp_path, "HEAD")
    assert selection(TARGET, renamed[TARGET], ALL, set())[1] == CONTRACTS | {COMMON, PHONE}
    assert selection(TARGET, [({"RemovedTests"}, False, tmp_path / "Gone.swift")], ALL, set())[1] == CONTRACTS


def test_app_only_and_helpers_and_other_scheme(tmp_path):
    assert not changed_sources(["ios/Avibe/App.swift"], tmp_path, "HEAD")
    assert selection(TARGET, [], ALL, set()) == (sorted(CONTRACTS), CONTRACTS)
    assert (
        selection(
            TARGET,
            [({"NativeBadgeTests", "FakeAppBadgeCenter", "NSObject"}, True, tmp_path / "Badge.swift")],
            ALL,
            set(),
        )[0].count(TARGET)
        == 0
    )
    assert selection(TARGET, [({"FakeClock"}, True, tmp_path / "Support.swift")], ALL, set()) == ([TARGET], CONTRACTS)
    fix = "AvibeSheetFixtureTests/HoldTouchUITests/testTouch"
    assert selection(
        "AvibeSheetFixtureTests", [({"HoldTouchUITests"}, True, tmp_path / "Sheet.swift")], {fix}, set()
    ) == (["AvibeSheetFixtureTests/HoldTouchUITests"], {fix})


def test_unregistered_file_is_not_a_helper_only_green(tmp_path):
    swift = tmp_path / "New.swift"
    sources = [({"PhoneTests"}, True, swift)]
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
        selection(TARGET, [({"PhoneTests"}, True, tmp_path / "Phone.swift")], enabled, disabled)
    with pytest.raises(ValueError):
        enumeration({"errors": ["failed"], "values": [config]}, TARGET)
