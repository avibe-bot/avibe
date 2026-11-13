import Foundation

/// One stream to the instance over the iroh connection, as the local proxy
/// uses it: `read`, `write`, `closeWrite`, and `cancel`. Its four methods
/// are the `DirectByteStream` requirements of the persistent-proxy seam.
///
/// The binding's stream calls cannot be cancelled, and each half holds a
/// lock across its own awaits, so `reset` and `stop` cannot interrupt a
/// write or a read in progress (see `DirectIrohEngine.swift`). This type
/// owns each half's state, and every change of it happens under one lock:
///
/// - **A binding call that was started runs to its end and records its own
///   outcome.** Starting it and recording that it runs are one step; its
///   completion and `cancel()` take the same lock, so one of them comes
///   first and the other sees it.
/// - **`cancel()`, and the cancellation of a caller's task, end every call
///   in progress at once** for the caller. If `cancel()` came first, the
///   call's result is not returned, whatever it was: a read does not
///   report bytes or a clean end from a cancelled stream.
/// - **A half that ended cleanly is left alone.** After the instance's end
///   of stream the receiving half is never stopped. From the step in which
///   `closeWrite` hands `finish` to the binding, the sending half is never
///   reset by `cancel()`: the bytes written before are still to be
///   delivered. If that `finish` fails, the call that made it resets the
///   half.
/// - **A half that did not end cleanly is abandoned exactly once:** the
///   sending half reset, the receiving half stopped. Each request takes
///   effect once that half's call in progress has ended in the binding:
///   when the instance reacts to the other half, when the transport
///   accepts or delivers bytes, or when the connection closes.
///
/// One read and one write may be in progress at a time, as in a relay. A
/// second one, a write after `closeWrite`, and a second `closeWrite` while
/// the first is in progress are refused without reaching the binding.
final class DirectIrohByteStream: @unchecked Sendable {
    /// The most bytes asked of the binding in one read: it allocates the
    /// whole limit for every call.
    static let maxReadBytes = 64 * 1024

    private enum SendState {
        case open
        /// A `writeAll` is in the binding.
        case writing
        /// `finish` was handed to the binding. The call that did it owns
        /// the half until it records `finished` or `abandoned`.
        case finishing
        /// Finished cleanly. No more binding calls; never reset.
        case finished
        /// Reset, or about to be: by `cancel()`, on release, or after a
        /// write or a `finish` that failed.
        case abandoned
    }

    private enum ReceiveState {
        case open
        /// A `read` is in the binding.
        case reading
        /// The instance finished sending. No more binding calls; never
        /// stopped.
        case ended
        /// Stopped, or about to be: by `cancel()`, on release, or after a
        /// read that failed.
        case abandoned
    }

    /// What a call that reached the binding found when it came back.
    private enum Completion<Value> {
        case value(Value)
        case failure(Error)
    }

    private let send: any DirectIrohSendHalf
    private let receive: any DirectIrohReceiveHalf
    private let lock = NSLock()
    private var isCancelled = false
    private var sendState = SendState.open
    private var receiveState = ReceiveState.open
    private var lastCall = 0
    private var calls: [Int: (Error) -> Void] = [:]

    init(_ halves: DirectIrohStreamHalves) {
        send = halves.send
        receive = halves.receive
    }

    deinit {
        // Released without `cancel()`. No call is in progress, or the
        // stream would still be held: a half that is still open is
        // abandoned, not finished as if its bytes were complete.
        guard !isCancelled else {
            return
        }
        Self.abandon(
            send: sendState == .open ? send : nil,
            receive: receiveState == .open ? receive : nil
        )
    }

    /// The next bytes from the instance, or `nil` once it has finished
    /// sending.
    func read(maxLength: Int) async throws -> Data? {
        let receive = receive
        let limit = min(max(maxLength, 1), Self.maxReadBytes)
        return try await perform(
            start: { () throws -> Start<Data?> in
                switch self.receiveState {
                case .open:
                    self.receiveState = .reading
                    return .call
                case .reading:
                    throw DirectIrohError.busy
                case .ended:
                    // Settled from the recorded end: the binding is not
                    // asked again.
                    return .settled(nil)
                case .abandoned:
                    throw DirectIrohError.cancelled
                }
            },
            call: { () async throws -> Data in
                try await receive.read(sizeLimit: limit)
            },
            finish: { (completion: Completion<Data>) throws -> Data? in
                guard self.receiveState == .reading else {
                    // `cancel()` came first and stopped the half: whatever
                    // the binding returned, a clean end included, is not
                    // this read's answer.
                    throw DirectIrohError.cancelled
                }
                switch completion {
                case .value(let data):
                    guard !data.isEmpty else {
                        self.receiveState = .ended
                        return nil
                    }
                    self.receiveState = .open
                    return data
                case .failure(let error):
                    self.receiveState = .abandoned
                    Self.abandon(send: nil, receive: receive)
                    throw error
                }
            }
        )
    }

    /// Returns once the transport has accepted `data`. Not returning is the
    /// backpressure. Throws `closed` after `closeWrite`.
    func write(_ data: Data) async throws {
        let send = send
        try await perform(
            start: { () throws -> Start<Void> in
                switch self.sendState {
                case .open:
                    guard !data.isEmpty else {
                        return .settled(())
                    }
                    self.sendState = .writing
                    return .call
                case .writing:
                    throw DirectIrohError.busy
                case .finishing, .finished:
                    throw DirectIrohError.closed
                case .abandoned:
                    throw DirectIrohError.cancelled
                }
            },
            call: { () async throws -> Void in
                try await send.writeAll(data)
            },
            finish: { (completion: Completion<Void>) throws -> Void in
                guard self.sendState == .writing else {
                    // `cancel()` came first and reset the half.
                    throw DirectIrohError.cancelled
                }
                switch completion {
                case .value:
                    self.sendState = .open
                case .failure(let error):
                    self.sendState = .abandoned
                    Self.abandon(send: send, receive: nil)
                    throw error
                }
            }
        )
    }

    /// This side has finished sending. Reads continue. When this returns,
    /// the sending half is finished and is never reset afterwards. Calling
    /// it again then does nothing.
    func closeWrite() async throws {
        let send = send
        try await perform(
            start: { () throws -> Start<Void> in
                switch self.sendState {
                case .open:
                    self.sendState = .finishing
                    return .call
                case .writing, .finishing:
                    throw DirectIrohError.busy
                case .finished:
                    return .settled(())
                case .abandoned:
                    throw DirectIrohError.cancelled
                }
            },
            call: { () async throws -> Void in
                try await send.finish()
            },
            finish: { (completion: Completion<Void>) throws -> Void in
                // `cancel()` leaves a half that is finishing to this call,
                // so the state is still `finishing` here either way.
                switch completion {
                case .value:
                    self.sendState = .finished
                case .failure(let error):
                    // Nothing says a `finish` that failed closed the half.
                    self.sendState = .abandoned
                    Self.abandon(send: send, receive: nil)
                    throw error
                }
            }
        )
    }

    /// Ends both directions at once. A `read` or `write` in progress throws.
    func cancel() {
        let ended: [(Error) -> Void]? = lock.withLock {
            guard !isCancelled else {
                return nil
            }
            isCancelled = true
            switch sendState {
            case .open, .writing:
                sendState = .abandoned
                Self.abandon(send: send, receive: nil)
            case .finishing, .finished, .abandoned:
                // Finishing: the call that handed `finish` over still owns
                // the half and records how it ended.
                break
            }
            switch receiveState {
            case .open, .reading:
                receiveState = .abandoned
                Self.abandon(send: nil, receive: receive)
            case .ended, .abandoned:
                break
            }
            let pending = Array(calls.values)
            calls.removeAll()
            return pending
        }
        for fail in ended ?? [] {
            fail(DirectIrohError.cancelled)
        }
    }

    /// Abandons a stream nobody will use: one that opened too late.
    static func abort(_ halves: DirectIrohStreamHalves) {
        abandon(send: halves.send, receive: halves.receive)
    }

    /// Asks the binding to reset a sending half and to stop a receiving
    /// half. Each request is made once for a half, by whoever moved it to
    /// `abandoned`.
    private static func abandon(
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

    private enum Start<Value> {
        /// The binding is called.
        case call
        /// The answer is already known: the binding is not called.
        case settled(Value)
    }

    /// Runs one operation of a half.
    ///
    /// `start` and `finish` run under the stream's lock. `start` decides
    /// from the half's state whether the binding is called, and records
    /// that it is; a stream that is already cancelled never gets that far.
    /// If the binding is called, the call runs to its end even if its
    /// caller leaves, and `finish` then records what the half became and
    /// what the caller gets. When `cancel()` came first, `finish` still
    /// records the half's state, and the caller gets `cancelled` instead.
    private func perform<Raw: Sendable, Value: Sendable>(
        start: () throws -> Start<Value>,
        call bindingCall: @escaping @Sendable () async throws -> Raw,
        finish: @escaping @Sendable (Completion<Raw>) throws -> Value
    ) async throws -> Value {
        if Task.isCancelled {
            // The caller is gone before anything started.
            cancel()
            throw CancellationError()
        }
        let call = DirectIrohCall<Value>()
        let started: Start<Value> = try lock.withLock {
            guard !isCancelled else {
                throw DirectIrohError.cancelled
            }
            let decision = try start()
            if case .call = decision {
                lastCall += 1
                let id = lastCall
                calls[id] = { error in
                    call.fail(error)
                }
                // Handed over in the step that recorded it, and run even if
                // the caller has left by then: the half is never left in a
                // state no call will settle.
                call.begin(.always) {
                    let completion: Completion<Raw>
                    do {
                        completion = .value(try await bindingCall())
                    } catch {
                        completion = .failure(error)
                    }
                    let outcome: Value = try self.lock.withLock {
                        self.calls[id] = nil
                        // Records what the half became, whoever came
                        // first. If it was `cancel()`, the caller gets
                        // `cancelled`, never this result.
                        let value = try finish(completion)
                        guard !self.isCancelled else {
                            throw DirectIrohError.cancelled
                        }
                        return value
                    }
                    return outcome
                }
            }
            return decision
        }
        if case .settled(let value) = started {
            return value
        }
        do {
            return try await call.wait()
        } catch let error as CancellationError {
            // The caller's task was cancelled and the binding's call left
            // in the middle of its bytes: the stream is over.
            cancel()
            throw error
        }
    }
}
