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
        else:
            require((bundle / "Anchor.der").is_file(), f"{bundle.name}: missing Anchor.der")
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


def validate_lifecycle(out: Path) -> None:
    lifecycle_cases = ("lifecycle-reload", "lifecycle-recreate", "lifecycle-relaunch")
    events = [json.loads(line) for line in (out / "cert/fixture.jsonl").read_text().splitlines() if line.strip()]
    observations = []
    errors = []

    def clients(case: str, step: str) -> list[dict]:
        return [event for event in events if event.get("event") == "client"
                and event.get("case") == case and event.get("step") == step]

    def request_for(case: str, result: dict, route: str, path: str, after: int | None = None) -> dict:
        matches = [event for event in events if event.get("event") == "request"
                   and event.get("case") == case and event.get("id") == result.get("id")
                   and event.get("route") == route and event.get("path") == path
                   and event.get("connection") == result.get("connection")
                   and event.get("method") == "GET"
                   and (after is None or event.get("sequence", 0) > after)]
        require(matches, f"{case}: missing correlated {route} GET {path} request")
        return matches[-1] if after is not None else matches[0]

    def write_observations() -> None:
        (out / "lifecycle-observations.json").write_text(
            json.dumps(observations, indent=2, sort_keys=True)
        )

    for case in lifecycle_cases:
        try:
            initial_events = clients(case, "lifecycle_initial")
            result_events = clients(case, "lifecycle_result")
            done_events = clients(case, "lifecycle_done")
            require(len(initial_events) == 1, f"{case}: expected one lifecycle_initial event")
            require(len(result_events) == 1, f"{case}: expected one lifecycle_result event")
            require(len(done_events) == 1, f"{case}: expected one lifecycle_done event")
            initial_client = initial_events[0]
            result_client = result_events[0]
            route_a = initial_client["result"]
            route_a_page = route_a["page"]
            route_a_warm = route_a["warmFetch"]
            route_b = result_client["result"]["routeB"]
            route_b_page = route_b["page"]
            route_b_fetch = route_b["fetch"]
            require(route_a_page.get("route") == "origin" and route_a_page.get("protocol") == "1.1"
                    and route_a_page.get("proxyConnection") is None,
                    f"{case}: route A page was not an unproxied H1 origin request")
            require(route_a_warm.get("route") == "origin" and route_a_warm.get("protocol") == "1.1"
                    and route_a_warm.get("ok") is True
                    and route_a_page.get("connection") == route_a_warm.get("connection")
                    and route_a_page.get("tcpConnection") == route_a_warm.get("tcpConnection"),
                    f"{case}: route A warm GET did not reuse the page TLS/H1 identity")
            require(route_b_page.get("route") in ("origin", "proxy-backend")
                    and route_b_page.get("protocol") == "1.1"
                    and route_b_fetch.get("route") in ("origin", "proxy-backend")
                    and route_b_fetch.get("protocol") == "1.1"
                    and route_b_fetch.get("ok") is True,
                    f"{case}: lifecycle load did not produce correlated H1 results")

            boundary_step = "lifecycle_ready" if case == "lifecycle-relaunch" else "lifecycle_before_route_b"
            boundary_events = clients(case, boundary_step)
            require(len(boundary_events) == 1, f"{case}: expected one {boundary_step} event")
            boundary_sequence = boundary_events[0]["sequence"]
            for connection in {route_a_page.get("connection"), route_a_warm.get("connection")}:
                closes = [event for event in events if event.get("event") == "tls_close"
                          and event.get("connection") == connection
                          and event.get("sequence", 0) < boundary_sequence]
                require(not closes, f"{case}: route A connection closed before lifecycle boundary")

            initial_page_request = request_for(case, route_a_page, "origin", "/page")
            warm_request = request_for(case, route_a_warm, "origin", "/fetch")
            final_page_request = request_for(
                case, route_b_page, route_b_page["route"], "/page", after=boundary_sequence
            )
            final_fetch_request = request_for(
                case, route_b_fetch, route_b_fetch["route"], "/fetch", after=boundary_sequence
            )

            operation_connects = {}
            for name, result, request in (("page", route_b_page, final_page_request),
                                          ("fetch", route_b_fetch, final_fetch_request)):
                if result["route"] == "proxy-backend":
                    proxy_connection = result.get("proxyConnection")
                    require(isinstance(proxy_connection, str),
                            f"{case}: {name} proxy result lacks CONNECT identity")
                    connects = [event for event in events if event.get("event") == "proxy_connect"
                                and event.get("case") == case
                                and event.get("connection") == proxy_connection]
                    require(len(connects) == 1, f"{case}: {name} missing authoritative CONNECT identity")
                    require(request.get("proxyConnection") == proxy_connection,
                            f"{case}: {name} request/CONNECT identity mismatch")
                    operation_connects[name] = connects[0]
                else:
                    require(result["route"] == "origin" and result.get("proxyConnection") is None
                            and request.get("proxyConnection") is None,
                            f"{case}: {name} direct result lacks an unproxied identity")
                    operation_connects[name] = None

            page_uses_route_b = route_b_page["route"] == "proxy-backend"
            fetch_uses_route_b = route_b_fetch["route"] == "proxy-backend"
            observed_route_b = page_uses_route_b and fetch_uses_route_b
            result_payload = result_client["result"]
            require(result_payload.get("newTrafficUsesRouteB") is observed_route_b,
                    f"{case}: lifecycle Boolean disagrees with observed operations")
            require(result_payload.get("newTrafficOperationsUseRouteB") == {
                "page": page_uses_route_b, "fetch": fetch_uses_route_b
            }, f"{case}: per-operation route Boolean disagrees with observed routes")
            outcome = ("all-route-b" if observed_route_b else
                       "none-route-b" if not page_uses_route_b and not fetch_uses_route_b else "mixed")
            require(result_payload.get("newTrafficRouteOutcome") == outcome,
                    f"{case}: lifecycle route outcome disagrees with observed operations")

            if case == "lifecycle-relaunch":
                require(initial_client.get("phase") == "prepare" and result_client.get("phase") == "resume",
                        f"{case}: missing prepare/resume lifecycle phases")
                require(initial_client.get("appPID") != result_client.get("appPID"),
                        f"{case}: app PID did not change across kill/relaunch")
            else:
                require(initial_client.get("phase") == "single" and result_client.get("phase") == "single",
                        f"{case}: unexpected lifecycle phase")
            observations.append({
                "case": case,
                "initial": route_a,
                "initialPageRequest": initial_page_request,
                "warmRequest": warm_request,
                "routeB": route_b,
                "routeBPageRequest": final_page_request,
                "routeBFetchRequest": final_fetch_request,
                "connect": operation_connects,
                "newTrafficUsesRouteB": observed_route_b,
                "newTrafficOperationsUseRouteB": {
                    "page": page_uses_route_b, "fetch": fetch_uses_route_b
                },
                "newTrafficRouteOutcome": outcome,
                "streamSurvival": "not claimed",
                "replaySafety": "unmeasured: GET only",
            })
        except (KeyError, StopIteration, ValueError) as error:
            observations.append({"case": case, "verdict": "unmeasured", "error": str(error)})
            errors.append(f"{case}: {error}")
        write_observations()

    require(not errors, "lifecycle evidence incomplete: " + "; ".join(errors))
    print(json.dumps({"stage": "lifecycle", "cases": len(observations),
                      "authoritativeRouteBObservations": len(observations),
                      "negativeObservations": sum(not item.get("newTrafficUsesRouteB", False)
                                                   for item in observations)}))


def validate_evidence(stage: str, out: Path, matrix_path: Path) -> None:
    if stage == "lifecycle":
        validate_lifecycle(out)
        return
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
