import CryptoKit
import Foundation
import Network

// The direct transport's view of iroh, and the helpers that make its calls
// safe to cancel. The App links no iroh binary yet, so nothing implements
// `DirectIrohEngine` here: the implementation over the generated binding is
// added with the first build that links the gated binary.
// Design: docs/plans/remote-direct-ios-transport.md.

enum DirectIrohError: Error, Equatable {
    /// A bound passed before the operation finished.
    case timedOut
    /// The stream was cancelled, or an operation on it was abandoned.
    case cancelled
    /// A write after `closeWrite`: the sending half takes no more bytes.
    case closed
    /// A second read, write, or `closeWrite` while one is in progress.
    case busy
    /// The tags a stream was asked under are no longer current, or no path
    /// is usable for them.
    case stale
    /// The peer's identity is not the one the rendezvous answer named.
    case wrongPeer
    /// The instance did not answer the probe's CONNECT with `200`.
    case probeRefused
    /// The selected host is not one a request head may carry.
    case invalidHost
    /// The binding could not make a launch key.
    case noLaunchKey
}

// MARK: The subset of the generated binding the transport uses

// Every method below mirrors one call of the generated UniFFI binding
// (iroh-ffi v1.2.0). Two properties of that binding shape everything built
// on these protocols:
//
// - None of its async calls honours Swift task cancellation. A cancelled
//   caller keeps waiting until the Rust future completes.
// - Each stream half is a lock held across its own awaits: `reset` waits
//   behind a `writeAll` in progress, and `stop` behind a `read` in progress.
//
// `directIrohCall` and `DirectIrohByteStream` are how callers stay prompt
// anyway, and `directIrohCall` is also the one place where a network action
// of the transport starts.

/// This launch's endpoint key: 32 secret bytes, kept in memory only, and
/// the endpoint ID that is their public half.
struct DirectIrohLaunchKey: Sendable {
    let secret: Data
    let endpointID: DirectEndpointID
}

protocol DirectIrohEngine: Sendable {
    /// A new key. Opens no socket and starts no endpoint.
    func makeLaunchKey() throws -> DirectIrohLaunchKey

    /// Binds an endpoint for `secretKey` with the minimal preset: no
    /// discovery or address lookup, nothing accepted. `relayURLs` is its
    /// relay map; empty means no relay at all. This is the first UDP socket.
    func bind(secretKey: Data, relayURLs: [String]) async throws -> any DirectIrohEndpoint
}

protocol DirectIrohEndpoint: AnyObject, Sendable {
    /// Dials `endpointID` at exactly `addresses` and, if set, through
    /// `relayURL`, offering `alpn`. The address is built from these alone.
    func connect(
        to endpointID: DirectEndpointID,
        addresses: [String],
        relayURL: String?,
        alpn: Data
    ) async throws -> any DirectIrohConnection

    /// Closes the endpoint, its sockets, and everything still running on
    /// it, a dial in progress included.
    func close() async

    /// This endpoint's own candidate addresses now, by family.
    var localFamilies: DirectAddressFamilies? { get }
}

extension DirectIrohEndpoint {
    var localFamilies: DirectAddressFamilies? {
        nil
    }
}

protocol DirectIrohConnection: AnyObject, Sendable {
    /// The identity the peer proved in the handshake.
    var remoteEndpointID: DirectEndpointID? { get }

    func openStream() async throws -> DirectIrohStreamHalves

    /// Returns once the connection has closed, for any reason.
    func closed() async

    /// Closes the connection and every stream on it, at once.
    func close()

    /// The path selected for application data now, if the binding can
    /// tell.
    var selectedPath: DirectPathKind? { get }

    /// The selected path's kind, class, and round-trip time, if the binding
    /// can tell.
    var selectedPathDetail: DirectSelectedPath? { get }

    /// Calls `onChange` with the selected path whenever the connection's
    /// paths change. Nil when the binding cannot watch.
    func watchSelectedPath(
        _ onChange: @escaping @Sendable (DirectPathKind?) -> Void
    ) -> (any DirectPathWatch)?
}

/// A running watch; `stop` ends it.
protocol DirectPathWatch: Sendable {
    func stop()
}

extension DirectIrohConnection {
    var selectedPath: DirectPathKind? {
        nil
    }

    var selectedPathDetail: DirectSelectedPath? {
        selectedPath.map { DirectSelectedPath(kind: $0, local: nil, rttMs: nil) }
    }

    /// Calls `onChange` with the selected path whenever the connection's
    /// paths change. Nil when the binding cannot watch.
    func watchSelectedPath(
        _ onChange: @escaping @Sendable (DirectPathKind?) -> Void
    ) -> (any DirectPathWatch)? {
        nil
    }
}

struct DirectIrohStreamHalves: Sendable {
    let send: any DirectIrohSendHalf
    let receive: any DirectIrohReceiveHalf
}

protocol DirectIrohSendHalf: AnyObject, Sendable {
    /// Returns once the transport has accepted all of `data`.
    func writeAll(_ data: Data) async throws
    /// No more data follows. What was written is still delivered.
    func finish() async throws
    /// Abandons the half: what was not delivered yet is dropped.
    func reset() async
}

protocol DirectIrohReceiveHalf: AnyObject, Sendable {
    /// The next bytes, at most `sizeLimit`. Empty once the peer finished.
    func read(sizeLimit: Int) async throws -> Data
    /// Tells the peer to stop sending.
    func stop() async
}

// MARK: Calls that cannot be cancelled

/// One call of an operation that may ignore task cancellation.
///
/// The operation runs in a task of its own, so its caller can leave at
/// once: `wait` gives the caller the result, or an error as soon as the
/// caller's task is cancelled or `fail` is called. Exactly one outcome
/// reaches the caller. A value the operation produces after the caller got
/// an error goes to `discard`; an error it produces then goes nowhere.
///
/// **The start gate.** That task does not run at once, and by the time it
/// does its caller may be gone. `Start.whileAwaited` makes the call's own
/// lock decide which came first: the task asks `enter()` as the last thing
/// before the operation, and `enter()` succeeds only if the caller has not
/// left. So either the caller left first and the operation never starts,
/// or the operation started first and may end late, its value discarded.
/// A caller leaves only by its wait ending, whatever the reason: a result,
/// an error, a deadline that cancels the waiting task, or the cancellation
/// of an owner. There is no list of reasons to keep complete.
final class DirectIrohCall<Value: Sendable>: @unchecked Sendable {
    /// When the operation may start.
    enum Start {
        /// Always, even if the caller has left: for a caller whose own
        /// state depends on the operation settling it (a stream's half).
        case always
        /// Only while the caller has not left, and only if `allowed` says
        /// so at that moment. `allowed` is asked first; the gate is the
        /// last thing before the operation, with no await between them.
        case whileAwaited(allowed: @Sendable () -> Bool)
    }

    private enum State {
        /// No result yet and no waiting caller: before `wait`, or the
        /// operation has not finished.
        case running
        /// The result arrived before the caller asked for it.
        case finished(Result<Value, Error>)
        case waiting(CheckedContinuation<Value, Error>)
        /// `fail` was called before the caller asked.
        case failed(Error)
        /// The caller has its outcome.
        case done
    }

    private let lock = NSLock()
    private var state = State.running
    private var discard: @Sendable (Value) -> Void = { _ in }

    /// Hands `operation` to its own task. Call it once. It never changes
    /// what `fail` already decided.
    func begin(
        _ start: Start,
        discard: @escaping @Sendable (Value) -> Void = { _ in },
        _ operation: @escaping @Sendable () async throws -> Value
    ) {
        lock.withLock {
            self.discard = discard
        }
        Task {
            if case .whileAwaited(let allowed) = start {
                guard allowed() else {
                    self.settle(.failure(DirectIrohError.stale))
                    return
                }
                guard self.enter() else {
                    // The caller left first: nothing starts.
                    return
                }
            }
            let result: Result<Value, Error>
            do {
                result = .success(try await operation())
            } catch {
                result = .failure(error)
            }
            self.settle(result)
        }
    }

    /// The gate: `true` unless the caller already has an error. Decided
    /// under the same lock as `fail`, so one of the two comes first.
    private func enter() -> Bool {
        lock.withLock {
            switch state {
            case .running, .waiting:
                return true
            case .finished, .failed, .done:
                return false
            }
        }
    }

    /// The operation's result, or the error of whichever came first: `fail`,
    /// or the cancellation of the calling task. Call it once.
    func wait() async throws -> Value {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Value, Error>) in
                let ready: Result<Value, Error>? = self.lock.withLock {
                    switch self.state {
                    case .running:
                        self.state = .waiting(continuation)
                        return nil
                    case .finished(let result):
                        self.state = .done
                        return result
                    case .failed(let error):
                        self.state = .done
                        return .failure(error)
                    case .waiting, .done:
                        return .failure(DirectIrohError.cancelled)
                    }
                }
                if let ready {
                    continuation.resume(with: ready)
                }
            }
        } onCancel: {
            self.fail(CancellationError())
        }
    }

    /// Ends the caller's wait with `error`, unless the caller already has
    /// its outcome, and closes the gate. An operation that has started is
    /// not touched.
    func fail(_ error: Error) {
        let effect: Effect = lock.withLock {
            switch state {
            case .running:
                state = .failed(error)
                return .none
            case .finished(let result):
                // The result was there, and nobody will take it now.
                state = .failed(error)
                return .discard(result)
            case .waiting(let waiting):
                state = .done
                return .resume(waiting, .failure(error))
            case .failed, .done:
                return .none
            }
        }
        perform(effect)
    }

    private enum Effect {
        case none
        case resume(CheckedContinuation<Value, Error>, Result<Value, Error>)
        case discard(Result<Value, Error>)
    }

    private func settle(_ result: Result<Value, Error>) {
        let effect: Effect = lock.withLock {
            switch state {
            case .running:
                state = .finished(result)
                return .none
            case .waiting(let waiting):
                state = .done
                return .resume(waiting, result)
            case .finished, .failed, .done:
                // The caller already left: the result came too late.
                return .discard(result)
            }
        }
        perform(effect)
    }

    private func perform(_ effect: Effect) {
        switch effect {
        case .none:
            break
        case .resume(let continuation, let result):
            continuation.resume(with: result)
        case .discard(let result):
            if case .success(let value) = result {
                let discard = lock.withLock { self.discard }
                discard(value)
            }
        }
    }
}

/// Starts one network action of the binding, which may ignore task
/// cancellation, so that the caller still returns as soon as its own task
/// is cancelled.
///
/// Every bind, connect, probe action, and stream open goes through here,
/// and through nothing else. The action starts only if, in the step that
/// starts it:
///
/// - `allowed` says so. It has no default: each call names what gives it
///   the right to act, a lease's deadline or a path's authority, read at
///   that moment on whatever thread that is.
/// - The caller has not left. The cancellation handler is in place before
///   the action's task is scheduled and stays until the wait ends, so a
///   caller that is cancelled at any moment, before the task runs
///   included, closes the gate first. A deadline is such a cancellation,
///   and so is everything that ends an owner.
///
/// What started may still end late: its value goes to `discard`.
func directIrohCall<Value: Sendable>(
    allowed: @escaping @Sendable () -> Bool,
    discard: @escaping @Sendable (Value) -> Void = { _ in },
    _ operation: @escaping @Sendable () async throws -> Value
) async throws -> Value {
    let call = DirectIrohCall<Value>()
    return try await withTaskCancellationHandler {
        call.begin(.whileAwaited(allowed: allowed), discard: discard, operation)
        return try await call.wait()
    } onCancel: {
        call.fail(CancellationError())
    }
}

/// Runs `operation` with a deadline. The operation must end promptly when
/// its task is cancelled (wrap binding calls in `directIrohCall`); the call
/// returns once it has.
///
/// The deadline and the operation can finish in the same instant. If the
/// deadline is taken first, a value the operation still returns goes to
/// `discard`, so what it holds is closed and not dropped. Exactly one of
/// the two happens to a value: it is returned, or it is discarded.
func directIrohDeadline<Value: Sendable>(
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
                try await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
            } catch {
                // Cancelled: the operation finished first.
                return nil
            }
            return .failure(DirectIrohError.timedOut)
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
        return first ?? .failure(DirectIrohError.timedOut)
    }
    return try outcome.get()
}

// MARK: The right to start something on the network

/// A lease's deadline with the clock it is read on: what gives a dial or a
/// probe the right to start its next network action. It is a value. It is
/// fixed when the dial or the probe starts and has no state to revoke or
/// renew: an old dial or probe never borrows a newer answer's time, and
/// what ends it early is its caller leaving (see `directIrohCall`).
struct DirectIrohLeaseDeadline: Sendable {
    let deadline: TimeInterval
    let clock: @Sendable () -> TimeInterval

    /// Whether a new network action may start at this moment.
    var allowsStart: Bool {
        clock() < deadline
    }
}

/// The right to open streams on a live path.
///
/// A stream's open is asked for by a caller that does not own the path, so
/// the path can end, or stop being usable, while that caller still waits.
/// This is the one authority with state: the lease's deadline, renewed by
/// a newly accepted answer of the same generation; how many times the path
/// stopped being usable; and whether the path has ended. The transport is
/// its only writer. An open asks it in the step that starts it, with the
/// suspension count of its admission: it is refused once the path was
/// suspended again, even if the path is usable again by then.
final class DirectIrohAuthority: @unchecked Sendable {
    private let lock = NSLock()
    private let clock: @Sendable () -> TimeInterval
    private var deadline: TimeInterval
    private var isRevoked = false
    private var suspensions = 0

    init(deadline: TimeInterval, clock: @escaping @Sendable () -> TimeInterval) {
        self.deadline = deadline
        self.clock = clock
    }

    /// Whether an open admitted when the path had been suspended
    /// `admittedSuspensions` times may start at this moment.
    func allowsStart(admittedAt admittedSuspensions: Int) -> Bool {
        let now = clock()
        return lock.withLock {
            !isRevoked && now < deadline && suspensions == admittedSuspensions
        }
    }

    /// How many times the path stopped being usable.
    var suspensionCount: Int {
        lock.withLock { suspensions }
    }

    /// The path has ended. Final.
    func revoke() {
        lock.withLock {
            isRevoked = true
        }
    }

    /// The path stopped being usable: nothing admitted before starts.
    func suspend() {
        lock.withLock {
            suspensions += 1
        }
    }

    /// A newly accepted answer for the same path.
    func renew(deadline: TimeInterval) {
        lock.withLock {
            self.deadline = deadline
        }
    }
}

// MARK: Time and the network

/// Proposed bounds for the transport. None of them is a measurement
/// (docs/plans/remote-direct-ios.md, cell Q7).
struct DirectIrohTiming: Sendable {
    /// One rendezvous request, the page's own five seconds included.
    var fetchSeconds: TimeInterval = 8
    var bindSeconds: TimeInterval = 2
    /// The handshake with the instance's endpoint, at every admitted
    /// address at once.
    var dialSeconds: TimeInterval = 1.5
    /// The same through a relay of the public-relay owner trial: DNS, TCP,
    /// TLS and the relay handshake come first, then QUIC over two legs.
    /// To be measured in the trial.
    var relayDialSeconds: TimeInterval = 5
    /// One stream opened and its CONNECT answered.
    var probeSeconds: TimeInterval = 1
    /// How soon to ask again while the runtime's page is still loading,
    /// and how often before that counts as a failure.
    var pageRetrySeconds: TimeInterval = 0.5
    var pageRetryLimit = 40
    /// The wait before the next attempt, by consecutive failures. The first
    /// is short: an admission notice can lose the race with a fast dial.
    var retryLadder: [TimeInterval] = [0.25, 1, 5, 15, 60, 300]
    /// A path that stays usable this long clears the failure count.
    var stableSeconds: TimeInterval = 30
    /// The share of an answer's lifetime after which it is asked again, so
    /// the admission is extended before it ends.
    var refreshFraction = 0.8
    /// How soon a failed refresh is tried again while the path stays up,
    /// and the least time between an accepted answer and the next request.
    var refreshRetrySeconds: TimeInterval = 15
    /// An answer with less than this left of its lifetime is not used.
    var minimumLifetimeSeconds: TimeInterval = 30
    /// The quiet time after a network change before the path is probed.
    var networkDebounceSeconds: TimeInterval = 0.5
    /// The least time between two probes asked for by failed candidates.
    var failureProbeSpacingSeconds: TimeInterval = 1

    static let standard = DirectIrohTiming()
}

protocol DirectIrohTimer: AnyObject {
    @MainActor func cancel()
}

protocol DirectIrohScheduling: AnyObject {
    /// Runs `action` on the main actor after `seconds`, unless the timer is
    /// cancelled first.
    @MainActor
    func schedule(
        after seconds: TimeInterval,
        _ action: @escaping @MainActor () -> Void
    ) -> any DirectIrohTimer
}

final class DirectIrohTaskScheduler: DirectIrohScheduling {
    private final class TaskTimer: DirectIrohTimer {
        private let task: Task<Void, Never>

        init(task: Task<Void, Never>) {
            self.task = task
        }

        @MainActor
        func cancel() {
            task.cancel()
        }
    }

    @MainActor
    func schedule(
        after seconds: TimeInterval,
        _ action: @escaping @MainActor () -> Void
    ) -> any DirectIrohTimer {
        TaskTimer(
            task: Task { @MainActor in
                do {
                    try await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
                } catch {
                    return
                }
                guard !Task.isCancelled else {
                    return
                }
                action()
            }
        )
    }
}

protocol DirectIrohNetworkMonitoring: AnyObject {
    /// Starts watching. `onChange` runs each time the network path changes
    /// after the first reading. Starting again replaces the watch.
    @MainActor func start(onChange: @escaping @MainActor () -> Void)
    @MainActor func stop()
    /// How many updates of the system's path, since this monitor was made,
    /// changed nothing the transport depends on and were not reported.
    @MainActor var ignoredUpdates: Int { get }
}

extension DirectIrohNetworkMonitoring {
    @MainActor var ignoredUpdates: Int {
        0
    }
}

/// One address of one network interface, read to be hashed and then
/// dropped: it is never kept, logged, or sent anywhere.
struct DirectIrohInterfaceAddress: Hashable, Sendable {
    enum Family: UInt8, Sendable {
        case ipv4 = 4
        case ipv6 = 6
    }

    let interface: String
    let family: Family
    let bytes: [UInt8]

    /// An interface a VPN or another tunnel runs on.
    static func isTunnel(_ interface: String) -> Bool {
        ["utun", "ipsec", "ppp", "tun", "tap"].contains { interface.hasPrefix($0) }
    }

    /// IPv4 169.254/16 or IPv6 fe80::/10. The system keeps tunnel
    /// interfaces of its own that carry only such addresses.
    var isLinkLocal: Bool {
        switch family {
        case .ipv4:
            return bytes.count == 4 && bytes[0] == 169 && bytes[1] == 254
        case .ipv6:
            return bytes.count == 16 && bytes[0] == 0xFE && bytes[1] & 0xC0 == 0x80
        }
    }

    /// Every IPv4 and IPv6 address of every interface that is up, from
    /// `getifaddrs`. Empty if the system does not answer.
    static func system() -> [DirectIrohInterfaceAddress] {
        var first: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&first) == 0, let first else {
            return []
        }
        defer {
            freeifaddrs(first)
        }
        var found: [DirectIrohInterfaceAddress] = []
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let entry = cursor {
            cursor = entry.pointee.ifa_next
            guard
                Int32(entry.pointee.ifa_flags) & IFF_UP != 0,
                let address = entry.pointee.ifa_addr
            else {
                continue
            }
            let name = String(cString: entry.pointee.ifa_name)
            switch Int32(address.pointee.sa_family) {
            case AF_INET:
                let bytes = address.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { pointer in
                    var raw = pointer.pointee.sin_addr
                    return withUnsafeBytes(of: &raw) { Array($0) }
                }
                found.append(DirectIrohInterfaceAddress(interface: name, family: .ipv4, bytes: bytes))
            case AF_INET6:
                let bytes = address.withMemoryRebound(to: sockaddr_in6.self, capacity: 1) { pointer in
                    var raw = pointer.pointee.sin6_addr
                    return withUnsafeBytes(of: &raw) { Array($0) }
                }
                found.append(DirectIrohInterfaceAddress(interface: name, family: .ipv6, bytes: bytes))
            default:
                break
            }
        }
        return found
    }
}

/// What of the system's network path decides whether the direct path has
/// to be checked again. Two readings that are equal here are the same
/// network to the transport, whatever else the system reported with them.
///
/// Compared: whether there is a path at all; the interfaces it can use, by
/// name and kind, in the system's order of preference (Wi-Fi to cellular
/// and back, a VPN that takes the default route); whether it carries IPv4,
/// IPv6 and DNS; its gateways; and the addresses of those interfaces and of
/// every tunnel interface that has a routable address (an address added or
/// removed on the same interface, a VPN turned on or off). Gateways and
/// addresses are kept only as SHA-256 digests.
///
/// Not compared: whether the path is expensive or constrained. Neither
/// changes what this App's own connections can reach; when one comes with
/// another interface or address, that is what is seen.
struct DirectIrohNetworkSnapshot: Equatable, Sendable {
    enum Status: Equatable, Sendable {
        case satisfied
        case unsatisfied
        case requiresConnection
    }

    enum InterfaceKind: Equatable, Sendable {
        case wifi
        case cellular
        case wired
        case loopback
        case other
    }

    struct Interface: Equatable, Sendable {
        let name: String
        let kind: InterfaceKind
    }

    let status: Status
    let interfaces: [Interface]
    let supportsIPv4: Bool
    let supportsIPv6: Bool
    let supportsDNS: Bool
    let gatewayDigest: Data
    let addressDigest: Data

    /// `gateways` and `addresses` are hashed here and not kept.
    init(
        status: Status,
        interfaces: [Interface],
        supportsIPv4: Bool,
        supportsIPv6: Bool,
        supportsDNS: Bool,
        gateways: [String],
        addresses: [DirectIrohInterfaceAddress]
    ) {
        self.status = status
        self.interfaces = interfaces
        self.supportsIPv4 = supportsIPv4
        self.supportsIPv6 = supportsIPv6
        self.supportsDNS = supportsDNS
        gatewayDigest = Self.digest(gateways.sorted().map { gateway -> [UInt8] in Array(gateway.utf8) })
        let records = Self.relevant(addresses, pathInterfaces: Set(interfaces.map(\.name)))
            .map { address -> [UInt8] in
                var record = Array(address.interface.utf8)
                record.append(0)
                record.append(address.family.rawValue)
                record.append(contentsOf: address.bytes)
                return record
            }
        addressDigest = Self.digest(records.sorted { $0.lexicographicallyPrecedes($1) })
    }

    /// The addresses that count: all of the path's own interfaces, and the
    /// routable ones of tunnel interfaces. A tunnel interface with only
    /// link-local addresses is the system's own, not a VPN.
    static func relevant(
        _ addresses: [DirectIrohInterfaceAddress],
        pathInterfaces: Set<String>
    ) -> [DirectIrohInterfaceAddress] {
        Array(Set(addresses.filter { address in
            pathInterfaces.contains(address.interface)
                || (DirectIrohInterfaceAddress.isTunnel(address.interface) && !address.isLinkLocal)
        }))
    }

    private static func digest(_ records: [[UInt8]]) -> Data {
        var hasher = SHA256()
        for record in records {
            // Length first, so no two lists of records hash alike.
            withUnsafeBytes(of: UInt32(record.count).bigEndian) { hasher.update(bufferPointer: $0) }
            hasher.update(data: record)
        }
        return Data(hasher.finalize())
    }
}

extension DirectIrohNetworkSnapshot {
    /// The system's path, with the interface addresses read beside it.
    init(path: NWPath, addresses: [DirectIrohInterfaceAddress]) {
        let status: Status
        switch path.status {
        case .satisfied:
            status = .satisfied
        case .requiresConnection:
            status = .requiresConnection
        default:
            status = .unsatisfied
        }
        self.init(
            status: status,
            interfaces: path.availableInterfaces.map { interface in
                let kind: InterfaceKind
                switch interface.type {
                case .wifi:
                    kind = .wifi
                case .cellular:
                    kind = .cellular
                case .wiredEthernet:
                    kind = .wired
                case .loopback:
                    kind = .loopback
                default:
                    kind = .other
                }
                return Interface(name: interface.name, kind: kind)
            },
            supportsIPv4: path.supportsIPv4,
            supportsIPv6: path.supportsIPv6,
            supportsDNS: path.supportsDNS,
            gateways: path.gateways.map { String(describing: $0) },
            addresses: addresses
        )
    }
}

/// Which readings of the system's path are changes. The first describes the
/// path as it is; a later one is a change only if it differs from the one
/// before it.
struct DirectIrohNetworkChangeFilter {
    enum Verdict: Equatable {
        case baseline
        case unchanged
        case changed
    }

    private var last: DirectIrohNetworkSnapshot?

    mutating func observe(_ snapshot: DirectIrohNetworkSnapshot) -> Verdict {
        guard let last else {
            self.last = snapshot
            return .baseline
        }
        guard snapshot != last else {
            return .unchanged
        }
        self.last = snapshot
        return .changed
    }

    /// The next reading is a baseline again.
    mutating func reset() {
        last = nil
    }
}

/// The system's view of the network path. A change is only a hint: the
/// transport validates its path before it reports anything lost. An update
/// that changes nothing in `DirectIrohNetworkSnapshot` is not a change: the
/// system sends such updates on its own, seconds apart on some networks, and
/// each one reported would suspend a working path to probe it again.
@MainActor
final class DirectIrohPathMonitor: DirectIrohNetworkMonitoring {
    private var monitor: NWPathMonitor?
    private var filter = DirectIrohNetworkChangeFilter()
    private var onChange: (@MainActor () -> Void)?
    private let readAddresses: @Sendable () -> [DirectIrohInterfaceAddress]
    private(set) var ignoredUpdates = 0

    init(
        readAddresses: @escaping @Sendable () -> [DirectIrohInterfaceAddress] = {
            DirectIrohInterfaceAddress.system()
        }
    ) {
        self.readAddresses = readAddresses
    }

    func start(onChange: @escaping @MainActor () -> Void) {
        stop()
        self.onChange = onChange
        let monitor = NWPathMonitor()
        self.monitor = monitor
        let readAddresses = readAddresses
        monitor.pathUpdateHandler = { [weak self, weak monitor] path in
            // Read where the update arrives, so the addresses are the ones
            // that came with it. Only their digest leaves this closure.
            let snapshot = DirectIrohNetworkSnapshot(path: path, addresses: readAddresses())
            MainActor.assumeIsolated {
                guard let self, let monitor, self.monitor === monitor else {
                    return
                }
                switch self.filter.observe(snapshot) {
                case .baseline:
                    break
                case .unchanged:
                    self.ignoredUpdates += 1
                case .changed:
                    self.onChange?()
                }
            }
        }
        monitor.start(queue: .main)
    }

    func stop() {
        monitor?.cancel()
        monitor = nil
        filter.reset()
        onChange = nil
    }
}
