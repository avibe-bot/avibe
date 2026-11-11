"""Validate built probe products and positive execution; bound CI subprocesses."""

import json
import os
import plistlib
import re
import signal
import subprocess
import sys
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_bundle(app: Path, settings_path: Path) -> None:
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

    settings = json.loads(settings_path.read_text())
    tests = next(row["buildSettings"] for row in settings if row["target"] == "WebKitProxyChangeTests")
    host_info = plistlib.loads((app / "Info.plist").read_bytes())
    for key in ("TEST_HOST", "BUNDLE_LOADER"):
        require(Path(tests[key]).resolve() == app / host_info["CFBundleExecutable"],
                f"{key} does not resolve to the built host executable")
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
    log = (out / f"{stage}.log").read_text()
    counts = re.findall(r"Executed (\d+) tests?, with (\d+) failures?", log)
    require(bool(counts) and all(count == "1" and failures == "0" for count, failures in counts),
            f"{stage}: expected exactly one passing XCTest, counts={counts}")
    require(any("Test Case" in line and methods[stage] in line and "passed" in line for line in log.splitlines()),
            f"{stage}: expected test method did not pass")
    if stage == "test-launch":
        return
    events = [json.loads(line) for line in (out / "cert/fixture.jsonl").read_text().splitlines()]
    cases = json.loads(matrix_path.read_text())
    if stage == "full-matrix":
        for case in cases:
            require(any(e.get("event") == "client" and e.get("case") == case["id"]
                        and e.get("step") == "case_complete" for e in events),
                    f"{case['id']}: no completed case evidence")
    else:
        direct = stage == "direct-minimal"
        action = "coldBeforeFirstRequest" if direct else "coldBeforeWebView"
        case = next(c for c in cases if c["action"] == action and c["origin"] == "h1")
        case_id = case["id"]
        route = "origin" if direct else "proxy-backend"
        suffixes = ("direct-minimal", "direct-minimal-fetch") if direct else ("minimal", "minimal-fetch")
        for suffix in suffixes:
            require(any(e.get("event") == "request" and e.get("id") == f"{case_id}-{suffix}"
                        and e.get("route") == route and e.get("protocol") == "1.1" for e in events),
                    f"{stage}: missing authoritative {route} request {case_id}-{suffix}")
        if not direct:
            require(any(e.get("event") == "proxy_connect" and e.get("case") == case_id for e in events),
                    f"{stage}: missing CONNECT evidence")
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
