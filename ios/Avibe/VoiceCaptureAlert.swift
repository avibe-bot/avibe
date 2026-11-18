import SwiftUI
import UIKit

/// Says why "Talk to Main Bot" brought Avibe to the front instead of
/// listening, in the words the island would have used.
@MainActor
enum VoiceCaptureAlert {
    /// The alert's text: for a Live Activity that would not start, the
    /// failure line with its step and code (the notification's line, which
    /// may not show if notifications are off).
    static func message(
        _ reason: VoiceCaptureAttributes.Reason,
        failure: VoiceCaptureAttributes.ContentState?
    ) -> String {
        if reason == .activityFailed, let failure, failure.failedStep != nil {
            return String(localized: VoiceCaptureCopy.reasonLine(
                .recordingFailed, errorCode: failure.errorCode, step: failure.failedStep
            ))
        }
        return String(localized: VoiceCaptureCopy.reason(reason))
    }

    static func present(
        _ reason: VoiceCaptureAttributes.Reason,
        failure: VoiceCaptureAttributes.ContentState? = nil,
        attempt: Int = 0
    ) {
        guard let presenter = topViewController() else {
            // The scene is still coming up after `continueInForeground`.
            guard attempt < 20 else { return }
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(250))
                present(reason, failure: failure, attempt: attempt + 1)
            }
            return
        }
        let alert = UIAlertController(
            title: String(localized: VoiceCaptureCopy.text("voiceCapture.phase.needsApp")),
            message: message(reason, failure: failure),
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(
            title: String(localized: VoiceCaptureCopy.text("voiceCapture.action.ok")),
            style: .default
        ))
        presenter.present(alert, animated: true)
    }

    private static func topViewController() -> UIViewController? {
        let window = UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .filter { $0.activationState == .foregroundActive }
            .flatMap(\.windows)
            .first { $0.isKeyWindow }
        var top = window?.rootViewController
        while let presented = top?.presentedViewController {
            top = presented
        }
        return top
    }
}
