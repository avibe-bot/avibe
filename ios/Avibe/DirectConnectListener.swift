import Foundation
import Network

/// Bounds on one loopback CONNECT listener. The head limits are the
/// instance's own (docs/plans/remote-direct-ingress.md section 3).
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

    static let standard = DirectConnectLimits(
        headBytes: 4096,
        headSeconds: 5,
        openSeconds: 2,
        answerSeconds: 3,
        maxConnections: 64,
        lingerSeconds: 30,
        chunkBytes: 64 * 1024
    )
}

/// Why the direct path could not serve a connection WebKit sent to it. A
/// request the listener refused for its own content is not a failure of the
/// path and is not reported.
enum DirectConnectFailure: Equatable, Sendable {
    /// The transport did not open a stream in time.
    case streamUnavailable
    /// The instance answered the CONNECT with this status instead of `200`.
    case instanceRefused(Int)
    /// The instance did not answer the CONNECT, or not with HTTP.
    case noAnswer
    /// `maxConnections` was reached.
    case capacity
    /// The listening socket failed.
    case listenerFailed
}

/// WebKit's request head, as far as it has arrived.
enum DirectConnectRequest: Equatable {
    /// More bytes are needed.
    case incomplete
    /// A CONNECT for the authority. `earlyData` followed the head.
    case accepted(earlyData: Data)
    /// Refused with this HTTP status.
    case refused(Int)

    /// Accepts `CONNECT <host>:443 HTTP/1.1` for exactly `authority` and
    /// refuses everything else. `matchDomains` is a suffix match, so WebKit
    /// also sends this listener a CONNECT for any subdomain of the host.
    static func parse(
        _ received: Data,
        authority: DirectAuthority,
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
        // The target decides. Another host, a subdomain, another port, no
        // port, userinfo, and an IP literal all fail this one comparison.
        guard requestLine[1].lowercased() == authority.connectTarget else {
            return .refused(403)
        }
        if let host = hosts.first,
           host != authority.host,
           host != authority.connectTarget
        {
            return .refused(400)
        }
        return .accepted(earlyData: Data(received[end.upperBound...]))
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

private enum DirectConnectError: Error {
    case timedOut
    case closedBeforeHead
    case refused(Int)
    case noAnswer
}

/// A loopback HTTP CONNECT proxy for one instance authority. WebKit reaches
/// it through the account data store's `proxyConfigurations`; each accepted
/// connection is carried over one transport stream.
actor DirectConnectListener {
    private let authority: DirectAuthority
    private let limits: DirectConnectLimits
    private let openStream: @Sendable () async throws -> any DirectByteStream
    private let onFailure: @Sendable (DirectConnectFailure) -> Void
    private let queue = DispatchQueue(label: "bot.avibe.ios.direct-connect")
    private var listener: NWListener?
    private var relays: [UUID: Task<Void, Never>] = [:]
    private var isStopped = false

    init(
        authority: DirectAuthority,
        limits: DirectConnectLimits = .standard,
        openStream: @escaping @Sendable () async throws -> any DirectByteStream,
        onFailure: @escaping @Sendable (DirectConnectFailure) -> Void
    ) {
        self.authority = authority
        self.limits = limits
        self.openStream = openStream
        self.onFailure = onFailure
    }

    /// Binds `127.0.0.1` on a port the system chooses and returns that port.
    /// A socket bound to the loopback address receives nothing from another
    /// host.
    func start() async throws -> NWEndpoint.Port {
        guard !isStopped, listener == nil else {
            throw CancellationError()
        }
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(
            host: "127.0.0.1",
            port: .any
        )
        let listener = try NWListener(using: parameters, on: .any)
        self.listener = listener
        listener.newConnectionHandler = { [weak self, queue] connection in
            guard let self else {
                connection.cancel()
                return
            }
            // Started here, inside the listener's own handler, as the
            // listener expects of a connection it hands over.
            connection.start(queue: queue)
            Task {
                await self.accept(connection)
            }
        }
        return try await withCheckedThrowingContinuation { continuation in
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
                    if let port = listener?.port {
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
    }

    /// Stops accepting and closes every connection and its stream.
    func stop() {
        guard !isStopped else {
            return
        }
        isStopped = true
        listener?.cancel()
        listener = nil
        for relay in relays.values {
            relay.cancel()
        }
        relays.removeAll()
    }

    private func listenerDidFail() {
        guard !isStopped else {
            return
        }
        onFailure(.listenerFailed)
    }

    private func accept(_ connection: NWConnection) {
        guard !isStopped else {
            connection.cancel()
            return
        }
        guard relays.count < limits.maxConnections else {
            onFailure(.capacity)
            Task {
                await Self.refuse(connection, status: 503, within: 1)
                connection.cancel()
            }
            return
        }
        let id = UUID()
        let authority = authority
        let limits = limits
        let openStream = openStream
        let onFailure = onFailure
        relays[id] = Task { [weak self] in
            await Self.serve(
                connection,
                authority: authority,
                limits: limits,
                openStream: openStream,
                onFailure: onFailure
            )
            connection.cancel()
            await self?.relayDidEnd(id)
        }
    }

    private func relayDidEnd(_ id: UUID) {
        relays.removeValue(forKey: id)
    }

    // Everything below runs off the actor: one task per connection, so
    // copying bytes for one connection never waits for another.

    private static func serve(
        _ connection: NWConnection,
        authority: DirectAuthority,
        limits: DirectConnectLimits,
        openStream: @escaping @Sendable () async throws -> any DirectByteStream,
        onFailure: @Sendable (DirectConnectFailure) -> Void
    ) async {
        let earlyData: Data
        do {
            let request = try await withDeadline(limits.headSeconds) {
                try await readRequest(
                    from: connection,
                    authority: authority,
                    limits: limits
                )
            }
            switch request {
            case .accepted(let data):
                earlyData = data
            case .refused(let status):
                await refuse(
                    connection,
                    status: status,
                    within: limits.headSeconds
                )
                return
            case .incomplete:
                return
            }
        } catch {
            // A head that never completes, or a client that went away, is
            // closed without an answer.
            return
        }

        let stream: any DirectByteStream
        do {
            stream = try await withDeadline(limits.openSeconds) {
                let stream = try await openStream()
                // A stream that arrives after the deadline is not kept.
                if Task.isCancelled {
                    stream.cancel()
                    throw CancellationError()
                }
                return stream
            }
        } catch {
            guard !Task.isCancelled else {
                return
            }
            onFailure(.streamUnavailable)
            await refuse(
                connection,
                status: gatewayStatus(for: error),
                within: limits.headSeconds
            )
            return
        }
        defer {
            stream.cancel()
        }

        let leftover: Data
        do {
            leftover = try await withDeadline(limits.answerSeconds) {
                try await write(
                    DirectConnectWire.canonicalRequest(for: authority),
                    to: stream
                )
                return try await readAnswer(from: stream, limits: limits)
            }
        } catch {
            guard !Task.isCancelled else {
                return
            }
            stream.cancel()
            // The instance's refusal reaches WebKit as a failed proxy
            // connection with the same status.
            if case DirectConnectError.refused(let status) = error {
                onFailure(.instanceRefused(status))
                await refuse(
                    connection,
                    status: (400...599).contains(status) ? status : 502,
                    within: limits.headSeconds
                )
            } else {
                onFailure(.noAnswer)
                await refuse(
                    connection,
                    status: gatewayStatus(for: error),
                    within: limits.headSeconds
                )
            }
            return
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
            // Either side ended the tunnel. The defers close both.
        }
    }

    private static func gatewayStatus(for error: Error) -> Int {
        if case DirectConnectError.timedOut = error {
            return 504
        }
        return 502
    }

    private static func readRequest(
        from connection: NWConnection,
        authority: DirectAuthority,
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
                authority: authority,
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
    /// `seconds` to read the answer and close. Closing first, with bytes of
    /// the client's still unread, would reset the connection and could cost
    /// it the answer.
    private static func refuse(
        _ connection: NWConnection,
        status: Int,
        within seconds: TimeInterval
    ) async {
        _ = try? await withDeadline(seconds) {
            try await connection.sendAll(DirectConnectWire.refusal(status))
            try await connection.finishSending()
            while try await !connection.receiveChunk(maximumLength: 4096).isComplete {}
        }
    }

    private static func withDeadline<Value: Sendable>(
        _ seconds: TimeInterval,
        _ operation: @escaping @Sendable () async throws -> Value
    ) async throws -> Value {
        try await withThrowingTaskGroup(of: Value.self) { group in
            defer {
                group.cancelAll()
            }
            group.addTask(operation: operation)
            group.addTask {
                try await Task.sleep(for: .seconds(seconds))
                throw DirectConnectError.timedOut
            }
            guard let value = try await group.next() else {
                throw DirectConnectError.timedOut
            }
            return value
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
}

private extension NWConnection {
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
        try await send(data, isFinal: false)
    }

    /// Tells the peer this side has no more bytes.
    func finishSending() async throws {
        try await send(nil, isFinal: true)
    }

    private func send(_ data: Data?, isFinal: Bool) async throws {
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
