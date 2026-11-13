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

    /// Binds an endpoint for `secretKey` with the minimal preset: no relay,
    /// no discovery, nothing accepted. This is the first UDP socket.
    func bind(secretKey: Data) async throws -> any DirectIrohEndpoint
}

protocol DirectIrohEndpoint: AnyObject, Sendable {
    /// Dials `endpointID` at exactly `addresses`, offering `alpn`.
    func connect(
        to endpointID: DirectEndpointID,
        addresses: [String],
        alpn: Data
    ) async throws -> any DirectIrohConnection

    /// Closes the endpoint, its sockets, and everything still running on
    /// it, a dial in progress included.
    func close() async
}

protocol DirectIrohConnection: AnyObject, Sendable {
    /// The identity the peer proved in the handshake.
    var remoteEndpointID: DirectEndpointID? { get }

    func openStream() async throws -> DirectIrohStreamHalves

    /// Returns once the connection has closed, for any reason.
    func closed() async

    /// Closes the connection and every stream on it, at once.
    func close()
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
}

/// The system's view of the network path. A change is only a hint: the
/// transport validates its path before it reports anything lost.
@MainActor
final class DirectIrohPathMonitor: DirectIrohNetworkMonitoring {
    private var monitor: NWPathMonitor?
    private var hasBaseline = false
    private var onChange: (@MainActor () -> Void)?

    func start(onChange: @escaping @MainActor () -> Void) {
        stop()
        self.onChange = onChange
        let monitor = NWPathMonitor()
        self.monitor = monitor
        monitor.pathUpdateHandler = { [weak self, weak monitor] _ in
            MainActor.assumeIsolated {
                guard let self, let monitor, self.monitor === monitor else {
                    return
                }
                // The first update describes the path as it is, not a change.
                guard self.hasBaseline else {
                    self.hasBaseline = true
                    return
                }
                self.onChange?()
            }
        }
        monitor.start(queue: .main)
    }

    func stop() {
        monitor?.cancel()
        monitor = nil
        hasBaseline = false
        onChange = nil
    }
}
