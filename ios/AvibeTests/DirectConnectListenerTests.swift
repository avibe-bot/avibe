import Network
import XCTest
@testable import Avibe

/// The loopback CONNECT proxy, driven over real loopback sockets. Stand-in
/// streams play the direct path and the public origin, and a stand-in plan
/// plays the path controller, so nothing here needs a transport or a
/// network.
final class DirectConnectListenerTests: XCTestCase {
    private let host = "instance.example"
    private let canonicalHead =
        "CONNECT instance.example:443 HTTP/1.1\r\nHost: instance.example:443\r\n\r\n"
    private let established = "HTTP/1.1 200 Connection Established\r\n\r\n"

    // MARK: Refusals

    func testAnswersConnectOnlyForAnAllowedHostOnPort443() async throws {
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
            ("an ownership check with another nonce", "OWNER not-this-listener HTTP/1.1\r\n\r\n", 405),
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

        // A refused request never reaches the plan or either backend.
        XCTAssertEqual(fixture.plans.planCount, 0)
        XCTAssertEqual(fixture.direct.opened.count, 0)
        XCTAssertEqual(fixture.publicOrigin.opened.count, 0)
    }

    func testAHostIsRefusedUntilItIsAllowed() async throws {
        let fixture = try await makeListener(allowedHosts: [])
        let refused = LoopbackClient(port: fixture.port)
        try await refused.send(canonicalHead)
        let answer = try await refused.receiveUntilClosed()
        XCTAssertTrue(String(decoding: answer, as: UTF8.self).hasPrefix("HTTP/1.1 403 "))

        await fixture.listener.allowHost(host)
        let served = LoopbackClient(port: fixture.port)
        _ = try await establish(served, fixture)
        served.close()
    }

    func testAPlanThatRefusesTheHostAnswers403() async throws {
        let fixture = try await makeListener()
        fixture.plans.set(refuse: true)
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead)
        let answer = try await client.receiveUntilClosed()

        XCTAssertTrue(String(decoding: answer, as: UTF8.self).hasPrefix("HTTP/1.1 403 "))
        XCTAssertEqual(fixture.direct.opened.count, 0)
        XCTAssertEqual(fixture.publicOrigin.opened.count, 0)
    }

    func testAHeadThatNeverCompletesIsClosedWithoutAnAnswer() async throws {
        let fixture = try await makeListener(limits: limits(headSeconds: 0.3))
        let client = LoopbackClient(port: fixture.port)

        try await client.send("CONNECT instance.example:443 HTTP/1.1\r\nHost: ")
        let answer = try await client.receiveUntilClosed()

        XCTAssertEqual(answer, Data())
        XCTAssertEqual(fixture.plans.planCount, 0)
    }

    func testBytesBeyondTheEarlyLimitCloseTheConnectionBeforeAnyBackendIsDialled() async throws {
        let fixture = try await makeListener(limits: limits(earlyBytes: 8))
        let client = LoopbackClient(port: fixture.port)

        try await client.send(canonicalHead + String(repeating: "E", count: 64))
        let answer = try await client.receiveUntilClosed()

        XCTAssertEqual(answer, Data())
        XCTAssertEqual(fixture.plans.planCount, 0)
        XCTAssertEqual(fixture.direct.opened.count, 0)
        XCTAssertEqual(fixture.publicOrigin.opened.count, 0)
    }

    // MARK: Direct relay

    func testRelaysAnAcceptedConnectBehindTheCanonicalHead() async throws {
        let fixture = try await makeListener()
        let client = LoopbackClient(port: fixture.port)

        // WebKit's own proxy headers, a host in another case, and bytes
        // sent before the answer.
        try await client.send(
            "CONNECT Instance.Example:443 HTTP/1.1\r\nHost: instance.example\r\nUser-Agent: test\r\nProxy-Connection: keep-alive\r\n\r\nEARLY"
        )
        let stream = try await fixture.direct.waitForStream()
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
        XCTAssertEqual(fixture.plans.directFailures, 0)
        XCTAssertEqual(fixture.publicOrigin.opened.count, 0)
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

    // MARK: Selection and the commit point

    func testADirectCandidateThatFailsAndAPublicOriginThatFailsAreAnsweredAsAFailedProxy() async throws {
        struct Case {
            let name: String
            let streams: StandInStreamSource.Mode
            let answer: StandInInstanceAnswer
            let status: Int
            var limits = DirectConnectLimits.patient
        }
        let cases = [
            Case(name: "the stream cannot be opened", streams: .fail, answer: .none, status: 502),
            Case(name: "the stream does not open in time", streams: .hang, answer: .none, status: 504, limits: limits(openSeconds: 0.3)),
            Case(name: "the instance refuses the authority", streams: .open, answer: .bytes("HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"), status: 403),
            Case(name: "the ingress is stopped", streams: .open, answer: .bytes("HTTP/1.1 503 Service Unavailable\r\n\r\n"), status: 503),
            Case(name: "the answer is not HTTP", streams: .open, answer: .bytes("\u{16}\u{03}\u{01}\r\n\r\n"), status: 502),
            Case(name: "the instance closes without answering", streams: .open, answer: .finish, status: 502),
            Case(name: "the instance never answers", streams: .open, answer: .none, status: 504, limits: limits(answerSeconds: 0.3)),
        ]

        for testCase in cases {
            let fixture = try await makeListener(
                limits: testCase.limits,
                direct: testCase.streams,
                publicOrigin: .fail
            )
            let client = LoopbackClient(port: fixture.port)
            try await client.send(canonicalHead)
            if testCase.streams == .open {
                let stream = try await fixture.direct.waitForStream()
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
            // The failure reaches the controller once, and the public
            // origin was tried as soon as direct had settled.
            try await eventually { fixture.plans.directFailures == 1 }
            XCTAssertEqual(fixture.publicOrigin.attempts, 1, testCase.name)
            if let stream = fixture.direct.opened.first {
                try await eventually { stream.isCancelled }
            }
            client.close()
            await fixture.listener.stop()
        }
    }

    func testAFailedDirectCandidateFallsBackToThePublicOriginBeforeTheCommitPoint() async throws {
        let fixture = try await makeListener(direct: .fail)
        let client = LoopbackClient(port: fixture.port)

        try await client.send(canonicalHead + "HELLO")
        let publicStream = try await fixture.publicOrigin.waitForStream()
        let answer = try await client.receive(count: established.utf8.count)

        // The App answers the CONNECT itself; the public origin gets
        // WebKit's own bytes and never a CONNECT head.
        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        try await eventually { publicStream.written.count == 5 }
        XCTAssertEqual(String(decoding: publicStream.written, as: UTF8.self), "HELLO")
        XCTAssertEqual(fixture.plans.directFailures, 1)

        publicStream.instanceSends(Data("WORLD".utf8))
        let reply = try await client.receive(count: 5)
        XCTAssertEqual(String(decoding: reply, as: UTF8.self), "WORLD")
        client.close()
    }

    func testAPlanWithoutADirectCandidateGoesStraightToThePublicOrigin() async throws {
        let fixture = try await makeListener()
        fixture.plans.set(direct: false)
        let client = LoopbackClient(port: fixture.port)

        try await client.send(canonicalHead)
        let publicStream = try await fixture.publicOrigin.waitForStream()
        let answer = try await client.receive(count: established.utf8.count)

        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        XCTAssertEqual(fixture.direct.attempts, 0)
        XCTAssertEqual(publicStream.written, Data())
        client.close()
    }

    func testTheHedgeDialsThePublicOriginWhenDirectIsSlowAndTheFirstReadyWins() async throws {
        let fixture = try await makeListener(
            limits: limits(hedgeSeconds: 0.2),
            direct: .hang
        )
        let client = LoopbackClient(port: fixture.port)

        try await client.send(canonicalHead)
        _ = try await fixture.publicOrigin.waitForStream()
        let answer = try await client.receive(count: established.utf8.count)

        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        XCTAssertEqual(fixture.direct.attempts, 1)
        // The loser was cancelled, and a cancelled candidate is not a
        // failure of the path.
        XCTAssertEqual(fixture.plans.directFailures, 0)
        client.close()
    }

    func testAFencedPlanIsReplannedOnceAndThenRefusedWithoutA200() async throws {
        let fixture = try await makeListener()
        fixture.plans.set(current: false)
        let client = LoopbackClient(port: fixture.port)

        try await client.send(canonicalHead)
        let stream = try await fixture.direct.waitForStream()
        stream.instanceSends(Data(established.utf8))
        let second = try await fixture.direct.waitForStream(count: 2)
        second.instanceSends(Data(established.utf8))
        let answer = try await client.receiveUntilClosed()

        XCTAssertTrue(String(decoding: answer, as: UTF8.self).hasPrefix("HTTP/1.1 503 "))
        XCTAssertEqual(fixture.plans.planCount, 2)
        try await eventually { stream.isCancelled && second.isCancelled }
        // No byte of WebKit's reached either fenced stream past the head.
        XCTAssertEqual(String(decoding: stream.written, as: UTF8.self), canonicalHead)
        XCTAssertEqual(String(decoding: second.written, as: UTF8.self), canonicalHead)
    }

    func testAPlanFencedOnceIsServedByItsReplacement() async throws {
        let fixture = try await makeListener()
        fixture.plans.set(current: false)
        let client = LoopbackClient(port: fixture.port)

        try await client.send(canonicalHead)
        let stream = try await fixture.direct.waitForStream()
        stream.instanceSends(Data(established.utf8))
        try await eventually { fixture.plans.planCount == 2 }
        fixture.plans.set(current: true)
        let second = try await fixture.direct.waitForStream(count: 2)
        second.instanceSends(Data(established.utf8))
        let answer = try await client.receive(count: established.utf8.count)

        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        try await eventually { stream.isCancelled }
        XCTAssertFalse(second.isCancelled)
        client.close()
    }

    func testACommittedTunnelIsNeverRedialledOrReplayed() async throws {
        let fixture = try await makeListener()
        let client = LoopbackClient(port: fixture.port)
        let stream = try await establish(client, fixture)
        try await client.send("request")
        try await eventually {
            String(decoding: stream.written, as: UTF8.self).hasSuffix("request")
        }

        // The direct stream breaks after the commit point.
        stream.cancel()
        let rest = try await client.receiveUntilClosed()

        XCTAssertEqual(rest, Data())
        XCTAssertEqual(fixture.plans.planCount, 1)
        XCTAssertEqual(fixture.direct.attempts, 1)
        XCTAssertEqual(fixture.publicOrigin.attempts, 0)
    }

    func testClosingRelaysCountsCommittedOnesPerHostAndKeepsTheOtherBackend() async throws {
        let fixture = try await makeListener()
        let first = LoopbackClient(port: fixture.port)
        let firstStream = try await establish(first, fixture)
        let second = LoopbackClient(port: fixture.port)
        let secondStream = try await establish(second, fixture)
        fixture.plans.set(direct: false)
        let third = LoopbackClient(port: fixture.port)
        let publicStream = try await establishPublic(third, fixture)

        let closed = await fixture.listener.closeRelays(backend: .direct)

        XCTAssertEqual(closed, [host: 2])
        try await eventually { firstStream.isCancelled && secondStream.isCancelled }
        let firstRest = try await first.receiveUntilClosed()
        XCTAssertEqual(firstRest, Data())
        // The public relay is untouched and still carries bytes.
        XCTAssertFalse(publicStream.isCancelled)
        try await third.send("still")
        try await eventually {
            String(decoding: publicStream.written, as: UTF8.self) == "still"
        }

        // Nothing left to close is an empty count, not a closure.
        let again = await fixture.listener.closeRelays(backend: .direct)
        XCTAssertEqual(again, [:])
        let all = await fixture.listener.closeRelays(backend: nil)
        XCTAssertEqual(all, [host: 1])
        try await eventually { publicStream.isCancelled }
        second.close()
        third.close()
    }

    func testARelayCommittedAfterAnEventIsNotClosedByThatEventsLateClose() async throws {
        let fixture = try await makeListener()
        fixture.plans.set(generation: 5)
        let client = LoopbackClient(port: fixture.port)
        let stream = try await establish(client, fixture)

        // An event of generation 5 asked for a close before this relay's
        // plan existed; its close runs only now. The relay is kept.
        let early = await fixture.listener.closeRelays(backend: .direct, before: 5)
        XCTAssertEqual(early, [:])
        XCTAssertFalse(stream.isCancelled)
        // An event after the plan closes it, and counts it once.
        let later = await fixture.listener.closeRelays(backend: .direct, before: 6)
        XCTAssertEqual(later, [host: 1])
        try await eventually { stream.isCancelled }
        client.close()
    }

    func testAFencedConnectIsNeverPlannedAgainUnderAnotherSelectionOrAccount() async throws {
        let fixture = try await makeListener()
        fixture.plans.set(current: false)
        let client = LoopbackClient(port: fixture.port)

        try await client.send(canonicalHead)
        let stream = try await fixture.direct.waitForStream()
        // The selection changes before the first plan's candidate is ready.
        fixture.plans.set(scope: DirectPlanScope(selection: 1, accountEpoch: 0))
        stream.instanceSends(Data(established.utf8))
        let answer = try await client.receiveUntilClosed()

        XCTAssertTrue(String(decoding: answer, as: UTF8.self).hasPrefix("HTTP/1.1 503 "))
        XCTAssertEqual(fixture.plans.planCount, 2)
        // The second plan was not raced: no second stream, no public dial.
        XCTAssertEqual(fixture.direct.attempts, 1)
        XCTAssertEqual(fixture.publicOrigin.attempts, 0)
    }

    /// Committed relays that ended before any event (the stream failed
    /// under one, WebKit reset another, a third ended normally) are not
    /// counted by a later event's close: it did not close them. With a limit
    /// of one connection, each next CONNECT is served only once the previous
    /// relay is gone.
    func testARelayThatEndedBeforeAnEventIsNotCountedByIt() async throws {
        let fixture = try await makeListener(
            limits: limits(maxConnections: 1, lingerSeconds: 0.3)
        )
        let (failing, failed) = try await serveWhenASlotIsFree(fixture)
        failed.cancel()
        _ = try await failing.receiveUntilClosed()

        let (resetting, _) = try await serveWhenASlotIsFree(fixture)
        resetting.close()

        let (ending, ended) = try await serveWhenASlotIsFree(fixture)
        try await ending.finish()
        ended.instanceFinishes()
        _ = try await ending.receiveUntilClosed()

        let (live, _) = try await serveWhenASlotIsFree(fixture)
        let closed = await fixture.listener.closeRelays(backend: .direct, before: 1)
        // Only the live relay this close ended.
        XCTAssertEqual(closed, [host: 1])
        live.close()
    }

    /// The relay's own end and an event's close, in both orders: whichever
    /// comes first decides, and only a close that came first counts.
    func testARelayEndingAndAnEventCloseAreDecidedOnce() {
        let endedFirst = DirectRelayClaim()
        XCTAssertTrue(endedFirst.end())
        XCTAssertFalse(endedFirst.closeByEvent())

        let closedFirst = DirectRelayClaim()
        XCTAssertTrue(closedFirst.closeByEvent())
        XCTAssertFalse(closedFirst.end())
    }

    /// A relay ends on its own and the event's close arrives before the
    /// actor has heard of the end (or after; both orders occur here). The
    /// end is decided before WebKit's side is closed, so once the client
    /// sees the close the event cannot count the relay, whichever order the
    /// actor takes them in.
    func testAnEventCloseRightAfterARelayEndedDoesNotCountIt() async throws {
        for _ in 0..<10 {
            let fixture = try await makeListener()
            let client = LoopbackClient(port: fixture.port)
            let stream = try await establish(client, fixture)

            stream.cancel()
            _ = try await client.receiveUntilClosed()
            let closed = await fixture.listener.closeRelays(backend: .direct, before: 1)

            XCTAssertEqual(closed, [:])
            client.close()
            await fixture.listener.stop()
        }
    }

    func testARetiredScopeEndsOnlyUncommittedWorkOfOlderScopes() async throws {
        let fixture = try await makeListener()
        fixture.plans.set(scope: DirectPlanScope(selection: 3, accountEpoch: 0))
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead)
        let stream = try await fixture.direct.waitForStream()

        // A retirement for an older scope, running late: the CONNECT planned
        // under a newer one is untouched.
        await fixture.listener.retireUncommitted(
            before: 0,
            outside: DirectPlanScope(selection: 2, accountEpoch: 0)
        )
        await fixture.listener.retireUncommitted(
            before: 0,
            outside: DirectPlanScope(selection: 3, accountEpoch: 0)
        )
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertFalse(stream.isCancelled)
        XCTAssertEqual(fixture.plans.planCount, 1)

        // A newer selection: the CONNECT ends before any answer, and its
        // candidate is closed.
        await fixture.listener.retireUncommitted(
            before: 0,
            outside: DirectPlanScope(selection: 4, accountEpoch: 0)
        )
        let answer = try await client.receiveUntilClosed()
        XCTAssertEqual(answer, Data())
        try await eventually { stream.isCancelled }
        XCTAssertEqual(fixture.publicOrigin.attempts, 0)
        XCTAssertEqual(fixture.plans.directFailures, 0)
    }

    func testANewerPathGenerationCancelsAnOlderRaceAndTheConnectIsPlannedAgain() async throws {
        let fixture = try await makeListener(direct: .hang)
        fixture.plans.set(generation: 5)
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead)
        try await eventually { fixture.direct.attempts == 1 }

        // The path generation advances: the hanging candidate is cancelled
        // now, not at its deadline, and the CONNECT gets a fresh plan.
        fixture.direct.setMode(.open)
        fixture.plans.set(generation: 6)
        await fixture.listener.retireUncommitted(
            before: 6,
            outside: DirectPlanScope(selection: 0, accountEpoch: 0)
        )
        let stream = try await fixture.direct.waitForStream()
        stream.instanceSends(Data(established.utf8))
        let answer = try await client.receive(count: established.utf8.count)

        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        XCTAssertEqual(fixture.plans.planCount, 2)
        XCTAssertEqual(fixture.direct.attempts, 2)
        // A cancelled candidate is no failure of the path.
        XCTAssertEqual(fixture.plans.directFailures, 0)
        XCTAssertEqual(fixture.publicOrigin.attempts, 0)
        client.close()
    }

    func testALateRetirementLeavesCommittedRelaysAndWorkPlannedAfterIt() async throws {
        let fixture = try await makeListener()
        fixture.plans.set(generation: 5)
        let committedClient = LoopbackClient(port: fixture.port)
        let committed = try await establish(committedClient, fixture)
        // A second CONNECT, planned after the event, still racing.
        fixture.direct.setMode(.hang)
        fixture.plans.set(generation: 6)
        let racing = LoopbackClient(port: fixture.port)
        try await racing.send(canonicalHead)
        try await eventually { fixture.direct.attempts == 2 }

        await fixture.listener.retireUncommitted(
            before: 6,
            outside: DirectPlanScope(selection: 0, accountEpoch: 0)
        )
        try await Task.sleep(for: .milliseconds(300))

        // Neither is touched: the committed relay still carries bytes, and
        // the newer race was not planned again.
        XCTAssertFalse(committed.isCancelled)
        try await committedClient.send("ping")
        try await eventually {
            String(decoding: committed.written, as: UTF8.self).hasSuffix("ping")
        }
        XCTAssertEqual(fixture.plans.planCount, 2)
        XCTAssertEqual(fixture.direct.attempts, 2)
        committedClient.close()
        racing.close()
    }

    func testAPlanRegisteredAfterItsGenerationWasRetiredIsNeverRaced() async throws {
        let fixture = try await makeListener()
        // The event and its retirement come first; the CONNECT's plan was
        // made under the generation before it.
        await fixture.listener.retireUncommitted(
            before: 6,
            outside: DirectPlanScope(selection: 0, accountEpoch: 0)
        )
        fixture.plans.set(generation: 5)
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead)
        let answer = try await client.receiveUntilClosed()

        XCTAssertTrue(String(decoding: answer, as: UTF8.self).hasPrefix("HTTP/1.1 503 "))
        XCTAssertEqual(fixture.plans.planCount, 2)
        XCTAssertEqual(fixture.direct.attempts, 0)
        XCTAssertEqual(fixture.publicOrigin.attempts, 0)
    }

    func testAPlanOfARetiredScopeIsRefused() async throws {
        let fixture = try await makeListener()
        await fixture.listener.retireUncommitted(
            before: 0,
            outside: DirectPlanScope(selection: 2, accountEpoch: 0)
        )
        fixture.plans.set(scope: DirectPlanScope(selection: 1, accountEpoch: 0))
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead)
        let answer = try await client.receiveUntilClosed()

        XCTAssertTrue(String(decoding: answer, as: UTF8.self).hasPrefix("HTTP/1.1 503 "))
        XCTAssertEqual(fixture.direct.attempts, 0)
        XCTAssertEqual(fixture.publicOrigin.attempts, 0)
    }

    func testBytesSentInTwoChunksPastTheEarlyLimitCloseTheConnectionBeforeTheAnswer() async throws {
        let fixture = try await makeListener(limits: limits(earlyBytes: 10))
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead + "123456")
        let stream = try await fixture.direct.waitForStream()
        try await client.send("789012")
        let answer = try await client.receiveUntilClosed()

        XCTAssertEqual(answer, Data())
        try await eventually { stream.isCancelled }
        XCTAssertEqual(String(decoding: stream.written, as: UTF8.self), canonicalHead)
    }

    // MARK: The reader's early bound, with receives the test completes

    /// The commit point passes with a receive in flight, and that receive
    /// then delivers more than the early bound: legal tunnel bytes, since
    /// WebKit may already have its `200`. They are relayed, not refused.
    func testALargeReceiveCompletingAfterTheCommitPointIsTheTunnels() async throws {
        let receives = ManualReceives()
        let reader = DirectClientReader(
            maximumLength: 64 * 1024,
            earlyLimit: 16,
            initial: Data("ab".utf8),
            receive: receives.receive
        )
        reader.start()
        XCTAssertEqual(receives.pending, 1)
        XCTAssertTrue(reader.startTunnel())

        receives.complete(with: Data(repeating: 0x41, count: 40_000))

        let first = try await reader.read()
        let second = try await reader.read()
        XCTAssertEqual(first, Data("ab".utf8))
        XCTAssertEqual(second?.count, 40_000)
    }

    /// The opposite order: the in-flight receive delivers before the commit
    /// point, past the bound. The reader fails closed, and the commit point
    /// cannot pass.
    func testAReceiveCompletingBeforeTheCommitPointPastTheBoundFailsClosed() async throws {
        let receives = ManualReceives()
        let reader = DirectClientReader(
            maximumLength: 64 * 1024,
            earlyLimit: 16,
            initial: Data("ab".utf8),
            receive: receives.receive
        )
        reader.start()

        receives.complete(with: Data(repeating: 0x41, count: 15))

        XCTAssertFalse(reader.startTunnel())
        do {
            let data = try await reader.read()
            XCTFail("Read \(String(describing: data)) past the early bound.")
        } catch {
            XCTAssertEqual(error as? DirectConnectError, .earlyOverflow)
        }
    }

    func testExactlyTheEarlyBoundIsAccepted() async throws {
        let receives = ManualReceives()
        let reader = DirectClientReader(
            maximumLength: 64 * 1024,
            earlyLimit: 16,
            initial: Data("ab".utf8),
            receive: receives.receive
        )
        reader.start()
        receives.complete(with: Data(repeating: 0x41, count: 14))

        XCTAssertTrue(reader.startTunnel())
        let first = try await reader.read()
        let second = try await reader.read()
        XCTAssertEqual(first?.count, 2)
        XCTAssertEqual(second?.count, 14)
    }

    func testAfterTheCommitPointNothingIsReceivedUntilSomeoneReads() async throws {
        let receives = ManualReceives()
        let reader = DirectClientReader(
            maximumLength: 64 * 1024,
            earlyLimit: 16,
            initial: Data("ab".utf8),
            receive: receives.receive
        )
        reader.start()
        XCTAssertTrue(reader.startTunnel())
        receives.complete(with: Data("cd".utf8))
        XCTAssertEqual(receives.pending, 0)

        let first = try await reader.read()
        let second = try await reader.read()
        XCTAssertEqual(first, Data("ab".utf8))
        XCTAssertEqual(second, Data("cd".utf8))
        XCTAssertEqual(receives.pending, 0)

        let reading = Task { try await reader.read() }
        try await eventually { receives.pending == 1 }
        receives.complete(with: Data(repeating: 0x42, count: 100))
        let tunnel = try await reading.value
        XCTAssertEqual(tunnel?.count, 100)
    }

    /// The watcher's two ends, cancellation and the side ending, in both
    /// orders: each resumes it exactly once (a second resume would crash).
    func testTheGoneWatcherIsResumedOnceWhicheverEndComesFirst() async throws {
        for cancelFirst in [true, false] {
            let receives = ManualReceives()
            let reader = DirectClientReader(
                maximumLength: 64 * 1024,
                earlyLimit: 16,
                initial: Data(),
                receive: receives.receive
            )
            reader.start()
            let ended = Recorder<Bool>()
            let watching = Task {
                do {
                    try await reader.waitUntilGone()
                    ended.record(true)
                } catch {
                    ended.record(false)
                }
            }
            await Task.yield()
            if cancelFirst {
                watching.cancel()
                receives.complete(with: nil, isComplete: true)
            } else {
                receives.complete(with: nil, isComplete: true)
                watching.cancel()
            }
            _ = await watching.result
            XCTAssertEqual(ended.values.count, 1)
        }
    }

    func testSplitChunksPastTheBoundFailClosedEvenWithTheWatcherCancelled() async throws {
        let receives = ManualReceives()
        let reader = DirectClientReader(
            maximumLength: 64 * 1024,
            earlyLimit: 10,
            initial: Data(),
            receive: receives.receive
        )
        reader.start()
        // The watcher is cancelled with a receive in flight, as when a race
        // ends; the reader keeps counting without it.
        let watching = Task { try await reader.waitUntilGone() }
        watching.cancel()
        _ = await watching.result

        receives.complete(with: Data(repeating: 0x43, count: 6))
        XCTAssertEqual(receives.pending, 1)
        receives.complete(with: Data(repeating: 0x43, count: 6))

        XCTAssertFalse(reader.startTunnel())
        do {
            _ = try await reader.read()
            XCTFail("Read past the early bound.")
        } catch {
            XCTAssertEqual(error as? DirectConnectError, .earlyOverflow)
        }
        // The watcher of a later race sees it gone at once.
        try await reader.waitUntilGone()
    }

    func testEarlyBytesBeyondTheBoundInTheHeadNeverStartATunnel() {
        let receives = ManualReceives()
        let reader = DirectClientReader(
            maximumLength: 64 * 1024,
            earlyLimit: 4,
            initial: Data("12345".utf8),
            receive: receives.receive
        )
        reader.start()
        XCTAssertEqual(receives.pending, 0)
        XCTAssertFalse(reader.startTunnel())
    }

    func testAClientThatClosesBeforeTheAnswerEndsTheRaceAndFreesItsSlot() async throws {
        let fixture = try await makeListener(limits: limits(maxConnections: 1))
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead)
        let stream = try await fixture.direct.waitForStream()

        // WebKit gives up on the CONNECT while the instance has not answered.
        client.close()

        try await eventually { stream.isCancelled }
        XCTAssertEqual(fixture.publicOrigin.attempts, 0)
        XCTAssertEqual(fixture.plans.directFailures, 0)
        // Its slot is free: with a limit of one, the next CONNECT is served.
        let (next, _) = try await serveWhenASlotIsFree(fixture)
        next.close()
    }

    func testBytesSentSeparatelyBeforeTheAnswerAreHeldAndForwardedAfterIt() async throws {
        let fixture = try await makeListener()
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead)
        let stream = try await fixture.direct.waitForStream()
        try await client.send("EARLY2")
        try await Task.sleep(for: .milliseconds(200))
        // Nothing but the canonical head before the answer.
        XCTAssertEqual(String(decoding: stream.written, as: UTF8.self), canonicalHead)

        stream.instanceSends(Data(established.utf8))
        _ = try await client.receive(count: established.utf8.count)
        try await eventually {
            String(decoding: stream.written, as: UTF8.self) == self.canonicalHead + "EARLY2"
        }
        try await client.send("ping")
        try await eventually {
            String(decoding: stream.written, as: UTF8.self).hasSuffix("EARLY2ping")
        }
        client.close()
    }

    func testBytesSentSeparatelyBeyondTheEarlyLimitCloseTheConnectionBeforeTheAnswer() async throws {
        let fixture = try await makeListener(limits: limits(earlyBytes: 8))
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead)
        let stream = try await fixture.direct.waitForStream()

        try await client.send(String(repeating: "E", count: 64))
        let answer = try await client.receiveUntilClosed()

        XCTAssertEqual(answer, Data())
        try await eventually { stream.isCancelled }
        XCTAssertEqual(String(decoding: stream.written, as: UTF8.self), canonicalHead)
    }

    // MARK: Limits and lifecycle

    func testClosesAConnectionBeyondItsLimitAtOnce() async throws {
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
        XCTAssertEqual(fixture.direct.opened.count, 2)

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
                fixture.direct.opened.count >= 3 || !closed.values.isEmpty
            }
            return fixture.direct.opened.count >= 3
        }
        second.close()
    }

    /// Every slot is held by relays the App keeps (as public relays are kept
    /// through the background). The listener's own ownership check still
    /// gets an answer, on the one spare connection; a CONNECT on it does not.
    func testTheOwnershipCheckIsAnsweredWithEverySlotHeld() async throws {
        let fixture = try await makeListener(limits: limits(maxConnections: 1))
        let client = LoopbackClient(port: fixture.port)
        _ = try await establish(client, fixture)
        let nonce = await fixture.listener.ownershipNonce

        let owned = await DirectConnectListener.ownershipCheck(
            port: fixture.port.rawValue,
            nonce: nonce,
            within: 10
        )
        XCTAssertTrue(owned)

        let extra = LoopbackClient(port: fixture.port)
        try? await extra.send(canonicalHead)
        let answer = try await extra.receiveUntilClosed()
        XCTAssertEqual(answer, Data())
        XCTAssertEqual(fixture.direct.opened.count, 1)
        client.close()
    }

    /// A request that never completes its head takes the spare connection
    /// first. It holds it only for the spare's short head bound, and the
    /// ownership check, which tries again within its window, still passes.
    func testAnIncompleteRequestOnTheSpareDoesNotStarveTheOwnershipCheck() async throws {
        let fixture = try await makeListener(limits: limits(maxConnections: 1))
        let client = LoopbackClient(port: fixture.port)
        _ = try await establish(client, fixture)
        let slow = LoopbackClient(port: fixture.port)
        try await slow.send("CONNECT instance.example:443 HTTP/1.1\r\nHost: ")
        let nonce = await fixture.listener.ownershipNonce

        let owned = await DirectConnectListener.ownershipCheck(
            port: fixture.port.rawValue,
            nonce: nonce,
            within: 3
        )

        XCTAssertTrue(owned)
        let rest = try await slow.receiveUntilClosed()
        XCTAssertEqual(rest, Data())
        client.close()
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

    func testStoppingDuringABindEndsThatBind() async throws {
        for _ in 0..<5 {
            let listener = DirectConnectListener(
                limits: .patient,
                planner: { _ in nil },
                onListenerFailure: {}
            )
            let binding = Task {
                try await listener.bind(port: nil)
            }
            await listener.stop()
            let ended = Recorder<Bool>()
            Task {
                _ = await binding.result
                ended.record(true)
            }
            try await eventually(timeout: .seconds(10)) { !ended.values.isEmpty }
        }
    }

    // MARK: The lifecycle authority

    private typealias Phase = DirectListenerLifecycle.Phase

    /// A lifecycle brought to `phase` through legal calls only.
    private func makeLifecycle(
        in phase: String,
        published: Bool = false
    ) -> DirectListenerLifecycle {
        let lifecycle = DirectListenerLifecycle()
        func serve() {
            let g = lifecycle.beginBind()!
            XCTAssertTrue(lifecycle.ready(g))
            XCTAssertTrue(lifecycle.confirm(g))
            if published {
                XCTAssertTrue(lifecycle.publish(g) { true })
            }
        }
        switch phase {
        case "idle":
            break
        case "binding":
            _ = lifecycle.beginBind()
        case "verifying":
            XCTAssertTrue(lifecycle.ready(lifecycle.beginBind()!))
        case "serving":
            serve()
        case "closing":
            serve()
            XCTAssertTrue(lifecycle.beginClose())
        case "closed":
            serve()
            XCTAssertTrue(lifecycle.beginClose())
            lifecycle.closeCompleted()
        case "rebinding":
            serve()
            XCTAssertTrue(lifecycle.beginClose())
            lifecycle.closeCompleted()
            _ = lifecycle.beginBind()
        case "failed":
            serve()
            XCTAssertTrue(lifecycle.socketFailed(1))
        case "stopped":
            lifecycle.stop()
        default:
            XCTFail("No phase \(phase).")
        }
        return lifecycle
    }

    /// Admission and publication in every phase: only `serving` admits
    /// relays and publishes, `verifying` admits only the ownership check,
    /// and a stale generation is refused even while serving.
    func testOnlyServingAdmitsRelaysAndPublishes() {
        let table: [(phase: String, admit: DirectListenerLifecycle.Admission, publish: Bool)] = [
            ("idle", .refused, false),
            ("binding", .refused, false),
            ("verifying", .ownershipOnly, false),
            ("serving", .relay, true),
            ("closing", .refused, false),
            ("closed", .refused, false),
            ("rebinding", .refused, false),
            ("failed", .refused, false),
            ("stopped", .refused, false),
        ]
        for row in table {
            let lifecycle = makeLifecycle(in: row.phase)
            let generation: Int
            switch lifecycle.phase {
            case .binding(let g, _), .verifying(let g, _), .serving(let g):
                generation = g
            default:
                generation = 1
            }
            let id = UUID()
            XCTAssertEqual(lifecycle.admit(generation, relay: id), row.admit, row.phase)
            // Only an admitted relay is a candidate.
            XCTAssertEqual(lifecycle.isCandidate(id), row.admit == .relay, row.phase)
            let stale = UUID()
            XCTAssertEqual(
                lifecycle.admit(generation + 1, relay: stale),
                .refused,
                "\(row.phase), a stale generation"
            )
            XCTAssertFalse(lifecycle.isCandidate(stale), row.phase)
            var staleWrote = false
            XCTAssertFalse(lifecycle.publish(generation + 1) { staleWrote = true; return true })
            XCTAssertFalse(staleWrote, "\(row.phase): a stale generation never writes")
            var wrote = false
            XCTAssertEqual(lifecycle.publish(generation) { wrote = true; return true }, row.publish, row.phase)
            XCTAssertEqual(wrote, row.publish, "\(row.phase): the write ran only when allowed")
        }
        // A serving listener with no room for a relay admits the connection
        // for the ownership check only, and records no candidate.
        let full = makeLifecycle(in: "serving")
        XCTAssertEqual(full.admit(1, relay: nil), .ownershipOnly)
        XCTAssertEqual(full.relayIDs, [])
    }

    /// Every failure of the current bind (a bind failure, a rejected
    /// ownership check, a socket failure) is the same terminal state,
    /// before a route or after one, and nothing leaves it.
    func testEveryFailureIsTheOneTerminalStateBeforeOrAfterARoute() {
        for published in [false, true] {
            let bindFails = makeLifecycle(in: published ? "rebinding" : "binding", published: published)
            let g = published ? 2 : 1
            XCTAssertTrue(bindFails.bindFailed(g))
            XCTAssertEqual(bindFails.phase, .failed(generation: g))

            let rejected = makeLifecycle(in: published ? "rebinding" : "binding", published: published)
            XCTAssertTrue(rejected.ready(g))
            XCTAssertTrue(rejected.reject(g))
            XCTAssertEqual(rejected.phase, .failed(generation: g))

            let failsServing = makeLifecycle(in: "serving", published: published)
            XCTAssertTrue(failsServing.socketFailed(1))
            XCTAssertEqual(failsServing.phase, .failed(generation: 1))
            XCTAssertEqual(failsServing.hasPublished, published)

            // Terminal: no bind, no confirmation, no admission, no
            // publication, no grant, no close, and a stop leaves it failed.
            for failed in [bindFails, rejected, failsServing] {
                let id = UUID()
                XCTAssertNil(failed.beginBind())
                XCTAssertFalse(failed.ready(g))
                XCTAssertFalse(failed.confirm(g))
                XCTAssertEqual(failed.admit(g, relay: id), .refused)
                XCTAssertFalse(failed.publish(g) { true })
                XCTAssertFalse(failed.grant(id, host: "instance.example", backend: .direct, generation: 0))
                XCTAssertFalse(failed.beginClose())
                failed.closeCompleted()
                failed.stop()
                if case .failed = failed.phase {
                } else {
                    XCTFail("A failed listener left the failed phase: \(failed.phase)")
                }
                XCTAssertEqual(failed.hasPublished, published)
            }
        }
    }

    /// The transitions that must not happen: confirm after a failure, a
    /// failure of a stale socket, a second failure, a failure of a listener
    /// closed on purpose, binding while serving, a close of what is not
    /// open, and anything after stop.
    func testIllegalTransitionsLeaveThePhaseUnchanged() {
        let confirmAfterFailure = makeLifecycle(in: "verifying")
        XCTAssertTrue(confirmAfterFailure.socketFailed(1))
        XCTAssertFalse(confirmAfterFailure.confirm(1))
        XCTAssertEqual(confirmAfterFailure.phase, .failed(generation: 1))

        let staleFailure = makeLifecycle(in: "rebinding", published: true)
        XCTAssertTrue(staleFailure.ready(2))
        // A stale bind's completion: neither confirms nor rejects the newer
        // bind.
        XCTAssertFalse(staleFailure.confirm(1))
        XCTAssertFalse(staleFailure.reject(1))
        XCTAssertFalse(staleFailure.bindFailed(1))
        XCTAssertEqual(staleFailure.phase, .verifying(generation: 2, rebind: true))
        XCTAssertTrue(staleFailure.confirm(2))
        XCTAssertFalse(staleFailure.socketFailed(1))
        XCTAssertEqual(staleFailure.phase, .serving(generation: 2))

        let twice = makeLifecycle(in: "serving", published: true)
        XCTAssertTrue(twice.socketFailed(1))
        XCTAssertFalse(twice.socketFailed(1))
        XCTAssertFalse(twice.reject(1))
        XCTAssertFalse(twice.beginClose())

        // A listener closed on purpose does not fail: what its bind or its
        // socket reports afterwards is not a failure.
        for phase in ["closing", "closed"] {
            let closedOnPurpose = makeLifecycle(in: phase, published: true)
            let before = closedOnPurpose.phase
            XCTAssertFalse(closedOnPurpose.bindFailed(1), phase)
            XCTAssertFalse(closedOnPurpose.reject(1), phase)
            XCTAssertFalse(closedOnPurpose.socketFailed(1), phase)
            XCTAssertFalse(closedOnPurpose.confirm(1), phase)
            XCTAssertEqual(closedOnPurpose.phase, before, phase)
        }
        // The same for a re-bind under way that the background closes.
        let rebindClosed = makeLifecycle(in: "rebinding", published: true)
        XCTAssertTrue(rebindClosed.beginClose())
        XCTAssertFalse(rebindClosed.bindFailed(2))
        XCTAssertFalse(rebindClosed.reject(2))
        XCTAssertFalse(rebindClosed.ready(2))
        XCTAssertEqual(rebindClosed.phase, .closing(generation: 2))

        let serving = makeLifecycle(in: "serving")
        XCTAssertNil(serving.beginBind())
        XCTAssertFalse(serving.ready(1))

        let idle = makeLifecycle(in: "idle")
        XCTAssertFalse(idle.beginClose())
        XCTAssertFalse(idle.confirm(1))

        let stopped = makeLifecycle(in: "stopped")
        XCTAssertNil(stopped.beginBind())
        XCTAssertFalse(stopped.beginClose())
        XCTAssertFalse(stopped.reject(1))
        XCTAssertEqual(stopped.phase, .stopped)
    }

    /// Closed is reusable and keeps the publication fact; a re-bind is
    /// marked as one; stopped is terminal.
    func testAClosedListenerBindsAgainAndKeepsWhatWasPublished() {
        let lifecycle = makeLifecycle(in: "closed", published: true)
        XCTAssertEqual(lifecycle.phase, .closed)
        let g = lifecycle.beginBind()
        XCTAssertEqual(g, 2)
        XCTAssertEqual(lifecycle.phase, .binding(generation: 2, rebind: true))
        XCTAssertTrue(lifecycle.hasPublished)
        lifecycle.stop()
        XCTAssertNil(lifecycle.beginBind())
        XCTAssertTrue(lifecycle.hasPublished)
    }

    // MARK: The relay ledger

    /// A grant is made once, for a candidate, while the bind that admitted
    /// it serves; nothing but the relay's end removes it.
    func testAGrantIsMadeOnceForACandidateOfTheServingBind() {
        let lifecycle = makeLifecycle(in: "serving")
        let id = UUID()
        // Not admitted: nothing to grant.
        XCTAssertFalse(lifecycle.grant(id, host: host, backend: .direct, generation: 3))
        XCTAssertEqual(lifecycle.admit(1, relay: id), .relay)
        XCTAssertTrue(lifecycle.isCandidate(id))
        XCTAssertEqual(lifecycle.grants, [:])

        XCTAssertTrue(lifecycle.grant(id, host: host, backend: .direct, generation: 3))
        let granted = DirectListenerLifecycle.Grant(host: host, backend: .direct, generation: 3)
        XCTAssertEqual(lifecycle.grants, [id: granted])
        XCTAssertFalse(lifecycle.isCandidate(id))
        // Once: a second grant, with anything else, changes nothing.
        XCTAssertFalse(lifecycle.grant(id, host: "other.example", backend: .publicOrigin, generation: 9))
        XCTAssertEqual(lifecycle.grants, [id: granted])

        lifecycle.end(id)
        XCTAssertEqual(lifecycle.grants, [:])
        XCTAssertEqual(lifecycle.relayIDs, [])
        // An ended relay is neither a candidate nor grantable again.
        XCTAssertFalse(lifecycle.grant(id, host: host, backend: .direct, generation: 3))
    }

    /// Leaving `serving` (a failure, a close) revokes every candidate in
    /// that same step and keeps every grant; a revoked candidate is never
    /// granted, even when the listener serves again.
    func testLeavingServingRevokesEveryCandidateAndKeepsEveryGrant() {
        for transition in ["socket failed", "close and serve again", "stop"] {
            let lifecycle = makeLifecycle(in: "serving", published: true)
            let granted = UUID()
            let candidate = UUID()
            XCTAssertEqual(lifecycle.admit(1, relay: granted), .relay)
            XCTAssertEqual(lifecycle.admit(1, relay: candidate), .relay)
            XCTAssertTrue(lifecycle.grant(granted, host: host, backend: .publicOrigin, generation: 0))

            switch transition {
            case "socket failed":
                XCTAssertTrue(lifecycle.socketFailed(1))
            case "close and serve again":
                XCTAssertTrue(lifecycle.beginClose())
                XCTAssertFalse(lifecycle.isCandidate(candidate), transition)
                lifecycle.closeCompleted()
                let g = lifecycle.beginBind()!
                XCTAssertTrue(lifecycle.ready(g))
                XCTAssertTrue(lifecycle.confirm(g))
            default:
                lifecycle.stop()
            }

            XCTAssertFalse(lifecycle.isCandidate(candidate), transition)
            XCTAssertFalse(
                lifecycle.grant(candidate, host: host, backend: .direct, generation: 0),
                transition
            )
            if transition == "stop" {
                // A stop ends every relay, granted or not.
                XCTAssertEqual(lifecycle.relayIDs, [], transition)
            } else {
                XCTAssertEqual(lifecycle.relayIDs, [granted], transition)
                XCTAssertEqual(lifecycle.grants[granted]?.backend, .publicOrigin, transition)
            }
        }
    }

    func testAFailureOfAnEarlierSocketOfTheSameListenerIsStale() async throws {
        let failures = Recorder<Bool>()
        let listener = DirectConnectListener(
            limits: .patient,
            planner: { _ in nil },
            onListenerFailure: { failures.record(true) }
        )
        addTeardownBlock {
            await listener.stop()
        }
        let lifecycle = await listener.lifecycle
        let first = try await listener.bind(port: nil)
        XCTAssertEqual(first.generation, 1)
        XCTAssertTrue(lifecycle.confirm(first.generation))
        await listener.closeListeningSocket()
        XCTAssertEqual(lifecycle.phase, .closed)
        let second = try await listener.bind(port: first.port)
        XCTAssertEqual(second, DirectBinding(port: first.port, generation: 2))
        XCTAssertEqual(lifecycle.phase, .verifying(generation: 2, rebind: true))
        // The first bind's completion, arriving late, confirms nothing.
        XCTAssertFalse(lifecycle.confirm(first.generation))

        // What Network's queue does for each socket's failure.
        XCTAssertFalse(lifecycle.socketFailed(1))
        XCTAssertEqual(failures.values, [])
        XCTAssertTrue(lifecycle.socketFailed(2))
        await listener.socketDidFail()
        XCTAssertEqual(failures.values, [true])
        XCTAssertEqual(lifecycle.phase, .failed(generation: 2))
    }

    /// The socket failed, a re-bind was rejected, a close began, or the
    /// listener stopped, and a connection arrives before anything else
    /// happens: the actor's one admission call refuses it with no answer,
    /// and no plan, relay, spare, or dial is made for it.
    func testNoConnectionIsAdmittedOnceTheListenerLeftServing() async throws {
        for transition in ["socket failed", "closing", "stopped"] {
            let fixture = try await makeListener()
            let lifecycle = await fixture.listener.lifecycle
            switch transition {
            case "socket failed":
                XCTAssertTrue(lifecycle.socketFailed(1))
            case "closing":
                XCTAssertTrue(lifecycle.beginClose())
            default:
                lifecycle.stop()
            }

            let client = LoopbackClient(port: fixture.port)
            try? await client.send(canonicalHead)
            let answer = try await client.receiveUntilClosed()

            XCTAssertEqual(answer, Data(), transition)
            XCTAssertEqual(fixture.plans.planCount, 0, transition)
            XCTAssertEqual(fixture.direct.attempts, 0, transition)
            XCTAssertEqual(lifecycle.relayIDs, [], transition)
            client.close()
            await fixture.listener.stop()
        }
    }

    /// Work admitted while serving, when the listener then leaves serving:
    /// the authority revokes it in that step, and this actor, which hears
    /// of every such transition afterwards, ends its task. A granted relay
    /// stays. Shown for a socket failure and for a deliberate close.
    func testUncommittedWorkEndsWhenTheListenerLeavesServingAndCommittedRelaysStay() async throws {
        for transition in ["socket failed", "close"] {
            let fixture = try await makeListener()
            let committedClient = LoopbackClient(port: fixture.port)
            let committed = try await establish(committedClient, fixture)
            fixture.direct.setMode(.hang)
            let racing = LoopbackClient(port: fixture.port)
            try await racing.send(canonicalHead)
            try await eventually { fixture.direct.attempts == 2 }

            let lifecycle = await fixture.listener.lifecycle
            XCTAssertEqual(lifecycle.relayIDs.count, 2, transition)
            if transition == "socket failed" {
                // What Network's queue records, then what reaches the actor.
                XCTAssertTrue(lifecycle.socketFailed(1))
                // Revoked already, before the actor's cleanup has run.
                XCTAssertEqual(lifecycle.relayIDs, Set(lifecycle.grants.keys), transition)
                await fixture.listener.socketDidFail()
            } else {
                await fixture.listener.closeListeningSocket()
            }

            let rest = try await racing.receiveUntilClosed()
            XCTAssertEqual(rest, Data(), transition)
            XCTAssertEqual(fixture.publicOrigin.attempts, 0, transition)
            XCTAssertFalse(committed.isCancelled, transition)
            XCTAssertEqual(lifecycle.grants.count, 1, transition)
            try await committedClient.send("ping")
            try await eventually {
                String(decoding: committed.written, as: UTF8.self).hasSuffix("ping")
            }
            committedClient.close()
            await fixture.listener.stop()
        }
    }

    // MARK: The final grant

    /// The grant against a failure, failure first, with the actor's cleanup
    /// not yet run: a candidate admitted while serving has its stream ready
    /// after the socket's failure was recorded. It is never granted, no
    /// `200` is sent, and its stream is closed. A relay granted before the
    /// failure stays usable.
    func testACandidateThatReachesItsCommitPointAfterAFailureIsNeverGranted() async throws {
        let fixture = try await makeListener()
        let controlClient = LoopbackClient(port: fixture.port)
        let control = try await establish(controlClient, fixture)
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead)
        let candidate = try await fixture.direct.waitForStream(count: 2)

        // What Network's queue records; the actor has not heard of it.
        let lifecycle = await fixture.listener.lifecycle
        XCTAssertTrue(lifecycle.socketFailed(1))
        candidate.instanceSends(Data(established.utf8))

        let answer = try await client.receiveUntilClosed()
        XCTAssertEqual(answer, Data())
        try await eventually { candidate.isCancelled }
        XCTAssertEqual(lifecycle.grants.count, 1)
        XCTAssertFalse(control.isCancelled)
        try await controlClient.send("ping")
        try await eventually {
            String(decoding: control.written, as: UTF8.self).hasSuffix("ping")
        }
        controlClient.close()
    }

    /// The same order inside the commit point's own turn: the plan's last
    /// validation passes, and a failure or a close recorded before the
    /// grant that follows it, in that turn, still refuses the grant. No
    /// `200`, the stream is closed, and the CONNECT is not planned again.
    func testAFailureOrCloseRecordedBeforeTheGrantRefusesItInItsOwnTurn() async throws {
        for transition in ["socket failed", "close"] {
            let fixture = try await makeListener()
            let lifecycle = await fixture.listener.lifecycle
            // The second validation of a CONNECT is the one made in the
            // turn that grants it.
            fixture.plans.atValidation(2) {
                if transition == "socket failed" {
                    XCTAssertTrue(lifecycle.socketFailed(1))
                } else {
                    XCTAssertTrue(lifecycle.beginClose())
                }
            }
            let client = LoopbackClient(port: fixture.port)
            try await client.send(canonicalHead)
            let candidate = try await fixture.direct.waitForStream()
            candidate.instanceSends(Data(established.utf8))

            let answer = try await client.receiveUntilClosed()
            XCTAssertEqual(answer, Data(), transition)
            try await eventually { candidate.isCancelled }
            XCTAssertEqual(fixture.plans.validationCount, 2, transition)
            XCTAssertEqual(fixture.plans.planCount, 1, transition)
            XCTAssertEqual(fixture.direct.attempts, 1, transition)
            XCTAssertEqual(fixture.publicOrigin.attempts, 0, transition)
            XCTAssertEqual(lifecycle.grants, [:], transition)
            client.close()
            await fixture.listener.stop()
        }
    }

    /// The other order: a relay granted before the failure, or before the
    /// close, is kept by both, and its bytes keep flowing.
    func testARelayGrantedBeforeAFailureOrACloseIsKept() async throws {
        for transition in ["socket failed", "close"] {
            let fixture = try await makeListener()
            let lifecycle = await fixture.listener.lifecycle
            let client = LoopbackClient(port: fixture.port)
            let stream = try await establish(client, fixture)
            XCTAssertEqual(lifecycle.grants.count, 1, transition)

            if transition == "socket failed" {
                XCTAssertTrue(lifecycle.socketFailed(1))
                await fixture.listener.socketDidFail()
            } else {
                await fixture.listener.closeListeningSocket()
            }

            XCTAssertEqual(lifecycle.grants.count, 1, transition)
            XCTAssertFalse(stream.isCancelled, transition)
            try await client.send("ping")
            try await eventually {
                String(decoding: stream.written, as: UTF8.self).hasSuffix("ping")
            }
            // It leaves the ledger when it ends, and only then.
            stream.cancel()
            try await eventually { lifecycle.grants.isEmpty }
            client.close()
            await fixture.listener.stop()
        }
    }

    /// A plan that is no longer current in the turn that would grant it is
    /// not granted and leaves nothing behind. That is still before the
    /// commit point, so the CONNECT is planned once more.
    func testAPlanNoLongerCurrentAtTheGrantIsNotGrantedAndIsPlannedOnceMore() async throws {
        let fixture = try await makeListener()
        let lifecycle = await fixture.listener.lifecycle
        let plans = fixture.plans
        plans.atValidation(2) {
            plans.set(current: false)
        }
        let client = LoopbackClient(port: fixture.port)
        try await client.send(canonicalHead)
        let first = try await fixture.direct.waitForStream()
        first.instanceSends(Data(established.utf8))

        // Planned once more; that plan is fenced as well: no `200` at all.
        let second = try await fixture.direct.waitForStream(count: 2)
        XCTAssertEqual(lifecycle.grants, [:])
        second.instanceSends(Data(established.utf8))
        let answer = try await client.receiveUntilClosed()
        XCTAssertTrue(
            String(decoding: answer, as: UTF8.self).hasPrefix("HTTP/1.1 503 "),
            String(decoding: answer, as: UTF8.self)
        )
        try await eventually { first.isCancelled && second.isCancelled }
        XCTAssertEqual(plans.planCount, 2)
        XCTAssertEqual(lifecycle.grants, [:])
        client.close()
    }

    /// Nothing undoes a grant. Once granted, a plan that stops being
    /// current changes nothing for this relay, and a backend that fails
    /// only ends it: it is not validated or planned again, nothing is
    /// dialled again, and it leaves the ledger by ending.
    func testAGrantIsNeverUndoneAndTheRelayIsNeverPlannedAgain() async throws {
        let fixture = try await makeListener()
        let lifecycle = await fixture.listener.lifecycle
        let plans = fixture.plans
        let client = LoopbackClient(port: fixture.port)
        let stream = try await establish(client, fixture)
        XCTAssertEqual(lifecycle.grants.count, 1)
        XCTAssertEqual(plans.validationCount, 2)

        // What made the plan current is gone; the relay stays as granted.
        plans.set(current: false, generation: 7)
        try await client.send("ping")
        try await eventually {
            String(decoding: stream.written, as: UTF8.self).hasSuffix("ping")
        }
        XCTAssertEqual(lifecycle.grants.count, 1)

        // Its backend fails: the relay ends, and that is all.
        stream.cancel()
        let rest = try await client.receiveUntilClosed()
        XCTAssertEqual(rest, Data())
        try await eventually { lifecycle.relayIDs.isEmpty }
        XCTAssertEqual(plans.validationCount, 2)
        XCTAssertEqual(plans.planCount, 1)
        XCTAssertEqual(fixture.direct.attempts, 1)
        XCTAssertEqual(fixture.publicOrigin.attempts, 0)
    }

    /// WebKit's side goes away in the very turn that grants the relay, so
    /// the tunnel cannot start or its `200` cannot be delivered: the relay
    /// only ends. It is not validated or planned again, and nothing is
    /// dialled again.
    func testAClientGoneAtTheGrantOnlyEndsTheRelay() async throws {
        let fixture = try await makeListener(limits: limits(lingerSeconds: 0.5))
        let lifecycle = await fixture.listener.lifecycle
        let plans = fixture.plans
        let client = LoopbackClient(port: fixture.port)
        plans.atValidation(2) {
            client.close()
        }
        try await client.send(canonicalHead)
        let stream = try await fixture.direct.waitForStream()
        stream.instanceSends(Data(established.utf8))

        try await eventually { stream.isCancelled }
        try await eventually { lifecycle.relayIDs.isEmpty }
        XCTAssertEqual(plans.validationCount, 2)
        XCTAssertEqual(plans.planCount, 1)
        XCTAssertEqual(fixture.direct.attempts, 1)
        XCTAssertEqual(fixture.publicOrigin.attempts, 0)
    }

    /// A connection admitted while serving finishes its request head after
    /// the failure was recorded: no plan is admitted for it, and nothing is
    /// dialled.
    func testWorkAdmittedBeforeAFailureIsNotPlannedAfterIt() async throws {
        let fixture = try await makeListener()
        let client = LoopbackClient(port: fixture.port)
        try await client.send("CONNECT instance.example:443 HTTP/1.1\r\n")
        try await Task.sleep(for: .milliseconds(100))

        let lifecycle = await fixture.listener.lifecycle
        XCTAssertTrue(lifecycle.socketFailed(1))
        try await client.send("Host: instance.example:443\r\n\r\n")

        let answer = try await client.receiveUntilClosed()
        XCTAssertTrue(
            answer.isEmpty || String(decoding: answer, as: UTF8.self).hasPrefix("HTTP/1.1 503 "),
            String(decoding: answer, as: UTF8.self)
        )
        XCTAssertEqual(fixture.direct.attempts, 0)
        XCTAssertEqual(fixture.publicOrigin.attempts, 0)
    }

    func testABindOnAPortSomeoneElseHoldsFails() async throws {
        let control = try ControlListener(family: AF_INET, address: "127.0.0.1")
        defer { control.close() }
        let listener = DirectConnectListener(
            limits: .patient,
            planner: { _ in nil },
            onListenerFailure: {}
        )
        addTeardownBlock {
            await listener.stop()
        }

        do {
            let binding = try await listener.bind(port: control.socket.port)
            XCTFail("Bound \(binding.port), which another socket holds.")
        } catch {}
    }

    func testTheOwnershipCheckIsAnsweredOnlyByThisListenerWithItsNonce() async throws {
        let fixture = try await makeListener()
        let nonce = await fixture.listener.ownershipNonce

        let owned = await DirectConnectListener.ownershipCheck(
            port: fixture.port.rawValue,
            nonce: nonce,
            within: 10
        )
        XCTAssertTrue(owned)
        let otherNonce = await DirectConnectListener.ownershipCheck(
            port: fixture.port.rawValue,
            nonce: "another-nonce",
            within: 1
        )
        XCTAssertFalse(otherNonce)

        // A socket that accepts and never answers is not this listener.
        let control = try ControlListener(family: AF_INET, address: "127.0.0.1")
        defer { control.close() }
        let silent = await DirectConnectListener.ownershipCheck(
            port: control.socket.port,
            nonce: nonce,
            within: 0.5
        )
        XCTAssertFalse(silent)
        XCTAssertEqual(fixture.plans.planCount, 0)
    }

    func testClosingTheListeningSocketKeepsCommittedRelays() async throws {
        let fixture = try await makeListener()
        let client = LoopbackClient(port: fixture.port)
        let stream = try await establish(client, fixture)

        await fixture.listener.closeListeningSocket()
        try await eventually {
            await !LoopbackClient.canConnect(to: fixture.port)
        }
        XCTAssertFalse(try listeningSockets().contains(fixture.socket))
        // The committed relay still carries bytes both ways.
        try await client.send("ping")
        try await eventually {
            String(decoding: stream.written, as: UTF8.self).hasSuffix("ping")
        }
        stream.instanceSends(Data("pong".utf8))
        let pong = try await client.receive(count: 4)
        XCTAssertEqual(String(decoding: pong, as: UTF8.self), "pong")
        XCTAssertFalse(stream.isCancelled)
        client.close()
    }

    func testAPortThatNeverCarriedAConnectionCanBeBoundAgainAndPassesTheOwnershipCheck() async throws {
        let fixture = try await makeListener()
        await fixture.listener.closeListeningSocket()

        let binding = try await fixture.listener.bind(port: fixture.port.rawValue)
        let port = binding.port

        XCTAssertEqual(port, fixture.port.rawValue)
        let nonce = await fixture.listener.ownershipNonce
        // Bound but not confirmed: a CONNECT is not served yet.
        let early = LoopbackClient(port: fixture.port)
        try? await early.send(canonicalHead)
        let refused = try await early.receiveUntilClosed()
        XCTAssertEqual(refused, Data())
        XCTAssertEqual(fixture.plans.planCount, 0)
        let owned = await DirectConnectListener.ownershipCheck(
            port: port,
            nonce: nonce,
            within: 10
        )
        XCTAssertTrue(owned)
        let lifecycle = await fixture.listener.lifecycle
        XCTAssertTrue(lifecycle.confirm(binding.generation))
        let client = LoopbackClient(port: fixture.port)
        _ = try await establish(client, fixture)
        client.close()
    }

    /// What B2 meets on a return from the background: the port still has
    /// connections of this listener (a committed relay, and one the App
    /// closed itself). Whether an exclusive bind succeeds then is not
    /// established. This test holds the safety property either way, and
    /// logs which way it went: a failed bind throws and costs the live
    /// relay nothing; a successful one is this listener's own.
    func testReBindingAPortThatStillHasConnectionsNeverPretendsAndKeepsTheRelay() async throws {
        let fixture = try await makeListener()
        // One relay the App closes itself, as the background does...
        let closed = LoopbackClient(port: fixture.port)
        _ = try await establish(closed, fixture)
        let closedCount = await fixture.listener.closeRelays(backend: nil)
        XCTAssertEqual(closedCount, [host: 1])
        // ...and one that stays committed, as a public relay does.
        let live = LoopbackClient(port: fixture.port)
        let stream = try await establish(live, fixture)
        await fixture.listener.closeListeningSocket()

        let outcome: String
        do {
            let port = try await fixture.listener.bind(port: fixture.port.rawValue).port
            XCTAssertEqual(port, fixture.port.rawValue)
            let nonce = await fixture.listener.ownershipNonce
            let owned = await DirectConnectListener.ownershipCheck(
                port: port,
                nonce: nonce,
                within: 10
            )
            XCTAssertTrue(owned)
            outcome = "bound again"
        } catch {
            outcome = "refused: \(error)"
        }
        print("B2 re-bind with live and closed connections on the port: \(outcome)")

        try await live.send("ping")
        try await eventually {
            String(decoding: stream.written, as: UTF8.self).hasSuffix("ping")
        }
        XCTAssertFalse(stream.isCancelled)
        live.close()
        closed.close()
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

        // The port no longer accepts.
        try await eventually {
            await !LoopbackClient.canConnect(to: fixture.port)
        }
        XCTAssertFalse(try listeningSockets().contains(fixture.socket))
    }

    // MARK: Parsing

    func testTheRequestHeadIsAcceptedOnlyWhenCompleteAndWithinItsLimit() {
        let allowed: Set<String> = [host]
        let authority = DirectAuthority(host: host)!
        let head = Data(canonicalHead.utf8)
        for prefix in 0..<head.count {
            XCTAssertEqual(
                DirectConnectRequest.parse(
                    head.prefix(prefix),
                    allowedHosts: allowed,
                    limit: 4096
                ),
                .incomplete,
                "prefix \(prefix)"
            )
        }
        XCTAssertEqual(
            DirectConnectRequest.parse(head, allowedHosts: allowed, limit: 4096),
            .accepted(authority, earlyData: Data())
        )
        XCTAssertEqual(
            DirectConnectRequest.parse(
                head + Data("early".utf8),
                allowedHosts: allowed,
                limit: 4096
            ),
            .accepted(authority, earlyData: Data("early".utf8))
        )
        XCTAssertEqual(
            DirectConnectRequest.parse(head, allowedHosts: [], limit: 4096),
            .refused(403)
        )
        XCTAssertEqual(
            DirectConnectRequest.parse(
                Data("OWNER abc HTTP/1.1\r\n\r\n".utf8),
                allowedHosts: [],
                limit: 4096
            ),
            .ownershipCheck(nonce: "abc")
        )

        // The limit counts the head up to its blank line.
        let exact = head.count - 4
        XCTAssertEqual(
            DirectConnectRequest.parse(head, allowedHosts: allowed, limit: exact),
            .accepted(authority, earlyData: Data())
        )
        XCTAssertEqual(
            DirectConnectRequest.parse(
                head,
                allowedHosts: allowed,
                limit: exact - 1
            ),
            .refused(400)
        )
        // Past the limit with no end in sight is refused without waiting.
        XCTAssertEqual(
            DirectConnectRequest.parse(
                Data(repeating: UInt8(ascii: "a"), count: 4097),
                allowedHosts: allowed,
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

        XCTAssertEqual(DirectAuthority(host: "alex-app.avibe.bot")?.connectTarget, "alex-app.avibe.bot:443")
        XCTAssertNil(DirectAuthority(host: "127.0.0.1"))
        XCTAssertNil(DirectAuthority(host: "owner@alex-app.avibe.bot"))
        XCTAssertNil(DirectAuthority(host: "alex-app.avibe.bot:443"))
    }

    // MARK: Fixture

    private struct Fixture {
        let listener: DirectConnectListener
        let port: NWEndpoint.Port
        let plans: StandInPlans
        let direct: StandInStreamSource
        let publicOrigin: StandInStreamSource

        /// Where the listener must be found: the loopback address, and no
        /// other.
        var socket: ListeningSocket {
            ListeningSocket(address: "127.0.0.1", port: port.rawValue)
        }
    }

    /// The standard sizes, with deadlines a loaded simulator cannot miss.
    /// A test about one deadline shortens that one.
    private func limits(
        headSeconds: TimeInterval = 60,
        openSeconds: TimeInterval = 60,
        answerSeconds: TimeInterval = 60,
        maxConnections: Int = 64,
        lingerSeconds: TimeInterval = 60,
        chunkBytes: Int = 64 * 1024,
        earlyBytes: Int = 16 * 1024,
        hedgeSeconds: TimeInterval = 60
    ) -> DirectConnectLimits {
        DirectConnectLimits(
            headBytes: 4096,
            headSeconds: headSeconds,
            openSeconds: openSeconds,
            answerSeconds: answerSeconds,
            maxConnections: maxConnections,
            lingerSeconds: lingerSeconds,
            chunkBytes: chunkBytes,
            earlyBytes: earlyBytes,
            hedgeSeconds: hedgeSeconds
        )
    }

    private func makeListener(
        limits: DirectConnectLimits = .patient,
        allowedHosts: Set<String>? = nil,
        direct: StandInStreamSource.Mode = .open,
        publicOrigin: StandInStreamSource.Mode = .open
    ) async throws -> Fixture {
        let directStreams = StandInStreamSource(mode: direct)
        let publicStreams = StandInStreamSource(mode: publicOrigin)
        let plans = StandInPlans(direct: directStreams, publicOrigin: publicStreams)
        let listener = DirectConnectListener(
            limits: limits,
            planner: { authority in
                plans.plan(for: authority)
            },
            onListenerFailure: {}
        )
        for allowed in allowedHosts ?? [host] {
            await listener.allowHost(allowed)
        }
        let binding = try await listener.bind(port: nil)
        let bound = binding.port
        // What the path controller does once the ownership check passed.
        let lifecycle = await listener.lifecycle
        XCTAssertTrue(lifecycle.confirm(binding.generation))
        addTeardownBlock {
            await listener.stop()
        }
        return Fixture(
            listener: listener,
            port: NWEndpoint.Port(rawValue: bound)!,
            plans: plans,
            direct: directStreams,
            publicOrigin: publicStreams
        )
    }

    /// Connects until a CONNECT is served (while every slot is held, a new
    /// connection is closed at once), and completes it on the direct path.
    private func serveWhenASlotIsFree(
        _ fixture: Fixture
    ) async throws -> (LoopbackClient, StandInDirectStream) {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(30))
        while clock.now < deadline {
            let before = fixture.direct.opened.count
            let client = LoopbackClient(port: fixture.port)
            try? await client.send(canonicalHead)
            let wait = clock.now.advanced(by: .milliseconds(500))
            while fixture.direct.opened.count == before, clock.now < wait {
                try await Task.sleep(for: .milliseconds(10))
            }
            if fixture.direct.opened.count > before {
                let stream = fixture.direct.opened[before]
                stream.instanceSends(Data(established.utf8))
                let answer = try await client.receive(count: established.utf8.count)
                XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
                return (client, stream)
            }
            client.close()
        }
        XCTFail("No slot became free.")
        throw DirectTestTimeout()
    }

    /// Completes one CONNECT on the direct path and returns its stream.
    private func establish(
        _ client: LoopbackClient,
        _ fixture: Fixture
    ) async throws -> StandInDirectStream {
        let before = fixture.direct.opened.count
        try await client.send(canonicalHead)
        let stream = try await fixture.direct.waitForStream(count: before + 1)
        stream.instanceSends(Data(established.utf8))
        let answer = try await client.receive(count: established.utf8.count)
        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        return stream
    }

    /// Completes one CONNECT on the public origin and returns its stream.
    private func establishPublic(
        _ client: LoopbackClient,
        _ fixture: Fixture
    ) async throws -> StandInDirectStream {
        let before = fixture.publicOrigin.opened.count
        try await client.send(canonicalHead)
        let stream = try await fixture.publicOrigin.waitForStream(count: before + 1)
        let answer = try await client.receive(count: established.utf8.count)
        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        return stream
    }
}

// MARK: - Stand-ins shared with the path controller tests

extension DirectConnectLimits {
    /// The standard sizes, with deadlines a loaded simulator cannot miss and
    /// a hedge long enough that the public origin is dialled only when the
    /// direct candidate has settled.
    static let patient = DirectConnectLimits(
        headBytes: 4096,
        headSeconds: 60,
        openSeconds: 60,
        answerSeconds: 60,
        maxConnections: 64,
        lingerSeconds: 60,
        chunkBytes: 64 * 1024,
        earlyBytes: 16 * 1024,
        hedgeSeconds: 60
    )
}

/// The path controller's side of the listener in a test: what each plan
/// offers, whether it is current, and what it was told.
final class StandInPlans: @unchecked Sendable {
    private let lock = NSLock()
    private let direct: StandInStreamSource
    private let publicOrigin: StandInStreamSource
    private var offersDirect = true
    private var isCurrent = true
    private var refuses = false
    private var plans = 0
    private var failures = 0
    private var scope = DirectPlanScope(selection: 0, accountEpoch: 0)
    private var generation = 0
    private var validations = 0
    private var validationActions: [Int: () -> Void] = [:]

    init(direct: StandInStreamSource, publicOrigin: StandInStreamSource) {
        self.direct = direct
        self.publicOrigin = publicOrigin
    }

    var planCount: Int {
        lock.withLock { plans }
    }

    var directFailures: Int {
        lock.withLock { failures }
    }

    func set(
        direct: Bool? = nil,
        current: Bool? = nil,
        refuse: Bool? = nil,
        scope: DirectPlanScope? = nil,
        generation: Int? = nil
    ) {
        lock.withLock {
            if let scope {
                self.scope = scope
            }
            if let generation {
                self.generation = generation
            }
            if let direct {
                offersDirect = direct
            }
            if let current {
                isCurrent = current
            }
            if let refuse {
                refuses = refuse
            }
        }
    }

    func plan(for authority: DirectAuthority) -> DirectConnectPlan? {
        let (refuse, offerDirect, scope, generation) = lock.withLock {
            () -> (Bool, Bool, DirectPlanScope, Int) in
            plans += 1
            return (refuses, offersDirect, self.scope, self.generation)
        }
        guard !refuse else {
            return nil
        }
        let direct = direct
        let publicOrigin = publicOrigin
        var openDirect: (@Sendable () async throws -> any DirectByteStream)?
        if offerDirect {
            openDirect = {
                try await direct.open()
            }
        }
        return DirectConnectPlan(
            scope: scope,
            generation: generation,
            openDirect: openDirect,
            openPublic: {
                try await publicOrigin.open()
            },
            isCurrent: { [weak self] in
                self?.currentNow() ?? false
            },
            directFailed: { [weak self] in
                self?.recordDirectFailure()
            }
        )
    }

    /// How many times a plan of this stand-in was validated.
    var validationCount: Int {
        lock.withLock { validations }
    }

    /// Runs `action` inside validation number `index` (counted from one),
    /// before that validation's answer is read. A CONNECT's first validation
    /// is made when its candidate is ready; its second is the one made in
    /// the main-actor turn that grants it.
    func atValidation(_ index: Int, _ action: @escaping () -> Void) {
        lock.withLock {
            validationActions[index] = action
        }
    }

    private func currentNow() -> Bool {
        let action = lock.withLock { () -> (() -> Void)? in
            validations += 1
            return validationActions[validations]
        }
        action?()
        return lock.withLock { isCurrent }
    }

    private func recordDirectFailure() {
        lock.withLock {
            failures += 1
        }
    }
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
/// system chooses or on `port`: something the scan must see, or something
/// that holds a port. `reuseAddress` sets `SO_REUSEADDR`, so the socket can
/// take a port that only has connections in TIME_WAIT left on it.
final class ControlListener {
    struct Unavailable: Error {
        let step: String
        let code: Int32
    }

    let socket: ListeningSocket
    private let descriptor: Int32

    init(
        family: Int32,
        address: String,
        port requested: UInt16 = 0,
        reuseAddress: Bool = false
    ) throws {
        let descriptor = Darwin.socket(family, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            throw Unavailable(step: "socket", code: errno)
        }
        if reuseAddress {
            var on: Int32 = 1
            setsockopt(descriptor, SOL_SOCKET, SO_REUSEADDR, &on, socklen_t(MemoryLayout<Int32>.size))
        }
        var bound: Int32 = -1
        var storage = sockaddr_storage()
        var length = socklen_t(MemoryLayout<sockaddr_storage>.size)
        if family == AF_INET {
            var name = sockaddr_in()
            name.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            name.sin_family = sa_family_t(AF_INET)
            name.sin_port = requested.bigEndian
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
            name.sin6_port = requested.bigEndian
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

    private var isClosed = false

    /// Closes the socket once; later calls do nothing, so a descriptor the
    /// system has reused is never closed by mistake.
    func close() {
        guard !isClosed else {
            return
        }
        isClosed = true
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
    private var calls = 0

    init(mode: Mode = .open) {
        self.mode = mode
    }

    var opened: [StandInDirectStream] {
        lock.withLock { streams }
    }

    /// Every `open()` call, whatever came of it.
    var attempts: Int {
        lock.withLock { calls }
    }

    func setMode(_ mode: Mode) {
        lock.withLock {
            self.mode = mode
        }
    }

    func open() async throws -> any DirectByteStream {
        let mode = lock.withLock { () -> Mode in
            calls += 1
            return self.mode
        }
        switch mode {
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

/// Receives a test completes by hand, in order.
final class ManualReceives: @unchecked Sendable {
    private let lock = NSLock()
    private var completions: [@Sendable (Data?, Bool, Error?) -> Void] = []

    var pending: Int {
        lock.withLock { completions.count }
    }

    var receive: DirectClientReader.Receive {
        { [self] _, completion in
            lock.withLock {
                completions.append(completion)
            }
        }
    }

    /// Completes the oldest pending receive.
    func complete(with data: Data?, isComplete: Bool = false, error: Error? = nil) {
        let completion = lock.withLock { completions.removeFirst() }
        completion(data, isComplete, error)
    }
}
