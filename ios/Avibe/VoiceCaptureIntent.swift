import AppIntents
import Foundation
import UIKit

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
/// It is also a `LiveActivityIntent`: an App that is not in front may start
/// a Live Activity only from such an intent
/// (developer.apple.com/documentation/appintents/liveactivityintent), and a
/// press that finds no running process must still be able to.
@available(iOS 18.0, *)
struct SpeakToMainBotIntent: AudioRecordingIntent, LiveActivityIntent {
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
        #if DEBUG
        VoiceCaptureProbeLog.record("intent: perform, app state \(UIApplication.shared.applicationState.rawValue)")
        #endif
        let outcome = await controller.toggle()
        #if DEBUG
        VoiceCaptureProbeLog.record("intent: outcome \(outcome)")
        #endif
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
                VoiceCaptureAlert.present(reason)
                return .result()
            } catch {
                // The system declined; the island says to open Avibe.
            }
        }
        controller.showNeedsApp(reason)
        return .result()
    }
}
