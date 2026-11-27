"""Run the fixed iOS PR contracts and the compiled XCTest classes a PR touches."""

from __future__ import annotations

from collections import defaultdict
import json
import os
from pathlib import Path
import re
import subprocess

from scripts.ci_docs_only import classify

SCHEMES = {"AvibeTests": "Avibe", "AvibeSheetFixtureTests": "AvibeSheetFixture"}
CONTRACTS = {
    "AvibeTests/NativeBadgeTests/testBadgeSetTakesExactlyOneCountNumber",
    "AvibeTests/NavigationPolicyTests/testConfiguredOriginRequiresHTTPSAndCanonicalizesToOrigin",
    "AvibeTests/SystemScheduleTests/testSharedContractVectorsCoverSwiftParsersAndEveryTerminalReceipt",
}


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
        # Identifiers in inactive #if branches and qualified extensions are deliberately
        # included. Only real class names from the built test target can match them.
        names = (
            set(re.findall(r"\w+", b"\n".join(x for x in (before, after) if x is not None).decode()))
            if path.suffix == ".swift"
            else set()
        )
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
    for words, exists, _ in sources:
        matches = classes & words
        for cls in matches:
            prefix = f"{target}/{cls}"
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
        subprocess.run(
            [
                *common,
                "-resultBundlePath",
                str(result),
                *["-only-testing:" + s for s in selectors],
                "test-without-building",
            ],
            check=True,
        )
        tree = subprocess.check_output(["xcrun", "xcresulttool", "get", "test-results", "tests", "--path", str(result)])
        verify(json.loads(tree), target, required)


if __name__ == "__main__":
    main()
