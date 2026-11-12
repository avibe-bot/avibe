"""In-memory regression controls for the ten-cell evidence contract; no listeners."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from instance_evidence import analyze, analyze_case, write_report

CASES = json.loads(Path(__file__).with_name("instance-cases.json").read_text())


class Trace:
    def __init__(self, c):
        self.c = c
        self.events = []
        self.sockets = {}
        self.owners = {}
        self.counter = 0

    def add(self, event, **kw):
        row = {"event":event,"case":self.c["id"],"sequence":len(self.events)+1, **kw}
        self.events.append(row)
        return row

    def client(self, step, **kw):
        return self.add("client",extension="instance-v1",step=step,appPID=42,**kw)

    def listener(self, owner, port, stalled=False):
        name = f"{owner}-{port}"
        epoch = None if owner == "standin" else 1 if port == 18100 else 2
        fields = {"owner":owner,"port":port,"pid":len(self.owners)+101,"epoch":epoch,"listener":name}
        self.owners[name] = fields
        self.add("instance_listener",**fields,state="ready",stalled=stalled)
        return name

    def socket(self, host, path):
        self.counter += 1
        tcp = f"{self.c['id']}-tls-{self.counter}"
        h2 = self.c["protocol"] == "h2"
        session = f"{self.c['id']}-h2-{self.counter}" if h2 else tcp
        proxy = None
        if path != "direct":
            name = next(n for n,v in self.owners.items() if v["port"] == (18101 if path == "P2" else 18100))
            owner = self.owners[name]
            proxy = f"{self.c['id']}-cx-{self.counter}"
            self.add("instance_accept",**owner,connection=proxy)
            self.add("instance_connect",**owner,connection=proxy,host=host,target=host+":443",stalled=False)
            self.add("instance_upstream",**owner,connection=proxy,host=host,sourcePort=20000+self.counter,upstreamPort=18444 if h2 else 18443)
        fields = {"route":"origin" if path == "direct" else "proxy-backend", "connection":session,
                  "tcpConnection":tcp,"proxyConnection":proxy,"protocol":"2.0" if h2 else "1.1",
                  "alpn":"h2" if h2 else "http/1.1","stream":1 if h2 else None}
        self.add("tls_open",connection=tcp,sni=host,proxyConnection=proxy,peerPort=20000+self.counter,
                 route=fields["route"],alpn=fields["alpn"],
                 port=443 if path == "direct" else 18444 if h2 else 18443)
        if h2: self.add("h2_session",connection=session)
        self.sockets[(host,path)] = fields
        return fields

    def write(self, store, seq, hosts, port):
        for phase in ("start","end"):
            self.client("config_write",store=store,storeUUID=store+"-uuid",configSeq=seq,
                        hosts=sorted(hosts),port=port,allowFailover=False,phase=phase)

    def op(self, store, view, host, label, kind, seq, path):
        id_ = f"{self.c['id']}-{store}-{view}-{label}"
        fields = {"store":store,"storeUUID":store+"-uuid","view":view,"id":id_,"host":host,"label":label,"kind":kind,"attempt":1,"configSeq":seq}
        self.client("op_start",**fields)
        if path in ("failed","bounded_failure"):
            result = {"ok":False,"settled":True,"outcome":path,"id":id_,"error":"fixture failure"}
        else:
            socket = self.sockets.get((host,path)) or self.socket(host,path)
            result = {**socket,"host":host,"id":id_,"path":"/page" if kind == "nav" else "/fetch","method":"GET"}
            self.add("request",**result)
            self.add("response_finished",**result)
            result.update(ok=True,settled=True,outcome="routed")
        self.client("op_end",**fields,result=result)
        return result

    def build(self, paths=None, control_path="direct"):
        c = self.c
        host = lambda r: f"{c['id']}-{r}.webkit-proxy.test"
        count = 2 if c["question"] == "q4" else 1
        self.add("instance_setup",parentPID=100,protocol=c["protocol"],mode=c["mode"])
        old = self.listener("standin" if c["mode"] == "occupied-existing" else "listener(1)",18100)
        self.client("cell_start",deadlineSeconds=240,stores=[{"store":f"s{i}","storeUUID":f"s{i}-uuid"} for i in range(count)])
        warm = []
        for i in range(count):
            s,v = f"s{i}",f"v{i}"
            self.write(s,1,[] if c["question"] == "q2" else [host("a" if i == 0 else "b")],18100)
            h = host("control" if c["question"] == "q2" else "a" if i == 0 else "b")
            path = "direct" if c["question"] == "q2" else "P"
            if c["question"] == "q3": self.op(s,"v1",h,"seed-page","nav",1,path)
            for label,kind in (("warm-page","nav"),("warm-1","fetch"),("warm-2","fetch")):
                r = self.op(s,v,h,label,kind,1,path)
            warm.append(r)
            self.client("warm",store=s,connection=r["connection"],tcpConnection=r["tcpConnection"])
        if c["question"] == "q3": self.op("s0","v0",host("a"),"a-before","fetch",1,"P")
        self.client("boundary")
        if c["question"] == "q4":
            if c["mode"] == "occupied-existing":
                self.add("instance_stall",**self.owners[old],connections=[r["proxyConnection"] for r in warm])
            self.add("instance_recovery_start",owned=[] if c["mode"] == "occupied-existing" else [old])
            if c["mode"] != "occupied-existing":
                self.add("instance_owned_closed",**self.owners[old],connections=[r["proxyConnection"] for r in warm])
                self.add("instance_listener",**self.owners[old],state="stopped")
                for r in warm: self.add("tls_close",connection=r["tcpConnection"])
                # Stop using old sockets; immutable history retained.
                self.sockets.clear()
            if c["mode"] == "occupied-new": self.listener("standin",18100,True)
            self.listener("listener(2)",18101)
            self.add("instance_recovery_ready")
        for i in range(count):
            hosts = [host("a" if i == 0 else "b")]
            if c["question"] == "q3": hosts.append(host("b"))
            self.write(f"s{i}",2,hosts,18101 if c["question"] == "q4" else 18100)
        for i in range(count):
            s = f"s{i}"; v = "v1" if c["question"] == "q3" else f"v{i}"
            h = host("b" if c["question"] == "q3" or i == 1 else "a")
            desired = "P2" if c["question"] == "q4" else "P"
            for n,(label,kind) in enumerate((("after-immediate","fetch"),("after-nav","nav"),("after-fetch","fetch"))):
                self.op(s,v,h,label,kind,2,(paths or [desired]*3)[n])
        if c["question"] == "q2": self.op("s0","v0",host("control"),"control-after","fetch",2,control_path)
        if c["question"] == "q3": self.op("s0","v0",host("a"),"a-after","fetch",2,"P")
        self.client("cell_done")
        if c["mode"].startswith("occupied"):
            standin = next(v for v in self.owners.values() if v["owner"] == "standin")
            self.add("instance_snapshot",**standin,stalled=True,connections=[])
        self.add("instance_observation_end")
        return self.events


def renumber(events):
    for i,e in enumerate(events): e["sequence"] = i+1


class InstanceEvidenceTests(unittest.TestCase):
    def test_ten_positive_cases(self):
        for c in CASES:
            with self.subTest(case=c["id"]):
                r = analyze_case(c,Trace(c).build())
                self.assertEqual(r["reasons"],[])
                self.assertIs(r["usesIntendedRoute"],True)

    def test_false_mixed_failed_bounded_are_measured(self):
        for c in CASES:
            for paths in (["direct"]*3,["failed"]*3,["bounded_failure"]*3,
                          ["direct","P2" if c["question"] == "q4" else "P","direct"]):
                with self.subTest(case=c["id"],paths=paths):
                    r = analyze_case(c,Trace(c).build(paths))
                    self.assertEqual(r["verdict"],"measured",r)
                    self.assertIs(r["usesIntendedRoute"],False)

    def test_q2_control_host_requires_completed_direct_observation(self):
        for c in CASES[:2]:
            for path in ("failed", "bounded_failure", "P"):
                with self.subTest(case=c["id"], path=path):
                    r = analyze_case(c, Trace(c).build(control_path=path))
                    self.assertEqual(r["verdict"], "unmeasured")
                    self.assertIsNone(r["usesIntendedRoute"])
                    self.assertIs(r["controlRemainsDirect"], False)
                    self.assertIn("control host direct observation missing", r["reasons"])
                    control = next(o for o in r["operations"] if o["label"] == "control-after")
                    self.assertEqual(control["outcome"], "routed" if path == "P" else path)
                    self.assertTrue(all(o["outcome"] == "routed" for o in r["operations"] if o["label"].startswith("after-")))

    def test_every_child_must_live_through_observation_end(self):
        for c in CASES:
            original = Trace(c).build()
            workers = [e for e in original if e["event"] == "instance_listener" and e["state"] == "ready"]
            for worker in workers:
                for early in (True, False):
                    with self.subTest(case=c["id"], owner=worker["owner"], early=early):
                        events = copy.deepcopy(original)
                        finish = next(i for i,e in enumerate(events) if e["event"] == "instance_observation_end")
                        events.insert(finish if early else finish+1, {
                            "event":"instance_child_exit", "case":c["id"], "listener":worker["listener"],
                            "pid":worker["pid"], "code":0, "signal":None,
                        })
                        renumber(events)
                        r = analyze_case(c,events)
                        if early:
                            self.assertIsNone(r["usesIntendedRoute"])
                            self.assertIn("listener child exited before observation end",r["reasons"])
                            self.assertTrue(r["operations"])
                        else:
                            self.assertIs(r["usesIntendedRoute"],True,r)

    def test_failure_payload_identity_required_for_fetch_and_navigation(self):
        for c in CASES:
            for outcome in ("failed", "bounded_failure"):
                for label in ("after-immediate", "after-nav", "after-fetch"):
                    for bad_id in (None, "wrong-id"):
                        with self.subTest(case=c["id"],outcome=outcome,label=label,id=bad_id):
                            events = Trace(c).build([outcome]*3)
                            end = next(e for e in events if e.get("step") == "op_end" and e.get("label") == label)
                            if bad_id is None: end["result"].pop("id")
                            else: end["result"]["id"] = bad_id
                            r = analyze_case(c,events)
                            self.assertIsNone(r["usesIntendedRoute"])
                            op = next(o for o in r["operations"] if o["id"] == end["id"])
                            self.assertEqual(op["outcome"], "unmeasured")
                            self.assertEqual(op["clientResult"]["outcome"], outcome)
                            self.assertEqual(op["error"], "failure operation ID missing/mismatched")

    def test_missing_producer_records_cannot_qualify(self):
        for c in CASES:
            original = Trace(c).build()
            filters = [lambda e:e.get("step") == "cell_done",
                       lambda e:e.get("step") == "op_end" and e.get("label") == "after-fetch",
                       lambda e:e.get("event") == "response_finished",
                       lambda e:e.get("event") == "instance_connect",
                       lambda e:e.get("event") == "tls_open",
                       lambda e:e.get("step") == "config_write" and e.get("configSeq") == 2]
            for predicate in filters:
                events = [e for e in original if not predicate(e)]
                r = analyze_case(c,events)
                self.assertIsNone(r["usesIntendedRoute"],(c,r))
                self.assertTrue(r["operations"])

    def test_warm_close_is_unmeasured(self):
        for c in CASES:
            events = Trace(c).build()
            boundary = next(i for i,e in enumerate(events) if e.get("step") == "boundary")
            warm = next(e for e in events if e.get("step") == "warm")
            events.insert(boundary,{"event":"tls_close","connection":warm["tcpConnection"]})
            renumber(events)
            self.assertIsNone(analyze_case(c,events)["usesIntendedRoute"])

    def test_speculative_fresh_host_connect_rejected(self):
        for c in CASES[:4]:
            events = Trace(c).build()
            host = f"{c['id']}-{'a' if c['question']=='q2' else 'b'}.webkit-proxy.test"
            events.insert(1,{"event":"instance_connect","case":c["id"],"host":host})
            renumber(events)
            self.assertIn("speculatively",analyze_case(c,events)["reasons"][0])

    def test_ownership_and_stall_controls(self):
        for c in CASES[4:]:
            events = Trace(c).build()
            if c["mode"] == "occupied-existing":
                events.insert(-1,{"event":"instance_owned_closed","case":c["id"],"owner":"standin","epoch":None})
            else:
                events = [e for e in events if e["event"] != "instance_owned_closed"]
            renumber(events)
            self.assertIsNone(analyze_case(c,events)["usesIntendedRoute"])
        for c in CASES[6:]:
            events = Trace(c).build()
            events = [e for e in events if e["event"] != "instance_snapshot"]
            self.assertIsNone(analyze_case(c,events)["usesIntendedRoute"])

    def test_store_epoch_and_probe_order(self):
        for change in ("configSeq","storeUUID","kind"):
            c = CASES[-1]; events = Trace(c).build()
            e = next(e for e in events if e.get("step") == "op_start" and e.get("label") == "after-immediate" and e.get("store") == "s1")
            e[change] = 1 if change == "configSeq" else "wrong"
            self.assertIsNone(analyze_case(c,events)["usesIntendedRoute"])

    def test_reused_connect_is_valid_and_no_contact_old_port_required(self):
        c = CASES[6]; events = Trace(c).build()
        r = analyze_case(c,events)
        self.assertTrue(r["usesIntendedRoute"])
        self.assertFalse(any(e["event"] == "instance_connect" and e.get("owner") == "standin" for e in events))
        after = [o for o in r["operations"] if o["label"].startswith("after-") and o["store"] == "s0"]
        self.assertEqual(len({o["route"]["proxyConnection"] for o in after}),1)

    def test_partial_report_preserves_other_cells(self):
        a = Trace(CASES[0]).build()
        b = Trace(CASES[1]).build()
        b = [e for e in b if e.get("step") != "cell_done"]
        events = a + b
        renumber(events)
        r = analyze(CASES[:2],events)
        self.assertEqual(r["measuredCases"],1)
        self.assertTrue(r["cases"][0]["usesIntendedRoute"])
        self.assertIsNone(r["cases"][1]["usesIntendedRoute"])
        captured = []
        def text(path):
            return json.dumps(CASES[:2]) if path.name == "instance-cases.json" else "\n".join(map(json.dumps,events))
        with patch.object(Path,"exists",return_value=True),patch.object(Path,"read_text",text),patch.object(Path,"write_text",lambda p,t:captured.append(t)):
            write_report(Path("/fake"),Path("/fake/instance-cases.json"))
        self.assertEqual(json.loads(captured[0])["measuredCases"],1)

    def test_cross_host_h2_reuse_is_measured_not_missing(self):
        c = CASES[3]
        t = Trace(c)
        events = t.build()
        a_request = next(e for e in events if e["event"] == "request" and e["host"].endswith("-a.webkit-proxy.test"))
        # Server-authoritative B GETs travel over A's existing CONNECT/session.
        for e in events:
            if e["event"] in ("request", "response_finished") and e.get("host", "").endswith("-b.webkit-proxy.test"):
                for k in ("connection", "tcpConnection", "proxyConnection"):
                    e[k] = a_request[k]
            if e.get("step") == "op_end" and e.get("host", "").endswith("-b.webkit-proxy.test"):
                for k in ("connection", "tcpConnection", "proxyConnection"):
                    e["result"][k] = a_request[k]
        r = analyze_case(c,events)
        self.assertEqual(r["verdict"],"measured",r)
        self.assertIs(r["usesIntendedRoute"],False)
        self.assertTrue(r["aRoutePreserved"])

    def test_attempts_retained(self):
        c = CASES[0]; events = Trace(c).build()
        start = next(e for e in events if e.get("step") == "op_start" and e.get("label") == "after-immediate")
        req = next(e for e in events if e["event"] == "request" and e.get("id") == start["id"])
        events.insert(events.index(req),copy.deepcopy(req)); renumber(events)
        r = analyze_case(c,events)
        o = next(o for o in r["operations"] if o["label"] == "after-immediate")
        self.assertEqual(len(o["attempts"]),2)
        self.assertEqual(o["getAttemptCount"],2)
        self.assertIs(r["usesIntendedRoute"],True)


if __name__ == "__main__":
    unittest.main()
