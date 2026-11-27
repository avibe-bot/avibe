import XCTest
@testable import Avibe

/// Seconds the tests move by hand.
final class DirectPathUseTestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 600

    var now: Int {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
    }
}

final class DirectPathUseTests: XCTestCase {
    private typealias Connection = DirectPathUseConnection

    private func record(
        _ direct: DirectRaceRecord.Leg,
        _ publicOrigin: DirectRaceRecord.Leg,
        _ winner: DirectRelayBackend?,
        sticky: Bool = false
    ) -> DirectRaceRecord {
        DirectRaceRecord(direct: direct, publicOrigin: publicOrigin, winner: winner, path: .relay, sticky: sticky)
    }

    /// One account's store and one instance: whose connections are counted.
    private let scope = DirectPathUse.Scope(store: UUID(), host: "instance-one.example")

    private func object(_ report: DirectPathUse.Report) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(report.json.utf8)) as? [String: Any])
    }

    func testAConnectionIsReadFromTheListenersRecordAndThePlansOwnFacts() {
        XCTAssertEqual(
            Connection(record: record(.ready(120), .cancelled, .direct), carried: .direct, strict: false, tunnelOnly: false),
            Connection(carrier: .direct, plan: .raced, readyMs: 120)
        )
        XCTAssertEqual(
            Connection(record: record(.cancelled, .ready(80), .publicOrigin), carried: .publicOrigin, strict: false, tunnelOnly: false),
            Connection(carrier: .tunnel, plan: .raced, readyMs: 80)
        )
        // No direct candidate: for want of a path, or because there is no
        // direct session for the host at all.
        XCTAssertEqual(
            Connection(record: record(.none, .ready(70), .publicOrigin), carried: .publicOrigin, strict: false, tunnelOnly: false),
            Connection(carrier: .tunnel, plan: .noDirect, readyMs: 70)
        )
        XCTAssertEqual(
            Connection(record: record(.none, .ready(70), .publicOrigin), carried: .publicOrigin, strict: false, tunnelOnly: true),
            Connection(carrier: .tunnel, plan: .tunnelOnly, readyMs: 70)
        )
        // A sticky attempt that missed its bound, carried by the tunnel.
        XCTAssertEqual(
            Connection(
                record: record(.timedOut, .ready(90), .publicOrigin, sticky: true),
                carried: .publicOrigin, strict: false, tunnelOnly: false
            ),
            Connection(carrier: .tunnel, plan: .sticky, readyMs: 90, directTimedOut: true)
        )
        // A race the direct leg won, and the listener gave the connection
        // up all the same: nothing carried it, and no time is kept for it.
        XCTAssertEqual(
            Connection(record: record(.ready(120), .cancelled, .direct), carried: nil, strict: false, tunnelOnly: false),
            Connection(carrier: .none, plan: .raced)
        )
        // Strict, and the direct leg failed: nothing carried it.
        XCTAssertEqual(
            Connection(record: record(.failed, .none, nil), carried: nil, strict: true, tunnelOnly: false),
            Connection(carrier: .none, plan: .strict, directFailed: true)
        )
    }

    func testAPathIsNamedInTheInstancesWords() {
        XCTAssertEqual(DirectPathUse.Path(kind: nil, local: nil), DirectPathUse.Path.none)
        XCTAssertEqual(DirectPathUse.Path(kind: .relay, local: nil), .relay)
        XCTAssertEqual(DirectPathUse.Path(kind: .ipv4, local: true), .lan)
        XCTAssertEqual(DirectPathUse.Path(kind: .ipv6, local: false), .punched)
        // An address path whose class is not known yet is never called LAN.
        XCTAssertEqual(DirectPathUse.Path(kind: .ipv4, local: nil), .punched)
    }

    func testAMinuteIsReportedOnceItIsOverWithExactlyTheInstancesKeys() throws {
        let clock = DirectPathUseTestClock()
        let use = DirectPathUse(seconds: { clock.now }, app: nil)
        use.connection(Connection(carrier: .direct, plan: .raced, readyMs: 120), scope: scope, path: .relay)
        use.connection(Connection(carrier: .tunnel, plan: .raced, readyMs: 80), scope: scope, path: .relay)
        use.connection(Connection(carrier: .tunnel, plan: .raced, readyMs: 60), scope: scope, path: .relay)
        use.connection(Connection(carrier: .tunnel, plan: .sticky, readyMs: 90, directTimedOut: true), scope: scope, path: .relay)
        use.connection(Connection(carrier: .none, plan: .strict, directFailed: true), scope: scope, path: .relay)
        use.connection(Connection(carrier: .tunnel, plan: .noDirect, readyMs: 30), scope: scope, path: .none)
        use.handover(refused: .otherDocuments, scope: scope, path: .relay)
        use.handover(refused: .otherDocuments, scope: scope, path: .relay)
        use.handover(refused: .notUsable, scope: scope, path: .relay)
        use.handover(refused: nil, scope: scope, path: .relay)
        use.retired(.background, scope: scope, path: .relay)
        // Nothing while the minute is still counting.
        XCTAssertEqual(use.pending(for: scope), [])

        clock.now += 60
        use.connection(Connection(carrier: .direct, plan: .raced, readyMs: 10), scope: scope, path: .lan)
        let first = use.pending(for: scope)
        XCTAssertEqual(first.map(\.path), [.none, .relay])
        XCTAssertEqual(
            first[1].json,
            "{\"client\":\"ios\",\"path\":\"relay\",\"handover_rule\":\"other_documents\",\"retired_rule\":\"background\""
                + ",\"age_min\":1,\"direct\":1,\"tunnel\":3,\"raced\":3,\"direct_won\":1,\"tunnel_won\":2"
                + ",\"no_direct\":0,\"sticky\":1,\"strict\":1,\"tunnel_only\":0,\"direct_failed\":1,\"direct_timed_out\":1"
                + ",\"direct_ms\":120,\"tunnel_ms\":80"
                + ",\"handover_asked\":4,\"handover_done\":1,\"handover_refused\":3,\"retired\":1}"
        )
        // Exactly the keys the instance requires (`_PATH_USE_KEYS` in
        // vibe/direct_rendezvous.py): one more or one fewer and it ignores
        // the whole report.
        XCTAssertEqual(
            Set(try object(first[0]).keys),
            [
                "client", "age_min", "path", "handover_rule", "retired_rule",
                "direct", "tunnel", "raced", "direct_won", "tunnel_won", "no_direct", "sticky", "strict",
                "tunnel_only", "direct_failed", "direct_timed_out", "handover_asked", "handover_done",
                "handover_refused", "retired", "direct_ms", "tunnel_ms",
            ]
        )
        XCTAssertEqual(try object(first[0])["no_direct"] as? Int, 1)
        XCTAssertEqual(try object(first[0])["handover_rule"] as? String, "none")

        // Asking changed nothing: a request that failed reports them again,
        // older by then.
        clock.now += 120
        let again = use.pending(for: scope)
        XCTAssertEqual(again.map(\.path), [.none, .relay, .lan])
        XCTAssertEqual(try object(again[1])["age_min"] as? Int, 3)
        // Answered: gone, and the others stay.
        use.answered(first)
        XCTAssertEqual(use.pending(for: scope).map(\.path), [.lan])
        use.answered(use.pending(for: scope))
        XCTAssertEqual(use.pending(for: scope), [])
    }

    /// The App's build rides each entry as `app` when the instance would
    /// accept it (1–32 printable ASCII characters), escaped for JSON; an
    /// unknown or unacceptable build sends no `app` at all.
    func testEachEntryCarriesTheAppsBuildWhenItIsAcceptable() throws {
        let clock = DirectPathUseTestClock()
        let use = DirectPathUse(seconds: { clock.now }, app: "57.1")
        use.connection(Connection(carrier: .direct, plan: .raced, readyMs: 12), scope: scope, path: .lan)
        clock.now += 60
        let entry = try object(XCTUnwrap(use.pending(for: scope).first))
        XCTAssertEqual(entry["app"] as? String, "57.1")
        XCTAssertEqual(entry.count, 23)
        XCTAssertTrue(try XCTUnwrap(use.pending(for: scope).first).json.hasSuffix(",\"retired\":0,\"app\":\"57.1\"}"))

        let quoted = DirectPathUse(seconds: { clock.now }, app: #"a"b\c"#)
        quoted.connection(Connection(carrier: .tunnel, plan: .noDirect, readyMs: 3), scope: scope, path: .none)
        clock.now += 60
        XCTAssertEqual(try object(XCTUnwrap(quoted.pending(for: scope).first))["app"] as? String, #"a"b\c"#)

        XCTAssertEqual(DirectPathUse.acceptedBuild("3.2.9-rc.4"), "3.2.9-rc.4")
        XCTAssertEqual(DirectPathUse.acceptedBuild(String(repeating: "9", count: 32))?.count, 32)
        XCTAssertNil(DirectPathUse.acceptedBuild(""))
        XCTAssertNil(DirectPathUse.acceptedBuild(String(repeating: "9", count: 33)))
        XCTAssertNil(DirectPathUse.acceptedBuild("57.1\n"))
        XCTAssertNil(DirectPathUse.acceptedBuild("版本"))
        XCTAssertNil(DirectPathUse(app: "").app)
        XCTAssertNil(DirectPathUse(app: nil).app)
    }

    func testCountsStayWithTheirOwnInstanceAndAccount() throws {
        let clock = DirectPathUseTestClock()
        let use = DirectPathUse(seconds: { clock.now }, app: nil)
        let other = DirectPathUse.Scope(store: scope.store, host: "instance-two.example")
        let nextAccount = DirectPathUse.Scope(store: UUID(), host: scope.host)
        use.connection(Connection(carrier: .direct, plan: .raced, readyMs: 20), scope: scope, path: .lan)
        use.connection(Connection(carrier: .tunnel, plan: .tunnelOnly, readyMs: 90), scope: other, path: .none)
        use.handover(refused: .notUsable, scope: other, path: .none)
        clock.now += 60
        // A request to one instance carries that instance's minutes only.
        let mine = use.pending(for: scope)
        XCTAssertEqual(mine.map(\.path), [.lan])
        XCTAssertEqual(try object(mine[0])["tunnel_only"] as? Int, 0)
        let theirs = use.pending(for: other)
        XCTAssertEqual(theirs.map(\.path), [DirectPathUse.Path.none])
        XCTAssertEqual(try object(theirs[0])["tunnel_only"] as? Int, 1)
        XCTAssertEqual(try object(theirs[0])["handover_refused"] as? Int, 1)
        // The same host under another account's store has none of them.
        XCTAssertEqual(use.pending(for: nextAccount), [])
        // Answering one instance's leaves the other's.
        use.answered(mine)
        XCTAssertEqual(use.pending(for: scope), [])
        XCTAssertEqual(use.pending(for: other).count, 1)
        // An account change drops everything.
        use.removeAll()
        XCTAssertEqual(use.pending(for: other), [])
    }

    /// A hand-over's own record: what it closed and kept, how long it took
    /// and what refused it, in exactly the seven keys the instance accepts
    /// (`log_handovers` in vibe/direct_rendezvous.py), oldest first.
    func testEachHandoverIsRecordedWithExactlyTheInstancesKeys() throws {
        let clock = DirectPathUseTestClock()
        let use = DirectPathUse(seconds: { clock.now }, app: nil)
        XCTAssertEqual(use.pendingHandovers(for: scope).json, [])

        // Taken, and it moved part: one closed, two busy ones kept, after
        // 2.4 s of waiting for them to go quiet.
        use.handoverAnswered(
            .done(closed: 1, kept: 2), refusedBy: .sole, took: .milliseconds(2_400), scope: scope, path: .punched
        )
        clock.now += 7
        // Refused for a voice take, at once.
        use.handoverAnswered(
            .skipped(.otherDocuments), refusedBy: .voice, took: .microseconds(300), scope: scope, path: .lan
        )
        // Refused because the path was not usable: no document is named,
        // whatever the caller passed.
        use.handoverAnswered(.skipped(.notUsable), refusedBy: .runtime, took: .zero, scope: scope, path: .none)
        clock.now += 3

        let report = use.pendingHandovers(for: scope)
        XCTAssertEqual(report.json, [
            #"{"age_s":12,"ms":2400,"refused":"none","other":"none","closed":1,"kept":2,"path":"punched"}"#,
            #"{"age_s":3,"ms":0,"refused":"other_documents","other":"voice","closed":0,"kept":0,"path":"lan"}"#,
            #"{"age_s":3,"ms":0,"refused":"not_usable","other":"none","closed":0,"kept":0,"path":"none"}"#,
        ])
        for entry in report.json {
            let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(entry.utf8)) as? [String: Any])
            XCTAssertEqual(Set(object.keys), ["age_s", "ms", "refused", "other", "closed", "kept", "path"])
        }
        // Asking changes nothing; an answered request ends them.
        XCTAssertEqual(use.pendingHandovers(for: scope).json, report.json)
        use.answered(report)
        XCTAssertEqual(use.pendingHandovers(for: scope).json, [])

        // The oldest ask is first, also when it was answered last: a window
        // was refused at once while the page's own hand-over, asked three
        // seconds before, still waited for its connections to go quiet.
        use.handoverAnswered(.skipped(.otherDocuments), refusedBy: .runtime, took: .zero, scope: scope, path: .lan)
        clock.now += 1
        use.handoverAnswered(.done(closed: 2, kept: 0), refusedBy: .sole, took: .seconds(3), scope: scope, path: .lan)
        XCTAssertEqual(use.pendingHandovers(for: scope).json, [
            #"{"age_s":3,"ms":3000,"refused":"none","other":"none","closed":2,"kept":0,"path":"lan"}"#,
            #"{"age_s":1,"ms":0,"refused":"other_documents","other":"runtime","closed":0,"kept":0,"path":"lan"}"#,
        ])
    }

    /// The instance accepts a document's kind with `other_documents` and
    /// only with it, and ignores the whole list otherwise: a refusal for
    /// another document always names one, `unknown` when the App could not
    /// tell, and every kind is one of the instance's words.
    func testARefusalForAnotherDocumentAlwaysNamesOne() throws {
        let clock = DirectPathUseTestClock()
        let use = DirectPathUse(seconds: { clock.now }, app: nil)
        let kinds: [DirectOtherDocument] = [.sole, .voice, .child, .runtime, .unloaded, .unknown]
        for kind in kinds {
            use.handoverAnswered(.skipped(.otherDocuments), refusedBy: kind, took: .zero, scope: scope, path: .relay)
        }
        let sent = try use.pendingHandovers(for: scope).json.map {
            try XCTUnwrap((JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any])?["other"] as? String)
        }
        XCTAssertEqual(sent, ["unknown", "voice", "child", "runtime", "unloaded", "unknown"])
    }

    func testHandoversStayWithTheirOwnInstanceAndAreBounded() throws {
        let clock = DirectPathUseTestClock()
        let use = DirectPathUse(seconds: { clock.now }, app: nil)
        let other = DirectPathUse.Scope(store: scope.store, host: "instance-two.example")
        func closed(_ report: DirectPathUse.HandoverReport) throws -> [Int] {
            try report.json.map {
                try XCTUnwrap((JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any])?["closed"] as? Int)
            }
        }
        // Many for one instance: its newest eight are kept, oldest first,
        // and they push out nothing of another instance's.
        use.handoverAnswered(.done(closed: 100, kept: 0), refusedBy: .sole, took: .zero, scope: other, path: .lan)
        for index in 0..<40 {
            use.handoverAnswered(.done(closed: index, kept: 0), refusedBy: .sole, took: .zero, scope: scope, path: .lan)
        }
        XCTAssertEqual(try closed(use.pendingHandovers(for: scope)), Array(32..<40))
        // Another instance's request carries only its own, and answering it
        // ends only its own.
        let theirs = use.pendingHandovers(for: other)
        XCTAssertEqual(try closed(theirs), [100])
        use.answered(theirs)
        XCTAssertEqual(use.pendingHandovers(for: other).json, [])
        XCTAssertEqual(use.pendingHandovers(for: scope).json.count, DirectPathUse.handoverLimit)

        // Over every instance no more than `handoversKept`: the oldest go.
        for index in 0..<5 {
            let scope = DirectPathUse.Scope(store: scope.store, host: "instance-\(index).example")
            for _ in 0..<DirectPathUse.handoverLimit {
                use.handoverAnswered(.skipped(.notUsable), refusedBy: .sole, took: .zero, scope: scope, path: .none)
            }
        }
        XCTAssertEqual(use.pendingHandovers(for: scope).json, [])
        XCTAssertEqual(
            (0..<5).map { index in
                use.pendingHandovers(for: DirectPathUse.Scope(store: scope.store, host: "instance-\(index).example")).json.count
            },
            [0, 8, 8, 8, 8]
        )

        // The age is from the ask, never more than the instance accepts,
        // and one older than that is dropped unsent.
        let late = DirectPathUse.Scope(store: scope.store, host: "instance-late.example")
        use.handoverAnswered(.done(closed: 0, kept: 1), refusedBy: .sole, took: .seconds(3), scope: late, path: .lan)
        clock.now += DirectPathUse.maxHandoverAgeSeconds - 3
        XCTAssertTrue(try XCTUnwrap(use.pendingHandovers(for: late).json.first).hasPrefix(#"{"age_s":86400,"ms":3000,"#))
        clock.now += 1
        XCTAssertEqual(use.pendingHandovers(for: late).json, [])

        // An account change drops them all.
        use.handoverAnswered(.done(closed: 0, kept: 0), refusedBy: .sole, took: .zero, scope: scope, path: .lan)
        use.removeAll()
        XCTAssertEqual(use.pendingHandovers(for: scope).json, [])
    }

    func testWhatIsKeptAndSentIsBounded() throws {
        let clock = DirectPathUseTestClock()
        let use = DirectPathUse(seconds: { clock.now }, app: nil)
        for minute in 0..<40 {
            clock.now += 60
            use.retired(.pathMonitor, scope: scope, path: minute % 2 == 0 ? .lan : .punched)
        }
        clock.now += 60
        // At most ten in one request, the oldest kept first.
        let many = use.pending(for: scope)
        XCTAssertEqual(many.count, DirectPathUse.reportLimit)
        XCTAssertEqual(try object(many[0])["age_min"] as? Int, DirectPathUse.kept)
        XCTAssertEqual(try object(many[0])["retired_rule"] as? String, "path_monitor")
        // At most thirty kept: the oldest ten were dropped.
        var total = 0
        while true {
            let next = use.pending(for: scope)
            if next.isEmpty {
                break
            }
            total += next.count
            use.answered(next)
        }
        XCTAssertEqual(total, DirectPathUse.kept)

        // A minute older than the instance accepts is dropped unsent.
        use.retired(.superseded, scope: scope, path: .relay)
        clock.now += 60 * (DirectPathUse.maxAgeMinutes + 2)
        XCTAssertEqual(use.pending(for: scope), [])

        // A minute's median is of the times kept, and the counts are of all.
        for ms in 0..<100 {
            use.connection(Connection(carrier: .direct, plan: .raced, readyMs: ms), scope: scope, path: .lan)
        }
        clock.now += 60
        let busy = try object(try XCTUnwrap(use.pending(for: scope).first))
        XCTAssertEqual(busy["direct"] as? Int, 100)
        XCTAssertEqual(busy["direct_ms"] as? Int, 15)
        XCTAssertEqual(DirectPathUse.median([]), 0)
        XCTAssertEqual(DirectPathUse.median([9, 1, 5, 7]), 5)
    }
}
