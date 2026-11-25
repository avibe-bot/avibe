import Network
import XCTest
@testable import Avibe

/// 「线路测速」's transport (`DirectMeasure.swift`): the sampling rules as
/// data, the measurement's own port against real sockets with a stand-in
/// for the App's link, and the two sessions against a local server.
///
/// What cannot run here is the whole path through URLSession: TLS to an
/// instance through the port. The test target has no TLS server identity,
/// and the system sends a request for a loopback address round a route, so
/// a local server cannot stand in for the instance behind the port. That
/// path was measured by hand on macOS against a public host (the PR has the
/// result); here its parts are held separately.
final class DirectMeasureTests: XCTestCase {
    private static let host = "one-app.avibe.bot"

    // MARK: Sampling

    private func farEnd(_ limits: DirectMeasure.Limits = .standard) -> ContinuousClock.Instant {
        ContinuousClock.now.advanced(by: .seconds(limits.totalSeconds))
    }

    /// Runs the sampling with `answers[index]` as each request's result;
    /// returns the result and the indexes that were asked for.
    private func sampled(
        limits: DirectMeasure.Limits = .standard,
        unchanged: @escaping @Sendable (Int, Int) -> Bool = { _, _ in true },
        _ answer: @escaping @Sendable (Int) async throws -> DirectMeasure.Sample
    ) async -> (DirectMeasure.SampleResult, [Int]) {
        let asked = MeasureLog<Int>()
        let checks = MeasureCounter()
        let result = await DirectMeasure.sample(
            limits: limits,
            end: farEnd(limits),
            unchanged: { index in unchanged(index, checks.next()) }
        ) { index in
            asked.add(index)
            return try await answer(index)
        }
        return (result, asked.values)
    }

    private func good(_ milliseconds: Int) -> DirectMeasure.Sample {
        DirectMeasure.Sample(elapsed: .milliseconds(milliseconds), healthy: true, counts: true)
    }

    func testOneWarmUpThenFiveTimedRequestsAreTheLeg() async {
        // The warm-up is slow, as a first request is, and is not timed.
        let (result, asked) = await sampled { index in
            index == 0 ? self.good(900) : self.good(10 * index)
        }
        XCTAssertEqual(asked, [0, 1, 2, 3, 4, 5])
        XCTAssertEqual(result.times, [10, 20, 30, 40, 50].map { Duration.milliseconds($0) })
        XCTAssertNil(result.error)
        let leg = DirectMeasure.leg(.relay, scope: nil, relayRegion: "euc1", result)
        XCTAssertEqual(
            leg,
            DirectMeasure.Leg(
                kind: .relay, scope: nil, relayRegion: "euc1", ok: 5, medianMs: 30, maxMs: 50, error: nil
            )
        )
    }

    func testAnUnhealthyAnswerIsTheLegsErrorAndTheRestGoOn() async {
        // Answered, but not well: not timed, and the first of them is the
        // error. The warm-up's own health is not asked.
        let (result, asked) = await sampled { index in
            switch index {
            case 0, 2, 4:
                return DirectMeasure.Sample(elapsed: .milliseconds(5), healthy: false, counts: true)
            default:
                return self.good(10 * index)
            }
        }
        XCTAssertEqual(asked, [0, 1, 2, 3, 4, 5])
        XCTAssertEqual(result.times, [10, 30, 50].map { Duration.milliseconds($0) })
        XCTAssertEqual(result.error, .failed)
        let leg = DirectMeasure.leg(.tunnel, scope: nil, relayRegion: nil, result)
        XCTAssertEqual(leg.ok, 3)
        XCTAssertEqual(leg.medianMs, 30)
        XCTAssertEqual(leg.maxMs, 50)
        XCTAssertEqual(leg.error, .failed)
    }

    func testARequestOnAnotherConnectionEndsTheLegWithWhatWasTimedBeforeIt() async {
        // The third timed request was sent again on a new connection, or
        // went round the port: `counts` is false. It is not timed, and
        // nothing after it is asked, since it would run on that connection.
        let (result, asked) = await sampled { index in
            index == 3
                ? DirectMeasure.Sample(elapsed: .milliseconds(1), healthy: true, counts: false)
                : self.good(10 * index)
        }
        XCTAssertEqual(asked, [0, 1, 2, 3])
        XCTAssertEqual(result.times, [10, 20].map { Duration.milliseconds($0) })
        XCTAssertEqual(result.error, .failed)
        // A warm-up that did not go the leg's way: nothing is timed at all.
        let (none, onlyWarmUp) = await sampled { _ in
            DirectMeasure.Sample(elapsed: .milliseconds(1), healthy: true, counts: false)
        }
        XCTAssertEqual(onlyWarmUp, [0])
        XCTAssertEqual(none, DirectMeasure.SampleResult(times: [], error: .failed))
        XCTAssertEqual(DirectMeasure.leg(.ipv4, scope: .local, relayRegion: nil, none).ok, 0)
        XCTAssertNil(DirectMeasure.leg(.ipv4, scope: .local, relayRegion: nil, none).medianMs)
    }

    func testAChangeOfThePathEndsTheLegBeforeAndAfterARequest() async {
        // `unchanged` is asked before and after every request: checks 0 and
        // 1 are the warm-up's, 2 and 3 the first timed request's, and so on.
        // Changed before the third timed request: it is never made.
        let (before, askedBefore) = await sampled(unchanged: { _, check in check < 6 }) { index in
            self.good(10 * index)
        }
        XCTAssertEqual(askedBefore, [0, 1, 2])
        XCTAssertEqual(before.times, [10, 20].map { Duration.milliseconds($0) })
        XCTAssertEqual(before.error, .failed)
        // Changed while the third timed request ran: it was made and
        // answered, and is not timed.
        let (during, askedDuring) = await sampled(unchanged: { _, check in check < 7 }) { index in
            self.good(10 * index)
        }
        XCTAssertEqual(askedDuring, [0, 1, 2, 3])
        XCTAssertEqual(during.times, [10, 20].map { Duration.milliseconds($0) })
        XCTAssertEqual(during.error, .failed)
        // No path at all when it began.
        let (never, askedNever) = await sampled(unchanged: { _, _ in false }) { _ in self.good(1) }
        XCTAssertEqual(askedNever, [])
        XCTAssertEqual(never, DirectMeasure.SampleResult(times: [], error: .failed))
    }

    func testARequestThatFailsEndsTheLegAndTheFirstErrorIsKept() async {
        struct Refused: Error {}
        let (result, asked) = await sampled { index in
            if index == 4 {
                throw Refused()
            }
            return self.good(10 * index)
        }
        XCTAssertEqual(asked, [0, 1, 2, 3, 4])
        XCTAssertEqual(result.times.count, 3)
        XCTAssertEqual(result.error, .failed)
    }

    func testARequestSlowerThanItsBoundEndsTheLegAsTimeout() async {
        var limits = DirectMeasure.Limits.standard
        limits.sampleSeconds = 0.2
        let began = ContinuousClock.now
        let (result, asked) = await sampled(limits: limits) { index in
            if index == 3 {
                // It would answer, long after its bound.
                try await Task.sleep(for: .seconds(20))
            }
            return self.good(10 * index)
        }
        XCTAssertEqual(asked, [0, 1, 2, 3])
        XCTAssertEqual(result.times, [10, 20].map { Duration.milliseconds($0) })
        XCTAssertEqual(result.error, .timeout)
        // Far below the 20 s the request would have taken.
        XCTAssertLessThan(began.duration(to: .now), .seconds(15))
        // An unhealthy answer before it is the first error, and stays it.
        let (mixed, _) = await sampled(limits: limits) { index in
            if index == 1 {
                return DirectMeasure.Sample(elapsed: .milliseconds(1), healthy: false, counts: true)
            }
            if index == 3 {
                try await Task.sleep(for: .seconds(20))
            }
            return self.good(10 * index)
        }
        XCTAssertEqual(mixed.times, [20].map { Duration.milliseconds($0) })
        XCTAssertEqual(mixed.error, .failed)
    }

    func testTheWholeMeasurementsBoundEndsALegWhateverItsRequestsDo() async {
        // The App's bounds: five seconds a request, thirty for everything.
        XCTAssertEqual(DirectMeasure.Limits.standard.sampleSeconds, 5)
        XCTAssertEqual(DirectMeasure.Limits.standard.totalSeconds, 30)
        XCTAssertEqual(DirectMeasure.samples, 5)
        XCTAssertEqual(DirectMeasure.basis, "warm-https-health")

        // Each request is within its own bound; together they are not
        // within the measurement's. The leg ends at that bound.
        var limits = DirectMeasure.Limits.standard
        limits.sampleSeconds = 5
        limits.totalSeconds = 0.7
        let began = ContinuousClock.now
        let (result, asked) = await sampled(limits: limits) { index in
            try await Task.sleep(for: .milliseconds(300))
            return self.good(10 * index)
        }
        let took = began.duration(to: .now)
        XCTAssertEqual(result.error, .timeout)
        XCTAssertLessThan(asked.count, 4)
        XCTAssertLessThan(result.times.count, 3)
        XCTAssertGreaterThanOrEqual(took, .milliseconds(650))
        XCTAssertLessThan(took, .seconds(10))

        // A request that does not end when it is cancelled is not waited
        // for past the bound either.
        limits.totalSeconds = 0.3
        let stubborn = ContinuousClock.now
        let held = MeasureGate()
        let (stuck, _) = await sampled(limits: limits) { _ in
            await held.wait()
            return self.good(1)
        }
        XCTAssertEqual(stuck, DirectMeasure.SampleResult(times: [], error: .timeout))
        XCTAssertLessThan(stubborn.duration(to: .now), .seconds(10))
        held.open()
    }

    /// Keeps the thread it is called on, idle, for `seconds`.
    private static func holdThread(for seconds: TimeInterval) {
        Thread.sleep(forTimeInterval: seconds)
    }

    func testNothingIsBegunOrTakenAtOrAfterItsDeadline() async {
        // A deadline that has passed: the work is not begun, and the
        // answer is `timeout`, every time and not as the scheduler has it.
        let begun = MeasureCounter()
        for past in [Duration.zero, .milliseconds(1), .seconds(5)] {
            let deadline = ContinuousClock.now - past
            let late = await DirectMeasure.bounded(until: deadline) { () -> Int in
                _ = begun.next()
                return 1
            }
            XCTAssertEqual(late, .failure(.timeout), "\(past)")
        }
        XCTAssertEqual(begun.next(), 0)

        // Work that begins in time and ends after its deadline, while the
        // timer cannot be first: every other thread that runs tasks is held
        // from the moment the work begins. Its result is not taken, whether
        // it succeeded or failed.
        struct Refused: Error {}
        let threads = ProcessInfo.processInfo.activeProcessorCount
        for fails in [false, true] {
            let holders = MeasureLog<Task<Void, Never>>()
            let deadline = ContinuousClock.now.advanced(by: .milliseconds(100))
            let overran = await DirectMeasure.bounded(until: deadline) { () -> Int in
                for _ in 0..<(2 * threads) {
                    holders.add(Task { Self.holdThread(for: 0.5) })
                }
                Self.holdThread(for: 0.3)
                if fails {
                    throw Refused()
                }
                return 1
            }
            XCTAssertEqual(overran, .failure(.timeout), "\(fails)")
            for task in holders.values {
                await task.value
            }
        }

        // The work's task is first run after the deadline, though the
        // call was made before it: every thread that runs tasks is held
        // meanwhile, as a device under load or just resumed holds them.
        // The work is not begun then. (Where a thread was free after all
        // and the work began in time, that is in order too: what is asked
        // is that it never begins late.)
        let entered = MeasureLog<Duration>()
        let holding = (0..<(2 * threads)).map { _ in
            Task {
                Self.holdThread(for: 0.25)
            }
        }
        let tight = ContinuousClock.now.advanced(by: .milliseconds(5))
        let crowded = await DirectMeasure.bounded(until: tight) { () -> Int in
            entered.add(tight.duration(to: .now))
            return 1
        }
        for task in holding {
            await task.value
        }
        if crowded != .success(1) {
            XCTAssertEqual(crowded, .failure(.timeout))
        }
        // Give a late start of the work, if there was one, time to show.
        try? await Task.sleep(for: .milliseconds(100))
        XCTAssertTrue(
            entered.values.allSatisfy { $0 < .milliseconds(50) },
            "work was begun \(entered.values) after its deadline"
        )

        // In time: the work's own result.
        let far = ContinuousClock.now.advanced(by: .seconds(30))
        let done = await DirectMeasure.bounded(until: far) { 7 }
        XCTAssertEqual(done, .success(7))
        let failed = await DirectMeasure.bounded(until: far) { () -> Int in throw Refused() }
        XCTAssertEqual(failed, .failure(.failed))

        // The measurement's bound has passed when a leg's turn comes: no
        // request is made, and the leg says `timeout`.
        let asked = MeasureLog<Int>()
        let result = await DirectMeasure.sample(
            limits: .standard,
            end: ContinuousClock.now,
            unchanged: { _ in true }
        ) { index in
            asked.add(index)
            return self.good(1)
        }
        XCTAssertEqual(result, DirectMeasure.SampleResult(times: [], error: .timeout))
        XCTAssertEqual(asked.values, [])
    }

    func testARequestTheSystemTimedOutIsATimeoutAndAnyOtherFailureIsNot() async {
        let far = ContinuousClock.now.advanced(by: .seconds(30))
        // The session's own request timeout, as the system reports it,
        // in either of the forms it is thrown in.
        let timedOut: [Error] = [
            URLError(.timedOut),
            NSError(domain: NSURLErrorDomain, code: NSURLErrorTimedOut),
        ]
        for error in timedOut {
            let result = await DirectMeasure.bounded(until: far) { () -> Int in throw error }
            XCTAssertEqual(result, .failure(.timeout), "\(type(of: error))")
        }
        // The connection, the name, the certificate: failures.
        for code in [
            URLError.Code.cannotConnectToHost, .networkConnectionLost, .notConnectedToInternet,
            .cannotFindHost, .secureConnectionFailed, .serverCertificateUntrusted,
            .serverCertificateHasBadDate, .cancelled,
        ] {
            let result = await DirectMeasure.bounded(until: far) { () -> Int in throw URLError(code) }
            XCTAssertEqual(result, .failure(.failed), "\(code.rawValue)")
        }

        // In a leg: the request the system timed out ends it as `timeout`,
        // with what was timed before it.
        let (result, asked) = await sampled { index in
            if index == 3 {
                throw URLError(.timedOut)
            }
            return self.good(10 * index)
        }
        XCTAssertEqual(asked, [0, 1, 2, 3])
        XCTAssertEqual(result.times, [10, 20].map { Duration.milliseconds($0) })
        XCTAssertEqual(result.error, .timeout)
        // An earlier sample's error is the leg's, and stays it.
        let (mixed, _) = await sampled { index in
            if index == 1 {
                return DirectMeasure.Sample(elapsed: .milliseconds(1), healthy: false, counts: true)
            }
            if index == 3 {
                throw URLError(.timedOut)
            }
            return self.good(10 * index)
        }
        XCTAssertEqual(mixed.times, [20].map { Duration.milliseconds($0) })
        XCTAssertEqual(mixed.error, .failed)
    }

    func testALegsNumbersAreOverTheCompletedSamplesOnly() {
        let times = { (values: [Int]) in values.map { Duration.milliseconds($0) } }
        // An even count: the median is between the two in the middle.
        let four = DirectMeasure.leg(
            .ipv6, scope: .global, relayRegion: nil,
            DirectMeasure.SampleResult(times: times([40, 10, 30, 20]), error: .timeout)
        )
        XCTAssertEqual(four.ok, 4)
        XCTAssertEqual(four.medianMs, 25)
        XCTAssertEqual(four.maxMs, 40)
        XCTAssertEqual(four.error, .timeout)
        XCTAssertEqual(four.scope, .global)
        // Fewer than all of them with no reason given is a failure, never
        // a clean leg.
        let short = DirectMeasure.leg(
            .tunnel, scope: nil, relayRegion: nil,
            DirectMeasure.SampleResult(times: times([7]), error: nil)
        )
        XCTAssertEqual(short.error, .failed)
        XCTAssertEqual(short.medianMs, 7)
        // Fractions of a millisecond are kept.
        let fine = DirectMeasure.leg(
            .tunnel, scope: nil, relayRegion: nil,
            DirectMeasure.SampleResult(times: [.microseconds(1_500)], error: .failed)
        )
        XCTAssertEqual(fine.maxMs, 1.5)
    }

    // MARK: One at a time

    @MainActor
    func testASecondMeasurementWhileOneRunsIsBusyAtOnce() async {
        let empty = DirectMeasure.Measurement(measuredAt: Date(timeIntervalSince1970: 1), current: nil, tunnel: nil)
        let held = MeasureGate()
        let started = MeasureGate()
        let first = Task { @MainActor in
            await DirectMeasure.exclusively {
                started.open()
                await held.wait()
                return empty
            }
        }
        await started.wait()
        // While the first runs: busy, and its own work is never begun.
        let began = ContinuousClock.now
        let second = await DirectMeasure.exclusively {
            XCTFail("A second measurement ran beside the first.")
            return empty
        }
        XCTAssertEqual(second, .busy)
        XCTAssertLessThan(began.duration(to: .now), .seconds(5))
        held.open()
        let done = await first.value
        XCTAssertEqual(done, .measured(empty))
        // The first is over: the next one runs.
        let third = await DirectMeasure.exclusively { empty }
        XCTAssertEqual(third, .measured(empty))
    }

    func testAnAddressThatIsNotAnInstancesMeasuresNothing() async {
        // Not an HTTPS origin on the default port with a DNS name: nothing
        // is attempted on either path, and nothing is asked of the link.
        let link = MeasureLinkStandIn(host: Self.host)
        for address in ["http://one-app.avibe.bot", "https://one-app.avibe.bot:8443", "https://192.0.2.7"] {
            let measured = await DirectMeasure.measurement(
                ofInstanceAt: URL(string: address)!,
                link: link.link
            )
            XCTAssertNil(measured.current, address)
            XCTAssertNil(measured.tunnel, address)
        }
        XCTAssertEqual(link.opened, 0)
    }

    // MARK: The measurement's port

    private struct ReadEnded: Error {}

    /// A connection to the loopback `port`, once it is made.
    private func connected(
        _ port: UInt16,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws -> NWConnection {
        let connection = NWConnection(
            host: "127.0.0.1",
            port: try XCTUnwrap(NWEndpoint.Port(rawValue: port), file: file, line: line),
            using: .tcp
        )
        let settled = MeasureLog<Bool>()
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready: settled.add(true)
            case .waiting, .failed, .cancelled: settled.add(false)
            default: break
            }
        }
        connection.start(queue: .global())
        let deadline = ContinuousClock.now.advanced(by: .seconds(10))
        while settled.values.isEmpty, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(10))
        }
        guard settled.values.first == true else {
            connection.cancel()
            XCTFail("No connection to the port was made.", file: file, line: line)
            throw ReadEnded()
        }
        return connection
    }

    /// Reads from `connection` until `enough` says so of what was read.
    /// Every wait has a bound, and the end of the connection before
    /// `enough` is a failure: a peer that sends too little, open or
    /// closed, fails the test that asked instead of holding it.
    private func read(
        _ connection: NWConnection,
        until enough: (String) -> Bool,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws -> String {
        var read = ""
        let deadline = ContinuousClock.now.advanced(by: .seconds(10))
        while !enough(read) {
            let next = await DirectMeasure.bounded(until: deadline) {
                try await connection.receiveChunk(maximumLength: 4096)
            }
            guard case .success(let chunk) = next else {
                XCTFail("Nothing more came within the bound, after: \(read)", file: file, line: line)
                throw ReadEnded()
            }
            read += String(decoding: chunk.data, as: UTF8.self)
            if chunk.isComplete, !enough(read) {
                XCTFail("The connection ended before what was expected, after: \(read)", file: file, line: line)
                throw ReadEnded()
            }
        }
        return read
    }

    /// Everything `connection` is sent until its far end closes it. A far
    /// end that neither goes on nor closes fails the test that asked.
    private func readToEnd(
        _ connection: NWConnection,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async -> String {
        var read = ""
        let deadline = ContinuousClock.now.advanced(by: .seconds(10))
        while true {
            let next = await DirectMeasure.bounded(until: deadline) {
                try await connection.receiveChunk(maximumLength: 4096)
            }
            switch next {
            case .success(let chunk):
                read += String(decoding: chunk.data, as: UTF8.self)
                if chunk.isComplete {
                    return read
                }
            case .failure(.failed):
                // Closed hard instead of ended: closed all the same.
                return read
            case .failure(.timeout):
                XCTFail("The port neither went on nor closed, after: \(read)", file: file, line: line)
                return read
            }
        }
    }

    /// One exchange with the port over a real socket: sends `request`,
    /// returns everything answered until the port closes.
    private func ask(
        _ port: UInt16,
        _ request: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws -> String {
        let connection = try await connected(port, file: file, line: line)
        defer {
            connection.cancel()
        }
        try await connection.sendAll(Data(request.utf8))
        return await readToEnd(connection, file: file, line: line)
    }

    /// Whether anything accepts a connection on the loopback `port` now.
    private func accepts(_ port: UInt16) async -> Bool {
        guard let endpoint = NWEndpoint.Port(rawValue: port) else {
            return false
        }
        let connection = NWConnection(host: "127.0.0.1", port: endpoint, using: .tcp)
        defer {
            connection.cancel()
        }
        let settled = MeasureLog<Bool>()
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready: settled.add(true)
            // A refused connection waits to be tried again: nothing there.
            case .waiting, .failed, .cancelled: settled.add(false)
            default: break
            }
        }
        connection.start(queue: .global())
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
        while settled.values.isEmpty, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(10))
        }
        return settled.values.first ?? false
    }

    private func connect(_ host: String, port: Int = 443) -> String {
        "CONNECT \(host):\(port) HTTP/1.1\r\nHost: \(host):\(port)\r\n\r\n"
    }

    func testThePortServesItsInstancesHostOverTheLinkAndNothingElseOverAnything() async throws {
        let link = MeasureLinkStandIn(host: Self.host)
        let port = DirectMeasurePort(link: link.link)
        let bound = try await port.start()
        addTeardownBlock {
            port.stop()
        }

        // Anything but `CONNECT <the host>:443` is refused, and nothing is
        // opened for it: there is no other way out of this port than the
        // link, and no request for another place reaches even that.
        for (request, status) in [
            (connect("two-app.avibe.bot"), 403),
            (connect("sub.\(Self.host)"), 403),
            (connect("example.com"), 403),
            (connect(Self.host, port: 80), 403),
            (connect("192.0.2.7"), 403),
            ("GET http://\(Self.host)/health HTTP/1.1\r\nHost: \(Self.host)\r\n\r\n", 405),
            ("GET /health HTTP/1.1\r\nHost: \(Self.host)\r\n\r\n", 405),
            ("OWNER abc HTTP/1.1\r\n\r\n", 403),
            ("nonsense\r\n\r\n", 400),
        ] {
            let answered = try await ask(bound, request)
            XCTAssertTrue(answered.hasPrefix("HTTP/1.1 \(status) "), "\(request.prefix(40)): \(answered)")
        }
        XCTAssertEqual(port.tunnels, 0)
        XCTAssertEqual(link.opened, 0)

        // Its own host: one stream over the link, asked as the App's
        // listener asks the instance, then opaque bytes both ways.
        let connection = try await connected(bound)
        try await connection.sendAll(Data(connect(Self.host).utf8))
        let head = try await read(connection) { $0.hasSuffix("\r\n\r\n") }
        XCTAssertEqual(head, "HTTP/1.1 200 Connection Established\r\n\r\n")
        XCTAssertEqual(port.tunnels, 1)
        XCTAssertEqual(link.opened, 1)
        let stream = try XCTUnwrap(link.streams.first)
        XCTAssertEqual(
            String(decoding: stream.head, as: UTF8.self),
            "CONNECT \(Self.host):443 HTTP/1.1\r\nHost: \(Self.host):443\r\n\r\n"
        )
        for message in ["first", "second"] {
            try await connection.sendAll(Data(message.utf8))
            let echoed = try await read(connection) { $0.count >= message.count }
            XCTAssertEqual(echoed, message)
        }

        // A second request for the host is what a connection made anew
        // looks like, and what ends a leg: it is counted and refused, and
        // no second stream is opened over the link for it. The first
        // tunnel goes on.
        let again = try await ask(bound, connect(Self.host))
        XCTAssertTrue(again.hasPrefix("HTTP/1.1 503 "), again)
        XCTAssertEqual(port.tunnels, 2)
        XCTAssertEqual(link.opened, 1)
        try await connection.sendAll(Data("third".utf8))
        let still = try await read(connection) { $0.count >= 5 }
        XCTAssertEqual(still, "third")

        // The port closes with its measurement: what it carried ends, its
        // stream is given up, and nothing listens there afterwards.
        port.stop()
        let ended = try? await connection.receiveChunk(maximumLength: 16)
        XCTAssertTrue(ended == nil || (ended?.isComplete == true && ended?.data.isEmpty == true))
        try await eventually(timeout: .seconds(10)) { stream.isCancelled }
        try await eventually(timeout: .seconds(10)) {
            await self.accepts(bound) == false
        }
        connection.cancel()
    }

    func testThePortRefusesWhenTheLinkIsNotAsItWasOrDoesNotCarry() async throws {
        // No longer current: refused, and the link is not asked for a
        // stream. (The request still counts as one made.)
        let stale = MeasureLinkStandIn(host: Self.host)
        stale.current.set(false)
        let stalePort = DirectMeasurePort(link: stale.link)
        let staleBound = try await stalePort.start()
        addTeardownBlock {
            stalePort.stop()
        }
        let unavailable = try await ask(staleBound, connect(Self.host))
        XCTAssertTrue(unavailable.hasPrefix("HTTP/1.1 503 "), unavailable)
        XCTAssertEqual(stalePort.tunnels, 1)
        XCTAssertEqual(stale.opened, 0)

        // The link gives no stream, or the instance refuses the request, or
        // never answers it: 502, each within the port's patience.
        var limits = DirectMeasure.Limits.standard
        limits.sampleSeconds = 0.5
        for mode in [MeasureLinkStandIn.Mode.fails, .refuses, .silent] {
            let link = MeasureLinkStandIn(host: Self.host, mode: mode)
            let port = DirectMeasurePort(link: link.link, limits: limits)
            let bound = try await port.start()
            let began = ContinuousClock.now
            let answered = try await ask(bound, connect(Self.host))
            XCTAssertTrue(answered.hasPrefix("HTTP/1.1 502 "), "\(mode): \(answered)")
            XCTAssertLessThan(began.duration(to: .now), .seconds(8), "\(mode)")
            XCTAssertEqual(link.opened, 1, "\(mode)")
            port.stop()
            if mode != .fails {
                let stream = try XCTUnwrap(link.streams.first)
                try await eventually(timeout: .seconds(10)) { stream.isCancelled }
            }
        }

        // A stream that the link gives only after the port stopped waiting
        // for it is given up when it comes, not left open with no one on it.
        let delay = MeasureGate()
        let slow = MeasureLinkStandIn(host: Self.host, delay: delay)
        let slowPort = DirectMeasurePort(link: slow.link, limits: limits)
        let slowBound = try await slowPort.start()
        addTeardownBlock {
            slowPort.stop()
        }
        let waited = try await ask(slowBound, connect(Self.host))
        XCTAssertTrue(waited.hasPrefix("HTTP/1.1 502 "), waited)
        XCTAssertTrue(slow.streams.isEmpty)
        delay.open()
        try await eventually(timeout: .seconds(10)) { slow.streams.first?.isCancelled == true }
        XCTAssertEqual(slow.opened, 1)

        // A client that never finishes its request is not waited for.
        let quiet = MeasureLinkStandIn(host: Self.host)
        let quietPort = DirectMeasurePort(link: quiet.link, limits: limits)
        let quietBound = try await quietPort.start()
        addTeardownBlock {
            quietPort.stop()
        }
        let began = ContinuousClock.now
        let nothing = try await ask(quietBound, "CONNECT \(Self.host):443 HTTP/1.1\r\n")
        XCTAssertEqual(nothing, "")
        XCTAssertLessThan(began.duration(to: .now), .seconds(8))
        XCTAssertEqual(quietPort.tunnels, 0)
    }

    func testThePortHoldsAFewConnectionsAndGivesOneOfThemAStream() async throws {
        var limits = DirectMeasure.Limits.standard
        XCTAssertEqual(limits.portConnections, 4)
        // Long enough that nothing here is closed for being slow: what is
        // closed below is closed by the limit.
        limits.sampleSeconds = 120
        let link = MeasureLinkStandIn(host: Self.host)
        let port = DirectMeasurePort(link: link.link, limits: limits)
        let bound = try await port.start()
        addTeardownBlock {
            port.stop()
        }

        // As many connections as the port holds, none of which has sent
        // anything: each is held, waiting for its request.
        var held: [NWConnection] = []
        for _ in 0..<limits.portConnections {
            held.append(try await connected(bound))
        }
        let all = held
        addTeardownBlock {
            for connection in all {
                connection.cancel()
            }
        }

        // One more is closed at once and unread: with a handler it would
        // have been answered `200` and given a stream for what it sent.
        for _ in 0..<3 {
            let excess = try await connected(bound)
            let began = ContinuousClock.now
            try? await excess.sendAll(Data(connect(Self.host).utf8))
            let answered = await readToEnd(excess)
            excess.cancel()
            XCTAssertEqual(answered, "")
            XCTAssertLessThan(began.duration(to: .now), .seconds(9))
        }
        XCTAssertEqual(port.tunnels, 0)
        XCTAssertEqual(link.opened, 0)

        // The connections it holds are served, and the first request for
        // the host among them gets the one stream. The port cannot tell
        // whose it is: the measurement's own, or another process's that
        // came first.
        try await held[0].sendAll(Data(connect(Self.host).utf8))
        let head = try await read(held[0]) { $0.hasSuffix("\r\n\r\n") }
        XCTAssertEqual(head, "HTTP/1.1 200 Connection Established\r\n\r\n")
        try await held[0].sendAll(Data("ping".utf8))
        let echoed = try await read(held[0]) { $0.count >= 4 }
        XCTAssertEqual(echoed, "ping")
        XCTAssertEqual(port.tunnels, 1)
        XCTAssertEqual(link.opened, 1)

        // The same request on another of them (the measurement's, if the
        // first was not): counted, which ends the leg, refused, and the
        // link is not asked for a second stream.
        try await held[1].sendAll(Data(connect(Self.host).utf8))
        let refused = await readToEnd(held[1])
        XCTAssertTrue(refused.hasPrefix("HTTP/1.1 503 "), refused)
        XCTAssertEqual(port.tunnels, 2)
        XCTAssertEqual(link.opened, 1)

        // That one has ended, so there is room for one again: a new
        // connection is read and answered, as soon as the port has seen
        // the end of the other.
        try await eventually(timeout: .seconds(10)) {
            guard let next = try? await self.connected(bound) else {
                return false
            }
            defer {
                next.cancel()
            }
            try? await next.sendAll(Data("nonsense\r\n\r\n".utf8))
            return await self.readToEnd(next).hasPrefix("HTTP/1.1 400 ")
        }
        XCTAssertEqual(link.opened, 1)
    }

    func testThePortAsksAgainWhetherTheLinkIsAsItWasBeforeItAnswersTheClient() async throws {
        // The instance holds its answer to the port's request. `revoked`:
        // meanwhile the link stops being the one the measurement began on
        // (the App's listener failed, say; the transport keeps its
        // streams). Then the answer comes.
        for revoked in [true, false] {
            let link = MeasureLinkStandIn(host: Self.host, mode: .held)
            let port = DirectMeasurePort(link: link.link)
            let bound = try await port.start()
            let connection = try await connected(bound)
            // The request, and bytes the client sends before any answer.
            try await connection.sendAll(Data((connect(Self.host) + "early").utf8))
            try await eventually(timeout: .seconds(10)) {
                link.streams.first?.hasRequest == true
            }
            let stream = try XCTUnwrap(link.streams.first)
            XCTAssertEqual(link.opened, 1)
            link.current.set(!revoked)
            stream.release()

            if revoked {
                // No `200`, none of the client's bytes sent on, and the
                // stream, which carried nothing yet, given up.
                let answered = await readToEnd(connection)
                XCTAssertTrue(answered.hasPrefix("HTTP/1.1 503 "), answered)
                XCTAssertFalse(answered.contains(" 200 "), answered)
                try await eventually(timeout: .seconds(10)) { stream.isCancelled }
                XCTAssertEqual(stream.payload, Data())
            } else {
                // The same exchange with the link unchanged: answered, and
                // the early bytes go on (the stand-in sends them back).
                let answered = try await read(connection) { $0.hasSuffix("early") }
                XCTAssertEqual(answered, "HTTP/1.1 200 Connection Established\r\n\r\nearly")
                XCTAssertEqual(stream.payload, Data("early".utf8))
            }
            connection.cancel()
            port.stop()
        }
    }

    // MARK: The two sessions

    func testTheTwoSessionsAreEachTheirOwnAndOnlyOneHasARoute() throws {
        let limits = DirectMeasure.Limits.standard
        let port = try XCTUnwrap(NWEndpoint.Port(rawValue: 49_152))
        let current = DirectMeasure.currentConfiguration(port: port, limits: limits)
        let tunnel = DirectMeasure.tunnelConfiguration(limits: limits)
        // The measured path's session goes to the port and nowhere else.
        XCTAssertEqual(current.proxyConfigurations.count, 1)
        XCTAssertEqual(current.proxyConfigurations.first?.allowFailover, false)
        // The tunnel's has no route of the App's.
        XCTAssertTrue(tunnel.proxyConfigurations.isEmpty)
        for configuration in [current, tunnel] {
            XCTAssertEqual(configuration.httpMaximumConnectionsPerHost, 1)
            XCTAssertEqual(configuration.requestCachePolicy, .reloadIgnoringLocalAndRemoteCacheData)
            XCTAssertNil(configuration.urlCache)
            XCTAssertNil(configuration.httpCookieStorage)
            XCTAssertNil(configuration.urlCredentialStorage)
            XCTAssertFalse(configuration.httpShouldSetCookies)
            XCTAssertFalse(configuration.waitsForConnectivity)
            XCTAssertEqual(configuration.timeoutIntervalForRequest, limits.sampleSeconds)
        }
    }

    func testEachSessionKeepsToOneConnectionOfItsOwnAndSaysWhenItDidNot() async throws {
        let server = try MeasureHealthServer()
        let health = try await server.start()
        addTeardownBlock {
            server.stop()
        }
        let limits = DirectMeasure.Limits.standard
        let first = DirectMeasure.Requester(
            configuration: DirectMeasure.tunnelConfiguration(limits: limits), url: health, limits: limits
        )
        let second = DirectMeasure.Requester(
            configuration: DirectMeasure.tunnelConfiguration(limits: limits), url: health, limits: limits
        )
        addTeardownBlock {
            first.finish()
            second.finish()
        }

        // One session: its first request makes the connection, and every
        // later one uses it again, with one attempt each.
        for index in 0..<4 {
            let observed = try await first.get()
            XCTAssertTrue(observed.healthy, "\(index)")
            XCTAssertEqual(observed.reused, index > 0, "\(index)")
            XCTAssertEqual(observed.transactions, 1, "\(index)")
            XCTAssertFalse(observed.viaProxy, "\(index)")
            XCTAssertGreaterThan(observed.elapsed, .zero)
        }
        XCTAssertEqual(server.connections, 1)
        // The other session shares nothing with it: its first request is
        // on a connection of its own, though one to the same place is open.
        let other = try await second.get()
        XCTAssertFalse(other.reused)
        XCTAssertEqual(server.connections, 2)
        let otherAgain = try await second.get()
        XCTAssertTrue(otherAgain.reused)
        XCTAssertEqual(server.connections, 2)
        XCTAssertEqual(server.requests, 6)

        // The connection is closed under the session. The system may send
        // the next request again on a new connection and answer it as if
        // nothing had happened (measured on macOS), or fail it. Either way
        // it is never an answer that says it ran on the old connection
        // with one attempt, which is what a sample has to say to count.
        server.closeConnections()
        do {
            let renewed = try await first.get()
            XCTAssertTrue(!renewed.reused || renewed.transactions > 1, "\(renewed)")
            XCTAssertEqual(server.connections, 3)
            print("[iOS fixture] stage=measure a cut connection was answered on a new one, reused=\(renewed.reused) transactions=\(renewed.transactions)")
        } catch {
            print("[iOS fixture] stage=measure a cut connection failed its next request: \((error as NSError).code)")
        }

        // Answers that are not the instance being well.
        for (answer, why) in [
            (MeasureHealthServer.Answer(status: 200, body: #"{"status":"degraded"}"#), "another status word"),
            (MeasureHealthServer.Answer(status: 200, body: "ok"), "not JSON"),
            (MeasureHealthServer.Answer(status: 503, body: #"{"status":"ok"}"#), "not 200"),
            (
                MeasureHealthServer.Answer(
                    status: 200,
                    body: #"{"status":"ok","pad":""# + String(repeating: "x", count: 9_000) + #""}"#
                ),
                "more than is read"
            ),
        ] {
            server.answer = answer
            let observed = try await second.get()
            XCTAssertFalse(observed.healthy, why)
        }
        // A redirect is not followed: its target is never asked.
        server.answer = MeasureHealthServer.Answer(status: 302, body: "", location: "/elsewhere")
        let redirected = try await second.get()
        XCTAssertFalse(redirected.healthy)
        XCTAssertFalse(server.paths.contains("/elsewhere"))
        XCTAssertTrue(server.paths.allSatisfy { $0 == "/health" })
    }

    func testARequestThatDoesNotGoThroughThePortsTunnelNeverSaysItDid() async throws {
        // A session routed to the port asks for a loopback address. On
        // macOS the system sends such a request straight there, whatever
        // route the session has (measured; it is why the port's own path
        // cannot be driven against a local server): a request that went
        // round the port. It is answered, and `viaProxy` is false, which is
        // what the sampling refuses. Where the system keeps to the route
        // instead, the port refuses it, since it is not its instance's
        // host. Either way no tunnel is made and the link is never asked.
        let server = try MeasureHealthServer()
        let health = try await server.start()
        addTeardownBlock {
            server.stop()
        }
        let link = MeasureLinkStandIn(host: Self.host)
        let port = DirectMeasurePort(link: link.link)
        let started = try await port.start()
        let bound = try XCTUnwrap(NWEndpoint.Port(rawValue: started))
        addTeardownBlock {
            port.stop()
        }
        let limits = DirectMeasure.Limits.standard
        let routed = DirectMeasure.Requester(
            configuration: DirectMeasure.currentConfiguration(port: bound, limits: limits),
            url: health,
            limits: limits
        )
        addTeardownBlock {
            routed.finish()
        }
        do {
            let observed = try await routed.get()
            XCTAssertFalse(observed.viaProxy, "\(observed)")
            print("[iOS fixture] stage=measure a loopback request went round the route, viaProxy=\(observed.viaProxy)")
        } catch {
            XCTAssertEqual(server.connections, 0)
            print("[iOS fixture] stage=measure a loopback request kept to the route and was refused: \((error as NSError).code)")
        }
        XCTAssertEqual(port.tunnels, 0)
        XCTAssertEqual(link.opened, 0)
    }

    // MARK: The relay's region

    func testARelaysRegionIsReadFromTheSelectedPathsOwnRelayAndOnlyIfKnown() {
        // The four public relays, as the binding reports a relay path's
        // remote address: the relay's URL, with the root dot or without.
        for (url, region) in [
            ("https://use1-1.relay.n0.iroh.link./", "use1"),
            ("https://usw1-1.relay.n0.iroh.link./", "usw1"),
            ("https://euc1-1.relay.n0.iroh.link./", "euc1"),
            ("https://aps1-1.relay.n0.iroh.link./", "aps1"),
            ("https://euc1-1.relay.n0.iroh.link/", "euc1"),
            ("https://APS1-1.relay.n0.iroh.link.", "aps1"),
            // The default port, written out, is the same place.
            ("https://usw1-1.relay.n0.iroh.link.:443/", "usw1"),
            ("HTTPS://use1-1.relay.n0.iroh.link:443", "use1"),
        ] {
            XCTAssertEqual(DirectSelectedPath.relayRegion(ofRelayURL: url), region, url)
            let path = DirectSelectedPath(isRelay: true, isIp: false, remoteAddress: url, rttMs: 80)
            XCTAssertEqual(path?.kind, .relay, url)
            XCTAssertEqual(path?.relayRegion, region, url)
            XCTAssertNil(path?.local, url)
        }
        // Every relay a dial may use has a region, and they are four.
        XCTAssertEqual(
            DirectTrialRelay.publicTrialRelayURLs.map { DirectSelectedPath.relayRegion(ofRelayURL: $0) },
            ["use1", "usw1", "euc1", "aps1"]
        )
        // Another port or path on a relay's host is another service, and a
        // URL with more to it than the relay's own root is not that relay.
        for url in [
            "https://euc1-1.relay.n0.iroh.link.:8443/",
            "https://euc1-1.relay.n0.iroh.link:80/",
            "https://euc1-1.relay.n0.iroh.link./relay",
            "https://euc1-1.relay.n0.iroh.link.//",
            "https://euc1-1.relay.n0.iroh.link./?region=use1",
            "https://euc1-1.relay.n0.iroh.link./#use1",
            "https://user@euc1-1.relay.n0.iroh.link./",
            "https://user:word@euc1-1.relay.n0.iroh.link./",
            "https://euc1-1.relay.n0.iroh.link../",
            "wss://euc1-1.relay.n0.iroh.link./",
        ] {
            XCTAssertNil(DirectSelectedPath.relayRegion(ofRelayURL: url), url)
        }
        // Any other relay has no region: none is guessed from its name.
        for url in [
            "https://relay.example/",
            "https://euc1-2.relay.n0.iroh.link./",
            "https://euc1-1.relay.n0.iroh.link.evil.example/",
            "https://euw1-1.relay.n0.iroh.link./",
            "http://euc1-1.relay.n0.iroh.link./",
            "euc1-1.relay.n0.iroh.link",
            "",
        ] {
            XCTAssertNil(DirectSelectedPath.relayRegion(ofRelayURL: url), url)
            XCTAssertNil(
                DirectSelectedPath(isRelay: true, isIp: false, remoteAddress: url, rttMs: 1)?.relayRegion,
                url
            )
        }
        // A path to an address has none, whatever its address reads like.
        XCTAssertNil(
            DirectSelectedPath(isRelay: false, isIp: true, remoteAddress: "203.0.113.5:4433", rttMs: 9)?.relayRegion
        )
        XCTAssertNil(DirectSelectedPath(kind: .ipv4, local: false, rttMs: 9, relayRegion: "euc1").relayRegion)
        XCTAssertNil(DirectSelectedPath(kind: .relay, local: nil, rttMs: 9).relayRegion)
        XCTAssertEqual(
            DirectSelectedPath(kind: .relay, local: nil, rttMs: 9, relayRegion: "use1").relayRegion,
            "use1"
        )
    }
}

// MARK: Stand-ins

private final class MeasureLog<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var kept: [Value] = []

    var values: [Value] {
        lock.withLock { kept }
    }

    func add(_ value: Value) {
        lock.withLock {
            kept.append(value)
        }
    }
}

private final class MeasureCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    /// 0, 1, 2, … one per call.
    func next() -> Int {
        lock.withLock {
            defer {
                count += 1
            }
            return count
        }
    }
}

private final class MeasureFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Bool

    init(_ value: Bool) {
        self.value = value
    }

    func get() -> Bool {
        lock.withLock { value }
    }

    func set(_ value: Bool) {
        lock.withLock {
            self.value = value
        }
    }
}

/// Waited for until it is opened, cancelled or not; once open it stays so.
private final class MeasureGate: @unchecked Sendable {
    private let lock = NSLock()
    private var isOpen = false
    private var waiting: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        await withCheckedContinuation { continuation in
            let open = lock.withLock { () -> Bool in
                if !isOpen {
                    waiting.append(continuation)
                }
                return isOpen
            }
            if open {
                continuation.resume()
            }
        }
    }

    func open() {
        let waiting = lock.withLock { () -> [CheckedContinuation<Void, Never>] in
            isOpen = true
            defer {
                self.waiting = []
            }
            return self.waiting
        }
        for continuation in waiting {
            continuation.resume()
        }
    }
}

/// The App's link to an instance, for the port: every stream it opens is an
/// instance's ingress that answers the request the listener sends and then
/// sends back whatever it is sent.
private final class MeasureLinkStandIn: @unchecked Sendable {
    enum Mode: Equatable {
        /// Answers `200`, then echoes.
        case echoes
        /// The link gives no stream.
        case fails
        /// The instance answers `403`.
        case refuses
        /// The instance never answers.
        case silent
        /// The instance answers `200` when the test releases it, then
        /// echoes.
        case held
    }

    struct Unavailable: Error {}

    let current = MeasureFlag(true)
    private let lock = NSLock()
    private var made: [MeasureInstanceStream] = []
    private var asked = 0
    private let mode: Mode
    private let authority: DirectAuthority
    /// When set, a stream is given only once this is open, however long
    /// that is and whether or not the one who asked still waits.
    private let delay: MeasureGate?

    init(host: String, mode: Mode = .echoes, delay: MeasureGate? = nil) {
        self.mode = mode
        self.delay = delay
        authority = DirectAuthority(host: host)!
    }

    /// How often a stream was asked for.
    var opened: Int {
        lock.withLock { asked }
    }

    var streams: [MeasureInstanceStream] {
        lock.withLock { made }
    }

    var link: DirectMeasureLink {
        let current = current
        return DirectMeasureLink(
            authority: authority,
            kind: .ipv4,
            local: true,
            relayRegion: nil,
            open: { [self] in
                await delay?.wait()
                return try lock.withLock { () -> MeasureInstanceStream in
                    asked += 1
                    guard mode != .fails else {
                        throw Unavailable()
                    }
                    let stream = MeasureInstanceStream(mode: mode)
                    made.append(stream)
                    return stream
                }
            },
            isCurrent: { current.get() }
        )
    }
}

private final class MeasureInstanceStream: DirectByteStream, @unchecked Sendable {
    struct Closed: Error {}

    private let lock = NSLock()
    private let mode: MeasureLinkStandIn.Mode
    private var requestHead = Data()
    private var answeredHead = false
    private var heldAnswer: Data?
    private var sentOn = Data()
    private var outgoing: [Data] = []
    private var reader: CheckedContinuation<Data?, Error>?
    private var cancelled = false
    private var finished = false

    init(mode: MeasureLinkStandIn.Mode) {
        self.mode = mode
    }

    /// The request head it was sent before anything else.
    var head: Data {
        lock.withLock { requestHead }
    }

    var isCancelled: Bool {
        lock.withLock { cancelled }
    }

    /// Whether the whole request head has reached it.
    var hasRequest: Bool {
        lock.withLock { answeredHead }
    }

    /// Everything it was sent after the request head.
    var payload: Data {
        lock.withLock { sentOn }
    }

    /// `held` only: the instance answers now.
    func release() {
        let waiting: (CheckedContinuation<Data?, Error>, Data)? = lock.withLock {
            guard let answer = heldAnswer, !cancelled else {
                return nil
            }
            heldAnswer = nil
            if let reader {
                self.reader = nil
                return (reader, answer)
            }
            outgoing.append(answer)
            return nil
        }
        waiting?.0.resume(returning: waiting?.1)
    }

    func read(maxLength: Int) async throws -> Data? {
        try await withCheckedThrowingContinuation { continuation in
            let ready: Result<Data?, Error>? = lock.withLock {
                if cancelled {
                    return .failure(Closed())
                }
                if !outgoing.isEmpty {
                    return .success(outgoing.removeFirst())
                }
                if finished {
                    return .success(nil)
                }
                reader = continuation
                return nil
            }
            if let ready {
                continuation.resume(with: ready)
            }
        }
    }

    func write(_ data: Data) async throws {
        let waiting: (CheckedContinuation<Data?, Error>, Data)? = try lock.withLock {
            guard !cancelled else {
                throw Closed()
            }
            let answer: Data?
            if !answeredHead {
                requestHead.append(data)
                guard requestHead.range(of: Data("\r\n\r\n".utf8)) != nil else {
                    return nil
                }
                answeredHead = true
                switch mode {
                case .echoes: answer = Data("HTTP/1.1 200 Connection Established\r\n\r\n".utf8)
                case .refuses: answer = Data("HTTP/1.1 403 Forbidden\r\n\r\n".utf8)
                case .silent, .fails: answer = nil
                case .held:
                    heldAnswer = Data("HTTP/1.1 200 Connection Established\r\n\r\n".utf8)
                    answer = nil
                }
            } else {
                sentOn.append(data)
                answer = data
            }
            guard let answer else {
                return nil
            }
            if let reader {
                self.reader = nil
                return (reader, answer)
            }
            outgoing.append(answer)
            return nil
        }
        waiting?.0.resume(returning: waiting?.1)
    }

    func closeWrite() async throws {
        let reader: CheckedContinuation<Data?, Error>? = lock.withLock {
            finished = true
            defer {
                self.reader = nil
            }
            return outgoing.isEmpty ? self.reader : nil
        }
        reader?.resume(returning: nil)
    }

    func cancel() {
        let reader: CheckedContinuation<Data?, Error>? = lock.withLock {
            cancelled = true
            defer {
                self.reader = nil
            }
            return self.reader
        }
        reader?.resume(throwing: Closed())
    }
}

/// A local `/health` over plain HTTP that keeps its connections open, and
/// counts them.
private final class MeasureHealthServer: @unchecked Sendable {
    struct Answer {
        var status = 200
        var body = #"{"status":"ok"}"#
        var location: String?
    }

    private let listener: NWListener
    private let queue = DispatchQueue(label: "bot.avibe.ios.tests.measure")
    private let lock = NSLock()
    private var open: [NWConnection] = []
    private var accepted = 0
    private var asked: [String] = []
    private var current = Answer()

    init() throws {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
        listener = try NWListener(using: parameters, on: .any)
    }

    var answer: Answer {
        get { lock.withLock { current } }
        set { lock.withLock { current = newValue } }
    }

    /// Connections accepted so far.
    var connections: Int {
        lock.withLock { accepted }
    }

    var requests: Int {
        lock.withLock { asked.count }
    }

    /// The path of every request, in order.
    var paths: [String] {
        lock.withLock { asked }
    }

    func start() async throws -> URL {
        listener.newConnectionHandler = { [weak self, queue] connection in
            guard let self else {
                connection.cancel()
                return
            }
            self.lock.withLock {
                self.accepted += 1
                self.open.append(connection)
            }
            connection.start(queue: queue)
            self.serve(connection, buffered: Data())
        }
        let port: UInt16 = try await withCheckedThrowingContinuation { continuation in
            let resumed = MeasureFlag(false)
            listener.stateUpdateHandler = { [listener] state in
                switch state {
                case .ready:
                    if !resumed.get() {
                        resumed.set(true)
                        continuation.resume(returning: listener.port?.rawValue ?? 0)
                    }
                case .failed(let error):
                    if !resumed.get() {
                        resumed.set(true)
                        continuation.resume(throwing: error)
                    }
                default:
                    break
                }
            }
            listener.start(queue: queue)
        }
        return URL(string: "http://127.0.0.1:\(port)/health")!
    }

    func stop() {
        listener.cancel()
        closeConnections()
    }

    /// Closes every connection it has open, as a far end may at any time.
    func closeConnections() {
        let closing = lock.withLock { () -> [NWConnection] in
            defer {
                open = []
            }
            return open
        }
        for connection in closing {
            connection.forceCancel()
        }
    }

    private func serve(_ connection: NWConnection, buffered: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 8192) { [weak self] data, _, isComplete, error in
            guard let self, error == nil else {
                connection.cancel()
                return
            }
            var buffer = buffered
            buffer.append(data ?? Data())
            while let end = buffer.range(of: Data("\r\n\r\n".utf8)) {
                let head = String(decoding: buffer[..<end.lowerBound], as: UTF8.self)
                buffer = Data(buffer[end.upperBound...])
                let target = head.split(separator: "\r\n").first?.split(separator: " ").dropFirst().first
                let answer = self.lock.withLock { () -> Answer in
                    self.asked.append(target.map(String.init) ?? "")
                    return self.current
                }
                var response = "HTTP/1.1 \(answer.status) Answer\r\nContent-Type: application/json\r\n"
                    + "Content-Length: \(answer.body.utf8.count)\r\n"
                if let location = answer.location {
                    response += "Location: \(location)\r\n"
                }
                response += "\r\n" + answer.body
                connection.send(content: Data(response.utf8), completion: .contentProcessed { _ in })
            }
            if isComplete {
                connection.cancel()
                return
            }
            self.serve(connection, buffered: buffer)
        }
    }
}
