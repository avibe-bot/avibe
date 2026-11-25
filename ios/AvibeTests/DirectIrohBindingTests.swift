import Foundation
import IrohLib
import XCTest
@testable import Avibe

/// Hosted-simulator qualification of the real generated binding adapter.
/// These tests deliberately use only test-owned loopback endpoints; they do
/// not register the App transport or contact a sidecar, backend, relay, or
/// public address.
final class DirectIrohBindingTests: XCTestCase {
    private static let alpn = Data("avibe-direct/1".utf8)
    private static let payload = Data("avibe-direct-interop-v2\n".utf8)
    private static let closeBound: TimeInterval = 3
    // Full native endpoint drain is separate from pending-call settlement.
    private static let endpointDrainBound: Duration = .seconds(5)

    /// The real binding's relay map takes the four pinned URLs exactly as
    /// the custom-relay branch of `DirectIrohBinding.bind` passes them.
    /// Building a map and a mode opens no socket.
    func testRealRelayMapTakesTheFourPinnedRelaysExactly() throws {
        let urls = DirectTrialRelay.publicTrialRelayURLs
        let map = try IrohLib.RelayMap.fromUrls(urls: urls)
        for url in urls {
            XCTAssertEqual(try map.get(url: url)?.url, url)
        }
        XCTAssertNil(try map.get(url: "https://relay.example./"))
        let mode = IrohLib.RelayMode.custom(map: map)
        for url in urls {
            XCTAssertNotNil(try mode.relayMap().get(url: url))
        }
    }

    func testRealBindingLoopbackEchoAndCleanHalfClose() async throws {
        let pair = try await makePair()
        defer {
            pair.client.close()
        }

        XCTAssertEqual(pair.client.remoteEndpointID, pair.peer.id)

        let clientHalves = try await pair.client.openStream()
        let accepting = Task {
            try await pair.server.acceptBi()
        }

        try await clientHalves.send.writeAll(Self.payload)
        try await clientHalves.send.finish()
        let serverStream = try await accepting.value

        let received = try await serverStream.recv().read(
            sizeLimit: UInt32(Self.payload.count)
        )
        XCTAssertEqual(received, Self.payload)

        try await serverStream.send().writeAll(buf: Self.payload)
        try await serverStream.send().finish()

        let echoed = try await clientHalves.receive.read(
            sizeLimit: Self.payload.count
        )
        XCTAssertEqual(echoed, Self.payload)
        let end = try await clientHalves.receive.read(sizeLimit: 1)
        XCTAssertTrue(end.isEmpty)

        try await close(pair)
    }

    /// What a measurement's mark of the path rests on (`DirectPathMark`):
    /// the real binding's watch of a connection's paths reports, and its
    /// first report needs no change of the paths, since the binding reports
    /// the state as it is when it first looks; and a selected path can be
    /// told from another by something that stays the same between two reads.
    func testRealBindingsPathWatchReportsAndASelectedPathIsNamedTheSameTwice() async throws {
        let pair = try await makePair()
        defer {
            pair.client.close()
        }
        let reports = PathReportCount()
        let watch = try XCTUnwrap(pair.client.watchSelectedPath { _ in reports.note() })
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
        while reports.count == 0, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertGreaterThanOrEqual(reports.count, 1)

        // Whether a loopback connection has a selected path this soon is
        // the binding's business: either way is said, and only a selected
        // path is asked for its name.
        while pair.client.selectedPath == nil, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(20))
        }
        if pair.client.selectedPath != nil {
            let read = try XCTUnwrap(pair.client.selectedPathRead)
            XCTAssertTrue(read.identity.hasPrefix("address "))
            XCTAssertEqual(pair.client.selectedPathRead?.identity, read.identity)
            // A path to an address has its labels in the same read.
            XCTAssertNotNil(read.labels)
        }
        print(
            "[iOS fixture] stage=measure the real binding's path watch reported \(reports.count) time(s);"
                + " a path was selected: \(pair.client.selectedPath != nil)"
        )
        watch.stop()
        try await close(pair)
    }

    func testRealBindingPendingConnectEndsWhenEndpointCloses() async throws {
        let peer = try await makePeer()
        let binding = DirectIrohBinding()
        let key = try binding.makeLaunchKey()
        let endpoint = try await binding.bind(secretKey: key.secret, relayURLs: [])

        let completions = CompletionCounter()
        let connectTask = Task {
            defer { completions.mark() }
            return try await endpoint.connect(
                to: peer.id,
                addresses: [peer.address],
                relayURL: nil,
                alpn: Self.alpn
            )
        }
        // Receiving, but deliberately not accepting, the incoming handshake
        // establishes a real pending connect without a timing-only setup sleep.
        let incoming = await peer.endpoint.acceptNext()
        defer {
            withExtendedLifetime(incoming) {}
        }
        XCTAssertNotNil(incoming)
        XCTAssertEqual(completions.count, 0)

        let clock = ContinuousClock()
        let closeIssuedAt = clock.now
        let closing = Task {
            await endpoint.close()
            return closeIssuedAt.duration(to: clock.now)
        }
        // Observe connect independently: Endpoint.close also waits for QUIC
        // close-frame drain, documented by the pinned core to take about 3s.
        let result = await connectTask.result
        let connectElapsed = closeIssuedAt.duration(to: clock.now)
        XCTAssertNotNil(Self.failure(of: result))
        XCTAssertLessThan(connectElapsed, .seconds(Self.closeBound))

        let drainElapsed = await closing.value
        XCTAssertLessThan(drainElapsed, Self.endpointDrainBound)
        print("real-binding pending-connect settlement=\(connectElapsed) endpoint-drain=\(drainElapsed)")

        await peer.close()
    }

    func testRealBindingCancelledOpensEndWhenConnectionCloses() async throws {
        let pair = try await makePair()
        let server = pair.server
        let client = pair.client
        defer {
            client.close()
        }

        let completions = CompletionCounter()
        let opened = (0..<128).map { _ in
            Task {
                defer {
                    completions.mark()
                }
                return try await client.openStream()
            }
        }
        try await Task.sleep(for: .milliseconds(100))
        let completedBeforeCancel = completions.count
        XCTAssertLessThan(completedBeforeCancel, opened.count)

        let cancelledAt = Date()
        opened.forEach { $0.cancel() }
        client.close()
        await pair.endpoint.close()
        try? server.close(errorCode: 0, reason: Data())

        var succeeded = 0
        for task in opened {
            let result = await task.result
            if case .success(let halves) = result {
                succeeded += 1
                DirectIrohByteStream.abort(halves)
            }
        }
        let elapsed = Date().timeIntervalSince(cancelledAt)
        XCTAssertLessThan(elapsed, Self.closeBound)
        print("real-binding cancelled-open attempted=128 completed-before-cancel=\(completedBeforeCancel) succeeded=\(succeeded) connection-close-seconds=\(elapsed)")

        await pair.peer.close()
    }

    func testRealBindingBothHalvesCancelPromptlyAndSessionCloseEndsCalls() async throws {
        let pair = try await makePair()
        defer {
            pair.client.close()
        }

        try pair.server.setReceiveWindow(count: 1024)
        let clientHalves = try await pair.client.openStream()
        let accepting = Task {
            try await pair.server.acceptBi()
        }
        let recorder = CompletionRecorder()
        let observed = DirectIrohStreamHalves(
            send: ObservingSend(base: clientHalves.send, recorder: recorder),
            receive: ObservingReceive(base: clientHalves.receive, recorder: recorder)
        )
        let stream = DirectIrohByteStream(observed)
        let readTask = Task {
            try await stream.read(maxLength: DirectIrohByteStream.maxReadBytes)
        }
        let writeTask = Task {
            try await stream.write(Data(repeating: 0xA2, count: 16 * 1024 * 1024))
        }
        let acceptedServerStream = try await accepting.value
        defer {
            withExtendedLifetime(acceptedServerStream) {}
        }
        // Keep the peer from consuming either half. The small receive window
        // makes the write exercise flow-control backpressure rather than a
        // merely buffered localhost transfer.
        try await Task.sleep(for: .milliseconds(100))
        let nativeCallsStarted = await waitForStarted(recorder)
        XCTAssertTrue(nativeCallsStarted)
        XCTAssertTrue(recorder.bothInFlight)

        let cancelledAt = Date()
        stream.cancel()
        let readResult = await readTask.result
        let writeResult = await writeTask.result
        let callerElapsed = Date().timeIntervalSince(cancelledAt)

        XCTAssertNotNil(Self.failure(of: readResult))
        XCTAssertNotNil(Self.failure(of: writeResult))
        XCTAssertLessThan(callerElapsed, Self.closeBound)

        pair.client.close()
        await pair.endpoint.close()
        try? pair.server.close(errorCode: 0, reason: Data())

        let nativeCallsSettled = await waitForSettled(recorder)
        XCTAssertTrue(nativeCallsSettled)
        print("real-binding both-halves caller-cancel-seconds=\(callerElapsed) native-calls-settled=\(nativeCallsSettled)")
        await pair.peer.close()
    }

    private final class CompletionCounter: @unchecked Sendable {
        private let lock = NSLock()
        private var value = 0

        var count: Int {
            lock.withLock { value }
        }

        func mark() {
            lock.withLock {
                value += 1
            }
        }
    }

    private final class CompletionRecorder: @unchecked Sendable {
        private let lock = NSLock()
        private var sendWriteStarted = false
        private var receiveReadStarted = false
        private var sendWrite = false
        private var sendReset = false
        private var receiveRead = false
        private var receiveStop = false

        var bothStarted: Bool {
            lock.withLock {
                sendWriteStarted && receiveReadStarted
            }
        }

        var bothInFlight: Bool {
            lock.withLock {
                sendWriteStarted && receiveReadStarted && !sendWrite && !receiveRead
            }
        }

        var allSettled: Bool {
            lock.withLock {
                sendWrite && sendReset && receiveRead && receiveStop
            }
        }

        func markSendWriteStarted() { lock.withLock { sendWriteStarted = true } }
        func markReceiveReadStarted() { lock.withLock { receiveReadStarted = true } }
        func markSendWrite() { lock.withLock { sendWrite = true } }
        func markSendReset() { lock.withLock { sendReset = true } }
        func markReceiveRead() { lock.withLock { receiveRead = true } }
        func markReceiveStop() { lock.withLock { receiveStop = true } }
    }

    private final class ObservingSend: DirectIrohSendHalf, @unchecked Sendable {
        private let base: any DirectIrohSendHalf
        private let recorder: CompletionRecorder

        init(base: any DirectIrohSendHalf, recorder: CompletionRecorder) {
            self.base = base
            self.recorder = recorder
        }

        func writeAll(_ data: Data) async throws {
            recorder.markSendWriteStarted()
            defer { recorder.markSendWrite() }
            try await base.writeAll(data)
        }

        func finish() async throws {
            try await base.finish()
        }

        func reset() async {
            defer { recorder.markSendReset() }
            await base.reset()
        }
    }

    private final class ObservingReceive: DirectIrohReceiveHalf, @unchecked Sendable {
        private let base: any DirectIrohReceiveHalf
        private let recorder: CompletionRecorder

        init(base: any DirectIrohReceiveHalf, recorder: CompletionRecorder) {
            self.base = base
            self.recorder = recorder
        }

        func read(sizeLimit: Int) async throws -> Data {
            recorder.markReceiveReadStarted()
            defer { recorder.markReceiveRead() }
            return try await base.read(sizeLimit: sizeLimit)
        }

        func stop() async {
            defer { recorder.markReceiveStop() }
            await base.stop()
        }
    }

    private struct Pair: @unchecked Sendable {
        let endpoint: any DirectIrohEndpoint
        let client: any DirectIrohConnection
        let peer: Peer
        let server: IrohLib.Connection
    }

    private final class Peer: @unchecked Sendable {
        let endpoint: IrohLib.Endpoint
        let id: DirectEndpointID
        let address: String

        init(endpoint: IrohLib.Endpoint, id: DirectEndpointID, address: String) {
            self.endpoint = endpoint
            self.id = id
            self.address = address
        }

        func acceptConnection() async throws -> IrohLib.Connection {
            guard let incoming = await endpoint.acceptNext() else {
                throw DirectIrohError.timedOut
            }
            return try await incoming.accept().connect()
        }

        func close() async {
            try? await endpoint.close()
        }
    }

    private func waitForStarted(
        _ recorder: CompletionRecorder,
        timeout: TimeInterval = 1
    ) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if recorder.bothStarted {
                return true
            }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return recorder.bothStarted
    }

    private func waitForSettled(
        _ recorder: CompletionRecorder,
        timeout: TimeInterval = 3
    ) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if recorder.allSettled {
                return true
            }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return recorder.allSettled
    }

    private func makePair() async throws -> Pair {
        let peer = try await makePeer()
        let binding = DirectIrohBinding()
        let key = try binding.makeLaunchKey()
        let endpoint = try await binding.bind(secretKey: key.secret, relayURLs: [])
        let accepting = Task {
            try await peer.acceptConnection()
        }
        let client = try await endpoint.connect(
            to: peer.id,
            addresses: [peer.address],
            relayURL: nil,
            alpn: Self.alpn
        )
        let server = try await accepting.value
        return Pair(endpoint: endpoint, client: client, peer: peer, server: server)
    }

    private func makePeer() async throws -> Peer {
        let key = IrohLib.SecretKey.generate()
        let endpoint = try await IrohLib.Endpoint.bind(
            options: IrohLib.EndpointOptions(
                preset: IrohLib.presetMinimal(),
                bindAddr: "127.0.0.1:0",
                secretKey: key.toBytes(),
                alpns: [Self.alpn],
                relayMode: IrohLib.RelayMode.disabled()
            )
        )
        guard let address = endpoint.boundSockets().first(where: {
            $0.hasPrefix("127.0.0.1:")
        }) else {
            try? await endpoint.close()
            throw DirectIrohError.timedOut
        }
        guard let id = DirectEndpointID(bytes: endpoint.id().toBytes()) else {
            try? await endpoint.close()
            throw DirectIrohError.noLaunchKey
        }
        return Peer(endpoint: endpoint, id: id, address: address)
    }

    private final class PathReportCount: @unchecked Sendable {
        private let lock = NSLock()
        private var reports = 0

        var count: Int {
            lock.withLock { reports }
        }

        func note() {
            lock.withLock {
                reports += 1
            }
        }
    }

    private static func failure<Value>(of outcome: Result<Value, Error>) -> Error? {
        guard case .failure(let error) = outcome else {
            return nil
        }
        return error
    }

    private func close(_ pair: Pair) async throws {
        pair.client.close()
        try? pair.server.close(errorCode: 0, reason: Data())
        await pair.endpoint.close()
        await pair.peer.close()
    }
}
