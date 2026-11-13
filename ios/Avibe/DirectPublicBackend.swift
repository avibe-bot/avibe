import Darwin
import Foundation
import Network

// The public backend of the App's local proxy: the App's own TCP connection
// to a public address of an allowed instance host, port 443. It carries
// WebKit's TLS bytes unchanged; WebKit's session ends at the ordinary public
// endpoint for that host (in Avibe's deployment, the Cloudflare edge).
// Design: docs/plans/remote-direct-ios.md section 6.

/// A numeric address a host name resolved to.
struct DirectResolvedAddress: Equatable, Sendable, CustomStringConvertible {
    enum Family: Equatable, Sendable {
        case ipv4
        case ipv6
    }

    let family: Family
    /// Network byte order: 4 bytes for IPv4, 16 for IPv6.
    let bytes: [UInt8]

    init?(ipv4 bytes: [UInt8]) {
        guard bytes.count == 4 else {
            return nil
        }
        family = .ipv4
        self.bytes = bytes
    }

    init?(ipv6 bytes: [UInt8]) {
        guard bytes.count == 16 else {
            return nil
        }
        family = .ipv6
        self.bytes = bytes
    }

    var description: String {
        switch family {
        case .ipv4:
            return bytes.map(String.init).joined(separator: ".")
        case .ipv6:
            return stride(from: 0, to: 16, by: 2)
                .map { String(UInt16(bytes[$0]) << 8 | UInt16(bytes[$0 + 1]), radix: 16) }
                .joined(separator: ":")
        }
    }
}

/// Which resolved addresses the public backend may connect to: public
/// unicast only. Loopback, unspecified, link-local, multicast, private,
/// shared (CGNAT), and documentation or benchmarking ranges are refused. A
/// local-network path is the direct backend's job, not this one's.
enum DirectPublicAddressPolicy {
    static func allows(_ address: DirectResolvedAddress) -> Bool {
        switch address.family {
        case .ipv4:
            return allowsIPv4(address.bytes)
        case .ipv6:
            return allowsIPv6(address.bytes)
        }
    }

    static func allowsIPv4(_ bytes: [UInt8]) -> Bool {
        guard bytes.count == 4 else {
            return false
        }
        let (first, second, third) = (bytes[0], bytes[1], bytes[2])
        switch first {
        case 0, 10, 127:
            // This network, private, loopback.
            return false
        case 100 where (64...127).contains(second):
            // Shared address space (CGNAT).
            return false
        case 169 where second == 254:
            // Link-local.
            return false
        case 172 where (16...31).contains(second):
            return false
        case 192 where second == 168:
            return false
        case 192 where second == 0 && (third == 0 || third == 2):
            // IETF protocol assignments, documentation.
            return false
        case 198 where second == 18 || second == 19:
            // Benchmarking.
            return false
        case 198 where second == 51 && third == 100:
            return false
        case 203 where second == 0 && third == 113:
            return false
        case 224...255:
            // Multicast, reserved, broadcast.
            return false
        default:
            return true
        }
    }

    static func allowsIPv6(_ bytes: [UInt8]) -> Bool {
        guard bytes.count == 16 else {
            return false
        }
        // An IPv4 address carried in IPv6 is judged as that IPv4 address:
        // IPv4-mapped (::ffff:a.b.c.d) and the NAT64 well-known prefix
        // (64:ff9b::/96), which iOS synthesizes on IPv6-only networks.
        let prefix = Array(bytes[0..<12])
        if prefix == [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0xFF]
            || prefix == [0x00, 0x64, 0xFF, 0x9B, 0, 0, 0, 0, 0, 0, 0, 0]
        {
            return allowsIPv4(Array(bytes[12..<16]))
        }
        // Global unicast only (2000::/3). This excludes the unspecified and
        // loopback addresses, unique-local (fc00::/7), link-local
        // (fe80::/10), and multicast (ff00::/8).
        guard bytes[0] & 0xE0 == 0x20 else {
            return false
        }
        // Teredo (2001::/32) and documentation (2001:db8::/32).
        if bytes[0] == 0x20, bytes[1] == 0x01,
           (bytes[2] == 0x00 && bytes[3] == 0x00)
            || (bytes[2] == 0x0D && bytes[3] == 0xB8)
        {
            return false
        }
        // 6to4 (2002::/16) carries an IPv4 address of any kind.
        if bytes[0] == 0x20, bytes[1] == 0x02 {
            return false
        }
        return true
    }
}

enum DirectPublicError: Error, Equatable {
    case resolutionFailed
    case noPublicAddress
    case invalidAddress
}

/// Resolves a host name to its numeric addresses.
protocol DirectHostResolving: Sendable {
    /// Every address `host` resolves to. Returns promptly when its task is
    /// cancelled, even if the underlying lookup is still running; that
    /// lookup's answer is then discarded.
    func resolve(_ host: String) async throws -> [DirectResolvedAddress]
}

/// Connects TCP to one numeric address.
protocol DirectPublicDialing: Sendable {
    /// Connects to exactly `address` on `port`: no name resolution, no
    /// proxy. Returns promptly, with the connection closed, when its task is
    /// cancelled.
    func dial(_ address: DirectResolvedAddress, port: UInt16) async throws -> any DirectByteStream
}

/// Opens public-origin connections for allowed instance hosts.
struct DirectPublicBackend: Sendable {
    let resolver: any DirectHostResolving
    let dialer: any DirectPublicDialing
    /// Resolution, vetting, and the connection together.
    let connectSeconds: TimeInterval
    /// One address's dial, when another address follows it.
    let attemptSeconds: TimeInterval

    static let standardConnectSeconds: TimeInterval = 10
    /// The bound on each address but the last. Proposed, not measured.
    static let standardAttemptSeconds: TimeInterval = 3

    init(
        resolver: any DirectHostResolving = SystemHostResolver(),
        dialer: any DirectPublicDialing = NetworkPublicDialer(),
        connectSeconds: TimeInterval = DirectPublicBackend.standardConnectSeconds,
        attemptSeconds: TimeInterval = DirectPublicBackend.standardAttemptSeconds
    ) {
        self.resolver = resolver
        self.dialer = dialer
        self.connectSeconds = connectSeconds
        self.attemptSeconds = attemptSeconds
    }

    /// Resolves `authority`'s host, keeps only addresses the policy allows,
    /// and connects to the first that answers, on port 443. No refused
    /// address is ever dialled, and nothing is dialled once this task is
    /// cancelled or the bound has passed. The resolver's own work may
    /// outlive the bound (a system lookup cannot be cancelled); its late
    /// answer is not used.
    func open(_ authority: DirectAuthority) async throws -> any DirectByteStream {
        let resolver = resolver
        let dialer = dialer
        let attemptSeconds = attemptSeconds
        return try await directDeadline(connectSeconds, discard: { $0.cancel() }) {
            let addresses = try await resolver.resolve(authority.host)
            try Task.checkCancellation()
            let allowed = addresses.filter(DirectPublicAddressPolicy.allows)
            guard !allowed.isEmpty else {
                throw DirectPublicError.noPublicAddress
            }
            var lastError: Error = DirectPublicError.noPublicAddress
            for (index, address) in allowed.enumerated() {
                try Task.checkCancellation()
                // Every address but the last gets its own bound, so one that
                // never answers (a dial waits through refusals and missing
                // paths) leaves time for the next. The last has whatever is
                // left of the overall bound.
                let isLast = index == allowed.count - 1
                do {
                    let stream: any DirectByteStream
                    if isLast {
                        stream = try await dialer.dial(address, port: 443)
                    } else {
                        stream = try await directDeadline(
                            attemptSeconds,
                            discard: { $0.cancel() }
                        ) {
                            try await dialer.dial(address, port: 443)
                        }
                    }
                    if Task.isCancelled {
                        stream.cancel()
                        throw CancellationError()
                    }
                    return stream
                } catch is CancellationError {
                    throw CancellationError()
                } catch {
                    lastError = error
                }
            }
            throw lastError
        }
    }
}

/// The system resolver (`getaddrinfo`), on a thread of its own. A lookup
/// cannot be cancelled: a cancelled caller stops waiting at once, and the
/// lookup's answer is discarded when it comes.
struct SystemHostResolver: DirectHostResolving {
    func resolve(_ host: String) async throws -> [DirectResolvedAddress] {
        let answer = DirectOnceAnswer<[DirectResolvedAddress]>()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                answer.wait(with: continuation)
                DispatchQueue.global(qos: .userInitiated).async {
                    answer.resolve(with: Result { try Self.lookup(host) })
                }
            }
        } onCancel: {
            answer.resolve(with: .failure(CancellationError()))
        }
    }

    static func lookup(_ host: String) throws -> [DirectResolvedAddress] {
        var hints = addrinfo()
        hints.ai_family = AF_UNSPEC
        hints.ai_socktype = SOCK_STREAM
        hints.ai_protocol = IPPROTO_TCP
        hints.ai_flags = AI_ADDRCONFIG
        var result: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host, "443", &hints, &result) == 0, let first = result else {
            throw DirectPublicError.resolutionFailed
        }
        defer {
            freeaddrinfo(first)
        }
        var addresses: [DirectResolvedAddress] = []
        var cursor: UnsafeMutablePointer<addrinfo>? = first
        while let info = cursor {
            if let address = info.pointee.ai_addr {
                switch info.pointee.ai_family {
                case AF_INET:
                    let bytes = address.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { pointer in
                        var raw = pointer.pointee.sin_addr
                        return withUnsafeBytes(of: &raw) { Array($0) }
                    }
                    if let resolved = DirectResolvedAddress(ipv4: bytes) {
                        addresses.append(resolved)
                    }
                case AF_INET6:
                    let bytes = address.withMemoryRebound(to: sockaddr_in6.self, capacity: 1) { pointer in
                        var raw = pointer.pointee.sin6_addr
                        return withUnsafeBytes(of: &raw) { Array($0) }
                    }
                    if let resolved = DirectResolvedAddress(ipv6: bytes) {
                        addresses.append(resolved)
                    }
                default:
                    break
                }
            }
            cursor = info.pointee.ai_next
        }
        return addresses
    }
}

/// An `NWConnection` to one numeric address, with no proxy.
struct NetworkPublicDialer: DirectPublicDialing {
    /// How long a committed public relay may stay not viable before the App
    /// closes it (docs/plans/remote-direct-ios.md section 9). Proposed, not
    /// measured. Unlike a direct relay, a public relay is not closed by a
    /// path event, so this bounds one that silently stopped working.
    static let notViableSeconds: TimeInterval = 5

    func dial(_ address: DirectResolvedAddress, port: UInt16) async throws -> any DirectByteStream {
        let host: NWEndpoint.Host
        switch address.family {
        case .ipv4:
            guard let ipv4 = IPv4Address(Data(address.bytes)) else {
                throw DirectPublicError.invalidAddress
            }
            host = .ipv4(ipv4)
        case .ipv6:
            guard let ipv6 = IPv6Address(Data(address.bytes)) else {
                throw DirectPublicError.invalidAddress
            }
            host = .ipv6(ipv6)
        }
        guard let endpointPort = NWEndpoint.Port(rawValue: port) else {
            throw DirectPublicError.invalidAddress
        }
        let parameters = NWParameters.tcp
        // Never through a proxy: neither the App's own local proxy (an
        // `NWConnection` does not use WebKit's routes) nor a system one.
        parameters.preferNoProxies = true
        let stream = NWConnectionByteStream(
            connection: NWConnection(host: host, port: endpointPort, using: parameters),
            notViableSeconds: Self.notViableSeconds
        )
        try await stream.connect()
        return stream
    }
}

/// A `DirectByteStream` over an `NWConnection`.
final class NWConnectionByteStream: DirectByteStream, @unchecked Sendable {
    private let connection: NWConnection
    private let queue = DispatchQueue(label: "bot.avibe.ios.direct-stream")
    private let lock = NSLock()
    private var finished = false
    private let notViableSeconds: TimeInterval?
    /// Runs on `queue` only.
    private var notViableClose: DispatchWorkItem?

    /// With `notViableSeconds`, the ready connection is cancelled once it
    /// has stayed not viable that long; a read or write in progress then
    /// throws and the relay ends.
    init(connection: NWConnection, notViableSeconds: TimeInterval? = nil) {
        self.connection = connection
        self.notViableSeconds = notViableSeconds
    }

    /// How long a connection waiting for a path waits before it tries again.
    static let retrySeconds: TimeInterval = 0.25

    /// Starts the connection and returns once it is ready. A connection that
    /// is waiting (no usable path yet, or refused) keeps trying until it is
    /// ready, fails, or this task is cancelled, as the caller's deadline
    /// does. Throws when it fails or is cancelled; the connection is then
    /// closed.
    func connect() async throws {
        let connection = connection
        let queue = queue
        let answer = DirectOnceAnswer<Void>()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                answer.wait(with: continuation)
                connection.stateUpdateHandler = { state in
                    switch state {
                    case .ready:
                        answer.resolve(with: .success(()))
                    case .waiting:
                        // No usable path yet, or the address refused: not a
                        // failure. Try again shortly; the caller's deadline
                        // or cancellation ends the wait.
                        queue.asyncAfter(deadline: .now() + Self.retrySeconds) {
                            if case .waiting = connection.state {
                                connection.restart()
                            }
                        }
                    case .failed(let error):
                        connection.cancel()
                        answer.resolve(with: .failure(error))
                    case .cancelled:
                        answer.resolve(with: .failure(CancellationError()))
                    default:
                        break
                    }
                }
                if self.notViableSeconds != nil {
                    connection.viabilityUpdateHandler = { [weak self] isViable in
                        self?.viabilityChanged(isViable)
                    }
                }
                connection.start(queue: queue)
            }
        } onCancel: {
            connection.cancel()
            answer.resolve(with: .failure(CancellationError()))
        }
    }

    /// On `queue`.
    private func viabilityChanged(_ isViable: Bool) {
        notViableClose?.cancel()
        notViableClose = nil
        guard !isViable, let notViableSeconds else {
            return
        }
        let connection = connection
        let close = DispatchWorkItem {
            connection.cancel()
        }
        notViableClose = close
        queue.asyncAfter(deadline: .now() + notViableSeconds, execute: close)
    }

    func read(maxLength: Int) async throws -> Data? {
        if lock.withLock({ finished }) {
            return nil
        }
        let chunk = try await connection.receiveChunk(maximumLength: maxLength)
        if chunk.isComplete {
            lock.withLock { finished = true }
            return chunk.data.isEmpty ? nil : chunk.data
        }
        return chunk.data
    }

    func write(_ data: Data) async throws {
        try await connection.sendAll(data)
    }

    func closeWrite() async throws {
        try await connection.finishSending()
    }

    func cancel() {
        connection.cancel()
        queue.async { [weak self] in
            self?.notViableClose?.cancel()
            self?.notViableClose = nil
        }
    }
}

/// A continuation resumed exactly once, by whichever comes first: the
/// answer, or a cancellation that arrived before or after the wait began.
final class DirectOnceAnswer<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Value, Error>?
    private var early: Result<Value, Error>?
    private var done = false

    func wait(with continuation: CheckedContinuation<Value, Error>) {
        let ready: Result<Value, Error>? = lock.withLock {
            if let early {
                done = true
                return early
            }
            self.continuation = continuation
            return nil
        }
        if let ready {
            continuation.resume(with: ready)
        }
    }

    func resolve(with result: Result<Value, Error>) {
        resolveFirst { result }
    }

    /// Resolves with what `decide` returns, if nothing has resolved this
    /// answer yet, and says whether it did. `decide` runs under this
    /// answer's lock, and only for the resolver that wins: a state change it
    /// makes and the answer it gives are one decision, so no other resolver
    /// can win the answer after that change, or make its own change after
    /// losing. `decide` must not wait or resolve this answer.
    @discardableResult
    func resolveFirst(_ decide: () -> Result<Value, Error>) -> Bool {
        var waiting: CheckedContinuation<Value, Error>?
        var answer: Result<Value, Error>?
        let won: Bool = lock.withLock {
            guard !done, early == nil else {
                return false
            }
            let result = decide()
            if let continuation {
                done = true
                self.continuation = nil
                waiting = continuation
                answer = result
            } else {
                early = result
            }
            return true
        }
        if let waiting, let answer {
            waiting.resume(with: answer)
        }
        return won
    }
}
