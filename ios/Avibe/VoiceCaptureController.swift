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
    /// `spokenAt`: when the words were spoken, for a take that is sent
    /// later than that (ISO 8601 with the phone's offset).
    func postMessage(
        _ text: String, to sessionPath: String, capture: String?, timing: String?, earlier: String?,
        spokenAt: String?
    ) async throws
    func close()
    var releaseBarrier: WebsiteDataStoreReleaseBarrier? { get }
    /// Requests that did not reach the instance (which, where it gave up,
    /// when), newest last: states and codes only.
    var failures: [String] { get }
    /// The take's one transcription id, kept with it while it is unsent.
    var dictationID: String { get }
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

/// The audio of a take kept from an earlier run: only its file.
private final class StoredTakeAudio: VoiceCaptureAudio {
    let url: URL
    let firstAudioAt: Date? = nil
    let firstAudioLate = false
    let report: String? = nil
    var onInputLost: (() -> Void)?

    init(url: URL) {
        self.url = url
    }

    func prepare(_ candidate: VoiceCaptureSessionCandidate) throws {}
    func start() throws {}
    func stop() -> TimeInterval { 0 }
    func discard() { try? FileManager.default.removeItem(at: url) }
}

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
    /// The client for a take; `dictationID` is the one a take kept from an
    /// earlier run already has (nil: a new take makes its own).
    var makeInstance: (_ target: VoiceCaptureTarget, _ dictationID: String?) -> VoiceCaptureInstance
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
    /// Where takes that are not sent yet are kept (nil: only in memory).
    var outbox: VoiceCaptureOutbox?
    /// Schedules the lasting notification 这条语音没能发出去，打开 Avibe 可以重试。
    /// for `date` (replacing one already scheduled), or with nil removes it.
    var scheduleUnsentNotice: (Date?) -> Void
    /// In front: one alert that `count` takes are not sent, the oldest
    /// spoken at `oldest`, with Retry (`true`) and Discard (`false`); nil
    /// when it could not be shown.
    var presentUnsent: (_ count: Int, _ oldest: Date, _ answer: @escaping (Bool?) -> Void) -> Void
    /// Whether that alert is on screen now.
    var unsentAlertIsShown: () -> Bool
    /// Says a line in front, where the island is not drawn.
    var sayInFront: (VoiceCaptureAttributes.Reason) -> Void
    var now: () -> Date
    var sleep: (Duration) async -> Void

    static let live = VoiceCaptureEnvironment(
        target: { VoiceCaptureTarget.current() },
        makeInstance: { target, dictationID in
            dictationID.map { VoiceCaptureInstanceClient(target: target, dictationID: $0) }
                ?? VoiceCaptureInstanceClient(target: target)
        },
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
        outbox: (try? VoiceCaptureOutbox.standardDirectory()).map(VoiceCaptureOutbox.init(directory:)),
        scheduleUnsentNotice: { VoiceCaptureFailureNotification.scheduleUnsent(at: $0) },
        presentUnsent: { VoiceCaptureAlert.presentUnsent(count: $0, oldest: $1, answer: $2) },
        unsentAlertIsShown: { VoiceCaptureAlert.unsentIsShown },
        sayInFront: { VoiceCaptureBanner.show(String(localized: VoiceCaptureCopy.reason($0))) },
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
        // A take that was kept has its steps as they stood then.
        let steps = take.entry?.timing ?? recordedTiming(of: take)
        return take.retries > 0 ? "\(steps) tries=\(take.retries)" : steps
    }

    private func recordedTiming(of take: Take) -> String {
        let audio = take.audio.firstAudioAt.map { ["audio=\(Int($0.timeIntervalSince(take.pressAt) * 1000))"] }
            ?? ["audio=none"]
        return (take.marks + audio).joined(separator: " ")
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
    /// How long a notice (needs the App, an older Avibe) stays.
    static let noticeDismissal: TimeInterval = 30

    private struct Take {
        let id: UUID
        /// Where it goes: the instance selected when it was spoken.
        let target: VoiceCaptureTarget
        let instance: VoiceCaptureInstance
        let audio: VoiceCaptureAudio
        /// Its island; none once it waits behind a newer take, or after a
        /// relaunch.
        var activity: VoiceCaptureActivityHandle?
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
        /// When it was first kept unsent, why it is kept now, and whether
        /// it is sent again by itself.
        var keptAt: Date?
        var keptReason: VoiceCaptureAttributes.Reason?
        var keptAutomatic = false
        /// Its record in the outbox, once it was kept.
        var entry: VoiceOutboxEntry?
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
            Task { @MainActor in await self?.activated() }
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
        // What an earlier run left is known before this press's take can be
        // sent: it goes in its turn, not ahead of them.
        if loadOutbox() {
            Task { await self.sendKept(.byItself) }
        }
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
            // A new press never discards a take that is not sent yet: it
            // gives its island up and goes on being sent, in its turn.
            await park(take)
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
                // The island's Discard: this take only.
                note("discarded", for: take)
                await discardKept(take)
                scheduleRetry()
                updateUnsentNotice()
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
        if nextAutomatic != nil || draining {
            // Earlier takes are still on their way: this one waits its
            // turn behind them, and is never sent before them.
            await keep(take, reason: .offline, automatic: true, queued: true)
            // A pass that is running sends it in its turn; the stop does
            // not wait for that.
            if !askRunningPass(.byItself) { await sendKept(.byItself) }
        } else {
            await deliver(take)
        }
    }

    /// Retry on the island of a take that is not sent yet: it is sent
    /// again now, after any earlier one that is still within its window
    /// (an older one waits for the alert's Retry).
    func retry() async {
        guard case .failed = state else {
            if case .idle = state, keptTakes.isEmpty { _ = await endLeftovers(reason: .interrupted) }
            return
        }
        await sendKept(.comingForward, showSending: true)
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
        // A kept take the outbox could not take still has its audio there.
        if keptTakes.allSatisfy({ $0.entry != nil }) {
            environment.discardLeftoverAudio()
        }
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
        case .listening(let take), .sending(let take), .failed(let take): return take.activity?.id
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
        retryTask?.cancel()
        retryTask = nil
        islandTask?.cancel()
        islandTask = nil
        releaseRetryHold?()
        releaseRetryHold = nil
        // What an account's presses left is not another account's, and
        // neither are its takes that are not sent yet: all go at once.
        environment.dropEarlier(environment.earlier())
        var takes = parked
        parked = []
        switch state {
        case .listening(let current), .sending(let current), .failed(let current):
            takes.append(current)
        case .idle, .preparing:
            break
        }
        state = .idle
        environment.outbox?.removeAll()
        // Also one an earlier run scheduled, which this run never read.
        unsentNoticeFor = nil
        environment.scheduleUnsentNotice(nil)
        guard !takes.isEmpty else { return [] }
        record("account transition: \(takes.count) take(s) given up")
        var barriers: [WebsiteDataStoreReleaseBarrier] = []
        for take in takes {
            ended.insert(take.id)
            take.consent.cancel()
            if let barrier = take.instance.releaseBarrier { barriers.append(barrier) }
            take.audio.stop()
            take.instance.close()
            take.audio.discard()
            if let activity = take.activity {
                Task { await activity.end(nil, dismissal: .immediate) }
            }
        }
        return barriers
    }

    /// Whether Avibe is in front now.
    var isInFront: Bool { environment.isInFront() }

    /// What an alert says when a start in front ended without recording:
    /// the start's failure if it is new since `before` (read before the
    /// press's first start), else the notice shown since, else nothing.
    nonisolated static func stoppedAlert(
        before: VoiceCaptureAttributes.ContentState?,
        after: VoiceCaptureAttributes.ContentState?,
        noticesBefore: Int,
        noticesAfter: Int,
        lastNoticeReason: VoiceCaptureAttributes.Reason?
    ) -> (reason: VoiceCaptureAttributes.Reason, failure: VoiceCaptureAttributes.ContentState?)? {
        if let after, after != before {
            return (.recordingFailed, after)
        }
        if noticesAfter != noticesBefore, let lastNoticeReason {
            return (lastNoticeReason, nil)
        }
        return nil
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

        let instance = environment.makeInstance(target, nil)
        let consent = Task { await Self.refusal(from: instance) }
        state = .listening(Take(
            id: id, target: target, instance: instance, audio: audio, activity: activity, startedAt: startedAt,
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
        guard case .idle = state, parked.isEmpty else { return "voice" }
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
        await take.activity?.end(nil, dismissal: .immediate)
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
        /// It was not asked for by the user, and its `automaticWindow` ended
        /// before the message could be posted: nothing was posted, and it
        /// waits for Retry.
        case tooLate
    }

    /// Asks the instance, transcribes and posts one take, touching neither
    /// the state nor the island. Nothing leaves the phone before the
    /// instance said yes; an answer that was "unreachable" is asked again
    /// now, not reused.
    /// `byUser`: the user asked for this send (the stop, Retry in the
    /// alert). Any other send posts only while the take is still within
    /// `automaticWindow`, however long its requests took.
    private func send(_ take: inout Take, byUser: Bool) async -> Sent {
        let sendGeneration = generation
        let id = take.id
        // Given up meanwhile (an account change closed its client; it was
        // discarded or replaced while an automatic send was under way):
        // nothing more is asked or posted.
        var current: Bool { generation == sendGeneration && !ended.contains(id) }
        var answer = await take.consent.value
        guard current else { return .superseded }
        // "Unreachable" and "sign in" are answers about then, not now:
        // each is asked again.
        if answer?.transient == true || answer?.reason == .signIn {
            answer = await Self.refusal(from: take.instance)
            let fresh = answer
            take.consent = Task { fresh }
            guard current else { return .superseded }
        }
        if let refusal = answer {
            // Unreachable is not a no: the take stays on the phone.
            if refusal.transient { return .kept(.offline, automatic: !refusal.untilReopened) }
            // Neither is an expired session: the take waits for the App to
            // be signed in again.
            if refusal.reason == .signIn { return .needsApp(.signIn) }
            return .refused(refusal)
        }
        // Once the message POST has been made, its outcome may be unknown
        // (a lost response): it is never repeated, so sending again cannot
        // deliver the same spoken instruction twice.
        var posted = false
        do {
            if take.text == nil {
                let audio = try Data(contentsOf: audioURL(of: take))
                take.text = try await take.instance.transcribe(
                    audio: audio, name: "voice.m4a", mime: "audio/mp4"
                )
            }
            guard current else { return .superseded }
            guard let text = take.text else { throw VoiceCaptureInstanceClient.Failure.nothingHeard }
            let path = try await take.instance.mainBotMessagesPath()
            guard current else { return .superseded }
            // The requests above may have outlasted the take's window (a
            // slow transcription, the App suspended meanwhile).
            guard byUser || isRecent(take) else { return .tooLate }
            // On disk before it is made: after a relaunch the POST is
            // never made again. A marker that cannot be written means no
            // POST: it could not be told from one that never began.
            if var entry = take.entry {
                entry.text = take.text
                entry.posted = true
                do {
                    try environment.outbox?.update(entry)
                } catch {
                    record("outbox: the POST marker was not written \(error)")
                    return .kept(.sendFailed, automatic: false)
                }
                take.entry = entry
            }
            posted = true
            let earlier = environment.earlier()
            // Sent later than it was spoken: its first line says when, so
            // the main Bot does not take it for an instruction given now.
            let stamp = lateStamp(of: take)
            let message = stamp == nil
                ? text
                : VoiceCaptureLateNote.message(text, spoken: spokenAt(take), now: environment.now())
            try await take.instance.postMessage(
                message, to: path, capture: take.capture, timing: timing(of: take),
                earlier: earlier.isEmpty ? nil : earlier.joined(separator: " | "),
                spokenAt: stamp
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
            if posted, var entry = take.entry {
                // The instance said no to the POST (sign in, or consent):
                // it provably did not arrive, so it may be made again.
                entry.posted = false
                try? environment.outbox?.update(entry)
                take.entry = entry
            }
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

    /// A send the user asked for (the stop, the island's Send): the island
    /// says it is sending, and nothing else starts meanwhile.
    private func deliver(_ initial: Take) async {
        var take = initial
        guard sending != take.id else { return }
        sending = take.id
        let deliverGeneration = generation
        state = .sending(take)
        await take.activity?.update(content(for: take, phase: .sending))
        let result = await send(&take, byUser: true)
        if sending == take.id { sending = nil }
        guard generation == deliverGeneration else { return }
        await settle(result, of: take)
        scheduleRetry()
    }

    /// What a finished send means for the take the state holds, and its
    /// island.
    private func settle(_ result: Sent, of take: Take) async {
        switch result {
        case .superseded:
            return
        case .tooLate:
            // Its island goes with its window (`scheduleIslandEnd`).
            await keep(take, reason: take.keptReason ?? .offline, automatic: take.keptAutomatic)
        case .kept(let reason, let automatic):
            await keep(take, reason: reason, automatic: automatic)
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
            state = .idle
            await end(take, dismissal: .immediate)
            surface(refusal)
        case .nothingHeard:
            note("nothingHeard", for: take)
            state = .idle
            await end(take, dismissal: .after(environment.now().addingTimeInterval(Self.sentDismissal)),
                      final: content(for: take, phase: .failed, reason: .nothingHeard))
        case .needsApp(let reason):
            note("needsApp:\(reason.rawValue)", for: take)
            let notice = content(for: take, phase: .needsApp, reason: reason)
            let dismissal = ActivityUIDismissalPolicy.after(
                environment.now().addingTimeInterval(Self.noticeDismissal)
            )
            guard reason == .signIn else {
                // A "no" given since: stop, send nothing, keep nothing.
                state = .idle
                await end(take, dismissal: dismissal, final: notice)
                return
            }
            // Signed out of the instance: the take is not lost. It waits,
            // without its island, for Retry once the App is signed in.
            var kept = take
            kept.keptAt = kept.keptAt ?? environment.now()
            kept.keptReason = .signIn
            kept.keptAutomatic = false
            persist(&kept)
            let island = kept.activity
            kept.activity = nil
            islandTask?.cancel()
            islandTask = nil
            parked.append(kept)
            state = .idle
            updateUnsentNotice()
            if environment.isInFront() {
                // The island is not drawn over the App: say it there, and
                // open the main Bot's conversation, where it can sign in.
                await island?.end(nil, dismissal: .immediate)
                surface(Refusal(reason: .signIn, needsApp: true))
            } else {
                await island?.end(notice, dismissal: dismissal)
            }
        case .unknown:
            // It may have arrived; the chat shows whether it did.
            note("sendUnknown", for: take)
            state = .idle
            await end(take, dismissal: .after(environment.now().addingTimeInterval(Self.noticeDismissal)),
                      final: content(for: take, phase: .needsApp, reason: .sendUnknown))
        }
    }

    // MARK: Takes that are not sent yet

    /// Takes kept unsent that a newer press took the island from, or that
    /// an earlier run of the App left: oldest first. With the take the
    /// state holds as `.failed` (the newest, still on its island) they are
    /// the takes that are not sent yet; each is sent in its turn, none is
    /// merged with another, and a new press discards none.
    private var parked: [Take] = []

    /// Every take that is not sent yet, oldest first.
    private var keptTakes: [Take] {
        if case .failed(let take) = state { return parked + [take] }
        return parked
    }

    /// How many takes are not sent yet (tests and the alert).
    var unsentCount: Int { keptTakes.count }

    private func kept(_ id: UUID) -> Take? {
        keptTakes.first { $0.id == id }
    }

    /// Writes a kept take back where it is held (the state or `parked`),
    /// with the island it has there now.
    private func replaceKept(_ take: Take) {
        var take = take
        if case .failed(let current) = state, current.id == take.id {
            take.activity = current.activity
            state = .failed(take)
        } else if let index = parked.firstIndex(where: { $0.id == take.id }) {
            take.activity = nil
            parked[index] = take
        }
    }

    /// The next automatic send, and the end of the newest kept take's
    /// island (tests await them).
    private(set) var retryTask: Task<Void, Never>?
    private(set) var islandTask: Task<Void, Never>?
    private var releaseRetryHold: (() -> Void)?

    /// How long after a failed send the next one is tried by itself: 2, 5,
    /// 10, 20 and 30 s, then every minute.
    static func retryDelay(after retries: Int) -> Duration {
        let schedule = [2, 5, 10, 20, 30]
        return .seconds(retries < schedule.count ? schedule[retries] : 60)
    }

    /// Where a kept take's audio is: in the outbox once it is recorded
    /// there.
    private func audioURL(of take: Take) -> URL {
        if let entry = take.entry, let outbox = environment.outbox {
            return outbox.audioURL(for: entry.id)
        }
        return take.audio.url
    }

    /// Writes a kept take to the outbox (the first time: its audio moves
    /// there from Caches), so it outlives this run of the App. A take the
    /// outbox cannot take stays in memory, for this run only.
    private func persist(_ take: inout Take) {
        guard let outbox = environment.outbox else { return }
        if var entry = take.entry {
            entry.text = take.text
            entry.retries = take.retries
            entry.reason = take.keptReason?.rawValue
            try? outbox.update(entry)
            take.entry = entry
            return
        }
        let entry = VoiceOutboxEntry(
            id: take.id, sequence: outbox.nextSequence(), createdAt: take.startedAt, spokenAt: spokenAt(take),
            accountID: take.target.accountID, instanceID: take.target.instanceID,
            origin: take.target.origin.serialized, storeIdentifier: take.target.storeIdentifier,
            dictationID: take.instance.dictationID, capture: take.capture, timing: recordedTiming(of: take),
            inBackground: take.inBackground, text: take.text, retries: take.retries,
            reason: take.keptReason?.rawValue
        )
        do {
            let dropped = try outbox.add(entry, movingAudioFrom: take.audio.url)
            take.entry = entry
            for old in dropped {
                // Too many unsent takes: the oldest went. It is over, a
                // send under way for it included.
                guard let gone = parked.first(where: { $0.id == old.id }) else { continue }
                note("overflow", for: gone)
                forget(gone)
            }
        } catch {
            record("outbox: could not keep the take on disk \(error)")
        }
    }

    /// Keeps the take the state holds: it was not sent and nothing of it
    /// was posted. It is written to the outbox and stays on its island
    /// (还没发出去，连上你的 Avibe 后会自动发送。 when it is sent again by itself)
    /// with Retry and Discard, until `automaticWindow` after it was spoken;
    /// then the island goes and the take waits in the outbox for Retry.
    private func keep(
        _ kept: Take, reason: VoiceCaptureAttributes.Reason, automatic: Bool, queued: Bool = false
    ) async {
        var take = kept
        let first = take.keptAt == nil
        if first {
            take.keptAt = environment.now()
            // `queued`: nothing failed for it; it waits behind an earlier take.
            note(queued ? "queued" : "kept:\(reason.rawValue)\(unreached(take))", for: take)
        }
        take.keptReason = reason
        take.keptAutomatic = automatic
        persist(&take)
        state = .failed(take)
        scheduleIslandEnd(of: take)
        updateUnsentNotice()
        if first, environment.isInFront() {
            // The island is not drawn over the App: say it here.
            environment.sayInFront(automatic ? .willSend : reason)
        }
        await take.activity?.update(content(for: take, phase: .failed, reason: automatic ? .willSend : reason))
    }

    private func scheduleIslandEnd(of take: Take) {
        islandTask?.cancel()
        let id = take.id
        // The island promises a send by itself: it goes when that ends.
        let elapsed = environment.now().timeIntervalSince(spokenAt(take))
        let remaining = Duration.seconds(max(Self.automaticWindow - elapsed, 0))
        let sleep = environment.sleep
        islandTask = Task { [weak self] in
            await sleep(remaining)
            guard !Task.isCancelled, let self, case .failed(let current) = self.state, current.id == id else {
                return
            }
            self.islandTask = nil
            await self.park(current)
            self.scheduleRetry()
        }
    }

    /// The take gives its island up (to a newer press, or when its
    /// `automaticWindow` ends) and goes on waiting to be sent, in its turn.
    private func park(_ kept: Take) async {
        var take = kept
        let island = take.activity
        take.activity = nil
        islandTask?.cancel()
        islandTask = nil
        parked.append(take)
        if case .failed(let current) = state, current.id == take.id { state = .idle }
        record("a kept take waits without its island")
        await island?.end(nil, dismissal: .immediate)
    }

    /// A take sent more than this long after it was spoken says when it
    /// was spoken: a first line before its words (`VoiceCaptureLateNote`)
    /// and `metadata.spoken_at`.
    static let lateAfter: TimeInterval = 60

    /// When a take that is sent late was spoken, as it goes in the message.
    private func lateStamp(of take: Take) -> String? {
        let spoken = spokenAt(take)
        guard environment.now().timeIntervalSince(spoken) > Self.lateAfter else { return nil }
        return Self.stamp.string(from: spoken)
    }

    private static let stamp: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = .current
        return formatter
    }()

    /// How long after the end of speaking a take may still be sent without
    /// the user asking: the island's schedule, and the silent send when
    /// Avibe comes to the front. After it only Retry in the alert sends the
    /// take, so the main Bot is never handed an old instruction unasked
    /// (Product, 11-20).
    static let automaticWindow: TimeInterval = 10 * 60

    /// When the take was spoken: the end of its recording.
    private func spokenAt(_ take: Take) -> Date {
        take.endedAt ?? take.startedAt
    }

    private func isRecent(_ take: Take) -> Bool {
        environment.now().timeIntervalSince(spokenAt(take)) < Self.automaticWindow
    }

    /// Which kept takes a pass sends, from the narrowest to the widest.
    enum KeptScope: Int, Comparable {
        static func < (lhs: KeptScope, rhs: KeptScope) -> Bool { lhs.rawValue < rhs.rawValue }

        /// The schedule: those an unreachable instance kept, still within
        /// `automaticWindow`.
        case byItself
        /// Avibe coming to the front, or Retry on the island: every take
        /// still within the window.
        case comingForward
        /// Retry in the alert: every take.
        case retry
    }

    private func belongs(_ take: Take, to scope: KeptScope) -> Bool {
        switch scope {
        case .byItself: return take.keptAutomatic && isRecent(take)
        case .comingForward: return isRecent(take)
        case .retry: return true
        }
    }

    /// The oldest take that is sent by itself.
    private var nextAutomatic: Take? {
        keptTakes.first { belongs($0, to: .byItself) }
    }

    /// Plans the next automatic send, for the oldest take that has one,
    /// and keeps the process running for it for a while.
    private func scheduleRetry() {
        retryTask?.cancel()
        retryTask = nil
        guard let next = nextAutomatic else {
            releaseRetryHold?()
            releaseRetryHold = nil
            return
        }
        if releaseRetryHold == nil {
            releaseRetryHold = environment.holdWhileRetrying()
        }
        let delay = Self.retryDelay(after: next.retries)
        let sleep = environment.sleep
        retryTask = Task { [weak self] in
            await sleep(delay)
            guard !Task.isCancelled, let self else { return }
            // Not this task's to cancel from here on: a send asks nothing
            // once its task is cancelled.
            self.retryTask = nil
            await self.sendKept(.byItself)
        }
    }

    private var draining = false
    /// What was asked for while a pass was running: the running pass does
    /// it before it ends, so a Retry is never dropped.
    private var askedMeanwhile: (scope: KeptScope, showSending: Bool)?
    private var passWaiters: [CheckedContinuation<Void, Never>] = []

    /// Asks for a pass without waiting for it: a pass that is running does
    /// it too before it ends. False when none is running.
    private func askRunningPass(_ scope: KeptScope, showSending: Bool = false) -> Bool {
        guard draining else { return false }
        askedMeanwhile = (
            max(askedMeanwhile?.scope ?? scope, scope),
            (askedMeanwhile?.showSending ?? false) || showSending
        )
        return true
    }

    /// Sends the takes that are not sent yet and belong to `scope`, oldest
    /// first, one at a time, stopping at the first that still cannot reach
    /// the instance, so a later take never arrives before an earlier one
    /// that is on its way. A take that needs the user (it could not be
    /// turned into text, or it was spoken more than `automaticWindow` ago)
    /// does not hold the others up. One pass runs at a time; asked for
    /// during one, this is done by that pass before it ends, and returns
    /// when it has.
    func sendKept(_ scope: KeptScope, showSending: Bool = false) async {
        if askRunningPass(scope, showSending: showSending) {
            await withCheckedContinuation { passWaiters.append($0) }
            return
        }
        draining = true
        var pass = (scope: scope, showSending: showSending)
        while true {
            var tried: Set<UUID> = []
            while let next = keptTakes.first(where: {
                !tried.contains($0.id) && sending != $0.id && belongs($0, to: pass.scope)
            }) {
                tried.insert(next.id)
                guard await sendOneKept(
                next.id, showSending: pass.showSending, byUser: pass.scope == .retry
            ) else { break }
            }
            guard let asked = askedMeanwhile else { break }
            askedMeanwhile = nil
            pass = asked
        }
        draining = false
        scheduleRetry()
        updateUnsentNotice()
        let waiters = passWaiters
        passWaiters = []
        waiters.forEach { $0.resume() }
    }

    /// Sends one kept take. It stays kept while this runs, so a new press
    /// or the island's Discard is never swallowed. False when the instance
    /// could not be reached, or the account changed: the takes after it
    /// wait.
    private func sendOneKept(_ id: UUID, showSending: Bool, byUser: Bool) async -> Bool {
        guard sending != id, var take = kept(id), !ended.contains(id) else { return true }
        sending = id
        take.retries += 1
        replaceKept(take)
        let sendGeneration = generation
        if showSending, let island = take.activity {
            await island.update(content(for: take, phase: .sending))
        }
        let result = await send(&take, byUser: byUser)
        if sending == id { sending = nil }
        guard generation == sendGeneration else { return false }
        guard kept(id) != nil, !ended.contains(id) else {
            // Discarded meanwhile. Only a POST already on its way cannot be
            // taken back; the next message says it went, or may have.
            switch result {
            case .sent: note("sentAfterEnd", for: take)
            case .unknown: note("unknownAfterEnd", for: take)
            default: break
            }
            return true
        }
        // What the send learned stays with the take where it is held now
        // (it may have given its island up meanwhile).
        replaceKept(take)
        guard let now = kept(id) else { return true }
        if case .failed(let current) = state, current.id == id {
            await settle(result, of: now)
            if case .kept(let reason, _) = result { return reason != .offline }
            return true
        }
        return settleParked(result, of: now)
    }

    /// What a finished send means for a take without an island. False
    /// when the takes after it must wait.
    private func settleParked(_ result: Sent, of take: Take) -> Bool {
        switch result {
        case .superseded:
            return false
        case .tooLate:
            // What it learned (its words) is kept for Retry.
            var kept = take
            persist(&kept)
            replaceKept(kept)
        case .kept(let reason, let automatic):
            var kept = take
            kept.keptReason = reason
            kept.keptAutomatic = automatic
            persist(&kept)
            replaceKept(kept)
            // Unreachable: the rest wait their turn. Not turned into text:
            // it waits for the user, and the rest go on.
            return reason != .offline
        case .sent:
            record("outbox: a kept take was sent")
            if !take.instance.failures.isEmpty { note("sentAfter\(unreached(take))", for: take) }
            forget(take)
        case .refused(let refusal):
            note("refused:\(refusal.reason.rawValue)", for: take)
            forget(take)
        case .nothingHeard:
            note("nothingHeard", for: take)
            forget(take)
        case .needsApp(let reason):
            guard reason == .consent else {
                // Signed out of the instance: every take waits for the App.
                var kept = take
                kept.keptReason = .signIn
                kept.keptAutomatic = false
                persist(&kept)
                replaceKept(kept)
                return false
            }
            // A "no" given since: send nothing, keep nothing.
            note("needsApp:\(reason.rawValue)", for: take)
            forget(take)
        case .unknown:
            note("sendUnknown", for: take)
            forget(take)
        }
        return true
    }

    /// A take without an island is over: its record, audio and client go.
    private func forget(_ take: Take) {
        parked.removeAll { $0.id == take.id }
        ended.insert(take.id)
        take.consent.cancel()
        take.instance.close()
        take.audio.discard()
        if let entry = take.entry { environment.outbox?.remove(entry) }
    }

    /// The takes an earlier run of the App left in the outbox come back,
    /// oldest first, without islands; those older than a day, of another
    /// account, or whose POST was begun (it may have arrived, so it is
    /// never made again) are removed. Then those still within their window
    /// are sent.
    func restoreOutbox() async {
        guard loadOutbox() else { return }
        await sendKept(.byItself)
    }

    /// Reads the outbox once per run, sending nothing. True when it found
    /// takes. A press does this before it records, so its take is never
    /// sent ahead of an earlier one.
    @discardableResult
    private func loadOutbox() -> Bool {
        guard let outbox = environment.outbox, !restored else { return false }
        restored = true
        var removed = false
        for old in outbox.removeExpired(now: environment.now()) {
            noteRestored("expired", old)
            removed = true
        }
        let account = environment.target()?.accountID
        var found = false
        for entry in outbox.entries() where kept(entry.id) == nil && !ended.contains(entry.id) {
            guard let target = entry.target, account == nil || target.accountID == account else {
                outbox.remove(entry)
                removed = true
                continue
            }
            guard !entry.posted else {
                noteRestored("sendUnknown", entry)
                outbox.remove(entry)
                removed = true
                continue
            }
            let unreachable = Refusal(reason: .offline, needsApp: false, transient: true)
            // Why it was kept: only one an unreachable instance kept is
            // sent again by itself (a listener that had failed belongs to
            // the run that ended).
            let reason = entry.reason.flatMap(VoiceCaptureAttributes.Reason.init(rawValue:)) ?? .offline
            parked.append(Take(
                id: entry.id, target: target,
                instance: environment.makeInstance(target, entry.dictationID),
                audio: StoredTakeAudio(url: outbox.audioURL(for: entry.id)), activity: nil,
                startedAt: entry.createdAt, capture: entry.capture,
                // Asked afresh before anything is sent.
                consent: Task { unreachable }, inBackground: entry.inBackground,
                marks: [], pressAt: entry.createdAt, endedAt: entry.spokenAt, text: entry.text,
                keptAt: entry.createdAt, keptReason: reason, keptAutomatic: reason == .offline,
                entry: entry, retries: entry.retries
            ))
            found = true
        }
        parked.sort { ($0.entry?.sequence ?? .max) < ($1.entry?.sequence ?? .max) }
        if found {
            record("outbox: \(parked.count) kept from an earlier run")
            updateUnsentNotice()
        } else if removed, keptTakes.isEmpty {
            // Nothing is left to retry: the notice the earlier run scheduled
            // (this run never read when) goes too.
            unsentNoticeFor = nil
            environment.scheduleUnsentNotice(nil)
        }
        return found
    }

    private var restored = false

    private func noteRestored(_ what: String, _ entry: VoiceOutboxEntry) {
        environment.noteEarlier(
            "\(Self.noteTime.string(from: environment.now())) \(what) \(entry.capture) \(entry.timing)"
        )
    }

    /// Removes the takes kept longer than `VoiceCaptureOutbox.retention`.
    private func removeExpired() async {
        let now = environment.now()
        for take in keptTakes where now.timeIntervalSince(take.startedAt) >= VoiceCaptureOutbox.retention {
            note("expired\(unreached(take))", for: take)
            await discardKept(take)
        }
    }

    /// Ends a kept take wherever it is held.
    private func discardKept(_ take: Take) async {
        if case .failed(let current) = state, current.id == take.id {
            islandTask?.cancel()
            islandTask = nil
            state = .idle
            await end(current, dismissal: .immediate)
        } else {
            forget(take)
        }
    }

    /// Avibe came to the front. Takes spoken within `automaticWindow` are
    /// sent once more, silently; if any take is still unsent, one alert
    /// says how many and how long ago the oldest was spoken, with Retry
    /// (all, oldest first) and Discard (the ones it spoke of). Not while a
    /// take is being recorded or sent, and not over an alert already up:
    /// the next time Avibe comes to the front asks then.
    func activated() async {
        await restoreOutbox()
        await removeExpired()
        guard !keptTakes.isEmpty else { return }
        await sendKept(.comingForward)
        let unsent = keptTakes
        guard let oldest = unsent.map(spokenAt).min(), environment.isInFront(),
              !environment.unsentAlertIsShown()
        else {
            return
        }
        switch state {
        case .idle, .failed: break
        case .preparing, .listening, .sending: return
        }
        let ids = Set(unsent.map(\.id))
        environment.presentUnsent(unsent.count, oldest) { [weak self] retry in
            Task { @MainActor in
                guard let self else { return }
                switch retry {
                case true?: await self.sendKept(.retry, showSending: true)
                case false?: await self.discardKept(ids)
                case nil: break
                }
            }
        }
    }

    /// Discard in the alert: the takes it spoke of that are still unsent
    /// are deleted (one kept since is not).
    func discardKept(_ ids: Set<UUID>) async {
        let takes = keptTakes.filter { ids.contains($0.id) }
        guard !takes.isEmpty else { return }
        note("discardedAll \(takes.count)")
        for take in takes { await discardKept(take) }
        scheduleRetry()
        updateUnsentNotice()
    }

    /// The lasting notification that a take was not sent: when the oldest
    /// one's `automaticWindow` ends (from then only Retry sends it), and
    /// gone once none is left. The system shows it whether or not the App
    /// is running then.
    private func updateUnsentNotice() {
        guard let oldest = keptTakes.map(spokenAt).min() else {
            if unsentNoticeFor != nil { environment.scheduleUnsentNotice(nil) }
            unsentNoticeFor = nil
            return
        }
        guard unsentNoticeFor != oldest else { return }
        unsentNoticeFor = oldest
        let date = oldest.addingTimeInterval(Self.automaticWindow)
        // One that is already due was shown (or scheduled) by the run that
        // kept the take.
        if date > environment.now() { environment.scheduleUnsentNotice(date) }
    }

    private var unsentNoticeFor: Date?

    /// The take's requests that did not reach the instance, for a note.
    private func unreached(_ take: Take) -> String {
        let failures = take.instance.failures
        return failures.isEmpty ? "" : " {\(failures.joined(separator: "; "))}"
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
        if let entry = take.entry { environment.outbox?.remove(entry) }
        await take.activity?.end(final, dismissal: dismissal)
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
