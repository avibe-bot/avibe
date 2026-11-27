"""Run the fixed iOS PR contracts and the compiled XCTest classes a PR touches."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from scripts.ci_docs_only import classify

SCHEMES = {"AvibeTests": "Avibe", "AvibeSheetFixtureTests": "AvibeSheetFixture"}
CONTRACTS = {
    "AvibeTests/NativeBadgeTests/testBadgeSetTakesExactlyOneCountNumber",
    "AvibeTests/NavigationPolicyTests/testConfiguredOriginRequiresHTTPSAndCanonicalizesToOrigin",
    "AvibeTests/SystemScheduleTests/testSharedContractVectorsCoverSwiftParsersAndEveryTerminalReceipt",
}


MASTER_INTEROP = "AvibeTests/DirectIrohInteropTests"


@contextmanager
def fixture_env(required, env, temp):
    child_env = dict(env)
    if any(s.startswith("AvibeTests/NativeScreenCaptureTests/") for s in required):
        child_env["TEST_RUNNER_AVIBE_NATIVE_CAPTURES"] = "1"
    if not any(s.startswith("AvibeTests/ShowShareForegroundTests/") for s in required):
        yield child_env
        return
    # The repository fixture owns two disposable loopback listeners. Wait for its
    # own readiness record, not a fixed port or a process-wide environment value.
    with tempfile.TemporaryDirectory(dir=temp) as directory:
        report = Path(directory) / "ready.json"
        proc = subprocess.Popen(["node", "ios/scripts/show_share_foreground.mjs", "--report-file", str(report)])
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    ready = json.loads(report.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.1)
                    continue
                if ready.get("pid") != proc.pid or not re.fullmatch(
                    r"http://127\.0\.0\.1:[1-9]\d{0,4}", ready.get("url", "")
                ):
                    raise ValueError("Invalid Show foreground fixture readiness")
                child_env["TEST_RUNNER_AVIBE_I1_FOREGROUND_ORIGIN"] = ready["url"]
                break
            else:
                raise RuntimeError("Show foreground fixture did not become ready")
            yield child_env
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def declared_classes(source):
    """Parse Swift syntax, including declarations inside conditional branches."""
    from tree_sitter import Language, Parser
    import tree_sitter_swift

    tree = Parser(Language(tree_sitter_swift.language())).parse(source)
    names = set()
    todo = [tree.root_node]
    while todo:
        node = todo.pop()
        if node.is_error or node.is_missing:
            raise ValueError("Cannot establish Swift declaration ownership")
        # Function bodies cannot contribute test suite declarations/extensions at
        # file scope. Ignore parser recovery for newer syntax inside those bodies.
        if node.type == "function_body":
            continue
        if node.type == "class_declaration":
            kind = node.child_by_field_name("declaration_kind")
            if kind and kind.type in ("class", "extension"):
                name = node.child_by_field_name("name")
                if kind.type == "extension" and name and name.type == "user_type":
                    identifiers = [part for part in name.named_children if part.type == "type_identifier"]
                    name = identifiers[-1] if identifiers else None
                if not name or name.type != "type_identifier" or name.has_error:
                    raise ValueError("Cannot establish Swift declaration name")
                names.add(name.text.decode().strip("`"))
        todo.extend(node.named_children)
    return names


def changed_sources(paths, root, base):
    """Both revisions matter: a deleted extension can belong to a surviving class."""
    if not paths:
        raise ValueError("No verified PR paths")
    changes = defaultdict(list)
    for name in paths:
        path = Path(name)
        if len(path.parts) < 3 or path.parts[0] != "ios" or path.parts[1] not in SCHEMES:
            continue
        # Missing old blob is normal only if the path was absent from the base tree.
        entry = subprocess.check_output(["git", "ls-tree", "-z", base, "--", name], cwd=root)
        before = subprocess.check_output(["git", "show", f"{base}:{name}"], cwd=root) if entry else None
        source = root / path
        if source.is_symlink() or (source.exists() and not source.is_file()):
            raise ValueError(f"Not a regular test input: {path}")
        after = source.read_bytes() if source.exists() else None
        if before is None and after is None:
            raise ValueError(f"Missing both sides: {path}")
        # Parse revisions independently: one side's comment/string/conditional
        # must never change how the other side's declaration is interpreted.
        names = set()
        if path.suffix == ".swift":
            for revision in (before, after):
                if revision is not None:
                    try:
                        names.update(declared_classes(revision))
                    except ValueError:
                        names = None  # Unknown ownership on either side, even for a deletion.
                        break
        changes[path.parts[1]].append((names, after is not None, path))
    return changes


def enumeration(report, target):
    if report.get("errors") != [] or len(report.get("values", [])) != 1:
        raise ValueError("Invalid XCTest enumeration")
    (config,) = report["values"]

    def ids(key):
        result = set()
        for row in config[key]:
            identifier = row["identifier"].removesuffix("()")
            if identifier.split("/")[0] != target or len(identifier.split("/")) != 3:
                raise ValueError(f"Unexpected {target} test identity: {identifier}")
            result.add(identifier)
        return result

    enabled, disabled = ids("enabledTests"), ids("disabledTests")
    if not enabled or enabled & disabled:
        raise ValueError(f"Missing or ambiguous enumeration for {target}")
    return enabled, disabled


def selection(target, sources, enabled, disabled):
    required = {s for s in CONTRACTS if s.startswith(target + "/")}
    selectors = set(required)
    available = enabled | disabled
    classes = {s.split("/")[1] for s in available}
    broad = False
    for owners, exists, _ in sources:
        if owners is None:
            broad = True
            required.update(s for s in enabled if not s.startswith(MASTER_INTEROP + "/"))
            continue
        matches = classes & owners
        for cls in matches:
            prefix = f"{target}/{cls}"
            if prefix == MASTER_INTEROP:
                continue  # Named, explicitly reported exclusion: never a passing PR receipt.
            selectors.add(prefix)
            required.update(s for s in available if s.startswith(prefix + "/"))
        if not matches and exists:
            broad = True  # An unowned shared helper/resource: exercise this target.
    if not required <= enabled:
        raise ValueError(f"Required tests unavailable on this configuration: {sorted(required - enabled)}")
    if broad:
        return [target], required
    return sorted(s for s in selectors if not any(s.startswith(parent + "/") for parent in selectors)), required


def verify(report, target, required):
    """Target, test identity and outcome must all come from the same result bundle."""
    cases = defaultdict(list)

    def visit(node, bundle=None):
        kind = node["nodeType"]
        if kind in ("Unit test bundle", "UI test bundle"):
            bundle = node["name"]
        if kind == "Test Case" and bundle == target:
            identifier = node["nodeIdentifier"].removesuffix("()")
            cases[f"{target}/{identifier}"].append(node.get("result"))
        for child in node.get("children", []):
            visit(child, bundle)

    for node in report["testNodes"]:
        visit(node)
    if not any("Passed" in results for results in cases.values()):
        raise ValueError(f"No passing {target} cases in xcresult")
    missing = sorted(s for s in required if cases.get(s) != ["Passed"])
    if missing:
        raise ValueError(f"Missing, skipped, duplicate or failed required XCTest: {missing}")


def verify_membership(target, sources, derived):
    """An added but unregistered Swift file must not look like a helper-only change."""
    needed = {path.resolve() for _, exists, path in sources if exists and path.suffix == ".swift"}
    if not needed:
        return
    compiled = set()
    for listing in derived.glob(
        f"Build/Intermediates.noindex/**/*.build/Debug-iphonesimulator/{target}.build/Objects-normal/*/{target}.SwiftFileList"
    ):
        compiled.update(Path(line).resolve() for line in listing.read_text().splitlines())
    if needed - compiled:
        raise ValueError(f"{target}: touched sources missing from the compiler file list: {needed - compiled}")


def main():
    env = os.environ
    scope = classify(
        env["GITHUB_EVENT_NAME"], json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text()), env["GITHUB_SHA"], Path.cwd()
    )
    changes = changed_sources(scope.verified_paths, Path.cwd(), "HEAD^1")
    for target, scheme in SCHEMES.items():
        sources = changes.get(target, [])
        if not sources and target != "AvibeTests":
            continue
        temp = Path(env["RUNNER_TEMP"])
        result = temp / f"{target}.xcresult"
        common = [
            "xcodebuild",
            "-project",
            "ios/Avibe.xcodeproj",
            "-scheme",
            scheme,
            "-configuration",
            "Debug",
            "-destination",
            "platform=iOS Simulator,id=" + env["SIMULATOR_ID"],
            "-derivedDataPath",
            str(temp / "AvibeDerivedData"),
            "-parallel-testing-enabled",
            "NO",
            "CODE_SIGNING_ALLOWED=NO",
        ]
        subprocess.run([*common, "build-for-testing"], check=True)
        verify_membership(target, sources, temp / "AvibeDerivedData")
        catalog = temp / f"{target}-enumerated.json"
        subprocess.run(
            [
                *common,
                "test-without-building",
                "-enumerate-tests",
                "-test-enumeration-style",
                "flat",
                "-test-enumeration-format",
                "json",
                "-test-enumeration-output-path",
                str(catalog),
            ],
            check=True,
        )
        enabled, disabled = enumeration(json.loads(catalog.read_text()), target)
        selectors, required = selection(target, sources, enabled, disabled)
        if not selectors:  # All touched fixture classes were genuinely deleted.
            continue
        if target == "AvibeSheetFixtureTests":
            subprocess.run(
                [
                    "xcrun",
                    "simctl",
                    "privacy",
                    env["SIMULATOR_ID"],
                    "grant",
                    "microphone",
                    "bot.avibe.ios.sheet-fixture",
                ],
                check=True,
            )
        print(f"{scheme}: {selectors}", flush=True)
        skip = []
        if target == "AvibeTests":
            note = f"{MASTER_INTEROP}: not run here — separate merged-master qualification; this PR supplies no interop verdict."
            print(note, flush=True)
            with Path(env["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                print(note, file=summary)
            skip = ["-skip-testing:" + MASTER_INTEROP]
        with fixture_env(required, env, temp) as test_env:
            subprocess.run(
                [
                    *common,
                    "-resultBundlePath",
                    str(result),
                    *["-only-testing:" + s for s in selectors],
                    *skip,
                    "test-without-building",
                ],
                check=True,
                env=test_env,
            )
        tree = subprocess.check_output(["xcrun", "xcresulttool", "get", "test-results", "tests", "--path", str(result)])
        verify(json.loads(tree), target, required)


if __name__ == "__main__":
    main()
