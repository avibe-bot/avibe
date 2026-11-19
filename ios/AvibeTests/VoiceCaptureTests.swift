import ActivityKit
import AppIntents
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

    /// Recording starts before the instance answers, so no word is lost;
    /// without the account's yes the take is discarded unsent (5.1.2(i)).
    func testWithoutTheAccountsYesTheTakeIsDiscardedUnsent() async {
        for consent in [VoiceCaptureInstanceClient.Status.Consent.notAsked, .declined] {
            let world = World()
            world.instance.statusResult = .success(.init(available: true, consent: consent))
            let controller = world.controller()
            let outcome = await controller.toggle()
            XCTAssertEqual(outcome, .started)
            await controller.consentWatch?.value
            XCTAssertEqual(controller.phase, .idle)
            XCTAssertEqual(world.audio?.discarded, true)
            XCTAssertEqual(world.instance.transcribed, 0, "nothing leaves the phone")
            XCTAssertEqual(world.instance.posts, 0)
            XCTAssertTrue(world.activities.first?.endedImmediately == true)
            XCTAssertEqual(world.activities.last?.states.first?.phase, .needsApp)
            XCTAssertEqual(world.activities.last?.states.first?.reason, .consent)
        }
    }

    /// A stop before the answer waits for it: a no sends nothing.
    func testAStopBeforeANoSendsNothing() async {
        let world = World()
        world.instance.statusResult = .success(.init(available: true, consent: .declined))
        world.instance.holdStatus = true
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 3
        while !world.instance.isHolding { await Task.yield() }
        let stop = Task { await controller.toggle() }
        while controller.phase != .sending { await Task.yield() }
        world.instance.release()
        _ = await stop.value
        await controller.consentWatch?.value
        XCTAssertEqual(world.instance.transcribed, 0)
        XCTAssertEqual(world.instance.posts, 0)
        XCTAssertEqual(world.audio?.discarded, true)
        XCTAssertEqual(controller.phase, .idle)
    }

    /// A stop before a yes waits for it, then sends.
    func testAStopBeforeTheAnswerWaitsForItThenSends() async {
        let world = World()
        world.instance.holdStatus = true
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 3
        while !world.instance.isHolding { await Task.yield() }
        let stop = Task { await controller.toggle() }
        while controller.phase != .sending { await Task.yield() }
        for _ in 0..<5 { await Task.yield() }
        XCTAssertEqual(world.instance.transcribed, 0, "not before the yes")
        world.instance.release()
        _ = await stop.value
        XCTAssertEqual(world.instance.sent, ["hello"])
    }

    /// In front, a no is said in the App (the chat asks), not on the island.
    func testANoInFrontIsSaidInTheApp() async {
        let world = World()
        world.inFront = true
        world.instance.statusResult = .success(.init(available: true, consent: .notAsked))
        let controller = world.controller()
        let reserved = await controller.beginPress()
        XCTAssertNil(reserved)
        _ = await controller.startTake(inFront: true)
        await controller.consentWatch?.value
        XCTAssertEqual(world.presented.map(\.0), [.consent])
        XCTAssertEqual(world.presented.map(\.1), [true])
        XCTAssertEqual(world.activities.count, 1, "no notice island in front")
        XCTAssertEqual(world.audio?.discarded, true)
    }

    func testAnOlderAvibeIsRefusedWithTheUpdateNotice() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.olderAvibe)
        let controller = world.controller()
        _ = await controller.toggle()
        await controller.consentWatch?.value
        XCTAssertEqual(world.audio?.discarded, true)
        XCTAssertEqual(world.instance.transcribed, 0)
        XCTAssertEqual(world.activities.last?.states.first?.reason, .updateAvibe)
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
        XCTAssertEqual(world.activities.first?.ended?.reason, .sendUnknown, "it may have arrived")
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

        // A take still waiting on the instance's answer is given up too,
        // and the answer, when it comes, does nothing.
        let waiting = World()
        waiting.instance.holdStatus = true
        let other = waiting.controller()
        _ = await other.toggle()
        while !waiting.instance.isHolding { await Task.yield() }
        _ = other.cancelForAccountTransition()
        waiting.instance.release()
        await other.consentWatch?.value
        XCTAssertEqual(waiting.audio?.discarded, true, "nothing kept for the old account")
        XCTAssertEqual(waiting.instance.transcribed, 0)
        XCTAssertEqual(other.phase, .idle)

        // A press reserved while Avibe came forward gives up as well.
        let coming = World()
        let third = coming.controller()
        let reserved = await third.beginPress()
        XCTAssertNil(reserved)
        _ = third.cancelForAccountTransition()
        let late = await third.startTake(inFront: true)
        XCTAssertEqual(late, .stopped)
        XCTAssertNil(coming.audio)
    }

    // MARK: A take whose process is gone (the owner's 11-17 device test)

    /// Start, then the App is killed, then a press: the island the gone
    /// process left is ended (said to be interrupted) instead of the press
    /// bringing Avibe forward, and the press after that records again.
    func testStartThenKillThenPressEndsTheLeftoverAndThenRecordsAgain() async {
        let world = World()
        let before = world.controller()
        _ = await before.toggle()
        let leftover = try! XCTUnwrap(world.activities.first)
        // The process is killed: its controller and recorder are gone, the
        // system keeps the Live Activity.
        world.leftovers = [leftover]
        world.activities = []
        world.audio = nil
        let discardedBefore = world.leftoverAudioDiscarded
        let after = world.controller()

        let first = await after.toggle()
        XCTAssertEqual(first, .stopped, "never .needsApp: the App must not come forward")
        XCTAssertEqual(leftover.ended?.phase, .failed)
        XCTAssertEqual(leftover.ended?.reason, .interrupted)
        XCTAssertNil(world.audio, "the ending press records nothing")
        XCTAssertEqual(world.leftoverAudioDiscarded, discardedBefore + 1, "its audio is deleted")

        let second = await after.toggle()
        XCTAssertEqual(second, .started)
        XCTAssertEqual(after.phase, .listening)
    }

    /// A press that cold-launches the App after the kill: launch cleanup may
    /// end the leftover before the intent runs; that press still only ends it.
    func testThePressThatLaunchesTheAppEndsTheLeftoverEvenIfLaunchCleanupRanFirst() async {
        let world = World()
        world.leftovers = [FakeActivity(.init(phase: .listening, startedAt: Date()))]
        let controller = world.controller()
        await controller.endLeftoversAtLaunch()
        let press = await controller.toggle()
        XCTAssertEqual(press, .stopped)
        XCTAssertNil(world.audio)
        let next = await controller.toggle()
        XCTAssertEqual(next, .started)
    }

    func testAnOrdinaryLaunchDoesNotSwallowALaterPress() async {
        let world = World()
        world.leftovers = [FakeActivity(.init(phase: .listening, startedAt: Date()))]
        var clock = Date()
        world.now = { clock }
        let controller = world.controller()
        await controller.endLeftoversAtLaunch()
        clock = clock.addingTimeInterval(60)
        let press = await controller.toggle()
        XCTAssertEqual(press, .started)
    }

    func testASendingLeftoverSaysItMayHaveArrived() async {
        let world = World()
        let leftover = FakeActivity(.init(phase: .sending, startedAt: Date()))
        world.leftovers = [leftover]
        await world.controller().endLeftoversAtLaunch()
        XCTAssertEqual(leftover.ended?.phase, .needsApp)
        XCTAssertEqual(leftover.ended?.reason, .sendUnknown)
    }

    func testLaunchEndsALeftoverIsland() async {
        let world = World()
        let leftover = FakeActivity(.init(phase: .listening, startedAt: Date()))
        world.leftovers = [leftover]
        await world.controller().endLeftoversAtLaunch()
        XCTAssertEqual(leftover.ended?.reason, .interrupted)
    }

    func testTheIslandsButtonsEndATakeNoProcessHolds() async {
        let cancel = World()
        let cancelled = FakeActivity(.init(phase: .listening, startedAt: Date()))
        cancel.leftovers = [cancelled]
        await cancel.controller().finish(send: false)
        XCTAssertTrue(cancelled.endedImmediately)

        let send = World()
        let sent = FakeActivity(.init(phase: .listening, startedAt: Date()))
        send.leftovers = [sent]
        await send.controller().finish(send: true)
        XCTAssertEqual(sent.ended?.reason, .interrupted, "nothing to send: say it was interrupted")
        XCTAssertTrue(send.instance.sent.isEmpty)
    }

    func testALeftoverNeverTouchesTheTakeThisProcessHolds() async {
        let world = World()
        let controller = world.controller()
        _ = await controller.toggle()
        await controller.endLeftoversAtLaunch()
        XCTAssertNil(world.activities.first?.ended)
        XCTAssertEqual(controller.phase, .listening)
    }

    /// The owner's 11-18 report: permission granted, yet the island said to
    /// allow the microphone. A recorder that will not start is not a
    /// permission problem; say what happened, with the system's code.
    func testARecorderThatWillNotStartSaysSoWithItsCodeNotThePermissionLine() async {
        let world = World()
        // AVAudioSession.ErrorCode.cannotStartRecording ('!rec').
        world.recorderStartError = NSError(domain: NSOSStatusErrorDomain, code: 561145187)
        let outcome = await world.controller().toggle()
        XCTAssertEqual(outcome, .stopped)
        // The last try's island carries the line (try 1 starts a fresh one).
        let shown = world.activities.last?.ended
        XCTAssertEqual(shown?.phase, .failed)
        XCTAssertEqual(shown?.reason, .recordingFailed)
        XCTAssertEqual(shown?.errorCode, 561145187)
    }

    /// `record()` returning false is Avibe's own error: no number that
    /// would look like a system code.
    func testARecorderThatWouldNotRecordShowsNoMadeUpCode() async {
        let world = World()
        world.recorderStartError = VoiceCaptureRecorder.Failure.start
        _ = await world.controller().toggle()
        XCTAssertEqual(world.activities.last?.ended?.reason, .recordingFailed)
        XCTAssertNil(world.activities.last?.ended?.errorCode)
    }

    func testADoubledRunAfterAnyFailureNoticeShowsOneNotice() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.olderAvibe)
        let controller = world.controller()
        _ = await controller.toggle()
        await controller.consentWatch?.value
        let again = await controller.toggle()
        XCTAssertEqual(again, .stopped)
        XCTAssertEqual(world.activities.filter { $0.states.first?.reason == .updateAvibe }.count, 1)
    }

    /// An unreachable instance is not a no: the take records on, is kept on
    /// the phone unsent, and Retry asks again before anything is sent.
    func testAnUnreachableInstanceKeepsTheTakeForRetryUnsent() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.offline)
        let controller = world.controller()
        _ = await controller.toggle()
        await controller.consentWatch?.value
        XCTAssertEqual(controller.phase, .listening, "still recording")
        world.audio?.duration = 3
        _ = await controller.toggle()
        XCTAssertEqual(controller.phase, .failed)
        XCTAssertEqual(world.instance.transcribed, 0, "nothing sent before a yes")
        XCTAssertEqual(world.audio?.discarded, false, "kept for Retry")
        XCTAssertEqual(world.activities.first?.states.last?.reason, .offline)
        // Let any answer asked early settle while still unreachable.
        for _ in 0..<20 { await Task.yield() }

        // Retry asks again then, not with an answer taken during the outage.
        world.instance.statusResult = .success(.init(available: true, consent: .granted))
        await controller.retry()
        XCTAssertEqual(world.instance.sent, ["hello"])
    }

    /// Unreachable when recording began, reachable at the stop: it sends then.
    func testAnInstanceUnreachableAtTheStartButReachableAtTheStopSends() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.offline)
        let controller = world.controller()
        _ = await controller.toggle()
        await controller.consentWatch?.value
        world.instance.statusResult = .success(.init(available: true, consent: .granted))
        world.audio?.duration = 3
        _ = await controller.toggle()
        XCTAssertEqual(world.instance.sent, ["hello"])
    }

    /// A no deletes the take's file; the launch sweep clears leftovers.
    func testANoDeletesTheTakesFileAndTheLaunchSweepClearsLeftovers() throws {
        let url = try VoiceCaptureRecorder.takeURL(id: UUID())
        try Data([1, 2, 3]).write(to: url)
        let recorder = VoiceCaptureRecorder(url: url, session: FakeSession(), permission: { true }, makeEngine: { _ in FakeEngine() })
        recorder.discard()
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))

        let leftover = try VoiceCaptureRecorder.takeURL(id: UUID())
        try Data([4, 5, 6]).write(to: leftover)
        VoiceCaptureRecorder.discardAll()
        XCTAssertFalse(FileManager.default.fileExists(atPath: leftover.path))
    }

    func testTakesAreExcludedFromBackup() throws {
        let url = try VoiceCaptureRecorder.takeURL(id: UUID())
        let values = try url.deletingLastPathComponent().resourceValues(forKeys: [.isExcludedFromBackupKey])
        XCTAssertEqual(values.isExcludedFromBackup, true)
    }

    func testATakeFileThatCannotBeMadeIsNotAPermissionProblemEither() async {
        let world = World()
        world.takeFileFails = true
        let outcome = await world.controller().toggle()
        XCTAssertEqual(outcome, .stopped)
        XCTAssertEqual(world.activities.first?.states.first?.reason, .recordingFailed)
        XCTAssertNotNil(world.activities.first?.states.first?.errorCode)
    }

    /// Two runs for one press (11-18: two identical notices): a run within a
    /// second of a start that failed does nothing, so one notice shows.
    func testASecondRunRightAfterAFailedStartPostsNoSecondNotice() async {
        let world = World()
        world.recorderStartError = NSError(domain: NSOSStatusErrorDomain, code: 561145187)
        var clock = Date()
        world.now = { clock }
        let controller = world.controller()
        _ = await controller.toggle()
        let islands = world.activities.count
        clock = clock.addingTimeInterval(0.4)
        let second = await controller.toggle()
        XCTAssertEqual(second, .stopped)
        XCTAssertEqual(world.activities.count, islands, "nothing more for the same press")
        XCTAssertEqual(world.failureNotices.count, 1, "one notice")

        // A press a moment later is a new try.
        clock = clock.addingTimeInterval(2)
        _ = await controller.toggle()
        XCTAssertGreaterThan(world.activities.count, islands)
    }

    func testAnIslandThatCannotStartRecordsNothingAndSaysWhy() async {
        let world = World()
        world.activityRequestFails = true
        let outcome = await world.controller().toggle()
        XCTAssertEqual(outcome, .needsApp(.activityFailed))
        XCTAssertEqual(world.audio?.started, false, "no island, no recording")
        XCTAssertEqual(world.audio?.discarded, true)
    }

    func testAStaleIslandShowsTheTakeAsInterruptedNotARunningTimer() {
        let start = Date(timeIntervalSince1970: 0)
        let listening = VoiceCaptureShown(state: .init(phase: .listening, startedAt: start), isStale: true).state
        XCTAssertEqual(listening.phase, .failed)
        XCTAssertEqual(listening.reason, .interrupted)
        let sending = VoiceCaptureShown(state: .init(phase: .sending, startedAt: start), isStale: true).state
        XCTAssertEqual(sending.reason, .sendUnknown, "a stale send may have arrived")
        let fresh = VoiceCaptureShown(state: .init(phase: .listening, startedAt: start), isStale: false).state
        XCTAssertEqual(fresh.phase, .listening)
    }

    func testTheIntentMayStartItsIslandFromTheBackgroundAndKeepRecording() throws {
        guard #available(iOS 18.0, *) else { throw XCTSkip("iOS 18") }
        // The sample (11-19): only an AudioRecordingIntent that is also a
        // LiveActivityIntent may start its island from the background;
        // without it the request fails with `visibility`, as on 37.1.
        XCTAssertTrue((SpeakToMainBotIntent.self as Any) is any AudioRecordingIntent.Type)
        XCTAssertTrue((SpeakToMainBotIntent.self as Any) is any LiveActivityIntent.Type)
        // Recording continues in the background only with the audio mode
        // (Apple: AVAudioSession.Category.record).
        let modes = Bundle.main.object(forInfoDictionaryKey: "UIBackgroundModes") as? [String]
        XCTAssertEqual(modes, ["audio"])
    }

    /// Background audio is held only while a take records: every way a take
    /// ends stops its recorder (whose `stop()` releases the session, below).
    func testEveryWayATakeEndsStopsItsRecorder() async {
        func recording(_ setup: (World) -> Void = { _ in }) async -> (World, VoiceCaptureController) {
            let world = World()
            setup(world)
            let controller = world.controller()
            _ = await controller.toggle()
            world.audio?.duration = 2
            return (world, controller)
        }
        var (world, controller) = await recording()
        _ = await controller.toggle()
        XCTAssertGreaterThan(world.audio?.stops ?? 0, 0, "send")

        (world, controller) = await recording()
        await controller.finish(send: false)
        XCTAssertGreaterThan(world.audio?.stops ?? 0, 0, "cancel")

        (world, controller) = await recording { $0.instance.pathFailures = 1 }
        _ = await controller.toggle()
        XCTAssertGreaterThan(world.audio?.stops ?? 0, 0, "failure")

        (world, controller) = await recording()
        _ = controller.cancelForAccountTransition()
        XCTAssertGreaterThan(world.audio?.stops ?? 0, 0, "account change")
    }

    /// The real recorder: the session is active only between a start that
    /// recorded and the first stop, and a start that fails leaves it off.
    func testTheRecorderHoldsTheAudioSessionOnlyWhileRecording() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "take-\(UUID().uuidString).m4a")
        let session = FakeSession()
        let engine = FakeEngine()
        let recorder = VoiceCaptureRecorder(url: url, session: session, permission: { true }, makeEngine: { _ in engine })
        XCTAssertThrowsError(try recorder.start(), "no prepared session")
        XCTAssertEqual(session.activations, 0)
        try recorder.prepare(.exclusiveRecord)
        try recorder.start()
        XCTAssertTrue(session.active)
        recorder.stop()
        XCTAssertFalse(session.active)
        recorder.stop()
        recorder.discard()
        XCTAssertEqual(session.deactivations, 1, "released exactly once")

        let refusing = FakeEngine()
        refusing.records = false
        let refused = FakeSession()
        let refusedRecorder = VoiceCaptureRecorder(url: url, session: refused, permission: { true }, makeEngine: { _ in refusing })
        try refusedRecorder.prepare(.exclusiveRecord)
        XCTAssertThrowsError(try refusedRecorder.start())
        XCTAssertFalse(refused.active, "a recorder that would not record")

        let broken = FakeSession()
        let brokenRecorder = VoiceCaptureRecorder(
            url: url, session: broken, permission: { true }, makeEngine: { _ in throw CancellationError() }
        )
        try brokenRecorder.prepare(.exclusiveRecord)
        XCTAssertThrowsError(try brokenRecorder.start())
        XCTAssertFalse(broken.active, "a recorder that could not be made")

        let discarded = FakeSession()
        let other = VoiceCaptureRecorder(url: url, session: discarded, permission: { true }, makeEngine: { _ in FakeEngine() })
        try other.prepare(.exclusiveRecord)
        try other.start()
        other.discard()
        XCTAssertFalse(discarded.active, "discard")

        let denied = FakeSession()
        XCTAssertThrowsError(try VoiceCaptureRecorder(
            url: url, session: denied, permission: { false }, makeEngine: { _ in FakeEngine() }
        ).prepare(.exclusiveRecord))
        XCTAssertEqual(denied.activations, 0)
    }

    // MARK: One mixable background try, then in front (11-20: two projects
    // record from the background with a mixable first activation in a
    // process the press launched; 38.1 was refused in a warm process)

    /// No word is lost to the instance's round trip: recording starts
    /// before the instance is asked (on master it was asked first). In the
    /// background exactly one try: the island, the mixable session, the
    /// recorder.
    func testAPressRecordsBeforeAskingTheInstanceWithOneMixableTry() async {
        let world = World()
        let controller = world.controller()
        let outcome = await controller.toggle()
        XCTAssertEqual(outcome, .started)
        await controller.consentWatch?.value
        XCTAssertEqual(world.events, ["activity", "prepare2", "record", "announce", "status"])
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertEqual(world.instance.captures.first ?? nil, "0 [background warm bgTries=0]")
    }

    /// The background try refused: its island goes at once, nothing stays
    /// active, and the press goes on in front, where it records; the message
    /// names the refusal.
    func testARefusedBackgroundTryGoesOnInFrontAndRecords() async {
        let world = World()
        world.prepareErrors = [.mixableBackground: VoiceCaptureRecorder.StartFailure(
            step: .sessionActivate, underlying: NSError(domain: NSOSStatusErrorDomain, code: 561015905)
        )]
        let controller = world.controller()
        let reserved = await controller.beginPress()
        XCTAssertNil(reserved)
        let background = await controller.startTake(inFront: false, foregroundFallback: true)
        XCTAssertEqual(background, .needsForeground)
        XCTAssertEqual(controller.phase, .preparing, "still reserved")
        XCTAssertEqual(world.activities.first?.endedImmediately, true)
        XCTAssertTrue(world.announced.isEmpty)
        XCTAssertTrue(world.failureNotices.isEmpty)
        let front = await controller.startTake(inFront: true)
        XCTAssertEqual(front, .started)
        XCTAssertEqual(world.announced, [true])
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertEqual(
            world.instance.captures.first ?? nil,
            "3 [background warm bgTries=0] (0 sessionActivate 561015905)"
        )
    }

    /// The process counts its background activations (iOS allows one).
    func testTheMessageCountsThisProcesssEarlierBackgroundTries() async {
        let world = World()
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertEqual(world.instance.captures.last ?? nil, "0 [background warm bgTries=1]")
    }

    /// Avibe could not come forward: one notice names the refused try.
    func testWhenAvibeCannotComeForwardTheNoticeNamesTheRefusal() async {
        let world = World()
        world.prepareErrors = [.mixableBackground: NSError(domain: NSOSStatusErrorDomain, code: 561015905)]
        let controller = world.controller()
        _ = await controller.beginPress()
        _ = await controller.startTake(inFront: false, foregroundFallback: true)
        controller.foregroundUnavailable()
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertEqual(world.failureNotices.count, 1)
        XCTAssertEqual(world.failureNotices.first?.errorText, "561015905")
    }

    // MARK: Variant (a): end the process after a background capture

    func testTheProcessEndsAfterASentBackgroundCaptureOnlyBehindTheSwitch() async {
        let off = World()
        let first = off.controller()
        _ = await first.toggle()
        off.audio?.duration = 2
        _ = await first.toggle()
        await first.endingTask?.value
        XCTAssertEqual(off.ended, 0, "the switch is off by default")
        XCTAssertEqual(off.endDecisions, ["off"])

        let on = World()
        on.endSwitch = true
        let second = on.controller()
        _ = await second.toggle()
        on.audio?.duration = 2
        _ = await second.toggle()
        await second.endingTask?.value
        XCTAssertEqual(on.instance.sent, ["hello"], "only after the message was accepted")
        XCTAssertEqual(on.audio?.discarded, true)
        XCTAssertEqual(on.ended, 1)
        XCTAssertEqual(on.endDecisions, ["ended"])
        XCTAssertEqual(on.held, 1, "kept running while it waits")
        XCTAssertEqual(on.released, 1)
    }

    /// A wait that ran late (the process was suspended; the next wake may be
    /// the next press) does not end the process.
    func testALateWaitNeverEndsTheProcess() async {
        let world = World()
        world.endSwitch = true
        var clock = Date()
        world.now = { clock }
        world.beforeEnding = { clock = clock.addingTimeInterval(VoiceCaptureController.endingWindow + 1) }
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        await controller.endingTask?.value
        XCTAssertEqual(world.ended, 0)
        XCTAssertEqual(world.endDecisions, ["skip:late"])
        XCTAssertEqual(world.released, 1)
    }

    /// An older intent resuming after a newer press reserved cannot take it.
    func testAnOlderIntentCannotTakeANewerPress() async {
        let world = World()
        world.prepareErrors = [.mixableBackground: NSError(domain: NSOSStatusErrorDomain, code: 561015905)]
        var clock = Date()
        world.now = { clock }
        let controller = world.controller()
        _ = await controller.beginPress()
        let old = controller.reservation
        _ = await controller.startTake(inFront: false, foregroundFallback: true, reservation: old)
        clock = clock.addingTimeInterval(VoiceCaptureController.foregroundWait + 1)
        _ = await controller.beginPress()
        XCTAssertNotEqual(controller.reservation, old)
        let late = await controller.startTake(inFront: true, reservation: old)
        XCTAssertEqual(late, .stopped)
        XCTAssertEqual(controller.phase, .preparing, "the newer press keeps its reservation")
    }

    func testTheProcessNeverEndsWhenShownInFrontOrBusy() async {
        for setup in [{ (w: World) in w.shown = true }, { (w: World) in w.otherWork = "push" }] {
            let world = World()
            world.endSwitch = true
            setup(world)
            let controller = world.controller()
            _ = await controller.toggle()
            world.audio?.duration = 2
            _ = await controller.toggle()
            await controller.endingTask?.value
            XCTAssertEqual(world.ended, 0)
            XCTAssertTrue(world.endDecisions.first?.hasPrefix("skip:") == true)
        }

        // A take recorded in front never ends the process.
        let front = World()
        front.endSwitch = true
        let controller = front.controller()
        _ = await controller.beginPress()
        _ = await controller.startTake(inFront: true)
        front.audio?.duration = 2
        _ = await controller.toggle()
        await controller.endingTask?.value
        XCTAssertEqual(front.ended, 0)
        XCTAssertTrue(front.endDecisions.isEmpty)

        // Nor a take that failed (it is kept for Retry).
        let failed = World()
        failed.endSwitch = true
        failed.instance.pathFailures = 1
        let other = failed.controller()
        _ = await other.toggle()
        failed.audio?.duration = 2
        _ = await other.toggle()
        XCTAssertEqual(failed.ended, 0)
    }

    func testTheSwitchIsAHiddenURLOnly() {
        XCTAssertFalse(VoiceCaptureEndSwitch.handle(URL(string: "https://avibe.bot/voice/end-after-background?on=1")!))
        XCTAssertFalse(VoiceCaptureEndSwitch.handle(URL(string: "avibe-diagnostics://voice/other?on=1")!))
        XCTAssertFalse(
            VoiceCaptureEndSwitch.handle(URL(string: "avibe-diagnostics://voice/end-after-background?on=1")!, allowed: false),
            "an App Store build ignores it"
        )
        let defaults = UserDefaults.standard
        let before = defaults.object(forKey: VoiceCaptureEndSwitch.key)
        defer { defaults.set(before, forKey: VoiceCaptureEndSwitch.key) }
        XCTAssertTrue(VoiceCaptureEndSwitch.handle(URL(string: "avibe-diagnostics://voice/end-after-background?on=1")!))
        XCTAssertTrue(VoiceCaptureEndSwitch.isOn)
        XCTAssertTrue(VoiceCaptureEndSwitch.handle(URL(string: "avibe-diagnostics://voice/end-after-background?on=0")!))
        XCTAssertFalse(VoiceCaptureEndSwitch.isOn)
    }

    /// A reservation whose intent ended without coming forward (never
    /// `startTake(inFront:)` nor `foregroundUnavailable()`) expires on the
    /// next press after the wait.
    func testAnAbandonedReservationExpiresOnTheNextPress() async {
        let world = World()
        world.prepareErrors = [.mixableBackground: NSError(domain: NSOSStatusErrorDomain, code: 561015905)]
        var clock = Date()
        world.now = { clock }
        let controller = world.controller()
        _ = await controller.beginPress()
        _ = await controller.startTake(inFront: false, foregroundFallback: true)
        let soon = await controller.beginPress()
        XCTAssertEqual(soon, .busy)
        clock = clock.addingTimeInterval(VoiceCaptureController.foregroundWait + 1)
        world.prepareErrors = [:]
        let later = await controller.toggle()
        XCTAssertEqual(later, .started)
    }

    /// A press that waited too long to come forward never records.
    func testAPressWaitingTooLongForTheAppNeverRecords() async {
        let world = World()
        world.prepareErrors = [.mixableBackground: NSError(domain: NSOSStatusErrorDomain, code: 561015905)]
        var clock = Date()
        world.now = { clock }
        let controller = world.controller()
        _ = await controller.beginPress()
        _ = await controller.startTake(inFront: false, foregroundFallback: true)
        clock = clock.addingTimeInterval(VoiceCaptureController.foregroundWait + 1)
        let late = await controller.startTake(inFront: true)
        XCTAssertEqual(late, .stopped)
        XCTAssertTrue(world.announced.isEmpty)
        XCTAssertEqual(world.failureNotices.count, 1)
    }

    /// In front: once Avibe is active, at once the island, the session and
    /// the recorder; only then the haptic and the line, then the instance.
    func testInFrontItRecordsAsSoonAsActiveAndSaysSoOnlyThen() async {
        let world = World()
        let controller = world.controller()
        let reserved = await controller.beginPress()
        XCTAssertNil(reserved)
        XCTAssertEqual(controller.phase, .preparing)
        let outcome = await controller.startTake(inFront: true)
        XCTAssertEqual(outcome, .started)
        await controller.consentWatch?.value
        XCTAssertEqual(world.events, ["active", "activity", "prepare1", "record", "announce", "status"])
        XCTAssertEqual(world.announced, [true])
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertEqual(world.instance.captures.first ?? nil, "3 [background warm bgTries=0]")
    }

    /// The recording line and haptic never come before capture really
    /// starts, and never for a start that fails.
    func testNoRecordingSignalWithoutCapture() async {
        let refused = World()
        refused.prepareErrors = [.mixableBackground: NSError(domain: NSOSStatusErrorDomain, code: 561015905)]
        _ = await refused.controller().toggle()
        XCTAssertTrue(refused.announced.isEmpty)

        let noIsland = World()
        noIsland.activityRequestFails = true
        _ = await noIsland.controller().toggle()
        XCTAssertTrue(noIsland.announced.isEmpty)
    }

    /// A reserved press is busy to a second run; a stop is never reserved.
    func testAStartingPressIsReservedAndAStopIsNot() async {
        let world = World()
        let controller = world.controller()
        let reserved = await controller.beginPress()
        XCTAssertNil(reserved)
        let second = await controller.beginPress()
        XCTAssertEqual(second, .busy)
        _ = await controller.startTake(inFront: true)
        world.audio?.duration = 2
        let stop = await controller.beginPress()
        XCTAssertEqual(stop, .stopped, "the second press sends, without coming forward")
        XCTAssertEqual(world.instance.sent, ["hello"])
    }

    func testThePressContextNamesOtherAudioAndTheOutput() {
        XCTAssertEqual(
            VoiceCaptureLaunch.audioContext(otherAudio: true, silenceHint: true, outputs: ["CarAudio"]),
            "otherAudio=1 silenceHint=1 out=CarAudio"
        )
        XCTAssertEqual(
            VoiceCaptureLaunch.audioContext(otherAudio: false, silenceHint: false, outputs: []),
            "otherAudio=0 silenceHint=0 out=-"
        )
    }

    func testTheInAppLineIsPlain() throws {
        func value(_ language: String) throws -> String {
            let path = try XCTUnwrap(Bundle.main.path(forResource: language, ofType: "lproj"))
            return try XCTUnwrap(Bundle(path: path)).localizedString(forKey: "voiceCapture.startedInApp", value: nil, table: "VoiceCapture")
        }
        XCTAssertEqual(try value("zh-Hans"), "正在录音，再长按一次侧边键发送")
        XCTAssertEqual(try value("en"), "Recording. Press and hold the side button again to send.")
    }

    /// The real recorder sets the candidate's category on the session.
    func testTheRecorderPreparesTheCandidatesSession() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "take-\(UUID().uuidString).m4a")
        let session = FakeSession()
        let recorder = VoiceCaptureRecorder(url: url, session: session, permission: { true }, makeEngine: { _ in FakeEngine() })
        try recorder.prepare(.exclusiveRecord)
        XCTAssertTrue(session.active)
        try recorder.start()
        XCTAssertEqual(session.configured, [.exclusiveRecord], "start keeps the prepared session")
        recorder.stop()
        XCTAssertFalse(session.active)

        let refused = FakeSession()
        refused.activateError = NSError(domain: NSOSStatusErrorDomain, code: 560557684)
        let other = VoiceCaptureRecorder(url: url, session: refused, permission: { true }, makeEngine: { _ in FakeEngine() })
        XCTAssertThrowsError(try other.prepare(.exclusiveRecord)) {
            XCTAssertEqual(($0 as? VoiceCaptureRecorder.StartFailure)?.step, .sessionActivate)
        }
        XCTAssertFalse(refused.active)
    }

    func testTheAttemptsLineReadsPlainly() throws {
        func format(_ language: String, _ key: String) throws -> String {
            let path = try XCTUnwrap(Bundle.main.path(forResource: language, ofType: "lproj"))
            return try XCTUnwrap(Bundle(path: path)).localizedString(forKey: key, value: nil, table: "VoiceCapture")
        }
        XCTAssertEqual(try format("zh-Hans", "voiceCapture.attempt %lld %@ %@"), "方案%1$lld %2$@ 错误 %3$@")
        XCTAssertEqual(try format("zh-Hans", "voiceCapture.attempt.separator"), "；")
        XCTAssertEqual(try format("en", "voiceCapture.attempt %lld %@ %@"), "try %1$lld: %2$@, error %3$@")
        let attempts = [
            VoiceCaptureAttributes.Attempt(candidate: 1, step: .sessionActivate, error: "560557684"),
            VoiceCaptureAttributes.Attempt(candidate: 2, step: .recorder, error: "561145187"),
        ]
        var state = VoiceCaptureAttributes.ContentState(phase: .failed, startedAt: Date(), reason: .recordingFailed)
        state.attempts = attempts
        let body = VoiceCaptureFailureNotification.content(for: state).body
        let language = Bundle.main.preferredLocalizations.first ?? "en"
        if language.hasPrefix("zh") {
            XCTAssertEqual(body, "录音没能开始（方案1 激活音频会话 错误 560557684；方案2 启动录音器 错误 561145187）。")
        } else {
            XCTAssertEqual(body, "Recording couldn't start (try 1: activating the audio session, error 560557684; try 2: starting the recorder, error 561145187).")
        }
    }

    // MARK: Where a start stopped (the owner's one-press diagnosis, 11-19)

    /// The real recorder names the step that refused and keeps the system's
    /// error: setting up the session, activating it, or the recorder.
    func testTheRecorderNamesTheStepThatRefused() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "take-\(UUID().uuidString).m4a")
        func failure(_ session: FakeSession, engine: @escaping (URL) throws -> VoiceCaptureRecordingEngine = { _ in FakeEngine() })
            -> VoiceCaptureRecorder.StartFailure?
        {
            let recorder = VoiceCaptureRecorder(url: url, session: session, permission: { true }, makeEngine: engine)
            do {
                try recorder.prepare(.exclusiveRecord)
                try recorder.start()
                return nil
            } catch { return error as? VoiceCaptureRecorder.StartFailure }
        }
        let cannotInterrupt = NSError(domain: NSOSStatusErrorDomain, code: 560557684)

        let category = FakeSession()
        category.configureError = cannotInterrupt
        XCTAssertEqual(failure(category)?.step, .sessionCategory)
        XCTAssertFalse(category.active)

        let activation = FakeSession()
        activation.activateError = cannotInterrupt
        let activationFailure = failure(activation)
        XCTAssertEqual(activationFailure?.step, .sessionActivate)
        XCTAssertEqual(VoiceCaptureController.systemCode(try XCTUnwrap(activationFailure)), 560557684)

        let made = FakeSession()
        XCTAssertEqual(failure(made, engine: { _ in throw cannotInterrupt })?.step, .recorder)
        XCTAssertFalse(made.active)

        let refusing = FakeEngine()
        refusing.records = false
        let refused = failure(FakeSession(), engine: { _ in refusing })
        XCTAssertEqual(refused?.step, .recorder)
        XCTAssertNil(VoiceCaptureController.systemCode(try XCTUnwrap(refused)), "record() false has no system code")
    }

    /// One press that cannot record leaves one notification, which stays in
    /// Notification Center, naming the step and the system's code; the
    /// island says the same.
    func testAFailedStartPostsOneLastingNoticeWithItsStepAndCode() async {
        let world = World()
        world.recorderStartError = VoiceCaptureRecorder.StartFailure(
            step: .sessionActivate, underlying: NSError(domain: NSOSStatusErrorDomain, code: 561145187)
        )
        var clock = Date()
        world.now = { clock }
        let controller = world.controller()
        _ = await controller.toggle()
        clock = clock.addingTimeInterval(0.3)
        _ = await controller.toggle()  // the system running the same press again

        XCTAssertEqual(world.failureNotices.count, 1)
        let notice = world.failureNotices.first
        XCTAssertEqual(notice?.reason, .recordingFailed)
        XCTAssertEqual(notice?.failedStep, .sessionActivate)
        XCTAssertEqual(notice?.errorCode, 561145187)
        XCTAssertEqual(world.activities.last?.ended?.failedStep, .sessionActivate)
        XCTAssertEqual(world.activities.last?.ended?.errorCode, 561145187)
    }

    /// No island at all: the notification is still posted.
    func testAnIslandThatCannotStartStillLeavesTheNotice() async {
        let world = World()
        world.activityRequestFails = true
        let outcome = await world.controller().toggle()
        XCTAssertEqual(outcome, .needsApp(.activityFailed))
        XCTAssertEqual(world.failureNotices.count, 1)
        XCTAssertEqual(world.failureNotices.first?.failedStep, .liveActivity)
        XCTAssertEqual(world.failureNotices.first?.errorCode, 7)
    }

    /// Notifications off: the alert Avibe shows on coming forward carries
    /// the same step and code.
    func testTheForegroundAlertCarriesTheStepAndCodeToo() async {
        let world = World()
        world.activityRequestFails = true
        let controller = world.controller()
        _ = await controller.toggle()
        XCTAssertEqual(controller.lastFailure?.failedStep, .liveActivity)
        let message = VoiceCaptureAlert.message(.activityFailed, failure: controller.lastFailure)
        XCTAssertTrue(message.contains("7"), message)
        XCTAssertNotEqual(message, VoiceCaptureAlert.message(.activityFailed, failure: nil))
    }

    func testATakeFileThatCannotBeMadeNamesThatStep() async {
        let world = World()
        world.takeFileFails = true
        _ = await world.controller().toggle()
        XCTAssertEqual(world.failureNotices.first?.failedStep, .takeFile)
    }

    /// The line as the island and the notification build it (the code that
    /// makes the key), in the test host's language.
    func testTheNotificationCarriesTheBuiltLine() throws {
        var failure = VoiceCaptureController.failure(
            step: .sessionActivate,
            error: VoiceCaptureRecorder.StartFailure(
                step: .sessionActivate, underlying: NSError(domain: NSOSStatusErrorDomain, code: 561145187)
            ),
            at: Date()
        )
        XCTAssertEqual(failure.errorText, "561145187")
        let content = VoiceCaptureFailureNotification.content(for: failure)
        XCTAssertEqual(content.threadIdentifier, "voice-capture")
        let language = Bundle.main.preferredLocalizations.first ?? "en"
        if language.hasPrefix("zh") {
            XCTAssertEqual(content.body, "录音没能开始（激活音频会话，错误 561145187）。")
        } else {
            XCTAssertEqual(content.body, "Recording couldn't start (activating the audio session, error 561145187).")
        }
        XCTAssertEqual(AvibeAppDelegate.foregroundPresentationOptions(for: content), [.list])

        failure.errorText = nil
        failure.errorCode = nil
        XCTAssertFalse(VoiceCaptureFailureNotification.content(for: failure).body.contains("error"))
    }

    /// An ActivityKit-style Swift error shows its type and case, not only a
    /// bridged position number.
    func testANonSystemErrorShowsItsName() {
        enum SampleActivityError: Error { case first, visibility }
        let text = VoiceCaptureController.errorText(SampleActivityError.visibility)
        XCTAssertTrue(text?.contains("visibility") == true, text ?? "nil")
        XCTAssertNil(VoiceCaptureController.errorText(VoiceCaptureRecorder.Failure.start))
    }

    func testTheFailureLineReadsPlainlyInBothLanguages() throws {
        func format(_ language: String, _ key: String) throws -> String {
            let path = try XCTUnwrap(Bundle.main.path(forResource: language, ofType: "lproj"))
            return try XCTUnwrap(Bundle(path: path)).localizedString(forKey: key, value: nil, table: "VoiceCapture")
        }
        XCTAssertEqual(try format("zh-Hans", "voiceCapture.reason.recordingFailedAt %@ %@"), "录音没能开始（%1$@，错误 %2$@）。")
        XCTAssertEqual(try format("en", "voiceCapture.reason.recordingFailedAt %@ %@"), "Recording couldn't start (%1$@, error %2$@).")
        XCTAssertEqual(try format("zh-Hans", "voiceCapture.reason.recordingFailedAt %@"), "录音没能开始（%@）。")
        let steps: [(VoiceCaptureAttributes.Step, String)] = [
            (.takeFile, "准备录音文件"), (.sessionCategory, "设置音频会话"), (.sessionActivate, "激活音频会话"),
            (.recorder, "启动录音器"), (.liveActivity, "启动实时活动"),
        ]
        for (step, zh) in steps {
            XCTAssertEqual(try format("zh-Hans", "voiceCapture.step.\(step.rawValue)"), zh)
        }
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
    /// Live Activities a gone process left behind.
    var leftovers: [FakeActivity] = []
    var activityRequestFails = false
    /// What the recorder throws when it starts (the background refusal).
    var recorderStartError: Error?
    var prepareErrors: [VoiceCaptureSessionCandidate: Error] = [:]
    var startErrors: [VoiceCaptureSessionCandidate: Error] = [:]
    /// The order things happened in: active, activity, prepare1, record,
    /// announce, status.
    var events: [String] = []
    var onActivityRequested: ((FakeActivity) -> Void)?
    /// The App's state at the press, as the message metadata gives it.
    var pressContext = "background warm"
    var takeFileFails = false
    var leftoverAudioDiscarded = 0
    var now: () -> Date = Date.init
    /// The failure notifications posted (each stays in Notification Center).
    var failureNotices: [VoiceCaptureAttributes.ContentState] = []
    /// Whether Avibe is in front (refusals are then said in the App).
    var inFront = false
    /// What was said in front: the reason, and whether the chat opened.
    var presented: [(VoiceCaptureAttributes.Reason, Bool)] = []
    /// `captureStarted` calls: whether in front.
    var announced: [Bool] = []
    /// Variant (a): the switch, whether the process was shown, other work,
    /// and what happened.
    var endSwitch = false
    var shown = false
    var otherWork: String?
    var ended = 0
    var endDecisions: [String] = []
    var beforeEnding: () -> Void = {}
    var held = 0
    var released = 0

    func controller() -> VoiceCaptureController {
        instance.log = { self.events.append($0) }
        return VoiceCaptureController(environment: VoiceCaptureEnvironment(
            target: { self.target },
            makeInstance: { _ in self.instance },
            makeAudio: { _ in
                if self.takeFileFails { throw CocoaError(.fileWriteNoPermission) }
                let audio = FakeAudio()
                audio.startError = self.recorderStartError
                audio.prepareErrors = self.prepareErrors
                audio.startErrors = self.startErrors
                audio.log = { self.events.append($0) }
                self.audio = audio
                return audio
            },
            microphoneAllowed: { self.microphone },
            activitiesEnabled: { self.activitiesEnabled },
            requestActivity: { state in
                if self.activityRequestFails {
                    // ActivityKit's ActivityAuthorizationError.targetMaximumExceeded-like failure.
                    throw NSError(domain: "com.apple.ActivityKit.ActivityAuthorizationError", code: 7)
                }
                let activity = FakeActivity(state)
                self.activities.append(activity)
                self.events.append("activity")
                self.onActivityRequested?(activity)
                return activity
            },
            existingActivities: { (self.leftovers + self.activities).filter { $0.ended == nil } },
            discardLeftoverAudio: { self.leftoverAudioDiscarded += 1 },
            postFailureNotice: { self.failureNotices.append($0) },
            pressContext: { self.pressContext },
            isInFront: { self.inFront },
            untilActive: { self.events.append("active") },
            captureStarted: { inFront in
                self.announced.append(inFront)
                self.events.append("announce")
            },
            presentInFront: { reason, needsApp in self.presented.append((reason, needsApp)) },
            endProcessAfterBackgroundCapture: { self.endSwitch },
            processEverShown: { self.shown },
            otherWorkInFlight: { self.otherWork },
            waitBeforeEnding: { self.beforeEnding() },
            holdWhileEnding: {
                self.held += 1
                return { self.released += 1 }
            },
            endProcess: { self.ended += 1 },
            noteEndDecision: { self.endDecisions.append($0) },
            now: { self.now() },
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
    var captures: [String?] = []
    var closed = false
    var log: (String) -> Void = { _ in }
    private var held: CheckedContinuation<Void, Never>?

    func status() async throws -> VoiceCaptureInstanceClient.Status {
        log("status")
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

    /// Whether a request is suspended, waiting for `release()`.
    var isHolding: Bool { held != nil }

    func mainBotMessagesPath() async throws -> String {
        if pathFailures > 0 {
            pathFailures -= 1
            throw VoiceCaptureInstanceClient.Failure.offline
        }
        return "/api/sessions/ses_main"
    }

    func postMessage(_ text: String, to sessionPath: String, capture: String?) async throws {
        captures.append(capture)
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
    var stops = 0
    var duration: TimeInterval = 0

    init() {
        url = FileManager.default.temporaryDirectory.appending(path: "voice-\(UUID().uuidString).m4a")
        try? Data([0, 1, 2]).write(to: url)
    }

    var startError: Error?
    /// What each session candidate's activation throws, and per-candidate
    /// recorder errors (the background refusals the owner's press showed).
    var prepareErrors: [VoiceCaptureSessionCandidate: Error] = [:]
    var startErrors: [VoiceCaptureSessionCandidate: Error] = [:]
    var prepared: VoiceCaptureSessionCandidate?
    var log: (String) -> Void = { _ in }

    func prepare(_ candidate: VoiceCaptureSessionCandidate) throws {
        log("prepare\(candidate.rawValue)")
        if let error = prepareErrors[candidate] { throw error }
        prepared = candidate
    }

    func start() throws {
        log("record")
        if let startError { throw startError }
        if let prepared, let error = startErrors[prepared] { throw error }
        started = true
    }
    func stop() -> TimeInterval { stops += 1; prepared = nil; log("stop"); return duration }
    func discard() { discarded = true }
}

@MainActor
private final class FakeActivity: VoiceCaptureActivityHandle {
    let id = UUID().uuidString
    var states: [VoiceCaptureAttributes.ContentState]
    var ended: VoiceCaptureAttributes.ContentState?
    var endedImmediately = false
    /// Runs while `end` is suspended (an account change landing then).
    var onEnd: (() -> Void)?

    var currentState: VoiceCaptureAttributes.ContentState? {
        ended == nil ? states.last : nil
    }

    init(_ state: VoiceCaptureAttributes.ContentState) {
        states = [state]
    }

    func update(_ state: VoiceCaptureAttributes.ContentState) async { states.append(state) }

    func end(_ state: VoiceCaptureAttributes.ContentState?, dismissal: ActivityUIDismissalPolicy) async {
        onEnd?()
        ended = state ?? states.last
        // Cancel ends with no final state (gone at once); an ending that
        // says something carries one.
        endedImmediately = state == nil
    }
}

private final class FakeSession: VoiceCaptureAudioSession {
    var active = false
    var activations = 0
    var deactivations = 0
    var configureError: Error?
    var activateError: Error?
    var configured: [VoiceCaptureSessionCandidate] = []
    func configure(for candidate: VoiceCaptureSessionCandidate) throws {
        configured.append(candidate)
        if let configureError { throw configureError }
    }
    func activate() throws {
        if let activateError { throw activateError }
        active = true
        activations += 1
    }
    func deactivate() { active = false; deactivations += 1 }
}

private final class FakeEngine: VoiceCaptureRecordingEngine {
    var records = true
    var currentTime: TimeInterval { 1 }
    var isRecording: Bool { records }
    func record() -> Bool { records }
    func stop() {}
}
