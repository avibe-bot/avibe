import ActivityKit
import AVFoundation
import Foundation
import OSLog
import UIKit

/// What a take needs from the selected instance.
@MainActor
protocol VoiceCaptureInstance: AnyObject {
    func status() async throws -> VoiceCaptureInstanceClient.Status
    func transcribe(audio: Data, name: String, mime: String) async throws -> String
    func mainBotMessagesPath() async throws -> String
    /// `capture` records which session candidate recorded this take (and
    /// which were refused first), in the message's metadata only.
    func postMessage(_ text: String, to sessionPath: String, capture: String?) async throws
    func close()
    var releaseBarrier: WebsiteDataStoreReleaseBarrier? { get }
}

extension VoiceCaptureInstanceClient: VoiceCaptureInstance {}

/// One take's recording.
protocol VoiceCaptureAudio: AnyObject {
    var url: URL { get }
    /// Sets up and activates the session for one candidate; records nothing.
    func prepare(_ candidate: VoiceCaptureSessionCandidate) throws
    /// Starts recording on the prepared session.
    func start() throws
    /// Stops and returns the take's length in seconds.
    @discardableResult func stop() -> TimeInterval
    func discard()
}

extension VoiceCaptureRecorder: VoiceCaptureAudio {}

/// One take's Live Activity.
@MainActor
protocol VoiceCaptureActivityHandle: AnyObject {
    /// The system's identifier, to tell this process's take from a leftover.
    var id: String { get }
    /// What it shows now (nil when the system no longer has it).
    var currentState: VoiceCaptureAttributes.ContentState? { get }
    func update(_ state: VoiceCaptureAttributes.ContentState) async
    func end(_ state: VoiceCaptureAttributes.ContentState?, dismissal: ActivityUIDismissalPolicy) async
}

@MainActor
private final class LiveVoiceCaptureActivity: VoiceCaptureActivityHandle {
    let activity: Activity<VoiceCaptureAttributes>

    init(_ activity: Activity<VoiceCaptureAttributes>) {
        self.activity = activity
    }

    var id: String { activity.id }

    var currentState: VoiceCaptureAttributes.ContentState? {
        switch activity.activityState {
        case .active, .stale: return activity.content.state
        default: return nil
        }
    }

    /// Past this the island draws the take as interrupted by itself, so a
    /// take whose process is gone never shows a timer that runs on.
    static func staleDate(for state: VoiceCaptureAttributes.ContentState) -> Date? {
        switch state.phase {
        case .listening:
            return state.startedAt.addingTimeInterval(VoiceCaptureController.staleAfter)
        case .sending:
            // Each send attempt (Retry too) gets its own window.
            return Date().addingTimeInterval(VoiceCaptureController.sendStaleAfter)
        default:
            return nil
        }
    }

    func update(_ state: VoiceCaptureAttributes.ContentState) async {
        await activity.update(ActivityContent(
            state: state, staleDate: Self.staleDate(for: state), relevanceScore: 100
        ))
    }

    func end(_ state: VoiceCaptureAttributes.ContentState?, dismissal: ActivityUIDismissalPolicy) async {
        await activity.end(
            state.map { ActivityContent(state: $0, staleDate: nil) },
            dismissalPolicy: dismissal
        )
    }
}

/// Everything the controller reaches outside itself; tests replace it.
@MainActor
struct VoiceCaptureEnvironment {
    var target: () -> VoiceCaptureTarget?
    var makeInstance: (VoiceCaptureTarget) -> VoiceCaptureInstance
    var makeAudio: (UUID) throws -> VoiceCaptureAudio
    var microphoneAllowed: () -> Bool
    var activitiesEnabled: () -> Bool
    var requestActivity: (VoiceCaptureAttributes.ContentState) throws -> VoiceCaptureActivityHandle
    /// Every Live Activity of ours the system still shows, including one a
    /// process that is gone left behind.
    var existingActivities: () -> [VoiceCaptureActivityHandle]
    /// Deletes recordings a gone process left behind.
    var discardLeftoverAudio: () -> Void
    /// Posts the failure as a notification that stays in Notification
    /// Center, so one press yields a line the user can send back.
    var postFailureNotice: (VoiceCaptureAttributes.ContentState) -> Void
    /// The App's state when a press arrives: `active`, `inactive` or
    /// `background`, and `cold` (this process was launched by the press) or
    /// `warm`.
    var pressContext: () -> String
    /// Whether Avibe is in front (an alert, not the island, then says what
    /// went wrong: the island is not drawn over the App).
    var isInFront: () -> Bool
    /// Waits, briefly, for Avibe to be active after the press brought it
    /// forward, so the session activates at the first moment it may.
    var untilActive: () async -> Void
    /// The moment capture really starts: a haptic, and in front the line
    /// 正在录音，再长按一次侧边键发送. Never shown before.
    var captureStarted: (_ inFront: Bool) -> Void
    /// Says a refusal in front: the main Bot chat for what the App must ask
    /// (voice consent, sign-in), and an alert with the reason.
    var presentInFront: (_ reason: VoiceCaptureAttributes.Reason, _ needsApp: Bool) -> Void
    /// Variant (a), behind a hidden switch (default off): end this process
    /// shortly after a background capture is sent, so the next press gets a
    /// process launched for it (iOS lets such a process start audio once).
    var endProcessAfterBackgroundCapture: () -> Bool
    /// Whether a scene of this process was ever active (shown).
    var processEverShown: () -> Bool
    /// Other work that must not be cut off (nil when none), e.g. `push`.
    var otherWorkInFlight: () -> String?
    /// Waits before ending the process (about 1 s).
    var waitBeforeEnding: () async -> Void
    /// Keeps the process running while it waits to end (a background task
    /// assertion); returns what releases it.
    var holdWhileEnding: () -> () -> Void
    var endProcess: () -> Void
    /// Keeps the decision for the next press's `capture_attempt`.
    var noteEndDecision: (String) -> Void
    var now: () -> Date
    var sleep: (Duration) async -> Void

    static let live = VoiceCaptureEnvironment(
        target: { VoiceCaptureTarget.current() },
        makeInstance: { VoiceCaptureInstanceClient(target: $0) },
        makeAudio: { VoiceCaptureRecorder(url: try VoiceCaptureRecorder.takeURL(id: $0)) },
        microphoneAllowed: { VoiceCaptureRecorder.hasPermission },
        activitiesEnabled: { ActivityAuthorizationInfo().areActivitiesEnabled },
        requestActivity: { state in
            LiveVoiceCaptureActivity(
                try Activity.request(
                    attributes: VoiceCaptureAttributes(botName: nil),
                    content: ActivityContent(
                        state: state,
                        staleDate: LiveVoiceCaptureActivity.staleDate(for: state),
                        relevanceScore: 100
                    )
                )
            )
        },
        existingActivities: {
            Activity<VoiceCaptureAttributes>.activities.map(LiveVoiceCaptureActivity.init)
        },
        discardLeftoverAudio: { VoiceCaptureRecorder.discardAll() },
        postFailureNotice: { VoiceCaptureFailureNotification.post($0) },
        pressContext: { VoiceCaptureLaunch.pressContext() },
        isInFront: { UIApplication.shared.applicationState == .active },
        untilActive: {
            for _ in 0..<40 where UIApplication.shared.applicationState != .active {
                try? await Task.sleep(for: .milliseconds(25))
            }
        },
        captureStarted: { inFront in
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            if inFront {
                VoiceCaptureBanner.show(String(localized: VoiceCaptureCopy.text("voiceCapture.startedInApp")))
            }
        },
        presentInFront: { reason, needsApp in
            if needsApp {
                // The main Bot's conversation, where the Web UI asks.
                InstancePathRequests.shared.open(path: OpenMainBotIntent.path)
            }
            VoiceCaptureAlert.present(reason)
        },
        endProcessAfterBackgroundCapture: { VoiceCaptureEndSwitch.isOn },
        processEverShown: { VoiceCaptureLaunch.everShown },
        otherWorkInFlight: {
            if UIApplication.shared.applicationState != .background { return "front" }
            if AvibeAppDelegate.healthSync.hasWorkInFlight { return "health" }
            if NativePushCoordinator.current?.hasWorkInFlight == true { return "push" }
            return nil
        },
        waitBeforeEnding: { try? await Task.sleep(for: .seconds(1)) },
        holdWhileEnding: {
            var id = UIBackgroundTaskIdentifier.invalid
            id = UIApplication.shared.beginBackgroundTask(withName: "voice-capture-end") {
                UIApplication.shared.endBackgroundTask(id)
                id = .invalid
            }
            return {
                guard id != .invalid else { return }
                UIApplication.shared.endBackgroundTask(id)
                id = .invalid
            }
        },
        endProcess: { exit(0) },
        noteEndDecision: { VoiceCaptureEndSwitch.noteDecision($0) },
        now: Date.init,
        sleep: { try? await Task.sleep(for: $0) }
    )
}

/// One background voice take at a time: from the Action Button press,
/// through the Live Activity, to the message the main Bot receives.
///
/// Every entry (the intent, the island's buttons) runs in the App's process
/// on the main actor, so the state below has one writer.
@MainActor
final class VoiceCaptureController {
    static let shared = VoiceCaptureController(environment: .live)

    enum Outcome: Equatable {
        case started
        case stopped
        case busy
        case needsApp(VoiceCaptureAttributes.Reason)
        /// The background try was refused; the take stays reserved. Bring
        /// Avibe forward and call `startTake(inFront: true)`, or
        /// `foregroundUnavailable()`.
        case needsForeground

        var isNeedsApp: Bool {
            if case .needsApp = self { return true }
            return false
        }
    }


    /// The background try a press made before it came forward.
    private var backgroundAttempt: VoiceCaptureAttributes.Attempt?
    /// When the reserved press arrived: a press that waits longer than
    /// `foregroundWait` to come forward (a locked phone unlocked later)
    /// never records.
    private var reservedAt: Date?
    static let foregroundWait: TimeInterval = 30
    /// Background activations this process has attempted: iOS lets a
    /// process it launched in the background start audio once.
    private var backgroundActivations = 0

    /// Why the instance will not take this voice input: shown as a notice
    /// (or in front, an alert); `needsApp` when the App must ask (voice
    /// consent, sign-in) in the main Bot chat.
    struct Refusal: Equatable {
        let reason: VoiceCaptureAttributes.Reason
        let needsApp: Bool
        /// The instance could not be reached (or answered with an error):
        /// not a no. The take is kept on the phone for Retry, which asks
        /// again before anything is sent.
        var transient = false
    }

    /// The App's state at the press that is starting, for the message.
    private var pressContext = ""
    /// The account generation a press reserved its take under.
    private var reservedGeneration = 0
    /// The press holding the reservation: an older intent resuming late
    /// cannot take over a newer press's reservation.
    private(set) var reservation: UUID?

    /// The longest take; it is then sent as if the user stopped it.
    static let maximumDuration: Duration = .seconds(5 * 60)
    /// The code the system answered (an OSStatus such as
    /// `cannotStartRecording`, or a Cocoa file error); nil for Avibe's own
    /// errors, whose numbers would mean nothing (`record()` returning false).
    static func systemCode(_ error: Error) -> Int? {
        if let failure = error as? VoiceCaptureRecorder.StartFailure {
            return systemCode(failure.underlying)
        }
        if error is VoiceCaptureRecorder.Failure { return nil }
        // As the system gave it (an OSStatus such as 561145187, a Cocoa
        // code, or ActivityKit's); the step says whose it is.
        return (error as NSError).code
    }

    /// The error as shown: an OSStatus, Cocoa or AVFoundation code as its
    /// number; anything else (ActivityKit's errors are Swift enums, whose
    /// bridged number is only the case's position) by its type and case.
    static func errorText(_ error: Error) -> String? {
        let error = (error as? VoiceCaptureRecorder.StartFailure)?.underlying ?? error
        if error is VoiceCaptureRecorder.Failure { return nil }
        let ns = error as NSError
        if [NSOSStatusErrorDomain, NSCocoaErrorDomain, AVFoundationErrorDomain].contains(ns.domain) {
            return String(ns.code)
        }
        // "ActivityKit.ActivityAuthorizationError.visibility" → its type and case.
        let reflected = String(reflecting: error)
        let name = reflected.hasPrefix("Error Domain=")
            ? String(ns.domain.split(separator: ".").last ?? Substring(ns.domain))
            : reflected.split(separator: ".").suffix(2).joined(separator: ".")
        return String("\(name) \(ns.code)".prefix(80))
    }

    /// A run this soon after a failed start is the same press again.
    static let repeatWindow: TimeInterval = 1
    /// When a start last failed with a notice (see `repeatWindow`).
    private var failedStartAt: Date?
    /// When a listening or sending island goes stale on its own: the cap
    /// plus time to send. Only a take whose process is gone gets there.
    static let staleAfter: TimeInterval = 5 * 60 + 90
    /// How long one send attempt may show 正在发送 before it goes stale.
    static let sendStaleAfter: TimeInterval = 120
    /// A press this soon after launch cleanup ended a running leftover is
    /// the press that launched the App: it ends that take, not starts one.
    static let launchPressWindow: TimeInterval = 10
    /// A take shorter than this is treated as a slip and discarded.
    static let minimumDuration: TimeInterval = 0.6
    /// How long the island keeps a sent take before it goes.
    static let sentDismissal: TimeInterval = 4
    /// How long a failed take stays offered for Retry.
    static let failedDismissal: Duration = .seconds(10 * 60)
    /// How long a notice (needs the App, an older Avibe) stays.
    static let noticeDismissal: TimeInterval = 30

    private struct Take {
        let id: UUID
        let instance: VoiceCaptureInstance
        let audio: VoiceCaptureAudio
        let activity: VoiceCaptureActivityHandle
        let startedAt: Date
        /// How it recorded (3: in the App; 0: in the background, where Avibe
        /// cannot come forward) with the App's state at the press.
        let capture: String
        /// The instance's answer on voice input, asked once recording has
        /// begun so no word is lost: the audio stays on the phone until it
        /// is a yes, and is discarded unsent on a no (5.1.2(i)).
        var consent: Task<Refusal?, Never>
        /// Recorded in the background (not in front): the process may then
        /// end after it is sent, behind the switch.
        let inBackground: Bool
        var endedAt: Date?
        /// Kept once transcribed, so Retry does not transcribe again.
        var text: String?
    }

    enum Phase: Equatable {
        case idle, preparing, listening, sending, failed
    }

    private enum State {
        case idle
        case preparing
        case listening(Take)
        case sending(Take)
        case failed(Take)
    }

    private static let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "bot.avibe.ios",
        category: "VoiceCapture"
    )

    private let environment: VoiceCaptureEnvironment
    private var state: State = .idle
    /// Raised by an account change; a start that was waiting on the
    /// instance when it changed gives up instead of recording.
    private var generation = 0
    private var maximumDurationTask: Task<Void, Never>?
    private var interruptionObserver: NSObjectProtocol?

    init(environment: VoiceCaptureEnvironment) {
        self.environment = environment
    }

    var phase: Phase {
        switch state {
        case .idle: return .idle
        case .preparing: return .preparing
        case .listening: return .listening
        case .sending: return .sending
        case .failed: return .failed
        }
    }

    // MARK: Entry points

    /// What a press does before anything starts. A run the system repeats,
    /// a press that ends a leftover, a stop, or a busy press is handled here
    /// and returned. Nil when this press starts a take: it is reserved
    /// (`.preparing`) for `startTake(inFront:)`, which the intent calls once
    /// it has brought Avibe forward.
    func beginPress() async -> Outcome? {
        // A reservation whose intent ended without coming forward (or
        // saying so) is abandoned once the wait is over.
        if case .preparing = state, let reservedAt,
           environment.now().timeIntervalSince(reservedAt) >= Self.foregroundWait
        {
            record("an abandoned press expired")
            state = .idle
            backgroundAttempt = nil
        }
        // The system may run the intent twice for one press: a run right
        // after a start that failed and said so does nothing more.
        if case .idle = state, let failedStartAt,
           environment.now().timeIntervalSince(failedStartAt) < Self.repeatWindow
        {
            record("ignored a run right after a failed start")
            return .stopped
        }
        // A take this process does not hold is a leftover of one whose
        // process is gone (killed, or ended by the system): its recording is
        // lost. A press then ends it and says so; the next press starts anew.
        if case .idle = state {
            let ended = await endLeftovers(reason: .interrupted)
            let byLaunch = launchEndedRunningLeftoverAt.map {
                environment.now().timeIntervalSince($0) < Self.launchPressWindow
            } ?? false
            launchEndedRunningLeftoverAt = nil
            if ended || byLaunch {
                return .stopped
            }
        }
        switch state {
        case .listening:
            await finish(send: true)
            return .stopped
        case .preparing, .sending:
            return .busy
        case .failed(let take):
            // A new press replaces a failed take the user did not retry.
            state = .idle
            await end(take, dismissal: .immediate)
        case .idle:
            break
        }
        guard case .idle = state else { return .busy }
        state = .preparing
        reservedGeneration = generation
        reservation = UUID()
        reservedAt = environment.now()
        backgroundAttempt = nil
        pressContext = environment.pressContext() + " bgTries=\(backgroundActivations)"
        return nil
    }

    /// The whole press where Avibe does not come forward (before iOS 26, or
    /// when the system declines), and in tests.
    func toggle() async -> Outcome {
        if let handled = await beginPress() { return handled }
        return await startTake(inFront: false)
    }

    /// The island's Send (`send: true`) or Cancel, or the second press.
    /// Cancel on a failed take discards it.
    func finish(send: Bool) async {
        guard case .listening(var take) = state else {
            if !send, case .failed(let take) = state {
                state = .idle
                await end(take, dismissal: .immediate)
            } else if case .idle = state {
                // The island's button for a take no process holds.
                _ = await endLeftovers(reason: send ? .interrupted : nil)
            }
            return
        }
        stopObservingInterruptions()
        maximumDurationTask?.cancel()
        maximumDurationTask = nil
        let duration = take.audio.stop()
        take.endedAt = environment.now()
        record("stop send=\(send) duration=\(String(format: "%.1f", duration))")
        guard send, duration >= Self.minimumDuration else {
            state = .idle
            await end(take, dismissal: .immediate)
            return
        }
        await deliver(take)
    }

    /// Retry on a failed take: send the kept audio (or text) again.
    func retry() async {
        guard case .failed(let take) = state else {
            if case .idle = state { _ = await endLeftovers(reason: .interrupted) }
            return
        }
        await deliver(take)
    }

    /// At launch: whatever this process does not hold is a leftover.
    func endLeftoversAtLaunch() async {
        guard case .idle = state else { return }
        if await endLeftovers(reason: .interrupted) {
            launchEndedRunningLeftoverAt = environment.now()
        }
    }

    /// When launch cleanup last ended a take that was still running.
    private var launchEndedRunningLeftoverAt: Date?

    /// Ends every Live Activity of ours this process does not hold, and the
    /// audio such a take left. A listening or sending one is shown as
    /// interrupted for a moment (`reason`), anything else goes at once.
    /// Returns whether one was still listening or sending.
    private func endLeftovers(reason: VoiceCaptureAttributes.Reason?) async -> Bool {
        // Taken before any wait, while nothing is recording, so a take that
        // starts meanwhile keeps its own activity and audio.
        let held = heldActivityID
        let leftovers = environment.existingActivities().filter { $0.id != held }
        environment.discardLeftoverAudio()
        var wasRunning = false
        for activity in leftovers {
            guard let shown = activity.currentState else { continue }
            let running = shown.phase == .listening || shown.phase == .sending
            wasRunning = wasRunning || running
            if running, let reason {
                let now = environment.now()
                // A sending take may already have reached the main Bot: say
                // so, rather than that nothing was sent.
                let ended = shown.phase == .sending
                    ? VoiceCaptureAttributes.ContentState(
                        phase: .needsApp, startedAt: shown.startedAt, endedAt: now, reason: .sendUnknown
                    )
                    : VoiceCaptureAttributes.ContentState(
                        phase: .failed, startedAt: shown.startedAt, endedAt: now, reason: reason
                    )
                await activity.end(
                    ended,
                    dismissal: .after(now.addingTimeInterval(Self.sentDismissal))
                )
            } else {
                await activity.end(nil, dismissal: .immediate)
            }
        }
        if wasRunning { record("ended a leftover take") }
        return wasRunning
    }

    private var heldActivityID: String? {
        switch state {
        case .listening(let take), .sending(let take), .failed(let take): return take.activity.id
        case .idle, .preparing: return nil
        }
    }

    /// The account is signing out or changing: stop and discard any take,
    /// release its WebView, and keep nothing for Retry. Returns the WebViews
    /// the store transition waits for.
    func cancelForAccountTransition() -> [WebsiteDataStoreReleaseBarrier] {
        generation += 1
        backgroundAttempt = nil
        maximumDurationTask?.cancel()
        maximumDurationTask = nil
        stopObservingInterruptions()
        let take: Take?
        switch state {
        case .listening(let current), .sending(let current), .failed(let current):
            take = current
        case .idle, .preparing:
            take = nil
        }
        state = .idle
        guard let take else { return [] }
        record("account transition: the take is given up")
        take.consent.cancel()
        let barrier = take.instance.releaseBarrier
        take.audio.stop()
        take.instance.close()
        take.audio.discard()
        let activity = take.activity
        Task { await activity.end(nil, dismissal: .immediate) }
        return barrier.map { [$0] } ?? []
    }

    /// Opens the main Bot's conversation, where the Web UI asks for
    /// whatever is missing (voice consent, the microphone, sign-in).
    func openMainBotChat() {
        InstancePathRequests.shared.open(path: OpenMainBotIntent.path)
    }

    /// Shows a "needs the App" notice when the intent cannot bring the App
    /// to the front itself (before iOS 26, or when the system declines).
    func showNeedsApp(_ reason: VoiceCaptureAttributes.Reason) {
        showNotice(phase: .needsApp, reason: reason)
    }

    // MARK: Take lifecycle

    private func requestListeningIsland(_ startedAt: Date) throws -> VoiceCaptureActivityHandle {
        do {
            return try environment.requestActivity(
                VoiceCaptureAttributes.ContentState(phase: .listening, startedAt: startedAt)
            )
        } catch {
            record("start: activity request failed \((error as NSError).domain) \((error as NSError).code) \(error)")
            throw error
        }
    }

    /// Starts the take a press reserved, recording at the first moment it
    /// may: in front (`inFront`, iOS 26: the intent brought Avibe forward)
    /// as soon as Avibe is active. The instance's answer on voice input is
    /// asked once recording has begun; until it is a yes the audio stays on
    /// the phone, and a no discards it unsent.
    ///
    /// On the owner's iPhone a background-launched App is refused the
    /// microphone (11-19, 38.1: `!int` for `.record`, `!pla` for a mixable
    /// session, with no other audio playing), so in front is the way.
    func startTake(inFront: Bool, foregroundFallback: Bool = false, reservation press: UUID? = nil) async -> Outcome {
        guard case .preparing = state, generation == reservedGeneration,
              press == nil || press == reservation
        else { return .stopped }
        if inFront, let reservedAt, environment.now().timeIntervalSince(reservedAt) >= Self.foregroundWait {
            // Avibe came forward long after the press: never open the
            // microphone for it now.
            foregroundUnavailable()
            return .stopped
        }
        let startGeneration = generation
        guard let target = environment.target() else {
            state = .idle
            record("start: no selected instance")
            return .needsApp(.signIn)
        }
        guard environment.microphoneAllowed() else {
            state = .idle
            record("start: no microphone permission")
            return .needsApp(.microphone)
        }
        guard environment.activitiesEnabled() else {
            // Without a Live Activity the system stops the recording.
            state = .idle
            record("start: Live Activities are off")
            return .needsApp(.liveActivities)
        }
        let id = UUID()
        let audio: VoiceCaptureAudio
        do {
            audio = try environment.makeAudio(id)
        } catch {
            record("start: take file failed \(error)")
            state = .idle
            showFailure(step: .takeFile, error: error)
            return .stopped
        }
        if inFront {
            await environment.untilActive()
            guard case .preparing = state, generation == startGeneration else {
                // The account changed while Avibe came forward.
                audio.discard()
                return .stopped
            }
        }

        // The island is what the system requires when recording begins;
        // then the session, then the recorder.
        let startedAt = environment.now()
        let number = inFront ? 3 : 0
        let activity: VoiceCaptureActivityHandle
        do {
            activity = try requestListeningIsland(startedAt)
        } catch {
            audio.discard()
            if !inFront, foregroundFallback {
                return await goOnInFront(Self.attempt(number, error: error, step: .liveActivity), island: nil,
                                         generation: startGeneration)
            }
            state = .idle
            failedStartAt = environment.now()
            var failed = Self.failure(step: .liveActivity, error: error, at: environment.now())
            failed.attempts = backgroundAttempt.map { [$0, Self.attempt(number, error: error, step: .liveActivity)] }
            postFailure(failed)
            return .needsApp(.activityFailed)
        }
        do {
            if !inFront { backgroundActivations += 1 }
            try audio.prepare(inFront ? .exclusiveRecord : .mixableBackground)
            try audio.start()
        } catch {
            record("start: try \(number) refused \((error as NSError).domain) \((error as NSError).code) \(error)")
            _ = audio.stop()
            audio.discard()
            if !inFront, foregroundFallback {
                return await goOnInFront(Self.attempt(number, error: error, step: .recorder), island: activity,
                                         generation: startGeneration)
            }
            state = .idle
            // Permission was checked above: this is the system refusing to
            // record, not a missing permission. Say where, with its code.
            failedStartAt = environment.now()
            let step = (error as? VoiceCaptureRecorder.StartFailure)?.step ?? .recorder
            var failed = Self.failure(step: step, error: error, at: startedAt)
            failed.attempts = backgroundAttempt.map { [$0, Self.attempt(number, error: error, step: .recorder)] }
            postFailure(failed)
            await activity.end(failed, dismissal: .after(startedAt.addingTimeInterval(Self.noticeDismissal)))
            return .stopped
        }
        environment.captureStarted(inFront)
        let capture = "\(number) [\(pressContext)]" + (backgroundAttempt.map {
            " (\($0.candidate) \($0.step.rawValue) \($0.error ?? "-"))"
        } ?? "")
        backgroundAttempt = nil
        record("start: recording with try \(number) [\(capture)]")

        let instance = environment.makeInstance(target)
        let consent = Task { await Self.refusal(from: instance) }
        state = .listening(Take(
            id: id, instance: instance, audio: audio, activity: activity, startedAt: startedAt,
            capture: capture, consent: consent, inBackground: !inFront
        ))
        consentWatch = Task { [weak self] in
            // A transient failure is not a no: the take records on, and its
            // send keeps it for Retry instead.
            guard let refusal = await consent.value, !refusal.transient else { return }
            await self?.consentRefused(refusal, takeID: id)
        }
        observeInterruptions()
        let sleep = environment.sleep
        maximumDurationTask = Task { [weak self] in
            await sleep(Self.maximumDuration)
            guard !Task.isCancelled, let self, case .listening(let take) = self.state, take.id == id else {
                return
            }
            // This task sends: `finish` must not cancel it (its ask would
            // then ask nothing).
            self.maximumDurationTask = nil
            await self.finish(send: true)
        }
        record("start: listening")
        return .started
    }

    /// The background try was refused: nothing is left active, its island
    /// goes at once, and the take stays reserved for the try in front.
    private func goOnInFront(
        _ attempt: VoiceCaptureAttributes.Attempt,
        island: VoiceCaptureActivityHandle?,
        generation startGeneration: Int
    ) async -> Outcome {
        backgroundAttempt = attempt
        if let island { await island.end(nil, dismissal: .immediate) }
        guard generation == startGeneration, case .preparing = state else {
            // The account changed meanwhile.
            backgroundAttempt = nil
            return .stopped
        }
        record("start: background try refused; continuing in the App")
        return .needsForeground
    }

    /// Avibe could not come forward (or came forward too late): the notice
    /// names what the background try was refused.
    func foregroundUnavailable(reservation press: UUID? = nil) {
        guard case .preparing = state, press == nil || press == reservation else { return }
        state = .idle
        guard let attempt = backgroundAttempt else { return }
        backgroundAttempt = nil
        var failed = Self.failure([attempt], at: environment.now())
        failed.reason = .recordingFailed
        postFailure(failed)
        showNotice(failed)
    }

    /// One refused try, its step taken from the recorder's error when it
    /// says (setting up vs activating the session).
    static func attempt(
        _ number: Int, error: Error, step fallback: VoiceCaptureAttributes.Step
    ) -> VoiceCaptureAttributes.Attempt {
        VoiceCaptureAttributes.Attempt(
            candidate: number,
            step: (error as? VoiceCaptureRecorder.StartFailure)?.step ?? fallback,
            error: errorText(error)
        )
    }

    /// A start whose tries were refused; the last try's step and code also
    /// fill the single-step fields.
    static func failure(
        _ attempts: [VoiceCaptureAttributes.Attempt], at date: Date
    ) -> VoiceCaptureAttributes.ContentState {
        var state = VoiceCaptureAttributes.ContentState(
            phase: .failed, startedAt: date, endedAt: date, reason: .recordingFailed
        )
        state.attempts = attempts.count > 1 ? attempts : nil
        state.failedStep = attempts.last?.step
        state.errorText = attempts.last?.error
        state.errorCode = attempts.last?.error.flatMap { Int($0) }
        return state
    }

    /// The longest the wait before ending may take before it is not trusted.
    static let endingWindow: TimeInterval = 2.5

    /// The pending end of this process after a background capture (tests
    /// await it).
    private(set) var endingTask: Task<Void, Never>?

    /// Variant (a): once a background capture is sent (its message
    /// accepted, its audio deleted), and only behind the switch, end this
    /// process about a second later, so the next press is a fresh launch.
    /// Never in front, in a process ever shown, with a take under way, or
    /// with other work in flight; the decision goes into the next capture.
    private func considerEndingProcess(after take: Take) {
        guard take.inBackground else { return }
        guard environment.endProcessAfterBackgroundCapture() else {
            environment.noteEndDecision("off")
            return
        }
        if let blocker = endBlocker() {
            environment.noteEndDecision("skip:\(blocker)")
            return
        }
        // Held as a background task so the wait runs now, not at the next
        // wake (which may be the next press); a wait that ran late is not
        // trusted.
        let release = environment.holdWhileEnding()
        let scheduled = environment.now()
        endingTask = Task { [weak self] in
            guard let self else { return release() }
            await self.environment.waitBeforeEnding()
            if self.environment.now().timeIntervalSince(scheduled) > Self.endingWindow {
                self.environment.noteEndDecision("skip:late")
                return release()
            }
            if let blocker = self.endBlocker() {
                self.environment.noteEndDecision("skip:\(blocker)")
                return release()
            }
            self.environment.noteEndDecision("ended")
            self.record("ending the process after a background capture")
            self.environment.endProcess()
            release()
        }
    }

    private func endBlocker() -> String? {
        guard case .idle = state else { return "voice" }
        if environment.processEverShown() { return "shown" }
        return environment.otherWorkInFlight()
    }

    /// Follows the current take's voice-input answer (tests await it).
    private(set) var consentWatch: Task<Void, Never>?

    /// A no while the take still records: stop, discard it unsent, and say
    /// why. (A take already sending meets the answer in `deliver`.)
    private func consentRefused(_ refusal: Refusal, takeID: UUID) async {
        guard case .listening(let take) = state, take.id == takeID else { return }
        record("consent refused while recording: \(refusal.reason)")
        stopObservingInterruptions()
        maximumDurationTask?.cancel()
        maximumDurationTask = nil
        take.audio.stop()
        take.audio.discard()
        take.consent.cancel()
        take.instance.close()
        state = .idle
        await take.activity.end(nil, dismissal: .immediate)
        surface(refusal)
    }

    /// Says a refusal: in front an alert (and the main Bot chat when the App
    /// must ask), else a notice on the island.
    private func surface(_ refusal: Refusal) {
        failedStartAt = environment.now()
        if environment.isInFront() {
            noticeCount += 1
            lastNoticeReason = refusal.reason
            environment.presentInFront(refusal.reason, refusal.needsApp)
        } else {
            showNotice(phase: refusal.needsApp ? .needsApp : .failed, reason: refusal.reason)
        }
    }

    /// Nil when the instance takes this voice input; otherwise why not. A
    /// cancelled ask (the take was given up) asks nothing.
    private static func refusal(from instance: VoiceCaptureInstance) async -> Refusal? {
        guard !Task.isCancelled else { return Refusal(reason: .offline, needsApp: false, transient: true) }
        do {
            let status = try await instance.status()
            guard status.available else { return Refusal(reason: .voiceUnavailable, needsApp: false) }
            guard status.consent == .granted else { return Refusal(reason: .consent, needsApp: true) }
            return nil
        } catch let failure as VoiceCaptureInstanceClient.Failure {
            switch failure {
            case .signIn: return Refusal(reason: .signIn, needsApp: true)
            case .consentRequired: return Refusal(reason: .consent, needsApp: true)
            case .olderAvibe: return Refusal(reason: .updateAvibe, needsApp: false)
            case .unavailable: return Refusal(reason: .voiceUnavailable, needsApp: false)
            default: return Refusal(reason: .offline, needsApp: false, transient: true)
            }
        } catch {
            return Refusal(reason: .offline, needsApp: false, transient: true)
        }
    }

    private func deliver(_ initial: Take) async {
        var take = initial
        let deliverGeneration = generation
        state = .sending(take)
        await take.activity.update(content(for: take, phase: .sending))
        // Nothing leaves the phone before the instance said yes. An answer
        // that was "unreachable" is asked again now, not reused.
        var answer = await take.consent.value
        // An account change meanwhile closed this take's client: ask nothing.
        guard generation == deliverGeneration else { return }
        if answer?.transient == true {
            answer = await Self.refusal(from: take.instance)
            let fresh = answer
            take.consent = Task { fresh }
        }
        if let refusal = answer {
            guard generation == deliverGeneration else { return }
            if refusal.transient {
                // Unreachable is not a no: keep the take on the phone; Retry
                // asks again.
                record("deliver: instance unreachable; kept for Retry, unsent")
                await keepForRetry(take, reason: .offline)
                return
            }
            record("deliver: consent refused \(refusal.reason); discarded unsent")
            take.audio.discard()
            take.instance.close()
            state = .idle
            await take.activity.end(nil, dismissal: .immediate)
            surface(refusal)
            return
        }
        guard generation == deliverGeneration else { return }
        // Once the message POST has been made, its outcome may be unknown
        // (a lost response): it is never repeated, so Retry cannot send the
        // same spoken instruction twice.
        var posted = false
        do {
            if take.text == nil {
                let audio = try Data(contentsOf: take.audio.url)
                take.text = try await take.instance.transcribe(
                    audio: audio, name: "voice.m4a", mime: "audio/mp4"
                )
            }
            guard generation == deliverGeneration else { return }
            state = .sending(take)
            guard let text = take.text else { throw VoiceCaptureInstanceClient.Failure.nothingHeard }
            let path = try await take.instance.mainBotMessagesPath()
            guard generation == deliverGeneration else { return }
            posted = true
            try await take.instance.postMessage(text, to: path, capture: take.capture)
            guard generation == deliverGeneration else { return }
            record("deliver: sent")
            state = .idle
            let heard = String(text.prefix(VoiceCaptureAttributes.ContentState.heardLimit))
            await end(
                take,
                dismissal: .after(environment.now().addingTimeInterval(Self.sentDismissal)),
                final: content(for: take, phase: .sent, heard: heard)
            )
            considerEndingProcess(after: take)
        } catch let failure as VoiceCaptureInstanceClient.Failure {
            record("deliver: failed \(failure) posted=\(posted)")
            guard generation == deliverGeneration else { return }
            if posted, failure != .consentRequired, failure != .signIn {
                // It may have arrived; the chat shows whether it did.
                state = .idle
                await end(take, dismissal: .after(environment.now().addingTimeInterval(Self.noticeDismissal)),
                          final: content(for: take, phase: .needsApp, reason: .sendUnknown))
                return
            }
            switch failure {
            case .nothingHeard:
                state = .idle
                await end(take, dismissal: .after(environment.now().addingTimeInterval(Self.sentDismissal)),
                          final: content(for: take, phase: .failed, reason: .nothingHeard))
            case .consentRequired:
                // A "no" given since: stop, send nothing, keep nothing.
                state = .idle
                await end(take, dismissal: .after(environment.now().addingTimeInterval(Self.noticeDismissal)),
                          final: content(for: take, phase: .needsApp, reason: .consent))
            case .signIn:
                state = .idle
                await end(take, dismissal: .after(environment.now().addingTimeInterval(Self.noticeDismissal)),
                          final: content(for: take, phase: .needsApp, reason: .signIn))
            default:
                // Nothing was posted yet, so Retry is safe.
                let reason: VoiceCaptureAttributes.Reason =
                    take.text == nil && failure != .offline ? .transcriptionFailed : .offline
                await keepForRetry(take, reason: reason)
            }
        } catch {
            record("deliver: failed \(error)")
            guard generation == deliverGeneration else { return }
            if posted {
                state = .idle
                await end(take, dismissal: .after(environment.now().addingTimeInterval(Self.noticeDismissal)),
                          final: content(for: take, phase: .needsApp, reason: .sendUnknown))
            } else {
                await keepForRetry(take, reason: .transcriptionFailed)
            }
        }
    }

    private func keepForRetry(_ take: Take, reason: VoiceCaptureAttributes.Reason) async {
        state = .failed(take)
        await take.activity.update(content(for: take, phase: .failed, reason: reason))
        // A failed take the user leaves alone is dropped with its island.
        let id = take.id
        let sleep = environment.sleep
        Task { [weak self] in
            await sleep(Self.failedDismissal)
            guard let self, case .failed(let current) = self.state, current.id == id else { return }
            self.state = .idle
            await self.end(current, dismissal: .immediate)
        }
    }

    // MARK: Live Activity

    private func content(
        for take: Take,
        phase: VoiceCaptureAttributes.Phase,
        reason: VoiceCaptureAttributes.Reason? = nil,
        heard: String? = nil
    ) -> VoiceCaptureAttributes.ContentState {
        VoiceCaptureAttributes.ContentState(
            phase: phase,
            startedAt: take.startedAt,
            endedAt: take.endedAt,
            reason: reason,
            heard: heard
        )
    }

    /// Ends a take: closes its instance client, deletes its audio, and ends
    /// its Live Activity, with `final` shown until `dismissal`.
    private func end(
        _ take: Take,
        dismissal: ActivityUIDismissalPolicy,
        final: VoiceCaptureAttributes.ContentState? = nil
    ) async {
        // Any ask still pending for this take asks nothing once it is over.
        take.consent.cancel()
        take.instance.close()
        take.audio.discard()
        await take.activity.end(final, dismissal: dismissal)
    }

    /// The last start that failed, with its step and code. When the
    /// notification cannot be shown (notifications off), the alert Avibe
    /// shows on coming forward carries the same line.
    private(set) var lastFailure: VoiceCaptureAttributes.ContentState?

    private func postFailure(_ failure: VoiceCaptureAttributes.ContentState) {
        lastFailure = failure
        environment.postFailureNotice(failure)
    }

    static func failure(
        step: VoiceCaptureAttributes.Step, error: Error, at date: Date
    ) -> VoiceCaptureAttributes.ContentState {
        var state = VoiceCaptureAttributes.ContentState(
            phase: .failed, startedAt: date, endedAt: date, reason: .recordingFailed
        )
        state.failedStep = step
        state.errorCode = systemCode(error)
        state.errorText = errorText(error)
        return state
    }

    /// A start that failed before its island existed: the notification and
    /// a short island both say where.
    private func showFailure(step: VoiceCaptureAttributes.Step, error: Error) {
        let failed = Self.failure(step: step, error: error, at: environment.now())
        postFailure(failed)
        showNotice(failed)
    }

    /// The failure's own short island.
    private func showNotice(_ failure: VoiceCaptureAttributes.ContentState) {
        failedStartAt = environment.now()
        do {
            let activity = try environment.requestActivity(failure)
            Task {
                await activity.end(failure, dismissal: .after(failure.startedAt.addingTimeInterval(Self.noticeDismissal)))
            }
        } catch {
            record("notice failed: \(error)")
        }
    }

    /// Every notice shown or said in front, for the intent's alert in front,
    /// where the island is not drawn.
    private(set) var noticeCount = 0
    private(set) var lastNoticeReason: VoiceCaptureAttributes.Reason?

    /// A short notice for a press that never recorded.
    private func showNotice(
        phase: VoiceCaptureAttributes.Phase,
        reason: VoiceCaptureAttributes.Reason,
        errorCode: Int? = nil
    ) {
        noticeCount += 1
        lastNoticeReason = reason
        let now = environment.now()
        failedStartAt = now
        var notice = VoiceCaptureAttributes.ContentState(
            phase: phase, startedAt: now, endedAt: now, reason: reason
        )
        notice.errorCode = errorCode
        do {
            let activity = try environment.requestActivity(notice)
            Task {
                await activity.end(notice, dismissal: .after(now.addingTimeInterval(Self.noticeDismissal)))
            }
        } catch {
            record("notice failed: \(error)")
        }
    }

    // MARK: Interruptions

    private func observeInterruptions() {
        stopObservingInterruptions()
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(),
            queue: .main
        ) { [weak self] notification in
            guard
                let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                AVAudioSession.InterruptionType(rawValue: raw) == .began
            else {
                return
            }
            Task { @MainActor in
                // A call or another recorder took the microphone: send what
                // was said so far rather than lose it.
                self?.record("interrupted")
                await self?.finish(send: true)
            }
        }
    }

    private func stopObservingInterruptions() {
        if let interruptionObserver {
            NotificationCenter.default.removeObserver(interruptionObserver)
        }
        interruptionObserver = nil
    }

    private func record(_ event: String) {
        Self.logger.info("\(event, privacy: .public)")
    }
}

/// Whether a press found this process just launched for it (cold) or
/// already running (warm), with the App's state, for the message metadata.
@MainActor
enum VoiceCaptureLaunch {
    private static var launchedAt: Date?
    /// The App's state when this process finished launching: background
    /// when an intent (or the system) launched it, inactive when on screen.
    private static var launchState = "unknown"
    private static var everActive = false
    private static var presses = 0
    private static var observer: NSObjectProtocol?

    static func markLaunched() {
        launchedAt = Date()
        launchState = name(UIApplication.shared.applicationState)
        observer = NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main
        ) { _ in
            MainActor.assumeIsolated { everActive = true }
        }
    }

    private static func name(_ state: UIApplication.State) -> String {
        switch state {
        case .active: return "active"
        case .inactive: return "inactive"
        case .background: return "background"
        @unknown default: return "unknown"
        }
    }

    static var everShown: Bool { everActive }

    /// The press's process facts (E1, 11-20: iOS lets a process it launched
    /// in the background start audio once): `pid=`, `start=` (launch, Unix
    /// seconds), `age=` (seconds since), `byPress=1` when this press
    /// launched it (within 2 s, never shown, first press), `scene=` whether
    /// a scene was ever active, `presses=` presses that started a take
    /// before, `session=` whether our audio session was already active,
    /// `launch=` the state at launch, `prevEnd=` what the last background
    /// capture decided about ending its process.
    static func pressContext() -> String {
        let age = launchedAt.map { Date().timeIntervalSince($0) } ?? -1
        let byPress = !everActive && presses == 0 && age >= 0 && age < 2
        let start = launchedAt.map { String(Int($0.timeIntervalSince1970)) } ?? "-"
        let facts = "\(name(UIApplication.shared.applicationState)) \(byPress ? "cold" : "warm") "
            + "pid=\(ProcessInfo.processInfo.processIdentifier) start=\(start) age=\(String(format: "%.1f", age)) "
            + "byPress=\(byPress ? 1 : 0) scene=\(everActive ? 1 : 0) presses=\(presses) "
            + "session=\(VoiceCaptureAudioState.active ? 1 : 0) launch=\(launchState) "
            + "prevEnd=\(VoiceCaptureEndSwitch.takeLastDecision() ?? "-")"
        presses += 1
        let session = AVAudioSession.sharedInstance()
        return facts + " " + audioContext(
            otherAudio: session.isOtherAudioPlaying,
            silenceHint: session.secondaryAudioShouldBeSilencedHint,
            outputs: session.currentRoute.outputs.map(\.portType.rawValue)
        )
    }

    /// Whether another App holds audio (navigation prompts, music) and where
    /// sound goes, for the message metadata. The input is left out: before
    /// a take's session is set up, the route's input reflects only whatever
    /// category the shared session last had. `otherAudio=0` between
    /// navigation prompts does not rule navigation out.
    nonisolated static func audioContext(otherAudio: Bool, silenceHint: Bool, outputs: [String]) -> String {
        "otherAudio=\(otherAudio ? 1 : 0) silenceHint=\(silenceHint ? 1 : 0) "
            + "out=\(outputs.isEmpty ? "-" : outputs.joined(separator: ","))"
    }
}

/// The hidden switch for variant (a), default off. Not in Settings and not
/// discoverable: the owner flips it by opening
/// `avibe-diagnostics://voice/end-after-background?on=1` (or `on=0`).
@MainActor
enum VoiceCaptureEndSwitch {
    static let key = "avibe.voiceCapture.endProcessAfterBackgroundCapture"
    private static let decisionKey = "avibe.voiceCapture.lastEndDecision"

    /// Off in an App Store build even if a TestFlight build had set it.
    static var isOn: Bool { isOn(in: .standard) }

    static func isOn(in defaults: UserDefaults) -> Bool {
        isInternalBuild && defaults.bool(forKey: key)
    }

    /// Handles the switch's URL; returns whether it was one.
    @discardableResult
    static func handle(_ url: URL, allowed: Bool = isInternalBuild, defaults: UserDefaults = .standard) -> Bool {
        guard allowed, url.scheme == "avibe-diagnostics", url.host == "voice",
              url.path == "/end-after-background",
              let on = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "on" })?.value
        else { return false }
        let enabled = on == "1"
        defaults.set(enabled, forKey: key)
        VoiceCaptureBanner.show(String(localized: VoiceCaptureCopy.text(
            enabled ? "voiceCapture.diagnostics.endOn" : "voiceCapture.diagnostics.endOff"
        )))
        return true
    }

    /// Only TestFlight and development builds take the switch's URL; an App
    /// Store build ignores it.
    nonisolated static var isInternalBuild: Bool {
        #if DEBUG
        return true
        #else
        return Bundle.main.appStoreReceiptURL?.lastPathComponent == "sandboxReceipt"
        #endif
    }

    static func noteDecision(_ decision: String) {
        UserDefaults.standard.set(decision, forKey: decisionKey)
        // The process may end right after: write it through now.
        UserDefaults.standard.synchronize()
    }

    /// The last decision, once (the next press reports it).
    static func takeLastDecision() -> String? {
        let decision = UserDefaults.standard.string(forKey: decisionKey)
        UserDefaults.standard.removeObject(forKey: decisionKey)
        return decision
    }
}
