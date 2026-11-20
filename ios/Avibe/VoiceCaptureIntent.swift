import AppIntents
import Foundation

/// 「和主伙伴说话」: the Action Button (or Shortcuts, Siri, Spotlight) records
/// what the user says and sends it to the main Bot of the selected instance:
/// in the background when iOS allows it, else with Avibe brought forward.
///
/// A run while a take is recording stops it and sends it; that is how a
/// second long press of the Action Button ends a take. While a take is
/// being sent, a run does nothing.
///
/// `AudioRecordingIntent` tells the system the App records audio; the
/// system then requires a Live Activity for as long as it records
/// (developer.apple.com/documentation/appintents/audiorecordingintent).
/// It is also a `LiveActivityIntent`: only then may it start its island
/// from the background, warm or cold (developer.apple.com/documentation/
/// appintents/liveactivityintent; the 11-19 sample: without it the request
/// fails with `visibility`, as on the owner's 37.1).
@available(iOS 18.0, *)
struct SpeakToMainBotIntent: AudioRecordingIntent, LiveActivityIntent {
    static let title: LocalizedStringResource = "Talk to Main Bot"
    static let description = IntentDescription(
        "Records what you say and sends it to your main Bot. Avibe may open to record."
    )

    /// The main Bot can act on the user's computer, so a locked phone must
    /// not send it anything: the system asks for Face ID first.
    static let authenticationPolicy: IntentAuthenticationPolicy =
        .requiresAuthentication

    /// Starts in the background; comes forward only when the background
    /// try is refused (or the App must ask for something).
    @available(iOS 26.0, *)
    static var supportedModes: IntentModes {
        [.background, .foreground(.dynamic)]
    }

    /// What an alert says when a start in front ended without recording:
    /// the start's failure if it is new since `before` (read before the
    /// press's first start), else the notice shown since, else nothing.
    static func stoppedAlert(
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

    @MainActor
    func perform() async throws -> some IntentResult {
        let controller = VoiceCaptureController.shared
        // A stop, a repeated run or a leftover is handled in the background.
        if await controller.beginPress() != nil {
            return .result()
        }
        let press = controller.reservation
        var canComeForward = false
        if #available(iOS 26.0, *) {
            canComeForward = systemContext.currentMode.canContinueInForeground
        }
        // Avibe already in front records there at once, with the session
        // that pauses other audio (39.1: the mixable one recorded the music
        // playing from the speaker). Otherwise one background try first
        // (island, a mixable session, the recorder); if iOS refuses it,
        // Avibe comes forward and records there as soon as it is active.
        let active = controller.isInFront
        // Taken before any start: a start in front that fails is said in
        // an alert (the island is not drawn over the App), and that is told
        // by the failure being new.
        let before = controller.lastFailure
        let notices = controller.noticeCount
        var outcome = await controller.startTake(
            inFront: active, foregroundFallback: !active && canComeForward, reservation: press
        )
        var inFront = active
        if outcome == .needsForeground || (canComeForward && !inFront && outcome.isNeedsApp) {
            if #available(iOS 26.0, *) {
                do {
                    try await continueInForeground(alwaysConfirm: false)
                    inFront = true
                    controller.markInFront(reservation: press)
                } catch {
                    // The system declined.
                }
            }
            if outcome == .needsForeground {
                guard inFront else {
                    controller.foregroundUnavailable(reservation: press)
                    return .result()
                }
                outcome = await controller.startTake(inFront: true, reservation: press)
            }
        }
        switch outcome {
        case .started, .busy, .needsForeground:
            break
        case .needsApp(let reason):
            if inFront {
                // Never in front without a word.
                controller.openMainBotChat()
                VoiceCaptureAlert.present(reason, failure: controller.lastFailure)
            } else {
                controller.showNeedsApp(reason)
            }
        case .stopped:
            // In front the island is not drawn, so say a failure here.
            guard inFront else { break }
            if let alert = Self.stoppedAlert(
                before: before, after: controller.lastFailure,
                noticesBefore: notices, noticesAfter: controller.noticeCount,
                lastNoticeReason: controller.lastNoticeReason
            ) {
                VoiceCaptureAlert.present(alert.reason, failure: alert.failure)
            }
        }
        return .result()
    }
}
