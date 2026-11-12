import Foundation
import Network

/// Bounds on the loopback CONNECT listener. The head limits are the
/// instance's own (docs/plans/remote-direct-ingress.md section 3). The time
/// bounds are proposed values for source and tests, not measurements
/// (docs/plans/remote-direct-ios.md section 6).
struct DirectConnectLimits: Equatable, Sendable {
    /// The largest request head from WebKit, and the largest answer head
    /// from the instance.
    let headBytes: Int
    /// How long WebKit has to finish its request head.
    let headSeconds: TimeInterval
    /// How long the transport has to open a stream.
    let openSeconds: TimeInterval
    /// How long the instance has to answer the CONNECT.
    let answerSeconds: TimeInterval
    /// Connections served at once, counted from accept to close.
    let maxConnections: Int
    /// After one side finishes sending, how long the other has to finish.
    let lingerSeconds: TimeInterval
    /// The most bytes one relay holds in memory per direction.
    let chunkBytes: Int
    /// The most bytes WebKit may send after its CONNECT head before a
    /// backend is selected. They are held, never forwarded early.
    let earlyBytes: Int
    /// How long a direct attempt runs alone before the public origin is
    /// also dialled.
    let hedgeSeconds: TimeInterval

    static let standard = DirectConnectLimits(
        headBytes: 4096,
        headSeconds: 5,
        openSeconds: 2,
        answerSeconds: 3,
        maxConnections: 64,
        lingerSeconds: 30,
        chunkBytes: 64 * 1024,
        earlyBytes: 16 * 1024,
        hedgeSeconds: 0.5
    )
}

/// WebKit's request head, as far as it has arrived.
enum DirectConnectRequest: Equatable {
    /// More bytes are needed.
    case incomplete
    /// A CONNECT for an allowed authority. `earlyData` followed the head.
    case accepted(DirectAuthority, earlyData: Data)
    /// The listener's own ownership check, carrying a nonce.
    case ownershipCheck(nonce: String)
    /// Refused with this HTTP status.
    case refused(Int)

    /// Accepts `CONNECT <host>:443 HTTP/1.1` for exactly one of
    /// `allowedHosts` and refuses everything else. `matchDomains` is a
    /// suffix match, so WebKit also sends this listener a CONNECT for any
    /// subdomain of a routed host.
    static func parse(
        _ received: Data,
        allowedHosts: Set<String>,
        limit: Int
    ) -> DirectConnectRequest {
        guard let end = DirectConnectWire.endOfHead(in: received) else {
            return received.count > limit ? .refused(400) : .incomplete
        }
        guard
            end.lowerBound - received.startIndex <= limit,
            let lines = DirectConnectWire.lines(of: received[..<end.lowerBound])
        else {
            return .refused(400)
        }
        let requestLine = lines[0].split(
            separator: " ",
            omittingEmptySubsequences: false
        )
        guard requestLine.count == 3, !requestLine[0].isEmpty else {
            return .refused(400)
        }
        if requestLine[0] == "OWNER", requestLine[2] == "HTTP/1.1", lines.count == 1 {
            return .ownershipCheck(nonce: String(requestLine[1]))
        }
        guard requestLine[0] == "CONNECT" else {
            return .refused(405)
        }
        guard requestLine[2] == "HTTP/1.1" else {
            return .refused(400)
        }

        var hosts: [String] = []
        for line in lines.dropFirst() {
            guard
                let colon = line.firstIndex(of: ":"),
                colon != line.startIndex,
                !line[..<colon].contains(where: { $0 == " " || $0 == "\t" })
            else {
                return .refused(400)
            }
            let name = line[..<colon].lowercased()
            if name == "content-length" || name == "transfer-encoding" {
                return .refused(400)
            }
            if name == "host" {
                hosts.append(
                    line[line.index(after: colon)...]
                        .trimmingCharacters(in: .whitespaces)
                        .lowercased()
                )
            }
        }
        guard hosts.count <= 1 else {
            return .refused(400)
        }
        // The target decides: exactly an allowed host, on port 443. Another
        // host, a subdomain, another port, no port, userinfo, and an IP
        // literal all fail here.
        let target = requestLine[1].lowercased()
        guard target.hasSuffix(":443") else {
            return .refused(403)
        }
        let host = String(target.dropLast(4))
        guard
            allowedHosts.contains(host),
            let authority = DirectAuthority(host: host)
        else {
            return .refused(403)
        }
        if let header = hosts.first,
           header != authority.host,
           header != authority.connectTarget
        {
            return .refused(400)
        }
        return .accepted(authority, earlyData: Data(received[end.upperBound...]))
    }
}

/// The instance's answer to the CONNECT, as far as it has arrived.
enum DirectConnectAnswer: Equatable {
    case incomplete
    /// `200`. `leftover` followed the head.
    case established(leftover: Data)
    /// Any other status.
    case refused(Int)
    /// Not an HTTP/1.1 response head, or one over the limit.
    case malformed

    static func parse(_ received: Data, limit: Int) -> DirectConnectAnswer {
        guard let end = DirectConnectWire.endOfHead(in: received) else {
            return received.count > limit ? .malformed : .incomplete
        }
        guard
            end.lowerBound - received.startIndex <= limit,
            let lines = DirectConnectWire.lines(of: received[..<end.lowerBound])
        else {
            return .malformed
        }
        let statusLine = lines[0].split(
            separator: " ",
            maxSplits: 2,
            omittingEmptySubsequences: false
        )
        guard
            statusLine.count >= 2,
            statusLine[0] == "HTTP/1.1",
            statusLine[1].utf8.count == 3,
            let status = Int(statusLine[1]),
            (100...599).contains(status)
        else {
            return .malformed
        }
        guard status == 200 else {
            return .refused(status)
        }
        return .established(leftover: Data(received[end.upperBound...]))
    }
}

enum DirectConnectWire {
    static func endOfHead(in data: Data) -> Range<Data.Index>? {
        data.range(of: Data("\r\n\r\n".utf8))
    }

    /// The head's lines, or `nil` when it is not printable ASCII.
    static func lines(of head: Data) -> [Substring]? {
        guard
            head.allSatisfy({ $0 == 0x0D || $0 == 0x0A || $0 == 0x09 || (0x20...0x7E).contains($0) }),
            let text = String(data: head, encoding: .ascii)
        else {
            return nil
        }
        var lines: [Substring] = []
        var rest = Substring(text)
        while let separator = rest.range(of: "\r\n") {
            lines.append(rest[..<separator.lowerBound])
            rest = rest[separator.upperBound...]
        }
        lines.append(rest)
        // A bare CR or LF inside a line is not a line ending.
        guard lines.allSatisfy({ !$0.contains("\r") && !$0.contains("\n") }) else {
            return nil
        }
        return lines
    }

    /// The head written to the instance: the ingress contract's canonical
    /// one, never the bytes WebKit sent.
    static func canonicalRequest(for authority: DirectAuthority) -> Data {
        Data(
            "CONNECT \(authority.connectTarget) HTTP/1.1\r\nHost: \(authority.connectTarget)\r\n\r\n".utf8
        )
    }

    static let established = Data(
        "HTTP/1.1 200 Connection Established\r\n\r\n".utf8
    )

    /// The listener's answer to its own ownership check.
    static func ownershipAnswer(nonce: String) -> Data {
        Data("HTTP/1.1 204 No Content\r\nX-Avibe-Owner: \(nonce)\r\n\r\n".utf8)
    }

    static func refusal(_ status: Int) -> Data {
        let reason: String
        switch status {
        case 400: reason = "Bad Request"
        case 403: reason = "Forbidden"
        case 405: reason = "Method Not Allowed"
        case 502: reason = "Bad Gateway"
        case 503: reason = "Service Unavailable"
        case 504: reason = "Gateway Timeout"
        default: reason = "Refused"
        }
        return Data(
            "HTTP/1.1 \(status) \(reason)\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".utf8
        )
    }
}

enum DirectConnectError: Error, Equatable {
    case timedOut
    case closedBeforeHead
    case refused(Int)
    case noAnswer
}

/// Runs `operation` with a deadline. The operation must honour cancellation
/// promptly; the call returns once it has. A value the operation produces
/// after the deadline already decided is handed to `discard`, so a stream
/// that arrives late is closed rather than leaked.
func directDeadline<Value: Sendable>(
    _ seconds: TimeInterval,
    discard: @escaping @Sendable (Value) -> Void = { _ in },
    _ operation: @escaping @Sendable () async throws -> Value
) async throws -> Value {
    let outcome: Result<Value, Error> = await withTaskGroup(
        of: Result<Value, Error>?.self
    ) { group in
        group.addTask {
            do {
                return .success(try await operation())
            } catch {
                return .failure(error)
            }
        }
        group.addTask {
            do {
                try await Task.sleep(for: .seconds(seconds))
            } catch {
                // Cancelled: the operation finished first.
                return nil
            }
            return .failure(DirectConnectError.timedOut)
        }
        var first: Result<Value, Error>?
        while let next = await group.next() {
            guard let result = next else {
                continue
            }
            if first == nil {
                first = result
                group.cancelAll()
            } else if case .success(let late) = result {
                discard(late)
            }
        }
        return first ?? .failure(DirectConnectError.timedOut)
    }
    return try outcome.get()
}

/// Which backend a committed relay runs on.
enum DirectRelayBackend: Equatable, Sendable {
    case direct
    case publicOrigin
}

/// What the path controller decides for one CONNECT the listener accepted
/// (docs/plans/remote-direct-ios.md section 6).
struct DirectConnectPlan: Sendable {
    /// Opens a direct stream. `nil` when direct is not authorized for this
    /// CONNECT.
    let openDirect: (@Sendable () async throws -> any DirectByteStream)?
    /// Opens a connection to the host's public origin.
    let openPublic: @Sendable () async throws -> any DirectByteStream
    /// Whether the selection, lease and path generation this plan was made
    /// under are all still current. Checked when a candidate is ready and
    /// again just before the commit point.
    let isCurrent: @Sendable () async -> Bool
    /// The direct candidate failed before anything was forwarded.
    let directFailed: @Sendable () -> Void
}

/// Decides each accepted CONNECT. `nil` refuses it.
typealias DirectConnectPlanner = @Sendable (DirectAuthority) async -> DirectConnectPlan?

/// The App's loopback HTTP CONNECT proxy. There is one per App process, and
/// every route the App writes points at its port. WebKit reaches it through
/// the account data stores' `proxyConfigurations`. For each CONNECT it relays
/// opaque bytes to a direct stream or to the host's public origin; it never
/// reads or terminates TLS.
actor DirectConnectListener {
    private struct Relay {
        let task: Task<Void, Never>
        /// Set at the commit point; until then the relay is a candidate.
        var committed: (host: String, backend: DirectRelayBackend)?
    }

    private let limits: DirectConnectLimits
    private let planner: DirectConnectPlanner
    private let onListenerFailure: @Sendable () -> Void
    private let queue = DispatchQueue(label: "bot.avibe.ios.direct-connect")
    /// Made once per listener. Only this listener's accept loop answers an
    /// ownership check that carries it.
    let ownershipNonce: String
    private var listener: NWListener?
    private(set) var port: UInt16?
    private var allowedHosts: Set<String> = []
    private var relays: [UUID: Relay] = [:]
    private var isStopped = false

    init(
        limits: DirectConnectLimits = .standard,
        ownershipNonce: String = UUID().uuidString,
        planner: @escaping DirectConnectPlanner,
        onListenerFailure: @escaping @Sendable () -> Void
    ) {
        self.limits = limits
        self.ownershipNonce = ownershipNonce
        self.planner = planner
        self.onListenerFailure = onListenerFailure
    }

    /// The hosts a CONNECT may name, each on port 443 only.
    func setAllowedHosts(_ hosts: Set<String>) {
        allowedHosts = hosts
    }

    /// Adds one host to those a CONNECT may name.
    func allowHost(_ host: String) {
        allowedHosts.insert(host)
    }

    /// Binds `127.0.0.1` on exactly `port`, or on a port the system chooses
    /// when `port` is `nil`, and returns the bound port. The socket does not
    /// share its address (`allowLocalEndpointReuse` is off), so a port held by
    /// anyone else fails the bind.
    func bind(port requested: UInt16?) async throws -> UInt16 {
        guard !isStopped, listener == nil else {
            throw CancellationError()
        }
        let endpointPort: NWEndpoint.Port
        if let requested {
            guard let exact = NWEndpoint.Port(rawValue: requested) else {
                throw CancellationError()
            }
            endpointPort = exact
        } else {
            endpointPort = .any
        }
        let parameters = NWParameters.tcp
        parameters.allowLocalEndpointReuse = false
        parameters.requiredLocalEndpoint = .hostPort(
            host: "127.0.0.1",
            port: endpointPort
        )
        // An exact port is given once, in the required local endpoint:
        // passing it to `NWListener` as well fails with EINVAL ("Local
        // endpoint has port set, cannot override").
        let listener: NWListener
        if requested == nil {
            listener = try NWListener(using: parameters, on: .any)
        } else {
            listener = try NWListener(using: parameters)
        }
        self.listener = listener
        listener.newConnectionHandler = { [weak self, queue] connection in
            guard let self else {
                connection.cancel()
                return
            }
            // Keep this start inside the handler, and keep `acceptLocalOnly`
            // off the parameters above. With the connection started after
            // the handler returned and that flag set, the iOS simulator
            // reset every accepted connection. Which of the two caused it
            // was not established; macOS showed neither.
            connection.start(queue: queue)
            Task {
                await self.accept(connection)
            }
        }
        let bound: UInt16 = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                listener.stateUpdateHandler = { [weak self, weak listener] state in
                    switch state {
                    case .ready:
                        // From here on, a state change is the socket failing.
                        listener?.stateUpdateHandler = { [weak self] state in
                            if case .failed = state {
                                Task {
                                    await self?.listenerDidFail()
                                }
                            }
                        }
                        if let port = listener?.port?.rawValue {
                            continuation.resume(returning: port)
                        } else {
                            continuation.resume(throwing: CancellationError())
                        }
                    case .failed(let error), .waiting(let error):
                        listener?.stateUpdateHandler = nil
                        listener?.cancel()
                        continuation.resume(throwing: error)
                    case .cancelled:
                        listener?.stateUpdateHandler = nil
                        continuation.resume(throwing: CancellationError())
                    default:
                        break
                    }
                }
                listener.start(queue: queue)
            }
        } onCancel: {
            // A cancelled bind ends in `.cancelled`, which resumes above.
            listener.cancel()
        }
        if self.listener !== listener {
            // Closed or stopped while binding.
            listener.cancel()
            throw CancellationError()
        }
        port = bound
        return bound
    }

    /// Closes the listening socket and keeps every accepted connection
    /// (B2 on background). `bind(port:)` may follow.
    func closeListeningSocket() {
        listener?.stateUpdateHandler = nil
        listener?.cancel()
        listener = nil
    }

    /// Stops for good: the listening socket and every connection.
    func stop() {
        guard !isStopped else {
            return
        }
        isStopped = true
        closeListeningSocket()
        for relay in relays.values {
            relay.task.cancel()
        }
        relays.removeAll()
    }

    /// Closes the committed relays on `backend` (`nil`: both), and cancels
    /// every relay not yet committed. Returns, per host, how many committed
    /// relays it closed. A relay this listener did not accept is never
    /// touched.
    func closeRelays(backend: DirectRelayBackend?) -> [String: Int] {
        var closed: [String: Int] = [:]
        for (id, relay) in relays {
            if let committed = relay.committed {
                guard backend == nil || committed.backend == backend else {
                    continue
                }
                closed[committed.host, default: 0] += 1
            }
            relay.task.cancel()
            relays.removeValue(forKey: id)
        }
        return closed
    }

    /// Cancels every relay not yet committed; committed ones are untouched.
    func cancelUncommitted() {
        for (id, relay) in relays where relay.committed == nil {
            relay.task.cancel()
            relays.removeValue(forKey: id)
        }
    }

    /// How many relays are committed, per backend. For diagnostics and tests.
    func committedCount(backend: DirectRelayBackend) -> Int {
        relays.values.filter { $0.committed?.backend == backend }.count
    }

    private func listenerDidFail() {
        guard !isStopped, listener != nil else {
            return
        }
        onListenerFailure()
    }

    private func accept(_ connection: NWConnection) {
        guard !isStopped else {
            connection.cancel()
            return
        }
        guard relays.count < limits.maxConnections else {
            // Closed at once, with no answer and no task of its own, so the
            // limit bounds every connection this listener holds.
            connection.cancel()
            return
        }
        let id = UUID()
        let limits = limits
        let planner = planner
        let allowedHosts = allowedHosts
        let nonce = ownershipNonce
        relays[id] = Relay(task: Task { [weak self] in
            await Self.serve(
                connection,
                id: id,
                listener: self,
                allowedHosts: allowedHosts,
                ownershipNonce: nonce,
                limits: limits,
                planner: planner
            )
            connection.cancel()
            await self?.relayDidEnd(id)
        })
    }

    private func relayDidEnd(_ id: UUID) {
        relays.removeValue(forKey: id)
    }

    /// Records the commit point. `false` when the relay was closed meanwhile
    /// (an account transition, a stop), so nothing may be forwarded.
    private func commit(_ id: UUID, host: String, backend: DirectRelayBackend) -> Bool {
        guard !isStopped, relays[id] != nil else {
            return false
        }
        relays[id]?.committed = (host, backend)
        return true
    }

    private func uncommit(_ id: UUID) {
        relays[id]?.committed = nil
    }

    // Everything below runs off the actor: one task per connection, so
    // copying bytes for one connection never waits for another.

    private enum RaceOutcome {
        case won(DirectRelayBackend, any DirectByteStream, leftover: Data)
        /// A candidate was ready but its plan was no longer current.
        case fenced
        case failed(status: Int)
    }

    private static func serve(
        _ connection: NWConnection,
        id: UUID,
        listener: DirectConnectListener?,
        allowedHosts: Set<String>,
        ownershipNonce: String,
        limits: DirectConnectLimits,
        planner: DirectConnectPlanner
    ) async {
        let authority: DirectAuthority
        let earlyData: Data
        do {
            let request = try await directDeadline(limits.headSeconds) {
                try await readRequest(
                    from: connection,
                    allowedHosts: allowedHosts,
                    limits: limits
                )
            }
            switch request {
            case .accepted(let accepted, let data):
                authority = accepted
                earlyData = data
            case .ownershipCheck(let nonce):
                if nonce == ownershipNonce {
                    _ = try? await directDeadline(0.25) {
                        try await connection.sendAll(
                            DirectConnectWire.ownershipAnswer(nonce: nonce)
                        )
                        try await connection.finishSending()
                    }
                } else {
                    await refuse(connection, status: 405, within: limits.headSeconds)
                }
                return
            case .refused(let status):
                await refuse(connection, status: status, within: limits.headSeconds)
                return
            case .incomplete:
                return
            }
        } catch {
            // A head that never completes, or a client that went away, is
            // closed without an answer.
            return
        }
        // Bytes sent ahead of the answer are held, never forwarded before a
        // backend is selected, and bounded.
        guard earlyData.count <= limits.earlyBytes else {
            return
        }

        // One fresh plan if the first one is fenced before its commit point:
        // the selection, lease or path generation changed while it raced.
        for _ in 0..<2 {
            guard !Task.isCancelled, let plan = await planner(authority) else {
                if !Task.isCancelled {
                    await refuse(connection, status: 403, within: limits.headSeconds)
                }
                return
            }
            switch await race(plan, authority: authority, limits: limits) {
            case .fenced:
                continue
            case .failed(let status):
                guard !Task.isCancelled else {
                    return
                }
                await answerRefusal(connection, status: status)
                await awaitClose(connection, within: limits.headSeconds)
                return
            case .won(let backend, let stream, let leftover):
                // The commit point. Registered first, so an event that comes
                // after the check below finds this relay and closes it.
                guard
                    let listener,
                    await listener.commit(id, host: authority.host, backend: backend)
                else {
                    stream.cancel()
                    return
                }
                guard await plan.isCurrent() else {
                    await listener.uncommit(id)
                    stream.cancel()
                    continue
                }
                defer {
                    stream.cancel()
                }
                do {
                    try await connection.sendAll(DirectConnectWire.established)
                    try await relay(
                        connection,
                        stream,
                        earlyData: earlyData,
                        leftover: leftover,
                        limits: limits
                    )
                } catch {
                    // Either side ended the tunnel, or the relay was closed.
                    // A tunnel is never redialled and nothing is replayed.
                }
                return
            }
        }
        await answerRefusal(connection, status: 503)
        await awaitClose(connection, within: limits.headSeconds)
    }

    private enum Candidate {
        case direct
        case publicOrigin
    }

    private enum CandidateResult {
        case ready(Candidate, any DirectByteStream, Data)
        case failed(Candidate, Error)
    }

    /// Races the candidates of one plan. Only this function selects a winner:
    /// the first ready candidate whose plan is still current. Every other
    /// candidate is cancelled, and one that becomes ready anyway is closed.
    /// No byte from WebKit has been sent on any of them.
    private static func race(
        _ plan: DirectConnectPlan,
        authority: DirectAuthority,
        limits: DirectConnectLimits
    ) async -> RaceOutcome {
        let directAttempted = plan.openDirect != nil
        let directSettled = DirectSettledFlag()
        return await withTaskGroup(of: CandidateResult.self) { group in
            if let openDirect = plan.openDirect {
                group.addTask {
                    do {
                        let stream = try await directDeadline(
                            limits.openSeconds,
                            discard: { $0.cancel() }
                        ) {
                            try await openDirect()
                        }
                        do {
                            let leftover = try await directDeadline(limits.answerSeconds) {
                                try await write(
                                    DirectConnectWire.canonicalRequest(for: authority),
                                    to: stream
                                )
                                return try await readAnswer(from: stream, limits: limits)
                            }
                            return .ready(.direct, stream, leftover)
                        } catch {
                            stream.cancel()
                            throw error
                        }
                    } catch {
                        // Lets the public candidate start now instead of
                        // waiting out the hedge.
                        directSettled.set()
                        return .failed(.direct, error)
                    }
                }
            }
            group.addTask {
                if directAttempted {
                    // The hedge: the public origin is dialled when the direct
                    // attempt has had its head start, or as soon as it fails.
                    let deadline = ContinuousClock.now.advanced(by: .seconds(limits.hedgeSeconds))
                    while !directSettled.isSet, ContinuousClock.now < deadline {
                        do {
                            try await Task.sleep(for: .milliseconds(25))
                        } catch {
                            return .failed(.publicOrigin, CancellationError())
                        }
                    }
                }
                guard !Task.isCancelled else {
                    return .failed(.publicOrigin, CancellationError())
                }
                do {
                    let stream = try await plan.openPublic()
                    return .ready(.publicOrigin, stream, Data())
                } catch {
                    return .failed(.publicOrigin, error)
                }
            }

            var winner: (DirectRelayBackend, any DirectByteStream, Data)?
            var fenced = false
            var directRefusal: Int?
            var directTimedOut = false
            while let result = await group.next() {
                switch result {
                case .ready(let candidate, let stream, let leftover):
                    guard winner == nil, !Task.isCancelled else {
                        stream.cancel()
                        continue
                    }
                    if await plan.isCurrent() {
                        winner = (candidate == .direct ? .direct : .publicOrigin, stream, leftover)
                        group.cancelAll()
                    } else {
                        // Fenced: every candidate of this plan is invalid.
                        stream.cancel()
                        fenced = true
                        group.cancelAll()
                    }
                case .failed(let candidate, let error):
                    guard candidate == .direct, winner == nil, !fenced, !(error is CancellationError) else {
                        continue
                    }
                    if case DirectConnectError.refused(let status) = error {
                        directRefusal = status
                    } else if case DirectConnectError.timedOut = error {
                        directTimedOut = true
                    }
                    plan.directFailed()
                }
            }
            if let winner {
                return .won(winner.0, winner.1, leftover: winner.2)
            }
            if fenced {
                return .fenced
            }
            if let directRefusal, (400...599).contains(directRefusal) {
                return .failed(status: directRefusal)
            }
            return .failed(status: directTimedOut ? 504 : 502)
        }
    }

    private static func readRequest(
        from connection: NWConnection,
        allowedHosts: Set<String>,
        limits: DirectConnectLimits
    ) async throws -> DirectConnectRequest {
        var received = Data()
        while true {
            let chunk = try await connection.receiveChunk(
                maximumLength: limits.chunkBytes
            )
            received.append(chunk.data)
            let request = DirectConnectRequest.parse(
                received,
                allowedHosts: allowedHosts,
                limit: limits.headBytes
            )
            if request != .incomplete {
                return request
            }
            if chunk.isComplete {
                throw DirectConnectError.closedBeforeHead
            }
        }
    }

    private static func readAnswer(
        from stream: any DirectByteStream,
        limits: DirectConnectLimits
    ) async throws -> Data {
        var received = Data()
        while true {
            guard
                let data = try await read(
                    from: stream,
                    maxLength: limits.chunkBytes
                )
            else {
                throw DirectConnectError.noAnswer
            }
            received.append(data)
            switch DirectConnectAnswer.parse(received, limit: limits.headBytes) {
            case .incomplete:
                continue
            case .established(let leftover):
                return leftover
            case .refused(let status):
                throw DirectConnectError.refused(status)
            case .malformed:
                throw DirectConnectError.noAnswer
            }
        }
    }

    /// Copies both directions until both have finished. Each direction reads
    /// its next chunk only after the previous one was accepted, so a slow
    /// reader on one side slows the sender on the other.
    private static func relay(
        _ connection: NWConnection,
        _ stream: any DirectByteStream,
        earlyData: Data,
        leftover: Data,
        limits: DirectConnectLimits
    ) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            defer {
                group.cancelAll()
            }
            group.addTask {
                var pending = earlyData
                while true {
                    if !pending.isEmpty {
                        try await write(pending, to: stream)
                    }
                    let chunk = try await connection.receiveChunk(
                        maximumLength: limits.chunkBytes
                    )
                    pending = chunk.data
                    if chunk.isComplete {
                        if !pending.isEmpty {
                            try await write(pending, to: stream)
                        }
                        try await closeWrite(of: stream)
                        return
                    }
                }
            }
            group.addTask {
                var pending = leftover
                while true {
                    if !pending.isEmpty {
                        try await connection.sendAll(pending)
                    }
                    guard
                        let data = try await read(
                            from: stream,
                            maxLength: limits.chunkBytes
                        )
                    else {
                        try await connection.finishSending()
                        return
                    }
                    pending = data
                }
            }
            // One side has finished sending. The other direction keeps
            // going, for a bounded time.
            _ = try await group.next()
            group.addTask {
                try await Task.sleep(for: .seconds(limits.lingerSeconds))
                throw DirectConnectError.timedOut
            }
            _ = try await group.next()
        }
    }

    /// Answers a request that will not be tunnelled, then gives the client
    /// `seconds` to read the answer and close.
    private static func refuse(
        _ connection: NWConnection,
        status: Int,
        within seconds: TimeInterval
    ) async {
        await answerRefusal(connection, status: status)
        await awaitClose(connection, within: seconds)
    }

    /// Writes the refusal and finishes sending. A short write to a socket
    /// that was just accepted; bounded in case the client is not reading.
    private static func answerRefusal(
        _ connection: NWConnection,
        status: Int
    ) async {
        _ = try? await directDeadline(0.25) {
            try await connection.sendAll(DirectConnectWire.refusal(status))
            try await connection.finishSending()
        }
    }

    /// Gives the client `seconds` to read the answer and close. Closing
    /// first, with bytes of the client's still unread, would reset the
    /// connection and could cost it the answer.
    private static func awaitClose(
        _ connection: NWConnection,
        within seconds: TimeInterval
    ) async {
        _ = try? await directDeadline(seconds) {
            while try await !connection.receiveChunk(maximumLength: 4096).isComplete {}
        }
    }

    // A cancelled task ends its stream, so a stream that does not watch for
    // cancellation itself still stops.

    private static func read(
        from stream: any DirectByteStream,
        maxLength: Int
    ) async throws -> Data? {
        try await withTaskCancellationHandler {
            try await stream.read(maxLength: maxLength)
        } onCancel: {
            stream.cancel()
        }
    }

    private static func write(
        _ data: Data,
        to stream: any DirectByteStream
    ) async throws {
        try await withTaskCancellationHandler {
            try await stream.write(data)
        } onCancel: {
            stream.cancel()
        }
    }

    private static func closeWrite(
        of stream: any DirectByteStream
    ) async throws {
        try await withTaskCancellationHandler {
            try await stream.closeWrite()
        } onCancel: {
            stream.cancel()
        }
    }

    /// Connects to `port` on the loopback address and asks the listener
    /// there for `nonce`. True only when this listener's own accept loop
    /// answered. A liveness and correlation check for the App's own port,
    /// not authentication against a hostile local process.
    static func ownershipCheck(
        port: UInt16,
        nonce: String,
        within seconds: TimeInterval
    ) async -> Bool {
        guard let endpointPort = NWEndpoint.Port(rawValue: port) else {
            return false
        }
        let connection = NWConnection(
            host: "127.0.0.1",
            port: endpointPort,
            using: .tcp
        )
        let stream = NWConnectionByteStream(connection: connection)
        defer {
            stream.cancel()
        }
        do {
            return try await directDeadline(seconds) {
                try await stream.connect()
                try await stream.write(Data("OWNER \(nonce) HTTP/1.1\r\n\r\n".utf8))
                var received = Data()
                while let data = try await stream.read(maxLength: 1024) {
                    received.append(data)
                    if received.count > 1024 {
                        break
                    }
                }
                return received == DirectConnectWire.ownershipAnswer(nonce: nonce)
            }
        } catch {
            return false
        }
    }
}

/// Set once the direct candidate has settled, either way.
private final class DirectSettledFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    var isSet: Bool {
        lock.withLock { value }
    }

    func set() {
        lock.withLock { value = true }
    }
}

extension NWConnection {
    /// The next bytes. `isComplete` once the peer has finished sending.
    func receiveChunk(
        maximumLength: Int
    ) async throws -> (data: Data, isComplete: Bool) {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                self.receive(
                    minimumIncompleteLength: 1,
                    maximumLength: maximumLength
                ) { data, _, isComplete, error in
                    if let error {
                        continuation.resume(throwing: error)
                    } else {
                        continuation.resume(
                            returning: (data ?? Data(), isComplete)
                        )
                    }
                }
            }
        } onCancel: {
            self.cancel()
        }
    }

    /// Returns once the stack has taken `data`.
    func sendAll(_ data: Data) async throws {
        try await sendChunk(data, isFinal: false)
    }

    /// Tells the peer this side has no more bytes.
    func finishSending() async throws {
        try await sendChunk(nil, isFinal: true)
    }

    private func sendChunk(_ data: Data?, isFinal: Bool) async throws {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<Void, Error>) in
                self.send(
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
        } onCancel: {
            self.cancel()
        }
    }
}
