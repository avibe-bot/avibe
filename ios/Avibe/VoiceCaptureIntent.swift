import AppIntents
import Foundation

/// 「和主伙伴说话」: the Action Button (or Shortcuts, Siri, Spotlight) records
/// what the user says without bringing Avibe to the front, and sends it to
/// the main Bot of the selected instance.
///
/// A run while a take is recording stops it and sends it; that is how a
/// second long press of the Action Button ends a take. While a take is
/// being sent, a run does nothing.
///
/// `AudioRecordingIntent` tells the system the App records audio; the
/// system then requires a Live Activity for as long as it records
/// (developer.apple.com/documentation/appintents/audiorecordingintent).
/// It is an `AudioRecordingIntent` only, as in 26.1, the one build whose
/// press started recording in the background on the owner's phone. With
/// `LiveActivityIntent` too (#3923), the system launched it as a background
/// Live Activity start, and the microphone was refused (35.1: 560557684,
/// 561015905). A cold press, whose island cannot start from the
/// background, goes on in the App (try 3) and records there.
@available(iOS 18.0, *)
struct SpeakToMainBotIntent: AudioRecordingIntent {
    static let title: LocalizedStringResource = "Talk to Main Bot"
    static let description = IntentDescription(
        "Records what you say and sends it to your main Bot without opening Avibe."
    )

    /// The main Bot can act on the user's computer, so a locked phone must
    /// not send it anything: the system asks for Face ID first.
    static let authenticationPolicy: IntentAuthenticationPolicy =
        .requiresAuthentication

    /// Run in the background; come to the front only when the App is needed
    /// (sign-in, microphone permission, voice-input consent).
    @available(iOS 26.0, *)
    static var supportedModes: IntentModes {
        [.background, .foreground(.dynamic)]
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        let controller = VoiceCaptureController.shared
        var canComeForward = false
        if #available(iOS 26.0, *) {
            canComeForward = systemContext.currentMode.canContinueInForeground
        }
        let outcome = await controller.toggle(foregroundFallback: canComeForward)
        if outcome == .needsForeground {
            // Every background try was refused: try 3 records in the App
            // at once, with one line saying why it opened.
            if #available(iOS 26.0, *) {
                do {
                    try await continueInForeground(alwaysConfirm: false)
                    let before = controller.lastFailure
                    let notices = controller.noticeCount
                    switch await controller.startInForeground() {
                    case .started:
                        VoiceCaptureBanner.show(String(localized: VoiceCaptureCopy.text("voiceCapture.startedInApp")))
                    case .needsApp(let reason):
                        // Never in front without a word.
                        controller.openMainBotChat()
                        VoiceCaptureAlert.present(reason, failure: controller.lastFailure)
                    default:
                        // Try 3 refused too, or the press waited too long:
                        // the notice is not shown in front, so say it here.
                        if let failure = controller.lastFailure, failure != before {
                            VoiceCaptureAlert.present(.recordingFailed, failure: failure)
                        } else if controller.noticeCount != notices, let reason = controller.lastNoticeReason {
                            // Its own status check refused (offline, …).
                            VoiceCaptureAlert.present(reason)
                        }
                    }
                    return .result()
                } catch {
                    // The system declined; the notice names the tries.
                }
            }
            controller.foregroundUnavailable()
            return .result()
        }
        guard case .needsApp(let reason) = outcome else {
            return .result()
        }
        if #available(iOS 26.0, *),
           systemContext.currentMode.canContinueInForeground
        {
            do {
                try await continueInForeground(alwaysConfirm: false)
                controller.openMainBotChat()
                // Never in front without a word: say why it came forward.
                VoiceCaptureAlert.present(reason, failure: controller.lastFailure)
                return .result()
            } catch {
                // The system declined; the island says to open Avibe.
            }
        }
        controller.showNeedsApp(reason)
        return .result()
    }
}
