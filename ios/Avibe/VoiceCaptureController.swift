import ActivityKit
import AVFoundation
import Foundation
import OSLog

/// What a take needs from the selected instance.
@MainActor
protocol VoiceCaptureInstance: AnyObject {
    func status() async throws -> VoiceCaptureInstanceClient.Status
    func transcribe(audio: Data, name: String, mime: String) async throws -> String
    func mainBotMessagesPath() async throws -> String
    func postMessage(_ text: String, to sessionPath: String) async throws
    func close()
    var releaseBarrier: WebsiteDataStoreReleaseBarrier? { get }
}

extension VoiceCaptureInstanceClient: VoiceCaptureInstance {}

/// One take's recording.
protocol VoiceCaptureAudio: AnyObject {
    var url: URL { get }
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
    }

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

    /// The intent: start a take, or stop and send the one recording.
    func toggle() async -> Outcome {
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
            return await start()
        case .idle:
            return await start()
        }
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

    private func start() async -> Outcome {
        state = .preparing
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

        // Ask the instance before the microphone opens: never record for an
        // account that has not said yes to voice input (5.1.2(i)).
        let instance = environment.makeInstance(target)
        if let refusal = await checkStatus(instance) {
            instance.close()
            if generation == startGeneration { state = .idle }
            return refusal
        }
        guard generation == startGeneration else {
            // The account changed while the instance answered.
            instance.close()
            return .stopped
        }

        let id = UUID()
        let audio: VoiceCaptureAudio
        do {
            audio = try environment.makeAudio(id)
        } catch {
            record("start: take file failed \(error)")
            instance.close()
            state = .idle
            showFailure(step: .takeFile, error: error)
            return .stopped
        }

        // The system requires the Live Activity when recording begins.
        let startedAt = environment.now()
        let activity: VoiceCaptureActivityHandle
        do {
            activity = try environment.requestActivity(
                VoiceCaptureAttributes.ContentState(phase: .listening, startedAt: startedAt)
            )
        } catch {
            instance.close()
            state = .idle
            // No island means no recording (Apple stops it). Nothing was
            // recorded; the App comes forward and says why, and the
            // notification keeps the step and code.
            audio.discard()
            record("start: activity request failed \((error as NSError).domain) \((error as NSError).code) \(error)")
            failedStartAt = environment.now()
            postFailure(Self.failure(
                step: .liveActivity, error: error, at: environment.now()
            ))
            return .needsApp(.activityFailed)
        }
        do {
            try audio.start()
        } catch {
            record("start: recorder failed \((error as NSError).domain) \((error as NSError).code) \(error)")
            instance.close()
            state = .idle
            // Permission was checked above: this is the system refusing to
            // record, not a missing permission. Say where, with its code.
            failedStartAt = environment.now()
            let step = (error as? VoiceCaptureRecorder.StartFailure)?.step ?? .recorder
            let failed = Self.failure(step: step, error: error, at: startedAt)
            postFailure(failed)
            await activity.end(
                failed,
                dismissal: .after(startedAt.addingTimeInterval(Self.noticeDismissal))
            )
            return .stopped
        }
        state = .listening(Take(
            id: id, instance: instance, audio: audio, activity: activity, startedAt: startedAt
        ))
        observeInterruptions()
        let sleep = environment.sleep
        maximumDurationTask = Task { [weak self] in
            await sleep(Self.maximumDuration)
            guard !Task.isCancelled, let self, case .listening(let take) = self.state, take.id == id else {
                return
            }
            await self.finish(send: true)
        }
        record("start: listening")
        return .started
    }

    /// Nil when the take may record; otherwise what the press returns.
    private func checkStatus(_ instance: VoiceCaptureInstance) async -> Outcome? {
        do {
            let status = try await instance.status()
            guard status.available else {
                showNotice(phase: .failed, reason: .voiceUnavailable)
                return .stopped
            }
            guard status.consent == .granted else {
                record("start: consent \(status.consent)")
                return .needsApp(.consent)
            }
            return nil
        } catch let failure as VoiceCaptureInstanceClient.Failure {
            record("start: status failed \(failure)")
            switch failure {
            case .signIn: return .needsApp(.signIn)
            case .consentRequired: return .needsApp(.consent)
            case .olderAvibe:
                showNotice(phase: .failed, reason: .updateAvibe)
                return .stopped
            case .unavailable:
                showNotice(phase: .failed, reason: .voiceUnavailable)
                return .stopped
            default:
                showNotice(phase: .failed, reason: .offline)
                return .stopped
            }
        } catch {
            showNotice(phase: .failed, reason: .offline)
            return .stopped
        }
    }

    private func deliver(_ initial: Take) async {
        var take = initial
        let deliverGeneration = generation
        state = .sending(take)
        await take.activity.update(content(for: take, phase: .sending))
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
            try await take.instance.postMessage(text, to: path)
            guard generation == deliverGeneration else { return }
            record("deliver: sent")
            state = .idle
            let heard = String(text.prefix(VoiceCaptureAttributes.ContentState.heardLimit))
            await end(
                take,
                dismissal: .after(environment.now().addingTimeInterval(Self.sentDismissal)),
                final: content(for: take, phase: .sent, heard: heard)
            )
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

    /// A short notice for a press that never recorded.
    private func showNotice(
        phase: VoiceCaptureAttributes.Phase,
        reason: VoiceCaptureAttributes.Reason,
        errorCode: Int? = nil
    ) {
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
