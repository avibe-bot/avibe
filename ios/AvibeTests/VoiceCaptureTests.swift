import ActivityKit
import XCTest
@testable import Avibe

/// The background voice take (`SpeakToMainBotIntent`): what the instance's
/// answers mean, and the take's states from press to the main Bot.
@MainActor
final class VoiceCaptureTests: XCTestCase {
    private typealias Failure = VoiceCaptureInstanceClient.Failure
    private typealias Response = VoiceCaptureInstanceClient.Response

    // MARK: Instance answers

    func testStatusReadsConsentAndRefusesAnInstanceThatNeverAsks() throws {
        func status(_ body: [String: Any]) throws -> VoiceCaptureInstanceClient.Status {
            try VoiceCaptureInstanceClient.parseStatus(Response(status: 200, json: body, redirected: false))
        }
        XCTAssertEqual(try status(["available": true, "consent": "granted"]).consent, .granted)
        XCTAssertEqual(try status(["available": true, "consent": "declined"]).consent, .declined)
        XCTAssertEqual(try status(["available": true, "consent": NSNull()]).consent, .notAsked)
        XCTAssertFalse(try status(["available": false, "consent": "granted"]).available)
        // No `consent` field: an Avibe from before the question. Never record.
        XCTAssertThrowsError(try status(["available": true])) {
            XCTAssertEqual($0 as? Failure, .olderAvibe)
        }
    }

    func testInstanceErrorsMapToWhatTheUserCanDo() {
        func failure(_ status: Int, _ error: String? = nil, redirected: Bool = false) -> Failure? {
            let json = error.map { ["error": $0] as [String: Any] }
            do {
                try VoiceCaptureInstanceClient.check(Response(status: status, json: json, redirected: redirected))
                return nil
            } catch {
                return error as? Failure
            }
        }
        XCTAssertNil(failure(200))
        XCTAssertEqual(failure(403, "voice_consent_required"), .consentRequired)
        XCTAssertEqual(failure(401), .signIn)
        XCTAssertEqual(failure(200, redirected: true), .signIn)
        XCTAssertEqual(failure(403, "remote_access_revoked"), .signIn)
        XCTAssertEqual(failure(403, "instance_access_forbidden"), .forbidden)
        XCTAssertEqual(failure(400, "asr_unavailable"), .unavailable)
        XCTAssertEqual(failure(0), .offline)
        XCTAssertEqual(failure(502), .offline)
        XCTAssertEqual(failure(422, "invalid_dictation"), .server(422, "invalid_dictation"))
    }

    // MARK: The take

    func testAPressRecordsAndASecondPressSendsTheWordsToTheMainBot() async {
        let world = World()
        let controller = world.controller()

        let started = await controller.toggle()
        XCTAssertEqual(started, .started)
        XCTAssertEqual(controller.phase, .listening)
        XCTAssertEqual(world.audio?.started, true)
        XCTAssertEqual(world.activities.first?.states.first?.phase, .listening)

        world.audio?.duration = 3
        let stopped = await controller.toggle()
        XCTAssertEqual(stopped, .stopped)
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertEqual(world.instance.transcribed, 1)
        XCTAssertEqual(world.instance.sent, ["hello"])
        XCTAssertEqual(world.activities.first?.ended?.phase, .sent)
        XCTAssertEqual(world.activities.first?.ended?.heard, "hello")
        XCTAssertEqual(world.audio?.discarded, true)
        XCTAssertEqual(world.instance.closed, true)
    }

    func testNothingIsRecordedWithoutTheAccountsYes() async {
        for consent in [VoiceCaptureInstanceClient.Status.Consent.notAsked, .declined] {
            let world = World()
            world.instance.statusResult = .success(.init(available: true, consent: consent))
            let outcome = await world.controller().toggle()
            XCTAssertEqual(outcome, .needsApp(.consent))
            XCTAssertNil(world.audio, "the microphone must not open")
            XCTAssertTrue(world.activities.isEmpty)
        }
    }

    func testAnOlderAvibeIsRefusedWithTheUpdateNotice() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.olderAvibe)
        let outcome = await world.controller().toggle()
        XCTAssertEqual(outcome, .stopped)
        XCTAssertNil(world.audio)
        XCTAssertEqual(world.activities.first?.states.first?.reason, .updateAvibe)
    }

    func testMissingPrerequisitesSendTheUserToTheApp() async {
        let signedOut = World()
        signedOut.target = nil
        let first = await signedOut.controller().toggle()
        XCTAssertEqual(first, .needsApp(.signIn))

        let noMicrophone = World()
        noMicrophone.microphone = false
        let second = await noMicrophone.controller().toggle()
        XCTAssertEqual(second, .needsApp(.microphone))

        let noActivities = World()
        noActivities.activitiesEnabled = false
        let third = await noActivities.controller().toggle()
        XCTAssertEqual(third, .needsApp(.liveActivities))
        XCTAssertNil(noActivities.audio)
    }

    func testCancelAndASlipSendNothing() async {
        let cancelled = World()
        let controller = cancelled.controller()
        _ = await controller.toggle()
        cancelled.audio?.duration = 3
        await controller.finish(send: false)
        XCTAssertTrue(cancelled.instance.sent.isEmpty)
        XCTAssertEqual(cancelled.audio?.discarded, true)
        XCTAssertEqual(controller.phase, .idle)

        let slip = World()
        let other = slip.controller()
        _ = await other.toggle()
        slip.audio?.duration = 0.2
        _ = await other.toggle()
        XCTAssertEqual(slip.instance.transcribed, 0)
    }

    func testAFailureBeforeTheMessageIsPostedIsKeptForRetryWithoutTranscribingAgain() async {
        let world = World()
        world.instance.pathFailures = 1
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertEqual(controller.phase, .failed)
        XCTAssertEqual(world.activities.first?.states.last?.reason, .offline)
        XCTAssertEqual(world.audio?.discarded, false, "kept for Retry")

        await controller.retry()
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertEqual(world.instance.transcribed, 1)
        XCTAssertEqual(world.instance.sent, ["hello"])
        XCTAssertEqual(world.activities.first?.ended?.phase, .sent)
    }

    func testAMessageWhosePostFailedIsNeverSentAgain() async {
        let world = World()
        world.instance.postFailures = 1
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        // The POST may have reached the instance; Retry would send it twice.
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertEqual(world.activities.first?.ended?.reason, .sendFailed)
        XCTAssertFalse(VoiceCaptureDetail.retryable(.sendFailed))
        XCTAssertEqual(world.instance.posts, 1)
        XCTAssertEqual(world.audio?.discarded, true)
    }

    func testAnAccountChangeStopsTheTakeAndKeepsNothing() async {
        let world = World()
        let controller = world.controller()
        _ = await controller.toggle()
        _ = controller.cancelForAccountTransition()
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertEqual(world.audio?.discarded, true)
        XCTAssertEqual(world.instance.closed, true)
        await controller.retry()
        XCTAssertTrue(world.instance.sent.isEmpty)

        // A start still waiting on the instance gives up.
        let waiting = World()
        waiting.instance.holdStatus = true
        let other = waiting.controller()
        let start = Task { await other.toggle() }
        while other.phase != .preparing { await Task.yield() }
        for _ in 0..<5 { await Task.yield() }
        _ = other.cancelForAccountTransition()
        waiting.instance.release()
        let outcome = await start.value
        XCTAssertEqual(outcome, .stopped)
        XCTAssertNil(waiting.audio, "nothing recorded for the old account")
        XCTAssertEqual(other.phase, .idle)
    }

    func testANoGivenSinceStopsWithoutRetry() async {
        let world = World()
        world.instance.transcribeResult = .failure(Failure.consentRequired)
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertEqual(world.activities.first?.ended?.phase, .needsApp)
        XCTAssertEqual(world.activities.first?.ended?.reason, .consent)
        XCTAssertEqual(world.audio?.discarded, true)
    }

    func testAPressWhileSendingIsIgnored() async {
        let world = World()
        world.instance.holdTranscribe = true
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 2
        let sending = Task { await controller.toggle() }
        while controller.phase != .sending { await Task.yield() }
        let busy = await controller.toggle()
        XCTAssertEqual(busy, .busy)
        world.instance.release()
        _ = await sending.value
        XCTAssertEqual(world.instance.sent, ["hello"])
    }
}

// MARK: Fakes

@MainActor
private final class World {
    var target: VoiceCaptureTarget? = VoiceCaptureTarget(
        accountID: "account-1",
        instanceID: "instance-1",
        origin: WebOrigin.parseConfigured("https://studio.example.invalid")!,
        storeIdentifier: UUID()
    )
    var microphone = true
    var activitiesEnabled = true
    let instance = FakeInstance()
    var audio: FakeAudio?
    var activities: [FakeActivity] = []

    func controller() -> VoiceCaptureController {
        VoiceCaptureController(environment: VoiceCaptureEnvironment(
            target: { self.target },
            makeInstance: { _ in self.instance },
            makeAudio: { _ in
                let audio = FakeAudio()
                self.audio = audio
                return audio
            },
            microphoneAllowed: { self.microphone },
            activitiesEnabled: { self.activitiesEnabled },
            requestActivity: { state in
                let activity = FakeActivity(state)
                self.activities.append(activity)
                return activity
            },
            now: Date.init,
            sleep: { _ in await Task.yield(); try? await Task.sleep(for: .seconds(3600)) }
        ))
    }
}

@MainActor
private final class FakeInstance: VoiceCaptureInstance {
    var statusResult: Result<VoiceCaptureInstanceClient.Status, Error> =
        .success(.init(available: true, consent: .granted))
    var transcribeResult: Result<String, Error> = .success("hello")
    var pathFailures = 0
    var postFailures = 0
    var posts = 0
    var holdTranscribe = false
    var holdStatus = false
    var transcribed = 0
    var sent: [String] = []
    var closed = false
    private var held: CheckedContinuation<Void, Never>?

    func status() async throws -> VoiceCaptureInstanceClient.Status {
        if holdStatus {
            await withCheckedContinuation { held = $0 }
        }
        return try statusResult.get()
    }

    func transcribe(audio: Data, name: String, mime: String) async throws -> String {
        if holdTranscribe {
            await withCheckedContinuation { held = $0 }
        }
        transcribed += 1
        return try transcribeResult.get()
    }

    func release() {
        held?.resume()
        held = nil
    }

    func mainBotMessagesPath() async throws -> String {
        if pathFailures > 0 {
            pathFailures -= 1
            throw VoiceCaptureInstanceClient.Failure.offline
        }
        return "/api/sessions/ses_main"
    }

    func postMessage(_ text: String, to sessionPath: String) async throws {
        posts += 1
        if postFailures > 0 {
            postFailures -= 1
            throw VoiceCaptureInstanceClient.Failure.offline
        }
        sent.append(text)
    }

    func close() { closed = true }
    var releaseBarrier: WebsiteDataStoreReleaseBarrier? { nil }
}

private final class FakeAudio: VoiceCaptureAudio {
    let url: URL
    var started = false
    var discarded = false
    var duration: TimeInterval = 0

    init() {
        url = FileManager.default.temporaryDirectory.appending(path: "voice-\(UUID().uuidString).m4a")
        try? Data([0, 1, 2]).write(to: url)
    }

    func start() throws { started = true }
    func stop() -> TimeInterval { duration }
    func discard() { discarded = true }
}

@MainActor
private final class FakeActivity: VoiceCaptureActivityHandle {
    var states: [VoiceCaptureAttributes.ContentState]
    var ended: VoiceCaptureAttributes.ContentState?

    init(_ state: VoiceCaptureAttributes.ContentState) {
        states = [state]
    }

    func update(_ state: VoiceCaptureAttributes.ContentState) async { states.append(state) }

    func end(_ state: VoiceCaptureAttributes.ContentState?, dismissal: ActivityUIDismissalPolicy) async {
        ended = state ?? states.last
    }
}
