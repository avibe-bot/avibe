"""Pure correlation for the separate ten-cell instance-route extension.

No target route is an evidence gate. Missing preconditions are null, never false.
The writer/CLI preserves every operation and partial case before failing coverage.
"""
import json
import sys
from pathlib import Path


class EvidenceError(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise EvidenceError(message)


def one(rows, message):
    need(len(rows) == 1, message)
    return rows[0]


def select(events, event=None, **fields):
    return [e for e in events if (event is None or e.get("event") == event)
            and all(e.get(k) == v for k, v in fields.items())]


def client(events, step, **fields):
    return select(events, "client", step=step, extension="instance-v1", **fields)


def route(events, request):
    """Map request to TLS, CONNECT and listener identity (including reused CONNECT)."""
    seq = request["sequence"]
    tls = one([e for e in select(events, "tls_open", connection=request.get("tcpConnection"))
               if e["sequence"] < seq], "TLS identity missing/ambiguous")
    allowed = {f"{request['case']}-{role}.webkit-proxy.test" for role in ("control", "a", "b")}
    need(tls.get("sni") in allowed, "TLS SNI outside fixture cell")
    need(request.get("method") == "GET", "non-GET excluded")
    need(request.get("protocol") in ("1.1", "2.0"), "missing HTTP protocol")
    need(request.get("alpn") == ("h2" if request["protocol"] == "2.0" else "http/1.1"), "ALPN mismatch")
    need(tls.get("alpn") == request["alpn"] and tls.get("route") == request["route"], "TLS/request protocol or route mismatch")
    if request["protocol"] == "2.0":
        need(isinstance(request.get("stream"), int), "missing H2 stream")
        one([e for e in select(events, "h2_session", connection=request["connection"])
             if e["sequence"] < seq], "missing H2 session")
    else:
        need(request["connection"] == request["tcpConnection"], "H1 socket mismatch")
    need(not [e for e in select(events, "tls_close", connection=request["tcpConnection"])
              if e["sequence"] < seq], "request after TLS close")
    connect_id = request.get("proxyConnection")
    result = {k: request.get(k) for k in ("route", "connection", "tcpConnection", "stream", "protocol", "proxyConnection", "sequence")}
    result["tlsSNI"] = tls["sni"]
    result["requestHost"] = request["host"]
    if connect_id is None:
        need(tls.get("proxyConnection") is None and request.get("route") == "origin", "unlinked proxy request")
        result.update(path="direct", listener=None, epoch=None, owner=None)
    else:
        con = one([e for e in select(events, "instance_connect", connection=connect_id)
                   if e["sequence"] < seq], "CONNECT identity missing")
        up = one([e for e in select(events, "instance_upstream", connection=connect_id)
                  if e["sequence"] < tls["sequence"]], "upstream mapping missing")
        ready = one([e for e in select(events, "instance_listener", listener=con["listener"], state="ready")
                     if e["sequence"] < con["sequence"]], "listener not ready")
        one([e for e in select(events, "instance_accept", connection=connect_id)
             if ready["sequence"] < e["sequence"] < con["sequence"]], "accept missing")
        need(con["host"] in allowed and con["target"] == con["host"] + ":443"
             and con["host"] == tls["sni"], "CONNECT/TLS target mismatch")
        need(tls.get("proxyConnection") == connect_id and up["sourcePort"] == tls["peerPort"]
             and up["upstreamPort"] == tls["port"], "upstream/TLS correlation mismatch")
        need(con["port"] in (18100, 18101) and request["route"] == "proxy-backend", "wrong fixture endpoint")
        for field in ("owner", "epoch", "pid", "port"):
            need(con[field] == ready[field] == up[field], "listener identity changed")
        need((con["owner"], con["epoch"]) in (("listener(1)", 1), ("listener(2)", 2), ("standin", None)), "invalid owner/epoch")
        result.update(path="P2" if con["port"] == 18101 else "P", listener=con["listener"], connectHost=con["host"],
                      epoch=con["epoch"], owner=con["owner"], pid=con["pid"])
    return result


def operation(events, start):
    result = {k: start[k] for k in ("id", "store", "storeUUID", "view", "host", "label", "kind", "configSeq")}
    attempts = select(events, "request", id=start["id"])
    result["attempts"] = attempts
    result["getAttemptCount"] = len(attempts)
    try:
        end = one(client(events, "op_end", id=start["id"]), "operation did not finish")
        need(start["sequence"] < end["sequence"], "operation boundary order")
        for k in ("store", "storeUUID", "view", "host", "label", "kind", "configSeq", "attempt", "appPID"):
            need(start.get(k) == end.get(k), "operation identity changed")
        need(start.get("attempt") == 1, "harness retry excluded")
        payload = end["result"]
        result["clientResult"] = payload
        need(payload.get("settled") is True, "operation/bridge not settled")
        for req in attempts:
            need(start["sequence"] < req["sequence"] < end["sequence"], "request outside operation boundary")
            need(req["host"] == start["host"] and req["path"] == ("/page" if start["kind"] == "nav" else "/fetch"), "request target mismatch")
        if payload.get("outcome") == "routed":
            need(payload.get("ok") is True and payload.get("id") == start["id"], "stale page or false success")
            matches = [r for r in attempts if all(r.get(k) == payload.get(k) for k in
                       ("route", "connection", "tcpConnection", "stream", "proxyConnection", "protocol", "host", "id", "path"))]
            completed = [r for r in matches if any(r["sequence"] < e["sequence"] < end["sequence"]
                         and all(e.get(k) == r.get(k) for k in ("host", "path", "tcpConnection", "stream", "proxyConnection", "route"))
                         for e in select(events,"response_finished",id=r["id"],connection=r["connection"]))]
            need(bool(completed), "no matching completed authoritative request")
            req = max(completed, key=lambda r:r["sequence"])
            # WebKit may make several GET attempts; retain all, including earlier
            # routes. The harness itself never retries an operation.
            result.update(outcome="routed", route=route(events, req))
        else:
            need(payload.get("ok") is False and payload.get("outcome") in ("failed", "bounded_failure"), "invalid failure evidence")
            need(payload.get("id") == start["id"], "failure operation ID missing/mismatched")
            result.update(outcome=payload["outcome"], route=None)
        result["attemptRoutes"] = [route(events, req) for req in attempts]
        result["startSequence"] = start["sequence"]
        result["endSequence"] = end["sequence"]
    except (KeyError, TypeError, EvidenceError) as error:
        result.update(outcome="unmeasured", error=str(error), route=None)
    return result


def analyze_case(c, events):
    case = c["id"]
    ev = [e for e in events if e.get("case") == case or e.get("event") in ("tls_open", "tls_close", "h2_session", "h2_close")]
    starts = client(ev, "op_start")
    ops = [operation(ev, s) for s in starts]
    report = {"case":case, "operations":ops, "usesIntendedRoute":None, "verdict":"unmeasured", "reasons":[]}
    try:
        setup = one(select(ev,"instance_setup"),"missing setup")
        begin = one(client(ev,"cell_start"),"missing cell_start")
        boundary = one(client(ev,"boundary"),"missing mutation boundary")
        done = one(client(ev,"cell_done"),"missing completion")
        finish = one(select(ev,"instance_observation_end"),"missing observation end")
        need(setup["sequence"] < begin["sequence"] < boundary["sequence"] < done["sequence"] < finish["sequence"],"cell order")
        need(not client(ev,"cell_error") and not client(ev,"finish_error"),"cell interrupted")
        # stop-owned closes listener(1)'s sockets/server, not its child process.
        # Every worker stays alive until teardown after observation-end.
        need(not [e for e in select(ev,"instance_child_exit") if e["sequence"] < finish["sequence"]],
             "listener child exited before observation end")
        need(all(e.get("appPID") == begin["appPID"] for e in ev if e.get("extension") == "instance-v1"),"App process changed")
        expected_count = 2 if c["question"] == "q4" else 1
        stores = begin["stores"]
        need(begin["deadlineSeconds"] == 240, "missing whole-cell budget")
        need(len(stores) == expected_count and len({s["storeUUID"] for s in stores}) == expected_count,"live-store inventory")
        need({s["store"] for s in stores} == {f"s{i}" for i in range(expected_count)},"store handles")
        need(all(o["store"] in {s["store"] for s in stores} for o in ops), "unregistered store operation")
        need(len({s["id"] for s in starts}) == len(starts),"duplicate operation ID")
        need(all(o["outcome"] != "unmeasured" for o in ops),"missing operation evidence")
        host = lambda role: f"{case}-{role}.webkit-proxy.test"
        before = {}
        updates = []
        for index, store in enumerate(stores):
            handle = store["store"]
            own_ops = [o for o in ops if o["store"] == handle]
            need(all(o["storeUUID"] == store["storeUUID"] for o in own_ops),"store UUID changed")
            writes = client(ev,"config_write",store=handle)
            need(len(writes) == 4,"expected two bracketed writes per store")
            config_ends = []
            for config_seq in (1,2):
                start = one([e for e in writes if e["configSeq"] == config_seq and e["phase"] == "start"],"write start missing")
                end = one([e for e in writes if e["configSeq"] == config_seq and e["phase"] == "end"],"write end missing")
                need(start["sequence"] < end["sequence"],"write order")
                expected_hosts = ([host("a" if index == 0 else "b")] if c["question"] != "q2" or config_seq == 2 else [])
                if c["question"] == "q3" and config_seq == 2: expected_hosts = [host("a"),host("b")]
                expected_port = 18101 if c["question"] == "q4" and config_seq == 2 else 18100
                for e in (start,end):
                    need(e["storeUUID"] == store["storeUUID"] and e["hosts"] == sorted(expected_hosts)
                         and e["port"] == expected_port and e["allowFailover"] is False,"wrong configuration")
                need(end["sequence"] < boundary["sequence"] if config_seq == 1 else start["sequence"] > boundary["sequence"],"configuration epoch boundary")
                config_ends.append(end)
            updates.append(config_ends[1])
            for o in own_ops:
                need(o["startSequence"] > config_ends[o["configSeq"]-1]["sequence"],"op before config installed")
                if o["configSeq"] == 1: need(o["endSequence"] < boundary["sequence"],"old op across boundary")
                need(o["endSequence"] < done["sequence"],"operation after completion")
            warm = [one([o for o in own_ops if o["label"] == label],"missing warm operation") for label in ("warm-page","warm-1","warm-2")]
            need(all(o["outcome"] == "routed" for o in warm),"warm load failed")
            a,b = warm[1:]
            proto = "2.0" if c["protocol"] == "h2" else "1.1"
            need(all(o["route"]["protocol"] == proto for o in warm),"warm protocol mismatch")
            need(a["route"]["connection"] == b["route"]["connection"] and a["route"]["tcpConnection"] == b["route"]["tcpConnection"],"warm reuse missing")
            need(a["endSequence"] < b["startSequence"] < b["endSequence"] < boundary["sequence"],"warm order")
            expected_path = "direct" if c["question"] == "q2" else "P"
            need(all(o["route"]["path"] == expected_path for o in warm),"warm route missing")
            warm_marker = one(client(ev,"warm",store=handle),"warm marker missing")
            need(warm_marker["connection"] == b["route"]["connection"] and warm_marker["tcpConnection"] == b["route"]["tcpConnection"],"warm marker identity")
            need(not [e for e in select(ev,"tls_close",connection=b["route"]["tcpConnection"]) if e["sequence"] < boundary["sequence"]],"pre-boundary TLS close")
            need(not [e for e in select(ev,"h2_close",connection=b["route"]["connection"]) if e["sequence"] < boundary["sequence"]],"pre-boundary H2 close")
            before[handle] = b["route"]

        target_ops = [o for o in ops if o["label"].startswith("after-")]
        expected_views = ["v1"] if c["question"] == "q3" else [f"v{i}" for i in range(expected_count)]
        for i,view in enumerate(expected_views):
            probes = [one([o for o in target_ops if o["view"] == view and o["label"] == label],"missing probe")
                      for label in ("after-immediate","after-nav","after-fetch")]
            need([o["kind"] for o in probes] == ["fetch","nav","fetch"],"probe kind order")
            expected_host = host("b" if c["question"] == "q3" or i == 1 else "a")
            need(all(o["host"] == expected_host and o["configSeq"] == 2 for o in probes),"probe target/epoch")
            need(probes[0]["endSequence"] < probes[1]["startSequence"] and probes[1]["endSequence"] < probes[2]["startSequence"],"probe sequence")
        need(len(target_ops) == 3*expected_count,"unexpected target probes")
        if c["question"] in ("q2","q3"):
            fresh = host("a" if c["question"] == "q2" else "b")
            need(not [e for e in ev if (e.get("host") == fresh or e.get("sni") == fresh)
                      and e["event"] in ("request","tls_open","instance_connect","proxy_connect")
                      and e["sequence"] < updates[0]["sequence"]],"fresh host already used/speculatively connected")
            if c["question"] == "q2":
                control = one([o for o in ops if o["label"] == "control-after"],"control host check missing")
                need(control["host"] == host("control"),"wrong control host")
                report["controlRemainsDirect"] = control["outcome"] == "routed" and control["route"]["path"] == "direct"
                need(report["controlRemainsDirect"], "control host direct observation missing")
            else:
                a_before = one([o for o in ops if o["label"] == "a-before"],"A before missing")
                a_after = one([o for o in ops if o["label"] == "a-after"],"A after missing")
                need(a_before["endSequence"] < boundary["sequence"] < a_after["startSequence"],"A probe boundaries")
                report["aRoutePreserved"] = all(o["outcome"] == "routed" and o["route"]["path"] == "P" for o in (a_before,a_after))
                report["aConnectionPreserved"] = report["aRoutePreserved"] and all(a_after["route"][k] == before["s0"][k] for k in ("connection","tcpConnection","proxyConnection"))
        else:
            recovery = one(select(ev,"instance_recovery_start"),"recovery missing")
            ready = one(select(ev,"instance_recovery_ready"),"P2 readiness missing")
            need(boundary["sequence"] < recovery["sequence"] < ready["sequence"] < min(e["sequence"] for e in updates),"recovery/config order")
            p2 = one(select(ev,"instance_listener",owner="listener(2)",state="ready"),"P2 listener missing")
            need(p2["port"] == 18101 and p2["epoch"] == 2 and p2["sequence"] < ready["sequence"],"P2 readiness identity")
            need(all(o["startSequence"] > max(e["sequence"] for e in updates) for o in target_ops),"probe before all stores updated")
            owned_closed = select(ev,"instance_owned_closed")
            need(all(e.get("owner") == "listener(1)" and e.get("epoch") == 1 for e in owned_closed),"recovery closed foreign sockets")
            if c["mode"] == "occupied-existing":
                need(not owned_closed and recovery["owned"] == [],"foreign-only pool was closed by recovery")
                stall = one(select(ev,"instance_stall",owner="standin"),"stall activation missing")
                need(boundary["sequence"] < stall["sequence"] < recovery["sequence"],"stall boundary")
                need(all(r["owner"] == "standin" and r["proxyConnection"] in stall["connections"] for r in before.values()),"foreign existing warm sockets missing")
            else:
                closure = one(owned_closed,"owned closure missing")
                need(recovery["sequence"] < closure["sequence"] < ready["sequence"],"closure boundary")
                need(all(r["owner"] == "listener(1)" and r["proxyConnection"] in closure["connections"] for r in before.values()),"owned warm pool not closed")
                for r in before.values():
                    need(any(boundary["sequence"] < e["sequence"] < min(o["startSequence"] for o in target_ops)
                             for e in select(ev,"tls_close",connection=r["tcpConnection"])),"old TLS closure not observed")
            if c["mode"].startswith("occupied"):
                standin = one(select(ev,"instance_listener",owner="standin",state="ready"),"occupied listener missing")
                need(standin["epoch"] is None and standin["port"] == 18100 and standin["pid"] != setup["parentPID"]
                     and standin["pid"] != p2["pid"],"stand-in not independently owned")
                need(standin["sequence"] < ready["sequence"],"stand-in not ready at recovery")
                if c["mode"] == "occupied-new": need(standin["stalled"] is True,"new listener not stalling")
                snapshot = one(select(ev,"instance_snapshot",owner="standin"),"stand-in final snapshot missing")
                need(snapshot["stalled"] is True and max(o["endSequence"] for o in target_ops) < snapshot["sequence"] < finish["sequence"],"stand-in absent during probes")
                need(not select(ev,"instance_owned_closed",listener=standin["listener"]),"foreign closure")
        expected_labels = {"warm-page", "warm-1", "warm-2", "after-immediate", "after-nav", "after-fetch"}
        if c["question"] == "q2": expected_labels.add("control-after")
        if c["question"] == "q3": expected_labels.update(("seed-page", "a-before", "a-after"))
        need(len(ops) == len(expected_labels) * expected_count and {o["label"] for o in ops} == expected_labels,
             "missing/unexpected operation inventory")
        need(all(o["startSequence"] > begin["sequence"] for o in ops), "operation before cell start")
        if c["question"] == "q3":
            seed = one([o for o in ops if o["label"] == "seed-page"], "B view seed missing")
            need(seed["outcome"] == "routed" and seed["host"] == host("a") and seed["view"] == "v1", "B view document not established")
        desired = "P2" if c["question"] == "q4" else "P"
        decisions = [o["outcome"] == "routed" and o["route"]["path"] == desired
                     and o["route"].get("connectHost") == o["host"]
                     and all(r["path"] == desired and r.get("connectHost") == o["host"] for r in o["attemptRoutes"])
                     for o in target_ops]
        report.update(verdict="measured",usesIntendedRoute=all(decisions),
                      routeOutcome="all-intended" if all(decisions) else "none-intended" if not any(decisions) else "mixed")
    except (EvidenceError,KeyError,TypeError,IndexError) as error:
        report["reasons"].append(str(error))
    return report


def analyze(cases, events):
    rows = [analyze_case(c,events) for c in cases]
    return {"cases":rows,"expectedCases":len(cases),"measuredCases":sum(r["verdict"] == "measured" for r in rows),
            "transportQualified":False,"replaySafety":"GET only; POST replay excluded","streamSurvival":"not claimed"}


def write_report(out: Path, inventory: Path):
    events = []
    log = out / "cert/fixture.jsonl"
    if log.exists():
        # Keep complete records if a killed runner leaves a partial final line.
        for line in log.read_text().splitlines():
            try: events.append(json.loads(line))
            except json.JSONDecodeError: break
    report = analyze(json.loads(inventory.read_text()), events)
    (out / "instance-observations.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


if __name__ == "__main__":
    write_report(Path(sys.argv[1]), Path(sys.argv[2]))
