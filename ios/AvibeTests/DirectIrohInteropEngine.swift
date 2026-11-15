import Foundation
import IrohLib
@testable import Avibe

/// Forwarding observers record real binding effects. They do not supply responses,
/// force reports, or replace the production engine/transport behavior.
final class InteropEngine: DirectIrohEngine, @unchecked Sendable {
    struct Snapshot: Sendable {
        var streams = 0
        var probes: [Int] = []
        var connectFailure: String?
        var nativeCalls = 0
        var connectionsClosed = 0
    }
    let key: DirectIrohLaunchKey
    private let binding = DirectIrohBinding()
    private let lock = NSLock()
    private var state = Snapshot()
    private var stopping = false
    private var endpoints: [Endpoint] = []
    private var connections: [any DirectIrohConnection] = []

    init(key: DirectIrohLaunchKey) { self.key = key }
    func makeLaunchKey() throws -> DirectIrohLaunchKey { key }
    var snapshot: Snapshot { lock.withLock { state } }
    var isStopping: Bool { lock.withLock { stopping } }
    func begin() { lock.withLock { state.nativeCalls += 1 } }
    func end() { lock.withLock { state.nativeCalls -= 1 } }
    func probe(_ status: Int) { lock.withLock { state.probes.append(status) } }
    func streamOpened() { lock.withLock { state.streams += 1 } }
    func connectionEnded() { lock.withLock { state.connectionsClosed += 1 } }

    static func rejection(_ error: Error) -> String? {
        guard let error = error as? IrohLib.IrohError else { return nil }
        return rejection(kind: error.kind(), debug: error.debugMessage())
    }

    static func rejection(kind: IrohLib.IrohErrorKind, debug: String) -> String? {
        // Fail closed on unfamiliar native failures. In particular the outer
        // .connect category also wraps local deadlines and local shutdown.
        let description = debug.lowercased().filter { $0.isLetter || $0.isNumber }
        guard !["timeout", "timedout", "locallyclosed", "locallyrejected", "localclosed"].contains(where: description.contains) else { return nil }
        let peerFailure = ["handshakefailure", "transporterror", "connectionclosed", "applicationclosed", "connectionreset", "resetbythepeer"]
            .contains(where: description.contains)
        guard peerFailure else { return nil }
        switch kind {
        case .connect: return "handshake"
        case .connection: return "closed_by_peer"
        default: return nil
        }
    }

    func failedConnect(_ error: Error) {
        lock.withLock { state.connectFailure = Self.rejection(error) ?? "unqualified_failure" }
    }

    func bind(secretKey: Data) async throws -> any DirectIrohEndpoint {
        begin()
        defer { end() }
        let base = try await binding.bind(secretKey: secretKey)
        let endpoint = Endpoint(base: base, owner: self)
        let stopped = lock.withLock {
            if !stopping { endpoints.append(endpoint) }
            return stopping
        }
        if stopped { await endpoint.close(); throw DirectIrohError.cancelled }
        return endpoint
    }

    func connected(_ connection: any DirectIrohConnection) throws {
        let stopped = lock.withLock {
            if !stopping { connections.append(connection) }
            return stopping
        }
        if stopped { connection.close(); throw DirectIrohError.cancelled }
    }

    /// Closing twice joins the same native close task. A transport-issued close
    /// may already be draining when the fixture starts its cleanup.
    func close() async throws {
        defer { lock.withLock { endpoints.removeAll(); connections.removeAll() } }
        let owned = lock.withLock {
            stopping = true
            return (endpoints, connections)
        }
        for connection in owned.1 { connection.close() }
        let cleanup = InteropBudget(seconds: 6)
        for endpoint in owned.0 {
            try await cleanup.run { await endpoint.close() }
        }
        while snapshot.nativeCalls != 0 {
            try interopRequire(cleanup.remaining > 0, "native calls did not settle during cleanup")
            try await Task.sleep(for: .milliseconds(10))
        }
        let late = lock.withLock { endpoints }
        for endpoint in late { try await cleanup.run { await endpoint.close() } }
        lock.withLock { endpoints.removeAll(); connections.removeAll() }
    }

    private final class Endpoint: DirectIrohEndpoint, @unchecked Sendable {
        let base: any DirectIrohEndpoint
        let owner: InteropEngine
        private let lock = NSLock()
        private var closing: Task<Void, Never>?
        init(base: any DirectIrohEndpoint, owner: InteropEngine) { self.base = base; self.owner = owner }
        func connect(to id: DirectEndpointID, addresses: [String], alpn: Data) async throws -> any DirectIrohConnection {
            owner.begin()
            defer { owner.end() }
            do {
                let connection = try await base.connect(to: id, addresses: addresses, alpn: alpn)
                try owner.connected(connection)
                return Connection(base: connection, owner: owner)
            } catch {
                owner.failedConnect(error)
                throw error
            }
        }
        func close() async {
            let task = lock.withLock {
                if let closing { return closing }
                let base = base
                let task = Task { await base.close() }
                closing = task
                return task
            }
            await task.value
        }
    }

    private final class Connection: DirectIrohConnection, @unchecked Sendable {
        let base: any DirectIrohConnection
        let owner: InteropEngine
        init(base: any DirectIrohConnection, owner: InteropEngine) { self.base = base; self.owner = owner }
        var remoteEndpointID: DirectEndpointID? { base.remoteEndpointID }
        func openStream() async throws -> DirectIrohStreamHalves {
            owner.begin()
            defer { owner.end() }
            let halves = try await base.openStream()
            if owner.isStopping {
                DirectIrohByteStream.abort(halves)
                throw DirectIrohError.cancelled
            }
            owner.streamOpened()
            return DirectIrohStreamHalves(send: Send(base: halves.send, owner: owner), receive: Receive(base: halves.receive, owner: owner))
        }
        func closed() async { await base.closed(); owner.connectionEnded() }
        func close() { base.close() }
    }

    private final class Send: DirectIrohSendHalf, @unchecked Sendable {
        let base: any DirectIrohSendHalf
        let owner: InteropEngine
        init(base: any DirectIrohSendHalf, owner: InteropEngine) { self.base = base; self.owner = owner }
        func writeAll(_ data: Data) async throws {
            owner.begin(); defer { owner.end() }
            try await base.writeAll(data)
        }
        func finish() async throws {
            owner.begin(); defer { owner.end() }
            try await base.finish()
        }
        func reset() async {
            owner.begin(); defer { owner.end() }
            await base.reset()
        }
    }

    private final class Receive: DirectIrohReceiveHalf, @unchecked Sendable {
        let base: any DirectIrohReceiveHalf
        let owner: InteropEngine
        private let lock = NSLock()
        private var head = Data()
        private var recorded = false
        init(base: any DirectIrohReceiveHalf, owner: InteropEngine) { self.base = base; self.owner = owner }
        func read(sizeLimit: Int) async throws -> Data {
            owner.begin()
            defer { owner.end() }
            let bytes = try await base.read(sizeLimit: sizeLimit)
            let status: Int? = lock.withLock {
                guard !recorded, head.count < 4096 else { return nil }
                head.append(bytes)
                guard head.range(of: Data("\r\n\r\n".utf8)) != nil else { return nil }
                recorded = true
                if head == Data("HTTP/1.1 200 Connection Established\r\n\r\n".utf8) { return 200 }
                if head == Data("HTTP/1.1 403 Forbidden\r\n\r\n".utf8) { return 403 }
                return nil
            }
            if let status { owner.probe(status) }
            return bytes
        }
        func stop() async {
            owner.begin(); defer { owner.end() }
            await base.stop()
        }
    }
}
