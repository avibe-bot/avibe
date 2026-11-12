import Foundation

/// One stream to the instance over the iroh connection, as the local proxy
/// uses it: `read`, `write`, `closeWrite`, and `cancel`. Its four methods
/// are the `DirectByteStream` requirements of the persistent-proxy seam.
///
/// The binding's stream calls cannot be cancelled, and each half holds a
/// lock across its own awaits, so `reset` and `stop` cannot interrupt a
/// write or a read in progress (see `DirectIrohEngine.swift`). This type is
/// what makes the stream prompt anyway:
///
/// - `cancel()`, and the cancellation of a caller's task, end every call in
///   progress at once. The binding's own call is left to finish by itself,
///   and bytes it still produces are dropped.
/// - A call that was abandoned leaves the stream cancelled: bytes it was
///   reading or writing are unaccounted for, so the stream is never used
///   again.
/// - Cancelling asks the binding to reset the sending half and to stop the
///   receiving half. Each request takes effect once that half's call in
///   progress has ended: when the instance reacts to the other half, when
///   the transport accepts or delivers bytes, or when the connection closes.
///
/// One read and one write may be in progress at a time, as in a relay.
final class DirectIrohByteStream: @unchecked Sendable {
    /// The most bytes asked of the binding in one read: it allocates the
    /// whole limit for every call.
    static let maxReadBytes = 64 * 1024

    private let send: any DirectIrohSendHalf
    private let receive: any DirectIrohReceiveHalf
    private let lock = NSLock()
    private var isCancelled = false
    /// `finish` completed: what was written is still to be delivered, so
    /// the sending half is not reset afterwards.
    private var didFinishSending = false
    /// The instance finished sending: nothing is left to stop.
    private var didReceiveEnd = false
    private var lastCall = 0
    private var calls: [Int: (Error) -> Void] = [:]

    private struct Cancellation {
        let fails: [(Error) -> Void]
        let resetSend: Bool
        let stopReceive: Bool
    }

    init(_ halves: DirectIrohStreamHalves) {
        send = halves.send
        receive = halves.receive
    }

    deinit {
        // Released without `cancel()`: a half that was left open is
        // abandoned, not finished as if its bytes were complete.
        guard !isCancelled else {
            return
        }
        Self.abort(
            send: didFinishSending ? nil : send,
            receive: didReceiveEnd ? nil : receive
        )
    }

    /// The next bytes from the instance, or `nil` once it has finished
    /// sending.
    func read(maxLength: Int) async throws -> Data? {
        let receive = receive
        let limit = min(max(maxLength, 1), Self.maxReadBytes)
        let data = try await perform {
            try await receive.read(sizeLimit: limit)
        }
        guard !data.isEmpty else {
            lock.withLock {
                didReceiveEnd = true
            }
            return nil
        }
        return data
    }

    /// Returns once the transport has accepted `data`. Not returning is the
    /// backpressure.
    func write(_ data: Data) async throws {
        let send = send
        try await perform {
            if !data.isEmpty {
                try await send.writeAll(data)
            }
        }
    }

    /// This side has finished sending. Reads continue.
    func closeWrite() async throws {
        let send = send
        try await perform {
            try await send.finish()
        }
        lock.withLock {
            didFinishSending = true
        }
    }

    /// Ends both directions at once. A `read` or `write` in progress throws.
    func cancel() {
        let pending: Cancellation? = lock.withLock {
            guard !isCancelled else {
                return nil
            }
            isCancelled = true
            let fails = Array(calls.values)
            calls.removeAll()
            return Cancellation(
                fails: fails,
                resetSend: !didFinishSending,
                stopReceive: !didReceiveEnd
            )
        }
        guard let pending else {
            return
        }
        for fail in pending.fails {
            fail(DirectIrohError.cancelled)
        }
        Self.abort(
            send: pending.resetSend ? send : nil,
            receive: pending.stopReceive ? receive : nil
        )
    }

    /// Abandons a stream nobody will use: one that opened too late.
    static func abort(_ halves: DirectIrohStreamHalves) {
        abort(send: halves.send, receive: halves.receive)
    }

    private static func abort(
        send: (any DirectIrohSendHalf)?,
        receive: (any DirectIrohReceiveHalf)?
    ) {
        if let send {
            Task {
                await send.reset()
            }
        }
        if let receive {
            Task {
                await receive.stop()
            }
        }
    }

    private func perform<Value: Sendable>(
        _ operation: @escaping @Sendable () async throws -> Value
    ) async throws -> Value {
        let call = DirectIrohCall<Value>()
        let registered: Int? = lock.withLock {
            guard !isCancelled else {
                return nil
            }
            lastCall += 1
            calls[lastCall] = { error in
                call.fail(error)
            }
            return lastCall
        }
        guard let id = registered else {
            throw DirectIrohError.cancelled
        }
        defer {
            lock.withLock {
                calls[id] = nil
            }
        }
        do {
            return try await call.run(operation)
        } catch let error as CancellationError {
            // The caller's task was cancelled and the binding's call
            // abandoned in the middle of its bytes.
            cancel()
            throw error
        }
    }
}
