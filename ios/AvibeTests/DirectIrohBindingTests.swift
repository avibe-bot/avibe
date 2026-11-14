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

    func testRealBindingLoopbackEchoAndCleanHalfClose() async throws {
        let pair = try await makePair()
        defer {
            pair.client.close()
        }

        XCTAssertEqual(pair.client.remoteEndpointID, pair.peer.id)

        let clientHalves = try await pair.client.openStream()
        let serverStream = try await pair.server.openBi()

        try await clientHalves.send.writeAll(Self.payload)
        try await clientHalves.send.finish()

        let received = try await serverStream.recv().read(
            sizeLimit: UInt32(Self.payload.count)
        )
        XCTAssertEqual(received, Self.payload)

        try await serverStream.send().writeAll(Self.payload)
        try await serverStream.send().finish()

        let echoed = try await clientHalves.receive.read(
            sizeLimit: UInt32(Self.payload.count)
        )
        XCTAssertEqual(echoed, Self.payload)
        let end = try await clientHalves.receive.read(sizeLimit: 1)
        XCTAssertTrue(end.isEmpty)

        try await close(pair)
    }

    func testRealBindingPendingConnectEndsWhenEndpointCloses() async throws {
        let peer = try await makePeer()
        let binding = DirectIrohBinding()
        let key = try binding.makeLaunchKey()
        let endpoint = try await binding.bind(secretKey: key.secret)

        let started = Date()
        let connectTask = Task {
            try await endpoint.connect(
                to: peer.id,
                addresses: [peer.address],
                alpn: Self.alpn
            )
        }
        try await Task.sleep(for: .milliseconds(100))
        await endpoint.close()

        let result = await connectTask.result
        let elapsed = Date().timeIntervalSince(started)
        XCTAssertNotNil(Self.failure(of: result))
        XCTAssertLessThan(elapsed, Self.closeBound)
        print("real-binding pending-connect endpoint-close seconds=\(elapsed)")

        await peer.close()
    }

    func testRealBindingCancelledOpensEndWhenConnectionCloses() async throws {
        let pair = try await makePair()
        let server = pair.server
        let client = pair.client
        defer {
            client.close()
        }

        let opened = (0..<16).map { _ in
            Task {
                try await client.openStream()
            }
        }
        try await Task.sleep(for: .milliseconds(100))
        let cancelledAt = Date()
        opened.forEach { $0.cancel() }
        client.close()
        await pair.endpoint.close()
        try? server.close(errorCode: 0, reason: Data())

        for task in opened {
            let result = await task.result
            XCTAssertNotNil(Self.failure(of: result))
        }
        let elapsed = Date().timeIntervalSince(cancelledAt)
        XCTAssertLessThan(elapsed, Self.closeBound)
        print("real-binding cancelled-open count=16 connection-close seconds=\(elapsed)")

        await pair.peer.close()
    }

    func testRealBindingBothHalvesCancelPromptlyAndSessionCloseEndsCalls() async throws {
        let pair = try await makePair()
        defer {
            pair.client.close()
        }

        let clientHalves = try await pair.client.openStream()
        let serverStream = try await pair.server.openBi()
        try pair.server.setReceiveWindow(count: 1024)

        let stream = DirectIrohByteStream(clientHalves)
        let readTask = Task {
            try await stream.read(maxLength: DirectIrohByteStream.maxReadBytes)
        }
        let writeTask = Task {
            try await stream.write(Data(repeating: 0xA2, count: 16 * 1024 * 1024))
        }
        // Keep the peer from consuming either half. The small receive window
        // makes the write exercise flow-control backpressure rather than a
        // merely buffered localhost transfer.
        _ = serverStream
        try await Task.sleep(for: .milliseconds(100))

        let cancelledAt = Date()
        stream.cancel()
        let readResult = await readTask.result
        let writeResult = await writeTask.result
        let callerElapsed = Date().timeIntervalSince(cancelledAt)

        XCTAssertNotNil(Self.failure(of: readResult))
        XCTAssertNotNil(Self.failure(of: writeResult))
        XCTAssertLessThan(callerElapsed, Self.closeBound)

        let sessionClosed = Task {
            await pair.server.closed()
        }
        pair.client.close()
        await pair.endpoint.close()
        try? pair.server.close(errorCode: 0, reason: Data())
        _ = await sessionClosed.result

        print("real-binding both-halves caller-cancel seconds=\(callerElapsed) session-close=true")
        await pair.peer.close()
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

    private func makePair() async throws -> Pair {
        let peer = try await makePeer()
        let binding = DirectIrohBinding()
        let key = try binding.makeLaunchKey()
        let endpoint = try await binding.bind(secretKey: key.secret)
        let accepting = Task {
            try await peer.acceptConnection()
        }
        let client = try await endpoint.connect(
            to: peer.id,
            addresses: [peer.address],
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
