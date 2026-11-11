import Network
import XCTest
@testable import Avibe

/// The loopback CONNECT listener, driven over real loopback sockets. A
/// stand-in stream plays the instance, so nothing here needs a transport or
/// a network.
final class DirectConnectListenerTests: XCTestCase {
    private let authority = DirectAuthority(
        instanceURL: URL(string: "https://instance.example")!
    )!
    private let canonicalHead =
        "CONNECT instance.example:443 HTTP/1.1\r\nHost: instance.example:443\r\n\r\n"
    private let established = "HTTP/1.1 200 Connection Established\r\n\r\n"

    // MARK: Refusals

    func testAnswersConnectOnlyForTheInstanceAuthorityOnPort443() async throws {
        let oversized = "CONNECT instance.example:443 HTTP/1.1\r\nX-Padding: "
            + String(repeating: "a", count: 4096) + "\r\n\r\n"
        let refusals: [(name: String, head: String, status: Int)] = [
            ("another host", "CONNECT other.example:443 HTTP/1.1\r\nHost: other.example:443\r\n\r\n", 403),
            ("a subdomain", "CONNECT child.instance.example:443 HTTP/1.1\r\nHost: child.instance.example:443\r\n\r\n", 403),
            ("a host that only ends like it", "CONNECT notinstance.example:443 HTTP/1.1\r\n\r\n", 403),
            ("another port", "CONNECT instance.example:8443 HTTP/1.1\r\nHost: instance.example:8443\r\n\r\n", 403),
            ("no port", "CONNECT instance.example HTTP/1.1\r\nHost: instance.example\r\n\r\n", 403),
            ("userinfo", "CONNECT owner@instance.example:443 HTTP/1.1\r\nHost: instance.example:443\r\n\r\n", 403),
            ("an IP literal", "CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n", 403),
            ("an origin-form GET", "GET / HTTP/1.1\r\nHost: instance.example\r\n\r\n", 405),
            ("a forward-proxy GET", "GET http://instance.example:443/ HTTP/1.1\r\nHost: instance.example:443\r\n\r\n", 405),
            ("a lowercase method", "connect instance.example:443 HTTP/1.1\r\n\r\n", 405),
            ("HTTP/1.0", "CONNECT instance.example:443 HTTP/1.0\r\n\r\n", 400),
            ("a body", "CONNECT instance.example:443 HTTP/1.1\r\nContent-Length: 4\r\n\r\nbody", 400),
            ("a chunked body", "CONNECT instance.example:443 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n", 400),
            ("a repeated Host", "CONNECT instance.example:443 HTTP/1.1\r\nHost: instance.example:443\r\nHost: instance.example:443\r\n\r\n", 400),
            ("a Host that disagrees", "CONNECT instance.example:443 HTTP/1.1\r\nHost: other.example:443\r\n\r\n", 400),
            ("no version", "CONNECT instance.example:443\r\n\r\n", 400),
            ("a header without a name", "CONNECT instance.example:443 HTTP/1.1\r\n: value\r\n\r\n", 400),
            ("bytes that are not HTTP", "\u{01}\u{02}\u{03} instance.example:443\r\n\r\n", 400),
            ("an oversized head", oversized, 400),
        ]
        let fixture = try await makeListener()

        for refusal in refusals {
            let client = LoopbackClient(port: fixture.port)
            try await client.send(refusal.head)
            let answer = try await client.receiveUntilClosed()
            XCTAssertTrue(
                String(decoding: answer, as: UTF8.self)
                    .hasPrefix("HTTP/1.1 \(refusal.status) "),
                "\(refusal.name): \(String(decoding: answer, as: UTF8.self))"
            )
            client.close()
        }

        // A refused request never reaches the transport, and is the
        // client's fault, not the path's.
        XCTAssertEqual(fixture.streams.opened.count, 0)
        XCTAssertEqual(fixture.failures.values, [])
    }

    func testAHeadThatNeverCompletesIsClosedWithoutAnAnswer() async throws {
        let fixture = try await makeListener(limits: limits(headSeconds: 0.3))
        let client = LoopbackClient(port: fixture.port)

        try await client.send("CONNECT instance.example:443 HTTP/1.1\r\nHost: ")
        let answer = try await client.receiveUntilClosed()

        XCTAssertEqual(answer, Data())
        XCTAssertEqual(fixture.streams.opened.count, 0)
        XCTAssertEqual(fixture.failures.values, [])
    }

    // MARK: Relay

    func testRelaysAnAcceptedConnectBehindTheCanonicalHead() async throws {
        let fixture = try await makeListener()
        let client = LoopbackClient(port: fixture.port)

        // WebKit's own proxy headers, a host in another case, and bytes
        // sent before the answer.
        try await client.send(
            "CONNECT Instance.Example:443 HTTP/1.1\r\nHost: instance.example\r\nUser-Agent: test\r\nProxy-Connection: keep-alive\r\n\r\nEARLY"
        )
        let stream = try await fixture.streams.waitForStream()
        try await eventually {
            stream.written.count >= self.canonicalHead.utf8.count
        }
        // Only the canonical head: nothing WebKit sent is forwarded before
        // the instance has answered.
        XCTAssertEqual(
            String(decoding: stream.written, as: UTF8.self),
            canonicalHead
        )

        stream.instanceSends(Data((established + "LEFTOVER").utf8))
        let answer = try await client.receive(
            count: established.utf8.count + "LEFTOVER".utf8.count
        )
        XCTAssertEqual(
            String(decoding: answer, as: UTF8.self),
            established + "LEFTOVER"
        )
        try await eventually {
            stream.written.count == self.canonicalHead.utf8.count + 5
        }
        XCTAssertEqual(
            String(decoding: stream.written, as: UTF8.self),
            canonicalHead + "EARLY"
        )

        try await client.send("ping")
        try await eventually {
            String(decoding: stream.written, as: UTF8.self).hasSuffix("ping")
        }
        stream.instanceSends(Data("pong".utf8))
        let pong = try await client.receive(count: 4)
        XCTAssertEqual(String(decoding: pong, as: UTF8.self), "pong")
        XCTAssertEqual(fixture.failures.values, [])
        client.close()
    }

    func testPassesAFinishedSideOnAndKeepsTheOtherDirection() async throws {
        let fixture = try await makeListener()

        // The client finishes first: the instance still delivers.
        let client = LoopbackClient(port: fixture.port)
        let stream = try await establish(client, fixture)
        try await client.send("request")
        try await client.finish()
        try await eventually { stream.isWriteClosed }
        XCTAssertTrue(
            String(decoding: stream.written, as: UTF8.self).hasSuffix("request")
        )
        XCTAssertFalse(stream.isCancelled)
        stream.instanceSends(Data("response".utf8))
        stream.instanceFinishes()
        let response = try await client.receiveUntilClosed()
        XCTAssertEqual(String(decoding: response, as: UTF8.self), "response")

        // The instance finishes first: the client still delivers.
        let second = LoopbackClient(port: fixture.port)
        let secondStream = try await establish(second, fixture)
        secondStream.instanceSends(Data("goodbye".utf8))
        secondStream.instanceFinishes()
        let goodbye = try await second.receiveUntilClosed()
        XCTAssertEqual(String(decoding: goodbye, as: UTF8.self), "goodbye")
        XCTAssertFalse(secondStream.isCancelled)
        try await second.send("tail")
        try await second.finish()
        try await eventually { secondStream.isWriteClosed }
        XCTAssertTrue(
            String(decoding: secondStream.written, as: UTF8.self)
                .hasSuffix("tail")
        )
    }

    func testAFinishedSideDoesNotHoldTheConnectionOpenForever() async throws {
        let fixture = try await makeListener(limits: limits(lingerSeconds: 0.3))
        let client = LoopbackClient(port: fixture.port)
        let stream = try await establish(client, fixture)

        // The client finished and the instance never does.
        try await client.finish()
        try await eventually { stream.isWriteClosed }
        let rest = try await client.receiveUntilClosed()

        XCTAssertEqual(rest, Data())
        try await eventually { stream.isCancelled }
    }

    func testASlowInstanceSlowsTheClientInsteadOfFillingMemory() async throws {
        let chunk = 16 * 1024
        let fixture = try await makeListener(limits: limits(chunkBytes: chunk))
        let client = LoopbackClient(port: fixture.port)
        let stream = try await establish(client, fixture)
        let upload = Data((0..<(2 * 1024 * 1024)).map { UInt8($0 % 251) })

        let offeredBefore = stream.offeredBytes
        stream.holdWrites()
        let sending = Task {
            try await client.send(upload)
        }
        try await eventually { stream.offeredBytes > offeredBefore }
        // The relay took one chunk and is waiting for the transport to
        // accept it. However long that takes, it reads nothing more.
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertLessThanOrEqual(stream.offeredBytes - offeredBefore, chunk)

        stream.releaseWrites()
        try await sending.value
        try await eventually {
            stream.written.count
                == self.canonicalHead.utf8.count + upload.count
        }
        XCTAssertEqual(Data(stream.written.suffix(upload.count)), upload)
        client.close()
    }

    func testASlowClientSlowsTheInstanceInsteadOfFillingMemory() async throws {
        let chunk = 64 * 1024
        let total = 16 * 1024 * 1024
        let fixture = try await makeListener(limits: limits(chunkBytes: chunk))
        let client = LoopbackClient(port: fixture.port)
        let stream = try await establish(client, fixture)

        // The instance has 16 MiB ready; the client reads nothing yet.
        let deliveredBefore = stream.deliveredBytes
        stream.instanceOffers(totalBytes: total)
        try await eventually { stream.deliveredBytes > deliveredBefore }
        try await Task.sleep(for: .milliseconds(500))
        let deliveredWhileIdle = stream.deliveredBytes - deliveredBefore
        // What left the stand-in sits in socket buffers, not in the relay:
        // far less than the instance has ready.
        XCTAssertLessThan(deliveredWhileIdle, total / 2)

        let received = try await client.receiveAndCount(count: total)
        XCTAssertEqual(received, total)
        XCTAssertEqual(stream.deliveredBytes - deliveredBefore, total)
        client.close()
    }

    // MARK: Failures of the path

    func testAPathThatCannotServeAConnectionIsReportedAndAnsweredAsAFailedProxy() async throws {
        struct Case {
            let name: String
            let streams: StandInStreamSource.Mode
            let answer: StandInInstanceAnswer
            let status: Int
            let failure: DirectConnectFailure
            var limits = DirectConnectLimits.patient
        }
        let cases = [
            Case(name: "the stream cannot be opened", streams: .fail, answer: .none, status: 502, failure: .streamUnavailable),
            Case(name: "the stream does not open in time", streams: .hang, answer: .none, status: 504, failure: .streamUnavailable, limits: limits(openSeconds: 0.3)),
            Case(name: "the instance refuses the authority", streams: .open, answer: .bytes("HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"), status: 403, failure: .instanceRefused(403)),
            Case(name: "the ingress is stopped", streams: .open, answer: .bytes("HTTP/1.1 503 Service Unavailable\r\n\r\n"), status: 503, failure: .instanceRefused(503)),
            Case(name: "the answer is not HTTP", streams: .open, answer: .bytes("\u{16}\u{03}\u{01}\r\n\r\n"), status: 502, failure: .noAnswer),
            Case(name: "the instance closes without answering", streams: .open, answer: .finish, status: 502, failure: .noAnswer),
            Case(name: "the instance never answers", streams: .open, answer: .none, status: 504, failure: .noAnswer, limits: limits(answerSeconds: 0.3)),
        ]

        for testCase in cases {
            let fixture = try await makeListener(
                limits: testCase.limits,
                streams: testCase.streams
            )
            let client = LoopbackClient(port: fixture.port)
            try await client.send(canonicalHead)
            if testCase.streams == .open {
                let stream = try await fixture.streams.waitForStream()
                switch testCase.answer {
                case .bytes(let text):
                    stream.instanceSends(Data(text.utf8))
                case .finish:
                    stream.instanceFinishes()
                case .none:
                    break
                }
            }

            let answer = try await client.receiveUntilClosed()
            XCTAssertTrue(
                String(decoding: answer, as: UTF8.self)
                    .hasPrefix("HTTP/1.1 \(testCase.status) "),
                "\(testCase.name): \(String(decoding: answer, as: UTF8.self))"
            )
            try await eventually { fixture.failures.values == [testCase.failure] }
            if let stream = fixture.streams.opened.first {
                try await eventually { stream.isCancelled }
            }
            client.close()
            await fixture.listener.stop()
        }
    }

    func testClosesAConnectionBeyondItsLimitAtOnceAndReportsIt() async throws {
        let fixture = try await makeListener(limits: limits(maxConnections: 2))
        let first = LoopbackClient(port: fixture.port)
        let firstStream = try await establish(first, fixture)
        let second = LoopbackClient(port: fixture.port)
        _ = try await establish(second, fixture)

        // The third sends a valid head and keeps its side open. It gets no
        // answer and no wait: the listener closes it at once.
        let third = LoopbackClient(port: fixture.port)
        try? await third.send(canonicalHead)
        let answer = try await third.receiveUntilClosed()
        XCTAssertEqual(answer, Data())
        try await eventually { fixture.failures.values == [.capacity] }
        XCTAssertEqual(fixture.streams.opened.count, 2)

        // A connection that ends gives its slot back.
        first.close()
        firstStream.instanceFinishes()
        try await eventually { firstStream.isCancelled }
        try await eventually {
            let retry = LoopbackClient(port: fixture.port)
            defer { retry.close() }
            try? await retry.send(self.canonicalHead)
            // Served: a third stream is opened for it. Still over the
            // limit: it is closed.
            let closed = Recorder<Data>()
            let reading = Task {
                closed.record(try await retry.receiveUntilClosed())
            }
            defer { reading.cancel() }
            try await eventually {
                fixture.streams.opened.count >= 3 || !closed.values.isEmpty
            }
            return fixture.streams.opened.count >= 3
        }
        second.close()
    }

    func testStoppingClosesARefusalThatIsStillWaitingForItsClient() async throws {
        // The client has up to a minute to read a refusal and close.
        let fixture = try await makeListener(limits: limits(headSeconds: 60))
        let client = LoopbackClient(port: fixture.port)
        try await client.send("CONNECT other.example:443 HTTP/1.1\r\n\r\n")
        let refusal = DirectConnectWire.refusal(403)
        let answer = try await client.receive(count: refusal.count)
        XCTAssertEqual(answer, refusal)

        // The client keeps its side open, so the refusal is still in flight
        // when the listener stops. Stopping ends it.
        let clock = ContinuousClock()
        let stopped = clock.now
        await fixture.listener.stop()
        let rest = try await client.receiveUntilClosed()
        XCTAssertEqual(rest, Data())
        XCTAssertLessThan(clock.now - stopped, .seconds(10))
    }

    func testTheStatusIsWrittenBeforeTheFailureIsReportedSoAStopCannotCostIt() async throws {
        // What the path controller does on a failure: stop the listener.
        for attempt in 0..<20 {
            let streams = StandInStreamSource(mode: attempt % 2 == 0 ? .fail : .open)
            let holder = Recorder<DirectConnectListener>()
            let listener = DirectConnectListener(
                authority: authority,
                limits: .patient,
                openStream: {
                    try await streams.open()
                },
                onFailure: { _ in
                    Task {
                        await holder.values.first?.stop()
                    }
                }
            )
            holder.record(listener)
            let port = try await listener.start()
            let client = LoopbackClient(port: port)
            try await client.send(canonicalHead)
            let expected: Int
            if attempt % 2 == 0 {
                // The stream cannot be opened.
                expected = 502
            } else {
                // The instance refuses the CONNECT.
                let stream = try await streams.waitForStream()
                stream.instanceSends(
                    Data("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n".utf8)
                )
                expected = 403
            }

            let answer = try await client.receiveUntilClosed()
            XCTAssertTrue(
                String(decoding: answer, as: UTF8.self)
                    .hasPrefix("HTTP/1.1 \(expected) "),
                "attempt \(attempt): \(String(decoding: answer, as: UTF8.self))"
            )
            client.close()
            await listener.stop()
        }
    }

    func testListensOnTheLoopbackAddressOnlyOnAPortTheSystemChooses() async throws {
        try assertSocketScanSeesListeners()
        let before = try listeningSockets()
        let fixture = try await makeListener()
        let another = try await makeListener()

        // Exactly two new listeners, both on 127.0.0.1: nothing on the
        // wildcard address and nothing on IPv6.
        XCTAssertEqual(
            try newListeners(since: before),
            [fixture.socket, another.socket]
        )
        XCTAssertNotEqual(fixture.port, another.port)
    }

    func testTheSocketScanSeesListenersOnLoopbackAndWildcardAddressesOfBothFamilies() throws {
        try assertSocketScanSeesListeners()
    }

    func testStoppingClosesEveryConnectionAndItsStream() async throws {
        let fixture = try await makeListener()
        XCTAssertTrue(try listeningSockets().contains(fixture.socket))
        let client = LoopbackClient(port: fixture.port)
        let stream = try await establish(client, fixture)
        let pending = LoopbackClient(port: fixture.port)
        try await pending.send("CONNECT instance.example:443 HTTP/1.1\r\n")

        await fixture.listener.stop()

        let rest = try await client.receiveUntilClosed()
        XCTAssertEqual(rest, Data())
        let unanswered = try await pending.receiveUntilClosed()
        XCTAssertEqual(unanswered, Data())
        try await eventually { stream.isCancelled }
        // Stopping is not a failure of the path.
        XCTAssertEqual(fixture.failures.values, [])

        // The port no longer accepts.
        try await eventually {
            await !LoopbackClient.canConnect(to: fixture.port)
        }
        XCTAssertFalse(try listeningSockets().contains(fixture.socket))
    }

    // MARK: Parsing

    func testTheRequestHeadIsAcceptedOnlyWhenCompleteAndWithinItsLimit() {
        let head = Data(canonicalHead.utf8)
        for prefix in 0..<head.count {
            XCTAssertEqual(
                DirectConnectRequest.parse(
                    head.prefix(prefix),
                    authority: authority,
                    limit: 4096
                ),
                .incomplete,
                "prefix \(prefix)"
            )
        }
        XCTAssertEqual(
            DirectConnectRequest.parse(head, authority: authority, limit: 4096),
            .accepted(earlyData: Data())
        )
        XCTAssertEqual(
            DirectConnectRequest.parse(
                head + Data("early".utf8),
                authority: authority,
                limit: 4096
            ),
            .accepted(earlyData: Data("early".utf8))
        )

        // The limit counts the head up to its blank line.
        let exact = head.count - 4
        XCTAssertEqual(
            DirectConnectRequest.parse(head, authority: authority, limit: exact),
            .accepted(earlyData: Data())
        )
        XCTAssertEqual(
            DirectConnectRequest.parse(
                head,
                authority: authority,
                limit: exact - 1
            ),
            .refused(400)
        )
        // Past the limit with no end in sight is refused without waiting.
        XCTAssertEqual(
            DirectConnectRequest.parse(
                Data(repeating: UInt8(ascii: "a"), count: 4097),
                authority: authority,
                limit: 4096
            ),
            .refused(400)
        )
    }

    func testTheInstanceAnswerIsReadAsAStatusOnly() {
        func parse(_ text: String) -> DirectConnectAnswer {
            DirectConnectAnswer.parse(Data(text.utf8), limit: 4096)
        }

        XCTAssertEqual(parse("HTTP/1.1 200 Connection Established\r\n"), .incomplete)
        XCTAssertEqual(
            parse("HTTP/1.1 200 Connection Established\r\n\r\n"),
            .established(leftover: Data())
        )
        XCTAssertEqual(
            parse("HTTP/1.1 200 OK\r\nServer: x\r\n\r\nhello"),
            .established(leftover: Data("hello".utf8))
        )
        XCTAssertEqual(parse("HTTP/1.1 403 Forbidden\r\n\r\n"), .refused(403))
        XCTAssertEqual(parse("HTTP/1.1 101 Switching Protocols\r\n\r\n"), .refused(101))
        XCTAssertEqual(parse("HTTP/1.0 200 OK\r\n\r\n"), .malformed)
        XCTAssertEqual(parse("HTTP/1.1 20 OK\r\n\r\n"), .malformed)
        XCTAssertEqual(parse("HTTP/1.1 abc OK\r\n\r\n"), .malformed)
        XCTAssertEqual(parse("200 OK\r\n\r\n"), .malformed)
        XCTAssertEqual(
            DirectConnectAnswer.parse(
                Data(repeating: UInt8(ascii: "a"), count: 4097),
                limit: 4096
            ),
            .malformed
        )
    }

    func testOnlyAnHTTPSInstanceNameOnTheDefaultPortHasADirectAuthority() {
        func authority(_ url: String) -> String? {
            DirectAuthority(instanceURL: URL(string: url)!)?.connectTarget
        }

        XCTAssertEqual(authority("https://alex-app.avibe.bot"), "alex-app.avibe.bot:443")
        XCTAssertEqual(authority("https://alex-app.avibe.bot:443/path"), "alex-app.avibe.bot:443")
        XCTAssertEqual(authority("https://Alex-App.Avibe.Bot"), "alex-app.avibe.bot:443")
        XCTAssertNil(authority("http://alex-app.avibe.bot"))
        XCTAssertNil(authority("https://alex-app.avibe.bot:8443"))
        XCTAssertNil(authority("https://owner@alex-app.avibe.bot"))
        XCTAssertNil(authority("https://192.168.1.20"))
        XCTAssertNil(authority("https://[fd12::1]"))
        XCTAssertNil(authority("https://alex_app.avibe.bot"))
        XCTAssertNil(authority("https://alex-app..avibe.bot"))
    }

    // MARK: Fixture

    private struct Fixture {
        let listener: DirectConnectListener
        let port: NWEndpoint.Port

        /// Where the listener must be found: the loopback address, and no
        /// other.
        var socket: ListeningSocket {
            ListeningSocket(address: "127.0.0.1", port: port.rawValue)
        }
        let streams: StandInStreamSource
        let failures: Recorder<DirectConnectFailure>
    }

    /// The standard sizes, with deadlines a loaded simulator cannot miss.
    /// A test about one deadline shortens that one.
    private func limits(
        headSeconds: TimeInterval = 60,
        openSeconds: TimeInterval = 60,
        answerSeconds: TimeInterval = 60,
        maxConnections: Int = 64,
        lingerSeconds: TimeInterval = 60,
        chunkBytes: Int = 64 * 1024
    ) -> DirectConnectLimits {
        DirectConnectLimits(
            headBytes: 4096,
            headSeconds: headSeconds,
            openSeconds: openSeconds,
            answerSeconds: answerSeconds,
            maxConnections: maxConnections,
            lingerSeconds: lingerSeconds,
            chunkBytes: chunkBytes
        )
    }

    private func makeListener(
        limits: DirectConnectLimits = .patient,
        streams mode: StandInStreamSource.Mode = .open
    ) async throws -> Fixture {
        let streams = StandInStreamSource(mode: mode)
        let failures = Recorder<DirectConnectFailure>()
        let listener = DirectConnectListener(
            authority: authority,
            limits: limits,
            openStream: {
                try await streams.open()
            },
            onFailure: {
                failures.record($0)
            }
        )
        let port = try await listener.start()
        addTeardownBlock {
            await listener.stop()
        }
        return Fixture(
            listener: listener,
            port: port,
            streams: streams,
            failures: failures
        )
    }

    /// Completes one CONNECT and returns the stream it is carried on.
    private func establish(
        _ client: LoopbackClient,
        _ fixture: Fixture
    ) async throws -> StandInDirectStream {
        let before = fixture.streams.opened.count
        try await client.send(canonicalHead)
        let stream = try await fixture.streams.waitForStream(count: before + 1)
        stream.instanceSends(Data(established.utf8))
        let answer = try await client.receive(count: established.utf8.count)
        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        return stream
    }
}

// MARK: - Stand-ins shared with the path controller tests

extension DirectConnectLimits {
    /// The standard sizes, with deadlines a loaded simulator cannot miss.
    static let patient = DirectConnectLimits(
        headBytes: 4096,
        headSeconds: 60,
        openSeconds: 60,
        answerSeconds: 60,
        maxConnections: 64,
        lingerSeconds: 60,
        chunkBytes: 64 * 1024
    )
}

struct DirectTestTimeout: Error {}

/// Waits for `condition`. A wait that runs out fails at the line that
/// asked for it, then throws.
func eventually(
    timeout: Duration = .seconds(30),
    file: StaticString = #filePath,
    line: UInt = #line,
    _ condition: @escaping () async throws -> Bool
) async throws {
    let clock = ContinuousClock()
    let deadline = clock.now.advanced(by: timeout)
    while clock.now < deadline {
        if try await condition() {
            return
        }
        try await Task.sleep(for: .milliseconds(20))
    }
    XCTFail(
        "The condition did not become true within \(timeout).",
        file: file,
        line: line
    )
    throw DirectTestTimeout()
}

final class Recorder<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [Value] = []

    var values: [Value] {
        lock.withLock { recorded }
    }

    func record(_ value: Value) {
        lock.withLock {
            recorded.append(value)
        }
    }

    /// Records `value` only if nothing is recorded yet.
    func recordFirst(_ value: Value) -> Bool {
        lock.withLock {
            guard recorded.isEmpty else {
                return false
            }
            recorded.append(value)
            return true
        }
    }
}

enum StandInInstanceAnswer {
    case bytes(String)
    case finish
    case none
}

/// One transport stream, with the test playing the instance on its far end.
final class StandInDirectStream: DirectByteStream, @unchecked Sendable {
    struct Closed: Error {}

    private let lock = NSLock()
    private var fromInstance: [Data] = []
    private var offeredRemaining = 0
    private var instanceFinished = false
    private var pendingRead: (continuation: CheckedContinuation<Data?, Error>, maxLength: Int)?
    private var delivered = 0
    private var toInstance = Data()
    private var offered = 0
    private var writesHeld = false
    private var heldWrites: [(data: Data, continuation: CheckedContinuation<Void, Error>)] = []
    private var writeClosed = false
    private var cancelled = false

    /// Everything the listener wrote that the stand-in has accepted.
    var written: Data {
        lock.withLock { toInstance }
    }

    /// Bytes the listener has tried to write, accepted or not.
    var offeredBytes: Int {
        lock.withLock { offered }
    }

    /// Bytes the listener has read from the instance.
    var deliveredBytes: Int {
        lock.withLock { delivered }
    }

    var isWriteClosed: Bool {
        lock.withLock { writeClosed }
    }

    var isCancelled: Bool {
        lock.withLock { cancelled }
    }

    func instanceSends(_ data: Data) {
        lock.lock()
        fromInstance.append(data)
        let resume = takePendingRead()
        lock.unlock()
        resume?()
    }

    /// The instance has `totalBytes` ready, produced as the listener reads.
    func instanceOffers(totalBytes: Int) {
        lock.lock()
        offeredRemaining += totalBytes
        let resume = takePendingRead()
        lock.unlock()
        resume?()
    }

    func instanceFinishes() {
        lock.lock()
        instanceFinished = true
        let resume = takePendingRead()
        lock.unlock()
        resume?()
    }

    func holdWrites() {
        lock.withLock {
            writesHeld = true
        }
    }

    func releaseWrites() {
        lock.lock()
        writesHeld = false
        let held = heldWrites
        heldWrites = []
        for write in held {
            toInstance.append(write.data)
        }
        lock.unlock()
        for write in held {
            write.continuation.resume()
        }
    }

    func read(maxLength: Int) async throws -> Data? {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            pendingRead = (continuation, maxLength)
            let resume = takePendingRead()
            lock.unlock()
            resume?()
        }
    }

    func write(_ data: Data) async throws {
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            lock.lock()
            guard !cancelled, !writeClosed else {
                lock.unlock()
                continuation.resume(throwing: Closed())
                return
            }
            offered += data.count
            if writesHeld {
                heldWrites.append((data, continuation))
                lock.unlock()
                return
            }
            toInstance.append(data)
            lock.unlock()
            continuation.resume()
        }
    }

    func closeWrite() async throws {
        try lock.withLock {
            guard !cancelled else {
                throw Closed()
            }
            writeClosed = true
        }
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let read = pendingRead
        pendingRead = nil
        let held = heldWrites
        heldWrites = []
        lock.unlock()
        read?.continuation.resume(throwing: Closed())
        for write in held {
            write.continuation.resume(throwing: Closed())
        }
    }

    /// With the lock held: what completes the waiting read, if anything can.
    private func takePendingRead() -> (() -> Void)? {
        guard let read = pendingRead else {
            return nil
        }
        if cancelled {
            pendingRead = nil
            return { read.continuation.resume(throwing: Closed()) }
        }
        if !fromInstance.isEmpty {
            var next = fromInstance.removeFirst()
            if next.count > read.maxLength {
                fromInstance.insert(Data(next.dropFirst(read.maxLength)), at: 0)
                next = Data(next.prefix(read.maxLength))
            }
            delivered += next.count
            pendingRead = nil
            return { read.continuation.resume(returning: next) }
        }
        if offeredRemaining > 0 {
            let count = min(read.maxLength, offeredRemaining)
            offeredRemaining -= count
            delivered += count
            pendingRead = nil
            return {
                read.continuation.resume(
                    returning: Data(repeating: 0x5A, count: count)
                )
            }
        }
        if instanceFinished {
            pendingRead = nil
            return { read.continuation.resume(returning: nil) }
        }
        return nil
    }
}

/// One TCP socket this process is listening on.
struct ListeningSocket: Hashable, CustomStringConvertible {
    let address: String
    let port: UInt16

    var description: String {
        "\(address):\(port)"
    }
}

struct SocketScanError: Error, CustomStringConvertible {
    let call: String
    let descriptor: Int32
    let code: Int32

    var description: String {
        "\(call) failed on descriptor \(descriptor) with errno \(code)"
    }
}

/// Every TCP socket this process is listening on, on any address of either
/// family, read from the kernel's own state for the socket. This is how a
/// test sees a listener from outside, both that one exists and that none
/// does. A descriptor that is not a socket, or that goes away during the
/// scan, is skipped. Any other failure throws, so a scan that cannot see is
/// never read as "nothing there"; `assertSocketScanSeesListeners` shows that
/// it can.
func listeningSockets() throws -> Set<ListeningSocket> {
    // `TCPS_LISTEN` in <netinet/tcp_fsm.h>.
    let listening: UInt8 = 1
    var sockets = Set<ListeningSocket>()
    for descriptor in 0..<getdtablesize() {
        var type: Int32 = 0
        var length = socklen_t(MemoryLayout<Int32>.size)
        if getsockopt(descriptor, SOL_SOCKET, SO_TYPE, &type, &length) != 0 {
            let code = errno
            guard code == EBADF || code == ENOTSOCK else {
                throw SocketScanError(
                    call: "getsockopt(SO_TYPE)",
                    descriptor: descriptor,
                    code: code
                )
            }
            continue
        }
        guard type == SOCK_STREAM else {
            continue
        }

        var storage = sockaddr_storage()
        var storageLength = socklen_t(MemoryLayout<sockaddr_storage>.size)
        let named = withUnsafeMutablePointer(to: &storage) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(descriptor, $0, &storageLength)
            }
        }
        if named != 0 {
            let code = errno
            // EINVAL: the socket has been shut down, so it is not listening.
            guard code == EBADF || code == EINVAL else {
                throw SocketScanError(
                    call: "getsockname",
                    descriptor: descriptor,
                    code: code
                )
            }
            continue
        }
        let family = Int32(storage.ss_family)
        guard family == AF_INET || family == AF_INET6 else {
            continue
        }

        var info = tcp_connection_info()
        var infoLength = socklen_t(MemoryLayout<tcp_connection_info>.size)
        if getsockopt(descriptor, IPPROTO_TCP, TCP_CONNECTION_INFO, &info, &infoLength) != 0 {
            let code = errno
            guard code == EBADF else {
                throw SocketScanError(
                    call: "getsockopt(TCP_CONNECTION_INFO)",
                    descriptor: descriptor,
                    code: code
                )
            }
            continue
        }
        guard info.tcpi_state == listening else {
            continue
        }

        var text = [CChar](repeating: 0, count: Int(INET6_ADDRSTRLEN))
        let port: UInt16
        if family == AF_INET {
            port = withUnsafePointer(to: &storage) {
                $0.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                    var address = $0.pointee.sin_addr
                    inet_ntop(AF_INET, &address, &text, socklen_t(text.count))
                    return UInt16(bigEndian: $0.pointee.sin_port)
                }
            }
        } else {
            port = withUnsafePointer(to: &storage) {
                $0.withMemoryRebound(to: sockaddr_in6.self, capacity: 1) {
                    var address = $0.pointee.sin6_addr
                    inet_ntop(AF_INET6, &address, &text, socklen_t(text.count))
                    return UInt16(bigEndian: $0.pointee.sin6_port)
                }
            }
        }
        sockets.insert(ListeningSocket(address: String(cString: text), port: port))
    }
    return sockets
}

/// The listeners that exist now and did not at `baseline`.
func newListeners(
    since baseline: Set<ListeningSocket>
) throws -> Set<ListeningSocket> {
    try listeningSockets().subtracting(baseline)
}

/// A plain listening socket made with the system calls, on a port the
/// system chooses: something the scan must see.
final class ControlListener {
    struct Unavailable: Error {
        let step: String
        let code: Int32
    }

    let socket: ListeningSocket
    private let descriptor: Int32

    init(family: Int32, address: String) throws {
        let descriptor = Darwin.socket(family, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            throw Unavailable(step: "socket", code: errno)
        }
        var bound: Int32 = -1
        var storage = sockaddr_storage()
        var length = socklen_t(MemoryLayout<sockaddr_storage>.size)
        if family == AF_INET {
            var name = sockaddr_in()
            name.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            name.sin_family = sa_family_t(AF_INET)
            inet_pton(AF_INET, address, &name.sin_addr)
            bound = withUnsafePointer(to: &name) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        } else {
            var name = sockaddr_in6()
            name.sin6_len = UInt8(MemoryLayout<sockaddr_in6>.size)
            name.sin6_family = sa_family_t(AF_INET6)
            inet_pton(AF_INET6, address, &name.sin6_addr)
            bound = withUnsafePointer(to: &name) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in6>.size))
                }
            }
        }
        guard bound == 0, listen(descriptor, 1) == 0 else {
            let code = errno
            Darwin.close(descriptor)
            throw Unavailable(step: "bind or listen on \(address)", code: code)
        }
        let named = withUnsafeMutablePointer(to: &storage) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(descriptor, $0, &length)
            }
        }
        guard named == 0 else {
            let code = errno
            Darwin.close(descriptor)
            throw Unavailable(step: "getsockname", code: code)
        }
        let port: UInt16 = withUnsafePointer(to: &storage) {
            family == AF_INET
                ? $0.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                    UInt16(bigEndian: $0.pointee.sin_port)
                }
                : $0.withMemoryRebound(to: sockaddr_in6.self, capacity: 1) {
                    UInt16(bigEndian: $0.pointee.sin6_port)
                }
        }
        self.descriptor = descriptor
        socket = ListeningSocket(address: address, port: port)
    }

    func close() {
        Darwin.close(descriptor)
    }
}

/// Shows that the scan sees a listener on the loopback and the wildcard
/// address of both families, and stops seeing each when it closes. A test
/// that relies on the scan finding nothing calls this first, so a scan that
/// cannot see fails the test instead of passing it.
func assertSocketScanSeesListeners(
    file: StaticString = #filePath,
    line: UInt = #line
) throws {
    let controls = try [
        (AF_INET, "127.0.0.1"),
        (AF_INET, "0.0.0.0"),
        (AF_INET6, "::1"),
        (AF_INET6, "::"),
    ].map { try ControlListener(family: $0.0, address: $0.1) }
    let open = try listeningSockets()
    for control in controls {
        XCTAssertTrue(
            open.contains(control.socket),
            "The socket scan does not see a listener on \(control.socket).",
            file: file,
            line: line
        )
        control.close()
    }
    let closed = try listeningSockets()
    for control in controls {
        XCTAssertFalse(
            closed.contains(control.socket),
            "The socket scan still reports \(control.socket) after it closed.",
            file: file,
            line: line
        )
    }
}

/// What a transport's `openStream` does in a test.
final class StandInStreamSource: @unchecked Sendable {
    enum Mode: Equatable {
        case open
        case fail
        /// Never answers until its task is cancelled.
        case hang
    }

    struct Unavailable: Error {}

    private let lock = NSLock()
    private var mode: Mode
    private var streams: [StandInDirectStream] = []

    init(mode: Mode = .open) {
        self.mode = mode
    }

    var opened: [StandInDirectStream] {
        lock.withLock { streams }
    }

    func setMode(_ mode: Mode) {
        lock.withLock {
            self.mode = mode
        }
    }

    func open() async throws -> any DirectByteStream {
        switch lock.withLock({ mode }) {
        case .open:
            let stream = StandInDirectStream()
            lock.withLock {
                streams.append(stream)
            }
            return stream
        case .fail:
            throw Unavailable()
        case .hang:
            try await Task.sleep(for: .seconds(3600))
            throw Unavailable()
        }
    }

    func waitForStream(
        count: Int = 1,
        timeout: Duration = .seconds(10)
    ) async throws -> StandInDirectStream {
        try await eventually(timeout: timeout) {
            self.opened.count >= count
        }
        return opened[count - 1]
    }
}

/// A plain TCP client on loopback: what WebKit's network process is to the
/// listener.
final class LoopbackClient: @unchecked Sendable {
    private let connection: NWConnection
    private let queue = DispatchQueue(label: "bot.avibe.ios.tests.direct-client")
    private let lock = NSLock()
    private var buffered = Data()
    private var finished = false

    init(port: NWEndpoint.Port) {
        connection = NWConnection(
            host: "127.0.0.1",
            port: port,
            using: .tcp
        )
        connection.start(queue: queue)
    }

    func send(_ text: String) async throws {
        try await send(Data(text.utf8))
    }

    func send(_ data: Data) async throws {
        try await send(data, isFinal: false)
    }

    /// Finishes this side; the connection stays open for reading.
    func finish() async throws {
        try await send(nil, isFinal: true)
    }

    private func send(_ data: Data?, isFinal: Bool) async throws {
        let watchdog = giveUpAfterThirtySeconds()
        defer {
            watchdog.cancel()
        }
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            connection.send(
                content: data,
                contentContext: isFinal ? .finalMessage : .defaultMessage,
                isComplete: isFinal,
                completion: .contentProcessed { error in
                    if let error {
                        continuation.resume(throwing: error)
                    } else {
                        continuation.resume()
                    }
                }
            )
        }
    }

    func receive(count: Int) async throws -> Data {
        while lock.withLock({ buffered.count }) < count {
            if try await !receiveMore(),
               lock.withLock({ buffered.count }) < count
            {
                throw DirectTestTimeout()
            }
        }
        return lock.withLock {
            let data = Data(buffered.prefix(count))
            buffered.removeFirst(count)
            return data
        }
    }

    /// Reads `count` bytes without keeping them.
    func receiveAndCount(count: Int) async throws -> Int {
        var received = lock.withLock {
            let held = buffered.count
            buffered.removeAll()
            return held
        }
        while received < count {
            guard try await receiveMore() else {
                break
            }
            received += lock.withLock {
                let held = buffered.count
                buffered.removeAll()
                return held
            }
        }
        return received
    }

    /// Everything the peer sends until it closes, with or without an error.
    func receiveUntilClosed() async throws -> Data {
        while (try? await receiveMore()) == true {}
        return lock.withLock {
            let data = buffered
            buffered.removeAll()
            return data
        }
    }

    /// Whether a new TCP connection to `port` is accepted.
    static func canConnect(to port: NWEndpoint.Port) async -> Bool {
        let connection = NWConnection(host: "127.0.0.1", port: port, using: .tcp)
        let answered = Recorder<Bool>()
        defer {
            connection.cancel()
        }
        return await withCheckedContinuation { continuation in
            let answer: @Sendable (Bool) -> Void = { connected in
                if answered.recordFirst(connected) {
                    continuation.resume(returning: connected)
                }
            }
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    answer(true)
                case .waiting, .failed, .cancelled:
                    answer(false)
                default:
                    break
                }
            }
            connection.start(queue: .global())
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
                answer(false)
            }
        }
    }

    func close() {
        connection.cancel()
    }

    /// A wrong relay then fails a test instead of hanging it.
    private func giveUpAfterThirtySeconds() -> Task<Void, Error> {
        Task { [connection] in
            try await Task.sleep(for: .seconds(30))
            connection.cancel()
        }
    }

    /// One read. `false` once the peer has finished.
    private func receiveMore() async throws -> Bool {
        if lock.withLock({ finished }) {
            return false
        }
        let watchdog = giveUpAfterThirtySeconds()
        defer {
            watchdog.cancel()
        }
        return try await withCheckedThrowingContinuation { continuation in
            connection.receive(
                minimumIncompleteLength: 1,
                maximumLength: 1_048_576
            ) { data, _, isComplete, error in
                self.lock.withLock {
                    if let data {
                        self.buffered.append(data)
                    }
                    if isComplete || error != nil {
                        self.finished = true
                    }
                }
                if let error, data == nil {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: !isComplete)
                }
            }
        }
    }
}
