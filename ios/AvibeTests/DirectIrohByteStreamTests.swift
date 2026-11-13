import XCTest
@testable import Avibe

/// The stream over the binding, and the two helpers under it: calls that
/// cannot be cancelled must still end at once for their caller.
final class DirectIrohByteStreamTests: XCTestCase {
    // MARK: Calls and deadlines

    func testACancelledCallerLeavesAtOnceAndTheLateValueIsDiscarded() async throws {
        let operation = DirectIrohPending<Int>()
        let discarded = DirectIrohPending<Int>()
        let started = DirectIrohPending<Void>()
        let task = Task<Int, Error> {
            try await directIrohCall(
                discard: { (late: Int) -> Void in
                    discarded.resolve(late)
                },
                {
                    started.resolve(())
                    return try await operation.wait()
                }
            )
        }
        try await started.wait()

        task.cancel()
        let outcome = await directIrohOutcome(of: task)
        XCTAssertTrue(Self.failure(of: outcome) is CancellationError)

        // The operation ignored the cancellation and finishes later.
        operation.resolve(7)
        let late = try await discarded.wait()
        XCTAssertEqual(late, 7)
    }

    func testNothingIsStartedForACallerThatIsAlreadyCancelled() async throws {
        let ran = DirectIrohEventLog()
        let entered = DirectIrohPending<Void>()
        let proceed = DirectIrohPending<Void>()
        let task = Task<Int, Error> {
            entered.resolve(())
            try? await proceed.wait()
            // The task is cancelled by now.
            return try await directIrohCall {
                ran.add("ran")
                return 1
            }
        }
        try await entered.wait()
        task.cancel()
        proceed.resolve(())

        let outcome = await directIrohOutcome(of: task)
        XCTAssertTrue(Self.failure(of: outcome) is CancellationError)
        XCTAssertEqual(ran.all, [])
    }

    func testADeadlineEndsAnOperationThatNeverFinishes() async throws {
        let operation = DirectIrohPending<Int>()
        let task = Task<Int, Error> {
            try await directIrohDeadline(0.05) {
                try await directIrohCall {
                    try await operation.wait()
                }
            }
        }
        let outcome = await directIrohOutcome(of: task)
        XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohError, .timedOut)

        let value = try await directIrohDeadline(30) {
            try await directIrohCall {
                42
            }
        }
        XCTAssertEqual(value, 42)

        let failing = Task<Int, Error> {
            try await directIrohDeadline(30) {
                try await directIrohCall {
                    throw DirectIrohTestError.scripted
                }
            }
        }
        let failed = await directIrohOutcome(of: failing)
        XCTAssertEqual(Self.failure(of: failed) as? DirectIrohTestError, .scripted)
    }

    func testAValueThatLosesToItsDeadlineIsDiscardedNotDropped() async throws {
        // The operation returns a value although the deadline was already
        // taken: the two finished together, or the operation did not stop
        // when told to. The caller gets the timeout, and the value goes to
        // `discard`, which closes what it holds.
        let operation = DirectIrohPending<Int>()
        let discarded = DirectIrohPending<Int>()
        let task = Task<Int, Error> {
            try await directIrohDeadline(
                0.05,
                discard: { (late: Int) -> Void in
                    discarded.resolve(late)
                },
                {
                    try await operation.wait()
                }
            )
        }
        try await Task.sleep(nanoseconds: 300_000_000)
        operation.resolve(7)
        let late = try await discarded.wait()
        XCTAssertEqual(late, 7)
        let outcome = await directIrohOutcome(of: task)
        XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohError, .timedOut)

        // A value that wins is returned and never discarded.
        let unused = DirectIrohEventLog()
        let value = try await directIrohDeadline(
            30,
            discard: { (_: Int) -> Void in
                unused.add("discarded")
            },
            {
                5
            }
        )
        XCTAssertEqual(value, 5)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(unused.all, [])
    }

    // MARK: Bytes

    func testBytesPassBothWaysAndTheEndIsNil() async throws {
        let log = DirectIrohEventLog()
        let fake = DirectIrohFakeStream(log: log)
        let stream = DirectIrohByteStream(fake.halves)

        fake.receive.enqueue(Data("hello".utf8))
        fake.receive.enqueue(Data())
        let first = try await stream.read(maxLength: 16)
        XCTAssertEqual(first, Data("hello".utf8))
        // The binding allocates the whole limit for each read.
        _ = try await stream.read(maxLength: 10_000_000)
        XCTAssertEqual(fake.receive.limits, [16, DirectIrohByteStream.maxReadBytes])

        try await stream.write(Data("abc".utf8))
        try await stream.write(Data())
        try await stream.write(Data("def".utf8))
        XCTAssertEqual(fake.send.written, Data("abcdef".utf8))
        try await stream.closeWrite()
        XCTAssertEqual(log.all, ["read", "read", "write", "write", "finish"])

        // Both directions ended as they should: nothing is left to abandon,
        // and what was written is still to be delivered.
        stream.cancel()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(log.count(of: "reset"), 0)
        XCTAssertEqual(log.count(of: "stop"), 0)
    }

    func testAWriteReturnsOnlyOnceTheTransportAcceptedIt() async throws {
        let log = DirectIrohEventLog()
        let fake = DirectIrohFakeStream(log: log)
        let stream = DirectIrohByteStream(fake.halves)
        let gate = fake.send.holdWrites()
        let done = DirectIrohEventLog()

        let task = Task<Void, Error> {
            try await stream.write(Data("abc".utf8))
            done.add("written")
        }
        try await eventually("the write reaches the transport") { log.count(of: "write") == 1 }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(done.all, [])

        gate.resolve(())
        let outcome = await directIrohOutcome(of: task)
        XCTAssertNotNil(outcome)
        XCTAssertNil(Self.failure(of: outcome))
        XCTAssertEqual(fake.send.written, Data("abc".utf8))
    }

    func testCancelEndsAReadAndAWriteInProgressAtOnce() async throws {
        let log = DirectIrohEventLog()
        let fake = DirectIrohFakeStream(log: log)
        let stream = DirectIrohByteStream(fake.halves)
        let lateRead = fake.receive.enqueuePending()
        let gate = fake.send.holdWrites()

        let reading = Task<Data?, Error> {
            try await stream.read(maxLength: 16)
        }
        let writing = Task<Void, Error> {
            try await stream.write(Data("abc".utf8))
        }
        try await eventually("both calls reach the transport") {
            log.count(of: "read") == 1 && log.count(of: "write") == 1
        }

        stream.cancel()
        let readOutcome = await directIrohOutcome(of: reading)
        let writeOutcome = await directIrohOutcome(of: writing)
        XCTAssertEqual(Self.failure(of: readOutcome) as? DirectIrohError, .cancelled)
        XCTAssertEqual(Self.failure(of: writeOutcome) as? DirectIrohError, .cancelled)
        try await eventually("both halves are abandoned") {
            log.count(of: "reset") == 1 && log.count(of: "stop") == 1
        }

        // The binding's calls end later: their bytes go nowhere.
        lateRead.resolve(Data("late".utf8))
        gate.resolve(())

        // Nothing more reaches the transport, and cancelling again is
        // nothing.
        let before = log.all.filter { $0 == "read" || $0 == "write" || $0 == "finish" }
        await assertCancelled { _ = try await stream.read(maxLength: 16) }
        await assertCancelled { try await stream.write(Data("x".utf8)) }
        await assertCancelled { try await stream.closeWrite() }
        stream.cancel()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(log.all.filter { $0 == "read" || $0 == "write" || $0 == "finish" }, before)
        XCTAssertEqual(log.count(of: "reset"), 1)
        XCTAssertEqual(log.count(of: "stop"), 1)
    }

    func testACancelledCallerEndsItsReadAtOnceAndTheStreamWithIt() async throws {
        let log = DirectIrohEventLog()
        let fake = DirectIrohFakeStream(log: log)
        let stream = DirectIrohByteStream(fake.halves)

        let reading = Task<Data?, Error> {
            try await stream.read(maxLength: 16)
        }
        try await eventually("the read reaches the transport") { log.count(of: "read") == 1 }

        reading.cancel()
        let outcome = await directIrohOutcome(of: reading)
        XCTAssertTrue(Self.failure(of: outcome) is CancellationError)
        // The abandoned read may still take bytes, so the stream is over.
        await assertCancelled { try await stream.write(Data("x".utf8)) }
        try await eventually("both halves are abandoned") {
            log.count(of: "reset") == 1 && log.count(of: "stop") == 1
        }
    }

    func testCancelAbandonsOnlyTheHalvesThatHaveNotEnded() async throws {
        // Finished sending, still receiving: only the receiving half.
        do {
            let log = DirectIrohEventLog()
            let fake = DirectIrohFakeStream(log: log)
            let stream = DirectIrohByteStream(fake.halves)
            try await stream.closeWrite()
            stream.cancel()
            try await eventually("the receiving half is stopped") { log.count(of: "stop") == 1 }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(log.count(of: "reset"), 0)
        }
        // The instance finished, this side is still sending: only the
        // sending half.
        do {
            let log = DirectIrohEventLog()
            let fake = DirectIrohFakeStream(log: log)
            let stream = DirectIrohByteStream(fake.halves)
            fake.receive.enqueue(Data())
            let end = try await stream.read(maxLength: 16)
            XCTAssertNil(end)
            stream.cancel()
            try await eventually("the sending half is reset") { log.count(of: "reset") == 1 }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(log.count(of: "stop"), 0)
        }
    }

    func testACancelDuringCloseWriteNeverResetsTheSendingHalf() async throws {
        let log = DirectIrohEventLog()
        let fake = DirectIrohFakeStream(log: log)
        let stream = DirectIrohByteStream(fake.halves)
        try await stream.write(Data("abc".utf8))
        let gate = fake.send.holdFinish()

        let closing = Task<Void, Error> {
            try await stream.closeWrite()
        }
        try await eventually("the finish reaches the binding") { log.count(of: "finish") == 1 }

        // The binding's `finish` has not completed. From the moment it was
        // handed over, the bytes written before it are to be delivered: a
        // cancel ends the call and the receiving half, never the sending one.
        stream.cancel()
        let outcome = await directIrohOutcome(of: closing)
        XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohError, .cancelled)
        try await eventually("the receiving half is stopped") { log.count(of: "stop") == 1 }
        gate.resolve(())
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(log.count(of: "reset"), 0)
        // Handed to the binding once, and by nobody else afterwards.
        XCTAssertEqual(log.count(of: "finish"), 1)

        // The same when the stream is cancelled the moment `closeWrite`
        // returns, and when it is then released.
        do {
            let other = DirectIrohFakeStream(log: log)
            let second = DirectIrohByteStream(other.halves)
            try await second.write(Data("abc".utf8))
            try await second.closeWrite()
            second.cancel()
        }
        try await eventually("the second receiving half is stopped") { log.count(of: "stop") == 2 }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(log.count(of: "reset"), 0)
    }

    func testAFinishThatFailsAbandonsTheSendingHalfExactlyOnce() async throws {
        // Without a cancel: the caller gets the binding's error, and the
        // half, which nothing says the failure closed, is reset.
        do {
            let log = DirectIrohEventLog()
            let fake = DirectIrohFakeStream(log: log)
            let stream = DirectIrohByteStream(fake.halves)
            try await stream.write(Data("abc".utf8))
            let gate = fake.send.holdFinish()
            let closing = Task<Void, Error> {
                try await stream.closeWrite()
            }
            try await eventually("the finish reaches the binding") { log.count(of: "finish") == 1 }
            gate.fail(DirectIrohTestError.scripted)
            let outcome = await directIrohOutcome(of: closing)
            XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohTestError, .scripted)
            try await eventually("the sending half is reset") { log.count(of: "reset") == 1 }

            // The half is over: nothing more reaches the binding for it,
            // and a later cancel does not reset it a second time.
            await assertThrows(.cancelled) { try await stream.closeWrite() }
            await assertThrows(.cancelled) { try await stream.write(Data("x".utf8)) }
            stream.cancel()
            try await eventually("the receiving half is stopped") { log.count(of: "stop") == 1 }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(log.count(of: "reset"), 1)
            XCTAssertEqual(log.count(of: "finish"), 1)
            XCTAssertEqual(log.count(of: "write"), 1)
        }
        // With a cancel while the finish is in the binding: the cancel
        // leaves the half to that call, and the call resets it when its
        // finish fails.
        do {
            let log = DirectIrohEventLog()
            let fake = DirectIrohFakeStream(log: log)
            let stream = DirectIrohByteStream(fake.halves)
            let gate = fake.send.holdFinish()
            let closing = Task<Void, Error> {
                try await stream.closeWrite()
            }
            try await eventually("the finish reaches the binding") { log.count(of: "finish") == 1 }
            stream.cancel()
            let outcome = await directIrohOutcome(of: closing)
            XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohError, .cancelled)
            try await eventually("the receiving half is stopped") { log.count(of: "stop") == 1 }
            XCTAssertEqual(log.count(of: "reset"), 0)
            gate.fail(DirectIrohTestError.scripted)
            try await eventually("the sending half is reset by its own call") {
                log.count(of: "reset") == 1
            }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(log.count(of: "reset"), 1)
            XCTAssertEqual(log.count(of: "finish"), 1)
        }
    }

    func testTheSendingHalfTakesNothingAfterCloseWriteAndIsFinishedOnce() async throws {
        let log = DirectIrohEventLog()
        let fake = DirectIrohFakeStream(log: log)
        let stream = DirectIrohByteStream(fake.halves)
        try await stream.write(Data("abc".utf8))
        let gate = fake.send.holdFinish()
        let closing = Task<Void, Error> {
            try await stream.closeWrite()
        }
        try await eventually("the finish reaches the binding") { log.count(of: "finish") == 1 }

        // While the finish is in the binding.
        await assertThrows(.closed) { try await stream.write(Data("late".utf8)) }
        await assertThrows(.busy) { try await stream.closeWrite() }

        gate.resolve(())
        let outcome = await directIrohOutcome(of: closing)
        XCTAssertNotNil(outcome)
        XCTAssertNil(Self.failure(of: outcome))

        // After it returned: closing again is nothing, and a write is
        // refused. Neither reaches the binding.
        try await stream.closeWrite()
        await assertThrows(.closed) { try await stream.write(Data("late".utf8)) }
        await assertThrows(.closed) { try await stream.write(Data()) }
        XCTAssertEqual(log.count(of: "finish"), 1)
        XCTAssertEqual(log.count(of: "write"), 1)
        XCTAssertEqual(fake.send.written, Data("abc".utf8))
    }

    func testOneReadAndOneWriteAtATime() async throws {
        let log = DirectIrohEventLog()
        let fake = DirectIrohFakeStream(log: log)
        let stream = DirectIrohByteStream(fake.halves)
        let gate = fake.send.holdWrites()
        let pendingRead = fake.receive.enqueuePending()

        let writing = Task<Void, Error> {
            try await stream.write(Data("abc".utf8))
        }
        let reading = Task<Data?, Error> {
            try await stream.read(maxLength: 16)
        }
        try await eventually("both calls reach the binding") {
            log.count(of: "write") == 1 && log.count(of: "read") == 1
        }

        // A second call of either kind, and a close during a write, are
        // refused without reaching the binding. The stream goes on.
        await assertThrows(.busy) { try await stream.write(Data("x".utf8)) }
        await assertThrows(.busy) { try await stream.closeWrite() }
        await assertThrows(.busy) { _ = try await stream.read(maxLength: 16) }

        gate.resolve(())
        pendingRead.resolve(Data("hello".utf8))
        let written = await directIrohOutcome(of: writing)
        XCTAssertNil(Self.failure(of: written))
        guard case .success(let bytes)? = await directIrohOutcome(of: reading) else {
            return XCTFail("The read failed.")
        }
        XCTAssertEqual(bytes, Data("hello".utf8))
        try await stream.closeWrite()
        XCTAssertEqual(log.count(of: "write"), 1)
        XCTAssertEqual(log.count(of: "read"), 1)
        XCTAssertEqual(log.count(of: "finish"), 1)
    }

    func testACleanEndAndACancelAreOneDecision() async throws {
        // The cancel first: the read does not report a clean end, even
        // though the binding then returns one.
        do {
            let log = DirectIrohEventLog()
            let fake = DirectIrohFakeStream(log: log)
            let stream = DirectIrohByteStream(fake.halves)
            let pendingRead = fake.receive.enqueuePending()
            let reading = Task<Data?, Error> {
                try await stream.read(maxLength: 16)
            }
            try await eventually("the read reaches the binding") { log.count(of: "read") == 1 }

            stream.cancel()
            pendingRead.resolve(Data())
            let outcome = await directIrohOutcome(of: reading)
            XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohError, .cancelled)
            try await eventually("the receiving half is stopped") { log.count(of: "stop") == 1 }
            await assertThrows(.cancelled) { _ = try await stream.read(maxLength: 16) }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(log.count(of: "read"), 1)
            XCTAssertEqual(log.count(of: "stop"), 1)
        }
        // The end first: it is the stream's answer from then on, the
        // binding is not asked again, and a cancel does not stop the half.
        do {
            let log = DirectIrohEventLog()
            let fake = DirectIrohFakeStream(log: log)
            let stream = DirectIrohByteStream(fake.halves)
            fake.receive.enqueue(Data())
            let first = try await stream.read(maxLength: 16)
            let second = try await stream.read(maxLength: 16)
            XCTAssertNil(first)
            XCTAssertNil(second)
            XCTAssertEqual(log.count(of: "read"), 1)

            stream.cancel()
            try await eventually("the sending half is reset") { log.count(of: "reset") == 1 }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(log.count(of: "stop"), 0)
            XCTAssertEqual(log.count(of: "read"), 1)
        }
    }

    func testAReadOrAWriteThatFailsAbandonsItsHalfAndLeavesTheOther() async throws {
        let log = DirectIrohEventLog()
        let fake = DirectIrohFakeStream(log: log)
        let stream = DirectIrohByteStream(fake.halves)

        // The receiving half fails: it is stopped once; sending goes on.
        let pendingRead = fake.receive.enqueuePending()
        let reading = Task<Data?, Error> {
            try await stream.read(maxLength: 16)
        }
        try await eventually("the read reaches the binding") { log.count(of: "read") == 1 }
        pendingRead.fail(DirectIrohTestError.scripted)
        let readOutcome = await directIrohOutcome(of: reading)
        XCTAssertEqual(Self.failure(of: readOutcome) as? DirectIrohTestError, .scripted)
        try await eventually("the receiving half is stopped") { log.count(of: "stop") == 1 }
        await assertThrows(.cancelled) { _ = try await stream.read(maxLength: 16) }
        try await stream.write(Data("abc".utf8))
        XCTAssertEqual(log.count(of: "reset"), 0)

        // The sending half fails: it is reset once.
        let gate = fake.send.holdWrites()
        let writing = Task<Void, Error> {
            try await stream.write(Data("def".utf8))
        }
        try await eventually("the write reaches the binding") { log.count(of: "write") == 2 }
        gate.fail(DirectIrohTestError.scripted)
        let writeOutcome = await directIrohOutcome(of: writing)
        XCTAssertEqual(Self.failure(of: writeOutcome) as? DirectIrohTestError, .scripted)
        try await eventually("the sending half is reset") { log.count(of: "reset") == 1 }

        stream.cancel()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(log.count(of: "reset"), 1)
        XCTAssertEqual(log.count(of: "stop"), 1)
        XCTAssertEqual(log.count(of: "read"), 1)
    }

    func testACallerThatIsAlreadyCancelledHandsNothingToTheBinding() async throws {
        let log = DirectIrohEventLog()
        let fake = DirectIrohFakeStream(log: log)
        let stream = DirectIrohByteStream(fake.halves)
        let entered = DirectIrohPending<Void>()
        let proceed = DirectIrohPending<Void>()
        let closing = Task<Void, Error> {
            entered.resolve(())
            try? await proceed.wait()
            try await stream.closeWrite()
        }
        try await entered.wait()
        closing.cancel()
        proceed.resolve(())

        let outcome = await directIrohOutcome(of: closing)
        XCTAssertTrue(Self.failure(of: outcome) is CancellationError)
        // Nothing was handed over, so the stream is abandoned whole.
        try await eventually("both halves are abandoned") {
            log.count(of: "reset") == 1 && log.count(of: "stop") == 1
        }
        XCTAssertEqual(log.count(of: "finish"), 0)
    }

    func testAStreamReleasedWithoutCancelIsAbandonedNotFinished() async throws {
        let log = DirectIrohEventLog()
        let fake = DirectIrohFakeStream(log: log)
        do {
            let stream = DirectIrohByteStream(fake.halves)
            try await stream.write(Data("abc".utf8))
        }
        try await eventually("both halves are abandoned") {
            log.count(of: "reset") == 1 && log.count(of: "stop") == 1
        }
        XCTAssertEqual(log.count(of: "finish"), 0)

        let late = DirectIrohFakeStream(log: log)
        DirectIrohByteStream.abort(late.halves)
        try await eventually("a stream that opened too late is abandoned") {
            log.count(of: "reset") == 2 && log.count(of: "stop") == 2
        }
    }

    // MARK: Helpers

    private static func failure<Value>(of outcome: Result<Value, Error>?) -> Error? {
        guard case .failure(let error)? = outcome else {
            return nil
        }
        return error
    }

    private func assertCancelled(
        file: StaticString = #filePath,
        line: UInt = #line,
        _ operation: () async throws -> Void
    ) async {
        await assertThrows(.cancelled, file: file, line: line, operation)
    }

    private func assertThrows(
        _ expected: DirectIrohError,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ operation: () async throws -> Void
    ) async {
        do {
            try await operation()
            XCTFail("Nothing was thrown.", file: file, line: line)
        } catch {
            XCTAssertEqual(error as? DirectIrohError, expected, file: file, line: line)
        }
    }

    private func eventually(
        _ description: String,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ condition: @MainActor () -> Bool
    ) async throws {
        try await directIrohEventually(description, file: file, line: line, condition)
    }
}
