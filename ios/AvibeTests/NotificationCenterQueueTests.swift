import UserNotifications
import XCTest
@testable import Avibe

/// The App's calls to the notification center run off the main thread, in
/// call order; and the unit tests' host starts nothing live.
final class NotificationCenterQueueTests: XCTestCase {
    /// The unsent notice's schedule and removal, made while the queue is
    /// still busy: they reach the center in call order, none on the main
    /// thread, and a removal never overtakes the schedule it clears.
    @MainActor
    func testTheUnsentNoticeIsScheduledAndRemovedInCallOrderOffTheMainThread() async {
        let queue = NotificationCenterQueue(label: "NotificationCenterQueueTests.\(UUID().uuidString)")
        let recorder = CenterRecorder()
        let center = VoiceCaptureFailureNotification.Center(
            add: { request, _ in
                recorder.record("add \(request.identifier)")
                if let trigger = request.trigger as? UNTimeIntervalNotificationTrigger {
                    recorder.recordInterval(trigger.timeInterval)
                }
            },
            remove: { identifiers in recorder.record("remove \(identifiers.joined(separator: ","))") }
        )
        // Hold the queue so every call below waits behind it.
        let held = DispatchSemaphore(value: 0)
        queue.run { held.wait() }

        let later = Date().addingTimeInterval(3_600)
        VoiceCaptureFailureNotification.scheduleUnsent(at: later, queue: queue, center: center)
        VoiceCaptureFailureNotification.scheduleUnsent(at: nil, queue: queue, center: center)
        VoiceCaptureFailureNotification.scheduleUnsent(at: later, queue: queue, center: center)
        VoiceCaptureFailureNotification.post(
            VoiceCaptureAttributes.ContentState(phase: .failed, startedAt: Date(), reason: .recordingFailed),
            queue: queue,
            center: center
        )
        VoiceCaptureFailureNotification.scheduleUnsent(at: nil, queue: queue, center: center)
        // Nothing reached the center while the queue was held.
        XCTAssertEqual(recorder.calls, [])

        let releasedAt = Date()
        held.signal()
        let _: Void = await queue.value { answer in answer(()) }
        XCTAssertEqual(
            recorder.calls,
            [
                "add voice-capture-unsent",
                "remove voice-capture-unsent",
                "add voice-capture-unsent",
                "add voice-capture-failure",
                "remove voice-capture-unsent",
            ]
        )
        XCTAssertEqual(recorder.onMainThread, [false, false, false, false, false])
        // The unsent notice's delay was taken when the queue reached it,
        // after the hold, not when it was asked for: the time it waited is
        // not added to it.
        XCTAssertEqual(recorder.intervals.count, 2)
        for interval in recorder.intervals {
            XCTAssertLessThanOrEqual(interval, later.timeIntervalSince(releasedAt))
        }
    }

    /// `value` answers after every call submitted before it, off the main
    /// thread.
    @MainActor
    func testAValueWaitsForEveryEarlierCall() async {
        let queue = NotificationCenterQueue(label: "NotificationCenterQueueTests.\(UUID().uuidString)")
        let recorder = CenterRecorder()
        queue.run { recorder.record("first") }
        queue.run { recorder.record("second") }
        let seen: [String] = await queue.value { answer in
            recorder.record("value")
            answer(recorder.calls)
        }
        XCTAssertEqual(seen, ["first", "second", "value"])
        XCTAssertEqual(recorder.onMainThread, [false, false, false])
    }

    /// The tests' host starts no live model, Health sync or App Lock; an
    /// App outside tests starts all three. This process is the host.
    func testTheTestHostStartsNothingLive() {
        XCTAssertEqual(
            LaunchServices.forProcess(runningTests: true),
            LaunchServices(liveModel: false, healthSync: false, appLock: false)
        )
        XCTAssertEqual(
            LaunchServices.forProcess(runningTests: false),
            LaunchServices(liveModel: true, healthSync: true, appLock: true)
        )
        XCTAssertEqual(LaunchServices.current, LaunchServices.forProcess(runningTests: true))
    }

    /// A model that does not start at launch (the host's) sets up nothing
    /// that runs: it does not take the voice capture's load gate, which a
    /// started model sets synchronously in its init, in the same step that
    /// starts its account, push, lifecycle and bootstrap work.
    @MainActor
    func testAModelThatDoesNotStartLeavesEverythingIdle() throws {
        let earlier = VoiceCaptureLoadGate.provider
        defer { VoiceCaptureLoadGate.provider = earlier }
        VoiceCaptureLoadGate.provider = nil
        let suiteName = "NotificationCenterQueueTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let scheduleDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("NotificationCenterQueue.\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: scheduleDirectory) }

        let model = AppModel(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(
                testBaseURL: try XCTUnwrap(URL(string: "http://127.0.0.1:9"))
            ),
            pushKeychain: InMemoryPushKeychainStore(),
            pushGenerationStore: InMemoryPushGenerationStore(),
            pushInstallationMarker: InMemoryPushInstallationMarkerStore(),
            systemScheduleStore: FileSystemSystemScheduleStore(
                fileURL: scheduleDirectory.appendingPathComponent("ledger.json")
            ),
            startsAtLaunch: false
        )
        XCTAssertNil(VoiceCaptureLoadGate.provider)
        withExtendedLifetime(model) {}
    }
}

/// What a stand-in center was asked, in order, and from which thread.
private final class CenterRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [String] = []
    private var threads: [Bool] = []
    private var triggerIntervals: [TimeInterval] = []

    func recordInterval(_ interval: TimeInterval) {
        lock.lock()
        triggerIntervals.append(interval)
        lock.unlock()
    }

    var intervals: [TimeInterval] {
        lock.lock()
        defer { lock.unlock() }
        return triggerIntervals
    }

    func record(_ call: String) {
        lock.lock()
        recorded.append(call)
        threads.append(Thread.isMainThread)
        lock.unlock()
    }

    var calls: [String] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    var onMainThread: [Bool] {
        lock.lock()
        defer { lock.unlock() }
        return threads
    }
}
