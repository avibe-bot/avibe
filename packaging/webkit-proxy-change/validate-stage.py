"""Validate built probe products and positive execution; bound CI subprocesses."""

import json
import os
import plistlib
import re
import shlex
import signal
import subprocess
import sys
from pathlib import Path

from evidence import analyze_matrix, inventory, operation


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_bundle(app: Path, build_log: Path) -> None:
    app = app.resolve()
    test = app / "PlugIns/WebKitProxyChangeTests.xctest"
    for bundle, identifier, package in (
        (app, "com.avibe.webkit-probe", "APPL"),
        (test, "com.avibe.webkit-probe.tests", "BNDL"),
    ):
        info = plistlib.loads((bundle / "Info.plist").read_bytes())
        executable = info.get("CFBundleExecutable")
        require(
            isinstance(executable, str) and bool(executable)
            and "$" not in executable and "/" not in executable,
            f"{bundle.name}: missing/invalid CFBundleExecutable",
        )
        require((bundle / executable).is_file(), f"{bundle.name}: executable missing")
        require(os.access(bundle / executable, os.X_OK), f"{bundle.name}: binary not executable")
        require(info.get("CFBundleIdentifier") == identifier, f"{bundle.name}: invalid identifier")
        require(info.get("CFBundlePackageType") == package, f"{bundle.name}: invalid package type")
        for key in ("CFBundleVersion", "CFBundleShortVersionString"):
            value = info.get(key)
            require(isinstance(value, str) and bool(value) and "$" not in value,
                    f"{bundle.name}: missing/invalid {key}")
        if bundle == test:
            for name in ("Anchor.der", "matrix.json"):
                require((bundle / name).is_file(), f"{bundle.name}: missing {name}")
        print(json.dumps({"bundle": bundle.name, "executable": executable, "valid": True}))

    # Use the command that actually linked this bundle, not a new Xcode
    # settings query. Paths are rooted at the recorded app so downloaded CI
    # artifacts can be checked without recreating the runner's directory.
    host_info = plistlib.loads((app / "Info.plist").read_bytes())
    test_info = plistlib.loads((test / "Info.plist").read_bytes())
    expected_relative_output = Path("PlugIns/WebKitProxyChangeTests.xctest") / test_info["CFBundleExecutable"]
    links = [shlex.split(line) for line in build_log.read_text().splitlines() if " -bundle_loader " in line]
    require(len(links) == 1, f"expected one XCTest link command, found {len(links)}")
    command = links[0]
    output = Path(command[command.index("-o") + 1])
    recorded_app = output.parents[2]
    require(recorded_app.name == app.name and output.relative_to(recorded_app) == expected_relative_output,
            "link command does not produce the built XCTest bundle")
    loader = Path(command[command.index("-bundle_loader") + 1])
    require(loader == recorded_app / host_info["CFBundleExecutable"],
            "BUNDLE_LOADER does not resolve to the built host executable")
    products = app.parent.parent
    runs = list(products.glob("*.xctestrun"))
    require(len(runs) == 1, f"expected one xctestrun, found {len(runs)}")
    run = plistlib.loads(runs[0].read_bytes())
    target = run["WebKitProxyChangeTests"]

    def resolve(value: str) -> Path:
        return Path(value.replace("__TESTROOT__", str(products)).replace("__TESTHOST__", str(app))).resolve()

    require(resolve(target["TestHostPath"]) == app, "xctestrun TestHostPath mismatch")
    require(resolve(target["TestBundlePath"]) == test, "xctestrun TestBundlePath mismatch")
    require(target.get("IsAppHostedTestBundle") is True, "xctestrun is not app-hosted")
    require(target.get("TestHostBundleIdentifier") == host_info["CFBundleIdentifier"], "host identifier mismatch")
    dependencies = {resolve(p) for p in target["DependentProductPaths"]}
    require({app, test}.issubset(dependencies), "missing xctestrun host/test dependency")
    require(all(p.exists() for p in dependencies), "missing xctestrun dependent product")
    print(json.dumps({"xctestrun": runs[0].name, "hostAndLoaderPathsValid": True}))


def validate_evidence(stage: str, out: Path, matrix_path: Path) -> None:
    methods = {"test-launch": "testHostLaunch", "direct-minimal": "testDirectTLSMinimal",
               "cold-minimal": "testColdMinimal", "full-matrix": "testMatrix"}
    cases = json.loads(matrix_path.read_text())
    inventory(cases)
    events = [json.loads(line) for line in (out / "cert/fixture.jsonl").read_text().splitlines()]
    if stage == "full-matrix":
        report = analyze_matrix(cases, events)
        (out / "matrix-observations.json").write_text(json.dumps(report, indent=2, sort_keys=True))
        require(report["observedCases"] == report["expectedCases"],
                f"matrix coverage: {report['observedCases']}/{report['expectedCases']} observed; see matrix-observations.json")
    log = (out / f"{stage}.log").read_text()
    counts = re.findall(r"Executed (\d+) tests?, with (\d+) failures?", log)
    require(bool(counts) and all(count == "1" and failures == "0" for count, failures in counts),
            f"{stage}: expected exactly one passing XCTest, counts={counts}")
    require(any("Test Case" in line and methods[stage] in line and "passed" in line for line in log.splitlines()),
            f"{stage}: expected test method did not pass")
    if stage == "test-launch":
        return
    if stage != "full-matrix":
        direct = stage == "direct-minimal"
        action = "coldBeforeFirstRequest" if direct else "coldBeforeWebView"
        case = next(c for c in cases if c["action"] == action and c["origin"] == "h1")
        route = "origin-direct" if direct else "proxy-backend"
        steps = (("direct-minimal-page", "direct-minimal", "/page"), ("direct-minimal-fetch", "direct-minimal-fetch", "/fetch")) if direct else (
            ("minimal-page", "minimal", "/page"), ("minimal-fetch", "minimal-fetch", "/fetch"))
        observations = [operation(case, step, suffix, path, events) for step, suffix, path in steps]
        (out / f"{stage}-observations.json").write_text(json.dumps(observations, indent=2, sort_keys=True))
        require(all(o["outcome"] == "routed" and o["route"]["path"] == route and o["route"]["protocol"] == "1.1" for o in observations),
                f"{stage}: missing correlated client/server/TLS/CONNECT route evidence")
    print(json.dumps({"stage": stage, "expectedTestCount": 1, "positiveExecutionEvidence": True}))


def bounded(seconds: float, command: list[str]) -> int:
    # Only terminate the process group created for this CI command.
    process = subprocess.Popen(command, start_new_session=True)
    try:
        return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        print(f"UNMEASURED: command exceeded {seconds}s: {command[0]}", file=sys.stderr)
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        return 124


if __name__ == "__main__":
    try:
        if sys.argv[1] == "timeout":
            raise SystemExit(bounded(float(sys.argv[2]), sys.argv[3:]))
        if sys.argv[1] == "bundle":
            validate_bundle(Path(sys.argv[2]), Path(sys.argv[3]))
        else:
            validate_evidence(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
    except (ValueError, OSError, StopIteration, KeyError) as error:
        print(f"UNMEASURED: {error}", file=sys.stderr)
        raise SystemExit(66) from error
