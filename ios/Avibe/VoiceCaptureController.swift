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
    /// `timing`: milliseconds from the press to each step (device numbers
    /// for the first-words question), in the metadata only.
    /// `earlier`: presses before this one that left no message (what
    /// happened to each; no audio, no words), in the metadata only.
    func postMessage(
        _ text: String, to sessionPath: String, capture: String?, timing: String?, earlier: String?
    ) async throws
    func close()
    var releaseBarrier: WebsiteDataStoreReleaseBarrier? { get }
    /// Requests that did not reach the instance (which, where it gave up,
    /// when), newest last: states and codes only.
    var failures: [String] { get }
}

extension VoiceCaptureInstanceClient: VoiceCaptureInstance {}

/// One take's recording.
protocol VoiceCaptureAudio: AnyObject {
    var url: URL { get }
    /// Sets up and activates the session for one candidate; records nothing.
    func prepare(_ candidate: VoiceCaptureSessionCandidate) throws
    /// Starts recording on the prepared session.
    func start() throws
    /// When the recorder first had audio (nil until then, or unknown).
    var firstAudioAt: Date? { get }
    /// Recording stopped with no audio seen.
    var firstAudioLate: Bool { get }
    /// Stops and returns the take's length in seconds.
    @discardableResult func stop() -> TimeInterval
    func discard()
    /// Which recorder took the take, or what the session said when none
    /// would (`rec[...]`), for `capture_attempt`.
    var report: String? { get }
    /// Called when the take lost its input and could not go on recording
    /// (the second recorder, after the input changed under it): the take
    /// is then sent as far as it got.
    var onInputLost: (() -> Void)? { get set }
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
    /// 正在录音，再长按一次操作按钮发送. Never shown before.
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
    /// Keeps the process running for a while as a kept take is sent again
    /// (a background task assertion); returns what releases it.
    var holdWhileRetrying: () -> () -> Void
    /// Presses that left no message, kept on the phone for the next
    /// message's `capture_earlier`: what happened to each, with no audio
    /// and no words. Add one, read them, and drop the ones a message that
    /// arrived carried (those lines, not a count: others may have been
    /// added, and old ones pushed out, while it was on its way).
    var noteEarlier: (String) -> Void
    var earlier: () -> [String]
    var dropEarlier: ([String]) -> Void
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
        holdWhileEnding: { VoiceCaptureEnvironment.backgroundHold("voice-capture-end") },
        endProcess: { exit(0) },
        noteEndDecision: { VoiceCaptureEndSwitch.noteDecision($0) },
        holdWhileRetrying: { VoiceCaptureEnvironment.backgroundHold("voice-capture-retry") },
        noteEarlier: { VoiceCaptureEarlier.add($0) },
        earlier: { VoiceCaptureEarlier.lines },
        dropEarlier: { VoiceCaptureEarlier.drop($0) },
        now: Date.init,
        sleep: { try? await Task.sleep(for: $0) }
    )

    /// A background task assertion; the returned closure ends it.
    private static func backgroundHold(_ name: String) -> () -> Void {
        var id = UIBackgroundTaskIdentifier.invalid
        id = UIApplication.shared.beginBackgroundTask(withName: name) {
            UIApplication.shared.endBackgroundTask(id)
            id = .invalid
        }
        return {
            guard id != .invalid else { return }
            UIApplication.shared.endBackgroundTask(id)
            id = .invalid
        }
    }
}

/// What happened to presses that left no message, kept on the phone until
/// a message carries them (`capture_earlier`): a few short lines of states,
/// codes and times. Never audio, never what was said.
enum VoiceCaptureEarlier {
    static let key = "avibe.voiceCapture.earlier"
    static let maximumLines = 5
    static let maximumLength = 700

    /// `lines` with `line` added: the newest `maximumLines`, each cut short.
    static func adding(_ line: String, to lines: [String]) -> [String] {
        Array((lines + [String(line.prefix(maximumLength))]).suffix(maximumLines))
    }

    static var lines: [String] {
        UserDefaults.standard.stringArray(forKey: key) ?? []
    }

    static func add(_ line: String) {
        UserDefaults.standard.set(adding(line, to: lines), forKey: key)
    }

    /// `lines` without the ones in `sent` (each once).
    static func removing(_ sent: [String], from lines: [String]) -> [String] {
        var left = sent
        return lines.filter { line in
            guard let index = left.firstIndex(of: line) else { return true }
            left.remove(at: index)
            return false
        }
    }

    static func drop(_ sent: [String]) {
        UserDefaults.standard.set(removing(sent, from: lines), forKey: key)
    }
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


    /// When the press arrived, and its steps since (ms): the island, the
    /// session, the recorder, and for a press that came forward, the
    /// refusal and Avibe being in front. Sent as `capture_timing`.
    private var pressAt: Date?
    private var marks: [String] = []

    private func mark(_ step: String) {
        guard let pressAt else { return }
        marks.append("\(step)=\(Int(environment.now().timeIntervalSince(pressAt) * 1000))")
    }

    /// The intent brought Avibe forward (for its own press only).
    func markInFront(reservation press: UUID? = nil) {
        guard press == nil || press == reservation else { return }
        mark("front")
    }

    private func timing(of take: Take) -> String {
        let audio = take.audio.firstAudioAt.map { ["audio=\(Int($0.timeIntervalSince(take.pressAt) * 1000))"] }
            ?? ["audio=none"]
        return (take.marks + audio + (take.retries > 0 ? ["tries=\(take.retries)"] : [])).joined(separator: " ")
    }

    // MARK: Presses that left no message

    private static let noteTime: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()

    /// One line for the next message's `capture_earlier`: when, the press
    /// (or the take's own capture line), what happened, and its steps.
    private func note(_ what: String, for take: Take? = nil, press: Bool = false) {
        let context = take?.capture ?? (press ? "[\(pressContext)]" : "")
        let steps = take?.marks ?? (press ? marks : [])
        let line = ([Self.noteTime.string(from: environment.now()), what, context] + steps)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
        environment.noteEarlier(line)
    }

    /// The background try a press made before it came forward.
    private var backgroundAttempt: VoiceCaptureAttributes.Attempt?
    /// What the refused background try's recorder said (`rec[...]`).
    private var backgroundReport: String?
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
        /// Unreachable until Avibe is opened again (the App's local
        /// listener failed for this process): sending again by itself
        /// cannot help.
        var untilReopened = false
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
        /// The press's steps, ms from the press (see `mark`), and the
        /// interruptions and route changes heard while it recorded.
        var marks: [String]
        let pressAt: Date
        var endedAt: Date?
        /// Kept once transcribed, so Retry does not transcribe again.
        var text: String?
        /// When it was first kept unsent, and whether it is sent again by
        /// itself.
        var keptAt: Date?
        var keptAutomatic = false
        /// Sends tried again since it was kept.
        var retries = 0
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
    private var audioEventObservers: [NSObjectProtocol] = []

    private var activeObserver: NSObjectProtocol?

    init(environment: VoiceCaptureEnvironment) {
        self.environment = environment
        activeObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in await self?.retryKeptNow() }
        }
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
            note("abandoned", press: true)
            state = .idle
            backgroundAttempt = nil
        }
        // The system may run the intent twice for one press: a run right
        // after a start that failed and said so does nothing more.
        if case .idle = state, let failedStartAt,
           environment.now().timeIntervalSince(failedStartAt) < Self.repeatWindow
        {
            record("ignored a run right after a failed start")
            note("repeat")
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
                note("leftover")
                return .stopped
            }
        }
        switch state {
        case .listening:
            await finish(send: true, cause: "press")
            return .stopped
        case .preparing, .sending:
            return .busy
        case .failed(let take):
            // A new press replaces a failed take the user did not retry.
            stopRetrying()
            note("replaced", for: take)
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
        pressAt = environment.now()
        marks = []
        audioEvents = 0
        backgroundAttempt = nil
        backgroundReport = nil
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
    func finish(send: Bool, cause: String = "island") async {
        guard case .listening(var take) = state else {
            if !send, case .failed(let take) = state {
                stopRetrying()
                note("discarded", for: take)
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
        take.marks.append("stop=\(Int(environment.now().timeIntervalSince(take.pressAt) * 1000)):\(cause)")
        record("stop send=\(send) cause=\(cause) duration=\(String(format: "%.1f", duration))")
        guard send, duration >= Self.minimumDuration else {
            note(send ? "short:\(String(format: "%.1f", duration))" : "cancel", for: take)
            state = .idle
            await end(take, dismissal: .immediate)
            return
        }
        await deliver(take)
    }

    /// Retry on a failed take: send the kept audio (or text) again.
    func retry() async {
        guard case .failed(var take) = state else {
            if case .idle = state { _ = await endLeftovers(reason: .interrupted) }
            return
        }
        // Already being sent again by itself: one send at a time, so the
        // message cannot be posted twice.
        guard sending != take.id else { return }
        take.retries += 1
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
        stopRetrying()
        // What an account's presses left is not another account's.
        environment.dropEarlier(environment.earlier())
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

    /// Whether Avibe is in front now.
    var isInFront: Bool { environment.isInFront() }

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
            note("needsApp:signIn", press: true)
            return .needsApp(.signIn)
        }
        guard environment.microphoneAllowed() else {
            state = .idle
            record("start: no microphone permission")
            note("needsApp:microphone", press: true)
            return .needsApp(.microphone)
        }
        guard environment.activitiesEnabled() else {
            // Without a Live Activity the system stops the recording.
            state = .idle
            record("start: Live Activities are off")
            note("needsApp:liveActivities", press: true)
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
            mark(inFront ? "frontIsland" : "island")
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
            note("start:liveActivity \(Self.errorText(error) ?? "-")", press: true)
            return .needsApp(.activityFailed)
        }
        observeAudioEvents()
        do {
            if !inFront { backgroundActivations += 1 }
            // A hold in the App gives the microphone up before this take
            // sets the session, so its end cannot deactivate this take's.
            NativeHoldCapture.shared.yieldToActionButton()
            try audio.prepare(inFront ? .exclusiveRecord : .mixableBackground)
            mark(inFront ? "frontSession" : "session")
            try audio.start()
            mark(inFront ? "frontRecord" : "record")
        } catch {
            record("start: try \(number) refused \((error as NSError).domain) \((error as NSError).code) \(error) \(audio.report ?? "")")
            if !inFront { backgroundReport = audio.report }
            stopObservingInterruptions()
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
            note(
                "start:\(step.rawValue) \(Self.errorText(error) ?? "-") \(audio.report ?? "")"
                    + (backgroundAttempt.map { " after (\($0.step.rawValue) \($0.error ?? "-") \(backgroundReport ?? ""))" } ?? ""),
                press: true
            )
            await activity.end(failed, dismissal: .after(startedAt.addingTimeInterval(Self.noticeDismissal)))
            return .stopped
        }
        audio.onInputLost = { [weak self] in
            Task { @MainActor in
                guard let self, case .listening(let take) = self.state, take.id == id else { return }
                self.record("the input was lost")
                await self.finish(send: true, cause: "input")
            }
        }
        environment.captureStarted(inFront)
        let capture = "\(number) [\(pressContext)]" + (backgroundAttempt.map {
            " (\($0.candidate) \($0.step.rawValue) \($0.error ?? "-")\(backgroundReport.map { " " + $0 } ?? ""))"
        } ?? "") + (audio.report.map { " " + $0 } ?? "")
        backgroundAttempt = nil
        backgroundReport = nil
        record("start: recording with try \(number) [\(capture)]")

        let instance = environment.makeInstance(target)
        let consent = Task { await Self.refusal(from: instance) }
        state = .listening(Take(
            id: id, instance: instance, audio: audio, activity: activity, startedAt: startedAt,
            capture: capture, consent: consent, inBackground: !inFront,
            marks: marks, pressAt: pressAt ?? startedAt
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
            await self.finish(send: true, cause: "max")
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
        mark("refused")
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
        note("noFront (\(attempt.step.rawValue) \(attempt.error ?? "-") \(backgroundReport ?? ""))", press: true)
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
        note("refused:\(refusal.reason.rawValue)", for: take)
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
            case .directFault:
                return Refusal(reason: .offline, needsApp: false, transient: true, untilReopened: true)
            default: return Refusal(reason: .offline, needsApp: false, transient: true)
            }
        } catch {
            return Refusal(reason: .offline, needsApp: false, transient: true)
        }
    }

    /// How sending a take ended (`send`).
    private enum Sent {
        case sent(String)
        /// The instance said no: the take is discarded unsent.
        case refused(Refusal)
        /// Nothing was posted, so sending again is safe: the instance could
        /// not be reached (`offline`), or the take was not turned into text.
        /// `automatic`: sending again by itself can help (the instance was
        /// unreachable, and not because this process's listener failed).
        case kept(VoiceCaptureAttributes.Reason, automatic: Bool)
        case nothingHeard
        /// The App must ask again (voice consent, sign-in).
        case needsApp(VoiceCaptureAttributes.Reason)
        /// The message was posted and its answer lost: it may have arrived.
        case unknown
        /// The account changed meanwhile: nothing more is done with it.
        case superseded
    }

    /// Asks the instance, transcribes and posts one take, touching neither
    /// the state nor the island. Nothing leaves the phone before the
    /// instance said yes; an answer that was "unreachable" is asked again
    /// now, not reused.
    private func send(_ take: inout Take) async -> Sent {
        let sendGeneration = generation
        let id = take.id
        // Given up meanwhile (an account change closed its client; it was
        // discarded or replaced while an automatic send was under way):
        // nothing more is asked or posted.
        var current: Bool { generation == sendGeneration && !ended.contains(id) }
        var answer = await take.consent.value
        guard current else { return .superseded }
        if answer?.transient == true {
            answer = await Self.refusal(from: take.instance)
            let fresh = answer
            take.consent = Task { fresh }
            guard current else { return .superseded }
        }
        if let refusal = answer {
            // Unreachable is not a no: the take stays on the phone.
            return refusal.transient ? .kept(.offline, automatic: !refusal.untilReopened) : .refused(refusal)
        }
        // Once the message POST has been made, its outcome may be unknown
        // (a lost response): it is never repeated, so sending again cannot
        // deliver the same spoken instruction twice.
        var posted = false
        do {
            if take.text == nil {
                let audio = try Data(contentsOf: take.audio.url)
                take.text = try await take.instance.transcribe(
                    audio: audio, name: "voice.m4a", mime: "audio/mp4"
                )
            }
            guard current else { return .superseded }
            guard let text = take.text else { throw VoiceCaptureInstanceClient.Failure.nothingHeard }
            let path = try await take.instance.mainBotMessagesPath()
            guard current else { return .superseded }
            posted = true
            let earlier = environment.earlier()
            try await take.instance.postMessage(
                text, to: path, capture: take.capture, timing: timing(of: take),
                earlier: earlier.isEmpty ? nil : earlier.joined(separator: " | ")
            )
            // They went with a message that arrived.
            environment.dropEarlier(earlier)
            guard generation == sendGeneration else { return .superseded }
            return .sent(text)
        } catch let failure as VoiceCaptureInstanceClient.Failure {
            record("deliver: failed \(failure) posted=\(posted)")
            // The client was closed before the request was made: the take
            // is over, and provably nothing went out, the POST included.
            if failure == .closed { return .superseded }
            guard posted ? generation == sendGeneration : current else { return .superseded }
            if posted, failure != .consentRequired, failure != .signIn { return .unknown }
            switch failure {
            case .nothingHeard: return .nothingHeard
            // A "no" given since: stop, send nothing, keep nothing.
            case .consentRequired: return .needsApp(.consent)
            case .signIn: return .needsApp(.signIn)
            case .directFault:
                return .kept(.offline, automatic: false)
            default:
                let reason: VoiceCaptureAttributes.Reason =
                    take.text == nil && failure != .offline ? .transcriptionFailed : .offline
                return .kept(reason, automatic: reason == .offline)
            }
        } catch {
            record("deliver: failed \(error)")
            guard posted ? generation == sendGeneration : current else { return .superseded }
            return posted ? .unknown : .kept(.transcriptionFailed, automatic: false)
        }
    }

    /// The take a send is under way for. One send per take at a time, set
    /// before a send's first wait and cleared when it returns: whichever of
    /// the stop, Retry, the retry schedule and Avibe coming to the front
    /// gets there first sends, and the others do nothing, so a message is
    /// never posted twice.
    private var sending: UUID?
    /// Takes that are over (`end`): a send still under way for one asks and
    /// posts nothing more, and its client loads nothing more.
    private var ended: Set<UUID> = []

    /// A send the user asked for (the stop, the island's Send or Retry):
    /// the island says it is sending, and nothing else starts meanwhile.
    private func deliver(_ initial: Take) async {
        var take = initial
        guard sending != take.id else { return }
        sending = take.id
        let deliverGeneration = generation
        retryTask?.cancel()
        retryTask = nil
        state = .sending(take)
        await take.activity.update(content(for: take, phase: .sending))
        let result = await send(&take)
        if sending == take.id { sending = nil }
        guard generation == deliverGeneration else { return }
        await settle(result, of: take)
    }

    /// A kept take sent again by itself. It stays kept while this runs (its
    /// island goes on saying it will send), so a new press or the island's
    /// Discard is never swallowed: either ends the take, and this send then
    /// asks nothing more and makes no request. Only a POST already on its
    /// way cannot be taken back; the next message's `capture_earlier` then
    /// says it went (`sentAfterEnd`) or may have (`unknownAfterEnd`).
    private func sendKeptAgain(id: UUID) async {
        guard sending != id, case .failed(var take) = state, take.id == id, !ended.contains(id) else { return }
        sending = id
        take.retries += 1
        state = .failed(take)
        let result = await send(&take)
        if sending == id { sending = nil }
        guard case .failed(let current) = state, current.id == id, !ended.contains(id) else {
            switch result {
            case .sent: note("sentAfterEnd", for: take)
            case .unknown: note("unknownAfterEnd", for: take)
            default: break
            }
            return
        }
        retryTask = nil
        await settle(result, of: take)
    }

    /// What a finished send means for its take and island.
    private func settle(_ result: Sent, of take: Take) async {
        if case .kept(let reason, let automatic) = result {
            await keepForRetry(take, reason: reason, automatic: automatic)
            return
        }
        stopRetrying()
        switch result {
        case .kept, .superseded:
            return
        case .sent(let text):
            record("deliver: sent")
            // What kept it from the instance before it arrived rides on the
            // next message.
            if !take.instance.failures.isEmpty { note("sentAfter\(unreached(take))", for: take) }
            state = .idle
            let heard = String(text.prefix(VoiceCaptureAttributes.ContentState.heardLimit))
            await end(
                take,
                dismissal: .after(environment.now().addingTimeInterval(Self.sentDismissal)),
                final: content(for: take, phase: .sent, heard: heard)
            )
            considerEndingProcess(after: take)
        case .refused(let refusal):
            record("deliver: consent refused \(refusal.reason); discarded unsent")
            note("refused:\(refusal.reason.rawValue)", for: take)
            ended.insert(take.id)
            take.audio.discard()
            take.instance.close()
            state = .idle
            await take.activity.end(nil, dismissal: .immediate)
            surface(refusal)
        case .nothingHeard:
            note("nothingHeard", for: take)
            state = .idle
            await end(take, dismissal: .after(environment.now().addingTimeInterval(Self.sentDismissal)),
                      final: content(for: take, phase: .failed, reason: .nothingHeard))
        case .needsApp(let reason):
            note("needsApp:\(reason.rawValue)", for: take)
            state = .idle
            await end(take, dismissal: .after(environment.now().addingTimeInterval(Self.noticeDismissal)),
                      final: content(for: take, phase: .needsApp, reason: reason))
        case .unknown:
            // It may have arrived; the chat shows whether it did.
            note("sendUnknown", for: take)
            state = .idle
            await end(take, dismissal: .after(environment.now().addingTimeInterval(Self.noticeDismissal)),
                      final: content(for: take, phase: .needsApp, reason: .sendUnknown))
        }
    }

    /// The kept take's next automatic send, or its end (tests await it).
    private(set) var retryTask: Task<Void, Never>?
    private var releaseRetryHold: (() -> Void)?

    /// How long after a failed send the next one is tried by itself: 2, 5,
    /// 10, 20 and 30 s, then every minute.
    static func retryDelay(after retries: Int) -> Duration {
        let schedule = [2, 5, 10, 20, 30]
        return .seconds(retries < schedule.count ? schedule[retries] : 60)
    }

    /// Keeps a take that was not sent, nothing of it posted. One the
    /// instance could not be reached for (`automatic`) is sent again by
    /// itself, and when Avibe comes to the front, until it arrives; after
    /// `failedDismissal` it gets one last send and is then dropped with its
    /// island. Any other waits for Retry until then: one that could not be
    /// turned into text, or one this process cannot send until Avibe is
    /// opened again.
    private func keepForRetry(_ kept: Take, reason: VoiceCaptureAttributes.Reason, automatic: Bool) async {
        var take = kept
        let now = environment.now()
        if let keptAt = take.keptAt, now.timeIntervalSince(keptAt) >= Self.failedDismissal / .seconds(1) {
            await drop(take)
            return
        }
        if take.keptAt == nil {
            take.keptAt = now
            note("kept:\(reason.rawValue)\(unreached(take))", for: take)
        }
        take.keptAutomatic = automatic
        if !automatic {
            // Waiting for the user's Retry needs no running process.
            releaseRetryHold?()
            releaseRetryHold = nil
        }
        state = .failed(take)
        scheduleRetry(of: take, automatic: automatic)
        await take.activity.update(content(for: take, phase: .failed, reason: automatic ? .willSend : reason))
    }

    private func scheduleRetry(of take: Take, automatic: Bool) {
        retryTask?.cancel()
        let id = take.id
        let keptAt = take.keptAt ?? environment.now()
        let elapsed = Duration.seconds(environment.now().timeIntervalSince(keptAt))
        let remaining = max(Self.failedDismissal - elapsed, Duration.zero)
        let delay = automatic ? min(Self.retryDelay(after: take.retries), remaining) : remaining
        if automatic, releaseRetryHold == nil {
            releaseRetryHold = environment.holdWhileRetrying()
        }
        let sleep = environment.sleep
        retryTask = Task { [weak self] in
            await sleep(delay)
            guard !Task.isCancelled, let self, case .failed(let current) = self.state, current.id == id,
                  self.sending != id
            else {
                return
            }
            if automatic {
                await self.sendKeptAgain(id: id)
            } else {
                self.retryTask = nil
                await self.drop(current)
            }
        }
    }

    /// Avibe came to the front: a take kept for an unreachable instance is
    /// sent again now, unless a send of it is already under way.
    func retryKeptNow() async {
        guard case .failed(let take) = state, take.keptAutomatic, sending != take.id else { return }
        retryTask?.cancel()
        retryTask = nil
        await sendKeptAgain(id: take.id)
    }

    /// The take's requests that did not reach the instance, for a note.
    private func unreached(_ take: Take) -> String {
        let failures = take.instance.failures
        return failures.isEmpty ? "" : " {\(failures.joined(separator: "; "))}"
    }

    private func stopRetrying() {
        retryTask?.cancel()
        retryTask = nil
        releaseRetryHold?()
        releaseRetryHold = nil
    }

    /// A kept take nobody sent in time goes with its island.
    private func drop(_ take: Take) async {
        stopRetrying()
        note("expired\(unreached(take))", for: take)
        state = .idle
        await end(take, dismissal: .immediate)
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
        // Any ask still pending for this take asks nothing once it is over,
        // and a send still under way for it posts nothing.
        ended.insert(take.id)
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
        if interruptionObserver != nil {
            NotificationCenter.default.removeObserver(interruptionObserver!)
        }
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
                await self?.finish(send: true, cause: "interrupted")
            }
        }
    }

    /// Interruptions and route changes from the try's session on, in
    /// `capture_timing` (ms from the press): `intBegan=`/`intEnded=` with
    /// the reason (`:r<n>`) or whether to resume (`:resume`), and
    /// `route<reason>=` with the input port then. A navigation prompt that
    /// cuts a take shows here.
    private func observeAudioEvents() {
        removeAudioEventObservers()
        let center = NotificationCenter.default
        let session = AVAudioSession.sharedInstance()
        audioEventObservers = [
            center.addObserver(forName: AVAudioSession.interruptionNotification, object: session, queue: .main) { [weak self] note in
                let info = note.userInfo ?? [:]
                let began = (info[AVAudioSessionInterruptionTypeKey] as? UInt)
                    .flatMap(AVAudioSession.InterruptionType.init(rawValue:)) == .began
                let detail: String
                if began {
                    detail = (info[AVAudioSessionInterruptionReasonKey] as? UInt).map { ":r\($0)" } ?? ""
                } else {
                    let options = AVAudioSession.InterruptionOptions(
                        rawValue: info[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
                    )
                    detail = options.contains(.shouldResume) ? ":resume" : ""
                }
                MainActor.assumeIsolated { self?.noteAudioEvent(began ? "intBegan" : "intEnded", detail) }
            },
            center.addObserver(forName: AVAudioSession.routeChangeNotification, object: session, queue: .main) { [weak self] note in
                let reason = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt ?? 0
                let port = AVAudioSession.sharedInstance().currentRoute.inputs.first?.portType.rawValue ?? "-"
                MainActor.assumeIsolated { self?.noteAudioEvent("route\(reason)", ":\(port)") }
            },
        ]
    }

    /// At most this many events per press: enough to tell a cut take.
    static let maximumAudioEvents = 8

    private var audioEvents = 0

    private func noteAudioEvent(_ name: String, _ detail: String) {
        guard let pressAt, audioEvents < Self.maximumAudioEvents else { return }
        let event = "\(name)=\(Int(environment.now().timeIntervalSince(pressAt) * 1000))\(detail)"
        switch state {
        case .listening(var take):
            take.marks.append(event)
            state = .listening(take)
        case .preparing:
            marks.append(event)
        default:
            return
        }
        audioEvents += 1
    }

    private func removeAudioEventObservers() {
        audioEventObservers.forEach(NotificationCenter.default.removeObserver)
        audioEventObservers = []
    }

    private func stopObservingInterruptions() {
        if let interruptionObserver {
            NotificationCenter.default.removeObserver(interruptionObserver)
        }
        interruptionObserver = nil
        removeAudioEventObservers()
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
