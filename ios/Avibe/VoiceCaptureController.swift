import ActivityKit
import AVFoundation
import Foundation
import OSLog
import UIKit

/// One background voice take at a time: from the Action Button press,
/// through the Live Activity, to the message the main Bot receives.
///
/// Every entry (the intent, the island's buttons) runs in the App's process
/// on the main actor, so the state below has one writer.
@MainActor
final class VoiceCaptureController {
    static let shared = VoiceCaptureController()

    enum Outcome: Equatable {
        case started
        case stopped
        case busy
        case needsApp(VoiceCaptureAttributes.Reason)
    }

    /// The longest take; it is then sent as if the user stopped it.
    static let maximumDuration: TimeInterval = 5 * 60
    /// A take shorter than this is treated as a slip and discarded.
    static let minimumDuration: TimeInterval = 0.6
    /// How long the island keeps a sent take before it goes.
    static let sentDismissal: TimeInterval = 4
    /// How long a failed take stays offered for Retry.
    static let failedDismissal: TimeInterval = 10 * 60
    /// How long a "needs the App" notice stays.
    static let needsAppDismissal: TimeInterval = 30

    private struct Take {
        let id: UUID
        let client: VoiceCaptureInstanceClient
        let recorder: VoiceCaptureRecorder
        let activity: Activity<VoiceCaptureAttributes>
        let startedAt: Date
        var endedAt: Date?
        /// Kept once transcribed, so Retry after a failed send does not
        /// transcribe (and bill) the audio again.
        var text: String?
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

    private var state: State = .idle
    private var maximumDurationTask: Task<Void, Never>?
    private var interruptionObserver: NSObjectProtocol?

    private init() {}

    // MARK: Entry points

    /// The intent: start a take, or stop and send the one recording.
    func toggle() async -> Outcome {
        switch state {
        case .listening:
            await finish(send: true)
            return .stopped
        case .preparing, .sending:
            return .busy
        case .failed(let take):
            // A new press replaces a failed take the user did not retry.
            await end(take, discardAudio: true, dismissal: .immediate)
            state = .idle
            return await start()
        case .idle:
            return await start()
        }
    }

    /// The island's Send (`send: true`) or Cancel, or the second press.
    func finish(send: Bool) async {
        guard case .listening(var take) = state else {
            if !send, case .failed(let take) = state {
                await end(take, discardAudio: true, dismissal: .immediate)
                state = .idle
            }
            return
        }
        stopObservingInterruptions()
        maximumDurationTask?.cancel()
        maximumDurationTask = nil
        let duration = take.recorder.stop()
        take.endedAt = Date()
        record("stop send=\(send) duration=\(String(format: "%.1f", duration))")
        guard send, duration >= Self.minimumDuration else {
            await end(take, discardAudio: true, dismissal: .immediate)
            state = .idle
            return
        }
        await deliver(take)
    }

    /// Retry on a failed take: send the kept audio (or text) again.
    func retry() async {
        guard case .failed(let take) = state else { return }
        await deliver(take)
    }

    /// Opens the main Bot's conversation, where the Web UI asks for
    /// whatever is missing (voice consent, the microphone, sign-in).
    func openMainBotChat() {
        InstancePathRequests.shared.open(path: OpenMainBotIntent.path)
    }

    /// Shows a "needs the App" notice when the intent cannot bring the App
    /// to the front itself (before iOS 26, or when the system declines).
    func showNeedsApp(_ reason: VoiceCaptureAttributes.Reason) {
        let now = Date()
        let content = ActivityContent(
            state: VoiceCaptureAttributes.ContentState(
                phase: .needsApp, startedAt: now, endedAt: now, reason: reason
            ),
            staleDate: nil
        )
        do {
            let activity = try Activity.request(
                attributes: VoiceCaptureAttributes(botName: nil),
                content: content
            )
            Task {
                await activity.end(
                    content,
                    dismissalPolicy: .after(now.addingTimeInterval(Self.needsAppDismissal))
                )
            }
        } catch {
            record("needsApp activity failed: \(error)")
        }
    }

    // MARK: Take lifecycle

    private func start() async -> Outcome {
        state = .preparing
        guard let target = VoiceCaptureTarget.current() else {
            state = .idle
            record("start: no selected instance")
            return .needsApp(.signIn)
        }
        guard VoiceCaptureRecorder.hasPermission else {
            state = .idle
            record("start: no microphone permission")
            return .needsApp(.microphone)
        }
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            // Without a Live Activity the system stops the recording.
            state = .idle
            record("start: Live Activities are off")
            return .needsApp(.liveActivities)
        }

        // Ask the instance before the microphone opens: never record for an
        // account that has not said yes to voice input (5.1.2(i)).
        let client = VoiceCaptureInstanceClient(target: target)
        do {
            let status = try await client.status()
            guard status.available else {
                client.close()
                state = .idle
                showFailureNotice(.voiceUnavailable)
                return .stopped
            }
            guard status.consent == .granted else {
                client.close()
                state = .idle
                record("start: consent \(status.consent)")
                return .needsApp(.consent)
            }
        } catch let failure as VoiceCaptureInstanceClient.Failure {
            client.close()
            state = .idle
            record("start: status failed \(failure)")
            switch failure {
            case .signIn: return .needsApp(.signIn)
            case .consentRequired: return .needsApp(.consent)
            case .olderAvibe:
                showFailureNotice(.updateAvibe)
                return .stopped
            case .unavailable:
                showFailureNotice(.voiceUnavailable)
                return .stopped
            default:
                showFailureNotice(.offline)
                return .stopped
            }
        } catch {
            client.close()
            state = .idle
            showFailureNotice(.offline)
            return .stopped
        }

        let id = UUID()
        let recorder: VoiceCaptureRecorder
        do {
            recorder = VoiceCaptureRecorder(url: try VoiceCaptureRecorder.takeURL(id: id))
        } catch {
            client.close()
            state = .idle
            showFailureNotice(.microphone)
            return .stopped
        }

        // The system requires the Live Activity when recording begins.
        let startedAt = Date()
        let activity: Activity<VoiceCaptureAttributes>
        do {
            activity = try Activity.request(
                attributes: VoiceCaptureAttributes(botName: nil),
                content: ActivityContent(
                    state: VoiceCaptureAttributes.ContentState(
                        phase: .listening, startedAt: startedAt
                    ),
                    staleDate: nil,
                    relevanceScore: 100
                )
            )
        } catch {
            client.close()
            state = .idle
            record("start: activity request failed \(error)")
            return .needsApp(.liveActivities)
        }
        do {
            try recorder.start()
        } catch {
            record("start: recorder failed \(error)")
            client.close()
            await endActivity(activity, phase: .failed, reason: .microphone,
                              dismissal: .after(Date().addingTimeInterval(Self.needsAppDismissal)))
            state = .idle
            return .stopped
        }
        let take = Take(
            id: id,
            client: client,
            recorder: recorder,
            activity: activity,
            startedAt: startedAt
        )
        state = .listening(take)
        observeInterruptions()
        maximumDurationTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.maximumDuration))
            guard !Task.isCancelled else { return }
            await self?.finish(send: true)
        }
        record("start: listening")
        #if DEBUG
        // Probe: does the process keep running (and recording) after the
        // intent returns?
        Task { [weak self] in
            for second in 1...20 {
                try? await Task.sleep(for: .seconds(1))
                guard let self, case .listening(let current) = self.state, current.id == id else { return }
                self.record("heartbeat \(second)s recording=\(current.recorder.isRecording) activities=\(Activity<VoiceCaptureAttributes>.activities.count) state=\(current.activity.activityState)")
            }
        }
        #endif
        return .started
    }

    private func deliver(_ initial: Take) async {
        var take = initial
        state = .sending(take)
        await update(take, phase: .sending)
        do {
            if take.text == nil {
                let audio = try Data(contentsOf: take.recorder.url)
                take.text = try await take.client.transcribe(
                    audio: audio, name: "voice.m4a", mime: "audio/mp4"
                )
                state = .sending(take)
            }
            guard let text = take.text else { throw VoiceCaptureInstanceClient.Failure.nothingHeard }
            try await take.client.sendToMainBot(text)
            record("deliver: sent")
            let heard = String(text.prefix(VoiceCaptureAttributes.ContentState.heardLimit))
            await end(
                take, discardAudio: true,
                dismissal: .after(Date().addingTimeInterval(Self.sentDismissal)),
                phase: .sent, heard: heard
            )
            state = .idle
        } catch let failure as VoiceCaptureInstanceClient.Failure {
            record("deliver: failed \(failure)")
            switch failure {
            case .nothingHeard:
                await end(take, discardAudio: true,
                          dismissal: .after(Date().addingTimeInterval(Self.sentDismissal)),
                          phase: .failed, reason: .nothingHeard)
                state = .idle
            case .consentRequired:
                // A "no" given since: stop, send nothing, keep nothing.
                await end(take, discardAudio: true,
                          dismissal: .after(Date().addingTimeInterval(Self.needsAppDismissal)),
                          phase: .needsApp, reason: .consent)
                state = .idle
            case .signIn:
                await end(take, discardAudio: true,
                          dismissal: .after(Date().addingTimeInterval(Self.needsAppDismissal)),
                          phase: .needsApp, reason: .signIn)
                state = .idle
            default:
                let reason: VoiceCaptureAttributes.Reason =
                    failure == .offline ? .offline
                    : take.text == nil ? .transcriptionFailed : .sendFailed
                await update(take, phase: .failed, reason: reason)
                state = .failed(take)
                scheduleFailedExpiry(take)
            }
        } catch {
            record("deliver: failed \(error)")
            await update(take, phase: .failed, reason: .transcriptionFailed)
            state = .failed(take)
            scheduleFailedExpiry(take)
        }
    }

    /// A failed take the user leaves alone is dropped with its island.
    private func scheduleFailedExpiry(_ take: Take) {
        let id = take.id
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.failedDismissal))
            guard let self, case .failed(let current) = self.state, current.id == id else { return }
            await self.end(current, discardAudio: true, dismissal: .immediate)
            self.state = .idle
        }
    }

    // MARK: Live Activity

    private func content(
        for take: Take,
        phase: VoiceCaptureAttributes.Phase,
        reason: VoiceCaptureAttributes.Reason? = nil,
        heard: String? = nil
    ) -> ActivityContent<VoiceCaptureAttributes.ContentState> {
        ActivityContent(
            state: VoiceCaptureAttributes.ContentState(
                phase: phase,
                startedAt: take.startedAt,
                endedAt: take.endedAt,
                reason: reason,
                heard: heard
            ),
            staleDate: nil,
            relevanceScore: 100
        )
    }

    private func update(
        _ take: Take,
        phase: VoiceCaptureAttributes.Phase,
        reason: VoiceCaptureAttributes.Reason? = nil
    ) async {
        await take.activity.update(content(for: take, phase: phase, reason: reason))
    }

    private func end(
        _ take: Take,
        discardAudio: Bool,
        dismissal: ActivityUIDismissalPolicy,
        phase: VoiceCaptureAttributes.Phase? = nil,
        reason: VoiceCaptureAttributes.Reason? = nil,
        heard: String? = nil
    ) async {
        take.client.close()
        if discardAudio {
            take.recorder.discard()
        }
        let finalContent = phase.map { content(for: take, phase: $0, reason: reason, heard: heard) }
        await take.activity.end(finalContent, dismissalPolicy: dismissal)
    }

    private func endActivity(
        _ activity: Activity<VoiceCaptureAttributes>,
        phase: VoiceCaptureAttributes.Phase,
        reason: VoiceCaptureAttributes.Reason,
        dismissal: ActivityUIDismissalPolicy
    ) async {
        let now = Date()
        await activity.end(
            ActivityContent(
                state: VoiceCaptureAttributes.ContentState(
                    phase: phase, startedAt: now, endedAt: now, reason: reason
                ),
                staleDate: nil
            ),
            dismissalPolicy: dismissal
        )
    }

    /// A short notice for a take that never started (the instance is
    /// offline, older, or has no voice input).
    private func showFailureNotice(_ reason: VoiceCaptureAttributes.Reason) {
        let now = Date()
        let content = ActivityContent(
            state: VoiceCaptureAttributes.ContentState(
                phase: .failed, startedAt: now, endedAt: now, reason: reason
            ),
            staleDate: nil
        )
        do {
            let activity = try Activity.request(
                attributes: VoiceCaptureAttributes(botName: nil),
                content: content
            )
            Task {
                await activity.end(
                    content,
                    dismissalPolicy: .after(now.addingTimeInterval(Self.needsAppDismissal))
                )
            }
        } catch {
            record("failure notice failed: \(error)")
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

    // MARK: Diagnostics

    private func record(_ event: String) {
        Self.logger.info("\(event, privacy: .public)")
        VoiceCaptureProbe.record(event)
    }
}

/// Debug builds append each event, with the App's state, to a file that the
/// Simulator CI probe collects. Release builds record nothing here.
@MainActor
enum VoiceCaptureProbe {
    static func record(_ event: String) {
        #if DEBUG
        let appState: String
        switch UIApplication.shared.applicationState {
        case .active: appState = "active"
        case .inactive: appState = "inactive"
        case .background: appState = "background"
        @unknown default: appState = "unknown"
        }
        let line = "\(ISO8601DateFormatter().string(from: Date())) [\(appState)] \(event)\n"
        guard
            let directory = try? FileManager.default.url(
                for: .cachesDirectory, in: .userDomainMask,
                appropriateFor: nil, create: true
            )
        else { return }
        let url = directory.appending(path: "voice-capture-probe.log")
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(Data(line.utf8))
            try? handle.close()
        } else {
            try? Data(line.utf8).write(to: url)
        }
        #endif
    }
}
