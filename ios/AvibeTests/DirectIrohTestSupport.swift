import Foundation
import XCTest
@testable import Avibe

// Stand-ins for the iroh binding. Like the binding, none of their async
// calls honours task cancellation: a call waits until the test resolves it.

enum DirectIrohTestError: Error, Equatable {
    case released
    case scripted
    case timedOut
}

/// A value a test supplies later. Waiting ignores task cancellation.
final class DirectIrohPending<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var result: Result<Value, Error>?
    private var waiters: [CheckedContinuation<Value, Error>] = []

    deinit {
        for waiter in waiters {
            waiter.resume(throwing: DirectIrohTestError.released)
        }
    }

    func wait() async throws -> Value {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Value, Error>) in
            let ready: Result<Value, Error>? = self.lock.withLock {
                if let result = self.result {
                    return result
                }
                self.waiters.append(continuation)
                return nil
            }
            if let ready {
                continuation.resume(with: ready)
            }
        }
    }

    func resolve(_ value: Value) {
        finish(.success(value))
    }

    func fail(_ error: Error = DirectIrohTestError.scripted) {
        finish(.failure(error))
    }

    private func finish(_ result: Result<Value, Error>) {
        let waiting: [CheckedContinuation<Value, Error>] = lock.withLock {
            guard self.result == nil else {
                return []
            }
            self.result = result
            let current = waiters
            waiters = []
            return current
        }
        for waiter in waiting {
            waiter.resume(with: result)
        }
    }
}

/// What the stand-ins were asked to do, in order.
final class DirectIrohEventLog: @unchecked Sendable {
    private let lock = NSLock()
    private var events: [String] = []

    func add(_ event: String) {
        lock.withLock {
            events.append(event)
        }
    }

    var all: [String] {
        lock.withLock { events }
    }

    func count(of event: String) -> Int {
        lock.withLock { events.filter { $0 == event }.count }
    }
}

final class DirectIrohFakeSendHalf: DirectIrohSendHalf, @unchecked Sendable {
    private let log: DirectIrohEventLog
    private let lock = NSLock()
    private var storedWritten = Data()
    private var storedGate: DirectIrohPending<Void>?
    private var storedFinishGate: DirectIrohPending<Void>?

    init(log: DirectIrohEventLog) {
        self.log = log
    }

    var written: Data {
        lock.withLock { storedWritten }
    }

    /// Makes the next writes wait, as a transport that is not accepting.
    func holdWrites() -> DirectIrohPending<Void> {
        let gate = DirectIrohPending<Void>()
        lock.withLock {
            storedGate = gate
        }
        return gate
    }

    func writeAll(_ data: Data) async throws {
        log.add("write")
        let gate = lock.withLock { storedGate }
        if let gate {
            try await gate.wait()
        }
        lock.withLock {
            storedWritten.append(data)
        }
    }

    /// Makes `finish` wait, as a binding call that has not completed.
    func holdFinish() -> DirectIrohPending<Void> {
        let gate = DirectIrohPending<Void>()
        lock.withLock {
            storedFinishGate = gate
        }
        return gate
    }

    func finish() async throws {
        log.add("finish")
        let gate = lock.withLock { storedFinishGate }
        if let gate {
            try await gate.wait()
        }
    }

    func reset() async {
        log.add("reset")
    }
}

final class DirectIrohFakeReceiveHalf: DirectIrohReceiveHalf, @unchecked Sendable {
    private let log: DirectIrohEventLog
    private let lock = NSLock()
    private var reads: [DirectIrohPending<Data>] = []
    private var served: [DirectIrohPending<Data>] = []
    private var storedLimits: [Int] = []

    init(log: DirectIrohEventLog) {
        self.log = log
    }

    /// The size limits the reads asked for.
    var limits: [Int] {
        lock.withLock { storedLimits }
    }

    /// What the next read that has no answer yet returns. Empty is the end.
    func enqueue(_ data: Data) {
        let read = DirectIrohPending<Data>()
        read.resolve(data)
        lock.withLock {
            reads.append(read)
        }
    }

    /// A read the test answers later.
    func enqueuePending() -> DirectIrohPending<Data> {
        let read = DirectIrohPending<Data>()
        lock.withLock {
            reads.append(read)
        }
        return read
    }

    func read(sizeLimit: Int) async throws -> Data {
        log.add("read")
        let pending: DirectIrohPending<Data> = lock.withLock {
            storedLimits.append(sizeLimit)
            // With nothing scripted the read waits, as on a quiet stream.
            let read = reads.isEmpty ? DirectIrohPending<Data>() : reads.removeFirst()
            served.append(read)
            return read
        }
        return try await pending.wait()
    }

    func stop() async {
        log.add("stop")
    }
}

struct DirectIrohFakeStream {
    let send: DirectIrohFakeSendHalf
    let receive: DirectIrohFakeReceiveHalf

    init(log: DirectIrohEventLog) {
        send = DirectIrohFakeSendHalf(log: log)
        receive = DirectIrohFakeReceiveHalf(log: log)
    }

    var halves: DirectIrohStreamHalves {
        DirectIrohStreamHalves(send: send, receive: receive)
    }
}

final class DirectIrohFakeConnection: DirectIrohConnection, @unchecked Sendable {
    static let established = Data("HTTP/1.1 200 Connection Established\r\n\r\n".utf8)
    static let refused = Data("HTTP/1.1 503 Service Unavailable\r\n\r\n".utf8)

    private let log: DirectIrohEventLog
    private let lock = NSLock()
    private let closedSignal = DirectIrohPending<Void>()
    private let remote: DirectEndpointID?
    private var answers: [Data]
    private var storedStreams: [DirectIrohFakeStream] = []
    private var storedGate: DirectIrohPending<Void>?

    /// `answers` are what the first read of each new stream returns, in
    /// order: the instance's answer to a probe. A stream beyond them stays
    /// quiet.
    init(log: DirectIrohEventLog, remote: DirectEndpointID?, answers: [Data]) {
        self.log = log
        self.remote = remote
        self.answers = answers
    }

    var remoteEndpointID: DirectEndpointID? {
        remote
    }

    var streams: [DirectIrohFakeStream] {
        lock.withLock { storedStreams }
    }

    /// Makes the next open wait, as a peer that grants no stream yet.
    func holdNextOpen() -> DirectIrohPending<Void> {
        let gate = DirectIrohPending<Void>()
        lock.withLock {
            storedGate = gate
        }
        return gate
    }

    func openStream() async throws -> DirectIrohStreamHalves {
        log.add("open")
        let gate: DirectIrohPending<Void>? = lock.withLock {
            let held = storedGate
            storedGate = nil
            return held
        }
        if let gate {
            try await gate.wait()
        }
        let stream = DirectIrohFakeStream(log: log)
        let answer: Data? = lock.withLock {
            storedStreams.append(stream)
            return answers.isEmpty ? nil : answers.removeFirst()
        }
        if let answer {
            stream.receive.enqueue(answer)
        }
        return stream.halves
    }

    func closed() async {
        _ = try? await closedSignal.wait()
    }

    func close() {
        log.add("connection.close")
        closedSignal.resolve(())
    }

    /// The connection ends by itself: the peer closed it, or it timed out.
    func drop() {
        closedSignal.resolve(())
    }
}

final class DirectIrohFakeEndpoint: DirectIrohEndpoint, @unchecked Sendable {
    struct Dial: Equatable {
        let endpointID: DirectEndpointID
        let addresses: [String]
        let alpn: Data
    }

    private let log: DirectIrohEventLog
    private let lock = NSLock()
    private var storedDials: [Dial] = []
    /// What the dial returns, once the test supplies it.
    let connection = DirectIrohPending<DirectIrohFakeConnection>()

    init(log: DirectIrohEventLog) {
        self.log = log
    }

    var dials: [Dial] {
        lock.withLock { storedDials }
    }

    func connect(
        to endpointID: DirectEndpointID,
        addresses: [String],
        alpn: Data
    ) async throws -> any DirectIrohConnection {
        log.add("connect")
        lock.withLock {
            storedDials.append(Dial(endpointID: endpointID, addresses: addresses, alpn: alpn))
        }
        return try await connection.wait()
    }

    func close() async {
        log.add("endpoint.close")
    }
}

final class DirectIrohFakeEngine: DirectIrohEngine, @unchecked Sendable {
    let key = DirectIrohLaunchKey(
        secret: Data(repeating: 7, count: 32),
        endpointID: DirectEndpointID(bytes: Data(repeating: 0xAB, count: 32))!
    )

    private let log: DirectIrohEventLog
    private let lock = NSLock()
    private var prepared: [DirectIrohFakeEndpoint] = []
    private var storedBound: [DirectIrohFakeEndpoint] = []
    private var storedSecrets: [Data] = []
    private var storedBindGate: DirectIrohPending<Void>?

    init(log: DirectIrohEventLog) {
        self.log = log
    }

    /// The endpoints bound so far, in order.
    var bound: [DirectIrohFakeEndpoint] {
        lock.withLock { storedBound }
    }

    var secrets: [Data] {
        lock.withLock { storedSecrets }
    }

    /// The endpoint the next bind returns.
    func prepare() -> DirectIrohFakeEndpoint {
        let endpoint = DirectIrohFakeEndpoint(log: log)
        lock.withLock {
            prepared.append(endpoint)
        }
        return endpoint
    }

    func makeLaunchKey() throws -> DirectIrohLaunchKey {
        log.add("key")
        return key
    }

    /// Makes the next binds wait, as a bind that has not completed.
    func holdBinds() -> DirectIrohPending<Void> {
        let gate = DirectIrohPending<Void>()
        lock.withLock {
            storedBindGate = gate
        }
        return gate
    }

    func bind(secretKey: Data) async throws -> any DirectIrohEndpoint {
        log.add("bind")
        let gate = lock.withLock { storedBindGate }
        if let gate {
            try await gate.wait()
        }
        let fresh = DirectIrohFakeEndpoint(log: log)
        let endpoint: DirectIrohFakeEndpoint = lock.withLock {
            storedSecrets.append(secretKey)
            let next = prepared.isEmpty ? fresh : prepared.removeFirst()
            storedBound.append(next)
            return next
        }
        return endpoint
    }
}

/// Timers a test fires by hand.
@MainActor
final class DirectIrohManualScheduler: DirectIrohScheduling {
    private final class Entry: DirectIrohTimer {
        let seconds: TimeInterval
        var action: (@MainActor () -> Void)?

        init(seconds: TimeInterval, action: @escaping @MainActor () -> Void) {
            self.seconds = seconds
            self.action = action
        }

        @MainActor
        func cancel() {
            action = nil
        }
    }

    private var entries: [Entry] = []

    func schedule(
        after seconds: TimeInterval,
        _ action: @escaping @MainActor () -> Void
    ) -> any DirectIrohTimer {
        let entry = Entry(seconds: seconds, action: action)
        entries.append(entry)
        return entry
    }

    /// The delays of the timers that are still to fire, in the order they
    /// were set.
    var pending: [TimeInterval] {
        entries.filter { $0.action != nil }.map(\.seconds)
    }

    /// Fires the oldest live timer set for `seconds`.
    @discardableResult
    func fire(_ seconds: TimeInterval) -> Bool {
        guard
            let entry = entries.first(where: { $0.action != nil && $0.seconds == seconds }),
            let action = entry.action
        else {
            return false
        }
        entry.action = nil
        action()
        return true
    }
}

/// The transport's clock, moved by hand. It is read from any thread, as
/// the real one is, and never by a timer: what the clock says and when a
/// callback runs are separate things in these tests.
final class DirectIrohManualClock: @unchecked Sendable {
    private let lock = NSLock()
    private var current: TimeInterval = 1_000
    private var jump: (afterReads: Int, seconds: TimeInterval)?
    private var held: (reached: DirectIrohPending<Void>, release: DispatchSemaphore)?

    var now: TimeInterval {
        get {
            lock.withLock { current }
        }
        set {
            lock.withLock { current = newValue }
        }
    }

    /// What the transport reads. Counts the read.
    func read() -> TimeInterval {
        // A read that is being held stops here, where a network action's
        // task asks whether it may start, before it starts anything.
        let hold: (reached: DirectIrohPending<Void>, release: DispatchSemaphore)? = lock.withLock {
            guard !Thread.isMainThread, let waiting = held else {
                return nil
            }
            held = nil
            return waiting
        }
        if let hold {
            hold.reached.resolve(())
            hold.release.wait()
        }
        return lock.withLock {
            let value = current
            if let pending = jump {
                if pending.afterReads <= 1 {
                    current += pending.seconds
                    jump = nil
                } else {
                    jump = (pending.afterReads - 1, pending.seconds)
                }
            }
            return value
        }
    }

    /// Holds the next read made off the main thread, which is the one a
    /// network action's task makes just before it would start: the task
    /// was set going and has not reached the binding. `reached` resolves
    /// when the task is there; `release` lets it go on.
    func holdNextBackgroundRead() -> (reached: DirectIrohPending<Void>, release: () -> Void) {
        let reached = DirectIrohPending<Void>()
        let semaphore = DispatchSemaphore(value: 0)
        lock.withLock {
            held = (reached, semaphore)
        }
        return (reached, { semaphore.signal() })
    }

    /// Moves the clock forward once the transport has read it `reads`
    /// more times: between two steps of one operation that no test can
    /// otherwise get between.
    func advance(by seconds: TimeInterval, afterReads reads: Int) {
        lock.withLock {
            jump = (reads, seconds)
        }
    }
}

@MainActor
final class DirectIrohFakeNetwork: DirectIrohNetworkMonitoring {
    private var onChange: (@MainActor () -> Void)?

    var isWatching: Bool {
        onChange != nil
    }

    func start(onChange: @escaping @MainActor () -> Void) {
        self.onChange = onChange
    }

    func stop() {
        onChange = nil
    }

    func change() {
        onChange?()
    }
}

/// The rendezvous the transport asks. Each request takes the next scripted
/// answer; with none scripted it waits for the test.
@MainActor
final class DirectIrohFakeRendezvous {
    struct Request: Equatable {
        let selection: DirectIrohSelection
        let clientEndpointID: DirectEndpointID
    }

    private(set) var requests: [Request] = []
    private var script: [Result<Data, Error>] = []
    private(set) var held: [DirectIrohPending<Data>] = []

    func answer(_ data: Data) {
        script.append(.success(data))
    }

    func fail(_ error: Error) {
        script.append(.failure(error))
    }

    func fetch(_ selection: DirectIrohSelection, _ clientEndpointID: DirectEndpointID) async throws -> Data {
        requests.append(Request(selection: selection, clientEndpointID: clientEndpointID))
        guard !script.isEmpty else {
            let pending = DirectIrohPending<Data>()
            held.append(pending)
            return try await pending.wait()
        }
        return try script.removeFirst().get()
    }
}

enum DirectIrohFixtures {
    static let instanceID = "inst_1"
    static let host = "alice-app.avibe.bot"
    static let instanceEndpoint = DirectEndpointID(bytes: Data(repeating: 0xCD, count: 32))!

    static func selection(_ number: Int = 1) -> DirectIrohSelection {
        DirectIrohSelection(
            number: number,
            instanceID: instanceID,
            host: host,
            storeIdentifier: UUID(uuidString: "00000000-0000-0000-0000-0000000000A1")!
        )
    }

    static func ready(
        generation: String = "boot:1",
        client: DirectEndpointID,
        instanceID: String = DirectIrohFixtures.instanceID,
        endpoint: DirectEndpointID = DirectIrohFixtures.instanceEndpoint,
        addresses: [[String: String]] = [
            ["kind": "lan", "address": "192.168.1.20:51234"],
            ["kind": "lan", "address": "[fd12::1]:51234"],
        ]
    ) -> Data {
        let endpointObject: [String: Any] = [
            "id": endpoint.hex,
            "addresses": addresses,
            "relay": NSNull(),
        ]
        let clientObject: [String: Any] = [
            "endpoint_id": client.hex,
            "admitted_until": 1_800_000_300,
        ]
        let object: [String: Any] = [
            "version": 1,
            "status": "ready",
            "instance_id": instanceID,
            "hostname": host,
            "generation": generation,
            "issued_at": 1_800_000_000,
            "expires_at": 1_800_000_300,
            "alpn": "avibe-direct/1",
            "endpoint": endpointObject,
            "client": clientObject,
        ]
        // swiftlint:disable:next force_try
        return try! JSONSerialization.data(withJSONObject: object)
    }

    static func unavailable(
        retryAfterSeconds: Int = 60,
        instanceID: String = DirectIrohFixtures.instanceID,
        hostname: String = DirectIrohFixtures.host
    ) -> Data {
        let object: [String: Any] = [
            "version": 1,
            "status": "unavailable",
            "reason": "not_running",
            "instance_id": instanceID,
            "hostname": hostname,
            "retry_after_seconds": retryAfterSeconds,
        ]
        // swiftlint:disable:next force_try
        return try! JSONSerialization.data(withJSONObject: object)
    }
}

/// Waits until `condition` holds. Throws, after failing the test, when it
/// does not within `timeout`.
@MainActor
func directIrohEventually(
    _ description: String,
    timeout: TimeInterval = 30,
    file: StaticString = #filePath,
    line: UInt = #line,
    _ condition: @MainActor () -> Bool
) async throws {
    let deadline = Date().addingTimeInterval(timeout)
    while !condition() {
        guard Date() < deadline else {
            XCTFail("Timed out waiting for: \(description)", file: file, line: line)
            throw DirectIrohTestError.timedOut
        }
        try await Task.sleep(nanoseconds: 5_000_000)
    }
}

/// The outcome of `task`, or `nil` when it has not ended within `seconds`:
/// a test of promptness must not hang on what it tests.
func directIrohOutcome<Value: Sendable>(
    of task: Task<Value, Error>,
    within seconds: TimeInterval = 30
) async -> Result<Value, Error>? {
    let outcome = DirectIrohPending<Result<Value, Error>>()
    Task {
        outcome.resolve(await task.result)
    }
    Task {
        try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        outcome.fail(DirectIrohTestError.timedOut)
    }
    return try? await outcome.wait()
}
