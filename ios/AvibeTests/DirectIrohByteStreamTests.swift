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
        XCTAssertGreaterThanOrEqual(log.count(of: "finish"), 1)

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
        do {
            try await operation()
            XCTFail("The stream still works.", file: file, line: line)
        } catch {
            XCTAssertEqual(error as? DirectIrohError, .cancelled, file: file, line: line)
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
