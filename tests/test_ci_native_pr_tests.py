"""Verify routing against a compiled catalog and receipts from the result tree."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import ci_native_pr_tests as native
from scripts.ci_native_pr_tests import (
    CONTRACTS,
    Owners,
    changed_sources,
    declared_classes,
    enumerate_tests,
    enumeration,
    execute_tests,
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


@pytest.mark.parametrize(
    "mode,calls,passes",
    [
        ("success", 1, True),
        ("mach", 2, True),
        ("runner-exit", 2, True),
        ("twice", 2, False),
        ("missing-second", 2, False),
        ("ordinary", 1, False),
        ("unrelated-mach", 1, False),
        ("bad-identity", 1, False),
        ("nonzero", 1, False),
        ("wrong-bundle-with-mach", 1, False),
        ("scheme-with-mach", 1, False),
        ("mixed-errors", 1, False),
        ("scheme-second", 2, False),
        ("fatal-no-catalog", 1, False),
        ("fatal-generic-catalog", 1, False),
        ("fatal-pure-host", 2, True),
    ],
)
def test_enumeration_retry_is_bounded_and_not_a_test_receipt(tmp_path, monkeypatch, capsys, mode, calls, passes):
    catalog = {"errors": [], "values": [{"enabledTests": [{"identifier": PHONE + "()"}], "disabledTests": []}]}
    mach = "IDELaunchReport: Launching AvibeTests Finished with error: Mach error -308 - (ipc/mig) server died"
    seen = []

    def run(cmd, **kwargs):
        assert cmd[:-1] == [
            "xcodebuild",
            "test-without-building",
            "-enumerate-tests",
            "-test-enumeration-style",
            "flat",
            "-test-enumeration-format",
            "json",
            "-test-enumeration-output-path",
        ]
        path = Path(cmd[-1])
        assert path not in seen and not path.exists()
        seen.append(path)
        rc, output, data = 0, "", catalog
        if mode == "twice" or (len(seen) == 1 and mode in ("mach", "missing-second", "scheme-second")):
            data, output = {"errors": ["Unable to enumerate"], "values": []}, mach
        elif mode == "runner-exit" and len(seen) == 1:
            data, rc = {"errors": ["Test runner exited unexpectedly"], "values": []}, 65
        elif mode == "missing-second":
            data = None
        elif mode in ("ordinary", "unrelated-mach"):
            data, output = {"errors": ["scheme not found"]}, ("Mach error -305" if mode == "unrelated-mach" else "")
        elif mode == "bad-identity":
            data = {
                "errors": [],
                "values": [{"enabledTests": [{"identifier": "lost connection to test runner"}], "disabledTests": []}],
            }
        elif mode == "wrong-bundle-with-mach":
            data, output = (
                {
                    "errors": [],
                    "values": [{"enabledTests": [{"identifier": "OtherTests/Phone/testX()"}], "disabledTests": []}],
                },
                mach,
            )
        elif mode in ("scheme-with-mach", "scheme-second", "mixed-errors"):
            reasons = ["Scheme does not support the test action"]
            if mode == "mixed-errors":
                reasons.insert(0, "Test host crashed")
            data, output = {"errors": reasons, "values": []}, ("" if mode == "scheme-second" else mach)
        elif mode.startswith("fatal-") and len(seen) == 1:
            data = {"errors": ["Unable to enumerate"], "values": []} if mode == "fatal-generic-catalog" else None
            rc = 65
            cause = (
                "Test runner exited unexpectedly"
                if mode == "fatal-pure-host"
                else "Scheme Avibe is not currently configured for the test action."
            )
            output = mach + "\nxcodebuild: error: " + cause
        elif mode == "nonzero":
            rc = 65
        if data is not None:
            path.write_text(json.dumps(data))
        return subprocess.CompletedProcess(cmd, rc, output)

    monkeypatch.setattr(subprocess, "run", run)
    if passes:
        assert enumerate_tests(["xcodebuild"], TARGET, tmp_path) == ({PHONE}, set())
    else:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            enumerate_tests(["xcodebuild"], TARGET, tmp_path)
    assert len(seen) == calls
    if mode in ("scheme-with-mach", "scheme-second", "mixed-errors"):
        log = capsys.readouterr().out
        assert f"{TARGET} enumeration attempt {calls}: catalog errors:" in log
        assert "Scheme does not support the test action" in log
    assert (tmp_path / "native-enumeration-retry.txt").exists() == (calls == 2)


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


@pytest.mark.parametrize("mode, calls", [
    ("green", 1), ("stall", 2), ("normal assertion", 1), ("budget exhausted", 1),
    ("driver error", 1), ("unattributed file error", 1), ("suite mention", 1), ("early suite end", 1),
    ("truncated driver", 1), ("extra failure", 1), ("missing contract", 1), ("rerun skipped", 2),
    ("before method", 1), ("after method", 1), ("after suite", 1), ("after driver", 1),
    ("other running method", 1), ("wrong target assertion", 1), ("same method follow-on", 2),
    ("wrong bundle", 2), ("rerun red", 2), ("extra rerun case", 2),
])
def test_selected_execution_recovers_only_the_stalled_method(tmp_path, monkeypatch, capfd, mode, calls):
    case = "-[AvibeTests.PhoneTests testOnPhone]"
    log = f"""Test Case '{case}' started.
WebContent process (0x114121d80) took 30.805767 seconds to launch
T.swift:1: error: {case} : Asynchronous wait failed: Exceeded timeout
Test Case '{case}' failed (61 seconds).
Test Suite 'Selected tests' failed at 2026-11-27 15:50:19.310.
** TEST EXECUTE FAILED **
"""
    end = "Test Suite 'Selected tests' failed at 2026-11-27 15:50:19.310."
    follow_on = f"Fixture.swift:23: error: {case} : XCTAssertTrue failed"
    terminal = f"Test Case '{case}' failed (61 seconds)."
    if mode == "before method":
        log = follow_on + "\n" + log
    elif mode == "after method":
        log = log.replace(end, follow_on + "\n" + end)
    elif mode == "after suite":
        log = log.replace(end, end + "\n" + follow_on)
    elif mode == "after driver":
        log += follow_on + "\n"
    elif mode == "other running method":
        other = "-[AvibeTests.CommonTests testAlways]"
        log = log.replace(end, f"Test Case '{other}' started.\n{follow_on}\nTest Case '{other}' passed.\n{end}")
    elif mode in ("wrong target assertion", "same method follow-on"):
        if mode == "wrong target assertion":
            follow_on = follow_on.replace("AvibeTests.", "UnrelatedTests.")
        log = log.replace(terminal, follow_on + "\n" + terminal)
    if mode == "driver error":
        log += "xcodebuild: error: Failed to collect test diagnostics: the test host disconnected.\n"
    elif mode == "unattributed file error":
        log += "Fixture.swift:27: error: Could not finalize the test session.\n"
    elif mode == "suite mention":
        log = log.replace(end, f"App: waiting for {end} no result yet")
    elif mode == "early suite end":
        log = end + "\n" + log.replace(end, "")
    elif mode == "truncated driver":
        log = log.replace("** TEST EXECUTE FAILED **", "")
    if mode == "normal assertion":
        log = log.replace("Asynchronous wait failed: Exceeded timeout", "XCTAssertEqual failed")
    seen = []
    env = {"FIXTURE": "same"}
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))

    def run(cmd, *, env, stdout=None, stderr=None, check=False):
        assert env == {"FIXTURE": "same"} and cmd[-1] == "test-without-building"
        seen.append([s for s in cmd if s.startswith("-only-testing:")])
        if stdout is not None:
            stdout.write(log.encode())
            return subprocess.CompletedProcess(cmd, 0 if mode == "green" else 65)
        assert check
        if mode == "rerun red":
            raise subprocess.CalledProcessError(65, cmd)

    def receipt(cmd):
        if cmd[-1] == str(tmp_path / f"{TARGET}.xcresult"):
            outcomes = {PHONE: "Passed" if mode == "green" else "Failed", COMMON: "Passed"}
            if mode == "extra failure":
                outcomes[OPTIONAL] = "Failed"
            if mode == "missing contract":
                del outcomes[COMMON]
            return json.dumps(tree(outcomes))
        assert cmd[-1] == str(tmp_path / f"{TARGET}Rerun1.xcresult")
        outcomes = {PHONE: "Skipped" if mode == "rerun skipped" else "Passed"}
        if mode == "extra rerun case":
            outcomes[COMMON] = "Passed"
        return json.dumps(tree(outcomes, "OtherTests" if mode == "wrong bundle" else TARGET))

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", receipt)
    args = (["xcodebuild"], TARGET, [TARGET], {COMMON, PHONE}, [], env, tmp_path, 0 if mode == "budget exhausted" else 3)
    if mode in ("green", "stall", "same method follow-on"):
        assert execute_tests(*args) == calls - 1
    else:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            execute_tests(*args)
    assert seen == [["-only-testing:" + TARGET], ["-only-testing:" + PHONE]][:calls]
    assert (tmp_path / "ios-simulator-stall.txt").exists() == (calls == 2)
    assert ("::warning title=Simulator stall::" in capfd.readouterr().out) == (calls == 2)


@pytest.mark.skipif(sys.platform != 'darwin', reason='The native selector reads the OpenStep project using macOS plutil')
@pytest.mark.parametrize(('paths', 'fixture_builds', 'fixture_runs'), [
    (['ios/Avibe/NativeBridge.swift'], 1, 0),  # Shared app source, not under the fixture directory (#4397).
    (['ios/AvibeSheetFixture/SheetFixtureApp.swift', 'ios/Moved.swift'], 1, 0),  # Deleted/renamed old side.
    (['ios/Avibe/AppModel.swift', 'ios/Other/NativeBridge.swift'], 0, 0),
    ([native.PROJECT], 1, 0),  # Removed/retargeted membership is not in the new graph.
    (['ios/Avibe/NativeBridge.swift', 'ios/AvibeSheetFixtureTests/SomeTest.swift'], 1, 1),
])
def test_native_fixture_build_vs_execution(monkeypatch, tmp_path, paths, fixture_builds, fixture_runs):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    for key in ('SIMULATOR_ID', 'NATIVE_PATH_RECEIPT', 'RUNNER_TEMP', 'GITHUB_STEP_SUMMARY'):
        monkeypatch.setenv(key, str(tmp_path / key))
    target = 'AvibeSheetFixtureTests'
    changes = {target: [object()]} if fixture_runs else {}
    monkeypatch.setattr(native, 'verified_merge_paths', lambda *_: ('base', paths))
    monkeypatch.setattr(native, 'changed_sources', lambda *_: changes)
    commands, enumerated, executed = [], [], []
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] == '/usr/bin/plutil':
            return real_run(cmd, **kwargs)
        commands.append(cmd)
        assert kwargs['check']

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(native, 'verify_membership', lambda *_: None)
    monkeypatch.setattr(native, 'enumerate_tests', lambda _, t, __: (enumerated.append(t), set()))
    monkeypatch.setattr(native, 'selection', lambda *_: (['selected'], set()))
    monkeypatch.setattr(native, 'execute_tests', lambda _, t, *args: executed.append(t) or 0)
    native.main()
    assert [cmd[cmd.index('-scheme') + 1] for cmd in commands if cmd[-1] == 'build-for-testing'] == (
        ['Avibe'] + ['AvibeSheetFixture'] * fixture_builds
    )
    assert enumerated == executed == ['AvibeTests'] + [target] * fixture_runs


@pytest.mark.skipif(sys.platform != 'darwin', reason='Requires the native OpenStep project reader')
def test_fixture_members_have_an_outer_native_pr_route(monkeypatch):
    from scripts.ci_targeted_paths import matched_proofs

    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    project = json.loads(subprocess.check_output(['/usr/bin/plutil', '-convert', 'json', '-o', '-', native.PROJECT]))
    # A new shared source outside ios/ must also reach this selector from Linux.
    for path in native.fixture_members(project):
        assert 'ios-app' in matched_proofs(path), path


def test_unreadable_fixture_project_builds_instead_of_skipping(monkeypatch):
    monkeypatch.setattr(subprocess, 'check_output', lambda *_: b'{}')
    assert native.needs_fixture_build(['ios/Avibe/AppModel.swift'])


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
