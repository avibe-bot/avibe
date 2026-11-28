"""Run the fixed iOS PR contracts and the compiled XCTest classes a PR touches."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from ios.scripts.xctest_stall_retry import record_rerun, stalled_failures
from scripts.ci_docs_only import git
from scripts.ci_fast_pr import native_path_receipt

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


@dataclass
class Owners:
    classes: set[str] = field(default_factory=set)
    extensions: set[str] = field(default_factory=set)


def declared_classes(source):
    """Parse Swift syntax, including declarations inside conditional branches."""
    from tree_sitter import Language, Parser
    import tree_sitter_swift

    tree = Parser(Language(tree_sitter_swift.language())).parse(source)
    owners = Owners()
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
                    parts = name.named_children
                    if not parts or any(part.type != "type_identifier" for part in parts) or name.has_error:
                        raise ValueError("Cannot establish extended Swift type")
                    owners.extensions.add(".".join(part.text.decode().strip("`") for part in parts))
                elif name and name.type == "type_identifier" and not name.has_error:
                    (owners.extensions if kind.type == "extension" else owners.classes).add(
                        name.text.decode().strip("`")
                    )
                else:
                    raise ValueError("Cannot establish Swift declaration name")
        todo.extend(node.named_children)
    return owners


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
        target = changes[path.parts[1]]
        if path.suffix != ".swift":
            target.append((Owners(), after is not None, path))
            continue
        for revision, exists in ((before, False), (after, True)):
            if revision is not None:
                try:
                    owners = declared_classes(revision)
                except ValueError:
                    owners = None  # Unknown on either side, even for a deletion.
                target.append((owners, exists, path))
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


# Only diagnostics from this enumeration process (or its own errors array),
# never test identifiers, earlier build output or later execution failures.
ENUM_HOST_DEATH = re.compile(
    r"Mach error -308 - \(ipc/mig\) server died|"
    r"\b(?:test runner(?: process)?|test host(?: process)?) (?:crashed|died|exited unexpectedly)\b|"
    r"\blost connection to (?:the )?test (?:runner|host)\b",
    re.I,
)


# Match explicit fatal records, not words like "Finished with error" in the
# launch report above. Apply the same cause policy to these and catalog.errors.
ENUM_FATAL_LINE = re.compile(r"^\s*(?:xcodebuild:\s*)?(?:fatal\s+)?error:\s*(.*)$", re.I)


def enumerate_tests(common, target, temp):
    for attempt in (1, 2):
        with tempfile.TemporaryDirectory(dir=temp) as directory:
            catalog = Path(directory) / "catalog.json"
            cmd = [
                *common,
                "test-without-building",
                "-enumerate-tests",
                "-test-enumeration-style",
                "flat",
                "-test-enumeration-format",
                "json",
                "-test-enumeration-output-path",
                str(catalog),
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
            print(proc.stdout, end="", flush=True)
            # A catalog with entries must stand on its own. Never turn an
            # identity/schema/configuration failure into an infrastructure retry.
            report = json.loads(catalog.read_text()) if catalog.is_file() else None
            errors = report.get("errors") if isinstance(report, dict) else None
            if errors:
                print(
                    f"{target} enumeration attempt {attempt}: catalog errors: {json.dumps(errors)[:1200]}", flush=True
                )
            if catalog.is_file() and not (isinstance(report, dict) and isinstance(errors, list)):
                raise ValueError(f"{target} enumeration attempt {attempt}: malformed catalog")
            values = report.get("values", []) if report is not None else []
            if values != []:
                result = enumeration(report, target)
                proc.check_returncode()
                return result

            # No catalog, or explicitly no values: only known discovery errors
            # are compatible with a host crash. A separate error vetoes recovery.
            diagnostic = proc.stdout
            fatal_messages = [match[1] for line in proc.stdout.splitlines() if (match := ENUM_FATAL_LINE.match(line))]
            for error in [*(errors or []), *fatal_messages]:
                if not isinstance(error, str) or not (
                    ENUM_HOST_DEATH.fullmatch(error.strip().rstrip(".")) or error == "Unable to enumerate"
                ):
                    raise ValueError(
                        f"{target} enumeration attempt {attempt}: independent or unknown fatal diagnostic: {str(error)[:300]}"
                    )
                diagnostic += "\n" + error
            signature = ENUM_HOST_DEATH.search(diagnostic)
            if attempt == 2 or not signature:
                proc.check_returncode()
                raise ValueError(f"{target} enumeration attempt {attempt}: no valid catalog")
            note = f'{target}: enumeration host failure ({signature[0]}); retrying enumeration once before tests.'
            print(note, flush=True)
            with (temp / 'native-enumeration-retry.txt').open('a') as evidence:
                print(note, file=evidence)


def selection(target, sources, enabled, disabled):
    required = {s for s in CONTRACTS if s.startswith(target + "/")}
    selectors = set(required)
    available = enabled | disabled
    classes = {s.split("/")[1] for s in available}
    broad = False
    for owners, exists, _ in sources:
        # A class declaration may be an ordinary helper. An extension receiver
        # on the other hand can be a typealias for a different compiled suite,
        # even when another owner in this very file matched the catalog.
        extensions = {name.removeprefix(target + ".") for name in owners.extensions} if owners is not None else set()
        if owners is None or extensions - classes:
            broad = True
            required.update(s for s in available if not s.startswith(MASTER_INTEROP + "/"))
            continue
        matches = (classes & owners.classes) | extensions
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


def verify(report, target, required, *, stalled=frozenset(), isolated=False):
    """Target, test identity and outcome must all come from the same result bundle."""
    cases = defaultdict(list)

    def visit(node, bundle=None):
        kind = node["nodeType"]
        if kind in ("Unit test bundle", "UI test bundle"):
            bundle = node["name"]
        if kind == "Test Case" and (isolated or stalled) and bundle != target:
            raise ValueError(f"Unexpected XCTest bundle in stall recovery: {bundle}")
        if kind == "Test Case" and bundle == target:
            identifier = node["nodeIdentifier"].removesuffix("()")
            cases[f"{target}/{identifier}"].append(node.get("result"))
        for child in node.get("children", []):
            visit(child, bundle)

    for node in report["testNodes"]:
        visit(node)
    if isolated and set(cases) != required:
        raise ValueError(f"Isolated rerun ran unexpected cases: {sorted(cases)}")
    if stalled:
        # A log can nominate a retry, never excuse an absent receipt or a
        # second failure. Broad selections can include unrelated opt-in skips.
        other = {s for s, outcomes in cases.items() if outcomes not in (["Passed"], ["Skipped"])}
        if other != stalled or any(cases[s] != ["Failed"] for s in stalled):
            raise ValueError(f"xcresult failures do not match the simulator stall: {sorted(other)}")
    elif not any("Passed" in results for results in cases.values()):
        raise ValueError(f"No passing {target} cases in xcresult")
    missing = sorted(s for s in required - stalled if cases.get(s) != ["Passed"])
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


def verified_merge_paths(root, expected):
    """Recompute the Linux classifier's exact merge diff without replaying the event on Mac."""
    merge = git(root, 'rev-parse', 'HEAD').decode().strip()
    parents = git(root, 'show', '-s', '--format=%P', merge).decode().split()
    if len(parents) != 2:
        raise ValueError('Native PR checkout is not a two-parent merge')
    base = parents[0]
    raw = git(root, 'diff', '--name-only', '-z', '--no-renames', '--no-ext-diff', '--no-textconv',
              '--ignore-submodules=none', base, merge, '--')
    if not raw.endswith(b'\0'):
        raise ValueError('Empty or incomplete native PR diff')
    paths = tuple(raw[:-1].decode().split('\0'))
    if native_path_receipt(merge, paths) != expected:
        raise ValueError('Native PR checkout or paths do not match the changes job')
    return base, paths


def execute_tests(common, target, selectors, required, skip, env, temp, remaining):
    def command(result, only):
        return [*common, "-resultBundlePath", str(result), *["-only-testing:" + s for s in only],
                *skip, "test-without-building"]

    def report(result):
        return json.loads(subprocess.check_output(
            ["xcrun", "xcresulttool", "get", "test-results", "tests", "--path", str(result)]
        ))

    result = temp / f"{target}.xcresult"
    log = temp / f"{target}.log"
    # Keep live Actions output and a log of this execution only (not discovery).
    with subprocess.Popen(["tee", str(log)], stdin=subprocess.PIPE) as tee:
        first = subprocess.run(command(result, selectors), env=env, stdout=tee.stdin, stderr=subprocess.STDOUT)
        tee.stdin.close()
        if tee.wait():
            raise RuntimeError(f"Could not save {target} execution log")
    if first.returncode == 0:
        verify(report(result), target, required)
        return 0
    if first.returncode != 65:
        first.check_returncode()
    failed, reason = stalled_failures(log.read_text(errors="replace").splitlines(), remaining, selected=True)
    if reason:
        raise ValueError(f"Not rerun: {reason}")
    methods = {f"{case.target}/{case.cls}/{case.method}" for case in failed}
    if len(methods) != len(failed) or any(
        s.startswith(MASTER_INTEROP + "/") or not any(s == sel or s.startswith(sel + "/") for sel in selectors)
        for s in methods
    ):
        raise ValueError("Duplicate or unselected stall candidate")
    verify(report(result), target, required, stalled=methods)
    for index, case in enumerate(failed, 1):
        method = f"{case.target}/{case.cls}/{case.method}"
        rerun = temp / f"{target}Rerun{index}.xcresult"
        record_rerun([case], temp / "ios-simulator-stall.txt")
        subprocess.run(command(rerun, [method]), check=True, env=env)
        verify(report(rerun), target, {method}, isolated=True)
    return len(methods)


PROJECT = 'ios/Avibe.xcodeproj/project.pbxproj'
FIXTURE = 'AvibeSheetFixture'


def fixture_members(project):
    """Resolve the fixture application's Compile Sources through the project group tree."""
    objects = project['objects']
    root = objects[project['rootObject']]
    target, = (objects[t] for t in root['targets'] if objects[t].get('name') == FIXTURE)
    if root.get('projectDirPath') or target.get('fileSystemSynchronizedGroups'):
        raise ValueError('Non-explicit project membership')
    phase, = (objects[p] for p in target['buildPhases'] if objects[p]['isa'] == 'PBXSourcesBuildPhase')
    parents = defaultdict(list)
    for key, obj in objects.items():
        if obj['isa'] == 'PBXGroup':
            for child in obj['children']:
                parents[child].append(key)

    members = set()
    for entry in phase['files']:
        ref = objects[entry]['fileRef']
        if objects[ref]['isa'] != 'PBXFileReference' or not objects[ref]['path']:
            raise ValueError('Unknown fixture source reference')
        parts = []
        seen = set()
        while True:
            obj = objects[ref]
            part = obj.get('path', '')
            tree = obj['sourceTree']
            if ref in seen or tree not in ('<group>', 'SOURCE_ROOT') or '$' in part or Path(part).is_absolute():
                raise ValueError('Unknown fixture source location')
            parts.append(part)
            seen.add(ref)
            if tree == 'SOURCE_ROOT' or ref == root['mainGroup']:
                break
            ref, = parents[ref]
        path = Path(os.path.normpath(os.path.join('ios', *reversed(parts))))
        if not path.is_file() or path.resolve() != Path.cwd() / path:
            raise ValueError(f'Missing or indirect fixture source: {path}')
        members.add(path.as_posix())
    return members


def read_fixture_members():
    try:
        project = json.loads(subprocess.check_output(['/usr/bin/plutil', '-convert', 'json', '-o', '-', PROJECT]))
        return fixture_members(project)
    except (OSError, subprocess.CalledProcessError, KeyError, TypeError, ValueError, AttributeError) as exc:
        print(f'Cannot establish fixture source membership; building {FIXTURE}: {exc}', flush=True)
        return None  # Unknown, never an empty inventory.


def needs_fixture_build(paths):
    # A project edit can remove or move an old member; never rely on the new
    # inventory alone. --no-renames in verified_merge_paths includes both names.
    if PROJECT in paths or 'scripts/ci_native_pr_tests.py' in paths or any(
        p.startswith('ios/Avibe.xcodeproj/xcshareddata/xcschemes/') for p in paths
    ):
        return True
    members = read_fixture_members()
    return members is None or not members.isdisjoint(paths)


def main():
    env = os.environ
    base, paths = verified_merge_paths(Path.cwd(), env['NATIVE_PATH_RECEIPT'])
    changes = changed_sources(paths, Path.cwd(), base)
    remaining = 3  # Per PR simulator job, not per scheme.
    for target, scheme in SCHEMES.items():
        sources = changes.get(target, [])
        compile_only = target == 'AvibeSheetFixtureTests' and not sources
        if compile_only and not needs_fixture_build(paths):
            continue
        temp = Path(env["RUNNER_TEMP"])
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
        if compile_only:
            print(f'{scheme}: compiled; no fixture test change selected for execution.', flush=True)
            continue
        verify_membership(target, sources, temp / "AvibeDerivedData")
        enabled, disabled = enumerate_tests(common, target, temp)
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
            remaining -= execute_tests(common, target, selectors, required, skip, test_env, temp, remaining)


if __name__ == "__main__":
    main()
