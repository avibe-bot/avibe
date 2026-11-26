import XCTest
@testable import Avibe

/// The App's side of 「仅直连和中继」 and 「线路测速」 that does not need the
/// path controller: the revert rules, the measurement's bound and its
/// answer, and the stored choice.
@MainActor
final class DirectPathSeamsTests: XCTestCase {
    private let start = ContinuousClock.now

    private func at(_ seconds: Double) -> ContinuousClock.Instant {
        start.advanced(by: .milliseconds(Int64(seconds * 1_000)))
    }

    private func seen(
        _ seconds: Double,
        linked: Bool = false,
        drops: UInt64 = 0,
        unlinked: UInt64 = 0,
        listenerDownSince: Double? = nil
    ) -> DirectOnlyRevertRules.Observation {
        DirectOnlyRevertRules.Observation(
            now: at(seconds),
            linked: linked,
            strictFailures: drops,
            unlinkedRendezvous: unlinked,
            listenerDownSince: listenerDownSince.map(at)
        )
    }

    private func bootstrapped(unlinked: UInt64 = 0, linked: Bool = false) -> DirectOnlyRevertRules {
        var rules = DirectOnlyRevertRules()
        rules.beginBootstrap(unlinkedRendezvous: unlinked, linked: linked)
        return rules
    }

    // MARK: Revert rules

    /// a1: two rendezvous the instance answered without a link, counted
    /// from the bootstrap's start (earlier ones in the process do not
    /// count); one is not enough; a link in between keeps the choice.
    func testTwoUnlinkedRendezvousInOneBootstrapRevert() {
        var rules = bootstrapped(unlinked: 5)
        XCTAssertEqual(rules.observe(seen(0, unlinked: 5)), .keep(nextCheck: nil, reloadsForLink: false))
        XCTAssertEqual(rules.observe(seen(1, unlinked: 6)), .keep(nextCheck: nil, reloadsForLink: false))
        XCTAssertEqual(rules.observe(seen(2, unlinked: 7)), .revert)

        var linked = bootstrapped(unlinked: 0)
        XCTAssertEqual(linked.observe(seen(1, linked: true, unlinked: 2)), .keep(nextCheck: nil, reloadsForLink: true))

        // A new bootstrap starts the count again.
        var again = bootstrapped(unlinked: 0)
        _ = again.observe(seen(0, unlinked: 1))
        again.beginBootstrap(unlinkedRendezvous: 1, linked: false)
        XCTAssertEqual(again.observe(seen(1, unlinked: 2)), .keep(nextCheck: nil, reloadsForLink: false))
    }

    /// a2: a fresh page and no link within 20 s of it; the check is asked
    /// for when it is due; a link before then keeps the choice; a new
    /// bootstrap forgets the page.
    func testAFreshPageWithoutALinkFor20SecondsReverts() {
        var rules = bootstrapped()
        rules.noteFreshPage(at: at(3))
        XCTAssertEqual(rules.observe(seen(3)), .keep(nextCheck: at(23), reloadsForLink: false))
        // A second page does not move the start.
        rules.noteFreshPage(at: at(10))
        XCTAssertEqual(rules.observe(seen(22.9)), .keep(nextCheck: at(23), reloadsForLink: false))
        XCTAssertEqual(rules.observe(seen(23)), .revert)

        var linked = bootstrapped()
        linked.noteFreshPage(at: at(0))
        XCTAssertEqual(linked.observe(seen(19, linked: true)), .keep(nextCheck: nil, reloadsForLink: true))
        XCTAssertEqual(linked.observe(seen(40, linked: true)), .keep(nextCheck: nil, reloadsForLink: false))

        // The link came up: the bootstrap succeeded. Its loss later is not
        // a2 (the controller counts it as a drop, a new bootstrap), nor a1.
        var established = bootstrapped()
        established.noteFreshPage(at: at(0))
        XCTAssertEqual(established.observe(seen(1, linked: true)), .keep(nextCheck: nil, reloadsForLink: true))
        XCTAssertEqual(established.observe(seen(30)), .keep(nextCheck: nil, reloadsForLink: false))
        XCTAssertEqual(established.observe(seen(31, unlinked: 2)), .keep(nextCheck: nil, reloadsForLink: false))

        // The view left for a sign-in page: a2's time does not run while it
        // is away, and starts again at the next fresh page.
        var away = bootstrapped()
        away.noteFreshPage(at: at(0))
        away.noteLeftInstancePage()
        XCTAssertEqual(away.observe(seen(45)), .keep(nextCheck: nil, reloadsForLink: false))
        away.noteFreshPage(at: at(50))
        XCTAssertEqual(away.observe(seen(60)), .keep(nextCheck: at(70), reloadsForLink: false))
        XCTAssertEqual(away.observe(seen(70)), .revert)

        var forgotten = bootstrapped()
        forgotten.noteFreshPage(at: at(0))
        forgotten.beginBootstrap(unlinkedRendezvous: 0, linked: false)
        XCTAssertEqual(forgotten.observe(seen(30)), .keep(nextCheck: nil, reloadsForLink: false))
    }

    /// Rule c's clock counts only what the rule counts: not the first
    /// attempt of a launch or a return (only from its second); a failure
    /// while serving in front, from that failure; never `waiting`, which
    /// starts the count over.
    func testTheListenerClockAppliesRuleCsExclusions() {
        var clock = DirectOnlyListenerClock()
        // A failure in front while serving: counts from it.
        XCTAssertEqual(clock.observe(recovery: (since: at(10), failureAt: at(10), waiting: false)), at(10))
        XCTAssertEqual(clock.observe(recovery: (since: at(10), failureAt: at(15), waiting: false)), at(10))
        // Serving again: cleared.
        XCTAssertNil(clock.observe(recovery: nil))
        // A return: its first attempt does not count, its second does.
        XCTAssertNil(clock.observe(recovery: (since: at(100), failureAt: nil, waiting: false)))
        XCTAssertNil(clock.observe(recovery: (since: at(100), failureAt: at(101), waiting: false)))
        XCTAssertEqual(clock.observe(recovery: (since: at(100), failureAt: at(104), waiting: false)), at(104))
        // `waiting` (no network) stops it and starts the count over: the
        // next failure is a first again.
        XCTAssertNil(clock.observe(recovery: (since: at(100), failureAt: at(110), waiting: true)))
        XCTAssertNil(clock.observe(recovery: (since: at(100), failureAt: at(115), waiting: false)))
        XCTAssertEqual(clock.observe(recovery: (since: at(100), failureAt: at(120), waiting: false)), at(120))
    }

    /// A direct-only launch whose listener never serves (the deferred first
    /// route keeps failing) is not held for good: c counts from its second
    /// failed attempt, and the rules revert 60 s after it.
    func testALaunchWhoseListenerNeverServesRevertsAfterSixtySeconds() {
        var clock = DirectOnlyListenerClock()
        var rules = bootstrapped()
        XCTAssertNil(clock.observe(recovery: (since: at(0), failureAt: at(1), waiting: false)))
        let since = clock.observe(recovery: (since: at(0), failureAt: at(4), waiting: false))
        XCTAssertEqual(since, at(4))
        XCTAssertEqual(
            rules.observe(seen(30, listenerDownSince: 4)),
            .keep(nextCheck: at(64), reloadsForLink: false)
        )
        XCTAssertEqual(clock.observe(recovery: (since: at(0), failureAt: at(50), waiting: false)), at(4))
        XCTAssertEqual(rules.observe(seen(64, listenerDownSince: 4)), .revert)
    }

    /// b: three drops within 10 minutes arm it, and it is made only at the
    /// next evidence that the instance answers. Without that evidence (no
    /// network) the choice stays; drops spread over more than 10 minutes do
    /// not arm it.
    func testThreeDropsInTenMinutesRevertAtTheNextEvidence() {
        var rules = bootstrapped()
        XCTAssertEqual(rules.observe(seen(0, linked: true)), .keep(nextCheck: nil, reloadsForLink: true))
        _ = rules.observe(seen(60, drops: 1))
        _ = rules.observe(seen(120, drops: 2))
        // The third drop: armed, but nothing has answered since.
        XCTAssertEqual(rules.observe(seen(180, drops: 3)), .keep(nextCheck: nil, reloadsForLink: false))
        XCTAssertEqual(rules.observe(seen(900, drops: 3)), .keep(nextCheck: nil, reloadsForLink: false))
        // The instance answers a rendezvous.
        XCTAssertEqual(rules.observe(seen(901, drops: 3, unlinked: 1)), .revert)

        var byPage = bootstrapped()
        _ = byPage.observe(seen(0, drops: 0))
        _ = byPage.observe(seen(1, drops: 3))
        byPage.noteFreshPage(at: at(2))
        XCTAssertEqual(byPage.observe(seen(2, drops: 3)), .revert)

        var byLink = bootstrapped()
        _ = byLink.observe(seen(0, drops: 0))
        _ = byLink.observe(seen(1, drops: 3))
        XCTAssertEqual(byLink.observe(seen(2, linked: true, drops: 3)), .revert)

        var spread = bootstrapped()
        _ = spread.observe(seen(0, drops: 0))
        _ = spread.observe(seen(0, drops: 1))
        _ = spread.observe(seen(400, drops: 2))
        _ = spread.observe(seen(700, drops: 3))
        XCTAssertEqual(spread.observe(seen(701, linked: true, drops: 3)), .keep(nextCheck: nil, reloadsForLink: true))
    }

    /// A drop is a new bootstrap: the unlinked count starts again from it,
    /// and the link's return asks for one more reload.
    func testADropBeginsANewBootstrap() {
        var rules = bootstrapped()
        _ = rules.observe(seen(0, linked: true))
        _ = rules.observe(seen(1, unlinked: 1))
        // The drop, with one more unlinked answer since the open: a1 would
        // fire without the new bootstrap.
        XCTAssertEqual(rules.observe(seen(2, drops: 1, unlinked: 2)), .keep(nextCheck: nil, reloadsForLink: false))
        XCTAssertEqual(rules.observe(seen(3, linked: true, drops: 1, unlinked: 2)), .keep(nextCheck: nil, reloadsForLink: true))
    }

    /// c: 60 s from the first failed attempt that counts (the caller leaves
    /// out the first attempt, `waiting`, the background and anything before
    /// a success by passing no instant); due time asked for. Not before.
    func testTheListenerDownFor60SecondsReverts() {
        var rules = bootstrapped()
        XCTAssertEqual(
            rules.observe(seen(10, listenerDownSince: 5)),
            .keep(nextCheck: at(65), reloadsForLink: false)
        )
        XCTAssertEqual(
            rules.observe(seen(64.9, listenerDownSince: 5)),
            .keep(nextCheck: at(65), reloadsForLink: false)
        )
        XCTAssertEqual(rules.observe(seen(65, listenerDownSince: 5)), .revert)

        // Reset: no instant, then a new one.
        var reset = bootstrapped()
        _ = reset.observe(seen(50, listenerDownSince: 0))
        _ = reset.observe(seen(55))
        XCTAssertEqual(
            reset.observe(seen(70, listenerDownSince: 60)),
            .keep(nextCheck: at(120), reloadsForLink: false)
        )
    }

    /// Nothing reverts on time alone: no evidence, no listener failure, no
    /// matter how long.
    func testNothingRevertsWithoutEvidence() {
        var rules = bootstrapped()
        for second in stride(from: 0.0, through: 3_600, by: 30) {
            XCTAssertEqual(rules.observe(seen(second)), .keep(nextCheck: nil, reloadsForLink: false))
        }
    }

    // MARK: Measurement

    /// The controller's answer passes through; a second call while one runs
    /// is busy at once, and the first still gets its answer; the
    /// controller's own busy passes through.
    func testTheMeasurementRunsOneAtATime() async {
        let measuring = HeldMeasuring()
        let runner = DirectMeasureRunner(measuring: measuring)
        let url = URL(string: "https://instance-one.example")!
        let first = Task { await runner.measure(forInstanceURL: url) }
        await measuring.waitForCall()
        let second = await runner.measure(forInstanceURL: url)
        XCTAssertEqual(second, .busy)
        XCTAssertEqual(measuring.calls, [url])
        measuring.answer(.measured(Self.measurement))
        let answered = await first.value
        XCTAssertEqual(answered, .measured(Self.measurement))

        let busy = Task { await runner.measure(forInstanceURL: url) }
        await measuring.waitForCall(count: 2)
        measuring.answer(.busy)
        let busyAnswer = await busy.value
        XCTAssertEqual(busyAnswer, .busy)
    }

    /// A controller that never answers: the App answers at its bound, and
    /// the next call is accepted.
    func testTheMeasurementIsBoundedByTheApp() async {
        XCTAssertEqual(DirectMeasureRunner.bound, .seconds(35))
        let measuring = HeldMeasuring()
        let runner = DirectMeasureRunner(measuring: measuring, bound: .milliseconds(50))
        let url = URL(string: "https://instance-one.example")!
        let timedOut = await runner.measure(forInstanceURL: url)
        XCTAssertEqual(timedOut, .timedOut)
        // A late answer changes nothing.
        measuring.answer(.measured(Self.measurement))
        let next = Task { await runner.measure(forInstanceURL: url) }
        await measuring.waitForCall(count: 2)
        measuring.answer(.measured(Self.measurement))
        let nextAnswer = await next.value
        XCTAssertEqual(nextAnswer, .measured(Self.measurement))
    }

    /// The answer's shape, as the protocol has it: every key present, nulls
    /// where there is nothing.
    func testTheMeasurementAnswersTheAgreedShape() throws {
        let object = Self.measurement.jsonObject
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        XCTAssertEqual(
            String(decoding: data, as: UTF8.self),
            #"{"basis":"warm-https-health","current":{"error":null,"kind":"relay","maxMs":61,"medianMs":48,"ok":5,"relayRegion":"tokyo","scope":null},"measuredAt":1700000000000,"samples":5,"tunnel":{"error":"timeout","kind":"tunnel","maxMs":null,"medianMs":null,"ok":0,"relayRegion":null,"scope":null}}"#
        )
        let none = DirectMeasurement(measuredAt: Date(timeIntervalSince1970: 1), current: nil, tunnel: nil)
        let noneData = try JSONSerialization.data(withJSONObject: none.jsonObject, options: [.sortedKeys])
        XCTAssertEqual(
            String(decoding: noneData, as: UTF8.self),
            #"{"basis":"warm-https-health","current":null,"measuredAt":1000,"samples":5,"tunnel":null}"#
        )
        let direct = DirectMeasureLeg(
            kind: .ipv6, scope: .global, relayRegion: nil, ok: 3, medianMs: 12, maxMs: 30, error: .failed
        )
        let directData = try JSONSerialization.data(withJSONObject: direct.jsonObject, options: [.sortedKeys])
        XCTAssertEqual(
            String(decoding: directData, as: UTF8.self),
            #"{"error":"failed","kind":"ipv6","maxMs":30,"medianMs":12,"ok":3,"relayRegion":null,"scope":"global"}"#
        )
    }

    /// The controller's legs (`DirectMeasure.Leg`) as the bridge answers
    /// them: the same words, milliseconds rounded to whole ones.
    func testTheControllersLegsMapToTheBridgesShape() {
        XCTAssertEqual(
            DirectMeasureLeg(
                DirectMeasure.Leg(
                    kind: .relay, scope: nil, relayRegion: "tokyo", ok: 5,
                    medianMs: 47.6, maxMs: 61.2, error: nil
                )
            ),
            DirectMeasureLeg(
                kind: .relay, scope: nil, relayRegion: "tokyo", ok: 5, medianMs: 48, maxMs: 61, error: nil
            )
        )
        // The relay row's region reaches the page's result.
        XCTAssertEqual(
            DirectMeasureLeg(
                DirectMeasure.Leg(
                    kind: .relay, scope: nil, relayRegion: "tokyo", ok: 5,
                    medianMs: 47.6, maxMs: 61.2, error: nil
                )
            ).jsonObject["relayRegion"] as? String,
            "tokyo"
        )
        XCTAssertEqual(
            DirectMeasureLeg(
                DirectMeasure.Leg(
                    kind: .ipv6, scope: .global, relayRegion: nil, ok: 3,
                    medianMs: 12.49, maxMs: .infinity, error: .timeout
                )
            ),
            DirectMeasureLeg(
                kind: .ipv6, scope: .global, relayRegion: nil, ok: 3, medianMs: 12, maxMs: nil, error: .timeout
            )
        )
        XCTAssertEqual(
            DirectMeasureLeg(
                DirectMeasure.Leg(
                    kind: .tunnel, scope: nil, relayRegion: nil, ok: 0,
                    medianMs: nil, maxMs: nil, error: .failed
                )
            ).jsonObject["kind"] as? String,
            "tunnel"
        )
    }

    static let measurement = DirectMeasurement(
        measuredAt: Date(timeIntervalSince1970: 1_700_000_000),
        current: DirectMeasureLeg(
            kind: .relay, scope: nil, relayRegion: "tokyo", ok: 5, medianMs: 48, maxMs: 61, error: nil
        ),
        tunnel: DirectMeasureLeg(
            kind: .tunnel, scope: nil, relayRegion: nil, ok: 0, medianMs: nil, maxMs: nil, error: .timeout
        )
    )

    // MARK: The stored choice

    /// `direct` and a reversion's `from` are kept; an entry stored before
    /// `from` existed reads as reverted from the tunnel; `pendingRelaunch`
    /// is never stored.
    func testTheStoredChoiceKeepsDirectAndReadsAnOldReversionAsFromTheTunnel() throws {
        let suite = "DirectPathSeams.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = DirectPathPreferenceStore(defaults: defaults)
        var pending = DirectPathPreference(mode: .direct)
        pending.pendingRelaunch = true
        store.set(pending, accountID: "a", origin: "https://one.example")
        XCTAssertEqual(
            store.preference(accountID: "a", origin: "https://one.example"),
            DirectPathPreference(mode: .direct)
        )
        let stored = try XCTUnwrap(
            (defaults.dictionary(forKey: DirectPathPreferenceStore.key) as? [String: Data])?["a https://one.example"]
        )
        XCTAssertFalse(String(decoding: stored, as: UTF8.self).contains("pendingRelaunch"))

        let fromDirect = DirectPathPreference(
            mode: .automatic,
            reverted: .init(at: Date(timeIntervalSince1970: 1_700_000_000), reason: .unreachable, from: .direct)
        )
        store.set(fromDirect, accountID: "a", origin: "https://two.example")
        XCTAssertEqual(store.preference(accountID: "a", origin: "https://two.example"), fromDirect)

        // As 54.1 stored it: no `from`.
        let old = try JSONEncoder().encode(
            DirectPathPreference(
                mode: .automatic,
                reverted: .init(at: Date(timeIntervalSince1970: 1_700_000_000), reason: .unreachable)
            )
        )
        var legacy = try XCTUnwrap(JSONSerialization.jsonObject(with: old) as? [String: Any])
        var reverted = try XCTUnwrap(legacy["reverted"] as? [String: Any])
        reverted.removeValue(forKey: "from")
        legacy["reverted"] = reverted
        defaults.set(
            ["a https://three.example": try JSONSerialization.data(withJSONObject: legacy)],
            forKey: DirectPathPreferenceStore.key
        )
        let read = store.preference(accountID: "a", origin: "https://three.example")
        XCTAssertEqual(read.mode, .automatic)
        XCTAssertEqual(read.reverted?.from, .tunnel)
        XCTAssertEqual(read.reverted?.reason, .unreachable)
    }
}

/// A controller's measurement that answers only when told.
@MainActor
private final class HeldMeasuring: DirectPathMeasuring {
    private(set) var calls: [URL] = []
    private var pending: [CheckedContinuation<DirectMeasureResult, Never>] = []
    private var callWaiters: [(Int, CheckedContinuation<Void, Never>)] = []

    func measurePaths(forInstanceURL url: URL) async -> DirectMeasureResult {
        calls.append(url)
        let ready = callWaiters.filter { calls.count >= $0.0 }
        callWaiters.removeAll { calls.count >= $0.0 }
        ready.forEach { $0.1.resume() }
        return await withCheckedContinuation { pending.append($0) }
    }

    func waitForCall(count: Int = 1) async {
        if calls.count >= count {
            return
        }
        await withCheckedContinuation { callWaiters.append((count, $0)) }
    }

    /// Answers the oldest call still waiting, if any.
    func answer(_ result: DirectMeasureResult) {
        guard !pending.isEmpty else {
            return
        }
        pending.removeFirst().resume(returning: result)
    }
}
