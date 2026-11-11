"""Non-Apple synthetic evidence regressions; never launch listeners or simulators."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from evidence import IDENTITY, analyze_case, analyze_matrix, inventory

CASES = json.loads(Path(__file__).with_name("matrix.json").read_text())
_spec = importlib.util.spec_from_file_location("stage_validator", Path(__file__).with_name("validate-stage.py"))
STAGES = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(STAGES)


class Trace:
    def __init__(self, case_id):
        self.case = next(c for c in CASES if c["id"] == case_id)
        self.events = []
        self.connections = set()
        self.connects = set()
        self.client("case_start", {"action": self.case["action"]})

    def emit(self, event, **fields):
        row = {"sequence": len(self.events) + 1, "event": event, **fields}
        self.events.append(row)
        return row

    def client(self, step, result):
        return self.emit("client", case=self.case["id"], step=step, result=result)

    def setter(self, mode, failover=False):
        self.client("setter", {"mode": mode, "allowFailover": failover})

    def request(self, suffix, path, connection="o1", route="origin", protocol="1.1", proxy=None, stream=None):
        if proxy and proxy not in self.connects:
            self.emit("proxy_connect", connection=proxy, case=self.case["id"])
            self.emit("proxy_upstream_connected", connection=proxy)
            self.connects.add(proxy)
        tcp = "tcp-" + connection
        host = self.case["id"] + ".webkit-proxy.test"
        if tcp not in self.connections:
            self.emit("tls_open", connection=tcp, sni=host)
            self.connections.add(tcp)
        payload = dict(id=self.case["id"] + "-" + suffix, case=self.case["id"], host=host,
                       path=path, method="GET", connection=connection, tcpConnection=tcp,
                       proxyConnection=proxy, stream=stream, route=route, protocol=protocol)
        self.emit("websocket" if path == "/ws" else "request", **payload)
        if path == "/events":
            self.emit("sse_tick", **payload, tick=1)
            payload["tick"] = 1
        elif path != "/ws":
            self.emit("response_finished", **payload)
        return payload

    def op(self, label, suffix=None, path="/fetch", **kwargs):
        payload = self.request(suffix or label, path, **kwargs)
        self.client(label, {"ok": True, **payload})
        return payload

    def fail(self, label, error="TypeError: Load failed"):
        self.client(label, {"ok": False, "id": self.case["id"] + "-" + label, "error": error})

    def warm(self, proxy=False, h2=False):
        args = dict(connection="p1" if proxy else "o1", route="proxy-backend" if proxy else "origin",
                    proxy="connect-1" if proxy else None, protocol="2.0" if h2 else "1.1")
        for label, suffix, path in (("page", "navigation", "/page"), ("warm-1", "warm-1", "/fetch"), ("warm-2", "warm-2", "/fetch")):
            self.op(label, suffix, path, stream=1 if h2 else None, **args)

    def finish(self):
        self.client("case_complete", {"executionComplete": True})
        return self

    def report(self):
        return analyze_case(self.case, self.events)

    def remove(self, event, step=None):
        self.events = [e for e in self.events if not (e["event"] == event and (step is None or e.get("step") == step))]


def idle(failed=False, proxy_after=False):
    trace = Trace("idle-h1")
    trace.warm()
    trace.setter("direct")
    for label in ("after-immediate", "after-settled"):
        if failed:
            trace.fail(label)
        else:
            trace.op(label, connection="p2" if proxy_after else "o1", route="proxy-backend" if proxy_after else "origin", proxy="connect-2" if proxy_after else None)
    return trace.finish()


def failure(kind="refuse", allow=True, failed=False):
    trace = Trace(f"failover-{kind}-{'true' if allow else 'false'}-h1")
    trace.op("failure-page", path="/page")
    trace.emit("origin_forced_close", host=trace.case["id"] + ".webkit-proxy.test", closed=1, connections=["tcp-o1"])
    if kind == "absent":
        trace.emit("absent_listener_check", case=trace.case["id"], host=trace.case["id"] + ".webkit-proxy.test", port=18081, state="refused")
    trace.setter(kind, allow)
    if kind != "absent":
        trace.emit(f"proxy_{kind}_accept", connection="failure-1")
        trace.emit("proxy_refused" if kind == "refuse" else "proxy_stall_connect", case=trace.case["id"], connection="failure-1",
                   host=trace.case["id"] + ".webkit-proxy.test", port=18082 if kind == "refuse" else 18083)
    if failed:
        trace.fail("failure-fetch", "AbortError: bounded timeout")
    else:
        trace.op("failure-fetch", connection="o2")
    return trace.finish()


def snapshot(first, last, original_count=1):
    old_key = f"{first['connection']}:{first['stream']}"
    new_key = f"{last['connection']}:{last['stream']}"
    return {"ok": True, "first": first, "last": [last], "count": original_count + (new_key != old_key),
            "originalCount": original_count, "identities": {old_key: original_count, **({new_key: 1} if new_key != old_key else {})}}


def constant(close=False, reconnect=False):
    trace = Trace("constant-proxy-h2" if close else "constant-proxy-surviving-h2")
    trace.warm(proxy=True, h2=True)
    first = trace.op("old-sse-first", "old-sse", "/events", connection="p1", route="proxy-backend", protocol="2.0", proxy="connect-1", stream=7)
    trace.client("old-sse-before", snapshot(first, first))
    trace.emit("proxy_mode", host=trace.case["id"] + ".webkit-proxy.test", mode="origin")
    if close:
        trace.emit("proxy_forced_close", host=trace.case["id"] + ".webkit-proxy.test", closed=1, connections=["connect-1"])
        trace.emit("response_closed", **first)
    for label in ("after-immediate", "after-settled"):
        trace.op(label, connection="o2", route="origin", protocol="2.0", proxy="connect-2", stream=9)
    last = first
    if reconnect:
        last = trace.request("old-sse", "/events", connection="o2", route="origin", protocol="2.0", proxy="connect-2", stream=11)
    elif not close:
        last = {**first, "tick": 2}
        trace.emit("sse_tick", **last)
    trace.client("old-sse-after", snapshot(first, last, 2 if not close and not reconnect else 1))
    trace.op("rebuilt-sse", "new-sse", "/events", connection="o2", route="origin", protocol="2.0", proxy="connect-2", stream=13)
    trace.op("new-websocket", path="/ws", connection="o3", route="origin", proxy="connect-3")
    return trace.finish()


def live(post_mutation_tick):
    trace = Trace("sse-h2")
    trace.warm(h2=True)
    first = trace.op("old-sse-first", "old-sse", "/events", protocol="2.0", stream=7)
    trace.client("old-sse-before", snapshot(first, first))
    latest = {**first, "tick": 2}
    if not post_mutation_tick:
        trace.emit("sse_tick", **latest)
    trace.setter("direct")
    trace.op("after-immediate", protocol="2.0", stream=9)
    trace.op("after-settled", protocol="2.0", stream=11)
    if post_mutation_tick:
        trace.emit("sse_tick", **latest)
    trace.client("old-sse-after", snapshot(first, latest, 2))
    trace.op("rebuilt-sse", path="/events", protocol="2.0", stream=13)
    trace.op("new-websocket", path="/ws", connection="o2")
    return trace.finish()


class EvidenceTests(unittest.TestCase):
    def test_live_stream_pre_setter_tick_is_not_survival(self):
        report = live(post_mutation_tick=False).report()
        self.assertEqual(report["verdict"], "observed", report)
        self.assertTrue(report["oldStream"]["originalProgressed"])
        self.assertFalse(report["oldStream"]["originalSurvivedThroughSample"])

    def test_live_stream_post_setter_tick_proves_sample_survival(self):
        report = live(post_mutation_tick=True).report()
        self.assertEqual(report["verdict"], "observed", report)
        self.assertTrue(report["oldStream"]["originalSurvivedThroughSample"])

    def test_live_setter_must_be_bracketed_by_stream_snapshots(self):
        trace = live(post_mutation_tick=True)
        before = next(e["sequence"] for e in trace.events if e.get("step") == "old-sse-before")
        next(e for e in trace.events if e.get("step") == "setter")["sequence"] = before - 0.5
        report = trace.report()
        self.assertEqual(report["verdict"], "unmeasured", report)
        self.assertIn("not bracketed", report["reasons"][0])

    def test_minimal_gates_correlate_each_response(self):
        for direct in (True, False):
            trace = Trace("cold-before-first-h1" if direct else "cold-before-webview-h1")
            stage = "direct-minimal" if direct else "cold-minimal"
            method = "testDirectTLSMinimal" if direct else "testColdMinimal"
            args = {} if direct else dict(connection="p1", route="proxy-backend", proxy="connect-1")
            trace.op("direct-minimal-page" if direct else "minimal-page", "direct-minimal" if direct else "minimal", "/page", **args)
            trace.op("direct-minimal-fetch" if direct else "minimal-fetch", **args)
            with tempfile.TemporaryDirectory() as temp:
                out = Path(temp)
                (out / "cert").mkdir()
                events_path = out / "cert/fixture.jsonl"
                events_path.write_text("".join(json.dumps(e) + "\n" for e in trace.events))
                log = out / f"{stage}.log"
                log.write_text(f"Test Case '-[WebKitProxyChangeTests {method}]' passed.\nExecuted 1 test, with 0 failures\n")
                STAGES.validate_evidence(stage, out, Path(__file__).with_name("matrix.json"))
                log.write_text(f"Test Case '-[WebKitProxyChangeTests {method}]' passed.\nExecuted 0 tests, with 0 failures\n")
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    STAGES.validate_evidence(stage, out, Path(__file__).with_name("matrix.json"))
                log.write_text(f"Test Case '-[WebKitProxyChangeTests {method}]' passed.\nExecuted 1 test, with 0 failures\n")
                trace.remove("response_finished" if direct else "proxy_connect")
                events_path.write_text("".join(json.dumps(e) + "\n" for e in trace.events))
                with self.assertRaisesRegex(ValueError, "correlated"):
                    STAGES.validate_evidence(stage, out, Path(__file__).with_name("matrix.json"))

    def test_full_gate_writes_unmeasured_report_before_rejecting(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            (out / "cert").mkdir()
            (out / "cert/fixture.jsonl").write_text("")
            (out / "full-matrix.log").write_text("Test Case '-[WebKitProxyChangeTests testMatrix]' passed.\nExecuted 1 test, with 0 failures\n")
            with self.assertRaisesRegex(ValueError, "matrix coverage"):
                STAGES.validate_evidence("full-matrix", out, Path(__file__).with_name("matrix.json"))
            report = json.loads((out / "matrix-observations.json").read_text())
            self.assertEqual(report["observedCases"], 0)
            self.assertFalse(report["transportQualified"])
            self.assertIn("GET", report["replaySafety"])

    def test_inflight_requires_response_still_open_at_mutation(self):
        trace = Trace("inflight-install-h1")
        trace.warm()
        slow = trace.request("slow", "/slow")
        finished = trace.events.pop()
        self.assertEqual(finished["event"], "response_finished")
        trace.setter("direct")
        trace.op("after-immediate")
        trace.op("after-settled")
        trace.emit("response_finished", **slow)
        trace.client("slow-result", {"ok": True, **slow})
        trace.finish()
        self.assertEqual(trace.report()["verdict"], "observed", trace.report())
        mutation = next(e["sequence"] for e in trace.events if e.get("step") == "setter")
        next(e for e in trace.events if e["event"] == "response_finished" and e.get("id", "").endswith("-slow"))["sequence"] = mutation - 0.5
        self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_restart_requires_validated_kill_and_changed_pid(self):
        trace = Trace("restart-empty-h1")
        trace.warm(proxy=True)
        trace.setter("empty")
        trace.op("before-restart", connection="p1", route="proxy-backend", proxy="connect-1")
        kill = {"pid": 111, "command": "CoreSimulator/com.apple.WebKit.Networking"}
        trace.emit("intentional_network_kill", **kill)
        trace.client("network-killed", kill)
        page = trace.request("restart-navigation", "/page", connection="o2")
        trace.client("network-restarted", {"changed": True, "oldPID": 111, "newPID": 222})
        trace.client("restart-page", page)
        trace.op("after-restart", connection="o2")
        trace.finish()
        self.assertEqual(trace.report()["verdict"], "observed", trace.report())
        next(e for e in trace.events if e.get("step") == "network-restarted")["result"]["newPID"] = 111
        self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_cold_h2_is_observation_not_desired_route_assertion(self):
        trace = Trace("cold-before-first-h2")
        trace.setter("direct")
        trace.op("cold-page", "cold-navigation", "/page", protocol="2.0", stream=1)
        trace.op("cold-fetch", protocol="2.0", stream=3)
        trace.finish()
        self.assertEqual(trace.report()["verdict"], "observed", trace.report())
        self.assertEqual(trace.report()["operations"][0]["route"]["path"], "origin-direct")
        trace.remove("client", "setter")
        self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_missing_authoritative_sse_tick_cannot_prove_survival(self):
        trace = constant()
        trace.remove("sse_tick")
        self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_inventory_requires_all_failure_booleans(self):
        inventory(CASES)
        with self.assertRaises(ValueError):
            inventory([c for c in CASES if c["id"] != "failover-stall-true-h1"])

    def test_completion_markers_without_requests_rejected(self):
        events = []
        for case in CASES:
            for step, result in (("case_start", {}), ("case_complete", {"executionComplete": True})):
                events.append(dict(sequence=len(events) + 1, event="client", case=case["id"], step=step, result=result))
        self.assertEqual(analyze_matrix(CASES, events)["observedCases"], 0)

    def test_unchanged_route_is_valid_negative_observation(self):
        report = idle().report()
        self.assertEqual(report["verdict"], "observed", report)
        self.assertEqual(report["operations"][-1]["route"]["path"], "origin-direct")
        self.assertEqual(report["qualification"], "not asserted")

    def test_failed_fetch_is_preserved_not_routing_success(self):
        report = idle(failed=True).report()
        self.assertEqual(report["verdict"], "observed", report)
        for op in report["operations"][-2:]:
            self.assertEqual(op["outcome"], "failed")
            self.assertIsNone(op["route"])
            self.assertFalse(op["clientResult"]["ok"])

    def test_missing_operation_and_false_success_are_unmeasured(self):
        trace = idle()
        trace.remove("client", "after-settled")
        self.assertEqual(trace.report()["verdict"], "unmeasured")
        trace = idle(failed=True)
        next(e for e in trace.events if e.get("step") == "after-settled")["result"]["ok"] = True
        self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_conflicting_completion_rejected(self):
        trace = idle()
        trace.client("case_unmeasured", {"error": "fixture failure"})
        self.assertEqual(trace.report()["execution"], "contradictory")
        self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_existing_connect_is_correlated_for_reuse(self):
        trace = idle(proxy_after=True)
        self.assertEqual(sum(e["event"] == "proxy_connect" for e in trace.events), 1)
        self.assertEqual(trace.report()["verdict"], "observed", trace.report())
        trace.remove("proxy_connect")
        self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_duplicate_request_identity_rejected(self):
        trace = idle()
        request = next(e for e in trace.events if e["event"] == "request" and e["id"].endswith("after-immediate"))
        trace.events.insert(trace.events.index(request), copy.deepcopy(request))
        self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_get_retry_attempts_not_hidden_or_claimed_replay_safe(self):
        trace = idle()
        request = next(e for e in trace.events if e["event"] == "request" and e["id"].endswith("after-immediate"))
        retry = {**request, "connection": "different-http-session"}
        trace.events.insert(trace.events.index(request), retry)
        report = trace.report()
        self.assertEqual(report["verdict"], "observed", report)
        self.assertEqual(next(o for o in report["operations"] if o["step"] == "after-immediate")["getAttemptCount"], 2)

    def test_failover_true_and_false_listener_controls(self):
        for kind in ("absent", "refuse", "stall"):
            for flag in (True, False):
                with self.subTest(kind=kind, flag=flag):
                    report = failure(kind, flag, failed=not flag or kind == "stall").report()
                    self.assertEqual(report["verdict"], "observed", report)
                    self.assertIs(report["failureCondition"]["allowFailover"], flag)

    def test_missing_listener_event_is_unmeasured(self):
        for kind, event in (("absent", "absent_listener_check"), ("refuse", "proxy_refused"), ("stall", "proxy_stall_connect")):
            trace = failure(kind)
            trace.remove(event)
            self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_fallback_cannot_reuse_initial_origin_socket(self):
        trace = failure("absent")
        for e in trace.events:
            target = e.get("result", e)
            if target.get("id", "").endswith("failure-fetch"):
                target["tcpConnection"] = "tcp-o1"
        self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_constant_original_stream_survival(self):
        report = constant().report()
        self.assertEqual(report["verdict"], "observed", report)
        self.assertTrue(report["oldStream"]["originalSurvivedThroughSample"])
        self.assertFalse(report["oldStream"]["automaticReconnectObserved"])

    def test_reconnect_is_not_original_stream_survival(self):
        report = constant(close=True, reconnect=True).report()
        self.assertEqual(report["verdict"], "observed", report)
        self.assertFalse(report["oldStream"]["originalSurvivedThroughSample"])
        self.assertTrue(report["oldStream"]["automaticReconnectObserved"])

    def test_missing_sse_and_zero_targeted_close_are_unmeasured(self):
        trace = constant(close=True)
        trace.remove("client", "old-sse-first")
        self.assertEqual(trace.report()["verdict"], "unmeasured")
        trace = constant(close=True)
        next(e for e in trace.events if e["event"] == "proxy_forced_close")["closed"] = 0
        self.assertEqual(trace.report()["verdict"], "unmeasured")

    def test_failed_h2_precondition_is_unmeasured(self):
        trace = constant()
        for e in trace.events:
            payload = e.get("result", e)
            if payload.get("id", "").endswith(("warm-1", "warm-2")):
                payload["protocol"] = "1.1"
        self.assertEqual(trace.report()["verdict"], "unmeasured")


if __name__ == "__main__":
    unittest.main()
