"""Pure interpretation of client observations plus independent fixture events.

Completion is not qualification. GET failures and unchanged routes are observations;
missing setup/evidence is unmeasured. This experiment makes no replay-safety claim.
"""

IDENTITY = ("id", "route", "protocol", "connection", "tcpConnection", "proxyConnection", "stream")
WARM_ACTIONS = {"idle", "live", "removeOpen", "removeClose", "flightInstall", "flightRemove",
                "restartEmpty", "restartPlaceholder", "constantProxy", "constantProxySurviving"}
PROXY_WARM = {"removeOpen", "removeClose", "flightRemove", "restartEmpty", "restartPlaceholder",
              "constantProxy", "constantProxySurviving"}
STREAM_ACTIONS = {"live", "constantProxy", "constantProxySurviving"}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def inventory(cases):
    require(len({c["id"] for c in cases}) == len(cases), "duplicate case IDs")
    failures = {(c.get("listener"), c.get("allowFailover")) for c in cases if c["action"] == "failure"}
    require(failures == {(kind, flag) for kind in ("absent", "refuse", "stall") for flag in (False, True)},
            "failure inventory must cover absent/refuse/stall × false/true")
    expected = {(action, protocol) for action in ("coldBeforeWebView", "coldBeforeFirstRequest", "idle",
                "removeOpen", "removeClose", "flightInstall", "flightRemove", "restartEmpty", "restartPlaceholder")
                for protocol in ("h1", "h2")}
    expected |= {("live", "h2"), ("constantProxy", "h2"), ("constantProxySurviving", "h2"), ("failure", "h1")}
    require({(c["action"], c["origin"]) for c in cases} == expected, "action/protocol inventory incomplete or unknown")
    require(len(cases) == 27, "expected 27 distinct planned cells")


def operations(case):
    action = case["action"]
    result = {}
    if action in WARM_ACTIONS:
        result.update({"page": ("navigation", "/page"), "warm-1": ("warm-1", "/fetch"), "warm-2": ("warm-2", "/fetch")})
    if action.startswith("cold"):
        result.update({"cold-page": ("cold-navigation", "/page"), "cold-fetch": ("cold-fetch", "/fetch")})
    if action in {"idle", "live", "removeOpen", "removeClose", "flightInstall", "flightRemove", "constantProxy", "constantProxySurviving"}:
        result.update({"after-immediate": ("after-immediate", "/fetch"), "after-settled": ("after-settled", "/fetch")})
    if action in STREAM_ACTIONS:
        result.update({"old-sse-first": ("old-sse", "/events"),
                       "rebuilt-sse": ("rebuilt-sse" if action == "live" else "new-sse", "/events"),
                       "new-websocket": ("new-websocket", "/ws")})
    if action in {"flightInstall", "flightRemove"}:
        result["slow-result"] = ("slow", "/slow")
    if action.startswith("restart"):
        result.update({"before-restart": ("before-restart", "/fetch"), "restart-page": ("restart-navigation", "/page"),
                       "after-restart": ("after-restart", "/fetch")})
    if action == "failure":
        result.update({"failure-page": ("failure-page", "/page"), "failure-fetch": ("failure-fetch", "/fetch")})
    require(bool(result), "unknown action")
    return result


def client(events, case_id, step):
    entries = [e for e in events if e.get("event") == "client" and e.get("case") == case_id and e.get("step") == step]
    require(len(entries) == 1, f"{step}: expected one client result, found {len(entries)}")
    return entries[0]


def route_record(request, events):
    require(request.get("host") == request.get("case", "") + ".webkit-proxy.test", "request host/case mismatch")
    require(request.get("route") in ("origin", "proxy-backend"), "unknown server route")
    require(bool(request.get("connection")) and bool(request.get("tcpConnection")), "missing connection identity")
    require(any(e.get("event") == "tls_open" and e.get("connection") == request["tcpConnection"]
                and e.get("sni") == request["host"] and e["sequence"] < request["sequence"] for e in events),
            "request has no matching TLS socket")
    proxy = request.get("proxyConnection")
    if proxy:
        require(any(e.get("event") == "proxy_connect" and e.get("connection") == proxy
                    and e.get("case") == request["case"] and e["sequence"] < request["sequence"] for e in events),
                "request has no matching existing CONNECT")
        require(any(e.get("event") == "proxy_upstream_connected" and e.get("connection") == proxy
                    and e["sequence"] < request["sequence"] for e in events), "CONNECT upstream not established")
    else:
        require(request["route"] == "origin", "proxy backend request without CONNECT identity")
    return {"path": "origin-via-proxy" if proxy and request["route"] == "origin" else
            "origin-direct" if not proxy else "proxy-backend", **{k: request.get(k) for k in IDENTITY},
            "sequence": request["sequence"]}


def operation(case, step, suffix, path, events):
    output = {"step": step, "id": f"{case['id']}-{suffix}", "outcome": "unmeasured", "route": None}
    try:
        entry = client(events, case["id"], step)
        raw = entry["result"]
        output.update(clientSequence=entry["sequence"], clientResult=raw)
        require(raw.get("unmeasured") is not True, "JavaScript evaluation failed, not a network observation")
        require(raw.get("id") == output["id"], "client operation ID mismatch")
        attempts = [e for e in events if e.get("event") == ("websocket" if path == "/ws" else "request")
                    and e.get("case") == case["id"] and e.get("id") == output["id"] and e.get("path") == path
                    and e["sequence"] < entry["sequence"]]
        require(all(e.get("method") == "GET" for e in attempts), "unexpected non-GET operation")
        identities = [tuple(e.get(k) for k in IDENTITY) for e in attempts]
        require(len(identities) == len(set(identities)), "duplicate authoritative request identity")
        output["attempts"] = [route_record(e, events) for e in attempts]
        output["getAttemptCount"] = len(attempts)
        if raw.get("ok") is False:
            require(isinstance(raw.get("error"), str) and bool(raw["error"]), "failure missing explicit error")
            output["outcome"] = "bounded_failure" if any(s in raw["error"].lower() for s in ("timeout", "abort")) else "failed"
            output["routeVerdict"] = "unmeasured: no successful response"
        else:
            require(raw.get("ok") is True or path == "/page", "missing operation success/failure")
            matched = [e for e in attempts if all(e.get(k) == raw.get(k) for k in IDENTITY)]
            require(len(matched) == 1, "successful client result lacks unique authoritative response identity")
            response_event = "sse_tick" if path == "/events" else "websocket" if path == "/ws" else "response_finished"
            require(any(e.get("event") == response_event and all(e.get(k) == raw.get(k) for k in IDENTITY)
                        and e["sequence"] < entry["sequence"] for e in events), "successful result lacks authoritative response evidence")
            output.update(outcome="routed", route=route_record(matched[0], events), routeVerdict="observed")
    except (ValueError, KeyError, TypeError) as error:
        output["reason"] = str(error)
    return output


def require_routed(op, route=None, protocol=None):
    require(op["outcome"] == "routed", f"{op['step']}: setup request not successful")
    if route:
        require(op["route"]["path"] == route, f"{op['step']}: setup route mismatch")
    if protocol:
        require(op["route"]["protocol"] == protocol, f"{op['step']}: setup protocol mismatch")


def close_evidence(events, host, kind, connection, after, before):
    rows = [e for e in events if e.get("event") == kind and e.get("host") == host and after < e["sequence"] < before]
    require(len(rows) == 1, f"missing/ambiguous {kind}")
    row = rows[0]
    require(row.get("closed", 0) > 0 and row["closed"] == len(row.get("connections", []))
            and connection in row["connections"], f"{kind}: target socket was not positively closed")
    return row


def stream_evidence(case, ops, events):
    first = ops["old-sse-first"]
    require_routed(first, protocol="2.0")
    require(first["route"]["connection"] == ops["warm-2"]["route"]["connection"], "old SSE not on warmed H2 session")
    require(isinstance(first["route"]["stream"], int), "old SSE missing H2 stream ID")
    snapshots = []
    for label in ("old-sse-before", "old-sse-after"):
        row = client(events, case["id"], label)
        raw = row["result"]
        require(raw.get("ok") is True and raw.get("first"), f"{label}: stream snapshot missing")
        require(all(raw["first"].get(k) == first["clientResult"].get(k) for k in IDENTITY), "snapshot original identity changed")
        require(raw.get("originalCount", 0) > 0 and raw.get("last"), "snapshot has no original SSE event")
        for tick in [raw["first"], *raw["last"]]:
            require(any(e.get("event") == "sse_tick" and all(e.get(k) == tick.get(k) for k in (*IDENTITY, "tick"))
                        and e["sequence"] < row["sequence"] for e in events), "snapshot SSE event not in server log")
        snapshots.append(row)
    before, after = snapshots
    require(before["sequence"] < after["sequence"], "stream snapshot ordering invalid")
    old_key = f"{first['route']['connection']}:{first['route']['stream']}"
    latest = after["result"]["last"][-1]
    same = all(latest.get(k) == first["clientResult"].get(k) for k in IDENTITY)
    progressed = after["result"]["originalCount"] > before["result"]["originalCount"]
    reconnected = any(key != old_key for key in after["result"].get("identities", {}))
    original_closed = any(e.get("event") == "response_closed" and all(e.get(k) == first["clientResult"].get(k) for k in IDENTITY)
                          and e["sequence"] < after["sequence"] for e in events)
    original_ticks = [e["sequence"] for e in events if e.get("event") == "sse_tick"
                      and all(e.get(k) == latest.get(k) for k in (*IDENTITY, "tick")) and same]
    return {"original": first["route"], "originalProgressed": progressed,
            "originalClosedBeforeSample": original_closed,
            "latestOriginalTickSequence": max(original_ticks, default=0),
            "originalSurvivedThroughSample": same and progressed and not reconnected and not original_closed,
            "automaticReconnectObserved": reconnected, "before": before["result"], "after": after["result"],
            "note": "Reconnected events are not survival of the original stream."}


def analyze_case(case, events):
    case_id, action = case["id"], case["action"]
    result = {"case": case_id, "action": action, "verdict": "unmeasured", "qualification": "not asserted",
              "reasons": [], "operations": []}
    try:
        start = client(events, case_id, "case_start")
        complete = [e for e in events if e.get("event") == "client" and e.get("case") == case_id and e.get("step") == "case_complete"]
        missing = [e for e in events if e.get("event") == "client" and e.get("case") == case_id and e.get("step") == "case_unmeasured"]
        result["execution"] = "contradictory" if complete and missing else "complete" if complete else "unmeasured"
        result["unmeasuredRecords"] = missing
        # Scope to the case invocation: minimal gates use some of the same hosts.
        end = max([e["sequence"] for e in complete + missing], default=float("inf"))
        scoped = [e for e in events if start["sequence"] <= e["sequence"] <= end]
        ops = {step: operation(case, step, suffix, path, events) for step, (suffix, path) in operations(case).items()}
        result["operations"] = list(ops.values())
        require(len(complete) == 1 and not missing, "missing completion or contradictory/unmeasured case record")
        require(complete[0]["result"].get("executionComplete") is True, "completion marker is not explicit execution evidence")
        require(all(op["outcome"] != "unmeasured" for op in ops.values()), "missing/invalid operation coverage")
        host = case_id + ".webkit-proxy.test"
        if action in WARM_ACTIONS:
            protocol = "2.0" if case["proxy" if action in PROXY_WARM else "origin"] == "h2" else "1.1"
            route = "proxy-backend" if action in PROXY_WARM else "origin-direct"
            for label in ("page", "warm-1", "warm-2"):
                require_routed(ops[label], route, protocol)
            require(ops["warm-1"]["route"]["connection"] == ops["warm-2"]["route"]["connection"], "warm pool not reused")
        if action.startswith("cold"):
            for op in ops.values():
                require_routed(op, protocol="2.0" if case["proxy"] == "h2" else "1.1")
        setters = [e for e in scoped if e.get("event") == "client" and e.get("case") == case_id and e.get("step") == "setter"]
        if action in {"idle", "live", "removeOpen", "removeClose", "flightInstall", "flightRemove", "restartEmpty", "restartPlaceholder"}:
            mode = "direct" if action in {"idle", "live", "flightInstall"} else "placeholder" if action == "restartPlaceholder" else "empty"
            mutations = [e for e in setters if e["result"].get("mode") == mode and e["sequence"] > ops["warm-2"]["clientSequence"]]
            require(len(mutations) == 1, "missing or ambiguous mutation")
            mutation = mutations[0]["sequence"]
            for label in ("after-immediate", "after-settled", "before-restart"):
                if label in ops:
                    require(ops[label]["clientSequence"] > mutation, "post-mutation operation precedes setter")
                    require(all(a["sequence"] > mutation for a in ops[label]["attempts"]), "post-mutation request started before setter")
            if action == "removeClose":
                close_evidence(scoped, host, "proxy_forced_close", ops["warm-2"]["route"]["proxyConnection"], mutation, ops["after-immediate"]["clientSequence"])
            if action.startswith("flight"):
                require(any(a["sequence"] < mutation for a in ops["slow-result"]["attempts"]), "slow request was not in flight before mutation")
                require(not any(e.get("event") in ("response_finished", "response_closed") and e.get("id") == f"{case_id}-slow"
                                and e["sequence"] < mutation for e in scoped), "slow response ended before mutation")
        if action == "coldBeforeFirstRequest":
            require(any(e["result"].get("mode") == "direct" and e["sequence"] < ops["cold-page"]["route"]["sequence"] for e in setters), "cold setter not before first request")
        if action in STREAM_ACTIONS:
            result["oldStream"] = stream_evidence(case, ops, scoped)
            before = client(scoped, case_id, "old-sse-before")["sequence"]
            after = client(scoped, case_id, "old-sse-after")["sequence"]
            if action == "live":
                require(before < mutation < after, "route setter not bracketed by stream observations")
                result["oldStream"]["originalSurvivedThroughSample"] &= mutation < result["oldStream"]["latestOriginalTickSequence"] < after
            if action.startswith("constantProxy"):
                changes = [e for e in scoped if e.get("event") == "proxy_mode" and e.get("host") == host and e.get("mode") == "origin" and before < e["sequence"] < after]
                require(len(changes) == 1, "backend mode change not bracketed by stream observations")
                result["oldStream"]["originalSurvivedThroughSample"] &= changes[0]["sequence"] < result["oldStream"]["latestOriginalTickSequence"] < after
                require(not any(e["sequence"] > changes[0]["sequence"] for e in setters), "constant proxy setter changed after backend selection")
                if action == "constantProxy":
                    close_evidence(scoped, host, "proxy_forced_close", ops["old-sse-first"]["route"]["proxyConnection"], changes[0]["sequence"], ops["after-immediate"]["clientSequence"])
                else:
                    require(not any(e.get("event") == "proxy_forced_close" and e.get("host") == host for e in scoped), "survival control forcibly closed sockets")
        if action.startswith("restart"):
            killed = client(scoped, case_id, "network-killed")["result"]
            restarted = client(scoped, case_id, "network-restarted")["result"]
            require(restarted.get("changed") is True and restarted.get("newPID", 0) > 1 and restarted["newPID"] != killed["pid"] == restarted["oldPID"], "restart PID change not demonstrated")
            require(any(e.get("event") == "intentional_network_kill" and e.get("pid") == killed["pid"] and e.get("command") == killed.get("command") for e in scoped), "no validated process kill evidence")
        if action == "failure":
            require_routed(ops["failure-page"], "origin-direct", "1.1")
            page, fetch = ops["failure-page"], ops["failure-fetch"]
            closed = close_evidence(scoped, host, "origin_forced_close", page["route"]["tcpConnection"], page["clientSequence"], fetch["clientSequence"])
            require(len(setters) == 1 and setters[0]["result"].get("mode") == case["listener"] and setters[0]["result"].get("allowFailover") is case["allowFailover"], "failure proxy listener/Boolean mismatch")
            require(closed["sequence"] < setters[0]["sequence"], "origin was not closed before failure setter")
            if fetch["outcome"] == "routed":
                require(fetch["route"]["tcpConnection"] not in closed["connections"], "fallback reused closed initial origin connection")
            event = {"absent": "absent_listener_check", "refuse": "proxy_refused", "stall": "proxy_stall_connect"}[case["listener"]]
            conditions = [e for e in scoped if e.get("event") == event and e.get("case") == case_id and closed["sequence"] < e["sequence"] < fetch["clientSequence"]]
            require(bool(conditions), "failure listener condition was not observed for this host")
            expected_port = {"absent": 18081, "refuse": 18082, "stall": 18083}[case["listener"]]
            require(all(e.get("host") == host and e.get("port") == expected_port for e in conditions), "failure listener host/port mismatch")
            if case["listener"] == "absent":
                require(all(e.get("state") == "refused" for e in conditions), "absent listener port was not refused")
            else:
                require(any(e["sequence"] > setters[0]["sequence"] for e in conditions), "listener was not exercised after setter")
                require(all(any(a.get("event") == f"proxy_{case['listener']}_accept" and a.get("connection") == e.get("connection")
                                and a["sequence"] < e["sequence"] for a in scoped) for e in conditions), "missing listener TCP accept correlation")
            result["failureCondition"] = {"listener": case["listener"], "allowFailover": case["allowFailover"], "events": conditions}
        result["verdict"] = "observed"
    except (ValueError, KeyError, TypeError) as error:
        result["reasons"].append(str(error))
    return result


def analyze_matrix(cases, events):
    inventory(cases)
    results = [analyze_case(case, events) for case in cases]
    return {"transportQualified": False, "replaySafety": "unmeasured: every operation uses GET",
            "expectedCases": len(cases), "observedCases": sum(r["verdict"] == "observed" for r in results),
            "cases": results}
