import ActivityKit
import AppIntents
import AVFAudio
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
        XCTAssertEqual(world.activities.first?.states.last?.reason, .willSend)
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
        XCTAssertEqual(world.activities.first?.states.last?.reason, .willSend)
        // Let any answer asked early settle while still unreachable.
        for _ in 0..<20 { await Task.yield() }

        // Retry asks again then, not with an answer taken during the outage.
        world.instance.statusResult = .success(.init(available: true, consent: .granted))
        await controller.retry()
        XCTAssertEqual(world.instance.sent, ["hello"])
    }

    /// An account change while a stop waits for the answer: nothing is asked
    /// again on the closed client.
    func testAnAccountChangeWhileSendingWaitsNeverAsksAgain() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.offline)
        world.instance.holdStatus = true
        let controller = world.controller()
        _ = await controller.toggle()
        while !world.instance.isHolding { await Task.yield() }
        world.audio?.duration = 3
        let stop = Task { await controller.toggle() }
        while controller.phase != .sending { await Task.yield() }
        _ = controller.cancelForAccountTransition()
        world.instance.release()
        _ = await stop.value
        XCTAssertEqual(world.instance.statusCalls, 1, "not asked again after the change")
        XCTAssertEqual(world.instance.transcribed, 0)
    }

    /// A take cancelled before its answer: the pending ask is cancelled too.
    func testACancelledTakeCancelsItsPendingAsk() async {
        let world = World()
        world.instance.holdStatus = true
        let controller = world.controller()
        _ = await controller.toggle()
        while !world.instance.isHolding { await Task.yield() }
        await controller.finish(send: false)
        XCTAssertTrue(world.instance.closed)
        world.instance.release()
        await controller.consentWatch?.value
        XCTAssertEqual(world.instance.statusCalls, 1)
        XCTAssertEqual(world.instance.transcribed, 0)
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
        // Its own directory, never the host App's Caches.
        let base = FileManager.default.temporaryDirectory.appending(path: "takes-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: base) }
        let url = try VoiceCaptureRecorder.takeURL(id: UUID(), in: base)
        try Data([1, 2, 3]).write(to: url)
        let recorder = VoiceCaptureRecorder(url: url, session: FakeSession(), permission: { true }, makeEngine: { _ in FakeEngine() })
        recorder.discard()
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))

        let leftover = try VoiceCaptureRecorder.takeURL(id: UUID(), in: base)
        try Data([4, 5, 6]).write(to: leftover)
        VoiceCaptureRecorder.discardAll(in: base)
        XCTAssertFalse(FileManager.default.fileExists(atPath: leftover.path))
    }

    func testTakesAreExcludedFromBackup() throws {
        let base = FileManager.default.temporaryDirectory.appending(path: "takes-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: base) }
        let url = try VoiceCaptureRecorder.takeURL(id: UUID(), in: base)
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

    /// The real recorder's watch: a late first audio is caught, and a take
    /// that stops with none says so.
    func testTheRecorderWatchesForItsFirstAudio() async throws {
        let base = FileManager.default.temporaryDirectory.appending(path: "takes-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: base) }
        let url = try VoiceCaptureRecorder.takeURL(id: UUID(), in: base)
        let recorder = VoiceCaptureRecorder(url: url, session: FakeSession(), permission: { true }, makeEngine: { _ in FakeEngine() })
        let late = FakeEngine()
        late.silentPolls = 250
        recorder.watchFirstAudio(late, poll: .microseconds(10))
        for _ in 0..<2_000 where recorder.firstAudioAt == nil { try await Task.sleep(for: .milliseconds(1)) }
        XCTAssertNotNil(recorder.firstAudioAt, "found after the fast second")

        let never = FakeEngine()
        never.silentPolls = .max
        recorder.watchFirstAudio(never, poll: .microseconds(10))
        try await Task.sleep(for: .milliseconds(20))
        never.recording = false
        for _ in 0..<500 where !recorder.firstAudioLate { try await Task.sleep(for: .milliseconds(1)) }
        XCTAssertTrue(recorder.firstAudioLate)
        XCTAssertNil(recorder.firstAudioAt)
    }

    // MARK: A second recorder in the background (39.1: navigation speaking,
    // the mixable session active, AVAudioRecorder's record() said no)

    /// In the background a refused `record()` hands the same session to the
    /// engine recorder, with no second activation, and the report says
    /// which took the take and what the session said; in front it does not.
    func testARefusedRecordInTheBackgroundRecordsWithTheEngineOnTheSameSession() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "take-\(UUID().uuidString).m4a")
        let reading = "in=1 port=MicrophoneBuiltIn ins=1 ch=1 rate=48000 cat=PlayAndRecord mode=Default opts=3 other=1 hint=1"

        let session = FakeSession()
        let second = FakeStartingEngine()
        var made = 0
        let recorder = VoiceCaptureRecorder(
            url: url, session: session, permission: { true },
            makeEngine: { _ in FakePreparableEngine() },
            makeFallback: { _ in made += 1; return second },
            readSession: { reading }
        )
        try recorder.prepare(.mixableBackground)
        try recorder.start()
        XCTAssertTrue(second.begun)
        XCTAssertTrue(recorder.isRecording)
        XCTAssertEqual(session.activations, 1, "the same session: no second start")
        XCTAssertTrue(session.active)
        XCTAssertEqual(recorder.report, "rec[engine vp=1 recorder=0 prep=1 \(reading)]")
        // An input the engine lost and could not take up again is passed on.
        var lost = 0
        recorder.onInputLost = { lost += 1 }
        second.onInputLost?()
        XCTAssertEqual(lost, 1)
        recorder.stop()
        XCTAssertFalse(second.begun)
        XCTAssertFalse(session.active)

        // The engine refused too: its code names the cause, nothing stays active.
        let refused = FakeSession()
        let failing = FakeStartingEngine()
        failing.failure = NSError(domain: NSOSStatusErrorDomain, code: 561145187)
        failing.detail = "vp=0(-10875)"
        let refusing = FakeEngine()
        refusing.records = false
        let both = VoiceCaptureRecorder(
            url: url, session: refused, permission: { true },
            makeEngine: { _ in refusing }, makeFallback: { _ in failing }, readSession: { reading }
        )
        try both.prepare(.mixableBackground)
        XCTAssertThrowsError(try both.start()) { error in
            XCTAssertEqual(VoiceCaptureController.errorText(error), "561145187")
            XCTAssertEqual((error as? VoiceCaptureRecorder.StartFailure)?.step, .recorder)
        }
        XCTAssertEqual(both.report, "rec[recorder=0 prep=- \(reading) engine=561145187(!rec) vp=0(-10875)]")
        XCTAssertEqual(refusing.stops, 1, "the refused recorder is let go")
        XCTAssertFalse(refused.active)

        // In front the exclusive session is the last try: no second recorder.
        let front = VoiceCaptureRecorder(
            url: url, session: FakeSession(), permission: { true },
            makeEngine: { _ in refusing }, makeFallback: { _ in made += 1; return FakeStartingEngine() },
            readSession: { reading }
        )
        try front.prepare(.exclusiveRecord)
        XCTAssertThrowsError(try front.start())
        XCTAssertEqual(front.report, "rec[recorder=0 prep=- \(reading)]")
        XCTAssertEqual(made, 1)

        // The path that works is unchanged, and says so.
        let usual = VoiceCaptureRecorder(
            url: url, session: FakeSession(), permission: { true },
            makeEngine: { _ in FakeEngine() }, makeFallback: { _ in made += 1; return FakeStartingEngine() },
            readSession: { reading }
        )
        try usual.prepare(.mixableBackground)
        try usual.start()
        XCTAssertEqual(usual.report, "rec[recorder]")
        XCTAssertEqual(made, 1)
    }

    func testAnOSStatusCodeIsSpelledWhenItIsFourCharacters() {
        XCTAssertEqual(VoiceCaptureRecorder.code(NSError(domain: NSOSStatusErrorDomain, code: 561145187)), "561145187(!rec)")
        XCTAssertEqual(VoiceCaptureRecorder.code(NSError(domain: NSOSStatusErrorDomain, code: -10868)), "-10868")
        XCTAssertEqual(VoiceCaptureRecorder.code(NSError(domain: "x", code: 48_000)), "48000")
    }

    /// The message says which recorder took the take, a successful one too,
    /// and for a press that went on in front, what the background recorder said.
    func testTheMessageSaysWhichRecorderTookTheTake() async {
        let world = World()
        world.reports = [.mixableBackground: "rec[engine recorder=0 prep=1 in=1]"]
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertEqual(
            world.instance.captures.first ?? nil,
            "0 [background warm bgTries=0] rec[engine recorder=0 prep=1 in=1]"
        )

        let refused = World()
        refused.startErrors = [.mixableBackground: VoiceCaptureRecorder.StartFailure(
            step: .recorder, underlying: NSError(domain: NSOSStatusErrorDomain, code: 561145187)
        )]
        refused.reports = [
            .mixableBackground: "rec[recorder=0 prep=1 in=0 engine=561145187(!rec)]",
            .exclusiveRecord: "rec[recorder]",
        ]
        let other = refused.controller()
        _ = await other.beginPress()
        _ = await other.startTake(inFront: false, foregroundFallback: true)
        _ = await other.startTake(inFront: true)
        refused.audio?.duration = 2
        _ = await other.toggle()
        XCTAssertEqual(
            refused.instance.captures.first ?? nil,
            "3 [background warm bgTries=0] (0 recorder 561145187 rec[recorder=0 prep=1 in=0 engine=561145187(!rec)]) rec[recorder]"
        )
    }

    /// Route changes and interruptions while the take records are in its
    /// timing: a navigation prompt that cut the take shows as `intBegan`.
    func testInterruptionsAndRouteChangesDuringATakeAreInItsTiming() async throws {
        let world = World()
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 2
        let session = AVAudioSession.sharedInstance()
        NotificationCenter.default.post(
            name: AVAudioSession.routeChangeNotification, object: session,
            userInfo: [AVAudioSessionRouteChangeReasonKey: AVAudioSession.RouteChangeReason.categoryChange.rawValue]
        )
        NotificationCenter.default.post(
            name: AVAudioSession.interruptionNotification, object: session,
            userInfo: [AVAudioSessionInterruptionTypeKey: AVAudioSession.InterruptionType.began.rawValue]
        )
        // The interruption sends what was said.
        for _ in 0..<200 where world.instance.timings.isEmpty { try await Task.sleep(for: .milliseconds(5)) }
        let timing = try XCTUnwrap(world.instance.timings.first ?? nil)
        XCTAssertTrue(timing.contains("route3="), timing)
        XCTAssertTrue(timing.contains("intBegan="), timing)
        XCTAssertTrue(timing.contains("audio="), timing)

        // After the take, nothing more is noted.
        let posted = world.instance.timings.count
        NotificationCenter.default.post(
            name: AVAudioSession.routeChangeNotification, object: session,
            userInfo: [AVAudioSessionRouteChangeReasonKey: 2]
        )
        XCTAssertEqual(world.instance.timings.count, posted)
    }

    // MARK: A recorded take is never lost to an unreachable instance
    // (39.1, 00:44Z: recorded in front, the instance unreachable at the
    // stop, the island said so, and nothing sent it again)

    /// Kept for an unreachable instance, the take is sent again by itself:
    /// after 2 s, then 5 s, until it arrives.
    func testATakeKeptForAnUnreachableInstanceIsSentAgainByItself() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.offline)
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 3
        _ = await controller.toggle()
        XCTAssertEqual(controller.phase, .failed)
        XCTAssertEqual(world.activities.first?.states.last?.reason, .willSend, "the island says it will send")
        XCTAssertEqual(world.retryHolds, 1, "kept running for the retries")

        // Still unreachable at the first retry (2 s): kept again, the next in 5 s.
        var retry = controller.retryTask
        await world.wakeRetry()
        await retry?.value
        XCTAssertEqual(controller.phase, .failed)
        XCTAssertTrue(world.instance.sent.isEmpty)

        world.instance.statusResult = .success(.init(available: true, consent: .granted))
        retry = controller.retryTask
        await world.wakeRetry()
        await retry?.value
        XCTAssertEqual(world.slept.filter { $0 <= .seconds(60) }, [.seconds(2), .seconds(5)])
        XCTAssertEqual(world.instance.sent, ["hello"])
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertEqual(world.activities.first?.ended?.phase, .sent)
        XCTAssertEqual(world.retryHolds, 0)
        XCTAssertTrue((world.instance.timings.last ?? nil)?.contains("tries=2") == true)
        XCTAssertEqual(controller.retryTask == nil, true)
    }

    /// While a kept take is being sent again by itself it stays kept: a new
    /// press, Discard or Retry is never swallowed, and a take given up
    /// meanwhile asks and posts nothing more.
    func testAnAutomaticSendNeverSwallowsAPressDiscardOrRetry() async {
        for action in ["discard", "press", "retry"] {
            let world = World()
            world.instance.statusResult = .failure(Failure.offline)
            let controller = world.controller()
            _ = await controller.toggle()
            world.audio?.duration = 3
            _ = await controller.toggle()
            let kept = world.audio
            // The automatic send's ask is on its way to an instance that now answers.
            world.instance.statusResult = .success(.init(available: true, consent: .granted))
            world.instance.holdStatus = true
            let retry = controller.retryTask
            await world.wakeRetry()
            await world.instance.untilHolding()
            XCTAssertEqual(controller.phase, .failed, "\(action): still kept, not busy")
            world.instance.holdStatus = false
            var asked: Task<Void, Never>?

            switch action {
            case "discard":
                await controller.finish(send: false)
                XCTAssertEqual(controller.phase, .idle)
            case "press":
                let outcome = await controller.toggle()
                XCTAssertEqual(outcome, .started, "the press records")
            default:
                asked = Task { await controller.retry() }
                for _ in 0..<20 { await Task.yield() }
                XCTAssertEqual(controller.phase, .failed, "one send at a time")
            }
            world.instance.release()
            await retry?.value
            await asked?.value

            if action == "discard" {
                XCTAssertEqual(world.instance.transcribed, 0, "the take given up is not sent")
                XCTAssertTrue(world.instance.sent.isEmpty)
            } else {
                // Retry: sent once, by the send already under way. A new
                // press: the earlier take is not discarded, and goes on.
                XCTAssertEqual(world.instance.sent, ["hello"], action)
                XCTAssertEqual(world.instance.posts, 1, action)
            }
            XCTAssertEqual(kept?.discarded, true, action)
            XCTAssertEqual(world.retryHolds, 0, action)
            XCTAssertEqual(controller.unsentCount, 0, action)
        }
    }

    /// Avibe becoming active twice in a row, or Retry right after it, sends
    /// a kept take once.
    func testOneSendOfATakeAtATimeHoweverItIsAsked() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.offline)
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 3
        _ = await controller.toggle()
        world.instance.statusResult = .success(.init(available: true, consent: .granted))
        world.instance.holdStatus = true
        let first = Task { await controller.activated() }
        let second = Task { await controller.activated() }
        await world.instance.untilHolding()
        world.instance.holdStatus = false
        // Asked again while that send is under way: each waits for it, and
        // none starts a second send of the take.
        let retry = Task { await controller.retry() }
        let third = Task { await controller.activated() }
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(world.instance.statusCalls, 3, "the start, the stop, and one send again")
        world.instance.release()
        await first.value
        await second.value
        await retry.value
        await third.value
        XCTAssertEqual(world.instance.posts, 1)
        XCTAssertEqual(world.instance.sent, ["hello"])
        XCTAssertEqual(controller.phase, .idle)
    }

    /// A message already on its way when its take is discarded cannot be
    /// taken back: the next message's `capture_earlier` says it went.
    func testAPostOnItsWayWhenItsTakeIsDiscardedIsRecorded() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.offline)
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 3
        _ = await controller.toggle()
        world.instance.statusResult = .success(.init(available: true, consent: .granted))
        world.instance.holdPost = true
        let send = Task { await controller.activated() }
        await world.instance.untilHolding()
        await controller.finish(send: false)
        XCTAssertEqual(controller.phase, .idle)
        world.instance.release()
        await send.value
        XCTAssertEqual(world.instance.posts, 1)
        XCTAssertTrue(world.earlier.contains { $0.contains("discarded") }, "\(world.earlier)")
        XCTAssertTrue(world.earlier.last?.contains("sentAfterEnd") == true, "\(world.earlier)")
    }

    /// Discarded while the send was just short of its POST (the client's
    /// draft read): nothing went out, and nothing says it may have.
    func testATakeDiscardedBeforeItsPostIsNotRecordedAsMaybeSent() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.offline)
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 3
        _ = await controller.toggle()
        world.instance.statusResult = .success(.init(available: true, consent: .granted))
        world.instance.holdPost = true
        world.instance.postError = Failure.closed
        let send = Task { await controller.activated() }
        await world.instance.untilHolding()
        await controller.finish(send: false)
        world.instance.release()
        await send.value
        XCTAssertTrue(world.instance.sent.isEmpty)
        XCTAssertFalse(world.earlier.contains { $0.contains("AfterEnd") }, "\(world.earlier)")
    }

    /// A client that was closed (its take is over) loads and runs nothing
    /// more, whatever send is still under way for it.
    func testAClosedClientMakesNoMoreRequests() async throws {
        let target = VoiceCaptureTarget(
            accountID: "a", instanceID: "i",
            origin: try XCTUnwrap(WebOrigin.parseConfigured("https://instance-one.example")),
            storeIdentifier: UUID()
        )
        var asked = 0
        let client = VoiceCaptureInstanceClient(
            target: target, gate: { asked += 1; return .open }, isActive: { true }, sleep: { _ in }
        )
        client.close()
        do {
            try await client.postMessage("hello", to: "/api/sessions/ses_main", capture: nil)
            XCTFail("a closed client posts nothing")
        } catch {
            XCTAssertEqual(error as? Failure, .closed)
        }
        XCTAssertEqual(asked, 0, "no document is made for it")
        XCTAssertNil(client.releaseBarrier)
        XCTAssertTrue(client.failures.last?.contains("POST /api/sessions/ses_main/messages closed") == true, "\(client.failures)")
    }

    /// Avibe already in front at the press: a start that fails there is
    /// said in an alert, told by the failure being new since before the
    /// press's first start.
    func testAFailedStartInFrontIsSaidWhenAvibeWasAlreadyActive() async {
        let world = World()
        world.inFront = true
        world.recorderStartError = VoiceCaptureRecorder.StartFailure(
            step: .recorder, underlying: NSError(domain: NSOSStatusErrorDomain, code: 561145187)
        )
        let controller = world.controller()
        _ = await controller.beginPress()
        let before = controller.lastFailure
        let notices = controller.noticeCount
        let outcome = await controller.startTake(inFront: controller.isInFront, foregroundFallback: false)
        XCTAssertEqual(outcome, .stopped)
        let alert = VoiceCaptureController.stoppedAlert(
            before: before, after: controller.lastFailure,
            noticesBefore: notices, noticesAfter: controller.noticeCount,
            lastNoticeReason: controller.lastNoticeReason
        )
        XCTAssertEqual(alert?.reason, .recordingFailed)
        XCTAssertEqual(alert?.failure?.failedStep, .recorder)
        XCTAssertEqual(alert?.failure?.errorCode, 561145187)
        XCTAssertEqual(world.events.filter { $0.hasPrefix("prepare") }, ["prepare1"], "the exclusive session, in front")

        // Read after the start (as the regression did), the failure is not new.
        XCTAssertNil(VoiceCaptureController.stoppedAlert(
            before: controller.lastFailure, after: controller.lastFailure,
            noticesBefore: notices, noticesAfter: controller.noticeCount, lastNoticeReason: nil
        ))
        // A notice said since is the alert otherwise.
        XCTAssertEqual(VoiceCaptureController.stoppedAlert(
            before: nil, after: nil, noticesBefore: 0, noticesAfter: 1, lastNoticeReason: .voiceUnavailable
        )?.reason, .voiceUnavailable)
    }

    func testTheRetryScheduleBacksOffToAMinute() {
        XCTAssertEqual((0..<7).map(VoiceCaptureController.retryDelay(after:)),
                       [2, 5, 10, 20, 30, 60, 60].map { Duration.seconds($0) })
    }

    /// Avibe coming to the front sends a kept take at once.
    func testAKeptTakeIsSentWhenAvibeComesToTheFront() async {
        let world = World()
        world.instance.statusResult = .failure(Failure.offline)
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 3
        _ = await controller.toggle()
        world.instance.statusResult = .success(.init(available: true, consent: .granted))
        await controller.activated()
        XCTAssertEqual(world.instance.sent, ["hello"])
        XCTAssertEqual(world.retryHolds, 0)

        // A take that waits for the user (not unreachable) is not sent by
        // the schedule, but is sent once more, silently, on coming forward.
        let other = World()
        other.instance.transcribeResult = .failure(Failure.server(500, nil))
        let second = other.controller()
        _ = await second.toggle()
        other.audio?.duration = 3
        _ = await second.toggle()
        XCTAssertEqual(other.activities.first?.states.last?.reason, .transcriptionFailed)
        XCTAssertNil(second.retryTask)
        XCTAssertEqual(other.retryHolds, 0)
        other.instance.transcribeResult = .success("hello")
        await second.activated()
        XCTAssertEqual(other.instance.sent, ["hello"])
        XCTAssertTrue(other.unsentAlerts.isEmpty, "everything went: nothing is shown")
    }

    /// Ten minutes after it was spoken: the island goes and the take stays,
    /// but is no longer sent by itself. The lasting notification was
    /// scheduled for that moment when the take was kept, and is removed
    /// once nothing is unsent.
    func testAfterTenMinutesTheIslandGoesAndTheTakeStays() async {
        let world = World()
        let start = Date()
        var clock = start
        world.now = { clock }
        world.instance.statusResult = .failure(Failure.offline)
        world.instance.failures = ["GET /api/asr/status loadp NSURLErrorDomain -1004 +12ms"]
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 3
        _ = await controller.toggle()
        // Why it did not reach the instance is in the line the next message carries.
        XCTAssertTrue(
            world.earlier.last?.contains("kept:offline {GET /api/asr/status loadp NSURLErrorDomain -1004 +12ms} 0 [") == true,
            "\(world.earlier)"
        )
        XCTAssertEqual(world.unsentNotices, [start.addingTimeInterval(600)])

        clock = clock.addingTimeInterval(601)
        let island = controller.islandTask
        await world.wakeIsland()
        await island?.value
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertEqual(world.activities.first?.endedImmediately, true)
        XCTAssertEqual(controller.unsentCount, 1, "kept, without its island")
        XCTAssertEqual(world.audio?.discarded, false)

        // From here it is never sent by itself, reachable or not: only Retry
        // in the alert sends it.
        world.instance.statusResult = .success(.init(available: true, consent: .granted))
        XCTAssertNil(controller.retryTask)
        XCTAssertEqual(world.retryHolds, 0)
        await controller.sendKept(.byItself)
        world.inFront = true
        await controller.activated()
        XCTAssertTrue(world.instance.sent.isEmpty, "not sent silently after ten minutes")
        XCTAssertEqual(world.unsentAlerts.map(\.count), [1])
        XCTAssertEqual(world.unsentAlerts.first?.oldest, start, "when it was spoken")

        world.unsentAlerts[0].answer(true)
        for _ in 0..<400 where controller.unsentCount > 0 { try? await Task.sleep(for: .milliseconds(5)) }
        // Its first line says when it was spoken; the words follow.
        XCTAssertEqual(world.instance.sent.count, 1)
        XCTAssertEqual(
            world.instance.sent.first,
            VoiceCaptureLateNote.message("hello", spoken: start, now: clock)
        )
        XCTAssertNotNil(world.instance.spokenAts.first ?? nil)
        XCTAssertEqual(controller.unsentCount, 0)
        XCTAssertEqual(world.unsentNotices.count, 2)
        XCTAssertNil(world.unsentNotices.last ?? Date(), "removed once nothing is unsent")
    }

    /// Discard or an account change ends the retrying.
    func testGivingAKeptTakeUpEndsItsRetries() async {
        for end in 0..<2 {
            let world = World()
            world.instance.statusResult = .failure(Failure.offline)
            let controller = world.controller()
            _ = await controller.toggle()
            world.audio?.duration = 3
            _ = await controller.toggle()
            let retry = controller.retryTask
            switch end {
            case 0: await controller.finish(send: false)
            default: _ = controller.cancelForAccountTransition()
            }
            XCTAssertEqual(retry?.isCancelled, true, "\(end)")
            XCTAssertEqual(world.retryHolds, 0, "\(end)")
            XCTAssertEqual(controller.unsentCount, 0, "\(end)")
            XCTAssertTrue(VoiceCaptureOutbox(directory: world.outboxDirectory).entries().isEmpty, "\(end)")
        }
    }

    // MARK: Takes that are not sent yet are kept in order (the outbox)

    /// Records a take and stops it while the instance is unreachable.
    private func keepATake(_ world: World, _ controller: VoiceCaptureController) async {
        world.instance.statusResult = .failure(Failure.offline)
        _ = await controller.toggle()
        world.audio?.duration = 3
        _ = await controller.toggle()
    }

    private func reachable(_ world: World) {
        world.instance.statusResult = .success(.init(available: true, consent: .granted))
    }

    /// A new press never discards a take that is not sent yet: both are
    /// kept, and sent oldest first, each as its own message.
    func testANewPressKeepsTheUnsentTakeAndBothAreSentInOrder() async {
        let world = World()
        world.pressContext = "first"
        let controller = world.controller()
        await keepATake(world, controller)
        let first = world.audio
        XCTAssertEqual(controller.unsentCount, 1)

        world.pressContext = "second"
        await keepATake(world, controller)
        XCTAssertEqual(first?.discarded, false, "not discarded by the new press")
        XCTAssertEqual(world.activities.first?.endedImmediately, true, "its island went to the new take")
        XCTAssertEqual(controller.unsentCount, 2)
        XCTAssertEqual(controller.phase, .failed, "the newest keeps its island")
        XCTAssertEqual(world.activities.last?.states.last?.reason, .willSend)
        XCTAssertEqual(VoiceCaptureOutbox(directory: world.outboxDirectory).entries().map(\.sequence), [1, 2])
        XCTAssertTrue(world.instance.sent.isEmpty)

        reachable(world)
        world.instance.transcripts = ["one", "two"]
        let retry = controller.retryTask
        await world.wakeRetry()
        await retry?.value
        XCTAssertEqual(world.instance.sent, ["one", "two"], "oldest first, never merged")
        XCTAssertEqual(world.instance.captures.map { $0?.contains("[first") }, [true, false])
        XCTAssertEqual(controller.unsentCount, 0)
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertEqual(world.activities.last?.ended?.phase, .sent)
        XCTAssertTrue(VoiceCaptureOutbox(directory: world.outboxDirectory).entries().isEmpty)
        XCTAssertEqual(world.retryHolds, 0)
    }

    /// A take that stops while an earlier one is still on its way waits
    /// behind it, even though the instance answers again.
    func testATakeIsNeverSentBeforeAnEarlierOneThatIsOnItsWay() async {
        let world = World()
        let controller = world.controller()
        await keepATake(world, controller)
        // Reachable again, and the earlier take's send is under way (held).
        reachable(world)
        world.instance.holdTranscribe = true
        world.instance.transcripts = ["one", "two"]
        let retry = controller.retryTask
        await world.wakeRetry()
        await world.instance.untilHolding()
        // A new take is recorded and stopped meanwhile.
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertTrue(world.instance.sent.isEmpty)
        XCTAssertEqual(controller.unsentCount, 2)
        world.instance.holdTranscribe = false
        world.instance.release()
        await retry?.value
        XCTAssertEqual(world.instance.sent, ["one", "two"])
        XCTAssertEqual(controller.unsentCount, 0)
    }

    /// The order survives the App being ended: a new run finds the takes,
    /// with their transcription ids, and sends them oldest first.
    func testUnsentTakesSurviveARelaunchInOrder() async {
        let before = World()
        before.instance.dictationID = "ios-action-kept"
        before.pressContext = "first"
        let ended = before.controller()
        await keepATake(before, ended)
        before.pressContext = "second"
        await keepATake(before, ended)

        let after = World()
        after.outboxDirectory = before.outboxDirectory
        after.instance.statusResult = .failure(Failure.offline)
        let controller = after.controller()
        await controller.restoreOutbox()
        XCTAssertEqual(controller.unsentCount, 2)
        XCTAssertEqual(controller.phase, .idle, "no island after a relaunch")
        XCTAssertEqual(after.madeWith, ["ios-action-kept", "ios-action-kept"])
        XCTAssertTrue(after.instance.sent.isEmpty, "still unreachable")

        reachable(after)
        after.instance.transcripts = ["one", "two"]
        let retry = controller.retryTask
        await after.wakeRetry()
        await retry?.value
        XCTAssertEqual(after.instance.sent, ["one", "two"])
        XCTAssertEqual(after.instance.captures.map { $0?.contains("[first") }, [true, false])
        XCTAssertTrue((after.instance.timings.first ?? nil)?.contains("stop=") == true, "its steps as they stood")
        XCTAssertTrue(VoiceCaptureOutbox(directory: after.outboxDirectory).entries().isEmpty)
        // Asked afresh about voice input before anything went.
        XCTAssertGreaterThanOrEqual(after.instance.statusCalls, 2)
    }

    /// A message whose POST was begun when the App ended is never posted
    /// again: it may have arrived. The next message says so.
    func testATakeWhosePostWasBegunIsNotSentAgainAfterARelaunch() async {
        let before = World()
        let ended = before.controller()
        await keepATake(before, ended)
        reachable(before)
        before.instance.holdPost = true
        let send = Task { await ended.sendKept(.byItself) }
        await before.instance.untilHolding()
        XCTAssertEqual(VoiceCaptureOutbox(directory: before.outboxDirectory).entries().first?.posted, true)

        // The App ends here; a new run starts.
        let after = World()
        after.outboxDirectory = before.outboxDirectory
        let controller = after.controller()
        await controller.restoreOutbox()
        XCTAssertEqual(controller.unsentCount, 0)
        XCTAssertEqual(after.instance.posts, 0)
        XCTAssertTrue(after.earlier.last?.contains("sendUnknown") == true, "\(after.earlier)")
        // Nothing is left to retry: the notice the earlier run scheduled goes.
        XCTAssertEqual(after.unsentNotices.count, 1)
        XCTAssertNil(after.unsentNotices.last ?? Date())
        XCTAssertTrue(VoiceCaptureOutbox(directory: after.outboxDirectory).entries().isEmpty)
        before.instance.release()
        await send.value
    }

    /// An account sign-out or change deletes every unsent take at once.
    func testAnAccountChangeDeletesEveryUnsentTake() async {
        let world = World()
        let controller = world.controller()
        await keepATake(world, controller)
        await keepATake(world, controller)
        XCTAssertEqual(VoiceCaptureOutbox(directory: world.outboxDirectory).entries().count, 2)
        _ = controller.cancelForAccountTransition()
        XCTAssertEqual(controller.unsentCount, 0)
        XCTAssertEqual(controller.phase, .idle)
        XCTAssertTrue(VoiceCaptureOutbox(directory: world.outboxDirectory).entries().isEmpty)
        XCTAssertNil(controller.retryTask)
        XCTAssertNil(world.unsentNotices.last ?? Date())
        reachable(world)
        await controller.activated()
        XCTAssertTrue(world.instance.sent.isEmpty)

        // A take another account left on disk is not this account's.
        let other = World()
        other.outboxDirectory = world.outboxDirectory
        let first = other.controller()
        await keepATake(other, first)
        let later = World()
        later.outboxDirectory = world.outboxDirectory
        later.target = VoiceCaptureTarget(
            accountID: "account-2", instanceID: "instance-9",
            origin: WebOrigin.parseConfigured("https://other.example.invalid")!, storeIdentifier: UUID()
        )
        let second = later.controller()
        await second.restoreOutbox()
        XCTAssertEqual(second.unsentCount, 0)
        XCTAssertTrue(VoiceCaptureOutbox(directory: world.outboxDirectory).entries().isEmpty)
    }

    /// Avibe coming to the front sends what is unsent once more, silently.
    /// Everything sent: nothing is shown. Still unsent: one alert with the
    /// count; Retry sends all, oldest first; Discard deletes all.
    func testComingToTheFrontRetriesSilentlyThenAsksOnce() async {
        let world = World()
        world.inFront = true
        let controller = world.controller()
        await keepATake(world, controller)
        await keepATake(world, controller)
        XCTAssertEqual(world.said, [.willSend, .willSend], "said in front, where no island is drawn")

        await controller.activated()
        XCTAssertEqual(world.unsentAlerts.map(\.count), [2])
        await controller.activated()
        XCTAssertEqual(world.unsentAlerts.count, 1, "one alert, not one per take or per return")

        // Retry: all of them, oldest first.
        reachable(world)
        world.instance.transcripts = ["one", "two"]
        world.unsentAlerts[0].answer(true)
        for _ in 0..<400 where world.instance.sent.count < 2 { try? await Task.sleep(for: .milliseconds(5)) }
        XCTAssertEqual(world.instance.sent, ["one", "two"])
        XCTAssertEqual(controller.unsentCount, 0)
        await controller.activated()
        XCTAssertEqual(world.unsentAlerts.count, 1, "nothing unsent: nothing shown")

        // Discard: all of them, with no second question.
        await keepATake(world, controller)
        await keepATake(world, controller)
        await controller.activated()
        XCTAssertEqual(world.unsentAlerts.map(\.count), [2, 2])
        world.unsentAlerts[1].answer(false)
        for _ in 0..<400 where controller.unsentCount > 0 { try? await Task.sleep(for: .milliseconds(5)) }
        XCTAssertEqual(controller.unsentCount, 0)
        XCTAssertTrue(VoiceCaptureOutbox(directory: world.outboxDirectory).entries().isEmpty)
        XCTAssertTrue(world.earlier.contains { $0.contains("discardedAll 2") }, "\(world.earlier)")
        XCTAssertEqual(world.instance.sent, ["one", "two"], "nothing more was sent")

        // In the background nothing is asked.
        let background = World()
        let other = background.controller()
        await keepATake(background, other)
        await other.activated()
        XCTAssertTrue(background.unsentAlerts.isEmpty)
    }

    /// A take that cannot be turned into text waits for the user without
    /// holding up the ones after it.
    func testATakeThatNeedsTheUserDoesNotHoldTheOthersUp() async {
        let world = World()
        let controller = world.controller()
        // The first take: reachable, but its transcription fails.
        world.instance.transcribeResult = .failure(Failure.server(500, nil))
        _ = await controller.toggle()
        world.audio?.duration = 3
        _ = await controller.toggle()
        XCTAssertEqual(world.activities.first?.states.last?.reason, .transcriptionFailed)
        // The second is recorded and sent while the first still waits.
        world.instance.transcribeResult = .success("two")
        _ = await controller.toggle()
        world.audio?.duration = 3
        _ = await controller.toggle()
        XCTAssertEqual(world.instance.sent, ["two"])
        XCTAssertEqual(controller.unsentCount, 1)
        // Retry (the alert's, or coming forward) sends the first too.
        world.instance.transcribeResult = .success("one")
        await controller.sendKept(.retry)
        XCTAssertEqual(world.instance.sent, ["two", "one"])
        XCTAssertEqual(controller.unsentCount, 0)
    }

    /// A day unsent: the take is deleted, and the next message says so.
    func testAnUnsentTakeIsDeletedAfterADay() async {
        let world = World()
        var clock = Date()
        world.now = { clock }
        let controller = world.controller()
        await keepATake(world, controller)
        clock = clock.addingTimeInterval(VoiceCaptureOutbox.retention - 1)
        await controller.activated()
        XCTAssertEqual(controller.unsentCount, 1)
        clock = clock.addingTimeInterval(2)
        reachable(world)
        await controller.activated()
        XCTAssertEqual(controller.unsentCount, 0)
        XCTAssertTrue(world.instance.sent.isEmpty, "deleted, not sent a day late")
        XCTAssertTrue(world.earlier.contains { $0.contains("expired") }, "\(world.earlier)")
        XCTAssertTrue(VoiceCaptureOutbox(directory: world.outboxDirectory).entries().isEmpty)

        // And one a run that ended left behind, at the next launch.
        let before = World()
        let start = Date()
        before.now = { start }
        await keepATake(before, before.controller())
        let after = World()
        after.outboxDirectory = before.outboxDirectory
        after.now = { start.addingTimeInterval(VoiceCaptureOutbox.retention + 1) }
        let later = after.controller()
        await later.restoreOutbox()
        XCTAssertEqual(later.unsentCount, 0)
        XCTAssertTrue(after.earlier.contains { $0.contains("expired") }, "\(after.earlier)")
    }

    /// A take found after a relaunch more than ten minutes after it was
    /// spoken is not sent by itself either: the alert asks.
    func testATakeOlderThanTenMinutesWaitsForRetryAfterARelaunch() async {
        let before = World()
        // A whole second, as the outbox keeps it.
        let spoken = Date(timeIntervalSince1970: 1_795_200_000)
        before.now = { spoken }
        await keepATake(before, before.controller())

        let after = World()
        after.outboxDirectory = before.outboxDirectory
        after.now = { spoken.addingTimeInterval(3 * 3600 + 5) }
        after.inFront = true
        let controller = after.controller()
        await controller.restoreOutbox()
        XCTAssertEqual(controller.unsentCount, 1)
        XCTAssertNil(controller.retryTask)
        XCTAssertEqual(after.instance.statusCalls, 0, "nothing was asked or sent by itself")
        await controller.activated()
        XCTAssertEqual(after.instance.statusCalls, 0)
        XCTAssertEqual(after.unsentAlerts.map(\.count), [1])
        XCTAssertEqual(after.unsentAlerts.first?.oldest, spoken)

        // A newer take does not wait behind it.
        _ = await controller.toggle()
        after.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertEqual(after.instance.sent, ["hello"])
        XCTAssertNil(after.instance.spokenAts.last ?? nil, "said just now: no note")
        XCTAssertEqual(controller.unsentCount, 1)
    }

    /// Retry in the alert while a scheduled send is under way is not
    /// dropped: the running pass sends the older take too before it ends.
    func testRetryInTheAlertDuringAScheduledSendStillSendsTheOldTake() async {
        let world = World()
        let start = Date()
        var clock = start
        world.now = { clock }
        world.inFront = true
        let controller = world.controller()
        await keepATake(world, controller)
        // Eleven minutes on: that take is Retry-only now. A new one is kept.
        clock = start.addingTimeInterval(11 * 60)
        await keepATake(world, controller)
        await controller.activated()
        XCTAssertEqual(world.unsentAlerts.map(\.count), [2])
        XCTAssertEqual(world.unsentAlerts.first?.oldest, start)

        // The schedule's send of the new take is under way...
        reachable(world)
        world.instance.holdStatus = true
        world.instance.transcripts = ["new", "old"]
        let scheduled = controller.retryTask
        await world.wakeRetry()
        await world.instance.untilHolding()
        world.instance.holdStatus = false
        // ...when Retry is tapped.
        world.unsentAlerts[0].answer(true)
        for _ in 0..<20 { await Task.yield() }
        world.instance.release()
        await scheduled?.value
        for _ in 0..<400 where controller.unsentCount > 0 { try? await Task.sleep(for: .milliseconds(5)) }
        XCTAssertEqual(world.instance.sent.count, 2)
        XCTAssertEqual(world.instance.sent.first, "new")
        XCTAssertEqual(
            world.instance.sent.last, VoiceCaptureLateNote.message("old", spoken: start, now: clock),
            "the old take, with when it was spoken"
        )
    }

    /// A POST the instance refuses with "sign in" did not arrive: the take
    /// waits for Retry, a relaunch included, and is not taken for one that
    /// may have been sent.
    func testATakeRefusedForSignInIsKeptAcrossARelaunch() async {
        let before = World()
        let ended = before.controller()
        await keepATake(before, ended)
        reachable(before)
        before.instance.postError = Failure.signIn
        await ended.sendKept(.byItself)
        XCTAssertEqual(before.instance.posts, 0)
        XCTAssertEqual(ended.unsentCount, 1, "not lost")
        XCTAssertEqual(ended.phase, .idle)
        XCTAssertEqual(before.activities.first?.ended?.reason, .signIn)
        let entry = VoiceCaptureOutbox(directory: before.outboxDirectory).entries().first
        XCTAssertEqual(entry?.posted, false, "it provably did not arrive")
        XCTAssertEqual(entry?.reason, "signIn")

        let after = World()
        after.outboxDirectory = before.outboxDirectory
        let controller = after.controller()
        await controller.restoreOutbox()
        XCTAssertEqual(controller.unsentCount, 1)
        XCTAssertFalse(after.earlier.contains { $0.contains("sendUnknown") }, "\(after.earlier)")
        XCTAssertEqual(after.instance.statusCalls, 0, "it waits for Retry")
        await controller.sendKept(.retry)
        XCTAssertEqual(after.instance.sent, ["hello"])
    }

    /// An expired instance session does not destroy a kept take: found
    /// signed out when it is asked again (also after a relaunch), it waits,
    /// and is sent once the session is back.
    func testAKeptTakeSurvivesAnExpiredSession() async {
        let before = World()
        await keepATake(before, before.controller())

        let after = World()
        after.outboxDirectory = before.outboxDirectory
        after.instance.statusResult = .failure(Failure.signIn)
        let controller = after.controller()
        await controller.restoreOutbox()
        XCTAssertEqual(controller.unsentCount, 1, "not destroyed")
        XCTAssertEqual(VoiceCaptureOutbox(directory: after.outboxDirectory).entries().count, 1)
        XCTAssertNil(controller.retryTask, "it waits for the App to be signed in")
        XCTAssertEqual(after.instance.transcribed, 0)

        // Signed in again: Retry asks afresh (not the answer from before) and sends.
        reachable(after)
        let asked = after.instance.statusCalls
        await controller.sendKept(.retry)
        XCTAssertEqual(after.instance.statusCalls, asked + 1)
        XCTAssertEqual(after.instance.sent, ["hello"])
        XCTAssertEqual(controller.unsentCount, 0)

        // The same for the take on its island, in this run.
        let world = World()
        let other = world.controller()
        await keepATake(world, other)
        world.instance.statusResult = .failure(Failure.signIn)
        await other.sendKept(.byItself)
        XCTAssertEqual(other.unsentCount, 1)
        XCTAssertEqual(world.audio?.discarded, false)
        XCTAssertEqual(world.activities.first?.ended?.reason, .signIn)
    }

    /// The POST is not made unless its marker reached the disk: a POST that
    /// began without one could be made again after a relaunch.
    func testNoPostWithoutItsMarkerOnDisk() async throws {
        let world = World()
        let controller = world.controller()
        await keepATake(world, controller)
        // The record can no longer be written (something sits in its place).
        let outbox = VoiceCaptureOutbox(directory: world.outboxDirectory)
        let entry = try XCTUnwrap(outbox.entries().first)
        let record = world.outboxDirectory.appending(
            path: "\(String(format: "%010d", entry.sequence))-\(entry.id.uuidString.lowercased()).json"
        )
        try FileManager.default.removeItem(at: record)
        try FileManager.default.createDirectory(at: record, withIntermediateDirectories: true)
        try Data().write(to: record.appending(path: "x"))

        reachable(world)
        await controller.sendKept(.byItself)
        XCTAssertEqual(world.instance.posts, 0, "no marker, no POST")
        XCTAssertTrue(world.instance.sent.isEmpty)
        XCTAssertEqual(controller.unsentCount, 1, "kept")
        XCTAssertEqual(world.audio?.discarded, false)
    }

    /// A send nobody asked for that outlasts the take's ten minutes (a slow
    /// transcription, the App suspended meanwhile) posts nothing: the take
    /// waits for Retry, with its words.
    func testAnAutomaticSendThatOutlastsTheWindowPostsNothing() async {
        let world = World()
        let start = Date()
        var clock = start
        world.now = { clock }
        let controller = world.controller()
        await keepATake(world, controller)
        clock = start.addingTimeInterval(9 * 60 + 59)
        reachable(world)
        world.instance.holdTranscribe = true
        let send = Task { await controller.sendKept(.byItself) }
        await world.instance.untilHolding()
        clock = start.addingTimeInterval(10 * 60 + 1)
        world.instance.holdTranscribe = false
        world.instance.release()
        await send.value
        XCTAssertEqual(world.instance.transcribed, 1)
        XCTAssertEqual(world.instance.posts, 0, "not posted after its window")
        XCTAssertEqual(controller.unsentCount, 1)
        XCTAssertNil(controller.retryTask)

        // Retry sends it, without transcribing again, and says when it was spoken.
        await controller.sendKept(.retry)
        XCTAssertEqual(world.instance.transcribed, 1)
        XCTAssertEqual(world.instance.sent, [VoiceCaptureLateNote.message("hello", spoken: start, now: clock)])
        XCTAssertEqual(controller.unsentCount, 0)
    }

    /// Discard in the alert deletes the takes the alert spoke of, not one
    /// kept since.
    func testDiscardInTheAlertLeavesATakeKeptSince() async {
        let world = World()
        world.inFront = true
        let controller = world.controller()
        await keepATake(world, controller)
        await controller.activated()
        XCTAssertEqual(world.unsentAlerts.map(\.count), [1])
        await keepATake(world, controller)
        XCTAssertEqual(controller.unsentCount, 2)
        world.unsentAlerts[0].answer(false)
        for _ in 0..<400 where controller.unsentCount > 1 { try? await Task.sleep(for: .milliseconds(5)) }
        XCTAssertEqual(controller.unsentCount, 1)
        XCTAssertEqual(controller.phase, .failed, "the newer take is still on its island")
        XCTAssertEqual(VoiceCaptureOutbox(directory: world.outboxDirectory).entries().map(\.sequence), [2])
    }

    /// A press right after a relaunch reads the outbox first: its take is
    /// not sent ahead of an earlier one that is still on its way.
    func testAPressBeforeTheOutboxWasReadDoesNotJumpTheQueue() async {
        let before = World()
        before.pressContext = "first"
        await keepATake(before, before.controller())

        let after = World()
        after.outboxDirectory = before.outboxDirectory
        after.pressContext = "second"
        let controller = after.controller()
        // No restore yet: the press itself reads what was left.
        await keepATake(after, controller)
        XCTAssertEqual(controller.unsentCount, 2)
        XCTAssertTrue(after.instance.sent.isEmpty)
        reachable(after)
        after.instance.transcripts = ["one", "two"]
        await controller.sendKept(.byItself)
        XCTAssertEqual(after.instance.sent, ["one", "two"])
        XCTAssertEqual(after.instance.captures.map { $0?.contains("[first") }, [true, false])
    }

    /// The alert is not put over a take being recorded, nor over itself.
    func testTheAlertWaitsWhileATakeIsBeingRecorded() async {
        let world = World()
        world.inFront = true
        let controller = world.controller()
        await keepATake(world, controller)
        // A new take is being recorded when Avibe becomes active.
        _ = await controller.toggle()
        XCTAssertEqual(controller.phase, .listening)
        await controller.activated()
        XCTAssertTrue(world.unsentAlerts.isEmpty)
        await controller.finish(send: false)
        await controller.activated()
        XCTAssertEqual(world.unsentAlerts.map(\.count), [1])
        await controller.activated()
        XCTAssertEqual(world.unsentAlerts.count, 1, "not over itself")
    }

    /// A take sent more than a minute after it was spoken says when, in its
    /// first line and in `spoken_at`; one sent sooner does not.
    func testATakeSentLateSaysWhenItWasSpoken() async {
        let world = World()
        let start = Date()
        var clock = start
        world.now = { clock }
        let controller = world.controller()
        await keepATake(world, controller)
        clock = start.addingTimeInterval(59)
        reachable(world)
        await controller.sendKept(.byItself)
        XCTAssertEqual(world.instance.sent, ["hello"])
        XCTAssertNil(world.instance.spokenAts.last ?? nil)

        clock = start.addingTimeInterval(120)
        await keepATake(world, controller)
        let spoken = clock
        clock = clock.addingTimeInterval(61)
        reachable(world)
        await controller.sendKept(.byItself)
        XCTAssertEqual(world.instance.sent.last, VoiceCaptureLateNote.message("hello", spoken: spoken, now: clock))
        XCTAssertTrue(world.instance.sent.last?.hasSuffix("\nhello") == true)
        XCTAssertEqual(world.instance.spokenAts.last ?? nil, ISO8601DateFormatter.string(
            from: spoken, timeZone: .current, formatOptions: [.withInternetDateTime]
        ))
        // The island still shows the words, not the note.
        XCTAssertEqual(world.activities.last?.ended?.heard, "hello")
    }

    func testTheLateNoteReadsAsDecided() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try XCTUnwrap(TimeZone(identifier: "Asia/Shanghai"))
        func date(_ day: Int, _ hour: Int, _ minute: Int) throws -> Date {
            try XCTUnwrap(calendar.date(from: DateComponents(year: 2026, month: 11, day: day, hour: hour, minute: minute)))
        }
        let zh = VoiceCaptureLateNote.parts(spoken: try date(19, 23, 10), language: "zh-Hans", calendar: calendar)
        XCTAssertEqual(zh.time, "23:10")
        XCTAssertEqual(zh.day, "11 月 19 日")
        let en = VoiceCaptureLateNote.parts(spoken: try date(19, 8, 44), language: "en", calendar: calendar)
        XCTAssertEqual(en.time, "08:44")
        XCTAssertEqual(en.day, "Nov 19")

        func format(_ language: String, _ key: String) throws -> String {
            let path = try XCTUnwrap(Bundle.main.path(forResource: language, ofType: "lproj"))
            return try XCTUnwrap(Bundle(path: path)).localizedString(forKey: key, value: nil, table: "VoiceCapture")
        }
        XCTAssertEqual(
            String(format: try format("zh-Hans", "voiceCapture.late.today %@"), "08:44"),
            "（这条语音说于今天 08:44，刚刚才送达。）"
        )
        XCTAssertEqual(
            String(format: try format("zh-Hans", "voiceCapture.late.day %@ %@"), zh.day, zh.time),
            "（这条语音说于 11 月 19 日 23:10，刚刚才送达。）"
        )
        XCTAssertEqual(
            String(format: try format("en", "voiceCapture.late.today %@"), "08:44"),
            "(Spoken today at 08:44; delivered just now.)"
        )
        XCTAssertEqual(
            String(format: try format("en", "voiceCapture.late.day %@ %@"), "Nov 19", "23:10"),
            "(Spoken on Nov 19 at 23:10; delivered just now.)"
        )
        // Today or another day, by the phone's calendar.
        let today = VoiceCaptureLateNote.line(
            spoken: try date(20, 8, 44), now: try date(20, 11, 52), language: "en", calendar: calendar
        )
        let other = VoiceCaptureLateNote.line(
            spoken: try date(19, 23, 10), now: try date(20, 0, 5), language: "en", calendar: calendar
        )
        XCTAssertNotEqual(today, other)
        XCTAssertTrue(today.contains("08:44"), today)
        XCTAssertTrue(other.contains("23:10"), other)
    }

    func testTheUnsentAlertAndNoticeReadAsDecided() throws {
        func format(_ language: String, _ key: String) throws -> String {
            let path = try XCTUnwrap(Bundle.main.path(forResource: language, ofType: "lproj"))
            return try XCTUnwrap(Bundle(path: path)).localizedString(forKey: key, value: nil, table: "VoiceCapture")
        }
        let now = Date(timeIntervalSince1970: 1_795_200_000)
        func age(_ seconds: TimeInterval, _ language: String) -> String {
            VoiceCaptureAlert.age(of: now.addingTimeInterval(-seconds), now: now, language: language)
        }
        // Chinese with a space between the number and the unit.
        XCTAssertEqual(age(3 * 3600 + 120, "zh-Hans"), "3 小时前")
        XCTAssertEqual(age(25 * 60 + 30, "zh-Hans"), "25 分钟前")
        XCTAssertEqual(age(20, "zh-Hans"), "1 分钟前", "never less than a minute")
        XCTAssertEqual(age(3 * 3600 + 120, "en"), "3 hours ago")
        XCTAssertEqual(age(25 * 60 + 30, "en"), "25 minutes ago")
        XCTAssertEqual(age(20, "en"), "1 minute ago")

        XCTAssertEqual(
            String(format: try format("zh-Hans", "voiceCapture.unsent.one %@"), "3 小时前"),
            "有一条 3 小时前的语音没能发出去。"
        )
        XCTAssertEqual(
            String(format: try format("en", "voiceCapture.unsent.one %@"), "3 hours ago"),
            "A voice message from 3 hours ago couldn't be sent."
        )
        XCTAssertEqual(
            String(format: try format("zh-Hans", "voiceCapture.unsent.many %lld %@"), 3, "3 小时前"),
            "有 3 条语音没能发出去，最早的是 3 小时前。"
        )
        XCTAssertEqual(
            String(format: try format("en", "voiceCapture.unsent.many %lld %@"), 3, "3 hours ago"),
            "3 voice messages couldn't be sent; the oldest is from 3 hours ago."
        )
        XCTAssertEqual(try format("zh-Hans", "voiceCapture.action.retryAll"), "全部重试")
        XCTAssertEqual(try format("en", "voiceCapture.action.retryAll"), "Retry all")
        XCTAssertEqual(try format("zh-Hans", "voiceCapture.unsent.notice"), "这条语音没能发出去，打开 Avibe 可以重试。")
        XCTAssertEqual(
            try format("en", "voiceCapture.unsent.notice"), "This voice message couldn't be sent. Open Avibe to retry."
        )
        XCTAssertEqual(try format("zh-Hans", "voiceCapture.phase.failed"), "没发出去")
        XCTAssertEqual(try format("zh-Hans", "voiceCapture.action.discard"), "放弃")
    }

    // MARK: What happened to a press that left no message leaves the phone

    /// A slip, a refused start and a kept take each leave a line; the next
    /// message that arrives carries them (no words, no audio), once.
    func testPressesThatLeftNoMessageRideOnTheNextOne() async {
        let world = World()
        world.pressContext = "background warm"
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 0.2
        _ = await controller.toggle()

        world.recorderStartError = VoiceCaptureRecorder.StartFailure(
            step: .recorder, underlying: NSError(domain: NSOSStatusErrorDomain, code: 561145187)
        )
        world.reports = [.mixableBackground: "rec[recorder=0 prep=1 in=0 engine=561145187(!rec)]"]
        _ = await controller.toggle()
        world.recorderStartError = nil
        world.reports = [:]
        XCTAssertEqual(world.earlier.count, 2)
        XCTAssertTrue(world.earlier[0].contains("short:0.2 0 [background warm bgTries=0]"), world.earlier[0])
        XCTAssertTrue(world.earlier[0].contains("stop="), world.earlier[0])
        XCTAssertTrue(
            world.earlier[1].contains("start:recorder 561145187 rec[recorder=0 prep=1 in=0 engine=561145187(!rec)]"),
            world.earlier[1]
        )
        XCTAssertFalse(world.earlier.joined().contains("hello"))

        // The answer to the next message is lost: they stay for the one after.
        var clock = Date().addingTimeInterval(60)
        world.now = { clock }
        world.instance.postFailures = 1
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertEqual(world.instance.earliers.last ?? nil, world.earlier.prefix(2).joined(separator: " | "))
        XCTAssertEqual(world.earlier.count, 3, "kept, with the lost send")

        clock = clock.addingTimeInterval(60)
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertEqual(world.instance.sent, ["hello"])
        XCTAssertEqual((world.instance.earliers.last ?? nil)?.components(separatedBy: " | ").count, 3)
        XCTAssertTrue(world.earlier.isEmpty, "sent once")

        clock = clock.addingTimeInterval(60)
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertNil(world.instance.earliers.last ?? nil)
    }

    /// Which branch read as "could not reach the instance", for the note.
    func testTheClientNamesWhyARequestDidNotReachTheInstance() {
        typealias Client = VoiceCaptureInstanceClient
        XCTAssertEqual(
            Client.unreachableCause(status: 0, errorCode: nil, fetchError: "AbortError", elapsed: 15.001, deadline: 15),
            "fetch0 AbortError 15001/15000ms"
        )
        XCTAssertEqual(
            Client.unreachableCause(status: 0, errorCode: nil, fetchError: "TypeError", elapsed: 0.012, deadline: 15),
            "fetch0 TypeError 12/15000ms"
        )
        XCTAssertEqual(
            Client.unreachableCause(status: 502, errorCode: nil, fetchError: nil, elapsed: 0.1, deadline: 15),
            "http 502 -"
        )
        XCTAssertNil(Client.unreachableCause(status: 503, errorCode: "asr_unavailable", fetchError: nil, elapsed: 0, deadline: 15))
        // Every status `check` reads as offline has a cause, and no other.
        for status in [0, 200, 400, 401, 403, 422, 500, 502, 503, 504, 520, 530, 599] {
            let response = Client.Response(status: status, json: nil, redirected: false)
            var offline = false
            do { try Client.check(response) } catch { offline = (error as? Client.Failure) == .offline }
            XCTAssertEqual(
                Client.unreachableCause(status: status, errorCode: nil, fetchError: nil, elapsed: 0, deadline: 1) != nil,
                offline, "\(status)"
            )
        }
        var failures: [String] = []
        for index in 0..<6 { failures = Client.adding("r\(index)", to: failures) }
        XCTAssertEqual(failures, ["r2", "r3", "r4", "r5"])
    }

    /// A take's transcription keeps one id however often it is sent.
    func testATakeKeepsOneDictationIdAcrossSends() throws {
        let target = VoiceCaptureTarget(
            accountID: "a", instanceID: "i",
            origin: try XCTUnwrap(WebOrigin.parseConfigured("https://instance-one.example")),
            storeIdentifier: UUID()
        )
        let client = VoiceCaptureInstanceClient(target: target)
        XCTAssertTrue(client.dictationID.hasPrefix("ios-action-"))
        XCTAssertNotEqual(client.dictationID, VoiceCaptureInstanceClient(target: target).dictationID)
        XCTAssertEqual(
            VoiceCaptureInstanceClient(target: target, dictationID: "ios-action-kept").dictationID, "ios-action-kept"
        )
    }

    // MARK: The hidden document waits for the direct path's load gate
    // (39.1, 00:44Z: its load was issued right after Avibe came forward,
    // while the App's local listener was closed or being bound again)

    func testInFrontAHeldGateIsWaitedForBeforeTheLoad() async {
        typealias Client = VoiceCaptureInstanceClient
        // Held for three reads, then open: the load goes once it opens.
        var gates: [DirectLoadGate] = [.held, .held, .held, .open]
        var slept: [Duration] = []
        var clock = ContinuousClock.now
        let sleep: (Duration) async -> Void = { slept.append($0); clock += $0 }
        var settled = await Client.settledGate(
            gate: { gates.count > 1 ? gates.removeFirst() : gates[0] },
            isActive: { true }, sleep: sleep, now: { clock }
        )
        XCTAssertEqual(settled, .open)
        XCTAssertEqual(slept, [.milliseconds(50), .milliseconds(50), .milliseconds(50)])

        // Never settling: five seconds by the clock, then the load goes anyway.
        slept = []
        settled = await Client.settledGate(gate: { .held }, isActive: { true }, sleep: sleep, now: { clock })
        XCTAssertEqual(settled, .held)
        XCTAssertEqual(slept.count, 100)
        // A busy main actor makes each step longer: still five seconds.
        slept = []
        settled = await Client.settledGate(
            gate: { .held }, isActive: { true },
            sleep: { slept.append($0); clock += .milliseconds(500) }, now: { clock }
        )
        XCTAssertEqual(slept.count, 10)

        // In the background the gate stays held as long as Avibe is there:
        // no wait, the load goes and WebKit takes its own path.
        slept = []
        settled = await Client.settledGate(gate: { .held }, isActive: { false }, sleep: sleep, now: { clock })
        XCTAssertEqual(settled, .held)
        XCTAssertTrue(slept.isEmpty)

        // The fault is terminal: not waited for.
        settled = await Client.settledGate(gate: { .fault }, isActive: { true }, sleep: sleep, now: { clock })
        XCTAssertEqual(settled, .fault)
        XCTAssertTrue(slept.isEmpty)
    }

    /// A faulted listener is its own reason, and the take is kept as for an
    /// unreachable instance (sent again by itself), not as a failed
    /// transcription.
    func testAFaultedListenerKeepsTheTakeLikeAnUnreachableInstance() async throws {
        let target = VoiceCaptureTarget(
            accountID: "a", instanceID: "i",
            origin: try XCTUnwrap(WebOrigin.parseConfigured("https://instance-one.example")),
            storeIdentifier: UUID()
        )
        let client = VoiceCaptureInstanceClient(target: target, gate: { .fault }, isActive: { true }, sleep: { _ in })
        do {
            _ = try await client.status()
            XCTFail("a faulted listener loads nothing")
        } catch {
            XCTAssertEqual(error as? Failure, .directFault)
        }
        XCTAssertEqual(client.failures.count, 1)
        XCTAssertTrue(client.failures[0].hasPrefix("GET /api/asr/status direct_fault +"), client.failures[0])

        let world = World()
        world.instance.transcribeResult = .failure(Failure.directFault)
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 3
        _ = await controller.toggle()
        // This process cannot reach it until Avibe is opened again: kept for
        // Retry, with no promise to send by itself and no retries.
        XCTAssertEqual(controller.phase, .failed)
        XCTAssertEqual(world.activities.first?.states.last?.reason, .offline)
        XCTAssertEqual(world.retryHolds, 0)
        XCTAssertEqual(world.instance.transcribed, 1)
        XCTAssertNil(controller.retryTask, "not sent again by itself")

        // The same when the consent ask meets the fault.
        let asked = World()
        asked.instance.statusResult = .failure(Failure.directFault)
        let other = asked.controller()
        _ = await other.toggle()
        asked.audio?.duration = 3
        _ = await other.toggle()
        XCTAssertEqual(asked.activities.first?.states.last?.reason, .offline)
        XCTAssertEqual(asked.retryHolds, 0)
    }

    func testOnlyAFirstLoadInFrontAroundAHeldGateIsMadeAgain() {
        typealias Client = VoiceCaptureInstanceClient
        XCTAssertTrue(Client.loadsAgain(attempt: 0, closed: false, active: true, heldBefore: true, heldAfter: false))
        XCTAssertTrue(Client.loadsAgain(attempt: 0, closed: false, active: true, heldBefore: false, heldAfter: true))
        XCTAssertFalse(Client.loadsAgain(attempt: 1, closed: false, active: true, heldBefore: true, heldAfter: true))
        // In the background the gate is always held: nothing is made twice.
        XCTAssertFalse(Client.loadsAgain(attempt: 0, closed: false, active: false, heldBefore: true, heldAfter: true))
        XCTAssertFalse(Client.loadsAgain(attempt: 0, closed: true, active: true, heldBefore: true, heldAfter: true))
        XCTAssertFalse(Client.loadsAgain(attempt: 0, closed: false, active: true, heldBefore: false, heldAfter: false))
    }

    /// Lines noted while a message was on its way stay for the next one,
    /// even when they pushed out lines that message carried.
    func testOnlyTheLinesAMessageCarriedAreDropped() {
        var lines = ["a", "b", "c", "d", "e"]
        let carried = lines
        lines = VoiceCaptureEarlier.adding("f", to: lines)
        lines = VoiceCaptureEarlier.adding("g", to: lines)
        XCTAssertEqual(lines, ["c", "d", "e", "f", "g"])
        XCTAssertEqual(VoiceCaptureEarlier.removing(carried, from: lines), ["f", "g"])
        // The same line twice is two lines.
        XCTAssertEqual(VoiceCaptureEarlier.removing(["x"], from: ["x", "x", "y"]), ["x", "y"])
    }

    /// The second recorder losing its input sends the take as far as it got.
    func testATakeThatLostItsInputIsSentAsFarAsItGot() async throws {
        let world = World()
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 2
        world.audio?.onInputLost?()
        for _ in 0..<400 where world.instance.sent.isEmpty { try await Task.sleep(for: .milliseconds(5)) }
        XCTAssertEqual(world.instance.sent, ["hello"])
        XCTAssertTrue((world.instance.timings.first ?? nil)?.contains(":input") == true)
    }

    func testOnlyTheLastFewLinesAreKeptEachCutShort() {
        var lines: [String] = []
        for index in 0..<7 { lines = VoiceCaptureEarlier.adding("\(index) " + String(repeating: "x", count: 900), to: lines) }
        XCTAssertEqual(lines.count, 5)
        XCTAssertEqual(lines.first?.prefix(2), "2 ")
        XCTAssertEqual(lines.map(\.count).max(), 700)
    }

    /// How a take was stopped is in its timing.
    func testTheTimingSaysWhatStoppedTheTake() async {
        let pressed = World()
        let controller = pressed.controller()
        _ = await controller.toggle()
        pressed.audio?.duration = 2
        _ = await controller.toggle()
        XCTAssertTrue((pressed.instance.timings.first ?? nil)?.contains(":press") == true)

        let island = World()
        let other = island.controller()
        _ = await other.toggle()
        island.audio?.duration = 2
        await other.finish(send: true)
        XCTAssertTrue((island.instance.timings.first ?? nil)?.contains(":island") == true)
    }

    /// Each step's time from the press rides along for device numbers.
    func testTheMessageCarriesEachStepsTimeFromThePress() async {
        let world = World()
        let controller = world.controller()
        _ = await controller.toggle()
        world.audio?.duration = 2
        _ = await controller.toggle()
        let timing = world.instance.timings.first ?? nil
        for step in ["island=", "session=", "record=", "audio="] {
            XCTAssertTrue(timing?.contains(step) == true, "\(step) in \(timing ?? "nil")")
        }

        let refused = World()
        refused.prepareErrors = [.mixableBackground: NSError(domain: NSOSStatusErrorDomain, code: 561015905)]
        let other = refused.controller()
        _ = await other.beginPress()
        _ = await other.startTake(inFront: false, foregroundFallback: true)
        other.markInFront()
        _ = await other.startTake(inFront: true)
        refused.audio?.duration = 2
        _ = await other.toggle()
        let fallback = refused.instance.timings.first ?? nil
        for step in ["refused=", "front=", "frontIsland=", "frontSession=", "frontRecord=", "audio="] {
            XCTAssertTrue(fallback?.contains(step) == true, "\(step) in \(fallback ?? "nil")")
        }
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

    func testTheSwitchIsAHiddenURLOnly() throws {
        // Its own preferences, never the host App's.
        let suite = "voice-capture-switch-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        func handle(_ string: String, allowed: Bool = true) -> Bool {
            VoiceCaptureEndSwitch.handle(URL(string: string)!, allowed: allowed, defaults: defaults)
        }
        XCTAssertFalse(handle("https://avibe.bot/voice/end-after-background?on=1"))
        XCTAssertFalse(handle("avibe-diagnostics://voice/other?on=1"))
        XCTAssertFalse(handle("avibe-diagnostics://voice/end-after-background?on=1", allowed: false), "an App Store build ignores it")
        XCTAssertFalse(VoiceCaptureEndSwitch.isOn(in: defaults))
        XCTAssertTrue(handle("avibe-diagnostics://voice/end-after-background?on=1"))
        XCTAssertTrue(VoiceCaptureEndSwitch.isOn(in: defaults))
        XCTAssertTrue(handle("avibe-diagnostics://voice/end-after-background?on=0"))
        XCTAssertFalse(VoiceCaptureEndSwitch.isOn(in: defaults))
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
        XCTAssertEqual(try value("zh-Hans"), "正在录音，再长按一次操作按钮发送")
        XCTAssertEqual(try value("en"), "Recording. Press and hold the Action Button again to send.")
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
        XCTAssertEqual(try format("zh-Hans", "voiceCapture.reason.willSend"), "还没发出去，连上你的 Avibe 后会自动发送。")
        XCTAssertEqual(try format("en", "voiceCapture.reason.willSend"), "Not sent yet. It'll send once your Avibe is reachable.")
        XCTAssertTrue(VoiceCaptureDetail.retryable(.willSend))
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
    var reports: [VoiceCaptureSessionCandidate: String] = [:]
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
    /// Background holds for a kept take's retries now open.
    var retryHolds = 0
    /// Presses that left no message (`capture_earlier`).
    var earlier: [String] = []
    /// Where takes that are not sent yet are kept; two Worlds on one
    /// directory are two runs of the App.
    var outboxDirectory = FileManager.default.temporaryDirectory.appending(path: "outbox-\(UUID().uuidString)")
    /// The dictation id each client was made with (nil: a new take).
    var madeWith: [String?] = []
    /// The unsent notice as scheduled (nil: removed), the alerts shown in
    /// front (how many takes, and the answer to give), and lines said.
    var unsentNotices: [Date?] = []
    var unsentAlerts: [(count: Int, oldest: Date, answer: (Bool?) -> Void)] = []
    var unsentAlertShown = false
    var said: [VoiceCaptureAttributes.Reason] = []

    /// Waits asked for; a retry's wait is held until `wakeRetry()`, and a
    /// kept take's island wait until `wakeIsland()`.
    var slept: [Duration] = []
    private var wakes = 0
    private var retryWaits = 0
    private var islandWakes = 0
    private var islandWaits = 0

    /// Ends the wait for a kept take's island to go, once one is under way.
    func wakeIsland(file: StaticString = #filePath, line: UInt = #line) async {
        let woken = islandWoken
        for _ in 0..<400 {
            if islandWaits > 0 { islandWakes += 1 }
            try? await Task.sleep(for: .milliseconds(5))
            if islandWoken > woken { return }
        }
        XCTFail("no island is waiting", file: file, line: line)
    }

    /// Ends the retry wait under way, once one is (a retry that was never
    /// scheduled fails the test instead of hanging it). A wait that was
    /// cancelled does not count: the one that replaced it is woken.
    func wakeRetry(file: StaticString = #filePath, line: UInt = #line) async {
        let woken = retryWoken
        for _ in 0..<400 {
            if retryWaits > 0 { wakes += 1 }
            try? await Task.sleep(for: .milliseconds(5))
            if retryWoken > woken { return }
        }
        XCTFail("no retry is waiting", file: file, line: line)
    }
    private var retryWoken = 0
    private var islandWoken = 0

    func controller() -> VoiceCaptureController {
        instance.log = { self.events.append($0) }
        return VoiceCaptureController(environment: VoiceCaptureEnvironment(
            target: { self.target },
            makeInstance: { _, dictationID in
                self.madeWith.append(dictationID)
                return self.instance
            },
            makeAudio: { _ in
                if self.takeFileFails { throw CocoaError(.fileWriteNoPermission) }
                let audio = FakeAudio()
                audio.startError = self.recorderStartError
                audio.prepareErrors = self.prepareErrors
                audio.startErrors = self.startErrors
                audio.reports = self.reports
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
            holdWhileRetrying: {
                self.retryHolds += 1
                return { self.retryHolds -= 1 }
            },
            noteEarlier: { self.earlier = VoiceCaptureEarlier.adding($0, to: self.earlier) },
            earlier: { self.earlier },
            dropEarlier: { self.earlier = VoiceCaptureEarlier.removing($0, from: self.earlier) },
            outbox: VoiceCaptureOutbox(directory: self.outboxDirectory),
            scheduleUnsentNotice: { self.unsentNotices.append($0) },
            presentUnsent: { count, oldest, answer in
                self.unsentAlertShown = true
                self.unsentAlerts.append((count, oldest, { reply in
                    self.unsentAlertShown = false
                    answer(reply)
                }))
            },
            unsentAlertIsShown: { self.unsentAlertShown },
            sayInFront: { self.said.append($0) },
            now: { self.now() },
            sleep: { duration in
                self.slept.append(duration)
                await Task.yield()
                if duration > .seconds(300) {
                    // A kept take's island (ten minutes): until the test
                    // ends it, or it is cancelled.
                    let ticket = self.islandWakes
                    self.islandWaits += 1
                    while self.islandWakes == ticket, !Task.isCancelled {
                        try? await Task.sleep(for: .milliseconds(5))
                    }
                    self.islandWaits -= 1
                    if !Task.isCancelled { self.islandWoken += 1 }
                    return
                }
                guard duration <= .seconds(60) else {
                    // Every other wait never ends in a test.
                    try? await Task.sleep(for: .seconds(3600))
                    return
                }
                // A retry's wait (a minute at most): until the test wakes
                // it, or it is cancelled.
                let ticket = self.wakes
                self.retryWaits += 1
                while self.wakes == ticket, !Task.isCancelled {
                    try? await Task.sleep(for: .milliseconds(5))
                }
                self.retryWaits -= 1
                if !Task.isCancelled { self.retryWoken += 1 }
            }
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
    var holdPost = false
    /// What the post throws once released (a client closed meanwhile).
    var postError: Error?
    var transcribed = 0
    var sent: [String] = []
    var captures: [String?] = []
    var closed = false
    var log: (String) -> Void = { _ in }
    private var held: CheckedContinuation<Void, Never>?

    var statusCalls = 0

    func status() async throws -> VoiceCaptureInstanceClient.Status {
        statusCalls += 1
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
        if !transcripts.isEmpty { return transcripts.removeFirst() }
        return try transcribeResult.get()
    }

    func release() {
        held?.resume()
        held = nil
    }

    /// Whether a request is suspended, waiting for `release()`.
    var isHolding: Bool { held != nil }

    /// Waits for a request to be held (a hold that never comes fails the
    /// test instead of hanging it).
    func untilHolding(file: StaticString = #filePath, line: UInt = #line) async {
        for _ in 0..<400 where held == nil { try? await Task.sleep(for: .milliseconds(5)) }
        if held == nil { XCTFail("no request is held", file: file, line: line) }
    }

    func mainBotMessagesPath() async throws -> String {
        if pathFailures > 0 {
            pathFailures -= 1
            throw VoiceCaptureInstanceClient.Failure.offline
        }
        return "/api/sessions/ses_main"
    }

    var timings: [String?] = []

    var earliers: [String?] = []

    var spokenAts: [String?] = []

    func postMessage(
        _ text: String, to sessionPath: String, capture: String?, timing: String?, earlier: String?,
        spokenAt: String?
    ) async throws {
        captures.append(capture)
        timings.append(timing)
        earliers.append(earlier)
        spokenAts.append(spokenAt)
        if holdPost {
            await withCheckedContinuation { held = $0 }
        }
        if let postError { throw postError }
        posts += 1
        if postFailures > 0 {
            postFailures -= 1
            throw VoiceCaptureInstanceClient.Failure.offline
        }
        sent.append(text)
    }

    func close() { closed = true }
    var releaseBarrier: WebsiteDataStoreReleaseBarrier? { nil }
    var failures: [String] = []
    var dictationID = "ios-action-fake"
    /// What each transcription returns, in order (then `transcribeResult`).
    var transcripts: [String] = []
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
    var onInputLost: (() -> Void)?
    var log: (String) -> Void = { _ in }
    /// What each candidate's recorder reports (`rec[...]`).
    var reports: [VoiceCaptureSessionCandidate: String] = [:]
    private var lastPrepared: VoiceCaptureSessionCandidate?
    var report: String? { lastPrepared.flatMap { reports[$0] } }

    func prepare(_ candidate: VoiceCaptureSessionCandidate) throws {
        log("prepare\(candidate.rawValue)")
        if let error = prepareErrors[candidate] { throw error }
        prepared = candidate
        lastPrepared = candidate
    }

    var firstAudioAt: Date?
    var firstAudioLate = false

    func start() throws {
        log("record")
        if let startError { throw startError }
        if let prepared, let error = startErrors[prepared] { throw error }
        started = true
        firstAudioAt = Date()
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
    /// Polls of `currentTime` before it turns positive (0: at once).
    var silentPolls = 0
    var recording: Bool?
    var currentTime: TimeInterval {
        if silentPolls > 0 { silentPolls -= 1; return 0 }
        return 1
    }
    var isRecording: Bool { recording ?? records }
    func record() -> Bool { records }
    var stops = 0
    func stop() { stops += 1 }
}

private final class FakePreparableEngine: VoiceCaptureRecordingEngine, VoiceCapturePreparable {
    var currentTime: TimeInterval { 0 }
    var isRecording: Bool { false }
    func record() -> Bool { false }
    func stop() {}
    func prepareToRecord() -> Bool { true }
}

private final class FakeStartingEngine: VoiceCaptureStartingEngine {
    var detail = "vp=1"
    var onInputLost: (() -> Void)?
    var failure: Error?
    var begun = false
    var currentTime: TimeInterval { begun ? 1 : 0 }
    var isRecording: Bool { begun }
    func record() -> Bool { (try? begin()) != nil }
    func begin() throws {
        if let failure { throw failure }
        begun = true
    }
    func stop() { begun = false }
}
